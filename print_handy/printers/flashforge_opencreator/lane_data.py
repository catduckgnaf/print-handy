"""Per-tool filament records from Moonraker's ``lane_data`` namespace (#1490).

Tranche 3: the reading half of lane_data reconciliation for the
OpenCreator models. Two different publishers can own the namespace on
an OpenCreator install, under two different key shapes (the Kalico
research on #1490 graded both source-verified, neither hardware
verified; a live namespace capture is still the arbiter):

- The generic AFC publisher (fork default branch ``something`` at
  7ce49713, ``klippy/extras/AFC_lane.py`` ``send_lane_data`` through
  ``AFC_utils.py``): one record per mapped tool command, key
  ``T<n>``, value ``{color, material, vendor_name, name, bed_temp,
  nozzle_temp, scan_time, td, lane, extruder_index, spool_id, weight,
  initial_weight}``. It posts only when the Moonraker config carries a
  ``[lane_data]`` section, deletes the record of an unmapped ``T``
  name, and posts the same shape with empty strings for a cleared
  lane. Whether the shipped standalone toolchanger configuration calls
  it at all is the part source alone does not settle.
- The ``creator5_filament_sync`` module (landed only on the fork's
  stale ``something-bleeding`` branch at 1cc8f589,
  ``klippy/extras/creator5_filament_sync.py``; the same contract is
  pinned by tests on ``ace-pro``): key ``c5_tool<n>``, value
  ``{material, color, nozzle_temp, bed_temp, spool_id, lane}`` with
  the material uppercased, the color ``''`` or ``'#RRGGBB'`` and
  ``lane`` the tool index as a string.

The record fields agree; only the key and minor field presence
differ. This parser accepts both shapes and normalizes them into one
per-tool record shape, labeling every record with the publisher shape
it came from (``source``), so status and debug surfaces can say which
publisher an install is actually running.

Fail-closed rules, mirroring the #1304 discipline in
``runtime/lane_data_sync``:

- Records are attributed to tools by KEY number alone. Both
  publishers set the record's inner ``lane`` field from the key by
  construction, so the key is the authority; the inner field is never
  consulted. (This is also the only reading consistent with tranche
  2's identity AFC map: records keyed by file tool number describe
  that toolhead.)
- When both shapes are present for one tool and they disagree on
  material (compared by slicing family, the watcher's precedent: PLA
  and PLA+ are one claim, PLA and PLA-CF are not) or on color, the
  tool's record is a conflict: the claim fields are emptied and the
  ``conflict`` entry carries both publishers' normalized records.
  Nothing is guessed and nothing is merged silently. A cleared record
  beside a loaded one is a disagreement of exactly this kind.
- Unknown keys (the IFS ``laneN`` keys another watcher owns,
  Moonraker bookkeeping keys, tool numbers outside this model's
  range, non-record values) are ignored, never an error.
- A namespace value that is not a mapping at all is unknown (None),
  never an empty namespace; a mapping with no recognized keys is a
  genuinely empty result ({}), as in ``parse_lane_data``.

Nothing converges from these records and nothing writes the namespace
from this tranche: which publisher owns a live install's namespace is
exactly what the still-missing hardware capture decides, and the
adapter's spool writes are refused with a named reason instead (see
``OpenCreatorAdapter.lane_data_write_refusal`` and
``spool_sync.write_lane_side``).
"""
from __future__ import annotations

import re
from typing import Any

from ...core import filament_color

# The publisher shapes, as the ``source`` label on every record.
SOURCE_AFC = "afc"
SOURCE_FILAMENT_SYNC = "filament_sync"

_AFC_KEY = re.compile(r"T(\d+)")
_SYNC_KEY = re.compile(r"c5_tool(\d+)")


def _norm_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _norm_color(value: Any) -> str:
    """A published color as compared between publishers: hex digits when hex."""
    hex_code = filament_color.normalize_hex(value)
    return hex_code.casefold() if hex_code else _norm_text(value)


def _spool_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _temp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _normalize(source: str, tool: int, record: dict[str, Any]) -> dict[str, Any]:
    """One publisher's record for one tool, in the common shape."""
    weight = record.get("weight")
    return {
        "tool": int(tool),
        "extruder_index": _spool_id(record.get("extruder_index")),
        "material": str(record.get("material") or ""),
        "color": str(record.get("color") or ""),
        "spool_id": _spool_id(record.get("spool_id")),
        "nozzle_temp": _temp(record.get("nozzle_temp")),
        "bed_temp": _temp(record.get("bed_temp")),
        "name": str(record.get("name") or ""),
        "vendor_name": str(record.get("vendor_name") or ""),
        "weight": float(weight) if isinstance(weight, (int, float))
        and not isinstance(weight, bool) else None,
        "source": source,
        "conflict": None,
        "raw": record,
    }


def _claims(record: dict[str, Any]) -> tuple[str, str]:
    """What a normalized record claims is loaded: (material family, color).

    The conflict comparison. An empty record (a cleared lane) claims
    ("", ""), so a cleared record beside a loaded one disagrees, as it
    should: the publishers then hold different physical states.
    """
    from ...core.printer_state import material_family

    material = str(record.get("material") or "").strip()
    return (material_family(material) if material else "",
            _norm_color(record.get("color")))


def _merge(tool: int, afc: dict[str, Any] | None,
           sync: dict[str, Any] | None) -> dict[str, Any]:
    """One tool's two publisher records (either may be absent) as one record."""
    if afc is None:
        return sync  # type: ignore[return-value]
    if sync is None:
        return afc
    if _claims(afc) != _claims(sync):
        # Fail closed: no claim fields survive, both readings are
        # carried for a surface to show, and nothing is merged.
        return {
            "tool": int(tool),
            "material": "",
            "color": "",
            "spool_id": None,
            "nozzle_temp": None,
            "bed_temp": None,
            "name": "",
            "vendor_name": "",
            "weight": None,
            "source": f"{SOURCE_AFC}+{SOURCE_FILAMENT_SYNC}",
            "conflict": {SOURCE_AFC: afc, SOURCE_FILAMENT_SYNC: sync},
            "raw": {SOURCE_AFC: afc["raw"], SOURCE_FILAMENT_SYNC: sync["raw"]},
        }
    # The publishers agree on what is loaded. The filament sync record
    # is the Spoolman-linked one (its SET_SPOOL_ID path does AFC's
    # synchronous Spoolman lookup), so its spool id and temperatures
    # win when it carries them; the AFC record's name, vendor and
    # weight have no sync counterpart and are kept.
    merged = dict(afc)
    merged["source"] = f"{SOURCE_AFC}+{SOURCE_FILAMENT_SYNC}"
    merged["raw"] = {SOURCE_AFC: afc["raw"], SOURCE_FILAMENT_SYNC: sync["raw"]}
    if sync["spool_id"] is not None:
        merged["spool_id"] = sync["spool_id"]
    if sync["nozzle_temp"] is not None:
        merged["nozzle_temp"] = sync["nozzle_temp"]
    if sync["bed_temp"] is not None:
        merged["bed_temp"] = sync["bed_temp"]
    return merged


def parse_tool_lane_data(value: Any, tool_count: int = 4) -> dict[int, dict[str, Any]] | None:
    """{tool: normalized record} from a raw ``lane_data`` namespace value.

    Accepts both OpenCreator publisher shapes (``T<n>`` and
    ``c5_tool<n>`` keys, n inside the model's tool range) and ignores
    every other key. Returns None when the value is not a mapping at
    all (the namespace read is unknown, never empty), and {} when a
    mapping carries no recognized record.
    """
    if not isinstance(value, dict):
        return None
    by_tool: dict[int, dict[str, dict[str, Any]]] = {}
    for key, record in value.items():
        if not isinstance(record, dict):
            continue
        match = _AFC_KEY.fullmatch(str(key))
        source = SOURCE_AFC
        if match is None:
            match = _SYNC_KEY.fullmatch(str(key))
            source = SOURCE_FILAMENT_SYNC
        if match is None:
            continue
        tool = int(match.group(1))
        if not 0 <= tool < int(tool_count):
            continue
        by_tool.setdefault(tool, {})[source] = _normalize(source, tool, record)
    return {tool: _merge(tool, found.get(SOURCE_AFC), found.get(SOURCE_FILAMENT_SYNC))
            for tool, found in sorted(by_tool.items())}
