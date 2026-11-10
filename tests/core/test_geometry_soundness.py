"""Geometry invariants for sizing, rotating, mirroring and support screening.

Each test builds an exact shape (a box, a right-triangle prism, a 45 degree
wedge), runs the production transform or measurement on it, and checks the
result against the value derived by hand. No slicer runs here.
"""
import math
import struct
import zipfile

import numpy as np
import pytest

from print_handy.core import edit_plan, geometry_layout, orca_adapter, support_clearance

BED = (220.0, 220.0)


def box(dx, dy, dz, origin=(0.0, 0.0, 0.0)):
    """Outward-wound box triangles."""
    ox, oy, oz = origin
    v = [(ox + x, oy + y, oz + z) for x, y, z in
         [(0, 0, 0), (dx, 0, 0), (dx, dy, 0), (0, dy, 0), (0, 0, dz), (dx, 0, dz), (dx, dy, dz), (0, dy, dz)]]
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
         (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return [[v[a], v[b], v[c]] for a, b, c in f]


def prism_xy(poly, z0, z1):
    """Extrude a counterclockwise XY polygon along Z, wound outward."""
    n = len(poly)
    tris = []
    for i in range(1, n - 1):
        tris.append([(*poly[0], z0), (*poly[i + 1], z0), (*poly[i], z0)])
        tris.append([(*poly[0], z1), (*poly[i], z1), (*poly[i + 1], z1)])
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        tris += [[(*a, z0), (*b, z0), (*b, z1)], [(*a, z0), (*b, z1), (*a, z1)]]
    return tris


def wedge45(length=40.0, run=20.0, width=30.0):
    """A wedge whose sloped underside rises 45 degrees from the plate edge (XZ section, extruded on Y)."""
    poly = [(0.0, 0.0), (run, run), (run + length, run), (run + length, run + 5.0), (0.0, run + 5.0)]
    # the sloped face is the edge (0,0)-(run,run); the section is closed by the
    # top edge, so the part touches the plate only along x = 0.
    tris = []
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        q = [(a[0], 0.0, a[1]), (a[0], width, a[1]), (b[0], width, b[1]), (b[0], 0.0, b[1])]
        tris += [[q[0], q[1], q[2]], [q[0], q[2], q[3]]]
    for i in range(1, n - 1):
        tris.append([(poly[0][0], 0.0, poly[0][1]), (poly[i][0], 0.0, poly[i][1]), (poly[i + 1][0], 0.0, poly[i + 1][1])])
        tris.append([(poly[0][0], width, poly[0][1]), (poly[i + 1][0], width, poly[i + 1][1]), (poly[i][0], width, poly[i][1])])
    return _outward(tris)


def _outward(tris):
    t = np.asarray(tris, float)
    if _volume(t) < 0:
        t = t[:, ::-1]
    return [[tuple(p) for p in tri] for tri in t]


def _volume(tris):
    t = np.asarray([[tuple(map(float, p)) for p in tri] for tri in tris], float).reshape(-1, 3, 3)
    return float(np.einsum('ij,ij->i', t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6)


def _extent(tris):
    t = np.asarray([[tuple(map(float, p)) for p in tri] for tri in tris], float).reshape(-1, 3)
    return t.min(0), t.max(0)


def write_stl(path, tris, header=b''):
    with open(path, 'wb') as fh:
        fh.write(header.ljust(80, b'\0')[:80])
        fh.write(struct.pack('<I', len(tris)))
        for tri in tris:
            fh.write(struct.pack('<12fH', 0, 0, 0, *(float(c) for p in tri for c in p), 0))
    return path


def write_3mf(path, tris, unit='millimeter', item_tf=None):
    verts, faces, seen = [], [], {}
    for tri in tris:
        face = []
        for p in tri:
            key = tuple(float(c) for c in p)
            if key not in seen:
                seen[key] = len(verts)
                verts.append(key)
            face.append(seen[key])
        faces.append(face)
    vx = ''.join(f'<vertex x="{a!r}" y="{b!r}" z="{c!r}"/>' for a, b, c in verts)
    tx = ''.join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces)
    tr = f' transform="{item_tf}"' if item_tf else ''
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           f'<model unit="{unit}" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
           f'<resources><object id="1" type="model" name="part"><mesh><vertices>{vx}</vertices>'
           f'<triangles>{tx}</triangles></mesh></object></resources>'
           f'<build><item objectid="1"{tr}/></build></model>')
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('3D/3dmodel.model', xml)
    return path


def test_lay_flat_counts_only_outermost_terrace_contact():
    # Three upward terraces total 75 mm2, but only the highest 25 mm2
    # can touch a plate after flipping. A 60 mm2 downward face wins.
    terraces = []
    for x, z in ((0, 1), (5, 2), (10, 3)):
        terraces += [[(x, 0, z), (x + 5, 0, z), (x + 5, 5, z)],
                     [(x, 0, z), (x + 5, 5, z), (x, 5, z)]]
    base = [[(0, 0, 0), (0, 4, 0), (15, 4, 0)],
            [(0, 0, 0), (15, 4, 0), (15, 0, 0)]]
    matrix, dims = edit_plan.lay_flat_rotation(terraces + base)
    assert matrix == pytest.approx([1, 0, 0, 0, 1, 0, 0, 0, 1])
    assert dims == pytest.approx((15, 5, 3))


# --------------------------------------------------------------- envelopes

def test_rotated_envelope_is_measured_on_vertices_not_the_rotated_box():
    # A right-triangle prism, legs 10 mm on X and Y, turned 45 degrees about
    # Z: its vertices span 10*sqrt(2) in X and 10/sqrt(2) in Y. Rotating its
    # 10 x 10 bounding box instead claims 10*sqrt(2) in both.
    tri = prism_xy([(0.0, 0.0), (10.0, 0.0), (0.0, 10.0)], 0.0, 5.0)
    m = edit_plan.rotation_matrix('z', 45)
    assert edit_plan.mesh_envelope(m, tri) == pytest.approx((10 * math.sqrt(2), 10 / math.sqrt(2), 5.0), abs=1e-4)
    assert edit_plan.transformed_dims(m, (10, 10, 5)) == pytest.approx((14.1421, 14.1421, 5.0), abs=1e-4)


@pytest.mark.parametrize('axis,deg,expected', [
    ('z', 30, (10 * math.cos(math.radians(30)) + 20 * math.sin(math.radians(30)),
               10 * math.sin(math.radians(30)) + 20 * math.cos(math.radians(30)), 30.0)),
    ('x', 90, (10.0, 30.0, 20.0)),
    ('y', 90, (30.0, 20.0, 10.0)),
])
def test_box_envelope_after_rotation(axis, deg, expected):
    m = edit_plan.rotation_matrix(axis, deg)
    assert edit_plan.mesh_envelope(m, box(10, 20, 30)) == pytest.approx(expected, abs=1e-4)


def test_rotation_matrices_are_rigid_and_right_handed():
    for axis in 'xyz':
        for deg in (30, 45, 90, 135, 180):
            m = np.array(edit_plan.rotation_matrix(axis, deg), float).reshape(3, 3)
            assert m @ m.T == pytest.approx(np.eye(3), abs=1e-12)
            assert np.linalg.det(m) == pytest.approx(1.0)
    # +90 about Z takes +X to +Y (right-handed, column vectors).
    assert edit_plan.apply_matrix(edit_plan.rotation_matrix('z', 90), (1, 0, 0)) == pytest.approx((0, 1, 0), abs=1e-12)


# --------------------------------------------------------------- placement

@pytest.mark.parametrize('rotation', [None, edit_plan.rotation_matrix('x', 45), edit_plan.rotation_matrix('z', 30)])
@pytest.mark.parametrize('scale', [1.0, 0.8, 1.37])
def test_place_drops_to_bed_centers_and_scales_volume_by_cube(tmp_path, rotation, scale):
    src = write_stl(tmp_path / 'off_origin.stl', box(10, 20, 30, origin=(500.0, -300.0, 77.0)))
    out = tmp_path / 'placed.stl'
    res = orca_adapter.place_stl(src, out, scale=scale, rotation=rotation, bed=BED)
    tris = orca_adapter.read_stl_triangles(out)
    lo, hi = _extent(tris)
    assert lo[2] == pytest.approx(0.0, abs=1e-4)
    assert (lo[:2] + hi[:2]) / 2 == pytest.approx(np.array(BED) / 2, abs=1e-4)
    expected = edit_plan.mesh_envelope(rotation or [1, 0, 0, 0, 1, 0, 0, 0, 1], box(10, 20, 30))
    assert hi - lo == pytest.approx(np.array(expected) * scale, abs=1e-3)
    assert _volume(tris) == pytest.approx(6000.0 * scale ** 3, rel=1e-5)
    placed = res['placed_bounds_mm']
    assert [b[1] - b[0] for b in placed] == pytest.approx(list(hi - lo), abs=1e-3)


@pytest.mark.parametrize('axis', ['x', 'y', 'z'])
def test_mirror_keeps_size_volume_and_outward_normals(tmp_path, axis):
    src = write_stl(tmp_path / 'wedge.stl', wedge45())
    out = tmp_path / 'mirrored.stl'
    orca_adapter.place_stl(src, out, mirror=edit_plan.mirror_matrix(axis), bed=BED)
    before, after = orca_adapter.read_stl_triangles(src), orca_adapter.read_stl_triangles(out)
    assert _volume(after) == pytest.approx(_volume(before), rel=1e-6) and _volume(after) > 0
    lo0, hi0 = _extent(before)
    lo1, hi1 = _extent(after)
    assert hi1 - lo1 == pytest.approx(hi0 - lo0, abs=1e-4)


def test_far_off_origin_3mf_keeps_its_size_through_the_float32_export(tmp_path):
    # At x = 1e6 mm float32 resolves only 0.0625 mm; the 3MF is read in
    # float64 and re-centered before the float32 STL write, so the 10.03 mm
    # side survives to well under a micron.
    src = write_3mf(tmp_path / 'far.3mf', box(10.03, 20.0, 30.0, origin=(1.0e6, -2.5e6, 4.0e5)))
    orca_adapter._TRIANGLE_CACHE.clear()
    out = tmp_path / 'far.stl'
    orca_adapter.export_geometry(src, out, bed=BED)
    lo, hi = _extent(orca_adapter.read_stl_triangles(out))
    assert hi - lo == pytest.approx([10.03, 20.0, 30.0], abs=1e-5)
    assert lo[2] == pytest.approx(0.0, abs=1e-6)


def test_binary_stl_whose_header_starts_with_solid_reads_as_binary(tmp_path):
    src = write_stl(tmp_path / 'solid_header.stl', box(10, 20, 30), header=b'solid exported by a CAD tool')
    tris = orca_adapter.read_stl_triangles(src)
    assert len(tris) == 12 and _volume(tris) == pytest.approx(6000.0)


@pytest.mark.parametrize('unit,factor', [('millimeter', 1.0), ('inch', 25.4), ('centimeter', 10.0), ('meter', 1000.0)])
def test_3mf_unit_attribute_scales_to_millimeters(tmp_path, unit, factor):
    src = write_3mf(tmp_path / 'u.3mf', box(1, 2, 0.5))
    text = zipfile.ZipFile(src).read('3D/3dmodel.model').decode().replace('unit="millimeter"', f'unit="{unit}"')
    with zipfile.ZipFile(src, 'w') as z:
        z.writestr('3D/3dmodel.model', text)
    parts, _raw, mm_factor, _warn = geometry_layout.load_parts(src)
    assert mm_factor == factor
    lo, hi = _extent(parts[0]['triangles'])
    assert hi - lo == pytest.approx(np.array([1, 2, 0.5]) * factor)


# --------------------------------------------------------------- size edits

def _job(dims, **extra):
    return {'downloaded_file': 'part.stl', 'audit': dict(zip(('size_x', 'size_y', 'size_z'), dims)), **extra}


def _edit(job, raw, source=None):
    plan = edit_plan.enrich_plan(edit_plan.validate_plan(raw), job, source)
    edit_plan.record_edit(job, plan, actor='test', lane='typed', source_sha256=None)
    edit_plan.apply_plan(job, plan)
    return plan


def test_successive_percent_edits_compound_and_undo_steps_back():
    job = _job((40.0, 20.0, 10.0))
    first = _edit(job, {'kind': 'scale_percent', 'percent': 80})
    assert job['scale'] == 0.8 and first['result_dims'] == [32.0, 16.0, 8.0]
    second = _edit(job, {'kind': 'scale_percent', 'percent': 80})
    assert job['scale'] == pytest.approx(0.64) and second['result_dims'] == [25.6, 12.8, 6.4]
    edit_plan.undo_last_edit(job, actor='test')
    assert job['scale'] == 0.8
    edit_plan.undo_last_edit(job, actor='test')
    assert job.get('scale') is None


def test_inch_conversion_is_folded_once_into_percent_edits():
    job = _job((2.0, 1.0, 0.5), unit_decision={'units': 'inch', 'source_sha256': 'x', 'original_mm': [2.0, 1.0, 0.5]})
    assert edit_plan.effective_scale(job) == pytest.approx(25.4)
    plan = _edit(job, {'kind': 'scale_percent', 'percent': 80})
    assert job['scale'] == pytest.approx(25.4 * 0.8)
    assert plan['result_dims'] == pytest.approx([40.64, 20.32, 10.16])
    _edit(job, {'kind': 'scale_percent', 'percent': 50})
    assert job['scale'] == pytest.approx(25.4 * 0.4)
    assert edit_plan.effective_scale(job) == pytest.approx(10.16)


def test_make_it_n_mm_tall_measures_the_rotated_height(tmp_path):
    # A 10 x 20 x 30 box turned 90 degrees about Y prints 30 x 20 x 10: "make
    # it 20 mm tall" doubles it, it does not shrink the authored 30 mm Z.
    src = write_stl(tmp_path / 'b.stl', box(10, 20, 30))
    job = _job((10.0, 20.0, 30.0))
    _edit(job, {'kind': 'rotate', 'axis': 'y', 'degrees': 90}, source=src)
    assert edit_plan.job_dims(job) == pytest.approx((30.0, 20.0, 10.0))
    plan = _edit(job, {'kind': 'scale_to_mm', 'mm': 20, 'axis': 'z'})
    assert job['scale'] == pytest.approx(2.0)
    assert plan['result_dims'] == pytest.approx([60.0, 40.0, 20.0])
    # The size told to the user is the size placed for slicing, to 0.01 mm.
    out = tmp_path / 'placed.stl'
    res = orca_adapter.place_stl(src, out, scale=job['scale'], rotation=job['orientation']['rotation'], bed=(400.0, 400.0))
    placed = [b[1] - b[0] for b in res['placed_bounds_mm']]
    assert placed == pytest.approx(plan['result_dims'], abs=0.01)


def test_rotation_edits_compose_in_the_printed_frame(tmp_path):
    src = write_stl(tmp_path / 'b.stl', box(10, 20, 30))
    job = _job((10.0, 20.0, 30.0))
    _edit(job, {'kind': 'rotate', 'axis': 'x', 'degrees': 90}, source=src)   # 10 x 30 x 20
    _edit(job, {'kind': 'rotate', 'axis': 'z', 'degrees': 90}, source=src)   # 30 x 10 x 20
    assert edit_plan.job_dims(job) == pytest.approx((30.0, 10.0, 20.0))


# --------------------------------------------------------------- supports

def test_overhang_normals_follow_a_non_uniform_item_scale(tmp_path):
    # The 45 degree underside (|nz| 0.707) is an overhang candidate for the
    # clearance screen (|nz| > 0.5). Stretched 2x in Z by the build item it
    # rises at atan(2) = 63.4 degrees, |nz| = 1/sqrt(5) = 0.447: no longer an
    # overhang. Normals come from the transformed vertices, so they follow.
    tris = wedge45()
    for tf, expect in (('1 0 0 0 1 0 0 0 1 0 0 0', 1 / math.sqrt(2)), ('1 0 0 0 1 0 0 0 2 0 0 0', 1 / math.sqrt(5))):
        src = write_3mf(tmp_path / 'w.3mf', tris, item_tf=tf)
        leaf = support_clearance.leaves(str(src))[0]['tris']
        n = np.cross(leaf[:, 1] - leaf[:, 0], leaf[:, 2] - leaf[:, 0])
        nz = n[:, 2] / np.linalg.norm(n, axis=1)
        sloped = nz[(nz < -1e-6) & (nz > -0.999)]
        assert sloped.size and np.allclose(-sloped, expect, atol=1e-9)
        assert support_clearance.OVERHANG_NZ < 1 / math.sqrt(2) and support_clearance.OVERHANG_NZ > 1 / math.sqrt(5)


def test_orca_support_threshold_is_measured_from_horizontal():
    # Orca's support_threshold_angle 30 supports faces less than 30 degrees
    # from horizontal: downward |nz| above cos(30 degrees).
    assert orca_adapter.SUPPORT_THRESHOLD_NZ == pytest.approx(-math.cos(math.radians(30)), abs=1e-3)
