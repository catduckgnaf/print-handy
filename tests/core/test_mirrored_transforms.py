"""A 3MF placed through a mirroring transform flattens to an outward solid.

A 3MF mesh is wound counterclockwise seen from outside in its own
coordinates; a build item or component transform with a negative
determinant mirrors it. Every flattener must reverse the winding of such
a leaf, or the exported solid is inside-out (negative signed volume,
inward normals): slicers then see holes where the part is, and our own
normal-based overhang and contact checks read up as down.
"""
import json
import zipfile

import numpy as np
import pytest

from print_handy.core import color_geometry, geometry_layout, orca_adapter, support_clearance

# A 10 x 20 x 30 mm box wound outward in its own coordinates.
BOX_V = [(0, 0, 0), (10, 0, 0), (10, 20, 0), (0, 20, 0), (0, 0, 30), (10, 0, 30), (10, 20, 30), (0, 20, 30)]
BOX_F = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
         (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
MIRROR_X = '-1 0 0 0 1 0 0 0 1 50 0 0'
MIRROR_Z_ROT = '0 -1 0 -1 0 0 0 0 1 0 0 0'   # a reflection across x = y (det -1)


def _write(path, item_tf=None, comp_tf=None):
    vx = ''.join(f'<vertex x="{a}" y="{b}" z="{c}"/>' for a, b, c in BOX_V)
    tx = ''.join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in BOX_F)
    objs = f'<object id="1" type="model" name="box"><mesh><vertices>{vx}</vertices><triangles>{tx}</triangles></mesh></object>'
    top = '1'
    if comp_tf:
        objs += (f'<object id="2" type="model" name="wrap"><components>'
                 f'<component objectid="1" transform="{comp_tf}"/></components></object>')
        top = '2'
    tr = f' transform="{item_tf}"' if item_tf else ''
    xml = ('<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" '
           'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
           f'<resources>{objs}</resources><build><item objectid="{top}"{tr}/></build></model>')
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('3D/3dmodel.model', xml)
    return path


def _volume(tris):
    t = np.asarray([[tuple(map(float, p)) for p in tri] for tri in tris], float).reshape(-1, 3, 3)
    return float(np.einsum('ij,ij->i', t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6)


CASES = [
    ('plain', {}),
    ('item mirror', {'item_tf': MIRROR_X}),
    ('component mirror', {'comp_tf': MIRROR_X}),
    ('diagonal reflection', {'item_tf': MIRROR_Z_ROT}),
    ('mirror twice', {'item_tf': MIRROR_X, 'comp_tf': MIRROR_X}),
]


@pytest.mark.parametrize('label,kw', CASES, ids=[c[0] for c in CASES])
def test_flattened_mirrored_solid_keeps_positive_volume(tmp_path, label, kw):
    src = _write(tmp_path / 'm.3mf', **kw)
    orca_adapter._TRIANGLE_CACHE.clear()
    assert _volume(orca_adapter.source_triangles(src)) == pytest.approx(6000.0)
    assert _volume(orca_adapter._object_triangles(src, '2' if 'comp_tf' in kw else '1')) == pytest.approx(6000.0)
    out = tmp_path / 'm.stl'
    orca_adapter.export_geometry(src, out)
    assert _volume(orca_adapter.read_stl_triangles(out)) == pytest.approx(6000.0, rel=1e-6)
    parts, _unit, _factor, _warn = geometry_layout.load_parts(src)
    assert _volume(parts[0]['triangles']) == pytest.approx(6000.0)
    # The support clearance reader already did this; it must keep agreeing.
    assert _volume(support_clearance.leaves(str(src))[0]['tris']) == pytest.approx(6000.0)


def test_mirrored_volume_plate_export_keeps_outward_winding(tmp_path):
    src = _write(tmp_path / 'm.3mf', item_tf=MIRROR_X)
    out = tmp_path / 'plate.3mf'
    color_geometry.export_volume_plate(src, out)
    leaves = support_clearance.leaves(str(out))
    assert leaves and sum(_volume(leaf['tris']) for leaf in leaves) == pytest.approx(6000.0)


def test_transform_mirrors_is_the_sign_of_the_composed_determinant():
    ident = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]
    mx = [float(v) for v in MIRROR_X.split()]
    scale = [2, 0, 0, 0, 3, 0, 0, 0, 0.5, 7, 8, 9]
    assert orca_adapter.transform_mirrors([ident]) is False
    assert orca_adapter.transform_mirrors([mx]) is True
    assert orca_adapter.transform_mirrors([mx, scale]) is True
    assert orca_adapter.transform_mirrors([mx, mx]) is False


@pytest.mark.parametrize('painted', [False, True])
@pytest.mark.parametrize('kw', [dict(item_tf=MIRROR_X), dict(comp_tf=MIRROR_X),
                               dict(item_tf=MIRROR_X, comp_tf=MIRROR_X)])
def test_color_export_mirrors_are_outward_or_explicitly_refused(tmp_path, painted, kw):
    from print_handy.core import source_colors
    src = _write(tmp_path / 'source.3mf', **kw)
    if painted:
        with zipfile.ZipFile(src) as archive:
            xml = archive.read('3D/3dmodel.model').replace(b'<triangle ', b'<triangle paint_color="8" ')
        # A painted source carries its palette: paint state 2 ("8") is
        # source filament 2, so the project names two filaments and the
        # color flow asks a slot for each. Without it the export refuses
        # earlier, as an unmapped paint state, and never reaches the
        # mirror check under test.
        project = {'filament_colour': ['#FF0000', '#00FF00'], 'filament_type': ['PLA', 'PLA']}
        with zipfile.ZipFile(src, 'w') as archive:
            archive.writestr('3D/3dmodel.model', xml)
            archive.writestr('Metadata/project_settings.config', json.dumps(project))
    manifest = source_colors.inventory(src)
    choices = {row['key']: {'slot': 1} for row in source_colors.canonical_prompts(manifest)}
    decisions = {'schema': 'part_slots_v1', 'mode': 'per_part',
                 'source_sha256': manifest['source_sha256'], 'choices': choices}
    out = tmp_path / 'out.3mf'
    reflected = len(kw) == 1
    if painted and reflected:
        with pytest.raises(RuntimeError, match='mirrored painted geometry'):
            color_geometry.export_color_geometry(src, out, decisions)
        assert not out.exists()
    else:
        color_geometry.export_color_geometry(src, out, decisions)
        assert sum(_volume(leaf['tris']) for leaf in support_clearance.leaves(str(out))) == pytest.approx(6000)


def _support_painted(src, attr='paint_supports', code='841'):
    """Give every triangle of a written source a subdivided support-paint code."""
    with zipfile.ZipFile(src) as archive:
        xml = archive.read('3D/3dmodel.model')
    xml = xml.replace(b'<triangle ', f'<triangle {attr}="{code}" '.encode())
    with zipfile.ZipFile(src, 'w') as archive:
        archive.writestr('3D/3dmodel.model', xml)
    return src


def _per_part_decisions(src):
    from print_handy.core import source_colors
    manifest = source_colors.inventory(src)
    choices = {row['key']: {'slot': 1} for row in source_colors.canonical_prompts(manifest)}
    return {'schema': 'part_slots_v1', 'mode': 'per_part',
            'source_sha256': manifest['source_sha256'], 'choices': choices}


@pytest.mark.parametrize('attr', ['paint_supports', 'paint_seam'])
def test_mirrored_support_or_seam_paint_is_refused_not_rewound(tmp_path, attr):
    # #1195 (painted leaves under a mirror, other paths): support and seam
    # paint use the same vertex-order subdivision as color paint, and both
    # exporters keep them. Rewinding a reflected leaf would move the painted
    # enforcers or seam to sub-triangles the author never painted, so both
    # exporters refuse instead.
    src = _support_painted(_write(tmp_path / 'source.3mf', item_tf=MIRROR_X), attr)
    out = tmp_path / 'out.3mf'
    with pytest.raises(RuntimeError, match=f'mirrored geometry with painted {attr}'):
        color_geometry.export_color_geometry(src, out, _per_part_decisions(src))
    assert not out.exists()
    plate = tmp_path / 'plate.3mf'
    with pytest.raises(RuntimeError, match=f'mirrored geometry with painted {attr}'):
        color_geometry.export_volume_plate(src, plate)
    assert not plate.exists()


def test_support_paint_without_a_mirror_still_exports_with_its_order(tmp_path):
    # The refusal is only for a reflected leaf: an unmirrored source, and a
    # source mirrored twice (orientation preserving), keep their support
    # paint on the triangles exactly as authored.
    for name, kw in (('plain', {}), ('twice', dict(item_tf=MIRROR_X, comp_tf=MIRROR_X))):
        src = _support_painted(_write(tmp_path / f'{name}.3mf', **kw))
        for export, out in ((lambda s, o: color_geometry.export_color_geometry(s, o, _per_part_decisions(s)),
                             tmp_path / f'{name}-color.3mf'),
                            (color_geometry.export_volume_plate, tmp_path / f'{name}-plate.3mf')):
            export(src, out)
            with zipfile.ZipFile(out) as archive:
                model = archive.read('3D/3dmodel.model')
            assert model.count(b'paint_supports="841"') == len(BOX_F)
            assert sum(_volume(leaf['tris']) for leaf in support_clearance.leaves(str(out))) == pytest.approx(6000)


def test_per_color_reader_counts_paint_the_same_under_a_mirror(tmp_path):
    # #1195 (per-color source reader): the reader counts paint states per
    # triangle leaf, which does not depend on vertex order, and it never
    # flattens geometry, so a reflected source reads exactly like the
    # unmirrored one. The order-dependent step is the flattening export,
    # refused above.
    from print_handy.core import painted_geometry, source_colors
    project = {'filament_colour': ['#FF0000', '#00FF00'], 'filament_type': ['PLA', 'PLA']}
    reads = {}
    for name, kw in (('plain', {}), ('item', dict(item_tf=MIRROR_X)), ('diagonal', dict(item_tf=MIRROR_Z_ROT)),
                     ('component', dict(comp_tf=MIRROR_X))):
        src = _write(tmp_path / f'{name}.3mf', **kw)
        with zipfile.ZipFile(src) as archive:
            xml = archive.read('3D/3dmodel.model').replace(b'<triangle ', b'<triangle paint_color="841" ')
        with zipfile.ZipFile(src, 'w') as archive:
            archive.writestr('3D/3dmodel.model', xml)
            archive.writestr('Metadata/project_settings.config', json.dumps(project))
        census = painted_geometry.paint_census(src)
        manifest = source_colors.inventory(src)
        reads[name] = ([row['states'] for row in census.values()], manifest['painted'],
                       [p['source_extruder'] for p in manifest['parts']],
                       [row['key'].split(':')[0] for row in source_colors.canonical_prompts(manifest)])
    assert reads['plain'][0] == [{1: len(BOX_F), 2: len(BOX_F)}]
    assert all(read == reads['plain'] for read in reads.values()), reads
