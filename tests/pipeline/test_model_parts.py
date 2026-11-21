"""Multi-file models from file-list sites: parts vs versions, typed file numbers, one combined project."""
import struct
import zipfile
from pathlib import Path

import pytest

from print_handy.core import model_parts, prepared_intake
from print_handy.core import heavy_worker

# The live Printables listing of model 1875711 (CC BY), as its API names the files on 2026-10-29.
UNO = ['UNO - cap-sign-1.stl', 'UNO - container-1.stl', 'UNO - uno-latch-1.stl', 'UNO - cap-nosign-1.stl']


def cube_stl(path, size=(10.0, 10.0, 10.0), origin=(0.0, 0.0, 0.0)):
    """A tiny closed binary STL box."""
    x0, y0, z0 = origin
    x1, y1, z1 = x0 + size[0], y0 + size[1], z0 + size[2]
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
         (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as out:
        out.write(b'\0' * 80 + struct.pack('<I', len(faces)))
        for a, b, c in faces:
            out.write(struct.pack('<12fH', 0, 0, 0, *v[a], *v[b], *v[c], 0))
    return path


@pytest.mark.parametrize('names', [UNO, [n.replace(' - ', '-') for n in UNO]])
def test_uno_model_is_two_parts_and_one_cap_question(names):
    plan = model_parts.plan(names)
    assert sorted(plan['parts']) == [1, 2]
    assert [plan['names'][i] for i in plan['parts']] == ['container', 'latch']
    assert len(plan['groups']) == 1
    group = plan['groups'][0]
    assert group['subject'] == 'cap'
    assert [(o['label'], o['files']) for o in group['options']] == [('sign', [0]), ('no sign', [3])]
    assert plan['assembly_3mf'] is None and plan['quantities'] == {}


@pytest.mark.parametrize('names', [
    ['arm_left.stl', 'arm_right.stl'],
    ['foot-L.stl', 'foot-R.stl'],
    ['part1.stl', 'part2.stl'],
    ['gear_1.stl', 'gear_2.stl'],
    ['top.stl', 'bottom.stl'],
    ['box.stl', 'box_lid.stl'],
])
def test_parts_that_are_both_needed_are_not_versions(names):
    plan = model_parts.plan(names)
    assert plan['groups'] == [] and sorted(plan['parts']) == [0, 1]


@pytest.mark.parametrize('names, labels', [
    (['insert_M3.stl', 'insert_M4.stl'], ['M3', 'M4']),
    (['stand_100mm.stl', 'stand_150mm.stl'], ['100mm', '150mm']),
    (['lid_v1.stl', 'lid_v2.stl'], ['v1', 'v2']),
    (['lid_with_logo.stl', 'lid_no_logo.stl'], ['with logo', 'no logo']),
    (['puzzle_easy.stl', 'puzzle_hard.stl'], ['easy', 'hard']),
    (['bust-supported.stl', 'bust-presupported.stl'], ['supported', 'presupported']),
    (['clip.stl', 'clip_v2.stl'], ['standard', 'v2']),
])
def test_obvious_versions_are_one_question(names, labels):
    plan = model_parts.plan(names)
    assert plan['parts'] == []
    assert [o['label'] for o in plan['groups'][0]['options']] == labels


def test_parts_sharing_the_same_sizes_are_asked_once():
    plan = model_parts.plan(['box_100mm.stl', 'box_150mm.stl', 'lid_100mm.stl', 'lid_150mm.stl', 'hinge.stl'])
    assert plan['parts'] == [4]
    assert len(plan['groups']) == 1
    group = plan['groups'][0]
    assert group['subject'] == 'box and lid'
    assert [(o['label'], o['files']) for o in group['options']] == [('100mm', [0, 2]), ('150mm', [1, 3])]


def test_one_3mf_among_stls_is_the_assembly_and_counts_are_read():
    assert model_parts.plan(['Rugged box.3mf', 'box.stl', 'lid.stl'])['assembly_3mf'] == 0
    assert model_parts.plan(['a.3mf', 'b.3mf', 'c.stl'])['assembly_3mf'] is None
    assert model_parts.plan(['a.3mf', 'b.3mf'])['assembly_3mf'] is None
    assert model_parts.quantity_hint('latch_x2.stl') == 2
    assert model_parts.quantity_hint('2x latch.stl') == 2
    assert model_parts.quantity_hint('bracket_2x4.stl') is None
    assert model_parts.quantity_hint('latch.stl') is None
    assert model_parts.plan(['box.stl', 'latch_x2.stl'])['quantities'] == {1: 2}


@pytest.mark.parametrize('text, picked', [
    ('1 3', [0, 2]), ('1,3', [0, 2]), ('#2 and 4', [1, 3]), ('2-4', [1, 2, 3]), ('all', [0, 1, 2, 3]), ('3', [2]),
])
def test_choose_files_reads_typed_numbers(text, picked):
    assert model_parts.parse_file_numbers(text, 4) == picked


@pytest.mark.parametrize('text', ['5', '0', 'the lid please', ''])
def test_choose_files_refuses_other_text(text):
    with pytest.raises(ValueError):
        model_parts.parse_file_numbers(text, 4)


def _objects(path):
    import xml.etree.ElementTree as ET
    ns = {'m': 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read('3D/3dmodel.model'))
    out = {}
    for obj in root.findall('.//m:resources/m:object', ns):
        xs = [float(v.get('x')) for v in obj.findall('.//m:vertex', ns)]
        ys = [float(v.get('y')) for v in obj.findall('.//m:vertex', ns)]
        zs = [float(v.get('z')) for v in obj.findall('.//m:vertex', ns)]
        out[obj.get('id')] = {'name': obj.get('name'), 'box': (min(xs), max(xs), min(ys), max(ys), min(zs))}
    for item in root.findall('.//m:build/m:item', ns):
        t = [float(v) for v in item.get('transform').split()]
        o = out[item.get('objectid')]
        x0, x1, y0, y1, z0 = o['box']
        o['placed'] = (x0 + t[9], x1 + t[9], y0 + t[10], y1 + t[10], z0 + t[11])
    return list(out.values())


def _overlap(a, b):
    return a[0] < b[1] and b[0] < a[1] and a[2] < b[3] and b[2] < a[3]


def test_combine_puts_one_object_per_file_on_the_bed(tmp_path):
    files = [cube_stl(tmp_path / 'in' / 'UNO-container-1.stl', (60, 40, 30), origin=(-30, -20, 5)),
             cube_stl(tmp_path / 'in' / 'UNO-uno-latch-1.stl', (20, 8, 4)),
             cube_stl(tmp_path / 'in' / 'UNO-cap-nosign-1.stl', (60, 40, 6))]
    result = model_parts.combine(tmp_path / 'out' / 'uno-parts.3mf', files, [220, 220, 250])
    assert result['on_one_bed'] is True
    objects = _objects(result['path'])
    assert sorted(o['name'] for o in objects) == ['UNO-cap-nosign-1', 'UNO-container-1', 'UNO-uno-latch-1']
    for o in objects:
        x0, x1, y0, y1, z0 = o['placed']
        assert 0 <= x0 and x1 <= 220 and 0 <= y0 and y1 <= 220 and abs(z0) < 1e-6
    for i, a in enumerate(objects):
        for b in objects[i + 1:]:
            assert not _overlap(a['placed'], b['placed'])
    # The color inventory sees three separately colorable parts, like any multi-object 3MF.
    from print_handy.core.source_colors import inventory
    assert len(inventory(Path(result['path']))['parts']) == 3


def test_combine_sets_parts_in_a_row_when_they_do_not_fit_one_bed(tmp_path):
    files = [cube_stl(tmp_path / 'a.stl', (150, 150, 10)), cube_stl(tmp_path / 'b.stl', (150, 150, 10))]
    result = model_parts.combine(tmp_path / 'ab.3mf', files, [220, 220])  # X, Y only, as bed_mm is
    assert result['on_one_bed'] is False
    a, b = _objects(result['path'])
    assert not _overlap(a['placed'], b['placed'])


def test_combine_runs_in_the_heavy_worker():
    assert ('model_parts', 'combine') in prepared_intake._OFFLOAD
    assert ('model_parts', 'combine') in heavy_worker.ALLOWED


@pytest.mark.parametrize('names, labels', [
    (['SnackTongs.stl', 'SnackTongs_FlatProfile.stl'], ['standard', 'flat profile']),
    (['clip.stl', 'clip_flat.stl'], ['standard', 'flat']),
    (['lid.stl', 'lid_alt.stl'], ['standard', 'alt']),
    (['lid.stl', 'lid-alternate.stl'], ['standard', 'alternate']),
    (['hook.stl', 'hook_low_profile.stl'], ['standard', 'low profile']),
    (['bust.stl', 'bust_noSupports.stl'], ['standard', 'no supports']),
    (['knob_original.stl', 'knob_remix.stl'], ['original', 'remix']),
    (['vase.stl', 'vase_v2.stl'], ['standard', 'v2']),
])
def test_variant_markers_make_one_version_question(names, labels):
    """#1779: clear variant markers are versions of one part, never two parts."""
    plan = model_parts.plan(names)
    assert plan['parts'] == []
    [group] = plan['groups']
    assert [o['label'] for o in group['options']] == labels


def test_a_variant_group_is_named_by_the_shared_part():
    plan = model_parts.plan(['SnackTongs.stl', 'SnackTongs_FlatProfile.stl'])
    assert plan['groups'][0]['subject'] == 'snack tongs'


@pytest.mark.parametrize('names', [
    ['bracket_flat.stl', 'bracket_angled.stl'],  # two shapes, not a marker pair
    ['frame.stl', 'frame_profile.stl'],           # "profile" alone is a part name
    ['base.stl', 'base_cover.stl'],
])
def test_words_near_the_markers_stay_parts(names):
    """The bias the module documents: a wrong part costs less than a wrong version."""
    plan = model_parts.plan(names)
    assert plan['groups'] == [] and sorted(plan['parts']) == [0, 1]


def test_variant_parts_beside_real_parts_still_print_the_parts():
    plan = model_parts.plan(['tongs.stl', 'tongs_flat.stl', 'stand.stl'])
    assert [plan['names'][i] for i in plan['parts']] == ['stand']
    [group] = plan['groups']
    assert group['subject'] == 'tongs'
    assert [o['label'] for o in group['options']] == ['standard', 'flat']
