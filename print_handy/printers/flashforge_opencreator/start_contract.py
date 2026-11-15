"""The OpenCreator dispatcher's synthesized print start, modeled offline (#1490).

OpenCreator's Kalico fork owns job start: when a file carries no explicit
``C5_PRINT_START`` line in its first 64 KB, its virtual_sdcard dispatcher
synthesizes one from the file itself and runs it before the first file
line executes. The contract below is source-verified against the fork's
default branch (FlashForge-C5-Modding-Group/kalico ``something`` at
7ce49713, plus the shipped opencreator-fs config tree at e4b07a76, read
2026-11-14 for issue #1490); it is not hardware verified. This module is
an independent implementation from that contract description, in the
same facts-only spirit as Print Handy's ZMod contract code: no fork code
is copied.

The synthesis, as the fork specifies it:

- Scan windows: the first 65536 characters (header) and the last 65536
  characters (footer). Command classes scan header lines in order with
  comments stripped; first match wins within each class.
- An explicit ``C5_PRINT_START`` line anywhere in the header opts the
  file out of synthesis entirely; the file's own line runs instead.
- TOOL: the first bare ``T0`` to ``T3`` line, else the first
  ``AFC_SELECT_TOOL TOOL=extruderN`` line, else the first slot whose
  Orca ``filament used [mm]`` value is greater than 0, else 0.
- HOTEND: the first ``M109`` with S greater than 0, else the first
  ``M104`` with S greater than 0, else the selected tool's
  ``nozzle_temperature_initial_layer`` from Orca's config block.
- BED: the first ``M140`` or ``M190`` with S greater than 0, else 0.
- CHAMBER: the first ``M141`` or ``M191`` with S greater than 0, else
  the maximum ``chamber_temperature`` over used tools whose
  ``activate_chamber_temp_control`` is on, from the config block.
- USED_TOOLS: every slot with filament used greater than 0. Per-tool
  temperatures for the other used tools come from the same config list;
  IDLE values come from ``idle_temperature`` entries greater than 0.
- Per-slot config lists are read from the footer first, then the
  header, taking the last occurrence in the first source that contains
  the key. When the file is longer than the header window, the
  header's trailing partial line is dropped for config reads, because
  the window may have cut a config line in half.
- The dispatcher also fills MATERIALn from ``filament_type`` and
  FIRST_LAYER_HEIGHT from the config block; this model covers the
  tool and temperature fields Print Handy's verification compares,
  and does not track those two.
- No discoverable hotend temperature is the firmware's own failure
  mode: the job fails before any motion with a console reason.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

WINDOW = 65536

_EXPLICIT_RE = re.compile(r"^\s*C5_PRINT_START\b", re.IGNORECASE)
_TOOL_RE = re.compile(r"^T([0-3])(?:\s|$)")
_SELECT_TOOL_RE = re.compile(r"^AFC_SELECT_TOOL\s+TOOL=extruder([0-3]?)", re.IGNORECASE)
_TEMP_RE = re.compile(r"^M(104|109|140|141|190|191)\b[^;]*?\bS\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_ARG_RE = re.compile(r"([A-Z_0-9]+)=([+-]?(?:\d+(?:\.\d*)?|\.\d+))", re.IGNORECASE)
_CONFIG_RE = re.compile(r"^;\s*([A-Za-z0-9_ \[\]]+?)\s*=\s*(.*?)\s*$")


@dataclass(frozen=True)
class SynthesizedStart:
    """What the dispatcher would run for one file, or the explicit line's own values."""

    explicit: bool
    tool: int = 0
    hotend: float | None = None
    bed: float = 0.0
    chamber: float | None = None
    used_tools: tuple = ()
    hotends: dict = field(default_factory=dict)
    idles: dict = field(default_factory=dict)
    args: dict = field(default_factory=dict)


def _code(line):
    """One G-code line with its comment stripped, whitespace-trimmed."""
    return line.split(";", 1)[0].strip()


def _config_values(header, footer, key):
    """One per-slot config list: footer first, then header, last occurrence wins."""
    for source in (footer, header):
        found = None
        for line in source.splitlines():
            match = _CONFIG_RE.match(line)
            if match and match.group(1).strip().lower() == key:
                found = match.group(2)
        if found is not None:
            values = []
            for part in found.split(","):
                part = part.strip().strip('"').strip()
                try:
                    values.append(float(part))
                except ValueError:
                    values.append(0.0)
            return values
    return []


def _first_temp(lines, commands):
    for line in lines:
        match = _TEMP_RE.match(line)
        if match and int(match.group(1)) in commands:
            value = float(match.group(2))
            if value > 0:
                return value
    return None


def synthesize_start(text):
    """The start the OpenCreator dispatcher would synthesize for ``text``.

    Returns a SynthesizedStart. For an explicit-line file, ``explicit``
    is True and tool / hotend / bed carry that line's own TOOL, HOTEND
    and BED arguments (hotend None when the line names none).
    """
    text = text if isinstance(text, str) else ""
    header = text[:WINDOW]
    footer = text[-WINDOW:] if len(text) > WINDOW else text
    # Config lists are read from a header whose trailing partial line is
    # dropped when the window cut the file mid-line (the dispatcher's
    # material_header); command classes scan the header as it stands.
    config_header = header
    if len(text) > WINDOW and not header.endswith(("\n", "\r")):
        config_header = header.rpartition("\n")[0]
    raw_lines = header.splitlines()
    for line in raw_lines:
        if _EXPLICIT_RE.match(line):
            args = {k.upper(): float(v) for k, v in _ARG_RE.findall(_code(line))}
            tool = args.get("TOOL")
            return SynthesizedStart(
                explicit=True,
                tool=int(tool) if tool is not None else 0,
                hotend=args.get("HOTEND"),
                bed=args.get("BED", 0.0),
                chamber=args.get("CHAMBER"),
                args=args)
    lines = [_code(line) for line in raw_lines]
    used = _config_values(config_header, footer, "filament used [mm]")
    used_tools = tuple(i for i, v in enumerate(used[:4]) if v > 0)
    tool = None
    for line in lines:
        match = _TOOL_RE.match(line.upper())
        if match:
            tool = int(match.group(1))
            break
    if tool is None:
        for line in lines:
            match = _SELECT_TOOL_RE.match(line)
            if match:
                tool = int(match.group(1) or 0)
                break
    if tool is None:
        tool = used_tools[0] if used_tools else 0
    hotend = _first_temp(lines, (109,))
    if hotend is None:
        hotend = _first_temp(lines, (104,))
    temps = _config_values(config_header, footer, "nozzle_temperature_initial_layer")
    if hotend is None and 0 <= tool < len(temps) and temps[tool] > 0:
        hotend = temps[tool]
    bed = _first_temp(lines, (140, 190)) or 0.0
    chamber = _first_temp(lines, (141, 191))
    if chamber is None:
        chamber_temps = _config_values(config_header, footer, "chamber_temperature")
        active = _config_values(config_header, footer, "activate_chamber_temp_control")
        candidates = [chamber_temps[i] for i in (used_tools or (tool,))
                      if i < len(chamber_temps)
                      and (i >= len(active) or active[i] > 0)]
        chamber = max(candidates) if candidates and max(candidates) > 0 else None
    hotends = {i: temps[i] for i in used_tools
               if i != tool and i < len(temps) and temps[i] > 0}
    idle_values = _config_values(config_header, footer, "idle_temperature")
    idles = {i: idle_values[i] for i in range(min(4, len(idle_values)))
             if idle_values[i] > 0}
    return SynthesizedStart(
        explicit=False, tool=tool, hotend=hotend, bed=bed, chamber=chamber,
        used_tools=used_tools, hotends=hotends, idles=idles)


def start_problems(text, bed, hotend):
    """Verification problems for one file against the job's bed and hotend.

    Empty when the start the firmware would run matches the job: for a
    synthesized start, the synthesized BED and HOTEND; for an explicit
    line, that line's own HOTEND (and BED when it names one). A file
    with no discoverable hotend temperature names the firmware's own
    failure mode, because that is what the printer would do with it.
    """
    import math

    start = synthesize_start(text)
    problems = []
    if start.hotend is None:
        problems.append(
            "no hotend temperature is discoverable in the G-code header or config block; "
            "the firmware would fail this job before motion with 'No hotend temperature "
            "found in G-code header; add C5_PRINT_START TOOL=n HOTEND=n BED=n'")
        return problems
    if not math.isclose(start.hotend, float(hotend), abs_tol=1e-3):
        problems.append(
            f"the start the firmware would synthesize runs hotend {start.hotend:g} C, "
            f"not the job's {float(hotend):g} C")
    if start.explicit:
        if "BED" in start.args and not math.isclose(start.bed, float(bed), abs_tol=1e-3):
            problems.append(
                f"the file's C5_PRINT_START line runs bed {start.bed:g} C, "
                f"not the job's {float(bed):g} C")
    elif not math.isclose(start.bed, float(bed), abs_tol=1e-3):
        problems.append(
            f"the start the firmware would synthesize runs bed {start.bed:g} C, "
            f"not the job's {float(bed):g} C")
    if not 0 <= start.tool <= 3:
        problems.append(f"the start the firmware would synthesize selects unknown tool T{start.tool}")
    return problems
