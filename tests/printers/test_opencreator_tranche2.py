"""Tranche 2 tests for the OpenCreator Creator models (#1490).

Offline, no printer, network or slicer. The contracts pinned here are
source-verified against the OpenCreator fork of Kalico (default branch
``something`` at 7ce49713) and the shipped opencreator-fs config tree,
not hardware verified: the dispatcher's synthesized print start per job
shape, readiness and active tool on the creator5_toolchanger object,
the AFC identity lane map, and the manufacturer temperature ceilings.
"""

import pytest

from print_handy.core import heater_ceiling
from print_handy.gcode.verify import verify_gcode_offline
from print_handy.printers.flashforge_opencreator import start_contract

from .test_adapters import adapter_for
from .test_opencreator import _ready_status

KEYS = ("flashforge_opencreator.creator5", "flashforge_opencreator.creator5_pro")
PRO = KEYS[1]

CONFIG = (
    "; filament used [mm] = {used}\n"
    "; filament_type = PLA,PLA,PLA,PLA\n"
    "; nozzle_temperature_initial_layer = {temps}\n"
    "; idle_temperature = {idles}\n"
    "; chamber_temperature = {chamber_temps}\n"
    "; activate_chamber_temp_control = {chamber_on}\n"
    "; nozzle_diameter = 0.4\n"
    "; first_layer_height = 0.2\n"
)


def _file(header_lines, used="100.0,0,0,0", temps="220,220,220,220",
          idles="0,0,0,0", chamber_temps="0,0,0,0", chamber_on="0,0,0,0",
          body="G1 Z0.2 F600\nG1 X10 Y10 E1.0\nG1 X20 Y20 E2.0\n"):
    return ("\n".join(header_lines) + "\n" + body
            + CONFIG.format(used=used, temps=temps, idles=idles,
                            chamber_temps=chamber_temps, chamber_on=chamber_on))


SINGLE_TOOL_T0 = _file([
    "SET_PRINT_STATS_INFO TOTAL_LAYER=42",
    "M140 S60",
    "M106",
    "G90",
    "M83",
])


def test_single_tool_t0_falls_back_to_the_config_block():
    # No M104/M109 and no T line in the documented start form: TOOL is
    # the first used slot and HOTEND is that slot's config temperature.
    start = start_contract.synthesize_start(SINGLE_TOOL_T0)
    assert start.explicit is False
    assert (start.tool, start.hotend, start.bed) == (0, 220.0, 60.0)
    assert start.used_tools == (0,)
    assert start_contract.start_problems(SINGLE_TOOL_T0, 60, 220) == []
    problems = start_contract.start_problems(SINGLE_TOOL_T0, 60, 215)
    assert any("hotend 220" in p for p in problems), problems


def test_multi_tool_synthesis_reads_used_tools_per_tool_temps_and_idles():
    text = _file(
        ["SET_PRINT_STATS_INFO TOTAL_LAYER=9", "M140 S65", "G90", "M83", "T0",
         "G1 X10 Y10 E1.0", "T1", "G1 X20 Y20 E1.0", "T3"],
        used="10.0,20.0,0,5.0", temps="220,240,0,200", idles="0,0,0,150")
    start = start_contract.synthesize_start(text)
    assert start.tool == 0
    assert start.used_tools == (0, 1, 3)
    assert start.hotends == {0: 220.0, 1: 240.0, 3: 200.0}
    assert start.idles == {3: 150.0}
    assert start.hotend == 220.0
    assert start_contract.start_problems(text, 65, 220) == []


def test_m109_wins_over_m104_and_m104_is_the_preheat_fallback():
    with_both = _file(["M104 S210", "M140 S60", "M109 S230"])
    assert start_contract.synthesize_start(with_both).hotend == 230.0
    preheat_only = _file(["M104 S210", "M140 S60"])
    assert start_contract.synthesize_start(preheat_only).hotend == 210.0


def test_chamber_job_reads_m191_and_the_config_fallback():
    chamber_line = _file(["M140 S60", "M191 S50"])
    assert start_contract.synthesize_start(chamber_line).chamber == 50.0
    from_config = _file(["M140 S60", "T1"], used="0,10.0,0,0",
                         chamber_temps="0,55,0,0", chamber_on="0,1,0,0")
    start = start_contract.synthesize_start(from_config)
    assert start.tool == 1
    assert start.chamber == 55.0


def test_explicit_c5_print_start_opts_out_of_synthesis():
    text = _file(["C5_PRINT_START TOOL=1 HOTEND=230 BED=65", "M140 S60"])
    start = start_contract.synthesize_start(text)
    assert start.explicit is True
    assert (start.tool, start.hotend, start.bed) == (1, 230.0, 65.0)
    assert start_contract.start_problems(text, 65, 230) == []
    problems = start_contract.start_problems(text, 65, 220)
    assert any("hotend 230" in p for p in problems), problems


def test_no_discoverable_temperature_is_a_verification_failure():
    text = _file(["M140 S60", "G90"], temps="0,0,0,0")
    start = start_contract.synthesize_start(text)
    assert start.hotend is None
    problems = start_contract.start_problems(text, 60, 220)
    assert len(problems) == 1
    assert "No hotend temperature found in G-code header" in problems[0]


def test_offline_verification_models_the_synthesis(tmp_path):
    adapter = adapter_for(PRO)
    path = tmp_path / "job.gcode"
    path.write_text(SINGLE_TOOL_T0)
    state = {"nozzle": {"diameter_mm": 0.4, "type": "hardened_steel"},
             "axis_limits_mm": {"x": [0, 256], "y": [0, 256], "z": [0, 256]}}
    result = verify_gcode_offline(path, state, adapter,
                                  recorded_temps={"nozzle": 220, "bed": 60})
    assert result["offline"] is True
    path.write_text(_file(["M140 S60", "G90"], temps="0,0,0,0"))
    with pytest.raises(RuntimeError, match="No hotend temperature"):
        verify_gcode_offline(path, state, adapter,
                             recorded_temps={"nozzle": 220, "bed": 60})


def test_readiness_toolchanger_contract():
    adapter = adapter_for(PRO)
    assert adapter.readiness_problems({}, _ready_status()) == []
    missing = _ready_status()
    del missing["creator5_toolchanger"]
    assert any("creator5_toolchanger is not reporting" in p
               for p in adapter.readiness_problems({}, missing))
    errored = _ready_status()
    errored["creator5_toolchanger"] = {
        "active_tool": None, "sensor_error": "dock 1 and grab 2 disagree",
        "busy": False, "build_plate_detected": None,
        "dock_positions": [], "tool_measurements": []}
    problems = adapter.readiness_problems({}, errored)
    assert any("tool sensor error" in p and "dock 1 and grab 2 disagree" in p
               for p in problems), problems
    unmounted = _ready_status()
    unmounted["creator5_toolchanger"]["active_tool"] = None
    problems = adapter.readiness_problems({}, unmounted)
    assert any("no tool is mounted" in p for p in problems), problems


def test_readiness_afc_identity_map_passes_and_a_swap_refuses():
    adapter = adapter_for(PRO)
    swapped = _ready_status()
    swapped["AFC_extruder extruder0"] = {"map": "T1", "current_map": "T1"}
    swapped["AFC_extruder extruder1"] = {"map": "T0", "current_map": "T0"}
    problems = adapter.readiness_problems({}, swapped)
    assert any("not the identity map" in p and "extruder0 is mapped to T1" in p
               and "extruder1 is mapped to T0" in p for p in problems), problems
    missing = _ready_status()
    del missing["AFC_extruder extruder2"]
    assert any("AFC_extruder extruder2 is not reporting" in p
               for p in adapter.readiness_problems({}, missing))
    list_form = _ready_status()
    list_form["AFC_extruder extruder3"] = {"map": ["T3"]}
    assert adapter.readiness_problems({}, list_form) == []


def test_manufacturer_ceilings_are_the_recorded_ceilings():
    pro = adapter_for(PRO)
    assert heater_ceiling.ceiling(pro, "chamber") == 65.0
    assert heater_ceiling.ceiling(pro, "bed") == 120.0
    assert heater_ceiling.ceiling(pro, "nozzle") == 320.0
    assert heater_ceiling.refusal(pro, "chamber", 80) is not None
    base = adapter_for(KEYS[0])
    assert heater_ceiling.ceiling(base, "bed") == 120.0
    assert heater_ceiling.ceiling(base, "nozzle") == 320.0
    # The base printer has no chamber heater and records no chamber
    # ceiling: the shared machinery keeps its unknown fallback.
    assert heater_ceiling.ceiling(base, "chamber") == heater_ceiling.UNKNOWN_HEATER_CEILING_C


def test_base_model_start_form_writes_no_chamber_command():
    base = adapter_for(KEYS[0])
    assert not any(line.startswith("M191") for line in base.start_plan(60, 220))
    assert "M191" not in base.machine_start_gcode("", "auto")
    pro = adapter_for(PRO)
    assert any(line.startswith("M191") for line in pro.start_plan(60, 220))
