"""Golden adversarial support-coverage pack (testing-hardening pass, E4).

One truth-labeled pack for the support-coverage grader, instead of the
per-fix test files (#963, #968, #969) each re-deriving a slice of the
geometry. Every case names the shape, the G-code (or its absence) and
the verdict the grader MUST reach; the manifest test asserts the pack
keeps covering the full case list, so a case cannot quietly rot away
when a neighboring fix refactors its own file.

All fixtures here are SYNTHETIC (generated in this file from primitives)
and labeled as such — no real-printer archive data is used.

Truth labels were settled from the geometry, then confirmed against
``support_clearance`` on current main: a flat box on the bed has no
overhang; a floating slab's underside is one supportable_overhang
region; in a two-object file only the floating object earns a region;
a sloped underside is an overhang except where it meets the bed.
"""
import math
import zipfile
from pathlib import Path

from print_handy.core import support_clearance

SB = (0.0, 0.0, 9.0, 9.0)


# ------------------------------------------------------------ mesh helpers

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


def ramp():
    """A wedge whose underside slopes from z=6 down to the bed at one
    edge: cross-section (y, z) = (0, 6), (20, 0), (20, 6), extruded
    30 mm in x. The diagonal case."""
    a, b, c = (0.0, 6.0), (20.0, 0.0), (20.0, 6.0)  # (y, z)
    xs = (0.0, 30.0)

    def p(x, yz):
        return (x, yz[0], yz[1])

    quads = []
    for edge in ((a, b), (b, c), (c, a)):
        quads.append([p(xs[0], edge[0]), p(xs[1], edge[0]),
                      p(xs[1], edge[1]), p(xs[0], edge[1])])
    tris = []
    for q in quads:
        tris.append([q[0], q[1], q[2]])
        tris.append([q[0], q[2], q[3]])
    tris.append([p(xs[0], a), p(xs[0], b), p(xs[0], c)])
    tris.append([p(xs[1], a), p(xs[1], c), p(xs[1], b)])
    return _orient(tris, (15.0, 40 / 3, 4.0))


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


def _points_gcode(points, z, style="Support"):
    lines = [f';TYPE:{style}', f'G1 Z{z:.2f}']
    if points:
        lines.append(f'G1 X{points[0][0]:.2f} Y{points[0][1]:.2f}')
    lines += [f'G1 X{x:.2f} Y{y:.2f} E1.0' for x, y in points]
    lines.append(';TYPE:Outer wall')
    return '\n'.join(lines) + '\n'


def _overhangs(report):
    return [r for r in report['regions'] if r.get('kind') == 'supportable_overhang']


# ------------------------------------------------------------- the manifest

REQUIRED_CASES = {
    "flat_control", "floating_control", "l_shaped", "diagonal",
    "multi_object", "support_outside_box", "scaled_z_50",
    "multi_copy", "multi_plate", "tree_interface",
}


def test_pack_manifest_covers_every_required_case():
    covered = {name.split("::")[-1].removeprefix("test_case_")
               for name in globals()
               if name.startswith("test_case_")}
    assert REQUIRED_CASES <= covered, REQUIRED_CASES - covered


# ------------------------------------------------------- analyze-side cases

def test_case_flat_control(tmp_path):
    src = write_3mf(tmp_path / 'flat.3mf', [('Flat', box(0, 0, 0, 20, 20, 3))])
    rep = support_clearance.analyze(str(src))
    assert _overhangs(rep) == [] and support_clearance.risky(rep) is False


def test_case_floating_control(tmp_path):
    src = write_3mf(tmp_path / 'slab.3mf', [('Slab', box(0, 0, 5, 30, 30, 8))])
    rep = support_clearance.analyze(str(src))
    regions = _overhangs(rep)
    assert len(regions) == 1 and regions[0]['part'] == 'Slab'
    assert regions[0]['footprint'], 'the underside footprint is persisted for grading'


def test_case_l_shaped(tmp_path):
    tris = box(0, 0, 5, 30, 10, 8) + box(0, 10, 5, 10, 30, 8)
    src = write_3mf(tmp_path / 'ell.3mf', [('ELL', tris)])
    rep = support_clearance.analyze(str(src))
    regions = _overhangs(rep)
    assert len(regions) == 1
    # Grading covers the L's footprint cells only: support in the
    # missing quadrant (cells i >= 4 and j >= 4) must not be required.
    (bx0, by0), (bx1, by1) = regions[0]['bbox_mm']
    nx, ny = math.ceil((bx1 - bx0) / 3.0), math.ceil((by1 - by0) / 3.0)
    pts = [(bx0 + (i + 0.5) * 3.0, by0 + (j + 0.5) * 3.0)
           for i in range(nx) for j in range(ny) if i <= 3 or j <= 3]
    result = support_clearance.verify_support_coverage(
        rep['regions'], _points_gcode(pts, 4.8))
    assert result['ok'] is True and result['regions'][0]['graded'] == 'footprint'


def test_case_diagonal(tmp_path):
    src = write_3mf(tmp_path / 'ramp.3mf', [('Ramp', ramp())])
    rep = support_clearance.analyze(str(src))
    assert _overhangs(rep), 'a sloped underside is an overhang away from the bed edge'
    assert 'BELOW_BED_GEOMETRY' not in support_clearance.codes(rep)


def test_case_multi_object(tmp_path):
    src = write_3mf(tmp_path / 'two.3mf', [
        ('Grounded', box(0, 0, 0, 15, 15, 3)),
        ('Floater', box(40, 40, 5, 65, 65, 8)),
    ])
    rep = support_clearance.analyze(str(src))
    assert [r['part'] for r in _overhangs(rep)] == ['Floater']


# -------------------------------------------------------- verify-side cases

_REGION = {'kind': 'supportable_overhang', 'part': 'P',
           'bbox_mm': [[0.0, 0.0], [9.0, 9.0]], 'z_mm': {'min': 5.0, 'max': 5.0}}
_CELLS = [(1.5 + 3 * i, 1.5 + 3 * j) for i in range(3) for j in range(3)]


def test_case_support_outside_box():
    far = [(x + 100, y + 100) for x, y in _CELLS]
    result = support_clearance.verify_support_coverage(
        [dict(_REGION)], _points_gcode(far, 4.8))
    assert result['ok'] is False and result['regions'][0]['coverage'] == 0.0
    near = support_clearance.verify_support_coverage(
        [dict(_REGION)], _points_gcode(_CELLS, 4.8))
    assert near['ok'] is True and near['regions'][0]['coverage'] == 1.0


def test_case_scaled_z_50():
    region = {'kind': 'supportable_overhang', 'part': 'Tall',
              'bbox_mm': [[0.0, 0.0], [9.0, 9.0]],
              'z_mm': {'min': 20.0, 'max': 20.0}}
    gcode = _points_gcode(_CELLS, 9.8)
    unscaled = support_clearance.verify_support_coverage([dict(region)], gcode)
    assert unscaled['ok'] is False  # support at 9.8 cannot hold a 20 mm underside
    scaled = support_clearance.verify_support_coverage(
        [dict(region)], gcode, z_scale=0.5)
    assert scaled['ok'] is True  # at 50% Z the underside sits at 10 mm


def _copies_gcode(support_points):
    lines = ['; printing object Hook id:0 copy 0', ';TYPE:Outer wall',
             'G1 X0 Y0 E1', 'G1 X9 Y9 E1', '; stop printing object Hook',
             '; printing object Hook id:0 copy 1', ';TYPE:Outer wall',
             'G1 X30 Y0 E1', 'G1 X39 Y9 E1', '; stop printing object Hook',
             ';TYPE:Support', 'G1 Z4.8']
    if support_points:
        lines.append(f'G1 X{support_points[0][0]:.1f} Y{support_points[0][1]:.1f}')
    lines += [f'G1 X{x:.1f} Y{y:.1f} E1.0' for x, y in support_points]
    return '\n'.join(lines) + '\n'


def test_case_multi_copy():
    region = {'kind': 'supportable_overhang', 'object': 'Hook', 'part': 'Hook',
              'bbox_mm': [[0.0, 0.0], [9.0, 9.0]], 'z_mm': {'min': 5.0, 'max': 5.0}}
    objects = support_clearance.gcode_object_bounds(_copies_gcode(_CELLS))
    one = support_clearance.verify_support_coverage(
        [region], _copies_gcode(_CELLS), source_bounds=SB, object_bounds=objects)
    assert one['ok'] is False
    assert sorted(r['coverage'] for r in one['regions']) == [0.0, 1.0]
    both_pts = _CELLS + [(x + 30, y) for x, y in _CELLS]
    objects = support_clearance.gcode_object_bounds(_copies_gcode(both_pts))
    both = support_clearance.verify_support_coverage(
        [region], _copies_gcode(both_pts), source_bounds=SB, object_bounds=objects)
    assert both['ok'] is True


def test_case_multi_plate():
    def plate(name, bounds, support_points):
        (x0, y0), (x1, y1) = bounds
        lines = [f'; printing object {name}', ';TYPE:Outer wall',
                 f'G1 X{x0} Y{y0} E1', f'G1 X{x1} Y{y1} E1',
                 f'; stop printing object {name}', ';TYPE:Support', 'G1 Z4.8']
        lines += [f'G1 X{x:.1f} Y{y:.1f} E1.0' for x, y in support_points]
        return '\n'.join(lines) + '\n'

    alpha = {'kind': 'supportable_overhang', 'object': 'Alpha', 'part': 'Alpha',
             'bbox_mm': [[0.0, 0.0], [9.0, 9.0]], 'z_mm': {'min': 5.0, 'max': 5.0}}
    beta = dict(alpha, object='Beta', part='Beta')
    pts_b = [(x + 100, y + 100) for x, y in _CELLS]
    plates = [{'gcode': plate('Alpha', ((0, 0), (9, 9)), _CELLS), 'source_bounds': SB},
              {'gcode': plate('Beta', ((100, 100), (109, 109)), []), 'source_bounds': SB}]
    result = support_clearance.verify_coverage_by_plate([alpha, beta], plates)
    assert result['plate_mode'] == 'per_plate' and result['ok'] is False
    plates[1] = {'gcode': plate('Beta', ((100, 100), (109, 109)), pts_b),
                 'source_bounds': SB}
    result = support_clearance.verify_coverage_by_plate([alpha, beta], plates)
    assert result['ok'] is True


def test_case_tree_interface():
    region = {'kind': 'supportable_overhang', 'part': 'P',
              'bbox_mm': [[0.0, 0.0], [12.0, 3.0]],
              'z_mm': {'min': 10.0, 'max': 10.0}}
    gcode = ('; support_type = tree(auto)\n'
             ';TYPE:Support interface\nG1 Z9.8\nG1 X0 Y1.5\nG1 X6 Y1.5 E5\n'
             ';TYPE:Support\nG1 Z9.8\nG1 X0 Y1.5\nG1 X12 Y1.5 E5\n')
    result = support_clearance.verify_support_coverage([region], gcode)
    # Tree acceptance reads only the interface (half tiled here); the
    # branches reaching everywhere do not rescue it.
    assert result['ok'] is False
    assert result['regions'][0]['acceptance_basis'] == 'interface'
