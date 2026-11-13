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
import shutil
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from typing import NamedTuple
from urllib.parse import urlsplit

from .. import diagnostics, logs, printers, settings
from . import _external, delivery, failure_report, owned_worker, plate_merge, purge, split_fit

from ..intake.fit_flow import (  # moved in 2.3.0 (#1323); old path kept for one release
    BED, FIT_FALLBACK_HEARTBEAT_S, FIT_HEARTBEAT_S, FIT_HINT_PREFIX, FIT_QUIET_S, FitStatus,
    LARGE_MESH_MB, SCALE_LABEL, SCALE_STEP_PERCENTS, _auto_rearrange_eligible, _elapsed_text,
    _fit_choice_reply, _fit_failure, _fit_state, _painted_no_fit_detail, _pct, _printer_name,
    _rotation_phrase, _scale_direction_reply, _scale_fit_offer_reply, _scale_percent_reply,
    _single_body, _unit_oversize_reply, audit_dimensions, auto_rearrange_notice, fit_choice_rows,
    fit_hint, fit_question, large_model_notice, row_brim_policy, write_split_plan,
)
FILAMENT_SLOTS = settings.current()['printer']['filament_slots']
print_summary = _external.lazy('discord_print_summary')
print_next = _external.lazy('print_next')
from .link_intake import ask, model_link, has_duration_question  # noqa: E402
from .quantity_selection import quantity_options, parse_quantity  # noqa: E402
from .threemf_canonical import intake_working_copy  # noqa: E402
from .background_slice import drop_runner, runner_for, slice_binding  # noqa: E402
from . import slice_warnings  # noqa: E402
from . import status_voice  # noqa: E402

from ..intake.support_flow import (  # moved in 2.3.0 (#1323); old path kept for one release
    PRINT_NO_SUPPORTS_LABEL, RESLICE_SUPPORTS_LABEL, supports_decision, supports_recommended,
    with_supports_line,
)
from .slice_recipe import RECIPE_VERSION, binding_sha256  # noqa: E402


from ..intake.worker import (  # moved in 2.3.0 (#1323); old path kept for one release
    _LOG_LEVELS, _OFFLOAD, _SLICER_STEPS, _STEP_LABELS, _WORKER_LOG_LINE, _await_background_slice,
    _forward_worker_log_lines, _heavy, _prefetch,
)


logger = logging.getLogger(__name__)


def _voice_identity(source):
    """Scope first-time copy jokes to a user and platform, not the job."""
    platform = getattr(getattr(source, 'platform', None), 'value', None) or 'discord'
    user_id = getattr(source, 'user_id', None)
    if user_id:
        return f'{platform}:user:{user_id}'
    return f"{platform}:chat:{getattr(source, 'chat_id', 'unknown')}"


def _voiced(text, stage, source, key):
    """``text`` plus one optional quip for a stage this intake really entered.

    ``key`` is the intake id: a re-render or retry of the same stage shows the
    same line, and an empty quip leaves ``text`` byte-identical.
    """
    return status_voice.voiced(text, stage, identity=_voice_identity(source), event_key=f'{key}:{stage}')


def _voice(stage, source, key):
    """A deferred quip for a FitStatus phase, chosen only if the phase is shown."""
    return lambda: status_voice.quip(stage, identity=_voice_identity(source), event_key=f'{key}:{stage}')


STOP_LABEL = "Stop"
STOP_CUSTOM_ID = "pbstop:"
RETRY_CUSTOM_ID = "pbretry:"
# Set by the print-feedback plugin: async (source, text, clarify_id) -> sent message with a single
# Stop button. None (tests, no Discord bot) falls back to the gateway clarify prompt.
STOP_SENDER = None
# Set by the plugin to edit the original Uploading/Stop prompt and install Retry after a safe refusal.
REFUSAL_EDITOR = None


class Stopped(BaseException):
    """The operator pressed Stop on a progress message."""

    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def stamp_started_by(job, source):
    """Stamp who asked for this print on the job dict, in place.

    The terminal post @mentions only that user on Discord
    (discord_print_summary's starter mention), so the requester has to be
    recorded where the completion path can find it: JOB.json, from where
    record_start carries it into the start context and the print record.
    A re-intake (reprint/reuse) re-stamps: the new requester is the starter
    of the new print. No user id, no stamp — a starter is never invented
    for a job nobody here requested.
    """
    starter_id = str(getattr(source, 'user_id', '') or '')
    if starter_id:
        job['started_by'] = {
            'platform': getattr(getattr(source, 'platform', None), 'value', None) or 'discord',
            'user_id': starter_id}


def _new_stages(timing_path, offset):
    """Stage names appended to a job's TIMING.jsonl after byte ``offset``; never raises."""
    try:
        with open(timing_path, 'rb') as handle:
            handle.seek(offset)
            tail = handle.read()
    except OSError:
        return set()
    stages = set()
    for line in tail.decode('utf-8', 'replace').splitlines():
        with contextlib.suppress(ValueError, AttributeError):
            stages.add(json.loads(line).get('stage'))
    return stages


async def _watch_start_progress(adapter, source, metadata, timing_path, offset, clock):
    """Post each START_PROGRESS note once its stage has lasted its grace period.

    A note whose later stage was already reached is stale and never posted; the watcher
    ends after the last note (or is cancelled when the start returns).
    """
    seen, posted = {}, set()
    last = START_PROGRESS[-1][0]
    while last not in posted:
        await asyncio.sleep(START_PROGRESS_POLL_S)
        stages = _new_stages(timing_path, offset)
        now = clock()
        for stage, _grace, _text in START_PROGRESS:
            if stage in stages:
                seen.setdefault(stage, now)
        for index, (stage, grace, text) in enumerate(START_PROGRESS):
            later = any(name in seen for name, _g, _t in START_PROGRESS[index + 1:])
            if stage in posted or stage not in seen or later or now - seen[stage] < grace:
                continue
            posted.add(stage)
            # A note is progress only: delivery failure never touches the start itself.
            with contextlib.suppress(Exception):
                if stage in START_PROGRESS_VOICE:
                    text = _voiced(text, START_PROGRESS_VOICE[stage], source, Path(timing_path).parent.name)
                await adapter.send(chat_id=str(source.chat_id), content=text, metadata=metadata)


async def _with_start_progress(adapter, source, metadata, job_path, work, clock=time.monotonic):
    """Await the start ``work`` unchanged while relaying its TIMING.jsonl progress (#734).

    ``work`` is awaited directly, so Stop, cancellation and its result behave exactly as
    without this wrapper; the watcher only posts notes and is stopped when work ends.
    """
    adapter_state = getattr(adapter, '__dict__', {})
    if 'picks' in adapter_state or 'clicks' in adapter_state or job_path is None:
        return await work  # scripted test adapters record questions only, as _with_stop does
    timing_path = Path(job_path).parent / 'TIMING.jsonl'
    try:
        offset = timing_path.stat().st_size
    except OSError:
        offset = 0
    watcher = asyncio.ensure_future(_watch_start_progress(adapter, source, metadata, timing_path, offset, clock))
    try:
        return await work
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await watcher


def _color_prompt_follows(refusal_context, tail):
    """True while the intake will ask Print anyway / Set screen / Cancel about this color refusal (#795).

    Only then is the Retry refusal text held back; when no question follows (the last round), the
    refusal keeps its guidance plus Retry like any other pre-upload refusal."""
    from . import material_accept
    return bool(refusal_context.get('color_prompt') and (color_confirm(tail) or label_confirm(tail)
                                                         or material_accept.pending(tail)
                                                         or SPOOL_CHANGED in str(tail)))


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
    from . import prompts as _prompt_wait
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
    waiter = asyncio.ensure_future(_prompt_wait.await_answer(cid, 3600, registry=cm))
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
                    and not result[0] and _safe_preupload_refusal(result[1])
                    and not _color_prompt_follows(refusal_context, result[1])):
                detail = str(result[1])
                refusal_context['start_prompt_message_id'] = str(getattr(stop_msg, 'id', '') or '')
                if REFUSAL_EDITOR is not None:
                    with contextlib.suppress(Exception):
                        handled = bool(await REFUSAL_EDITOR(source, stop_msg, _launch_refusal_text(detail),
                                                            refusal_context, detail))
                if not handled:
                    with contextlib.suppress(Exception):
                        await stop_msg.edit(content=_launch_refusal_text(detail), view=None)
                        handled = True
                refusal_context['prompt_handled'] = handled
            if not handled:
                with contextlib.suppress(Exception):
                    await stop_msg.edit(view=None)
        elif sent is not None and getattr(sent, "message_id", None):
            # Clarify path (#524 stage 2): the standing prompt is Print
            # Handy's own question message — it carries a message id and
            # there is no Stop view to retire. Complete it in place: its
            # buttons are retired through the adapter, the same end state
            # a click produces.
            handled = False
            if (refusal_context is not None and isinstance(result, tuple) and len(result) >= 2
                    and not result[0] and _safe_preupload_refusal(result[1])
                    and not _color_prompt_follows(refusal_context, result[1])):
                detail = str(result[1])
                refusal_context['start_prompt_message_id'] = str(sent.message_id)
                refusal_context['prompt_handled'] = handled
            if not handled:
                retire = getattr(adapter, "retire_prompt", None)
                if retire is not None:
                    with contextlib.suppress(Exception):
                        await retire(cid)
    return result





# Start progress (#734): print-now runs as a child process and its result arrives only at
# the end, while a real start spends 25-75 s between the tap and that result (the start
# request itself, then the readback while the printer prepares). The child already logs its
# stages to <job>/TIMING.jsonl as they happen; these short notes relay the two that matter,
# each only when that stage has lasted its grace period, so a fast start posts nothing extra.
START_PROGRESS_POLL_S = 1.0
# How long the Ready question waits for the speculative with-supports slice,
# so that option shows its numbers before it can be chosen (#1373).
SUPPORTS_ESTIMATE_WAIT_S = 120.0
# Rounding slack between the grams the Ready question showed and the slice's own figure.
SHORTAGE_PREACCEPT_SLACK_G = 0.5


def _preaccepted_shortage(choice, shown_shortages, job_json):
    """The material acceptance a print button under a shown shortage already gave, or None.

    The Ready question showed this spool as short and the operator pressed a print
    button under it: that is the answer, so the start does not ask again (#1373).
    Only for one short spool, only while the slice needs no more than was shown
    (rounding aside), and never under yolo, whose waiver must be posted at the
    start (2.3.2 review). A multi-slot job's per-slot grams are only known to the
    start gate, so it is asked there. Bound to this G-code like any acceptance.
    """
    from . import yolo as _yolo
    if choice != 0 or not shown_shortages or len(shown_shortages) != 1 or _yolo.ACTIVE.get():
        return None
    key, shown_g = shown_shortages[0]
    sliced_g = None
    with contextlib.suppress(Exception):
        from print_handy.runtime import discord_print_summary as dps
        plates = dps.plate_estimates(json.loads(Path(job_json).read_text()))
        grams = [p.get('filament_g') for p in plates or [] if p.get('filament_g') is not None]
        sliced_g = float(sum(grams)) if grams else None
    if sliced_g is None or sliced_g > shown_g + SHORTAGE_PREACCEPT_SLACK_G:
        return None
    return [key]


PRINT_ANYWAY_RISK_LABEL = 'Print anyway, accept the risk (bed is clear)'
SLICE_ANYWAY_RISK_LABEL = 'Slice anyway (accept the risk)'
START_PROGRESS = (
    ('upload_verified', 5.0, 'Uploaded to the printer. Sending the start now; '
                             'some printers take up to a minute to answer.'),
    # The readback after an accepted start usually confirms within a few
    # seconds; a shorter grace posted this note just before "Print started".
    ('start_submitted', 10.0, 'The printer accepted the start. It may load filament and heat for a '
                              "few minutes before printing; I'll post when the start is confirmed."),
)
# The status voice stage each START_PROGRESS note really reports. The upload
# is over when the first note posts and the start is being sent, so it
# carries a print_start line, never an upload one; the second note carries
# none (one quip per start).
START_PROGRESS_VOICE = {'upload_verified': 'print_start'}




























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
        elif (job or {}).get('site') == 'upload':
            # An uploaded file has no model page to quote.
            text += ' I will report time and filament after slicing.'
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


async def send_preview(adapter, source, job, row, metadata, state=None, voice_key=None):
    """Best effort, display only: no printer, Moonraker, Spoolman, HA or persistence I/O."""
    try:
        text = preview_sentence(job, row, _filament_note(job, state) if state is not None else None)
        if text and voice_key and (job or {}).get('site') != 'upload':
            text = _voiced(text, 'link_intake', source, voice_key)  # the link lookup really ran
        if text:
            await adapter.send(chat_id=source.chat_id, content=text, metadata=metadata)
    except Exception:
        pass














from ..intake.questions import (  # moved in 2.3.0 (#1323); old path kept for one release
    COLOR_CONFIRM_ROUNDS, PRINT_ANYWAY_LABEL, SET_SCREEN_LABEL, _COLOR_NAMES, _CONFIRM_COLOR,
    _CONFIRM_LABEL, _LIVE_KEY, _NOISE, _ask, _color_guess_text, _color_phrase,
    _raw_preupload_refusal, _retry_choice_reply, _typo_words, accept_color_mismatch,
    accept_label_mismatch, clear_color_acceptance, clear_label_acceptance, color_confirm,
    color_confirm_question, label_confirm, label_confirm_question, screen_unpushed_note,
    SPOOL_CHANGED, USE_LOADED_SPOOL_LABEL,
)






















MODEL_FILE_EXTS = {'.3mf', '.stl'}


def model_files(event):
    """Return valid STL/3MF uploads from one message, preserving their order."""
    return [path for p in (getattr(event, 'media_urls', None) or [])
            if (path := Path(str(p))).suffix.lower() in MODEL_FILE_EXTS and path.is_file()]


def archive_files(event):
    """Dropped .zip archives of model sources (#1204 item 3); a .3mf is never one of these."""
    from .source_archive import ARCHIVE_EXTS
    return [path for p in (getattr(event, 'media_urls', None) or [])
            if (path := Path(str(p))).suffix.lower() in ARCHIVE_EXTS and path.is_file()]


def model_file(event):
    """Single-file compatibility selector for existing design/edit callers."""
    hits = model_files(event)
    return hits[0] if len(hits) == 1 else None


def upload_job(path):
    """Local job for a file the operator downloaded themselves; same shape as a one-profile site inventory."""
    name = re.sub(r'^doc_[0-9a-f]{12}_', '', Path(path).name)
    stem = Path(name).stem
    return {'site': 'upload', 'model_id': stem, 'title': stem, 'slug': stem, 'license': None, 'summary': '',
            'sources': [{'n': 1, 'name': name, 'title': stem, 'path': str(path)}]}


def upload_job_set(paths):
    """Expose a group of local files through the same parts inventory as model sites."""
    paths = [Path(p) for p in paths]
    if not paths:
        raise ValueError('no printable uploads provided')
    if len(paths) == 1:
        return upload_job(paths[0])
    if not all(p.is_file() and p.suffix.lower() in MODEL_FILE_EXTS for p in paths):
        raise ValueError('all project uploads must be existing STL or 3MF files')
    names = [re.sub(r'^doc_[0-9a-f]{12}_', '', p.name) for p in paths]
    return {
        'site': 'upload', 'model_id': 'uploaded-parts', 'title': f'Uploaded project ({len(paths)} files)',
        'slug': 'uploaded-parts', 'license': None, 'summary': '', 'source_kind': 'upload_group',
        'sources': [{'n': i, 'name': name, 'title': Path(name).stem, 'path': str(path)}
                    for i, (name, path) in enumerate(zip(names, paths), 1)],
    }


ARCHIVE_STAGING_KEEP_S = 7 * 24 * 3600


def archive_job(archives, staging):
    """A dropped ZIP as the same parts inventory a multi-file upload gets (#1204 item 3).

    source_archive enumerates and extracts the printable STL/3MF entries
    (everything else is reported, never extracted); the extracted files go
    through upload_job_set unchanged, so the part chooser, combiner and
    project manifest are the ones every other source uses. Each source row
    keeps its path inside the archive.
    """
    import shutil
    import time as _time
    from . import source_archive
    if len(archives) != 1:
        raise source_archive.ArchiveRefused(
            f'that message has {len(archives)} ZIP files; send one ZIP per message.')
    staging = Path(staging)
    with contextlib.suppress(OSError):
        for old in staging.parent.glob('*.archive'):
            if old != staging and _time.time() - old.stat().st_mtime > ARCHIVE_STAGING_KEEP_S:
                shutil.rmtree(old, ignore_errors=True)
    paths, report = source_archive.extract(archives[0], staging)
    job = upload_job_set(paths)
    for row, part in zip(job['sources'], report['printable']):
        row['archive_path'] = part['archive_path']
    stem = Path(re.sub(r'^doc_[0-9a-f]{12}_', '', Path(archives[0]).name)).stem
    report['archive'] = Path(re.sub(r'^doc_[0-9a-f]{12}_', '', Path(archives[0]).name)).name
    job['source_kind'] = 'upload_archive'
    job['source_archive'] = report
    if len(paths) > 1:
        job.update(title=f'{stem} ({len(paths)} files)', model_id=stem, slug=_slug(stem) or 'uploaded-archive')
    return job


def _slug(text):
    return re.sub(r'[^a-z0-9]+', '-', str(text).lower()).strip('-')[:60]


def gcode_3mf_message(name):
    """The plain refusal for a sliced G-code package sent as a model (#922 gap 9)."""
    return (f'{name} is an already-sliced G-code package (.gcode.3mf), not an unsliced 3MF mesh. '
            'Send the original unsliced .3mf or .stl model instead, so it can be sliced for this '
            'printer. Nothing was sliced or started.')


def is_gcode_3mf(path):
    """True for an already-sliced .gcode.3mf package (#922 gap 9).

    Detected by the double suffix, or by content: a package carrying
    Metadata/plate_*.gcode whose root model has an empty <build> (no build
    items, hence no unsliced mesh inventory). A normal unsliced 3MF with
    plate G-code metadata but real build items is not one.
    """
    path = Path(path)
    if path.name.lower().endswith('.gcode.3mf'):
        return True
    if path.suffix.lower() != '.3mf' or not path.is_file():
        return False
    import zipfile
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if not any(re.fullmatch(r'Metadata/plate_\d+\.gcode', n) for n in names):
                return False
            if '3D/3dmodel.model' not in names:
                return False
            model = archive.read('3D/3dmodel.model').decode('utf-8', 'replace')
    except Exception:
        return False
    build = re.search(r'<build[^>]*>(.*?)</build>', model, re.S)
    return build is not None and '<item' not in build.group(1)


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
    r"\b(?:re-?print|print again|do it again|print (?:it|that|this|that one|this one) again"
    r"|(?:print|slice|make|do) (?:the |that )?(?:last|previous|same) (?:one|print|model|thing|job)"
    r"|(?:print|slice|make|do) (?:the )?(?:last|previous) one|(?:print|make) (?:another|one more))\b", re.I)


# "don't print it again", "never print that again", "no need to reprint":
# a negated reprint phrase is the opposite of a request (#979 review).
_REPRINT_NEGATION_RE = re.compile(
    r"\b(?:don'?t|do not|never|no need to|not|stop|without)\s+(?:\w+\s+){0,2}?"
    r"(?:re-?print|print|do|make|slice)\b", re.I)
# "why did it print again?", "did it print again?", "how come it printed again":
# a question about what happened is not a request to print (#979 review).
# "can you print it again?" / "could you reprint that?" stay requests.
_REPRINT_QUESTION_RE = re.compile(
    r"^\s*(?:why|how come|what|when|where|did|was|is|does|has|have|were)\b", re.I)


def reprint_request(text):
    """'print the previous print in black', 'reprint that', 'print it again'.

    Never when a link is present, never when the phrase is negated ("don't
    print it again"), and never in a question about what happened ("why did
    it print again?").
    """
    text = str(text or '')
    if 'http' in text.lower() or not REPRINT_RE.search(text):
        return False
    return not (_REPRINT_NEGATION_RE.search(text) or _REPRINT_QUESTION_RE.match(text))


class LastPrintNotReusable(LookupError):
    """The most recent print cannot be prepared again (#979 review).

    Raised instead of silently falling back to an older print: the operator
    asked for the last print, and an older model is not it.
    """

    def __init__(self, title, reason):
        super().__init__(f'{title}: {reason}')
        self.title = title
        self.reason = reason


def newest_archived_job(archive=None):
    """Newest archived job by JOB.json mtime whose source file is still on disk, else None.

    This is a record-recency question — the last intake whose job record was
    rewritten (prepared, re-sliced, edited, resumed) — and deliberately NOT
    "the last print": a job that was never printed can win it, and a printed
    job loses it the moment any other record is touched. Reprint target
    selection must use last_printed_job (launch records) instead (#979).
    Local files only.
    """
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


def last_printed_job(archive=None, printer_id=None):
    """The archived job that was actually printed most recently, else None.

    Selection follows launch records — the same source /print-again resolves
    from — so natural-language reprints name the job that printed last, not
    whichever archived record was rewritten most recently (#979). With
    printer_id, only launches whose job record belongs to that printer count.

    Only the newest launch is ever the answer. When its job can no longer be
    re-sliced (no selected source, or the source file is gone) this raises
    LastPrintNotReusable naming it -- it never falls back to an older launch,
    which would be a different model than the one the operator meant (the
    #979 failure itself). /print-again can still reprint that job's exact
    G-code. Local files only; read only.
    """
    from ..runtime import print_control as pc
    from . import model_job as mj
    archive = Path(archive or mj.ARCHIVE).resolve()
    for record in pc._launch_records(archive):
        job_path = pc._job_for(pc._artifact_for(record).resolve(), archive)
        if job_path is None:
            continue
        try:
            job = json.loads(job_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if printer_id is not None and str(mj._job_printer_id(job)) != str(printer_id):
            continue
        title = str(job.get('title') or job_path.parent.name)
        if not job.get('selected_source'):
            raise LastPrintNotReusable(title, 'it has no saved source model to slice again')
        f = Path(job.get('downloaded_file') or '')
        if not (f.is_file() and f.stat().st_size > 0):
            raise LastPrintNotReusable(title, 'its source model file is no longer on disk')
        return job
    return None


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


import os  # noqa: E402  the receipt-owner liveness lock below
try:  # noqa: E402
    import fcntl
except ImportError:  # non-POSIX platform: no liveness lock, the platform check still applies
    fcntl = None

_OWNER_LOCK = None  # this process's held liveness lock beside the receipts (an fd), once claimed
_OWNER_ID = re.compile(r'[0-9a-f]{32}')


def _owner_lock_path(root, owner):
    return Path(root) / '.owners' / f'{owner}.lock'


def claim_receipt_owner(root):
    """Hold this process's liveness lock beside the intake receipts, for the life of the process.

    The web chat runs BotCore in the Web UI process while the chat bot runs in its own, and both
    write receipts into the same folder. A receipt's ``owner`` names the process that wrote it; the
    lock tells another process whether that owner is still running. An OS file lock is released by
    the kernel when its holder dies, so a crash or a restart frees it and a reused PID cannot fake
    it. Where file locks are unavailable nothing is held, and the owner reads as gone (the
    platform check in ``announce_interrupted`` still keeps processes off each other's receipts).
    """
    global _OWNER_LOCK
    if _OWNER_LOCK is not None or fcntl is None:
        return
    fd = None
    try:
        path = _owner_lock_path(root, owned_worker._OWNER)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _OWNER_LOCK = fd
    except OSError:
        if fd is not None:
            os.close(fd)


def _owner_alive(root, owner):
    """True while the process that wrote a receipt still holds its liveness lock."""
    if fcntl is None or not _OWNER_ID.fullmatch(str(owner or '')):
        return False
    path = _owner_lock_path(root, owner)
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False  # no lock file: an owner from before liveness locks, or one long gone
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True  # held: that process is running
        with contextlib.suppress(OSError):
            path.unlink()  # its holder is gone; the file is litter
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _receipt_platform_matches(receipt, platform):
    """Whether this process's platform announces this receipt.

    A receipt names the chat platform it was written for. A receipt from before that field existed
    belongs to the chat bot, never to web chat: the web chat process only ever announces its own.
    """
    written = receipt.get('platform')
    if written:
        return str(written) == str(platform)
    return str(platform) != 'webui'


async def announce_interrupted(root, adapter, now=None, platform=None):
    """Tell each chat whose intake a restart stranded, once. Returns the intake ids announced.

    Only receipts this version wrote (they carry chat_id and owner) for this process's own platform
    (``platform``, by default the adapter's name), written by another Print Handy process that is no
    longer running, still in an in-progress status and recent. The web chat and the chat bot run in
    separate processes over the same receipt folder, so starting one never touches the other's live
    intake. The receipt is marked INTERRUPTED before the post, so a failing post or another restart
    never repeats it. Nothing is resumed: no approval survives.
    """
    root = Path(root)
    now = time.time() if now is None else now
    platform = platform or getattr(adapter, 'name', None) or 'discord'
    claim_receipt_owner(root)
    announced = []
    try:
        paths = sorted(root.glob('*.json'))
    except OSError:
        return announced
    stranded = {}  # (chat, thread) -> intake ids; one restart is one notice per conversation
    for path in paths:
        try:
            receipt = json.loads(path.read_text())
            age = now - path.stat().st_mtime
        except (OSError, ValueError):
            continue
        if (not isinstance(receipt, dict) or receipt.get('status') not in IN_PROGRESS_STATUSES
                or not receipt.get('chat_id') or receipt.get('owner') in (None, owned_worker._OWNER)
                or receipt.get('interrupted_notified') or age > INTERRUPTED_MAX_AGE_S
                or not _receipt_platform_matches(receipt, platform)
                or _owner_alive(root, receipt.get('owner'))):
            continue
        receipt.update(status='INTERRUPTED', interrupted_notified=True, interrupted_from=receipt['status'])
        try:
            path.write_text(json.dumps(receipt, indent=2) + '\n')
        except OSError:
            continue
        key = (str(receipt['chat_id']), receipt.get('thread_id'))
        stranded.setdefault(key, []).append(receipt.get('intake_id') or path.stem)
        announced.append(receipt.get('intake_id') or path.stem)
    for (chat_id, thread), ids in stranded.items():
        what = 'that model' if len(ids) == 1 else f'{len(ids)} models'
        again = 'the link or file' if len(ids) == 1 else 'each link or file'
        try:
            await adapter.send(
                chat_id=chat_id, metadata={'thread_id': thread} if thread else None,
                content=f'Print Handy restarted while it was preparing {what}, so the preparation '
                        f'stopped. No slice was finished, and nothing was uploaded or started. '
                        f'Send {again} again to start over.')
        except Exception:
            logger.warning('could not post the interrupted-intake note', exc_info=True)
    return announced




from .link_intake import PromptCancelled as IntakeCancelled  # noqa: E402  typed Cancel/Stop at any prompt
from .link_intake import PromptExpired as IntakeExpired  # noqa: E402  button question reached its timeout
from .site_accounts import SiteLoginNeeded  # noqa: E402  a download needs the operator's site login (#464)


class ConnectCancelled(IntakeCancelled):
    """The operator pressed Cancel on a connect prompt (#712): CANCELLED, nothing downloaded."""


# Sites whose credential can never unlock a download (#712): a missing or
# refused login there is refused plainly, never with the connect walkthrough.
NO_DOWNLOAD_SITES = {'cults3d'}


def _unsupported_for(site):
    from . import model_job as mj
    if site == 'cults3d':
        return mj.DownloadUnsupported(mj.CULTS_REFUSAL, 'Cults3D does not allow app downloads')
    return None


async def _spool_projection_text(mj, job, manifest, state, copies, shortages=None):
    """Display only: filament needed vs the chosen spool's grams. Never blocks the intake.

    ``shortages``, when given, collects the filament-shortage warning keys
    (``material_accept.key('filament', slot, spool_id)``) of the spools this
    text shows as short. A print button pressed under that text is the
    operator's answer to them, so the start does not ask again (#1373).
    """
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
        if shortages is not None and not getattr(weights, 'fetch_failed', False):
            from . import material_accept
            for row in rows:
                if (row.get('projected_remaining_g') is not None and row['projected_remaining_g'] < 0
                        and row.get('spoolman_id') is not None):
                    shortages.append((material_accept.key('filament', row.get('slot'), row['spoolman_id']),
                                      float(row.get('estimated_used_g') or 0)))
        text = mj.format_spool_projection(
            rows, position_noun='Toolhead' if mj._job_adapter(job).tool_based else 'Slot')
        if getattr(weights, 'fetch_failed', False):
            # The amounts above fell back to the slot record because
            # Spoolman did not answer (#839): say so on the same post, or
            # cached amounts read as a live check that found enough.
            note = ("Spoolman didn't answer, so the amounts above are the last recorded "
                    "ones, not a live check.")
            text = f'{text}\n{note}' if text else note
        return text
    except Exception:
        # Fail soft (#387) but not silently (#839): when Spoolman is
        # configured, the operator hears the check was skipped instead of
        # mistaking a missing line for "enough filament". Installs without
        # Spoolman stay quiet.
        try:
            from .printer_state import spoolman_url
            if spoolman_url(state):
                return ("Filament check skipped: Spoolman didn't answer, so the spool amounts were "
                        "not checked (spool tracking skipped for this job; the print can still start).")
        except Exception:
            pass
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
    from .site_accounts import accept_pasted, chat_safe, display_path, save_token

    def parse(text):
        try:
            return _saved_reply(text)
        except ValueError:
            pass
        token, problem = accept_pasted(site, text)
        if problem:
            raise ValueError(chat_safe(problem))
        try:
            save_token(site, token)
        except OSError as exc:
            raise ValueError(
                f"I couldn't save it to {display_path(site)} ({exc.strerror or type(exc).__name__}). "
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
            unsupported = _unsupported_for(exc.site) if exc.site in NO_DOWNLOAD_SITES else None
            if unsupported is not None:
                raise unsupported from None  # no walkthrough for a login that can't help (#712)
            if connect_round == MAX_CONNECT_ROUNDS:
                raise
            if not await _connect_account(adapter, source, key, metadata, exc, timeout):
                raise ConnectCancelled('connect cancelled') from None


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
    try:
        saved = await _ask(adapter, source, key, question, [SAVED_LABEL, 'Cancel'], timeout,
                           parser=_connect_reply(exc.site))
    except IntakeCancelled:
        return False
    return saved in (0, SAVED_LABEL)






_HEATER_TARGET = re.compile(
    r'(rightTargetTemp|platTargetTemp)\s*==\s*0\s*\(observed\s*([0-9]+(?:\.[0-9]+)?)', re.I)




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


























async def _post_yolo_waivers(adapter, source, metadata, tail, receipt=None, receipt_path=None):
    """Name every check a yolo start overrode, in one message (nothing skipped silently)."""
    from . import model_job as mj
    waived = mj.yolo_waivers(tail)
    if not waived:
        return
    if receipt is not None:
        receipt['yolo_waivers'] = waived
        if receipt_path is not None:
            with contextlib.suppress(Exception):
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    shown = waived[:12] + ([f'... and {len(waived) - 12} more'] if len(waived) > 12 else [])
    with contextlib.suppress(Exception):
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='Yolo overrode these checks:\n' + '\n'.join(f'- {w}' for w in shown))


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
                "starts until it is settled. That happens by itself when the printer shows that file "
                "printing (or in its job history), or about a minute after the printer gives up on it; "
                "then press Retry. If it stays blocked, ask whoever runs Print Handy to settle it on the server.")
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


def _start_report(tail):
    """The failure_report.Report for one print-now outcome (#817): refused before sending, or sent unknown."""
    raw = _raw_preupload_refusal(tail)
    low = str(tail or '').lower()
    return failure_report.start_report(
        refused=_safe_preupload_refusal(tail), printer_rejected=_printer_rejected(raw),
        upload_failed=('could not be uploaded' in low or 'upload failed' in low))


def _launch_refusal_text(tail):
    """A confirmed pre-upload refusal as posted in chat (#817).

    "Didn't start:" is itself the launch stage, so the text stays exactly as operators and
    the start-path contracts know it; the classification rides on the receipt's failure record.
    """
    return _refusal_text(tail)


def start_unknown_message(tail):
    """The post for a start the printer has not confirmed (#817).

    An upload that failed or was not confirmed gets the classified wording (stage upload, the
    printer may already have the file); "probably still heating" is only true of a start that
    was sent, so every other unresolved start keeps that message with its stage.
    """
    report = _start_report(tail)
    if report.stage == 'upload':
        return failure_report.plain(report)
    return START_PENDING_MESSAGE


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
        return _launch_refusal_text(tail)
    if str(tail or '').lstrip().upper().startswith('UNCERTAIN:') or not _raw_preupload_refusal(tail):
        return failure_report.plain(_start_report(tail))
    return _launch_refusal_text(tail)


def _restart_helps(tail):
    """A stale heater target, stuck screen or leftover job: a force restart clears these."""
    text = str(tail or '').casefold()
    return 'refused' in text and any(w in text for w in (
        'idle guard', 'targettemp', 'paused', 'cancelled', 'canceled', 'shutdown', 'klippy',
        'stale', 'retained job', 'not idle', 'state is error'))


async def _offer_force_restart(adapter, source, key, message, timeout=None, printer_id=None):
    """Offer the existing safe REBOOT flow; never start the print afterwards.

    The reboot targets the intake's routed printer (printer_id): without it
    force_reboot would reboot the default printer, not the one whose start
    was refused (SAFE-4 / P1-79).
    """
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
    from print_handy import printer_control
    ok, result = await asyncio.to_thread(printer_control.for_printer(printer_id).force_reboot)
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
            limit = delivery.effective_upload_limit(adapter)
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
        picked = await _ask(adapter, source, key, question + ('\n' + note if note else ''), choices, timeout,
                            approval=True)
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
                limit = delivery.effective_upload_limit(adapter)
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
        limit = delivery.effective_upload_limit(adapter)
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


def _unresolved_direct(job, row):
    """A direct-link source whose filename only arrives with the download
    response (a Dropbox share link with no filename in its path inventories
    as the suffix-less "shared-file") has no suffix to filter on yet;
    direct_download validates the name the response or final URL supplies
    (P2-241)."""
    return job.get('site') == 'direct' and not Path(row.get('name', '')).suffix


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


def with_design_origin(job, record):
    """An upload job set marked as a multi-part AI design's handoff (#1204 item 4).

    The design hands its part files over like a multi-file drop, with the
    design record in the event metadata. The record only names the job and
    its provenance; every question, fit check and approval after this is the
    upload path's own. A record that does not name exactly these files is
    ignored, never half-applied.
    """
    if not isinstance(record, dict) or not record.get('session_id'):
        return job
    rows = record.get('parts')
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return job  # malformed metadata is ignored, never a crash of the upload's intake
    names = {str(row.get('file')) for row in rows}
    if len(job.get('sources') or []) < 2 or {r.get('name') for r in job['sources']} != names:
        return job
    job.update(design=dict(record), title=str(record.get('title') or job.get('title')))
    return job


def _combined_source(job, rows, paths, combined):
    """JOB.json source fields for a multi-file job: one row per file, and the combined project for older readers."""
    parts = [dict(row, downloaded_file=str(path)) for row, path in zip(rows, paths)]
    selected = {'kind': 'parts', 'name': Path(combined['path']).name, 'title': _parts_title(rows),
                'files': [_file_name(r) for r in rows]}
    from . import projects
    if job.get('design'):  # a multi-part AI design's parts (#1204 item 4): the same builder
        project = projects.from_design(job['intake_id'], parts, job['design'], title=job.get('title'))
        source_kind = project['source_kind']
    else:
        source_kind = {'upload_archive': 'uploaded_archive', 'upload_collection': 'uploaded_collection'}.get(
            job.get('source_kind')) or ('uploaded_files' if job.get('site') == 'upload' else 'linked_files')
        project = projects.from_files(job['intake_id'], source_kind, parts, title=job.get('title'))
    return {'selected_source': selected, 'selected_sources': parts,
            'downloaded_file': combined['path'], 'combined_objects': combined['objects'],
            'project': project, 'project_id': project['project_id'],
            'project_source_kind': source_kind}


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
        await asyncio.to_thread(intake_working_copy, path)
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


def designer_pose_text(split_plan, rows, parts_count=None):
    """The plain trade-off when a full-size split tips parts onto another face (#1380), or ''.

    Names how many parts full size stands on another face (and whether they
    need supports), then the size at which every part prints as its designer
    laid it out: the as-designed split when one is offered, else the plain
    shrink when that is offered. Says nothing when no part was reoriented.
    """
    reoriented = (split_plan or {}).get('reorientation_by_object') or {}
    if not reoriented:
        return ''
    # The question lists parts, and one reoriented object can hold several of them.
    count = parts_count or len(reoriented)
    braced = any(isinstance(row, dict) and row.get('supports') for row in reoriented.values())
    parts = 'part' if count == 1 else 'parts'
    lead = (f' Full size stands {count} {parts} on another face'
            f"{' with supports' if braced else ''}, because {'it does' if count == 1 else 'they do'} "
            f"not fit the bed the way the designer laid {'it' if count == 1 else 'them'} out.")
    designed = next((row for row in rows if row.get('designer_pose')), None)
    if designed is not None:
        return (lead + f" At {_fit_pct(designed['scale'])}, every part prints as designed, "
                f"on {len(designed['plates'])} plates.")
    shrink = next((row for row in rows if row.get('kind') == 'scale'), None)
    if shrink is not None:
        return lead + f" Shrinking to {_fit_pct(shrink['scale'])} keeps the designer's orientation."
    return lead


def _fit_pct(scale):
    return f'{round(float(scale) * 100, 1):g}%'


SPLIT_TURN_SUPPORTS_NOTE = ('Parts the split turned to fit ({names}) keep the supports their new pose needs, '
                            'whichever supports choice you make here.')


def split_turn_supports_note(job):
    """The Ready-question line naming parts a full-size split turned onto supports, or ''.

    Choosing a split that reorients a part with "needs supports" is the approval
    for those supports (#221 R1), so a later global "without supports" does not
    remove them. The question says so instead of contradicting the slice (#1373,
    found by the corpus on a 23-plate tray project).
    """
    plan = (job or {}).get('full_size_split_plan') or {}
    turned = [str(object_id) for object_id, row in (plan.get('reorientation_by_object') or {}).items()
              if isinstance(row, dict) and row.get('supports')]
    if not turned:
        return ''
    names = {}
    for part in ((job or {}).get('color_manifest') or {}).get('parts') or []:
        names.setdefault(str(part.get('object_id')), part.get('name') or str(part.get('object_id')))
    shown = [re.sub(r'\.stl$', '', str(names.get(t, f'object {t}')), flags=re.I) for t in turned]
    listed = ', '.join(shown[:4]) + (f' and {len(shown) - 4} more' if len(shown) > 4 else '')
    return SPLIT_TURN_SUPPORTS_NOTE.format(names=listed)


async def _refresh_posed_clearance(job, path):
    """Keep ``job['support_clearance']`` describing the pose the job prints (#1373 review).

    The report made at intake is the authored, full-size pose. A fit rotation, a
    lay-flat turn or a scale changes which faces are undersides and how tall each
    gap is, so a stale report flagged side walls, missed gaps a shrink created,
    and aimed extend/sink at the wrong side. Re-analyzed only when the pose
    changed. Returns the report (or None when the file cannot be analyzed).
    """
    from . import support_clearance
    rotation = (job.get('orientation') or {}).get('rotation')
    scale = job.get('scale')
    want = support_clearance.pose_key(rotation, scale)
    current = job.get('support_clearance')
    authored = want == support_clearance.pose_key(None, None)
    if current is not None and (current.get('pose') == want or (authored and 'pose' not in current)):
        return current
    try:
        report = await asyncio.to_thread(
            support_clearance.posed_report, path, rotation, scale,
            first_layer_mm=support_clearance.job_first_layer_mm(job))
    except Exception:  # noqa: BLE001 - an unreadable file is analyzed after slicing instead
        job.pop('support_clearance', None)
        return None
    job['support_clearance'] = report
    return report


def _variant_estimate_str(job_data, result):
    if not result or not job_data:
        return 'slicing...'
    try:
        from print_handy.runtime import discord_print_summary as dps
        plates = dps.plate_estimates(job_data)
        if plates:
            secs = sum(p['seconds'] for p in plates if p.get('seconds') is not None)
            grams = sum(p['filament_g'] for p in plates if p.get('filament_g') is not None)
            parts = []
            if secs:
                parts.append(f"~{dps.human(secs)} printing")
            if grams:
                parts.append(f"~{grams:g} g filament")
            if parts:
                return ', '.join(parts)
    except Exception:
        pass
    # slice_job returns one Path for a single plate and a list for several (#1373 review:
    # result[0] on a Path raised TypeError and stopped the Ready question).
    if isinstance(result, (str, Path)):
        result = [result]
    if result and Path(result[0]).is_file():
        try:
            from print_handy.core import model_job as mj
            text = Path(result[0]).read_text()
            sec = mj.gcode_estimate_s(text)
            if sec:
                return f"~{mj.human_dur(sec)} printing"
        except Exception:
            pass
    return 'slicing...'


async def prepare_link(event, adapter, state, root, timeout=None, reuse=None, printer_id=None,
                       before_start=None, confirm_target=False):
    """The chat intake for one model link, upload or reprint (see the module docstring).

    "yolo" in the message runs the same intake with every question answered
    by its go option and the start made with ``print-now --yolo``
    (core/yolo.py); only print approvers may ask for it.

    confirm_target is set when the intake exists only because the dispatcher
    prepended a remembered link to a follow-up ("print it") — the user never
    typed the link in this message, so the intake must name its target as a
    confirmation question before anything else (#979).
    """
    from . import yolo
    text = getattr(event, 'text', '') or ''
    if yolo.ACTIVE.get() or not yolo.requested(text):
        return await _prepare_link(event, adapter, state, root, timeout=timeout, reuse=reuse,
                                   printer_id=printer_id, before_start=before_start,
                                   confirm_target=confirm_target)
    source = event.source
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    event.text = yolo.strip(text)
    platform = getattr(getattr(source, 'platform', None), 'value', None) or 'discord'
    if not yolo.may_yolo(getattr(source, 'user_id', None), platform):
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=yolo.refusal(platform) + ' Running the normal intake instead.')
        return await _prepare_link(event, adapter, state, root, timeout=timeout, reuse=reuse,
                                   printer_id=printer_id, before_start=before_start,
                                   confirm_target=confirm_target)
    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                       content='Yolo: slicing with the recommended answers and printing without '
                               'checks. Every answer and every skipped check is posted here.')
    token = yolo.ACTIVE.set(True)
    try:
        return await _prepare_link(event, adapter, state, root, timeout=timeout, reuse=reuse,
                                   printer_id=printer_id, before_start=before_start,
                                   confirm_target=confirm_target)
    finally:
        yolo.ACTIVE.reset(token)


YOLO_GCODE_LABEL = 'Print it as-is (yolo)'


async def print_gcode_file(event, adapter, root, path, timeout=None, printer_id=None):
    """A dropped, already-sliced G-code file (.gcode or .gcode.3mf).

    Print Handy slices models itself, so a sliced file only prints as the
    operator's explicit yolo: said in the message, or chosen on the one
    question asked here. It then goes to ``print-now --yolo`` exactly as
    sent: no slicing, every check reported instead of enforced.
    """
    from . import model_job as mj, yolo
    source = event.source
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    key = (getattr(event, 'metadata', None) or {}).get('live_session_key') or source.chat_id
    name = re.sub(r'^doc_[0-9a-f]{12}_', '', Path(path).name)
    platform = getattr(getattr(source, 'platform', None), 'value', None) or 'discord'
    if printer_id:
        # A bare G-code has no job record to route it, so print-now sends it to the
        # default printer; refuse rather than print on a printer this channel does not serve.
        from .. import settings as _settings
        default_id = None
        with contextlib.suppress(Exception):
            default_id = _settings.default_printer_id()
        if default_id and str(printer_id).split(':')[-1] != str(default_id):
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=f'{name} is sliced G-code, which prints as-is only on the default '
                                       f'printer ({default_id}) for now. Nothing was started.')
            return {'action': 'skip', 'reason': 'yolo G-code for a non-default printer'}
    # Bare G-code sliced for a different printer (#978): G-code is never
    # retargeted or rewritten, so name that plainly and ask for the source
    # model instead of offering to print another printer's file as-is.
    from . import ai_fix as _ai_fix
    _known_models = []
    with contextlib.suppress(Exception):
        from .. import printers as _printers
        _current = _printers.current()
        _model = getattr(_current, 'model', None)  # the PrinterModel record, not a name
        _known_models = [getattr(_current, 'key', None), getattr(_model, 'key', None),
                         getattr(_model, 'label', None)]
    with contextlib.suppress(Exception):
        from .. import settings as _settings
        _known_models.append((_settings.current().get('printer') or {}).get('name'))
    _foreign = _ai_fix.is_foreign_gcode(path, _known_models) if _known_models else None
    if _foreign:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=_ai_fix.foreign_gcode_message(name, _foreign))
        return {'action': 'skip', 'reason': 'foreign G-code for another printer'}
    explicit_yolo = yolo.requested(getattr(event, 'text', ''))
    if explicit_yolo and not yolo.may_yolo(getattr(source, 'user_id', None), platform):
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=f'{name} is already-sliced G-code, which prints only as yolo. '
                                   + yolo.refusal(platform) + ' Or send the .stl or .3mf model to '
                                   'have it sliced for this printer. Nothing was started.')
        return {'action': 'skip', 'reason': 'sliced G-code from a non-approver'}
    if not explicit_yolo:
        pick = await _ask(adapter, source, key,
                          f'{name} is already-sliced G-code, so it would print exactly as sent: no '
                          'slicing for this printer and no checks (yolo). Is the bed clear?',
                          [YOLO_GCODE_LABEL, print_next.CANCEL_LABEL], timeout, approval=True)
        if pick != 0:
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content='Cancelled. Nothing uploaded or started. Send the .stl or .3mf '
                                       'model to have it sliced for this printer.')
            return {'action': 'skip', 'reason': 'operator declined printing sliced G-code as-is'}
    sha = mj.file_sha256(path)
    folder = Path(mj.ARCHIVE) / f'yolo-gcode--{sha[:16]}'
    folder.mkdir(parents=True, exist_ok=True)
    note = ''
    if name.lower().endswith('.gcode.3mf'):
        try:
            gcode, plates = await asyncio.to_thread(yolo.extract_gcode, path, folder)
        except Exception as exc:  # noqa: BLE001 - a bad package is a plain refusal
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=f'Could not read the G-code inside {name}: {exc}. Nothing uploaded or started.')
            return {'action': 'skip', 'reason': 'unreadable .gcode.3mf package'}
        if plates > 1:
            note = f' The package has {plates} plates; printing plate 1.'
    else:
        gcode = folder / name
        if Path(path).resolve() != gcode.resolve():
            shutil.copyfile(path, gcode)
    gsha = mj.file_sha256(gcode)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    iid = mj.intake_id_from_message(source, getattr(event, 'message_id', None) or gsha)
    receipt_path = root / (iid + '.json')
    target = {'plan_id': f'yolo-{iid[:16]}', 'part_id': 'file', 'copy': 1, 'quantity': 1,
              'gcode': str(gcode), 'gcode_sha256': gsha, 'reprint': True, 'yolo': True}
    receipt = {'intake_id': iid, 'url': f'upload:{name}', 'status': 'START_REQUESTED', 'sliced': False,
               'uploaded': False, 'started': False, 'chat_id': source.chat_id,
               'thread_id': source.thread_id, 'owner': getattr(source, 'user_id', None),
               'gcodes': [str(gcode)], 'gcode_sha256': {str(gcode): gsha}, 'yolo': True,
               'bed_clear_approval': {'gcode': str(gcode), 'gcode_sha256': gsha, 'via': 'yolo',
                                      'granted_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')},
               'retry_target': target}
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    logger.info('yolo G-code print requested file=%s sha=%s intake=%s', name, gsha[:12], iid)
    ok, tail = await _with_stop(adapter, source, key, metadata,
                                f'Yolo: printing {name} as sent, no checks.{note} '
                                'Uploading to the printer. Press Stop to request cancellation.',
                                print_next.start(target), allow_stop=True)
    await _post_yolo_waivers(adapter, source, metadata, tail, receipt, receipt_path)
    outcome = classify_start(ok, tail)
    receipt.update(status=outcome.status, uploaded=outcome.started is not False or outcome.pending,
                   started=outcome.started)
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    if outcome.status == 'STARTED':
        content = f'Print started: {name} (yolo, sent as-is).'
    elif outcome.pending:
        content = start_unknown_message(tail)
    else:
        content = plain_start_failure(tail)
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=content)
    return {'action': 'skip', 'reason': f'yolo G-code {outcome.status.lower()}'}








async def _prepare_link(event, adapter, state, root, timeout=None, reuse=None, printer_id=None,
                        before_start=None, confirm_target=False):
    from . import model_job as mj
    from .source_colors import inventory, canonical_prompts, grouped_prompts, validate_decisions
    source = event.source
    url = None if reuse else model_link(event.text)
    uploads = [] if (url or reuse) else model_files(event)
    # A ZIP is opened only when the message has no STL/3MF of its own (#1204 item 3).
    archives = [] if (url or reuse or uploads) else archive_files(event)
    mid = getattr(event, 'message_id', None)
    if not (url or uploads or archives or reuse) or not mid:
        raise ValueError('plain model link requires a stable Discord message ID')
    iid = mj.intake_id_from_message(source, mid)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    receipt_path = root / (iid + '.json')
    origin = (url or (f'reprint:{reuse.get("intake_id")}' if reuse else None)
              or (f'upload-archive:{Path(archives[0]).name}' if archives else None)
              or (f'upload:{upload_job(uploads[0])["sources"][0]["name"]}' if len(uploads) == 1
                  else f'upload-project:{len(uploads)}-files'))
    receipt = {'intake_id': iid, 'url': origin, 'status': 'INSPECTING_SOURCE',
               'sliced': False, 'uploaded': False, 'started': False,
               'chat_id': str(source.chat_id), 'thread_id': str(source.thread_id) if source.thread_id else None,
               'owner': owned_worker._OWNER,
               'platform': getattr(getattr(source, 'platform', None), 'value', None) or 'discord'}
    claim_receipt_owner(root)  # announce_interrupted elsewhere reads this process as alive
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
        elif uploads:
            job = with_design_origin(upload_job_set(uploads), event_metadata.get('design_handoff'))
            collected = event_metadata.get('upload_collection')
            if isinstance(collected, dict) and len(uploads) > 1:
                # Collected over several messages (#1204 item 2): the same
                # inventory, marked so the project records where it came from.
                job['source_kind'] = 'upload_collection'
                job['upload_collection'] = collected
            skipped = archive_files(event)
            if skipped:
                await adapter.send(chat_id=source.chat_id, metadata=metadata, content=(
                    f"I left out {', '.join(re.sub(r'^doc_[0-9a-f]{12}_', '', p.name) for p in skipped)}: "
                    'a ZIP is opened only when it is sent in its own message.'))
        elif archives:
            job = await asyncio.to_thread(archive_job, archives, root / (iid + '.archive'))
            from .source_archive import summary as _archive_summary
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=_archive_summary(job['source_archive']))
        else:
            job = await _with_connect(adapter, source, key, metadata, timeout,
                                      lambda: asyncio.to_thread(mj.inventory_for_url, url))
        rows = job.get('sources', [])
        if job['site'] in FILE_LIST_SITES:
            rows = [r for r in rows if Path(r.get('name', '')).suffix.lower() in {'.3mf', '.stl'}
                    or _unresolved_direct(job, r)]
        if not rows and job.get('site') in NO_DOWNLOAD_SITES:
            raise _unsupported_for(job['site'])  # the plain refusal, same as without a key (#712)
        if not rows:
            raise ValueError(job.get('no_geometry_reason') or 'source inventory contains no selectable geometry')
        if confirm_target and not reuse:
            # A follow-up-born intake (#979): the dispatcher prepended a link
            # the user shared earlier and never typed in this message, so —
            # like the reuse route's "Did you mean …?" — the first question
            # names the target and lets the user stop the wrong model before
            # any profile, color or fit question. The preview alone is not a
            # gate: it sends nothing at all for a bare-title job.
            question = (f"Print {job.get('title') or 'the linked model'}? "
                        'That is the model from the link shared earlier in this channel.')
            from . import yolo as _yolo
            if _yolo.ACTIVE.get():
                # Yolo vouches for a file the operator named. Here nobody
                # named it -- the dispatcher supplied a remembered link -- so
                # the target question goes to a person even under yolo
                # (#979 review); every later question stays yolo's.
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content='Yolo does not answer this one; a person has to '
                                           'confirm which model to print.')
                # (_prepare_link has a local named `ask`; use the prompt by another name.)
                from .link_intake import ask as _person_ask
                pick = await _person_ask(adapter, source, key, question,
                                         ['Yes, print it', 'No, cancel'], timeout,
                                         live_key=_LIVE_KEY.get() or key)
            else:
                pick = await _ask(adapter, source, key, question, ['Yes, print it', 'No, cancel'],
                                  timeout)
            if pick != 0:
                receipt.update(status='DECLINED_FOLLOWUP')
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content='OK. Nothing was sliced or started. '
                                           'Send the link or drop the file for a different model.')
                return {'action': 'skip', 'reason': 'followup target declined'}
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
        await send_preview(adapter, source, job, chosen, metadata, state=state, voice_key=iid)
        folder = mj.intake_folder(mj.ARCHIVE, job['slug'], iid)
        folder.mkdir(parents=True, exist_ok=False)
        job.update(url=url, intake_id=iid, hardware=state, selected_source=chosen)
        stamp_started_by(job, source)
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
        if await asyncio.to_thread(is_gcode_3mf, path):
            # An already-sliced G-code package, not a corrupt 3MF (#922 gap 9):
            # refuse before the audit can misreport it as bad_3mf_structure.
            raise ValueError(gcode_3mf_message(Path(path).name))
        await asyncio.to_thread(check_source, path)
        # One canonical form for everything downstream (#814, #825): the audit, colors, fit, layout
        # and slicer all read this copy. The user's bytes are kept under source/original/.
        await asyncio.to_thread(intake_working_copy, path)
        if job['site'] == 'upload' and not job.get('design'):  # a design keeps its own name
            job['title'] = await asyncio.to_thread(upload_title, path) or job['title']
        job['audit'] = await _with_stop(adapter, source, key, metadata,
                                        _voiced(large_model_notice(path), 'model_audit', source, iid),
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
                adapter, source, key, metadata,
                _voiced("Checking the model. Press Stop to cancel.", "color_detection", source, iid),
                _heavy(inventory, path))
        # STL has no source palette, but still needs an exact-source color decision.
        if not manifest.get('source_sha256'):
            manifest.update(source_sha256=await asyncio.to_thread(mj.file_sha256, path))
        job['color_manifest'] = manifest
        job['color_schema'] = 'part_slots_v1'
        from . import support_clearance
        # The job's own printer's nozzle and the project's first layer (#1373 review:
        # the default printer's profile was read on multi-printer installs).
        clearance_rep = await asyncio.to_thread(
            support_clearance.safe_analyze, path,
            first_layer_mm=support_clearance.profile_first_layer_mm(printer_id, source=path))
        if clearance_rep:
            job['support_clearance'] = clearance_rep
        mj.stamp_job_printer(job, printer_id)  # a new job learns its printer at creation (#334); the intake channel's printer wins (#335)
        jp = folder / 'JOB.json'
        mj.write_job(folder, job)
        # The bed fit probes read the job's own printer's bed exclusions, not the default's (#1195).
        fit_state = _fit_state(state, job)
        receipt.update(job_json=str(jp), source_sha256=manifest['source_sha256'], status='AWAITING_COLOR')
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        # The author-layout fit probe needs only the file and the printer state, never the unit or
        # color answers, so start it now and let it run while the operator answers (#734). It is
        # awaited at the fit check below, which then usually has nothing left to wait for.
        # A resumed job (e.g. chat edit or undo re-entering, #903) uses its resumed
        # transforms so copy capacity reflects the edited model rather than clamping
        # to the authored size.
        from . import orca_adapter
        fit_kw = {}
        if job.get('resumed_from'):
            if job.get('scale') is not None:
                try:
                    fit_kw['scale'] = float(job['scale'])
                except (TypeError, ValueError):
                    pass
            orientation = job.get('orientation') or {}
            if orientation.get('rotation') is not None:
                fit_kw['rotation'] = orientation['rotation']
            if orientation.get('supports') is not None:
                fit_kw['supports'] = bool(orientation['supports'])
            if job.get('fit_brim_policy'):
                fit_kw['brim_policy'] = job['fit_brim_policy']
            if job.get('copies'):
                try:
                    fit_kw['copy_limit'] = max(16, int(job['copies']))
                except (TypeError, ValueError):
                    pass
        fit_probe = _prefetch(_heavy(orca_adapter.orca_plate_capacity, path, fit_state, **fit_kw))
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
                    # Too big as inches is a choice, not a cancel (#839): name
                    # the volume and offer the millimeter size or the largest
                    # scale of the inch size that fits. The job is not
                    # cancelled until the operator picks Cancel.
                    try:
                        fit_fraction = await _heavy(orca_adapter.largest_fitting_fraction,
                                                    path, fit_state, mj.INCH_TO_MM, copy_limit=1)
                    except Exception:
                        logger.exception('inch fit search failed')
                        fit_fraction = None
                    volume = (f"{float(bed[0]):g} x {float(bed[1]):g} x {float(height):g} mm printer volume"
                              if height is not None else
                              f"{float(bed[0]):g} x {float(bed[1]):g} mm printer bed")
                    rows = [{'kind': 'mm', 'label': 'Use mm instead'}]
                    if fit_fraction is not None:
                        rows.append({'kind': 'scale', 'label': f'Scale to {_pct(fit_fraction)}',
                                     'scale': fit_fraction})
                    rows.append({'kind': 'cancel', 'label': 'Cancel'})
                    pick = await _ask(
                        adapter, source, key,
                        f'Treating it as inches would make it {as_inch} mm, which exceeds the {volume}. '
                        'Use the millimeter size instead'
                        + (f', or scale the inch size to {_pct(fit_fraction)}?' if fit_fraction is not None
                           else '?'),
                        [row['label'] for row in rows], timeout,
                        parser=lambda text: _unit_oversize_reply(text, rows))
                    row = rows[pick]
                    if row['kind'] == 'cancel':
                        receipt.update(status='CANCELLED_OVERSIZED')
                        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                        await adapter.send(chat_id=source.chat_id,
                                           content='Print cancelled. Nothing was sliced, uploaded, or started.',
                                           metadata=metadata)
                        return {'action': 'skip', 'reason': 'inch-confirmed model exceeds printer volume'}
                    if row['kind'] == 'mm':
                        decision = mj.make_unit_decision('mm', manifest['source_sha256'],
                                                         unit_q['original_mm'])
                        mj.set_unit_decision(job, decision)
                        receipt.update(unit_decision=decision)
                    else:
                        # The scale is of the authored file, so it carries the
                        # inch factor: slicing's explicit scale then lands the
                        # part at the fitted fraction of the inch size.
                        job['scale'] = round(mj.INCH_TO_MM * row['scale'], 6)
                        receipt.update(scale=job['scale'])
                    mj.write_job(folder, job)
                    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        job_adapter = mj._job_adapter(job)
        # ZMod purge awareness (#832): re-read the printer's purge mode and plugins (read-only, short
        # timeouts, never blocks intake), then refuse a source an IFS Jacker printer cannot model yet.
        if getattr(job_adapter, 'has_native_screen', False):
            await asyncio.to_thread(purge.refresh_best_effort, state, job_adapter, job.get('printer_id'))
            purge.require_jacker_capacity(manifest, job_adapter, state)
        selections = [mj.resolve_filament_selection(state, dict(label, slot=int(slot)))
                      for slot, label in sorted(mj.configured_slots(state).items())]
        # A tool_based printer's positions are tools, never slots (#346, #1198).
        labels = (mj.build_tool_labels(selections) if job_adapter.tool_based
                  else mj.build_slot_labels(selections))
        if not labels or len(labels) > mj.position_limit(job_adapter) or len(set(labels)) != len(labels):
            raise ValueError('requires one to four distinct configured toolheads' if job_adapter.tool_based
                             else 'requires one to four distinct configured slots')
        # A job with a plate selection is asked only about the selected
        # plates' colors; unselected plates never prompt (#1110 Phase B).
        selected_plates = job.get('selected_plates') or None
        questions = canonical_prompts(manifest, selected_plate_ids=selected_plates)
        grouped = grouped_prompts(manifest, selected_plate_ids=selected_plates)
        families = [row for row in grouped if len(row['keys']) > 1]
        tally = {}
        for p in manifest['parts']:
            label = re.sub(r'\.(stl|3mf|obj|step|stp)$', '', p['name'], flags=re.I)
            tally[label] = tally.get(label, 0) + 1
        names = ', '.join(f'{n}× {k}' if n > 1 else k for k, n in tally.items()) or Path(path).name
        saved_decisions = job.get('color_decisions') if job.get('resumed_from') else None
        # Height-based color changes (#870): colors that arrive by layer, not
        # by part or paint. On a single-extruder printer they can only print
        # as pauses for a manual swap, or not at all — the operator chooses,
        # and the choice is recorded on the decisions, never left silent.
        layered = bool(manifest.get('layer_changes'))
        layer_single_choice = None
        if saved_decisions:
            decisions = saved_decisions
        elif layered and not manifest['painted'] and getattr(job_adapter, 'color_system', None) == 'single':
            from .source_colors import layer_change_summary
            count = len(manifest['parts'])
            prompt = (f"Inspected {count} part{'s' if count != 1 else ''}: {names}. This print "
                      f"{layer_change_summary(manifest)}. {_printer_name(job)} has one extruder, so those "
                      'colors cannot print on their own: pause at each change height so you can swap the '
                      'filament by hand, or print in one color for everything and the height colors are lost.')
            pick = await _ask(adapter, source, key, prompt,
                              ['Pause at each color change (swap filament by hand)',
                               'One color for everything'], timeout)
            layer_single_choice = 'pause' if pick == 0 else 'one_color'
            mode = 1
        elif any(row['key'] == 'painted:unknown' for row in questions):
            # Painted source with no readable palette (#717): the regions
            # cannot be told apart, so never ask an unanswerable region
            # question. Offer the one answer that can print.
            count = len(manifest['parts'])
            prompt = (f"Inspected {count} part{'s' if count != 1 else ''}: {names}. This file has painted "
                      'color regions but no readable color palette, so the regions cannot be kept apart. '
                      'Print it in one color for everything?')
            mode = 1 + await _ask(adapter, source, key, prompt, ['One color for everything'], timeout)
        elif questions and (len(manifest['parts']) > 1 or manifest['painted'] or layered):
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
            if layered and not manifest['painted'] and len(manifest['parts']) <= 1:
                # Nothing but the height changes makes this multicolor: ask
                # about them by name, not about "these parts".
                from .source_colors import layer_change_summary
                options[0] = 'Keep the height-based colors'
                prompt = (f'{found} This print {layer_change_summary(manifest)}. '
                          'Keep the height-based colors or use one color for everything?')
            mode = await _ask(adapter, source, key, prompt, options, timeout)
        else:
            mode = 1
        if not saved_decisions:
            # A resumed intake reuses the decisions already saved on the job:
            # they carry their mode, choices and selection, so nothing is
            # rebuilt and no color question is asked again (P1-38).
            decisions = {'source_sha256': manifest['source_sha256'], 'schema': 'part_slots_v1',
                         'mode': 'single_color' if mode == 1 else 'per_part'}
            if mode != 1:
                if job_adapter.tool_based:
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
                taken = {}  # tool -> source colour already assigned to it (tool_based only)
                for row in rows:
                    if job_adapter.tool_based:
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
                # Core face colors print as one color, and the operator is told (#714).
                from .source_colors import FACE_COLORS_NOTE
                note = f' {FACE_COLORS_NOTE}' if manifest.get('face_colors_ignored') else ''
                ask = f'Choose the color for {names}.{note}' if note else f'Choose the color for {names}'
                if layer_single_choice == 'pause':
                    ask = (f'Choose the starting color for {names} '
                           '(the print pauses at each height color change so you can swap it)')
                decisions['selection'] = selections[await _ask(adapter, source, key, ask, labels, timeout)]
            if layered and mode == 1:
                # Record what happens to the height colors on a one-color
                # print: pauses the operator chose, or dropped — named here
                # and in the slice record, never silent (#870).
                decisions['height_colors'] = layer_single_choice or 'one_color'
        latest = json.loads(jp.read_text())
        if saved_decisions:
            if (latest.get('intake_id') != iid
                    or mj.file_sha256(latest['downloaded_file']) != decisions['source_sha256']):
                raise ValueError('source/job changed during resume; decisions not saved')
        elif (latest.get('intake_id') != iid or latest.get('color_decisions')
                or mj.file_sha256(latest['downloaded_file']) != decisions['source_sha256']):
            raise ValueError('source/job changed during selection; decisions not saved')
        validated = validate_decisions(manifest, decisions, state, mj.resolve_filament_selection,
                                       selected_plate_ids=selected_plates)
        mj.set_color_decisions(latest, validated)
        if decisions['mode'] == 'single_color':
            latest['filament_selection'] = validated['selection']
        fit_status = FitStatus(adapter, source, metadata)
        try:
            capacity = await fit_status.run('checking the layout as the author laid it out', fit_probe,
                                            first_text=_voiced('Checking fit and calculating plate capacity.',
                                                               'plate_capacity', source, iid))
        except Exception as exc:
            logger.exception('fit check failed; intake is resumable')
            receipt['status'] = 'FIT_UNKNOWN'
            receipt['fit_error'] = str(exc)
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
            await adapter.send(chat_id=source.chat_id,
                               content=failure_report.post(
                                   exc, 'plate plan', subsystem='intake',
                                   headline="I could not finish checking whether this model fits the bed, so the check is unverified.",
                                   not_happened="No slice or print was started.",
                                   transient="Send the link or file again to resume the check."),
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
            fit_error = None
            try:
                fit = await _heavy(mj.painted_fit_for_job, path, latest['color_decisions'], state)
            except Exception as exc:
                fit = {'scale': None, 'error': str(exc)}
                fit_error = exc
            # A crashed fit check is not a size verdict (#839): the post says
            # the check itself failed and offers Retry/Cancel, never
            # "does not fit". Only a fit with no error is a real no-fit.
            while fit.get('error'):
                receipt['painted_fit'] = fit
                retry_rows = [{'kind': 'retry', 'label': 'Retry fit check'},
                              {'kind': 'cancel', 'label': 'Cancel'}]
                pick = await _ask(adapter, source, key,
                                  diagnostics.plain_post(
                                      'Stage: fit check. I could not check whether this fits the bed, '
                                      'so whether it fits is unverified.',
                                      not_happened='Nothing was sliced, uploaded or started.',
                                      next_step='Retry the fit check, or cancel the print.',
                                      subsystem='intake', exc=fit_error),
                                  [row['label'] for row in retry_rows], timeout,
                                  parser=lambda text: _retry_choice_reply(text, retry_rows))
                if retry_rows[pick]['kind'] == 'cancel':
                    receipt['status'] = 'CANCELLED'
                    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                    await adapter.send(chat_id=source.chat_id,
                                       content='Print cancelled. Nothing was sliced, uploaded, or started.',
                                       metadata=metadata)
                    return {'action': 'skip', 'reason': 'painted fit check failed; operator cancelled'}
                fit_error = None
                try:
                    fit = await _heavy(mj.painted_fit_for_job, path, latest['color_decisions'], state)
                except Exception as exc:
                    fit = {'scale': None, 'error': str(exc)}
                    fit_error = exc
            receipt['painted_fit'] = fit
            scale = fit.get('scale')
            if scale is None:
                picked = [{'kind': 'cancel', 'label': 'Cancel'}]
                prompt = (f"{title} does not fit the {_printer_name()} bed with its prime tower at 50% or larger, "
                          'even split into its plates.' + _painted_no_fit_detail(fit, state))
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
            # No fit-options dict exists on the painted path (``options`` here
            # is still the color-mode label list); painted_fit carries no brim
            # policy, so there is nothing to record (P1-27).
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
                job_adapter = mj._job_adapter(latest)
                one_plate = await fit_status.run('trying the parts rearranged on one plate at 100%',
                                                 _heavy(mj.full_size_one_plate, path, state,
                                                        latest.get('color_decisions'),
                                                        capacity=mj.position_limit(job_adapter),
                                                        multi_tool=job_adapter.multi_tool,
                                                        job=latest),
                                                 voice=_voice('plate_arrangement', source, iid))
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
                                                      {**fit_state, 'fit_rearrange_checked': True}
                                                      if rearrange_checked else fit_state),
                                               voice=_voice('bed_fit', source, iid))
            except Exception as exc:
                receipt['status'] = 'FIT_UNKNOWN'
                receipt['fit_error'] = str(exc)
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                await adapter.send(chat_id=source.chat_id,
                                   content=failure_report.post(
                                   exc, 'plate plan', subsystem='intake',
                                   headline="I could not finish checking the resize options, so they are unverified.",
                                   not_happened="No slice or print was started.",
                                   transient="Send the link or file again to resume the check."),
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
                                                         brim_policy=options.get('brim_policy'), state=fit_state),
                                                  voice=_voice('split', source, iid))
            except Exception:
                split_plan = None
            try:
                if rotate_100 or split_plan or single_body:
                    # A concrete split plan supersedes the generic "parts can be
                    # separated" offer, so that check is only needed without one.
                    raise LookupError('a split plan or a 100% one-plate fit already answers this')
                full_size_split = await fit_status.run('trying a split onto separate plates at 100%',
                                                       _heavy(plate_merge.full_size_split_possible, path, manifest,
                                                              int((state.get('bed_mm') or BED)[0]),
                                                              brim_policy=options.get('brim_policy'),
                                                              state=state),
                                                       voice=_voice('split', source, iid))
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
                turn_reasons = split_plan.get('turn_reason_by_object') or {}
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
                            why = turn_reasons.get(object_by_key.get(part_key, ''))
                            if why:
                                label += f'; {why}'
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
                tipped = sum(1 for keys in split_plan['plates'] for part_key in keys
                             if object_by_key.get(part_key, '') in reorientation_by_object)
                prompt = (lead + '; '.join(distribution) + '.'
                          + designer_pose_text(split_plan, picked, parts_count=tipped) +
                          ' Choose a fit option, or type ' + fit_hint(picked)[len(FIT_HINT_PREFIX):])
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
                latest['scale'] = float(row.get('scale') or 1.0)
                if row.get('designer_pose'):
                    # The as-designed split (#1380): every plate at one smaller
                    # scale, no part reoriented, so it carries only turns.
                    latest['full_size_split_plan'] = {'plates': row['plates'], 'scale': latest['scale'],
                                                      **({'yaw_by_object': row['yaw_by_object']}
                                                         if row.get('yaw_by_object') else {})}
                elif row.get('plates'):
                    latest['full_size_split_plan'] = {'plates': row['plates'],
                                                      **({'yaw_by_object': row['yaw_by_object']}
                                                         if row.get('yaw_by_object') else {}),
                                                      **({'placement_by_object': row['placement_by_object']}
                                                         if row.get('placement_by_object') else {}),
                                                      **({'shifts_by_plate': row['shifts_by_plate']}
                                                         if row.get('shifts_by_plate') else {}),
                                                      **({'reorientation_by_object': row['reorientation_by_object']}
                                                         if row.get('reorientation_by_object') else {}),
                                                      **({'turn_reason_by_object': row['turn_reason_by_object']}
                                                         if row.get('turn_reason_by_object') else {})}
                latest.pop('orientation', None)
                latest.pop('full_size_rearrange_plan', None)
                receipt['full_size_split'] = True
                if row.get('designer_pose'):
                    receipt['split_as_designed'] = True
                receipt['scale'] = latest['scale']
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
                                            _heavy(orca_adapter.orca_plate_capacity, path, fit_state,
                                                   scale=selected_scale,
                                                   **({'rotation': rotation, 'supports': bool(row.get('supports')),
                                                       # The search verified this turn with the
                                                       # exact-single probe (copy_limit=1: one
                                                       # centered copy, no arranging); confirming
                                                       # with the default multi-copy probe arranges
                                                       # instead and refuses turns that only fit
                                                       # as one centered copy (P1-115).
                                                       'copy_limit': 1,
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
        # A resumed job that already carries a count — a chat edit or undo
        # re-entering the flow (#903) — keeps it (bounded by what fits)
        # instead of being asked again and silently reset to 1.
        preset_copies = bool(latest.get('resumed_from') and latest.get('copies'))
        if preset_copies:
            chosen_quantity = max(1, min(int(latest['copies']), capacity))
        if len(quantities) > 1 and not preset_copies:
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
        # Quantity is validated against real capacity above. A once-per-user
        # Easter egg for 2, 10 or 11 must never influence selection or slicing.
        if not preset_copies and len(quantities) > 1 and chosen_quantity > 1:
            joke = status_voice.quip(
                'quantity_selected', copies=chosen_quantity,
                identity=_voice_identity(source),
                event_key=f'{iid}:copies:{chosen_quantity}')
            if joke:
                try:
                    await adapter.send(
                        chat_id=source.chat_id,
                        content=f'{chosen_quantity} copies selected. {joke}',
                        metadata=metadata)
                except Exception:
                    logger.warning('could not send optional quantity joke', exc_info=True)
        if before_start is not None:
            # An edit carried beside the link or file ("<link> shrink this by
            # 20%", #903) applies to the prepared job BEFORE any slice is armed
            # or a start is offered. When the edit takes over (it re-enters the
            # flow with the edited job), this intake ends here.
            handed = None
            try:
                handed = await before_start(jp, latest)
            except Exception:
                logger.exception('instruction before start failed; continuing the intake unchanged')
            if isinstance(handed, dict):
                receipt.update(status='EDITED', sliced=False, uploaded=False, started=False)
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                return handed
        # The clearance report follows the pose the fit flow chose (a turn or a
        # shrink) before anything is sliced or asked from it (#1373 review).
        await _refresh_posed_clearance(latest, path)
        mj.write_job(folder, latest)
        # Slice while the confirm question is on screen. Cancel below discards it,
        # so a job that never asked for G-code keeps none.
        early = runner_for(jp)
        binding = slice_binding(latest, decisions['source_sha256'])
        recipe_sha = binding_sha256(binding)  # the worker refuses to slice any other recipe
        await early.start(jp, binding, lambda job_path, work, runner, copies=chosen_quantity, **kwargs:
                          _heavy(mj.slice_job, str(job_path), recipe_sha256=kwargs.get('recipe_sha256', recipe_sha), **(
                              {'copies': copies} if copies and int(copies) > 1 else {})))
        await early.started.wait()  # armed before the question goes up
        multi = len(manifest['parts']) > 1
        def _color_of(sel):
            sel = sel or {}
            color = str(sel.get('color') or '').strip()
            noun = 'Toolhead' if mj._job_adapter(latest).tool_based else 'Slot'
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
        color_text = _color_guess_text(mj, latest, decisions)
        source_estimate = _preview_estimate(latest.get('selected_source'))
        # Post-slice choices come from the printer's vendor profile ([delivery] methods). Printing is
        # offered only where the vendor offers it AND the operator enabled it in [delivery] methods
        # (for Bambu Lab that means the LAN print opt-in; it is never a default there).
        enabled = delivery.methods()
        can_print = delivery.PRINTER in enabled
        # 'Scale the model' repeats this question at the new size: the scale is
        # recorded, the background slice restarts for it while the question is
        # on screen, and the operator can say the bed is clear without waiting
        # for the scaling. The only refusal is a size that does not fit the
        # bed — and a refused size offers the biggest size that does fit.
        choice = None
        scale_note = ''
        supports_choice = None
        clearance_ack_pending = False
        shown_shortages = []
        # "Reorient (lay flat)" is offered until it has nothing left to do:
        # the model already lies flat, or the lay-flat turn was applied (the
        # turn is worked out from the authored pose, so pressing it again
        # would record the same turn and change nothing).
        offer_reorient = True
        # The Ready question is composed from the finished first slice: the
        # with-supports offer (and the reason shown for it) depends on that
        # slice's warnings, so a free-running slice is waited out in full.
        # A capped wait made the offer timing-dependent — a real Orca slice
        # slower than the cap composed the question without the slicer's
        # floating-regions finding. A slice a test holds back (release
        # cleared) is not waited on: it starts when the flow asks to wait
        # for it after the question, as before.
        while choice is None:
            # Every time the Ready question is (re)composed, the slices it describes
            # are waited for: a Reorient or Scale answer restarts them and asks again
            # (#1373, found by the corpus explorer on witch-ghost).
            async def _wait_ready_slices():
                if (
                    early.running()
                    and hasattr(early, 'first_slice_done')
                    and not early.first_slice_done.is_set()
                    and getattr(early, 'release', None) is not None
                    and early.release.is_set()
                ):
                    await early.first_slice_done.wait()
                # The with-supports option must show its own time and filament
                # before it can be chosen (#1373 item 2): a flagged first slice
                # starts the speculative supports slice, so it is waited out too,
                # bounded so a very slow slice never holds the question forever.
                decided = getattr(early, 'flag_decided', None)
                if (decided is not None and early.running() and early.first_slice_done.is_set()
                        and getattr(early, 'release', None) is not None and early.release.is_set()):
                    # first_slice_done fires before the flagged verdict and the
                    # speculative supports slice exist; wait for those (bounded).
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(decided.wait(), SUPPORTS_ESTIMATE_WAIT_S)
                sup_task = getattr(early, 'supports_task', None)
                if (getattr(early, 'flagged', False) and sup_task is not None and not sup_task.done()
                        and getattr(early, 'release', None) is not None and early.release.is_set()):
                    # A timeout or a failed supports slice leaves the "slicing..." line;
                    # the intake's own cancellation still propagates.
                    with contextlib.suppress(asyncio.TimeoutError, Exception):
                        await asyncio.wait_for(asyncio.shield(sup_task), SUPPORTS_ESTIMATE_WAIT_S)
            waiting = early.running() and not (
                getattr(early, 'flag_decided', None) is not None and early.flag_decided.is_set()
                and not (getattr(early, 'supports_task', None) is not None and not early.supports_task.done()))
            if waiting and getattr(early, 'release', None) is not None and early.release.is_set():
                try:
                    comparing = getattr(early, 'flagged', False)
                    await _with_stop(adapter, source, key, metadata,
                                     ('Slicing to compare with and without supports. Press Stop to cancel.'
                                      if comparing else 'Slicing. Press Stop to cancel.'),
                                     _wait_ready_slices(), allow_stop=True)
                except Stopped:
                    await early.discard()
                    raise
            else:
                await _wait_ready_slices()
            from . import support_clearance, modeled_supports
            clearance = latest.get('support_clearance')
            if clearance is None and early.clearance:
                clearance = early.clearance
            is_flagged, sup_reason, sup_inputs = supports_decision(
                early.flagged, early.finding, clearance)
            # #1020 F1/F3: the decision is logged with its inputs either
            # way, and the audit record (reason, inputs, clearance report,
            # slicer finding) rides on the job so it lands in JOB.json.
            question_audit = {'shown': is_flagged, 'reason': sup_reason,
                              'inputs': sup_inputs, 'clearance': clearance,
                              'finding': early.finding}
            latest['supports_question'] = question_audit
            logger.info("supports question for %s: shown=%s reason=%r inputs=%s",
                        names, is_flagged, sup_reason, sup_inputs)

            if is_flagged:
                sup_est = _variant_estimate_str(early.job_with_supports, early.supports_result)
                sup_task_now = getattr(early, 'supports_task', None)
                if sup_est == 'slicing...' and (getattr(early, 'supports_error', None) is not None
                                                 or (sup_task_now is not None and sup_task_now.done())):
                    # The trial slice is over: never "slicing..." for a slice that
                    # ended without numbers (#1373). Choosing it slices again.
                    sup_est = 'no estimate (the trial slice with supports did not finish; choosing it slices again)'
                elif sup_est == 'slicing...' and sup_task_now is not None and not sup_task_now.done():
                    # Past the two-minute wait (a 23-plate project): say so, and what
                    # choosing it does, instead of a bare "slicing..." (#1373 corpus).
                    sup_est = (f'still slicing after {SUPPORTS_ESTIMATE_WAIT_S / 60:g} minutes '
                               '(choosing it waits for that slice to finish)')
                nosup_est = _variant_estimate_str(early.job_no_supports, early.result)
                from .background_slice import _sliced_with_supports
                if early.result and await asyncio.to_thread(_sliced_with_supports, early.result):
                    # The first slice used the designer's supports, so its numbers are
                    # not the without-supports print (#1373 review).
                    nosup_est = 'estimated after slicing without supports'
                if getattr(early, 'error', None) is not None and not early.result:
                    # The first slice failed: nothing is still slicing (#1373 review).
                    nosup_est = 'no estimate (the first slice failed)'
                    if sup_est == 'slicing...':
                        sup_est = 'no estimate (the first slice failed)'
                flag_lines = [
                    with_supports_line(sup_reason, sup_est, early.finding, clearance),
                    f"- Slice and print without supports: {nosup_est}",
                ]
                # Supports labeled "not recommended" are never the first
                # option: the without-supports line and button lead.
                sup_recommended = supports_recommended(early.finding, clearance)
                if not sup_recommended:
                    flag_lines.reverse()
                modeled = modeled_supports.safe_describe(path)
                if modeled:
                    flag_lines.extend(modeled)
                if clearance:
                    cdesc = support_clearance.describe(clearance)
                    if cdesc:
                        flag_lines.extend(cdesc)
                turned = split_turn_supports_note(latest)
                if turned:
                    flag_lines.append(turned)
                summary_extra = '\n' + '\n'.join(flag_lines)

                sup_label = 'Slice and print with supports (bed is clear)' if can_print else 'Slice with supports'
                nosup_label = 'Slice and print without supports (bed is clear)' if can_print else 'Slice without supports'
                if not sup_recommended:
                    # The button says what its menu line says (#1373 item 2).
                    sup_label = ('Slice and print with supports, not recommended (bed is clear)'
                                 if can_print else 'Slice with supports, not recommended')

                fix_actions = []
                # Extend and sink rewrite the authored mesh along its own Z: on a
                # turned job that is another side of the model, so they are not
                # offered (#1373 review). Reorient is not offered where its turn
                # would not reach the slice or would undo a fit: a split or a
                # rearranged plan slices its own poses, a fit rotation was needed
                # to fit the bed, and several copies would need a new bed check.
                turned_job = bool((latest.get('orientation') or {}).get('rotation'))
                reorient_ok = (offer_reorient and not turned_job and int(chosen_quantity or 1) == 1
                               and not latest.get('full_size_split_plan')
                               and not latest.get('full_size_rearrange_plan')
                               and latest.get('plate_mode') != 'separate')
                if clearance and support_clearance.risky(clearance):
                    for f in clearance.get('findings') or []:
                        for fix in f.get('fixes') or []:
                            # "Extend underside to plate" is only a repair when
                            # the underside faces the plate (#922 gap 5): over
                            # another part there is no plate to extend to.
                            if fix.get('fix') == 'extend_underside' and f.get('over') == 'plate' \
                                    and not turned_job \
                                    and not any(a['kind'] == 'extend' for a in fix_actions):
                                fix_actions.append({'kind': 'extend', 'label': 'Extend underside to plate', 'finding': f})
                                if fix.get('added_volume_mm3') is not None:
                                    flag_lines.append(
                                        f"- Extend underside to plate adds about "
                                        f"{fix['added_volume_mm3']:.0f} mm3 of material below the underside.")
                            elif fix.get('fix') == 'sink' and not turned_job \
                                    and not any(a['kind'] == 'sink' for a in fix_actions):
                                fix_actions.append({'kind': 'sink', 'label': 'Flatten (sink) to plate', 'finding': f})
                                if fix.get('removed_volume_mm3') is not None:
                                    flag_lines.append(
                                        f"- Flatten (sink) to plate removes about "
                                        f"{fix['removed_volume_mm3']:.0f} mm3 that would end up below the plate.")
                            elif fix.get('fix') == 'reorient' and reorient_ok \
                                    and not any(a['kind'] == 'reorient' for a in fix_actions):
                                fix_actions.append({'kind': 'reorient', 'label': 'Reorient (lay flat)', 'finding': f})
                    summary_extra = '\n' + '\n'.join(flag_lines)

                menu_options = [
                    {'kind': 'sup', 'label': sup_label},
                    {'kind': 'nosup', 'label': nosup_label},
                ]
                if not sup_recommended:
                    menu_options.reverse()
                for act in fix_actions:
                    menu_options.append(act)
                if clearance and support_clearance.risky(clearance):
                    # The one explicit waiver for a low-clearance finding
                    # (#922 gap 2): neither "with supports" nor "without
                    # supports" records it.
                    menu_options.append({'kind': 'print_anyway',
                                         'label': PRINT_ANYWAY_RISK_LABEL if can_print else SLICE_ANYWAY_RISK_LABEL})
                menu_options.append({'kind': 'slice_only', 'label': 'Slice only'})
                menu_options.append({'kind': 'scale', 'label': SCALE_LABEL})
                menu_options.append({'kind': 'cancel', 'label': cancel_label})
                first_labels = [opt['label'] for opt in menu_options]
            else:
                summary_extra = ''
                menu_options = [
                    {'kind': 'print', 'label': print_label},
                    {'kind': 'slice_only', 'label': 'Slice only'},
                    {'kind': 'scale', 'label': SCALE_LABEL},
                    {'kind': 'cancel', 'label': cancel_label},
                ]
                if not can_print:
                    menu_options = menu_options[1:]
                first_labels = [opt['label'] for opt in menu_options]

            shown_shortages = []
            spool_text = await _spool_projection_text(mj, latest, manifest, state, chosen_quantity,
                                                      shortages=shown_shortages)
            if color_text:
                spool_text = (spool_text + '\n' + color_text) if spool_text else color_text
            estimate_s = source_estimate[0] * chosen_quantity if source_estimate else None
            # No source estimate: there is no time to show yet (the post-slice message gives it).
            estimate_text = '' if estimate_s is None else f"~{mj.human_dur(estimate_s + 300)}"
            if chosen_quantity and chosen_quantity != 1:
                estimate_text = ' · '.join(x for x in (f"{chosen_quantity} copies", estimate_text) if x)
            picked = await _ask(adapter, source, key,
                ' · '.join(x for x in ('Ready to slice', names, estimate_text) if x)
                + summary_extra
                + ('\n' + spool_text if spool_text else '')
                + ('\n' + scale_note if scale_note else '') + caveat,
                first_labels, timeout,
                on_shown=early.release.set, approval=True)

            if is_flagged:
                opt = menu_options[picked]
                if opt['kind'] == 'sup':
                    supports_choice = True
                    choice = 0 if can_print else 1  # no print delivery: the slice-only path
                    # JOB.json must be stable before the decision is recorded:
                    # a still-running speculative slice rewrites it and would
                    # clobber the decision (see BackgroundSlice.settle).
                    if hasattr(early, 'settle'):
                        await early.settle()
                    mj.record_support_decision(folder, True)
                    mj.record_supports_question(folder, question_audit)
                    # "With supports" does NOT waive a low-clearance finding
                    # (#922 gap 2): supports cannot fit into a sub-1.0 mm gap,
                    # so no clearance acknowledgement is recorded for this
                    # choice; the flagged regions still owe post-slice
                    # toolpath coverage verification before start (below).
                    latest = json.loads(jp.read_text())
                    binding = early.supports_binding or slice_binding(latest, decisions['source_sha256'])
                    recipe_sha = binding_sha256(binding)
                    break
                elif opt['kind'] == 'nosup':
                    supports_choice = False
                    choice = 0 if can_print else 1  # no print delivery: the slice-only path
                    if hasattr(early, 'settle'):
                        await early.settle()
                    mj.record_support_decision(folder, False)
                    mj.record_supports_question(folder, question_audit)
                    if early.finding:
                        # An explicit override, bound to the slicer warning
                        # and the artifact hash (#922 gap 3) -- never a
                        # silent pass for a single-mesh cantilever.
                        slice_warnings.record_unsupported_cantilever_override(
                            folder, early.finding, source_file=path)
                    if clearance and support_clearance.risky(clearance):
                        # "Without supports" is the operator's explicit
                        # as-authored decision after seeing the findings, and
                        # is recorded as such (#922 gap 2 scopes the
                        # no-waive rule to the "with supports" choice, which
                        # promises supports that cannot fit a sub-1.0 mm gap;
                        # the witch-ghost corpus case pins this behavior).
                        # Recorded after the selected variant is installed
                        # (below), so the acknowledgement binds its hashes.
                        clearance_ack_pending = 'without_supports'
                    latest = json.loads(jp.read_text())
                    binding = slice_binding(latest, decisions['source_sha256'])
                    recipe_sha = binding_sha256(binding)
                    break
                elif opt['kind'] == 'print_anyway':
                    # Like every print button: it prints only where print delivery is
                    # on, and says the bed is clear (#1373 review: it used to start a
                    # print on a slice-only install and record an unasked bed check).
                    choice = 0 if can_print else 1
                    if hasattr(early, 'settle'):
                        await early.settle()
                    mj.record_supports_question(folder, question_audit)
                    # The explicit waiver (#922 gap 2): the only menu choice
                    # that satisfies a low-clearance finding without a
                    # verified geometry repair.
                    if clearance and support_clearance.risky(clearance):
                        # Deferred like the other choices, so the
                        # acknowledgement binds the final artifact's hashes.
                        clearance_ack_pending = 'print_anyway'
                    latest = json.loads(jp.read_text())
                    binding = slice_binding(latest, decisions['source_sha256'])
                    recipe_sha = binding_sha256(binding)
                    break
                elif opt['kind'] == 'reorient':
                    # Wire the reorient fix the findings name (#922 gap 7):
                    # lay the largest face flat, record the rotation with NO
                    # supports value, and clear the recorded support decision
                    # and any clearance acknowledgement, so the post-rotation
                    # slice evaluates support warnings freshly. PR #923
                    # delivers the matching slice_job/_job_fit half (a plain
                    # rotation must not become supports=False decided); this
                    # branch does not duplicate it.
                    from . import edit_plan
                    try:
                        tris = [tuple(map(tuple, tri)) for p in support_clearance.leaves(path)
                                for tri in p['tris']]
                    except Exception:
                        tris = []
                    found = edit_plan.lay_flat_rotation(tris) if tris else None
                    already_flat = found is not None and edit_plan.lies_flat_already(found[0])
                    if found is None or already_flat:
                        # Nothing to turn: no orientation is recorded. A
                        # recorded identity turn changed nothing on the bed
                        # but made a multicolor job slice as a resized one.
                        offer_reorient = False
                        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                           content=('This model already lies flat on its largest face, so '
                                                    'nothing was rotated. The support question is unchanged.'
                                                    if already_flat else
                                                    'I could not work out a flat reorientation for this model, '
                                                    'so nothing was rotated. The support question is unchanged.'))
                        # Nothing turned: the slice already running is still the right one (#1373 review).
                        continue
                    await early.discard()  # a real turn: the running slice is for the old pose
                    matrix, dims = found
                    # The rotated (unscaled) envelope goes with the rotation,
                    # as every other orientation writer records it: without
                    # it the bed pre-check and "make it N mm tall" measure the
                    # authored pose, not the one that prints.
                    latest['orientation'] = {'rotation': matrix,
                                             **({'dims_full_mm': [round(float(v), 4) for v in dims]}
                                                if dims else {})}
                    latest.pop('support_decision', None)
                    latest.pop('clearance_acknowledgement', None)
                    # The cached report describes the pose as authored;
                    # replace it with the rotated pose's report, or the old
                    # finding follows the job forever and the repair reads as
                    # if it changed nothing (#922 gap 7 review follow-up).
                    rotated = await _refresh_posed_clearance(latest, path)
                    clearance = rotated
                    if rotated is not None:
                        early.clearance = rotated
                    mj.write_job(folder, latest)
                    offer_reorient = False
                    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                       content=_voiced('Reoriented: laid the largest face flat. The support '
                                                       'question will be evaluated again for the new orientation.',
                                                       'rotation', source, iid))
                    binding = slice_binding(latest, decisions['source_sha256'])
                    recipe_sha = binding_sha256(binding)
                    await early.start(jp, binding, lambda job_path, work, runner, copies=chosen_quantity, **kwargs:
                                      _heavy(mj.slice_job, str(job_path), recipe_sha256=kwargs.get('recipe_sha256', recipe_sha), **(
                                          {'copies': copies} if copies and int(copies) > 1 else {})))
                    await early.started.wait()
                    if (early.running() and not early.first_slice_done.is_set()
                            and getattr(early, 'release', None) is not None
                            and early.release.is_set()):
                        await early.first_slice_done.wait()
                    continue
                elif opt['kind'] == 'extend':
                    f = opt['finding']
                    # Stop the background slices before touching the source or
                    # JOB.json: a running slice reads the source, and the
                    # speculative slice's restore would clobber the repair's
                    # manifest/clearance updates written below.
                    await early.discard()
                    res = support_clearance.extend_underside(path, gap_mm=f['clearance_mm']['min'])
                    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                       content=_voiced(
                                           f"Extended underside to touch build plate ({res['modified_vertices']} vertices adjusted, "
                                           f"added {res.get('added_volume_mm3', 0):.0f} mm3). Re-analyzing...",
                                           'mesh_repair', source, iid))
                    new_sha = mj.file_sha256(path)
                    decisions['source_sha256'] = new_sha
                    manifest['source_sha256'] = new_sha
                    latest['color_manifest'] = manifest
                    clearance_rep = support_clearance.safe_analyze(
                        path, first_layer_mm=support_clearance.job_first_layer_mm(latest))
                    latest['support_clearance'] = clearance_rep
                    early.clearance = clearance_rep
                    # The repair changed the source bytes: rebind the saved
                    # color decisions to the repaired source, or the restarted
                    # slice refuses them for binding the old SHA256.
                    validated = validate_decisions(manifest, decisions, state, mj.resolve_filament_selection,
                                       selected_plate_ids=selected_plates)
                    mj.set_color_decisions(latest, validated)
                    mj.write_job(folder, latest)
                    binding = slice_binding(latest, new_sha)
                    recipe_sha = binding_sha256(binding)
                    await early.start(jp, binding, lambda job_path, work, runner, copies=chosen_quantity, **kwargs:
                                      _heavy(mj.slice_job, str(job_path), recipe_sha256=kwargs.get('recipe_sha256', recipe_sha), **(
                                          {'copies': copies} if copies and int(copies) > 1 else {})))
                    await early.started.wait()
                    if (early.running() and not early.first_slice_done.is_set()
                            and getattr(early, 'release', None) is not None
                            and early.release.is_set()):
                        await early.first_slice_done.wait()
                    continue
                elif opt['kind'] == 'sink':
                    f = opt['finding']
                    await early.discard()  # see the extend action above
                    res = support_clearance.sink_model(path, dz=f['clearance_mm']['max'])
                    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                       content=_voiced(
                                           f"Flattened model to plate by sinking {res['sink_mm']} mm "
                                           f"({res.get('removed_volume_mm3', 0):.0f} mm3 ends up below the plate and is clipped). Re-analyzing...",
                                           'mesh_repair', source, iid))
                    new_sha = mj.file_sha256(path)
                    decisions['source_sha256'] = new_sha
                    manifest['source_sha256'] = new_sha
                    latest['color_manifest'] = manifest
                    clearance_rep = support_clearance.safe_analyze(
                        path, first_layer_mm=support_clearance.job_first_layer_mm(latest))
                    latest['support_clearance'] = clearance_rep
                    early.clearance = clearance_rep
                    # The repair changed the source bytes: rebind the saved
                    # color decisions to the repaired source, or the restarted
                    # slice refuses them for binding the old SHA256.
                    validated = validate_decisions(manifest, decisions, state, mj.resolve_filament_selection,
                                       selected_plate_ids=selected_plates)
                    mj.set_color_decisions(latest, validated)
                    mj.write_job(folder, latest)
                    binding = slice_binding(latest, new_sha)
                    recipe_sha = binding_sha256(binding)
                    await early.start(jp, binding, lambda job_path, work, runner, copies=chosen_quantity, **kwargs:
                                      _heavy(mj.slice_job, str(job_path), recipe_sha256=kwargs.get('recipe_sha256', recipe_sha), **(
                                          {'copies': copies} if copies and int(copies) > 1 else {})))
                    await early.started.wait()
                    if (early.running() and not early.first_slice_done.is_set()
                            and getattr(early, 'release', None) is not None
                            and early.release.is_set()):
                        await early.first_slice_done.wait()
                    continue
                elif opt['kind'] == 'slice_only':
                    choice = 1
                    break
                elif opt['kind'] == 'cancel':
                    choice = 3
                    break
                elif opt['kind'] == 'scale':
                    pass  # drops through to scaling direction question
            else:
                picked = picked if can_print else picked + 1
                if picked != 2:
                    choice = picked
                    break
            direction = await _ask(adapter, source, key,
                                    'Scale the model bigger or smaller?',
                                    ['Bigger', 'Smaller', 'Cancel scaling'], timeout,
                                    parser=_scale_direction_reply, approval=True)
            if direction == 2:
                continue
            amount = await _ask(adapter, source, key,
                                f"How much {'bigger' if direction == 0 else 'smaller'}?",
                                ['1%', '5%', '10%', 'Cancel scaling'], timeout,
                                parser=_scale_percent_reply, approval=True)
            if amount == len(SCALE_STEP_PERCENTS):
                continue
            step = SCALE_STEP_PERCENTS[amount]
            factor = (100 + step) / 100 if direction == 0 else (100 - step) / 100
            current_scale = float(latest.get('scale') or 1.0)
            new_scale = round(current_scale * factor, 6)
            orientation = latest.get('orientation') or {}
            if orientation.get('rotation'):
                probe_kw = {'rotation': orientation['rotation'],
                            'supports': bool(orientation.get('supports')), 'copy_limit': 1,
                            **({'brim_policy': {'type': 'outer_only',
                                                'width_mm': orca_adapter.ORIENTATION_BRIM_MM}}
                               if orientation.get('brim') else {})}
            else:
                probe_kw = {'copy_limit': max(1, int(chosen_quantity or 1))}
            try:
                capacity_at = await _heavy(orca_adapter.orca_plate_capacity, path, fit_state,
                                           scale=new_scale, **probe_kw)
            except Exception:
                logger.exception('scale fit check failed')
                await adapter.send(chat_id=source.chat_id,
                                   content=f'I could not check whether {_pct(new_scale)} fits the bed, '
                                           f'so nothing was scaled. The model stays at {_pct(current_scale)}.',
                                   metadata=metadata)
                continue
            if capacity_at < 1:
                # Refused as asked — but offer the biggest size that does fit,
                # searched under the same layout the probe just refused. A
                # bigger offer is always worth asking about; a smaller one
                # only when the current size is itself a split/rearranged
                # plan rather than a one-plate fit.
                try:
                    fit_scale = await _heavy(orca_adapter.largest_fitting_scale, path, fit_state,
                                             new_scale, **probe_kw)
                except Exception:
                    # The slicer failed while searching: say so, never imply
                    # that no smaller size fits.
                    logger.exception('largest fitting scale search failed')
                    await adapter.send(chat_id=source.chat_id,
                                       content=f'{_pct(new_scale)} does not fit the {_printer_name(latest)} bed, '
                                               f'so nothing was scaled. The model stays at {_pct(current_scale)}. '
                                               f'I could not work out the biggest size that fits because the '
                                               f'slicer check failed.',
                                       metadata=metadata)
                    continue
                planned = latest.get('full_size_split_plan') or latest.get('full_size_rearrange_plan')
                offer = fit_scale is not None and (
                    fit_scale > current_scale + 0.0005
                    or (planned and fit_scale < current_scale - 0.0005))
                if not offer:
                    note = (' That is the biggest size that fits.' if fit_scale is not None
                            and abs(fit_scale - current_scale) <= 0.0005 else '')
                    await adapter.send(chat_id=source.chat_id,
                                       content=f'{_pct(new_scale)} does not fit the {_printer_name(latest)} bed, '
                                               f'so nothing was scaled. The model stays at {_pct(current_scale)}.{note}',
                                       metadata=metadata)
                    continue
                picked_fit = await _ask(adapter, source, key,
                                        f'{_pct(new_scale)} does not fit the {_printer_name(latest)} bed. '
                                        f'The biggest size that fits is {_pct(fit_scale)}. '
                                        f'Scale to {_pct(fit_scale)}?',
                                        [f'Scale to {_pct(fit_scale)}', f'Keep {_pct(current_scale)}',
                                         'Cancel scaling'],
                                        timeout, parser=_scale_fit_offer_reply, approval=True)
                if picked_fit != 0:
                    continue
                new_scale = fit_scale
                try:
                    capacity_at = await _heavy(orca_adapter.orca_plate_capacity, path, fit_state,
                                               scale=new_scale, **probe_kw)
                except Exception:
                    logger.exception('scale fit check failed')
                    await adapter.send(chat_id=source.chat_id,
                                       content=f'I could not check whether {_pct(new_scale)} fits the bed, '
                                               f'so nothing was scaled. The model stays at {_pct(current_scale)}.',
                                       metadata=metadata)
                    continue
                if capacity_at < 1:
                    await adapter.send(chat_id=source.chat_id,
                                       content=f'{_pct(new_scale)} does not fit the {_printer_name(latest)} bed, '
                                               f'so nothing was scaled. The model stays at {_pct(current_scale)}.',
                                       metadata=metadata)
                    continue
            notes = []
            if latest.get('full_size_split_plan') or latest.get('full_size_rearrange_plan'):
                # Those plans pin the authored layout at 100%. A size verified
                # to fit one plate no longer needs one; a size that does not
                # fit one plate was refused above, so the plan is never
                # silently sliced at a size it was not planned for.
                latest.pop('full_size_split_plan', None)
                latest.pop('full_size_rearrange_plan', None)
                receipt.pop('full_size_split', None)
                receipt.pop('full_size_rearrange', None)
                notes.append('At this size it fits one plate, so it prints as one plate.')
            if capacity_at < chosen_quantity:
                chosen_quantity = capacity_at
                notes.append(f'Only {chosen_quantity} {"copy fits" if chosen_quantity == 1 else "copies fit"} '
                             f'at this size, so the count is now {chosen_quantity}.')
            latest['scale'] = new_scale
            latest['copies'] = chosen_quantity
            # Gaps scale with the model (#1373 review): a 1.6 mm gap is 0.8 mm at 50%.
            await _refresh_posed_clearance(latest, path)
            mj.write_job(folder, latest)
            receipt.update(scale=new_scale, copies=chosen_quantity)
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
            scale_note = (f'Scaled to {_pct(new_scale)} of the authored size.'
                          + ((' ' + ' '.join(notes)) if notes else ''))
            binding = slice_binding(latest, decisions['source_sha256'])
            recipe_sha = binding_sha256(binding)  # the worker refuses to slice any other recipe
            await early.start(jp, binding, lambda job_path, work, runner, copies=chosen_quantity, **kwargs:
                              _heavy(mj.slice_job, str(job_path), recipe_sha256=kwargs.get('recipe_sha256', recipe_sha), **(
                                  {'copies': copies} if copies and int(copies) > 1 else {})))
            await early.started.wait()  # armed before the repeated question goes up
        if choice == 3:
            await early.discard()  # the early slice was for a job that will not print
            receipt['status'] = 'CANCELLED'
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
            await adapter.send(chat_id=source.chat_id,
                               content='Cancelled. Nothing was sliced, uploaded, or started.',
                               metadata=metadata)
            return {'action': 'skip', 'reason': 'operator cancelled print request'}
        if is_flagged and hasattr(early, 'settle'):
            # A flagged menu answered with Slice only reaches this guard with
            # no recorded decision to settle for; the speculative slice must
            # still be finished before JOB.json is compared to the binding.
            await early.settle()
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

        # Set when an AI fix fails (not when it is declined): the same request
        # on the same job fails the same way, so the offer is withdrawn for the
        # rest of this intake instead of being re-posted.
        ai_fix_failed = False

        async def _ai_fix_attempt(problem):
            """One AI fix cycle (#978): propose on the heavy tier, show every
            change (with a render preview for geometry), confirm, then apply.

            Slicer changes are validated edit plans applied to the job record;
            geometry/mesh repairs write a copy of the source and repoint the
            job at it (the original file is never overwritten, and the color
            decisions are rebound to the repaired bytes exactly as the
            pre-slice extend/sink repairs do). Returns the applied proposal,
            or None when the proposal failed, was refused or was declined --
            nothing changed then. Applying never waives a gate and records
            no acknowledgement: the caller re-slices, every post-slice gate
            runs again on the fixed slice, and only the gate's own outcome
            (repaired, or an approver's Print anyway) is recorded. Requests
            go through the same [ai] allowlist and per-user rate limit as the
            AI edit lane (#902). The confirmation is not an approval
            question; where the Fix button sits inside an approval question
            (the post-slice gate), only approvers can reach it.
            """
            nonlocal path, clearance, validated, binding, recipe_sha, latest, ai_fix_failed
            from . import ai_fix, edit_plan
            allowed, why = ai_fix.lane_allowed(getattr(source, 'user_id', ''))
            if not allowed:
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content=f'No AI fix was requested ({why}), so nothing was changed.')
                return None
            job_now = json.loads(jp.read_text())
            # One fix budget per job, across every question here and the chat
            # repair flow, each request attributed to who asked (#978 follow-up).
            if ai_fix.attempts_left(job_now) <= 0:
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content=ai_fix.out_of_attempts_message())
                return None
            actor = (f"{getattr(getattr(source, 'platform', None), 'value', None) or 'discord'}:"
                     f"{getattr(source, 'user_id', '')}")
            ai_fix.note_attempt(job_now, actor=actor, surface='intake')
            mj.write_job(folder, job_now)
            mesh = await asyncio.to_thread(ai_fix.mesh_report, path)
            # Ground the proposal in what was measured on this job (#985): the
            # clearance gap a geometry fix must match, the bed it must fit and
            # the nozzle it slices for.
            measured_gap = ai_fix.measured_clearance_gap(clearance)
            bed_mm = nozzle_mm = None
            with contextlib.suppress(Exception):
                from ..runtime import _config
                printer_cfg = (_config.cfg() or {}).get('printer') or {}
                bed_mm = printer_cfg.get('bed_mm')
                nozzle_mm = printer_cfg.get('nozzle_diameter')
            try:
                proposal = await _heavy(ai_fix.propose_fix, problem, job_now, mesh=mesh,
                                        bed_mm=bed_mm, nozzle_mm=nozzle_mm,
                                        clearance_gap_mm=measured_gap)
            except Exception as exc:  # noqa: BLE001 - a failed proposal changes nothing
                logger.warning('AI fix proposal failed: %r', exc)
                # An endpoint that did not answer may answer next time; a
                # refused proposal is refused again.
                ai_fix_failed = ai_fix_failed or isinstance(exc, ai_fix.FixError)
                detail = (ai_fix.reason(exc) if isinstance(exc, ai_fix.FixError)
                          else 'the AI endpoint did not answer')
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content=f'No AI fix could be prepared ({detail}), so nothing '
                                           'was changed.')
                return None
            try:
                ai_fix.check_file_fix(job_now, proposal)
                enriched = [edit_plan.enrich_plan(plan, job_now,
                                                  path if Path(path).is_file() else None)
                            for plan in proposal.plans]
            except (edit_plan.PlanError, ai_fix.FixError) as exc:
                ai_fix_failed = True
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content=f'The AI proposed a fix that cannot be applied '
                                           f'({ai_fix.reason(exc)}), so nothing was changed.')
                return None
            fixed_path = None
            report = {}
            if proposal.geometry or proposal.mesh_repair:
                try:
                    fixed_path, report = await _heavy(ai_fix.build_fixed_copy,
                                                      path, proposal, folder,
                                                      measured_gap_mm=measured_gap)
                except Exception as exc:  # noqa: BLE001 - an unbuildable fix changes nothing
                    logger.warning('AI fix could not be built: %r', exc)
                    ai_fix_failed = True
                    detail = f' ({ai_fix.reason(exc)})' if isinstance(exc, ai_fix.FixError) else ''
                    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                       content=f'That fix could not be built{detail}, so nothing '
                                               'was changed.')
                    return None
                preview_failed = False
                try:
                    image_path = await _heavy(ai_fix.preview_image, fixed_path,
                                              Path(fixed_path).parent / 'ai-fix-preview.png')
                    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                       content='Preview of the fixed model (the original file '
                                               'is unchanged).\nMEDIA:' + image_path)
                except Exception:  # noqa: BLE001 - said out loud below, not silently skipped
                    preview_failed = True
            else:
                preview_failed = False
            if mesh:
                report['walls'] = mesh.get('walls')
            lines = ai_fix.fix_lines(proposal, report)
            lines.append('Nothing is overwritten: a fix works on a copy, and applying it '
                         're-slices and runs every check again.')
            if preview_failed:
                lines.append('(A preview of the fixed model could not be rendered on this install.)')
            choices = [ai_fix.APPLY_LABEL]
            if (report.get('mesh') or {}).get('openings'):
                choices.append(ai_fix.APPLY_KEEP_OPENINGS_LABEL)
            choices.append(print_next.CANCEL_LABEL)
            try:
                pick = await _ask(adapter, source, key, '\n'.join(lines), choices, timeout)
            except BaseException:
                ai_fix.discard_fixed_copy(fixed_path)  # an expired question leaves nothing behind
                raise
            if choices[pick] not in (ai_fix.APPLY_LABEL, ai_fix.APPLY_KEEP_OPENINGS_LABEL):
                ai_fix.discard_fixed_copy(fixed_path)
                return None
            if choices[pick] == ai_fix.APPLY_KEEP_OPENINGS_LABEL:
                ai_fix.discard_fixed_copy(fixed_path)
                try:
                    fixed_path, report = await _heavy(ai_fix.build_fixed_copy, path, proposal,
                                                      folder, fill_openings=False,
                                                      measured_gap_mm=measured_gap)
                except Exception as exc:  # noqa: BLE001 - nothing applied
                    detail = f' ({ai_fix.reason(exc)})' if isinstance(exc, ai_fix.FixError) else ''
                    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                       content=f'Keeping the openings left nothing to repair{detail}; '
                                               'no fix applied.')
                    return None
            # The fix was built from the bytes the job is bound to; a source
            # that changed since gets no fix built for older bytes.
            if not ai_fix.source_unchanged(path, decisions['source_sha256'], mj.file_sha256):
                ai_fix.discard_fixed_copy(fixed_path)
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content='The model file changed while the fix was being prepared, '
                                           'so the fix was not applied.')
                return None
            new_decisions = None
            if proposal.filament_map:
                # Re-resolve the remapped filaments against the target slots
                # BEFORE anything changes (#985): an unresolvable slot, or a
                # mapping that matches no filament here, refuses the whole fix.
                try:
                    new_decisions = ai_fix.remapped_decisions(
                        {'color_decisions': decisions, 'color_manifest': manifest},
                        proposal.filament_map,
                        lambda sel: mj.resolve_filament_selection(state, sel))
                except ai_fix.FixError as exc:
                    ai_fix_failed = True
                    ai_fix.discard_fixed_copy(fixed_path)
                    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                       content=f'{ai_fix.reason(exc)}, so nothing was changed.')
                    return None
            if fixed_path is not None:
                fixed_path = ai_fix.commit_fixed_copy(fixed_path, report)
            latest = json.loads(jp.read_text())
            # One undoable record for the whole fix: the file swap, the color
            # remap, the profile settings and every plan, restored together by
            # ai_fix.undo_last_fix.
            ai_fix.apply_to_job(latest, proposal, enriched, fixed_path=fixed_path, actor=actor,
                                problem=problem, report=report, file_sha256=mj.file_sha256)
            if new_decisions is not None:
                decisions.clear()
                decisions.update(new_decisions)
                validated = validate_decisions(manifest, decisions, state,
                                               mj.resolve_filament_selection,
                                               selected_plate_ids=selected_plates)
                mj.set_color_decisions(latest, validated)
            if fixed_path is not None:
                new_sha = mj.file_sha256(fixed_path)
                path = fixed_path
                decisions['source_sha256'] = new_sha
                manifest['source_sha256'] = new_sha
                latest['color_manifest'] = manifest
                validated = validate_decisions(manifest, decisions, state,
                                               mj.resolve_filament_selection,
                                               selected_plate_ids=selected_plates)
                mj.set_color_decisions(latest, validated)
                # A pre-existing acknowledgement belongs to the old file.
                latest.pop('clearance_acknowledgement', None)
            if fixed_path is not None or any(p.get('kind') in ('rotate', 'lay_flat') for p in enriched):
                # The clearance report must describe the fixed model in its
                # fixed pose, or the old finding follows the job and a real
                # repair reads as changing nothing (the #922 gap 7 rule the
                # pre-slice Reorient button already follows).
                try:
                    matrix = (latest.get('orientation') or {}).get('rotation')
                    first = support_clearance.job_first_layer_mm(latest)
                    clearance = (await asyncio.to_thread(support_clearance.rotated_report, path, matrix,
                                                         first_layer_mm=first)
                                 if matrix else await asyncio.to_thread(support_clearance.safe_analyze, path,
                                                                        first_layer_mm=first))
                    latest['support_clearance'] = clearance
                except Exception:  # noqa: BLE001 - re-analyzed on the next slice instead
                    latest.pop('support_clearance', None)
                    clearance = None
            mj.write_job(folder, latest)
            binding = slice_binding(latest, decisions['source_sha256'])
            recipe_sha = binding_sha256(binding)
            if receipt.get('slice_approval'):
                receipt['slice_approval']['recipe_sha256'] = recipe_sha
            receipt['ai_fix'] = {'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                                 'problem': str(problem)[:300],
                                 'changes': proposal.describe(),
                                 'served_by': proposal.served_by,
                                 'actor': actor}
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
            return proposal

        def _no_fix_left_note():
            """Why the heavy tier offers no fix: one failed here, or the budget is spent."""
            from . import ai_fix
            return ai_fix.FAILED_NOTE if ai_fix_failed else ai_fix.out_of_attempts_message()

        def _fix_budget_left():
            """AI fix attempts this job may still use (0 when none, or unreadable,
            or once a fix failed in this intake)."""
            from . import ai_fix
            if ai_fix_failed:
                return 0
            try:
                return ai_fix.attempts_left(json.loads(jp.read_text()))
            except (OSError, ValueError):
                return 0


        prep_sent = False  # one combined Preparing + Uploading message after slicing, with the real estimate
        try:
            if supports_choice is True:
                out = await early.take_supports()
                if out is None and early.supports_error is not None:
                    raise early.supports_error
                if out is None:
                    latest = json.loads(jp.read_text())
                    binding = slice_binding(latest, decisions['source_sha256'])
                    recipe_sha = binding_sha256(binding)
                    out = await _with_stop(adapter, source, key, metadata,
                                           _voiced("Slicing with supports. Press Stop to cancel.", 'supports', source, iid),
                                           _heavy(mj.slice_job, str(jp), recipe_sha256=recipe_sha, **(
                                               {'copies': chosen_quantity} if chosen_quantity and chosen_quantity > 1 else {})),
                                           allow_stop=True)
            elif supports_choice is False:
                out = await early.take_no_supports()
                if out is None and early.error is not None:
                    raise early.error
                if out is None:
                    out = await _with_stop(adapter, source, key, metadata,
                                           _voiced("Slicing. Press Stop to cancel.", 'slice', source, iid),
                                           _heavy(mj.slice_job, str(jp), recipe_sha256=recipe_sha, **(
                                               {'copies': chosen_quantity} if chosen_quantity and chosen_quantity > 1 else {})),
                                           allow_stop=True)
            else:
                if early.running():
                    # Still slicing: the wait goes through the same progress/Stop
                    # flow as a foreground slice (P2-58). A bare take() here gave
                    # the potentially longest wait of the intake no progress and
                    # a Stop button that did nothing.
                    out = await _with_stop(adapter, source, key, metadata,
                                           _voiced("Waiting for the background slice to finish. Press Stop to cancel.",
                                                   'slice', source, iid),
                                           _await_background_slice(early, binding),
                                           allow_stop=True)
                else:
                    out = await early.take(binding)  # finished while the question was up
                if out is None and early.error is not None:
                    raise early.error
                if out is None:
                    if chosen_quantity and chosen_quantity > 1:
                        out = await _with_stop(adapter, source, key, metadata,
                                               _voiced("Slicing. Press Stop to cancel.", 'slice', source, iid),
                                               _heavy(mj.slice_job, str(jp), copies=chosen_quantity, recipe_sha256=recipe_sha),
                                               allow_stop=True)  # nothing uploaded yet, so Stop is safe
                    else:
                        out = await _with_stop(adapter, source, key, metadata,
                                               _voiced("Slicing. Press Stop to cancel.", 'slice', source, iid),
                                               _heavy(mj.slice_job, str(jp), recipe_sha256=recipe_sha),
                                               allow_stop=True)
        except mj.FilamentWarningPending as pending:
            # Filament findings warn instead of refusing (owner ruling,
            # catduck, 2026-11-10): the operator is asked once, and only an
            # explicit acceptance — recorded bound to these warnings, this
            # source and these plates — lets the same slice run again.
            # Declining stops here with nothing sliced, uploaded or started.
            await early.discard()
            picked = await _ask(adapter, source, key,
                                'Filament warnings for this print:\n'
                                + '\n'.join('- ' + w for w in pending.warnings)
                                + '\nAccept these warnings and print anyway?',
                                ['Print anyway (accept the filament warnings)', 'No, cancel'],
                                timeout, approval=True)
            if picked != 0:
                receipt.update(status='CANCELLED', sliced=False, uploaded=False, started=False)
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content='Filament warnings not accepted. Nothing was sliced, '
                                           'uploaded or started.')
                return {'action': 'skip', 'reason': 'filament warnings declined'}
            mj.record_filament_warning_acknowledgement(
                jp, warnings=pending.warnings, source_sha256=pending.source_sha256,
                selected_plates=pending.selected_plates, via='print_anyway')
            latest = json.loads(jp.read_text())
            if (latest.get('intake_id') != iid or latest.get('color_decisions') != validated
                    or mj.file_sha256(latest['downloaded_file']) != decisions['source_sha256']
                    or slice_binding(latest, decisions['source_sha256']) != binding):
                raise ValueError('saved job changed before the accepted re-slice; no slice started')
            attempt = int(receipt.get('slice_attempt') or 1) + 1
            receipt.update(status='SLICE_APPROVED', slice_attempt=attempt,
                           slice_approval={'intake_id': iid, 'source_sha256': decisions['source_sha256'],
                                           'decisions_sha256': mj.decisions_digest(validated), 'attempt': attempt,
                                           'recipe_version': RECIPE_VERSION, 'recipe_sha256': recipe_sha})
            receipt.pop('slice_error', None)
            receipt.pop('failure', None)
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
            # The accepted re-slice is slice-only, like a retry: starting
            # the new artifact requires a fresh button.
            choice = 1
            try:
                if chosen_quantity and chosen_quantity > 1:
                    out = await _with_stop(adapter, source, key, metadata,
                                           "Slicing with the accepted filament warnings. Press Stop to cancel.",
                                           _heavy(mj.slice_job, str(jp), copies=chosen_quantity, recipe_sha256=recipe_sha))
                else:
                    out = await _with_stop(adapter, source, key, metadata,
                                           "Slicing with the accepted filament warnings. Press Stop to cancel.",
                                           _heavy(mj.slice_job, str(jp), recipe_sha256=recipe_sha))
            except Exception as accepted_error:
                logger.exception('slice after filament warning acceptance failed')
                await early.discard()
                from . import slice_failure
                receipt.update(status='SLICE_FAILED', sliced=False, uploaded=False, started=False,
                               slice_error=str(accepted_error),
                               failure=slice_failure.report(accepted_error).as_dict())
                receipt.pop('slice_approval', None)
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content=slice_failure.post(accepted_error, retry_failed=True))
                return {'action': 'skip', 'reason': 'slice after filament warning acceptance failed'}
        except Exception as slice_error:
            logger.exception('slice failed')
            # A failed attempt has no reusable slice approval or G-code. Retry is a
            # separate button action, and its output still needs a fresh start button.
            await early.discard()
            from . import slice_failure
            receipt.update(status='SLICE_FAILED', sliced=False, uploaded=False, started=False,
                           slice_error=str(slice_error),
                           failure=slice_failure.report(slice_error).as_dict())
            for stale in ('slice_approval', 'bed_clear_approval', 'gcodes'):
                receipt.pop(stale, None)
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
            # A deterministic refusal says why in plain words and offers its
            # one safe fix instead of a blind retry that would fail the same way.
            from . import slice_refusal
            from . import slice_failure
            refusal = slice_refusal.explain(slice_error)
            # "Fix this for my printer" (#978): with the heavy AI tier ready,
            # a slice failure or refusal also offers an AI fix; without it,
            # today's behavior stands, plus the one-line note where a fix
            # would have been offered.
            from . import ai_fix as _ai_fix
            heavy = _ai_fix.intake_fix_offered()
            fix_ready = heavy and _fix_budget_left() > 0
            # No AI-fix note at all when the intake does not offer one (#1373).
            no_fix_note = _no_fix_left_note() if heavy else ''
            if refusal is not None and not refusal.fix and not fix_ready:
                await adapter.send(chat_id=source.chat_id, metadata=metadata, content=diagnostics.plain_post(
                    'Stage: slice. ' + refusal.plain, not_happened='Nothing was sliced, uploaded or started.',
                    next_step=refusal.next_step, subsystem='slicing', exc=slice_error)
                    + ('\n' + no_fix_note if no_fix_note else ''))
                return {'action': 'skip', 'reason': 'slice refused: ' + str(slice_error)}
            if refusal is not None and not refusal.fix:
                prompt = diagnostics.plain_post(
                    'Stage: slice. ' + refusal.plain, not_happened='Nothing was sliced, uploaded or started.',
                    next_step=refusal.next_step, subsystem='slicing', exc=slice_error)
                labels = [_ai_fix.FIX_LABEL, 'No, cancel']
            elif refusal is not None:
                prompt = diagnostics.plain_post('Stage: slice. ' + refusal.plain, next_step=refusal.next_step,
                                                subsystem='slicing', exc=slice_error)
                labels = [refusal.fix_label] + ([_ai_fix.FIX_LABEL] if fix_ready else []) + ['No, cancel']
            else:
                prompt = slice_failure.post(slice_error)
                if not slice_failure.explain(slice_error).retryable and not fix_ready:
                    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                       content=prompt + ('\n' + no_fix_note if no_fix_note else ''))
                    return {'action': 'skip', 'reason': 'slice failed: ' + str(slice_error)}
                labels = ((['Retry slice once (no print)']
                           if slice_failure.explain(slice_error).retryable else [])
                          + ([_ai_fix.FIX_LABEL] if fix_ready else []) + ['Cancel'])
            if not fix_ready and no_fix_note:
                # A fix the intake offers but cannot give now says why (#985):
                # this job's budget spent.
                prompt = prompt + '\n' + no_fix_note
            retry = None
            fix_attempts = 0
            ai_fixed = False
            while True:
                picked = await _ask(adapter, source, key, prompt, labels, timeout)
                if labels[picked] != _ai_fix.FIX_LABEL:
                    retry = picked
                    break
                fix_attempts += 1
                problem = ('Slice failure. ' + (refusal.plain if refusal is not None
                                                else slice_failure.explain(slice_error).plain))
                if await _ai_fix_attempt(problem) is None:
                    if fix_attempts >= _ai_fix.MAX_FIX_ATTEMPTS or _fix_budget_left() <= 0:
                        # Out of fix attempts (this question's or the job's):
                        # the question goes on without the Fix button; with
                        # nothing but Cancel left, that is the answer.
                        labels = [label for label in labels if label != _ai_fix.FIX_LABEL]
                        if len(labels) <= 1:
                            retry = len(labels)  # past index 0: read as cancel below
                            break
                    continue  # declined or unusable proposal: the same question is asked again
                ai_fixed = True
                retry = 0  # a fix was applied: fall through to the re-slice below
                break
            if retry == 0 and refusal is not None and refusal.fix and not ai_fixed:
                try:
                    await owned_worker.in_thread(slice_refusal.apply_fix, refusal.fix)
                except Exception:
                    logger.exception('slice refusal fix failed')
                    # The fix only writes the local slot record's color, so a
                    # Spoolman bind is not the remedy: name the slot and the
                    # /spool form that records its spool (and its color) (#839).
                    fix_slot = (refusal.fix or {}).get('slot')
                    remedy = (f"Record the spool loaded in slot {fix_slot} with "
                              f"`/spool change {fix_slot} <spool#>`, then ask again."
                              if fix_slot else
                              'Record the slot\'s spool with `/spool`, then ask again.')
                    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                       content='That confirmation could not be saved, so nothing was sliced, '
                                               f'uploaded or started. {remedy}')
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
            receipt.pop('failure', None)  # the first attempt's classification does not describe this one
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
            # A prior "slice and print" choice applies only to the failed output.
            # The retry is slice-only; starting the new artifact requires a fresh button.
            choice = 1
            while True:
                try:
                    if chosen_quantity and chosen_quantity > 1:
                        out = await _with_stop(adapter, source, key, metadata,
                                               _voiced("Retrying the slice. Press Stop to cancel.", 'slice', source, iid),
                                               _heavy(mj.slice_job, str(jp), copies=chosen_quantity, recipe_sha256=recipe_sha))
                    else:
                        out = await _with_stop(adapter, source, key, metadata,
                                               _voiced("Retrying the slice. Press Stop to cancel.", 'slice', source, iid),
                                               _heavy(mj.slice_job, str(jp), recipe_sha256=recipe_sha))
                    break
                except Exception as retry_error:
                    logger.exception('slice retry failed')
                    await early.discard()
                    receipt.update(status='SLICE_FAILED', sliced=False, uploaded=False, started=False,
                                   slice_error=str(retry_error),
                                   failure=slice_failure.report(retry_error).as_dict())
                    receipt.pop('slice_approval', None)
                    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                    again = slice_refusal.explain(retry_error)
                    failure_post = (diagnostics.plain_post('Stage: slice. ' + again.plain,
                                                           not_happened='Nothing was sliced, uploaded or started.',
                                                           next_step=again.next_step, subsystem='slicing',
                                                           exc=retry_error) if again is not None else
                                    slice_failure.post(retry_error, retry_failed=True))
                    # A failed re-slice -- the first retry, or the re-slice of
                    # an applied fix -- gets the same fix offer (#985), under
                    # the same per-question cap and the job's fix budget,
                    # instead of a dead end.
                    if (_ai_fix.intake_fix_offered() and fix_attempts < _ai_fix.MAX_FIX_ATTEMPTS
                            and _fix_budget_left() > 0):
                        picked = await _ask(adapter, source, key, failure_post,
                                            [_ai_fix.FIX_LABEL, 'Cancel'], timeout)
                        if picked == 0:
                            fix_attempts += 1
                            problem = ('Slice failure. ' + (again.plain if again is not None
                                                           else slice_failure.explain(retry_error).plain))
                            if await _ai_fix_attempt(problem) is not None:
                                attempt = int(receipt.get('slice_attempt') or 1) + 1
                                receipt.update(status='SLICE_APPROVED', slice_attempt=attempt,
                                               slice_approval={'intake_id': iid,
                                                               'source_sha256': decisions['source_sha256'],
                                                               'decisions_sha256': mj.decisions_digest(validated),
                                                               'attempt': attempt,
                                                               'recipe_version': RECIPE_VERSION,
                                                               'recipe_sha256': recipe_sha})
                                receipt.pop('slice_error', None)
                                receipt.pop('failure', None)
                                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                                continue
                        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                           content='Cancelled. Nothing was sliced, uploaded or started.')
                        return {'action': 'skip', 'reason': 'slice retry failed'}
                    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                       content=failure_post + (
                                           '\n' + _no_fix_left_note() if _ai_fix.intake_fix_offered()
                                           else ''))
                    return {'action': 'skip', 'reason': 'slice retry failed'}
        mj._timing(jp, 'intake_slice_returned')
        gcodes = [str(g) for g in (out if isinstance(out, list) else [out])]
        sliced = json.loads(jp.read_text())
        # Deliveries are bound to these exact bytes; a file that changes afterwards is refused.
        receipt.update(status='SLICED', sliced=True, gcodes=gcodes,
                       gcode_sha256={g: delivery.sha256(g) for g in gcodes})
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')

        def _coverage_result(gcode_list):
            """The post-slice support coverage result for these plates (#922 gaps 2 and 4).

            Coverage is graded per plate with each plate's own bounds
            (#963 item 4), and the job's recorded scale / placement drive
            the source-to-slice mapping when they are recorded (#963
            item 5). Nothing fails open silently (#963 item 7): an
            unreadable plate file, unreadable source bounds or a failed
            check comes back as a named entry in the result's
            'unverified' list, for the caller to record in the receipt —
            never as a silent pass and never as a hard refusal. Since
            #968 the caller routes an unverified result to the Print
            anyway / Cancel question instead of proceeding on it.
            """
            report = sliced.get('support_clearance') or clearance
            if not report:
                return None
            fit = sliced.get('fit') or {}
            recorded = sliced.get('scale') or fit.get('scale')
            placement = fit.get('placement_mm')
            source_bounds = None
            source_height = None
            bounds_note = None
            try:
                import numpy as _np
                _tris = _np.concatenate([p['tris'].reshape(-1, 3)
                                         for p in support_clearance.leaves(path)])
                source_bounds = (float(_tris[:, 0].min()), float(_tris[:, 1].min()),
                                 float(_tris[:, 0].max()), float(_tris[:, 1].max()))
                source_height = float(_tris[:, 2].max()) or None
            except Exception as exc:  # noqa: BLE001 - named below, not silent
                bounds_note = (f'source bounds unreadable ({exc}); coverage compared '
                               'without a source-to-slice mapping')
            plates = []
            for g in gcode_list:
                plate = {'gcode': str(g), 'source_bounds': source_bounds,
                         'source_height': source_height}
                if recorded:
                    plate['recorded_scale'] = float(recorded)
                if placement:
                    plate['placement_mm'] = tuple(float(v) for v in placement)
                if bounds_note:
                    plate['unverified_reason'] = bounds_note
                plates.append(plate)
            return support_clearance.verify_coverage_by_plate(
                report.get('regions') or [], plates)

        def _record_coverage_unverified(result):
            """Name an unverified coverage state in the receipt and the log (#963 item 7)."""
            receipt.update(support_coverage=result)
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
            logger.warning('support coverage unverified for intake %s: %s',
                           iid, result.get('unverified'))

        async def _support_override(cov_failure, low_clearance, resliced=False, unverified_result=None):
            """Ask Print anyway / Cancel for the post-slice support gates.

            'waived' when an approver waives them (Print anyway), 'fixed'
            when an AI fix made the re-sliced job pass every gate (nothing
            waived, nothing acknowledged), False after a Cancel (already
            answered in chat). Both truthy results let the start proceed.
            The low-clearance acknowledgement the start guard checks is set
            here, on a waiver only, from the clearance state the approver
            actually saw -- never from an AI fix (#978 review). The gates
            stay strict; this is the explicit override they name, so they
            never end in a dead end. Only print approvers may answer
            (approval=True), as for every safety waiver.

            ``unverified_result`` is a coverage result whose check could not
            run (#968): it asks the same question, naming why coverage could
            not be verified, and the waiver is recorded the same way.

            With the heavy AI tier ready (and the job's fix budget not spent),
            the gate first offers "Fix this for my printer" (#978) on its own
            unmarked question that anyone may answer; "Decide without a fix"
            then asks the approval-marked Print anyway / Cancel (#985). An
            applied fix re-slices here and the gates are graded again on the
            new slice -- passing them returns 'fixed' with NO waiver recorded
            (a fix is not a waiver), failing them asks again for the new
            slice. Under yolo the single combined question is asked, and yolo
            never picks the fix. Without the tier, the question gains the
            one-line note that a fix exists when AI is set up.
            """
            nonlocal gcodes, sliced, clearance_ack_pending
            from . import ai_fix as _ai_fix
            heavy = _ai_fix.intake_fix_offered()
            fix_ready = heavy and _fix_budget_left() > 0
            fix_attempts = 0
            while True:
                unverified = (unverified_result or {}).get('unverified') or []
                lines = []
                if cov_failure is not None:
                    worst = min(cov_failure['regions'], key=lambda r: r['coverage'])
                    lines.append(('Re-sliced' if resliced else 'Sliced') + ' with supports, but the support '
                                 'toolpaths do not cover every flagged region (worst region: '
                                 f"{worst['coverage'] * 100:.0f}% of its 3 mm cells covered).")
                if unverified:
                    reasons = '; '.join(str(entry.get('reason') or entry) if isinstance(entry, dict)
                                        else str(entry) for entry in unverified)
                    lines.append(f"Support coverage couldn't be verified: {reasons}.")
                if low_clearance:
                    lines.append('This print has a low-clearance underside (under 1.0 mm) that slicer '
                                 'supports cannot fix.')
                lines.append('Nothing is uploaded yet. Print anyway to accept the risk, or cancel and '
                             'repair the geometry (extend or sink).')
                if heavy and not fix_ready and _fix_budget_left() <= 0:
                    lines.append(_no_fix_left_note())
                choices = [PRINT_ANYWAY_LABEL]
                if fix_ready:
                    choices.append(_ai_fix.FIX_LABEL)
                choices.append(print_next.CANCEL_LABEL)
                receipt.update(status='SLICED_NOT_STARTED', started=False)
                if cov_failure is not None:
                    receipt['support_coverage'] = cov_failure
                elif unverified_result is not None:
                    receipt['support_coverage'] = unverified_result
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                from . import yolo as _yolo_gate
                if fix_ready and not _yolo_gate.ACTIVE.get():
                    # The fix offer stands alone and unmarked (#985): anyone
                    # may ask for a fix. Only the waiver after "Decide without
                    # a fix" is an approval question, exactly as before.
                    offer = [_ai_fix.FIX_LABEL, _ai_fix.DECIDE_LABEL, print_next.CANCEL_LABEL]
                    first = await _ask(adapter, source, key,
                                       '\n'.join(lines + ['Anyone may ask for an AI fix first; '
                                                          'printing anyway still needs a print '
                                                          'approver.']),
                                       offer, timeout)
                    picked_fix = offer[first] == _ai_fix.FIX_LABEL
                    if offer[first] == print_next.CANCEL_LABEL:
                        pick = 1  # read as Cancel below
                    elif not picked_fix:
                        pick = await _ask(adapter, source, key, '\n'.join(lines),
                                          [PRINT_ANYWAY_LABEL, print_next.CANCEL_LABEL],
                                          timeout, approval=True)
                else:
                    # No fix on offer (or a yolo run, whose waiver is the
                    # operator's own and which never picks the fix): the one
                    # approval-marked question, as before.
                    pick = await _ask(adapter, source, key, '\n'.join(lines),
                                      choices, timeout, approval=True)
                    picked_fix = fix_ready and choices[pick] == _ai_fix.FIX_LABEL
                if picked_fix:
                    fix_attempts += 1
                    problem = 'Post-slice gate failure. ' + ' '.join(
                        line for line in lines
                        if line not in (_ai_fix.UNCONFIGURED_NOTE,)
                        and not line.startswith('Nothing is uploaded yet'))
                    if await _ai_fix_attempt(problem) is None:
                        if fix_attempts >= _ai_fix.MAX_FIX_ATTEMPTS or _fix_budget_left() <= 0:
                            fix_ready = False
                        continue  # declined or unusable proposal: ask the gate question again
                    # Re-slice with the fix and grade the gates again on the
                    # new slice; nothing about the fix waives any of them.
                    try:
                        out = await _with_stop(
                            adapter, source, key, metadata,
                            'Re-slicing with the AI fix. Press Stop to cancel.',
                            _heavy(mj.slice_job, str(jp), recipe_sha256=recipe_sha, **(
                                {'copies': chosen_quantity}
                                if chosen_quantity and chosen_quantity > 1 else {})),
                            allow_stop=True)
                    except Exception as reslice_error:  # noqa: BLE001 - reported, then cancelled
                        from . import slice_failure as _slice_failure
                        logger.exception('AI fix re-slice failed')
                        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                           content=_slice_failure.post(reslice_error))
                        return False
                    gcodes = [str(g) for g in (out if isinstance(out, list) else [out])]
                    sliced = json.loads(jp.read_text())
                    receipt.update(status='SLICED', sliced=True, gcodes=gcodes,
                                   gcode_sha256={g: delivery.sha256(g) for g in gcodes})
                    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                    cov_result = _coverage_result(gcodes)
                    if cov_result is not None and cov_result.get('unverified'):
                        _record_coverage_unverified(cov_result)
                    cov_failure = (cov_result if cov_result is not None and cov_result['checked']
                                   and not cov_result['ok'] else None)
                    unverified_result = (cov_result if cov_result is not None
                                         and cov_result.get('unverified') else None)
                    report = sliced.get('support_clearance') or clearance
                    low_clearance = False
                    if any(f.get('code') == support_clearance.CODE_CLEARANCE
                           for f in (report or {}).get('findings') or []):
                        ack = sliced.get('clearance_acknowledgement') or {}
                        low_clearance = (not ack.get('acknowledged')
                                         or ack.get('via', 'print_anyway')
                                         not in ('print_anyway', 'geometry_repair'))
                    if cov_failure is None and not low_clearance and unverified_result is None:
                        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                           content='The fixed slice passes the support checks. '
                                                   'Nothing was waived.')
                        return 'fixed'
                    resliced = True
                    if fix_attempts >= _ai_fix.MAX_FIX_ATTEMPTS or _fix_budget_left() <= 0:
                        fix_ready = False
                    continue
                if pick != 0:
                    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                       content='Cancelled. Nothing uploaded or started. The slice stays on disk.')
                    return False
                # Bound to the exact G-code bytes the operator saw the warning for.
                from . import yolo as _yolo
                receipt['support_override'] = {
                    # A yolo auto-answer is recorded apart from a person's Print anyway.
                    'via': 'yolo' if _yolo.ACTIVE.get() else 'print_anyway',
                    'support_coverage': cov_failure is not None or bool(unverified),
                    'low_clearance': bool(low_clearance),
                    'gcode_sha256': dict(receipt.get('gcode_sha256') or {}),
                }
                receipt.update(status='SLICED')
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                if low_clearance:
                    # Recorded below, once the final artifact is installed,
                    # so the start guard sees an acknowledgement bound to it.
                    # A waiver of a low clearance an AI fix did not repair is
                    # still a waiver: it is recorded as print_anyway.
                    clearance_ack_pending = 'print_anyway'
                return 'waived'

        if supports_choice is True:
            cov_result = _coverage_result(gcodes)
            if cov_result is not None and cov_result.get('unverified'):
                _record_coverage_unverified(cov_result)
            cov_failure = (cov_result if cov_result is not None and cov_result['checked']
                           and not cov_result['ok'] else None)
            # An unverified result asks the same question (#968): the job
            # no longer proceeds on a check that could not run.
            unverified_result = (cov_result if cov_result is not None and cov_result.get('unverified')
                                 else None)
            report = sliced.get('support_clearance') or clearance
            # Only a low-clearance finding is unwaivable by a with-supports
            # choice (#922 gap 2); other findings (modeled supports) keep
            # their Phase 1 behavior. Print anyway is the explicit waiver.
            low_clearance = False
            if any(f.get('code') == support_clearance.CODE_CLEARANCE
                   for f in (report or {}).get('findings') or []):
                ack = sliced.get('clearance_acknowledgement') or {}
                low_clearance = (not ack.get('acknowledged')
                                 or ack.get('via', 'print_anyway') not in ('print_anyway', 'geometry_repair'))
            if cov_failure is not None or low_clearance or unverified_result is not None:
                if not await _support_override(cov_failure, low_clearance,
                                               unverified_result=unverified_result):
                    return {'action': 'skip',
                            'reason': ('support coverage verification failed' if cov_failure is not None
                                       else 'support coverage unverified' if unverified_result is not None
                                       else 'low clearance not waived by supports choice')}
                # The clearance acknowledgement (if any) was set by
                # _support_override on an approver's waiver only; an AI fix
                # that made every gate pass records none (#978 review).
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
        # #722: source-profile compatibility warnings ride the slice provenance; they are
        # shown before the operator confirms the start. They change nothing: the values they name
        # were never used, because the slice runs on the target printer's own profile (#868).
        profile_warnings = provenance.get('source_profile_warnings') or []
        if profile_warnings:
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content='Heads up on the source profile:\n'
                                       + '\n'.join(f'- {w}' for w in profile_warnings))
        # #1279: a job that only renumbers filaments slices its source project
        # remapped; when the remapper refused the project and the rebuilt
        # export was equivalent, the refusal is named here before the start.
        from .slice_route import route_notice
        notice = route_notice(provenance)
        if notice:
            await adapter.send(chat_id=source.chat_id, metadata=metadata, content=notice)
        # Orca said supports are needed and the slice ran without them: ask before anything else
        # (bed-clear, upload, start). Only this finding asks; other warnings are information.
        finding = slice_warnings.support_finding(sliced)
        if finding is not None:
            pick = await _ask(adapter, source, key,
                              slice_warnings.question(finding, sliced.get('downloaded_file')),
                              [RESLICE_SUPPORTS_LABEL, PRINT_NO_SUPPORTS_LABEL, print_next.CANCEL_LABEL], timeout,
                              approval=True)  # only print approvers may waive or answer a safety finding
            receipt['support_decision'] = {'finding': finding, 'choice': pick}
            if pick == 2:
                # Cancelling the START, not the slice (#922 gap 6): this slice
                # is already referenced by JOB.json and the receipt above, so
                # early.discard() must not delete its directory. The files
                # stay on disk under SLICED_NOT_STARTED.
                receipt['status'] = 'SLICED_NOT_STARTED'
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content='Cancelled. Nothing uploaded or started. '
                                           'The slice stays on disk.')
                return {'action': 'skip', 'reason': 'operator cancelled on the support question'}
            mj.record_support_decision(jp.parent, pick == 0)
            if pick == 1:
                # Explicit no-supports override, bound to the warning and the
                # artifact hash (#922 gap 3).
                slice_warnings.record_unsupported_cantilever_override(
                    jp.parent, finding, source_file=sliced.get('downloaded_file'))
            if pick == 0:
                # The reslice replaces the artifact: a clearance
                # acknowledgement pending from the Ready menu was given for
                # the artifact sliced before it, so it is invalidated here
                # and never recorded against the new one (#1151). The new
                # artifact's low-clearance finding is asked again below.
                clearance_ack_pending = False
                latest = json.loads(jp.read_text())
                recipe_sha = binding_sha256(slice_binding(latest, decisions['source_sha256']))
                receipt['slice_approval']['recipe_sha256'] = recipe_sha
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                copies_kw = {'copies': chosen_quantity} if chosen_quantity and chosen_quantity > 1 else {}
                out = await _with_stop(adapter, source, key, metadata,
                                       _voiced('Re-slicing with supports. Press Stop to cancel.', 'supports', source, iid),
                                       _heavy(mj.slice_job, str(jp), recipe_sha256=recipe_sha, **copies_kw),
                                       allow_stop=True)  # still entirely pre-upload, so Stop is safe
                gcodes = [str(g) for g in (out if isinstance(out, list) else [out])]
                sliced = json.loads(jp.read_text())
                receipt.update(status='SLICED', gcodes=gcodes,
                               gcode_sha256={g: delivery.sha256(g) for g in gcodes})
                cov_result = _coverage_result(gcodes)
                if cov_result is not None and cov_result.get('unverified'):
                    # Same receipt pattern as the first slice: the dict
                    # update persists with this block's receipt writes.
                    receipt.update(support_coverage=cov_result)
                    logger.warning('support coverage unverified for intake %s: %s',
                                   iid, cov_result.get('unverified'))
                cov_failure = (cov_result if cov_result is not None and cov_result['checked']
                               and not cov_result['ok'] else None)
                unverified_result = (cov_result if cov_result is not None and cov_result.get('unverified')
                                     else None)
                # The resliced artifact owes its own low-clearance waiver:
                # the acknowledgement for the previous artifact was
                # invalidated above, and a "with supports" slice does not
                # waive a sub-1.0 mm gap (#922 gap 2, #1151).
                report = sliced.get('support_clearance') or clearance
                low_clearance = False
                if any(f.get('code') == support_clearance.CODE_CLEARANCE
                       for f in (report or {}).get('findings') or []):
                    ack = sliced.get('clearance_acknowledgement') or {}
                    low_clearance = (not ack.get('acknowledged')
                                     or ack.get('via', 'print_anyway')
                                     not in ('print_anyway', 'geometry_repair'))
                if cov_failure is not None or low_clearance or unverified_result is not None:
                    if not await _support_override(cov_failure, low_clearance, resliced=True,
                                                   unverified_result=unverified_result):
                        return {'action': 'skip',
                                'reason': ('support coverage verification failed' if cov_failure is not None
                                           else 'support coverage unverified' if unverified_result is not None
                                           else 'low clearance not waived for the resliced artifact')}
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        if clearance_ack_pending:
            # The selected variant's artifact is installed now: bind the
            # clearance acknowledgement to its recipe and G-code hashes.
            # Recording it when the operator answered captured whichever
            # variant the background slices had on disk then, and the start
            # guard compares the acknowledgement against the final artifact.
            from . import support_clearance
            support_clearance.record_clearance_acknowledgement(
                folder, source_file=path,
                via=clearance_ack_pending if isinstance(clearance_ack_pending, str)
                else 'print_anyway')
            sliced = json.loads(jp.read_text())
        # P1-20: a plate a guard refused during slicing is never dropped
        # silently. Name every refused plate before the start question; the
        # operator's confirmed start below records the acceptance print-now
        # requires for a partial job. Read after the support question, so a
        # re-slice with supports discloses the final slice's refusals.
        refused_plates = sliced.get('refused_plates') or []
        if refused_plates:
            named = '\n'.join(f"- Plate {row.get('plate_id')} (slot {row.get('slot')}): {row.get('reason')}"
                              for row in refused_plates)
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content='Heads up: part of this model will NOT print. These plates were '
                                       f'refused during slicing:\n{named}\n'
                                       'Starting prints only the plates that sliced cleanly.')
        has_dur_q = has_duration_question(getattr(event, 'text', ''))
        info_line = slice_warnings.compact_info(sliced)
        if info_line:
            await adapter.send(chat_id=source.chat_id, metadata=metadata, content=info_line)
        breakdown = print_summary.format_estimate_breakdown(sliced)
        # #720: the thermal facts recorded for the printer this job was sliced for ride with
        # the estimate everywhere it is shown (duration answer, split notice, start question),
        # but only when a material needs an enclosure; otherwise they are noise.
        try:
            from ..knowledge.thermal import thermal_lines_for_job
            thermal = thermal_lines_for_job(sliced.get('printer_model'), print_summary.job_materials(sliced),
                                            printer_id=sliced.get('printer_id'))
        except Exception:
            thermal = []
        if thermal:
            breakdown = breakdown + '\n' + '\n'.join(thermal)
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
            if job_adapter.tool_based:  # tools, not a slot map (#346, #1198)
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
        accepted = _preaccepted_shortage(choice, shown_shortages, jp)
        if accepted:
            target['accept_material'] = accepted
        from . import yolo as _yolo
        if _yolo.ACTIVE.get():
            target['yolo'] = True
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
            if delivery.only_printer(enabled):
                go = await _ask(adapter, source, key, question, [print_next.START_LABEL, print_next.CANCEL_LABEL], timeout,
                                approval=True)
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
        if refused_plates:
            # The start the operator just confirmed followed the refused-plate
            # disclosure above: that confirmation is the explicit acceptance
            # of the partial job print-now requires (P1-20).
            accepted = json.loads(jp.read_text())
            accepted['partial_job_accepted'] = mj.refused_plates_digest(accepted)
            mj.write_job(folder, accepted)
        # One click recorded: bed clear, bound to the exact JOB.json and G-code. print-now still runs every gate
        # (verify-gcode, live readiness, single-use approval, duplicate-start claim).
        receipt['bed_clear_approval'] = {'job_json': str(jp), 'gcode': str(first), 'gcode_sha256': target['gcode_sha256'],
                                         'granted_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'via': 'button'}
        receipt['status'] = 'START_REQUESTED'
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        # JOB.json keeps the job's FIRST print number, and this start mints its
        # own at print-now. A job printed before is a reprint: name the newest
        # earlier print from the launch registry, never as this start's number (#1017).
        previous = None
        with contextlib.suppress(Exception):
            from ..runtime import print_control
            previous = print_control.latest_number_for_job(jp)
        # Just the status: the estimate and slot mapping were on the confirmation the operator just answered.
        # The targets the uploaded file heats to, so a wrong temperature is caught before warmup.
        targets = ''
        with contextlib.suppress(Exception):
            from ..runtime.print_control import start_targets_text
            targets = start_targets_text(Path(first).read_text(errors='replace'))
        prep_msg = ' · '.join(['Preparing to start']
                              + ([f'A reprint of print #{previous}; it gets its own new print number'] if previous else [])
                              + ([targets] if targets else []))
        mj._timing(jp, 'intake_start_tapped')
        receipt['retry_target'] = target
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        start_context = {'target': target, 'receipt': receipt}
        uploading = ((prep_msg + "\n") if not prep_sent else "") + "Uploading to the printer. Press Stop to request cancellation."
        try:
            for attempt in range(COLOR_CONFIRM_ROUNDS):
                # Hold the Retry refusal text back only while a question will follow it (#795).
                start_context['color_prompt'] = attempt < COLOR_CONFIRM_ROUNDS - 1
                ok, tail = await _with_stop(adapter, source, key, metadata, uploading,
                                            _with_start_progress(adapter, source, metadata, jp,
                                                                 print_next.start(target)),
                                            allow_stop=True, refusal_context=start_context)
                confirms = [] if ok or not start_context['color_prompt'] else color_confirm(tail)
                label_confirms = [] if ok or not start_context['color_prompt'] else label_confirm(tail)
                from . import material_accept
                material_warnings = [] if ok or not start_context['color_prompt'] else material_accept.pending(tail)
                if material_warnings:
                    # A material warning never blocks on its own (#1336): Reslice for the
                    # loaded material (the default), Print as sliced, or Cancel. A filament
                    # shortage alone has nothing to reslice: Print anyway or Cancel.
                    material_choices = material_accept.choices(material_warnings)
                    pick = await _ask(adapter, source, key, material_accept.question(material_warnings),
                                      material_choices, timeout, approval=True)
                    receipt['material_warnings'] = {'warnings': material_warnings, 'choice': pick}
                    chosen = (material_choices[pick] if isinstance(pick, int) and not isinstance(pick, bool)
                              and 0 <= pick < len(material_choices) else None)
                    if chosen != material_accept.print_label(material_warnings):
                        receipt['status'] = 'SLICED_NOT_STARTED'
                        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                           content=(material_accept.reslice_message(material_warnings)
                                                    if pick == 0 and chosen != material_accept.CANCEL_LABEL
                                                    else 'Cancelled. Nothing uploaded or started.'))
                        return {'action': 'skip', 'reason': 'operator did not print as sliced on a material warning'}
                    target['accept_material'] = sorted(set(target.get('accept_material') or ())
                                                       | {w['key'] for w in material_warnings})
                    uploading = "Uploading to the printer. Press Stop to request cancellation."
                    continue
                if (not ok and start_context['color_prompt'] and SPOOL_CHANGED in str(tail)
                        and not target.get('accept_spool_change')):
                    # A slot spool swapped since slicing (same material family): ask, never
                    # a Retry-only refusal; yes passes --accept-spool-change (#1336).
                    pick = await _ask(adapter, source, key,
                                      'A slot holds another spool than when this was sliced (the same material '
                                      'family is still required). Print with the spool that is loaded now?',
                                      [USE_LOADED_SPOOL_LABEL, print_next.CANCEL_LABEL], timeout, approval=True)
                    receipt['spool_change'] = {'choice': pick}
                    if pick != 0:
                        receipt['status'] = 'SLICED_NOT_STARTED'
                        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                           content='Cancelled. Nothing uploaded or started.')
                        return {'action': 'skip', 'reason': 'operator cancelled on a swapped spool'}
                    target['accept_spool_change'] = True
                    uploading = "Uploading to the printer. Press Stop to request cancellation."
                    continue
                if not confirms and not label_confirms:
                    await _post_yolo_waivers(adapter, source, metadata, tail, receipt, receipt_path)
                    break
                if label_confirms and not confirms:
                    # A slot material-label difference gets the same ask-once
                    # treatment as a color difference (#839): the native
                    # label can be blank or stale, so the operator decides.
                    pick = await _ask(adapter, source, key, label_confirm_question(label_confirms),
                                      [PRINT_ANYWAY_LABEL, print_next.CANCEL_LABEL], timeout,
                                      approval=True)
                    receipt['label_mismatch'] = {'slots': label_confirms, 'choice': pick}
                    if pick == 0:
                        accept_label_mismatch(jp, target, label_confirms)
                    else:
                        receipt['status'] = 'SLICED_NOT_STARTED'
                        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                           content='Cancelled. Nothing uploaded or started.')
                        return {'action': 'skip', 'reason': 'operator cancelled on a slot material label difference'}
                    uploading = "Uploading to the printer. Press Stop to request cancellation."
                    continue
                # A slot color difference never blocks on its own (#795): the operator decides,
                # once for every mismatched slot; a later round only asks about what is new.
                pick = await _ask(adapter, source, key, color_confirm_question(confirms),
                                  [PRINT_ANYWAY_LABEL, SET_SCREEN_LABEL, print_next.CANCEL_LABEL], timeout,
                                  approval=True)
                receipt['color_mismatch'] = {'slots': confirms, 'choice': pick}
                if pick == 0:
                    accept_color_mismatch(jp, target, confirms)
                elif pick == 1:
                    unpushed = []
                    for confirm in confirms:
                        try:
                            pushed = await asyncio.to_thread(_external.load('spool_sync').sync_slot_screen,
                                                             confirm['slot'], printer_id=printer_id)
                        except Exception as exc:
                            logger.warning('screen color update failed slot=%s: %s', confirm['slot'], exc)
                            pushed = 'failed'
                        receipt['color_mismatch'].setdefault('screen', {})[str(confirm['slot'])] = pushed
                        if pushed != 'pushed':
                            unpushed.append(confirm)
                    if unpushed:
                        accept_color_mismatch(jp, target, unpushed)  # screen update unavailable: print as chosen
                        # The print still goes ahead; the channel is told which slots stayed as they were (#798).
                        with contextlib.suppress(Exception):
                            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                               content=screen_unpushed_note(unpushed))
                else:
                    receipt['status'] = 'SLICED_NOT_STARTED'
                    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                       content='Cancelled. Nothing uploaded or started.')
                    return {'action': 'skip', 'reason': 'operator cancelled on a slot color difference'}
                uploading = "Uploading to the printer. Press Stop to request cancellation."
        finally:
            with contextlib.suppress(Exception):
                clear_color_acceptance(jp)
            with contextlib.suppress(Exception):
                clear_label_acceptance(jp)
            # A Print as sliced answer is this start's only: a later Retry asks again (#1336).
            target.pop('accept_material', None)
            target.pop('accept_spool_change', None)
        mj._timing(jp, 'intake_start_returned', ok=bool(ok))
        started_as = classify_start(ok, tail)
        unknown, pending = started_as.unknown, started_as.pending
        receipt.update(status=started_as.status, started=started_as.started, start_outcome=started_as.outcome)
        if not ok and not pending:
            raw = _raw_preupload_refusal(tail)
            logger.warning('print start refused detail=%s', logs.redact(raw or str(tail)))
            receipt.update(start_refusal_detail=str(tail), failure=_start_report(tail).as_dict(),
                           uploaded=(True if _printer_rejected(raw) else False) if raw else None,
                           start_prompt_message_id=start_context.get('start_prompt_message_id', ''))
        if unknown and 'failure' not in receipt:
            # A start the printer never confirmed (a failed or unconfirmed upload included) is
            # classified too, so the receipt says which stage was in doubt (#817).
            receipt['failure'] = _start_report(tail).as_dict()
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        warming = ok and print_next.still_preparing(tail)
        # The remote file this start claimed, when warmup is still running. Only a
        # warming start with a known file can be edited by the listener later, so
        # only that start is recorded for the warmup edit (#374).
        remote = print_next.started_file(tail) if warming else None
        if remote is not None:
            # The warmup edit is made by EDITING this post, and the only edit
            # path is Discord (the listener edits via Discord REST), so other
            # transports are not recorded for it (P2-144).
            platform = getattr(source, 'platform', None)
            platform_name = str(getattr(platform, 'value', None)
                                or getattr(adapter, 'name', None) or 'discord')
            if platform_name.casefold() != 'discord':
                remote = None
        if unknown:
            message = start_unknown_message(tail)
        elif ok or pending:
            # No roast on the start post: the one roast per print rides the
            # completion post (#874), which the listener adds there.
            message = print_summary.format_started(exact_job, exact_plate) + later
        else:
            message = print_summary.scrub(_launch_refusal_text(tail))
            if _restart_helps(tail):
                message = await _offer_force_restart(adapter, source, key, message, timeout,
                                                     printer_id=printer_id)
            elif start_context.get('prompt_handled'):
                return {'action': 'skip', 'reason': 'start refused; prompt shows the reason and Retry'}
        sent = await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
        message_id = getattr(sent, 'message_id', None)
        if remote and message_id:
            try:
                print_next.record_start_message(
                    remote, message_id, chat_id=str(source.chat_id),
                    thread_id=str(source.thread_id) if getattr(source, 'thread_id', None) else None)
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
        elif isinstance(exc, ConnectCancelled):
            receipt.update(status='CANCELLED', sliced=False, uploaded=False, started=False,
                           error='connect cancelled by operator')
            message = 'Cancelled. Nothing was downloaded.'
            reason = 'cancelled by operator at the connect prompt'
        else:
            receipt.update(status='CANCELLED', sliced=False, uploaded=False, started=False)
            message = 'Cancelled. Nothing sliced, uploaded or started.'
            reason = 'cancelled by operator'
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
        return {'action': 'skip', 'reason': reason}
    except IntakeExpired:
        logger.warning('intake question expired')
        if jp is not None:
            # A failed intake discards its background slice (P2-23), as the
            # cancelled path above already does: the slice's output must not
            # outlive the intake that produced it. Resume re-slices.
            with contextlib.suppress(Exception):
                await runner_for(jp).discard()
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
        if jp is not None:
            # A failed intake discards its background slice (P2-23), as the
            # cancelled path above already does: the slice's output must not
            # outlive the intake that produced it.
            with contextlib.suppress(Exception):
                await runner_for(jp).discard()
        from .site_accounts import chat_safe, short_cause
        from . import model_job as _mj
        if isinstance(exc, SiteLoginNeeded):
            cause = short_cause(exc)  # the walkthrough stays in chat, not the receipt
        elif isinstance(exc, _mj.DownloadUnsupported):
            cause = exc.cause
        else:
            cause = chat_safe(exc)
        receipt.update(status='FAILED', error=cause, failure=failure_report.describe(exc, 'intake').as_dict())
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        from .intake_resources import IntakeLimitExceeded
        if isinstance(exc, IntakeLimitExceeded):
            # A curated, user-phrased refusal (its text names the limit and
            # the remedy): keep its reason, lead plainly, drop its prefix.
            reason = str(exc).removeprefix('Model intake refused: ')
            content = (f'That model could not be prepared. {reason}')
        elif isinstance(exc, _mj.DownloadUnsupported):
            content = str(exc)  # plain refusal, no walkthrough, no paths (#712)
        elif isinstance(exc, SiteLoginNeeded):
            # Curated and user-phrased by site_accounts: the text names the
            # token file and how to fill it, so keep it — the generic wording
            # would hide the remedy. Chat shows the path home-relative (#712).
            content = (f'That model could not be downloaded: {chat_safe(exc)} '
                       'Nothing was sliced, uploaded or started.')
        elif isinstance(exc, mj.DirectLinkError):
            # Also curated and user-phrased (#607): a direct file link that
            # returned a web page, pointed at a non-public address, or could
            # not be reached. Every curated case is on the diagnostics
            # allowlist (#683), so a recognised failure posts the known-cause
            # wording (P2-270); the branch's older text remains only for a
            # direct-link error the allowlist does not recognise.
            content = (diagnostics.known_post(
                'That file could not be fetched.',
                not_happened='Nothing was downloaded or sliced.',
                subsystem='intake', exc=exc)
                or f'That file could not be fetched. {exc}')
        elif isinstance(exc, ValueError) and str(exc).startswith('requires one to four distinct configured'):
            # Also curated and user-phrased: a multi-slot/toolhead printer
            # with nothing configured. The text names the exact setup gap,
            # so keep it — the generic wording would hide the remedy.
            content = (f'That model could not be prepared: this printer {exc}. '
                       'Nothing was sliced, uploaded or started.')
        else:
            # #590 / #817: a recognized cause (a missing site token, a damaged 3MF, ...) is named
            # with its stage and remedy; only a failure nothing recognizes keeps the generic lead.
            content = failure_report.post(
                exc, 'intake', subsystem='intake',
                headline='Preparing that model stopped before it was ready.',
                not_happened='Nothing was sliced, uploaded or started.',
                transient='Send the link or file again; if it stops again, run `/print-handy bundle` and share the bundle so it can be looked into.')
        await adapter.send(chat_id=source.chat_id, content=content, metadata=metadata)
        return {'action': 'skip', 'reason': 'source-first intake failed closed'}
    finally:
        if jp is not None:
            drop_runner(jp)
        _LIVE_KEY.reset(live_token)
        logs.unbind(log_token)


# Code moved out of this module (#1323) still sees assignments made here
# (mock.patch, monkeypatch) for one release; see print_handy/_relocated.py.
from .. import _relocated  # noqa: E402
from ..intake import fit_flow as _fit_flow, questions as _questions  # noqa: E402
from ..intake import support_flow as _support_flow, worker as _worker  # noqa: E402
_relocated.keep_old_path(__name__, _worker, _questions, _support_flow, _fit_flow)
