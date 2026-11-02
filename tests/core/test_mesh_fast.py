"""Generated, redistributable 3MF fixtures: compare every triangle, never samples."""
import xml.etree.ElementTree as ET
import zipfile

import pytest

from print_handy.core import mesh_fast, orca_adapter

NS = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
BODY = ('<vertices><vertex x="0" y="0" z="0"/>'
        '<vertex x="2" y="0" z="0"/><vertex x="0" y="3" z="1"/></vertices>'
        '<triangles><triangle v1="0" v2="1" v3="2"/></triangles>')


def document(body=BODY):
    return (f'<model xmlns="{NS}"><resources><object id="1"><mesh>{body}</mesh>'
            '</object></resources><build><item objectid="1"/></build></model>').encode()


def archive(tmp_path, data):
    path = tmp_path / 'generated.3mf'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('3D/3dmodel.model', data)
    mesh_fast._TABLE_CACHE.clear()
    return path


def reference(path, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(mesh_fast, 'archive_tables', lambda z, n: (z.read(n), None))
        return orca_adapter._parse_3mf_triangles(path)


@pytest.mark.parametrize('body', [
    BODY,
    BODY.replace('x="2" y="0"', 'y="0" x="2"'),
    BODY.replace('"', "'"),
    BODY.replace('x="2"', 'x="2e0"'),
    BODY.replace('<vertex ', '<vertex extra="ok" '),
    BODY.replace('<triangle ', '<triangle p1="2" '),
    BODY.replace('<vertices>', '<vertices>\n<!-- ignored -->'),
])
def test_full_geometry_matches_reference(tmp_path, monkeypatch, body):
    path = archive(tmp_path, document(body))
    expected = reference(path, monkeypatch)
    assert expected == [[(0., 0., 0.), (2., 0., 0.), (0., 3., 1.)]]
    assert orca_adapter._parse_3mf_triangles(path) == expected


@pytest.mark.parametrize('body', [
    BODY.replace('<vertex x="0" y="0" z="0"/>', '<vertex x="0" y="0" z="0">'),
    BODY.replace('<triangles>', '<triangles><broken>'),
    BODY.replace('v3="2"/>', 'v3="2" duplicate="a" duplicate="b"/>'),
    BODY.replace('</vertices>', '</wrong>'),
])
def test_malformed_mesh_cannot_be_sanitized_into_valid_geometry(tmp_path, monkeypatch, body):
    path = archive(tmp_path, document(body))
    with pytest.raises(ET.ParseError):
        reference(path, monkeypatch)
    with pytest.raises(ET.ParseError):
        orca_adapter._parse_3mf_triangles(path)


def test_comment_geometry_is_not_printable_geometry(tmp_path, monkeypatch):
    body = BODY.replace('<triangle v1="0" v2="1" v3="2"/>', '<!-- <triangle v1="0" v2="1" v3="2"/> -->')
    path = archive(tmp_path, document(body))
    with pytest.raises(RuntimeError, match='no printable triangles'):
        reference(path, monkeypatch)
    with pytest.raises(RuntimeError, match='no printable triangles'):
        orca_adapter._parse_3mf_triangles(path)


def test_bad_coordinate_in_unused_object_declines_fast_path(tmp_path, monkeypatch):
    # Object 2 is not referenced by the build; its invalid coordinate must not
    # reject the archive — the fast path declines and the reference reader,
    # which converts only referenced meshes, still returns object 1 (P2-72).
    bad = BODY.replace('x="2"', 'x="invalid"')
    data = (f'<model xmlns="{NS}"><resources><object id="1"><mesh>{BODY}</mesh></object>'
            f'<object id="2"><mesh>{bad}</mesh></object></resources>'
            '<build><item objectid="1"/></build></model>').encode()
    assert mesh_fast.mesh_tables(data) is None
    path = archive(tmp_path, data)
    assert orca_adapter._parse_3mf_triangles(path) == [[(0., 0., 0.), (2., 0., 0.), (0., 3., 1.)]]


def test_large_offset_vertices_keep_double_precision():
    # float32 storage collapsed 100000000 and 100000001 into one vertex before
    # the compensating component/build translation could separate them (P2-75).
    body = ('<vertices><vertex x="100000000" y="0" z="0"/><vertex x="100000001" y="0" z="0"/>'
            '<vertex x="100000000" y="3" z="1"/></vertices>'
            '<triangles><triangle v1="0" v2="1" v3="2"/></triangles>')
    tables = mesh_fast.mesh_tables(document(body))
    assert tables is not None
    vertices = tables['1'].vertices
    assert vertices.dtype.name == 'float64'
    assert vertices.tolist()[1][0] == 100000001.0
    assert vertices.tolist()[1][0] - vertices.tolist()[0][0] == 1.0


def test_canonical_geometry_really_uses_fast_path():
    tables = mesh_fast.mesh_tables(document())
    assert tables == {'1': ([(0., 0., 0.), (2., 0., 0.), (0., 3., 1.)], [(0, 1, 2)])}


@pytest.mark.parametrize('index', ['3', '999'])
def test_invalid_indices_preserve_reference_refusal(tmp_path, monkeypatch, index):
    path = archive(tmp_path, document(BODY.replace('v3="2"', f'v3="{index}"')))
    with pytest.raises(IndexError):
        reference(path, monkeypatch)
    with pytest.raises(IndexError):
        orca_adapter._parse_3mf_triangles(path)


def test_utf16_uses_reference_fallback(tmp_path, monkeypatch):
    data = document().decode().encode('utf-16')
    assert mesh_fast.mesh_tables(data) is None
    path = archive(tmp_path, data)
    assert orca_adapter._parse_3mf_triangles(path) == reference(path, monkeypatch)


def test_nested_repeated_transforms_match_every_triangle(tmp_path, monkeypatch):
    data = document().replace(
        b'</resources>',
        b'<object id="2"><components><component objectid="1" transform="2 0 0 0 1 0 0 0 1 5 6 7"/>'
        b'<component objectid="1" transform="1 0 0 0 -1 0 0 0 1 0 0 0"/></components></object>'
        b'<object id="3"><components><component objectid="2" transform="0 1 0 -1 0 0 0 0 1 10 0 0"/>'
        b'</components></object></resources>').replace(
            b'<item objectid="1"/>', b'<item objectid="3" transform="1 0 0 0 1 0 0 0 1 3 4 5"/>')
    path = archive(tmp_path, data)
    expected = reference(path, monkeypatch)
    assert len(expected) == 2
    assert expected[0][0] == (7., 9., 12.)
    assert orca_adapter._parse_3mf_triangles(path) == expected


def test_cache_reuses_member_but_invalidates_replaced_source(tmp_path):
    path = archive(tmp_path, document())
    with zipfile.ZipFile(path) as z:
        first = mesh_fast.archive_tables(z, '3D/3dmodel.model')
        assert mesh_fast.archive_tables(z, '3D/3dmodel.model')[1] is first[1]
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('3D/3dmodel.model', document(BODY.replace('x="2"', 'x="9"')))
    with zipfile.ZipFile(path) as z:
        second = mesh_fast.archive_tables(z, '3D/3dmodel.model')
    assert second != first
    assert second[1]['1'][0][1] == (9., 0., 0.)


def test_noncore_mesh_does_not_become_core_geometry(tmp_path, monkeypatch):
    data = document().replace(b'<mesh>', b'<mesh xmlns="urn:other">')
    path = archive(tmp_path, data)
    with pytest.raises(RuntimeError, match='no printable triangles'):
        reference(path, monkeypatch)
    with pytest.raises(RuntimeError, match='no printable triangles'):
        orca_adapter._parse_3mf_triangles(path)


def test_mesh_tables_refuses_out_of_range_index_itself():
    # Contract of the fast reader, independent of what the caller later does.
    assert mesh_fast.mesh_tables(document(BODY.replace('v3="2"', 'v3="3"'))) is None


def _old_bounds_and_volume(tris):
    lo = [min(p[a] for t in tris for p in t) for a in range(3)]
    hi = [max(p[a] for t in tris for p in t) for a in range(3)]
    vol = 0.0
    for (ax, ay, az), (bx, by, bz), (cx, cy, cz) in tris:
        vol += (ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)) / 6.0
    return lo, hi, vol


def test_bounds_and_volume_bit_identical_to_original_loops():
    import random
    rng = random.Random(47673462)
    for _ in range(50):
        tris = [[tuple(rng.uniform(-50, 50) for _ in range(3)) for _ in range(3)]
                for _ in range(rng.randint(1, 40))]
        assert mesh_fast.bounds_and_volume(tris) == _old_bounds_and_volume(tris)


def test_bounds_and_volume_known_tetrahedron():
    a, b, c, d = (0., 0., 0.), (6., 0., 0.), (0., 6., 0.), (0., 0., 6.)
    tris = [[a, c, b], [a, b, d], [a, d, c], [b, c, d]]
    lo, hi, vol = mesh_fast.bounds_and_volume(tris)
    assert (lo, hi) == ([0., 0., 0.], [6., 6., 6.])
    assert abs(vol) == pytest.approx(36.0)


def _annotated_bounds(tmp_path, monkeypatch, use_fast):
    from print_handy.core import source_colors
    obj = (f'<model xmlns="{NS}"><resources><object id="1"><mesh>{BODY}</mesh></object></resources></model>').encode()
    top = (f'<model xmlns="{NS}" xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">'
           '<resources><object id="2"><components>'
           '<component p:path="/3D/Objects/o.model" objectid="1" transform="0 2.137 0 -3.291 0 0 0 0 1.573 9 9 9"/>'
           '</components></object></resources>'
           '<build><item objectid="2" transform="1 0 0 0 1 0 0 0 2 100 100 100"/></build></model>').encode()
    path = tmp_path / 'ann.3mf'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('3D/3dmodel.model', top)
        z.writestr('3D/Objects/o.model', obj)
    mesh_fast._TABLE_CACHE.clear()
    if not use_fast:
        monkeypatch.setattr(source_colors, 'archive_tables', lambda z, n: (z.read(n), None))
    parts = [{'object_id': '2', 'part_id': '1', 'name': 'x'}]
    with zipfile.ZipFile(path) as z:
        source_colors._annotate_parts(z, set(z.namelist()), parts, [])
    return parts[0].get('bounds_mm')


def test_part_bounds_fast_matches_elementtree_reference(tmp_path, monkeypatch):
    expected = _annotated_bounds(tmp_path, monkeypatch, use_fast=False)
    assert expected == [9.9, 4.3, 3.1]  # hand computed: |-3.291*3|, 2.137*2, 1.573*1*2
    assert _annotated_bounds(tmp_path, monkeypatch, use_fast=True) == expected
