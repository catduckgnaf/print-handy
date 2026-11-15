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
    assert start.hotends == {1: 240.0, 3: 200.0}  # the selected tool's temp is HOTEND itself
    assert start.idles == {3: 150.0}
    assert start.hotend == 220.0
    assert start_contract.start_problems(text, 65, 220) == []


def test_chamber_fallback_uses_the_tool_slot_and_a_short_active_list_is_on():
    # No filament-used list at all: the chamber fallback reads the
    # selected tool's slot, and a slot past the end of the
    # activate_chamber_temp_control list counts as control on.
    text = _file(["T2"], used="0,0,0,0", temps="200,205,215,210",
                 chamber_temps="40,45,50,35", chamber_on="1")
    start = start_contract.synthesize_start(text)
    assert start.tool == 2
    assert start.chamber == 50.0


def test_config_line_cut_by_the_header_window_is_not_read():
    # A config line the 64 KB header window cuts inside its first value
    # is dropped for config reads, the way the dispatcher drops the
    # partial line: the value is not read from the truncated line, and
    # with no other source the file has no discoverable hotend temp.
    filler = "G1 X1 Y1\n" * 7277 + "G1\n"  # 65496 chars
    config = "; nozzle_temperature_initial_layer = 230,0,0,0\n"
    text = filler + config + "G1 X2 Y2\n" * 8000
    assert text[:65536].endswith("= 230")  # the window cuts the list there
    start = start_contract.synthesize_start(text)
    assert start.hotend is None


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
    # Every tool docked (no active tool, no sensor error) is the normal
    # idle state: print start picks up the tool it needs.
    docked = _ready_status()
    docked["creator5_toolchanger"]["active_tool"] = None
    assert adapter.readiness_problems({}, docked) == []
    busy = _ready_status()
    busy["creator5_toolchanger"]["busy"] = True
    assert any("toolchanger is busy" in p for p in adapter.readiness_problems({}, busy))
    unknown = _ready_status()
    unknown["creator5_toolchanger"]["active_tool"] = 7
    assert any("unknown active tool" in p for p in adapter.readiness_problems({}, unknown))
    # build_plate_detected is the last check's result, not a live
    # reading: a stale False must not refuse (print start re-checks).
    stale_plate = _ready_status()
    stale_plate["creator5_toolchanger"]["build_plate_detected"] = False
    assert adapter.readiness_problems({}, stale_plate) == []


def test_readiness_afc_state_fails_closed():
    adapter = adapter_for(PRO)
    no_afc = _ready_status()
    del no_afc["AFC"]
    assert any(p.startswith("AFC is not reporting") for p in adapter.readiness_problems({}, no_afc))
    errored = _ready_status()
    errored["AFC"]["error_state"] = True
    assert any("AFC reports an error state" in p for p in adapter.readiness_problems({}, errored))
    mount = _ready_status()
    mount["AFC_extruder extruder1"]["mount_sensor_error"] = "dock and grab both closed"
    problems = adapter.readiness_problems({}, mount)
    assert any("toolhead 2 reports a mount sensor error" in p
               and "dock and grab both closed" in p for p in problems), problems
    no_extruder = _ready_status()
    del no_extruder["AFC_extruder extruder2"]
    assert any("AFC_extruder extruder2 is not reporting" in p
               for p in adapter.readiness_problems({}, no_extruder))


def test_readiness_afc_identity_map_passes_and_a_swap_refuses():
    adapter = adapter_for(PRO)
    swapped = _ready_status()
    swapped["AFC_lane extruder0"] = {"map": ["T1"], "current_map": ""}
    swapped["AFC_lane extruder1"] = {"map": ["T0"], "current_map": ""}
    problems = adapter.readiness_problems({}, swapped)
    assert any("not the identity map" in p and "extruder0 is mapped to T1" in p
               and "extruder1 is mapped to T0" in p for p in problems), problems
    missing = _ready_status()
    del missing["AFC_lane extruder2"]
    assert any("AFC_lane extruder2 is not reporting" in p
               for p in adapter.readiness_problems({}, missing))
    # The fork's lane status is empty until its connect step finishes.
    not_connected = _ready_status()
    not_connected["AFC_lane extruder2"] = {}
    assert any("AFC_lane extruder2 does not report its lane map" in p
               for p in adapter.readiness_problems({}, not_connected))
    # The map is never read from the AFC_extruder object: the fork's
    # status there carries none, so a map only there must not pass.
    wrong_home = _ready_status()
    del wrong_home["AFC_lane extruder3"]
    wrong_home["AFC_extruder extruder3"]["map"] = "T3"
    assert any("AFC_lane extruder3 is not reporting" in p
               for p in adapter.readiness_problems({}, wrong_home))
    string_form = _ready_status()
    string_form["AFC_lane extruder3"] = {"map": "T3"}
    assert adapter.readiness_problems({}, string_form) == []
    double = _ready_status()
    double["AFC_lane extruder3"] = {"map": ["T3", "T2"]}
    assert any("extruder3 is mapped to T3, T2" in p
               for p in adapter.readiness_problems({}, double))


def test_pro_chamber_target_above_the_ceiling_is_refused():
    pro, base = adapter_for(PRO), adapter_for(KEYS[0])
    at_ceiling = _file(["M140 S60", "M191 S65"])
    assert pro.start_synthesis_problems(at_ceiling, 60, 220) == []
    hot = _file(["M140 S60", "M191 S75"])
    problems = pro.start_synthesis_problems(hot, 60, 220)
    assert any("chamber at 75 C" in p and "65 C chamber ceiling" in p for p in problems), problems
    from_config = _file(["M140 S60"], chamber_temps="70,0,0,0", chamber_on="1,0,0,0")
    assert any("chamber at 70 C" in p for p in pro.start_synthesis_problems(from_config, 60, 220))
    # The base model has no chamber heater: the firmware answers a
    # chamber target with a notice, so there is nothing to hold.
    assert base.start_synthesis_problems(hot, 60, 220) == []


def test_explicit_line_owns_its_hotend_and_first_tool():
    no_hotend = _file(["C5_PRINT_START TOOL=0 BED=60", "M140 S60"])
    problems = start_contract.start_problems(no_hotend, 60, 220)
    assert len(problems) == 1 and "names no HOTEND" in problems[0], problems
    wrong_tool = _file(["C5_PRINT_START TOOL=1 HOTEND=220 BED=60", "T2", "M140 S60"])
    problems = start_contract.start_problems(wrong_tool, 60, 220)
    assert any("selects T1" in p and "is T2" in p for p in problems), problems
    matching = _file(["C5_PRINT_START TOOL=2 HOTEND=220 BED=60", "T2", "M140 S60"])
    assert start_contract.start_problems(matching, 60, 220) == []


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



def _live_verify_errors(monkeypatch, tmp_path, text):
    """The live verify_gcode entry point's problems for an OpenCreator Pro file.

    These hand-built files carry no slicer width metadata, so the live
    check always reports that; the tests below read which start and end
    problems it reports beside it.
    """
    from print_handy.gcode import verify

    adapter = adapter_for(PRO)
    path = tmp_path / "job.gcode"
    path.write_text(text)
    state = {"nozzle": {"diameter_mm": 0.4, "type": "hardened_steel"},
             "spool": {"material": "PLA"},
             "axis_limits_mm": {"x": [0, 256], "y": [0, 256], "z": [0, 256]}}
    monkeypatch.setattr(verify.printer_state, "check", lambda *a, **k: (True, [], {}))
    with pytest.raises(RuntimeError) as caught:
        verify.verify_gcode(path, adapter=adapter, state=state,
                            recorded_temps={"nozzle": 220, "bed": 60})
    return str(caught.value)


def test_live_verification_models_the_synthesis_and_demands_no_end_macro(monkeypatch, tmp_path):
    # The production (live) entry point, not only the offline sibling.
    # A synthesized-start file has no start macro line and no end macro
    # line, because the firmware owns both: neither is reported missing.
    assert "C5_PRINT_START" not in SINGLE_TOOL_T0 and "END_PRINT" not in SINGLE_TOOL_T0
    message = _live_verify_errors(monkeypatch, tmp_path, SINGLE_TOOL_T0)
    assert "missing first layer WIDTH metadata" in message
    for absent in ("matching bed", "safe end sequence", "the firmware would synthesize",
                   "unverified mapping"):
        assert absent not in message, message


def test_live_verification_refuses_a_wrong_bed_and_a_hot_chamber(monkeypatch, tmp_path):
    message = _live_verify_errors(monkeypatch, tmp_path, _file(["M140 S70", "G90"]))
    assert "runs bed 70 C, not the job's 60 C" in message
    message = _live_verify_errors(monkeypatch, tmp_path, _file(["M140 S60", "M191 S75"]))
    assert "65 C chamber ceiling" in message


def test_short_or_missing_config_lists_never_raise():
    # Orca can write fewer entries than there are tools, or none: every
    # per-slot read is bounds-checked, so synthesis degrades to "not
    # discoverable" (the firmware's own failure mode), never an exception.
    short = ("T2
M140 S60
G1 X1 E1
; filament used [mm] = 0,0,5
"
             "; nozzle_temperature_initial_layer = 220
; chamber_temperature = 50
"
             "; activate_chamber_temp_control = 1
; idle_temperature = 0
")
    start = start_contract.synthesize_start(short)
    assert (start.tool, start.hotend, start.chamber, start.used_tools) == (2, None, None, (2,))
    [problem] = start_contract.start_problems(short, 60, 220)
    assert "No hotend temperature found" in problem
    bare = start_contract.synthesize_start("M140 S60
")
    assert (bare.tool, bare.hotend, bare.used_tools) == (0, None, ())
    assert start_contract.synthesize_start("").hotend is None


def test_status_reports_no_active_tool_while_the_sensors_disagree():
    adapter = adapter_for(PRO)
    raw = _ready_status()
    raw["creator5_toolchanger"].update(active_tool=1, sensor_error="dock 1 and grab 2 disagree")
    assert adapter.normalize_status(raw)["active_tool"] is None
    raw["creator5_toolchanger"]["sensor_error"] = None
    assert adapter.normalize_status(raw)["active_tool"] == 1
