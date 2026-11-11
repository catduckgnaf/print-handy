"""Printer accessories: hardware fitted to one unit, not shipped with its model (#1198).

An accessory sits beside the printer model the way a model sits beside its
vendor. It is per-unit (two printers of the same model differ because one has
it fitted), retrofittable without changing the host printer's identity, and it
changes behavior: slicing, print start, status or readiness, or filament and
color assignment. A purely passive add-on is not an accessory. Each accessory
has a verified detection signal or a manual record, never a guessed object
name. One position-shaping accessory at a time determines a printer's
effective color architecture; sensor and thermal accessories stack alongside
it orthogonally.

The first accessory is the Bondtech INDX toolchanger on a Klipper (Moonraker)
host: N passive tools, one shared extruder and one shared heater, exactly one
active tool, and no heating for a docked tool. Its effective color system is
"shared_tool", conferred by the accessory only; no printer model declares it.

INDX facts here are interface facts verified against the upstream Klipper
package (BondtechAB/INDX and BondtechAB/indx_klipper, checked 2026-11-11):
detection is the Moonraker configfile settings payload, the tool count is
``gcode_macro TOOL_POSITIONS`` ``tool_count`` with no upstream cap, the live
active tool is ``save_variables.variables.active_tool`` (-1 means an empty
head), and the named refusals are the persistent ``seat_suspect`` flag and
``docks_calibrated`` being false. Per-tool nozzle size, nozzle type and
filament are not detectable upstream; they are recorded manually on the
accessory record. The thermal numbers upstream stores per tool are never
mapped back to a material.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AccessoryProfile:
    """One known accessory: its key, kind, host requirements and behavior change."""

    key: str  # "<vendor>.<accessory>", the [[accessories]] key value
    label: str
    kind: str  # "toolchanger" is the first kind; sensor/thermal kinds stack
    # The effective color system this accessory confers on its host, or None
    # when it does not shape positions. "shared_tool" exists only here.
    confers_color_system: str | None = None
    # Vendor connection types the accessory can attach over (CONNECTIONS).
    connections: tuple[str, ...] = ()
    # The verified detection signal, or "manual only" when there is none.
    detection: str = "manual only"
    notes: str = ""


ACCESSORIES: dict[str, AccessoryProfile] = {
    "bondtech.indx": AccessoryProfile(
        key="bondtech.indx",
        label="Bondtech INDX",
        kind="toolchanger",
        confers_color_system="shared_tool",
        connections=("moonraker",),
        detection=(
            "Moonraker configfile settings carrying an [indx] section, an "
            "[mcu indxmcu] section, and an [extruder] with sensor_type "
            "\"indx\"; the tool count comes from [gcode_macro "
            "TOOL_POSITIONS] tool_count"),
        notes=(
            "Variable tool count with no upstream cap; passive tools with "
            "no heater of their own, one shared extruder and one shared "
            "heater on the Smart Head; a docked tool cannot be heated."),
    ),
}


def configured_accessories(config: dict | None) -> tuple[AccessoryProfile, ...]:
    """The accessory profiles a settings dict configures, in file order.

    ``config`` is a settings-shaped dict; its top-level "accessories" list
    holds one table per fitted accessory, each naming its registry ``key``.
    An unknown key is a ValueError naming it, never a silently dropped entry.
    """
    entries = (config or {}).get("accessories") or []
    profiles = []
    for entry in entries:
        key = entry if isinstance(entry, str) else (
            entry.get("key") if isinstance(entry, dict) else None)
        if not isinstance(key, str) or key not in ACCESSORIES:
            raise ValueError(
                f"unknown accessory {key!r} (known accessories: "
                f"{', '.join(sorted(ACCESSORIES))})")
        profiles.append(ACCESSORIES[key])
    return tuple(profiles)


def accessory_entry(config: dict | None, key: str) -> dict | None:
    """The raw configured entry for one accessory key, or None when not fitted."""
    for entry in (config or {}).get("accessories") or []:
        if isinstance(entry, dict) and entry.get("key") == key:
            return entry
    return None


def indx_entry(config: dict | None) -> dict | None:
    """The configured Bondtech INDX entry on this printer, or None."""
    return accessory_entry(config, "bondtech.indx")


@dataclass(frozen=True)
class ToolRecord:
    """One manually recorded INDX tool: its nozzle and filament, or its
    non-printing role. A tool with ``heats`` false is a no-heat dock position
    (a pen, cutter or camera upstream): it counts toward the tool count and
    carries no nozzle or filament record."""

    tool: int
    nozzle_size_mm: float | None = None
    nozzle_type: str | None = None
    filament: str = ""
    heats: bool = True


INDX_NOZZLE_TYPES = ("cht", "standard")


def _tool_count(entry: dict) -> int:
    count = entry.get("tool_count") if isinstance(entry, dict) else None
    if isinstance(count, bool) or not isinstance(count, int):
        raise ValueError(f"INDX tool_count must be a whole number, got {count!r}")
    if count < 2:
        raise ValueError(f"INDX tool_count must be at least 2, got {count}")
    return count


def indx_tool_count(entry: dict | None) -> int | None:
    """The validated tool count of an INDX entry, or None when there is none.

    There is deliberately no upper bound: upstream sets no cap, and dock
    space on the host is the only limit (community SV08 Max fits reach 15).
    """
    if not isinstance(entry, dict):
        return None
    return _tool_count(entry)


def parse_indx_tools(entry: dict) -> tuple[ToolRecord, ...]:
    """Validate an INDX accessory entry and return its recorded tools.

    The entry names ``tool_count`` (a whole number, at least 2, no maximum)
    and may list ``tools``: one table per recorded tool with a 0-based
    ``tool`` index below the count, a ``nozzle_size_mm`` above 0, a
    ``nozzle_type`` of "cht" or "standard", and a ``filament`` name. A tool
    with ``heats = false`` is a no-heat dock position and must carry no
    nozzle or filament record. Every problem is a ValueError naming it.
    """
    if not isinstance(entry, dict):
        raise ValueError(f"an INDX accessory entry must be a table, got {entry!r}")
    count = _tool_count(entry)
    raw_tools = entry.get("tools") or []
    if not isinstance(raw_tools, list):
        raise ValueError(f"INDX tools must be a list of tool tables, got {raw_tools!r}")
    records: list[ToolRecord] = []
    seen: set[int] = set()
    for raw in raw_tools:
        if not isinstance(raw, dict):
            raise ValueError(f"an INDX tool entry must be a table, got {raw!r}")
        tool = raw.get("tool", raw.get("index"))
        if isinstance(tool, bool) or not isinstance(tool, int):
            raise ValueError(f"INDX tool index must be a whole number, got {tool!r}")
        if not 0 <= tool < count:
            raise ValueError(
                f"INDX tool index {tool} is outside the tool count {count} "
                f"(tools are 0 to {count - 1})")
        if tool in seen:
            raise ValueError(f"INDX tool {tool} is recorded twice")
        seen.add(tool)
        heats = raw.get("heats", True)
        if not isinstance(heats, bool):
            raise ValueError(f"INDX tool {tool}: heats must be true or false, got {heats!r}")
        filament = raw.get("filament")
        if filament is not None and not isinstance(filament, str):
            raise ValueError(f"INDX tool {tool}: filament must be a name, got {filament!r}")
        if not heats:
            if raw.get("nozzle_size_mm") is not None or raw.get("nozzle_type") is not None \
                    or (filament or ""):
                raise ValueError(
                    f"INDX tool {tool} has heats = false, so it is a no-heat tool and "
                    "must carry no nozzle or filament record")
            records.append(ToolRecord(tool=tool, heats=False))
            continue
        size = raw.get("nozzle_size_mm")
        if isinstance(size, bool) or not isinstance(size, (int, float)) or not size > 0:
            raise ValueError(
                f"INDX tool {tool}: nozzle_size_mm must be a number above 0, got {size!r}")
        nozzle_type = raw.get("nozzle_type")
        if nozzle_type not in INDX_NOZZLE_TYPES:
            raise ValueError(
                f"INDX tool {tool}: nozzle_type must be one of "
                f"{', '.join(INDX_NOZZLE_TYPES)}, got {nozzle_type!r}")
        records.append(ToolRecord(tool=tool, nozzle_size_mm=float(size),
                                  nozzle_type=nozzle_type,
                                  filament=(filament or "").strip()))
    return tuple(sorted(records, key=lambda record: record.tool))


def _settings_section(settings: dict, name: str) -> dict:
    """One configfile settings section, matched case-insensitively.

    Klipper lower-cases option names and the section keys Print Handy reads
    arrive lower-cased in practice, but a hand-built fixture may not be, so
    the lookup folds case instead of trusting it.
    """
    for key, value in (settings or {}).items():
        if isinstance(key, str) and key.lower() == name and isinstance(value, dict):
            return value
    return {}


def detect_indx(configfile_settings: dict | None,
                object_names: Any = None) -> int | None:
    """The INDX tool count a Moonraker configfile settings payload proves, or None.

    Detection requires all of: an [indx] section, an [mcu indxmcu] section,
    and an [extruder] whose sensor_type is "indx". The count is read from
    [gcode_macro TOOL_POSITIONS] tool_count only: counting shortcut macros
    or dock pairs misreports real installs, whose shipped files disagree
    with tool_count. ``object_names`` (a Moonraker objects/list payload) is
    accepted as corroboration context and never substitutes for the
    configfile signal. No signal, no count: nothing is guessed.
    """
    settings = configfile_settings if isinstance(configfile_settings, dict) else {}
    if not _settings_section(settings, "indx"):
        return None
    if not _settings_section(settings, "mcu indxmcu"):
        return None
    extruder = _settings_section(settings, "extruder")
    if str(extruder.get("sensor_type") or "").strip().lower() != "indx":
        return None
    positions = _settings_section(settings, "gcode_macro tool_positions")
    raw_count = positions.get("tool_count")
    try:
        count = int(str(raw_count).strip())
    except (TypeError, ValueError):
        return None
    return count if count >= 1 else None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def active_tool(variables: dict | None) -> int | None:
    """The tool on the head from save_variables variables, or None.

    ``active_tool`` is an int: -1 (or a missing or unreadable value) means
    the head is empty, so there is no active tool to name.
    """
    value = _as_int((variables or {}).get("active_tool"))
    return value if value is not None and value >= 0 else None


def indx_refusals(variables: dict | None) -> list[str]:
    """The INDX refusal states a variables dict names, in plain words.

    ``seat_suspect`` at 0 or above is the persistent half-seated-tool flag:
    it survives restarts and blocks tool changes until an operator clears
    it by hand. ``docks_calibrated`` false is the shipped default on a
    fresh install and refuses every tool change until the docks are
    calibrated. Both are surfaced by name, never as a bare pause.
    """
    variables = variables if isinstance(variables, dict) else {}
    refusals = []
    suspect = _as_int(variables.get("seat_suspect"))
    if suspect is not None and suspect >= 0:
        refusals.append(
            f"INDX tool T{suspect} may be half seated on the head "
            "(seat_suspect); tool changes are refused until it is cleared "
            "by hand with MANUAL_TOOL_REMOVE, MANUAL_TOOL_SEAT or "
            "MANUAL_TOOLHEAD_RESET")
    calibrated = variables.get("docks_calibrated")
    if calibrated is False or (isinstance(calibrated, str)
                               and calibrated.strip().lower() == "false"):
        refusals.append(
            "INDX docks are not calibrated (docks_calibrated is False); "
            "tool changes are refused until the docks are calibrated")
    return refusals


# The OrcaSlicer tool change G-code for INDX, verbatim from the upstream
# slicer contract: the incoming tool is picked up first, then heated
# straight to its own filament's temperature.
INDX_TOOL_CHANGE_GCODE = "CHANGE_TOOL TOOL={next_extruder} TEMP={new_filament_temp}"


def indx_machine_overrides(tools: Any) -> dict[str, Any]:
    """Orca machine-profile overrides for a shared-tool INDX slice.

    ``tools`` is an INDX accessory entry (a dict with tool_count and its
    recorded tools) or an iterable of ToolRecord. Host templates are
    single-extruder, so the overrides expand the per-extruder arrays to
    the tool count, keep single_extruder_multi_material off (INDX is a
    true tool changer, not a filament switcher), set the tool change
    G-code to the INDX CHANGE_TOOL form under the key this repository's
    templates actually use (``change_filament_gcode``), and give each
    tool its recorded nozzle diameter. A tool with no record, and a
    no-heat tool, take the first recorded diameter, or 0.4 mm when
    nothing is recorded.
    """
    if isinstance(tools, dict):
        count = _tool_count(tools)
        records = parse_indx_tools(tools)
    else:
        records = tuple(tools or ())
        count = max((record.tool for record in records), default=-1) + 1
        if count < 2:
            raise ValueError(
                "indx_machine_overrides needs an INDX entry or tool records "
                f"covering at least 2 tools, got {count}")
    by_tool = {record.tool: record for record in records}
    recorded_sizes = [record.nozzle_size_mm for record in records
                      if record.nozzle_size_mm]
    default_size = recorded_sizes[0] if recorded_sizes else 0.4
    diameters = []
    for tool in range(count):
        record = by_tool.get(tool)
        size = record.nozzle_size_mm if record and record.nozzle_size_mm else default_size
        diameters.append(f"{size:g}")
    return {
        "single_extruder_multi_material": "0",
        "change_filament_gcode": INDX_TOOL_CHANGE_GCODE,
        "nozzle_diameter": diameters,
        "extruder_colour": ["#FFFFFF"] * count,
        "extruder_offset": ["0x0"] * count,
        "max_layer_height": [f"{0.28:g}"] * count,
        "min_layer_height": [f"{0.08:g}"] * count,
    }
