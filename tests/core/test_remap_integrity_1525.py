"""Remap integrity validation (#1525, tranche C).

Roles, painted identity, per-filament array schema, plate selection and
unknown references are checked against real inventory output; unknown
references are rejected, never adopted, and filament conditions warn
instead of blocking. Fixtures follow the corpus builder shape: a mesh
object plus an assembly object per part, Bambu-style model settings.
"""
import json
import zipfile
from pathlib import Path

from print_handy.core import remap_integrity as ri, source_colors

CORPUS = Path(__file__).resolve().parents[1] / "corpus" / "cases"
NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_VERTICES = [(0, 0, 0), (10, 0, 0), (0, 10, 0), (0, 0, 10)]
_FACES = [(0, 2, 1), (0, 1, 3), (1, 2, 3), (2, 0, 3)]

# (name, subtype, extruder)
SHAPES = [("body", "normal_part", 1), ("mod", "modifier_part", 2),
          ("cut", "negative_part", 1), ("block", "support_blocker", 1),
          ("force", "support_enforcer", 1)]


def _mesh_xml():
    verts = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in _VERTICES)
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in _FACES)
    return f"<mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh>"


def _project(tmp_path, settings_extra=None, name="proj.3mf"):
    """A five-part project on one plate, built with the plate-usage
    suite's proven package builder, with subtypes rewritten per part."""
    from tests.core.test_plate_usage import write_source
    built = write_source(tmp_path / "built.3mf",
                         [(part_name, extruder, None)
                          for part_name, _subtype, extruder in SHAPES],
                         [[0, 1, 2, 3, 4]])
    with zipfile.ZipFile(built) as archive:
        members = {info.filename: archive.read(info.filename)
                   for info in archive.infolist()}
    config = members["Metadata/model_settings.config"].decode()
    for index, (_part_name, subtype, _extruder) in enumerate(SHAPES):
        mesh_id = 2 * index + 2
        config = config.replace(
            f'<part id="{mesh_id}" subtype="normal_part">',
            f'<part id="{mesh_id}" subtype="{subtype}">')
    members["Metadata/model_settings.config"] = config.encode()
    if settings_extra:
        settings = json.loads(members["Metadata/project_settings.config"])
        settings.update(settings_extra)
        members["Metadata/project_settings.config"] = json.dumps(settings).encode()
    target = tmp_path / name
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for member, data in members.items():
            archive.writestr(member, data)
    return target


def test_roles_are_tracked_and_modifier_is_never_printable(tmp_path):
    report = ri.validate(_project(tmp_path))
    assert report["ok"], report["problems"]
    assert report["roles"]["modifiers"], report["roles"]
    assert report["roles"]["negative_parts"]
    assert report["roles"]["support_blockers"]
    assert report["roles"]["support_enforcers"]
    printable = set(report["roles"]["printable_parts"])
    assert not (printable & set(report["roles"]["modifiers"]))
    assert not (printable & set(report["roles"]["negative_parts"]))


def test_tracking_keeps_logical_material_slot_and_toolhead_separate(tmp_path):
    report = ri.validate(_project(tmp_path), decisions={1: {"slot": 2}, 2: {"slot": 1}})
    rows = {row["logical_filament"]: row for row in report["tracking"]}
    assert rows[1]["material"] == "PLA"
    assert rows[1]["physical_slot"] == 2
    assert rows[2]["physical_slot"] == 1
    # No printer model proved slot == head here, so toolhead stays unknown.
    assert rows[1]["toolhead"] is None


def test_unknown_references_are_rejected_not_adopted(tmp_path):
    target = _project(tmp_path)
    assert not ri.validate(target, mapping={9: 1, 8: 2, 7: 3, 6: 4, 5: 5,
                                            4: 6, 3: 7, 2: 8, 1: 9})["ok"]
    report = ri.validate(target, decisions={7: {"slot": 1}})
    assert not report["ok"]
    assert any("rejected" in p for p in report["problems"])


def test_unknown_plate_selection_is_rejected_when_plates_are_known(tmp_path):
    target = _project(tmp_path)
    known = ri.plate_ids(source_colors.inventory(target))
    if not known:
        # This fixture shape records no plate ids; plate validation is
        # covered where a manifest names plates (corpus multiplate case).
        return
    report = ri.validate(target, selected_plate_ids=["999"])
    assert not report["ok"]


def test_palette_length_mismatch_is_a_problem(tmp_path):
    target = _project(tmp_path, {"filament_type": ["PLA"]})
    report = ri.validate(target)
    assert not report["ok"]
    assert any("disagree in length" in p for p in report["problems"])


def test_unknown_palette_shaped_array_warns_and_is_named(tmp_path):
    target = _project(tmp_path, {"mystery_per_filament": ["a", "b", "c", "d"]})
    report = ri.validate(target)
    assert report["ok"], report["problems"]
    assert any("mystery_per_filament" in w for w in report["warnings"])


def test_collision_mapping_is_a_problem(tmp_path):
    report = ri.validate(_project(tmp_path), mapping={1: 1, 2: 1})
    assert not report["ok"]


def test_prusa_mmu_corpus_project_validates():
    source = CORPUS / "prusa-mmu-project" / "model.3mf"
    assert source.is_file()
    report = ri.validate(source)
    assert report["descriptor"]["kind"] in ("prusa", "orca", "bambu", "generic-core")
    assert isinstance(report["tracking"], list) and report["tracking"]
