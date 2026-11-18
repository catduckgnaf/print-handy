"""Reforge's print start, modeled offline from its published source.

Reforge (Klipper4FlashForge/firmware) owns job start through its
``[ff_print]`` Klipper extra. It wraps ``SDCARD_PRINT_FILE`` and ``M23``,
reads a few values out of the file itself, and runs its
``FF_BEFORE_PRINT_START`` macro before the file's first line executes;
that macro runs ``_FF_PREFLIGHT`` and, while its ``prepare`` variable is
1 (the shipped default), ``START_PRINT`` with the derived values. Print
end is ``FF_AFTER_PRINT_END``, run once the job leaves the printing state,
which calls ``END_PRINT``.

Sources, read on 2026-11-17 at Reforge master ``eda86c1``:
``pkgs/klipper/payload/klipper/klippy/extras/ff_print.py`` (the
derivation) and ``pkgs/klipper-config/payload/config/ff-print-macros.cfg``
(``FF_BEFORE_PRINT_START``, ``START_PRINT``, ``END_PRINT``), plus the
project's ``docs/how-a-print-runs.md`` and ``docs/printing.md``. Source
verified, not hardware verified. This is an independent implementation
of the behavior those files describe; no Reforge code is copied.

The derivation, as ``ff_print.py`` specifies it, over the first 256 KiB
of the file (``HEAD_BYTES``), each value from the first matching line:

- bed: ``^M1[49]0 S<t>`` (``M140`` or ``M190``), kept only when above 0,
  truncated to whole degrees;
- nozzle: ``^M10[49] S<t>`` (``M104`` or ``M109``), the same rules;
- tool: ``^T([0-3])\\b``, the file's initial tool;
- layer: ``^;HEIGHT:<h>``, the first layer's height.

What each missing value means on the printer (``START_PRINT``):
``NOZZLE`` falls back to 220 C, so the first tool would be cleaned and
its print offset computed at a temperature the job never asked for; no
``TOOL`` means no tool is checked, cleaned or grabbed before the file
starts; no ``LAYER`` drops the first-layer term from the print Z offset.
Print Handy's own start form carries all four, and verification refuses
a file that lacks any of them.

Two kinds of line also conflict with the automatic preparation and are
refused in the header: a ``START_PRINT`` line (the machine would be
prepared twice; Reforge says to turn ``prepare`` off for that path) and a
``G28`` line (Reforge's ``G28`` wrapper docks a mounted tool before homing
Z, so it would dock the tool the preparation just grabbed).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

HEAD_BYTES = 256 * 1024
# START_PRINT's own default when no NOZZLE= reaches it (ff-print-macros.cfg).
START_PRINT_DEFAULT_NOZZLE = 220

_BED_RE = re.compile(r"^M1[49]0 S([0-9]+(?:\.[0-9]+)?)", re.M)
_NOZZLE_RE = re.compile(r"^M10[49] S([0-9]+(?:\.[0-9]+)?)", re.M)
_TOOL_RE = re.compile(r"^T([0-3])\b", re.M)
_LAYER_RE = re.compile(r"^;HEIGHT:([0-9.]+)", re.M)
_CHAMBER_RE = re.compile(r"^M1[49]1\s+S\s*([0-9]+(?:\.[0-9]+)?)", re.M)
_CONFLICT_RE = re.compile(r"^\s*(START_PRINT|G28)\b", re.M | re.I)


@dataclass(frozen=True)
class DerivedStart:
    """What ff_print hands FF_BEFORE_PRINT_START for one file; None where it finds nothing."""

    bed: int | None
    nozzle: int | None
    tool: int | None
    layer: float | None


def head(text) -> str:
    """The part of the file ff_print reads: its first 256 KiB."""
    text = text if isinstance(text, str) else ""
    data = text.encode("utf-8")
    if len(data) <= HEAD_BYTES:
        return text
    return data[:HEAD_BYTES].decode("utf-8", "replace")


def _positive_int(match):
    if match is None:
        return None
    value = float(match.group(1))
    return int(value) if value > 0 else None


def derive_start(text) -> DerivedStart:
    """The BED, NOZZLE, TOOL and LAYER ff_print would derive from ``text``."""
    window = head(text)
    tool = _TOOL_RE.search(window)
    layer = _LAYER_RE.search(window)
    try:
        layer_value = float(layer.group(1)) if layer else None
    except ValueError:
        layer_value = None
    return DerivedStart(
        bed=_positive_int(_BED_RE.search(window)),
        nozzle=_positive_int(_NOZZLE_RE.search(window)),
        tool=int(tool.group(1)) if tool else None,
        layer=layer_value)


def start_problems(text, bed, hotend, *, max_chamber=None):
    """Verification problems for one file against the job's bed and hotend.

    Empty when what ff_print derives from the file is what the job
    asked for and nothing in the header conflicts with the automatic
    preparation. With ``max_chamber`` (the Pro's recorded ceiling), a
    chamber target above it is refused: the firmware would clamp only
    to its own, higher, config limit.
    """
    window = head(text)
    start = derive_start(text)
    problems = []
    for match in _CONFLICT_RE.finditer(window):
        command = match.group(1).upper()
        if command == "START_PRINT":
            problems.append(
                "the file carries its own START_PRINT line; Reforge's ff_print already "
                "runs START_PRINT before the file starts, so the printer would prepare twice")
        else:
            problems.append(
                "the file homes with G28 in its header; Reforge's G28 docks a mounted tool "
                "before homing Z, so it would dock the tool the print start just picked up")
        break
    if start.nozzle is None:
        problems.append(
            "no M104 or M109 nozzle temperature is in the first 256 KiB of the file; "
            "Reforge's START_PRINT would fall back to its default of "
            f"{START_PRINT_DEFAULT_NOZZLE} C for the first tool")
    elif not math.isclose(start.nozzle, float(hotend), abs_tol=1e-3):
        problems.append(
            f"Reforge's print start reads the nozzle as {start.nozzle} C (whole degrees, from "
            f"the file's first M104 or M109), not the job's {float(hotend):g} C")
    if start.bed is None:
        if float(bed) > 0:
            problems.append(
                "no M140 or M190 bed temperature is in the first 256 KiB of the file; "
                "Reforge's START_PRINT would not heat the bed")
    elif not math.isclose(start.bed, float(bed), abs_tol=1e-3):
        problems.append(
            f"Reforge's print start reads the bed as {start.bed} C (whole degrees, from the "
            f"file's first M140 or M190), not the job's {float(bed):g} C")
    if start.tool is None:
        problems.append(
            "no T0 to T3 line is in the first 256 KiB of the file; Reforge's START_PRINT "
            "would check, clean and pick up no tool before the file starts")
    if start.layer is None:
        problems.append(
            "no ;HEIGHT: first-layer line is in the first 256 KiB of the file; Reforge's "
            "print Z offset would lose its first-layer term")
    if max_chamber is not None:
        chamber = _CHAMBER_RE.search(window)
        if chamber is not None and float(chamber.group(1)) > float(max_chamber) + 1e-3:
            problems.append(
                f"the start runs the chamber at {float(chamber.group(1)):g} C, above this "
                f"printer's {float(max_chamber):g} C chamber ceiling")
    return problems


def prepare_problems(status) -> list[str]:
    """Readiness problems for the automatic preparation itself (ff_print, FF_BEFORE_PRINT_START).

    Print Handy's files rely on the shipped automatic path: ff_print
    must be reporting, and FF_BEFORE_PRINT_START must report ``prepare``
    on (with it off, nothing homes before the file's first move) and
    ``defer_mesh`` off (with it on, START_PRINT leaves the mesh, the
    first tool grab and the print offset to an ADAPTIVE_MESH call that
    Print Handy's files do not make). A ``defer_mesh`` the macro does
    not report is a build without that option, which never defers.
    """
    status = status if isinstance(status, dict) else {}
    problems = []
    if not isinstance(status.get("ff_print"), dict) or not status.get("ff_print"):
        problems.append("ff_print is not reporting (Reforge's print start cannot be checked)")
    macro = status.get("gcode_macro FF_BEFORE_PRINT_START")
    if not isinstance(macro, dict) or not macro:
        problems.append("FF_BEFORE_PRINT_START is not reporting (whether the printer prepares "
                        "itself before the file starts cannot be checked)")
        return problems
    prepare = macro.get("prepare")
    if isinstance(prepare, bool) or prepare not in (0, 1):
        problems.append(f"FF_BEFORE_PRINT_START reports no readable prepare setting ({prepare!r})")
    elif prepare != 1:
        problems.append(
            "FF_BEFORE_PRINT_START has prepare off, so the printer would not home, heat or pick "
            "up a tool before the file starts; Print Handy's files rely on it being on")
    defer = macro.get("defer_mesh", 0)
    if isinstance(defer, bool) or defer not in (0, 1):
        problems.append(f"FF_BEFORE_PRINT_START reports no readable defer_mesh setting ({defer!r})")
    elif defer == 1:
        problems.append(
            "FF_BEFORE_PRINT_START has defer_mesh on, which leaves the mesh, the first tool "
            "and the print offset to an ADAPTIVE_MESH call Print Handy's files do not make")
    return problems
