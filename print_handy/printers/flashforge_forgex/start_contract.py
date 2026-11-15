"""Forge-X 1.4.2 beta: which local screen the printer runs, and what that means for a start (#1506).

Forge-X documents two machine start forms (docs/SLICING.md on the ff5m
1.4.2 branch, checked 2026-11-15):

- Feather, Guppy or Headless: ``START_PRINT EXTRUDER_TEMP=.. BED_TEMP=..``
  on one line. This is the form Print Handy's Forge-X profiles slice.
- Stock screen: the same line followed by ``M190 S<bed>`` and
  ``M104 S<nozzle>``, because the stock screen's print path runs the
  file differently.

The active screen is the ``display`` setting of Forge-X's SET_MOD system,
published by Moonraker as ``mod_params.variables.display``. Its options are
STOCK, FEATHER, HEADLESS and GUPPY (mod_params.json, DisplayEnum); the
firmware's own macros compare it to 0 for stock, so both the name and the
number are accepted here. Nothing else is guessed: a missing or unrecognized
value is UNKNOWN.

Print Handy slices the Feather/Guppy/Headless form, so a printer that
positively reports the stock screen is refused before the start, with the
reason, rather than sent a file whose start form does not match. Calibration
routing and timelapse are separate work. Documented and config verified,
not hardware verified.
"""
from __future__ import annotations

import re

from enum import Enum


class ScreenMode(str, Enum):
    STOCK = "stock"
    FEATHER = "feather"
    HEADLESS = "headless"
    GUPPY = "guppy"
    UNKNOWN = "unknown"


# DisplayEnum: names as mod_params.json lists them; numbers as the macros compare them
# (display == 0 is the stock screen; 1 is Feather, per macros/headless.cfg).
_BY_NAME = {"STOCK": ScreenMode.STOCK, "FEATHER": ScreenMode.FEATHER,
            "HEADLESS": ScreenMode.HEADLESS, "GUPPY": ScreenMode.GUPPY}
_BY_NUMBER = {0: ScreenMode.STOCK, 1: ScreenMode.FEATHER}

# START_PRINT parameters Forge-X rejects: Z_OFFSET / SKIP_ZOFFSET are deprecated on Forge-X
# and cancel the print.
FORBIDDEN_START_PARAMS = ("Z_OFFSET", "SKIP_ZOFFSET")


def screen_mode(status) -> ScreenMode:
    """The Forge-X local screen a status sample reports, or UNKNOWN."""
    if not isinstance(status, dict):
        return ScreenMode.UNKNOWN
    mod = status.get("mod_params")
    variables = mod.get("variables") if isinstance(mod, dict) else None
    if not isinstance(variables, dict) or "display" not in variables:
        return ScreenMode.UNKNOWN
    value = variables["display"]
    if isinstance(value, bool):
        return ScreenMode.UNKNOWN
    if isinstance(value, int):
        return _BY_NUMBER.get(value, ScreenMode.UNKNOWN)
    if isinstance(value, str):
        return _BY_NAME.get(value.strip().strip("'\"").upper(), ScreenMode.UNKNOWN)
    return ScreenMode.UNKNOWN


def stock_screen_problem(status) -> str | None:
    """A readiness problem when the printer positively runs the stock screen."""
    if screen_mode(status) is not ScreenMode.STOCK:
        return None
    return ("this Forge-X printer is set to the Stock screen, and Print Handy's Forge-X profile "
            "slices the Feather/Guppy/Headless start form. Switch the display to Feather, "
            "Guppy or Headless (SET_MOD PARAM=display), or see docs/FORGEX.md")


# Per-print timelapse (#1506 tranche 2b). Forge-X's native timelapse is
# switched on at the printer (SET_MOD PARAM=timelapse VALUE=1); START_PRINT
# TIMELAPSE=0 skips it for one print, and START_PRINT refuses any value but
# 0 or 1 (config/stock.cfg in the 1.4.2 beta, read for behavior only). It
# cannot turn timelapse on for a print, so the setting has no "on".
TIMELAPSE_SETTING = "forgex_timelapse"
TIMELAPSE_CHOICES = ("printer", "off")
_START_RE = re.compile(r"^(\s*START_PRINT\b)(.*)$", re.IGNORECASE | re.MULTILINE)


def timelapse_choice(printer_settings) -> str:
    """The [printer] forgex_timelapse value, "printer" by default; ValueError when invalid."""
    table = printer_settings if isinstance(printer_settings, dict) else {}
    value = table.get(TIMELAPSE_SETTING, "printer")
    if value is None:
        value = "printer"
    if value not in TIMELAPSE_CHOICES:
        raise ValueError(f'[printer] {TIMELAPSE_SETTING} must be "printer" or "off", got {value!r}')
    return value


def with_timelapse_skip(text):
    """``text`` with TIMELAPSE=0 on its one START_PRINT line.

    Raises ValueError for a file with no START_PRINT line, more than one, or a
    line that already names TIMELAPSE, rather than guessing which to change.
    """
    matches = list(_START_RE.finditer(text if isinstance(text, str) else ""))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one START_PRINT line, found {len(matches)}")
    match = matches[0]
    words = {word.split("=", 1)[0].upper() for word in match.group(2).split()}
    if "TIMELAPSE" in words:
        raise ValueError("the START_PRINT line already names TIMELAPSE")
    line = match.group(1) + match.group(2).rstrip() + " TIMELAPSE=0"
    if forbidden_params(line):  # pragma: no cover - this rewrite never adds them
        raise ValueError("Forge-X start would carry a fatal parameter")
    return text[:match.start()] + line + text[match.end():]


def forbidden_params(line) -> list[str]:
    """Forge-X-fatal START_PRINT parameters on one start line."""
    if not isinstance(line, str) or not line.lstrip().upper().startswith("START_PRINT"):
        return []
    words = {word.split("=", 1)[0].upper() for word in line.split()[1:]}
    return [name for name in FORBIDDEN_START_PARAMS if name in words]
