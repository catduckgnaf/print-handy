"""Source-first Discord intake. Downloads, records choices, then one button slices and optionally prints.

Printing starts only from a button click that confirms the bed is clear. No typed approval is required.

Colour questions follow the job printer's adapter (see the coupling map in
print_handy/printers/base.py): IFS printers get slot labels; a multi_tool
printer (Creator 5 / 5 Pro, #346) gets one question per colour with toolhead
labels, a toolhead already assigned a different colour is not offered again,
and more distinct colours than toolheads is refused before asking."""
import asyncio
import contextlib
import contextvars
import hashlib
import json
import logging
import re
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from typing import NamedTuple
from urllib.parse import urlsplit

from .. import diagnostics, logs, printers, settings
from . import _external, delivery, owned_worker, plate_merge, split_fit

BED = settings.current()['printer']['bed_mm']
FILAMENT_SLOTS = settings.current()['printer']['filament_slots']
print_summary = _external.lazy('discord_print_summary')
print_next = _external.lazy('print_next')
from .link_intake import ask, model_link, has_duration_question  # noqa: E402
from .quantity_selection import quantity_options, parse_quantity  # noqa: E402
from .background_slice import drop_runner, runner_for, slice_binding  # noqa: E402
from .slice_recipe import RECIPE_VERSION, binding_sha256  # noqa: E402


_OFFLOAD = {('model_job', 'audit'), ('model_job', 'slice_job'), ('source_colors', 'inventory'),
            ('orca_adapter', 'orca_plate_capacity'), ('orca_adapter', 'fit_options'),
            ('model_job', 'per_part_fit_options'), ('plate_merge', 'full_size_split_possible'),
            ('geometry_layout', 'inspect_geometry'), ('geometry_layout', 'arrange_and_export'),
            ('geometry_layout', 'probe_single_plate_rearrange'), ('model_job', 'full_size_one_plate'),
            ('cad_split', 'check_source_against_split_plan'), ('cad_split', 'prepare_cad_split'),
            ('model_parts', 'combine')}
# Steps that run the slicer share one lane; the geometry-only steps share a small general lane.
_SLICER_STEPS = {('model_job', 'slice_job'), ('orca_adapter', 'orca_plate_capacity'), ('orca_adapter', 'fit_options')}
_STEP_LABELS = {'slice_job': 'slicing', 'audit': 'the model check', 'inventory': 'the color check',
                'orca_plate_capacity': 'the bed fit check', 'fit_options': 'the bed fit check',
                'per_part_fit_options': 'the per-part fit check', 'full_size_split_possible': 'the split check',
                'inspect_geometry': 'the geometry check', 'arrange_and_export': 'the layout',
                'probe_single_plate_rearrange': 'the layout check', 'combine': 'combining the files',
                'check_source_against_split_plan': 'the split check', 'prepare_cad_split': 'the split'}


logger = logging.getLogger(__name__)

STOP_LABEL = "Stop"
STOP_CUSTOM_ID = "pbstop:"
RETRY_CUSTOM_ID = "pbretry:"
# Set by the print-feedback plugin: async (source, text, clarify_id) -> sent message with a single
# Stop button. None (tests, no Discord bot) falls back to the gateway clarify prompt.
STOP_SENDER = None
# Set by the plugin to edit the original Uploading/Stop prompt and install Retry after a safe refusal.
REFUSAL_EDITOR = None
# Set by the plugin on the clarify-UI path only (#524 stage 2, S6), where the standing prompt is
# Print Handy's own question message rather than a Stop view: async
# (source, channel_id, message_id, text, context, raw) -> bool, editing that message by Discord
# id into plain guidance plus Retry after a safe refusal.
REFUSAL_EDITOR_BY_ID = None


class Stopped(BaseException):
    """The operator pressed Stop on a progress message."""

    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


async def _with_stop(adapter, source, key, metadata, text, work, *, allow_stop=False, refusal_context=None):
    """Run work with plain progress by default; opt in to Stop only when cancellation is safe."""
    adapter_state = getattr(adapter, "__dict__", {})
    if "picks" in adapter_state or "clicks" in adapter_state:
        return await work
    if not allow_stop:
        if not text:
            return await work
        status = text
        for suffix in (" Press Stop to cancel before it starts.", " Press Stop to cancel."):
            if status.endswith(suffix):
                status = status[:-len(suffix)]
                break
        try:
            await adapter.send(chat_id=str(source.chat_id), content=status, metadata=metadata)
        except Exception:
            pass  # progress delivery must not block offline work
        return await work
    import uuid
    from .prompts import gateway as _prompts
    cm = _prompts()
    cid = uuid.uuid4().hex
    cm.register(cid, key, text, [STOP_LABEL])
    stop_msg = None
    if STOP_SENDER is not None:
        # Plugin-owned one-button view: a status step offers Stop only, never "Other (type answer)".
        try:
            stop_msg = await STOP_SENDER(source, text, cid)
        except Exception:
            stop_msg = None
    if stop_msg is not None:
        sent = SimpleNamespace(success=True)
    else:
        sent = await adapter.send_clarify(chat_id=str(source.chat_id), question=text, choices=[STOP_LABEL],
                                          clarify_id=cid, session_key=key, metadata=metadata)
    if not getattr(sent, "success", True):
        cm.resolve_gateway_clarify(cid, cm.CANCELLED)
        return await work
    task = asyncio.ensure_future(work)
    waiter = asyncio.ensure_future(asyncio.to_thread(cm.wait_for_response, cid, 3600))
    result = None
    try:
        # Race the work against the Stop button. The work finishing must never wait on a press:
        # awaiting the button first froze every fit check until someone pressed Stop (2.0.6).
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if not task.done() and waiter.done() and waiter.result() == STOP_LABEL:
            task.cancel()
            try:
                result = await task
            except asyncio.CancelledError:
                raise Stopped(text)
            if isinstance(result, tuple) and len(result) >= 2 and isinstance(result[1], str):
                if result[1].startswith("UNCERTAIN:"):
                    raise Stopped(result[1], uncertain=True)
            # A coroutine that suppresses cancellation and returns has completed with a
            # definitive outcome. Preserve it instead of claiming a false cancellation.
            return result
        result = await task
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        cm.resolve_gateway_clarify(cid, cm.CANCELLED)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(waiter, 5)
        cm.wait_for_response(cid, 0.001)
        if stop_msg is not None:
            # The Stop button is dead once the work returns: retire it, and after a confirmed
            # pre-upload refusal turn the same prompt into plain guidance plus Retry.
            handled = False
            if (refusal_context is not None and isinstance(result, tuple) and len(result) >= 2
                    and not result[0] and _safe_preupload_refusal(result[1])):
                detail = str(result[1])
                refusal_context['start_prompt_message_id'] = str(getattr(stop_msg, 'id', '') or '')
                if REFUSAL_EDITOR is not None:
                    with contextlib.suppress(Exception):
                        handled = bool(await REFUSAL_EDITOR(source, stop_msg, _refusal_text(detail),
                                                            refusal_context, detail))
                if not handled:
                    with contextlib.suppress(Exception):
                        await stop_msg.edit(content=_refusal_text(detail), view=None)
                        handled = True
                refusal_context['prompt_handled'] = handled
            if not handled:
                with contextlib.suppress(Exception):
                    await stop_msg.edit(view=None)
        elif sent is not None and getattr(sent, "message_id", None):
            # Clarify path (#524 stage 2, S6): the standing prompt is Print
            # Handy's own question message — it carries a message id and
            # there is no Stop view to retire. Complete it in place: after
            # a confirmed pre-upload refusal it becomes the guidance-plus-
            # Retry message (edited by id, mirroring the Stop path's
            # outcome); otherwise its buttons are simply retired through
            # the adapter, the same end state a click produces.
            handled = False
            if (refusal_context is not None and isinstance(result, tuple) and len(result) >= 2
                    and not result[0] and _safe_preupload_refusal(result[1])):
                detail = str(result[1])
                refusal_context['start_prompt_message_id'] = str(sent.message_id)
                if REFUSAL_EDITOR_BY_ID is not None:
                    with contextlib.suppress(Exception):
                        handled = bool(await REFUSAL_EDITOR_BY_ID(
                            source, str(getattr(source, "thread_id", None) or source.chat_id),
                            str(sent.message_id), _refusal_text(detail),
                            refusal_context, detail))
                refusal_context['prompt_handled'] = handled
            if not handled:
                retire = getattr(adapter, "retire_prompt", None)
                if retire is not None:
                    with contextlib.suppress(Exception):
                        await retire(cid)
    return result


# Fit-check progress: one status line, refreshed while a check runs. When the
# chat adapter can edit messages (Discord, Matrix, Telegram) the same message is
# updated; otherwise a new line is posted only when the phase changes or, for a
# long phase, at most once per FIT_FALLBACK_HEARTBEAT_S.
FIT_HEARTBEAT_S = 15.0
FIT_FALLBACK_HEARTBEAT_S = 60.0


def _elapsed_text(seconds):
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f'{seconds} s'
    minutes, rest = divmod(seconds, 60)
    return f'{minutes} min {rest} s' if rest else f'{minutes} min'


class FitStatus:
    """A single, informative status message for the bed-fit checks.

    ``run(phase, work)`` shows what is being tried and how long the fit check
    has taken so far, refreshes that every FIT_HEARTBEAT_S while the work
    runs, and never repeats identical text. Delivery failures never block
    the check.
    """

    def __init__(self, adapter, source, metadata, clock=time.monotonic):
        self.adapter, self.source, self.metadata, self.clock = adapter, source, metadata, clock
        self.started = clock()
        self.message_id = None
        self.last_text = None
        self.last_post = None
        adapter_state = getattr(adapter, '__dict__', {})
        # Scripted test adapters (picks/clicks) record questions only, as _with_stop does.
        self.silent = 'picks' in adapter_state or 'clicks' in adapter_state
        self.can_edit = callable(getattr(adapter, 'edit_message', None))

    def elapsed(self):
        return _elapsed_text(self.clock() - self.started)

    def _target(self):
        thread = (self.metadata or {}).get('thread_id') or getattr(self.source, 'thread_id', None)
        return str(thread or self.source.chat_id)

    async def show(self, text, heartbeat=False):
        if self.silent or not text or text == self.last_text:
            return
        if self.message_id is not None and self.can_edit:
            try:
                result = await self.adapter.edit_message(chat_id=self._target(), message_id=self.message_id,
                                                         content=text)
            except Exception:
                result = None
            if result is not None and getattr(result, 'success', False):
                self.last_text = text
                return
            self.can_edit = False  # this adapter or message cannot be edited: post instead
        if heartbeat and self.last_post is not None and self.clock() - self.last_post < FIT_FALLBACK_HEARTBEAT_S:
            return
        try:
            sent = await self.adapter.send(chat_id=str(self.source.chat_id), content=text, metadata=self.metadata)
        except Exception:
            return  # progress delivery must not block offline work
        self.last_text, self.last_post = text, self.clock()
        message_id = getattr(sent, 'message_id', None)
        if message_id:
            self.message_id = str(message_id)

    async def run(self, phase, work, first_text=None):
        """Await ``work`` while the status names ``phase`` and the time spent so far."""
        await self.show(first_text or f'Checking the fit: {phase} ({self.elapsed()}).')
        task = asyncio.ensure_future(work)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=FIT_HEARTBEAT_S)
                if done:
                    return task.result()
                await self.show(f'Still checking the fit: {phase} ({self.elapsed()}).', heartbeat=True)
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def finish(self, outcome):
        """Close the status line with the result and total time, only if one was shown."""
        if self.last_text is not None:
            await self.show(f'Fit check done in {self.elapsed()}: {outcome}')


def _auto_rearrange_eligible(job, latest):
    """A 100% one-plate rearrangement may be applied without asking.

    Not when the operator already fixed a size or orientation for this job, and
    not for an inch-confirmed STL (its capacity was probed at authored size).
    """
    if (job.get('unit_decision') or {}).get('units') == 'inch':
        return False
    scale = latest.get('scale')
    if scale is not None and abs(float(scale) - 1.0) > 1e-9:
        return False
    return not (latest.get('orientation') or latest.get('full_size_split_plan'))


def _single_body(job, manifest):
    """True when the model is one object in one connected shell: nothing to rearrange or split.

    Conservative: any missing measurement (no audit, unknown shell count, a
    multi-part or multi-plate project) answers False so the full search runs.
    """
    audit = job.get('audit') if isinstance(job.get('audit'), dict) else {}
    mesh = audit.get('mesh_audit') if isinstance(audit.get('mesh_audit'), dict) else {}
    if mesh.get('shells') != 1:
        return False
    if len((manifest or {}).get('parts') or []) > 1:
        return False
    return int(audit.get('plates') or 1) <= 1 and int(audit.get('objects') or 1) <= 1


def audit_dimensions(job):
    """Measured [x, y, z] mm from the model audit, else None."""
    audit = job.get('audit') if isinstance(job.get('audit'), dict) else {}
    mesh = audit.get('mesh_audit') if isinstance(audit.get('mesh_audit'), dict) else {}
    dims = mesh.get('dimensions_mm')
    if isinstance(dims, (list, tuple)) and len(dims) >= 3 and all(isinstance(v, (int, float)) for v in dims[:3]):
        return [float(v) for v in dims[:3]]
    return None


def auto_rearrange_notice(title, plan, printer=None):
    """One line saying what was done, distinguishing the author's layout from ours."""
    count = int(plan.get('part_count') or 0)
    parts = f'the {count} parts' if count > 1 else 'the parts'
    verb = 'rotated and rearranged' if int(plan.get('turned_parts') or 0) else 'rearranged'
    return (f"{title} does not fit the {printer or _printer_name()} bed as the author laid it out, "
            f"so I {verb} {parts} onto one plate at 100%.")


LARGE_MESH_MB = 20


def large_model_notice(path):
    """A heads-up for meshes big enough that the model check takes minutes, else None."""
    import zipfile
    try:
        path = Path(path)
        if path.suffix.lower() == '.3mf':
            with zipfile.ZipFile(path) as z:
                size = sum(i.file_size for i in z.infolist() if i.filename.lower().endswith('.model'))
        else:
            size = path.stat().st_size
    except (OSError, zipfile.BadZipFile):
        return None
    mb = size / 1e6
    if mb < LARGE_MESH_MB:
        return None
    minutes = max(1, round(mb / 45))
    return (f'This is a large model ({mb:.0f} MB of mesh). Checking it takes about '
            f'{minutes} minute{"s" if minutes != 1 else ""}, so it is not stuck.')


async def _heavy(func, *args, **kwargs):
    """Run CPU-heavy intake work in a child process (own GIL) so Discord buttons answer in time.

    Threads are not enough: large 3MF XML parsing holds the GIL for ~10 s stretches and starves the
    gateway loop past Discord's 3 s interaction deadline. Substituted callables (tests) run in a thread.
    The child is owned: it has a deadline and an output limit, and a cancel stops its process group.
    """
    import importlib
    mod, name = getattr(func, '__module__', None), getattr(func, '__name__', None)
    short = (mod or '').rsplit('.', 1)[-1]
    real = (short, name) in _OFFLOAD and getattr(importlib.import_module(mod), name, None) is func
    if not real:
        return await owned_worker.in_thread(func, *args, **kwargs)
    def plain(v):
        return str(v) if isinstance(v, Path) else v
    payload = json.dumps({'module': short, 'func': name, 'args': [plain(a) for a in args],
                          'kwargs': {k: plain(v) for k, v in kwargs.items()}}).encode()
    slicer = (short, name) in _SLICER_STEPS
    # A slice worker leaves a record in its intake folder, so a restart mid-slice can be explained.
    record = Path(args[0]).parent / owned_worker.RECORD if (short, name) == ('model_job', 'slice_job') else None
    out, err = await owned_worker.run(
        [sys.executable, '-m', 'print_handy.core.heavy_worker'], payload, label=_STEP_LABELS.get(name, name),
        lane=owned_worker.SLICER if slicer else owned_worker.GENERAL, cwd=str(settings.ROOT), record=record)
    try:
        reply = json.loads(out.decode().rstrip().rsplit('\n', 1)[-1])
    except (ValueError, IndexError):
        detail = err.decode(errors="replace")[-400:].strip() or 'it stopped without reporting why (killed or crashed)'
        raise RuntimeError(f'{name} worker failed: {detail}') from None
    if not reply.get('ok'):
        from .worker_errors import decode
        raise decode(reply)
    return Path(reply['result']) if reply.get('path') else reply['result']


def write_split_plan(job_path, job, manifest, gcodes, plan_id, plans_dir=None):
    """Record a split slice as a multipart plan that /print-next can resolve exactly.

    Each plate gets one required part bound to its archived JOB.json and absolute
    G-code path. The plan id is also stored in JOB.json so record_start can put
    it in the print context and completions advance the plan.
    """
    print_records = _external.load('print_records')
    names = {p['key']: re.sub(r'\.(stl|3mf)$', '', p['name'], flags=re.I) for p in manifest['parts']}
    rows = {str(Path(r['gcode']).resolve()): r for r in job.get('derived_plates') or [] if r.get('gcode')}
    parts = []
    # Per-plate estimates in derived_plates order (plates_of keeps that order), keyed by G-code.
    by_gcode = {}
    derived = [r for r in job.get('derived_plates') or []]
    for r, est in zip(derived, _external.load('discord_print_summary').plate_estimates(job)):
        if r.get('gcode'):
            by_gcode[str(Path(r['gcode']).resolve())] = est
    for n, g in enumerate(gcodes, 1):
        row = rows.get(str(Path(g).resolve()), {})
        est = by_gcode.get(str(Path(g).resolve())) or {}
        label = ', '.join(names.get(k, k) for k in row.get('part_keys') or []) or Path(g).stem
        if row.get('slot') is not None:
            label += f" (slot {row['slot']})"
        parts.append({'id': f'plate{n}', 'label': label, 'plate': n, 'quantity': 1, 'completed_quantity': 0,
                      'status': 'planned', 'gcode_file': Path(g).name, 'job_json': str(Path(job_path).resolve()),
                      'gcode_path': str(Path(g).resolve())})
        # One part per plate, so the part and its plate share the plate's G-code time; unknown stays absent.
        if est.get('seconds') is not None:
            parts[-1]['estimated_part_seconds'] = parts[-1]['plate_estimate_seconds'] = est['seconds']
        if est.get('filament_g') is not None:
            parts[-1]['estimated_filament_g'] = est['filament_g']
    merge = job.get('plate_merge') or {}
    reason = merge.get('reason') or ('the chosen slots were sliced as separate single-color plates'
                                     if not merge else 'the merge planner could not place them on one bed')
    plan = {'plan_id': plan_id, 'title': job.get('title'), 'split_reason': reason, 'parts': parts}
    target = (plans_dir or print_records.PLANS_DIR) / f'{plan_id}.json'
    print_records._atomic_write(target, json.dumps(plan, indent=2) + '\n')
    latest = json.loads(Path(job_path).read_text())
    latest['multipart_plan'] = {'plan_id': plan_id, 'split_reason': reason, 'parts': parts}
    from . import model_job as mj
    mj.write_job(Path(job_path).parent, latest)  # atomic; JOB.json is read concurrently
    return plan


def _preview_description(text):
    """Brief source-derived description: first sentence, no URLs, paths, filenames or hashes."""
    text = re.sub(r'https?://\S+', ' ', str(text or ''))
    words = [w for w in text.split()
             if '/' not in w and '\\' not in w and not re.search(r'\.(?:3mf|stl|gcode|step|obj|zip)\b', w, re.I)
             and not re.fullmatch(r'[0-9a-fA-F]{16,}', w)]
    text = ' '.join(words).strip()
    first = re.split(r'(?<=[.!?])\s', text, maxsplit=1)[0].strip().rstrip('.!?:;,- ')
    if len(first) > 120:
        # Cut at a word boundary, drop dangling short words ('so I extended the') and mark the cut.
        words = first[:121].rsplit(' ', 1)[0].split()
        while len(words) > 1 and words[-1].lower().strip('.,;:-') in _DANGLING_WORDS:
            words.pop()
        first = ' '.join(words).rstrip('.,;:- ') + '…'
    return first


_DANGLING_WORDS = frozenset(
    'a an the and or but so to of for in on at by with from as that which who my your his her its our their '
    'i we you he she it they is are was were be this these those'.split())


def _end_sentence(text):
    """Close a sentence with a full stop unless it already ends with the truncation ellipsis."""
    return text if text.endswith('…') else text + '.'


_QUESTION_PREFIX = re.compile(r'^Choose (?:the |one )?(?:loaded )?(?:slot/)?color/material for ')


def _choice_summary(manifest, questions, choices, color_of):
    """Pre-slice summary of per-part choices, identical parts grouped.

    '2× frameLeg: Matte white PLA · seat-brick: Pink PLA'. A size is added only where two groups
    would otherwise show the same part name.
    """
    from .source_colors import short_part_name
    from .part_translator import size_hint
    parts = {'part:' + p['key']: p for p in manifest.get('parts', [])}
    questions_by_key = {row['key']: row.get('question', '') for row in questions}
    groups = {}
    for key, selection in choices.items():
        part = parts.get(key)
        if part is not None:
            # 'Tall post ("Part 1")' -> 'Tall post': the CAD name was for the question; the size
            # below tells same-named groups apart.
            name, hint = re.sub(r'\s*\("[^"]*"\)$', '', short_part_name(part)), size_hint(part)
        else:  # painted region rows and anything unexpected: the question's subject
            name, hint = _QUESTION_PREFIX.sub('', questions_by_key.get(key, '')).split(' (')[0] or key, ''
        group = (name, hint, color_of(selection))
        groups[group] = groups.get(group, 0) + 1
    hints_per_name = {}
    for name, hint, _ in groups:
        hints_per_name.setdefault(name, set()).add(hint)
    out = []
    for (name, hint, color), count in groups.items():
        label = f'{name} ({hint})' if hint and len(hints_per_name[name]) > 1 else name
        out.append(f"{f'{count}× ' if count > 1 else ''}{label}: {color}")
    return ' · '.join(out)


def _preview_estimate(row):
    """(seconds, grams) only from a source estimate explicitly for the AD5X; else None. Never guessed."""
    printer = re.sub(r'[^a-z0-9]', '', str((row or {}).get('site_printer') or '').lower())
    plates = (row or {}).get('plates')
    if not ('ad5x' in printer or 'adventurer5x' in printer) or not isinstance(plates, list) or not plates:
        return None
    seconds = grams = 0.0
    for plate in plates:
        if not isinstance(plate, dict):
            return None
        s, g = plate.get('prediction_s'), plate.get('weight_g')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v < 1e7 for v in (s, g)):
            return None
        seconds += float(s)
        grams += float(g)
    return seconds, grams


def _preview_profile(row):
    """The exact profile the intake will download, in a Discord-safe sentence fragment."""
    row = row or {}
    label = str(row.get('title') or row.get('name') or '').strip()
    label = re.sub(r'\s+', ' ', label)
    # A bare source filename is still the profile name; drop only its model extension.
    label = re.sub(r'\.(?:3mf|stl|step|stp|obj|gcode|zip)$', '', label, flags=re.I).strip()
    if not row.get('title') and re.fullmatch(r'(?:model|print|file)?[_-]?[0-9a-f]{6,}', label, re.I):
        return ''
    return _preview_description(label)


def _printer_name(job=None):
    """The selected printer's name for user-facing text; never a hard-coded model name (#273).

    For the configured printer this is its display_name (the configured
    [printer] name, #267); for a job recorded for a different model it is
    that model's label, because display_name would leak the configured
    printer's name from the shared settings. A job that names its roster
    printer (#335) is announced by that entry's name — the default printer
    is named wherever its output stands in for "the printer".
    """
    try:
        pid = job.get("printer_id") if isinstance(job, dict) else None
        if pid:
            from .. import settings
            return settings.by_id(pid).get("name") or str(pid)
    except Exception:
        pass
    try:
        from . import model_job as mj
        from .. import printers
        adapter = mj._job_adapter(job if isinstance(job, dict) else None)
        return adapter.display_name if adapter.key == printers.current().key else adapter.label
    except Exception:
        from .. import printers
        return printers.current().display_name


def preview_sentence(job, row, filament_note=None):
    title = re.sub(r'\s+', ' ', str((job or {}).get('title') or '')).strip()
    if not title:
        return None
    desc = _preview_description((job or {}).get('summary'))
    profile = _preview_profile(row)
    est = _preview_estimate(row)
    named_profile = bool(profile and str((row or {}).get('title') or '').strip())
    if not desc and not est and not named_profile and not filament_note:
        return None  # a bare title or filename echo adds nothing; a named profile does
    text = _end_sentence(f'That looks like {title}' + (f', {desc}' if desc else ''))
    if profile and (row or {}).get('kind') == 'parts':
        text += _end_sentence(f' I will print these files together: {profile}')
    elif profile and profile.casefold() != title.casefold():
        # Only MakerWorld rows are print profiles; the file-list sites list files.
        what = 'profile' if (job or {}).get('site') == 'makerworld' else 'file'
        text += _end_sentence(f' I will use the selected {what}: {profile}')
    if filament_note:
        text += f' Filament: {filament_note}.'
    if est:
        from . import model_job as mj
        text += (f' {_printer_name(job)} estimated print time about {mj.human_dur(est[0])}, plus ~5 min '
                 f'warmup ({mj.human_dur(est[0] + 300)} total), and {round(est[1])} g of filament before color selection.')
    else:
        from . import model_job as mj
        site = mj.source_site_estimate(row)
        if site:
            plates = len((row or {}).get('plates') or [])
            text += (f" Model page estimate: about {mj.human_dur(site[0])}"
                     f" and {round(site[1])} g of filament{f' over {plates} plates' if plates > 1 else ''}."
                     f' The {_printer_name(job)} figure after slicing will differ somewhat.')
        else:
            text += ' The model page gives no time or filament estimate; I will report both after slicing.'
    return text


def _filament_note(job, state):
    """The job printer's own description of the loaded filament and its profile (#264), when it has one.

    Display only: printers without a filament_summary (every non-Bambu printer today) add nothing.
    """
    try:
        from . import model_job as mj
        printer = mj._job_adapter(job if isinstance(job, dict) else None)
        describe = getattr(printer, "filament_summary", None)
        return describe(state) if describe else None
    except Exception:
        return None


async def send_preview(adapter, source, job, row, metadata, state=None):
    """Best effort, display only: no printer, Moonraker, Spoolman, HA or persistence I/O."""
    try:
        text = preview_sentence(job, row, _filament_note(job, state) if state is not None else None)
        if text:
            await adapter.send(chat_id=source.chat_id, content=text, metadata=metadata)
    except Exception:
        pass


def _pct(scale):
    return f'{round(scale * 100, 1):g}%'


def fit_choice_rows(options):
    """At most four short choices, with full-size rearrange/split before orientation/shrink."""
    rows = []
    rearrange = options.get('rearrange_100')
    if rearrange:
        rows.append({'kind': 'rearrange', 'label': '100% · rearrange 1 plate',
                     'scale': 1.0, 'rearrange_100': rearrange})
    split_plan = options.get('split_plan')
    if split_plan and len(rows) < 2:
        rows.append({'kind': 'split', 'label': f"100% · split {len(split_plan['plates'])} plates",
                     'plates': split_plan['plates'], 'scale': 1.0,
                     **({'yaw_by_object': split_plan['yaw_by_object']}
                        if split_plan.get('yaw_by_object') else {}),
                     **({'placement_by_object': split_plan['placement_by_object']}
                        if split_plan.get('placement_by_object') else {}),
                     **({'shifts_by_plate': split_plan['shifts_by_plate']}
                        if split_plan.get('shifts_by_plate') else {}),
                     **({'reorientation_by_object': split_plan['reorientation_by_object']}
                        if split_plan.get('reorientation_by_object') else {})})
    elif options.get('full_size_split') and len(rows) < 2:
        rows.append({'kind': 'split', 'label': 'Split plates at 100%', 'scale': 1.0})
    full_size_available = any(row['kind'] in ('split', 'rearrange') for row in rows)
    rotations = options.get('rotations') or []

    def rotate_row(alt, label):
        return {'kind': 'rotate', 'label': label, 'scale': float(alt['scale']),
                'rotation': alt['rotation'], 'axis': alt.get('axis'),
                'dims_full_mm': alt.get('dims_full_mm'), 'supports': bool(alt.get('supports')),
                'brim': bool(alt.get('brim'))}

    full_rotations = 0
    for alt in rotations:
        scale = float(alt['scale'])
        if scale < 0.995 or full_rotations >= (1 if full_size_available else 2):
            continue
        support = ' + supports' if alt.get('supports') else ' · rotated'
        rows.append(rotate_row(alt, f'{_pct(scale)}{support}'))
        full_rotations += 1
        if len(rows) == 2:
            break
    # Below 100%: offer the largest sizes that really fit, best first. A rotation
    # that needs supports is only worth a button when it beats every no-support
    # one, and a plain shrink only when no rotation without supports prints larger.
    partial = [alt for alt in rotations if 0 < float(alt['scale']) < 0.995]
    plain = max((a for a in partial if not a.get('supports')), key=lambda a: float(a['scale']), default=None)
    braced = max((a for a in partial if a.get('supports')), key=lambda a: float(a['scale']), default=None)
    shrink = next((float(v) for v in options.get('scales') or [] if float(v) < 0.995), None)
    offers = []
    if shrink is not None and not (plain and float(plain['scale']) > shrink):
        offers.append({'kind': 'scale', 'label': f'Shrink to {_pct(shrink)}', 'scale': shrink})
    if plain:
        offers.append(rotate_row(plain, f"{_pct(plain['scale'])} · {_rotation_phrase(plain)}"))
    if braced and (not plain or float(braced['scale']) > float(plain['scale'])):
        offers.append(rotate_row(braced, f"{_pct(braced['scale'])} · {_rotation_phrase(braced)} + supports"))
    offers.sort(key=lambda row: -row['scale'])
    for row in offers:
        if len(rows) < 3:
            rows.append(row)
    rows.append({'kind': 'cancel', 'label': 'Cancel'})
    return rows


def _rotation_phrase(alt):
    """Short button words for a rotated option: "turned 126°" or "rotated 90° on Y"."""
    name = str(alt.get('name') or alt.get('label') or '')
    turn = re.match(r'Turn (-?[\d.]+)°', name)
    if turn:
        return f'turned {turn[1]}°'
    tilt = re.match(r'Rotate (-?[\d.]+)° on ([A-Z])', name)
    return f'rotated {tilt[1]}° on {tilt[2]}' if tilt else 'rotated'


FIT_HINT_PREFIX = 'Pick a fit option, or type '


def row_brim_policy(row, options):
    """Brim policy that goes with the chosen fit row, or None to use the model's own rule.

    The tight-fit policy was sized for the authored orientation. A rotated row was
    sized separately: no brim when its bed contact is wide, or the 5 mm
    small-contact brim when it was flagged (that stays with the orientation).
    """
    if row.get('kind') != 'rotate':
        return options.get('brim_policy')
    if row.get('brim'):
        return None
    return {'type': 'no_brim', 'width_mm': 0.0, 'label': 'No brim'}


def fit_hint(rows):
    """The "type ..." hint, naming only actions that are actually offered."""
    kinds = {row['kind'] for row in rows}
    words = []
    if 'rearrange' in kinds:
        words.append('rearrange')
    if 'split' in kinds:
        words.append('split')
    if any(row['kind'] in ('rearrange', 'split') or (row['kind'] in ('rotate', 'painted') and row['scale'] >= 0.995)
           for row in rows):
        words.append('100%')
    if any(row['kind'] == 'scale' or (row['kind'] in ('rotate', 'painted') and row['scale'] < 0.995)
           for row in rows):
        words.append('shrink')
    words.append('cancel')
    return FIT_HINT_PREFIX + ' / '.join(words) + '.'


def _fit_choice_reply(text, rows):
    """Resolve typed fit intent only when it names one available action."""
    value = str(text or '').strip().casefold()
    if value in {'rearrange', 'rearrange 1 plate', 'rearrange plate', 'pack'}:
        matches = [i for i, row in enumerate(rows) if row['kind'] == 'rearrange']
    elif value in {'split', 'separate', 'split plates'}:
        matches = [i for i, row in enumerate(rows) if row['kind'] == 'split'
                   or (row['kind'] == 'painted' and 'split' in row['label'])]
    elif value in {'100%', '100', 'full', 'full size'} or 'full size' in value:
        matches = [i for i, row in enumerate(rows) if row['kind'] in ('rearrange', 'split')
                   or (row['kind'] in ('painted', 'rotate') and row['scale'] >= 1.0)]
    elif value in {'cancel', 'stop', "don't print"}:
        matches = [i for i, row in enumerate(rows) if row['kind'] == 'cancel']
    elif value in {'shrink', 'smaller'}:
        matches = [i for i, row in enumerate(rows) if row['kind'] == 'scale'
                   or (row['kind'] in ('painted', 'rotate') and row['scale'] < 0.995)]
        if len(matches) > 1:  # several smaller sizes are offered: "shrink" means the largest
            matches = [max(matches, key=lambda i: rows[i]['scale'])]
    elif re.fullmatch(r'\d+(?:\.\d+)?\s*%', value):
        percent = float(value.rstrip('%').strip())
        matches = [i for i, row in enumerate(rows)
                   if row['kind'] in ('scale', 'rotate', 'painted') and abs(row['scale'] * 100 - percent) < 0.06]
    elif re.fullmatch(r'\d+(?:\.\d+)?', value):
        percent = float(value)
        matches = [i for i, row in enumerate(rows)
                   if row['kind'] in ('scale', 'rotate', 'painted') and abs(row['scale'] * 100 - percent) < 0.06]
    else:
        return None
    if len(matches) != 1:
        raise ValueError('that fit choice is not available; choose a listed button')
    return matches[0]


def fit_question(title, options, rows=None):
    rows = rows if rows is not None else fit_choice_rows(options)
    scales = options.get('scales') or []
    rotations = options.get('rotations') or []
    parts = options.get('parts') or []
    part_name = options.get('part_name') or (parts[0].get('name') if parts else title)
    extent = options.get('extent_mm') or (parts[0].get('extent_mm') if parts else None)
    extent_text = ' × '.join(f'{v:g}' for v in extent[:3]) + ' mm' if extent else 'measured dimensions unavailable'
    checked = options.get('orientations_checked') or (parts[0].get('orientations_checked') if parts else None)
    orientations = ', '.join(checked or ['authored XY', 'verified rotations'])
    lines = [f'{part_name} ({extent_text}) does not fit the bed full size as the author laid it out.']
    rearrange = options.get('rearrange_100')
    if rearrange:
        count = rearrange.get('part_count')
        lines.append(f"Rearranged: {'all ' + str(count) + ' parts' if count else 'the parts'} "
                     'fit one plate at full size.')
    blocking = options.get('blocking_part') or next(
        (part for part in parts if part.get('fits_full_size') is False), None)
    if blocking:
        # This part is oversized on its own (#76): say so plainly, with the
        # measured limit it was checked against, before offering anything.
        reason = blocking.get('blocking_reason')
        if reason:
            lines.append(f"{blocking.get('name') or part_name} does not fit a plate on its own: {reason}.")
        lines.append('Splitting other parts onto separate plates, or reducing the gap between '
                     'parts, does not shrink this part. Supports can help overhangs but do not '
                     'make a part smaller, and can take more bed space.')
    if scales:
        lines.append(f'Shrink option: as authored it fits at {_pct(scales[0])} max.')
    for alt in rotations[:2]:
        size = ' x '.join(f'{v:g}' for v in alt.get('dims_mm') or [])
        warn = []
        if alt.get('supports'):
            warn.append('needs supports (overhangs face down; expect support marks)')
        if alt.get('brim'):
            warn.append('small bed contact for its height, so a 5 mm brim is added')
        lines.append(f"Rotated: {alt['label'].split(',')[0]} fits at "
                     f"{'full size' if alt['scale'] >= 1 else _pct(alt['scale'])} ({size} mm)"
                     + (f"; {'; '.join(warn)}" if warn else '') + '.')
    if options.get('full_size_split'):
        lines.append('Full-size option: separate already-independent parts onto the original plates (100%).')
    policy = options.get('brim_policy')
    if policy and float(policy.get('width_mm') or 0) > 0 and any(row['kind'] == 'scale' for row in rows):
        lines.append('Tight-fit brim policy: ' + policy.get('label', 'Outer-only brim') + '.')
    lines.append(fit_hint(rows))
    return '\n'.join(lines)


def _fit_failure(job, capacity):
    model = str(job.get('title') or 'The selected model')
    source = job.get('selected_source') or {}
    if isinstance(source, dict):
        plate = str(source.get('title') or source.get('name') or 'selected source')
    else:
        plate = str(source or 'selected source')
    return (f'{model} using {plate} does not fit on one {_printer_name(job)} plate: calculated capacity is {capacity} copy with '
            '10 mm spacing and 8 mm edge margins. Choose a smaller profile, split it into plates, or scale it down.')


MODEL_FILE_EXTS = {'.3mf', '.stl'}


def model_file(event):
    """The single .3mf/.stl dropped in Discord (gateway cache path), else None. A link always wins."""
    hits = [Path(p) for p in (getattr(event, 'media_urls', None) or [])
            if Path(str(p)).suffix.lower() in MODEL_FILE_EXTS]
    return hits[0] if len(hits) == 1 and hits[0].is_file() else None


def upload_job(path):
    """Local job for a file the operator downloaded themselves; same shape as a one-profile site inventory."""
    name = re.sub(r'^doc_[0-9a-f]{12}_', '', Path(path).name)
    stem = Path(name).stem
    return {'site': 'upload', 'model_id': stem, 'title': stem, 'slug': stem, 'license': None, 'summary': '',
            'sources': [{'n': 1, 'name': name, 'title': stem, 'path': str(path)}]}


def upload_title(path, limit=1 << 20):
    """The designer's Title from a dropped 3MF, so uploads read like links ("Slim 12oz Can Holder..."
    rather than "CanHolder3MF"). Reads only the head of 3D/3dmodel.model; None when absent or not a 3MF."""
    import html
    import zipfile
    try:
        with zipfile.ZipFile(path) as z, z.open('3D/3dmodel.model') as f:
            head = f.read(limit).decode('utf-8', 'ignore')
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    m = re.search(r'<metadata\s+name="Title"[^>]*>([^<]*)</metadata>', head)
    title = html.unescape(m[1]).strip() if m else ''
    return title[:120] or None


def copy_upload(job, source, target):
    import shutil
    target.mkdir(parents=True, exist_ok=True)
    dest = target / source['name']
    shutil.copy2(source['path'], dest)
    return dest


REPRINT_RE = re.compile(
    r"\b(?:re-?print|print (?:it|that|this|that one|this one) again"
    r"|(?:print|slice|make|do) (?:the |that )?(?:last|previous|same) (?:one|print|model|thing|job)"
    r"|(?:print|slice|make|do) (?:the )?(?:last|previous) one|(?:print|make) (?:another|one more))\b", re.I)


def reprint_request(text):
    """'print the previous print in black', 'reprint that', 'print it again'. Never when a link is present."""
    text = str(text or '')
    return 'http' not in text.lower() and bool(REPRINT_RE.search(text))


def last_job(archive=None):
    """Newest archived job whose source file is still on disk, else None. Local files only."""
    from . import model_job as mj
    best = None
    for jp in Path(archive or mj.ARCHIVE).glob('*/JOB.json'):
        try:
            job = json.loads(jp.read_text())
        except (OSError, ValueError):
            continue
        f = Path(job.get('downloaded_file') or '')
        if job.get('selected_source') and f.is_file() and f.stat().st_size > 0:
            when = jp.stat().st_mtime
            if best is None or when > best[0]:
                best = (when, job)
    return best[1] if best else None


def reuse_job(prev):
    """Fresh job built from an archived one: same model and profile, source copied from disk, no site I/O."""
    row = dict(prev['selected_source'], path=prev['downloaded_file'], name=Path(prev['downloaded_file']).name)
    keep = ('site', 'model_id', 'title', 'slug', 'license', 'summary')
    return dict({k: prev.get(k) for k in keep}, sources=[row], reused_from=prev.get('intake_id'))


def resume_job(saved):
    """Same source as an expired intake, plus any color decisions already written to JOB.json."""
    job = reuse_job(saved)
    job['resumed_from'] = saved.get('intake_id')
    if saved.get('color_decisions'):
        job['color_decisions'] = saved['color_decisions']
        if saved.get('filament_selection'):
            job['filament_selection'] = saved['filament_selection']
    return job


def _saved_folder(saved):
    """The intake folder of an archived job: the nearest parent of its source that holds JOB.json."""
    source = (saved or {}).get('downloaded_file')
    return next((p for p in Path(source).parents if (p / 'JOB.json').is_file()), None) if source else None


def interrupted_note(saved):
    """One sentence when Print Handy stopped while this saved job was being prepared, else None.

    The record only explains what happened. An interrupted preparation is never resumed with its
    old approvals: reuse_job and resume_job carry no approval, so every confirmation is asked again.
    """
    folder = _saved_folder(saved)
    record = owned_worker.interrupted(folder) if folder else None
    if not record:
        return None
    return (f"Its last preparation was interrupted during {record.get('step') or 'a heavy step'} when "
            'Print Handy stopped. That run is not reused; every choice and confirmation is asked again.')


# Receipt statuses where Print Handy itself is working or waiting on a button it posted. A restart
# kills the work and the buttons, so a receipt left in one of these by another process is stranded.
IN_PROGRESS_STATUSES = frozenset({'INSPECTING_SOURCE', 'AWAITING_COLOR', 'COLORS_SAVED', 'SLICE_APPROVED'})
INTERRUPTED_MAX_AGE_S = 24 * 3600  # older receipts are history, not a conversation someone is waiting in


async def announce_interrupted(root, adapter, now=None):
    """Tell each chat whose intake a restart stranded, once. Returns the intake ids announced.

    Only receipts this version wrote (they carry chat_id and owner), written by another Print Handy
    process, still in an in-progress status and recent. The receipt is marked INTERRUPTED before the post,
    so a failing post or another restart never repeats it. Nothing is resumed: no approval survives.
    """
    root = Path(root)
    now = time.time() if now is None else now
    announced = []
    try:
        paths = sorted(root.glob('*.json'))
    except OSError:
        return announced
    for path in paths:
        try:
            receipt = json.loads(path.read_text())
            age = now - path.stat().st_mtime
        except (OSError, ValueError):
            continue
        if (not isinstance(receipt, dict) or receipt.get('status') not in IN_PROGRESS_STATUSES
                or not receipt.get('chat_id') or receipt.get('owner') in (None, owned_worker._OWNER)
                or receipt.get('interrupted_notified') or age > INTERRUPTED_MAX_AGE_S):
            continue
        receipt.update(status='INTERRUPTED', interrupted_notified=True, interrupted_from=receipt['status'])
        try:
            path.write_text(json.dumps(receipt, indent=2) + '\n')
        except OSError:
            continue
        thread = receipt.get('thread_id')
        try:
            await adapter.send(
                chat_id=str(receipt['chat_id']), metadata={'thread_id': thread} if thread else None,
                content='Print Handy restarted while it was preparing that model, so the preparation '
                        'stopped. No slice was finished, and nothing was uploaded or started. '
                        'Send the link or file again to start over.')
        except Exception:
            logger.warning('could not post the interrupted-intake note', exc_info=True)
        announced.append(receipt.get('intake_id') or path.stem)
    return announced


_LIVE_KEY = contextvars.ContextVar('prepared_intake_live_key', default=None)


from .link_intake import PromptCancelled as IntakeCancelled  # noqa: E402  typed Cancel/Stop at any prompt
from .link_intake import PromptExpired as IntakeExpired  # noqa: E402  button question reached its timeout
from .site_accounts import SiteLoginNeeded  # noqa: E402  a download needs the operator's site login (#464)


async def _spool_projection_text(mj, job, manifest, state, copies):
    """Display only: filament needed vs the chosen spool's grams. Never blocks the intake."""
    try:
        ids = [sel.get('spoolman_id') for sel, _ in mj.filament_usage_by_selection(job, manifest)]
        if not ids:
            return ''
        fetch = None
        from .printer_state import spoolman_url
        base = spoolman_url(state)
        if base:
            from . import spool_lifecycle

            def fetch(sid):
                return spool_lifecycle.fetch_spoolman_spool(sid, base_url=base)
        weights = await _heavy(mj.intake_spool_weights, state, ids, fetch)
        rows = mj.intake_spool_projection(job, weights, manifest, copies=copies)
        return mj.format_spool_projection(
            rows, position_noun='Toolhead' if mj._job_adapter(job).multi_tool else 'Slot')
    except Exception:
        return ''


MAX_CONNECT_ROUNDS = 3


SAVED_LABEL = 'Saved — try again'


def _saved_reply(text):
    """Typed `saved`/`stored`/`done` answers the connect prompt like its button.

    Returns the button's own label: link_intake.typed_reply resolves the
    prompt with this value and ask() then matches it as that button, the
    same path a press takes.
    """
    value = str(text or '').strip()
    if value == SAVED_LABEL or value.casefold().rstrip('.!') in {'saved', 'stored', 'done', 'try again'}:
        return SAVED_LABEL
    raise ValueError('reply `saved` once the token file is in place, or `cancel`')


def _connect_reply(site):
    """The typed-reply parser for ``site``'s connect prompt.

    `saved` (and its synonyms) answers like the button. Anything else must be
    the token itself: it is normalized and shape-checked
    (site_accounts.accept_pasted), written to the token file
    (site_accounts.save_token) and the prompt is then answered as if Saved had
    been pressed, so the token never travels through the prompt registry. A
    value that does not look like a token is refused with the reason (never
    the value) and the prompt stays open. link_intake.note_secret_reply tells
    the chat handler to delete the operator's message.
    """
    from . import link_intake
    from .site_accounts import accept_pasted, save_token, token_path

    def parse(text):
        try:
            return _saved_reply(text)
        except ValueError:
            pass
        token, problem = accept_pasted(site, text)
        if problem:
            raise ValueError(problem)
        try:
            save_token(site, token)
        except OSError as exc:
            raise ValueError(
                f"I couldn't save it to {token_path(site)} ({exc.strerror or type(exc).__name__}). "
                'Save it there yourself and reply `saved`, or reply `cancel`.') from None
        link_intake.note_secret_reply()
        return SAVED_LABEL
    return parse


async def _with_connect(adapter, source, key, metadata, timeout, attempt):
    """Run ``attempt()`` (a coroutine factory); on SiteLoginNeeded offer the connect flow and retry.

    Covers both places a site credential is needed (#462, #464): the model
    lookup (MakerWorld, Thingiverse, Cults3D, MyMiniFactory without a key)
    and the download (every login site). Never a dead end; the operator can
    paste the credential or save it on the Print Handy machine.
    """
    for connect_round in range(MAX_CONNECT_ROUNDS + 1):
        try:
            return await attempt()
        except SiteLoginNeeded as exc:
            if connect_round == MAX_CONNECT_ROUNDS or not await _connect_account(
                    adapter, source, key, metadata, exc, timeout):
                raise


async def _connect_account(adapter, source, key, metadata, exc, timeout):
    """Walk the operator through connecting (or renewing) the site behind ``exc``.

    One message carries everything (site_accounts.connect_steps): why it is
    needed, where the credential comes from, the two ways to hand it over
    (paste it as the next message, or save the file on the Print Handy machine
    and press Saved), what pasting risks, and the docs link. A pasted value is
    saved by ``_connect_reply``; either way True comes back and the caller
    retries. False means the operator cancelled, and the caller reports the
    original refusal. An expired or refused login reads as a renewal.
    """
    from .site_accounts import LOGIN_SITES, connect_steps
    label = LOGIN_SITES[exc.site].label
    lead = {
        'missing': f'{label} needs you to be signed in for this model, and no {label} account is connected yet.',
        'empty': f'{label} needs you to be signed in for this model, and the saved {label} login is empty or incomplete.',
        'expired': f'Your saved {label} login has expired.',
        'refused': f'{label} turned down the saved login (it has most likely expired).',
    }.get(exc.reason, f'{label} needs you to be signed in for this model.')
    renew = exc.reason in ('expired', 'refused')
    question = connect_steps(exc.site, renew=renew, lead=f'**{lead}** Nothing was downloaded.')
    saved = await _ask(adapter, source, key, question, [SAVED_LABEL, 'Cancel'], timeout,
                       parser=_connect_reply(exc.site))
    return saved in (0, SAVED_LABEL)


def _color_guess_text(mj, job, decisions):
    """Display only: say so when a slot this job uses has no real color code.

    A slot whose color could not be worked out previews in neutral gray;
    the operator hears that before slicing rather than discovering it.
    """
    try:
        slots = {row.get('slot') for row in ((decisions or {}).get('choices') or {}).values()
                 if isinstance(row, dict) and row.get('slot') is not None}
        selection = (job or {}).get('filament_selection') or {}
        if isinstance(selection, dict) and selection.get('slot') is not None:
            slots.add(selection['slot'])
        from .filament_color import chat_notes
        if not slots:
            return ''
        return '\n'.join(chat_notes(mj.printer_state.load(), slots))
    except Exception:
        return ''


async def _ask(adapter, source, key, question, choices, timeout=None, **kwargs):
    """ask() registered under the live chat session so typed replies reach the prompt."""
    live_key = kwargs.pop('live_key', None) or _LIVE_KEY.get() or key
    on_shown = kwargs.pop('on_shown', None)
    if on_shown is not None:  # fire as the question goes up, before the operator answers
        on_shown()
    return await ask(adapter, source, key, question, list(choices), timeout, live_key=live_key, **kwargs)


_NOISE = re.compile(r'^(?:TIMING \{|G-CODE OK:)')
_HEATER_TARGET = re.compile(
    r'(rightTargetTemp|platTargetTemp)\s*==\s*0\s*\(observed\s*([0-9]+(?:\.[0-9]+)?)', re.I)


def _raw_preupload_refusal(tail):
    """Return the refusal line, excluding timing and preflight diagnostics."""
    lines = [ln.strip() for ln in str(tail or '').splitlines()
             if ln.strip() and not _NOISE.match(ln.strip())]
    return next((ln for ln in reversed(lines) if ln.startswith('REFUSED:')), '')


START_PENDING_MESSAGE = ('Start sent, but the printer has not confirmed it yet. It is probably still heating '
                         'and preparing, which can take several minutes. I will post here when it starts '
                         'printing. Do not send it again.')


class StartClass(NamedTuple):
    status: str          # the receipt status: STARTED, START_PENDING or START_REFUSED
    started: bool | None  # None while unresolved
    outcome: str | None  # 'unknown' while unresolved, else the outcome phase
    unknown: bool
    pending: bool


def classify_start(ok, tail):
    """How the intake reads one finished print-now run: from its exit status and its uniform
    `START READBACK {phase}` outcome line (the mapped native route's `MAPPED START READBACK` too).

    A start the printer has not confirmed (awaiting_confirmation, preparing_or_unresolved), or a failure
    that is not an explicit REFUSED line, is START_PENDING: "Print started" is never posted for it. Only
    a confirmed phase (executing, preparing) or an exit 0 with no outcome line (older runs) is STARTED.
    """
    outcome = print_next.start_outcome(tail)
    phase = str(outcome.get('phase') or '')
    unknown = (print_next.start_unresolved(outcome) or print_next.outcome_unreadable(tail)
               or (not ok and not _safe_preupload_refusal(tail)))
    pending = unknown or phase in ('preparing', 'executing')
    return StartClass('START_PENDING' if unknown else 'STARTED' if ok or pending else 'START_REFUSED',
                      None if unknown else bool(ok or pending), 'unknown' if unknown else phase or None,
                      unknown, pending)


def _printer_rejected(raw):
    """The refusal line of a start the printer itself rejected: the file was uploaded, nothing started."""
    return str(raw or '').startswith('REFUSED: the printer rejected the start request')


def _safe_preupload_refusal(tail):
    """Only an explicit terminal REFUSED line confirms nothing uploaded or started."""
    raw = _raw_preupload_refusal(tail)
    return bool(raw and not raw.startswith('UNCERTAIN:'))


def _refusal_text(tail):
    """Return plain user-facing guidance; raw internals remain in the receipt and job log."""
    raw = _raw_preupload_refusal(tail)
    slot = re.match(r'REFUSED: (printer slot \d+ .+?) \(T\d+\)\.', raw)
    if slot:
        return (f"Didn't start: {slot.group(1)}. Fix the slot on the printer or the spool in Spoolman, "
                "then press Retry.")
    held = re.match(r'REFUSED: an earlier start on this printer is uncertain \(job .*?, file (.+?), since ', raw)
    if held:
        return (f"Didn't start: an earlier start ({held.group(1)}) was never confirmed, so nothing new "
                "starts until it is settled. It clears by itself once the printer reports that file; "
                "if it never printed, run `reconcile-launch --not-started`, then press Retry.")
    rejected = re.match(r'REFUSED: the printer rejected the start request \((.+?)\)\. ', raw)
    if rejected:
        return (f"Didn't start: the printer rejected the start request ({rejected.group(1)}). "
                "The file is on the printer, but nothing is printing. Fix that, then press Retry.")
    targets = {name.casefold(): float(value) for name, value in _HEATER_TARGET.findall(raw) if float(value) > 0}
    if targets:
        heaters = []
        if targets.get('righttargettemp'):
            heaters.append(f"the nozzle set to {targets['righttargettemp']:g} °C")
        if targets.get('plattargettemp'):
            heaters.append(f"the bed to {targets['plattargettemp']:g} °C")
        if len(heaters) == 2:
            detail = f"{heaters[0]} and {heaters[1]} from the last job"
        else:
            detail = f"{heaters[0]} from the last job"
        return (f"Didn't start: the printer still has {detail}. Clear heating on the printer screen "
                "(or power cycle it), then press Retry.")
    return ("Didn't start: the printer could not confirm it was ready. Check the printer screen, "
            "resolve any warning, then press Retry.")


def plain_start_failure(tail):
    """The plain chat wording for a failed/refused print-now run (#467).

    ``tail`` is the print-now transport text: it stays in the receipt and
    the log, but it is never posted raw — REFUSED lines, UPLOAD FAILED /
    START FAILED stderr tails and UNCERTAIN markers are machinery, not a
    message for the person who pressed the button. A confirmed pre-upload
    refusal gets the specific _refusal_text guidance; anything else gets
    the honest uncertain wording (the printer may or may not have it).
    """
    if _safe_preupload_refusal(tail):
        return _refusal_text(tail)
    if str(tail or '').lstrip().upper().startswith('UNCERTAIN:') or not _raw_preupload_refusal(tail):
        return ("The printer did not confirm whether the print started. Check the printer "
                "screen before doing anything else, and do not press Start again until "
                "you know.")
    return _refusal_text(tail)


def _restart_helps(tail):
    """A stale heater target, stuck screen or leftover job: a force restart clears these."""
    text = str(tail or '').casefold()
    return 'refused' in text and any(w in text for w in (
        'idle guard', 'targettemp', 'paused', 'cancelled', 'canceled', 'shutdown', 'klippy',
        'stale', 'retained job', 'not idle', 'state is error'))


async def _offer_force_restart(adapter, source, key, message, timeout=None):
    """Offer the existing safe REBOOT flow; never start the print afterwards."""
    pc = _external.load('print_control')
    question = (message + '\n\nThe printer looks stuck from a previous job. Force restart clears it '
                '(and wipes any Resume on the screen). The print will not start by itself afterwards.')
    try:
        answer = await _ask(adapter, source, key, question, [pc.REBOOT_LABEL, pc.CANCEL_REBOOT_LABEL], timeout)
    except Exception:
        return message
    if answer != 0:  # _ask returns the button index
        return 'Not restarted. Nothing uploaded or started.'
    with contextlib.suppress(Exception):
        await adapter.send(chat_id=source.chat_id, content='Force restarting the printer now, about two minutes.',
                           metadata=getattr(source, 'metadata', None))
    ok, result = await asyncio.to_thread(pc.force_reboot)
    return result + (' Send the link or press Print again to retry.' if ok else '')


async def _pick_plate(adapter, source, key, parts, timeout=None):
    """One picker for a split job: index of the plate to print first, or None for Cancel."""
    labels = [print_next.plate_choice_label(part, part.get('plate')) for part in parts]
    picked = await _ask(adapter, source, key, 'Which plate prints first?', labels + ['Cancel'], timeout)
    return picked if picked < len(parts) else None


async def _delivery_menu(adapter, source, key, metadata, timeout, question, enabled, receipt, receipt_path,
                         jp, gcodes):
    """Post-slice choices from the vendor profile. 0 means Start was pressed; anything else means stop here.

    Start (only when the vendor can print) is the same per-job bed-clear button as before and is the only
    way to print. Each delivery runs at most once to completion, then leaves the menu; Done ends it.
    """
    can_print = delivery.PRINTER in enabled
    offered = [m for m in enabled if m != delivery.PRINTER]
    sizes = {}  # deflate measurements for this slice, taken once (#272)
    shown = False
    while True:
        pending = [m for m in offered if not delivery.done(receipt, m)]
        buttons, note = list(pending), ''
        if delivery.DISCORD in pending:
            # Size what the send would attach before offering it (#272): a file over [discord]
            # upload_max_bytes gets its size, the limit and its host path here, not a button that fails.
            limit = delivery.upload_limit()
            fit = await asyncio.to_thread(delivery.discord_candidates, gcodes, limit, cache=sizes)
            note = delivery.oversize_message(fit, limit)
            if not any(item.fits for item in fit):
                buttons.remove(delivery.DISCORD)
        if not buttons and not can_print:
            if note and not shown:  # no menu to carry it: the sizes still reach the operator
                await adapter.send(chat_id=source.chat_id, content=note, metadata=metadata)
            return 1
        shown = True
        choices = (([print_next.START_LABEL] if can_print else []) + [delivery.LABELS[m] for m in buttons]
                   + [delivery.DONE_LABEL])
        picked = await _ask(adapter, source, key, question + ('\n' + note if note else ''), choices, timeout)
        label = choices[picked]
        if label == print_next.START_LABEL:
            return 0
        if label == delivery.DONE_LABEL:
            return 1
        method = buttons[picked - (1 if can_print else 0)]
        await _deliver(adapter, source, metadata, method, receipt, jp, gcodes, pending)
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        question = 'Anything else for this slice?' + (' Start uploads and prints it immediately.' if can_print else '')


async def _deliver(adapter, source, metadata, method, receipt, jp, gcodes, pending):
    """Run one delivery and report exactly what happened. Never uploads to or starts a printer."""
    async def say(text):
        return await adapter.send(chat_id=source.chat_id, content=text, metadata=metadata)
    try:
        job = json.loads(Path(jp).read_text())
        artifacts = delivery.bound_artifacts(job, gcodes, receipt.get('gcode_sha256') or {})
        if method == delivery.BAMBU_HANDOFF:
            packages = await asyncio.to_thread(delivery.bambu_handoff, job, artifacts)
            delivery.record(receipt, method, [{'source': p, 'sent': None, 'ok': True} for p in packages], True,
                            'packaged')
            names = ', '.join(p.name for p in packages)
            where = ' On the Print Handy host: ' + ', '.join(str(p) for p in packages) + '.'
            if delivery.DISCORD in pending:
                # The package now exists, so its size is exact: offer the download only when it fits (#272).
                limit = delivery.upload_limit()
                fit = delivery.discord_candidates(artifacts, limit)
                fitting = sum(1 for f in fit if f.fits)
                offer = ('' if not fitting else " Use 'Send the files here' to download it." if fitting == len(fit)
                         else " Use 'Send the files here' to download the one(s) under the limit.")
                note = delivery.oversize_message(fit, limit, for_handoff=True)
                where = offer + (' ' + note if note else '')
            await say(f'Bambu package ready: {names}.{where}\n'
                      + delivery.handoff_note(delivery.current_model()))
            return
        files = await asyncio.to_thread(delivery.files_for_discord, job, artifacts)
        limit = delivery.upload_limit()
        already = delivery.delivered(receipt, method)
        todo = [f for f in files if str(f) not in already]
        plan = await asyncio.to_thread(delivery.discord_plan, todo, limit, Path(jp).parent / 'delivery')
        items, failed = [], []
        for item in plan:
            label = f'Plate {files.index(item.source) + 1} of {len(files)}: {item.source.name}' if len(files) > 1 else None
            text = delivery.discord_message(item, limit, label)
            if item.mode == 'too_big':
                await say(text)
                items.append({'source': item.source, 'sent': None, 'ok': False, 'reason': 'too_big'})
                continue
            try:
                result = await say(text)
                ok = getattr(result, 'success', True) is not False
                error = getattr(result, 'error', None) or 'Discord did not accept the upload'
            except Exception as exc:  # a transport error is a failed delivery, not a crash
                ok, error = False, str(exc)
            items.append({'source': item.source, 'sent': item.file, 'ok': ok, **({} if ok else {'reason': error})})
            if not ok:
                failed.append(f'{item.source.name} ({error})')
        sent = sum(1 for i in items if i['ok']) + len(already)
        big = [i['source'].name for i in items if i.get('reason') == 'too_big']
        delivery.record(receipt, method, items, not failed, f'{sent} of {len(files)} sent')
        parts = [f'Sent {sent} of {len(files)} file(s) here.']
        if big:
            parts.append('Too big for Discord: ' + ', '.join(big) + '.')
        if failed:
            parts.append('Failed: ' + '; '.join(failed) + ". Pick 'Send the files here' again to retry those.")
        await say(' '.join(parts) + ' Nothing was uploaded to a printer or started.')
    except delivery.DeliveryRefused as exc:
        delivery.record(receipt, method, [], False, str(exc))
        await say(f'The files were not sent here: {exc}. Nothing was uploaded to a printer or started.')


async def _send_render_preview(adapter, source, path, scale=1.0, rotation=None, bed=tuple(BED)):
    from .render_preview import render
    image_path = await asyncio.to_thread(render, path, scale, rotation, bed)
    await adapter.send(chat_id=source.chat_id,
                       content='Oriented and scaled model preview (bed outline shown).\\nMEDIA:' + image_path,
                       metadata={'thread_id': source.thread_id} if source.thread_id else None)


async def _send_source_thumbnail(adapter, source, path, target):
    """Best effort display of a source-authored painted 3MF preview; never blocks intake."""
    import zipfile
    from PIL import Image

    preferred = ('Auxiliaries/.thumbnails/thumbnail_3mf.png',
                 'Metadata/plate_1.png', 'Metadata/thumbnail.png')

    def extract():
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                name = next((item for item in preferred if item in names), None)
                if name is None:
                    return None
                target_path = Path(target)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source_file, Image.open(source_file) as image:
                    image.load()
                    if image.width < 32 or image.height < 32:
                        return None
                    image.convert('RGB').save(target_path, format='PNG')
                return str(target_path)
        except (OSError, ValueError, zipfile.BadZipFile):
            return None

    try:
        image_path = await asyncio.to_thread(extract)
        if image_path:
            await adapter.send(chat_id=source.chat_id,
                               content='Source model preview (from the supplied 3MF).\\nMEDIA:' + image_path,
                               metadata={'thread_id': source.thread_id} if source.thread_id else None)
    except Exception:
        pass


FILE_LIST_SITES = ('printables', 'thingiverse', 'myminifactory', 'cults3d', 'crealitycloud',
                   'thangs', 'nexprint', 'makeronline', 'direct')
CHOOSE_FILES = 'Choose files'


def _and(words):
    words = [str(w) for w in words]
    return words[0] if len(words) == 1 else ', '.join(words[:-1]) + ' and ' + words[-1]


def _file_name(row):
    return str(row.get('name') or row.get('title') or 'file')


def _parts_title(rows):
    """'UNO-container-1, UNO-uno-latch-1 and UNO-cap-sign-1': the files of a multi-file job, plainly."""
    return _and(re.sub(r'\.(?:stl|3mf)$', '', _file_name(r), flags=re.I) for r in rows)


async def _choose_files(adapter, source, key, rows, timeout):
    """The files of a file-list model to print, as rows: all parts by default.

    Files on Printables, Thingiverse and the other file-list sites are parts of
    one model, not alternatives. Versions of one part (cap with or without a
    sign, M3 or M4) get one short question per group; 'Choose files' picks any
    subset, including a single file. One 3MF among STLs usually holds the whole
    model already laid out, so it is offered first.
    """
    from . import model_parts
    names = [_file_name(r) for r in rows]
    three_mf = model_parts.plan(names)['assembly_3mf']
    if three_mf is not None:
        stls = [r for r in rows if _file_name(r).lower().endswith('.stl')]
        pick = await _ask(adapter, source, key,
                          f'This model has a 3MF project, {names[three_mf]}, and {len(stls)} STL '
                          f"file{'s' if len(stls) != 1 else ''}. A 3MF on the model page usually holds the "
                          "whole model already laid out, so I'll use it.",
                          [f'Use {names[three_mf]}'[:80], f"Print the STL file{'s' if len(stls) != 1 else ''}",
                           CHOOSE_FILES], timeout)
        if pick == 0:
            return [rows[three_mf]]
        if pick == 2:
            return await _pick_files(adapter, source, key, rows, timeout)
        rows = stls
        if len(rows) == 1:
            return rows
        names = [_file_name(r) for r in rows]
    plan = model_parts.plan(names)
    shown = plan['names']
    groups = plan['groups']
    described = [shown[i] for i in plan['parts']] + [
        f"{'a' if ' and ' not in g['subject'] else 'the'} {g['subject']} in {len(g['options'])} versions"
        for g in groups]
    lead = (f'This model has {len(rows)} files that print together: {_and(described)}. '
            "I'll put them on one job, one of each.")
    counts = [f'{names[i]} suggests {n}' for i, n in plan['quantities'].items()]
    if counts:
        lead += f" ({_and(counts)}; I still print one of each, so print more copies separately if you need them.)"
    picked = list(plan['parts'])
    if not groups:
        pick = await _ask(adapter, source, key, lead, [f'Print all {len(rows)}', CHOOSE_FILES], timeout)
        if pick == 1:
            return await _pick_files(adapter, source, key, rows, timeout)
        return rows
    for n, group in enumerate(groups):
        options = group['options'][:5]
        labels = [o['label'][:60] for o in options]
        question = f"Which {group['subject']}?"
        if n == 0:
            more = (f" Showing {len(options)} of {len(group['options'])}; use {CHOOSE_FILES} for the others."
                    if len(options) < len(group['options']) else '')
            pick = await _ask(adapter, source, key, f'{lead} {question}{more}', labels + [CHOOSE_FILES], timeout)
            if pick == len(labels):
                return await _pick_files(adapter, source, key, rows, timeout)
        else:
            pick = await _ask(adapter, source, key, question, labels, timeout)
        picked.extend(options[pick]['files'])
    return [r for i, r in enumerate(rows) if i in picked]


async def _pick_files(adapter, source, key, rows, timeout):
    """'Choose files': any subset by typed numbers, or one file by button (the old single pick)."""
    from . import model_parts
    listing = '\n'.join(f'{i}. {_file_name(r)}' for i, r in enumerate(rows, 1))
    shown = rows[:4]
    pick = await _ask(adapter, source, key,
                      'Which files should I print? Type their numbers, for example 1 3, or pick one file.\n' + listing,
                      [f'{i}: {_file_name(r)}'[:70] for i, r in enumerate(shown, 1)], timeout,
                      parser=lambda text: model_parts.parse_file_numbers(text, len(rows)))
    if isinstance(pick, int):
        return [shown[pick]]
    return [rows[i] for i in sorted(pick)]


def _combined_source(job, rows, paths, combined):
    """JOB.json source fields for a multi-file job: one row per file, and the combined project for older readers."""
    parts = [dict(row, downloaded_file=str(path)) for row, path in zip(rows, paths)]
    selected = {'kind': 'parts', 'name': Path(combined['path']).name, 'title': _parts_title(rows),
                'files': [_file_name(r) for r in rows]}
    return {'selected_source': selected, 'selected_sources': parts,
            'downloaded_file': combined['path'], 'combined_objects': combined['objects']}


async def _fetch_parts(adapter, source, key, metadata, timeout, job, parts, folder, fetch, state):
    """Download each chosen file once and combine them into one project; returns its path.

    Every file goes to its own folder (two files may share a name), through the
    same site-login and Stop wrapping as a single download. The combined 3MF has
    one object per part, so fit, rearrange, split and per-part colors treat it
    like any multi-object 3MF. JOB.json keeps every file in selected_sources.
    """
    from . import model_parts
    from .intake_resources import check_source
    paths = []
    for n, row in enumerate(parts, 1):
        dest = folder / 'source' / 'parts' / str(n)
        path = await _with_connect(
            adapter, source, key, metadata, timeout,
            lambda row=row, dest=dest: _with_stop(adapter, source, key, metadata, None,
                                                  asyncio.to_thread(fetch, job, row, dest)))
        await asyncio.to_thread(check_source, path)
        paths.append(Path(path))
    from . import model_job as mj
    unit_q = await asyncio.to_thread(mj.parts_unit_question, job, paths)
    inches = False
    if unit_q is not None:
        # The combined project is millimeters, so the STL files' units are settled here, once for the set.
        measured = ' x '.join(f'{float(value):g}' for value in unit_q['original_mm'])
        as_inch = ' x '.join(f'{float(value):g}' for value in unit_q['inch_mm'])
        inches = 1 == await _ask(adapter, source, key, (
            f'The largest STL file measures {measured} mm as written, and STL files do not record units. '
            f'Is that size in millimeters, or in inches (which would print at {as_inch} mm)? '
            'One answer covers all the STL files.'), unit_q['choices'], timeout)
    target = folder / 'source' / f"{job.get('slug') or 'model'}-parts.3mf"
    bed = list(state.get('bed_mm') or BED)[:2] + ([state['max_print_height_mm']] if state.get('max_print_height_mm') else [])
    combined = await _with_stop(adapter, source, key, metadata,
                                f'Putting the {len(paths)} files together as one project.',
                                _heavy(model_parts.combine, target, [str(p) for p in paths], bed, stl_inches=inches))
    job.update(_combined_source(job, parts, paths, combined))
    if unit_q is not None:
        job['unit_provenance'] = {'units': 'inch' if inches else 'mm', 'source': 'user_confirmation',
                                  'original_mm': unit_q['original_mm'],
                                  'factor': mj.INCH_TO_MM if inches else 1.0}
    return Path(combined['path'])


async def prepare_link(event, adapter, state, root, timeout=None, reuse=None, printer_id=None):
    from . import model_job as mj
    from .source_colors import inventory, canonical_prompts, grouped_prompts, validate_decisions
    source = event.source
    url = None if reuse else model_link(event.text)
    upload = None if (url or reuse) else model_file(event)
    mid = getattr(event, 'message_id', None)
    if not (url or upload or reuse) or not mid:
        raise ValueError('plain model link requires a stable Discord message ID')
    iid = mj.intake_id_from_message(source, mid)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    receipt_path = root / (iid + '.json')
    origin = (url or (f'reprint:{reuse.get("intake_id")}' if reuse else None)
              or f'upload:{upload_job(upload)["sources"][0]["name"]}')
    receipt = {'intake_id': iid, 'url': origin, 'status': 'INSPECTING_SOURCE',
               'sliced': False, 'uploaded': False, 'started': False,
               'chat_id': str(source.chat_id), 'thread_id': str(source.thread_id) if source.thread_id else None,
               'owner': owned_worker._OWNER}
    try:
        with receipt_path.open('x') as f:
            json.dump(receipt, f)
    except FileExistsError:
        return {'action': 'skip', 'reason': 'duplicate model-link event'}
    key = 'model-link:' + iid
    event_metadata = getattr(event, 'metadata', None) or {}
    live_token = _LIVE_KEY.set(event_metadata.get('live_session_key') or key)
    log_token = logs.bind(intake=iid)
    logger.info('intake started origin=%s', logs.redact(str(origin)))
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    jp = None
    try:
        if reuse:
            if reuse.get('resumed_from'):
                job = reuse
                url = reuse.get('url')
            else:
                done = reuse.get('sliced_at') and (reuse.get('filament_selection') or {}).get('color')
                when = f" (last sliced in {reuse['filament_selection']['color']})" if done else ''
                interrupted = interrupted_note(reuse)
                pick = await _ask(adapter, source, key, f"Did you mean {reuse.get('title') or 'the last model'}{when}? "
                                 'I will slice it again from the saved file, no download.'
                                 + (f' {interrupted}' if interrupted else ''),
                                 ['Yes, slice it again', "No, I'll send a link"], timeout)
                if interrupted:
                    owned_worker.clear(_saved_folder(reuse))  # explained once; the new intake has its own folder
                if pick != 0:
                    receipt.update(status='DECLINED_REPRINT')
                    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                       content='OK. Send the link or drop the file. Nothing was sliced or started.')
                    return {'action': 'skip', 'reason': 'reprint declined'}
                job = reuse_job(reuse)
                url = reuse.get('url')
        elif upload:
            job = upload_job(upload)
        else:
            job = await _with_connect(adapter, source, key, metadata, timeout,
                                      lambda: asyncio.to_thread(mj.inventory_for_url, url))
        rows = job.get('sources', [])
        if job['site'] in FILE_LIST_SITES:
            rows = [r for r in rows if Path(r.get('name', '')).suffix.lower() in {'.3mf', '.stl'}]
        if not rows:
            raise ValueError(job.get('no_geometry_reason') or 'source inventory contains no selectable geometry')
        if job['site'] == 'makeronline':
            # Inventory-only (#464): the listing is real, but MakerOnline
            # downloads need a browser fingerprint check, so refuse here —
            # before any profile choice, preview, download or slicing — with
            # the named gap and the browser-download workaround.
            names = ', '.join(str(r.get('name') or 'unnamed file') for r in rows[:5])
            raise ValueError(
                f"MakerOnline model \"{job.get('title') or 'untitled'}\": the inventory found "
                f"{len(rows)} printable file(s): {names}. "
                + (job.get('download_gap') or mj.MAKERONLINE_DOWNLOAD_GAP)
                + " Nothing was downloaded or sliced.")
        hint = re.fullmatch(r'profileId-(\d+)', urlsplit(url or '').fragment or '')
        parts = None  # two or more files of one file-list model, printed together
        if job['site'] == 'makerworld':
            from .makerworld_profiles import choose as choose_maker_profile, selection_summary
            chosen = choose_maker_profile(rows, hint[1] if hint else None)
            if chosen is not None:
                # The concise intake preview below carries model description and validated
                # AD5X estimates. Avoid repeating the long source-printer profile summary.
                pass
            else:
                labels = [f"{i}: {selection_summary(r)}"[:96] for i, r in enumerate(rows, 1)]
                labels.append('Do not print (Cancel)')
                selected = await _ask(adapter, source, key,
                    f"This model has {len(rows)} print profiles; choose the design/profile to inspect.", labels, timeout)
                if selected == len(rows):
                    raise IntakeCancelled
                chosen = rows[selected]
        elif len(rows) == 1:
            chosen = rows[0]
        else:
            picked = await _choose_files(adapter, source, key, rows, timeout)
            chosen = picked[0]
            if len(picked) > 1:
                parts = picked
                chosen = {'kind': 'parts', 'title': _parts_title(parts)}
        await send_preview(adapter, source, job, chosen, metadata, state=state)
        folder = mj.intake_folder(mj.ARCHIVE, job['slug'], iid)
        folder.mkdir(parents=True, exist_ok=False)
        job.update(url=url, intake_id=iid, hardware=state, selected_source=chosen)
        fetch = copy_upload if (reuse or job['site'] == 'upload') else (
            lambda j, chosen, dest: mj.download_for_site(j, chosen, dest))
        from .intake_resources import check_source
        if parts:
            path = await _fetch_parts(adapter, source, key, metadata, timeout, job, parts, folder, fetch, state)
        else:
            path = await _with_connect(
                adapter, source, key, metadata, timeout,
                lambda: _with_stop(adapter, source, key, metadata, None,
                                   asyncio.to_thread(fetch, job, chosen, folder / 'source')))
            job['downloaded_file'] = str(path)
        await asyncio.to_thread(check_source, path)
        if job['site'] == 'upload':
            job['title'] = await asyncio.to_thread(upload_title, path) or job['title']
        job['audit'] = await _with_stop(adapter, source, key, metadata, large_model_notice(path),
                                        _heavy(mj.audit, path))
        if isinstance(job['audit'], dict) and job['audit'].get('no_printable_volume'):
            # Refuse at intake (#194): asking for colors and copies first only to fail at slicing wastes the operator's time.
            raise mj.NoPrintableVolume(job['audit']['geometry_error'])
        # Inspecting a large multi-object 3MF is ~30 s of pure-Python XML work. It must never run on the
        # gateway event loop (that froze Discord button callbacks). The audit already inspected these exact
        # bytes; reuse its manifest only if it is bound to the file's current SHA256, else inspect off-loop.
        manifest = (job['audit'] or {}).get('color_manifest') if isinstance(job['audit'], dict) else None
        actual_sha = await asyncio.to_thread(mj.file_sha256, path)
        if not (isinstance(manifest, dict) and manifest.get('source_sha256') == actual_sha):
            manifest = await _with_stop(
                adapter, source, key, metadata, "Checking the model. Press Stop to cancel.",
                _heavy(inventory, path))
        # STL has no source palette, but still needs an exact-source color decision.
        if not manifest.get('source_sha256'):
            manifest.update(source_sha256=await asyncio.to_thread(mj.file_sha256, path))
        job['color_manifest'] = manifest
        job['color_schema'] = 'part_slots_v1'
        mj.stamp_job_printer(job, printer_id)  # a new job learns its printer at creation (#334); the intake channel's printer wins (#335)
        jp = folder / 'JOB.json'
        mj.write_job(folder, job)
        receipt.update(job_json=str(jp), source_sha256=manifest['source_sha256'], status='AWAITING_COLOR')
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        unit_q = mj.unit_question(job)
        if unit_q is not None:
            # STL records no units and this part is small enough to be either
            # millimeters or inches. Ask before any fit or color work, and
            # record the answer on the job; slicing refuses to assume (#206).
            measured = ' x '.join(f'{float(value):g}' for value in unit_q['original_mm'])
            as_inch = ' x '.join(f'{float(value):g}' for value in unit_q['inch_mm'])
            pick = await _ask(adapter, source, key,
                              f'This STL measures {measured} mm as written, and STL files do not record units. '
                              f'Is that size in millimeters, or in inches (which would print at {as_inch} mm)?',
                              unit_q['choices'], timeout)
            units = 'mm' if pick == 0 else 'inch'
            decision = mj.make_unit_decision(units, manifest['source_sha256'], unit_q['original_mm'])
            mj.set_unit_decision(job, decision)
            mj.write_job(folder, job)
            receipt.update(unit_decision=decision)
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
            if units == 'inch':
                bed = state.get('bed_mm') or BED
                height = state.get('max_print_height_mm')
                if (unit_q['inch_mm'][0] > float(bed[0]) or unit_q['inch_mm'][1] > float(bed[1])
                        or (height is not None and unit_q['inch_mm'][2] > float(height))):
                    receipt.update(status='CANCELLED_OVERSIZED')
                    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                    await adapter.send(chat_id=source.chat_id,
                                       content=f'Treating it as inches would make it {as_inch} mm, which exceeds '
                                               'the printer volume. Print cancelled. Nothing was sliced, '
                                               'uploaded, or started.',
                                       metadata=metadata)
                    return {'action': 'skip', 'reason': 'inch-confirmed model exceeds printer volume'}
        job_adapter = mj._job_adapter(job)
        selections = [mj.resolve_filament_selection(state, dict(label, slot=int(slot)))
                      for slot, label in sorted(mj.configured_slots(state).items())]
        # A multi_tool printer's positions are toolheads, never slots (#346).
        labels = (mj.build_tool_labels(selections) if job_adapter.multi_tool
                  else mj.build_slot_labels(selections))
        if not labels or len(labels) > mj.position_limit(job_adapter) or len(set(labels)) != len(labels):
            raise ValueError('requires one to four distinct configured toolheads' if job_adapter.multi_tool
                             else 'requires one to four distinct configured slots')
        questions = canonical_prompts(manifest)
        grouped = grouped_prompts(manifest)
        families = [row for row in grouped if len(row['keys']) > 1]
        tally = {}
        for p in manifest['parts']:
            label = re.sub(r'\.(stl|3mf|obj|step|stp)$', '', p['name'], flags=re.I)
            tally[label] = tally.get(label, 0) + 1
        names = ', '.join(f'{n}× {k}' if n > 1 else k for k, n in tally.items()) or Path(path).name
        saved_decisions = job.get('color_decisions') if job.get('resumed_from') else None
        if saved_decisions:
            decisions = saved_decisions
        elif questions and (len(manifest['parts']) > 1 or manifest['painted']):
            options = ['Keep distinct source regions' if manifest['painted'] else 'Choose colors for these parts',
                       'One color for everything']
            if families:
                together = '; '.join(row.get('label') or row['question'].split(' for ', 1)[1].split(' (', 1)[0]
                                     for row in families)
                options[0] = f'Choose colors ({together} share one color)'[:80]
                options.append('Choose every part individually')
            count = len(manifest['parts'])
            found = f"Inspected {count} part{'s' if count != 1 else ''}: {names}."
            prompt = (f'{found} Keep distinct source regions or explicitly use one color for everything? '
                      'Keeping painted regions prints each painted area in the color you pick for it.'
                      if manifest['painted'] else
                      f'{found} Choose colors for these parts or one color for everything?')
            mode = await _ask(adapter, source, key, prompt, options, timeout)
        else:
            mode = 1
        decisions = {'source_sha256': manifest['source_sha256'], 'schema': 'part_slots_v1',
                     'mode': 'single_color' if mode == 1 else 'per_part'}
        if mode != 1:
            if job_adapter.multi_tool:
                # One colour per toolhead (#346): more distinct source colours
                # than toolheads can never be assigned; refuse before asking.
                from .source_colors import distinct_source_colours
                colours = distinct_source_colours(manifest)
                if len(colours) > job_adapter.tool_count:
                    raise printers.MultiColorUnsupported(
                        f"{job_adapter.label} has {job_adapter.tool_count} toolheads and prints one color "
                        f"per toolhead; this source uses {len(colours)} colors. Reduce the source to at "
                        f"most {job_adapter.tool_count} colors or use a printer with more toolheads")
            # Numbered siblings (Stencil 1/2/3) get one question unless the operator chose per part.
            rows = grouped if mode == 0 else [{**row, 'keys': [row['key']]} for row in questions]
            choices = {}
            taken = {}  # toolhead -> source colour already assigned to it (multi_tool only)
            for row in rows:
                if job_adapter.multi_tool:
                    from .source_colors import prompt_source_colour
                    colour = prompt_source_colour(manifest, row)
                    offered = [(label, sel) for label, sel in zip(labels, selections)
                               if taken.get(sel['slot'], colour) == colour]
                    picked = offered[await _ask(adapter, source, key, row['question'],
                                                [label for label, _ in offered], timeout)][1]
                    taken[picked['slot']] = colour
                else:
                    picked = selections[await _ask(adapter, source, key, row['question'], labels, timeout)]
                for decision_key in row['keys']:
                    choices[decision_key] = picked
            decisions['choices'] = choices
        else:
            decisions['selection'] = selections[await _ask(adapter, source, key, f'Choose the color for {names}', labels, timeout)]
        latest = json.loads(jp.read_text())
        if saved_decisions:
            if (latest.get('intake_id') != iid
                    or mj.file_sha256(latest['downloaded_file']) != decisions['source_sha256']):
                raise ValueError('source/job changed during resume; decisions not saved')
        elif (latest.get('intake_id') != iid or latest.get('color_decisions')
                or mj.file_sha256(latest['downloaded_file']) != decisions['source_sha256']):
            raise ValueError('source/job changed during selection; decisions not saved')
        validated = validate_decisions(manifest, decisions, state, mj.resolve_filament_selection)
        mj.set_color_decisions(latest, validated)
        if decisions['mode'] == 'single_color':
            latest['filament_selection'] = validated['selection']
        from . import orca_adapter
        fit_status = FitStatus(adapter, source, metadata)
        try:
            capacity = await fit_status.run('checking the layout as the author laid it out',
                                            _heavy(orca_adapter.orca_plate_capacity, path, state),
                                            first_text='Checking whether it fits the bed.')
        except Exception as exc:
            logger.exception('fit check failed; intake is resumable')
            receipt['status'] = 'FIT_UNKNOWN'
            receipt['fit_error'] = str(exc)
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
            await adapter.send(chat_id=source.chat_id,
                               content=diagnostics.known_post(
                                   "I could not finish checking whether this model fits the bed, so the check is unverified.",
                                   not_happened="No slice or print was started.",
                                   subsystem='intake', exc=exc)
                               or diagnostics.plain_post(
                                   "I could not finish checking whether this model fits the bed, so the check is unverified.",
                                   not_happened="No slice or print was started.",
                                   next_step=diagnostics.next_step_for(
                                       exc,
                                       transient="Send the link or file again to resume the check.",
                                       other="Run `/print-handy bundle` and share the bundle so it can be looked into."),
                                   subsystem='intake', exc=exc),
                               metadata=metadata)
            return {'action': 'skip', 'reason': 'fit unknown; resumable'}
        if (job.get('unit_decision') or {}).get('units') == 'inch':
            # Capacity was probed at the authored size; an inch-confirmed part
            # prints 25.4x larger, so only the single copy is verified.
            capacity = min(capacity, 1)
        painted_multi = (manifest.get('painted') and (latest.get('color_decisions') or {}).get('mode') == 'per_part')
        if capacity < 1 and painted_multi:
            # Painted multicolor: one multicolor G-code per authored plate at
            # one shared scale, 100% whenever it fits. No per-slot shrink rows.
            title = latest.get('title') or 'This model'
            try:
                fit = await _heavy(mj.painted_fit_for_job, path, latest['color_decisions'])
            except Exception as exc:
                fit = {'scale': None, 'error': str(exc)}
            receipt['painted_fit'] = fit
            scale = fit.get('scale')
            if scale is None:
                picked = [{'kind': 'cancel', 'label': 'Cancel'}]
                prompt = (f"{title} does not fit the {_printer_name()} bed with its prime tower at 50% or larger, "
                          'even split into its plates.')
            else:
                plates = fit['plates']
                where = f"{plates} plates" if plates > 1 else 'one plate'
                label = f"100% · split {plates} plates" if scale >= 1.0 and plates > 1 else f"{_pct(scale)} · {where}"
                picked = [{'kind': 'painted', 'label': label, 'scale': scale}, {'kind': 'cancel', 'label': 'Cancel'}]
                if scale >= 1.0:
                    prompt = f"{title} prints at 100% as {where}, each in all its painted colors."
                else:
                    prompt = (f"{title} is too big to print at 100% even split into its plates. The largest size "
                              f"that fits with the color-change tower is {_pct(scale)}, as {where}, every part at the "
                              'same size so they still fit together.')
            row = picked[await _ask(adapter, source, key, prompt, [r['label'] for r in picked], timeout,
                                    parser=lambda text: _fit_choice_reply(text, picked))]
            if row['kind'] == 'cancel':
                receipt['status'] = 'CANCELLED_OVERSIZED'
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                await adapter.send(chat_id=source.chat_id,
                                   content='Print cancelled. Nothing was sliced, uploaded, or started.',
                                   metadata=metadata)
                return {'action': 'skip', 'reason': 'cancelled oversized model'}
            latest['plate_mode'] = 'separate'
            if options.get('brim_policy'):
                latest['fit_brim_policy'] = options['brim_policy']
                receipt['fit_brim_policy'] = options['brim_policy']
            latest['scale'] = row['scale']
            receipt['scale'] = row['scale']
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
            mj.write_job(folder, latest)
            capacity = 1
        # One object, one shell: there is nothing to rearrange or split, so both
        # searches are skipped (they only cost time) and neither is advertised.
        single_body = capacity < 1 and _single_body(job, manifest)
        rearrange_checked = single_body
        if capacity < 1 and not single_body and _auto_rearrange_eligible(job, latest):
            # Cheapest full-size answer first, before any Orca option search:
            # if every part fits one plate at 100% just by moving/turning
            # parts, do it and say so instead of asking. The check uses the
            # planner the slice path uses, so the plan is one it honors.
            try:
                one_plate = await fit_status.run('trying the parts rearranged on one plate at 100%',
                                                 _heavy(mj.full_size_one_plate, path, state,
                                                        latest.get('color_decisions')))
                rearrange_checked = True
            except Exception:
                logger.exception('one-plate rearrangement check failed; falling back to the full fit search')
                one_plate = None
            if one_plate:
                title = latest.get('title') or 'This model'
                latest['scale'] = 1.0
                latest['full_size_rearrange_plan'] = one_plate
                latest.pop('orientation', None)
                latest.pop('full_size_split_plan', None)
                receipt.update(full_size_rearrange=True, fit_auto_rearranged=True, scale=1.0,
                               fit_options={'rearrange_100': one_plate})
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                mj.write_job(folder, latest)
                await fit_status.finish('fits one plate at 100% rearranged.')
                await adapter.send(chat_id=source.chat_id, content=auto_rearrange_notice(title, one_plate),
                                   metadata=metadata)
                capacity = 1
        if capacity < 1:
            try:
                options = await fit_status.run('trying rotations and smaller sizes',
                                               _heavy(orca_adapter.fit_options, path,
                                                      {**state, 'fit_rearrange_checked': True}
                                                      if rearrange_checked else state))
            except Exception as exc:
                receipt['status'] = 'FIT_UNKNOWN'
                receipt['fit_error'] = str(exc)
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                await adapter.send(chat_id=source.chat_id,
                                   content=diagnostics.known_post(
                                   "I could not finish checking the resize options, so they are unverified.",
                                   not_happened="No slice or print was started.",
                                   subsystem='intake', exc=exc)
                                   or diagnostics.plain_post(
                                   "I could not finish checking the resize options, so they are unverified.",
                                   not_happened="No slice or print was started.",
                                   next_step=diagnostics.next_step_for(
                                       exc,
                                       transient="Send the link or file again to resume the check.",
                                       other="Run `/print-handy bundle` and share the bundle so it can be looked into."),
                                   subsystem='intake', exc=exc),
                                   metadata=metadata)
                return {'action': 'skip', 'reason': 'fit unknown; resumable'}
            title = latest.get('title') or 'This model'
            if options.get('scales') is not None:
                options['part_name'] = options.get('part_name') or title
            # Per-part slicing applies the chosen fit to every slot plate. Offer
            # only the options every slot plate was verified to honor.
            if (latest.get('color_decisions') or {}).get('mode') == 'per_part':
                # The geometry-only rearrange probe does not know the
                # multicolor merge rules (spacing, prime tower), so per-part
                # jobs offer a one-plate rearrangement only when
                # full_size_one_plate proved it above.
                options.pop('rearrange_100', None)
                options = await fit_status.run('checking each color plate',
                                               _heavy(mj.per_part_fit_options, path, latest['color_decisions'], options,
                                                      int((state.get('bed_mm') or BED)[0])))
            if options.get('parts'):
                # Prefer the per-part fit evaluation (#76): a part blocks only
                # when it fits no plate alone at any checked yaw or exceeds the
                # Z limit. Fall back to the authored-extent check for options
                # that carry no per-part evaluation.
                failing = [part for part in options['parts'] if part.get('fits_full_size') is False]
                if not failing and not any('fits_full_size' in part for part in options['parts']):
                    failing = [part for part in options['parts']
                               if max(part['extent_mm'][:2]) > min(state.get('bed_mm') or BED)]
                if failing:
                    options['part_name'] = failing[0]['name']
                    options['extent_mm'] = failing[0]['extent_mm']
                    options['blocking_part'] = failing[0]
                    options['orientations_checked'] = ['authored XY (measured)'] + [r.get('label', 'rotation') for r in options.get('rotations') or []]
            if not options.get('extent_mm'):
                measured = audit_dimensions(job)
                if measured:
                    options['extent_mm'] = measured
            full_size_split = False
            # A full-size single-plate answer (a plain rotation, or every part
            # rearranged onto one plate) ends the search: no split checks.
            rotate_100 = (any(float(r.get('scale') or 0) >= 0.995 and not r.get('supports')
                              for r in options.get('rotations') or [])
                          or bool(options.get('rearrange_100')))
            try:
                if rotate_100 or single_body:
                    raise LookupError('a 100% one-plate fit exists, or there is nothing to split')
                split_plan = await fit_status.run('trying a split onto separate plates at 100%',
                                                  _heavy(split_fit.split_plan_100, path,
                                                         int((state.get('bed_mm') or BED)[0]),
                                                         brim_policy=options.get('brim_policy'), state=state))
            except Exception:
                split_plan = None
            try:
                if rotate_100 or split_plan or single_body:
                    # A concrete split plan supersedes the generic "parts can be
                    # separated" offer, so that check is only needed without one.
                    raise LookupError('a split plan or a 100% one-plate fit already answers this')
                full_size_split = await fit_status.run('trying a split onto separate plates at 100%',
                                                       _heavy(plate_merge.full_size_split_possible, path, manifest,
                                                              int((state.get('bed_mm') or BED)[0])))
            except Exception:
                full_size_split = False
            if split_plan:
                options['split_plan'] = split_plan
            elif full_size_split:
                options['full_size_split'] = True
            picked = fit_choice_rows(options)
            fit_receipt = {'scales': options.get('scales') or [],
                           'rotations': [{k: r.get(k) for k in ('label', 'scale', 'axis', 'dims_mm',
                                                                'supports', 'brim')}
                                         for r in options.get('rotations') or []]}
            if split_plan:
                fit_receipt['split_plan'] = split_plan
            elif full_size_split:
                fit_receipt['full_size_split'] = True
            if options.get('brim_policy'):
                fit_receipt['brim_policy'] = options['brim_policy']
                latest['fit_brim_policy'] = options['brim_policy']
                receipt['fit_brim_policy'] = options['brim_policy']
            if options.get('parts'):
                fit_receipt['parts'] = options['parts']
            if options.get('blocking_parts'):
                fit_receipt['blocking_parts'] = options['blocking_parts']
            receipt['fit_options'] = fit_receipt
            if not any(r['kind'] in ('scale', 'rotate', 'split', 'rearrange') for r in picked):  # nothing printable to offer
                if (latest.get('color_decisions') or {}).get('mode') == 'per_part':
                    message = (f"{title} only fits when resized, and none of the resize options work with "
                               'these per-part colors. Cancel, then resend the link and choose single color to resize it.')
                else:
                    message = f"{title} has no verified fit at 50% or larger in any orientation."
                await _ask(adapter, source, key, message, ['Do not print (Cancel)'], timeout)
                receipt['status'] = 'CANCELLED_OVERSIZED'
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                await adapter.send(chat_id=source.chat_id,
                                   content='Print cancelled. Nothing was sliced, uploaded, or started.',
                                   metadata=metadata)
                return {'action': 'skip', 'reason': 'cancelled oversized model'}
            split_plan = options.get('split_plan')
            if split_plan:
                distribution = []
                names_by_key = {part['key']: part['name'] for part in manifest.get('parts', [])}
                object_by_key = {part['key']: str(part['object_id']) for part in manifest.get('parts', [])}
                yaw_by_object = split_plan.get('yaw_by_object') or {}
                placement_by_object = split_plan.get('placement_by_object') or {}
                shifts_by_plate = split_plan.get('shifts_by_plate') or []
                reorientation_by_object = split_plan.get('reorientation_by_object') or {}
                from print_handy.core.orca_adapter import UP_AXES as _UP_AXES
                for index, part_keys in enumerate(split_plan['plates'], 1):
                    plate_shifts = (shifts_by_plate[index - 1]
                                    if index - 1 < len(shifts_by_plate) else None) or {}
                    labels = []
                    for part_key in part_keys:
                        label = names_by_key.get(part_key, part_key)
                        yaw = yaw_by_object.get(object_by_key.get(part_key, ''))
                        if yaw:
                            label += f' (turned {float(yaw):g}° on the bed)'
                        placement = placement_by_object.get(object_by_key.get(part_key, ''))
                        if placement:
                            label += (f' (placed at {float(placement[0]):g}, {float(placement[1]):g} mm '
                                      'to avoid the bed exclusion)')
                        if object_by_key.get(part_key, '') in plate_shifts:
                            label += ' (rearranged on its shared plate by its actual mesh shape)'
                        reorientation = reorientation_by_object.get(object_by_key.get(part_key, ''))
                        if reorientation:
                            # The offer names the reorientation and its
                            # measured supports decision: choosing the split
                            # is the approval for those supports (#221 R1).
                            face = _UP_AXES[reorientation['axis']][0]
                            label += f' (reoriented: {face}'
                            if reorientation.get('yaw_deg'):
                                label += f", turned {float(reorientation['yaw_deg']):g}° on the bed"
                            if reorientation.get('supports'):
                                label += ', needs supports'
                            if reorientation.get('brim_recommended'):
                                label += ', brim recommended'
                            label += ')'
                        labels.append(label)
                    distribution.append(f"{', '.join(labels)} on plate {index}")
                if options.get('rearrange_100'):
                    lead = (f"{title} does not fit the {_printer_name()} bed as the author laid it out. "
                            'Rearranged, it fits one plate at full size. '
                            f"It also works full size as {len(split_plan['plates'])} plates: ")
                else:
                    lead = (f"{title} does not fit the {_printer_name()} bed full size as one plate, even rearranged. "
                            f"Full size works as {len(split_plan['plates'])} plates: ")
                prompt = (lead + '; '.join(distribution) +
                          '. Choose a fit option, or type ' + fit_hint(picked)[len(FIT_HINT_PREFIX):])
            else:
                prompt = fit_question(title, options, picked)
            # Fits at 100% by rotation alone (no supports): just do it, no question, no announcement.
            silent = next((r for r in picked if r['kind'] == 'rotate' and r['scale'] >= 0.995
                           and not r.get('supports')), None)
            await fit_status.finish('turned to fit at 100%.' if silent is not None else 'choose how to print it.')
            while silent is None:
                scale_pick = await _ask(adapter, source, key, prompt,
                                        [row['label'] for row in picked], timeout,
                                        parser=lambda text: _fit_choice_reply(text, picked))
                row = picked[scale_pick]
                if row['kind'] != 'render':
                    break
                try:
                    await _send_render_preview(adapter, source, path,
                        row.get('scale', 1.0), row.get('rotation'),
                        tuple(state.get('bed_mm') or BED))
                except Exception as exc:
                    await adapter.send(chat_id=source.chat_id,
                        content='I could not make a preview image, so no image was sent. The model itself is unchanged — you can still choose a size.', metadata=metadata)
            if silent is not None:
                row = silent
                receipt['fit_auto_rotated'] = True
            if row['kind'] == 'cancel':
                receipt['status'] = 'CANCELLED_OVERSIZED'
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                await adapter.send(chat_id=source.chat_id,
                                   content='Print cancelled. Nothing was sliced, uploaded, or started.',
                                   metadata=metadata)
                return {'action': 'skip', 'reason': 'cancelled oversized model'}
            brim_policy = row_brim_policy(row, options)
            if brim_policy:
                latest['fit_brim_policy'] = brim_policy
                receipt['fit_brim_policy'] = brim_policy
            else:
                # A turned model keeps its own brim rule (5 mm only when it stands
                # on a small contact patch); the authored layout's policy must not leak in.
                latest.pop('fit_brim_policy', None)
                receipt.pop('fit_brim_policy', None)
            if row['kind'] == 'rearrange':
                latest['scale'] = 1.0
                latest['full_size_rearrange_plan'] = row.get('rearrange_100') or {'rearrange_plate': True}
                latest.pop('orientation', None)
                latest.pop('full_size_split_plan', None)
                receipt['full_size_rearrange'] = True
                receipt['scale'] = 1.0
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                mj.write_job(folder, latest)
            if row['kind'] == 'split':
                latest['plate_mode'] = 'separate'
                latest['scale'] = 1.0
                if row.get('plates'):
                    latest['full_size_split_plan'] = {'plates': row['plates'],
                                                      **({'yaw_by_object': row['yaw_by_object']}
                                                         if row.get('yaw_by_object') else {}),
                                                      **({'placement_by_object': row['placement_by_object']}
                                                         if row.get('placement_by_object') else {}),
                                                      **({'shifts_by_plate': row['shifts_by_plate']}
                                                         if row.get('shifts_by_plate') else {}),
                                                      **({'reorientation_by_object': row['reorientation_by_object']}
                                                         if row.get('reorientation_by_object') else {})}
                latest.pop('orientation', None)
                latest.pop('full_size_rearrange_plan', None)
                receipt['full_size_split'] = True
                receipt['scale'] = 1.0
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                mj.write_job(folder, latest)
            selected_scale = row.get('scale', 1.0)
            rotation = row.get('rotation')
            if row['kind'] in ('split', 'rearrange'):
                # Capacity on the unsplit/unarranged source is zero by definition here; each
                # rearranged or split plate was verified at authored size (a turned
                # plate at exactly the yaw recorded in its split plan).
                capacity = 1
            else:
                capacity = await FitStatus(adapter, source, metadata).run(
                                            'confirming the chosen size on the bed',
                                            _heavy(orca_adapter.orca_plate_capacity, path, state,
                                                   scale=selected_scale,
                                                   **({'rotation': rotation, 'supports': bool(row.get('supports')),
                                                       **({'brim_policy': {'type': 'outer_only',
                                                                           'width_mm': orca_adapter.ORIENTATION_BRIM_MM}}
                                                          if row.get('brim') else {})}
                                                      if rotation else {})))
                if capacity < 1:
                    raise RuntimeError(f'Orca no longer verifies a fit for {row["label"]}')
            if selected_scale < 1.0:
                latest['scale'] = selected_scale
                receipt['scale'] = selected_scale
            if rotation:
                orientation = {k: row[k] for k in ('rotation', 'axis', 'dims_full_mm', 'supports', 'brim', 'label')}
                latest['orientation'] = orientation
                receipt['orientation'] = orientation
            mj.write_job(folder, latest)
        if capacity > 1 and not mj.copies_supported(latest):
            # Only single plate jobs clone copies into the G-code (#37); never offer a count it would lack.
            capacity = 1
            await adapter.send(chat_id=source.chat_id,
                               content='Printing one copy. Extra copies only work for jobs that print on one '
                                       'plate in one color right now.', metadata=metadata)
        quantities = orca_adapter.quantity_buttons(capacity)
        chosen_quantity = 1
        if len(quantities) > 1:
            quantity_choices = quantity_options(capacity)
            qty_labels = [label for label, _ in quantity_choices]
            qty_prompt = (f"How many copies? This plate fits up to {capacity}. Tap a button "
                          f"or type any number from 1 to {capacity}.")
            live_key = (getattr(event, 'metadata', None) or {}).get('live_session_key')
            try:
                picked_quantity = await _ask(adapter, source, key, qty_prompt, qty_labels, timeout,
                                            parser=lambda text: str(parse_quantity(text, capacity)),
                                            live_key=live_key)
                chosen_quantity = (quantity_choices[picked_quantity][1]
                                   if isinstance(picked_quantity, int) else int(picked_quantity))
            except RuntimeError as exc:
                if 'valid button choice' not in str(exc):
                    raise
                await adapter.send(chat_id=source.chat_id,
                                   content='Pick a quantity button or type one number from 1 to '
                                           f'{capacity}. Nothing was changed.', metadata=metadata)
                return {'action': 'skip', 'reason': 'quantity choice unresolved'}
        latest['copies'] = chosen_quantity
        mj.write_job(folder, latest)
        receipt.update(status='COLORS_SAVED', color_decisions=validated, copies=chosen_quantity)
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        # Slice while the confirm question is on screen. Stop and Cancel below discard it,
        # so a job that never asked for G-code keeps none.
        early = runner_for(jp)
        binding = slice_binding(latest, decisions['source_sha256'])
        recipe_sha = binding_sha256(binding)  # the worker refuses to slice any other recipe
        await early.start(jp, binding, lambda job_path, work, runner, copies=chosen_quantity:
                          _heavy(mj.slice_job, str(job_path), recipe_sha256=recipe_sha, **(
                              {'copies': copies} if copies and int(copies) > 1 else {})))
        await early.started.wait()  # armed before the question goes up
        multi = len(manifest['parts']) > 1
        def _color_of(sel):
            sel = sel or {}
            color = str(sel.get('color') or '').strip()
            noun = 'Toolhead' if mj._job_adapter(latest).multi_tool else 'Slot'
            from .printer_state import display_material
            material = display_material(sel.get('material'))
            return ' '.join(v for v in (color[:1].upper() + color[1:], material) if v) or f"{noun} {sel.get('slot')}"
        if validated.get('mode') == 'single_color':
            names = _color_of(validated.get('selection'))
        else:
            names = _choice_summary(manifest, questions, validated.get('choices', {}), _color_of)
        # Never promise "together" before slicing: the planner may still split with a stated reason.
        print_label = 'Slice and print now (bed is clear)'
        cancel_label = 'Cancel print request'
        caveat = (' Parts go on one multicolor bed when they fit; if they must split I will say why and '
                  'let you choose which plate prints first.' if multi and decisions['mode'] == 'per_part' else '')
        spool_text = await _spool_projection_text(mj, latest, manifest, state, chosen_quantity)
        color_text = _color_guess_text(mj, latest, decisions)
        if color_text:
            spool_text = (spool_text + '\n' + color_text) if spool_text else color_text
        source_estimate = _preview_estimate(latest.get('selected_source'))
        estimate_s = source_estimate[0] * chosen_quantity if source_estimate else None
        # No source estimate: there is no time to show yet (the post-slice message gives it).
        estimate_text = '' if estimate_s is None else f"~{mj.human_dur(estimate_s + 300)}"
        if chosen_quantity and chosen_quantity != 1:
            estimate_text = ' · '.join(x for x in (f"{chosen_quantity} copies", estimate_text) if x)
        # Post-slice choices come from the printer's vendor profile ([delivery] methods). Printing is
        # offered only where the vendor offers it AND the operator enabled it in [delivery] methods
        # (for Bambu Lab that means the LAN print opt-in; it is never a default there).
        enabled = delivery.methods()
        can_print = delivery.PRINTER in enabled
        first_labels = [print_label, 'Slice only', 'Stop with colors saved', cancel_label][0 if can_print else 1:]
        choice = await _ask(adapter, source, key,
            ' · '.join(x for x in ('Ready to slice', names, estimate_text) if x)
            + ('\n' + spool_text if spool_text else '') + caveat,
            first_labels, timeout,
            on_shown=early.release.set)
        choice = choice if can_print else choice + 1
        if choice in (2, 3):
            await early.discard()  # the early slice was for a job that will not print
            receipt['status'] = 'CANCELLED' if choice == 3 else 'COLORS_SAVED_STOPPED'
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
            message = ('Cancelled. Nothing was sliced, uploaded, or started.' if choice == 3
                       else 'Source inspected and colors saved. Stopped before slicing; nothing uploaded or started.')
            await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
            return {'action': 'skip', 'reason': 'operator cancelled print request' if choice == 3 else 'operator stopped before slicing'}
        latest = json.loads(jp.read_text())
        if (latest.get('intake_id') != iid or latest.get('color_decisions') != validated
                or mj.file_sha256(latest['downloaded_file']) != decisions['source_sha256']
                or slice_binding(latest, decisions['source_sha256']) != binding):
            await early.discard()  # the slice was for decisions that no longer match
            raise ValueError('source/job changed before slice approval; no handoff')
        logger.info('slice approved')
        receipt.update(status='SLICE_APPROVED', slice_attempt=1,
                       slice_approval={'intake_id': iid, 'source_sha256': decisions['source_sha256'],
                                       'decisions_sha256': mj.decisions_digest(validated), 'attempt': 1,
                                       'recipe_version': RECIPE_VERSION, 'recipe_sha256': recipe_sha})
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        mj._timing(jp, 'intake_slice_requested')
        prep_sent = False  # one combined Preparing + Uploading message after slicing, with the real estimate
        try:
            out = await early.take(binding)  # finished while the question was up, or still running
            if out is None and early.error is not None:
                raise early.error
            if out is None:
                if chosen_quantity and chosen_quantity > 1:
                    out = await _with_stop(adapter, source, key, metadata, "Slicing. Press Stop to cancel.",
                                           _heavy(mj.slice_job, str(jp), copies=chosen_quantity, recipe_sha256=recipe_sha),
                                           allow_stop=True)  # nothing uploaded yet, so Stop is safe
                else:
                    out = await _with_stop(adapter, source, key, metadata, "Slicing. Press Stop to cancel.",
                                           _heavy(mj.slice_job, str(jp), recipe_sha256=recipe_sha),
                                           allow_stop=True)
        except Exception as slice_error:
            logger.exception('slice failed')
            # A failed attempt has no reusable slice approval or G-code. Retry is a
            # separate button action, and its output still needs a fresh start button.
            await early.discard()
            receipt.update(status='SLICE_FAILED', sliced=False, uploaded=False, started=False,
                           slice_error=str(slice_error))
            for stale in ('slice_approval', 'bed_clear_approval', 'gcodes'):
                receipt.pop(stale, None)
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
            # A deterministic refusal says why in plain words and offers its
            # one safe fix instead of a blind retry that would fail the same way.
            from . import slice_refusal
            from . import slice_failure
            refusal = slice_refusal.explain(slice_error)
            if refusal is not None and not refusal.fix:
                await adapter.send(chat_id=source.chat_id, metadata=metadata, content=diagnostics.plain_post(
                    refusal.plain, not_happened='Nothing was sliced, uploaded or started.',
                    next_step=refusal.next_step, subsystem='slicing', exc=slice_error))
                return {'action': 'skip', 'reason': 'slice refused: ' + str(slice_error)}
            if refusal is not None:
                prompt = diagnostics.plain_post(refusal.plain, next_step=refusal.next_step,
                                                subsystem='slicing', exc=slice_error)
                labels = [refusal.fix_label, 'No, cancel']
            else:
                prompt = slice_failure.post(slice_error)
                if not slice_failure.explain(slice_error).retryable:
                    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=prompt)
                    return {'action': 'skip', 'reason': 'slice failed: ' + str(slice_error)}
                labels = ['Retry slice once (no print)', 'Cancel']
            retry = await _ask(adapter, source, key, prompt, labels, timeout)
            if retry == 0 and refusal is not None:
                try:
                    await owned_worker.in_thread(slice_refusal.apply_fix, refusal.fix)
                except Exception:
                    logger.exception('slice refusal fix failed')
                    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                       content='That confirmation could not be saved, so nothing was sliced, '
                                               'uploaded or started. Set the spool with `/spool`, then ask again.')
                    return {'action': 'skip', 'reason': 'slice refusal fix failed'}
            if retry != 0:
                receipt.update(status='CANCELLED', sliced=False, uploaded=False, started=False)
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                await adapter.send(chat_id=source.chat_id, content='Slice retry cancelled. Nothing was uploaded or started.',
                                   metadata=metadata)
                return {'action': 'skip', 'reason': 'slice retry cancelled'}
            latest = json.loads(jp.read_text())
            if (latest.get('intake_id') != iid or latest.get('color_decisions') != validated
                    or mj.file_sha256(latest['downloaded_file']) != decisions['source_sha256']
                    or slice_binding(latest, decisions['source_sha256']) != binding):
                raise ValueError('saved job changed before retry; no slice started')
            attempt = int(receipt.get('slice_attempt') or 1) + 1
            receipt.update(status='SLICE_APPROVED', slice_attempt=attempt,
                           slice_approval={'intake_id': iid, 'source_sha256': decisions['source_sha256'],
                                           'decisions_sha256': mj.decisions_digest(validated), 'attempt': attempt,
                                           'recipe_version': RECIPE_VERSION, 'recipe_sha256': recipe_sha})
            receipt.pop('slice_error', None)
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
            # A prior "slice and print" choice applies only to the failed output.
            # The retry is slice-only; starting the new artifact requires a fresh button.
            choice = 1
            try:
                if chosen_quantity and chosen_quantity > 1:
                    out = await _with_stop(adapter, source, key, metadata, "Retrying the slice. Press Stop to cancel.",
                                           _heavy(mj.slice_job, str(jp), copies=chosen_quantity, recipe_sha256=recipe_sha))
                else:
                    out = await _with_stop(adapter, source, key, metadata, "Retrying the slice. Press Stop to cancel.",
                                           _heavy(mj.slice_job, str(jp), recipe_sha256=recipe_sha))
            except Exception as retry_error:
                logger.exception('slice retry failed')
                await early.discard()
                receipt.update(status='SLICE_FAILED', sliced=False, uploaded=False, started=False,
                               slice_error=str(retry_error))
                receipt.pop('slice_approval', None)
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                again = slice_refusal.explain(retry_error)
                await adapter.send(chat_id=source.chat_id,
                    content=(diagnostics.plain_post(again.plain,
                                                    not_happened='Nothing was sliced, uploaded or started.',
                                                    next_step=again.next_step, subsystem='slicing',
                                                    exc=retry_error) if again is not None else
                             slice_failure.post(retry_error, retry_failed=True)),
                    metadata=metadata)
                return {'action': 'skip', 'reason': 'slice retry failed'}
        mj._timing(jp, 'intake_slice_returned')
        gcodes = [str(g) for g in (out if isinstance(out, list) else [out])]
        sliced = json.loads(jp.read_text())
        # Deliveries are bound to these exact bytes; a file that changes afterwards is refused.
        receipt.update(status='SLICED', sliced=True, gcodes=gcodes,
                       gcode_sha256={g: delivery.sha256(g) for g in gcodes})
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        provenance = sliced.get('provenance') or {}
        if not provenance and (sliced.get('derived_plates') or []):
            provenance = (sliced['derived_plates'][0].get('provenance') or {})
        fallback = provenance.get('fallback') or {}
        if fallback.get('from') == 'OrcaSlicer':
            reason = str(fallback.get('reason') or 'Orca slicer failure')
            logger.info('sliced with the Bambu Studio fallback: %s', logs.redact(reason))
            await adapter.send(chat_id=source.chat_id,
                               content='Sliced with the backup slicer (Bambu Studio) because the main slicer could not slice this model.',
                               metadata=metadata)
        has_dur_q = has_duration_question(getattr(event, 'text', ''))
        breakdown = print_summary.format_estimate_breakdown(sliced)
        if has_dur_q and gcodes:
            # Every plate counts: a split job is the sum of its plates, not plate 1.
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content="Estimated duration:\n" + breakdown)
        plan_id = 'intake-' + iid[:16]
        parts = []
        split = len(gcodes) > 1
        picked_plate = 0
        if split:
            plan = write_split_plan(jp, sliced, manifest, gcodes, plan_id)
            parts = plan['parts']
            listing = '\n'.join(f"- Plate {p['plate']}: {p['label']}" for p in parts)
            notice = (f"These print as {len(parts)} separate plates because {plan['split_reason']}.\n{listing}\n{breakdown}\n"
                      + ("Choose which plate prints first. When it finishes, a picker lists the rest." if can_print
                         else "Each plate is its own file."))
            receipt.update(split_plan=plan_id, split_reason=plan['split_reason'])
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
            await adapter.send(chat_id=source.chat_id, content=notice, metadata=metadata)
            picked_plate = await _pick_plate(adapter, source, key, parts, timeout) if can_print else 0
            if picked_plate is None:
                receipt['status'] = 'SLICED_NOT_STARTED'
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content=f'Sliced. Nothing uploaded or started. Plates stay ready: `/print-next {plan_id}`.')
                return {'action': 'skip', 'reason': 'operator did not start the sliced plate'}
            receipt['first_plate'] = parts[picked_plate]['id']
        first = Path(gcodes[picked_plate])
        exact_job, exact_plate = print_summary.find_job(str(first))
        if exact_job is None or exact_plate is None:
            raise ValueError('sliced artifact has no exact JOB.json record; refusing preparation message and start')
        actual_estimate = mj.gcode_estimate_s(first.read_text())
        slot_lines = ''
        job_adapter = None
        try:
            job_adapter = mj._job_adapter(exact_job)  # the exact job's printer, never "the current one" (#335)
            if job_adapter.multi_tool:  # toolheads, not a slot map (#346)
                mapping = (exact_job or {}).get('tool_mapping') or {}
                if mapping:
                    records = mj.configured_slots(state)
                    lines = []
                    for tool, position in sorted(mapping.items(), key=lambda kv: int(kv[0])):
                        record = records.get(str(position), {})
                        lines.append(f"T{tool} -> Toolhead {position}: "
                                     f"{record.get('color') or 'unknown color'} {record.get('material') or 'unknown material'}")
                    slot_lines = '\n'.join(lines)
            elif job_adapter.multi_slot:  # a single extruder printer has no slot map
                # Only a native adapter can describe an embedded slot map; a
                # multi_slot adapter without one (Bambu AMS, #671) has nothing
                # to describe here, exactly as while it was classified single.
                native = job_adapter.native_or_none
                if native is not None:
                    _map = native.embedded_mapping(first.read_text(), state)
                    if _map:
                        slot_lines = native.describe_mapping(_map, state)
        except Exception as exc:  # a bad map is refused later by print-now; show why now
            slot_lines = f'Heads-up on the filament positions: {exc}'
        estimate_text = (f"~{mj.human_dur(actual_estimate)} printing, ~{mj.human_dur(actual_estimate + 300)} total "
                         "including ~5 min warmup" if actual_estimate is not None else 'estimate unknown')
        target = {'plan_id': plan_id, 'part_id': parts[picked_plate]['id'] if split else 'plate1', 'copy': 1,
                  'gcode': str(first), 'gcode_sha256': mj.file_sha256(first), 'reprint': False,
                  'job_json': str(jp)}
        later = (f" Plate {picked_plate + 1} of {len(parts)} only; a picker for the rest follows when it finishes."
                 if split else '')
        if choice == 1:
            # Slice only never confirmed the bed. One start button for the exact plate, same machinery as /print-next.
            question = (f"Start plate {picked_plate + 1} of {len(gcodes)} ({parts[picked_plate]['label']}) now? "
                        'Starting uploads and prints immediately.'
                        if split else 'Start this exact plate? Starting uploads and prints immediately.')
            if not split:
                question = breakdown + '\n' + question
            if slot_lines:
                question = slot_lines + '\n' + question
            from .spool_remap import confirm_note
            remap = confirm_note(job_adapter, sliced, state)
            if remap:
                question += '\n' + remap
            if delivery.only_printer(enabled):
                go = await _ask(adapter, source, key, question, [print_next.START_LABEL, print_next.CANCEL_LABEL], timeout)
            else:
                if not can_print:
                    question = breakdown + '\nSliced and validated. What should I do with the files?'
                go = await _delivery_menu(adapter, source, key, metadata, timeout, question, enabled,
                                          receipt, receipt_path, jp, gcodes)
            if go != 0:
                receipt['status'] = 'SLICED_NOT_STARTED'
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                tail = f' Plates stay ready: `/print-next {plan_id}`.' if split and can_print else ''
                await adapter.send(chat_id=source.chat_id, content='Sliced. Nothing uploaded or started.' + tail, metadata=metadata)
                return {'action': 'skip', 'reason': 'operator did not start the sliced plate'}
        # One click recorded: bed clear, bound to the exact JOB.json and G-code. print-now still runs every gate
        # (verify-gcode, live readiness, single-use approval, duplicate-start claim).
        receipt['bed_clear_approval'] = {'job_json': str(jp), 'gcode': str(first), 'gcode_sha256': target['gcode_sha256'],
                                         'granted_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'via': 'button'}
        receipt['status'] = 'START_REQUESTED'
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        print_no = json.loads(jp.read_text()).get('print_number')
        # Just the status: the estimate and slot mapping were on the confirmation the operator just answered.
        prep_msg = ' · '.join(['Preparing to start'] + ([f'Print #{print_no}'] if print_no else []))
        mj._timing(jp, 'intake_start_tapped')
        receipt['retry_target'] = target
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        start_context = {'target': target, 'receipt': receipt}
        ok, tail = await _with_stop(adapter, source, key, metadata,
                                    ((prep_msg + "\n") if not prep_sent else "")
                                    + "Uploading to the printer. Press Stop to request cancellation.",
                                    print_next.start(target), allow_stop=True,
                                    refusal_context=start_context)
        mj._timing(jp, 'intake_start_returned', ok=bool(ok))
        started_as = classify_start(ok, tail)
        unknown, pending = started_as.unknown, started_as.pending
        receipt.update(status=started_as.status, started=started_as.started, start_outcome=started_as.outcome)
        if not ok and not pending:
            raw = _raw_preupload_refusal(tail)
            logger.warning('print start refused detail=%s', logs.redact(raw or str(tail)))
            receipt.update(start_refusal_detail=str(tail),
                           uploaded=(True if _printer_rejected(raw) else False) if raw else None,
                           start_prompt_message_id=start_context.get('start_prompt_message_id', ''))
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        warming = ok and print_next.still_preparing(tail)
        # The remote file this start claimed, when warmup is still running. Only a
        # warming start with a known file can be edited by the listener later, so
        # only that start defers its roast (#374).
        remote = print_next.started_file(tail) if warming else None
        if unknown:
            message = START_PENDING_MESSAGE
        elif ok or pending:
            message = print_summary.format_started(exact_job, exact_plate) + later
            if remote is None:
                # Already printing (or warmup cannot be tracked for an edit): this
                # post is the canonical start announcement and carries the one
                # roast now. A tracked warming start gets no roast here — the
                # listener adds exactly one when it edits this post on the
                # warmup-to-printing transition. Refusals below never roast.
                with contextlib.suppress(Exception):
                    roast = await asyncio.wait_for(asyncio.to_thread(
                        _external.load('moonraker_event_listener').generate_roast,
                        (exact_job or {}).get('title') or '', (exact_job or {}).get('summary') or ''), 8)
                    if roast:
                        message += f'\n\n{roast}'
        else:
            message = print_summary.scrub(_refusal_text(tail))
            if _restart_helps(tail):
                message = await _offer_force_restart(adapter, source, key, message, timeout)
            elif start_context.get('prompt_handled'):
                return {'action': 'skip', 'reason': 'start refused; prompt shows the reason and Retry'}
        sent = await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
        message_id = getattr(sent, 'message_id', None)
        if remote and message_id:
            try:
                print_next.record_start_message(remote, message_id)
            except OSError:
                pass  # no recorded id: the listener leaves the start post as is
        return {'action': 'skip', 'reason': 'sliced and start handled by button flow'}
    except (IntakeCancelled, Stopped) as exc:
        logger.warning('intake stopped by operator: %s', type(exc).__name__)
        uncertain_start = isinstance(exc, Stopped) and exc.uncertain
        if jp is not None and not uncertain_start:
            with contextlib.suppress(Exception):
                await runner_for(jp).discard()
        if uncertain_start:
            receipt.update(status='START_UNCERTAIN', sliced=True, uploaded=None, started=None,
                           start_outcome='uncertain', start_error=str(exc))
            message = print_summary.scrub(str(exc))
            reason = 'start outcome uncertain after cancellation'
        else:
            receipt.update(status='CANCELLED', sliced=False, uploaded=False, started=False)
            message = 'Cancelled. Nothing sliced, uploaded or started.'
            reason = 'cancelled by operator'
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
        return {'action': 'skip', 'reason': reason}
    except IntakeExpired:
        logger.warning('intake question expired')
        # The receipt and JOB.json written before this question stay on disk. Resume
        # re-reads them instead of asking for the link again.
        receipt.update(status='EXPIRED', sliced=False, uploaded=False, started=False)
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        try:
            resumed = await _ask(adapter, source, key,
                                 'This question expired. Nothing was sliced, uploaded or started.',
                                 ['Resume'], timeout)
        except (IntakeExpired, IntakeCancelled) as exc:
            # Exceptions raised inside this handler do not reach the sibling
            # cancellation/failure handlers. Close the unanswered recovery prompt
            # explicitly instead of leaving an EXPIRED receipt and escaping.
            receipt.update(status='CANCELLED' if isinstance(exc, IntakeCancelled) else 'FAILED')
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
            await adapter.send(chat_id=source.chat_id,
                               content='Intake closed. Nothing was sliced, uploaded or started.',
                               metadata=metadata)
            return {'action': 'skip', 'reason': 'resume cancelled or expired'}
        saved = receipt.get('job_json')
        if resumed == 0 and saved and Path(saved).is_file():
            job = json.loads(Path(saved).read_text())
            receipt.update(status='RESUMED')
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
            await adapter.send(chat_id=source.chat_id,
                               content=f"Resumed {job.get('title') or 'the saved model'} from the saved intake. "
                                       'Nothing was sliced, uploaded or started.',
                               metadata=metadata)
            continued_event = SimpleNamespace(
                source=source,
                message_id=str(mid) + ':resume',
                text=getattr(event, 'text', ''),
                metadata=getattr(event, 'metadata', None))
            continued = await prepare_link(
                continued_event, adapter, state, root, timeout,
                reuse=resume_job(job), printer_id=printer_id)
            if continued.get('action') == 'skip' and continued.get('reason') == 'duplicate model-link event':
                return {'action': 'skip', 'reason': 'resume could not continue the saved intake'}
            return continued
        await adapter.send(chat_id=source.chat_id,
                           content='Expired before any choice was saved. Send the link again. '
                                   'Nothing was sliced, uploaded or started.',
                           metadata=metadata)
        return {'action': 'skip', 'reason': 'expired before a choice'}
    except Exception as exc:
        logger.exception('intake failed')
        receipt.update(status='FAILED', error=str(exc))
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        from .intake_resources import IntakeLimitExceeded
        if isinstance(exc, IntakeLimitExceeded):
            # A curated, user-phrased refusal (its text names the limit and
            # the remedy): keep its reason, lead plainly, drop its prefix.
            reason = str(exc).removeprefix('Model intake refused: ')
            content = (f'That model could not be prepared. {reason}')
        elif isinstance(exc, SiteLoginNeeded):
            # Curated and user-phrased by site_accounts: the text names the
            # token file and how to fill it, so keep it — the generic wording
            # would hide the remedy.
            content = (f'That model could not be downloaded: {exc} '
                       'Nothing was sliced, uploaded or started.')
        elif isinstance(exc, mj.DirectLinkError):
            # Also curated and user-phrased (#607): a direct file link that
            # returned a web page, pointed at a non-public address, or could
            # not be reached. The text names the cause and the remedy, so
            # keep it — the generic wording would hide both.
            content = (f'That file could not be fetched. {exc}')
        elif isinstance(exc, ValueError) and str(exc).startswith('requires one to four distinct configured'):
            # Also curated and user-phrased: a multi-slot/toolhead printer
            # with nothing configured. The text names the exact setup gap,
            # so keep it — the generic wording would hide the remedy.
            content = (f'That model could not be prepared: this printer {exc}. '
                       'Nothing was sliced, uploaded or started.')
        elif diagnostics.known_cause(exc) is not None:
            # #590: the failure carries a specific, user-safe cause (a
            # missing site token, a site download gap, ...): name it and
            # its remedy instead of the generic wording, its UNKNOWN
            # marker and a resend that cannot help.
            content = diagnostics.known_post(
                'Preparing that model stopped before it was ready.',
                not_happened='Nothing was sliced, uploaded or started.',
                subsystem='intake', exc=exc)
        else:
            content = diagnostics.plain_post('Preparing that model stopped before it was ready.',
                                               not_happened='Nothing was sliced, uploaded or started.',
                                               next_step=diagnostics.next_step_for(
                                                   exc,
                                                   transient='Send the link or file again; if it stops again, run `/print-handy bundle` and share the bundle so it can be looked into.',
                                                   other='Run `/print-handy bundle` and share the bundle so it can be looked into.'),
                                               subsystem='intake', exc=exc)
        await adapter.send(chat_id=source.chat_id, content=content, metadata=metadata)
        return {'action': 'skip', 'reason': 'source-first intake failed closed'}
    finally:
        if jp is not None:
            drop_runner(jp)
        _LIVE_KEY.reset(live_token)
        logs.unbind(log_token)
