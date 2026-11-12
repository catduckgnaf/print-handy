"""The operator questions a job asks before slicing (units, colors, filament warnings), the
filament usage and spool projection, and the job summary.

Moved verbatim from ``print_handy.core.model_job`` (#1323). The old names are still
importable from there for one release.
"""
import logging
import json, time
from pathlib import Path

from ..core import printer_state
from ..gcode.artifact import file_sha256
from .filament import position_limit, resolve_filament_selection
from .slice import (
    INCH_TO_MM, UNIT_QUESTION_MAX_MM, _enforce_filament_readiness, _is_generated_mm,
    normalize_units,
)
from .store import (
    _job_adapter, _job_record_for_gcode, _recorded_printer_id, decisions_digest,
    effective_color_decisions, resolve_print_target, set_color_decisions, write_job,
)
from ..launch.print_now import _sync_active_spool
from ..printers.base import MultiColorUnsupported

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy.core.model_job")


def show(job):
    print(f"{job['title']} [{job['site']} {job['model_id']}]\nHardware: {printer_state.summary(job['hardware'])}")
    print("Sources:")
    for x in job["sources"]:
        if len(x.get('filaments', [])) > 1 or len(x.get('source_parts', [])) > 1:
            print('  Color/part choices required for this profile: ' + json.dumps({
                'filaments': x.get('filaments'), 'parts_by_plate': x.get('source_parts')}))
        if job["site"]=="makerworld":
            from ..core.makerworld_profiles import selection_summary
            print(f"  {x['n']}. {selection_summary(x)} | nozzle {x.get('site_nozzle_mm')} mm | {', '.join(x['materials']) or 'material unstated'}")
        else: print(f"  {x['n']}. {x['name']} | {x['kind']} | {x['bytes']/1048576:.1f} MB")


def retry_pending_spool_sync(filename, printer_id=None):
    """Retry a running job's launch-time pending active-spool sync (#838).

    At start, a Spoolman outage records `spoolman_usage_pending` on the job
    and the launch proceeds (#387) — but nothing read it back, so Moonraker's
    [spoolman] charged the previous print's spool for the whole run. The
    listener calls this after a successful Spoolman probe: when the current
    print's job carries an unresolved pending record naming a spool, point
    the active spool at it now and clear the record. Fail-soft by design:
    any failure leaves the record untouched and returns False; a multi-slot
    pending record (spool_id None — the stale-spool clear case) is not
    retried here, because no single spool is the job's.

    The retry acts on the JOB's printer: an explicit `printer_id` (the
    calling listener's roster identity) wins, else the job's recorded
    printer_id. Loading state without an ID reads the legacy/default
    printer's record, so a non-default printer's retry would otherwise
    point (or fail on) another printer's Moonraker endpoint (#847).
    """
    try:
        artifact = Path(resolve_print_target(filename)).expanduser().resolve()
        job, job_path = _job_record_for_gcode(artifact)
        pending = (job or {}).get('spoolman_usage_pending')
        if not isinstance(pending, dict) or not pending.get('spool_id'):
            return False
        slot = pending.get('slot')
        if slot not in (1, 2, 3, 4):
            return False
        routed = printer_id or _recorded_printer_id(job)
        st = printer_state.load(routed) if routed else printer_state.load()
        sync = (_sync_active_spool(st, slot, printer_id=routed) if routed
                else _sync_active_spool(st, slot))
        if sync.get('spoolman_pending') or sync.get('spool_id') != pending['spool_id']:
            return False
        job.pop('spoolman_usage_pending', None)
        write_job(job_path.parent, job)
        return True
    except (Exception, SystemExit) as exc:  # resolve_print_target refuses with SystemExit
        logger.warning('pending active-spool retry for %s left in place: %s', filename, exc)
        return False


def get_author_settings(src_path):
    """Extract author brim and support configurations from a 3MF file."""
    import zipfile
    p = Path(src_path)
    res = {}
    if not p.is_file() or p.suffix.lower() != ".3mf":
        return res
    try:
        with zipfile.ZipFile(p) as z:
            for n in z.namelist():
                if n.endswith("project_settings.config"):
                    data = json.loads(z.read(n).decode("utf-8", errors="ignore"))
                    # Brim settings
                    bt = data.get("brim_type", "")
                    bw = data.get("brim_width")
                    if bt == "no_brim":
                        res["brim_width"] = "0"
                    elif bw is not None:
                        res["brim_width"] = str(bw)
                    # Support settings
                    es = data.get("enable_support")
                    if es is not None:
                        val = int(es) if str(es).isdigit() else (1 if es else 0)
                        res["support_material"] = str(val)
                    st = data.get("support_type")
                    if st and "tree" in str(st).lower():
                        res["support_material_style"] = "organic"
                    elif st:
                        # Bambu normal(auto)/normal(manual) is grid style; never leave the template's organic in place.
                        res["support_material_style"] = "grid"
                    break
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"unable to read author settings from {p}: {exc}") from exc
    return res


def _audit_dims(job):
    """Authored extents from the job audit, or None when they were not measured."""
    ad = job.get('audit') or {}
    try:
        dims = tuple(float(ad[key]) for key in ('size_x', 'size_y', 'size_z'))
    except (KeyError, TypeError, ValueError):
        return None
    return dims if all(value > 0 for value in dims) else None


def make_unit_decision(units, source_sha256, dims):
    """A user unit confirmation bound to the exact source bytes it was given for."""
    return {'source_sha256': source_sha256, 'units': normalize_units(units),
            'original_mm': [float(value) for value in dims], 'source': 'user_confirmation'}


def set_unit_decision(job, decision):
    """Record a unit confirmation; a changed answer retires any artifact sliced under the old one."""
    if decisions_digest(job.get('unit_decision')) != decisions_digest(decision):
        if job.get('gcode'):
            job.setdefault('artifact_history', []).append({key: job[key] for key in
                ('gcode', 'gcode_verified', 'gcode_full_verified_sha256', 'provenance', 'artifact_binding',
                 'sliced_at', 'slice_config', 'slice_recipe') if key in job})
        for key in ('gcode', 'provenance', 'artifact_binding', 'sliced_at', 'slice_config', 'slice_recipe',
                    'gcode_full_verified_sha256'):
            job.pop(key, None)
        job['gcode_verified'] = False
    job['unit_decision'] = decision
    job['unit_provenance'] = {'units': decision['units'], 'source': 'user_confirmation',
                              'original_mm': decision['original_mm'],
                              'factor': INCH_TO_MM if decision['units'] == 'inch' else 1.0}


def unit_question(job):
    """The inch-vs-mm question for a small STL job, or None when its units are already settled.

    STL files do not record units. A largest authored dimension under 10 mm is
    ambiguous — a genuine millimeter pin and an inch-authored part look alike —
    so intake must ask instead of assuming inches. Units are settled by an
    explicit scale (including 1.0), by a source-bound user confirmation, or
    when Print Handy generated the part itself in millimeters.
    """
    src = job.get('downloaded_file')
    if not src or Path(str(src)).suffix.lower() != '.stl':
        return None
    dims = _audit_dims(job)
    if dims is None or max(dims) >= UNIT_QUESTION_MAX_MM:
        return None
    if job.get('scale') is not None:
        return None
    if _is_generated_mm(job):
        return None
    decision = job.get('unit_decision')
    if isinstance(decision, dict):
        try:
            normalize_units(decision.get('units'))
        except ValueError:
            pass
        else:
            known_sha = ((job.get('color_manifest') or {}).get('source_sha256')
                         or (job.get('audit') or {}).get('sha256'))
            if not decision.get('source_sha256') or not known_sha or decision['source_sha256'] == known_sha:
                return None
    return _unit_packet(dims)


def _unit_packet(dims):
    """The inch-vs-mm question packet for authored extents ``dims`` (mm as written)."""
    inch_dims = tuple(round(value * INCH_TO_MM, 6) for value in dims)
    measured = ' x '.join(f'{value:g}' for value in dims)
    as_inch = ' x '.join(f'{value:g}' for value in inch_dims)
    return {'status': 'UNIT_CONFIRMATION_REQUIRED',
            'original_mm': [float(value) for value in dims], 'inch_mm': list(inch_dims),
            'question': (f'STL files do not record units. This model measures {measured} mm as written: '
                         f'is that millimeters (prints at {measured} mm) or inches (prints at {as_inch} mm)?'),
            'choices': [f'Millimeters — print at {measured} mm', f'Inches — print at {as_inch} mm'],
            'resume': 'slice JOB.json --approved --units mm|inch (or unit-select JOB.json --units mm|inch)'}


def parts_unit_question(job, paths):
    """The inch-vs-mm question for the STL files of a multi-file job, or None when units are settled.

    A combined job is a 3MF (millimeters), so the single-STL check never sees
    its parts. Same rule on the parts instead: when the largest STL part is
    under UNIT_QUESTION_MAX_MM the set is ambiguous, and one answer covers all
    the STL files (3MF parts carry their own units).
    """
    if job.get('scale') is not None or _is_generated_mm(job):
        return None
    from ..core.geometry_layout import load_parts
    from ..core.model_parts import _bounds
    largest = None
    for path in paths:
        if Path(str(path)).suffix.lower() != '.stl':
            continue
        for part in load_parts(Path(path))[0]:
            lo, hi = _bounds(part['triangles'])
            dims = tuple(top - bottom for bottom, top in zip(lo, hi))
            if largest is None or max(dims) > max(largest):
                largest = dims
    if largest is None or not all(value > 0 for value in largest) or max(largest) >= UNIT_QUESTION_MAX_MM:
        return None
    return _unit_packet(largest)


def intake_unit_confirmation(job_path, units=None, interactive=False):
    """Normal intake gate: ask inch vs mm for a small STL and persist the answer on the job.

    Noninteractive callers receive a structured question packet and resume the
    same JOB via slice --units; no JSON file authoring is required. Settled
    jobs record their unit provenance and pass through.
    """
    path = Path(job_path)
    job = json.loads(path.read_text())
    question = unit_question(job)
    if question is None:
        dims = _audit_dims(job)
        if dims is not None and Path(str(job.get('downloaded_file') or '')).suffix.lower() == '.stl' \
                and not isinstance(job.get('unit_provenance'), dict):
            provenance = None
            if job.get('scale') is not None:
                provenance = {'units': None, 'source': 'explicit_scale',
                              'original_mm': [float(value) for value in dims], 'factor': float(job['scale'])}
            elif _is_generated_mm(job):
                provenance = {'units': 'mm', 'source': 'generated',
                              'original_mm': [float(value) for value in dims], 'factor': 1.0}
            elif max(dims) >= UNIT_QUESTION_MAX_MM:
                provenance = {'units': 'mm', 'source': 'authored_size',
                              'original_mm': [float(value) for value in dims], 'factor': 1.0}
            if provenance is not None:
                job['unit_provenance'] = provenance
                write_job(path.parent, job)
        return True
    question['job_json'] = str(path)
    if units is None:
        print(json.dumps(question, indent=2))
        if not interactive:
            return False
        answer = input('Units (mm or inch; blank stops): ').strip()
        if not answer:
            return False
        units = answer
    decision = make_unit_decision(units, file_sha256(job['downloaded_file']), question['original_mm'])
    set_unit_decision(job, decision)
    write_job(path.parent, job)
    return True


def _check_tool_assignments(manifest, rows, selections):
    """One colour per toolhead (#346): prompts whose source colours differ must
    not share a toolhead. Prompts sharing a source colour may share one."""
    from ..core.source_colors import prompt_source_colour
    seen = {}
    for row, selection in zip(rows, selections):
        slot = selection['slot']
        colour = prompt_source_colour(manifest, row)
        if slot in seen and seen[slot] != colour:
            raise ValueError(f"toolhead {slot} is already assigned another color in this job; "
                             "one toolhead prints one color per job")
        seen[slot] = colour


def record_filament_warning_acknowledgement(folder_or_job, warnings=None, source_sha256=None,
                                            selected_plates=None, via='print_anyway'):
    """Record the operator's explicit acceptance of named filament warnings.

    Owner ruling (catduck, 2026-11-10): filament conditions warn and the
    user may accept the warning; they never hard-block a print. The
    acceptance is bound to the exact warning texts, the source hash and the
    selected plates it was given for — never a blanket waiver (the binding
    rule slice_warnings.record_unsupported_cantilever_override states for
    its record). With no explicit warnings, the job's
    filament_warnings_pending record (written by the gate that raised
    FilamentWarningPending) supplies them; with neither, there is nothing
    to accept and the call refuses rather than recording a blank waiver.
    The acceptance authorizes one slicing run: slice_job marks it used and
    the next slice_job consumes it, so a reslice presents the warnings
    again (the clearance pattern's reslice rule).
    """
    is_path = isinstance(folder_or_job, (str, Path))
    if is_path:
        p = Path(folder_or_job)
        jp = p if p.name == 'JOB.json' else p / 'JOB.json'
        job = json.loads(jp.read_text())
    else:
        job = folder_or_job
        jp = None
    pending = job.get('filament_warnings_pending') or {}
    warnings = list(warnings) if warnings is not None else list(pending.get('warnings') or [])
    if not warnings:
        raise ValueError('no filament warnings to accept; an acceptance is never recorded blank')
    if source_sha256 is None:
        source_sha256 = pending.get('source_sha256')
    if selected_plates is None:
        selected_plates = pending.get('selected_plates')
    job['filament_warning_acknowledgement'] = {
        'code': 'FILAMENT_WARNINGS_ACCEPTED',
        'acknowledged': True,
        'via': via,
        'warnings': warnings,
        'selected_plates': [str(p) for p in selected_plates or []],
        'source_sha256': source_sha256,
        'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }
    job.pop('filament_warnings_pending', None)
    if is_path and jp is not None:
        write_job(jp.parent, job)
    return job


def intake_color_choices(job_path, state, mode=None, slots=None, interactive=False):
    """Normal intake gate: ask per part/palette and persist exact-source choices.

    Noninteractive callers receive a structured button-ready question packet.
    They resume the same JOB via slice; no JSON file authoring is required.
    """
    from ..core.source_colors import inventory, prompts, canonical_prompts, validate_decisions, distinct_source_colours
    path = Path(job_path)
    job = json.loads(path.read_text())
    manifest = inventory(job['downloaded_file'])
    # A job that records a plate selection asks only about the selected
    # plates' colors and validates decisions against that scope (#1110 B).
    selected_plates = job.get('selected_plates') or None
    if not manifest.get('source_sha256'):
        manifest['source_sha256'] = file_sha256(job['downloaded_file'])
    adapter = _job_adapter(job)
    multi_tool = adapter.tool_based
    # The slot questions below are built from the record's slot map, so they serve the
    # record-bound multi_slot printers (and, tool-shaped, the multi_tool pair). A Bambu
    # AMS model is multi_slot by class (#671) but not record-bound: its filament slots
    # are the file's own, matched to reported AMS trays at print start, so a source that
    # needs slot choices is refused here exactly as it was while the model was "single".
    if not multi_tool and not (adapter.multi_slot and adapter.slots_bound_by_record):
        if manifest['requires_choices']:
            # Height-based colors alone (#870) are not a slot choice: they
            # print as pauses for a manual swap or in one color, whichever
            # the recorded decisions say. Without a recorded choice the job
            # stops here and names that choice — never a silent one color.
            layered_only = (bool(manifest.get('layer_changes')) and len(manifest['parts']) <= 1
                            and not manifest['painted'])
            if layered_only and job.get('color_decisions'):
                validated = validate_decisions(manifest, job['color_decisions'], state,
                                               resolve_filament_selection,
                                               selected_plate_ids=selected_plates)
                if selected_plates:
                    _enforce_filament_readiness(job, manifest, validated, selected_plates,
                                                slot_capacity=position_limit(adapter),
                                                job_folder=path.parent, state=state)
                return True
            if layered_only:
                raise MultiColorUnsupported(
                    f"{adapter.label} has a single extruder; this source changes color by height. Choose "
                    "pauses at each change height (swap the filament by hand) or one color for everything "
                    "before slicing")
            raise MultiColorUnsupported(
                f"{adapter.label} has a single extruder; this source has several parts or colors that need a "
                "slot choice. Slice a single-color model or use a multi-slot printer")
        return True
    questions = (canonical_prompts(manifest, selected_plate_ids=selected_plates)
                 if job.get('color_schema') == 'part_slots_v1'
                 else prompts(manifest, selected_plate_ids=selected_plates))
    available = []
    for slot, label in sorted(state.get('filament_slots', {}).items()):
        selection = resolve_filament_selection(state, dict(label, slot=int(slot)))
        noun = 'Toolhead' if multi_tool else 'Slot'
        available.append({'label': f"{noun} {slot}: {selection['color']} {selection['material']} (configured label)",
                          'selection': selection})
    job['color_manifest'], job['color_prompts'] = manifest, questions
    job['color_questions'] = [dict(q, choices=available) for q in questions]
    write_job(path.parent, job)
    if not manifest['requires_choices']:
        return True
    if mode is None and job.get('color_decisions'):
        validated = validate_decisions(manifest, job['color_decisions'], state,
                                       resolve_filament_selection,
                                       selected_plate_ids=selected_plates)
        if selected_plates:
            _enforce_filament_readiness(job, manifest, validated, selected_plates,
                                        slot_capacity=position_limit(adapter),
                                        job_folder=path.parent, state=state)
        return True
    packet = {'status': 'COLOR_CHOICES_REQUIRED', 'job_json': str(path),
              'source_sha256': manifest['source_sha256'],
              'question': 'Preserve colors per part/source filament, or explicitly print everything in one color?',
              'choices': ['Preserve colors per part/source filament', 'Print everything in one color'],
              'questions': job['color_questions'],
              'next_action': 'Present the source-bound questions once through the internal color workflow and persist selections to this same job. Never ask the user to supply a filesystem path, raw source index or command.',
              'resume': 'slice JOB.json --approved --color-mode per_part --color-slot SLOT (repeat for each question)'}
    if mode is None:
        print(json.dumps(packet, indent=2))
        if not interactive:
            return False
        answer = input('Color mode (per_part or single_color; blank stops): ').strip()
        if not answer:
            return False
        mode = answer
    if mode not in ('per_part', 'single_color'):
        raise ValueError('color mode must be per_part or single_color')
    if multi_tool and mode == 'per_part':
        # One colour per toolhead (#346): more distinct source colours than
        # toolheads can never be assigned. (single_color stays available:
        # one toolhead prints everything.)
        colours = distinct_source_colours(manifest)
        if len(colours) > adapter.tool_count:
            raise MultiColorUnsupported(
                f"{adapter.label} has {adapter.tool_count} toolheads and prints one color per toolhead; "
                f"this source uses {len(colours)} colors. Reduce the source to at most "
                f"{adapter.tool_count} colors or use a printer with more toolheads")
    rows = questions if mode == 'per_part' else [
        {'question': f"Choose one {'toolhead' if multi_tool else 'slot'} for ALL parts and source colors"}]
    if slots is None and interactive:
        print('\n'.join(choice['label'] for choice in available))
        slots = [int(input(row['question'] + ': ')) for row in rows]
    if not slots or len(slots) != len(rows):
        raise ValueError('supply one --color-slot for every displayed question (one for single_color)')
    selections = [resolve_filament_selection(state, dict(state.get('filament_slots', {}).get(str(slot), {}), slot=slot)) for slot in slots]
    if multi_tool and mode == 'per_part':
        _check_tool_assignments(manifest, rows, selections)
    decisions = {'source_sha256': manifest['source_sha256'], 'mode': mode}
    if job.get('color_schema') == 'part_slots_v1':
        decisions['schema'] = 'part_slots_v1'
    if mode == 'single_color':
        decisions['selection'] = selections[0]
    else:
        decisions['choices'] = {row['key']: selection for row, selection in zip(rows, selections)}
    validated = validate_decisions(manifest, decisions, state, resolve_filament_selection,
                                   selected_plate_ids=selected_plates)
    if selected_plates:
        _enforce_filament_readiness(job, manifest, validated, selected_plates,
                                    slot_capacity=position_limit(adapter),
                                    job_folder=path.parent, state=state)
    set_color_decisions(job, validated)
    write_job(path.parent, job)
    return True


def _usage_for_source_extruder(manifest, source_extruder):
    stats = (manifest or {}).get('filament_stats') or {}
    extruders = stats.get('extruders') if isinstance(stats, dict) else {}
    if not isinstance(extruders, dict):
        return None
    row = extruders.get(source_extruder, extruders.get(str(source_extruder)))
    grams = row.get('g') if isinstance(row, dict) else None
    return float(grams) if isinstance(grams, (int, float)) and not isinstance(grams, bool) and grams > 0 else None


def source_site_estimate(row):
    """(seconds, grams) summed over the author's plates from the model page (MakerWorld), else None."""
    plates = (row or {}).get('plates') if isinstance(row, dict) else None
    if not isinstance(plates, list) or not plates:
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


def _reference_manifest(job, manifest):
    """Replace a volume-guessed total with the model page's own weight when one exists.

    A guessed total also lands entirely on extruder 1, so every part shares it equally.
    """
    stats = (manifest or {}).get('filament_stats') or {}
    parts = [p for p in (manifest or {}).get('parts') or [] if isinstance(p, dict)]
    extruders = stats.get('extruders') if isinstance(stats.get('extruders'), dict) else {}
    used = {str(p.get('source_extruder')) for p in parts if p.get('source_extruder') is not None}
    guessed = bool(stats.get('estimated')) or bool(used - {str(k) for k in extruders})
    if not guessed:
        return manifest, False
    site = source_site_estimate((job or {}).get('selected_source'))
    total = site[1] if site else stats.get('total_g')
    if not isinstance(total, (int, float)) or isinstance(total, bool) or total <= 0:
        return manifest, False
    flat = {**manifest, 'parts': [{**p, 'source_extruder': 1} for p in parts],
            'filament_stats': {**stats, 'total_g': float(total), 'extruders': {'1': {'g': float(total)}}}}
    return flat, True


def _selected_plate_stats(manifest, selected):
    """A manifest copy whose filament totals cover only the selected plates.

    Uses the slicer-reported per-plate records; returns None when any
    selected plate lacks a validated record, so the caller keeps the
    whole-source totals rather than presenting a partial sum as the total.
    """
    stats = (manifest or {}).get('filament_stats') or {}
    plates = stats.get('plates') if isinstance(stats, dict) else None
    if not isinstance(plates, dict):
        return None
    extruders, total_g, total_m = {}, 0.0, 0.0
    for plate_id in selected:
        row = plates.get(str(plate_id))
        if not isinstance(row, dict) or row.get('usage_known') is not True:
            return None
        records = row.get('extruders') or {}
        if not isinstance(records, dict):
            return None
        for ext, amounts in records.items():
            # A record missing an amount, or carrying a non-number, is not
            # a validated record (#1155): keep the whole-source totals
            # rather than fail the projection with a KeyError.
            try:
                ext_id, grams, meters = int(ext), float(amounts['g']), float(amounts['m'])
            except (KeyError, TypeError, ValueError):
                return None
            entry = extruders.setdefault(ext_id, {'g': 0.0, 'm': 0.0})
            entry['g'] += grams
            entry['m'] += meters
            total_g += grams
            total_m += meters
    scoped = {**stats, 'extruders': extruders, 'total_g': round(total_g, 1),
              'total_m': round(total_m, 1)}
    scoped.pop('estimated', None)
    return {**manifest, 'filament_stats': scoped}


def filament_usage_by_selection(job, manifest=None):
    """Aggregate source estimates by confirmed selection, without duplicating multi-color totals."""
    manifest = manifest or (job or {}).get('color_manifest') or {}
    selected = (job or {}).get('selected_plates')
    if selected:
        # A selected-plate print spends only the selected plates' filament:
        # scope the totals to those plates' validated slicer records (#1110
        # Phase B). The reference-manifest flattening must not run on the
        # scoped totals (unselected plates' extruders are absent by design).
        scoped = _selected_plate_stats(manifest, selected)
        if scoped is not None:
            manifest = scoped
        else:
            manifest, _ = _reference_manifest(job, manifest)
    else:
        manifest, _ = _reference_manifest(job, manifest)
    # A #1189 fallback artifact accounts for the slots it was sliced with.
    decisions = effective_color_decisions(job) or {}
    if decisions.get('mode') == 'single_color':
        selection = decisions.get('selection') or (job or {}).get('filament_selection')
        total = ((manifest.get('filament_stats') or {}).get('total_g')
                 if isinstance(manifest, dict) else None)
        if isinstance(selection, dict) and isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0:
            return [(selection, float(total))]
        return []
    choices = decisions.get('choices') if isinstance(decisions, dict) else None
    if not isinstance(choices, dict):
        return []
    # Painted sources contain both geometry and palette choice rows. The palette
    # rows are the authoritative per-extruder allocation, so do not count both.
    palette_choices = {key: value for key, value in choices.items()
                       if isinstance(key, str) and key.startswith('filament:')}
    part_extruders = {'part:' + str(part.get('key')): part.get('source_extruder')
                      for part in manifest.get('parts') or [] if isinstance(part, dict)}
    choice_rows = palette_choices or choices
    # An extruder total belongs to every part on that extruder together, never to each one.
    # Split it across those parts (by volume when known) so N parts never count N x the total.
    sharers = {}
    for key in choice_rows:
        if isinstance(key, str) and not key.startswith('filament:'):
            sharers.setdefault(str(part_extruders.get(key)), []).append(key)
    volumes = {'part:' + str(p.get('key')): p.get('volume_mm3')
               for p in manifest.get('parts') or [] if isinstance(p, dict)}
    totals = {}
    for key, selection in choice_rows.items():
        if not isinstance(selection, dict) or not isinstance(key, str):
            continue
        source_text = (key.split(':', 1)[1] if key.startswith('filament:') else part_extruders.get(key))
        try:
            source_extruder = int(source_text)
        except (TypeError, ValueError):
            continue
        grams = _usage_for_source_extruder(manifest, source_extruder)
        if grams is None:
            continue
        if not key.startswith('filament:'):
            group = sharers.get(str(source_text)) or [key]
            vols = [volumes.get(k) for k in group]
            if all(isinstance(v, (int, float)) and v > 0 for v in vols):
                grams *= float(volumes[key]) / sum(vols)
            else:
                grams /= len(group)
        identity = (selection.get('slot'), selection.get('spoolman_id'),
                    str(selection.get('color') or '').casefold(), str(selection.get('material') or '').casefold())
        item = totals.setdefault(identity, [selection, 0.0])
        item[1] += grams
    return [(selection, grams) for selection, grams in totals.values()]


def intake_spool_projection(job, spool_weights, manifest=None, copies=1):
    """Remaining-weight projection from exact source estimates and saved choices."""
    try:
        copies = max(1, int(copies or 1))
    except (TypeError, ValueError):
        copies = 1
    rows = []
    for selection, grams in filament_usage_by_selection(job, manifest):
        grams *= copies
        remaining = spool_weights.get(selection.get('spoolman_id')) if isinstance(spool_weights, dict) else None
        projected = (float(remaining) - grams if isinstance(remaining, (int, float))
                     and not isinstance(remaining, bool) else None)
        rows.append({'slot': selection.get('slot'), 'color': selection.get('color'),
                     'material': selection.get('material'), 'spoolman_id': selection.get('spoolman_id'),
                     'estimated_used_g': round(grams, 1),
                     'remaining_g': round(float(remaining), 1) if isinstance(remaining, (int, float))
                     and not isinstance(remaining, bool) else None,
                     'projected_remaining_g': round(projected, 1) if projected is not None else None})
    return rows


class IntakeSpoolWeights(dict):
    """Spool weights plus whether the live Spoolman fetch failed (#839).

    A plain dict for every existing reader; ``fetch_failed`` carries the
    availability signal alongside the fallback weights, so a caller can
    tell amounts that came from a live check from amounts that came from
    the cached slot record after Spoolman did not answer.
    """

    fetch_failed = False


def intake_spool_weights(state, spool_ids, fetch=None):
    """Currently available grams per Spoolman ID: Spoolman first, then the slot record, else absent.

    Display only. Never raises; an unreachable Spoolman just falls back,
    and the returned weights carry ``fetch_failed`` so the fallback is
    never mistaken for a live check (#839).
    """
    slots = {s.get('spoolman_id'): s for s in ((state or {}).get('filament_slots') or {}).values()
             if isinstance(s, dict) and s.get('spoolman_id') is not None}
    weights = IntakeSpoolWeights()
    for sid in spool_ids:
        grams = None
        if fetch is not None and isinstance(sid, int) and not isinstance(sid, bool):
            try:
                rem = (fetch(sid) or {}).get('remaining_weight')
                if isinstance(rem, (int, float)) and not isinstance(rem, bool) and rem >= 0:
                    grams = float(rem)
            except Exception:
                grams = None
                weights.fetch_failed = True
        if grams is None:
            rem = (slots.get(sid) or {}).get('remaining_grams')
            if isinstance(rem, (int, float)) and not isinstance(rem, bool) and rem >= 0:
                grams = float(rem)
        if grams is not None:
            weights[sid] = grams
    return weights


def format_spool_projection(rows, position_noun='Slot'):
    """One short line per spool: needed, available, left after. Unknowns stay unknown."""
    lines = []
    for r in rows:
        label = ' '.join(str(x) for x in (r.get('color'), r.get('material')) if x) or 'filament'
        if r.get('slot') is not None:
            label = f"{position_noun} {r['slot']} {label}"
        line = f"{label}: needs about {r['estimated_used_g']:g} g"
        if r.get('remaining_g') is None:
            line += '; spool weight unknown.'
        elif r['projected_remaining_g'] < 0:
            line += (f"; spool has {r['remaining_g']:g} g, short by about "
                     f"{-r['projected_remaining_g']:g} g.")
        else:
            line += (f"; spool has {r['remaining_g']:g} g, about "
                     f"{r['projected_remaining_g']:g} g left after.")
        lines.append(line)
    return '\n'.join(lines)
