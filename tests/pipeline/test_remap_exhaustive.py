"""Phase C of the #1110 completion program: exhaustive, atomic filament remapping.

Criterion 4: update every reference (paint, parts, feature/support assignments,
height ranges, layer changes, purge matrices and vectors), preserve geometry and
paint regions, leave the original immutable, fail without partial output, and
never silently drop a color. These tests pin the Phase C behavior:

- PrusaSlicer mmu_segmentation paint is decoded and translated to paint_color
  for the Orca destination (it was previously carried verbatim and printed in
  the base slot).
- The generic package remapper (package_remap.remap_package) permutes the
  palette, the per-filament arrays, the flush matrix and the support
  designations, remaps object/part extruders, height ranges (values and, when
  asked, object ids), layer tool changes and slice usage, preserves unrelated
  members byte-for-byte, stages its write, and refuses by name anything it
  cannot remap — including a flush_volumes_vector, whose layout is not
  established in this repository.
- The single-color collapse keeps the used source filament's own per-filament
  entries, and no longer carries stale filament references forward.
"""
import json
import re
import zipfile
from pathlib import Path

import pytest

from print_handy.core import layer_gcode, model_job, package_remap, painted_geometry, source_colors
from print_handy.core.color_geometry import (_declare_one_filament, export_color_geometry,
                                             export_production_plate, export_single_color_plate)
from tests.corpus.make_models import box, write_3mf

STATE = Path(__file__).parent / "fixtures" / "printer_hardware.json"
PRUSA_CASE = Path(__file__).parents[1] / "corpus" / "cases" / "prusa-mmu-project" / "model.3mf"
PALETTE = [("#E74C3C", "PLA"), ("#2980B9", "PLA")]


def _slot_choices(manifest, picks):
    """part_slots_v1 decisions: picks maps source extruder -> physical slot."""
    state = json.loads(STATE.read_text())
    choices = {}
    for row in source_colors.canonical_prompts(manifest):
        match = re.fullmatch(r"filament:(\d+)", row["key"])
        if not match:
            continue
        slot = picks[int(match.group(1))]
        choices[row["key"]] = model_job.resolve_filament_selection(
            state, dict(state["filament_slots"][str(slot)], slot=slot))
    return {"schema": "part_slots_v1", "mode": "per_part",
            "source_sha256": manifest["source_sha256"], "choices": choices}


# --- C2: Prusa mmu_segmentation is decoded and remapped -----------------------

def test_mmu_segmentation_states_are_decoded_per_part():
    manifest = source_colors.inventory(PRUSA_CASE)
    part = manifest["parts"][0]
    states = painted_geometry.leaf_states(PRUSA_CASE, part)
    # The corpus fixture paints the upper half of the box's 12 triangles with
    # state 2 ("8"); the lower half is unpainted and prints the base extruder.
    assert states["states"] == {2: 6}
    assert states["unpainted"] == 6


def test_mmu_segmentation_export_translates_paint_to_the_chosen_tools(tmp_path):
    manifest = source_colors.inventory(PRUSA_CASE)
    decisions = _slot_choices(manifest, {1: 2, 2: 4})
    target = tmp_path / "out.3mf"
    mapping, report = export_color_geometry(PRUSA_CASE, target, decisions)
    # Both palette rows govern the export; filament 2's choice is not dropped.
    assert mapping == {"0": 2, "1": 4}
    with zipfile.ZipFile(target) as archive:
        model = archive.read("3D/3dmodel.model")
        settings = archive.read("Metadata/model_settings.config").decode()
    assert b"mmu_segmentation" not in model
    # State 2 (the blue region) is now paint_color for tool 2 (slot 4's tool).
    assert b'paint_color="8"' in model
    assert 'key="extruder" value="1"' in settings  # the part's base tool (slot 2)


def test_mmu_segmentation_export_refuses_a_paint_state_with_no_choice(tmp_path):
    manifest = source_colors.inventory(PRUSA_CASE)
    decisions = _slot_choices(manifest, {1: 2, 2: 4})
    decisions["choices"].pop("filament:2")
    target = tmp_path / "out.3mf"
    target.write_bytes(b"existing destination sentinel")
    with pytest.raises(RuntimeError, match="incomplete|no slot choice"):
        export_color_geometry(PRUSA_CASE, target, decisions)
    assert target.read_bytes() == b"existing destination sentinel"


# --- C1/C3/C4/C5/C6/C10: the generic atomic package remapper ------------------

_MODEL = (
    '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
    'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06"><resources>'
    '<object id="5" name="Body"><mesh><vertices>'
    '<vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="0"/>'
    '</vertices><triangles>'
    '<triangle v1="0" v2="1" v3="2" paint_color="4"/>'
    '<triangle v1="0" v2="2" v3="1" slic3rpe:mmu_segmentation="8"/>'
    '</triangles></mesh></object></resources>'
    '<build><item objectid="5"/></build></model>')

_PROJECT = {"filament_colour": ["#111111", "#222222"], "filament_type": ["PLA", "PETG"],
            "nozzle_temperature": ["200", "220"], "filament_is_support": ["0", "1"],
            "flush_volumes_matrix": ["0", "100", "200", "0"],
            "support_filament": "2", "support_interface_filament": "0",
            "layer_height": "0.2"}

_RANGES = ('<objects><object id="5"><range min_z="0" max_z="5">'
           '<option opt_key="extruder">2</option>'
           '<option opt_key="layer_height">0.12</option></range></object></objects>')

_LAYER = ('<custom_gcode_per_layer><plate><layer type="0" gcode="ColorChange" '
          'extruder="2" top_z="4.2" color="#222222"/></plate></custom_gcode_per_layer>')

_SLICE_INFO = ('<config><plate><metadata key="index" value="1"/>'
               '<filament id="2" used_g="3.0" used_m="1.0"/></plate></config>')


def _generic_source(path, extra=None, project=None):
    members = {
        "[Content_Types].xml": b"<Types/>",
        "_rels/.rels": b"<Relationships/>",
        "3D/3dmodel.model": _MODEL.encode(),
        "Metadata/model_settings.config": (
            '<config><object id="5"><metadata key="name" value="Body"/>'
            '<metadata key="extruder" value="1"/>'
            '<part id="5" subtype="normal_part"><metadata key="name" value="Body"/>'
            '<metadata key="extruder" value="2"/></part></object>'
            '<plate><metadata key="plater_id" value="1"/>'
            '<model_instance><metadata key="object_id" value="5"/>'
            '<metadata key="instance_id" value="0"/></model_instance></plate></config>').encode(),
        "Metadata/project_settings.config": json.dumps(project or _PROJECT, indent=2).encode(),
        "Metadata/layer_config_ranges.xml": _RANGES.encode(),
        "Metadata/custom_gcode_per_layer.xml": _LAYER.encode(),
        "Metadata/slice_info.config": _SLICE_INFO.encode(),
        "Metadata/opaque_vendor_record.bin": b"preserve this payload exactly\x00\xff",
        "Auxiliaries/.thumbnails/thumbnail_3mf.png": b"not really png, but opaque bytes",
    }
    members.update(extra or {})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def test_generic_remap_permutates_every_reference_and_preserves_the_rest(tmp_path):
    source = _generic_source(tmp_path / "source.3mf")
    target = tmp_path / "out.3mf"
    original = source.read_bytes()
    report = package_remap.remap_package(source, target, {1: 2, 2: 1}, object_map={5: 9})
    assert source.read_bytes() == original
    with zipfile.ZipFile(target) as archive:
        project = json.loads(archive.read("Metadata/project_settings.config"))
        settings = archive.read("Metadata/model_settings.config").decode()
        ranges = archive.read("Metadata/layer_config_ranges.xml").decode()
        model = archive.read("3D/3dmodel.model")
        info = archive.read("Metadata/slice_info.config").decode()
        layer = layer_gcode.parse(archive.read("Metadata/custom_gcode_per_layer.xml"))
        assert archive.read("Metadata/opaque_vendor_record.bin") == b"preserve this payload exactly\x00\xff"
        assert archive.read("Auxiliaries/.thumbnails/thumbnail_3mf.png") == b"not really png, but opaque bytes"
    # Palette and every per-filament array follow the swap.
    assert project["filament_colour"] == ["#222222", "#111111"]
    assert project["filament_type"] == ["PETG", "PLA"]
    assert project["nozzle_temperature"] == ["220", "200"]
    assert project["filament_is_support"] == ["1", "0"]
    # The flush matrix is permuted on both axes: from-1-to-2 was 100, from-2-to-1
    # was 200; after the swap those entries trade places.
    assert project["flush_volumes_matrix"] == ["0", "200", "100", "0"]
    # Support designations are remapped; 0 (the object's own) stays 0.
    assert project["support_filament"] == "1"
    assert project["support_interface_filament"] == "0"
    assert project["layer_height"] == "0.2"
    # Object and part extruders are remapped; the range's extruder value and its
    # object id are both translated; the layer change names the mapped tool and
    # keeps its color attribute (Orca's 3MF loader requires the attribute; the
    # tool selection comes from the extruder, not the color).
    assert '<metadata key="extruder" value="2"/>' in settings  # object 1 -> 2
    assert '<metadata key="extruder" value="1"/>' in settings  # part 2 -> 1
    assert '<object id="9">' in ranges and '<option opt_key="extruder">1</option>' in ranges
    assert '<option opt_key="layer_height">0.12</option>' in ranges
    assert layer[0]["source_extruder"] == 1 and layer[0]["color"] == "#222222"
    assert '<filament id="1"' in info
    # Paint: paint_color state 1 -> state 2 ("8"); mmu state 2 is translated to
    # paint_color state 1 ("4") and the Prusa attribute is gone.
    assert b"mmu_segmentation" not in model
    assert b'paint_color="8"' in model and b'paint_color="4"' in model
    assert report["mapping"] == {"1": 2, "2": 1}


def test_generic_remap_identity_preserves_object_ids_and_members(tmp_path):
    source = _generic_source(tmp_path / "source.3mf")
    target = tmp_path / "out.3mf"
    package_remap.remap_package(source, target, {1: 1, 2: 2})
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(target) as after:
        # Object ids are never renumbered by a package remap; with an identity
        # mapping every member the remapper does not rewrite is byte-identical.
        assert after.read("Metadata/layer_config_ranges.xml") == before.read("Metadata/layer_config_ranges.xml")
        assert '<object id="5">' in after.read("Metadata/layer_config_ranges.xml").decode()


def test_generic_remap_refuses_the_unestablished_flush_vector(tmp_path):
    project = dict(_PROJECT, flush_volumes_vector=["140", "140", "140", "140"])
    source = _generic_source(tmp_path / "source.3mf", project=project)
    target = tmp_path / "out.3mf"
    target.write_bytes(b"existing destination sentinel")
    with pytest.raises(RuntimeError, match="flush_volumes_vector"):
        package_remap.remap_package(source, target, {1: 2, 2: 1})
    assert target.read_bytes() == b"existing destination sentinel"
    # The identity mapping needs no vector permutation, so it passes untouched.
    package_remap.remap_package(source, target, {1: 1, 2: 2})
    with zipfile.ZipFile(target) as archive:
        assert json.loads(archive.read("Metadata/project_settings.config"))["flush_volumes_vector"] == \
            ["140", "140", "140", "140"]


def test_generic_remap_refuses_an_unmapped_range_extruder_atomically(tmp_path):
    ranges = ('<objects><object id="5"><range min_z="0" max_z="5">'
              '<option opt_key="extruder">3</option></range></object></objects>')
    source = _generic_source(tmp_path / "source.3mf",
                             extra={"Metadata/layer_config_ranges.xml": ranges.encode()})
    target = tmp_path / "out.3mf"
    target.write_bytes(b"existing destination sentinel")
    original = source.read_bytes()
    with pytest.raises(RuntimeError, match="layer_config_ranges"):
        package_remap.remap_package(source, target, {1: 2, 2: 1})  # extruder 3 unmapped
    assert target.read_bytes() == b"existing destination sentinel"
    assert source.read_bytes() == original


@pytest.mark.parametrize("member,data", [
    ("Metadata/mystery_vendor.xml", b"<mystery/>"),
    ("Metadata/plate_1.gcode", b"G1 X1\nT1\n"),
])
def test_generic_remap_refuses_unclassified_and_embedded_gcode_members(tmp_path, member, data):
    source = _generic_source(tmp_path / "source.3mf", extra={member: data})
    target = tmp_path / "out.3mf"
    with pytest.raises(RuntimeError, match=re.escape(member.split("/")[-1])):
        package_remap.remap_package(source, target, {1: 2, 2: 1})
    assert not target.exists()


def test_generic_remap_rejects_a_noncontiguous_or_colliding_mapping(tmp_path):
    source = _generic_source(tmp_path / "source.3mf")
    with pytest.raises(RuntimeError, match="mapping"):
        package_remap.remap_package(source, tmp_path / "a.3mf", {1: 2, 2: 2})
    with pytest.raises(RuntimeError, match="mapping"):
        package_remap.remap_package(source, tmp_path / "b.3mf", {1: 3, 2: 1})


# --- C3/C5: the one-filament collapse keeps the used filament's entries -------

def test_declare_one_filament_keeps_the_used_filaments_entries():
    project = {"filament_colour": ["#111111", "#222222"], "filament_type": ["PLA", "PETG"],
               "nozzle_temperature": ["200", "220"],
               "flush_volumes_matrix": ["0", "100", "200", "0"],
               "flush_volumes_vector": ["10", "11", "20", "21"],
               "support_filament": "2", "support_interface_filament": "1",
               "layer_height": "0.2"}
    out = json.loads(_declare_one_filament(json.dumps(project).encode(), keep_index=2))
    assert out["filament_colour"] == ["#222222"]
    assert out["filament_type"] == ["PETG"]
    assert out["nozzle_temperature"] == ["220"]
    assert out["flush_volumes_matrix"] == ["0"]
    # The kept filament's own pair of the vector (two entries per filament in
    # the established fixture shape), not the first filament's.
    assert out["flush_volumes_vector"] == ["20", "21"]
    # The designation that named the kept filament now names filament 1; the
    # one that named a removed filament falls back to 0 (the object's own).
    assert out["support_filament"] == "1"
    assert out["support_interface_filament"] == "0"
    assert out["layer_height"] == "0.2"


# --- C8: the single-color copy no longer carries stale references -------------

def _two_part_source(path):
    write_3mf(path, [("Base", 1, box(30, 30, 10), (60, 60)), ("Cap", 2, box(20, 20, 8), (110, 110))],
              [("Plate 1", [0, 1])], PALETTE)
    extra = {
        "Metadata/custom_gcode_per_layer.xml":
            b'<custom_gcode_per_layer><plate><layer type="0" gcode="ColorChange" extruder="2" top_z="4.2"/></plate></custom_gcode_per_layer>',
        "Metadata/slice_info.config": _SLICE_INFO.encode(),
        "Metadata/layer_config_ranges.xml":
            b'<objects><object id="1"><range min_z="0" max_z="5"><option opt_key="extruder">2</option></range></object></objects>',
        "Metadata/plate_1.gcode": b"G1 X1\nT1\n",
        "Metadata/filament_sequence.json": json.dumps(
            {"print": [{"setting": {"wall_loops": "4"}}]}).encode(),
    }
    with zipfile.ZipFile(path, "a") as archive:
        for name, data in extra.items():
            archive.writestr(name, data)
    return path


def _single_decisions(source, slot=1):
    state = json.loads(STATE.read_text())
    manifest = source_colors.inventory(source)
    white = model_job.resolve_filament_selection(state, dict(state["filament_slots"][str(slot)], slot=slot))
    decisions = {"schema": "part_slots_v1", "mode": "per_part", "source_sha256": manifest["source_sha256"],
                 "choices": {row["key"]: white for row in source_colors.canonical_prompts(manifest)}}
    return manifest, decisions


def test_single_color_plate_carries_no_stale_filament_reference(tmp_path):
    source = _two_part_source(tmp_path / "source.3mf")
    manifest, decisions = _single_decisions(source)
    keys = [p["key"] for p in manifest["parts"] if p["source_extruder"] == 1]
    target = tmp_path / "plate.3mf"
    report = export_single_color_plate(source, target, decisions, keys,
                                       slot_colors=json.loads(STATE.read_text())["filament_slots"])
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        layer = layer_gcode.parse(archive.read("Metadata/custom_gcode_per_layer.xml"))
        ranges = archive.read("Metadata/layer_config_ranges.xml").decode()
        settings = archive.read("Metadata/model_settings.config").decode()
    # The layer change was remapped to the plate's one filament, not carried
    # naming source extruder 2; the ranges option names filament 1 as well.
    assert [row["source_extruder"] for row in layer] == [1]
    assert '<option opt_key="extruder">1</option>' in ranges
    # Sliced usage and embedded G-code name the removed filament's numbering:
    # both are dropped, and the drops are named in the report, never silent.
    assert "Metadata/slice_info.config" in report["dropped_entries"]
    assert "Metadata/plate_1.gcode" in report["dropped_entries"]
    assert "Metadata/slice_info.config" not in names and "Metadata/plate_1.gcode" not in names
    # The embedded preset record is provenance evidence about the source, not a
    # slicing reference: it is preserved byte-for-byte and named as preserved.
    assert "Metadata/filament_sequence.json" in report["preserved_unremapped_entries"]
    # Copy-path object-id policy: the export never renumbers object ids.
    with zipfile.ZipFile(source) as archive:
        source_settings = archive.read("Metadata/model_settings.config").decode()
    assert set(re.findall(r'<object id="(\d+)"', settings)) == \
        set(re.findall(r'<object id="(\d+)"', source_settings))


# --- C6: the rebuilt export refuses height ranges it cannot translate --------

def test_rebuilt_export_refuses_height_range_extruders_by_name(tmp_path):
    source = _two_part_source(tmp_path / "source.3mf")
    manifest, _decisions = _single_decisions(source)
    state = json.loads(STATE.read_text())
    choices = {}
    for row in source_colors.canonical_prompts(manifest):
        choices[row["key"]] = model_job.resolve_filament_selection(
            state, dict(state["filament_slots"]["1"], slot=1))
    decisions = {"schema": "part_slots_v1", "mode": "per_part",
                 "source_sha256": manifest["source_sha256"], "choices": choices}
    target = tmp_path / "out.3mf"
    target.write_bytes(b"existing destination sentinel")
    with pytest.raises(RuntimeError, match="layer_config_ranges"):
        export_color_geometry(source, target, decisions)
    assert target.read_bytes() == b"existing destination sentinel"


# --- C7: a kept layer change keeps the color attribute Orca's loader requires -

def test_rebuilt_layer_change_keeps_the_color_attribute(tmp_path):
    """The first Phase C build stripped the kept entries' color attribute, on
    the audit's UNVERIFIED note that no consumer treats it as authoritative.
    Real-Orca CI falsified the strip: Orca returned -6 ("the input model file
    can not be parsed") for every height-color slice (PR #1133, Slicer test
    stable shard 1: test_creator_height_colors_change_tool_at_their_heights
    and the corpus height-color-changes case, both green on the stack base).
    The attribute is load-bearing for parsing, and tool selection comes from
    the extruder attribute, so the color rides verbatim as display metadata."""
    source = tmp_path / "chroma.3mf"
    write_3mf(source, [("Tower", 1, box(20, 20, 12), (60, 60))], [("Plate 1", [0])],
              [("#111111", "PLA"), ("#222222", "PLA")])
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("Metadata/custom_gcode_per_layer.xml",
                         '<custom_gcode_per_layer><plate>'
                         '<layer type="0" gcode="ColorChange" extruder="2" top_z="4" color="#222222"/>'
                         '</plate></custom_gcode_per_layer>')
    manifest = source_colors.inventory(source)
    state = json.loads(STATE.read_text())
    picks = {1: 1, 2: 2}
    choices = {}
    for row in source_colors.canonical_prompts(manifest):
        match = re.fullmatch(r"(?:part:.+|filament:(\d+))", row["key"])
        slot = picks.get(int(match.group(1)), 1) if match and match.group(1) else 1
        choices[row["key"]] = model_job.resolve_filament_selection(
            state, dict(state["filament_slots"][str(slot)], slot=slot))
    decisions = {"schema": "part_slots_v1", "mode": "per_part",
                 "source_sha256": manifest["source_sha256"], "choices": choices}
    target = tmp_path / "out.3mf"
    _mapping, report = export_color_geometry(source, target, decisions)
    assert report["layer_changes"]["kept"] == 1
    with zipfile.ZipFile(target) as archive:
        rows = layer_gcode.parse(archive.read("Metadata/custom_gcode_per_layer.xml"))
    assert rows[0]["source_extruder"] == 2 and rows[0]["color"] == "#222222"


# --- C9: dropped per-part settings are named, not silent ----------------------

def test_rebuilt_export_names_dropped_per_part_settings(tmp_path):
    source = tmp_path / "settings.3mf"
    write_3mf(source, [("Base", 1, box(30, 30, 10), (60, 60)), ("Cap", 2, box(20, 20, 8), (110, 110))],
              [("Plate 1", [0, 1])], PALETTE)
    with zipfile.ZipFile(source) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    settings = entries["Metadata/model_settings.config"].decode()
    settings = settings.replace('<metadata key="name" value="Base"/>',
                                  '<metadata key="name" value="Base"/>'
                                  '<metadata key="wall_loops" value="3"/>'
                                  '<metadata key="vendor_custom_key" value="x"/>', 1)
    entries["Metadata/model_settings.config"] = settings.encode()
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    manifest = source_colors.inventory(source)
    state = json.loads(STATE.read_text())
    choices = {row["key"]: model_job.resolve_filament_selection(
        state, dict(state["filament_slots"]["1"], slot=1))
        for row in source_colors.canonical_prompts(manifest)}
    decisions = {"schema": "part_slots_v1", "mode": "per_part",
                 "source_sha256": manifest["source_sha256"], "choices": choices}
    _mapping, report = export_color_geometry(source, tmp_path / "out.3mf", decisions)
    dropped = {row["key"] for row in report["dropped_part_settings"]}
    assert "vendor_custom_key" in dropped and "wall_loops" not in dropped


# --- C10: the production copy preserves the whole member set ------------------

def test_production_plate_preserves_auxiliaries_members(tmp_path):
    source = tmp_path / "production.3mf"
    model = ('<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
             '<object id="1"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/>'
             '<vertex x="0" y="1" z="0"/></vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles>'
             '</mesh></object></resources><build><item objectid="1"/></build></model>')
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("3D/3dmodel.model", model)
        archive.writestr("Metadata/model_settings.config",
                         '<config><object id="1"><metadata key="extruder" value="1"/></object></config>')
        archive.writestr("Metadata/project_settings.config",
                         json.dumps({"filament_colour": ["#FFFFFF"], "filament_type": ["PLA"]}, indent=2))
        archive.writestr("Auxiliaries/.thumbnails/thumbnail_3mf.png", b"thumbnail bytes")
    manifest = source_colors.inventory(source)
    decisions = {"mode": "per_part", "source_sha256": manifest["source_sha256"],
                 "choices": {row["key"]: {"slot": 2} for row in source_colors.canonical_prompts(manifest)}}
    target = tmp_path / "out.3mf"
    export_production_plate(source, target, decisions, {"2": "#123456"})
    with zipfile.ZipFile(target) as archive:
        assert archive.read("Auxiliaries/.thumbnails/thumbnail_3mf.png") == b"thumbnail bytes"
