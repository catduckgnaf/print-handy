"""Issue #963: support coverage must grade underside footprints, not boxes.

The real Pixel_Patina archive is not in the repo, so these fixtures are
synthetic equivalents matching the issue's stated numbers and geometry
conditions (an underside of 33.5 mm2 inside a much larger bounding box,
10 of 20 bounding-box cells holding underside, tree(auto) supports).
"""
import math
import struct
import zipfile
from pathlib import Path

from print_handy.core import support_clearance

SB = (0.0, 0.0, 9.0, 9.0)


# ---------------------------------------------------------------- mesh helpers

def _orient(tris, center):
    import numpy as np
    out = []
    c = np.array(center, float)
    for tri in tris:
        t = np.array(tri, float)
        n = np.cross(t[1] - t[0], t[2] - t[0])
        if np.dot(n, t.mean(0) - c) < 0:
            t = t[::-1]
        out.append([tuple(v) for v in t])
    return out


def box(x0, y0, z0, x1, y1, z1):
    quads = [
        [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)],
        [(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)],
        [(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)],
        [(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)],
        [(x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1)],
        [(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)],
    ]
    tris = []
    for q in quads:
        tris.append([q[0], q[1], q[2]])
        tris.append([q[0], q[2], q[3]])
    return _orient(tris, ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2))


def write_3mf(path, objects):
    objs, items = [], []
    for i, (name, tris) in enumerate(objects):
        oid = str(i + 2)
        verts, seen, faces = [], {}, []
        for tri in tris:
            face = []
            for v in tri:
                key = tuple(round(float(c), 6) for c in v)
                if key not in seen:
                    seen[key] = len(verts)
                    verts.append(key)
                face.append(seen[key])
            faces.append(face)
        vxml = ''.join(f'<vertex x="{v[0]}" y="{v[1]}" z="{v[2]}"/>' for v in verts)
        txml = ''.join(f'<triangle v1="{f[0]}" v2="{f[1]}" v3="{f[2]}"/>' for f in faces)
        objs.append(f'<object id="{oid}" type="model" name="{name}">'
                    f'<mesh><vertices>{vxml}</vertices><triangles>{txml}</triangles></mesh></object>')
        items.append(f'<item objectid="{oid}"/>')
    model = ('<?xml version="1.0" encoding="utf-8"?>'
             '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" unit="millimeter">'
             f'<resources>{"".join(objs)}</resources><build>{"".join(items)}</build></model>')
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('3D/3dmodel.model', model)
    return path


def _points_gcode(points, z):
    lines = [';TYPE:Support', f'G1 Z{z:.2f}']
    lines += [f'G1 X{x:.2f} Y{y:.2f} E1.0' for x, y in points]
    lines.append(';TYPE:Outer wall')
    return '\n'.join(lines) + '\n'


# ------------------------------------------------------- item 1: footprint

def test_issue_numbers_footprint_grades_covered_when_bbox_would_fail():
    # The issue's shape in miniature: a 12 x 15 bbox (20 cells of 3 mm)
    # whose underside occupies only the left half: 10 footprint cells.
    footprint = [[x + 0.5, y + 0.5, 1.0] for x in range(6) for y in range(15)]
    region = {'kind': 'supportable_overhang', 'part': 'Hook',
              'bbox_mm': [[0.0, 0.0], [12.0, 15.0]], 'z_mm': {'min': 1.2, 'max': 1.2},
              'footprint': footprint, 'footprint_mm': 1.0}
    pts = [(1.5 + 3 * i, 1.5 + 3 * j) for i in range(2) for j in range(5)]
    result = support_clearance.verify_support_coverage([region], _points_gcode(pts, 1.0))
    assert result['ok'] is True
    entry = result['regions'][0]
    assert entry['graded'] == 'footprint'
    assert entry['total_cells'] == 10 and entry['covered_cells'] == 10
    assert entry['coverage'] == 1.0
    # Support only in the empty half of the bbox covers no footprint cell.
    pts = [(7.5 + 3 * i, 1.5 + 3 * j) for i in range(2) for j in range(5)]
    result = support_clearance.verify_support_coverage([region], _points_gcode(pts, 1.0))
    assert result['ok'] is False and result['regions'][0]['coverage'] == 0.0


def test_analyze_persists_footprint_and_l_shape_grades_by_it(tmp_path):
    tris = box(0, 0, 5, 30, 10, 8) + box(0, 10, 5, 10, 30, 8)   # one L-shaped mesh
    src = write_3mf(tmp_path / 'ell.3mf', [('ELL', tris)])
    rep = support_clearance.analyze(str(src))
    regions = [r for r in rep['regions'] if r.get('kind') == 'supportable_overhang']
    assert len(regions) == 1
    region = regions[0]
    assert region['footprint'], 'the region must persist its underside footprint'
    assert region['footprint_mm'] >= 1.0
    assert len(region['footprint']) <= support_clearance.FOOTPRINT_MAX_CELLS
    # Support toolpaths in every 3 mm grading cell the L touches, none in
    # the missing quadrant (grading cells i >= 4 and j >= 4).
    (bx0, by0), (bx1, by1) = region['bbox_mm']
    nx = math.ceil((bx1 - bx0) / 3.0)
    ny = math.ceil((by1 - by0) / 3.0)
    pts = [(bx0 + (i + 0.5) * 3.0, by0 + (j + 0.5) * 3.0)
           for i in range(nx) for j in range(ny) if i <= 3 or j <= 3]
    result = support_clearance.verify_support_coverage(rep['regions'], _points_gcode(pts, 4.8))
    assert result['ok'] is True and result['regions'][0]['coverage'] == 1.0
    assert result['regions'][0]['graded'] == 'footprint'


# ------------------------------------------- item 2: low_clearance targets

def test_unflagged_low_clearance_region_is_not_a_coverage_target():
    small = {'kind': 'low_clearance', 'part': 'small', 'flagged': False,
             'bbox_mm': [[0.0, 0.0], [9.0, 9.0]], 'z_mm': {'min': 0.5, 'max': 0.5}}
    result = support_clearance.verify_support_coverage([small], 'G1 X1 Y1 E1\n')
    assert result['checked'] is False and result['ok'] is True
    assert [e['part'] for e in result['excluded_targets']] == ['small']

    big = {'kind': 'low_clearance', 'part': 'big', 'flagged': True,
           'bbox_mm': [[0.0, 0.0], [9.0, 9.0]], 'z_mm': {'min': 0.5, 'max': 0.5}}
    result = support_clearance.verify_support_coverage([small, big], 'G1 X1 Y1 E1\n')
    assert result['checked'] is True and result['ok'] is False
    assert [r['part'] for r in result['regions']] == ['big']


def test_analyze_marks_low_clearance_flagged_only_when_it_is_a_finding(tmp_path):
    small = write_3mf(tmp_path / 'small.3mf', [('S', box(0, 0, 0.5, 10, 10, 3))])
    rep = support_clearance.analyze(str(small))
    lows = [r for r in rep['regions'] if r.get('kind') == 'low_clearance']
    assert lows and all(r['flagged'] is False for r in lows)

    big = write_3mf(tmp_path / 'big.3mf', [('B', box(0, 0, 0.5, 60, 40, 3))])
    rep = support_clearance.analyze(str(big))
    lows = [r for r in rep['regions'] if r.get('kind') == 'low_clearance']
    assert lows and all(r['flagged'] is True for r in lows)


# ------------------------------------------------------- item 3: copies

def _copies_gcode(support_points):
    lines = ['; printing object Hook id:0 copy 0', ';TYPE:Outer wall',
             'G1 X0 Y0 E1', 'G1 X9 Y9 E1', '; stop printing object Hook',
             '; printing object Hook id:0 copy 1', ';TYPE:Outer wall',
             'G1 X30 Y0 E1', 'G1 X39 Y9 E1', '; stop printing object Hook',
             ';TYPE:Support', 'G1 Z4.8']
    if support_points:
        # Travel (no extrusion) to the first point, so chained segments
        # between points never drag a diagonal across the other copy.
        lines.append(f'G1 X{support_points[0][0]:.1f} Y{support_points[0][1]:.1f}')
    lines += [f'G1 X{x:.1f} Y{y:.1f} E1.0' for x, y in support_points]
    return '\n'.join(lines) + '\n'


def test_copies_grade_per_instance_not_against_the_union_bounds():
    region = {'kind': 'supportable_overhang', 'object': 'Hook', 'part': 'Hook',
              'bbox_mm': [[0.0, 0.0], [9.0, 9.0]], 'z_mm': {'min': 5.0, 'max': 5.0}}
    one = [(1.5 + 3 * i, 1.5 + 3 * j) for i in range(3) for j in range(3)]
    both = one + [(x + 30, y) for x, y in one]
    objects = support_clearance.gcode_object_bounds(_copies_gcode(both))
    assert [o['bounds'] for o in objects] == [(0.0, 0.0, 9.0, 9.0), (30.0, 0.0, 39.0, 9.0)]

    # The old union mapping stretches the target across the gap between
    # copies and fails even though both copies are fully supported.
    union = support_clearance.verify_support_coverage(
        [region], _copies_gcode(both), source_bounds=SB,
        gcode_bounds=support_clearance.gcode_model_bounds(_copies_gcode(both)))
    assert union['ok'] is False

    per_copy = support_clearance.verify_support_coverage(
        [region], _copies_gcode(both), source_bounds=SB, object_bounds=objects)
    assert per_copy['ok'] is True
    assert len(per_copy['regions']) == 2
    assert all(r['coverage'] == 1.0 for r in per_copy['regions'])

    # One unsupported copy is enough to fail.
    half = support_clearance.verify_support_coverage(
        [region], _copies_gcode(one), source_bounds=SB, object_bounds=objects)
    assert half['ok'] is False
    assert min(r['coverage'] for r in half['regions']) == 0.0


# ------------------------------------------- item 5: recorded scale mapping

def test_recorded_scale_and_placement_beat_bounds_inference():
    region = {'kind': 'supportable_overhang', 'part': 'P',
              'bbox_mm': [[0.0, 0.0], [12.0, 3.0]], 'z_mm': {'min': 20.0, 'max': 20.0}}
    gcode = ';TYPE:Support\nG1 Z19.8\nG1 X50 Y51.5\nG1 X62 Y51.5 E5\n'
    kw = {'source_bounds': (0.0, 0.0, 12.0, 3.0), 'gcode_bounds': (0.0, 0.0, 24.0, 6.0)}
    inferred = support_clearance.verify_support_coverage([region], gcode, **kw)
    assert inferred['ok'] is False and inferred['scale_source'] == 'bounds'
    recorded = support_clearance.verify_support_coverage(
        [region], gcode, recorded_scale=1.0, placement_mm=(50.0, 50.0), **kw)
    assert recorded['ok'] is True and recorded['scale_source'] == 'recorded'


# ------------------------------------------- item 6: tree support acceptance

def _tree_gcode(header):
    return (header + '\n;TYPE:Support interface\nG1 Z9.8\nG1 X0 Y1.5\nG1 X6 Y1.5 E5\n'
            ';TYPE:Support\nG1 Z9.8\nG1 X0 Y1.5\nG1 X12 Y1.5 E5\n')


def test_tree_supports_grade_on_interface_grid_supports_on_all_support():
    region = {'kind': 'supportable_overhang', 'part': 'P',
              'bbox_mm': [[0.0, 0.0], [12.0, 3.0]], 'z_mm': {'min': 10.0, 'max': 10.0}}
    # The interface tiles only half the underside; the branches reach all
    # of it. Tree acceptance reads the interface (what touches the part);
    # grid acceptance reads every support path.
    tree = support_clearance.verify_support_coverage(
        [region], _tree_gcode('; support_type = tree(auto)'))
    assert tree['ok'] is False
    assert tree['regions'][0]['acceptance_basis'] == 'interface'
    assert tree['regions'][0]['support_style'] == 'tree'
    grid = support_clearance.verify_support_coverage(
        [region], _tree_gcode('; support_type = normal(auto)'))
    assert grid['ok'] is True
    assert grid['regions'][0]['acceptance_basis'] == 'all_support'


# ------------------------------- items 4 + 7: per-plate grading, visibility

def _plate_text(name, bounds, support_points):
    (x0, y0), (x1, y1) = bounds
    lines = [f'; printing object {name}', ';TYPE:Outer wall',
             f'G1 X{x0} Y{y0} E1', f'G1 X{x1} Y{y1} E1', f'; stop printing object {name}',
             ';TYPE:Support', 'G1 Z4.8']
    lines += [f'G1 X{x:.1f} Y{y:.1f} E1.0' for x, y in support_points]
    return '\n'.join(lines) + '\n'


def test_multi_plate_grades_each_plate_with_its_own_support():
    alpha = {'kind': 'supportable_overhang', 'object': 'Alpha', 'part': 'Alpha',
             'bbox_mm': [[0.0, 0.0], [9.0, 9.0]], 'z_mm': {'min': 5.0, 'max': 5.0}}
    beta = {'kind': 'supportable_overhang', 'object': 'Beta', 'part': 'Beta',
             'bbox_mm': [[0.0, 0.0], [9.0, 9.0]], 'z_mm': {'min': 5.0, 'max': 5.0}}
    pts_a = [(1.5 + 3 * i, 1.5 + 3 * j) for i in range(3) for j in range(3)]
    pts_b = [(x + 100, y + 100) for x, y in pts_a]
    plate1 = _plate_text('Alpha', ((0, 0), (9, 9)), pts_a)
    plate2 = _plate_text('Beta', ((100, 100), (109, 109)), [])
    plates = [{'gcode': plate1, 'source_bounds': SB}, {'gcode': plate2, 'source_bounds': SB}]
    result = support_clearance.verify_coverage_by_plate([alpha, beta], plates)
    assert result['plate_mode'] == 'per_plate'
    assert result['ok'] is False
    by_part = {r['part']: r for r in result['regions']}
    assert by_part['Alpha']['coverage'] == 1.0 and by_part['Alpha']['plate'] == 1
    assert by_part['Beta']['coverage'] == 0.0 and by_part['Beta']['plate'] == 2

    # Support for Beta drawn in plate 1's file does not rescue plate 2.
    plate1b = _plate_text('Alpha', ((0, 0), (9, 9)), pts_a + pts_b)
    plates = [{'gcode': plate1b, 'source_bounds': SB}, {'gcode': plate2, 'source_bounds': SB}]
    result = support_clearance.verify_coverage_by_plate([alpha, beta], plates)
    assert result['ok'] is False

    plates = [{'gcode': plate1, 'source_bounds': SB},
              {'gcode': _plate_text('Beta', ((100, 100), (109, 109)), pts_b), 'source_bounds': SB}]
    result = support_clearance.verify_coverage_by_plate([alpha, beta], plates)
    assert result['ok'] is True


def test_unreadable_plate_is_named_unverified_not_a_silent_pass(tmp_path):
    region = {'kind': 'supportable_overhang', 'object': 'Alpha', 'part': 'Alpha',
              'bbox_mm': [[0.0, 0.0], [9.0, 9.0]], 'z_mm': {'min': 5.0, 'max': 5.0}}
    missing = tmp_path / 'plate_9.gcode'
    result = support_clearance.verify_coverage_by_plate(
        [region], [{'gcode': str(missing), 'source_bounds': SB}])
    assert result['ok'] is True and result['checked'] is False
    assert len(result['unverified']) == 1
    assert result['unverified'][0]['plate'] == 1
    assert 'plate_9.gcode' in result['unverified'][0]['reason']
