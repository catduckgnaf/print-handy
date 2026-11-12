"""Operator filament presets (#264 phase 2): an exported Bambu Studio / OrcaSlicer preset registered over a
checked vendor parent, replacing that material's vendor profile until removed.

Offline only, like the A1 tests: templates come from the invented fixture tree (a1_fixture), presets are
invented JSON written by the tests, and slicing runs against a fake Orca. Nothing here is OrcaSlicer or
Bambu Lab material.
"""
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

from print_handy.core import prepared_intake
from print_handy.printers.bambulab import profiles

from . import a1_fixture
from .test_bambulab_a1 import a1_gcode, adapter_for, config_for, fake_slice, petg_state, problems, state

ROOT = Path(__file__).resolve().parents[2]
A1 = "bambulab.a1"
RESOURCES = a1_fixture.RESOURCES

# An invented exported user preset: a checked PETG parent plus the operator's own settings.
PETG_PRESET = {
    "type": "filament", "name": "PETG fast", "inherits": "Generic PETG @BBL A1", "from": "User",
    "instantiation": "true", "filament_flow_ratio": ["0.95"], "nozzle_temperature": ["250"],
    "nozzle_temperature_initial_layer": ["250"], "filament_retraction_length": ["0.6"],
    "fan_min_speed": ["20"],
}


@pytest.fixture(autouse=True)
def a1_profiles(monkeypatch):
    """Every test sees A1 templates generated from the fixture tree, which is trusted for the test only.

    The operator folder is removed around each test: installs preserve it by design, and the folder is
    shared by the whole test process, so a preset registered here must not leak into another test.
    """
    a1_fixture.trust(monkeypatch.setattr)
    folder = a1_fixture.install(adapter_for(A1))
    shutil.rmtree(folder / profiles.OPERATOR, ignore_errors=True)
    yield
    shutil.rmtree(folder / profiles.OPERATOR, ignore_errors=True)


def preset_file(tmp_path, data=PETG_PRESET, name="preset.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path


def import_petg(tmp_path, data=PETG_PRESET, **kw):
    return profiles.import_filament(adapter_for(A1), RESOURCES, preset_file(tmp_path, data), **kw)


def operator_dir():
    return adapter_for(A1).pinned_templates_folder() / profiles.OPERATOR


# ------------------------------------------------------------------ import and registration

def test_import_registers_the_preset_and_records_its_hash(tmp_path):
    entry = import_petg(tmp_path)
    assert entry["material"] == "PETG" and entry["parent"] == "Generic PETG @BBL A1"
    base = operator_dir()
    assert json.loads((base / "manifest.json").read_text()) == {"presets": {"PETG": {
        "name": "PETG fast", "parent": "Generic PETG @BBL A1", "sha256": entry["sha256"]}}}
    flat = json.loads((base / "upstream" / "PETG.json").read_text())
    assert profiles.sha256(flat) == entry["sha256"]
    # The preset's own settings survive flattening; the parent's material and G-code are untouched.
    assert flat["name"] == "PETG fast" and flat["filament_flow_ratio"] == ["0.95"]
    assert flat["nozzle_temperature"] == ["250"] and flat["filament_retraction_length"] == ["0.6"]
    parent = profiles.flatten_filaments(RESOURCES, ["Generic PETG @BBL A1"])["Generic PETG @BBL A1"]
    assert flat["filament_type"] == parent["filament_type"]
    assert flat["filament_start_gcode"] == parent["filament_start_gcode"]
    assert flat["filament_end_gcode"] == parent["filament_end_gcode"]
    assert (base / "PETG.json").read_text() == profiles.template_text("filament", flat)


def test_an_imported_preset_replaces_the_vendor_profile_for_its_material(tmp_path):
    a = adapter_for(A1)
    assert profiles.filament_template(a, "PETG") == a.pinned_templates_folder() / "filaments" / "PETG.json"
    import_petg(tmp_path)
    assert profiles.filament_template(a, "PETG") == operator_dir() / "PETG.json"
    assert profiles.filament_template(a, "PLA") == a.pinned_templates_folder() / "filament.json"
    assert profiles.filament_for_settings_id(a, "PETG fast") == operator_dir() / "PETG.json"
    assert profiles.filament_for_settings_id(a, "Generic PETG @BBL A1") == \
        a.pinned_templates_folder() / "filaments" / "PETG.json"


def test_an_operator_preset_for_the_default_material(tmp_path):
    a = adapter_for(A1)
    preset = {"type": "filament", "name": "PLA house", "inherits": "Generic PLA @BBL A1", "from": "User",
              "filament_flow_ratio": ["0.99"]}
    entry = profiles.import_filament(a, RESOURCES, preset_file(tmp_path, preset))
    assert entry["material"] == "PLA" and profiles.filament_template(a, "PLA") == operator_dir() / "PLA.json"
    text = a1_gcode(filament="PLA house", blocks="Generic PLA @BBL A1")
    assert problems(text) == [] and problems(text, state()) == []


def test_a_petg_slice_uses_the_operator_preset_and_passes_the_guard(tmp_path, monkeypatch):
    import_petg(tmp_path)
    text = a1_gcode(material="PETG", filament="PETG fast", blocks="Generic PETG @BBL A1", bed=80, hotend=240)
    assert problems(text) == [] and problems(text, petg_state()) == []
    _, provenance, folder, _ = fake_slice(tmp_path, monkeypatch, text, st=petg_state())
    job = json.loads((folder / "filament.json").read_text())
    assert job["name"] == "PETG fast" and job["filament_settings_id"] == ["PETG fast"]
    # The preset supplies the base flow (0.95); the record's calibration multiplier (0.95) applies on top.
    assert job["filament_type"] == ["PETG"] and job["filament_flow_ratio"] == ["0.9025"]
    # The hardware record still sets the temperatures at slice time, as with a vendor profile.
    assert job["nozzle_temperature"] == ["240"] and job["textured_plate_temp"] == ["80"]
    assert provenance["slice_guard"]["status"] in ("ok", "warnings")


def test_reimport_replaces_the_registered_preset(tmp_path):
    import_petg(tmp_path)
    changed = {**PETG_PRESET, "filament_flow_ratio": ["0.93"]}
    entry = import_petg(tmp_path, changed)
    flat = json.loads((operator_dir() / "upstream" / "PETG.json").read_text())
    assert flat["filament_flow_ratio"] == ["0.93"] and profiles.sha256(flat) == entry["sha256"]


# ------------------------------------------------------------------ import refusals

@pytest.mark.parametrize("mutate, message", [
    ({"inherits": "Bambu PETG HF @BBL A1"}, "is not a checked Bambu Lab A1 filament profile"),
    ({"inherits": "fixture_filament_common"}, "is not a checked Bambu Lab A1 filament profile"),
    ({"inherits": None}, "is not a checked Bambu Lab A1 filament profile"),
    ({"name": ""}, "names no preset"),
    ({"type": "process"}, "not a filament preset"),
    ({"layer_height": ["0.2"]}, "not filament settings of 'Generic PETG @BBL A1'"),
    ({"printer_model": ["Bambu Lab A1"]}, "not filament settings of 'Generic PETG @BBL A1'"),
    ({"filament_type": ["ABS"]}, "filament_type 'ABS' is not its parent's 'PETG'"),
    ({"filament_type": ["ABS"]}, "ABS is not recommended by Bambu Lab for the Bambu Lab A1"),
    ({"nozzle_temperature": ["320"]}, "outside 0..300 C"),
    ({"nozzle_temperature_initial_layer": ["-5"]}, "outside 0..300 C"),
    ({"textured_plate_temp": ["120"]}, "outside 0..100 C"),
    ({"fan_max_speed": ["150"]}, "outside 0..100%"),
    ({"filament_max_volumetric_speed": ["0"]}, "filament_max_volumetric_speed must be positive"),
    ({"filament_retraction_length": ["0"]}, "filament_retraction_length must be positive"),
    ({"filament_retraction_length": ["-0.5"]}, "filament_retraction_length must be positive"),
    ({"nozzle_temperature": ["hot"]}, "nozzle_temperature is not numeric"),
    ({"filament_start_gcode": ["M104 S1\n"]}, "changes the filament start or end G-code"),
    ({"filament_end_gcode": ["M104 S0\n"]}, "changes the filament start or end G-code"),
    ({"name": "Generic PETG @BBL A1"}, "is a checked vendor profile name"),
])
def test_import_refusals(tmp_path, mutate, message):
    data = {**PETG_PRESET, **mutate}
    if mutate.get("inherits") is None and "inherits" in mutate:
        data.pop("inherits")
    with pytest.raises(profiles.ProfileError, match=re.escape(message)):
        import_petg(tmp_path, data)
    # Nothing was written: no manifest entry, no preset files.
    assert profiles.operator_presets(adapter_for(A1)) == {}
    assert not (operator_dir() / "upstream" / "PETG.json").exists()


def test_import_accepts_the_unset_retraction_sentinel(tmp_path):
    # P2-130: Orca writes a filament preset that leaves retraction at the
    # printer default as ["nil"] — the unset sentinel, not a malformed
    # number. The import must accept it (there is no operator-supplied
    # retraction value to bound-check) instead of refusing the whole preset.
    entry = import_petg(tmp_path, {**PETG_PRESET, "filament_retraction_length": ["nil"]})
    assert entry["material"] == "PETG"
    flat = json.loads((operator_dir() / "upstream" / "PETG.json").read_text())
    assert flat["filament_retraction_length"] == ["nil"]  # preserved, not rewritten


def test_import_with_a_material_that_does_not_match_the_parent(tmp_path):
    with pytest.raises(profiles.ProfileError, match=re.escape(
            "--material PLA does not match the parent profile 'Generic PETG @BBL A1', which is a PETG profile")):
        import_petg(tmp_path, material="PLA")
    assert import_petg(tmp_path, material="PETG")["material"] == "PETG"


def test_import_refuses_a_second_material_under_the_same_name(tmp_path):
    import_petg(tmp_path)
    pla = {"type": "filament", "name": "PETG fast", "inherits": "Generic PLA @BBL A1", "from": "User"}
    with pytest.raises(profiles.ProfileError, match="already registered for PETG"):
        profiles.import_filament(adapter_for(A1), RESOURCES, preset_file(tmp_path, pla, "pla.json"))


# ------------------------------------------------------------------ trust at use: edited or stale presets

def test_an_edited_operator_template_is_refused(tmp_path):
    import_petg(tmp_path)
    path = operator_dir() / "PETG.json"
    data = json.loads(path.read_text())
    data["filament_flow_ratio"] = ["0.5"]
    path.write_text(json.dumps(data))
    with pytest.raises(profiles.ProfileError, match=re.escape("is not the imported operator preset template")):
        profiles.filament_template(adapter_for(A1), "PETG")
    text = a1_gcode(material="PETG", filament="PETG fast", blocks="Generic PETG @BBL A1", bed=80, hotend=240)
    assert any("cannot be checked" in p for p in problems(text)), problems(text)


def test_an_edited_flattened_preset_is_refused(tmp_path):
    import_petg(tmp_path)
    path = operator_dir() / "upstream" / "PETG.json"
    data = json.loads(path.read_text())
    data["filament_flow_ratio"] = ["0.5"]
    path.write_text(json.dumps(data))
    with pytest.raises(profiles.ProfileError, match=re.escape("recorded at import (edited?)")):
        profiles.filament_template(adapter_for(A1), "PETG")


def test_a_manifest_that_lies_about_the_hash_is_refused(tmp_path):
    import_petg(tmp_path)
    path = operator_dir() / "manifest.json"
    data = json.loads(path.read_text())
    data["presets"]["PETG"]["sha256"] = "0" * 64
    path.write_text(json.dumps(data))
    with pytest.raises(profiles.ProfileError, match=re.escape("recorded at import (edited?)")):
        profiles.filament_template(adapter_for(A1), "PETG")


def test_a_missing_operator_file_is_refused(tmp_path):
    import_petg(tmp_path)
    (operator_dir() / "PETG.json").unlink()
    with pytest.raises(profiles.ProfileError, match=re.escape("its files are missing")):
        profiles.filament_template(adapter_for(A1), "PETG")


def test_a_corrupt_manifest_refuses_instead_of_guessing(tmp_path):
    import_petg(tmp_path)
    (operator_dir() / "manifest.json").write_text("{not json")
    with pytest.raises(profiles.ProfileError, match="cannot be verified"):
        profiles.filament_template(adapter_for(A1), "PLA")


def test_regenerating_templates_keeps_operator_presets(tmp_path):
    import_petg(tmp_path)
    a = adapter_for(A1)
    profiles.install(a, RESOURCES)
    assert profiles.filament_template(a, "PETG") == operator_dir() / "PETG.json"
    text = a1_gcode(material="PETG", filament="PETG fast", blocks="Generic PETG @BBL A1", bed=80, hotend=240)
    assert problems(text, petg_state()) == []


# ------------------------------------------------------------------ removal

def test_remove_filament_restores_the_vendor_profile(tmp_path):
    import_petg(tmp_path)
    a = adapter_for(A1)
    entry = profiles.remove_filament(a, "PETG")
    assert entry["name"] == "PETG fast" and entry["material"] == "PETG"
    assert profiles.operator_presets(a) == {}
    assert profiles.filament_template(a, "PETG") == a.pinned_templates_folder() / "filaments" / "PETG.json"
    text = a1_gcode(material="PETG", filament="PETG fast", blocks="Generic PETG @BBL A1", bed=80, hotend=240)
    assert any("is not a checked Bambu Lab A1 vendor filament profile" in p for p in problems(text)), problems(text)
    with pytest.raises(profiles.ProfileError, match="no operator filament preset is registered for PETG"):
        profiles.remove_filament(a, "petg")


# ------------------------------------------------------------------ the bambu-templates.py commands

def run_cli(monkeypatch, *args):
    spec = importlib.util.spec_from_file_location("bambu_templates_cli", ROOT / "scripts" / "bambu-templates.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", ["bambu-templates.py", "--printer", "a1", *args])
    monkeypatch.setenv("PRINT_HANDY__PATHS__STATE_HOME", str(a1_fixture.HERMES))
    return module.main()


def test_cli_import_check_and_remove(tmp_path, monkeypatch, capsys):
    assert run_cli(monkeypatch, "--resources", str(RESOURCES), "--import-filament",
                   str(preset_file(tmp_path))) == 0
    assert "registered operator filament preset 'PETG fast' for PETG" in capsys.readouterr().out
    assert run_cli(monkeypatch, "--resources", str(RESOURCES), "--check") == 0
    assert "operator presets: PETG (PETG fast)" in capsys.readouterr().out
    assert run_cli(monkeypatch, "--remove-filament", "PETG") == 0
    assert "removed operator filament preset 'PETG fast' for PETG" in capsys.readouterr().out
    assert profiles.operator_presets(adapter_for(A1)) == {}


def test_cli_import_refusal_exits_1_and_writes_nothing(tmp_path, monkeypatch, capsys):
    bad = preset_file(tmp_path, {**PETG_PRESET, "nozzle_temperature": ["320"]})
    assert run_cli(monkeypatch, "--resources", str(RESOURCES), "--import-filament", str(bad)) == 1
    err = capsys.readouterr().err
    assert "REFUSED:" in err and "outside 0..300 C" in err
    assert profiles.operator_presets(adapter_for(A1)) == {}


def test_cli_check_with_import_is_refused_and_writes_nothing(tmp_path, monkeypatch, capsys):
    """P2-131: --check promises "writes nothing", but combined with
    --import-filament the import branch ran first and registered the
    preset before the check was ever reached."""
    assert run_cli(monkeypatch, "--resources", str(RESOURCES), "--check",
                   "--import-filament", str(preset_file(tmp_path))) == 1
    err = capsys.readouterr().err
    assert "REFUSED:" in err and "--check" in err and "--import-filament" in err
    assert profiles.operator_presets(adapter_for(A1)) == {}


# ------------------------------------------------------------------ summaries name the filament profile

def black_petg_state():
    st = petg_state()
    st["spool"].update(name="Black", color="Black")
    return st


def test_filament_summary_names_the_vendor_profile_then_the_preset(tmp_path):
    a = adapter_for(A1)
    assert a.filament_summary(black_petg_state()) == "Black PETG (Generic PETG @BBL A1)"
    import_petg(tmp_path)
    assert a.filament_summary(black_petg_state()) == "Black PETG, your preset 'PETG fast'"
    assert a.filament_summary(state()) == "white PLA (Generic PLA @BBL A1)"
    st = black_petg_state()
    st["spool"]["material"] = "ABS"
    assert a.filament_summary(st) is None  # no checked profile serves ABS; the summary invents nothing
    assert a.filament_summary({}) is None and a.filament_summary(None) is None


def test_preview_sentence_names_the_filament_when_given_a_note():
    job, row = {"title": "Ghost Stand", "summary": "A sturdy stand.", "site": "makerworld"}, {"title": "ghost"}
    plain = prepared_intake.preview_sentence(job, row)
    assert "Filament:" not in plain
    noted = prepared_intake.preview_sentence(job, row, "Black PETG, your preset 'PETG fast'")
    assert noted == ("That looks like Ghost Stand, A sturdy stand. I will use the selected profile: ghost. "
                     "Filament: Black PETG, your preset 'PETG fast'. The model page gives no time or "
                     "filament estimate; I will report both after slicing.")


def test_filament_note_comes_from_the_job_printer():
    job = {"printer_model": A1, "title": "T"}
    assert prepared_intake._filament_note(job, black_petg_state()) == "Black PETG (Generic PETG @BBL A1)"
    assert prepared_intake._filament_note(job, {}) is None
