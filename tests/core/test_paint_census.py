"""Reference behavior the native 3MF paint census must preserve (#1715)."""
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


def test_census_preserves_prefixed_meshes_paint_precedence_and_ranges(tmp_path):
    source = _source(
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


@pytest.mark.parametrize(('code', 'error'), [('FC', 'truncated paint_color'), ('44', 'trailing paint_color data')])
def test_bad_paint_does_not_change_the_census_of_another_mesh(tmp_path, code, error):
    source = _source(f'<p:triangle paint_color="{code}"/>', '<p:triangle paint_color="8"/>')
    with pg.decode_scope():
        with pytest.raises(ValueError, match=f'^{error}$'):
            pg.leaf_states(source, _part(1))
        assert pg.leaf_states(source, _part(2)) == {'states': {2: 1}, 'unpainted': 0}
