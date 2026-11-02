"""Hostile 3MF inputs for the byte level mesh reader.

Contract under test: for ANY input, the fast path either declines (mesh_tables is None and the
ElementTree path decides) or produces exactly what ElementTree would have produced, including the
exception type when the reference refuses. Each case also pins whether the fast path is allowed to
take it, so a guard that silently stops firing fails here instead of passing by luck.
"""
import io
import warnings
import zipfile

import pytest

from print_handy.core import mesh_fast, orca_adapter

NS = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
VERTS = ('<vertices><vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/>'
         '<vertex x="0" y="3" z="1"/></vertices>')
TRIS = '<triangles><triangle v1="0" v2="1" v3="2"/></triangles>'
MESH = f'<mesh>{VERTS}{TRIS}</mesh>'


def model(resources=None, build='<build><item objectid="1"/></build>', root_attrs='', prolog=''):
    resources = resources if resources is not None else f'<resources><object id="1">{MESH}</object></resources>'
    return f'{prolog}<model xmlns="{NS}"{root_attrs}>{resources}{build}</model>'.encode()


def write_zip(path, members):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')  # duplicate member names warn by design in these cases
        with zipfile.ZipFile(path, 'w') as z:
            for name, data in members:
                z.writestr(name, data)
    mesh_fast._TABLE_CACHE.clear()
    return path


def outcome(fn):
    try:
        return 'ok', repr(fn())  # repr: nan != nan, and -0.0 must stay distinguishable from 0.0
    except Exception as exc:  # noqa: BLE001 - the exception TYPE is the behaviour being compared
        return 'err', type(exc).__name__


def reference_outcome(path, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(mesh_fast, 'archive_tables', lambda z, n: (z.read(n), None))
        return outcome(lambda: orca_adapter._parse_3mf_triangles(path))


# (id, document bytes, fast path allowed?)
CASES = [
    ('baseline', model(), True),
    ('internal_entity_in_attribute',
     model(prolog='<!DOCTYPE model [<!ENTITY five "5">]>').replace(b'x="2"', b'x="&five;"'), False),
    ('entity_expansion_chain',
     model(prolog='<!DOCTYPE model [<!ENTITY a "0123456789"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
                  '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>').replace(b'x="2"', b'x="&c;"'), False),
    ('undefined_entity', model().replace(b'x="2"', b'x="&nope;"'), False),
    ('cdata_hides_triangle',
     model(f'<resources><object id="1">{MESH}</object></resources>',
           build='<build><item objectid="1"/></build><metadata><![CDATA[<triangle v1="0" v2="1" v3="9"/>]]></metadata>'),
     False),
    ('mesh_lookalike_element',
     model(f'<resources><meshes>{MESH}</meshes><object id="1">{MESH}</object></resources>'), False),
    ('mesh_prefix_namespace_alias',
     f'<m:model xmlns:m="{NS}"><m:resources><m:object id="1"><m:mesh>{VERTS}{TRIS}</m:mesh></m:object>'
     '</m:resources><m:build><m:item objectid="1"/></m:build></m:model>'.encode(), False),
    ('mesh_under_foreign_resources_namespace',
     model('<resources xmlns="urn:other"><object id="1">' + MESH + '</object></resources>'), False),
    ('triangle_redeclares_default_namespace',
     model().replace(b'<triangle ', b'<triangle xmlns="urn:x" '), False),
    ('triangle_declares_prefix',
     model().replace(b'<triangle ', b'<triangle xmlns:p="urn:x" p:a="1" '), False),
    ('vertex_redeclares_namespace',
     model().replace(b'<vertex x="2"', b'<vertex xmlns="urn:x" x="2"'), False),
    # xmlns placed AFTER v3 is the only spot the grammar's extra-attribute slot accepts; (?!xmlns) must refuse it.
    ('triangle_default_xmlns_after_v3', model().replace(b'v3="2"/>', b'v3="2" xmlns="urn:x"/>'), False),
    ('triangle_prefix_xmlns_after_v3', model().replace(b'v3="2"/>', b'v3="2" xmlns:p="urn:x"/>'), False),
    ('foreign_object_shares_id_with_core_object',
     model('<resources><object id="1">' + MESH + '</object><object xmlns="urn:o" id="1">'
           + MESH.replace('x="2"', 'x="7"') + '</object></resources>'), False),
    ('foreign_object_listed_first',
     model('<resources><object xmlns="urn:o" id="1">' + MESH.replace('x="2"', 'x="7"') + '</object><object id="1">'
           + MESH + '</object></resources>'), False),
    ('prefixed_core_object_beside_default_object',
     f'<model xmlns="{NS}" xmlns:m="{NS}"><resources><object id="1">{MESH}</object>'
     f'<m:object id="2"><m:mesh>{VERTS}{TRIS}</m:mesh></m:object></resources>'
     '<build><item objectid="1"/></build></model>'.encode(), False),
    ('negative_triangle_index_wraps', model().replace(b'v3="2"', b'v3="-1"'), False),
    ('plus_sign_index', model().replace(b'v3="2"', b'v3="+2"'), False),
    ('arabic_indic_digit_index', model().replace(b'v3="2"', 'v3="\u0662"'.encode()), False),
    ('index_with_whitespace', model().replace(b'v3="2"', b'v3=" 2 "'), False),
    ('index_out_of_range', model().replace(b'v3="2"', b'v3="3"'), False),
    ('huge_index', model().replace(b'v3="2"', b'v3="' + b'9' * 40 + b'"'), False),
    ('nan_coordinate', model().replace(b'x="2"', b'x="nan"'), True),
    ('inf_coordinate', model().replace(b'x="2"', b'x="-inf"'), True),
    ('overflow_coordinate', model().replace(b'x="2"', b'x="1e999"'), True),
    ('negative_zero', model().replace(b'x="2"', b'x="-0"'), True),
    # A coordinate float() cannot convert declines the fast path (P2-72): the
    # fallback reader converts only referenced meshes, so a bad number in an
    # unused resource no longer blocks the archive; in a referenced mesh the
    # fallback raises the same ValueError the equivalence assert pins above.
    ('hex_coordinate', model().replace(b'x="2"', b'x="0x10"'), False),
    ('empty_coordinate', model().replace(b'x="2"', b'x=""'), False),
    ('underscore_coordinate', model().replace(b'x="2"', b'x="1_0"'), True),
    ('garbage_coordinate', model().replace(b'x="2"', b'x="two"'), False),
    ('duplicate_object_ids_last_wins',
     model('<resources><object id="1">' + MESH.replace('x="2"', 'x="7"') + '</object><object id="1">' + MESH
           + '</object></resources>'), False),
    ('two_meshes_in_one_object',
     model('<resources><object id="1">' + MESH + MESH.replace('x="2"', 'x="7"') + '</object></resources>'), False),
    ('self_closing_mesh_before_real_mesh',
     model('<resources><object id="1"><mesh/></object><object id="2">' + MESH + '</object></resources>',
           build='<build><item objectid="2"/></build>'), False),
    ('gt_inside_attribute_before_id',
     model('<resources><object name="a&gt;b" id="1">' + MESH + '</object></resources>'), False),
    ('object_without_id_attribute',
     model('<resources><object>' + MESH + '</object></resources>'), False),
    ('comment_between_every_tag',
     model(f'<resources><object id="1"><mesh><!--x-->{VERTS}<!--y-->{TRIS}</mesh></object></resources>'), False),
    ('processing_instruction_in_mesh',
     model(f'<resources><object id="1"><mesh><?pi x?>{VERTS}{TRIS}</mesh></object></resources>'), False),
    ('nul_byte_in_document', model().replace(b'<build>', b'\x00<build>'), False),
    ('truncated_after_vertices', model().replace(TRIS.encode() + b'</mesh>', b''), False),
    ('empty_mesh_body', model('<resources><object id="1"><mesh></mesh></object></resources>'), False),
    ('bom_prefixed', b'\xef\xbb\xbf' + model(), None),
    ('non_millimeter_unit', model(root_attrs=' unit="inch"'), True),
]


@pytest.mark.parametrize('name,data,fast_allowed', CASES, ids=[c[0] for c in CASES])
def test_fast_path_matches_reference_or_declines(tmp_path, monkeypatch, name, data, fast_allowed):
    path = write_zip(tmp_path / 'case.3mf', [('3D/3dmodel.model', data)])
    expected = reference_outcome(path, monkeypatch)
    mesh_fast._TABLE_CACHE.clear()
    assert outcome(lambda: orca_adapter._parse_3mf_triangles(path)) == expected, name
    if fast_allowed is not None:
        taken = outcome(lambda: mesh_fast.mesh_tables(data))
        # Declined means exactly "returned None". A bad number (float('0x10'))
        # declines since P2-72; the equivalence assert above pins the outcome.
        accepted = taken != ('ok', 'None')
        assert accepted == fast_allowed, f'{name}: fast path {"accepted" if accepted else "declined"}'


def test_entity_chain_is_not_expanded_by_the_fast_reader():
    data = dict((c[0], c[1]) for c in CASES)['entity_expansion_chain']
    assert mesh_fast.mesh_tables(data) is None
    assert mesh_fast.strip_mesh_bodies(data) is not None  # stripping must not choke or expand either


def test_duplicate_zip_member_names_read_the_same_member_as_the_reference(tmp_path, monkeypatch):
    first = model().replace(b'x="2"', b'x="111"')
    last = model().replace(b'x="2"', b'x="222"')
    path = write_zip(tmp_path / 'dup.3mf', [('3D/3dmodel.model', first), ('3D/3dmodel.model', last)])
    expected = reference_outcome(path, monkeypatch)
    mesh_fast._TABLE_CACHE.clear()
    got = outcome(lambda: orca_adapter._parse_3mf_triangles(path))
    assert got == expected
    assert '222.0' in got[1] and '111.0' not in got[1]


def test_cache_never_serves_one_archives_geometry_for_another(tmp_path, monkeypatch):
    a = write_zip(tmp_path / 'a.3mf', [('3D/3dmodel.model', model().replace(b'x="2"', b'x="11"'))])
    b = write_zip(tmp_path / 'b.3mf', [('3D/3dmodel.model', model().replace(b'x="2"', b'x="22"'))])
    mesh_fast._TABLE_CACHE.clear()
    ra1 = orca_adapter._parse_3mf_triangles(a)
    rb = orca_adapter._parse_3mf_triangles(b)
    ra2 = orca_adapter._parse_3mf_triangles(a)
    assert ra1 == ra2 and ra1 != rb
    assert rb[0][1][0] == 22.0 and ra1[0][1][0] == 11.0


def test_in_memory_archives_with_same_member_do_not_share_cache_entries():
    def tables_for(x):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('3D/3dmodel.model', model().replace(b'x="2"', f'x="{x}"'.encode()))
        with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z:
            return mesh_fast.archive_tables(z, '3D/3dmodel.model')[1]['1'][0][1][0]
    mesh_fast._TABLE_CACHE.clear()
    assert [tables_for(5), tables_for(6), tables_for(7)] == [5.0, 6.0, 7.0]


def test_corrupt_member_raises_and_is_not_cached(tmp_path):
    path = tmp_path / 'bad.3mf'
    payload = model()
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as z:
        z.writestr('3D/3dmodel.model', payload)
    raw = bytearray(path.read_bytes())
    at = raw.index(b'x="2"') + 3
    raw[at] = ord('9')  # flip one payload byte: CRC no longer matches
    path.write_bytes(bytes(raw))
    mesh_fast._TABLE_CACHE.clear()
    with pytest.raises(zipfile.BadZipFile):
        orca_adapter._parse_3mf_triangles(path)
    assert not any(isinstance(k, tuple) for k in mesh_fast._TABLE_CACHE), 'poisoned entry cached'
    write_zip(path, [('3D/3dmodel.model', payload)])
    assert orca_adapter._parse_3mf_triangles(path)[0][1] == (2.0, 0.0, 0.0)


def test_hostile_numbers_in_bounds_and_volume_match_the_old_loops():
    def old(tris):
        lo = [min(p[a] for t in tris for p in t) for a in range(3)]
        hi = [max(p[a] for t in tris for p in t) for a in range(3)]
        vol = 0.0
        for (ax, ay, az), (bx, by, bz), (cx, cy, cz) in tris:
            vol += (ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)) / 6.0
        return lo, hi, vol
    nan, inf = float('nan'), float('inf')
    sets = [
        [],
        [((0., 0., 0.), (1., 0., 0.), (0., 1., 0.))],
        [((nan, 0., 0.), (1., 0., 0.), (0., 1., 0.)), ((0., 0., 0.), (1., 0., 0.), (0., 1., 0.))],
        [((0., 0., 0.), (1., 0., 0.), (0., 1., 0.)), ((nan, 0., 0.), (1., 0., 0.), (0., 1., 0.))],
        [((inf, 1., 0.), (-inf, 0., 1.), (0., 1., 1.))],
        [((1e308, 1e308, 1e308), (-1e308, 1e308, 1e308), (1e308, -1e308, 1e308))],
        [((-0., -0., -0.), (0., 0., 0.), (-0., 0., -0.))],
    ]
    for tris in sets:
        assert outcome(lambda: mesh_fast.bounds_and_volume(tris)) == outcome(lambda: old(tris)), tris


def test_nan_is_not_silently_skipped_by_bounds_and_volume():
    nan = float('nan')
    tris = [((nan, 0., 0.), (1., 0., 0.), (0., 1., 0.))]
    lo, hi, vol = mesh_fast.bounds_and_volume(tris)
    assert lo[0] != lo[0] and hi[0] != hi[0], 'NaN coordinate must propagate like min()/max() on the raw values'
    assert vol != vol


def test_cache_holds_one_archive_at_a_time_so_dense_meshes_are_released(tmp_path):
    a = write_zip(tmp_path / 'a.3mf', [('3D/3dmodel.model', model()), ('3D/Objects/o.model', model())])
    b = write_zip(tmp_path / 'b.3mf', [('3D/3dmodel.model', model().replace(b'x="2"', b'x="22"'))])
    with zipfile.ZipFile(a) as z:
        mesh_fast.archive_tables(z, '3D/3dmodel.model')
        mesh_fast.archive_tables(z, '3D/Objects/o.model')  # same archive: every member stays cached
        assert len([k for k in mesh_fast._TABLE_CACHE if isinstance(k, tuple)]) == 2
    with zipfile.ZipFile(b) as z:
        mesh_fast.archive_tables(z, '3D/3dmodel.model')
    keys = [k for k in mesh_fast._TABLE_CACHE if isinstance(k, tuple)]
    assert len(keys) == 1, 'members of the previous archive must be evicted when another archive is read'
