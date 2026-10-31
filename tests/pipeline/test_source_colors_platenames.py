from pathlib import Path
import sys, zipfile
from print_handy.core import source_colors


def test_duplicate_stl_names_use_bambu_plate_names(tmp_path):
    mesh = '<mesh><vertices/><triangles/></mesh>'
    model = ('<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
             + ''.join(f'<object id="{i}">{mesh}</object>' for i in (1, 2, 3))
             + '</resources><build><item objectid="1"/><item objectid="2"/><item objectid="3"/></build></model>')
    obj = lambda i: f'<object id="{i}"><metadata key="name" value="Holder.stl"/></object>'
    cfg = ('<config>' + obj(1) + obj(2) + obj(3)
           + '<plate><metadata key="plater_id" value="1"/><metadata key="plater_name" value="Knob"/>'
           '<model_instance><metadata key="object_id" value="2"/></model_instance></plate>'
           '<plate><metadata key="plater_id" value="2"/><metadata key="plater_name" value="Clamp"/>'
           '<model_instance><metadata key="object_id" value="1"/></model_instance>'
           '<model_instance><metadata key="object_id" value="3"/></model_instance></plate></config>')
    src = tmp_path / 'x.3mf'
    with zipfile.ZipFile(src, 'w') as z:
        z.writestr('3D/3dmodel.model', model)
        z.writestr('Metadata/model_settings.config', cfg)
    assert [p['name'] for p in source_colors.inventory(src)['parts']] == ['Clamp', 'Knob', 'Clamp']


def test_mesh_bodies_are_skipped_but_mesh_presence_is_kept():
    from print_handy.core import source_colors as sc
    data = (b'<model><resources><object id="1"><mesh><vertices><vertex x="0"/></vertices></mesh></object>'
            b'<object id="2"><components><component objectid="1"/></components></object></resources></model>')
    slim = sc._without_mesh_bodies(data)
    assert b'<vertex' not in slim and b'<mesh/>' in slim and b'<component objectid="1"/>' in slim


def test_paint_detection_matches_tags_and_attributes():
    from print_handy.core import source_colors as sc
    # Core 3MF materials are author colours, not paint (#711).
    assert not sc._has_paint(b'<m:basematerials id="1">')
    assert sc._has_paint(b'<triangle v1="0" paint_color="4"/>')
    assert not sc._has_paint(b'<triangle v1="0" p1="2"/>')
    assert not sc._has_paint(b'<triangle v1="0" v2="1"/><colorgroupish>')
