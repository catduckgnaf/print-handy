"""Printer accessories framework and the Bondtech INDX accessory (#1198).

Offline only: registry and parsing, the verified configfile detection
signal, settings validation, the Klipper adapter's effective shared-tool
shape, and the pure Orca machine overrides. No live hardware is claimed.
"""
import tempfile
import tomllib
from pathlib import Path

import pytest

from print_handy import printers, settings
from print_handy.knowledge.registry import COLOR_SYSTEMS as KNOWLEDGE_COLOR_SYSTEMS
from print_handy.printers import accessories
from print_handy.printers.accessories import (
    ACCESSORIES, INDX_TOOL_CHANGE_GCODE, ToolRecord, active_tool,
    configured_accessories, detect_indx, indx_machine_overrides,
    indx_refusals, parse_indx_tools)
from print_handy.printers.base import COLOR_SYSTEMS, MultiColorUnsupported, PrinterModel
from print_handy.printers.klipper.adapter import KlipperAdapter

ROOT = Path(__file__).resolve().parents[1]

INDX_ENTRY = {
    "key": "bondtech.indx",
    "tool_count": 3,
    "tools": [
        {"tool": 0, "nozzle_size_mm": 0.4, "nozzle_type": "cht", "filament": "PLA"},
        {"tool": 1, "nozzle_size_mm": 0.6, "nozzle_type": "cht", "filament": "PETG"},
        {"tool": 2, "heats": False},
    ],
}


def klipper_adapter(config=None):
    model = printers.MODELS["klipper.generic"]
    return KlipperAdapter(config or {"printer": {"model": "klipper.generic"}}, model)


def indx_adapter():
    return klipper_adapter({"printer": {"model": "klipper.generic"},
                            "accessories": [dict(INDX_ENTRY)]})


def load_settings(text, environ=None):
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.yaml"
        local.write_text(text)
        return settings.load(local=local, environ=environ or {})


# --------------------------------------------------------------------- registry

def test_indx_profile_is_registered_as_a_shared_tool_toolchanger():
    profile = ACCESSORIES["bondtech.indx"]
    assert profile.kind == "toolchanger"
    assert profile.confers_color_system == "shared_tool"
    assert profile.connections == ("moonraker",)
    assert "indx" in profile.detection and "tool_count" in profile.detection


def test_configured_accessories_resolves_profiles_and_names_unknown_keys():
    assert configured_accessories({}) == ()
    assert configured_accessories({"accessories": []}) == ()
    found = configured_accessories({"accessories": [{"key": "bondtech.indx"}]})
    assert found == (ACCESSORIES["bondtech.indx"],)
    with pytest.raises(ValueError, match="acme\\.warp-drive"):
        configured_accessories({"accessories": [{"key": "acme.warp-drive"}]})


def test_accessories_registry_toml_matches_code_profiles():
    registry_dir = ROOT / "print_handy" / "data" / "accessories-registry"
    entries = []
    for path in sorted(registry_dir.glob("*.toml")):
        with open(path, "rb") as handle:
            entries.extend(tomllib.load(handle).get("accessory", []))
    assert {entry["key"] for entry in entries} == set(ACCESSORIES)
    for entry in entries:
        profile = ACCESSORIES[entry["key"]]
        assert entry["label"] == profile.label
        assert entry["kind"] == profile.kind
        assert entry["confers_color_system"] == profile.confers_color_system
        assert tuple(entry["connections"]) == profile.connections
        assert entry["detection"] == profile.detection
        assert entry["notes"] == profile.notes


def test_shared_tool_is_a_color_system_but_never_a_model_trait():
    assert "shared_tool" in COLOR_SYSTEMS
    assert KNOWLEDGE_COLOR_SYSTEMS == COLOR_SYSTEMS
    with pytest.raises(ValueError, match="shared_tool"):
        PrinterModel(key="klipper.fake", label="Fake", adapter=KlipperAdapter,
                     templates="klipper_generic", settings={}, color_system="shared_tool")


# --------------------------------------------------------------------- INDX record parsing

def test_parse_indx_tools_reads_records_and_no_heat_tools():
    records = parse_indx_tools(INDX_ENTRY)
    assert records[0] == ToolRecord(tool=0, nozzle_size_mm=0.4, nozzle_type="cht", filament="PLA")
    assert records[2].heats is False
    assert records[2].nozzle_size_mm is None and records[2].filament == ""


def test_parse_indx_tools_has_no_upper_tool_count_bound():
    entry = {"key": "bondtech.indx", "tool_count": 15,
             "tools": [{"tool": 14, "nozzle_size_mm": 0.4, "nozzle_type": "standard",
                        "filament": "TPU"}]}
    records = parse_indx_tools(entry)
    assert records[0].tool == 14
    assert accessories.indx_tool_count(entry) == 15


@pytest.mark.parametrize("entry,match", [
    ({"tool_count": 1}, "at least 2"),
    ({"tool_count": "four"}, "whole number"),
    ({"tool_count": 3, "tools": [{"tool": 3, "nozzle_size_mm": 0.4,
                                  "nozzle_type": "cht", "filament": "PLA"}]}, "outside the tool count"),
    ({"tool_count": 3, "tools": [{"tool": 0, "nozzle_size_mm": 0.4, "nozzle_type": "cht",
                                  "filament": "PLA"},
                                 {"tool": 0, "nozzle_size_mm": 0.4, "nozzle_type": "cht",
                                  "filament": "PLA"}]}, "recorded twice"),
    ({"tool_count": 3, "tools": [{"tool": 0, "nozzle_size_mm": 0,
                                  "nozzle_type": "cht", "filament": "PLA"}]}, "nozzle_size_mm"),
    ({"tool_count": 3, "tools": [{"tool": 0, "nozzle_size_mm": 0.4,
                                  "nozzle_type": "brass", "filament": "PLA"}]}, "nozzle_type"),
    ({"tool_count": 3, "tools": [{"tool": 0, "heats": False, "nozzle_size_mm": 0.4}]},
     "no nozzle or filament"),
    ({"tool_count": 3, "tools": [{"tool": 0, "heats": False, "filament": "PLA"}]},
     "no nozzle or filament"),
])
def test_parse_indx_tools_refuses_bad_records(entry, match):
    with pytest.raises(ValueError, match=match):
        parse_indx_tools(entry)


# --------------------------------------------------------------------- detection and live state

def configfile_settings(**overrides):
    payload = {
        "indx": {"mcu": "indxmcu"},
        "mcu indxmcu": {"serial": "/dev/serial/by-id/usb-Bondtech_INDX_1"},
        "extruder": {"sensor_type": "indx", "heater_pin": "indx:heater"},
        "gcode_macro tool_positions": {"tool_count": "4"},
    }
    payload.update(overrides)
    return payload


def test_detect_indx_reads_tool_count_from_tool_positions():
    assert detect_indx(configfile_settings()) == 4
    assert detect_indx(configfile_settings(
        **{"gcode_macro tool_positions": {"tool_count": "15"}})) == 15


@pytest.mark.parametrize("payload", [
    {},
    {"mcu indxmcu": {}, "extruder": {"sensor_type": "indx"},
     "gcode_macro tool_positions": {"tool_count": "4"}},
    {"indx": {}, "extruder": {"sensor_type": "indx"},
     "gcode_macro tool_positions": {"tool_count": "4"}},
    {"indx": {}, "mcu indxmcu": {}, "extruder": {"sensor_type": "generic_thermistor"},
     "gcode_macro tool_positions": {"tool_count": "4"}},
    {"indx": {}, "mcu indxmcu": {}, "extruder": {"sensor_type": "indx"}},
    {"indx": {}, "mcu indxmcu": {}, "extruder": {"sensor_type": "indx"},
     "gcode_macro tool_positions": {"tool_count": "many"}},
])
def test_detect_indx_without_the_full_signal_is_none(payload):
    assert detect_indx(payload) is None


def test_active_tool_and_refusals_name_the_indx_states():
    assert active_tool({"active_tool": 2}) == 2
    assert active_tool({"active_tool": -1}) is None
    assert active_tool({}) is None
    assert indx_refusals({"seat_suspect": -1}) == []
    seat = indx_refusals({"seat_suspect": 1})
    assert len(seat) == 1 and "seat_suspect" in seat[0] and "T1" in seat[0]
    docks = indx_refusals({"docks_calibrated": False})
    assert len(docks) == 1 and "docks_calibrated" in docks[0]
    assert len(indx_refusals({"seat_suspect": 0, "docks_calibrated": "False"})) == 2


# --------------------------------------------------------------------- Orca overrides (pure)

def test_indx_machine_overrides_expand_the_single_extruder_template():
    overrides = indx_machine_overrides(INDX_ENTRY)
    assert INDX_TOOL_CHANGE_GCODE == "CHANGE_TOOL TOOL={next_extruder} TEMP={new_filament_temp}"
    assert overrides["change_filament_gcode"] == INDX_TOOL_CHANGE_GCODE
    assert overrides["single_extruder_multi_material"] == "0"
    # Per-tool diameters from the records; the no-heat tool takes the first
    # recorded diameter because Orca still numbers it as an extruder.
    assert overrides["nozzle_diameter"] == ["0.4", "0.6", "0.4"]
    assert len(overrides["extruder_colour"]) == 3
    assert len(overrides["extruder_offset"]) == 3


# --------------------------------------------------------------------- settings validation

def test_settings_refuse_an_unknown_accessory_by_name():
    with pytest.raises(settings.SettingsError, match="acme\\.warp-drive"):
        load_settings('accessories:\n  - key: "acme.warp-drive"\n')


def test_settings_refuse_a_bad_indx_record():
    with pytest.raises(settings.SettingsError, match="tool_count"):
        load_settings('accessories:\n  - key: "bondtech.indx"\n    tool_count: 1\n')


def test_a_valid_indx_config_loads_and_shapes_the_adapter():
    data = load_settings(
        "printer:\n  model: \"klipper.generic\"\n"
        "accessories:\n"
        "  - key: \"bondtech.indx\"\n"
        "    tool_count: 3\n"
        "    tools:\n"
        "      - tool: 0\n"
        "        nozzle_size_mm: 0.4\n"
        "        nozzle_type: \"cht\"\n"
        "        filament: \"PLA\"\n")
    assert data["accessories"][0]["key"] == "bondtech.indx"
    assert data["accessories"][0]["tool_count"] == 3
    adapter = printers.get("klipper.generic", data)
    assert adapter.color_system == "shared_tool"
    assert adapter.shared_tool and adapter.tool_based and not adapter.multi_tool
    assert adapter.slot_count == 3 and adapter.tool_count == 3


def test_no_accessories_config_keeps_the_model_shape():
    data = load_settings("printer:\n  model: \"klipper.generic\"\n")
    assert data["accessories"] == []
    adapter = printers.get("klipper.generic", data)
    assert adapter.color_system == "single"
    assert not adapter.shared_tool and not adapter.tool_based


# --------------------------------------------------------------------- Klipper adapter

def test_klipper_without_indx_is_unchanged():
    adapter = klipper_adapter()
    assert (adapter.slot_count, adapter.tool_count) == (1, 1)
    assert adapter.normalize_tool_mapping({"0": 1}, {}) == {"0": 1}
    with pytest.raises(MultiColorUnsupported):
        adapter.normalize_tool_mapping({"1": 2}, {})
    assert adapter.moonraker_objects() == ["webhooks", "print_stats", "virtual_sdcard",
                                           "extruder", "heater_bed", "exclude_object"]
    out = adapter.normalize_status({"print_stats": {"state": "standby"}})
    assert "active_tool" not in out and "indx_refusals" not in out
    assert adapter.gcode_problems("T0\nT1\n") == []


def test_klipper_indx_counts_and_identity_mapping():
    adapter = indx_adapter()
    assert (adapter.slot_count, adapter.tool_count) == (3, 3)
    assert adapter.normalize_tool_mapping({"0": 1, "2": 3}, {}) == {"0": 1, "2": 3}
    assert adapter.normalize_tool_mapping({}, {}) == {"0": 1}
    with pytest.raises(MultiColorUnsupported, match="always prints with INDX tool 2"):
        adapter.normalize_tool_mapping({"1": 3}, {})
    with pytest.raises(MultiColorUnsupported, match="T0-T2"):
        adapter.normalize_tool_mapping({"3": 4}, {})


def test_klipper_indx_objects_and_status():
    adapter = indx_adapter()
    objects = adapter.moonraker_objects()
    assert "save_variables" in objects and "configfile" in objects
    raw = {"print_stats": {"state": "printing", "filename": "a.gcode"},
           "virtual_sdcard": {"progress": 0.5},
           "extruder": {"temperature": 210.0, "target": 210.0},
           "heater_bed": {"temperature": 60.0, "target": 60.0},
           "save_variables": {"variables": {"active_tool": 1, "seat_suspect": -1}}}
    out = adapter.normalize_status(raw)
    assert out["active_tool"] == 1 and out["indx_refusals"] == []
    # The one extruder temperature is the active tool's; nothing per-tool
    # is invented beyond what basic_status derives from real extruders.
    assert out["hotend_c"] == 210.0
    raw["save_variables"]["variables"] = {"active_tool": -1, "seat_suspect": 2}
    out = adapter.normalize_status(raw)
    assert out["active_tool"] is None
    assert len(out["indx_refusals"]) == 1 and "seat_suspect" in out["indx_refusals"][0]


def test_klipper_indx_status_names_uncalibrated_docks_from_configfile():
    adapter = indx_adapter()
    raw = {"print_stats": {"state": "standby"},
           "save_variables": {"variables": {"active_tool": -1}},
           "configfile": {"settings": {"gcode_macro tool_positions":
                                        {"docks_calibrated": False}}}}
    out = adapter.normalize_status(raw)
    assert len(out["indx_refusals"]) == 1 and "docks_calibrated" in out["indx_refusals"][0]


def test_klipper_indx_readiness_names_refusals():
    adapter = indx_adapter()
    ready = {"print_stats": {"state": "standby"}, "webhooks": {"state": "ready"},
             "save_variables": {"variables": {"active_tool": 0, "seat_suspect": -1}}}
    assert adapter.readiness_problems({}, ready) == []
    refused = {"print_stats": {"state": "standby"}, "webhooks": {"state": "ready"},
               "save_variables": {"variables": {"active_tool": 0, "seat_suspect": 0}}}
    problems = adapter.readiness_problems({}, refused)
    assert len(problems) == 1 and "seat_suspect" in problems[0]


def test_klipper_indx_gcode_guard_requires_change_tool():
    adapter = indx_adapter()
    problems = adapter.gcode_problems("T0\nG1 X1\nT1\nG1 X2\n")
    assert len(problems) == 1 and "CHANGE_TOOL" in problems[0]
    assert adapter.gcode_problems("CHANGE_TOOL TOOL=1 TEMP=210\nG1 X2\n") == []
    assert adapter.gcode_problems("T0\nG1 X1\n") == []
