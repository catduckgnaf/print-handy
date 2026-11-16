"""#1490: the code-only items left after the tranche PRs.

Each test drives a production entry point (the readiness check through the
adapter the launch gate calls, the launch's charge plan and the real
record_start, the resume guard, the cancel report builder, doctor), with
the printer replaced by recorded shapes. Nothing contacts a printer, and
none of this is hardware verified: the shapes are the source-verified ones
on #1490 and the live captures in tests/fixtures/opencreator.
"""
import json
import time
from pathlib import Path
from unittest import mock

import pytest

from print_handy import doctor
from print_handy.core import model_job
from print_handy.launch import print_now
from print_handy.runtime import klipper_report, multislot_usage, print_control, print_records

from tests.printers.test_adapters import adapter_for
from tests.printers.test_opencreator import _ready_status

BASE = "flashforge_opencreator.creator5"
PRO = "flashforge_opencreator.creator5_pro"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "opencreator"


def _swapped(status):
    """T0 prints on toolhead 3 and T2 on toolhead 1 (a SET_MAP swap at the printer)."""
    status["AFC_lane extruder0"]["map"] = ["T2"]
    status["AFC_lane extruder2"]["map"] = ["T0"]
    return status


# ---------------------------------------------------------------- readiness (tranche 3)

@pytest.mark.parametrize("key", (BASE, PRO))
def test_a_held_power_loss_recovery_refuses_the_start(key):
    adapter = adapter_for(key)
    status = _ready_status()
    assert adapter.readiness_problems({}, status) == []
    status["power_loss_recovery"] = {"recovery_available": True, "filename": "benchy.gcode",
                                     "holding_bed": True}
    problems = adapter.readiness_problems({}, status)
    assert any("interrupted print" in p and "benchy.gcode" in p for p in problems), problems


@pytest.mark.parametrize("key", (BASE, PRO))
def test_an_unreadable_recovery_state_fails_closed(key):
    adapter = adapter_for(key)
    status = _ready_status()
    del status["power_loss_recovery"]
    assert any("power_loss_recovery is not reporting" in p
               for p in adapter.readiness_problems({}, status))
    status["power_loss_recovery"] = {"recovery_available": None}
    assert any("no readable recovery state" in p for p in adapter.readiness_problems({}, status))


def test_the_pro_needs_its_chamber_heater_and_the_base_model_does_not():
    status = _ready_status()
    del status["heater_generic chamber_heater"]
    assert any("chamber_heater is not reporting" in p
               for p in adapter_for(PRO).readiness_problems({}, status))
    assert adapter_for(BASE).readiness_problems({}, status) == []
    assert "heater_generic chamber_heater" not in adapter_for(BASE).moonraker_objects()


@pytest.mark.parametrize("key", (BASE, PRO))
def test_a_used_tool_without_filament_refuses_and_an_unused_one_does_not(key):
    # The firmware's synthesized start checks the first tool only, so a
    # used second tool with no filament would stop the print on runout.
    adapter = adapter_for(key)
    status = _ready_status()
    status["AFC_extruder extruder1"]["tool_start_status"] = False
    two_tools = {"tool_mapping": {"0": 1, "1": 2}}
    problems = adapter.readiness_problems({}, status, two_tools)
    assert any(p.startswith("T1 has no filament at the toolhead") for p in problems), problems
    assert adapter.readiness_problems({}, status, {"tool_mapping": {"0": 1}}) == []


def test_filament_presence_follows_the_afc_map():
    adapter = adapter_for(PRO)
    status = _swapped(_ready_status())
    # T0 prints on toolhead 3 now: an empty toolhead 1 does not stop a T0 job.
    status["AFC_extruder extruder0"]["tool_start_status"] = False
    assert adapter.readiness_problems({}, status, {"tool_mapping": {"0": 1}}) == []
    status["AFC_extruder extruder2"]["tool_start_status"] = False
    problems = adapter.readiness_problems({}, status, {"tool_mapping": {"0": 1}})
    assert any("T0 (toolhead 3) has no filament" in p for p in problems), problems


def test_unknown_filament_presence_is_a_refusal_not_a_pass():
    adapter = adapter_for(PRO)
    status = _ready_status()
    del status["AFC_extruder extruder0"]["tool_start_status"]
    problems = adapter.readiness_problems({}, status, {"tool_mapping": {"0": 1}})
    assert any("T0 filament presence is not reporting" in p for p in problems), problems


def test_the_live_toolchanger_capture_reads_no_recovery_object_as_unready():
    # Cartrigger's capture (#1490) queried AFC and the toolchanger only.
    # A sample without power_loss_recovery must never pass as ready.
    capture = json.loads((FIXTURES / "c5toolchanger.json").read_text())["result"]["status"]
    problems = adapter_for(PRO).readiness_problems({}, capture)
    assert any("power_loss_recovery is not reporting" in p for p in problems)


# ---------------------------------------------------------------- resume guard (tranche 3)

def _oc_sensor_call(active, readings):
    objects = ["toolhead", "extruder", "extruder1", "extruder2", "extruder3",
               "filament_switch_sensor extruder0_tool_start", "filament_switch_sensor extruder1_tool_start",
               "filament_switch_sensor extruder2_tool_start", "filament_switch_sensor extruder3_tool_start"]

    def call(path, method="GET", data=None, timeout=10, base=None):
        if path == "/printer/objects/list":
            return {"result": {"objects": objects}}
        if path.startswith("/printer/objects/query?"):
            status = {"toolhead": {"extruder": active}}
            for tool, detected in readings.items():
                status[f"filament_switch_sensor extruder{tool}_tool_start"] = {
                    "filament_detected": detected, "enabled": True}
            return {"result": {"status": status}}
        raise AssertionError(path)
    return call


@pytest.mark.real_head_sensor
def test_resume_guard_reads_the_active_toolheads_own_sensor():
    adapter = adapter_for(PRO)
    with mock.patch.object(print_control, "_adapter_for", return_value=adapter), \
            mock.patch.object(print_control, "_call", _oc_sensor_call("extruder1", {0: True, 1: False})):
        assert print_control.head_filament_loaded() is False
    with mock.patch.object(print_control, "_adapter_for", return_value=adapter), \
            mock.patch.object(print_control, "_call", _oc_sensor_call("extruder", {0: True, 1: False})):
        # T0 is printing and its sensor reads loaded: the idle T1 sensor does not block.
        assert print_control.head_filament_loaded() is True


# ---------------------------------------------------------------- start-phase refusals (tranche 3)

@pytest.mark.parametrize("line, meaning", [
    ("Cancelling print: no filament detected in selected tool T1", "had no filament"),
    ("Cancelling print: build plate not detected", "build plate was not detected"),
    ("Print cancelled during print start", "still preparing it"),
])
def test_a_start_phase_refusal_is_named_in_the_cancel_report(line, meaning):
    now = 1794800000.0
    status = {"webhooks": {"state": "ready"},
              "print_stats": {"filename": "f.gcode", "state": "cancelled", "total_duration": 30}}
    store = [{"message": "// " + line, "time": now - 20}]
    with mock.patch("print_handy.printers.current", return_value=adapter_for(PRO)):
        text = klipper_report.build("f.gcode", status, store, "", by_bot=False, now=now)
    assert "**Stop reason:**" in text and meaning in text, text
    assert "plain manual cancel" not in text


# ---------------------------------------------------------------- charging follows the map (decision 1, 2026-11-15)

TWO_TOOL = "\n".join(["; filament used [mm] = 1000.0, 500.0", "; filament used [g] = 3.0, 1.5",
                      "; filament_type = PETG;PLA", "; filament_colour = #FF0000;#00FF00",
                      "M140 S60", "T0", "G1 X10 Y10 E5", "T1", "G1 X20 Y20 E5", ""])


def _launch_plan(tmp_path, adapter, status, authority="print_handy"):
    jp = tmp_path / "JOB.json"
    jp.write_text(json.dumps({"title": "two tools"}))
    job = json.loads(jp.read_text())
    with mock.patch.object(print_now, "_launch_accounting_authority", return_value=authority), \
            mock.patch("print_handy.gcode.verify._mr", side_effect=lambda st, path: {
                "result": {"status": status}}):
        recorded = print_now._record_toolhead_charge_plan({}, adapter, job, jp, TWO_TOOL)
    return recorded, json.loads(jp.read_text())


def test_a_multi_tool_print_is_charged_to_the_spools_the_printer_map_uses(tmp_path, monkeypatch):
    adapter = adapter_for(PRO)
    recorded, job = _launch_plan(tmp_path, adapter, _swapped(_ready_status()))
    assert recorded is True
    # T0 prints on toolhead 3, T1 stays on toolhead 2.
    assert job["toolhead_charge_plan"] == {"0": 3, "1": 2}

    # The real record_start turns the plan into the per-position charge plan the listener uses.
    g = tmp_path / "x.gcode"
    g.write_text(TWO_TOOL)
    state = {"printer": "Creator 5 Pro", "moonraker_url": "http://printer.invalid",
             "temps_c": {"nozzle": 220, "bed": 60}, "nozzle": {"diameter_mm": 0.4, "type": "brass"},
             "spool": {}, "filament_slots": {"2": {"spoolman_id": 22, "material": "PLA"},
                                             "3": {"spoolman_id": 33, "material": "PETG"}}}
    context_path = tmp_path / "print_handy_context.json"
    monkeypatch.setattr(print_records, "PRINTS_DIR", tmp_path / "Prints")
    with mock.patch.object(model_job, "_job_for_gcode", return_value=job), \
            mock.patch.object(model_job.printer_state, "load", return_value=state), \
            mock.patch.object(model_job.printers, "current", return_value=adapter), \
            mock.patch.object(model_job, "CONTEXT", context_path):
        print_now.record_start(str(g), "x.gcode", started_at="2026-11-16T01:00:00Z", print_number=7)
    context = json.loads(context_path.read_text())["jobs"]["x.gcode"]
    plan = multislot_usage.slot_plan(context)
    assert [(row["tool"], row["slot"], row["spool_id"], row["planned_g"]) for row in plan] == [
        ("0", 3, 33, 3.0), ("1", 2, 22, 1.5)]


def test_an_unread_map_records_no_plan_and_never_guesses(tmp_path, capsys):
    adapter = adapter_for(PRO)
    status = _ready_status()
    del status["AFC"]
    recorded, job = _launch_plan(tmp_path, adapter, status)
    assert recorded is False and "toolhead_charge_plan" not in job
    assert "AFC tool map could not be read" in capsys.readouterr().out


def test_the_printer_as_authority_records_no_plan(tmp_path):
    recorded, job = _launch_plan(tmp_path, adapter_for(PRO), _ready_status(), authority="printer")
    assert recorded is True and "toolhead_charge_plan" not in job
    assert job["spool_accounting_authority"] == "printer"


def test_a_one_tool_print_and_other_printers_keep_the_active_spool_path(tmp_path):
    jp = tmp_path / "JOB.json"
    jp.write_text("{}")
    one_tool = "M140 S60\nT0\nG1 X1 E1\n"
    assert print_now._record_toolhead_charge_plan({}, adapter_for(PRO), {}, jp, one_tool) is False
    assert print_now._record_toolhead_charge_plan({}, adapter_for("flashforge.creator5"), {}, jp,
                                                  TWO_TOOL) is False


# ---------------------------------------------------------------- doctor names the firmware (tranche 1)

def _doctor(model, status):
    data = {"printer": {"model": model}, "services": {"moonraker_url": "http://creator.lan:7125"}}
    with mock.patch.object(doctor.moonraker, "moonraker_json",
                           return_value={"result": {"status": status}}):
        return doctor.check_firmware_identity(data)


def test_doctor_names_the_firmware_against_the_configured_model():
    opencreator = {"creator5_toolchanger": {"active_tool": None}, "AFC": {"lanes": []}, "zmod_color": {}}
    zmod = {"zmod_color": {"active_tool_id": 0}, "creator5_toolchanger": {}, "AFC": {}}
    assert _doctor(PRO, opencreator) == "OpenCreator, matching the configured model"
    assert _doctor("flashforge.creator5", zmod) == "ZMod, matching the configured model"
    with pytest.raises(RuntimeError, match="running OpenCreator, not ZMod"):
        _doctor("flashforge.creator5", opencreator)
    with pytest.raises(RuntimeError, match="running ZMod, not OpenCreator"):
        _doctor(BASE, zmod)
    assert _doctor("flashforge.ad5x", zmod) is None


def test_doctor_reports_an_unreachable_printer_as_unknown():
    data = {"printer": {"model": PRO}, "services": {"moonraker_url": "http://creator.lan:7125"}}
    with mock.patch.object(doctor.moonraker, "moonraker_json", side_effect=OSError("refused")):
        assert doctor.check_firmware_identity(data).startswith("unknown")


# ---------------------------------------------------------------- spec follow-up: ZMod live verification

@pytest.mark.parametrize("key", ("flashforge.ad5m", "flashforge.creator5"))
def test_zmod_screenless_adapters_name_their_start_and_end_macros(key, tmp_path):
    # Live verification reads adapter.start_macro for every adapter without a
    # native screen; on these ZMod models it raised AttributeError.
    from print_handy.gcode import verify
    adapter = adapter_for(key)
    assert (adapter.start_macro, adapter.end_macro) == ("START_PRINT", "END_PRINT")
    g = tmp_path / "x.gcode"
    g.write_text("\n".join(["; nozzle_diameter = 0.4", "; filament_type = PLA",
                            "; first_layer_height = 0.2", "START_PRINT EXTRUDER_TEMP=220 BED_TEMP=60",
                            "T0", "G1 X10 Y10 Z0.2 E1", "END_PRINT", ""]))
    state = {"nozzle": {"diameter_mm": 0.4, "type": "brass"}, "spool": {"material": "PLA"},
             "temps_c": {"nozzle": 220, "bed": 60},
             "axis_limits_mm": {"x": [0, 260], "y": [0, 260], "z": [0, 256]}}
    with mock.patch.object(verify.printer_state, "check", lambda *a, **k: (True, [], {})):
        try:
            verify.verify_gcode(g, adapter=adapter, state=state, recorded_temps={"nozzle": 220, "bed": 60})
        except RuntimeError as exc:
            assert "missing START_PRINT" not in str(exc)
