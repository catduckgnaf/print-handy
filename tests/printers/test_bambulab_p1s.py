"""Bambu Lab P1S (bambulab.p1s), offline only: P1S-specific model and Orca-profile provenance checks."""
import copy
import json
import re
import subprocess
from pathlib import Path

import pytest

from print_handy import printers, settings
from print_handy.core import orca_adapter
from print_handy.printers.bambulab import VENDOR, facts, profiles
from print_handy.printers.bambulab.adapter import BambuOfflineAdapter, LiveUnsupported
from print_handy.printers.bambulab.p1s import MODEL, P1SAdapter
from print_handy.printers.base import MultiColorUnsupported

from . import p1s_fixture
from .test_bambulab_a1 import PLACEHOLDER_VALUES, identity_lines, render as a1_render

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "tests" / "pipeline" / "fixtures" / "printer_hardware.json"
P1S = "bambulab.p1s"
CUBE = ROOT / "tests" / "slicer" / "fixtures" / "cube.stl"


def config_for(key=P1S, hermes=p1s_fixture.HERMES, **printer):
    env = {"PRINT_HANDY__PRINTER__MODEL": key, "PRINT_HANDY__PATHS__HERMES_HOME": str(hermes),
           **{f"PRINT_HANDY__PRINTER__{k.upper()}": str(v) for k, v in printer.items()}}
    return settings.load(local=False, environ=env)


@pytest.fixture(autouse=True)
def p1s_profiles(monkeypatch):
    """Every test sees P1S templates generated from the invented fixture tree."""
    p1s_fixture.trust(monkeypatch.setattr)
    p1s_fixture.install(adapter_for(P1S))


def adapter_for(key=P1S, cfg=None, hermes=p1s_fixture.HERMES):
    return printers.get(key, cfg or config_for(key, hermes=hermes))


def template(name="machine", key=P1S):
    return json.loads((adapter_for(key).templates_dir() / f"{name}.json").read_text())


def state():
    st = copy.deepcopy(json.loads(STATE.read_text()))
    st.update(printer="Bambu Lab P1S", moonraker_url="", filament_slots={}, bed_mm=[256, 256], max_print_height_mm=256)
    st["nozzle"]["type"] = "stainless_steel"
    return st


def p1s_gcode(body=None, *, model="Bambu Lab P1S", nozzle="0.4", nozzle_type="stainless_steel", bed=60,
              hotend=210, material="PLA", generator="OrcaSlicer 2.4.2"):
    machine, filament = template(), template("filament")
    start = a1_render(machine["machine_start_gcode"]).replace("M140 S60", f"M140 S{bed}").replace(
        "M104 S210", f"M104 S{hotend}").replace("M109 S210", f"M109 S{hotend}")
    end = a1_render(machine["machine_end_gcode"])
    body = body or ("G1 E-.8 F1800\nG1 Z.2 F720\nG1 X100 Y100 F30000\nG1 E.8 F1800\n"
                    "; FEATURE: Outer wall\n; LINE_WIDTH: 0.42\nG1 X120 Y100 E.8\nG1 X120 Y120 E.8\n"
                    "G1 X100 Y120 E.8\nG1 X100 Y100 E.8\n; CHANGE_LAYER\n; Z_HEIGHT: 0.4\nG1 Z.4\n"
                    "G1 X100 Y100\nG1 X120 Y100 E.8\nG1 E-.8\nM106 S0\n")
    return "\n".join([
        "; HEADER_BLOCK_START", *identity_lines(generator), "; model printing time: 5m; total estimated time: 9m",
        "; HEADER_BLOCK_END", "; CONFIG_BLOCK_START", f"; printer_model = {model}",
        f"; printer_settings_id = {facts.P1S['orca_profiles'].value['machine']}",
        f"; print_settings_id = {facts.P1S['orca_profiles'].value['process']}",
        f'; filament_settings_id = "{facts.P1S["orca_profiles"].value["filament"]}"',
        f"; filament_type = {material}", f"; nozzle_diameter = {nozzle}", f"; nozzle_type = {nozzle_type}",
        "; curr_bed_type = Textured PEI Plate", "; z_hop_types = Normal Lift", "; enable_arc_fitting = 0",
        "; brim_type = outer_only", "; CONFIG_BLOCK_END", "; EXECUTABLE_BLOCK_START", "M73 P0 R20",
        "M201 X6000 Y6000 Z1500 E5000", "M204 P12000 R5000 T12000", "; FEATURE: Custom", start,
        a1_render(filament["filament_start_gcode"][0]), "G90", "G21", "M83 ; use relative distances for extrusion",
        "M106 S0", "; CHANGE_LAYER", "; Z_HEIGHT: 0.2", body, "; FEATURE: Custom",
        a1_render(filament["filament_end_gcode"][0]), end, "M73 P100 R0", "; EXECUTABLE_BLOCK_END", ""])


def test_model_registration_defaults_and_p1s_specific_hardware(p1s_profiles):
    a = adapter_for()
    assert printers.MODELS[P1S] == MODEL and MODEL.vendor is VENDOR is printers.VENDORS["bambulab"]
    assert isinstance(a, P1SAdapter) and isinstance(a, BambuOfflineAdapter)
    # The AMS is the P1S's slot system (#671): multi_slot, four slots.
    assert (a.connection, a.slot_count, a.multi_slot) == ("bambulab_lan", 4, True)
    volume = a.build_volume()
    # P2-64: the working volume is the slicer's usable height (the Orca
    # machine profile's 250 mm), not the physical 256 mm specification —
    # a 253 mm model must not be told it fits. The fact itself stays 256.
    assert volume.bed_mm == (256.0, 256.0) and volume.max_height_mm == 250.0
    assert a.supported_nozzle_sizes == (0.4,)
    assert a.supported_nozzle_types == ("stainless_steel", "hardened_steel")
    assert a.fact("bed_exclude_area") == ((0.0, 0.0), (18.0, 0.0), (18.0, 28.0), (0.0, 28.0))
    assert a.fact("ams") == ("AMS", 4) and a.fact("enclosed") is True


def test_p1s_defaults_to_bambu_studio_and_orca_can_be_selected(p1s_profiles):
    assert MODEL.defaults()["printer"]["slicer"] == "bambu_studio"
    assert adapter_for().verified_slicers == ("bambu_studio", "orca")
    assert config_for(slicer="orca")["printer"]["slicer"] == "orca"
    assert config_for()["printer"]["bed_mm"] == [256.0, 256.0]
    assert config_for()["printer"]["max_print_height_mm"] == 250.0  # P2-64: usable height, above
    # The P1S's own AMS count is the default since #671 (multi_slot).
    assert config_for()["printer"]["filament_slots"] == 4
    with pytest.raises(settings.SettingsError, match="not supported by model 'bambulab.p1s'"):
        config_for(nozzle_diameter_mm=0.6)


def test_p1s_has_a_distinct_generated_profile_folder_and_offline_calls_fail(p1s_profiles, tmp_path):
    a = adapter_for(hermes=tmp_path)
    assert a.templates_dir().name == "bambulab_p1s"
    assert a.templates_dir() == tmp_path / "state" / "orca-profiles" / "bambulab_p1s"
    for call in (lambda: a.moonraker_start_commands("x.gcode"), a.moonraker_objects):
        with pytest.raises(LiveUnsupported, match="Nothing was sent"):
            call()
    with pytest.raises(MultiColorUnsupported, match="AMS multi-color is not supported"):
        a.normalize_tool_mapping({"0": 1, "1": 2}, {})


def test_p1s_profile_defaults_identity_and_provenance_match_installed_orca_resources(p1s_profiles):
    names = facts.P1S["orca_profiles"].value
    assert names == {"machine": "Bambu Lab P1S 0.4 nozzle", "process": "0.20mm Standard @BBL X1C",
                     "filament": "Bambu PLA Basic @BBL P1S 0.4 nozzle"}
    machine = template()
    assert machine["printer_model"] == "Bambu Lab P1S"
    assert machine["printable_area"] == ["0x0", "256x0", "256x256", "0x256"]
    assert machine["printable_height"] == "250" and machine["bed_exclude_area"] == ["0x0", "18x0", "18x28", "0x28"]
    assert machine["default_print_profile"] == names["process"]
    assert machine["default_filament_profile"] == [names["filament"]]
    assert json.loads((adapter_for().pinned_templates_folder() / "SOURCE.json").read_text())["vendor_version"] == "00.00.00.00"
    hashes = facts.P1S["orca_identity"].value[0]["sha256"]
    assert set(hashes) == {"machine", "process", "filament"}
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes.values())


def test_missing_or_edited_profiles_refuse_to_validate_or_package(p1s_profiles, tmp_path):
    a = adapter_for(hermes=tmp_path)
    assert any("are not installed" in problem for problem in profiles.problems(a))
    with pytest.raises(profiles.ProfileError, match="not installed"):
        a.machine_start_gcode(template()["machine_start_gcode"])


def test_p1s_gcode_guard_accepts_p1s_and_rejects_a1_ad5x_and_temperature_abuse(p1s_profiles):
    assert adapter_for().gcode_problems(p1s_gcode(), state()) == []
    # #434: real-format Bambu Studio output (header-block identity) is accepted for the P1S too.
    assert adapter_for().gcode_problems(p1s_gcode(generator="BambuStudio 02.08.02.61"), state()) == []
    assert any("not 'Bambu Lab P1S'" in item for item in
               adapter_for().gcode_problems(p1s_gcode(model="Bambu Lab A1")))
    too_hot = p1s_gcode().replace("M109 S210", "M109 S301")
    assert any("limit of 300 C" in item for item in adapter_for().gcode_problems(too_hot))


def test_p1s_guard_refuses_extrusion_in_the_declared_exclusion(p1s_profiles):
    # P2-63: the P1S fact declares the front-left 18 x 28 mm exclusion; the
    # guard must hold toolpath extrusion out of it, not just check profile
    # identity. Extrusion inside it is refused, extrusion clear of it passes.
    inside = ("G1 Z.2 F720\nG1 X5 Y5 F30000\nG1 X10 Y5 E.8\nG1 X10 Y10 E.8\nG1 E-.8\nM106 S0\n")
    problems = adapter_for().gcode_problems(p1s_gcode(inside), state())
    assert any("exclusion" in item for item in problems), problems
    clear = ("G1 Z.2 F720\nG1 X20 Y5 F30000\nG1 X30 Y5 E.8\nG1 X30 Y15 E.8\nG1 E-.8\nM106 S0\n")
    assert adapter_for().gcode_problems(p1s_gcode(clear), state()) == []


def test_p1s_slice_passes_guard_while_preserving_no_live_delivery(p1s_profiles, tmp_path, monkeypatch):
    calls = []

    def run(command, **_):
        calls.append(list(command))
        Path(command[command.index("--outputdir") + 1], "plate_1.gcode").write_text(p1s_gcode())
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(orca_adapter.subprocess, "run", run)
    out, provenance, configs = orca_adapter.slice_model(CUBE, tmp_path / "slice", state(), adapter=adapter_for())
    assert out and provenance["slice_guard"]["status"] in ("ok", "warnings") and calls
    machine_path = next(path for path in configs if path.name == "machine.json")
    assert json.loads(machine_path.read_text())["printable_height"] == "250"


def test_hardware_record_cannot_exceed_the_p1s_printable_volume(p1s_profiles):
    a = adapter_for()
    assert a.readiness_problems(state(), {}) == []
    assert "height 300 mm exceeds" in " ".join(a.readiness_problems({**state(), "max_print_height_mm": 300}, {}))
    assert "bed 300x256 mm exceeds" in " ".join(a.readiness_problems({**state(), "bed_mm": [300, 256]}, {}))


def test_no_bambu_or_orca_profile_content_is_shipped_for_p1s(p1s_profiles):
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    assert not any("bambulab_p1s" in path.lower() and path.endswith(".json") for path in tracked)
    assert not (ROOT / "print_handy/templates/orca/bambulab_p1s").exists()


def test_p1s_bed_exclusion_is_not_mistaken_for_printable_area_or_ad5x_limits(p1s_profiles):
    assert facts.P1S["bed_mm"].value == (256.0, 256.0)
    assert facts.P1S["orca_profile_height_mm"].value == 250.0
    assert facts.P1S["bed_exclude_area"].value != facts.A1["bed_exclude_area"].value
    assert facts.P1S["ams"].value == ("AMS", 4) and facts.A1["ams"].value == ("AMS Lite", 4)
    assert facts.P1S["max_hotend_c"].value == 300 and facts.P1S["max_bed_c"].value == 100


def test_motion_envelope_covers_every_literal_test_fixture_vendor_move(p1s_profiles):
    machine = template()
    xmin, ymin, xmax, ymax = facts.P1S["motion_envelope_mm"].value
    for key in ("machine_start_gcode", "machine_end_gcode", "change_filament_gcode", "time_lapse_gcode"):
        xs = [float(v) for v in re.findall(r"^\s*G[0-3]\b[^;\n]*?X(-?\d+\.?\d*)", machine.get(key, ""), re.M)]
        ys = [float(v) for v in re.findall(r"^\s*G[0-3]\b[^;\n]*?Y(-?\d+\.?\d*)", machine.get(key, ""), re.M)]
        assert all(xmin <= value <= xmax for value in xs) and all(ymin <= value <= ymax for value in ys), key


def test_p1s_fit_uses_model_specific_working_volume(p1s_profiles):
    assert (adapter_for().fact("max_print_height_mm"), adapter_for().fact("orca_profile_height_mm")) == (256.0, 250.0)
    assert adapter_for().fact("bed_exclude_area")[2] == (18.0, 28.0)
    assert state()["bed_mm"] == [256, 256]


def test_retained_242_folder_still_resolves_its_pla_name(p1s_profiles, monkeypatch):
    # P2-128 / P2-129: a folder generated from OrcaSlicer 2.4.2 names its PLA
    # profile "Bambu PLA Basic @BBL X1C" (the X1C profile listed the P1S
    # then); the current facts name the dedicated P1S profile. The folder
    # still matches a retained identity, so a sliced file naming the old
    # profile must resolve to the folder's verified default template
    # instead of being refused as an unchecked profile.
    a = adapter_for()
    folder = a.pinned_templates_folder()
    old_name = "Bambu PLA Basic @BBL X1C"
    upstream = json.loads((folder / "upstream" / "filament.json").read_text())
    upstream["name"] = old_name
    (folder / "upstream" / "filament.json").write_text(profiles._dump(upstream))
    (folder / "filament.json").write_text(profiles.template_text("filament", upstream))
    identity = dict(p1s_fixture.identity())
    identity["sha256"] = {**identity["sha256"], "filament": profiles.sha256(upstream)}
    monkeypatch.setattr(P1SAdapter, "facts",
                        {**facts.P1S, "orca_identity": facts.Fact((identity,), "TEST", False)})
    assert profiles.problems(a, folder) == []  # the retained folder still verifies
    assert profiles.filament_for_settings_id(a, old_name, folder) == folder / "filament.json"
    current = facts.P1S["orca_profiles"].value["filament"]
    assert profiles.filament_for_settings_id(a, current, folder) == folder / "filament.json"


def test_profile_defaults_reference_orca_profile_names_and_sources(p1s_profiles):
    assert facts.ORCA_RELEASE == ("v2.5.0-beta", "345efec64c7b95a05ce5cf96df0dde66a158835c")
    assert all(fact.source in {"ORCA", "BAMBU", "DERIVED"} for fact in facts.P1S.values())


def test_p1s_offers_no_print_delivery_and_does_not_use_the_moonraker_live_paths(p1s_profiles):
    # LAN telemetry is opt-in via [bambu] lan_*; the fixture config does not configure it.
    assert adapter_for().connection == "bambulab_lan"
    assert adapter_for().lan_configured() is False
    assert VENDOR.delivery == ("printer", "discord", "bambu_handoff")
    assert "printer" not in VENDOR.default_delivery  # offered, but opt-in only (stage d)
    assert adapter_for().normalize_status({"print_stats": {"state": "printing"}})["state"] == "unknown"


def test_p1s_defaults_to_bambu_studio_like_every_bambu_printer():
    from print_handy import settings
    for key in ("bambulab.a1", "bambulab.p1s"):
        config = settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": key})
        assert config["printer"]["slicer"] == "bambu_studio", key


def test_p1s_slices_each_material_with_its_checked_profile_and_refuses_the_rest(p1s_profiles, tmp_path, monkeypatch):
    """#264 on the P1S: the same selection as the A1, with the P1S's own checked profile names."""
    st = state()
    st["spool"]["material"], st["temps_c"] = "PETG", {"nozzle": 240, "bed": 80}
    petg = facts.P1S["orca_filaments"].value["PETG"]
    text = p1s_gcode(material="PETG", bed=80, hotend=240).replace(
        f'filament_settings_id = "{facts.P1S["orca_profiles"].value["filament"]}"', f'filament_settings_id = "{petg}"')

    def run(command, **_):
        Path(command[command.index("--outputdir") + 1], "plate_1.gcode").write_text(text)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(orca_adapter.subprocess, "run", run)
    orca_adapter.slice_model(CUBE, tmp_path / "slice", st, adapter=adapter_for())
    assert json.loads((tmp_path / "slice" / "filament.json").read_text())["filament_settings_id"] == [petg]
    found = adapter_for().gcode_problems(p1s_gcode(material="PETG", bed=80, hotend=240), st)
    assert any("sliced with the PLA filament profile" in p for p in found), found
    st["spool"]["material"] = "ASA"
    with pytest.raises(profiles.ProfileError, match="no checked vendor filament profile for 'ASA'"):
        orca_adapter.slice_model(CUBE, tmp_path / "slice2", st, adapter=adapter_for())
