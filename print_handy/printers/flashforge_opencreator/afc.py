"""Pure readers and command builders for OpenCreator's AFC surfaces (#1490)."""
from __future__ import annotations

from typing import Any


def parse_lanes(status: Any) -> dict[str, dict[str, Any]] | None:
    """Return lane-name keyed records from an AFC query, or None if unknown.

    AFC.lanes is the authority for available lane names; never synthesize
    lane identifiers from a tool number. A malformed response is unknown.
    """
    if not isinstance(status, dict):
        return None
    afc = status.get("AFC")
    names = afc.get("lanes") if isinstance(afc, dict) else None
    if not isinstance(names, (list, tuple)):
        return None
    lanes = {}
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        record = status.get(f"AFC_lane {name}")
        if isinstance(record, dict):
            lanes[name] = dict(record)
    return lanes


def identity_map_problems(lanes: Any, tool_count: int = 4) -> list[str]:
    """Fail closed on missing or non-identity lanes, naming every remap."""
    if not isinstance(lanes, dict):
        return ["AFC lane list is not reporting; its tool map cannot be checked"]
    problems = []
    for tool in range(int(tool_count)):
        name = f"extruder{tool}"
        lane = lanes.get(name)
        if not isinstance(lane, dict):
            problems.append(f"AFC lane {name} is not reporting; its map cannot be checked")
            continue
        raw = lane.get("map")
        mapped = ([x.strip() for x in raw.split(",") if x.strip()]
                  if isinstance(raw, str) else
                  [str(x).strip() for x in raw if str(x).strip()]
                  if isinstance(raw, (list, tuple, set)) else [])
        if mapped != [f"T{tool}"]:
            shown = ", ".join(mapped) or "no tool"
            problems.append(f"AFC lane {name} is mapped to {shown}, not T{tool}; restore the identity map before printing")
    return problems


def parse_tool_records(value: Any, tool_count: int = 4) -> dict[int, dict[str, Any]] | None:
    """Read T-keyed records and resolve physical head from extruder_index.

    Supports the legacy c5_toolN shape defensively. For AFC records the
    key number and physical extruder_index must agree; disagreement is
    omitted rather than guessed. An empty record remains a record.
    """
    if not isinstance(value, dict):
        return None
    import re
    out = {}
    for key, record in value.items():
        if not isinstance(record, dict):
            continue
        mt = re.fullmatch(r"T(\d+)", str(key))
        legacy = re.fullmatch(r"c5_tool(\d+)", str(key)) if mt is None else None
        if mt is None and legacy is None:
            continue
        match = mt or legacy
        assert match is not None
        keyed = int(match.group(1))
        if mt:
            head = record.get("extruder_index")
            if isinstance(head, bool) or not isinstance(head, int) or head != keyed:
                continue
        else:
            head = keyed
        if 0 <= head < int(tool_count):
            out[head] = {**record, "tool": head}
    return out


def bind_commands(lane: str, spool: dict[str, Any] | None, *, spoolman_enabled: bool) -> list[str]:
    """Printer-side spool bind commands for one lane; caller must enforce idle."""
    if not isinstance(lane, str) or not lane.strip() or any(c.isspace() for c in lane.strip()):
        raise ValueError("invalid AFC lane name")
    lane = lane.strip()
    if spoolman_enabled:
        if not isinstance(spool, dict) or isinstance(spool.get("id"), bool) or not isinstance(spool.get("id"), int):
            raise ValueError("a Spoolman spool id is required")
        return [f"SET_SPOOL_ID LANE={lane} SPOOL_ID={spool['id']}"]
    if not isinstance(spool, dict):
        return [f"SET_MATERIAL LANE={lane} MATERIAL=", f"SET_COLOR LANE={lane} COLOR="]
    material = str(spool.get("material") or "")
    color = str(spool.get("color") or spool.get("color_hex") or "")
    for value in (material, color):
        if any(c in value for c in "\r\n"):
            raise ValueError("unsafe AFC value")
    return [f"SET_MATERIAL LANE={lane} MATERIAL={material}", f"SET_COLOR LANE={lane} COLOR={color}"]
