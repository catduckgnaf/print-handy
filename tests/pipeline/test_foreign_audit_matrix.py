"""Step-2 audit for #867: a PrusaSlicer project and a Bambu-derivative (Creality/Orca) project
run through every Flashforge model Print Handy ships, on both routes a foreign project can
take (whole model, and a derived single-slot plate whose package reaches Orca directly).

Per combination the battery records the four fates the audit table in docs/PRINTER-PROFILES.md
summarizes: kept (author intent), replaced (machine half, speeds, temperatures), dropped
(foreign extras and differs-from-base bookkeeping), leaked (nothing foreign may reach Orca:
every "replaced/dropped" assertion doubles as the leak check for its cell).

Offline: Orca is faked; what it would read — the job configs and the private 3MF copy — is
asserted. The real-slicer proof for the PrusaSlicer fixture is tests/slicer/test_prusa_foreign_real.py.
"""
import io
import json
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import pytest

from print_handy import printers, settings
from print_handy.core import model_job, orca_adapter, source_colors
from print_handy.core.color_geometry import export_single_color_plate
from tests.corpus.make_models import box, write_3mf
from tests.pipeline.test_multi_tool_intake import creator_config, tool_state
from tests.pipeline.test_prusa_object_settings import PRUSA_CONFIG, prusa_object_source

MODELS = ["flashforge.ad5x", "flashforge_stock.ad5x", "flashforge.ad5m", "flashforge.ad5m_pro",
          "flashforge.creator5", "flashforge.creator5_pro"]
CREATORS = {"flashforge.creator5", "flashforge.creator5_pro"}
PALETTE = [("#FFFFFF", "PLA"), ("#067749", "PLA")]


def derivative_source(path):
    """A Bambu-derivative project (Creality-flavored Orca export): foreign machine half, fast
    speeds, hot temperatures, the author's walls/infill, a designated support filament, and a
    differs-from-base list naming the author's own keys (the silent-drop bait)."""
    project = {
        "printer_model": "Creality K1 Max",
        "machine_start_gcode": "DERIVATIVE FOREIGN START\nG28\n",
        "machine_end_gcode": "DERIVATIVE FOREIGN END\n",
        "outer_wall_speed": "300", "inner_wall_speed": "350",
        "nozzle_temperature": ["240", "240"], "hot_plate_temp": ["90", "90"],
        "wall_loops": "4", "sparse_infill_density": "35%",
        "enable_support": "1", "support_filament": "2", "support_interface_filament": "2",
        "filament_settings_id": ["Foreign PLA", "Foreign PLA"],
        "inherits": "Foreign Process Preset",
        "different_settings_to_system": ["wall_loops", "sparse_infill_density"],
    }
    write_3mf(path, [("Base", 1, box(30, 30, 10), (60, 60)), ("Cap", 2, box(20, 20, 8), (110, 110))],
              [("Plate 1", [0, 1])], PALETTE, project_extra=project)
    with zipfile.ZipFile(path) as archive:
        entries = {info.filename: archive.read(info) for info in archive.infolist()}
    config = entries["Metadata/model_settings.config"].decode()
    config = config.replace('<object id="1"><metadata key="name" value="Base"/>',
                            '<object id="1"><metadata key="name" value="Base"/>'
                            '<metadata key="sparse_infill_density" value="30%"/>', 1)
    entries["Metadata/model_settings.config"] = config.encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def prusa_matrix_source(path):
    """The PrusaSlicer fixture with the full foreign load: hot temps, a fast perimeter speed,
    and a designated support extruder."""
    src = prusa_object_source(path)
    with zipfile.ZipFile(src) as archive:
        entries = {info.filename: archive.read(info) for info in archive.infolist()}
    entries["Metadata/Slic3r_PE.config"] = (PRUSA_CONFIG +
                                            "; temperature = 240\n; first_layer_temperature = 245\n"
                                            "; bed_temperature = 90\n; perimeter_speed = 400\n"
                                            "; support_material_extruder = 2\n").encode()
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return src


SOURCES = {
    "prusa": {"build": prusa_matrix_source, "marker": "PRUSA FOREIGN START",
              "printer": "Original Prusa i3 MK3S", "wall_loops": "4", "infill": "25%",
              "object_infill": "80%", "hot": ("240", "245"), "fast": {"inner_wall_speed": "400"}},
    "derivative": {"build": derivative_source, "marker": "DERIVATIVE FOREIGN START",
                   "printer": "Creality K1 Max", "wall_loops": "4", "infill": "35%",
                   "object_infill": "30%", "hot": ("240",),
                   "fast": {"outer_wall_speed": "300", "inner_wall_speed": "350"}},
}


_CONTROL = {}


def _rig(model_key, monkeypatch, tmp_path):
    cfg = settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": model_key})
    monkeypatch.setattr(settings, "current", lambda: cfg)
    adapter = printers.get(model_key, cfg)
    state = tool_state() if model_key in CREATORS else model_job.printer_state.load()
    if model_key not in _CONTROL:
        # Control: a neutral one-filament source through the same rig. The job
        # temperatures a foreign source produces must equal these exactly —
        # temperatures come from the spool and the family's tuning, so any
        # difference is the author's temperatures leaking through.
        neutral = tmp_path / "neutral.3mf"
        write_3mf(neutral, [("Block", 1, box(20, 20, 10), (60, 60))], [("Plate 1", [0])],
                  [("#FFFFFF", "PLA")])
        _CONTROL[model_key] = _slice(tmp_path, neutral, state, adapter, {}, filament_slot=1)[1]
    return adapter, state, _CONTROL[model_key]


def _capture(captured, state):
    temps = state.get("temps_c") or {}
    nozzle, bed = temps.get("nozzle", 210), temps.get("bed", 60)
    gcode = ("; HEADER_BLOCK_START\n; generated by OrcaSlicer 2.4.2\n; HEADER_BLOCK_END\n"
             "; filament_type = PLA\n; nozzle_diameter = 0.4\n"
             f"M140 S{bed}\nM104 S{nozzle}\nM190 S{bed}\nM109 S{nozzle}\nT0\nG90\n"
             ";LAYER_CHANGE\nG1 Z0.2 F600\nG1 X10 Y10 E0.1 F1000\nG1 X20 Y20 E0.2\n"
             ";TYPE:Custom\nM104 S0\nM140 S0\n")

    def run(cmd, **kwargs):
        if zipfile.is_zipfile(cmd[-1]):
            with zipfile.ZipFile(cmd[-1]) as archive:
                captured["names"] = archive.namelist()
                captured["members"] = {name: archive.read(name) for name in captured["names"]}
        folder = Path(cmd[cmd.index("--outputdir") + 1])
        (folder / "plate_1.gcode").write_text(gcode)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    return run


def _slice(tmp_path, source, state, adapter, captured, **kwargs):
    spool = io.BytesIO(json.dumps({"archived": False, "filament": {"material": "PLA"}}).encode())
    with patch.object(orca_adapter.subprocess, "run", side_effect=_capture(captured, state)), \
            patch.object(urllib.request, "urlopen", return_value=spool):
        _out, provenance, configs = orca_adapter.slice_model(
            source, tmp_path / "out", state, adapter=adapter, **kwargs)
    job = {}
    for path in configs[:3]:
        job.update(json.loads(Path(path).read_text()))
    return provenance, job


def _copy_part_settings(captured):
    root = ET.fromstring(captured["members"]["Metadata/model_settings.config"])
    out = {}
    for obj in root.findall("object"):
        object_meta = {m.get("key"): m.get("value") for m in obj.findall("metadata")}
        for part in obj.findall("part"):
            meta = {**object_meta, **{m.get("key"): m.get("value") for m in part.findall("metadata")}}
            out[meta.get("name")] = meta
    return out


def _assert_fates(captured, job, adapter, spec, control, route):
    template_machine = json.loads((adapter.templates_dir() / "machine.json").read_text())
    template_process = json.loads((adapter.templates_dir() / "process.json").read_text())
    # Replaced: the machine half is the model's own, never the source printer's.
    assert job["printer_model"] == template_machine["printer_model"]
    assert spec["printer"] not in json.dumps(job)
    assert spec["marker"] not in job["machine_start_gcode"]
    # Replaced: speeds the author raised fall back to the template's (the job may
    # cap them further for the spool's finish, never raise them back).
    for key, author_value in spec["fast"].items():
        assert job[key] != author_value
        assert float(job[key]) <= float(template_process[key])
    # Replaced: temperatures are the spool's — identical to the neutral control
    # job's, and none of the author's values appear.
    assert job["nozzle_temperature"] == control["nozzle_temperature"]
    for hot in spec["hot"]:
        assert hot not in job["nozzle_temperature"]
    # Kept: the author's walls and infill reach the process profile.
    assert job["wall_loops"] == spec["wall_loops"]
    assert job["sparse_infill_density"] == spec["infill"]
    # Leaked: nothing foreign in anything Orca reads — the private copy included.
    for name, data in captured["members"].items():
        assert spec["marker"].encode() not in data, name
        assert spec["printer"].encode() not in data, name
    if route == "derived":
        project = json.loads(captured["members"]["Metadata/project_settings.config"])
        assert project["printer_model"] == template_machine["printer_model"]
        assert project["wall_loops"] == spec["wall_loops"]  # the differs list cannot drop it
        assert project["sparse_infill_density"] == spec["infill"]
        differs = project.get("different_settings_to_system") or []
        assert not any(differs), differs
        inherits = project.get("inherits") or []
        assert (not any(inherits)) if isinstance(inherits, list) else (not inherits), inherits


@pytest.mark.parametrize("model_key", MODELS)
@pytest.mark.parametrize("source_kind", ["prusa", "derivative"])
def test_audit_whole_model_route(model_key, source_kind, tmp_path, monkeypatch):
    adapter, state, control = _rig(model_key, monkeypatch, tmp_path)
    spec = SOURCES[source_kind]
    source = spec["build"](tmp_path / "source.3mf")
    captured = {}
    _provenance, job = _slice(tmp_path, source, state, adapter, captured, filament_slot=1)
    _assert_fates(captured, job, adapter, spec, control, "whole")
    # Kept on this route too: the per-object settings ride the rebuilt 3MF export.
    parts = _copy_part_settings(captured)
    name = "Insert" if source_kind == "prusa" else "Base"
    assert parts[name]["sparse_infill_density"] == spec["object_infill"]


@pytest.mark.parametrize("model_key", MODELS)
@pytest.mark.parametrize("source_kind", ["prusa", "derivative"])
def test_audit_derived_plate_route(model_key, source_kind, tmp_path, monkeypatch):
    adapter, state, control = _rig(model_key, monkeypatch, tmp_path)
    spec = SOURCES[source_kind]
    source = spec["build"](tmp_path / "source.3mf")
    manifest = source_colors.inventory(source)
    record = model_job.resolve_filament_selection(state, dict(state["filament_slots"]["1"], slot=1))
    decisions = {"schema": "part_slots_v1", "mode": "per_part", "source_sha256": manifest["source_sha256"],
                 "choices": {row["key"]: record for row in source_colors.canonical_prompts(manifest)}}
    keys = [part["key"] for part in manifest["parts"] if part["source_extruder"] == 1]
    plate = tmp_path / "plate.3mf"
    export_single_color_plate(source, plate, decisions, keys, slot_colors=state["filament_slots"])
    captured = {}
    _provenance, job = _slice(tmp_path, plate, state, adapter, captured,
                              preserve_layout=True, filament_slot=1)
    _assert_fates(captured, job, adapter, spec, control, "derived")
    parts = _copy_part_settings(captured)
    name = "Widget" if source_kind == "prusa" else "Base"
    expected = "5" if source_kind == "prusa" else None
    if source_kind == "prusa":
        assert parts["Insert"]["sparse_infill_density"] == "80%"
        assert parts[name]["wall_loops"] == expected
    else:
        assert parts[name]["sparse_infill_density"] == "30%"


def test_silent_drop_regression_author_keys_named_in_the_differs_list_reach_orca(tmp_path, monkeypatch):
    """ForgeBridge's silent-drop lesson, as a regression: a foreign project whose
    different_settings_to_system names the author's own settings (the mechanism by
    which a clean-profile replacement used to lose them) still slices with those
    settings — in the process profile and in the copy Orca reads."""
    adapter, state, control = _rig("flashforge.ad5x", monkeypatch, tmp_path)
    source = derivative_source(tmp_path / "source.3mf")
    project = json.loads(zipfile.ZipFile(source).read("Metadata/project_settings.config"))
    assert "wall_loops" in project["different_settings_to_system"]  # the bait is really set
    captured = {}
    _provenance, job = _slice(tmp_path, source, state, adapter, captured, filament_slot=1)
    assert job["wall_loops"] == "4" and job["sparse_infill_density"] == "35%"
    manifest = source_colors.inventory(source)
    record = model_job.resolve_filament_selection(state, dict(state["filament_slots"]["1"], slot=1))
    decisions = {"schema": "part_slots_v1", "mode": "per_part", "source_sha256": manifest["source_sha256"],
                 "choices": {row["key"]: record for row in source_colors.canonical_prompts(manifest)}}
    keys = [part["key"] for part in manifest["parts"] if part["source_extruder"] == 1]
    plate = tmp_path / "plate.3mf"
    export_single_color_plate(source, plate, decisions, keys, slot_colors=state["filament_slots"])
    captured = {}
    _provenance, job = _slice(tmp_path, plate, state, adapter, captured,
                              preserve_layout=True, filament_slot=1)
    copy_project = json.loads(captured["members"]["Metadata/project_settings.config"])
    assert copy_project["wall_loops"] == "4" and copy_project["sparse_infill_density"] == "35%"
