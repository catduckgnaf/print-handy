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
        assert entry["hosts"] == profile.hosts
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
        load_settings('printer:\n  model: "klipper.generic"\n'
                      'accessories:\n  - key: "bondtech.indx"\n    tool_count: 1\n')


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


# --------------------------------------------------------------------- per-printer binding (#1198 review)

INDX_YAML = ("    accessories:\n"
             "      - key: bondtech.indx\n"
             "        tool_count: 4\n")

OTHER_PRINTERS = {
    "ad5x": "    model: flashforge.ad5x\n    moonraker_url: http://192.0.2.10:7125\n",
    "creator": "    model: flashforge.creator5_pro\n    moonraker_url: http://192.0.2.11:7125\n",
    "x1c": ("    model: bambulab.x1c\n    lan_host: 192.0.2.60\n"
            "    lan_serial: \"00M00A000000000\"\n"),
    "coreone": "    model: prusa.coreone\n    link_host: 192.0.2.70\n",
    "chiron": "    model: octoprint.chiron\n    octoprint_url: http://192.0.2.80\n",
    "sv06": "    model: sovol.sv06\n    moonraker_url: http://192.0.2.12:7125\n",
}
INDX_PRINTER = "    model: klipper.generic\n    moonraker_url: http://192.0.2.13:7125\n" + INDX_YAML


def roster_yaml(indx_first):
    blocks = list(OTHER_PRINTERS.items())
    blocks.insert(0 if indx_first else 3, ("indx", INDX_PRINTER))
    return "printers:\n" + "".join(f"  - id: {pid}\n{body}" for pid, body in blocks)


SEAT_SUSPECT_STATUS = {"print_stats": {"state": "standby"}, "webhooks": {"state": "ready"},
                       "save_variables": {"variables": {"active_tool": 0, "seat_suspect": 1}}}


@pytest.mark.parametrize("indx_first", [True, False], ids=["indx-default", "indx-later"])
def test_a_roster_gives_the_indx_shape_only_to_the_printer_that_has_it(indx_first):
    # The review's leak: with INDX on one Klipper printer, every other
    # printer in the roster (and every adapter family) must keep its own
    # shape, readiness, G-code guard and slice profile.
    data = load_settings(roster_yaml(indx_first))
    shapes = {}
    for entry in data["printers"]:
        view = settings.view_for(entry["id"], data)
        adapter = printers.get(entry["model"], view)
        model = printers.MODELS[entry["model"]]
        shapes[entry["id"]] = adapter.color_system
        overrides = accessories.slice_machine_overrides(adapter)
        if entry["id"] == "indx":
            assert view["accessories"][0]["key"] == "bondtech.indx"
            assert adapter.shared_tool and adapter.tool_based and adapter.tool_count == 4
            assert overrides["change_filament_gcode"] == INDX_TOOL_CHANGE_GCODE
            assert any("seat_suspect" in p for p in adapter.readiness_problems({}, SEAT_SUSPECT_STATUS))
            assert any("CHANGE_TOOL" in p for p in adapter.gcode_problems("T0\nG1 X1\nT1\n"))
            continue
        assert view["accessories"] == [] and "accessories" not in entry
        assert adapter.accessories == ()
        assert adapter.color_system == model.color_system
        assert not adapter.shared_tool
        assert adapter.tool_based == (model.color_system == "multi_tool")
        assert overrides == {}
        if isinstance(adapter, KlipperAdapter):
            # The adapters that inherit the INDX code paths never take them.
            assert not any("seat_suspect" in p
                           for p in adapter.readiness_problems({}, SEAT_SUSPECT_STATUS))
            assert not any("CHANGE_TOOL" in p
                           for p in adapter.gcode_problems("T0\nG1 X1\nT1\n"))
            if adapter.connection == "moonraker":
                assert "save_variables" not in adapter.moonraker_objects()
                assert "indx_refusals" not in adapter.normalize_status(SEAT_SUSPECT_STATUS)
    assert shapes == {"indx": "shared_tool", "ad5x": "multi_slot", "creator": "multi_tool",
                      "x1c": "multi_slot", "coreone": "single", "chiron": "single",
                      "sv06": "single"}


def test_a_top_level_accessories_list_beside_a_roster_is_refused_by_name():
    text = roster_yaml(False) + "accessories:\n  - key: bondtech.indx\n    tool_count: 4\n"
    with pytest.raises(settings.SettingsError,
                       match=r"\[accessories\] at the top level applies only to a single-printer"):
        load_settings(text)


def test_a_single_printer_top_level_list_binds_to_that_printer():
    data = load_settings("printer:\n  model: klipper.generic\n"
                         "services:\n  moonraker_url: http://192.0.2.13:7125\n"
                         "accessories:\n  - key: bondtech.indx\n    tool_count: 3\n")
    entry = data["printers"][0]
    assert entry["accessories"][0]["tool_count"] == 3
    assert data["accessories"] == entry["accessories"]
    assert printers.current(data).shared_tool


@pytest.mark.parametrize("model,body,match", [
    ("bambulab.x1c", "    lan_host: 192.0.2.60\n    lan_serial: \"00M00A000000000\"\n",
     "attaches only to a printer reached over moonraker; bambulab.x1c connects over bambulab_lan"),
    ("prusa.coreone", "    link_host: 192.0.2.70\n", "connects over prusalink"),
    ("octoprint.chiron", "    octoprint_url: http://192.0.2.80\n", "connects over octoprint"),
    ("elegoo_sdcp.centauricarbon", "    host: 192.0.2.90\n", "connects over elegoo_sdcp"),
    ("flashforge.ad5x", "    moonraker_url: http://192.0.2.10:7125\n",
     "flashforge.ad5x uses an adapter with no Bondtech INDX support"),
    ("qidi.xplus4", "    moonraker_url: http://192.0.2.14:7125\n",
     "qidi.xplus4 uses an adapter with no Bondtech INDX support"),
    ("snapmaker.u1", "    moonraker_url: http://192.0.2.15:7125\n",
     "snapmaker.u1 uses an adapter with no Bondtech INDX support"),
])
def test_indx_on_a_host_it_does_not_attach_to_is_refused_by_name(model, body, match):
    text = f"printers:\n  - id: host\n    model: {model}\n{body}{INDX_YAML}"
    with pytest.raises(settings.SettingsError, match=r"\[printers\] entry 'host' accessories: "):
        load_settings(text)
    with pytest.raises(settings.SettingsError, match=match):
        load_settings(text)


def test_indx_on_a_legacy_non_moonraker_printer_is_refused():
    with pytest.raises(settings.SettingsError, match=r"\[accessories\]: .*bambulab_lan"):
        load_settings("printer:\n  model: bambulab.x1c\n"
                      "bambu:\n  lan_host: 192.0.2.60\n  lan_serial: \"00M00A000000000\"\n"
                      "accessories:\n  - key: bondtech.indx\n    tool_count: 4\n")


@pytest.mark.parametrize("extra,match", [
    ("        tool_cont: 4\n", r"unknown key\(s\) tool_cont"),
    ("        tools:\n          - tool: 0\n            nozzle: 0.4\n", r"unknown key\(s\) nozzle"),
])
def test_misspelled_accessory_keys_are_refused_by_name(extra, match):
    text = "printers:\n  - id: indx\n" + INDX_PRINTER + extra
    with pytest.raises(settings.SettingsError, match=match):
        load_settings(text)


def test_the_environment_cannot_set_a_printers_accessories():
    text = "printers:\n  - id: indx\n" + INDX_PRINTER
    with pytest.raises(settings.SettingsError, match="config file only"):
        load_settings(text, environ={"PRINT_HANDY__PRINTERS__INDX__ACCESSORIES": "bondtech.indx"})


def test_runtime_gate_keeps_inherited_and_mismatched_adapters_plain():
    # Settings refuse these shapes; the adapter still never resolves them if
    # a config reaches it another way.
    config = {"printer": {"model": "octoprint.chiron"}, "accessories": [dict(INDX_ENTRY)]}
    octo = printers.get("octoprint.chiron", config)
    assert octo.accessories == () and not octo.shared_tool
    # An adapter for another model built on the INDX printer's view is not
    # that printer (a job recorded for a different model).
    view = {"printer": {"model": "klipper.generic"}, "accessories": [dict(INDX_ENTRY)]}
    other = printers.get("sovol.sv06", view)
    assert other.accessories == () and other.color_system == "single"
    assert accessories.slice_machine_overrides(other) == {}


def test_an_adapter_with_no_config_is_plain():
    # tests/test_webui_controls.py builds a bare KlipperAdapter this way.
    adapter = object.__new__(KlipperAdapter)
    assert adapter.accessories == () and not adapter.shared_tool
    assert "exclude_object" in adapter.moonraker_objects()


def test_add_printer_moves_a_single_printer_accessory_onto_its_entry(tmp_path):
    import yaml

    from print_handy import roster_write
    path = tmp_path / "local.yaml"
    path.write_text("printer:\n  model: klipper.generic\n  name: Bench INDX\n"
                    "services:\n  moonraker_url: http://192.0.2.13:7125\n"
                    "accessories:\n  - key: bondtech.indx\n    tool_count: 3\n")
    roster_write.add_printer({"id": "sv06", "model": "sovol.sv06", "name": "SV06",
                              "moonraker_url": "http://192.0.2.12:7125"},
                             config_path=path, home=settings.HOME, environ={},
                             timestamp="20261111-120000")
    parsed = yaml.safe_load(path.read_text())
    assert "accessories" not in parsed
    first, second = parsed["printers"]
    assert first["accessories"] == [{"key": "bondtech.indx", "tool_count": 3}]
    assert "accessories" not in second
    data = settings.load(home=settings.HOME, local=path, environ={})
    adapters = {e["id"]: printers.get(e["model"], settings.view_for(e["id"], data))
                for e in data["printers"]}
    assert adapters["bench-indx"].shared_tool and not adapters["sv06"].shared_tool
