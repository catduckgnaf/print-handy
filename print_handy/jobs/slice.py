"""Slicing a job: slice_job and its single, merged, per-plate, painted and full-size split paths,
the fit and painted-scale searches, the filament fallbacks and warnings the slice enforces.

Moved verbatim from ``print_handy.core.model_job`` (#1323). The old names are still
importable from there for one release.
"""
import json, math, re, time, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import printers
from ..core import printer_state, purge
from ..core.filament_warnings import FilamentWarningPending
from ..gcode.artifact import file_sha256
from ..gcode.verify import _verify_routed, verify_gcode_offline
from .common import ARCHIVE, BED_MM, FILAMENT_SLOTS, MAX_HEIGHT_MM
from .filament import _validate_slot_selection, configured_slots, position_limit, resolve_filament_selection
from ..printers.base import MultiColorUnsupported
from .store import (
    _job_adapter, _recipe_binding, _recipe_changed, _recorded_printer_id, _timing, decisions_digest,
    gcode_estimate_s, human_dur, stamp_job_printer, write_job,
)


def finalize_multicolor(*args, **kwargs):
    """ZMod color/slot-map insertion and MD5, through the printer adapter. A printer without the
    ZMod contract (the stock AD5X, #658) gets nothing inserted: its launch binds the slot from the job."""
    native = printers.current().native_or_none
    return None if native is None else native.finalize(*args, **kwargs)


def plate_manifest(path):
    """Inventory source plate assignments without slicing or touching the printer."""
    path=Path(path)
    if path.suffix.lower() != ".3mf": raise RuntimeError("plate manifest requires a 3MF source")
    with zipfile.ZipFile(path) as archive:
        names=set(archive.namelist())
        preview_ids={int(m.group(1)) for name in names
                     if (m:=re.fullmatch(r"Metadata/plate_(\d+)\.png",name))}
        if "Metadata/model_settings.config" not in names:
            raise RuntimeError("3MF has no plate assignments in Metadata/model_settings.config")
        try:
            root=ET.fromstring(archive.read("Metadata/model_settings.config"))
            plates=[]
            for plate in root.iter("plate"):
                metadata={m.get("key"):m.get("value") for m in plate.findall("metadata")}
                raw_id=metadata.get("plater_id")
                if raw_id is None: raise RuntimeError("plate has no plater_id assignment")
                plate_id=int(raw_id)
                object_ids=[]
                for instance in plate.findall("model_instance"):
                    for entry in instance.findall("metadata"):
                        if entry.get("key")=="object_id":
                            value=entry.get("value")
                            if value is None: raise RuntimeError(f"plate {plate_id} has empty object assignment")
                            object_ids.append(int(value))
                if not object_ids: raise RuntimeError(f"plate {plate_id} has no object assignments")
                plates.append({"id":plate_id,"name":metadata.get("plater_name") or f"Plate {plate_id}",
                               "object_ids":object_ids,"status":"planned"})
        except (ET.ParseError,KeyError,TypeError,ValueError) as exc:
            raise RuntimeError(f"invalid 3MF plate assignments: {exc}") from exc
        ids=[p["id"] for p in plates]
        if not ids or len(ids)!=len(set(ids)) or (preview_ids and preview_ids!=set(ids)):
            raise RuntimeError("3MF plate assignments missing, duplicated, or inconsistent with previews")
    return {"source_file":str(path.resolve()),"source_sha256":file_sha256(path),
            "plates":sorted(plates,key=lambda p:p["id"])}


class NoPrintableVolume(RuntimeError):
    """The mesh encloses no volume (flat or degenerate): nothing to slice or print (#194)."""


def model_info_output(path):
    """Run or read cached geometry extents/manifoldness for a model file."""
    path = Path(path)
    global _INFO_CACHE
    if "_INFO_CACHE" not in globals():
        _INFO_CACHE = {}
    try:
        st = path.stat()
        key = (str(path.resolve()), st.st_size, st.st_mtime)
        if key in _INFO_CACHE:
            return _INFO_CACHE[key]
    except Exception:
        key = None

    # Measured from the printable mesh in-process (Orca-only stack; no Prusa).
    # Cutters and modifiers are already excluded by source_triangles.
    from ..core.orca_adapter import source_triangles
    try:
        tris = source_triangles(path)
    except RuntimeError as exc:
        # A file with no mesh at all (objects named, no triangles) is refused at intake like a
        # flat one (#194), not left to crash the fit check as an unexplained error (2026-11-03).
        if "no printable triangles" in str(exc):
            raise NoPrintableVolume(f"model has no printable geometry ({exc})") from exc
        raise
    if not tris:
        raise NoPrintableVolume("model has no printable geometry")
    from ..core.mesh_fast import bounds_and_volume
    lo, hi, vol = bounds_and_volume(tris)
    if not all(math.isfinite(float(v)) for v in (*lo, *hi, vol)):
        # NaN/inf coordinates poison every extent, volume and fit check downstream; refuse at the
        # boundary with a message the operator can act on.
        raise NoPrintableVolume("model contains non-finite (NaN or infinite) coordinates; "
                                "re-export it from your CAD or slicer tool")
    if min(hi[a] - lo[a] for a in range(3)) <= 1e-6 or abs(vol) <= 1e-9:
        # Flat or zero-volume mesh (#194): nothing for a slicer to fill, so refuse before
        # any plate is offered rather than slicing empty layers and starting a print.
        raise NoPrintableVolume("model has no printable volume (flat or zero-volume mesh)")
    info = "".join(f"{k} = {hi[a] - lo[a]:.2f}\n" for a, k in enumerate(("size_x", "size_y", "size_z")))
    info += f"volume = {abs(vol):.2f}\n"
    if key is not None:
        _INFO_CACHE[key] = info
    return info


def copies_supported(job, multi_plate=None):
    """Whether slicing this job clones copies into its G-code (#37).

    Only the single plate path passes copies to Orca. Multi-plate, split and
    per-part jobs slice one copy per plate, so they must never be offered more.
    """
    if multi_plate is None:
        try: multi_plate = int((job.get('audit') or {}).get('plates') or 0) > 1
        except (TypeError, ValueError): multi_plate = False
    return not (multi_plate or job.get('full_size_split_plan') or job.get('full_size_rearrange_plan')
                or job.get('plate_mode') == 'separate'
                or (job.get('color_decisions') or {}).get('mode') == 'per_part')


UNIT_QUESTION_MAX_MM = 10.0
INCH_TO_MM = 25.4


def normalize_units(value):
    """Canonical unit name for a user answer: 'mm' or 'inch'."""
    text = str(value or '').strip().lower()
    if text in ('mm', 'millimeter', 'millimeters', 'millimetre', 'millimetres'):
        return 'mm'
    if text in ('inch', 'inches', 'in'):
        return 'inch'
    raise ValueError('units must be mm or inch')


def _is_generated_mm(job):
    """True when Print Handy itself generated the source, so its units are known mm."""
    provenance = job.get('unit_provenance')
    if isinstance(provenance, dict) and provenance.get('source') == 'generated' and provenance.get('units') == 'mm':
        return True
    return job.get('generated') is True or job.get('generated_units') == 'mm'


def _placed_bounds_by_object(geometry, object_of_key):
    """Per-object placed bounds from a multicolor exporter report (P2-85).

    The union of the part rows' bounds and the cutter volumes' bounds. The
    merged-placement proof compares these against bounds computed from ALL
    of an object's authored volumes, so the landed side must include the
    cutters too: a negative volume reaching past its body otherwise reads
    as the export having landed somewhere else, and a sound slice is
    refused.
    """
    placed = {}

    def fold(oid, box):
        if oid in placed:
            prev = placed[oid]
            box = [(min(prev[a][0], box[a][0]), max(prev[a][1], box[a][1])) for a in range(3)]
        placed[oid] = box

    for row in geometry.get('parts', []):
        fold(object_of_key[row['part_key']], row['bounds_mm'])
    for oid, box in (geometry.get('cutter_bounds_by_object') or {}).items():
        fold(str(oid), box)
    return placed


def _filament_ack_covers(job, warnings, source_sha256, selected_plates):
    """Whether the job's recorded acceptance covers exactly these warnings."""
    ack = job.get('filament_warning_acknowledgement') or {}
    if ack.get('acknowledged') is not True:
        return False
    if source_sha256 is not None and ack.get('source_sha256') != source_sha256:
        return False
    if {str(p) for p in ack.get('selected_plates') or []} != {str(p) for p in selected_plates or []}:
        return False
    return set(warnings) <= set(ack.get('warnings') or [])


def _enforce_filament_warnings(job, warnings, source_sha256, selected_plates, job_folder=None,
                               via_slice=False):
    """Present-or-proceed for one set of filament warnings.

    Returns the warnings when a bound acceptance already covers them (and
    records them on the job as the slice's filament_warnings provenance).
    Otherwise records them as the job's filament_warnings_pending — on disk
    when job_folder is given, so the surface that presents them and the
    operator's later acceptance resume from the same record — and raises
    FilamentWarningPending: without an acceptance the job does not proceed.

    A pass inside slice_job (via_slice) marks the acceptance used by that
    slicing run; the next slice_job consumes a used acceptance before its
    gates run, so a reslice presents the warnings again instead of silently
    reusing the acceptance. Passes at color-intake time do not mark it: the
    acceptance is given for the print attempt that then slices.
    """
    warnings = list(dict.fromkeys(warnings))
    if not warnings:
        return []
    if _filament_ack_covers(job, warnings, source_sha256, selected_plates):
        job['filament_warnings'] = warnings
        job.pop('filament_warnings_pending', None)
        if via_slice:
            job['filament_warning_acknowledgement']['used_by_slice'] = True
            if job_folder is not None:
                # Mark it used on disk now, not only with the artifact: a
                # slicing run that fails after this point must not leave the
                # acceptance reusable, so the next run presents the warnings
                # again (#1189: a reslice re-presents them).
                try:
                    on_disk = json.loads((Path(job_folder) / 'JOB.json').read_text())
                except (OSError, ValueError):
                    on_disk = None
                ack = on_disk.get('filament_warning_acknowledgement') if isinstance(on_disk, dict) else None
                if isinstance(ack, dict) and not ack.get('used_by_slice'):
                    ack['used_by_slice'] = True
                    write_job(job_folder, on_disk)
        return warnings
    job['filament_warnings_pending'] = {
        'warnings': warnings,
        'source_sha256': source_sha256,
        'selected_plates': [str(p) for p in selected_plates or []],
        'at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
    if job_folder is not None:
        write_job(job_folder, job)
    raise FilamentWarningPending(warnings, source_sha256=source_sha256,
                                 selected_plates=selected_plates)


def filament_fallback_slot(state, limit=None):
    """(slot, phrase): the slot a filament fallback prints from, and how it was chosen (#1189).

    The printer's active slot when the hardware record names one and it is
    loaded; otherwise the record's first (lowest-numbered) loaded slot; with
    no loaded-slot information at all, slot 1, and the phrase says so. The
    source is the hardware record print start launches from: the slot map
    configured_slots offers (filament_slots, or a single-extruder printer's
    one configured spool) and its active_slot. "Loaded" is a slot within
    the printer's slot count that the record maps to a filament material:
    a fallback never names a slot (or tool) the printer does not have.
    """
    try:
        limit = int(limit if limit is not None else position_limit())
    except (TypeError, ValueError):
        limit = FILAMENT_SLOTS
    loaded = []
    for key, rec in (configured_slots(state) or {}).items():
        try:
            slot = int(key)
        except (TypeError, ValueError):
            continue
        if (1 <= slot <= limit and isinstance(rec, dict)
                and isinstance(rec.get('material'), str) and rec['material'].strip()):
            loaded.append(slot)
    loaded.sort()
    active = (state or {}).get('active_slot')
    if type(active) is int and active in loaded:
        return active, f"slot {active}, the printer's active slot"
    if loaded:
        return loaded[0], f"slot {loaded[0]}, the printer's first loaded slot"
    return 1, 'slot 1, because the printer record has no loaded-slot information'


def _fallback_selection(state, slot):
    """The selection for a fallback slot, filled from the slot's record (validate_decisions resolves it)."""
    rec = (configured_slots(state) or {}).get(str(slot)) or {}
    return {'slot': slot, **{key: rec[key] for key in ('color', 'material', 'spoolman_id')
                             if isinstance(rec, dict) and rec.get(key) is not None}}


def _choice_label(manifest, key):
    """A color-choice key as the operator knows it: the part name, or the source filament."""
    kind, _, rest = str(key).partition(':')
    if kind == 'filament':
        return f'source filament {rest}'
    if kind == 'part':
        part = next((p for p in manifest.get('parts') or [] if str(p.get('key')) == rest), None)
        if part and part.get('name'):
            return f"part {part['name']}"
    return str(key)


def filament_fallbacks(manifest, decisions, selected_plates, state, limit=None, choices_needed=False):
    """(decisions to slice with, warnings) under the #1189 filament fallbacks.

    Owner decision (catduck, 2026-11-10): no filament condition hard-blocks
    a print. Three conditions that refused until #1189 now print from a
    deterministic fallback, named in a warning the operator accepts first:

    - a slot number past the printer's slot count prints from the fallback
      slot (filament_fallback_slot: the active slot, else the first loaded
      slot, else slot 1);
    - a used filament with no slot chosen on a selected plate prints from
      the slot already chosen for that same source filament (the part's own
      filament: a sibling part or its filament row), else the slot chosen
      for the first part on that plate that has one (the base filament),
      else the fallback slot; the parts or filament row left unchosen get
      that slot;
    - color choices never made (choices_needed and no decisions) print
      every part single-color from the fallback slot, exactly like a
      recorded single-color decision.

    Nothing here is persisted: with no condition the decisions come back
    unchanged and the warnings empty. Otherwise the returned decisions carry
    a 'fallback' record (the original decisions, the filament keys added,
    the warnings); decisions_digest identifies them by the original, and a
    fallback passed back in is re-derived from its original, so the same
    inputs always give the same decisions and warning texts. Decisions
    bound to another source are a decision-integrity refusal and are never
    repaired here.
    """
    original = decisions
    if isinstance(decisions, dict) and isinstance(decisions.get('fallback'), dict):
        original = decisions['fallback'].get('original')
        choices_needed = choices_needed or original is None
    try:
        limit = int(limit if limit is not None else position_limit())
    except (TypeError, ValueError):
        limit = FILAMENT_SLOTS
    sha = manifest.get('source_sha256')
    slot, how = filament_fallback_slot(state, limit)
    warnings, added = [], []
    if not original:
        if not choices_needed:
            return original, []
        effective = {'source_sha256': sha, 'mode': 'single_color',
                     'selection': _fallback_selection(state, slot)}
        warnings.append('color choices were never made for this print; every part prints '
                        f'single-color from {how}')
    elif not isinstance(original, dict) or original.get('source_sha256') != sha:
        return original, []
    else:
        effective = json.loads(json.dumps(original))
        mode = effective.get('mode')

        def past(selection):
            number = selection.get('slot') if isinstance(selection, dict) else None
            return type(number) is int and not 1 <= number <= limit

        if mode == 'single_color' and past(effective.get('selection')):
            bad = effective['selection']['slot']
            effective['selection'] = _fallback_selection(state, slot)
            warnings.append(f'the one-color choice names slot {bad}, but the printer has only '
                            f'{limit} slots; it prints from {how} instead')
        elif mode == 'per_part' and isinstance(effective.get('choices'), dict):
            choices = effective['choices']
            over = {}
            for key in sorted(choices):
                if past(choices[key]):
                    over.setdefault(choices[key]['slot'], []).append(key)
                    choices[key] = _fallback_selection(state, slot)
            for bad in sorted(over):
                names = ', '.join(_choice_label(manifest, key) for key in over[bad])
                verb = 'it prints' if len(over[bad]) == 1 else 'they print'
                warnings.append(f'slot {bad} is chosen for {names}, but the printer has only '
                                f'{limit} slots; {verb} from {how} instead')
            if selected_plates:
                from ..core.source_colors import unmapped_plate_filaments
                for plate_id, ext, plate_parts in unmapped_plate_filaments(manifest, effective,
                                                                           selected_plates):
                    def chosen(key):
                        row = choices.get(key)
                        return row if isinstance(row, dict) and row.get('slot') is not None else None
                    holders = [p for p in plate_parts if p.get('source_extruder') == ext]
                    targets = [f"part:{p.get('key')}" for p in holders
                               if chosen(f"part:{p.get('key')}") is None] or [f'filament:{ext}']
                    if all(chosen(key) is not None for key in targets):
                        continue  # filled for an earlier plate: one slot per source filament
                    # The part's own filament: a slot already chosen for this
                    # source filament (a sibling part, or its filament row).
                    own = next((chosen(f"part:{p.get('key')}") for p in holders
                                if chosen(f"part:{p.get('key')}") is not None),
                               chosen(f'filament:{ext}'))
                    # Else the base filament: the first part on the plate with a slot.
                    base = next((p for p in plate_parts
                                 if chosen(f"part:{p.get('key')}") is not None), None)
                    if own is not None:
                        picked = dict(own)
                        named = (f"slot {picked['slot']}, the slot already chosen for source "
                                 f"filament {ext}")
                    elif base is not None:
                        picked = dict(chosen(f"part:{base.get('key')}"))
                        named = (f"slot {picked['slot']}, the slot chosen for part "
                                 f"{base.get('name') or base.get('key')}")
                    else:
                        picked, named = _fallback_selection(state, slot), how
                    for key in targets:
                        choices[key] = dict(picked)
                        added.append(key)
                    names = ', '.join(_choice_label(manifest, key) for key in targets
                                      if key.startswith('part:'))
                    warnings.append(f'plate {plate_id}: source filament {ext} is used but has no '
                                    f'slot chosen{" (" + names + ")" if names else ""}; '
                                    f'it prints from {named}')
    if not warnings:
        return original, []
    effective['fallback'] = {'original': original, 'added_keys': added, 'warnings': warnings}
    return effective, warnings


def _enforce_filament_readiness(job, manifest, decisions, selected_plates,
                                slot_capacity=None, job_folder=None, via_slice=False, state=None):
    """The selected-plate readiness gate under the filament warn-and-accept ruling.

    Readiness problems that are not filament data refuse exactly as
    require_plate_readiness always has (named, in one message). Filament
    problems — unknown usage, slicer/analysis disagreement, damaged usage
    claims — become warnings handled by _enforce_filament_warnings.

    With the printer record (`state`), the #1189 fallbacks apply first: a
    used filament with no slot chosen and a slot past the printer's slot
    count are filled by filament_fallbacks and become warnings naming the
    slot they print from, so they no longer refuse. Every caller that
    slices or saves decisions passes it; without it those rows refuse as
    before.
    """
    from ..core.source_colors import (plate_readiness_problems, filament_readiness_warnings,
                                validate_plate_usage_if_present)
    fallback_warnings = []
    if state is not None:
        decisions, fallback_warnings = filament_fallbacks(manifest, decisions, selected_plates,
                                                          state, slot_capacity)
    problems = plate_readiness_problems(manifest, decisions, selected_plates,
                                        slot_capacity=slot_capacity)
    readiness_warnings = filament_readiness_warnings(manifest, decisions, selected_plates,
                                                     slot_capacity=slot_capacity)
    hard = [p for p in problems if p not in set(readiness_warnings)]
    if hard:
        raise RuntimeError('print readiness blocked: ' + '; '.join(problems))
    warnings = (fallback_warnings + readiness_warnings
                + validate_plate_usage_if_present(manifest, selected_plates))
    return _enforce_filament_warnings(job, warnings, manifest.get('source_sha256'),
                                      selected_plates, job_folder=job_folder, via_slice=via_slice)


def _whole_source_usage_warnings(manifest):
    """Filament warnings for every plate of a whole-source multi-plate job.

    No production surface writes job['selected_plates'], so a multi-plate
    source is sliced whole and the selected-plate readiness gate never runs
    for it (#1155). Its usage findings over all plates (an unknown usage
    class, a slicer/analysis disagreement, slicer usage that could not be
    validated) are still presented: they warn and the operator may accept
    them (owner ruling, catduck, 2026-11-10; #1189). Only the warning rows
    are taken. The mapping rows (a used filament with no validated slot)
    are never applied to a whole-source job: its color questions were not
    scoped per plate, so they would refuse jobs that slice correctly today.
    """
    from ..core.source_colors import filament_readiness_warnings
    usage = manifest.get('plate_usage') or {}
    if not isinstance(usage, dict) or not usage:
        return []
    return filament_readiness_warnings(manifest, {}, sorted(usage, key=str))


class ImplicitPlateSplit(Exception):
    """A single implicit plate could not merge; the caller splits by slot."""

    def __init__(self, verdict):
        super().__init__(verdict.get('reason'))
        self.verdict = verdict


def single_color_groups(job, manifest):
    """Split an exact per-part decision into one group per physical slot.

    A group is one derived plate. Part keys appear once, and a group that
    mixes slots is not representable here. Decisions stay on the source job;
    this only plans the plates.
    """
    decisions = job.get('color_decisions') or {}
    if decisions.get('mode') != 'per_part' or decisions.get('schema') != 'part_slots_v1':
        raise RuntimeError('per-part plate split requires schema part_slots_v1')
    choices = decisions.get('choices') or {}
    groups = {}
    seen = set()
    for part in manifest.get('parts') or []:
        selection = choices.get('part:' + part['key'])
        if not selection:
            raise RuntimeError('per-part decision is missing a source part')
        slot = selection.get('slot')
        if part['key'] in seen:
            raise RuntimeError('source inventory repeats a part key')
        seen.add(part['key'])
        groups.setdefault(slot, []).append(part['key'])
    if not groups:
        raise RuntimeError('per-part decision has no printable parts')
    return [{'slot': slot, 'part_keys': keys} for slot, keys in sorted(groups.items(), key=lambda row: row[0])]


def refuse_assembly_split(src, manifest, groups, st, verdict=None):
    """Refuse, before any plate is exported, a split that would cut one build object.

    The printable flag selects whole build objects, so an assembly (one
    object holding parts of several slots) cannot be divided across the
    per-slot plates; export_single_color_plate would only refuse later with
    an internal-looking error (#820). Raises a classified SliceRefusal that
    names the model's size against the bed and the way out. ``verdict`` is
    the planner's split verdict (job['plate_merge']); its reason decides
    whether the message talks about space, materials or the operator's
    own choice.
    """
    slots_by_object = {}
    for group in groups:
        keys = set(group['part_keys'])
        for part in manifest.get('parts') or []:
            if part['key'] in keys:
                slots_by_object.setdefault(str(part['object_id']), set()).add(group['slot'])
    shared = sorted(oid for oid, slots in slots_by_object.items() if len(slots) > 1)
    if not shared:
        return
    size = None
    try:
        from ..core.color_geometry import _world_bounds
        with zipfile.ZipFile(src) as archive:
            boxes = _world_bounds(archive, set(shared))
        size = [max(b[axis][1] for b in boxes) - min(b[axis][0] for b in boxes) for axis in range(3)]
    except Exception:
        size = None  # the size is a courtesy in the message, never a reason to fail differently
    bed = (st.get('bed_mm') or [None, None])[:2]
    from ..core.slice_refusal import assembly_cannot_split
    reason = (verdict or {}).get('reason')
    raise assembly_cannot_split(
        len({slot for oid in shared for slot in slots_by_object[oid]}), size,
        bed if all(bed) else None,
        f"build object(s) {','.join(shared)} hold parts of more than one slot and the plan was split "
        f"({reason or 'no reason recorded'}); an assembly cannot be split per slot. Nothing sliced",
        reason)


def _per_part_view(manifest, decisions):
    """Exact per-part decisions for the geometry exporters.

    A single_color decision is expanded in memory only; the persisted job
    decisions (and their digest) stay exactly what the user chose.
    """
    if decisions['mode'] == 'per_part':
        return decisions
    from ..core.source_colors import canonical_prompts
    selection = decisions['selection']
    return {'schema': 'part_slots_v1', 'mode': 'per_part', 'source_sha256': manifest['source_sha256'],
            'choices': {row['key']: selection for row in canonical_prompts(manifest)}}


def _part_slots(view, manifest):
    if manifest.get('painted'):
        # Painted regions pick slots per source filament, not per part. Every
        # chosen filament slot is in play; part keys map to their base filament.
        choices = view['choices']
        out = {part['key']: choices[f"filament:{part['source_extruder']}"]['slot']
               for part in manifest['parts'] if f"filament:{part['source_extruder']}" in choices}
        out.update({key: row['slot'] for key, row in choices.items() if key.startswith('filament:')})
        return out
    out = {part['key']: view['choices']['part:' + part['key']]['slot'] for part in manifest['parts']}
    # Height-based color changes (#870): their filament choices are colors of
    # the plate too, so a plate whose parts all share one slot is not a
    # single-color plate when its layer changes switch to other slots.
    out.update({key: row['slot'] for key, row in view['choices'].items() if key.startswith('filament:')})
    return out


def _support_kw(job, supports=None):
    """slice_model kwargs carrying a supports choice: the path's own, else the operator's.

    Every slicing path calls this so "Re-slice with supports" reaches Orca whichever
    branch (merged, painted, multi-tool, split, per-plate) slices the job.
    """
    decided = (job.get('support_decision') or {}).get('supports') if isinstance(job, dict) else None
    if decided is True:
        supports = True  # chosen after the slicer's warning: the latest operator word on supports
    elif supports is None and decided is False:
        supports = False
    if supports is None and isinstance(job, dict):
        # A chat edit's supports choice (#903) fills the remaining gap on
        # every path, early-return slicing paths included.
        from ..core import edit_plan as _edit_plan_sk
        supports = _edit_plan_sk.supports_override(job)
    return {'supports': supports} if supports is not None else {}


def _edit_kw(job):
    """slice_model kwargs carrying a job's chat edit settings (#903).

    Every slicing path passes these, so a layer height, infill or support
    type edit is never dropped on the merged, painted, multi-tool or
    per-plate paths (Codex P2 on #908). slice_model refuses a mirror on
    the per-part and preserve-layout paths, loudly, instead of dropping it.
    """
    from ..core import edit_plan as _edit_plan_ek
    edits = _edit_plan_ek.slice_edits(job) if isinstance(job, dict) else None
    return {'edits': edits} if edits else {}


def _job_fit(job, brim_override):
    """Operator-chosen scale/orientation for per-part plates.

    Returns (fit, supports, brim). fit is None at authored size. The same
    verified option the whole-model path honors is applied to every slot
    plate: rotation, uniform scale, and the supports/brim it was offered with.
    """
    scale = job.get('scale')
    orientation = job.get('orientation') or {}
    rotation = orientation.get('rotation')
    decided = (job.get('support_decision') or {}).get('supports')  # operator's supports choice
    decided = decided if isinstance(decided, bool) else None
    if not rotation and (scale is None or float(scale) == 1.0):
        if brim_override is None and job.get('fit_brim_policy'):
            brim_override = job['fit_brim_policy'].get('width_mm', 0)
        return None, decided, brim_override
    fit = {'scale': float(scale) if scale is not None else 1.0, 'rotation': rotation}
    if rotation:
        # A rotation that carries no supports value leaves the choice
        # unresolved (#916): bool(None) here forced supports off on every
        # per-plate slice and marked the question decided.
        _orient_supports = orientation.get('supports')
        supports = _orient_supports if isinstance(_orient_supports, bool) else decided
    else:
        supports = decided
    if brim_override is None and job.get('fit_brim_policy'):
        brim_override = job['fit_brim_policy'].get('width_mm', 0)
    if rotation and brim_override is None and orientation.get('brim'):
        brim_override = 5
    return fit, supports, brim_override


def _merge_edge(brim):
    """Bed-edge margin for merge/rearrange packing: the merge edge margin plus the job's brim.

    The single-plate arranger already adds the brim (full_size_one_plate);
    the merge planner and the per-slot rearranger must too, or a brim on a
    part packed at the bare edge margin (1 mm with layout.spacing minimum)
    crosses the bed edge.
    """
    from ..core.plate_merge import EDGE_MARGIN_MM
    return EDGE_MARGIN_MM + float(brim or 0)


def _rearranged_group_placement(src, manifest, group, bed, brim=0):
    """export_single_color_plate kwargs that place one rearranged slot plate at 100%."""
    from ..core import plate_merge
    from ..core.orca_adapter import _yaw
    keys = set(group['part_keys'])
    object_ids = {str(part['object_id']) for part in manifest['parts'] if part['key'] in keys}
    arranged = plate_merge.arrange_group(src, object_ids, bed, edge=_merge_edge(brim))
    if arranged is None:
        raise RuntimeError(f"the slot {group['slot']} parts do not fit one {bed} mm plate at 100% even "
                           'rearranged; choose split or shrink instead. Nothing sliced')
    kwargs = {'item_shifts': arranged['shifts']}
    if arranged['yaw_by_object']:
        kwargs['fit_by_object'] = {oid: {'rotation': _yaw(deg)} for oid, deg in arranged['yaw_by_object'].items()}
    return kwargs


def _plates_for(source, manifest):
    """Source plates, or the implicit single plate a plate-less 3MF holds (as slicing treats it)."""
    try:
        return plate_manifest(source)['plates']
    except RuntimeError:
        return [{'id': 1, 'name': 'Plate 1', 'status': 'planned',
                 'object_ids': sorted({int(p['object_id']) for p in manifest['parts']})}]


def full_size_one_plate(source, state, decisions=None, capacity=None, multi_tool=False, job=None):
    """A 100%-scale, one-plate rearrangement the slicer will honor exactly, or None.

    This is the cheap first fit check after the author's layout fails the bed:
    no Orca probe runs. It answers with the planner the slice path uses, so an
    arrangement it reports is one slicing realizes:

    * per-part color decisions on a 3MF slice through the merge planner
      (``plate_merge.plan``) with the rearrangement flag, which keeps the
      merge spacing, bed-edge margin and prime-tower room for multi-slot jobs;
      ``capacity``/``multi_tool`` carry the job printer's position count and
      kind into that planner, exactly as ``slice_multi_plate`` passes them;
      ``job`` is the real job, carried into the planner under the flag: the
      planner reads the author's own text from it (a listing summary asking
      for separate printing), which a synthetic flag-only job hid (P2-235);
    * a single-color or generic single-plate source slices through
      ``geometry_layout.arrange_and_export``, so it is checked with
      ``probe_single_plate_rearrange`` at the same defaults.

    Multi-plate single-color sources and anything the planners cannot place
    return None, and intake falls back to the full fit search.
    """
    from ..core import plate_merge
    from ..core.geometry_layout import probe_single_plate_rearrange
    source = Path(source)
    st = dict(state or {})
    st.setdefault('bed_mm', [BED_MM, BED_MM])
    st.setdefault('max_print_height_mm', MAX_HEIGHT_MM)
    bed = (float(st['bed_mm'][0]), float(st['bed_mm'][1]), float(st['max_print_height_mm']))
    mode = (decisions or {}).get('mode')
    if mode == 'per_part':
        if source.suffix.lower() != '.3mf':
            return None
        from ..core.source_colors import inventory
        manifest = inventory(source)
        if len(manifest.get('parts') or []) < 2:
            return None
        view = _per_part_view(manifest, decisions)
        slots = _part_slots(view, manifest)
        flagged = {**(job or {}), 'full_size_rearrange_plan': {'rearrange_plate': True}}
        verdict = plate_merge.plan(source, manifest, _plates_for(source, manifest), slots, st,
                                   bed=int(bed[0]), job=flagged,
                                   edge=_merge_edge(st.get('brim_override')),
                                   capacity=capacity, multi_tool=multi_tool)
        if verdict.get('mode') != 'merge':
            return None
        boxes = list((verdict.get('placed_bounds_mm') or {}).values())
        span = ([round(max(b[0][1] for b in boxes) - min(b[0][0] for b in boxes), 2),
                 round(max(b[1][1] for b in boxes) - min(b[1][0] for b in boxes), 2),
                 round(max(b[2][1] for b in boxes), 2)] if boxes else None)
        return {'id': 'rearrange_100', 'scale': 1.0, 'rearrange_plate': True, 'via': 'merge_planner',
                'part_count': len(verdict.get('objects_out') or []),
                'turned_parts': len(verdict.get('yaw_by_object') or {}),
                'slots': verdict.get('slots') or [], **({'size_mm': span} if span else {})}
    if source.suffix.lower() == '.3mf':
        try:
            if len(plate_manifest(source).get('plates') or []) > 1:
                return None  # multi-plate sources slice through the merge planner, which needs decisions
        except RuntimeError:
            pass
    from ..core.geometry_layout import DEFAULT_MARGIN_MM as arrange_edge
    # The same margin the slice will arrange with: layout.arrange_edge_mm plus any brim.
    plan = probe_single_plate_rearrange(source, bed=bed,
                                        margin=arrange_edge + float(st.get('brim_override') or 0))
    if plan is None:
        return None
    return {**plan, 'via': 'arrange_and_export'}


def per_part_fit_options(source, decisions, options, bed=BED_MM):
    """Keep only the fit options every per-part slot plate can honor.

    Each slot group is exported with the option applied, exactly as slicing
    will. An option that crops, lifts a part off the bed, or otherwise fails
    for any slot is dropped, so intake never offers a choice it cannot slice.
    """
    import tempfile
    from ..core.color_geometry import export_single_color_plate
    from ..core.source_colors import inventory
    manifest = inventory(source)
    view = _per_part_view(manifest, decisions)
    plates = plate_manifest(source).get('plates', []) if Path(source).suffix.lower() == '.3mf' else []
    object_plate = {str(o): plate['id'] for plate in plates for o in plate.get('object_ids', [])} if plates else {}
    groups = {}
    for part in manifest['parts']:
        p_id = object_plate.get(str(part['object_id']), 1) if plates else 1
        slot = view['choices']['part:' + part['key']]['slot']
        groups.setdefault((p_id, slot) if plates else slot, []).append(part['key'])

    def honored(fit):
        with tempfile.TemporaryDirectory(prefix='per-part-fit-') as tmp:
            for group_key, keys in groups.items():
                tag = f"p{group_key[0]}_s{group_key[1]}" if isinstance(group_key, tuple) else f"slot{group_key}"
                try:
                    export_single_color_plate(source, Path(tmp) / f'{tag}.3mf', view, keys, bed=bed, fit=fit)
                except RuntimeError:
                    return False
        return True

    scales = [s for s in options.get('scales') or [] if honored({'scale': s})]
    rotations = [r for r in options.get('rotations') or []
                 if r.get('rotation') and honored({'scale': r.get('scale'), 'rotation': r['rotation']})]
    details = []
    prior = {str(row.get('object_id')): row for row in options.get('parts') or [] if row.get('object_id')}
    try:
        with zipfile.ZipFile(source) as archive:
            for part in manifest.get('parts', []):
                box = __import__('print_handy.core.color_geometry', fromlist=['_world_bounds'])._world_bounds(
                    archive, {str(part['object_id'])})[0]
                row = {'name': part.get('name') or f"Object {part['object_id']}",
                       'extent_mm': [round(box[i][1] - box[i][0], 1) for i in range(3)]}
                # Keep the per-part fit evaluation from fit_options (#76), so a
                # part that fits no plate alone stays blocking in per-part mode.
                fit = prior.get(str(part['object_id']))
                if fit:
                    row.update({k: fit[k] for k in ('object_id', 'fits_full_size', 'best_yaw_deg',
                                                    'best_extent_mm', 'usable_mm', 'height_limit_mm',
                                                    'blocking_reason', 'orientations_checked',
                                                    'placement_mm') if k in fit})
                details.append(row)
    except (OSError, zipfile.BadZipFile, RuntimeError, KeyError):
        details = []
    return {**options, 'scales': scales, 'rotations': rotations, 'parts': details}


def _gcode_object_count(text):
    return len(set(re.findall(r'^; printing object (.+?)(?: id:\d+)?(?: copy \d+)?\s*$', text, re.M)))


def slice_multi_plate(job, job_path, src, manifest, decisions, st, brim_override):
    """Slice every object of a multi-plate 3MF. Disk only, never the printer.

    job['plate_mode'] 'merge' (default) packs all objects on one bed and
    slices once. 'separate' (or any split reason) slices one G-code per
    source plate and slot. Every split carries a reason in job['plate_merge'].
    """
    from ..core import plate_merge
    if not decisions:
        raise RuntimeError('part/color choices required; run color-plan and color-select before slicing')
    view = _per_part_view(manifest, decisions)
    slots = _part_slots(view, manifest)
    implicit = False
    try:
        plates = plate_manifest(src)['plates']
    except RuntimeError:
        # A single-plate 3MF may carry no plate assignments; it is one plate
        # holding every object. A multi-plate source must name its plates.
        if job.get('audit', {}).get('plates', 0) and int(job['audit']['plates']) > 1:
            raise
        implicit = True
        plates = [{'id': 1, 'name': 'Plate 1', 'status': 'planned',
                   'object_ids': sorted({int(p['object_id']) for p in manifest['parts']})}]
    assigned = {str(o) for plate in plates for o in plate['object_ids']}
    missing = sorted({part['object_id'] for part in manifest['parts']} - assigned, key=str)
    if missing:
        raise RuntimeError(f'multi-plate 3MF objects {",".join(missing)} belong to no plate; no slicing attempted')
    work = Path(job_path).parent
    mode = job.get('plate_mode', 'merge')
    if mode not in ('merge', 'separate'):
        raise RuntimeError("plate_mode must be 'merge' or 'separate'")
    verdict = {'mode': 'split', 'reason': 'Print parts separately was chosen', 'plates_in': len(plates),
               'objects_in': sorted({p['object_id'] for p in manifest['parts']}, key=str)}
    fit, _, _ = _job_fit(job, brim_override)
    if fit is not None:
        # Merge shifts are planned at authored size. A resized or rotated job
        # slices one fitted plate per slot; the whole model was verified to fit.
        verdict['reason'] = 'Resized per-part job: one fitted plate per slot'
    elif mode == 'merge':
        _, _, plan_brim = _job_fit(job, brim_override)
        adapter = _job_adapter(job)
        # The planner's capacity is the job printer's position count: a
        # multi_tool model's toolheads, not the configured printer's IFS
        # slots (a Creator-configured process has filament_slots=1) (P1-72).
        verdict = plate_merge.plan(src, manifest, plates, slots, st, job=job, edge=_merge_edge(plan_brim),
                                   capacity=position_limit(adapter), multi_tool=adapter.tool_based)
        if verdict['mode'] == 'merge':
            merged_slice = (_slice_merged_multi_tool if adapter.tool_based else _slice_merged)
            try:
                result = merged_slice(job, job_path, src, manifest, decisions, view, verdict, st, brim_override, work)
            except plate_merge.SplitNeeded as exc:
                verdict = {**verdict, 'mode': 'split', 'reason': str(exc)}
            else:
                return result
    job['plate_merge'] = {k: v for k, v in verdict.items()
                          if k not in ('shifts', 'placed_bounds_mm', 'yaw_by_object')}
    if _job_adapter(job).tool_based:
        # The split fallbacks below are IFS-shaped (per-slot plates finalized
        # through the native slot contract). A toolhead printer prints a
        # multi-colour job as one merged plate or not at all (#346).
        raise ImplicitPlateSplit(job['plate_merge'])
    if manifest.get('painted') and len(set(slots.values())) > 1:
        # Painted colors live inside one mesh; a per-slot split would print the
        # same body twice. Split by authored plate instead, each one multicolor.
        print('SPLIT painted 3MF into per-plate multicolor G-code: ' + str(verdict['reason']))
        return _slice_painted_plates(job, job_path, src, manifest, decisions, view, plates, st, brim_override, work)
    if implicit or len(plates) == 1:
        # One plate that cannot merge splits by slot, the legacy single-color path.
        raise ImplicitPlateSplit(job['plate_merge'])
    print('SPLIT multi-plate 3MF into per-plate G-code: ' + str(verdict['reason']))
    return _slice_per_plate(job, job_path, src, manifest, decisions, view, plates, st, brim_override, work)


PAINTED_SCALE_STEPS = (1.0, 0.95, 0.9, 0.87, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55, 0.5)


def painted_plate_scale(src, view, object_ids, ceiling=1.0, tower_mm=None):
    """Largest ladder scale <= ceiling at which this plate plus its prime tower
    fits the bed. Pure geometry measurement, no slicer, no printer.
    tower_mm is the tower width to reserve (0 for a printer that purges into its chute).

    Each step is measured, not written (#1246): the export's measure-only
    mode runs the same checks and gives the same part bounds and tower
    verdict as a written export, without building a 3MF nobody reads.
    """
    from ..core import painted_geometry
    from ..core.color_geometry import export_color_geometry
    # The exact ceiling is tried first, whatever its size: the confirm
    # prompt's scaling records arbitrary cumulative scales (99%, 105%),
    # and printing the nearest ladder step instead would change the size
    # the operator approved. The ladder below is the fallback for a
    # ceiling that does not fit as-is; it only ever shrinks.
    steps = [ceiling] + [s for s in PAINTED_SCALE_STEPS if s < ceiling - 1e-9]
    with painted_geometry.decode_scope():
        for scale in steps:
            if scale > ceiling + 1e-9:
                continue
            try:
                _, geometry = export_color_geometry(src, None, view, object_ids=object_ids, scale=scale,
                                                    tower_mm=tower_mm)
            except RuntimeError:
                continue
            if all(0 <= ax[0] and ax[1] <= BED_LIMIT_MM for row in geometry['parts'] for ax in row['bounds_mm'][:2]):
                return scale
    return None


BED_LIMIT_MM = BED_MM


def _painted_tower_mm(state):
    """The tower width a painted-plate fit reserves for this printer: None keeps the configured one."""
    if not state:
        return None
    plan = purge.resolve(state=state)
    return 0.0 if plan.zmod and plan.towerless else None


def painted_plate_layout(src, view, object_ids, scale, tower_mm=None):
    """Measured layout of one painted plate at ``scale``, or None when unmeasurable.

    Display only, for fit-refusal wording (#839): the parts' extent and the
    prime tower footprint the fit reserves (tower_mm None when the printer
    needs no tower, #832). A failed measurement is not a fit verdict, so
    every failure returns None and the caller words the refusal without
    measured numbers.
    """
    from ..core import painted_geometry
    from ..core.color_geometry import export_color_geometry
    try:
        # Measure towerless (#839): the part bounds do not depend on
        # tower placement, but the export raises when no tower position
        # clears the parts — exactly the no-fit case this measurement
        # exists to explain. The tower size is reported from tower_mm
        # below, not from a placement that may not exist. Measured, not
        # written (#1246).
        with painted_geometry.decode_scope():
            _, geometry = export_color_geometry(src, None, view,
                                                object_ids=object_ids, scale=scale, tower_mm=0.0)
    except Exception:
        return None
    bounds = geometry.get('build_bounds_mm') or []
    if len(bounds) < 2:
        return None
    try:
        extent = [round(float(bounds[a][1]) - float(bounds[a][0]), 1) for a in range(2)]
    except (TypeError, ValueError, IndexError):
        return None
    tower = None
    if tower_mm is None:
        from ..core.color_geometry import effective_tower_mm
        tower = float(effective_tower_mm())
    elif float(tower_mm) > 0:
        tower = float(tower_mm)
    return {'extent_mm': extent, 'tower_mm': tower, 'scale': float(scale)}


def painted_fit(src, view, plates, ceiling=1.0, tower_mm=None):
    """One shared scale for every authored plate of a painted source.

    The whole fit runs in one decode scope and one inventory scope, so the
    source is read, inventoried and parsed once for every plate and step.
    """
    from ..core import painted_geometry
    from ..core.source_colors import inventory_scope
    with painted_geometry.decode_scope(), inventory_scope():
        return _painted_fit(src, view, plates, ceiling, tower_mm)


def _painted_fit(src, view, plates, ceiling, tower_mm):
    scale, blocking = ceiling, None
    for plate in plates:
        ids = {str(o) for o in plate['object_ids']}
        fitted = painted_plate_scale(src, view, ids, scale, tower_mm)
        if fitted is None:
            result = {'scale': None, 'blocking_plate': plate['id'], 'plates': len(plates)}
            layout = painted_plate_layout(src, view, ids, PAINTED_SCALE_STEPS[-1], tower_mm)
            if layout is not None:
                result['blocking_layout'] = layout
            return result
        if fitted < scale:
            scale, blocking = fitted, plate['id']
    return {'scale': scale, 'blocking_plate': blocking, 'plates': len(plates)}


def painted_fit_for_job(src, decisions, state=None):
    """Intake helper: shared painted-plate scale for an exact-source decision.

    The whole fit runs in one inventory scope (#1153): every geometry export
    it tries (up to one per ladder step per plate) and the plate manifest
    ask for the source's color manifest, and inside the scope a source whose
    bytes hash the same is inventoried once. Each caller still gets its own
    copy, and nothing outlives the call.
    """
    from ..core.source_colors import inventory_scope
    with inventory_scope():
        return _painted_fit_for_job(src, decisions, state)


def _painted_fit_for_job(src, decisions, state=None):
    from ..core.source_colors import inventory
    manifest = inventory(src)
    view = _per_part_view(manifest, decisions)
    try:
        plates = plate_manifest(src)['plates']
    except RuntimeError:
        plates = [{'id': 1, 'object_ids': sorted({str(p['object_id']) for p in manifest['parts']})}]
    plates = [p for p in plates if any(str(o) in {str(x['object_id']) for x in manifest['parts']} for o in p['object_ids'])]
    return painted_fit(src, view, plates, tower_mm=_painted_tower_mm(state))


def _slice_painted_plates(job, job_path, src, manifest, decisions, view, plates, st, brim_override, work):
    """One multicolor G-code per authored plate of a triangle-painted 3MF.

    Paint is remapped to the chosen slots inside every mesh, so no plate ever
    prints a body in the wrong color. Every plate uses one scale so parts
    still fit together: job['scale'] caps it (default full size), otherwise it
    is the largest ladder scale at which every plate and its prime tower fit.
    """
    import uuid
    from ..core.orca_adapter import slice_model
    source_sha = file_sha256(src); decision_sha = decisions_digest(decisions)
    fit = painted_fit(src, view, plates, float(job.get('scale') or 1.0), tower_mm=_painted_tower_mm(st))
    scale = fit['scale']
    if scale is None:
        raise RuntimeError(f"painted plate {fit['blocking_plate']} does not fit the bed with a prime tower at 50% or larger; no slicing attempted")
    artifacts = []
    for plate in plates:
        ids = {str(o) for o in plate['object_ids']}
        keys = [p['key'] for p in manifest['parts'] if str(p['object_id']) in ids]
        if not keys:
            continue
        slice_dir = work / 'sliced' / 'orca' / uuid.uuid4().hex
        out, provenance, configs = slice_model(src, slice_dir, st, brim_override, color_decisions=view,
                                               object_ids=ids, scale=None if scale == 1.0 else scale,
                                               **_support_kw(job), **_edit_kw(job))
        geometry = provenance.get('geometry') or {}
        if sorted(r['part_key'] for r in geometry.get('parts', [])) != sorted(keys):
            raise RuntimeError(f"painted plate {plate['id']} dropped or duplicated a source part")
        check = verify_gcode_offline(out, st, _job_adapter(job))
        tools = provenance.get('tool_mapping') or {}
        artifacts.append({'plate_id': plate['id'], 'slot': sorted(set(tools.values())),
                          'binding_key': f"{plate['id']}:painted", 'part_keys': keys,
                          'scale': scale, 'gcode': str(out), 'offline_check': check,
                          'estimate_s': gcode_estimate_s(Path(out).read_text(errors='ignore')),
                          'provenance': provenance, 'slice_config': [str(p) for p in configs]})
    if not artifacts:
        raise RuntimeError('painted source produced no plates')
    latest = json.loads(Path(job_path).read_text())
    if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha or _recipe_changed(job, latest, _recorded_printer_id(job)):
        raise RuntimeError('source, decisions or slice recipe changed during slicing; artifact not made ready')
    job['derived_plates'] = artifacts
    job['plate_merge'] = {'mode': 'split', 'painted': True, 'plates_in': len(plates), 'plates_out': len(artifacts),
                          'reason': 'painted multicolor plates sliced separately, one multicolor G-code each',
                          'scales': {str(a['plate_id']): a['scale'] for a in artifacts}, 'fits_bed': True,
                          'bed_mm': int(st['bed_mm'][0])}
    job['artifact_binding'] = {**_recipe_binding(job), 'source_sha256': source_sha, 'decisions_sha256': decision_sha,
                               'gcode_sha256': {a['binding_key']: file_sha256(a['gcode']) for a in artifacts}}
    job.update({'gcode': artifacts[0]['gcode'], 'gcode_verified': True,
                'sliced_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
    write_job(work, job)
    for a in artifacts:
        print(f"SLICED painted plate {a['plate_id']} at {round(a['scale'] * 100)}% (not uploaded): {a['gcode']}")
    return [a['gcode'] for a in artifacts]


def _slice_merged(job, job_path, src, manifest, decisions, view, verdict, st, brim_override, work):
    import uuid
    from ..core import plate_merge
    from ..core.orca_adapter import slice_model
    source_sha = file_sha256(src); decision_sha = decisions_digest(decisions)
    slice_dir = work / 'sliced' / 'orca' / uuid.uuid4().hex
    slots = sorted(set(_part_slots(view, manifest).values()))
    yaw_by_object = verdict.get('yaw_by_object') or {}
    if not isinstance(yaw_by_object, dict):
        raise RuntimeError('merge plan yaw_by_object must be a mapping of object id to degrees')
    fit_by_object = {}
    if yaw_by_object:
        # A recorded merge turn (#221 R2b) is realized only as the
        # exporters' per-object fit composed with the plan's recorded
        # shifts — the proved placement. Fail closed on anything else
        # instead of slicing the objects as authored.
        if not verdict.get('shifts'):
            raise RuntimeError('merge plan records a turn without the recorded shifts that realize it; '
                               'refusing to slice it as authored')
        from ..core.orca_adapter import _yaw
        unknown_yaw = sorted(set(yaw_by_object) - {str(p['object_id']) for p in manifest['parts']}, key=str)
        if unknown_yaw:
            raise RuntimeError(f"merge plan records a turn for unknown object(s) {','.join(unknown_yaw)}")
        for oid in sorted(yaw_by_object, key=str):
            try:
                value = float(yaw_by_object[oid])
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f'merge plan turn for object {oid} must be numeric, '
                                   f'got {yaw_by_object[oid]!r}') from exc
            if not math.isfinite(value) or not 0 < value <= 90:
                raise RuntimeError(f'merge plan turn must be between 0 and 90 degrees, got {value!r}')
            fit_by_object[str(oid)] = {'rotation': _yaw(value)}
    if len(slots) == 1 and not manifest.get('painted'):
        from ..core.color_geometry import export_single_color_plate
        derived = work / 'derived'; derived.mkdir(exist_ok=True)
        plate = derived / 'merged.3mf'
        report = export_single_color_plate(src, plate, view, [p['key'] for p in manifest['parts']], bed=int(st['bed_mm'][0]),
                                           slot_colors=st.get('filament_slots') or {}, item_shifts=verdict['shifts'],
                                           **({'fit_by_object': fit_by_object} if fit_by_object else {}))
        if fit_by_object:
            # Re-read the derived plate: every turned object must have
            # landed at its rotated authored bounds plus its recorded
            # shift, with the rotated extents — the shared split-plate
            # proof (#221 R2) applied to the merged plate.
            from ..core.color_geometry import _world_bounds as _bounds_of
            from ..core.color_geometry import _world_points as _points_of
            from ..core.orca_adapter import _rotate as _rotate_point
            shifts = {str(k): v for k, v in verdict['shifts'].items()}
            with zipfile.ZipFile(src) as archive:
                turned_points = {oid: _points_of(archive, {oid}) for oid in fit_by_object}
            with zipfile.ZipFile(plate) as archive:
                landed_boxes = {oid: _bounds_of(archive, {oid})[0] for oid in fit_by_object}
            for oid, entry in fit_by_object.items():
                rotated = [_rotate_point(point, entry['rotation']) for point in turned_points[oid]]
                base_min = [min(p[a] for p in rotated) for a in range(3)]
                base_extent = [max(p[a] for p in rotated) - base_min[a] for a in range(3)]
                landed = landed_boxes[oid]
                landed_extent = [landed[a][1] - landed[a][0] for a in range(3)]
                values = shifts[oid]
                if any(abs(landed[a][0] - (base_min[a] + float(values[a]))) > 2e-3 for a in range(3)) \
                        or any(abs(got - want) > 2e-3 for got, want in zip(landed_extent, base_extent)):
                    raise RuntimeError(f'merged plate did not land at the recorded turn for object {oid}; '
                                       'refusing to slice it as turned')
        out, provenance, configs = slice_model(plate, slice_dir, st, brim_override, preserve_layout=True, filament_slot=slots[0],
                                               **_support_kw(job), **_edit_kw(job))
        material = st.get('filament_slots', {}).get(str(slots[0]), {}).get('material')
        if not material:
            raise RuntimeError(f'merged plate physical IFS slot {slots[0]} has no verified material')
        finalize_multicolor(out, None, st, {'0': slots[0]})
        placed_ids = sorted(report['object_ids'], key=str)
        extra = {'plate': str(plate), 'plate_report': report}
    else:
        # Painted single-color choices still go through the paint-preserving
        # exporter, so pass the per-part view (it is exactly the user's choice).
        out, provenance, configs = slice_model(src, slice_dir, st, brim_override,
                                               color_decisions=view if manifest.get('painted') else decisions,
                                               item_shifts=verdict['shifts'], **_support_kw(job), **_edit_kw(job),
                                               **({'fit_by_object': fit_by_object} if fit_by_object else {}))
        geometry = provenance.get('geometry') or {}
        placed_keys = sorted(row['part_key'] for row in geometry.get('parts', []))
        if placed_keys != sorted(p['key'] for p in manifest['parts']):
            raise RuntimeError('merged geometry dropped or duplicated a source part')
        if fit_by_object:
            # This route has no derived plate file to re-read: the
            # multicolor exporter's report bounds are computed from the
            # exact vertices it wrote, so they are the strongest available
            # proof — every turned object's placed bounds must equal its
            # rotated authored bounds plus its recorded shift (#221 R2b).
            from ..core.color_geometry import _world_points as _points_of
            from ..core.orca_adapter import _rotate as _rotate_point
            object_of_key = {p['key']: str(p['object_id']) for p in manifest['parts']}
            placed_by_object = _placed_bounds_by_object(geometry, object_of_key)
            shifts = {str(k): v for k, v in verdict['shifts'].items()}
            with zipfile.ZipFile(src) as archive:
                turned_points = {oid: _points_of(archive, {oid}) for oid in fit_by_object}
            for oid, entry in fit_by_object.items():
                rotated = [_rotate_point(point, entry['rotation']) for point in turned_points[oid]]
                base_min = [min(p[a] for p in rotated) for a in range(3)]
                base_extent = [max(p[a] for p in rotated) - base_min[a] for a in range(3)]
                landed = placed_by_object[oid]
                landed_extent = [landed[a][1] - landed[a][0] for a in range(3)]
                values = shifts[oid]
                if any(abs(landed[a][0] - (base_min[a] + float(values[a]))) > 2e-3 for a in range(3)) \
                        or any(abs(got - want) > 2e-3 for got, want in zip(landed_extent, base_extent)):
                    raise RuntimeError(f'merged geometry did not land at the recorded turn for object {oid}; '
                                       'refusing to slice it as turned')
        placed_ids = sorted({p['object_id'] for p in manifest['parts'] if p['key'] in placed_keys}, key=str)
        extra = {'geometry_report': geometry}
    text = Path(out).read_text(errors='ignore')
    estimate = gcode_estimate_s(text)
    if estimate is not None and estimate > plate_merge.MAX_MERGED_S:
        raise plate_merge.SplitNeeded(f'merged estimate {human_dur(estimate)} exceeds the {plate_merge.MAX_MERGED_S / 3600:g} h limit')
    if placed_ids != verdict['objects_in']:
        raise RuntimeError('merged output does not account for every source object')
    labelled = _gcode_object_count(text)
    if labelled and labelled != len(manifest['parts']):
        raise RuntimeError(f'merged G-code prints {labelled} objects but the source has {len(manifest["parts"])}')
    check = verify_gcode_offline(out, st, _job_adapter(job))
    latest = json.loads(Path(job_path).read_text())
    if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha or _recipe_changed(job, latest, _recorded_printer_id(job)):
        raise RuntimeError('source, decisions or slice recipe changed during slicing; artifact not made ready')
    summary = {'mode': 'merge', 'reason': None, 'plates_in': verdict['plates_in'], 'objects_in': len(verdict['objects_in']),
               'objects_out': len(placed_ids), 'parts_in': len(manifest['parts']), 'bed_mm': verdict['bed_mm'],
               'spacing_mm': verdict['spacing_mm'], 'fits_bed': True, 'slots': slots,
               'placed_bounds_mm': verdict['placed_bounds_mm'], 'estimate_s': estimate,
               'estimate': human_dur(estimate) if estimate else None, 'gcode_objects_labelled': labelled or None,
               **({'yaw_by_object': yaw_by_object} if yaw_by_object else {})}
    job.pop('derived_plates', None); job.pop('refused_plates', None); job.pop('multipart_plan', None)
    job['plate_merge'] = summary
    job['artifact_binding'] = {**_recipe_binding(job), 'source_sha256': source_sha, 'decisions_sha256': decision_sha, 'gcode_sha256': file_sha256(out)}
    job.update({'slice_config': [str(p) for p in configs], 'gcode': str(out), 'provenance': provenance,
                'gcode_verified': True, 'offline_check': check, 'merged': extra,
                'sliced_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
    write_job(work, job)
    print(f"SLICED merged {summary['objects_in']} objects from {summary['plates_in']} plates onto one bed (not uploaded): {out}")
    return out


def _slice_merged_multi_tool(job, job_path, src, manifest, decisions, view, verdict, st, brim_override, work):
    """One merged G-code for a multi_tool printer (#346).

    The merged-colour exporter runs in identity mode: slicer tool T(n-1) is
    toolhead n, the position the operator picked for that colour. The picks
    are checked against the adapter's identity-only tool-mapping contract
    unchanged, and Orca slices through the model's own multi-extruder
    templates — there is no IFS finalize step on a toolhead printer.
    """
    import uuid
    from ..core import plate_merge
    from ..core.orca_adapter import slice_model
    adapter = _job_adapter(job)
    source_sha = file_sha256(src); decision_sha = decisions_digest(decisions)
    slice_dir = work / 'sliced' / 'orca' / uuid.uuid4().hex
    slots = sorted(set(_part_slots(view, manifest).values()))
    yaw_by_object = verdict.get('yaw_by_object') or {}
    if not isinstance(yaw_by_object, dict):
        raise RuntimeError('merge plan yaw_by_object must be a mapping of object id to degrees')
    fit_by_object = {}
    if yaw_by_object:
        if not verdict.get('shifts'):
            raise RuntimeError('merge plan records a turn without the recorded shifts that realize it; '
                               'refusing to slice it as authored')
        from ..core.orca_adapter import _yaw
        unknown_yaw = sorted(set(yaw_by_object) - {str(p['object_id']) for p in manifest['parts']}, key=str)
        if unknown_yaw:
            raise RuntimeError(f"merge plan records a turn for unknown object(s) {','.join(unknown_yaw)}")
        for oid in sorted(yaw_by_object, key=str):
            try:
                value = float(yaw_by_object[oid])
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f'merge plan turn for object {oid} must be numeric, '
                                   f'got {yaw_by_object[oid]!r}') from exc
            if not math.isfinite(value) or not 0 < value <= 90:
                raise RuntimeError(f'merge plan turn must be between 0 and 90 degrees, got {value!r}')
            fit_by_object[str(oid)] = {'rotation': _yaw(value)}
    out, provenance, configs = slice_model(src, slice_dir, st, brim_override,
                                           color_decisions=view if manifest.get('painted') else decisions,
                                           item_shifts=verdict['shifts'], **_support_kw(job), **_edit_kw(job),
                                           **({'fit_by_object': fit_by_object} if fit_by_object else {}))
    mapping = {str(tool): int(slot) for tool, slot in (provenance.get('tool_mapping') or {}).items()}
    # The recorded mapping must pass the adapter's identity contract exactly
    # as recorded: tool T(n-1) prints with toolhead n, nothing remapped (#346).
    adapter.normalize_tool_mapping(mapping, st)
    geometry = provenance.get('geometry') or {}
    placed_keys = sorted(row['part_key'] for row in geometry.get('parts', []))
    if placed_keys != sorted(p['key'] for p in manifest['parts']):
        raise RuntimeError('merged geometry dropped or duplicated a source part')
    if fit_by_object:
        from ..core.color_geometry import _world_points as _points_of
        from ..core.orca_adapter import _rotate as _rotate_point
        object_of_key = {p['key']: str(p['object_id']) for p in manifest['parts']}
        placed_by_object = _placed_bounds_by_object(geometry, object_of_key)
        shifts = {str(k): v for k, v in verdict['shifts'].items()}
        with zipfile.ZipFile(src) as archive:
            turned_points = {oid: _points_of(archive, {oid}) for oid in fit_by_object}
        for oid, entry in fit_by_object.items():
            rotated = [_rotate_point(point, entry['rotation']) for point in turned_points[oid]]
            base_min = [min(p[a] for p in rotated) for a in range(3)]
            base_extent = [max(p[a] for p in rotated) - base_min[a] for a in range(3)]
            landed = placed_by_object[oid]
            landed_extent = [landed[a][1] - landed[a][0] for a in range(3)]
            values = shifts[oid]
            if any(abs(landed[a][0] - (base_min[a] + float(values[a]))) > 2e-3 for a in range(3)) \
                    or any(abs(got - want) > 2e-3 for got, want in zip(landed_extent, base_extent)):
                raise RuntimeError(f'merged geometry did not land at the recorded turn for object {oid}; '
                                   'refusing to slice it as turned')
    placed_ids = sorted({p['object_id'] for p in manifest['parts'] if p['key'] in placed_keys}, key=str)
    text = Path(out).read_text(errors='ignore')
    estimate = gcode_estimate_s(text)
    if estimate is not None and estimate > plate_merge.MAX_MERGED_S:
        raise plate_merge.SplitNeeded(f'merged estimate {human_dur(estimate)} exceeds the {plate_merge.MAX_MERGED_S / 3600:g} h limit')
    if placed_ids != verdict['objects_in']:
        raise RuntimeError('merged output does not account for every source object')
    labelled = _gcode_object_count(text)
    if labelled and labelled != len(manifest['parts']):
        raise RuntimeError(f'merged G-code prints {labelled} objects but the source has {len(manifest["parts"])}')
    check = verify_gcode_offline(out, st, _job_adapter(job))
    latest = json.loads(Path(job_path).read_text())
    if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha or _recipe_changed(job, latest, _recorded_printer_id(job)):
        raise RuntimeError('source, decisions or slice recipe changed during slicing; artifact not made ready')
    summary = {'mode': 'merge', 'reason': None, 'plates_in': verdict['plates_in'], 'objects_in': len(verdict['objects_in']),
               'objects_out': len(placed_ids), 'parts_in': len(manifest['parts']), 'bed_mm': verdict['bed_mm'],
               'spacing_mm': verdict['spacing_mm'], 'fits_bed': True, 'slots': slots,
               'placed_bounds_mm': verdict['placed_bounds_mm'], 'estimate_s': estimate,
               'estimate': human_dur(estimate) if estimate else None, 'gcode_objects_labelled': labelled or None,
               **({'yaw_by_object': yaw_by_object} if yaw_by_object else {})}
    job.pop('derived_plates', None); job.pop('refused_plates', None); job.pop('multipart_plan', None)
    job['plate_merge'] = summary
    job['tool_mapping'] = mapping
    job['artifact_binding'] = {**_recipe_binding(job), 'source_sha256': source_sha, 'decisions_sha256': decision_sha, 'gcode_sha256': file_sha256(out)}
    job.update({'slice_config': [str(p) for p in configs], 'gcode': str(out), 'provenance': provenance,
                'gcode_verified': True, 'offline_check': check, 'merged': {'geometry_report': geometry},
                'sliced_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
    write_job(work, job)
    print(f"SLICED merged {summary['objects_in']} objects from {summary['plates_in']} plates onto one bed (not uploaded): {out}")
    return out


def _slice_per_plate(job, job_path, src, manifest, decisions, view, plates, st, brim_override, work):
    import uuid
    fit, supports, brim_override = _job_fit(job, brim_override)
    from ..core.color_geometry import export_single_color_plate
    from ..core.orca_adapter import slice_model
    object_plate = {str(o): plate['id'] for plate in plates for o in plate['object_ids']}
    if fit is not None and len(plates) <= 1:
        # The chosen fit was verified for the whole model on one bed, so the
        # source plate boundaries no longer split it: one plate per slot.
        object_plate = {key: 1 for key in object_plate}
    slots = _part_slots(view, manifest)
    groups = {}
    for part in manifest['parts']:
        groups.setdefault((object_plate[str(part['object_id'])], slots[part['key']]), []).append(part['key'])
    derived = work / 'derived'; derived.mkdir(exist_ok=True)
    source_sha = file_sha256(src); decision_sha = decisions_digest(decisions)
    single_plate = len({plate for plate, _ in groups}) == 1
    artifacts = []
    refused = []
    plate_scales = job.get('plate_scales') or {}
    for (plate_id, slot), keys in sorted(groups.items()):
        current_fit = fit
        if str(plate_id) in plate_scales:
            psc = float(plate_scales[str(plate_id)])
            current_fit = {'scale': psc} if psc < 1.0 else None
        plate = derived / f'plate{plate_id}_slot{slot}.3mf'
        try:
            if not st.get('filament_slots', {}).get(str(slot), {}).get('material'):
                raise RuntimeError(f'derived plate physical IFS slot {slot} has no verified material')
            report = export_single_color_plate(src, plate, view, keys, bed=int(st['bed_mm'][0]), slot_colors=st.get('filament_slots') or {}, **({'fit': current_fit} if current_fit is not None else {}))
            slice_dir = work / 'sliced' / 'orca' / uuid.uuid4().hex
            out, provenance, configs = slice_model(plate, slice_dir, st, brim_override, preserve_layout=True, filament_slot=slot, **_support_kw(job, supports), **_edit_kw(job))
            finalize_multicolor(out, None, st, {'0': slot})
            check = verify_gcode_offline(out, st, _job_adapter(job))
        except RuntimeError as exc:
            # One plate failing a safety guard (e.g. an off-bed move) must not discard the
            # other plates that sliced and verified cleanly. The failed plate is recorded,
            # never published, and never launchable; it needs its own fix and reslice.
            refused.append({'slot': slot, 'plate_id': plate_id, 'binding_key': f'{plate_id}:{slot}',
                            'part_keys': keys, 'reason': ' '.join(str(exc).split())[:500]})
            print(f'REFUSED plate {plate_id} slot {slot}: {" ".join(str(exc).split())[:300]}')
            continue
        artifacts.append({'slot': slot, 'plate_id': plate_id, 'binding_key': f'{plate_id}:{slot}', 'part_keys': keys,
                          'plate': str(plate), 'gcode': str(out), 'offline_check': check, 'plate_report': report,
                          'provenance': provenance, 'slice_config': [str(path) for path in configs]})
    if not artifacts:
        raise RuntimeError('every derived plate was refused: ' + '; '.join(
            f"plate {r['plate_id']} slot {r['slot']}: {r['reason']}" for r in refused))
    covered = sorted(k for row in artifacts + refused for k in row['part_keys'])
    if covered != sorted(p['key'] for p in manifest['parts']):
        raise RuntimeError('per-plate output does not account for every source part')
    latest = json.loads(Path(job_path).read_text())
    if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha or _recipe_changed(job, latest, _recorded_printer_id(job)):
        raise RuntimeError('source, decisions or slice recipe changed during slicing; artifacts not made ready')
    job['derived_plates'] = artifacts
    if refused:
        job['refused_plates'] = refused
    else:
        job.pop('refused_plates', None)
    job['artifact_binding'] = {**_recipe_binding(job), 'source_sha256': source_sha, 'decisions_sha256': decision_sha,
                               'gcode_sha256': {row['binding_key']: file_sha256(row['gcode']) for row in artifacts}}
    job.update({'gcode': artifacts[0]['gcode'], 'gcode_verified': True,
                'sliced_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
    write_job(work, job)
    print('SLICED separate plates (not uploaded): ' + ', '.join(row['gcode'] for row in artifacts))
    return [row['gcode'] for row in artifacts]


def _slice_full_size_split(job, job_path, src, manifest, decisions, st):
    """Slice the exact intake-approved authored-size part groups, disk only."""
    import uuid
    from ..core.color_geometry import export_single_color_plate
    from ..core.orca_adapter import UP_AXES, _check_rotation, _matmul, _yaw, slice_model
    plan = job.get('full_size_split_plan') or {}
    plates = plan.get('plates') or []
    expected = {part['key'] for part in manifest.get('parts') or []}
    flattened = [key for group in plates for key in group]
    if len(flattened) < 2 or len(flattened) != len(set(flattened)) or set(flattened) != expected:
        raise RuntimeError('full-size split plan does not cover every source part exactly once')
    yaw_by_object = plan.get('yaw_by_object') or {}
    if not isinstance(yaw_by_object, dict):
        raise RuntimeError('full-size split plan yaw_by_object must be a mapping of object id to degrees')
    placement_by_object = plan.get('placement_by_object') or {}
    if not isinstance(placement_by_object, dict):
        raise RuntimeError('full-size split plan placement_by_object must be a mapping of object id to [x, y] mm')
    reorientation_by_object = plan.get('reorientation_by_object') or {}
    if not isinstance(reorientation_by_object, dict):
        raise RuntimeError('full-size split plan reorientation_by_object must be a mapping of '
                           'object id to a reorientation record')
    shifts_by_plate = plan.get('shifts_by_plate') or []
    if not isinstance(shifts_by_plate, list) or len(shifts_by_plate) not in (0, len(plates)):
        raise RuntimeError('full-size split plan shifts_by_plate must be a list parallel to plates '
                           'of per-object [dx, dy, dz] shifts (or None for a plate sliced as authored)')
    key_to_object = {part['key']: str(part['object_id']) for part in manifest.get('parts') or []}
    unknown_yaw = sorted(set(yaw_by_object) - set(key_to_object.values()))
    if unknown_yaw:
        raise RuntimeError(f"full-size split plan records a turn for unknown object(s) {','.join(unknown_yaw)}")
    unknown_placement = sorted(set(placement_by_object) - set(key_to_object.values()))
    if unknown_placement:
        raise RuntimeError(f"full-size split plan records a placement for unknown object(s) {','.join(unknown_placement)}")
    unknown_reorientation = sorted(set(reorientation_by_object) - set(key_to_object.values()))
    if unknown_reorientation:
        raise RuntimeError('full-size split plan records a reorientation for unknown '
                           f"object(s) {','.join(unknown_reorientation)}")
    unknown_shifts = sorted({oid for entry in shifts_by_plate if isinstance(entry, dict)
                             for oid in entry} - set(key_to_object.values()))
    if unknown_shifts:
        raise RuntimeError(f"full-size split plan records shifts for unknown object(s) {','.join(unknown_shifts)}")
    bed_xy = [float(v) for v in (st.get('bed_mm') or [])[:2]]
    bed_x = bed_xy[0] if bed_xy else float(st['bed_mm'][0])
    bed_y = bed_xy[1] if len(bed_xy) > 1 else bed_x
    view = _per_part_view(manifest, decisions)
    slots = sorted(set(_part_slots(view, manifest).values()))
    if len(slots) != 1:
        raise RuntimeError('full-size split plan requires one physical filament slot')
    slot = slots[0]
    if not (st.get('filament_slots') or {}).get(str(slot), {}).get('material'):
        raise RuntimeError(f'full-size split physical IFS slot {slot} has no verified material')
    work = Path(job_path).parent
    derived = work / 'derived'
    derived.mkdir(exist_ok=True)
    source_sha = file_sha256(src)
    decision_sha = decisions_digest(decisions)
    artifacts = []
    for index, keys in enumerate(plates, 1):
        plate = derived / f'full_size_split_{index}.3mf'
        turned = {key_to_object[key] for key in keys if key_to_object[key] in yaw_by_object}
        placed = {key_to_object[key] for key in keys if key_to_object[key] in placement_by_object}
        reoriented = {key_to_object[key] for key in keys
                      if key_to_object[key] in reorientation_by_object}
        fit, yaw_deg, placement_mm = None, None, None
        reorientation, supports = None, None
        shared_yaw_by_object = None
        shared_reorientation_by_object = None
        if turned:
            if len(turned) == 1 and {key_to_object[key] for key in keys} == turned:
                # The exporter turns a whole derived plate rigidly about one
                # origin, so a recorded turn for that part alone is realized
                # through the whole-plate fit (#76).
                yaw_deg = float(yaw_by_object[turned.pop()])
                if not math.isfinite(yaw_deg) or not 0 < yaw_deg <= 90:
                    raise RuntimeError(f'full-size split plan turn must be between 0 and 90 degrees, got {yaw_deg!r}')
                rad = math.radians(yaw_deg)
                c, s = math.cos(rad), math.sin(rad)
                fit = {'scale': 1.0, 'rotation': [c, -s, 0.0, s, c, 0.0, 0.0, 0.0, 1.0]}
            else:
                # A turn recorded for a shared plate (#221 R2) is realizable
                # only as the exporter's per-object fit composed with the
                # plate's recorded shifts — the proved shared placement.
                # Without those shifts there is nothing that realizes the
                # turn, so refuse instead of slicing the plate as authored.
                if not shifts_by_plate or shifts_by_plate[index - 1] is None:
                    raise RuntimeError('full-size split plan records a turn for a plate that is not '
                                       'that part alone, without the recorded shifts that realize '
                                       'a shared turn; refusing to slice it as authored')
                shared_yaw_by_object = {}
                for oid in sorted(turned):
                    try:
                        value = float(yaw_by_object[oid])
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError(f'full-size split plan turn for object {oid} must be '
                                           f'numeric, got {yaw_by_object[oid]!r}') from exc
                    if not math.isfinite(value) or not 0 < value <= 90:
                        raise RuntimeError('full-size split plan turn must be between 0 and 90 '
                                           f'degrees, got {value!r}')
                    shared_yaw_by_object[oid] = value
        if placed:
            # Same realizability rule as a turn: the exporter places a whole
            # derived plate rigidly, so a recorded placement is only
            # realizable for that part alone (#221).
            if len(placed) != 1 or {key_to_object[key] for key in keys} != placed:
                raise RuntimeError('full-size split plan records a placement for a plate that is not '
                                   'that part alone; refusing to slice it as authored')
            raw = placement_by_object[placed.pop()]
            if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                raise RuntimeError(f'full-size split plan placement must be an [x, y] pair, got {raw!r}')
            try:
                placement_mm = [float(raw[0]), float(raw[1])]
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f'full-size split plan placement must be numeric, got {raw!r}') from exc
            if (not all(math.isfinite(v) for v in placement_mm)
                    or not 0 <= placement_mm[0] < bed_x or not 0 <= placement_mm[1] < bed_y):
                raise RuntimeError(f'full-size split plan placement {placement_mm!r} is outside the '
                                   f'{bed_x:g} x {bed_y:g} mm bed; refusing to slice it as authored')
            fit = {**(fit or {'scale': 1.0}), 'placement_mm': placement_mm}
        if reoriented:
            # Every reorientation record is validated exactly as in R1: a
            # known axis, a bed turn in [0, 90] degrees (a cardinal turn, or
            # a diagonal one when no cardinal turn fit — #221 diagonal), a
            # finite rigid matrix that is
            # exactly that face change composed with the turn, and boolean
            # supports/brim decisions. A reorientation is always the object's
            # only transform record — the matrix already contains its bed
            # turn, so a yaw entry for the same object refuses. What R3 adds
            # is only how a valid record is realized beyond the solo plate:
            # alone, it goes through the exporter's whole-plate fit, with a
            # placement recorded for that same object composed into the same
            # fit (the proved exclusion-avoiding landing, #221 R3); on a
            # shared plate it goes through the exporter's per-object fit
            # channel composed with the plate's recorded shifts — the proved
            # shared placement — and without those shifts there is nothing
            # that realizes the rotation, so refuse instead of slicing the
            # plate as authored.
            validated_reorientations = {}
            for reoriented_oid in sorted(reoriented):
                if reoriented_oid in yaw_by_object:
                    raise RuntimeError('full-size split plan records a reorientation for an object '
                                       'that also records a turn; refusing to slice it as authored')
                record = reorientation_by_object[reoriented_oid]
                if not isinstance(record, dict):
                    raise RuntimeError('full-size split plan reorientation record must be a '
                                       f'mapping, got {record!r}')
                axis = record.get('axis')
                if axis not in UP_AXES:
                    raise RuntimeError('full-size split plan reorientation axis must be one of '
                                       f'{sorted(UP_AXES)}, got {axis!r}')
                try:
                    yaw_value = float(record.get('yaw_deg'))
                except (TypeError, ValueError) as exc:
                    raise RuntimeError('full-size split plan reorientation yaw_deg must be '
                                       f"numeric, got {record.get('yaw_deg')!r}") from exc
                if not math.isfinite(yaw_value) or not 0.0 <= yaw_value <= 90.0:
                    raise RuntimeError('full-size split plan reorientation yaw_deg must be a '
                                       'bed turn between 0 and 90 degrees, '
                                       f'got {record.get("yaw_deg")!r}')
                raw_rotation = record.get('rotation')
                if not isinstance(raw_rotation, (list, tuple)) or len(raw_rotation) != 9:
                    raise RuntimeError('full-size split plan reorientation rotation must be a '
                                       f'9-value matrix, got {raw_rotation!r}')
                try:
                    matrix = [float(v) for v in raw_rotation]
                except (TypeError, ValueError) as exc:
                    raise RuntimeError('full-size split plan reorientation rotation must be '
                                       f'numeric, got {raw_rotation!r}') from exc
                if not all(math.isfinite(v) for v in matrix):
                    raise RuntimeError('full-size split plan reorientation rotation must be finite')
                try:
                    _check_rotation(matrix)
                except ValueError as exc:
                    raise RuntimeError(f'full-size split plan reorientation {exc}') from exc
                expected_matrix = _matmul(_yaw(yaw_value), UP_AXES[axis][1])
                if any(abs(a - b) > 1e-6 for a, b in zip(matrix, expected_matrix)):
                    raise RuntimeError('full-size split plan reorientation rotation is not the '
                                       'recorded face change composed with its bed turn; refusing '
                                       'to slice it as authored')
                if not isinstance(record.get('supports'), bool) \
                        or not isinstance(record.get('brim_recommended'), bool):
                    raise RuntimeError('full-size split plan reorientation record must carry '
                                       'boolean supports and brim_recommended decisions')
                validated_reorientations[reoriented_oid] = {**record, 'rotation': matrix,
                                                            'yaw_deg': yaw_value}
            if len(reoriented) == 1 and {key_to_object[key] for key in keys} == reoriented:
                # Solo (#221 R1): the exporter's whole-plate fit turns the
                # plate rigidly about one origin; a placement recorded for
                # this same object composes into the same fit (#221 R3).
                if shifts_by_plate and shifts_by_plate[index - 1] is not None:
                    raise RuntimeError('full-size split plan records a reorientation for a plate '
                                       'that also records shifts; refusing to slice it as authored')
                reorientation = validated_reorientations[next(iter(reoriented))]
                # The recorded supports decision — named in the intake offer,
                # so choosing the split approved it — is passed to the slicer
                # for this plate only; other plates keep today's default (None).
                supports = reorientation['supports']
                fit = {'scale': 1.0, 'rotation': reorientation['rotation'],
                       **({'placement_mm': placement_mm} if placement_mm is not None else {})}
            else:
                # Shared (#221 R3): realized per object through fit_by_object
                # composed with the plate's recorded shifts, validated below.
                if not shifts_by_plate or shifts_by_plate[index - 1] is None:
                    raise RuntimeError('full-size split plan records a reorientation for a plate '
                                       'that is not that part alone, without the recorded shifts '
                                       'that realize a shared reorientation; refusing to slice '
                                       'it as authored')
                shared_reorientation_by_object = validated_reorientations
                # One supports decision per plate, from the members' named
                # records: a member that needs supports turns them on for the
                # plate; when every member carries a record and none needs
                # supports, the records govern and supports stay off; when
                # non-reoriented members share the plate they keep today's
                # default (None) — a co-tenant's record never takes supports
                # away from a part that has none of its own.
                if any(row['supports'] for row in validated_reorientations.values()):
                    supports = True
                elif {key_to_object[key] for key in keys} == reoriented:
                    supports = False
        shifts_entry = shifts_by_plate[index - 1] if shifts_by_plate else None
        item_shifts = None
        if shifts_entry is not None:
            # Polygon-shared plate (#221 P2): the recorded per-object shifts
            # are the only realizable form of the proved hull placement.
            # Fail closed on anything the plan did not prove for exactly this
            # plate — never slice such a plate as authored.
            if not isinstance(shifts_entry, dict):
                raise RuntimeError('full-size split plan shifts for a plate must be a mapping '
                                   f'of object id to [dx, dy, dz], got {shifts_entry!r}')
            plate_objects = {key_to_object[key] for key in keys}
            if set(shifts_entry) != plate_objects:
                raise RuntimeError('full-size split plan records shifts that do not name exactly '
                                   'the objects of their plate; refusing to slice it as authored')
            if len(keys) < 2:
                raise RuntimeError('full-size split plan records shifts for a plate that is not '
                                   'shared; refusing to slice it as authored')
            if placed:
                raise RuntimeError('full-size split plan records shifts for a plate that also '
                                   'records a placement; refusing to slice it as authored')
            # A turn or a reorientation on this plate is the shared form
            # validated above (#221 R2/R3): its members' records are realized
            # per object, composed with these shifts. (A solo turn or
            # reorientation with shifts was already refused above.)
            item_shifts = {}
            for oid, raw in shifts_entry.items():
                if not isinstance(raw, (list, tuple)) or len(raw) != 3:
                    raise RuntimeError(f'full-size split plan shift for object {oid} must be a '
                                       f'[dx, dy, dz] triple, got {raw!r}')
                try:
                    values = [float(v) for v in raw]
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(f'full-size split plan shift for object {oid} must be '
                                       f'numeric, got {raw!r}') from exc
                if not all(math.isfinite(v) for v in values):
                    raise RuntimeError(f'full-size split plan shift for object {oid} must be '
                                       f'finite, got {raw!r}')
                item_shifts[oid] = values
        fit_by_object = None
        member_rotations = {}
        if shared_yaw_by_object:
            fit_by_object = {}
            for oid, degrees in shared_yaw_by_object.items():
                rad = math.radians(degrees)
                c, s = math.cos(rad), math.sin(rad)
                fit_by_object[oid] = {'rotation': [c, -s, 0.0, s, c, 0.0, 0.0, 0.0, 1.0]}
                member_rotations[oid] = fit_by_object[oid]['rotation']
        if shared_reorientation_by_object:
            fit_by_object = fit_by_object or {}
            for oid, row in shared_reorientation_by_object.items():
                fit_by_object[oid] = {'rotation': row['rotation']}
                member_rotations[oid] = row['rotation']
        report = export_single_color_plate(src, plate, view, keys, bed=int(st['bed_mm'][0]),
                                           slot_colors=st.get('filament_slots') or {},
                                           **({'fit': fit} if fit is not None else {}),
                                           **({'fit_by_object': fit_by_object}
                                              if fit_by_object is not None else {}),
                                           **({'item_shifts': item_shifts} if item_shifts is not None else {}))
        if item_shifts is not None:
            # Re-read the derived plate: every object must have landed at
            # exactly the recorded shift, or the plate is not the proved
            # polygon placement and must not be sliced. A rotated member
            # (#221 R2 shared yaw, #221 R3 shared reorientation) must land at
            # its rotated authored bounds plus the shift, with the rotated
            # extents — a plate that rotated it by any other matrix, or not
            # at all, is not the recorded plan.
            from ..core.color_geometry import _world_bounds as _bounds_of
            from ..core.color_geometry import _world_points as _points_of
            from ..core.orca_adapter import _rotate as _rotate_point
            with zipfile.ZipFile(src) as archive:
                authored = {oid: _bounds_of(archive, {oid})[0] for oid in item_shifts}
                rotated_points = {oid: _points_of(archive, {oid}) for oid in member_rotations}
            with zipfile.ZipFile(plate) as archive:
                landed_boxes = {oid: _bounds_of(archive, {oid})[0] for oid in item_shifts}
            for oid, values in item_shifts.items():
                if oid in member_rotations:
                    rotated = [_rotate_point(point, member_rotations[oid])
                               for point in rotated_points[oid]]
                    base_min = [min(point[a] for point in rotated) for a in range(3)]
                    base_extent = [max(point[a] for point in rotated) - base_min[a]
                                   for a in range(3)]
                    landed = landed_boxes[oid]
                    landed_extent = [landed[a][1] - landed[a][0] for a in range(3)]
                    if any(abs(landed[a][0] - (base_min[a] + values[a])) > 2e-3 for a in range(3)) \
                            or any(abs(got - want) > 2e-3
                                   for got, want in zip(landed_extent, base_extent)):
                        if shared_reorientation_by_object \
                                and oid in shared_reorientation_by_object:
                            raise RuntimeError('derived plate did not land at the recorded '
                                               'shared reorientation; refusing to slice '
                                               'it as reoriented')
                        raise RuntimeError('derived plate did not land at the recorded shared '
                                           'turn; refusing to slice it as turned')
                    continue
                for axis in range(3):
                    expected_min = authored[oid][axis][0] + values[axis]
                    if abs(landed_boxes[oid][axis][0] - expected_min) > 2e-3:
                        raise RuntimeError('derived plate did not land at the recorded shifts; '
                                           'refusing to slice it as placed')
        if placement_mm is not None:
            landed = report.get('placed_bounds_mm') or []
            if len(landed) != 2 or any(abs(landed[axis][0] - placement_mm[axis]) > 1e-6 for axis in range(2)):
                raise RuntimeError('derived plate did not land at the recorded placement; '
                                   'refusing to slice it as placed')
        if reorientation is not None:
            # Re-read the derived plate: the landed part must measure
            # exactly the rotated authored extents per axis and sit on the
            # bed (Z minimum ~0), or the plate is not the recorded
            # reorientation and must not be sliced.
            from ..core.color_geometry import _world_bounds as _bounds_of
            from ..core.color_geometry import _world_points as _points_of
            from ..core.orca_adapter import _rotate as _rotate_point
            oid = next(iter(reoriented))
            with zipfile.ZipFile(src) as archive:
                authored_points = _points_of(archive, {oid})
            rotated = [_rotate_point(point, reorientation['rotation']) for point in authored_points]
            expected_extents = [max(p[a] for p in rotated) - min(p[a] for p in rotated)
                                 for a in range(3)]
            with zipfile.ZipFile(plate) as archive:
                landed_box = _bounds_of(archive, {oid})[0]
            landed_extents = [landed_box[a][1] - landed_box[a][0] for a in range(3)]
            if any(abs(landed - expected) > 2e-3
                   for landed, expected in zip(landed_extents, expected_extents)) \
                    or abs(landed_box[2][0]) > 2e-3:
                raise RuntimeError('derived plate did not land at the recorded reorientation; '
                                   'refusing to slice it as reoriented')
        slice_dir = work / 'sliced' / 'orca' / uuid.uuid4().hex
        out, provenance, configs = slice_model(plate, slice_dir, st, preserve_layout=True,
                                               filament_slot=slot, **_support_kw(job, supports), **_edit_kw(job))
        finalize_multicolor(out, None, st, {'0': slot})
        check = verify_gcode_offline(out, st, _job_adapter(job))
        artifacts.append({'slot': slot, 'plate_id': index, 'binding_key': f'{index}:{slot}',
                          'part_keys': keys, 'plate': str(plate), 'gcode': str(out),
                          **({'yaw_deg': yaw_deg} if yaw_deg is not None else {}),
                          **({'yaw_by_object': shared_yaw_by_object}
                             if shared_yaw_by_object else {}),
                          **({'placement_mm': placement_mm} if placement_mm is not None else {}),
                          **({'reorientation': reorientation} if reorientation is not None else {}),
                          **({'reorientation_by_object': shared_reorientation_by_object}
                             if shared_reorientation_by_object else {}),
                          **({'shifts_by_object': item_shifts} if item_shifts is not None else {}),
                          'offline_check': check, 'plate_report': report, 'provenance': provenance,
                          'slice_config': [str(path) for path in configs]})
    latest = json.loads(Path(job_path).read_text())
    if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha or _recipe_changed(job, latest, _recorded_printer_id(job)):
        raise RuntimeError('source, decisions or slice recipe changed during full-size split slicing')
    job['derived_plates'] = artifacts
    job['plate_merge'] = {'mode': 'split', 'reason': f"operator chose full-size split into {len(artifacts)} plates",
                          'plates_in': len(artifacts), 'parts_in': len(expected), 'fits_bed': True,
                          'bed_mm': int(st['bed_mm'][0]), 'full_size': True}
    job['artifact_binding'] = {**_recipe_binding(job), 'source_sha256': source_sha, 'decisions_sha256': decision_sha,
                               'gcode_sha256': {row['binding_key']: file_sha256(row['gcode']) for row in artifacts}}
    job.update({'gcode': artifacts[0]['gcode'], 'gcode_verified': True,
                'sliced_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
    write_job(work, job)
    return [row['gcode'] for row in artifacts]


def slice_job(job_path, brim_override=None, copies=None, scale=None, recipe_sha256=None):
    # Disk only. Launch still runs printer_state.check inside print_now and
    # verify_gcode. A file that never leaves disk does not need Moonraker or Spoolman.
    job = json.loads(Path(job_path).read_text())
    # A filament warning acceptance authorizes one slicing run: an
    # acceptance an earlier slice_job run already used is consumed here,
    # before any gate reads it, so a reslice presents the warnings again
    # instead of silently reusing the acceptance (the clearance pattern's
    # rule that a reslice invalidates the pending acknowledgement).
    _filament_ack = job.get('filament_warning_acknowledgement') or {}
    if _filament_ack.get('used_by_slice'):
        job.pop('filament_warning_acknowledgement', None)
        job.pop('filament_warnings', None)
        write_job(Path(job_path).parent, job)
    # The job's own printer record (#334): slicing a secondary printer's job
    # against the default record would validate its filament selection
    # against the wrong printer's slots and slice for the wrong hardware
    # (SAFE-4 / P1-78). A job with no recorded printer keeps the legacy load.
    st = printer_state.load(job["printer_id"]) if job.get("printer_id") else printer_state.load()
    adapter = _job_adapter(job)
    # "generic" = not sliced through the record-bound IFS machinery (per-slot
    # selections validated against the record map, per-part plates exported by
    # record slot, native finalization). A multi_tool printer stays "generic"
    # here, and so does a Bambu AMS model (#671): multi_slot by class, but its
    # filament slots are the sliced file's own — Print Handy never slices per
    # slot for it — so its slicing behavior is exactly what it was as "single".
    generic = not (adapter.multi_slot and adapter.slots_bound_by_record)
    # A multi_tool printer stays "generic" for the IFS-shaped gates below, but
    # its per-toolhead colour decisions are honoured (#346): the refusal gate
    # and the decisions drop both exempt it, and per-part decisions route to
    # the identity tool-mapping slice.
    multi_tool = adapter.multi_tool
    job["printer_model"] = adapter.key  # print-now refuses a job sliced for another model
    _timing(job_path, 'slice_start')
    if copies is None:
        copies = job.get('copies')
    if copies:
        job['copies'] = int(copies)
    _recorded_slot = (job.get("filament_selection") or {}).get("slot") if isinstance(job.get("filament_selection"), dict) else None
    _slot_past_limit = type(_recorded_slot) is int and not 1 <= _recorded_slot <= position_limit(adapter)
    if not generic and job.get("filament_selection") is not None and (job.get('color_decisions') or {}).get('mode') != 'per_part' \
            and not (_slot_past_limit and job.get('color_decisions')):
        # A recorded one-color slot past the printer's slot count is not
        # refused here: the #1189 fallback below replaces it (named in a
        # warning the operator accepts) and validates the replacement.
        selection = resolve_filament_selection(st, job["filament_selection"])
        _validate_slot_selection(st, selection)
        job["filament_selection"] = selection
    src=Path(job["downloaded_file"]); ad=job["audit"]; multi_plate=False
    if src.suffix.lower() not in (".3mf",".stl"): raise RuntimeError("only STL/3MF can be sliced; selected source is not geometry")
    from ..core import slice_recipe as sr
    recipe = sr.record(sr.slice_recipe(job, file_sha256(src), st,
                                       copies=copies, scale=scale, brim_override=brim_override))
    if recipe_sha256 is not None and recipe_sha256 != recipe['sha256']:
        # The request was bound to a recipe the job no longer describes (a background slice
        # requested before the job changed). Slicing now would label one recipe with another.
        from ..core.slice_failure import SliceFailure
        raise SliceFailure('slice request is for another slice recipe; the job changed after it was requested. '
                           'Nothing sliced', kind='recipe')
    if src.suffix.lower() == ".3mf":
        with zipfile.ZipFile(src) as archive:
            actual_plates = {int(match.group(1)) for name in archive.namelist()
                             if (match := re.fullmatch(r"Metadata/plate_(\d+)\.(?:png|gcode)", name))}
            if "Metadata/model_settings.config" in archive.namelist():
                try:
                    settings = ET.fromstring(archive.read("Metadata/model_settings.config"))
                    # Every plate with a plater_id counts, with or without a <model_instance>:
                    # an exporter that omits instances (and thumbnails) still names separate
                    # beds, which must never be merged onto one (the same rule as audit()).
                    for plate in settings.iter("plate"):
                        for metadata in plate.iter("metadata"):
                            if metadata.get("key") == "plater_id":
                                plate_id = metadata.get("value")
                                if plate_id is None:
                                    raise RuntimeError("3MF plate assignment has no plater_id value")
                                actual_plates.add(int(plate_id))
                except (ET.ParseError, ValueError, TypeError) as exc:
                    raise RuntimeError(f"unable to inspect 3MF plate assignments: {exc}") from exc
        multi_plate = len(actual_plates) > 1 or int(ad.get("plates", 0) or 0) > 1

    from ..core.source_colors import inventory, prompts, require_single_color, validate_decisions
    try:
        manifest = inventory(src)
    except RuntimeError as exc:
        # Preserve the legacy safety contract for malformed, thumbnail-less
        # multi-plate archives before surfacing component-reference errors.
        has_thumbnails = False
        if src.suffix.lower() == ".3mf":
            with zipfile.ZipFile(src) as archive:
                names = archive.namelist()
                try:
                    settings = ET.fromstring(archive.read("Metadata/model_settings.config"))
                    plate_ids = {m.get("value") for m in settings.iter("metadata")
                                 if m.get("key") == "plater_id"}
                except (KeyError, ET.ParseError):
                    plate_ids = set()
                has_thumbnails = any(re.fullmatch(r"Metadata/plate_\d+\.png", name) for name in names)
            if len(plate_ids) > 1 and not has_thumbnails:
                raise RuntimeError("multi-plate 3MF source has no plate thumbnails; no slicing attempted") from exc
        # Owner ruling (catduck, 2026-11-10): unknown filament usage never
        # blocks a print; the job proceeds to the slicer. A plates-only
        # source — the archive carries its plates as thumbnails, the job's
        # audit accounts for every one of them, and the audit records no
        # build parts — has nothing for inventory to resolve: parts and
        # usage stay UNKNOWN and the slicer's output is authoritative.
        # The refusal below stands for the rest: a stale audit that hides
        # plates, and build parts the inventory cannot assign to plates,
        # are safety facts about the job record, not usage gaps.
        plates_only_unknown = (
            multi_plate and has_thumbnails
            and int(ad.get("plates", 0) or 0) == len(actual_plates)
            and not int(ad.get("build_items", 0) or 0))
        if plates_only_unknown:
            manifest = {'source_sha256': file_sha256(src), 'parts': [], 'filaments': [],
                        'painted': False, 'requires_choices': False,
                        'layer_changes': [], 'layer_extras': [], 'plate_usage': {}}
            from ..core.source_colors import _append_filament_stats
            _append_filament_stats(src, manifest)
        elif multi_plate:
            raise RuntimeError(f'multi-plate 3MF cannot be merged or split: {exc}; no slicing attempted') from exc
        else:
            raise
    selected_plates = job.get('selected_plates') or None
    filament_gate_ran = False
    # The #1189 fallbacks (owner decision, catduck, 2026-11-10: no filament
    # condition hard-blocks a print). On a printer whose decisions are
    # sliced (multi-slot, or multi_tool), a slot past the printer's slot
    # count, a used filament with no slot chosen and color choices never
    # made print from a deterministic fallback slot instead of refusing.
    # The decisions are derived in memory from JOB.json's color_decisions
    # and the printer record, before any slicing, so the G-code is made for
    # slots the printer has and passes the same bounds and tool checks.
    # Their warnings join the filament warning round below: nothing slices
    # until the operator accepts them, and a reslice presents them again.
    fallback_used = not generic or multi_tool
    slice_decisions_in, fallback_warnings = job.get('color_decisions'), []
    if fallback_used:
        if not manifest.get('source_sha256'):
            manifest['source_sha256'] = file_sha256(src)
        slice_decisions_in, fallback_warnings = filament_fallbacks(
            manifest, job.get('color_decisions'), selected_plates, st, position_limit(adapter),
            choices_needed=bool(manifest['requires_choices']))
    if multi_plate and selected_plates and slice_decisions_in:
        # The user selected exact source plates, so validate their usage and
        # mappings before slicing. Whole-source imports without source usage
        # metadata remain unknown until Orca provides authoritative output.
        _enforce_filament_readiness(job, manifest, slice_decisions_in, selected_plates,
                                    slot_capacity=position_limit(adapter),
                                    job_folder=Path(job_path).parent, via_slice=True,
                                    state=st if fallback_used else None)
        filament_gate_ran = True
    usage_warnings = []
    if multi_plate:
        # Source usage is evidence, not a prerequisite for slicing an unsliced
        # project. Damaged claims when present become filament warnings;
        # absent metadata stays UNKNOWN and the destination slicer's output
        # becomes authoritative. A job with a plate selection is warned only
        # about the selected plates' claims: an unselected plate never
        # contributes (#1110 Phase B).
        from ..core.source_colors import validate_plate_usage_if_present
        usage_warnings = validate_plate_usage_if_present(manifest, selected_plates)
        if not selected_plates:
            usage_warnings = _whole_source_usage_warnings(manifest) + usage_warnings
    # A height-color-only source (#870) is not a multi-color source: its parts
    # share one extruder and the recorded single-color decision carries the
    # operator's pause-or-one-color choice, which a single-extruder printer
    # honours through slice_model's pause path. It is exempt from the generic
    # refusal and the decisions drop below — but only that decision shape,
    # and never for multi-plate sources or full-size split plans.
    recorded = job.get('color_decisions')
    layered_single = bool(
        not multi_plate and not job.get('full_size_split_plan')
        and not manifest.get('painted') and manifest.get('layer_changes')
        and len({part['source_extruder'] for part in manifest['parts']}) <= 1
        and isinstance(recorded, dict) and recorded.get('mode') == 'single_color')
    if generic and (multi_plate or manifest['requires_choices'] or job.get('full_size_split_plan')):
        # A multi_tool printer honours multi-colour and multi-plate sources
        # through the identity tool mapping (#346); a full-size split plan is
        # still IFS-shaped and stays refused here.
        if (not multi_tool or job.get('full_size_split_plan')) and not layered_single:
            raise MultiColorUnsupported(
                f"{adapter.label} has a single extruder; multi-plate, multi-part or multi-color sources need a "
                "multi-slot printer. Slice one single-color plate instead")
    # STL has no source palette; bind decisions to the exact file bytes, as intake does.
    if not manifest.get('source_sha256'):
        manifest['source_sha256'] = file_sha256(src)
    job['color_manifest'], job['color_prompts'] = manifest, prompts(manifest, selected_plate_ids=selected_plates)
    write_job(Path(job_path).parent, job)
    _timing(job_path, 'inventory_done')
    job['slice_recipe'] = recipe  # persisted only with the artifact it describes
    if fallback_warnings and not filament_gate_ran:
        # Present the fallbacks before the decisions are validated: the
        # operator sees the named slot first, and a fallback slot that the
        # record cannot resolve (no loaded-slot information) refuses only
        # after acceptance, as the slot check it is.
        _enforce_filament_warnings(job, fallback_warnings + usage_warnings,
                                   manifest.get('source_sha256'), selected_plates,
                                   job_folder=Path(job_path).parent, via_slice=True)
        filament_gate_ran = True
    decisions = None if (generic and not multi_tool and not layered_single) else slice_decisions_in
    if decisions is not None:
        decisions = validate_decisions(manifest, decisions, st, resolve_filament_selection,
                                       selected_plate_ids=selected_plates)
        if selected_plates:
            # Automated readiness for a selected-plate print: a selected
            # plate whose used colors cannot be validated against the
            # decisions blocks here, before any slicing; unselected plates
            # contribute nothing (#1110 Phase B, criterion 6). Filament
            # usage findings warn instead of blocking (owner ruling,
            # catduck, 2026-11-10) — see _enforce_filament_readiness.
            _enforce_filament_readiness(job, manifest, decisions, selected_plates,
                                        slot_capacity=position_limit(adapter),
                                        job_folder=Path(job_path).parent, via_slice=True,
                                        state=st if fallback_used else None)
            filament_gate_ran = True
    if (usage_warnings or fallback_warnings) and not filament_gate_ran:
        # No readiness gate presented these (no plate selection, or no
        # decisions for it to check): damaged usage claims and the #1189
        # fallbacks still wait for the operator's acceptance before slicing.
        _enforce_filament_warnings(job, fallback_warnings + usage_warnings,
                                   manifest.get('source_sha256'), selected_plates,
                                   job_folder=Path(job_path).parent, via_slice=True)
    if fallback_warnings:
        # Accepted: record what this artifact is sliced with (provenance;
        # JOB.json's color_decisions stay the operator's own). A one-color
        # fallback is the job's slot selection, exactly as a recorded
        # one-color decision is, and print start launches from it.
        job['filament_fallback'] = {'warnings': fallback_warnings, 'decisions': decisions,
                                    'at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
        fallback_selection = (decisions or {}).get('selection') \
            if (decisions or {}).get('mode') == 'single_color' else None
        if isinstance(fallback_selection, dict) and job.get('filament_selection') != fallback_selection:
            if not generic and not multi_tool:
                _validate_slot_selection(st, fallback_selection)
            job['filament_selection'] = fallback_selection
            on_disk = json.loads(Path(job_path).read_text())
            on_disk['filament_selection'] = fallback_selection
            write_job(Path(job_path).parent, on_disk)
            # The recipe hashes the selected slot: record it for the slot this artifact is sliced for.
            recipe = sr.record(sr.slice_recipe(job, file_sha256(src), st, copies=copies, scale=scale,
                                               brim_override=brim_override))
            job['slice_recipe'] = recipe
    else:
        job.pop('filament_fallback', None)
    if manifest['requires_choices'] and not decisions:
        raise RuntimeError('part/color choices required; run color-plan and color-select before slicing')
    if copies and int(copies) > 1 and not generic and not copies_supported(job, multi_plate):
        # Split, multi-plate and per-part paths never clone copies (#37); keep JOB.json truthful.
        print(f'NOTE: {int(copies)} copies requested, but split and per-part jobs slice one copy per plate')
        copies = 1
        job['copies'] = 1
    if job.get('full_size_split_plan'):
        result = _slice_full_size_split(job, job_path, src, manifest, decisions, st)
        _timing(job_path, 'slice_done', path='full_size_split')
        return result
    if multi_plate:
        # Several source plates: merge onto one bed by default, otherwise one
        # derived plate per source plate. Never an unexplained refusal.
        try:
            result = slice_multi_plate(job, job_path, src, manifest, decisions, st, brim_override)
        except ImplicitPlateSplit as exc:
            if not adapter.multi_tool:
                raise
            raise RuntimeError(
                f"{adapter.label} prints a multi-color job as one merged plate; this source does not "
                f"merge onto one bed ({exc.verdict.get('reason')}). No slicing attempted") from exc
        _timing(job_path, 'slice_done', path='multi_plate')
        return result
    if decisions and decisions['mode'] == 'single_color':
        selection = decisions['selection']
        job['filament_selection'] = selection
        if multi_tool and src.suffix.lower() == '.3mf':
            # A toolhead pick is a one-tool identity job: route it through
            # the merged multi-tool slice (#346 machinery) so the geometry
            # and filament profiles land on the picked toolhead instead of
            # T0. The generic tail below has no tool channel to honor it,
            # and it recorded the pick while silently slicing toolhead 1.
            # The merged path slices the authored geometry once, so a job
            # asking for copies, a resize or a rotation refuses here with
            # the reason stated instead of silently dropping the transform.
            scale_now = scale if scale is not None else job.get('scale')
            if (copies and int(copies) > 1) or (job.get('orientation') or {}).get('rotation') \
                    or (scale_now is not None and float(scale_now) != 1.0):
                raise RuntimeError(
                    f"{adapter.label} slices a single-color toolhead job as one merged plate at "
                    "authored size; copies, resizing and rotation are not supported on that path. "
                    "Slice one copy at 100% to print on the picked toolhead. Nothing sliced")
            try:
                return slice_multi_plate(job, job_path, src, manifest, decisions, st, brim_override)
            except ImplicitPlateSplit as exc:
                raise RuntimeError(
                    f"{adapter.label} prints a single-color toolhead job as one merged plate; this source "
                    f"does not merge onto one bed ({exc.verdict.get('reason')}). No slicing attempted") from exc
        if not multi_tool:
            # The IFS slot verification is slot-shaped; a toolhead pick was
            # already resolved against the recorded toolheads (#346).
            _validate_slot_selection(st, selection)
    elif decisions and decisions['mode'] == 'per_part':
        if adapter.multi_tool:
            # Independent toolheads (#346): one merged G-code through the
            # identity tool mapping, or a stated refusal — never the IFS
            # per-slot split below, whose plates finalize through a native
            # slot contract this printer does not have.
            try:
                return slice_multi_plate(job, job_path, src, manifest, decisions, st, brim_override)
            except ImplicitPlateSplit as exc:
                raise RuntimeError(
                    f"{adapter.label} prints a multi-color job as one merged plate; this source does not "
                    f"merge onto one bed ({exc.verdict.get('reason')}). No slicing attempted") from exc
        if src.suffix.lower() == '.3mf':
            # Single-plate per-part decisions use the same merge planner as
            # multi-plate sources: one multicolor G-code unless a stated reason splits.
            try:
                return slice_multi_plate(job, job_path, src, manifest, decisions, st, brim_override)
            except ImplicitPlateSplit as exc:
                print('SPLIT single-plate 3MF into per-slot G-code: ' + str(exc.verdict.get('reason')))
                job['plate_merge'] = exc.verdict
        # Non-mergeable and non-3MF per-part sources use the single-slot plate split.
        groups = single_color_groups(job, manifest)
        if not groups:
            raise RuntimeError('per-part decision did not resolve any single-color plate')
        refuse_assembly_split(src, manifest, groups, st, job.get('plate_merge'))
        # Every slot group is sliced. The loaded spool is a launch fact, not a
        # reason to drop a plate that already names its own slot.
        from ..core.color_geometry import export_single_color_plate
        from ..core.orca_adapter import slice_model
        import uuid
        work = Path(job_path).parent
        derived = work / 'derived'
        derived.mkdir(exist_ok=True)
        source_sha = file_sha256(src)
        decision_sha = decisions_digest(decisions)
        fit, supports, brim_override = _job_fit(job, brim_override)
        artifacts = []
        for group in groups:
            plate = derived / f"slot{group['slot']}.3mf"
            placement_kw = {'fit': fit} if fit is not None else {}
            if fit is None and job.get('full_size_rearrange_plan'):
                # A chosen 100% rearrangement that could not merge onto one
                # multicolor bed: each slot plate is packed on its own bed
                # (parts may turn 90 degrees) instead of keeping the
                # author's layout, which is what overflowed the bed.
                placement_kw = _rearranged_group_placement(src, manifest, group, int(st['bed_mm'][0]),
                                                           brim=brim_override)
            report = export_single_color_plate(src, plate, decisions, group['part_keys'], bed=int(st['bed_mm'][0]), slot_colors=st.get('filament_slots') or {}, **placement_kw)
            slice_dir = work / 'sliced' / 'orca' / uuid.uuid4().hex
            # Chat edit settings (#903) ride onto every derived plate too:
            # the supports choice the edit made outranks the fit flow's
            # (it becomes the value _support_kw starts from; the operator's
            # support decision still applies on top, per _support_kw), and
            # the process edits apply per plate (a mirror in them is
            # refused by slice_model's per-part guard, loudly).
            from ..core import edit_plan as _edit_plan_pp
            _pp_supports = _edit_plan_pp.supports_override(job)
            _effective_supports = _pp_supports if _pp_supports is not None else supports
            out, provenance, configs = slice_model(plate, slice_dir, st, brim_override, preserve_layout=True, filament_slot=group['slot'], **_support_kw(job, _effective_supports), **_edit_kw(job))
            # Keep the plate's physical IFS slot explicit. Orca may retain
            # project-wide unused filament_type entries although all geometry
            # is remapped to logical T0 on this single-color derivative.
            material = st.get('filament_slots', {}).get(str(group['slot']), {}).get('material')
            if not material:
                raise RuntimeError(f"derived plate physical IFS slot {group['slot']} has no verified material")
            finalize_multicolor(out, None, st, {'0': group['slot']})
            # Verify the exact final bytes (after map, color and MD5 rewrite).
            check = verify_gcode_offline(out, st, _job_adapter(job))
            artifacts.append({'slot': group['slot'], 'part_keys': group['part_keys'], 'plate': str(plate),
                              'gcode': str(out), 'offline_check': check, 'plate_report': report,
                              'provenance': provenance, 'slice_config': [str(path) for path in configs]})
        latest = json.loads(Path(job_path).read_text())
        if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha or _recipe_changed(job, latest, _recorded_printer_id(job)):
            raise RuntimeError('source, decisions or slice recipe changed during slicing; artifacts not made ready')
        job['derived_plates'] = artifacts
        job.pop('refused_plates', None)
        job['artifact_binding'] = {**_recipe_binding(job), 'source_sha256': source_sha, 'decisions_sha256': decision_sha,
                                   'gcode_sha256': {row['slot']: file_sha256(row['gcode']) for row in artifacts}}
        job.update({'gcode': artifacts[0]['gcode'], 'gcode_verified': True,
                    'sliced_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
        _timing(job_path, 'slice_done')
        write_job(work, job)
        print('SLICED separate single-color plates (not uploaded): ' + ', '.join(row['gcode'] for row in artifacts))
        return [row['gcode'] for row in artifacts]
    scale = scale if scale is not None else job.get("scale")
    explicit_scale = scale is not None
    if scale is not None:
        scale = float(scale)
        if not scale > 0:
            raise RuntimeError('scale must be greater than 0')
    orientation = job.get("orientation") or {}
    rotation = orientation.get("rotation")
    if not all(ad.get(k) for k in ("size_x", "size_y", "size_z")):
        # Jobs audited by the retired Prusa probe can lack extents; re-measure.
        for line in model_info_output(src).splitlines():
            k, _, v = line.partition(" = ")
            if k in ("size_x", "size_y", "size_z"): ad[k] = v
    sx,sy,sz=(float(ad[k]) for k in ("size_x","size_y","size_z"))
    authored_dims = (sx, sy, sz)
    if rotation and orientation.get("dims_full_mm"):
        # Operator chose a rotated orientation: check the rotated envelope, not the authored one.
        sx, sy, sz = (float(v) for v in orientation["dims_full_mm"])
    rearrange_plan = job.get("full_size_rearrange_plan") or {}
    if rearrange_plan.get("size_mm"):
        sx, sy, sz = (float(v) for v in rearrange_plan["size_mm"])
    auto_scale = None
    unit_provenance = job.get('unit_provenance') if isinstance(job.get('unit_provenance'), dict) else None
    if src.suffix.lower() == '.stl':
        if explicit_scale:
            # An explicit scale — 1.0 included — is the operator's own size
            # instruction; it settles the size and is never overridden.
            unit_provenance = {'units': None, 'source': 'explicit_scale',
                               'original_mm': list(authored_dims), 'factor': float(scale)}
        elif _is_generated_mm(job):
            unit_provenance = {'units': 'mm', 'source': 'generated',
                               'original_mm': list(authored_dims), 'factor': 1.0}
        elif max(authored_dims) < UNIT_QUESTION_MAX_MM:
            # STL carries no units and a sub-10 mm part may be authored in
            # millimeters or inches. Never assume inches: slicing waits for a
            # source-bound user confirmation recorded on the job (#206).
            decision = job.get('unit_decision')
            expected_sha = manifest.get('source_sha256') or file_sha256(src)
            if not isinstance(decision, dict) or decision.get('source_sha256') != expected_sha:
                measured = ' x '.join(f'{value:g}' for value in authored_dims)
                as_inch = ' x '.join(f'{value * INCH_TO_MM:g}' for value in authored_dims)
                raise RuntimeError(
                    f'model measures {measured} mm and STL files do not record units; confirm whether that '
                    f'size is millimeters (prints at {measured} mm) or inches (prints at {as_inch} mm) before '
                    'slicing — record the answer on this job (unit-select JOB.json --units mm|inch, or slice '
                    '--units mm|inch). Nothing sliced')
            try:
                confirmed = normalize_units(decision.get('units'))
            except ValueError:
                raise RuntimeError('recorded unit confirmation must be mm or inch; confirm the units again '
                                   'before slicing. Nothing sliced')
            if confirmed == 'inch':
                inches_scale = INCH_TO_MM
                inch_dims = tuple(round(value * inches_scale, 6) for value in (sx, sy, sz))
                if (inch_dims[0] > st["bed_mm"][0] or inch_dims[1] > st["bed_mm"][1]
                        or inch_dims[2] > st["max_print_height_mm"]):
                    measured = f'{sx:g}x{sy:g}x{sz:g}'
                    final = f'{inch_dims[0]:g}x{inch_dims[1]:g}x{inch_dims[2]:g}'
                    raise RuntimeError(f'model measures {measured} mm and treating it as inches would be {final} mm, which exceeds printer volume')
                scale = inches_scale
                auto_scale = {'factor': inches_scale, 'reason': 'inch units',
                              'original_mm': [sx, sy, sz], 'final_mm': list(inch_dims)}
                unit_provenance = {'units': 'inch', 'source': 'user_confirmation',
                                   'original_mm': list(authored_dims), 'factor': inches_scale}
            else:
                unit_provenance = {'units': 'mm', 'source': 'user_confirmation',
                                   'original_mm': list(authored_dims), 'factor': 1.0}
        else:
            unit_provenance = {'units': 'mm', 'source': 'authored_size',
                               'original_mm': list(authored_dims), 'factor': 1.0}
    eff_sx, eff_sy, eff_sz = sx * (float(scale) if scale else 1.0), sy * (float(scale) if scale else 1.0), sz * (float(scale) if scale else 1.0)
    if eff_sx>st["bed_mm"][0] or eff_sy>st["bed_mm"][1] or eff_sz>st["max_print_height_mm"]: raise RuntimeError(f"model {sx}x{sy}x{sz} mm exceeds printer volume")
    if brim_override is None and job.get('fit_brim_policy'):
        brim_override = job['fit_brim_policy'].get('width_mm', 0)
    if rotation and brim_override is None and orientation.get("brim"):
        brim_override = 5
    # Canonical slicing is Orca only.
    from ..core.orca_adapter import slice_model
    work = Path(job_path).parent
    import uuid
    # Preserve historical artifact bytes on every reslice.
    slice_dir = work / "sliced" / "orca" / uuid.uuid4().hex
    source_sha = file_sha256(src)
    slice_src = src
    if rearrange_plan:
        from ..core.geometry_layout import arrange_and_export
        rearrange_dir = slice_dir / "rearranged_100pct"
        from ..core.geometry_layout import DEFAULT_MARGIN_MM as _arrange_edge
        layout_report = arrange_and_export(
            src,
            rearrange_dir,
            bed=(float(st["bed_mm"][0]), float(st["bed_mm"][1]), float(st["max_print_height_mm"])),
            # layout.arrange_edge_mm plus the job's brim, so the whole brim stays on the bed.
            margin=_arrange_edge + float(brim_override or 0),
            allow_geometry_only_strip=not bool(decisions and decisions.get("mode") in ("multicolor", "per_part")),
        )
        preserved = (layout_report.get("preserved_3mf_package") or {}).get("preserved_3mf")
        slice_src = rearrange_dir / (preserved or layout_report["exported_3mf_geometry_only"])
        job["layout_report"] = str(rearrange_dir / "validation.json")
    slice_decisions = decisions
    if decisions is not None and slice_src != src:
        # The decisions were validated against the original source above, but
        # the slicer validates them against the file it is handed — the
        # arranged derivative, whose bytes (and SHA) differ. Bind the copy
        # passed to the slicer to the derivative; JOB.json and decision_sha
        # keep the original decisions (P1-50).
        slice_decisions = {**decisions, 'source_sha256': file_sha256(slice_src)}
    # A single-slot printer slices as `generic`, which leaves `decisions` as None, but the Discord
    # intake has still written color_decisions into JOB.json. The post-slice check below hashes
    # the file's decisions, so hash the same thing here or every such slice is refused (#268).
    decision_sha = decisions_digest(decisions if not generic else job.get("color_decisions"))
    selected_slot = None if generic else (job.get("filament_selection") or {}).get("slot")
    scale_kw = {'scale': scale} if scale is not None else {}
    if adapter.key != printers.current().key:
        scale_kw['adapter'] = adapter
    if rotation:
        scale_kw.update(rotation=rotation)
        # Only a supports value the orientation actually carries is a
        # choice (#916): a rotate/lay-flat edit adds a rotation without
        # one, and bool() turned that absence into supports=False plus a
        # support_decided=True provenance, suppressing the support
        # question. _support_kw preserves an existing orientation choice,
        # the operator's support decision and a chat edit's choice, and
        # leaves the choice unresolved when none of them exists.
        _orient_supports = orientation.get("supports")
        scale_kw.update(_support_kw(job, _orient_supports
                                    if isinstance(_orient_supports, bool) else None))
    else:
        scale_kw.update(_support_kw(job))  # the operator's choice, not the author's enable_support
    # Chat edit plans (#903): a standalone supports choice has no other
    # channel (the fit flow's orientation supports only rides along with a
    # rotation), and the process-level edit settings ride as `edits`. The
    # edit's supports fills only a gap: a supports value already decided
    # above (rotation, or the operator's support decision) stands.
    from ..core import edit_plan as _edit_plan
    _job_edits = _edit_plan.slice_edits(job)
    if _job_edits:
        scale_kw['edits'] = _job_edits
    _supports_edit = _edit_plan.supports_override(job)
    if _supports_edit is not None and 'supports' not in scale_kw:
        scale_kw['supports'] = _supports_edit
    _timing(job_path, 'orca_begin')
    if selected_slot is not None:
        # Single-color jobs are bound to their named IFS slot exactly like
        # derived per-slot plates, so launch uses the mapped native route.
        out, provenance, configs = slice_model(slice_src, slice_dir, st, brim_override,
                                              color_decisions=slice_decisions, filament_slot=selected_slot,
                                              copies=copies, **scale_kw)
        finalize_multicolor(out, None, st, {"0": selected_slot})
    else:
        out, provenance, configs = slice_model(slice_src, slice_dir, st, brim_override,
                                              color_decisions=slice_decisions, copies=copies, **scale_kw)
    # #952: the interval above mixes the slicer subprocess with Print Handy's
    # own finalize/guard work; the split rides along as extras (and in the
    # slice provenance) so a "speedup not visible" report is answerable from
    # TIMING.jsonl alone.
    _split = (provenance or {}).get('timing_s') or {}
    _timing(job_path, 'orca_finalize_done',
            **{k: _split[k] for k in ('slicer_s', 'python_s') if k in _split})
    _timing(job_path, 'verify_begin')
    if adapter.key == printers.current().key:
        _verify_routed(out, adapter, st, _recorded_printer_id(job), job=job)
    latest = json.loads(Path(job_path).read_text())
    _timing(job_path, 'verify_done')
    if file_sha256(src) != source_sha or decisions_digest(latest.get("color_decisions")) != decision_sha or _recipe_changed(job, latest, _recorded_printer_id(job)):
        raise RuntimeError("source, decisions or slice recipe changed during slicing; artifact not made ready")
    job["artifact_binding"] = {**_recipe_binding(job), "source_sha256": source_sha, "decisions_sha256": decision_sha,
                               "gcode_sha256": file_sha256(out)}
    if auto_scale is not None:
        job['auto_scale'] = auto_scale
    else:
        job.pop('auto_scale', None)
    if unit_provenance is not None:
        job['unit_provenance'] = unit_provenance
    else:
        job.pop('unit_provenance', None)
    job.update({"printer_model": adapter.key, "slice_config": [str(p) for p in configs], "gcode": str(out),
                "provenance": provenance, "gcode_verified": True,
                # Only this path clones copies into the G-code; estimates read it, never the request.
                "sliced_copies": {"gcode": str(out), "copies": max(1, int(copies or 1))},
                "sliced_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    write_job(work, job)
    print(f"SLICED (not uploaded): {out}")
    _timing(job_path, 'slice_done')
    return out


def prepare_first_layer(slot, color, material, spoolman_id, slicer="orca"):
    """Offline geometry + real slicer output. No printer contact or start permission."""
    st = printer_state.load()
    selection = {"slot": slot, "color": color, "material": material, "spoolman_id": spoolman_id}
    mapped = st.get("filament_slots", {}).get(str(slot), {})
    if (mapped.get("spoolman_id") != spoolman_id or
            str(mapped.get("color", "")).casefold() != color.casefold() or
            str(mapped.get("material", "")).casefold() != material.casefold() or
            st["spool"]["spoolman_id"] != spoolman_id):
        raise ValueError("selected filament differs from recorded slot/active spool; live reconciliation required")
    folder = ARCHIVE / "ad5x-first-layer-sliced"
    source = folder / "source" / "first_layer_50x50.stl"
    source.parent.mkdir(parents=True, exist_ok=True)
    # Closed 50 x 50 x 0.2 mm rectangular solid, one printable layer.
    vertices = [(0,0,0), (50,0,0), (50,50,0), (0,50,0),
                (0,0,0.2), (50,0,0.2), (50,50,0.2), (0,50,0.2)]
    faces = [(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),
             (1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7)]
    lines = ["solid first_layer"]
    for face in faces:
        lines += ["facet normal 0 0 0", "outer loop"]
        lines += ["vertex " + " ".join(map(str, vertices[i])) for i in face]
        lines += ["endloop", "endfacet"]
    source.write_text("\n".join(lines + ["endsolid first_layer"]) + "\n")
    from ..core.orca_adapter import slice_model
    if slicer != "orca":
        raise ValueError("only Orca slicing is supported; legacy Prusa is retired")
    output, provenance, configs = slice_model(source, folder / "sliced", st, first_layer=True)
    text = output.read_text()
    if text.count(";LAYER_CHANGE") != 1:
        raise RuntimeError("first-layer output must contain exactly one layer")
    config_desc = [str(p) for p in configs]
    command = provenance["slicer_command"]
    job = {"title": "AD5X First Layer Test Square 50x50mm", "slug": folder.name,
           "downloaded_file": str(source), "gcode": str(output), "slice_config": config_desc,
           "filament_selection": selection, "gcode_verified": False,
           "audit": {"size_x": 50, "size_y": 50, "size_z": 0.2, "parts": 1},
           "provenance": provenance,
           "next_action": "Live readiness and verify-gcode required before authorized print-now"}
    stamp_job_printer(job)
    write_job(folder, job)
    print("PREPARED OFFLINE (not verified live, not uploaded): " + str(output))
    return output
