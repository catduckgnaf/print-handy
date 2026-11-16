"""Offline tests for the Flashforge Adventurer 5M / 5M Pro on Forge-X firmware (#1506).

No printer, network or slicer. The Forge-X contracts pinned here are
documented and config verified, not hardware verified: the documented
single-line start form targeting the 1.4.2 beta, the fatal deprecated
parameters' absence, readiness failing closed without any ZMod object,
plain .gcode delivery, the report-only firmware-update posture, and
the firmware-mismatch warnings in both directions — including the
ZMod AD5M direction, which passed readiness silently before #1506.
"""

import json

import pytest

from print_handy import firmware_update, printers, settings
from print_handy.printers.base import MultiColorUnsupported
from print_handy.printers.flashforge_forgex import facts, firmware_check

from .test_adapters import adapter_for

KEYS = ("flashforge_forgex.ad5m", "flashforge_forgex.ad5m_pro")
ZMOD_KEYS = ("flashforge.ad5m", "flashforge.ad5m_pro")

DOCUMENTED_START = (
    "SET_PRINT_STATS_INFO TOTAL_LAYER=[total_layer_count]\n"
    "START_PRINT EXTRUDER_TEMP=[nozzle_temperature_initial_layer] "
    "BED_TEMP=[bed_temperature_initial_layer_single]"
)


def _ready_status():
    return {"print_stats": {"state": "standby"}, "webhooks": {"state": "ready"},
            "extruder": {"temperature": 24.0}, "heater_bed": {"temperature": 24.0}}


@pytest.mark.parametrize("key", KEYS)
def test_model_is_a_single_extruder_on_its_own_vendor(key):
    model = printers.MODELS[key]
    assert model.vendor is printers.VENDORS["flashforge_forgex"]
    assert model.vendor.connection == "moonraker"
    assert model.vendor.start_code == "klipper_macro"
    assert model.vendor.file_formats == (".gcode",)
    assert model.label.endswith("(Forge-X)")
    assert model.color_system == "single"
    defaults = model.defaults()["printer"]
    assert defaults["bed_mm"] == [220.0, 220.0]
    assert defaults["max_print_height_mm"] == 220.0
    assert defaults["filament_slots"] == 1
    assert defaults["firmware"] == "Forge-X"
    assert defaults["start_macro"] == "START_PRINT"
    assert defaults["end_macro"] == "END_PRINT"
    assert model.supported_nozzle_sizes == (0.25, 0.4, 0.6, 0.8)
    for fact in facts.AD5M.values():
        assert fact.verified is False
        assert fact.source == "FORGEX"
    # Ceilings: extruder 350 C is beta-config-sourced, bed 130 C is
    # sourced from the beta config; no chamber ceiling exists to record,
    # because neither model has a chamber heater on Forge-X.
    assert facts.AD5M["max_hotend_c"].value == 350.0
    assert facts.AD5M["max_bed_c"].value == 130.0
    assert "max_chamber_c" not in facts.AD5M


@pytest.mark.parametrize("key", KEYS)
def test_adapter_reports_the_sourced_ceilings(key):
    adapter = adapter_for(key)
    from print_handy.core import heater_ceiling
    assert heater_ceiling.ceiling(adapter, "bed") == 130.0
    assert heater_ceiling.refusal(adapter, "bed", 131) is not None
    assert heater_ceiling.refusal(adapter, "bed", 130) is None
    # No chamber fact: the shared machinery keeps its unknown fallback.
    assert heater_ceiling.ceiling(adapter, "chamber") == heater_ceiling.UNKNOWN_HEATER_CEILING_C


@pytest.mark.parametrize("key", KEYS)
def test_tool_mapping_refuses_anything_but_the_single_extruder(key):
    adapter = adapter_for(key)
    assert adapter.multi_tool is False
    assert (adapter.slot_count, adapter.multi_slot) == (1, False)
    assert adapter.supported_accessories == frozenset()
    assert adapter.normalize_tool_mapping({"0": 1}, {}) == {"0": 1}
    with pytest.raises(MultiColorUnsupported, match="single extruder"):
        adapter.normalize_tool_mapping({"1": 2}, {})


@pytest.mark.parametrize("key", KEYS)
def test_moonraker_objects_are_the_standard_single_extruder_set(key):
    adapter = adapter_for(key)
    assert adapter.moonraker_objects() == [
        "webhooks", "print_stats", "virtual_sdcard", "extruder", "heater_bed",
        "exclude_object", "mod_params"]


@pytest.mark.parametrize("key", KEYS)
def test_start_contract_is_the_documented_beta_form(key):
    adapter = adapter_for(key)
    machine = json.loads((adapter.templates_dir() / "machine.json").read_text())
    assert machine["machine_start_gcode"] == DOCUMENTED_START
    # The adapter builds the same form whatever a template carries, and
    # the shared mesh argument is never written into it.
    assert adapter.machine_start_gcode(machine["machine_start_gcode"], "MESH_DATA") == DOCUMENTED_START
    assert adapter.machine_start_gcode("anything", "auto") == DOCUMENTED_START
    plan = adapter.start_plan(60, 215)
    assert plan == ["SET_PRINT_STATS_INFO TOTAL_LAYER=[total_layer_count]",
                    "START_PRINT EXTRUDER_TEMP=215 BED_TEMP=60"]
    # START_PRINT itself is a single line carrying both temperatures.
    start_lines = [line for line in plan if line.startswith("START_PRINT")]
    assert len(start_lines) == 1
    assert "EXTRUDER_TEMP=" in start_lines[0] and "BED_TEMP=" in start_lines[0]
    # The dangerous absences: no MESH= (its meaning is screen-mode
    # dependent), and neither deprecated parameter Forge-X rejects
    # fatally — Z_OFFSET cancels the print on this firmware.
    for text in (machine["machine_start_gcode"], "\n".join(plan)):
        for forbidden in ("MESH=", "Z_OFFSET", "SKIP_ZOFFSET", "PRINT_START"):
            assert forbidden not in text, forbidden
    # Print end is END_PRINT, present in template and adapter alike.
    assert machine["machine_end_gcode"] == "END_PRINT"
    assert adapter.machine_end_gcode(machine["machine_end_gcode"]) == "END_PRINT"
    assert adapter.machine_end_gcode("") == "END_PRINT"


@pytest.mark.parametrize("key", KEYS)
def test_templates_carry_the_forgex_deltas(key):
    adapter = adapter_for(key)
    folder = adapter.templates_dir()
    machine = json.loads((folder / "machine.json").read_text())
    process = json.loads((folder / "process.json").read_text())
    assert machine["printable_area"] == ["0x0", "220x0", "220x220", "0x220"]
    assert machine["printable_height"] == "220"
    assert machine["nozzle_diameter"] == ["0.4"]
    assert machine["host_type"] == "moonraker"
    assert machine["printer_agent"] == "moonraker"
    assert machine["use_3mf"] == "0"
    # Beta slicer guidance: 512x512 thumbnails for the Feather preview.
    assert machine["thumbnails"] == ["512x512/PNG"]
    assert machine["gcode_flavor"] == "klipper"
    # KAMP needs labeled objects and exclude objects (ff5m docs).
    assert process["gcode_label_objects"] == "1"
    assert process["exclude_object"] == "1"
    assert process["enable_arc_fitting"] == "0"
    # The beta's native timelapse handles frames itself: the layer
    # change carries the layer statistics and no frame call, anywhere.
    assert machine["layer_change_gcode"] == (
        ";AFTER_LAYER_CHANGE\n;[layer_z]\n"
        "SET_PRINT_STATS_INFO CURRENT_LAYER={layer_num + 1}")
    for name in ("machine", "process", "filament"):
        assert "TIMELAPSE_TAKE_FRAME" not in (folder / f"{name}.json").read_text()


@pytest.mark.parametrize("key", KEYS)
def test_readiness_fails_closed_and_needs_no_zmod_object(key):
    adapter = adapter_for(key)
    ready = {**_ready_status(), "mod_params": {"variables": {"display": 1}}}  # Feather (#235 F1)
    assert adapter.readiness_problems({}, ready) == []
    # No screen report is a refusal of its own (#235 audit F1).
    assert any("screen mode is unknown" in p for p in adapter.readiness_problems({}, _ready_status()))
    assert adapter.readiness_problems({}, {})
    missing_extruder = _ready_status()
    del missing_extruder["extruder"]
    assert any("extruder" in p for p in adapter.readiness_problems({}, missing_extruder))
    missing_bed = _ready_status()
    del missing_bed["heater_bed"]
    assert any("heater_bed" in p for p in adapter.readiness_problems({}, missing_bed))
    busy = _ready_status()
    busy["print_stats"] = {"state": "printing"}
    assert any("busy" in p for p in adapter.readiness_problems({}, busy))


@pytest.mark.parametrize("key", KEYS)
def test_status_is_the_basic_single_extruder_status(key):
    adapter = adapter_for(key)
    out = adapter.normalize_status({"print_stats": {"state": "printing"},
                                    "extruder": {"temperature": 210.0}})
    assert out["state"] == "printing"
    assert "slots" not in out
    assert "toolheads" not in out


@pytest.mark.parametrize("key", KEYS)
def test_delivery_is_plain_gcode_even_with_a_new_zmod_version(key, tmp_path):
    """The ZMod .gcode.3mf floor must not leak onto the Forge-X models:
    the upload artifact stays the plain .gcode even when the state names
    a ZMod version far above the 3MF floor."""
    from print_handy.launch.print_now import _zmod_upload_artifact

    adapter = adapter_for(key)
    assert "zmod_delivery_format" not in adapter.config["printer"]
    gcode = tmp_path / "plate_1.gcode"
    gcode.write_text("G28\n")
    state = {"nozzle": {"diameter_mm": 0.4}}
    upload, sha, remote, note = _zmod_upload_artifact(
        adapter, state, gcode, "deadbeef", "plate_1.gcode")
    assert (upload, sha, remote, note) == (gcode, "deadbeef", "plate_1.gcode", None)


def test_zmod_delivery_format_setting_is_refused_for_forgex_models():
    with pytest.raises(settings.SettingsError, match="zmod_delivery_format"):
        settings.load(local=False, environ={
            "PRINT_HANDY__PRINTER__MODEL": KEYS[0],
            "PRINT_HANDY__PRINTER__ZMOD_DELIVERY_FORMAT": "auto"})


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("evidence", [{"zmod_color": {"active_tool_id": 0}},
                                      {"zmod_ifs": {}},
                                      {"zmod": {"version": "1.7.6"}}])
def test_forgex_model_pointed_at_zmod_warns(key, evidence):
    adapter = adapter_for(key)
    status = _ready_status()
    status.update(evidence)
    problems = adapter.readiness_problems({}, status)
    assert any("appears to be running ZMod, not Forge-X" in p for p in problems), problems


@pytest.mark.parametrize("key", ZMOD_KEYS)
@pytest.mark.parametrize("evidence", ["mod_params", "forge-x"])
def test_zmod_model_pointed_at_forgex_warns(key, evidence):
    """The direction that passed silently before #1506: a ZMod AD5M
    model at a Forge-X printer now names the firmware mismatch."""
    adapter = adapter_for(key)
    status = _ready_status()
    status[evidence] = {}
    problems = adapter.readiness_problems({}, status)
    assert any("appears to be running Forge-X, not ZMod" in p for p in problems), problems


@pytest.mark.parametrize("key", ZMOD_KEYS)
def test_zmod_model_without_evidence_keeps_its_plain_readiness(key):
    adapter = adapter_for(key)
    assert adapter.readiness_problems({}, _ready_status()) == []


def test_zmod_creator_model_does_not_gain_a_forgex_warning():
    """The mirror hook sits on the exact single-extruder class only: the
    Creator multi-tool subclass, whose readiness shares that method,
    keeps its own wording (and its OpenCreator warning) unchanged."""
    adapter = adapter_for("flashforge.creator5")
    status = _ready_status()
    for tool in range(1, 4):
        status[f"extruder{tool}"] = {"temperature": 24.0}
    status["mod_params"] = {}
    problems = adapter.readiness_problems({}, status)
    assert problems, problems
    assert not any("Forge-X" in p for p in problems), problems


def test_firmware_check_reads_names_only_and_absence_is_not_evidence():
    assert firmware_check.forgex_evidence({}) == []
    assert firmware_check.forgex_evidence({"extruder": {}}) == []
    assert firmware_check.looks_like_zmod({"zmod_color": None}) is False
    assert firmware_check.looks_like_zmod({"extruder": {}}) is False
    assert firmware_check.zmod_mismatch_problem({}) is None
    assert firmware_check.forgex_mismatch_problem({}) is None
    assert firmware_check.forgex_mismatch_problem({"zmod_color": {}, "mod_params": {}}) is None


def test_spool_remain_reads_a_present_number_and_ignores_absence():
    adapter = adapter_for(KEYS[0])
    assert adapter.spool_remain_g({"extruder": {"filament_remaining_g": 120.5}}, 0) == 120.5
    assert adapter.spool_remain_g({"extruder": {"spool_remain_g": 0}}, 0) == 0.0
    assert adapter.spool_remain_g({"extruder": {}}, 0) is None
    assert adapter.spool_remain_g({}, 0) is None


def test_firmware_updates_are_reporting_only_for_forgex_models():
    """The registry choice is the apply decision (#1506): family "none"
    keeps the Forge-X models out of stage 2 apply, and the stage 4
    guidance entry covers them so the registry coverage test passes."""
    from print_handy.knowledge import registry
    entries = {entry.key: entry for entry in registry.load_registry().printers}
    for key in KEYS:
        entry = entries[key]
        assert entry.firmware_family == "none"
        assert firmware_update.is_apply_eligible(entry) is False
        assert firmware_update.apply_block_reason(entry) is not None
        guidance = firmware_update.guidance_for(entry)
        assert guidance is not None, key
        assert "Forge-X" in guidance.title
        assert "Software Update" in guidance.manual_steps


def test_docs_state_the_beta_only_targeting():
    """#1506's targeting decision, pinned in the user docs (catduck's
    final word): the 1.4.2 beta only now, stable 1.4.1 not supported,
    and the recorded promotion to stable when 1.4.2 ships stable."""
    doc = (settings.ROOT / "docs" / "FORGEX.md").read_text()
    assert "1.4.2 beta only" in doc
    assert "1.4.1 is not supported by these models" in doc.replace("**", "")
    assert "moves" in doc and "stays" in doc  # the promotion path
    assert "same common contract" not in doc
    assert "beta-primary" not in doc


def test_model_is_in_the_web_ui_printer_picker():
    from print_handy.webui.catalog import choices_for, field_by_id
    choices = choices_for(field_by_id("printer.model"))
    for key in KEYS:
        assert key in choices
