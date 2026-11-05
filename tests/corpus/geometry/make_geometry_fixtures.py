#!/usr/bin/env python3
"""Create synthetic core 3MF fixtures for Print Handy #911.

No printer control. Geometries are deliberately small, unit mm. Some
fixtures are intentionally malformed/unsafe to exercise guards.
Uses standard library xml.etree.ElementTree, numpy and trimesh.
"""
import json
import math
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import trimesh

DEST = Path(__file__).resolve().parent
N = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
T = lambda tag: '{%s}%s' % (N, tag)


def box(name, sx, sy, lo, hi, x=0, y=0, kind='model', color=0):
    m = trimesh.creation.box(extents=[sx, sy, hi - lo])
    m.apply_translation([x, y, (lo + hi) / 2])
    return dict(name=name, mesh=m, kind=kind, color=color)


def make(filename, desc, objects, expect, other=None):
    root = ET.Element(T('model'), attrib={'unit': 'millimeter', 'xmlns': N})
    rs = ET.SubElement(root, T('resources'))
    colors = ET.SubElement(rs, T('basematerials'), id='1')
    ET.SubElement(colors, T('base'), name='BLACK', displaycolor='#181818FF')
    ET.SubElement(colors, T('base'), name='BRONZE', displaycolor='#BA8A43FF')
    build = ET.SubElement(root, T('build'))
    for i, obj in enumerate(objects, 2):
        ob = ET.SubElement(rs, T('object'), id=str(i), type=obj.get('kind', 'model'),
                           name=obj['name'], pid='1', pindex=str(obj.get('color', 0)))
        mesh = ET.SubElement(ob, T('mesh'))
        vertices = ET.SubElement(mesh, T('vertices'))
        triangles = ET.SubElement(mesh, T('triangles'))
        m = obj['mesh']
        for vert in m.vertices:
            ET.SubElement(vertices, T('vertex'), {k: f'{v:.6f}' for k, v in zip(('x', 'y', 'z'), vert)})
        for a, b, c in m.faces:
            ET.SubElement(triangles, T('triangle'), {'v1': str(a), 'v2': str(b), 'v3': str(c)})
        item = ET.SubElement(build, T('item'), objectid=str(i))
        if obj.get('transform'):
            item.set('transform', obj['transform'])
    out = DEST / filename
    xml = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding='utf-8')
    types = (b'<?xml version="1.0" encoding="UTF-8"?>'
             b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
             b'<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
             b'</Types>')
    rels = (b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Target="/3D/3dmodel.model" Id="rel0" '
            b'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
            b'</Relationships>')
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr('[Content_Types].xml', types)
        z.writestr('_rels/.rels', rels)
        z.writestr('3D/3dmodel.model', xml)
    entry = {'fixture': filename, 'description': desc, 'expect_low_gap_flags': expect, 'other_checks': other or []}
    fixtures.append(entry)


fixtures = []
make('01_bed_gap_040_large.3mf', 'A large flat 60x40 underside at Z0.4 across open air.', [box('floating large plate', 60, 40, .4, 2.4)], 1)
make('02_bed_gap_080_large.3mf', 'Large underside Z0.8, still under default 1.0mm gate.', [box('floating large plate', 60, 40, .8, 2.8)], 1)
make('03_bed_gap_120_large.3mf', 'Large underside Z1.2 outside low-gap rule; normal supports may still be needed.', [box('floating plate', 60, 40, 1.2, 3.2)], 0, ['NORMAL_SUPPORT_EVALUATION'])
make('04_bed_gap_500_large.3mf', 'Z5.0 normal bridge/overhang; should not be treated as near-plate.', [box('normal floating plate', 60, 40, 5, 7)], 0, ['NORMAL_SUPPORT_EVALUATION'])
make('05_bed_gap_040_small_area.3mf', '20x20=400mm2 floating plate: intentionally below 500mm2 area cutoff; still potentially dangerous.', [box('small floating plate', 20, 20, .4, 2.4)], 0, ['SPAN_RISK_INDEPENDENT_OF_AREA'])
make('06_part_gap_040_large.3mf', 'Large slab at Z2.4 only 0.4mm above another part top Z2.0.', [box('foundation', 60, 40, 0, 2), box('raised plate', 60, 40, 2.4, 3.4)], 1)
make('07_part_gap_080_large.3mf', 'Large slab only 0.8mm above underlying model, independent of distance to bed.', [box('foundation', 60, 40, 0, 2), box('raised plate', 60, 40, 2.8, 3.8)], 1)
make('08_part_gap_300_large.3mf', 'Another 60x40 underside with 3.0mm air gap above a base; normal support check only.', [box('foundation', 60, 40, 0, 2), box('raised plate', 60, 40, 5, 6)], 0, ['NORMAL_SUPPORT_EVALUATION'])
make('09_modeled_support_tiny_contact.3mf', 'A few tiny posts under a broad face; they barely touch XY and have a top Z gap.', [box('wide roof', 60, 40, 2.65, 3.65), box('REMOVABLE SUPPORT A', 2, 2, 0, 2.45, x=-23, kind='model'), box('REMOVABLE SUPPORT B', 2, 2, 0, 2.45, x=23, kind='model')], 0, ['MODELED_SUPPORT_COVERAGE_TOO_LOW'])
make('10_build_transform_z040.3mf', 'Core 3MF build transform positions a local zero-level 60x40 plate at +0.4mm.', [dict(box('transformed plate', 60, 40, 0, 2), transform='1 0 0 0 1 0 0 0 1 0 0 0.4')], 1, ['BUILD_TRANSFORM_MUST_APPLY'])
make('11_disjoint_islands_below_cutoff.3mf', 'Two independent 20x20 platforms, each <500mm2; do not sum into one connected risk.', [box('island A', 20, 20, .4, 2.4, x=-17), box('island B', 20, 20, .4, 2.4, x=17)], 0, ['PRESERVE_CONNECTED_COMPONENTS'])
make('12_long_narrow_bridge_small_area.3mf', '80x4=320mm2 can still have an 80mm unsupported span despite area cutoff.', [box('long narrow bridge', 80, 4, .4, 2.4)], 0, ['SPAN_RISK_INDEPENDENT_OF_AREA'])
sloped = box('sloped roof', 60, 40, .65, 2.65)
sloped_mesh = sloped['mesh']
assert isinstance(sloped_mesh, trimesh.Trimesh)
sloped_mesh.apply_transform(trimesh.transformations.rotation_matrix(math.radians(6), [1, 0, 0], point=[0, 0, 0]))
make('13_sloped_low_clearance.3mf', '6-degree underside tilt: reference horizontal-only analyzer is insufficient.', [sloped], 0, ['PRODUCTION_SLOPED_TRIANGLE_RAYCAST'])
make('14_support_interpenetrates_model.3mf', 'A modeled support intersects a structural roof; extrusion collision guard must flag.', [box('roof', 60, 40, 2.65, 3.65), box('REMOVABLE SUPPORT intersects', 2, 2, 0, 3.1)], 0, ['INTERSECTION_COLLISION_GUARD'])
make('15_below_plate_negative_z.3mf', 'Mesh penetrates build plate by 0.4mm; crop/sink changes must state loss.', [box('embedded plate', 60, 40, -.4, 1.6)], 0, ['BELOW_BED_GEOMETRY', 'SINK_LOSS_REPORT'])
make('16_multicolor_gap_inlay.3mf', 'Small bronze islands floating 0.02mm over black top; materials must be preserved.', [box('BLACK BODY', 60, 40, 0, 3.9, color=0), box('BRONZE DETAILS', 8, 8, 3.92, 4.42, color=1), box('BRONZE DETAIL 2', 8, 8, 3.92, 4.42, x=12, color=1)], 0, ['MULTICOLOR_MATERIAL_PRESERVATION', 'THIN_COLOR_INTERFACE'])
make('17_raft_false_solution.3mf', 'Same Z0.4 underside with a raft hypothetical; moving whole object/raft preserves relative gap.', [box('floating plate', 60, 40, .4, 2.4)], 1, ['RAFT_MUST_NOT_BE_SUGGESTED_AS_GAP_FIX'])

(DEST / 'fixture_manifest.json').write_text(
    json.dumps({'purpose': 'Test candidate detection and false-positive boundaries, not a promise of printability.',
                'gap_mm': 1.0, 'area_mm2': 500, 'cases': fixtures}, indent=2) + '\n'
)
print('Generated', len(fixtures), 'fixtures')
