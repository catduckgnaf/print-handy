"""Declared roles, assembly transforms and collisions (#1017 item 3).

Declarations come from a project_v1 manifest (or the same shape passed
directly). A declared role beats the file-name inference and is labeled
declared; a declared rack ear earns the panel-clearance check by
declaration, not by the height heuristic. Declared transforms place the
parts, and placed parts get a real interpenetration check — flush contact
is not a collision, and parts without placement are named as unchecked
instead of passing silently.
"""
import math
from pathlib import Path

import pytest

from print_handy.core import assembly_check as ac
from print_handy.core import projects
from print_handy.core.geometry_layout import write_binary_stl

AD5X_BED = (220, 220, 220)


def _box(path, dx, dy, dz, origin=(0, 0, 0)):
    ox, oy, oz = origin
    v = [(ox, oy, oz), (ox + dx, oy, oz), (ox + dx, oy + dy, oz), (ox, oy + dy, oz),
         (ox, oy, oz + dz), (ox + dx, oy, oz + dz), (ox + dx, oy + dy, oz + dz),
         (ox, oy + dy, oz + dz)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    write_binary_stl([[v[a], v[b], v[c]] for a, b, c in faces], path)
    return path


def _rotated_box(path, dx, dy, dz, angle_deg):
    """A box rotated about Z through its center, written as an STL."""
    raw = _box(Path(str(path) + ".raw"), dx, dy, dz,
               origin=(-dx / 2, -dy / 2, 0))
    from print_handy.core.orca_adapter import read_stl_triangles
    tris = read_stl_triangles(raw)
    raw.unlink()
    a = math.radians(angle_deg)
    cos_a, sin_a = math.cos(a), math.sin(a)
    rotated = [[(x * cos_a - y * sin_a, x * sin_a + y * cos_a, z) for x, y, z in tri]
               for tri in tris]
    write_binary_stl(rotated, path)
    return path


def _codes(findings, code):
    return [f for f in findings if f.code == code]


# ---------------------------------------------------------------- declared roles

def test_declared_rack_ear_gets_the_clearance_check_by_declaration(tmp_path):
    # 80 mm tall in a 4U project: the height heuristic (half of 177.8 mm)
    # would never flag this part as an ear; the declaration does.
    ear = _box(tmp_path / "widget.stl", 20, 30, 80)
    other = _box(tmp_path / "plate.stl", 50, 50, 5)
    declared = ac.review([ear, other], bed_mm=AD5X_BED, rack_units=4,
                         declarations={"widget.stl": {"role": "rack_ear"}})
    assert _codes(declared, "rack_clearance_ok")
    plain = ac.review([ear, other], bed_mm=AD5X_BED, rack_units=4)
    assert not _codes(plain, "rack_clearance_ok")
    assert not _codes(plain, "rack_panel_clearance")


def test_declared_non_ear_is_not_ear_checked_by_the_height_heuristic(tmp_path):
    arm = _box(tmp_path / "ARM.stl", 20, 30, 88.5)
    tray = _box(tmp_path / "TRAY.stl", 50, 50, 5)
    inferred = ac.review([arm, tray], bed_mm=AD5X_BED, rack_units=2)
    assert _codes(inferred, "rack_panel_clearance")  # heuristic fires undeclared
    declared = ac.review([arm, tray], bed_mm=AD5X_BED, rack_units=2,
                         declarations={"ARM.stl": {"role": "structural"}})
    assert not _codes(declared, "rack_panel_clearance")
    assert not _codes(declared, "rack_clearance_ok")


def test_declared_rack_ear_without_units_is_a_warning_not_a_pass(tmp_path):
    ear = _box(tmp_path / "ear.stl", 20, 30, 80)
    other = _box(tmp_path / "plate.stl", 50, 50, 5)
    findings = ac.review([ear, other], bed_mm=AD5X_BED,
                         declarations={"ear.stl": {"role": "rack_ear"}})
    warn = _codes(findings, "rack_ear_unchecked")
    assert warn and warn[0].level == "warn"


def test_declared_reference_and_coupon_roles_beat_file_names(tmp_path):
    plain = _box(tmp_path / "plain.stl", 300, 300, 36)   # name says nothing
    nut = _box(tmp_path / "00_TEST_NUT.stl", 20, 20, 5)  # name says coupon
    tray = _box(tmp_path / "tray.stl", 50, 50, 5)
    findings = ac.review([plain, nut, tray], bed_mm=AD5X_BED, declarations={
        "plain.stl": {"role": "reference"}, "00_TEST_NUT.stl": {"role": "structural"}})
    shape = next(f for f in findings if f.code == "parts")
    assert shape.evidence["reference"] == ["plain.stl"]
    assert shape.evidence["structural"] == ["00_TEST_NUT.stl", "tray.stl"]
    assert shape.evidence["role_source"] == {"plain.stl": "declared",
                                             "00_TEST_NUT.stl": "declared",
                                             "tray.stl": "inferred"}
    assert not any(f.part == "plain.stl" and f.code == "exceeds_bed" for f in findings)


def test_declarations_from_a_project_v1_manifest(tmp_path):
    ear = _box(tmp_path / "ear.stl", 20, 30, 80)
    tray = _box(tmp_path / "tray.stl", 50, 50, 5)
    project = projects.from_files("intake-9", "uploaded_files", [
        {"name": "ear.stl", "role": "rack_ear"},
        {"name": "tray.stl", "role": "structural",
         "assembly_transform": {"translate_mm": [0, 0, 10]}},
    ], "Rack")
    findings = ac.review([ear, tray], bed_mm=AD5X_BED, rack_units=4, project=project)
    assert _codes(findings, "rack_clearance_ok")
    placement = _codes(findings, "assembly_placement")
    assert placement and placement[0].part == "tray.stl"
    assert placement[0].evidence["placed_bounds_mm"][0][2] == pytest.approx(10.0)


def test_invalid_declarations_raise(tmp_path):
    part = _box(tmp_path / "a.stl", 10, 10, 10)
    with pytest.raises(ValueError):
        ac.review([part], bed_mm=AD5X_BED, declarations={"a.stl": {"role": "ear"}})
    with pytest.raises(ValueError):
        ac.review([part], bed_mm=AD5X_BED,
                  declarations={"a.stl": {"assembly_transform": {"scale": [2, 2, 2]}}})


# ---------------------------------------------------------------- transforms

def test_placed_bounds_apply_rotation_then_translation():
    report = ac.PartReport("x.stl", "structural",
                           {"dimensions_mm": [10, 20, 5], "bounds_min_mm": [0, 0, 0],
                            "bounds_max_mm": [10, 20, 5], "faces": 12},
                           transform={"translate_mm": [100, 0, 10], "rotate_deg": [0, 0, 90]})
    lo, hi = report.placed_bounds
    assert lo == pytest.approx([80.0, 0.0, 10.0])
    assert hi == pytest.approx([100.0, 10.0, 15.0])


def test_separated_placements_prove_no_collision(tmp_path):
    a = _box(tmp_path / "a.stl", 50, 50, 5)
    b = _box(tmp_path / "b.stl", 50, 50, 5)
    findings = ac.review([a, b], bed_mm=AD5X_BED, declarations={
        "a.stl": {"assembly_transform": {"translate_mm": [0, 0, 0]}},
        "b.stl": {"assembly_transform": {"translate_mm": [0, 0, 10]}}})
    verdict = _codes(findings, "assembly_no_collision")
    assert verdict and verdict[0].level == "info"
    assert not _codes(findings, "assembly_collision")
    assert "transforms and collisions" not in ac.summary_text(findings)


def test_flush_contact_is_not_a_collision(tmp_path):
    a = _box(tmp_path / "a.stl", 50, 50, 5)
    b = _box(tmp_path / "b.stl", 50, 50, 5)
    findings = ac.review([a, b], bed_mm=AD5X_BED, declarations={
        "a.stl": {"assembly_transform": {"translate_mm": [0, 0, 0]}},
        "b.stl": {"assembly_transform": {"translate_mm": [50, 0, 0]}}})
    assert not _codes(findings, "assembly_collision")
    assert _codes(findings, "assembly_no_collision")


def test_interpenetrating_placements_fail_as_a_collision(tmp_path):
    a = _box(tmp_path / "a.stl", 50, 50, 5)
    b = _box(tmp_path / "b.stl", 50, 50, 5)
    findings = ac.review([a, b], bed_mm=AD5X_BED, declarations={
        "a.stl": {"assembly_transform": {"translate_mm": [0, 0, 0]}},
        "b.stl": {"assembly_transform": {"translate_mm": [25, 10, 2]}}})
    hit = _codes(findings, "assembly_collision")
    assert hit and hit[0].level == "fail"
    assert hit[0].evidence["overlap_mm"] == pytest.approx([25.0, 40.0, 3.0])


def test_crossing_parts_with_no_vertex_inside_still_collide(tmp_path):
    # Two long slabs crossing like a plus sign: no vertex of either lies
    # inside the other, so only the edge-piercing test can catch it.
    a = _box(tmp_path / "a.stl", 100, 20, 5)
    b = _box(tmp_path / "b.stl", 20, 100, 5)
    findings = ac.review([a, b], bed_mm=AD5X_BED, declarations={
        "a.stl": {"assembly_transform": {"translate_mm": [0, 40, 0]}},
        "b.stl": {"assembly_transform": {"translate_mm": [40, 0, 2]}}})
    assert _codes(findings, "assembly_collision")


def test_overlapping_bounds_without_interpenetration_are_not_a_collision(tmp_path):
    a = _box(tmp_path / "a.stl", 20, 20, 5)
    b = _rotated_box(tmp_path / "b.stl", 20, 20, 5, 45)
    findings = ac.review([a, b], bed_mm=AD5X_BED, declarations={
        "a.stl": {"assembly_transform": {"translate_mm": [0, 0, 0]}},
        "b.stl": {"assembly_transform": {"translate_mm": [28, 28, 0]}}})
    assert not _codes(findings, "assembly_collision")
    verdict = _codes(findings, "assembly_no_collision")
    assert verdict and verdict[0].evidence["bounds_overlap_mm"] is not None


def test_partial_placement_is_named_and_never_a_full_pass(tmp_path):
    a = _box(tmp_path / "a.stl", 50, 50, 5)
    b = _box(tmp_path / "b.stl", 50, 50, 5)
    c = _box(tmp_path / "c.stl", 50, 50, 5)
    findings = ac.review([a, b, c], bed_mm=AD5X_BED, declarations={
        "a.stl": {"assembly_transform": {"translate_mm": [0, 0, 0]}},
        "b.stl": {"assembly_transform": {"translate_mm": [0, 0, 10]}}})
    warn = _codes(findings, "assembly_placement_partial")
    assert warn and warn[0].level == "warn" and warn[0].evidence["unplaced"] == ["c.stl"]
    assert "only for the placed parts" in ac.summary_text(findings)


def test_no_placement_keeps_the_not_checked_statement(tmp_path):
    a = _box(tmp_path / "a.stl", 50, 50, 5)
    b = _box(tmp_path / "b.stl", 50, 50, 5)
    findings = ac.review([a, b], bed_mm=AD5X_BED)
    assert not _codes(findings, "assembly_no_collision")
    assert "Not checked: assembly transforms and collisions" in ac.summary_text(findings)


# ------------------------------------------------------- #1059: aligned solids

def test_identical_placements_collide(tmp_path):
    # Two copies of one part at the same placement share every face
    # plane: no vertex is strictly inside the other and no edge pierces
    # a face, but the overlap is total, so it must read as a collision.
    a = _box(tmp_path / "a.stl", 10, 10, 10)
    b = _box(tmp_path / "b.stl", 10, 10, 10)
    findings = ac.review([a, b], bed_mm=AD5X_BED, declarations={
        "a.stl": {"assembly_transform": {"translate_mm": [0, 0, 0]}},
        "b.stl": {"assembly_transform": {"translate_mm": [0, 0, 0]}}})
    hit = _codes(findings, "assembly_collision")
    assert hit and hit[0].level == "fail"
    assert not _codes(findings, "assembly_no_collision")


def test_aligned_overlap_sharing_face_planes_collides(tmp_path):
    # B overlaps A by 5 mm in x (500 mm^3) and shares A's y and z face
    # planes exactly, so every candidate witness lies on a boundary.
    # Before the #1059 fix this reported assembly_no_collision.
    a = _box(tmp_path / "a.stl", 10, 10, 10)
    b = _box(tmp_path / "b.stl", 10, 10, 10)
    findings = ac.review([a, b], bed_mm=AD5X_BED, declarations={
        "a.stl": {"assembly_transform": {"translate_mm": [0, 0, 0]}},
        "b.stl": {"assembly_transform": {"translate_mm": [5, 0, 0]}}})
    hit = _codes(findings, "assembly_collision")
    assert hit and hit[0].level == "fail"
    assert hit[0].evidence["overlap_mm"] == pytest.approx([5.0, 10.0, 10.0])
    assert not _codes(findings, "assembly_no_collision")


def test_partial_face_overlap_sharing_planes_collides(tmp_path):
    # B is offset in y as well, so the faces it shares with A overlap
    # only in part; the coplanar patches still have positive area and
    # both solids sit on the same side of them.
    a = _box(tmp_path / "a.stl", 10, 10, 10)
    b = _box(tmp_path / "b.stl", 10, 10, 10)
    findings = ac.review([a, b], bed_mm=AD5X_BED, declarations={
        "a.stl": {"assembly_transform": {"translate_mm": [0, 0, 0]}},
        "b.stl": {"assembly_transform": {"translate_mm": [0, 5, 0]}}})
    assert _codes(findings, "assembly_collision")
    assert not _codes(findings, "assembly_no_collision")


@pytest.mark.parametrize('depth', [0.005, 0.00001])
def test_overlap_inside_the_contact_band_is_unchecked_not_clearance(tmp_path, depth):
    # A 0.005 mm deep overlap is real but no deeper than the 0.01 mm
    # contact epsilon the check itself uses, so it cannot be told from
    # contact at this resolution: a warning, never a clearance.
    a = _box(tmp_path / "a.stl", 10, 10, 10)
    b = _box(tmp_path / "b.stl", 10, 10, 10)
    findings = ac.review([a, b], bed_mm=AD5X_BED, declarations={
        "a.stl": {"assembly_transform": {"translate_mm": [0, 0, 0]}},
        "b.stl": {"assembly_transform": {"translate_mm": [10 - depth, 0, 0]}}})
    warn = _codes(findings, "assembly_collision_unchecked")
    assert warn and warn[0].level == "warn"
    assert not _codes(findings, "assembly_collision")
    assert not _codes(findings, "assembly_no_collision")
