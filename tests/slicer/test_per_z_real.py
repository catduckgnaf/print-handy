"""Issue #721: controlled per-Z test — does Orca already handle height?

Slices the four controlled fixtures (tests/slicer/per_z_analysis.py) through Print
Handy's real Orca path for one enclosed Klipper/ZMod printer (flashforge.ad5x) and one
open-frame Klipper printer (sovol.sv06), then answers from the sliced G-code:

1. Layer-time cooling / slowdown on short top layers (small_top fixture).
2. Bridge fan engagement high in the print (tall_bridge_top fixture).
3. Whether outer-wall speed / acceleration is constant with Z (tall_thin fixture).

Skipped unless PRINT_HANDY_REAL_SLICER=1; CI runs these in the Slicer test jobs.
Findings are printed per fixture/printer so the CI log is the evidence record.
No print behavior is changed by this test — it pins what Orca does today.
"""
import os
import tempfile
from pathlib import Path

import pytest

from print_handy import settings
from tests.slicer.per_z_analysis import (FIXTURES, analyze_gcode, band, fan_max,
                                         footer_config, outer_wall_feed_range, write_fixture)

REAL = os.environ.get("PRINT_HANDY_REAL_SLICER") == "1"
WORK = settings.ROOT / ".slicer-work"
PRINTERS = ["flashforge.ad5x", "sovol.sv06"]

STATE = {
    "moonraker_url": "http://127.0.0.1:7125",
    "spoolman_url": "http://127.0.0.1:7912",
    "nozzle": {"diameter_mm": 0.4, "type": "hardened_steel"},
    "spool": {"spoolman_id": 1, "material": "PLA", "density": 1.24},
    "temps_c": {"nozzle": 220, "bed": 60},
    "calibration_baseline": {"native_mesh": "auto"},
    "filament_slots": {"1": {"slot": 1, "spoolman_id": 1, "material": "PLA",
                             "physical_color_verified": True, "hex": "FFFFFF"}},
}


def _slice(printer_key, fixture_name, tmp):
    from print_handy import printers
    from print_handy.core import orca_adapter

    stl = write_fixture(tmp / f"{fixture_name}.stl", FIXTURES[fixture_name])
    out = tmp / f"out_{printer_key}_{fixture_name}"
    out.mkdir()
    adapter = printers.get(printer_key, settings.current())
    gcode, _prov, _cfgs = orca_adapter.slice_model(stl, out, dict(STATE), adapter=adapter)
    text = gcode.read_text(errors="replace")
    return analyze_gcode(text), footer_config(text)


@pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CLI")
@pytest.mark.parametrize("printer_key", PRINTERS)
def test_layer_time_cooling_engages_on_short_top_layers(printer_key):
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        layers, _cfg = _slice(printer_key, "small_top", Path(tmp))
    base = band(layers, 2, 18)
    top = band(layers, 80, 100)
    base_fan, top_fan = fan_max(base), fan_max(top)
    base_feed = outer_wall_feed_range(base)
    top_feed = outer_wall_feed_range(top)
    print(f"\n#721 {printer_key} small_top: fan base={base_fan} top={top_fan}; "
          f"outer-wall feed base={base_feed} top={top_feed}")
    assert top, "no layers found in the small top section"
    fan_engaged = (top_fan or 0) > (base_fan or 0)
    slowdown_engaged = (top_feed[0] is not None and base_feed[0] is not None
                        and top_feed[0] < base_feed[0])
    assert fan_engaged or slowdown_engaged, (
        "neither layer-time fan nor slowdown engaged on short top layers — "
        "a genuine slicer miss to report on #721")


@pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CLI")
@pytest.mark.parametrize("printer_key", PRINTERS)
def test_bridge_fan_engages_high_in_the_print(printer_key):
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        layers, _cfg = _slice(printer_key, "tall_bridge_top", Path(tmp))
    high = band(layers, 115, 128)
    bridge_layers = [layer for layer in high
                     if any("bridge" in t.lower() for t in layer["types"])]
    bridge_fans = [layer["bridge_fan_max"] for layer in bridge_layers
                   if layer["bridge_fan_max"] is not None]
    print(f"\n#721 {printer_key} tall_bridge_top: bridge layers near top: {len(bridge_layers)}; "
          f"fan during bridges: {bridge_fans}")
    assert bridge_layers, "Orca produced no bridge feature near the top of the bridge fixture"
    assert bridge_fans and max(bridge_fans) >= 128, (
        "the part fan was not strongly on while bridges printed high in the print")


@pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CLI")
@pytest.mark.parametrize("printer_key", PRINTERS)
def test_outer_wall_speed_and_acceleration_recorded_across_height(printer_key):
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        layers, config = _slice(printer_key, "tall_thin", Path(tmp))
    low = outer_wall_feed_range(band(layers, 2, 46))
    high = outer_wall_feed_range(band(layers, 94, 140))
    accels = sorted({a for layer in layers for a in layer["accelerations"]})
    print(f"\n#721 {printer_key} tall_thin: outer-wall feed low={low} high={high}; "
          f"M204 accelerations={accels}; footer outer_wall_speed="
          f"{config.get('outer_wall_speed')} outer_wall_acceleration="
          f"{config.get('outer_wall_acceleration')}")
    assert low[0] is not None and high[0] is not None, "outer-wall moves missing in a height band"
