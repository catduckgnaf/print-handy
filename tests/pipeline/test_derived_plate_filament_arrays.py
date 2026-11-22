"""A derived single-slot plate declares exactly one filament in its project settings (OrcaSlicer 2.5 checks this)."""
import json
import zipfile
from pathlib import Path

import pytest

from print_handy.core import source_colors
from print_handy.jobs import filament as jobs_filament
from print_handy.core.color_geometry import export_single_color_plate
from tests.corpus.make_models import box, write_3mf

STATE = Path(__file__).parent / "fixtures" / "printer_hardware.json"
PALETTE = [("#FFFFFF", "PLA"), ("#067749", "PLA")]


def source_with_arrays(path, colours=2):
    write_3mf(path, [("Base", 1, box(30, 30, 10), (60, 60)), ("Cap", 2, box(20, 20, 8), (110, 110))],
              [("Plate 1", [0, 1])], PALETTE[:colours] if colours > 1 else [("#FFFFFF", "PLA")])
    project = {"filament_colour": [c for c, _ in PALETTE[:colours]], "filament_type": ["PLA"] * colours,
               "filament_is_support": ["0"] * colours, "filament_settings_id": ["Generic PLA"] * colours,
               "nozzle_temperature": ["210"] * colours, "flush_volumes_matrix": ["0", "280", "280", "0"][:colours * colours]
               if colours == 2 else ["0"], "flush_volumes_vector": ["140", "140"] * colours,
               "layer_height": "0.2", "nozzle_diameter": ["0.4", "0.4"]}
    with zipfile.ZipFile(path) as archive:
        entries = {i.filename: archive.read(i) for i in archive.infolist()}
    entries["Metadata/project_settings.config"] = json.dumps(project, indent=4).encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


def export(tmp_path, extruder, slot_colors):
    source = tmp_path / "source.3mf"
    source_with_arrays(source)
    state = json.loads(STATE.read_text())
    manifest = source_colors.inventory(source)
    white = jobs_filament.resolve_filament_selection(state, dict(state["filament_slots"]["1"], slot=1))
    decisions = {"schema": "part_slots_v1", "mode": "per_part", "source_sha256": manifest["source_sha256"],
                 "choices": {row["key"]: white for row in source_colors.canonical_prompts(manifest)}}
    keys = [p["key"] for p in manifest["parts"] if p["source_extruder"] == extruder]
    plate = tmp_path / "plate.3mf"
    export_single_color_plate(source, plate, decisions, keys, slot_colors=slot_colors)
    with zipfile.ZipFile(plate) as archive:
        return json.loads(archive.read("Metadata/project_settings.config"))


@pytest.mark.parametrize("extruder", [1, 2])
@pytest.mark.parametrize("colored", [True, False])
def test_every_per_filament_array_matches_the_one_filament_the_plate_uses(tmp_path, extruder, colored):
    state = json.loads(STATE.read_text())
    project = export(tmp_path, extruder, state["filament_slots"] if colored else None)
    assert len(project["filament_colour"]) == 1
    for key in ("filament_type", "filament_is_support", "filament_settings_id", "nozzle_temperature"):
        assert len(project[key]) == 1, key
    assert project["flush_volumes_matrix"] == ["0"]
    assert len(project["flush_volumes_vector"]) == 2
    # Machine-level arrays and scalars are not per-filament and stay as authored.
    assert project["nozzle_diameter"] == ["0.4", "0.4"]
    assert project["layer_height"] == "0.2"
    if colored:
        assert project["filament_colour"] == ["#FFFFFF"]
