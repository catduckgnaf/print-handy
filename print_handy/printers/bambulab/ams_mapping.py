"""AMS tray mapping resolution for Bambu Lab print starts (#308).

Pure functions, no I/O: given a sliced file's filament requirements and one LAN
status snapshot, decide which physical tray prints each filament slot — the
mapping `client.project_file_payload` sends as `ams_mapping` / `ams_mapping2`.

Wire facts are from the #298 Stage 0 audit of maziggy/bambuddy at 98ecaf5
(protocol facts only; Bambuddy is AGPL-3.0, this repo is MIT — no code copied):

  - A mapping is a list of canonical global tray ids indexed by the file's
    filament slots: a regular AMS / AMS Lite tray is `ams_id * 4 + slot_id`,
    an AMS-HT unit is its unit id (128-135, single tray), the external spool
    is 254 (left on a dual-nozzle printer) or 255 (right), -1 is unmapped.
  - A user-supplied mapping that is present and not all-unmapped is trusted
    verbatim; a missing or all-unmapped one is recomputed from live status,
    and a recompute that cannot cover every slot fails loudly — a job must
    never silently print from the external spool.
  - Matching is nozzle-aware: on a dual-nozzle printer, candidates for a
    filament slot are hard-filtered to the extruder that slot prints with —
    except when a Filament Track Switch is installed, where any tray can
    route to any extruder and the filter is skipped. Extruders are numbered
    0 = right/main, 1 = left/deputy, and an AMS unit's extruder assignment is
    taken from the printer's own report, never assumed: a tray whose unit
    reports no extruder is not a candidate for an extruder-bound slot.

#307 owns decoding snapshot tray state for display; this module reads only the
tray fields the mapping decision needs, from the same raw snapshot. Nothing
here is hardware-verified: no Bambu printer has run any of it, and the FTS
presence flag in particular has no verified snapshot field yet, so the print
path passes fts=False (the strict side) until one is verified.
"""
from __future__ import annotations

import re

from .client import EXTERNAL_TRAYS, UNMAPPED_TRAY

_AMS_HT_RANGE = range(128, 136)


class MappingError(RuntimeError):
    """A filament-to-tray mapping could not be resolved; the job must not start."""


def _config(text, key):
    match = re.search(rf"^; {re.escape(key)} = (.*)$", text or "", re.M)
    return match.group(1).strip() if match else None


def tools_used(text):
    """Tool indexes the toolpath selects with bare T commands (T0 included)."""
    from ...core.slice_guard import split_toolpath
    parts = split_toolpath(text or "")
    body = parts[1] if parts else (text or "")
    return sorted({int(n) for n in re.findall(r"^[ \t]*T(\d+)\b", body, re.M)})


def requirements_from_gcode(text):
    """One requirement per filament slot the file declares, in slot order.

    Each requirement is {"material": <declared filament_type>, "extruder": int | None}.
    The extruder comes from Orca's `filament_extruder_id` config list (1-based in
    the file, 0-based here: 0 = right/main, 1 = left/deputy); a slot the file does
    not assign an extruder gets None and is matched on material alone.
    """
    raw_types = _config(text, "filament_type")
    from ...core.orca_config import split_config_list
    try:
        types = [t.strip() for t in split_config_list(raw_types)] if raw_types else []
    except ValueError:
        types = []
    raw_extruders = _config(text, "filament_extruder_id")
    extruders = []
    if raw_extruders:
        for piece in raw_extruders.split(","):
            try:
                extruders.append(int(piece.strip()) - 1)
            except ValueError:
                extruders.append(None)
    return [{"material": material,
             "extruder": extruders[i] if i < len(extruders) else None}
            for i, material in enumerate(types)]


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def trays_from_snapshot(snapshot):
    """Canonical tray records from a LAN snapshot (client.snapshot() or its print payload).

    Each tray is {"tray": <canonical global id>, "material": str | None,
    "extruder": int | None, "external": bool}. Empty AMS slots (no material
    reported) are not trays a job can print from and are left out. An AMS unit's
    extruder is read from the unit's own report when it carries one; otherwise
    it is None (unknown — never assumed, per the audit).
    """
    payload = snapshot.get("print") if isinstance(snapshot, dict) else None
    if not isinstance(payload, dict):
        payload = snapshot if isinstance(snapshot, dict) else {}
    trays = []

    def add(tray_id, data, extruder=None, external=False):
        material = data.get("tray_type") or data.get("tray_sub_brands")
        if not material:
            return
        trays.append({"tray": tray_id, "material": str(material),
                      "extruder": extruder, "external": external})

    ams = payload.get("ams")
    units = ams.get("ams") if isinstance(ams, dict) else None
    for unit in units or []:
        if not isinstance(unit, dict):
            continue
        unit_id = _int_or_none(unit.get("id"))
        if unit_id is None:
            continue
        extruder = _int_or_none(unit.get("extruder"))
        for tray in unit.get("tray") or []:
            if not isinstance(tray, dict):
                continue
            slot = _int_or_none(tray.get("id"))
            if slot is None:
                continue
            if slot in EXTERNAL_TRAYS:  # a dual-nozzle external spool reported in the tray list
                add(slot, tray, external=True)
            elif unit_id in _AMS_HT_RANGE:  # AMS-HT: one tray per unit, id passes through
                add(unit_id, tray, extruder=extruder)
            else:
                add(unit_id * 4 + slot, tray, extruder=extruder)
    vt = payload.get("vt_tray")
    if isinstance(vt, dict) and vt:
        add(EXTERNAL_TRAYS[0], vt, external=True)
    return trays


def validate_supplied(mapping, slot_count):
    """A JOB.json-supplied mapping, shape-checked; returns the list of tray ids.

    Trusted verbatim once the shape holds (audit: manual overrides are
    preserved). Raises MappingError for anything that is not one plausible
    canonical tray id per filament slot.
    """
    if isinstance(mapping, tuple):
        mapping = list(mapping)
    if not isinstance(mapping, list) or len(mapping) != slot_count:
        raise MappingError(f"the job's ams_mapping must list exactly {slot_count} tray id(s), "
                           f"one per filament slot; got {mapping!r}")
    for tray in mapping:
        if isinstance(tray, bool) or not isinstance(tray, int) or not (
                tray == UNMAPPED_TRAY or tray in EXTERNAL_TRAYS or 0 <= tray <= 135):
            raise MappingError(f"the job's ams_mapping holds {tray!r}, which is not a canonical "
                               "tray id (-1 unmapped, 0-135 AMS trays, 254/255 external spool)")
    assigned = [tray for tray in mapping if tray != UNMAPPED_TRAY]
    if len(set(assigned)) != len(assigned):
        # A tray serves one slot (resolve() enforces the same rule): one physical
        # tray cannot hold two of the job's filaments at once.
        raise MappingError(f"the job's ams_mapping assigns a tray to more than one filament "
                           f"slot: {mapping!r}; each filament slot needs its own tray")
    return list(mapping)


def resolve(requirements, trays, *, dual=False, fts=False):
    """Match every filament slot to a distinct tray; raise MappingError naming the slot that failed.

    A tray serves one slot. Matching is by material family (the printer_state
    rule: PLA-CF does not silently match a plain PLA tray). On a dual-nozzle
    printer a slot that names its extruder only matches trays whose unit
    reports that extruder — unless an FTS is installed (fts=True), where any
    tray can route to any extruder and the filter is skipped.
    """
    # Imported here, not at module level: printer discovery imports every module in
    # this package, and core.printer_state reads settings at import time (guard.py
    # follows the same pattern).
    from ...core.printer_state import same_material
    used, mapping = set(), []
    for index, req in enumerate(requirements):
        candidates = [t for t in trays if t["tray"] not in used
                      and same_material(req["material"], t["material"] or "")]
        if dual and not fts and req.get("extruder") is not None:
            candidates = [t for t in candidates if t["extruder"] == req["extruder"]]
        if not candidates:
            where = (f" for extruder {req['extruder']}" if dual and req.get("extruder") is not None
                     and not fts else "")
            raise MappingError(
                f"filament slot {index + 1} needs {req['material']!r}{where}, but no loaded AMS "
                "tray or external spool on the printer reports it; load that filament (or fix "
                "the job's ams_mapping) — the job will not be started from the wrong tray")
        chosen = candidates[0]
        used.add(chosen["tray"])
        mapping.append(chosen["tray"])
    return mapping


def resolve_for_print(text, snapshot, *, job_mapping=None, dual=False, fts=False):
    """(mapping | None, source) for one print start; source is "job", "snapshot" or "legacy".

    A job-supplied mapping wins verbatim. Otherwise the mapping is recomputed
    from the snapshot. A multi-slot or tool-changing job whose mapping cannot
    be resolved raises MappingError — it was refused before #308 and still is,
    now with the reason. A single-slot job that matches nothing reported falls
    back to (None, "legacy"): the pre-#308 payload, sliced for the recorded
    spool, which telemetry may not report (for example an external spool the
    snapshot does not describe); refusing it would regress every current model.
    """
    requirements = requirements_from_gcode(text)
    if not requirements:
        # Nothing declared, nothing to map: the text guard owns refusing a file
        # with no filament_type; the mapping step must not invent a refusal.
        return None, "legacy"
    if job_mapping is not None:
        supplied = validate_supplied(job_mapping, len(requirements))
        if any(tray != UNMAPPED_TRAY for tray in supplied):
            return supplied, "job"
        # An all-unmapped stored mapping counts as missing (audit): recompute below.
    trays = trays_from_snapshot(snapshot)
    multi = len(requirements) > 1 or any(tool > 0 for tool in tools_used(text))
    try:
        return resolve(requirements, trays, dual=dual, fts=fts), "snapshot"
    except MappingError:
        if multi:
            raise
        return None, "legacy"
