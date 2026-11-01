"""Core 3MF material colors (#711).

Plain 3MF exports carry per-object colors as <basematerials>/<colorgroup>
referenced by object pid/pindex, with no slicer palette. They must read as
author colors on ordinary per-part questions, never as triangle paint (which
asked an unanswerable 'painted:unknown' question and rejected every answer).
Every model here is generated in the test, so it is CC0 and public.
"""
import zipfile

import pytest

from print_handy.core import source_colors as sc

NS = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
CT = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
      '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
RELS = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')

PARTS = [('body', '#FAF9F7'), ('hat', '#61506F'), ('band', '#C47CE7'),
         ('gold', '#FFE57C'), ('ink', '#383845'), ('blush', '#FFB3C4')]


def _box(x0):
    v = [(x, y, z) for z in (0, 10) for y in (0, 10) for x in (x0, x0 + 10)]
    f = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 5), (0, 5, 4),
         (2, 6, 7), (2, 7, 3), (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
    return ('<mesh><vertices>' + ''.join(f'<vertex x="{a}" y="{b}" z="{c}"/>' for a, b, c in v)
            + '</vertices><triangles>' + ''.join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in f)
            + '</triangles></mesh>')


def _write(path, style):
    """style: 'basematerials' (assembly of components), 'colorgroup' (separate build items) or 'one_color'."""
    parts = [(n, PARTS[0][1]) for n, _ in PARTS] if style == 'one_color' else PARTS
    if style == 'colorgroup':
        res = (f'<m:colorgroup id="1">' + ''.join(f'<m:color color="{c}FF"/>' for _, c in parts) + '</m:colorgroup>')
    else:
        res = '<basematerials id="1">' + ''.join(f'<base name="{n}" displaycolor="{c}"/>' for n, c in parts) + '</basematerials>'
    objs = ''.join(f'<object id="{i + 2}" name="{n}" type="model" pid="1" pindex="{i}">{_box(i * 12)}</object>'
                   for i, (n, _) in enumerate(parts))
    if style == 'colorgroup':
        build = ''.join(f'<item objectid="{i + 2}"/>' for i in range(len(parts)))
    else:
        objs += (f'<object id="{len(parts) + 2}" name="Assembly" type="model"><components>'
                 + ''.join(f'<component objectid="{i + 2}"/>' for i in range(len(parts))) + '</components></object>')
        build = f'<item objectid="{len(parts) + 2}"/>'
    model = (f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xmlns="{NS}" '
             f'xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02">'
             f'<resources>{res}{objs}</resources><build>{build}</build></model>')
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', CT)
        z.writestr('_rels/.rels', RELS)
        z.writestr('3D/3dmodel.model', model)
    return path


@pytest.mark.parametrize('style', ['basematerials', 'colorgroup'])
def test_core_material_colors_are_author_colors_not_paint(tmp_path, style):
    m = sc.inventory(_write(tmp_path / f'{style}.3mf', style))
    assert m['painted'] is False
    assert [p.get('author_color', '').upper() for p in m['parts']] == [c for _, c in PARTS]
    rows = sc.canonical_prompts(m)
    assert all(r['key'].startswith('part:') for r in rows) and len(rows) == len(PARTS)
    assert all('author made it' in r['question'] for r in rows)


def test_one_core_color_everywhere_is_not_announced(tmp_path):
    m = sc.inventory(_write(tmp_path / 'one.3mf', 'one_color'))
    assert m['painted'] is False and not any(p.get('author_color') for p in m['parts'])


def test_answered_core_material_selection_validates(tmp_path):
    """The 2026-10-31 failure: every part answered, then 'requires an explicit selection'."""
    m = sc.inventory(_write(tmp_path / 'b.3mf', 'basematerials'))
    choices = {r['key']: {'slot': 1} for r in sc.canonical_prompts(m)}
    out = sc.validate_decisions(m, {'source_sha256': m['source_sha256'], 'schema': 'part_slots_v1',
                                    'mode': 'per_part', 'choices': choices}, {}, lambda st, v: v)
    assert set(out['choices']) == set(choices)


def test_paint_detection_is_slicer_paint_only():
    assert sc._has_paint(b'<triangle v1="0" paint_color="4"/>')
    assert sc._has_paint(b'<triangle v1="0" mmu_segmentation="4"/>')
    assert not sc._has_paint(b'<m:basematerials id="1"><object pid="1" pindex="0">')
    assert not sc._has_paint(b'<triangle v1="0" v2="1" v3="2" p1="2"/>')


def test_volume_extruder_zero_inherits_object_filament(tmp_path):
    """Orca/Bambu write extruder 0 on a volume (often a modifier) to mean "use the
    object's filament"; 2.1.7 rejected the whole file as an invalid assignment."""
    from pathlib import Path
    source = Path(__file__).parents[1] / "corpus/cases/parts-one-plate/model.3mf"
    target = tmp_path / "inherit.3mf"
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "Metadata/model_settings.config":
                data = data.replace(b'<part id="2" subtype="normal_part"><metadata key="name" value="Base"/>'
                                    b'<metadata key="extruder" value="1"/>',
                                    b'<part id="2" subtype="normal_part"><metadata key="name" value="Base"/>'
                                    b'<metadata key="extruder" value="0"/>')
                assert b'value="0"' in data
            dst.writestr(item, data)
    parts = {p["name"]: p for p in sc.inventory(target)["parts"]}
    assert parts["Base"]["source_extruder"] == 1
