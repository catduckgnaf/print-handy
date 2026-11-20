"""Reference behavior the native 3MF paint census must preserve (#1715)."""
import os
import zipfile

import pytest

from print_handy.core import painted_geometry as pg


_MEMBER = '3D/3dmodel.model'


def _source(tmp_path, *objects):
    rows = ''.join(
        f'<p:object id="{mesh_id}"><p:mesh><p:triangles>{triangles}'
        '</p:triangles></p:mesh></p:object>'
        for mesh_id, triangles in enumerate(objects, 1)
    )
    path = tmp_path / 'painted.3mf'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(
            _MEMBER,
            '<p:model xmlns:p="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
            'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">'
            f'<p:resources>{rows}</p:resources></p:model>',
        )
    return path


def _part(mesh_id, **kwargs):
    return {'ancestry': [[_MEMBER, str(mesh_id)]], **kwargs}


@pytest.fixture(params=['python', 'native'])
def census_backend(request, monkeypatch):
    if request.param == 'python':
        monkeypatch.setattr(pg, '_load_native_paint_census', lambda: None)
    elif pg._load_native_paint_census() is None:
        if os.environ.get('PRINT_HANDY_PAINT_CENSUS_REQUIRE_NATIVE') == '1':
            pytest.fail('the native paint census must import in this job')
        pytest.skip('the optional native paint census is not built')


def test_census_preserves_prefixed_meshes_paint_precedence_and_ranges(tmp_path, census_backend):
    source = _source(
        tmp_path,
        '<p:triangle paint_color="0fC" slic3rpe:mmu_segmentation="8"/>'
        '<p:triangle slic3rpe:mmu_segmentation="849"/>'
        '<p:triangle/>',
        '<p:triangle paint_color="0"/>',
    )
    with pg.decode_scope():
        assert pg.leaf_states(source, _part(1)) == {'states': {1: 1, 2: 1, 18: 1}, 'unpainted': 1}
        assert pg.leaf_states(source, _part(2)) == {'states': {0: 1}, 'unpainted': 0}
        assert pg.leaf_states(source, _part(1, triangle_ranges=[[1, 2]])) == {
            'states': {1: 1, 2: 1}, 'unpainted': 1,
        }


def test_native_prefixed_mesh_does_not_decode_in_python(tmp_path, monkeypatch):
    if pg._load_native_paint_census() is None:
        if os.environ.get('PRINT_HANDY_PAINT_CENSUS_REQUIRE_NATIVE') == '1':
            pytest.fail('the native paint census must import in this job')
        pytest.skip('the optional native paint census is not built')
    source = _source(tmp_path, '<p:triangle paint_color="849"/>')

    def unexpected_decode(code):
        pytest.fail(f'native census fell back to Python for {code}')

    monkeypatch.setattr(pg, 'decode_states', unexpected_decode)
    assert pg.leaf_states(source, _part(1)) == {'states': {1: 1, 2: 1}, 'unpainted': 0}


def test_whole_mesh_uses_one_native_census_and_ranges_use_python(tmp_path, monkeypatch):
    source = _source(tmp_path, '<p:triangle paint_color="4"/>', '<p:triangle paint_color="8"/>')
    calls = []

    def census(data, blocks):
        calls.append((data, blocks))
        return {'1': ({1: 1}, 0), '2': ({2: 1}, 0)}

    monkeypatch.setattr(pg, '_native_paint_census', census)
    real_decode = pg.decode_states
    decodes = []

    def decode(code):
        decodes.append(code)
        return real_decode(code)

    monkeypatch.setattr(pg, 'decode_states', decode)
    with pg.decode_scope():
        row = pg.leaf_states(source, _part(1))
        assert row == {'states': {1: 1}, 'unpainted': 0}
        row['states'].clear()
        assert pg.leaf_states(source, _part(1)) == {'states': {1: 1}, 'unpainted': 0}
        assert pg.leaf_states(source, _part(2)) == {'states': {2: 1}, 'unpainted': 0}
        assert not decodes
        assert pg.leaf_states(source, _part(2, triangle_ranges=[[0, 0]])) == {
            'states': {2: 1}, 'unpainted': 0,
        }
    assert len(calls) == 1
    assert decodes == ['8']


def test_native_error_falls_back_once_per_member(tmp_path, monkeypatch):
    source = _source(tmp_path, '<p:triangle paint_color="4"/>', '<p:triangle paint_color="8"/>')
    calls = []

    def census(*args):
        calls.append(args)
        raise ValueError('native census failed')

    monkeypatch.setattr(pg, '_native_paint_census', census)
    with pg.decode_scope():
        assert pg.leaf_states(source, _part(1)) == {'states': {1: 1}, 'unpainted': 0}
        assert pg.leaf_states(source, _part(2)) == {'states': {2: 1}, 'unpainted': 0}
    assert len(calls) == 1


@pytest.mark.parametrize(('code', 'error'), [('FC', 'truncated paint_color'), ('44', 'trailing paint_color data')])
def test_bad_paint_does_not_change_the_census_of_another_mesh(tmp_path, code, error):
    source = _source(tmp_path, f'<p:triangle paint_color="{code}"/>', '<p:triangle paint_color="8"/>')
    with pg.decode_scope():
        with pytest.raises(ValueError, match=f'^{error}$'):
            pg.leaf_states(source, _part(1))
        assert pg.leaf_states(source, _part(2)) == {'states': {2: 1}, 'unpainted': 0}
