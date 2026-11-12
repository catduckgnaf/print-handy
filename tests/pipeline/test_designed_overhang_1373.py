"""#1373: a designed self-overhang in a no-supports project is not a clearance risk.

Live case: a MakerWorld pinecone lantern whose designer profile prints with
supports off. Its stacked scales sit 0.6 mm over the scale below, inside one
mesh. The pre-check called that a gap "to the part below" that supports could
not reach, offered supports, and told the operator to repair the geometry.
"""

import json
import zipfile

from print_handy.core import support_clearance

NS = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'


def _box(x0, y0, z0, x1, y1, z1):
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
         (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return v, f


def _mesh_xml(boxes):
    verts, tris = [], []
    for box in boxes:
        v, f = _box(*box)
        base = len(verts)
        verts += v
        tris += [(a + base, b + base, c + base) for a, b, c in f]
    vx = ''.join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in verts)
    tx = ''.join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    return f'<mesh><vertices>{vx}</vertices><triangles>{tx}</triangles></mesh>'


# A 40 x 40 x 10 mm block and a 30 x 30 mm shelf floating 0.5 mm above it:
# 900 mm2 of underside below the 1.0 mm gap threshold.
LOWER = (0, 0, 0, 40, 40, 10)
SHELF = (5, 5, 10.5, 35, 35, 12)


def _write(path, objects, enable_support=None):
    body = ''.join(f'<object id="{i}" type="model">{_mesh_xml(boxes)}</object>'
                   for i, boxes in enumerate(objects, 1))
    items = ''.join(f'<item objectid="{i}"/>' for i in range(1, len(objects) + 1))
    model = (f'<?xml version="1.0"?><model unit="millimeter" xmlns="{NS}">'
             f'<resources>{body}</resources><build>{items}</build></model>')
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('3D/3dmodel.model', model)
        if enable_support is not None:
            archive.writestr('Metadata/project_settings.config',
                             json.dumps({'enable_support': enable_support, 'layer_height': '0.2'}))
    return str(path)


def test_self_overhang_in_a_no_supports_project_is_a_designed_overhang(tmp_path):
    src = _write(tmp_path / 'one_mesh.3mf', [[LOWER, SHELF]], enable_support='0')
    rep = support_clearance.analyze(src)
    assert rep['authored_supports'] is False
    assert not support_clearance.risky(rep)
    assert support_clearance.CODE_CLEARANCE not in support_clearance.codes(rep)
    designed = [r for r in rep['regions'] if r['kind'] == 'designed_overhang']
    assert designed and all(r['over_self'] and r['flagged'] is False for r in designed)
    assert support_clearance.describe(rep) == []


def test_self_overhang_without_a_designer_setting_is_still_a_risk(tmp_path):
    # A plain 3MF says nothing about supports: the screening stays as it was,
    # but the text no longer calls the same mesh "the part below".
    src = _write(tmp_path / 'plain.3mf', [[LOWER, SHELF]])
    rep = support_clearance.analyze(src)
    assert rep['authored_supports'] is None
    assert support_clearance.risky(rep)
    finding = next(f for f in rep['findings'] if f['code'] == support_clearance.CODE_CLEARANCE)
    assert finding['over_self'] is True
    assert 'its own surface below' in support_clearance.describe(rep)[0]


def test_a_gap_over_another_part_stays_a_risk_in_a_no_supports_project(tmp_path):
    # Two separate objects: the shelf floats over a different part, which
    # the designer's no-supports setting does not make printable.
    src = _write(tmp_path / 'two_parts.3mf', [[LOWER], [SHELF]], enable_support='0')
    rep = support_clearance.analyze(src)
    assert support_clearance.risky(rep)
    finding = next(f for f in rep['findings'] if f['code'] == support_clearance.CODE_CLEARANCE)
    assert finding['over_self'] is False
    assert 'the part below' in support_clearance.describe(rep)[0]


def test_authored_supports_reads_the_project_setting(tmp_path):
    assert support_clearance.authored_supports(_write(tmp_path / 'a.3mf', [[LOWER]], '1')) is True
    assert support_clearance.authored_supports(_write(tmp_path / 'b.3mf', [[LOWER]], ['0'])) is False
    assert support_clearance.authored_supports(_write(tmp_path / 'c.3mf', [[LOWER]])) is None
    stl = tmp_path / 'd.stl'
    stl.write_bytes(b'solid x\nendsolid x\n')
    assert support_clearance.authored_supports(str(stl)) is None
