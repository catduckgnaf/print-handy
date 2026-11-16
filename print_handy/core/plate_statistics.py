"""Per-plate statistics artifact after a verified slice (#1525, tranche G).

Built on the existing slicer-metadata parser (`spool_lifecycle`) and the
plate thumbnail path — no second parser, no second renderer. The
artifact is read-only and is an *estimate*: grams come from the slicer's
own accounting of its configured density and purge, not from weighing
anything, and slicing never charges inventory (this module has no
inventory surface at all).

Number sources, in preference order, with the winning source recorded
in ``statistics_source``:

1. Orca's optional statistics JSON (``plate_N.statistics.json`` from
   ``--export-statistics``) — used only when the caller supplies a
   record that parses and names per-filament weights. Whether the
   pinned Orca 2.5.0 binary actually emits it is proven by the
   slicer-tier test, not assumed here.
2. Verified G-code comments, through `spool_lifecycle.parse_slicer_metadata`.

Anything neither source states stays unknown (None), never fabricated:
Bambu Studio runs that report only a total keep per-filament values
unknown instead of splitting the total by guesswork.

Spool suggestions are deterministic: an exact material match (and an
exact color match when both sides name a color), ordered by spool id.
The closest color is never suggested as the mounted roll.
"""
from __future__ import annotations

import json
import re

from . import spool_lifecycle

_LAYER_COUNT = re.compile(r"^;\s*(?:total layers|layer count)\s*[:=]\s*(\d+)", re.M | re.I)


def parse_statistics_json(text):
    """Per-filament grams from an Orca statistics record, or None.

    Accepts the shapes Orca's statistics writer is documented to use:
    a mapping with a per-filament weight list under a filament/weight
    key. A record without a per-filament list proves nothing here and
    returns None, so the caller falls back to G-code comments.
    """
    try:
        record = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(record, dict):
        return None
    for key in ("filament_used_g", "filament_weights_g", "filaments"):
        value = record.get(key)
        if isinstance(value, list) and value and all(
                isinstance(v, (int, float)) for v in value):
            return [float(v) for v in value]
        if isinstance(value, list) and value and all(
                isinstance(v, dict) for v in value):
            weights = []
            for row in value:
                weight = row.get("used_g") or row.get("weight_g") or row.get("filament_used_g")
                if not isinstance(weight, (int, float)):
                    break
                weights.append(float(weight))
            else:
                return weights
    return None


def suggest_spools(filament: dict, spools) -> list[dict]:
    """Deterministic spool suggestions for one logical filament.

    Exact material match first; when both the filament and a spool name
    a color, the color must match exactly too. Ordered by spool id, so
    the same inputs always suggest the same list. No color-distance
    ranking exists here on purpose.
    """
    material = (filament.get("material") or "").strip().lower()
    color = (filament.get("color") or "").strip().lower()
    out = []
    for spool in spools or ():
        spool_material = (spool.get("material") or "").strip().lower()
        if material and spool_material and spool_material != material:
            continue
        spool_color = (spool.get("color") or "").strip().lower()
        if color and spool_color and spool_color != color:
            continue
        out.append({"spool_id": spool.get("id"),
                    "confidence": "exact" if (material and spool_material == material
                                              and (not color or spool_color == color))
                    else "partial"})
    return sorted(out, key=lambda row: (row["spool_id"] is None, row["spool_id"]))


def build_plate_statistics(*, gcode_text=None, statistics_text=None,
                           filaments=(), slot_map=None, spools=(),
                           job_id=None, source_sha256=None, plate_id=None,
                           printer_id=None, slicer_version=None,
                           thumbnail_present=False, print_started=False,
                           completion_state="not_started") -> dict:
    """The structured per-plate artifact for one verified slice."""
    slot_map = slot_map or {}
    per_filament = parse_statistics_json(statistics_text) if statistics_text else None
    source = "orca-statistics-json" if per_filament is not None else None
    meta = spool_lifecycle.parse_slicer_metadata(gcode_text or "")
    if per_filament is None and meta.get("filament_used_g_list"):
        per_filament = [float(v) for v in meta["filament_used_g_list"]]
        source = "gcode-comments"
    layers = None
    if gcode_text:
        match = _LAYER_COUNT.search(gcode_text)
        layers = int(match.group(1)) if match else None
    rows = []
    for index, filament in enumerate(filaments):
        number = filament.get("source_extruder", index + 1)
        grams = (per_filament[index] if per_filament is not None
                 and index < len(per_filament) else None)
        slot = slot_map.get(number, slot_map.get(str(number)))
        rows.append({
            "logical_filament": number,
            "material": filament.get("material"),
            "color": filament.get("color"),
            "estimated_grams": grams,
            "grams_status": "estimated" if grams is not None else "unknown",
            "physical_slot": slot,
            "slot_status": "mapped" if slot is not None else "unknown",
            "spool_suggestions": suggest_spools(filament, spools)})
    total = (sum(r["estimated_grams"] for r in rows
                 if r["estimated_grams"] is not None)
             if rows and all(r["estimated_grams"] is not None for r in rows)
             else meta.get("total_filament_used_g"))
    return {"schema_version": 1, "job_id": job_id, "plate_id": plate_id,
            "source_sha256": source_sha256, "printer_id": printer_id,
            "slicer_version": slicer_version,
            "statistics_source": source or "unknown",
            "thumbnail_present": bool(thumbnail_present),
            "layer_count": layers,
            "estimated_time": meta.get("estimated_time_str"),
            "total_estimated_grams": total,
            "filaments": rows,
            "print_started": bool(print_started),
            "completion_state": completion_state,
            "note": "Estimated grams from the slicer's own accounting, not a "
                    "measurement. Slicing alone never deducts inventory."}
