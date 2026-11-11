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


# ---------------------------------------------------------------- #1195 audit: concave and contained parts

def _prism(outer, inner, height, z0=0.0):
    """A closed, outward-wound prism over a CCW polygon, with an optional same-count CCW hole."""
    tris = []
    n = len(outer)
    rings = [(outer, 1)]
    if inner is None:
        for i in range(1, n - 1):
            a, b, c = outer[0], outer[i], outer[i + 1]
            tris.append([(a[0], a[1], z0), (c[0], c[1], z0), (b[0], b[1], z0)])
            tris.append([(a[0], a[1], z0 + height), (b[0], b[1], z0 + height), (c[0], c[1], z0 + height)])
    else:
        assert len(inner) == n
        rings.append((inner, -1))
        for i in range(n):
            o0, o1, i0, i1 = outer[i], outer[(i + 1) % n], inner[i], inner[(i + 1) % n]
            for z, top in ((z0, False), (z0 + height, True)):
                quad = [(o0[0], o0[1], z), (o1[0], o1[1], z), (i1[0], i1[1], z), (i0[0], i0[1], z)]
                pair = [[quad[0], quad[1], quad[2]], [quad[0], quad[2], quad[3]]]
                tris += pair if top else [tri[::-1] for tri in pair]
    for ring, sign in rings:
        for i in range(len(ring)):
            p, q = ring[i], ring[(i + 1) % len(ring)]
            a, b = (p[0], p[1], z0), (q[0], q[1], z0)
            c, d = (q[0], q[1], z0 + height), (p[0], p[1], z0 + height)
            tris += [[a, b, c], [a, c, d]] if sign > 0 else [[a, c, b], [a, d, c]]
    return tris


def _circle(radius, n=24, turn=0.0):
    return [(radius * math.cos(2 * math.pi * i / n + turn), radius * math.sin(2 * math.pi * i / n + turn))
            for i in range(n)]


def _placed_pair(tmp_path, a_tris, b_tris, b_transform=None):
    a, b = tmp_path / "a.stl", tmp_path / "b.stl"
    write_binary_stl(a_tris, a)
    write_binary_stl(b_tris, b)
    return ac.review([a, b], bed_mm=AD5X_BED, declarations={
        "a.stl": {"assembly_transform": {"translate_mm": [0, 0, 0]}},
        "b.stl": {"assembly_transform": b_transform or {"translate_mm": [0, 0, 0]}}})


@pytest.mark.parametrize("peg_radius, turn, collides", [
    (4.8, 0.0, False),                # 0.15 mm clearance to the bore's flats: no collision
    (5.3, 0.0, True),                 # 0.3 mm interference, polygons aligned
    (5.3, math.pi / 24, True),        # the same, peg facets turned half a step
    (5.05, math.pi / 24, True),       # 0.05 mm interference, still past the contact band
])
def test_peg_through_a_bore_is_judged_by_the_material_not_the_bounds(tmp_path, peg_radius, turn, collides):
    # A peg through a tube's bore: the placed bounds always overlap, and a
    # clearance fit has no point of either solid inside the other, while an
    # interference fit puts peg material inside the tube wall.
    findings = _placed_pair(tmp_path, _prism(_circle(10), _circle(5), 20),
                            _prism(_circle(peg_radius, turn=turn), None, 30, z0=-5))
    assert bool(_codes(findings, "assembly_collision")) is collides
    assert bool(_codes(findings, "assembly_no_collision")) is not collides
    assert not _codes(findings, "assembly_collision_unchecked")


def test_a_part_wholly_inside_another_collides(tmp_path):
    findings = _placed_pair(tmp_path, _prism(_circle(20), None, 20), _prism(_circle(3), None, 5, z0=5))
    assert _codes(findings, "assembly_collision")


def test_rotated_placement_collides_through_its_turned_geometry(tmp_path):
    bar = _prism([(0, 0), (100, 0), (100, 10), (0, 10)], None, 5)
    crossing = _placed_pair(tmp_path, bar, bar, {"translate_mm": [50, -40, 0], "rotate_deg": [0, 0, 45]})
    assert _codes(crossing, "assembly_collision")
    # Turned 90 degrees and moved clear, the same bar is apart from the first.
    apart = _placed_pair(tmp_path, bar, bar, {"translate_mm": [120, 0, 0], "rotate_deg": [0, 0, 90]})
    assert _codes(apart, "assembly_no_collision") and not _codes(apart, "assembly_collision")
