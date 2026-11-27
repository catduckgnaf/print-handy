"""export_volume_plate writes mesh XML directly, byte for byte what ElementTree wrote (#1940)."""
import zipfile

import pytest

from print_handy.core import color_geometry

BOX_V = [(0, 0, 0), (10, 0, 0), (10, 20, 0), (0, 20, 0), (0, 0, 30), (10, 0, 30), (10, 20, 30), (0, 20, 30)]
BOX_F = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
         (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
MIRROR_X = '-1 0 0 0 1 0 0 0 1 50 0 0'


def _write(path, tri_attrs='', item_tf=None, mesh_children=''):
    # Indented like a real slicer export: the whitespace between elements is text and tail.
    vx = '\n     ' + ''.join(f'<vertex x="{a / 3}" y="{b * 1.1}" z="{c}"/>\n     ' for a, b, c in BOX_V)
    tx = '\n     ' + ''.join(f'<triangle v1="{a}" v2="{b}" v3="{c}"{tri_attrs}/>\n     ' for a, b, c in BOX_F)
    tf = f' transform="{item_tf}"' if item_tf else ''
    xml = ('<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" '
           'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
           '<resources><object id="1" type="model" name="box"><mesh>'
           f'<vertices>{vx}</vertices><triangles>{tx}</triangles>{mesh_children}</mesh></object></resources>'
           f'<build><item objectid="1"{tf}/></build></model>')
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('3D/3dmodel.model', xml)
    return path


def _members(path):
    with zipfile.ZipFile(path) as archive:
        return archive.read('3D/3dmodel.model'), archive.read('Metadata/model_settings.config')


CASES = {
    'plain': ({}, {}),
    'mirrored item': ({'item_tf': MIRROR_X}, {}),
    'rotated, offset and scaled': ({'item_tf': '0 1 0 -1 0 0 0 0 1 12.5 -3.25 4'}, {'scale': 1.5, 'bed': (256, 256)}),
    'material attributes dropped': ({'tri_attrs': ' pid="1" p1="0" p2="1" p3="2" extra="keep"'}, {}),
}


@pytest.mark.parametrize('name', CASES)
def test_direct_write_matches_elementtree_bytes(tmp_path, monkeypatch, name):
    source_kw, export_kw = CASES[name]
    src = _write(tmp_path / 'src.3mf', **source_kw)
    real = color_geometry._plain_mesh_xml
    handled = []

    def spy(*args):
        out = real(*args)
        handled.append(out is not None)
        return out

    monkeypatch.setattr(color_geometry, '_plain_mesh_xml', spy)
    color_geometry.export_volume_plate(src, tmp_path / 'fast.3mf', **export_kw)
    assert handled and all(handled), 'the direct writer must take this fixture, not fall back'
    # With the direct writer switched off the old deepcopy + ElementTree path runs.
    monkeypatch.setattr(color_geometry, '_plain_mesh_xml', lambda *args: None)
    color_geometry.export_volume_plate(src, tmp_path / 'slow.3mf', **export_kw)
    assert _members(tmp_path / 'fast.3mf') == _members(tmp_path / 'slow.3mf')


def test_unusual_mesh_falls_back_to_elementtree(tmp_path, monkeypatch):
    src = _write(tmp_path / 'src.3mf', mesh_children='<extra/>')
    seen = []
    real = color_geometry._plain_mesh_xml
    monkeypatch.setattr(color_geometry, '_plain_mesh_xml', lambda *a: seen.append(real(*a)) or seen[-1])
    color_geometry.export_volume_plate(src, tmp_path / 'out.3mf')
    assert seen == [None]
    assert b'<vertices>' in _members(tmp_path / 'out.3mf')[0]
