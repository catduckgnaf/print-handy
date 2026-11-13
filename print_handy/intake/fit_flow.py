"""Fitting a model on the bed: the fit status message, the fit, scale and rotation choices, the
auto-rearrange notice, the split plan and the fit failure texts.

Moved verbatim from ``print_handy.core.prepared_intake`` (#1323). The old names are still
importable from there for one release.
"""
import asyncio, contextlib, json, re, time
from pathlib import Path

from .. import settings
from ..core import _external
from .questions import _typo_words


BED = settings.current()['printer']['bed_mm']


# Fit-check progress: one status line, refreshed while a check runs. When the
# chat adapter can edit messages (Discord, Matrix, Telegram) the same message is
# updated; otherwise a new line is posted only when the phase changes or, for a
# long phase, at most once per FIT_FALLBACK_HEARTBEAT_S.
FIT_HEARTBEAT_S = 15.0
FIT_FALLBACK_HEARTBEAT_S = 60.0
# A fit check that settles this fast posts no status at all: the prefetched probe (#734) is
# usually a second or two behind a quick color answer, and a status that appears and is
# replaced at once is noise (2026-11-03).
FIT_QUIET_S = 3.0


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

    async def run(self, phase, work, first_text=None, voice=None):
        """Await ``work`` while the status names ``phase`` and the time spent so far.

        ``work`` may be a task started earlier (:func:`_prefetch`); when it has already
        finished, or finishes within FIT_QUIET_S while no status is showing yet, no status
        line is posted at all. ``voice`` (see :func:`_voice`) adds one optional quip
        after this phase's factual line, chosen once and only if the line is shown.
        """
        task = asyncio.ensure_future(work)
        line = None

        def voiced(text):
            nonlocal line
            if voice is None:
                return text
            if line is None:
                try:
                    line = voice() or ''
                except Exception:
                    line = ''
            return f'{text} {line}' if line else text
        if task.done():
            return task.result()
        try:
            if self.last_text is None and FIT_QUIET_S > 0:
                # Nothing shown yet: give a nearly finished check a moment before announcing it.
                # Inside the try, so a cancelled intake still cancels the worker below.
                await asyncio.wait({task}, timeout=FIT_QUIET_S)
                if task.done():
                    return task.result()
            await self.show(first_text or voiced(f'Checking the fit: {phase} ({self.elapsed()}).'))
            while True:
                done, _ = await asyncio.wait({task}, timeout=FIT_HEARTBEAT_S)
                if done:
                    return task.result()
                await self.show(voiced(f'Still checking the fit: {phase} ({self.elapsed()}).'), heartbeat=True)
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


def write_split_plan(job_path, job, manifest, gcodes, plan_id, plans_dir=None):
    """Record a split slice as a multipart plan that /print-next can resolve exactly.

    Each plate gets one required part bound to its archived JOB.json and absolute
    G-code path. The plan id is also stored in JOB.json so record_start can put
    it in the print context and completions advance the plan.
    """
    print_records = _external.load('print_records')
    from ..core import projects
    project = job.get('project') if isinstance(job.get('project'), dict) else None
    project_id = (project or {}).get('project_id') or projects.project_id_for_intake(
        str(job.get('intake_id') or plan_id))
    source_kind = (project or {}).get('source_kind') or job.get('project_source_kind') or (
        'multi_plate_3mf' if job.get('downloaded_file', '').lower().endswith('.3mf') else 'split_print')
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
                      'gcode_path': str(Path(g).resolve()),
                      **projects.plate_info(n - 1, row.get('part_keys'))})
        # One part per plate, so the part and its plate share the plate's G-code time; unknown stays absent.
        if est.get('seconds') is not None:
            parts[-1]['estimated_part_seconds'] = parts[-1]['plate_estimate_seconds'] = est['seconds']
        if est.get('filament_g') is not None:
            parts[-1]['estimated_filament_g'] = est['filament_g']
    merge = job.get('plate_merge') or {}
    reason = merge.get('reason') or ('the chosen slots were sliced as separate single-color plates'
                                     if not merge else 'the merge planner could not place them on one bed')
    plan = {'plan_id': plan_id, 'title': job.get('title'), 'split_reason': reason, 'parts': parts,
            'project_id': project_id, 'source_kind': source_kind, 'project_run_id': 'original'}
    target = (plans_dir or print_records.PLANS_DIR) / f'{plan_id}.json'
    print_records._atomic_write(target, json.dumps(plan, indent=2) + '\n')
    latest = json.loads(Path(job_path).read_text())
    latest['multipart_plan'] = dict(plan)
    latest['project_id'] = project_id
    latest['project_source_kind'] = source_kind
    latest.setdefault('project', project or {
        'schema': projects.SCHEMA, 'project_id': project_id,
        'title': job.get('title') or 'Multi-plate project',
        'source_kind': source_kind, 'components': [],
    })
    from ..core import model_job as mj
    mj.write_job(Path(job_path).parent, latest)  # atomic; JOB.json is read concurrently
    return plan


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
        from ..core import model_job as mj
        from .. import printers
        adapter = mj._job_adapter(job if isinstance(job, dict) else None)
        return adapter.display_name if adapter.key == printers.current().key else adapter.label
    except Exception:
        from .. import printers
        return printers.current().display_name


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
                        if split_plan.get('reorientation_by_object') else {}),
                     **({'turn_reason_by_object': split_plan['turn_reason_by_object']}
                        if split_plan.get('turn_reason_by_object') else {})})
        designed = split_plan.get('designer_pose')
        if designed:
            # The same split with nothing tipped onto another face (#1380):
            # smaller, but every part prints the way its designer laid it out.
            rows.append({'kind': 'split', 'designer_pose': True,
                         'label': f"{_pct(designed['scale'])} · split {len(designed['plates'])} plates as designed",
                         'plates': designed['plates'], 'scale': float(designed['scale']),
                         **({'yaw_by_object': designed['yaw_by_object']}
                            if designed.get('yaw_by_object') else {})})
    elif options.get('full_size_split') and len(rows) < 2:
        rows.append({'kind': 'split', 'label': 'Split plates at 100%', 'scale': 1.0})
    full_size_available = any(row['kind'] in ('split', 'rearrange') and row['scale'] >= 0.995 for row in rows)
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
    if any(row.get('designer_pose') for row in rows):
        words.append('as designed')
    if any((row['kind'] in ('rearrange', 'split') and row['scale'] >= 0.995)
           or (row['kind'] in ('rotate', 'painted') and row['scale'] >= 0.995) for row in rows):
        words.append('100%')
    if any(row['kind'] == 'scale' or (row['kind'] in ('rotate', 'painted') and row['scale'] < 0.995)
           for row in rows):
        words.append('shrink')
    words.append('cancel')
    return FIT_HINT_PREFIX + ' / '.join(words) + '.'


def _fit_choice_reply(text, rows):
    """Resolve typed fit intent only when it names one available action."""
    value = _typo_words(str(text or '').strip().casefold())
    if value in {'rearrange', 'rearrange 1 plate', 'rearrange plate', 'pack'}:
        matches = [i for i, row in enumerate(rows) if row['kind'] == 'rearrange']
    elif value in {'as designed', 'split as designed', 'keep orientation', 'keep the orientation'}:
        matches = [i for i, row in enumerate(rows) if row.get('designer_pose')]
    elif value in {'split', 'separate', 'split plates'}:
        matches = [i for i, row in enumerate(rows) if (row['kind'] == 'split' and not row.get('designer_pose'))
                   or (row['kind'] == 'painted' and 'split' in row['label'])]
    elif value in {'100%', '100', 'full', 'full size'} or 'full size' in value:
        matches = [i for i, row in enumerate(rows) if (row['kind'] in ('rearrange', 'split') and row['scale'] >= 1.0)
                   or (row['kind'] in ('painted', 'rotate') and row['scale'] >= 1.0)]
    elif value in {'cancel', 'stop', "don't print"}:
        matches = [i for i, row in enumerate(rows) if row['kind'] == 'cancel']
    elif value in {'shrink', 'smaller'}:
        matches = [i for i, row in enumerate(rows) if row['kind'] == 'scale' or row.get('designer_pose')
                   or (row['kind'] in ('painted', 'rotate') and row['scale'] < 0.995)]
        if len(matches) > 1:  # several smaller sizes are offered: "shrink" means the largest
            matches = [max(matches, key=lambda i: rows[i]['scale'])]
    elif re.fullmatch(r'\d+(?:\.\d+)?\s*%', value):
        percent = float(value.rstrip('%').strip())
        matches = [i for i, row in enumerate(rows)
                   if (row['kind'] in ('scale', 'rotate', 'painted') or row.get('designer_pose'))
                   and abs(row['scale'] * 100 - percent) < 0.06]
    elif re.fullmatch(r'\d+(?:\.\d+)?', value):
        percent = float(value)
        matches = [i for i, row in enumerate(rows)
                   if (row['kind'] in ('scale', 'rotate', 'painted') or row.get('designer_pose'))
                   and abs(row['scale'] * 100 - percent) < 0.06]
    else:
        return None
    if len(matches) != 1:
        raise ValueError('that fit choice is not available; choose a listed button')
    return matches[0]


SCALE_STEP_PERCENTS = (1, 5, 10)
SCALE_LABEL = 'Scale the model'


def _scale_direction_reply(text):
    """Typed answer to the scale direction question: 0 bigger, 1 smaller, 2 back, else None."""
    value = _typo_words(str(text or '').strip().casefold())
    if value in {'cancel', 'back', 'never mind', 'nevermind'}:
        return 2
    if any(word in value for word in ('bigger', 'larger', 'grow', 'up')):
        return 0
    if any(word in value for word in ('smaller', 'shrink', 'down')):
        return 1
    return None


def _scale_percent_reply(text):
    """Typed answer to the scale amount question: index into SCALE_STEP_PERCENTS, 3 back, else None."""
    value = str(text or '').strip().casefold().rstrip('%').strip()
    if value in {'cancel', 'back', 'never mind', 'nevermind'}:
        return len(SCALE_STEP_PERCENTS)
    for index, step in enumerate(SCALE_STEP_PERCENTS):
        if value == str(step):
            return index
    return None


def _scale_fit_offer_reply(text):
    """Typed answer to the biggest-fit offer: 0 take it, 1 keep the current size, 2 back, else None.

    Refusals are read before affirmatives: "do not scale" and "no, keep the
    current scale" both contain the word "scale" but decline the offer.
    """
    value = ' '.join(str(text or '').casefold().split()).strip('.,!')
    if value in {'cancel', 'back', 'never mind', 'nevermind'}:
        return 2
    if (value.startswith('no') or 'keep' in value or 'stay' in value or 'current' in value
            or 'not scale' in value or "don't scale" in value or 'do not scale' in value):
        return 1
    if (value in {'yes', 'scale', 'biggest', 'do it', 'ok', 'okay', 'yep'}
            or value.startswith('scale to') or value.startswith('scale it')):
        return 0
    return None


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
    # Name the measured extent against the usable area, so the operator can
    # tell whether the model is over by 2 mm or 80 mm (#839).
    measured = ''
    dims = audit_dimensions(job)
    if dims:
        hardware = job.get('hardware') if isinstance(job.get('hardware'), dict) else {}
        bed = hardware.get('bed_mm') or BED
        measured = (f" ({dims[0]:g} x {dims[1]:g} mm vs usable {float(bed[0]) - 16:g} x "
                    f"{float(bed[1]) - 16:g} mm)")
    return (f'{model} using {plate} does not fit on one {_printer_name(job)} plate{measured}: calculated capacity is {capacity} copy with '
            '10 mm spacing and 8 mm edge margins. Choose a smaller profile, split it into plates, or scale it down.')


def _painted_no_fit_detail(fit, state):
    """The measured size sentence for a real painted no-fit (#839).

    Uses the blocking plate's layout measured at the 50% floor by
    painted_fit_for_job; when no measurement exists, says so instead of
    inventing numbers.
    """
    layout = fit.get('blocking_layout') or {}
    extent = layout.get('extent_mm')
    if not extent or len(extent) < 2:
        return ' I could not measure the plate layout, so no measured size is available for this refusal.'
    bed = (state or {}).get('bed_mm') or BED
    detail = (f" Even at 50%, the plate that blocks it measures {float(extent[0]):g} x "
              f"{float(extent[1]):g} mm")
    tower = layout.get('tower_mm')
    if tower:
        detail += f" plus a {float(tower):g} x {float(tower):g} mm prime tower"
    return detail + f"; the usable area is {float(bed[0]):g} x {float(bed[1]):g} mm."


def _unit_oversize_reply(text, rows):
    """Typed answers for the oversize-inch question: mm, scale (or its percent), or cancel."""
    value = _typo_words(str(text or '').strip().casefold())
    if value in {'mm', 'millimeter', 'millimeters', 'use mm', 'use mm instead', 'millimeters instead'}:
        matches = [i for i, row in enumerate(rows) if row['kind'] == 'mm']
    elif value in {'scale', 'shrink', 'smaller', 'scale it', 'scale to fit'}:
        matches = [i for i, row in enumerate(rows) if row['kind'] == 'scale']
    elif re.fullmatch(r'\d+(?:\.\d+)?\s*%', value):
        percent = float(value.rstrip('%').strip())
        matches = [i for i, row in enumerate(rows)
                   if row['kind'] == 'scale' and abs(row['scale'] * 100 - percent) < 0.06]
    elif value in {'cancel', 'stop', "don't print"}:
        matches = [i for i, row in enumerate(rows) if row['kind'] == 'cancel']
    else:
        return None
    if len(matches) != 1:
        raise ValueError('that choice is not available; choose a listed button')
    return matches[0]


def _fit_state(state, job):
    """The printer state the bed fit probes read: ``state`` naming the job's printer model.

    The probes run in a worker process whose settings are the default printer's,
    so without the model they would apply the default printer's bed exclusion
    area to another printer's job (#1195). A job with no printer id belongs to
    the default printer and gets ``state`` unchanged. An id the roster does not
    know raises (settings.by_id), so the probe fails by name instead of guessing.
    """
    printer_id = job.get('printer_id')
    if not printer_id:
        return state
    return {**state, 'printer_model': settings.by_id(printer_id)['model']}
