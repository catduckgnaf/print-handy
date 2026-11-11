"""#1155 deferred usage reads: modifier extruders and PrusaSlicer per-layer files.

- A Bambu/OrcaSlicer modifier_part that names its own extruder prints the
  region it covers with that filament. It was filtered out with the other
  non-printing volumes, so the plate's usage never named that filament, and
  the rebuilt export wrote the modifier with no extruder at all: the region
  printed in the body's color with nothing said.
- PrusaSlicer writes height-based tool and color changes to
  Metadata/Prusa_Slicer_custom_gcode_per_print_z.xml and per-range extruders
  to Metadata/Prusa_Slicer_layer_config_ranges.xml. Neither was read, so a
  plate whose source changes filament by height reported its layer-change and
  height-range usage as known and empty.

Usage the source names but no existing color question maps, or that the
export does not carry, is UNKNOWN usage (a warning the operator may accept,
#1189/#1190), never a used filament or a new question (#1184, the #1203
question-set gate).
"""
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from print_handy.core import model_job, source_colors as sc
from print_handy.core.color_geometry import export_color_geometry
from tests.corpus.make_models import _write_package

NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
STATE = Path(__file__).parents[1] / "pipeline" / "fixtures" / "printer_hardware.json"


def _box(dx, dy, dz, offset=(0.0, 0.0, 0.0)):
    ox, oy, oz = offset
    v = [(ox, oy, oz), (ox + dx, oy, oz), (ox + dx, oy + dy, oz), (ox, oy + dy, oz),
         (ox, oy, oz + dz), (ox + dx, oy, oz + dz), (ox + dx, oy + dy, oz + dz), (ox, oy + dy, oz + dz)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (2, 3, 7), (2, 7, 6), (1, 2, 6), (1, 6, 5), (3, 0, 4), (3, 4, 7)]
    verts = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in v)
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces)
    return f"<mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh>"


def modifier_source(path, modifier_extruder, *, palette=("#FFFFFF", "#067749", "#2244CC")):
    """Plate 1: an assembly (body on filament 1 plus a modifier). Plate 2: a Cap on filament 2.

    modifier_extruder None writes no extruder on the modifier part; any other
    value is written as the part's own extruder metadata.
    """
    ext = ("" if modifier_extruder is None else
           f'<metadata key="extruder" value="{modifier_extruder}"/>')
    settings = ('<?xml version="1.0" encoding="UTF-8"?><config><object id="1">'
                '<metadata key="name" value="assembly"/><metadata key="extruder" value="1"/>'
                '<part id="2" subtype="normal_part"><metadata key="name" value="main"/>'
                '<metadata key="extruder" value="1"/></part>'
                '<part id="3" subtype="modifier_part"><metadata key="name" value="band"/>'
                f'{ext}</part></object>'
                '<object id="4"><metadata key="name" value="Cap"/><metadata key="extruder" value="2"/>'
                '<part id="4" subtype="normal_part"><metadata key="name" value="Cap"/>'
                '<metadata key="extruder" value="2"/></part></object>'
                '<plate><metadata key="plater_id" value="1"/><model_instance>'
                '<metadata key="object_id" value="1"/></model_instance></plate>'
                '<plate><metadata key="plater_id" value="2"/><model_instance>'
                '<metadata key="object_id" value="4"/></model_instance></plate></config>')
    model = ('<?xml version="1.0" encoding="UTF-8"?>'
             f'<model unit="millimeter" xmlns="{NS}"><resources>'
             f'<object id="2" name="main" type="model">{_box(40, 40, 10)}</object>'
             f'<object id="3" name="band" type="model">{_box(10, 10, 12, (5, 5, 0))}</object>'
             f'<object id="4" name="Cap" type="model">{_box(20, 20, 8, (60, 0, 0))}</object>'
             '<object id="1" name="assembly" type="model"><components>'
             '<component objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
             '<component objectid="3" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
             '</components></object>'
             '</resources><build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
             '<item objectid="4" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></build></model>')
    project = {"filament_colour": list(palette), "filament_type": ["PLA"] * len(palette)}
    _write_package(path, {"3D/3dmodel.model": model, "Metadata/model_settings.config": settings,
                          "Metadata/project_settings.config": json.dumps(project)})
    return path


def _question_keys(manifest):
    return {row["key"] for row in sc.canonical_prompts(manifest)}


def _part_keys(manifest):
    return {"part:" + p["key"] for p in manifest["parts"]}


# --- modifier extruders: usage ------------------------------------------------------------------

def test_modifier_extruder_a_question_already_maps_is_counted_on_its_plate(tmp_path):
    manifest = sc.inventory(modifier_source(tmp_path / "mod.3mf", 2))
    entry = manifest["plate_usage"]["1"]
    assert entry["classes"]["modifiers"]["status"] == "known"
    assert entry["classes"]["modifiers"]["source_filaments"] == [2]
    assert entry["source_filaments"] == [1, 2]
    assert entry["filament_classes"]["2"] == ["modifiers"]
    assert entry["usage_known"]
    # Filament 2 is already the Cap's question: no new question.
    assert _question_keys(manifest) == _part_keys(manifest)
    # Plate 2 holds no modifier.
    assert manifest["plate_usage"]["2"]["classes"]["modifiers"]["source_filaments"] == []


def test_modifier_extruder_no_question_maps_is_unknown_never_a_question(tmp_path):
    manifest = sc.inventory(modifier_source(tmp_path / "mod.3mf", 3))
    entry = manifest["plate_usage"]["1"]
    assert "modifiers" in entry["unknown_classes"]
    assert 3 not in entry["source_filaments"]
    assert not entry["usage_known"]
    assert _question_keys(manifest) == _part_keys(manifest)
    warnings = sc.filament_readiness_warnings(manifest, {}, ["1", "2"])
    assert warnings and all("plate 1" in w for w in warnings), warnings


def test_modifier_extruder_outside_the_palette_is_unknown_not_used(tmp_path):
    manifest = sc.inventory(modifier_source(tmp_path / "mod.3mf", 9))
    entry = manifest["plate_usage"]["1"]
    assert "modifiers" in entry["unknown_classes"]
    assert "outside the project palette" in entry["classes"]["modifiers"]["reason"]
    assert 9 not in entry["source_filaments"]
    assert "filament:9" not in _question_keys(manifest)


@pytest.mark.parametrize("extruder", [None, "0", "1"])
def test_modifier_inheriting_or_repeating_the_object_filament_adds_nothing(tmp_path, extruder):
    manifest = sc.inventory(modifier_source(tmp_path / "mod.3mf", extruder))
    entry = manifest["plate_usage"]["1"]
    assert entry["classes"]["modifiers"] == {
        "status": "known", "source_filaments": [],
        "evidence": entry["classes"]["modifiers"]["evidence"]}
    assert entry["source_filaments"] == [1]
    assert entry["usage_known"]


# --- modifier extruders: the rebuilt export -----------------------------------------------------

def _decisions(source, picks):
    state = json.loads(STATE.read_text())
    manifest = sc.inventory(source)
    choices = {}
    for row in sc.canonical_prompts(manifest):
        part = next(p for p in manifest["parts"] if "part:" + p["key"] == row["key"])
        slot = picks[part["source_extruder"]]
        choices[row["key"]] = model_job.resolve_filament_selection(
            state, dict(state["filament_slots"][str(slot)], slot=slot))
    return {"schema": "part_slots_v1", "mode": "per_part",
            "source_sha256": manifest["source_sha256"], "choices": choices}


def _part_meta(package, name):
    with zipfile.ZipFile(package) as archive:
        root = ET.fromstring(archive.read("Metadata/model_settings.config"))
    for obj in root.findall("object"):
        for part in obj.findall("part"):
            meta = {m.get("key"): m.get("value") for m in part.findall("metadata")}
            if meta.get("name") == name:
                return meta, part.get("subtype")
    raise AssertionError(f"no part named {name}")


def test_rebuilt_export_writes_the_modifiers_own_filament_as_its_tool(tmp_path):
    source = modifier_source(tmp_path / "mod.3mf", 2)
    # Non-identity: filament 1 -> slot 3, filament 2 -> slot 1; tools are the
    # sorted slots [1, 3], so filament 2 is tool 1 and filament 1 is tool 2.
    target = tmp_path / "out.3mf"
    mapping, report = export_color_geometry(source, target, _decisions(source, {1: 3, 2: 1}))
    assert mapping == {"0": 1, "1": 3}
    body, _ = _part_meta(target, "main")
    band, subtype = _part_meta(target, "band")
    assert subtype == "modifier_part"
    assert body["extruder"] == "2"
    assert band["extruder"] == "1"
    assert not [row for row in report["feature_filaments"] if row["key"] == "extruder"]


def test_rebuilt_export_names_a_modifier_filament_the_job_does_not_load(tmp_path):
    source = modifier_source(tmp_path / "mod.3mf", 3)
    target = tmp_path / "out.3mf"
    _mapping, report = export_color_geometry(source, target, _decisions(source, {1: 1, 2: 2}))
    band, _ = _part_meta(target, "band")
    # Not written: the body's tool prints the region, and the report says so.
    assert "extruder" not in band
    rows = [row for row in report["feature_filaments"]
            if row["key"] == "extruder" and row["status"] == "unknown"]
    assert len(rows) == 1 and "band" in rows[0]["where"], report["feature_filaments"]


# --- paint pass cost: one inventory per flow ----------------------------------------------------

def painted_grid_source(path, cells=120):
    """A flat painted grid (2 * cells**2 triangles), every triangle painted filament 2."""
    verts = "".join(f'<vertex x="{x}" y="{y}" z="0"/>' for y in range(cells + 1) for x in range(cells + 1))
    tris = []
    for y in range(cells):
        for x in range(cells):
            a = y * (cells + 1) + x
            b, c, d = a + 1, a + cells + 1, a + cells + 2
            tris.append(f'<triangle v1="{a}" v2="{b}" v3="{d}" paint_color="8"/>')
            tris.append(f'<triangle v1="{a}" v2="{d}" v3="{c}" paint_color="8"/>')
    mesh = f"<mesh><vertices>{verts}</vertices><triangles>{''.join(tris)}</triangles></mesh>"
    settings = ('<?xml version="1.0" encoding="UTF-8"?><config><object id="1">'
                '<metadata key="name" value="Tile"/><metadata key="extruder" value="1"/>'
                '<part id="2" subtype="normal_part"><metadata key="name" value="Tile"/>'
                '<metadata key="extruder" value="1"/></part></object>'
                '<plate><metadata key="plater_id" value="1"/><model_instance>'
                '<metadata key="object_id" value="1"/></model_instance></plate></config>')
    model = ('<?xml version="1.0" encoding="UTF-8"?>'
             f'<model unit="millimeter" xmlns="{NS}"><resources>'
             f'<object id="2" type="model">{mesh}</object>'
             '<object id="1" type="model"><components><component objectid="2"/></components></object>'
             '</resources><build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 10 10 0"/></build></model>')
    project = {"filament_colour": ["#FFFFFF", "#067749"], "filament_type": ["PLA", "PLA"]}
    _write_package(path, {"3D/3dmodel.model": model, "Metadata/model_settings.config": settings,
                          "Metadata/project_settings.config": json.dumps(project)})
    return path


def test_a_second_inventory_in_one_flow_skips_the_paint_pass(tmp_path):
    import time
    source = painted_grid_source(tmp_path / "grid.3mf")
    with sc.inventory_scope():
        start = time.perf_counter()
        first = sc.inventory(source)
        cold = time.perf_counter() - start
        start = time.perf_counter()
        second = sc.inventory(source)
        warm = time.perf_counter() - start
    assert first == second == sc.inventory(source)
    assert first["painted"]
    assert first["plate_usage"]["1"]["classes"]["painting"]["source_filaments"] == [2]
    # The warm call hashes the file and copies a manifest; the cold one
    # decodes 28,800 painted triangles. A generous bound, not a benchmark.
    assert warm < cold / 3, (cold, warm)


# --- PrusaSlicer per-layer files ----------------------------------------------------------------

def prusa_source(path, *, members=None):
    """A one-object PrusaSlicer project on extruder 1 of a two-filament palette."""
    config = ("; generated by PrusaSlicer 2.8.0\n; filament_colour = #FF0000;#00FF00\n"
              "; extruder_colour = #FF0000;#00FF00\n; filament_type = PLA;PLA\n"
              "; perimeter_extruder = 1\n; infill_extruder = 1\n; solid_infill_extruder = 1\n"
              "; support_material_extruder = 0\n; support_material_interface_extruder = 0\n")
    model = ('<?xml version="1.0" encoding="UTF-8"?>'
             f'<model unit="millimeter" xmlns="{NS}"><resources>'
             f'<object id="1" type="model">{_box(20, 20, 20)}</object>'
             '</resources><build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 50 50 0"/></build></model>')
    model_config = ('<?xml version="1.0" encoding="UTF-8"?><config><object id="1" instances_count="1">'
                    '<metadata type="object" key="name" value="Sign"/>'
                    '<metadata type="object" key="extruder" value="1"/>'
                    '<volume firstid="0" lastid="11"><metadata type="volume" key="name" value="Sign"/>'
                    '</volume></object></config>')
    _write_package(path, {"3D/3dmodel.model": model, "Metadata/Slic3r_PE.config": config,
                          "Metadata/Slic3r_PE_model.config": model_config, **(members or {})})
    return path


PRUSA_GCODE = "Metadata/Prusa_Slicer_custom_gcode_per_print_z.xml"
PRUSA_RANGES = "Metadata/Prusa_Slicer_layer_config_ranges.xml"


def _prusa_codes(*codes, mode="MultiAsSingle"):
    rows = "".join(f'<code print_z="{z}" type="{t}" extruder="{e}" color="{c}" extra="" gcode="{g}"/>'
                   for z, t, e, c, g in codes)
    return (f'<?xml version="1.0" encoding="utf-8"?><custom_gcodes_per_print_z>{rows}'
            f'<mode value="{mode}"/></custom_gcodes_per_print_z>')


def _ranges(extruder):
    return ('<?xml version="1.0" encoding="utf-8"?><objects><object id="1">'
            '<range min_z="0" max_z="5"><option opt_key="layer_height">0.2</option>'
            f'<option opt_key="extruder">{extruder}</option></range></object></objects>')


def test_prusa_tool_change_by_height_is_unknown_never_known_empty(tmp_path):
    path = prusa_source(tmp_path / "sign.3mf", members={
        PRUSA_GCODE: _prusa_codes(("5", "2", "2", "#00FF00", "tool_change"))})
    manifest = sc.inventory(path)
    entry = manifest["plate_usage"]["1"]
    assert entry["classes"]["layer_changes"]["status"] == "unknown"
    assert "Prusa_Slicer_custom_gcode_per_print_z.xml" in entry["classes"]["layer_changes"]["reason"]
    assert not entry["usage_known"]
    assert 2 not in entry["source_filaments"]
    assert _question_keys(manifest) == _part_keys(manifest)
    assert sc.filament_readiness_warnings(manifest, {}, ["1"])


def test_prusa_color_change_is_unknown_usage(tmp_path):
    path = prusa_source(tmp_path / "sign.3mf", members={
        PRUSA_GCODE: _prusa_codes(("3.2", "0", "1", "#0000FF", "M600"), mode="SingleExtruder")})
    entry = sc.inventory(path)["plate_usage"]["1"]
    assert entry["classes"]["layer_changes"]["status"] == "unknown"
    assert not entry["usage_known"]


def test_prusa_pauses_and_custom_gcode_change_no_filament(tmp_path):
    path = prusa_source(tmp_path / "sign.3mf", members={
        PRUSA_GCODE: _prusa_codes(("4", "1", "1", "", "M601"), ("6", "4", "1", "", "M117 hello"))})
    entry = sc.inventory(path)["plate_usage"]["1"]
    assert entry["classes"]["layer_changes"]["status"] == "known"
    assert entry["classes"]["layer_changes"]["source_filaments"] == []
    assert entry["usage_known"]


def test_prusa_height_range_extruder_is_unknown_never_known_empty(tmp_path):
    manifest = sc.inventory(prusa_source(tmp_path / "sign.3mf", members={PRUSA_RANGES: _ranges(2)}))
    entry = manifest["plate_usage"]["1"]
    assert entry["classes"]["height_ranges"]["status"] == "unknown"
    assert "Prusa_Slicer_layer_config_ranges.xml" in entry["classes"]["height_ranges"]["reason"]
    assert not entry["usage_known"]
    assert _question_keys(manifest) == _part_keys(manifest)


def test_prusa_height_range_with_the_objects_own_extruder_stays_known(tmp_path):
    entry = sc.inventory(prusa_source(tmp_path / "sign.3mf", members={PRUSA_RANGES: _ranges(0)}))[
        "plate_usage"]["1"]
    assert entry["classes"]["height_ranges"]["status"] == "known"
    assert entry["usage_known"]


@pytest.mark.parametrize("member", [PRUSA_GCODE, PRUSA_RANGES])
def test_unparseable_prusa_layer_file_is_unknown(tmp_path, member):
    entry = sc.inventory(prusa_source(tmp_path / "sign.3mf", members={member: "<not xml"}))[
        "plate_usage"]["1"]
    assert not entry["usage_known"]
    assert any("could not be parsed" in (entry["classes"][name].get("reason") or "")
               for name in ("layer_changes", "height_ranges"))
