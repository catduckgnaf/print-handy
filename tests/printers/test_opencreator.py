"""Offline tests for the Flashforge Creator 5 / 5 Pro on OpenCreator firmware (#1490).

No printer, network or slicer. The OpenCreator contracts pinned here are
documented and config verified, not hardware verified: the documented
minimal start form, the identity tool contract, readiness failing closed
without any ZMod object, the active tool reported as unknown, plain
.gcode delivery, and the firmware-mismatch warnings in both directions.
"""

import json

import pytest

from print_handy import printers, settings
from print_handy.printers.base import MultiColorUnsupported
from print_handy.printers.flashforge_opencreator import facts, firmware_check

from .test_adapters import adapter_for

KEYS = ("flashforge_opencreator.creator5", "flashforge_opencreator.creator5_pro")
ZMOD_KEYS = ("flashforge.creator5", "flashforge.creator5_pro")

DOCUMENTED_START = (
    "SET_PRINT_STATS_INFO TOTAL_LAYER=[total_layer_count]\n"
    "M140 S[bed_temperature_initial_layer_single]\n"
    "M191 S[overall_chamber_temperature]\n"
    "M106\n"
    "M106 P2 S0 ; center fresh air input fan off\n"
    "M106 S0 ; model fan off\n"
    "M106 P3 S0 ; filter unit fan off\n"
    "G90\n"
    "M83"
)


def _ready_status():
    status = {"print_stats": {"state": "standby"}, "webhooks": {"state": "ready"},
              "heater_bed": {"temperature": 24.0}}
    for tool in range(4):
        name = "extruder" if tool == 0 else f"extruder{tool}"
        status[name] = {"temperature": 24.0}
    return status


@pytest.mark.parametrize("key", KEYS)
def test_model_is_a_four_tool_changer_on_its_own_vendor(key):
    model = printers.MODELS[key]
    assert model.vendor is printers.VENDORS["flashforge_opencreator"]
    assert model.vendor.connection == "moonraker"
    assert model.vendor.start_code == "klipper_macro"
    assert model.vendor.file_formats == (".gcode",)
    assert model.label.endswith("(OpenCreator)")
    defaults = model.defaults()["printer"]
    assert defaults["bed_mm"] == [256.0, 256.0]
    assert defaults["max_print_height_mm"] == 256.0
    assert defaults["filament_slots"] == 1
    assert defaults["tool_count"] == 4
    assert defaults["firmware"] == "OpenCreator Kalico"
    assert model.supported_nozzle_sizes == (0.25, 0.4, 0.6, 0.8)
    for fact in facts.CREATOR5.values():
        assert fact.verified is False
        assert fact.source == "OPENCREATOR"
    # No temperature ceiling fact is recorded for this family: the docs
    # and the shipped config disagree, so the ceiling stays unknown.
    assert "max_chamber_c" not in facts.CREATOR5
    assert "max_hotend_c" not in facts.CREATOR5


@pytest.mark.parametrize("key", KEYS)
def test_tool_mapping_keeps_each_tool_on_its_own_head(key):
    adapter = adapter_for(key)
    assert adapter.multi_tool is True
    assert (adapter.slot_count, adapter.multi_slot) == (4, False)
    assert adapter.normalize_tool_mapping({"0": 1, "1": 2, "2": 3, "3": 4}, {}) == {
        "0": 1, "1": 2, "2": 3, "3": 4}
    with pytest.raises(MultiColorUnsupported, match="toolhead 1"):
        adapter.normalize_tool_mapping({"0": 2}, {})
    with pytest.raises(MultiColorUnsupported, match="T4"):
        adapter.normalize_tool_mapping({"4": 5}, {})


@pytest.mark.parametrize("key", KEYS)
def test_moonraker_objects_are_the_standard_multi_tool_set(key):
    adapter = adapter_for(key)
    assert adapter.moonraker_objects() == [
        "webhooks", "print_stats", "virtual_sdcard",
        "extruder", "extruder1", "extruder2", "extruder3",
        "heater_bed", "exclude_object"]


@pytest.mark.parametrize("key", KEYS)
def test_start_contract_is_the_documented_form(key):
    adapter = adapter_for(key)
    machine = json.loads((adapter.templates_dir() / "machine.json").read_text())
    assert machine["machine_start_gcode"] == DOCUMENTED_START
    # The adapter adds nothing to the template start: no macro line, no
    # mesh argument, no nozzle heating line, so the firmware dispatcher
    # synthesizes its print start from the file as documented.
    assert adapter.machine_start_gcode(machine["machine_start_gcode"], "auto") == DOCUMENTED_START
    for forbidden in ("START_PRINT", "PRINT_START", "MESH=", "C5_PRINT_START"):
        assert forbidden not in machine["machine_start_gcode"], forbidden
        assert forbidden not in "\n".join(adapter.start_plan(60, 215)), forbidden
    plan = adapter.start_plan(60, 215)
    assert plan[0] == "SET_PRINT_STATS_INFO TOTAL_LAYER=[total_layer_count]"
    assert plan[1] == "M140 S60"
    assert plan[-2:] == ["G90", "M83"]
    # Print end is firmware-owned: no end macro line anywhere.
    assert machine["machine_end_gcode"] == ""
    assert adapter.machine_end_gcode(machine["machine_end_gcode"]) == ""
    assert adapter.machine_end_gcode("END_PRINT") == ""


@pytest.mark.parametrize("key", KEYS)
def test_templates_carry_the_opencreator_deltas(key):
    adapter = adapter_for(key)
    folder = adapter.templates_dir()
    machine = json.loads((folder / "machine.json").read_text())
    process = json.loads((folder / "process.json").read_text())
    assert machine["printable_area"] == ["0x0", "256x0", "256x256", "0x256"]
    assert machine["printable_height"] == "256"
    assert machine["nozzle_diameter"] == ["0.4"] * 4
    assert machine["host_type"] == "moonraker"
    assert machine["printer_agent"] == "moonraker"
    assert machine["use_3mf"] == "0"
    assert machine["retraction_speed"] == ["45"] * 4
    assert machine["deretraction_speed"] == ["45"] * 4
    assert machine["z_hop"] == ["0.3"] * 4
    assert process["gcode_label_objects"] == "1"
    assert process["exclude_object"] == "1"
    assert process["enable_arc_fitting"] == "0"
    # The one end line the OpenCreator docs say to remove is absent.
    for name in ("machine", "process", "filament"):
        assert "G1 X150 Y150 E-1.2 F12000" not in (folder / f"{name}.json").read_text()


def test_pro_template_records_chamber_and_filtration_base_does_not():
    pro = json.loads((adapter_for(KEYS[1]).templates_dir() / "machine.json").read_text())
    base = json.loads((adapter_for(KEYS[0]).templates_dir() / "machine.json").read_text())
    assert (pro["support_chamber_temp_control"], pro["support_air_filtration"]) == ("1", "1")
    assert (base["support_chamber_temp_control"], base["support_air_filtration"]) == ("0", "0")


@pytest.mark.parametrize("key", KEYS)
def test_readiness_fails_closed_and_needs_no_zmod_object(key):
    adapter = adapter_for(key)
    assert adapter.readiness_problems({}, _ready_status()) == []
    assert adapter.readiness_problems({}, {})
    missing_tool = _ready_status()
    del missing_tool["extruder2"]
    assert any("extruder2" in p for p in adapter.readiness_problems({}, missing_tool))
    busy = _ready_status()
    busy["print_stats"] = {"state": "printing"}
    assert any("busy" in p for p in adapter.readiness_problems({}, busy))


@pytest.mark.parametrize("key", KEYS)
def test_status_reports_toolheads_and_unknown_active_tool(key):
    adapter = adapter_for(key)
    out = adapter.normalize_status({"print_stats": {"state": "printing"},
                                    "extruder": {"temperature": 210.0},
                                    "extruder2": {"temperature": 215.0}})
    assert [h["temperature"] for h in out["toolheads"]] == [210.0, None, 215.0, None]
    # No verified object provides the active tool on this firmware yet.
    assert out["active_tool"] is None
    assert "slots" not in out


@pytest.mark.parametrize("key", KEYS)
def test_delivery_is_plain_gcode_even_with_a_new_zmod_version(key, tmp_path):
    """The ZMod .gcode.3mf floor must not leak onto the OpenCreator models:
    the upload artifact stays the plain .gcode even when the state names a
    ZMod version far above the 3MF floor."""
    from print_handy.launch.print_now import _zmod_upload_artifact

    adapter = adapter_for(key)
    assert "zmod_delivery_format" not in adapter.config["printer"]
    gcode = tmp_path / "plate_1.gcode"
    gcode.write_text("G28\n")
    state = {"nozzle": {"diameter_mm": 0.4}}
    upload, sha, remote, note = _zmod_upload_artifact(
        adapter, state, gcode, "deadbeef", "plate_1.gcode")
    assert (upload, sha, remote, note) == (gcode, "deadbeef", "plate_1.gcode", None)


def test_zmod_delivery_format_setting_is_refused_for_opencreator_models():
    with pytest.raises(settings.SettingsError, match="zmod_delivery_format"):
        settings.load(local=False, environ={
            "PRINT_HANDY__PRINTER__MODEL": KEYS[0],
            "PRINT_HANDY__PRINTER__ZMOD_DELIVERY_FORMAT": "auto"})


@pytest.mark.parametrize("key", KEYS)
def test_opencreator_model_pointed_at_zmod_warns_and_refuses(key):
    adapter = adapter_for(key)
    status = _ready_status()
    status["zmod_color"] = {"active_tool_id": 1}
    problems = adapter.readiness_problems({}, status)
    assert any("appears to be running ZMod, not OpenCreator" in p for p in problems), problems


@pytest.mark.parametrize("key", ZMOD_KEYS)
@pytest.mark.parametrize("evidence", ["creator5_toolchanger", "AFC", "update_manager opencreator"])
def test_zmod_model_pointed_at_opencreator_warns(key, evidence):
    adapter = adapter_for(key)
    status = _ready_status()
    status[evidence] = {}
    problems = adapter.readiness_problems({}, status)
    assert any("appears to be running OpenCreator, not ZMod" in p for p in problems), problems


@pytest.mark.parametrize("key", ZMOD_KEYS)
def test_zmod_model_without_evidence_keeps_the_plain_zmod_color_wording(key):
    adapter = adapter_for(key)
    problems = adapter.readiness_problems({}, _ready_status())
    assert any("zmod_color is not reporting" in p for p in problems), problems
    assert not any("OpenCreator" in p for p in problems), problems


def test_firmware_check_reads_names_only_and_absence_is_not_evidence():
    assert firmware_check.opencreator_evidence({}) == []
    assert firmware_check.opencreator_evidence({"extruder": {}}) == []
    assert firmware_check.looks_like_zmod({"zmod_color": None}) is False
    assert firmware_check.zmod_mismatch_problem({}) is None
    assert firmware_check.opencreator_mismatch_problem({"zmod_color": {}}) is None


def test_spool_remain_reads_a_present_number_and_ignores_absence():
    adapter = adapter_for(KEYS[0])
    status = {"extruder": {"filament_remaining_g": 120.5}, "extruder1": {"spool_remain_g": 0}}
    assert adapter.spool_remain_g(status, 0) == 120.5
    assert adapter.spool_remain_g(status, 1) == 0.0
    assert adapter.spool_remain_g(status, 2) is None
    assert adapter.spool_remain_g({}, 0) is None


def test_model_is_in_the_web_ui_printer_picker():
    from print_handy.webui.catalog import choices_for, field_by_id
    choices = choices_for(field_by_id("printer.model"))
    for key in KEYS:
        assert key in choices
