"""Pure readers and command builders for OpenCreator's AFC surfaces (#1490)."""
from __future__ import annotations

import re
from typing import Any


# A lane answers under one of two object names, depending on how the
# unit that owns it registers it: a standalone toolhead lane is
# "AFC_lane <name>", a lane driven by a stepper unit is "AFC_stepper
# <name>". Both carry the same lane status.
LANE_PREFIXES = ("AFC_lane", "AFC_stepper")

_TOOL = re.compile(r"T(\d+)")
_HEAD = re.compile(r"extruder(\d+)")


def lane_names(status: Any) -> list[str] | None:
    """The lane names AFC itself lists, or None when AFC does not say.

    AFC.lanes is the authority for available lane names; never synthesize
    lane identifiers from a tool number. A malformed response is unknown.
    """
    if not isinstance(status, dict):
        return None
    afc = status.get("AFC")
    names = afc.get("lanes") if isinstance(afc, dict) else None
    if not isinstance(names, (list, tuple)):
        return None
    return [name.strip() for name in names if isinstance(name, str) and name.strip()]


def lane_object_names(status: Any) -> list[str]:
    """Every Moonraker object a listed lane could answer under, for a follow-up query."""
    return [f"{prefix} {name}" for name in lane_names(status) or [] for prefix in LANE_PREFIXES]


def parse_lanes(status: Any) -> dict[str, dict[str, Any]] | None:
    """Return lane-name keyed records from an AFC query, or None if unknown.

    Only a lane AFC lists is read, under whichever object name carries
    its status. A lane with no reporting object is left out, never
    invented.
    """
    names = lane_names(status)
    if names is None:
        return None
    lanes = {}
    for name in names:
        for prefix in LANE_PREFIXES:
            record = status.get(f"{prefix} {name}")
            if isinstance(record, dict) and record:
                lanes[name] = dict(record)
                break
    return lanes


def _mapped_tools(raw: Any) -> list[str]:
    if isinstance(raw, str):
        parts = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(part).strip() for part in raw]
    else:
        parts = []
    # NONE is the firmware's own placeholder for an unmapped entry.
    return [part for part in parts if part and part.upper() != "NONE"]


def ace_units(status: Any) -> list[str]:
    """Unit names AFC reports that are ACE units (their type starts with ACE)."""
    afc = status.get("AFC") if isinstance(status, dict) else None
    units = afc.get("units") if isinstance(afc, dict) else None
    if not isinstance(units, (list, tuple)):
        return []
    return [unit.strip() for unit in units
            if isinstance(unit, str) and unit.strip().upper().startswith("ACE")]


def resolve_tool_map(status: Any, tool_count: int = 4) -> tuple[dict[int, dict[str, Any]] | None, list[str]]:
    """The printer's own tool map: file tool -> the lane and toolhead it prints on.

    Read from the lanes AFC lists. Each lane's map names the T commands
    it answers and its extruder names the toolhead it feeds. The map is
    followed, whatever it is; it is refused only when it cannot be
    resolved to exactly one lane and one toolhead per tool. Returns
    (map, problems); the map is None whenever a problem is named.
    """
    names = lane_names(status)
    if names is None:
        return None, ["AFC lane list is not reporting; the printer's tool map cannot be read"]
    problems = []
    afc = status["AFC"]
    if afc.get("multiple_tool_mapping"):
        problems.append("AFC has multiple tool mapping on, so one lane can answer several tools; "
                        "Print Handy cannot tell which filament each tool prints with")
    lanes = parse_lanes(status) or {}
    by_tool: dict[int, list[str]] = {}
    heads: dict[str, int] = {}
    for name in names:
        lane = lanes.get(name)
        if lane is None:
            problems.append(f"AFC lane {name} is not reporting (its tool map cannot be read)")
            continue
        mapped = _mapped_tools(lane.get("map"))
        if not mapped:
            problems.append(f"AFC lane {name} does not report its tool map")
            continue
        head = _HEAD.fullmatch(str(lane.get("extruder") or ""))
        if head is None or not 0 <= int(head.group(1)) < int(tool_count):
            problems.append(f"AFC lane {name} does not report which toolhead it feeds "
                            f"(extruder is {lane.get('extruder')!r})")
            continue
        heads[name] = int(head.group(1))
        for entry in mapped:
            tool = _TOOL.fullmatch(entry)
            if tool is None:
                problems.append(f"AFC lane {name} is mapped to {entry}, which is not a tool")
            else:
                by_tool.setdefault(int(tool.group(1)), []).append(name)
    resolved: dict[int, dict[str, Any]] = {}
    for tool in range(int(tool_count)):
        owners = by_tool.get(tool) or []
        if len(owners) > 1:
            problems.append(f"T{tool} is mapped to more than one AFC lane ({', '.join(owners)})")
        elif not owners:
            if not problems:
                problems.append(f"T{tool} is not mapped to any AFC lane")
        else:
            resolved[tool] = {"lane": owners[0], "head": heads[owners[0]]}
    shared: dict[int, list[int]] = {}
    for tool, row in resolved.items():
        shared.setdefault(row["head"], []).append(tool)
    for head, tools in sorted(shared.items()):
        if len(tools) > 1:
            problems.append(f"toolhead {head + 1} is mapped to more than one tool "
                            f"({', '.join(f'T{t}' for t in tools)})")
    if problems:
        return None, problems
    return resolved, []


def describe_tool_map(tool_map: dict[int, dict[str, Any]]) -> str:
    """One line naming every tool's toolhead, for a remapped printer's records."""
    return ", ".join(f"T{tool} on toolhead {row['head'] + 1}" for tool, row in sorted(tool_map.items()))


def identity_map_problems(lanes: Any, tool_count: int = 4) -> list[str]:
    """Name every lane whose map is not the identity. Reporting only: readiness follows the map."""
    if not isinstance(lanes, dict):
        return ["AFC lane list is not reporting; its tool map cannot be checked"]
    problems = []
    for tool in range(int(tool_count)):
        name = f"extruder{tool}"
        lane = lanes.get(name)
        if not isinstance(lane, dict):
            problems.append(f"AFC lane {name} is not reporting; its map cannot be checked")
            continue
        mapped = _mapped_tools(lane.get("map"))
        if mapped != [f"T{tool}"]:
            shown = ", ".join(mapped) or "no tool"
            problems.append(f"AFC lane {name} is mapped to {shown}, not T{tool}")
    return problems


def parse_tool_records(value: Any, tool_count: int = 4) -> dict[int, dict[str, Any]] | None:
    """Read per-tool filament records, each with the toolhead it is on.

    A generic AFC record is keyed by the T command its lane answers and
    names the physical toolhead in extruder_index. The two differ on a
    remapped printer, and the record is kept under its tool with the
    toolhead beside it: that is the filament the tool prints with. A
    record with no usable extruder_index is omitted rather than guessed.
    The legacy c5_toolN shape is keyed by toolhead and has no map. An
    empty record remains a record.
    """
    if not isinstance(value, dict):
        return None
    out = {}
    for key, record in value.items():
        if not isinstance(record, dict):
            continue
        mt = _TOOL.fullmatch(str(key))
        legacy = re.fullmatch(r"c5_tool(\d+)", str(key)) if mt is None else None
        match = mt or legacy
        if match is None:
            continue
        keyed = int(match.group(1))
        if mt:
            head = record.get("extruder_index")
            if isinstance(head, bool) or not isinstance(head, int):
                continue
        else:
            head = keyed
        if 0 <= keyed < int(tool_count) and 0 <= head < int(tool_count):
            out[keyed] = {**record, "tool": keyed, "head": head}
    return out
