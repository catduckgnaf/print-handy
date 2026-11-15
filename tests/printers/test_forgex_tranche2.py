"""Forge-X tranche 2 (#1506): screen mode and the start contract it decides.

Forge-X documents one machine start form for Feather, Guppy and Headless
(the form these profiles slice) and a different one for the stock screen
(the same line plus M190/M104). The screen is mod_params.variables.display
(DisplayEnum STOCK/FEATHER/HEADLESS/GUPPY; the macros compare 0 for stock).
A printer that positively reports the stock screen is refused by name; an
unknown or missing value is not guessed, and adds no problem of its own.
"""
import pytest

from print_handy.printers.flashforge_forgex import start_contract as sc
from print_handy.printers.flashforge_forgex.start_contract import ScreenMode
from .test_adapters import adapter_for

KEYS = ("flashforge_forgex.ad5m", "flashforge_forgex.ad5m_pro")


def _status(display=None, **extra):
    status = {"print_stats": {"state": "standby"}, "webhooks": {"state": "ready"},
              "extruder": {"temperature": 24.0}, "heater_bed": {"temperature": 24.0}}
    if display is not None:
        status["mod_params"] = {"variables": {"display": display}}
    status.update(extra)
    return status


@pytest.mark.parametrize("value, mode", [
    ("STOCK", ScreenMode.STOCK), ("stock", ScreenMode.STOCK), ("'STOCK'", ScreenMode.STOCK),
    (0, ScreenMode.STOCK), ("FEATHER", ScreenMode.FEATHER), (1, ScreenMode.FEATHER),
    ("GUPPY", ScreenMode.GUPPY), ("HEADLESS", ScreenMode.HEADLESS),
    ("SOMETHING_NEW", ScreenMode.UNKNOWN), (7, ScreenMode.UNKNOWN), (True, ScreenMode.UNKNOWN),
    (None, ScreenMode.UNKNOWN),
])
def test_screen_mode_reads_the_display_mod_param(value, mode):
    assert sc.screen_mode(_status(value) if value is not None else {"mod_params": {}}) is mode


def test_screen_mode_without_mod_params_is_unknown():
    assert sc.screen_mode({}) is ScreenMode.UNKNOWN
    assert sc.screen_mode(None) is ScreenMode.UNKNOWN
    assert sc.screen_mode({"mod_params": {"variables": {}}}) is ScreenMode.UNKNOWN


@pytest.mark.parametrize("key", KEYS)
def test_stock_screen_is_refused_by_name_and_the_others_pass(key):
    adapter = adapter_for(key)
    problems = adapter.readiness_problems({}, _status("STOCK"))
    assert any("Stock screen" in p and "SET_MOD PARAM=display" in p for p in problems)
    for display in ("FEATHER", "GUPPY", "HEADLESS", 1):
        assert adapter.readiness_problems({}, _status(display)) == []
    # Unknown is not guessed either way: no screen problem is added.
    assert adapter.readiness_problems({}, _status("SOMETHING_NEW")) == []


@pytest.mark.parametrize("key", KEYS)
def test_start_is_the_single_line_alternate_screen_form_without_fatal_params(key):
    adapter = adapter_for(key)
    plan = adapter.start_plan(60, 215)
    start = [line for line in plan if line.startswith("START_PRINT")]
    assert start == ["START_PRINT EXTRUDER_TEMP=215 BED_TEMP=60"]
    assert not any(line.startswith(("M190", "M104")) for line in plan)
    machine = adapter.machine_start_gcode("anything", "auto")
    assert sc.forbidden_params(machine.splitlines()[-1]) == []


def test_forbidden_params_names_the_fatal_ones():
    assert sc.forbidden_params("START_PRINT EXTRUDER_TEMP=215 BED_TEMP=60 Z_OFFSET=0.1") == ["Z_OFFSET"]
    assert sc.forbidden_params("START_PRINT SKIP_ZOFFSET=1 Z_OFFSET=0") == ["Z_OFFSET", "SKIP_ZOFFSET"]
    assert sc.forbidden_params("START_PRINT EXTRUDER_TEMP=215 BED_TEMP=60") == []
    assert sc.forbidden_params("M104 S215") == []


@pytest.mark.parametrize("key", KEYS)
def test_moonraker_subscription_includes_mod_params(key):
    assert "mod_params" in adapter_for(key).moonraker_objects()
