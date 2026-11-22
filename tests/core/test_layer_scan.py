"""Layer scans for the print repair skill (#1777): islands and overhangs on generated meshes.

The real fixture (the 2026-11-21 couch organizer) is pinned in
tests/corpus/large_mesh/test_repair_fixture.py; these cases cover each rule
on meshes small enough to reason about: bed contact, floating solids
beside and inside a body, holes that must stay holes, overlapping bodies,
the 45 degree allowance on both sides of the threshold, open meshes that
must still scan, layer heights, limits, and native against Python welding.
"""
import contextlib
import math
import os

import numpy as np
import pytest

from print_handy.core import layer_scan, mesh_native

# Box topology over 8 corners: bottom 0-3 counterclockwise from above, top 4-7.
_QUADS = ((0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7))


def hexahedron(corners, inverted=False):
    """12 outward triangles of a box-like solid; ``inverted`` faces them inward (a cavity)."""
    corners = np.asarray(corners, dtype=float)
    tris = []
    for a, b, c, d in _QUADS:
        tris.append([corners[a], corners[b], corners[c]])
        tris.append([corners[a], corners[c], corners[d]])
    tris = np.asarray(tris)
    return tris[:, ::-1] if inverted else tris


def box(x0, y0, z0, x1, y1, z1, inverted=False):
    return hexahedron([(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
                       (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)], inverted)


def leaning(dx, height=10.0, width=10.0, depth=20.0):
    """A prism whose +X wall leans out by ``dx`` over ``height`` (slope atan(height/dx))."""
    return hexahedron([(0, 0, 0), (width, 0, 0), (width, depth, 0), (0, depth, 0),
                       (0, 0, height), (width + dx, 0, height), (width + dx, depth, height),
                       (0, depth, height)])


def mesh(*parts):
    return layer_scan.weld(np.concatenate(parts))


@contextlib.contextmanager
def impl(value):
    old = os.environ.get('PRINT_HANDY_BOUNDS_IMPL')
    os.environ['PRINT_HANDY_BOUNDS_IMPL'] = value
    mesh_native._reset_cache()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop('PRINT_HANDY_BOUNDS_IMPL', None)
        else:
            os.environ['PRINT_HANDY_BOUNDS_IMPL'] = old
        mesh_native._reset_cache()


def test_a_box_on_the_bed_has_no_islands_or_overhangs():
    m = mesh(box(0, 0, 0, 20, 10, 5))
    islands = layer_scan.island_scan(m, 0.2)
    assert islands['islands'] == []
    assert islands['layers'] == 25
    assert islands['open_layers'] == []
    assert layer_scan.overhang_scan(m, 0.2)['overhangs'] == []


def test_a_floating_box_beside_a_body_is_an_island_at_its_bottom():
    m = mesh(box(0, 0, 0, 10, 10, 20), box(20, 0, 5, 30, 8, 7))
    found = layer_scan.island_scan(m, 0.2)['islands']
    assert len(found) == 1
    island = found[0]
    assert island['z'] == pytest.approx(5.0)
    assert island['area_mm2'] == pytest.approx(80.0, rel=0.02)
    assert island['bounds'] == pytest.approx([20, 0, 30, 8], abs=0.06)
    overhangs = layer_scan.overhang_scan(m, 0.2)['overhangs']
    assert [o['floating'] for o in overhangs] == [True]
    assert overhangs[0]['z_min'] == pytest.approx(5.0)


def test_a_solid_floating_inside_a_cavity_is_an_island():
    shell = np.concatenate((box(0, 0, 0, 30, 30, 20), box(5, 5, 2, 25, 25, 18, inverted=True)))
    m = mesh(shell, box(10, 10, 8, 20, 20, 12))
    found = layer_scan.island_scan(m, 0.2)['islands']
    # The cavity's ceiling at Z 18 rests on the walls; only the inner solid floats.
    assert [round(i['z'], 1) for i in found] == [8.0]
    assert found[0]['area_mm2'] == pytest.approx(100.0, rel=0.02)


def test_a_hole_stays_a_hole():
    """A tube's bore is not filled, and a lid over it is a supported bridge, not an island."""
    tube = np.concatenate((box(0, 0, 0, 20, 20, 10), box(5, 5, 0, 15, 15, 10, inverted=True)))
    lid = box(0, 0, 10, 20, 20, 12)
    m = mesh(tube, lid)
    assert layer_scan.island_scan(m, 0.2)['islands'] == []
    overhangs = layer_scan.overhang_scan(m, 0.2)['overhangs']
    assert len(overhangs) == 1
    bridge = overhangs[0]
    assert bridge['z_min'] == pytest.approx(10.0)
    assert not bridge['floating']
    # The bore minus the 45 degree allowance all round.
    assert bridge['area_mm2'] == pytest.approx((10 - 2 * 0.25) ** 2, rel=0.05)


def test_overlapping_bodies_union_like_a_slicer():
    """A shelf box that overlaps its column is one solid: no island, one cantilever."""
    m = mesh(box(0, 0, 0, 10, 10, 20), box(-1, 0, 9, 30, 10, 11))
    assert layer_scan.island_scan(m, 0.2)['islands'] == []
    overhangs = layer_scan.overhang_scan(m, 0.2)['overhangs']
    big = [o for o in overhangs if o['area_mm2'] > 50]
    assert len(big) == 1
    assert big[0]['z_min'] == pytest.approx(9.0)
    assert big[0]['bounds'][0] == pytest.approx(10.25, abs=0.06)
    assert big[0]['bounds'][2] == pytest.approx(30.0, abs=0.06)


@pytest.mark.parametrize('angle,expected', [(60.0, 0), (30.0, 1)])
def test_the_45_degree_allowance(angle, expected):
    m = mesh(leaning(10.0 / math.tan(math.radians(angle))))
    overhangs = layer_scan.overhang_scan(m, 0.2)['overhangs']
    assert len(overhangs) == expected
    if expected:
        ramp = overhangs[0]
        assert ramp['layers'] >= 45
        assert ramp['z_min'] == pytest.approx(0.2)
        assert not ramp['floating']
    assert layer_scan.island_scan(m, 0.2)['islands'] == []


def test_the_angle_is_a_parameter():
    """A 60 degree wall holds itself at the 45 degree default, not at an 80 degree threshold."""
    m = mesh(leaning(10.0 / math.tan(math.radians(60.0))))
    assert not layer_scan.overhang_scan(m, 0.2, angle_deg=45.0)['overhangs']
    assert layer_scan.overhang_scan(m, 0.2, angle_deg=80.0, min_area_mm2=0.1)['overhangs']


def test_an_open_mesh_still_scans_and_says_where_it_is_open():
    """#1782 aborted the whole scan on a contour that did not close."""
    tris = np.concatenate((box(0, 0, 0, 10, 10, 10), box(20, 0, 4, 30, 10, 6)))
    open_tris = np.delete(tris, 6, axis=0)  # one triangle of the first box's +X wall
    result = layer_scan.island_scan(layer_scan.weld(open_tris), 0.2)
    assert result['open_layers']
    assert [round(i['z'], 1) for i in result['islands']] == [4.0]


def test_a_shared_edge_on_a_scan_row_stays_closed():
    """The column's wall diagonal crosses Z 5.15 at Y 2.575, exactly a scan row's
    center: both faces must cut it at the same point, or that row's winding
    never closes and joins the column to the floating box."""
    m = mesh(box(0, 0, 0, 10, 10, 20), box(20, 0, 5.0, 30, 10, 6))
    result = layer_scan.island_scan(m, 0.3, first_layer_mm=0.2)
    assert result['open_layers'] == []
    assert len(result['islands']) == 1


def test_layer_heights_set_the_planes():
    m = mesh(box(0, 0, 0, 10, 10, 20), box(20, 0, 5.0, 30, 10, 6))
    result = layer_scan.island_scan(m, 0.3, first_layer_mm=0.2)
    assert result['islands'][0]['z'] == pytest.approx(5.0)
    assert result['islands'][0]['sample_z'] == pytest.approx(5.15)
    assert result['layers'] == 1 + math.ceil((20 - 0.2) / 0.3)


def test_z_is_measured_from_the_lowest_point():
    m = mesh(box(0, 0, 50, 10, 10, 70), box(20, 0, 55, 30, 10, 56))
    assert layer_scan.island_scan(m, 0.2)['islands'][0]['z'] == pytest.approx(5.0)


@pytest.mark.parametrize('kwargs,message', [
    ({'layer_mm': 0}, 'layer height'),
    ({'layer_mm': float('nan')}, 'layer height'),
    ({'first_layer_mm': -1}, 'first layer'),
    ({'pitch_mm': 0}, 'pitch'),
])
def test_bad_parameters_are_refused(kwargs, message):
    m = mesh(box(0, 0, 0, 1, 1, 1))
    args = {'layer_mm': 0.2, **kwargs}
    with pytest.raises(layer_scan.ScanError, match=message):
        layer_scan.island_scan(m, args.pop('layer_mm'), **args)


def test_bad_angle_is_refused():
    with pytest.raises(layer_scan.ScanError, match='angle'):
        layer_scan.overhang_scan(mesh(box(0, 0, 0, 1, 1, 1)), 0.2, angle_deg=90)


def test_limits_refuse_with_a_reason(monkeypatch):
    m = mesh(box(0, 0, 0, 10, 10, 10))
    monkeypatch.setattr(layer_scan, 'MAX_LAYERS', 10)
    with pytest.raises(layer_scan.ScanError, match='layer limit'):
        layer_scan.island_scan(m, 0.2)
    monkeypatch.setattr(layer_scan, 'MAX_LAYERS', 20_000)
    monkeypatch.setattr(layer_scan, 'MAX_FACE_LAYER_PAIRS', 10)
    with pytest.raises(layer_scan.ScanError, match='face-layer pairs'):
        layer_scan.island_scan(m, 0.2)
    monkeypatch.setattr(layer_scan, 'MAX_FACE_LAYER_PAIRS', 10**9)
    monkeypatch.setattr(layer_scan, 'MAX_LAYER_INTERVALS', 10)
    with pytest.raises(layer_scan.ScanError, match='row crossings'):
        layer_scan.island_scan(m, 0.2)


def test_an_empty_or_non_finite_mesh_is_refused():
    with pytest.raises(layer_scan.ScanError):
        layer_scan.island_scan(layer_scan.Mesh(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)))
    tris = box(0, 0, 0, 1, 1, 1)
    tris[0, 0, 0] = np.inf
    with pytest.raises(layer_scan.ScanError, match='non-finite'):
        layer_scan.island_scan(layer_scan.Mesh(tris.reshape(-1, 3), np.arange(36).reshape(-1, 3)))


def test_welding_joins_corners_on_the_sixth_decimal():
    tris = box(0, 0, 0, 1, 1, 1)
    tris[1, 0] += 1e-8  # a near-duplicate corner, as STL exporters write them
    m = layer_scan.weld(tris)
    assert len(m.vertices) == 8
    assert m.faces.shape == (12, 3)


def _generated():
    rng = np.random.default_rng(1777)
    parts = [box(0, 0, 0, 10, 10, 20), box(20, 0, 5, 30, 8, 7), leaning(12.0)]
    tris = np.concatenate(parts)
    jitter = rng.uniform(-4e-7, 4e-7, size=tris.shape)
    return tris + jitter


def test_python_welding_is_the_oracle():
    tris = _generated()
    with impl('python'):
        m = layer_scan.weld(tris)
    keys, order, ids = {}, [], []
    for point in tris.reshape(-1, 3).tolist():
        key = tuple(round(v, 6) for v in point)
        if key not in keys:
            keys[key] = len(order)
            order.append(key)
        ids.append(keys[key])
    assert m.vertices.tolist() == [list(k) for k in order]
    assert m.faces.reshape(-1).tolist() == ids


def test_native_welding_and_scans_match_python():
    tris = _generated()
    with impl('auto'):
        if not mesh_native.available():
            pytest.skip('native mesh kernel not built')
        native = layer_scan.weld(tris)
        native_scan = layer_scan.overhang_scan(native, 0.2)
    with impl('python'):
        python = layer_scan.weld(tris)
        python_scan = layer_scan.overhang_scan(python, 0.2)
    assert np.array_equal(native.vertices, python.vertices)
    assert np.array_equal(native.faces, python.faces)
    assert native_scan == python_scan


def test_load_reads_stl_and_3mf_alike(tmp_path):
    from print_handy.core import geometry_layout, support_clearance
    column, floating = box(0, 0, 0, 10, 10, 20), box(20, 0, 5, 30, 8, 7)
    stl = tmp_path / 'part.stl'
    support_clearance._write_binary_stl(stl, np.concatenate((column, floating)))
    threemf = tmp_path / 'part.3mf'
    geometry_layout.write_geometry_only_3mf(
        [{'name': 'column', 'triangles': column.tolist()},
         {'name': 'floating', 'triangles': floating.tolist()}], threemf)
    for path in (stl, threemf):
        found = layer_scan.island_scan(layer_scan.load(path), 0.2)['islands']
        assert [round(i['z'], 1) for i in found] == [5.0], path.name
