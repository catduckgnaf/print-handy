"""Canonical source descriptor and Cura translation (#1525, tranche B).

Classification comes from file content only. Archive-safety cases are
negative tests: oversized, duplicated, traversal and bomb-shaped entries
are refused by name, and no test here labels a synthetic fixture genuine.
"""
import io
import zipfile
from pathlib import Path

import pytest

from print_handy.core import author_settings, source_descriptor as sd

FIXTURES = Path(__file__).resolve().parents[1] / "corpus" / "producer-fixtures"
NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
MODEL = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="{NS}">
 <resources><object id="1" type="model"><mesh>
  <vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/></vertices>
  <triangles><triangle v1="0" v2="1" v3="2"/></triangles>
 </mesh></object></resources>
 <build><item objectid="1"/></build>
</model>"""


def _write(tmp_path, name, members):
    target = tmp_path / name
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for member, data in members.items():
            archive.writestr(member, data)
    return target


def _base_members():
    return {"3D/3dmodel.model": MODEL}


def test_cura_project_is_classified_editable(tmp_path):
    target = _write(tmp_path, "cura.3mf", {
        **_base_members(),
        "Cura/global.cfg": "[values]\nlayer_height = 0.2\n"})
    descriptor = sd.describe(target)
    assert descriptor["kind"] == "cura"
    assert descriptor["user_class"] == "editable-project"
    assert descriptor["has_geometry"] is True
    assert "editable project" in sd.user_facing_summary(descriptor)


def test_core_geometry_is_geometry_only(tmp_path):
    descriptor = sd.describe(_write(tmp_path, "core.3mf", _base_members()))
    assert descriptor["kind"] == "generic-core"
    assert descriptor["user_class"] == "geometry-only"


def test_sliced_only_package_is_sliced_only(tmp_path):
    target = _write(tmp_path, "plate.gcode.3mf", {
        "Metadata/plate_1.gcode": "G1 X1 Y1\n"})
    descriptor = sd.describe(target)
    assert descriptor["user_class"] == "sliced-only"
    assert descriptor["has_geometry"] is False


def test_bambu_and_orca_kinds_come_from_application_metadata(tmp_path):
    model = MODEL.replace("<model ", '<model data-x="1" ').replace(
        "<resources>", '<metadata name="Application">OrcaSlicer-2.5.0</metadata><resources>')
    target = _write(tmp_path, "orca.3mf", {
        **_base_members(), "3D/3dmodel.model": model,
        "Metadata/project_settings.config": "{}"})
    descriptor = sd.describe(target)
    assert descriptor["kind"] == "orca"
    assert descriptor["source_version"] == "2.5.0"


def test_orca_metadata_outranks_the_bambu_application_name(tmp_path):
    # A genuine OrcaSlicer save (orca-genuine.3mf) names its Bambu base
    # in Application and itself in an OrcaSlicer metadata element, and
    # keeps its mesh in a 3D/Objects component file. Both shapes are
    # pinned here synthetically; the genuine fixture is pinned in
    # test_cross_slicer_contract_1525.py.
    component = MODEL.replace('id="1"', 'id="9"')
    root_only = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="{NS}">
 <metadata name="Application">BambuStudio-02.08.01.55</metadata>
 <metadata name="OrcaSlicer">2.5.1</metadata>
 <resources><object id="1" type="model"><components>
  <component objectid="9"/>
 </components></object></resources>
 <build><item objectid="1"/></build>
</model>"""
    target = _write(tmp_path, "orca.3mf", {
        "3D/3dmodel.model": root_only,
        "3D/Objects/cube_1.model": component,
        "Metadata/project_settings.config": "{}"})
    descriptor = sd.describe(target)
    assert descriptor["kind"] == "orca"
    assert descriptor["application"] == "OrcaSlicer-2.5.1"
    assert descriptor["source_version"] == "2.5.1"
    assert descriptor["has_geometry"] is True
    assert descriptor["user_class"] == "editable-project"
    record = author_settings.read_project(target)
    assert record["source_slicer"] == "OrcaSlicer"
    assert record["source_family"] == "orca"
    assert record["source_version"] == "2.5.1"


def test_anycubic_marker_is_detection_only(tmp_path):
    target = _write(tmp_path, "ac.3mf", {
        **_base_members(),
        "Metadata/project_settings.config": "{}",
        "Metadata/slice_info.config":
            "<config><header><header_item key=\"X-ACNext-Version\" value=\"1.0\"/>"
            "</header></config>"})
    assert sd.describe(target)["kind"] == "anycubic"


def test_corrupt_and_empty_archives_are_unsupported(tmp_path):
    bad = tmp_path / "bad.3mf"
    bad.write_bytes(b"not a zip at all")
    assert sd.describe(bad)["user_class"] == "unsupported-corrupt"
    empty = _write(tmp_path, "empty.3mf", {"readme.txt": "hi"})
    descriptor = sd.describe(empty)
    assert descriptor["user_class"] == "unsupported-corrupt"
    assert descriptor["problems"]


def test_archive_safety_refusals(tmp_path):
    dup = tmp_path / "dup.3mf"
    with zipfile.ZipFile(dup, "w") as archive:
        archive.writestr("3D/3dmodel.model", MODEL)
        archive.writestr("3D/3dmodel.model", MODEL)
    assert "duplicate" in " ".join(sd.describe(dup)["problems"])

    traversal = _write(tmp_path, "trav.3mf", {
        **_base_members(), "../evil.txt": "x"})
    assert "unsafe" in " ".join(sd.describe(traversal)["problems"])

    bomb = _write(tmp_path, "bomb.3mf", {
        **_base_members(), "big.bin": b"0" * (2 * 1024 * 1024)})
    assert "compressed more" in " ".join(sd.describe(bomb)["problems"])


def test_source_file_is_never_mutated(tmp_path):
    target = _write(tmp_path, "cura.3mf", {
        **_base_members(), "Cura/global.cfg": "[values]\nlayer_height = 0.2\n"})
    before = target.read_bytes()
    sd.describe(target)
    author_settings.read_project(target)
    assert target.read_bytes() == before


def test_cura_settings_translate_the_validated_subset():
    raw = {"layer_height": "0.16", "initial_layer_height": "0.24",
           "wall_line_count": "3", "top_layers": "5", "bottom_layers": "4",
           "infill_sparse_density": "25", "infill_pattern": "gyroid",
           "support_enable": "True", "support_structure": "tree",
           "support_placement": "everywhere", "adhesion_type": "brim",
           "brim_width": "6", "z_seam_type": "sharpest_corner"}
    translated, notes = sd.translate_cura_settings(raw)
    assert translated == {
        "layer_height": "0.16", "initial_layer_print_height": "0.24",
        "wall_loops": "3", "top_shell_layers": "5", "bottom_shell_layers": "4",
        "sparse_infill_density": "25%", "sparse_infill_pattern": "gyroid",
        "enable_support": "1", "support_type": "tree(auto)",
        "support_on_build_plate_only": "0", "brim_type": "outer_only",
        "brim_width": "6", "seam_position": "nearest"}
    assert any("carried" in note for note in notes)


def test_cura_out_of_range_and_unmapped_values_are_refused_with_notes():
    translated, notes = sd.translate_cura_settings(
        {"layer_height": "9", "infill_pattern": "cross3d",
         "z_seam_type": "user_specified", "wall_line_count": "abc"})
    assert translated == {}
    assert len(notes) == 4


def test_cura_forbidden_keys_never_translate():
    raw = {"machine_start_gcode": "G28 ; FOREIGN", "machine_end_gcode": "M104 S0",
           "material_print_temperature": "215", "machine_name": "Other",
           "machine_width": "220", "acceleration_print": "500",
           "layer_height": "0.2"}
    translated, _notes = sd.translate_cura_settings(raw)
    assert translated == {"layer_height": "0.2"}
    for key in sd.FORBIDDEN_SOURCE_KEYS:
        assert key not in translated


def test_cura_fixture_flows_through_the_production_reader():
    fixture = FIXTURES / "cura-spec-synthetic.3mf"
    assert fixture.is_file(), "run scripts/generate_producer_fixtures.py"
    record = author_settings.read_project(fixture)
    assert record["format"] == "cura_project"
    assert record["source_family"] == "cura"
    assert record["author"]["layer_height"] == "0.16"
    assert record["author"]["sparse_infill_pattern"] == "gyroid"
    blob = str(record["author"])
    assert "SPEC SYNTHETIC" not in blob and "machine_start_gcode" not in blob
    descriptor = sd.describe(fixture)
    assert descriptor["kind"] == "cura"
    assert descriptor["user_class"] == "editable-project"


def test_provenance_manifest_labels_fixture_classes_honestly():
    import json
    provenance = json.loads((FIXTURES / "PROVENANCE.json").read_text())
    rows = {row["file"]: row for row in provenance["fixtures"]}
    assert rows["cura-spec-synthetic.3mf"]["class"] == "spec-synthetic"
    for row in rows.values():
        assert row["class"] in ("genuine", "spec-synthetic")
        assert row["sha256"]


def test_genuine_prusa_fixture_is_a_real_prusa_export():
    import json
    provenance = json.loads((FIXTURES / "PROVENANCE.json").read_text())
    rows = {row["file"]: row for row in provenance["fixtures"]}
    genuine = rows.get("prusa-genuine.3mf")
    if genuine is None:
        return  # the extended tier has not produced one yet; nothing to check
    assert genuine["class"] == "genuine"
    assert "PrusaSlicer-2.8.1" in genuine["producer"]
    descriptor = sd.describe(FIXTURES / "prusa-genuine.3mf")
    # The real CLI export names PrusaSlicer in its Application metadata
    # and carries a model config, but no settings snapshot: producer
    # proven, class honestly geometry-only.
    assert descriptor["kind"] == "prusa"
    assert descriptor["source_version"] == "2.8.1"
    assert descriptor["user_class"] == "geometry-only"
