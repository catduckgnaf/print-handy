"""Offline tests for one-bed merging of multi-plate 3MF sources."""
import json
import re
import zipfile
from pathlib import Path

import pytest

from print_handy.core import model_job
from print_handy.core import plate_merge
from print_handy.core import source_colors

STATE = Path(__file__).parent / 'fixtures' / 'printer_hardware.json'


def _box(w, d, h):
    pts = [(0, 0, 0), (w, 0, 0), (0, d, 0), (0, 0, h)]
    body = ''.join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in pts)
    return f'<model unit="millimeter"><resources><object id="1"><mesh><vertices>{body}</vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources></model>'


def _source(path, sizes, plates, notes=None):
    """sizes {object_id: (w, d, h)}; plates [[object ids], ...]. All objects use extruder 1."""
    resources, items, objects = [], [], []
    for oid, (w, d, h) in sizes.items():
        resources.append(f'<object id="{oid}" name="Part {oid}"><components><component p:path="/3D/Objects/object_{oid}.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>')
        items.append(f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {20 + 5 * int(oid)} 30 0"/>')
        objects.append(f'<object id="{oid}"><metadata key="name" value="Part {oid}"/><metadata key="extruder" value="1"/><part id="1"><metadata key="name" value="Part {oid}"/><metadata key="extruder" value="1"/></part></object>')
    plate_xml = ''.join(
        f'<plate><metadata key="plater_id" value="{i}"/>' +
        ''.join(f'<model_instance><metadata key="object_id" value="{o}"/></model_instance>' for o in ids) + '</plate>'
        for i, ids in enumerate(plates, 1))
    meta = f'<metadata name="Description">{notes}</metadata>' if notes else ''
    with zipfile.ZipFile(path, 'w') as archive:
        for oid, size in sizes.items():
            archive.writestr(f'3D/Objects/object_{oid}.model', _box(*size))
        archive.writestr('3D/3dmodel.model', f'<model xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" unit="millimeter">{meta}<resources>{"".join(resources)}</resources><build>{"".join(items)}</build></model>')
        archive.writestr('Metadata/model_settings.config', f'<config>{"".join(objects)}{plate_xml}</config>')
        archive.writestr('Metadata/project_settings.config', '{\n  "filament_colour": [\n    "#FFFFFF"\n  ],\n  "filament_type": [\n    "PLA"\n  ]\n}')
        archive.writestr('3D/_rels/3dmodel.model.rels', '<Relationships>' + ''.join(f'<Relationship Target="/3D/Objects/object_{o}.model" Id="r{o}"/>' for o in sizes) + '</Relationships>')
        for i in range(1, len(plates) + 1):
            archive.writestr(f'Metadata/plate_{i}.png', b'png')


def _job(tmp_path, source, plate_mode=None):
    state = json.loads(STATE.read_text())
    manifest = source_colors.inventory(source)
    white = model_job.resolve_filament_selection(state, dict(state['filament_slots']['1'], slot=1))
    job = {'downloaded_file': str(source), 'audit': {'plates': len(set(re.findall(r'plate_(\d+)\.png', ' '.join(zipfile.ZipFile(source).namelist()))))},
           'color_decisions': {'schema': 'part_slots_v1', 'mode': 'single_color', 'source_sha256': manifest['source_sha256'], 'selection': white}}
    if plate_mode:
        job['plate_mode'] = plate_mode
    path = tmp_path / 'JOB.json'
    path.write_text(json.dumps(job))
    return path


def _fake_slice(estimate='2h 10m 0s'):
    calls = []

    def fake(source, folder, state, brim=None, first_layer=False, color_decisions=None, preserve_layout=False, filament_slot=None, item_shifts=None):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source) as archive:
            model = archive.read('3D/3dmodel.model').decode()
        printable = re.findall(r'<item objectid="(\d+)"[^>]*printable="1"', model)
        slot = state['filament_slots'][str(filament_slot)]
        out = folder / 'plate_1.gcode'
        labels = ''.join(f'; printing object Part {o} id:{i} copy 0\n' for i, o in enumerate(printable))
        out.write_text(f'; filament_colour = {slot["hex"]}\n; nozzle_diameter = 0.4\n; estimated printing time (normal mode) = {estimate}\n'
                       f'M190 S{state["temps_c"]["bed"]}\nM104 S{state["temps_c"]["nozzle"]} T0\n'
                       f'START_PRINT BED_TEMP={state["temps_c"]["bed"]} EXTRUDER_TEMP={state["temps_c"]["nozzle"]}\n{labels}G1 X10 Y10 Z0.2\nEND_PRINT\n')
        calls.append({'source': str(source), 'printable': printable, 'slot': filament_slot, 'item_shifts': item_shifts})
        return out, {'slot': filament_slot}, []
    fake.calls = calls
    return fake


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(model_job.printer_state, 'check', lambda *a, **k: (_ for _ in ()).throw(AssertionError('slice contacted the printer')))


def test_fits_is_merged_into_one_gcode_with_every_object(tmp_path, monkeypatch):
    source = tmp_path / 's.3mf'
    _source(source, {'1': (60, 40, 10), '2': (50, 50, 8), '3': (30, 30, 5)}, [['1', '2'], ['3']])
    job_path = _job(tmp_path, source)
    fake = _fake_slice()
    monkeypatch.setattr('print_handy.core.orca_adapter.slice_model', fake)
    out = model_job.slice_job(job_path)
    saved = json.loads(job_path.read_text())
    assert isinstance(out, (str, Path)) and len(fake.calls) == 1
    assert sorted(fake.calls[0]['printable']) == ['1', '2', '3']
    merge = saved['plate_merge']
    assert (merge['mode'], merge['plates_in'], merge['objects_in'], merge['objects_out'], merge['fits_bed']) == ('merge', 2, 3, 3, True)
    assert 'derived_plates' not in saved and saved['gcode_verified'] is True
    # spacing between every pair of placed footprints
    boxes = list(merge['placed_bounds_mm'].values())
    for i, a in enumerate(boxes):
        assert a[0][0] >= plate_merge.EDGE_MARGIN_MM - 1e-6 and a[1][0] >= plate_merge.EDGE_MARGIN_MM - 1e-6
        assert a[0][1] <= 220 - plate_merge.EDGE_MARGIN_MM + 1e-6 and a[1][1] <= 220 - plate_merge.EDGE_MARGIN_MM + 1e-6
        for b in boxes[i + 1:]:
            gap_x = max(b[0][0] - a[0][1], a[0][0] - b[0][1])
            gap_y = max(b[1][0] - a[1][1], a[1][0] - b[1][1])
            assert max(gap_x, gap_y) >= plate_merge.SPACING_MM - 1e-6
    model_job.require_current_artifact(saved, out)


def test_too_big_splits_with_reason_and_still_covers_every_part(tmp_path, monkeypatch):
    source = tmp_path / 's.3mf'
    _source(source, {'1': (150, 150, 10), '2': (150, 150, 10), '3': (30, 30, 5)}, [['1'], ['2', '3']])
    job_path = _job(tmp_path, source)
    fake = _fake_slice()
    monkeypatch.setattr('print_handy.core.orca_adapter.slice_model', fake)
    paths = model_job.slice_job(job_path)
    saved = json.loads(job_path.read_text())
    assert len(paths) == 2 and len(fake.calls) == 2
    assert saved['plate_merge']['mode'] == 'split' and 'do not fit' in saved['plate_merge']['reason']
    assert sorted(k for row in saved['derived_plates'] for k in row['part_keys']) == ['1:1', '2:1', '3:1']
    model_job.require_current_artifact(saved, paths[1])


def test_estimate_over_twelve_hours_splits_with_reason(tmp_path, monkeypatch):
    source = tmp_path / 's.3mf'
    _source(source, {'1': (40, 40, 10), '2': (40, 40, 10)}, [['1'], ['2']])
    job_path = _job(tmp_path, source)
    seq = iter(['13h 5m 0s', '6h 0m 0s', '6h 0m 0s'])
    calls = []

    def fake(*args, **kwargs):
        calls.append(Path(args[0]).name)
        return _fake_slice(next(seq))(*args, **kwargs)
    monkeypatch.setattr('print_handy.core.orca_adapter.slice_model', fake)
    paths = model_job.slice_job(job_path)
    saved = json.loads(job_path.read_text())
    assert len(paths) == 2 and calls[0] == 'merged.3mf' and 'merged' not in ''.join(calls[1:])
    assert saved['plate_merge']['mode'] == 'split' and '12 h' in saved['plate_merge']['reason']


def test_author_note_requests_separate_printing(tmp_path, monkeypatch):
    source = tmp_path / 's.3mf'
    _source(source, {'1': (40, 40, 10), '2': (40, 40, 10)}, [['1'], ['2']], notes='Please print the parts separately, one at a time.')
    job_path = _job(tmp_path, source)
    fake = _fake_slice()
    monkeypatch.setattr('print_handy.core.orca_adapter.slice_model', fake)
    paths = model_job.slice_job(job_path)
    saved = json.loads(job_path.read_text())
    assert len(paths) == 2
    assert 'author notes' in saved['plate_merge']['reason']


def test_full_size_split_possible_applies_the_brim_budget(tmp_path):
    """P2-79: the legacy split fallback ignored the job's brim, so 'Split
    plates at 100%' was offered for parts whose brims would cross the bed
    edge — the same parts the brim-aware check had already refused."""
    bed = 220
    width = bed - 2 * plate_merge.EDGE_MARGIN_MM - 2  # fits the plain edge budget, barely
    source = tmp_path / 's.3mf'
    _source(source, {'1': (width, 40, 10), '2': (40, 40, 10)}, [['1'], ['2']])
    manifest = source_colors.inventory(source)
    assert plate_merge.full_size_split_possible(source, manifest, bed=bed)
    policy = {'type': 'outer_only', 'width_mm': 3.0}
    assert not plate_merge.full_size_split_possible(source, manifest, bed=bed, brim_policy=policy)
    assert not plate_merge.full_size_split_possible(source, manifest, bed=bed,
                                                     state={'brim_policy': policy})


def test_explicit_separate_mode_skips_merge(tmp_path, monkeypatch):
    source = tmp_path / 's.3mf'
    _source(source, {'1': (40, 40, 10), '2': (40, 40, 10)}, [['1'], ['2']])
    assert plate_merge.full_size_split_possible(source, source_colors.inventory(source))
    _source(source, {'1': (220, 40, 10), '2': (40, 40, 10)}, [['1'], ['2']])
    assert not plate_merge.full_size_split_possible(source, source_colors.inventory(source))
    _source(source, {'1': (40, 40, 10), '2': (40, 40, 10)}, [['1', '2']])
    assert not plate_merge.full_size_split_possible(source, source_colors.inventory(source))
    _source(source, {'1': (40, 40, 10), '2': (40, 40, 10)}, [['1'], ['2']])
    job_path = _job(tmp_path, source, plate_mode='separate')
    monkeypatch.setattr('print_handy.core.orca_adapter.slice_model', _fake_slice())
    assert len(model_job.slice_job(job_path)) == 2
    assert json.loads(job_path.read_text())['plate_merge']['reason'] == 'Print parts separately was chosen'


def test_pack_refuses_when_nothing_fits_and_is_deterministic():
    assert plate_merge.pack({'a': (250, 10)}, 220) is None
    assert plate_merge.pack({'a': (100, 100), 'b': (100, 100), 'c': (100, 100)}, 220) is None
    first = plate_merge.pack({'a': (90, 90), 'b': (90, 90), 'c': (40, 40)}, 220)
    assert first == plate_merge.pack({'a': (90, 90), 'b': (90, 90), 'c': (40, 40)}, 220)


def test_missing_object_in_merged_output_is_refused(tmp_path, monkeypatch):
    source = tmp_path / 's.3mf'
    _source(source, {'1': (40, 40, 10), '2': (40, 40, 10)}, [['1'], ['2']])
    job_path = _job(tmp_path, source)
    real = _fake_slice()

    def drop_one(*args, **kwargs):
        out, prov, cfg = real(*args, **kwargs)
        out.write_text(out.read_text().replace('; printing object Part 2 id:1 copy 0\n', ''))
        return out, prov, cfg
    monkeypatch.setattr('print_handy.core.orca_adapter.slice_model', drop_one)
    with pytest.raises(RuntimeError, match='prints 1 objects but the source has 2'):
        model_job.slice_job(job_path)


def _two_slot_decisions(manifest):
    """Parts 1 and 3 print in slot 1, part 2 in slot 2 (a real multi-slot source)."""
    state = json.loads(STATE.read_text())
    sel = {s: model_job.resolve_filament_selection(state, dict(state['filament_slots'][s], slot=int(s))) for s in ('1', '2')}
    choices = {}
    for part in manifest['parts']:
        choices['part:' + part['key']] = sel['2'] if part['object_id'] == '2' else sel['1']
    for row in source_colors.canonical_prompts(manifest):
        choices.setdefault(row['key'], choices.get(row['key']) or sel['1'])
    return {'schema': 'part_slots_v1', 'mode': 'per_part', 'source_sha256': manifest['source_sha256'], 'choices': choices}


def test_multi_slot_merge_collapses_plates_and_parks_other_slot(tmp_path):
    """Regression for real Orca -50 (empty plate 2) and -52 (printable=0 item overlapping the bed)."""
    from print_handy.core import color_geometry
    source = tmp_path / 's.3mf'
    _source(source, {'1': (60, 40, 10), '2': (50, 50, 8), '3': (30, 30, 5)}, [['1'], ['2'], ['3']])
    manifest = source_colors.inventory(source)
    decisions = _two_slot_decisions(manifest)
    keys = [p['key'] for p in manifest['parts'] if p['object_id'] in ('1', '3')]
    shifts = {'1': [0, 0, 0], '3': [80, 0, 0]}
    target = tmp_path / 'slot1.3mf'
    report = color_geometry.export_single_color_plate(source, target, decisions, keys, item_shifts=shifts)
    assert report['slot'] == 1 and report['object_ids'] == ['1', '3'] and report['merged_placement'] is True
    with zipfile.ZipFile(target) as archive:
        settings = archive.read('Metadata/model_settings.config').decode()
        model = archive.read('3D/3dmodel.model').decode()
    # every selected object sits on the single remaining plate; no empty plate is left behind
    assert settings.count('<plate>') == 1
    plate = re.search(r'<plate>.*?</plate>', settings, re.S).group(0)
    assert sorted(re.findall(r'key="object_id" value="(\d+)"', plate)) == ['1', '3']
    assert '"2"' not in plate
    # the slot 2 object is printable=0 and parked outside the 220 mm bed (+50 mm margin)
    items = {m[0]: m[1] for m in re.findall(r'<item objectid="(\d+)"([^>]*)>', model)}
    assert 'printable="1"' in items['1'] and 'printable="1"' in items['3'] and 'printable="0"' in items['2']
    parked_x = float(re.search(r'transform="([^"]+)"', items['2']).group(1).split()[9])
    assert parked_x >= 220 + 50
    # selected shifts applied per object
    x1 = float(re.search(r'transform="([^"]+)"', items['1']).group(1).split()[9])
    x3 = float(re.search(r'transform="([^"]+)"', items['3']).group(1).split()[9])
    assert x3 - x1 == pytest.approx((20 + 5 * 3 + 80) - (20 + 5 * 1))


def test_multi_slot_merge_other_slot_gets_its_own_plate(tmp_path):
    from print_handy.core import color_geometry
    source = tmp_path / 's.3mf'
    _source(source, {'1': (60, 40, 10), '2': (50, 50, 8), '3': (30, 30, 5)}, [['1'], ['2'], ['3']])
    manifest = source_colors.inventory(source)
    decisions = _two_slot_decisions(manifest)
    key2 = [p['key'] for p in manifest['parts'] if p['object_id'] == '2']
    target = tmp_path / 'slot2.3mf'
    report = color_geometry.export_single_color_plate(source, target, decisions, key2)
    assert report['slot'] == 2 and report['object_ids'] == ['2']
    with zipfile.ZipFile(target) as archive:
        settings = archive.read('Metadata/model_settings.config').decode()
    assert settings.count('<plate>') == 1 and 'value="2"' in settings


def test_multi_slot_item_shifts_must_name_exactly_selected_objects(tmp_path):
    from print_handy.core import color_geometry
    source = tmp_path / 's.3mf'
    _source(source, {'1': (60, 40, 10), '2': (50, 50, 8)}, [['1'], ['2']])
    manifest = source_colors.inventory(source)
    decisions = _two_slot_decisions(manifest)
    keys = [p['key'] for p in manifest['parts'] if p['object_id'] == '1']
    with pytest.raises(RuntimeError, match='item shifts must name exactly'):
        color_geometry.export_single_color_plate(source, tmp_path / 'x.3mf', decisions, keys, item_shifts={'1': [0, 0, 0], '2': [0, 0, 0]})


# --- #221 P1: polygon (convex hull) merge fallback ---------------------------

TRI_A = [(0, 0), (180, 0), (0, 110)]          # right triangle, right angle at origin
TRI_B = [(0, 110), (180, 0), (180, 110)]      # its complement in the same 180x110 rectangle


def _prism_model(poly, h):
    body = ''.join(f'<vertex x="{x}" y="{y}" z="0"/>' for x, y in poly)
    body += f'<vertex x="{poly[0][0]}" y="{poly[0][1]}" z="{h}"/>'
    top = len(poly)
    tris = ''.join(f'<triangle v1="0" v2="{i}" v3="{i + 1}"/>' for i in range(1, len(poly) - 1))
    tris += f'<triangle v1="0" v2="1" v3="{top}"/>'
    return f'<model unit="millimeter"><resources><object id="1"><mesh><vertices>{body}</vertices><triangles>{tris}</triangles></mesh></object></resources></model>'


def _prism_source(path, shapes, plates):
    """shapes {object_id: (poly_xy, h, (tx, ty))}; plates [[object ids], ...]."""
    resources, items, objects = [], [], []
    for oid, (poly, h, at) in shapes.items():
        resources.append(f'<object id="{oid}" name="Part {oid}"><components><component p:path="/3D/Objects/object_{oid}.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>')
        items.append(f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {at[0]} {at[1]} 0"/>')
        objects.append(f'<object id="{oid}"><metadata key="name" value="Part {oid}"/><metadata key="extruder" value="1"/><part id="1"><metadata key="name" value="Part {oid}"/><metadata key="extruder" value="1"/></part></object>')
    plate_xml = ''.join(
        f'<plate><metadata key="plater_id" value="{i}"/>' +
        ''.join(f'<model_instance><metadata key="object_id" value="{o}"/></model_instance>' for o in ids) + '</plate>'
        for i, ids in enumerate(plates, 1))
    with zipfile.ZipFile(path, 'w') as archive:
        for oid, (poly, h, at) in shapes.items():
            archive.writestr(f'3D/Objects/object_{oid}.model', _prism_model(poly, h))
        archive.writestr('3D/3dmodel.model', f'<model xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" unit="millimeter"><resources>{"".join(resources)}</resources><build>{"".join(items)}</build></model>')
        archive.writestr('Metadata/model_settings.config', f'<config>{"".join(objects)}{plate_xml}</config>')
        archive.writestr('Metadata/project_settings.config', '{\n  "filament_colour": [\n    "#FFFFFF"\n  ],\n  "filament_type": [\n    "PLA"\n  ]\n}')
        archive.writestr('3D/_rels/3dmodel.model.rels', '<Relationships>' + ''.join(f'<Relationship Target="/3D/Objects/object_{o}.model" Id="r{o}"/>' for o in shapes) + '</Relationships>')
        for i in range(1, len(plates) + 1):
            archive.writestr(f'Metadata/plate_{i}.png', b'png')


def _plan(source):
    state = json.loads(STATE.read_text())
    manifest = source_colors.inventory(source)
    plates = model_job.plate_manifest(source)['plates']
    slots = {p['key']: 1 for p in manifest['parts']}
    return plate_merge.plan(source, manifest, plates, slots, state)


def test_plan_reserves_the_fitted_nozzles_tower_width(tmp_path, monkeypatch):
    # P2-34: the merge planner reserves room for the tower the slice will
    # actually emit — a 0.6 mm nozzle's 60 mm tower, not the 45 mm default.
    source = tmp_path / 'two.3mf'
    _source(source, {'1': (60, 60, 10), '2': (60, 60, 10)}, [['1'], ['2']])
    state = json.loads(STATE.read_text())
    state['nozzle']['diameter_mm'] = 0.6
    manifest = source_colors.inventory(source)
    plates = model_job.plate_manifest(source)['plates']
    slots = {p['key']: i + 1 for i, p in enumerate(manifest['parts'])}
    seen = []
    real_pack = plate_merge.pack

    def spy_pack(extents, bed, spacing=None, edge=None, reserve_y=0.0):
        seen.append(reserve_y)
        return real_pack(extents, bed, spacing, edge=edge, reserve_y=reserve_y)
    monkeypatch.setattr(plate_merge, 'pack', spy_pack)
    plate_merge.plan(source, manifest, plates, slots, state)
    expected = (60 + plate_merge.TOWER_CLEARANCE_MM + plate_merge.TOWER_SLACK_MM
                + plate_merge.EDGE_MARGIN_MM)
    assert max(seen) == pytest.approx(expected)


def _placed_hulls(source, verdict):
    from print_handy.core import color_geometry
    placed = {}
    with zipfile.ZipFile(source) as archive:
        for oid, shift in verdict['shifts'].items():
            hull, _bounds = color_geometry._world_hull_xy(archive, {oid})[0]
            placed[oid] = [(x + shift[0], y + shift[1]) for x, y in hull]
    return placed


def test_pack_polygon_merges_complementary_triangles_that_rectangles_cannot():
    assert plate_merge.pack({'a': (180, 110), 'b': (180, 110)}, 220) is None
    placed = plate_merge.pack_polygon({'a': TRI_A, 'b': TRI_B}, 220)
    assert placed is not None and set(placed) == {'a', 'b'}
    hulls = {'a': TRI_A, 'b': TRI_B}
    polys = {k: [(x + placed[k][0], y + placed[k][1]) for x, y in hulls[k]] for k in placed}
    assert plate_merge.hull_distance(polys['a'], polys['b']) >= plate_merge.SPACING_MM - 1e-9
    for poly in polys.values():
        assert all(plate_merge.EDGE_MARGIN_MM - 1e-9 <= v <= 220 - plate_merge.EDGE_MARGIN_MM + 1e-9
                   for point in poly for v in point)


def test_pack_polygon_agrees_with_pack_for_rectangular_hulls():
    cases = [
        {'a': (90, 90), 'b': (90, 90), 'c': (40, 40)},
        {'a': (100, 100), 'b': (100, 100), 'c': (100, 100)},
        {'a': (250, 10)},
        {'a': (60, 40), 'b': (50, 50)},
        {'a': (200, 200)},
    ]
    for extents in cases:
        hulls = {k: [(0, 0), (w, 0), (w, d), (0, d)] for k, (w, d) in extents.items()}
        assert (plate_merge.pack_polygon(hulls, 220) is not None) == (plate_merge.pack(extents, 220) is not None), extents


def test_hull_distance_touching_apart_and_overlapping():
    a = [(0, 0), (10, 0), (10, 10), (0, 10)]
    touching = [(10, 0), (20, 0), (20, 10), (10, 10)]
    apart = [(25, 0), (35, 0), (35, 10), (25, 10)]
    overlapping = [(5, 5), (15, 5), (15, 15), (5, 15)]
    assert plate_merge.hull_distance(a, touching) == 0.0
    assert not plate_merge._polygons_overlap(a, touching)
    assert plate_merge.hull_distance(a, apart) == pytest.approx(15.0)
    assert plate_merge.hull_distance(a, overlapping) == 0.0
    assert plate_merge._polygons_overlap(a, overlapping)


def test_pack_polygon_degenerate_hull_falls_back_to_its_aabb():
    placed = plate_merge.pack_polygon({'line': [(0, 0), (100, 0)],
                                       'sq': [(0, 0), (60, 0), (60, 60), (0, 60)]}, 220)
    assert placed is not None and set(placed) == {'line', 'sq'}


def test_pack_polygon_feature_budget_replaces_spacing_and_edge_once():
    placed = plate_merge.pack_polygon({'a': TRI_A, 'b': TRI_B}, 220, feature=6.0)
    assert placed is not None
    hulls = {'a': TRI_A, 'b': TRI_B}
    polys = {k: [(x + placed[k][0], y + placed[k][1]) for x, y in hulls[k]] for k in placed}
    # hull-to-hull is spacing + 2*feature; hull-to-edge is edge + feature
    assert plate_merge.hull_distance(polys['a'], polys['b']) >= plate_merge.SPACING_MM + 12.0 - 1e-9
    for poly in polys.values():
        assert all(v >= plate_merge.EDGE_MARGIN_MM + 6.0 - 1e-9 for point in poly for v in point)
    # a budget so large the AABBs alone exceed the shrunken usable area refuses
    assert plate_merge.pack_polygon({'a': TRI_A, 'b': TRI_B}, 220, feature=20.0) is None


def test_pack_polygon_search_budget_exhaustion_is_todays_verdict():
    assert plate_merge.pack_polygon({'a': TRI_A, 'b': TRI_B}, 220, max_nodes=1) is None


def test_pack_polygon_is_deterministic():
    first = plate_merge.pack_polygon({'a': TRI_A, 'b': TRI_B}, 220)
    assert first == plate_merge.pack_polygon({'b': TRI_B, 'a': TRI_A}, 220) == \
        plate_merge.pack_polygon({'a': TRI_A, 'b': TRI_B}, 220)


def test_pack_polygon_respects_exclusions_and_reserve():
    sq = [(0, 0), (60, 0), (60, 60), (0, 60)]
    placed = plate_merge.pack_polygon({'a': sq, 'b': sq}, 220, exclusions=[(8, 8, 100, 100)])
    assert placed is not None
    for corner in placed.values():
        poly = [(x + corner[0], y + corner[1]) for x, y in sq]
        assert not plate_merge._polygons_overlap(poly, plate_merge._rect_polygon((8, 8, 100, 100)))
    big = [(0, 0), (200, 0), (200, 200), (0, 200)]
    assert plate_merge.pack_polygon({'a': big}, 220, exclusions=[(8, 8, 100, 100)]) is None
    # the prime-tower reserve strip stays unusable to the polygon packer too
    assert plate_merge.pack_polygon({'a': TRI_A, 'b': TRI_B}, 220, reserve_y=100.0) is None
    assert plate_merge.pack_polygon({'a': TRI_A, 'b': TRI_B}, 220, reserve_y=0.0) is not None


def test_plan_merges_triangles_via_polygon_fallback(tmp_path):
    source = tmp_path / 'tri.3mf'
    _prism_source(source, {'1': (TRI_A, 10, (20, 30)), '2': (TRI_B, 10, (100, 90))}, [['1'], ['2']])
    verdict = _plan(source)
    assert verdict['mode'] == 'merge' and verdict['packing'] == 'polygon'
    placed = _placed_hulls(source, verdict)
    ids = sorted(placed)
    assert plate_merge.hull_distance(placed[ids[0]], placed[ids[1]]) >= plate_merge.SPACING_MM - 1e-9


def test_plan_rectangle_only_sources_keep_todays_verdict(tmp_path):
    rect = [(0, 0), (90, 0), (90, 90), (0, 90)]
    source = tmp_path / 'rect.3mf'
    _prism_source(source, {'1': (rect, 10, (0, 0)), '2': (rect, 10, (120, 0))}, [['1'], ['2']])
    verdict = _plan(source)
    assert verdict['mode'] == 'merge' and verdict['packing'] == 'rectangle'
    # rectangle placements are exactly the shelf packer's corners as shifts
    corners = plate_merge.pack({'1': (90, 90), '2': (90, 90)}, 220)
    for oid, (px, py) in corners.items():
        assert verdict['shifts'][oid][:2] == pytest.approx([px - (0 if oid == '1' else 120), py])
    big = [(0, 0), (150, 0), (150, 150), (0, 150)]
    _prism_source(source, {'1': (big, 10, (0, 0)), '2': (big, 10, (0, 0))}, [['1'], ['2']])
    verdict = _plan(source)
    assert verdict['mode'] == 'split' and 'do not fit' in verdict['reason'] and 'packing' not in verdict


# --- #221 R2b: per-object yaw on the one-bed merge path ---------------------
# A 215 x 35 mm bar fits the 204 mm usable bed only turned (45 degrees, a
# ~176.8 mm envelope); a 60 mm square fits as authored. Before R2b the merge
# planner split this source: the bar exceeds the usable width as authored, so
# rectangle packing fails, and both hulls are rectangles, so the polygon
# fallback does not even run. The yawed fallback packs the bar's hull rotated
# to its recorded yaw and the square into the notch beside it.

YAW_BAR = [(0, 0), (215, 0), (215, 35), (0, 35)]
YAW_SQ = [(0, 0), (60, 0), (60, 60), (0, 60)]


def _yaw_merge_source(path):
    _prism_source(path, {'1': (YAW_BAR, 10, (0, 0)), '2': (YAW_SQ, 10, (300, 300))}, [['1'], ['2']])


def _placed_hull(source, verdict, oid):
    from print_handy.core import color_geometry, split_fit
    with zipfile.ZipFile(source) as archive:
        hull, _bounds = color_geometry._world_hull_xy(archive, {oid})[0]
    if oid in (verdict.get('yaw_by_object') or {}):
        hull = split_fit._rotate_hull_xy(hull, verdict['yaw_by_object'][oid])
    shift = verdict['shifts'][oid]
    return [(x + shift[0], y + shift[1]) for x, y in hull]


def test_plan_yawed_merge_turns_a_part_that_only_fits_turned(tmp_path):
    source = tmp_path / 'yaw-merge.3mf'
    _yaw_merge_source(source)
    # The authored verdicts this fallback supersedes: no rectangle packing,
    # and rectangle hulls mean the polygon fallback never runs.
    assert plate_merge.pack({'1': (215, 35), '2': (60, 60)}, 220) is None
    verdict = _plan(source)
    assert verdict['mode'] == 'merge' and verdict['packing'] == 'polygon'
    assert verdict['yaw_by_object'] == {'1': 45.0}
    placed = {oid: _placed_hull(source, verdict, oid) for oid in ('1', '2')}
    assert plate_merge.hull_distance(placed['1'], placed['2']) >= plate_merge.SPACING_MM - 2e-3
    for poly in placed.values():
        assert all(plate_merge.EDGE_MARGIN_MM - 2e-3 <= v <= 220 - plate_merge.EDGE_MARGIN_MM + 2e-3
                   for point in poly for v in point)
    # The bar's placed bounds are its rotated extents, (215 + 35) / sqrt(2).
    bounds = verdict['placed_bounds_mm']['1']
    assert bounds[0][1] - bounds[0][0] == pytest.approx(176.777, abs=0.01)
    assert bounds[1][1] - bounds[1][0] == pytest.approx(176.777, abs=0.01)
    # The square keeps its authored extents and no yaw record.
    bounds = verdict['placed_bounds_mm']['2']
    assert bounds[0][1] - bounds[0][0] == 60 and bounds[1][1] - bounds[1][0] == 60
    assert _plan(source) == verdict


def test_plan_merges_that_need_no_turn_record_no_yaw(tmp_path):
    # Authored rectangle merge wins first and stays exactly today's plan.
    source = tmp_path / 'auth.3mf'
    _prism_source(source, {'1': ([(0, 0), (180, 0), (180, 35), (0, 35)], 10, (0, 0)),
                           '2': (YAW_SQ, 10, (300, 300))}, [['1'], ['2']])
    verdict = _plan(source)
    assert verdict['mode'] == 'merge' and verdict['packing'] == 'rectangle'
    assert 'yaw_by_object' not in verdict
    # The authored polygon fallback likewise records no yaw.
    source = tmp_path / 'tri.3mf'
    _prism_source(source, {'1': (TRI_A, 10, (20, 30)), '2': (TRI_B, 10, (100, 90))}, [['1'], ['2']])
    verdict = _plan(source)
    assert verdict['mode'] == 'merge' and verdict['packing'] == 'polygon'
    assert 'yaw_by_object' not in verdict


def test_plan_yawed_merge_still_refuses_a_part_that_fits_at_no_yaw(tmp_path):
    # A 250 x 40 mm bar fits the usable bed at no checked yaw (its best
    # envelope is ~205 mm), so the yawed fallback cannot merge it either.
    source = tmp_path / 'never.3mf'
    _prism_source(source, {'1': ([(0, 0), (250, 0), (250, 40), (0, 40)], 10, (0, 0)),
                           '2': (YAW_SQ, 10, (300, 300))}, [['1'], ['2']])
    verdict = _plan(source)
    assert verdict['mode'] == 'split' and 'do not fit' in verdict['reason']
    assert 'yaw_by_object' not in verdict


def test_pack_polygon_rotated_hulls_charge_feature_and_exclusions_once():
    from print_handy.core import split_fit
    bar = split_fit._rotate_hull_xy(YAW_BAR, 45.0)
    placed = plate_merge.pack_polygon({'a': bar, 'b': YAW_SQ}, 220, feature=6.0,
                                      exclusions=[(190, 8, 212, 60)])
    assert placed is not None

    def poly(hull, corner):
        min_x = min(p[0] for p in hull)
        min_y = min(p[1] for p in hull)
        return [(x - min_x + corner[0], y - min_y + corner[1]) for x, y in hull]

    polys = {'a': poly(bar, placed['a']), 'b': poly(YAW_SQ, placed['b'])}
    # Charged exactly once, as for authored hulls: hull-to-hull is
    # spacing + 2*feature, hull-to-edge is edge + feature, hull-to-exclusion
    # is feature.
    assert plate_merge.hull_distance(polys['a'], polys['b']) >= plate_merge.SPACING_MM + 12.0 - 1e-9
    exclusion = plate_merge._rect_polygon((190, 8, 212, 60))
    for placed_poly in polys.values():
        assert plate_merge.hull_distance(placed_poly, exclusion) >= 6.0 - 1e-9
        assert all(plate_merge.EDGE_MARGIN_MM + 6.0 - 1e-9 <= v
                   <= 220 - plate_merge.EDGE_MARGIN_MM - 6.0 + 1e-9
                   for point in placed_poly for v in point)
    # A budget so large the rotated envelope alone exceeds the shrunken
    # usable area refuses, rather than stacking or ignoring the budget.
    assert plate_merge.pack_polygon({'a': bar}, 220, feature=20.0) is None


def test_yawed_merge_slices_to_one_gcode_at_the_recorded_turn(tmp_path, monkeypatch):
    from print_handy.core import color_geometry
    import math
    source = tmp_path / 'yaw-merge.3mf'
    _yaw_merge_source(source)
    verdict = _plan(source)
    assert verdict['yaw_by_object'] == {'1': 45.0}
    job_path = _job(tmp_path, source)
    fake = _fake_slice()
    monkeypatch.setattr('print_handy.core.orca_adapter.slice_model', fake)
    out = model_job.slice_job(job_path)
    saved = json.loads(job_path.read_text())
    assert isinstance(out, (str, Path)) and len(fake.calls) == 1
    assert saved['plate_merge']['mode'] == 'merge'
    assert saved['plate_merge']['yaw_by_object'] == {'1': 45.0}
    assert set(saved['merged']['plate_report']['fit_by_object']) == {'1'}
    # Derived-plate re-read proof, per object: the bar landed at its rotated
    # authored bounds plus its recorded shift, with the rotated extents; the
    # square landed at its authored bounds plus its shift.
    derived = Path(saved['merged']['plate'])
    shifts = verdict['shifts']
    with zipfile.ZipFile(source) as archive:
        authored_points = color_geometry._world_points(archive, {'1'})
        authored_square = color_geometry._world_bounds(archive, {'2'})[0]
    rad = math.radians(45.0)
    c, s = math.cos(rad), math.sin(rad)
    rotated = [(x * c - y * s, x * s + y * c, z) for x, y, z in authored_points]
    rotated_min = [min(p[a] for p in rotated) for a in range(3)]
    rotated_extent = [max(p[a] for p in rotated) - rotated_min[a] for a in range(3)]
    with zipfile.ZipFile(derived) as archive:
        landed_bar = color_geometry._world_bounds(archive, {'1'})[0]
        landed_square = color_geometry._world_bounds(archive, {'2'})[0]
    for a in range(3):
        assert landed_bar[a][0] == pytest.approx(rotated_min[a] + shifts['1'][a], abs=2e-3)
        assert landed_bar[a][1] - landed_bar[a][0] == pytest.approx(rotated_extent[a], abs=2e-3)
        assert landed_square[a][0] == pytest.approx(authored_square[a][0] + shifts['2'][a], abs=2e-3)
    placed = {oid: _placed_hull(source, verdict, oid) for oid in ('1', '2')}
    assert plate_merge.hull_distance(placed['1'], placed['2']) >= plate_merge.SPACING_MM - 2e-3


def test_slicing_a_merge_refuses_unproved_yaws(tmp_path, monkeypatch):
    source = tmp_path / 'yaw-merge.3mf'
    _yaw_merge_source(source)
    good = _plan(source)
    assert good['mode'] == 'merge' and good['yaw_by_object'] == {'1': 45.0}
    bad = [
        ({'yaw_by_object': {'9': 30.0}}, 'unknown'),
        ({'yaw_by_object': {'1': float('inf')}}, 'between 0 and 90'),
        ({'yaw_by_object': {'1': 0.0}}, 'between 0 and 90'),
        ({'yaw_by_object': [('1', 30.0)]}, 'must be a mapping'),
        ({'yaw_by_object': {'1': 30.0}, 'shifts': None}, 'without the recorded shifts'),
    ]
    for overrides, match in bad:
        verdict = {**good, **overrides}
        monkeypatch.setattr(plate_merge, 'plan', lambda *a, **k: verdict)
        job_path = _job(tmp_path, source)
        fake = _fake_slice()
        monkeypatch.setattr('print_handy.core.orca_adapter.slice_model', fake)
        with pytest.raises(RuntimeError, match=match):
            model_job.slice_job(job_path)
        assert fake.calls == []


# The multicolor exporter additionally requires a prime-tower position
# clearing every placed AABB by its clearance margin, so its fixtures use
# a thinner turned strip (206 x 4 mm, a ~148 mm rotated envelope) whose
# placed AABB leaves a tower lane on the bed.
YAW_BAR_SMALL = [(0, 0), (206, 0), (206, 4), (0, 4)]
YAW_SQ_SMALL = [(0, 0), (40, 0), (40, 40), (0, 40)]


def _yaw_merge_small_source(path):
    _prism_source(path, {'1': (YAW_BAR_SMALL, 10, (0, 0)), '2': (YAW_SQ_SMALL, 10, (300, 300))}, [['1'], ['2']])


def test_multicolor_merge_export_rotates_the_turned_object(tmp_path):
    from print_handy.core import color_geometry
    from print_handy.core.orca_adapter import _rotate, _yaw
    source = tmp_path / 'yaw-merge.3mf'
    _yaw_merge_small_source(source)
    verdict = _plan(source)
    assert verdict['yaw_by_object'] == {'1': 45.0}
    manifest = source_colors.inventory(source)
    decisions = _two_slot_decisions(manifest)
    target = tmp_path / 'merged.3mf'
    _mapping, geometry = color_geometry.export_color_geometry(
        source, target, decisions, item_shifts=verdict['shifts'],
        fit_by_object={'1': {'rotation': _yaw(45.0)}})
    assert geometry['fit_by_object'] == {'1': {'rotation': _yaw(45.0)}}
    assert geometry['merged_placement'] is True
    records = {row['part_key']: row for row in geometry['parts']}
    object_of_key = {p['key']: str(p['object_id']) for p in manifest['parts']}
    shifts = verdict['shifts']
    with zipfile.ZipFile(source) as archive:
        authored = {oid: color_geometry._world_points(archive, {oid}) for oid in ('1', '2')}
    for key, row in records.items():
        oid = object_of_key[key]
        points = authored[oid]
        if oid == '1':
            points = [_rotate(p, _yaw(45.0)) for p in points]
        for a in range(3):
            expected = min(p[a] for p in points) + shifts[oid][a]
            assert row['bounds_mm'][a][0] == pytest.approx(expected, abs=2e-3)
            expected_extent = max(p[a] for p in points) - min(p[a] for p in points)
            assert row['bounds_mm'][a][1] - row['bounds_mm'][a][0] == pytest.approx(expected_extent, abs=2e-3)


def test_multicolor_merge_export_refuses_unproved_fits(tmp_path):
    from print_handy.core import color_geometry
    from print_handy.core.orca_adapter import _yaw
    source = tmp_path / 'yaw-merge.3mf'
    _yaw_merge_source(source)
    verdict = _plan(source)
    manifest = source_colors.inventory(source)
    decisions = _two_slot_decisions(manifest)
    target = tmp_path / 'x.3mf'
    with pytest.raises(RuntimeError, match='not exported'):
        color_geometry.export_color_geometry(source, target, decisions, item_shifts=verdict['shifts'],
                                             fit_by_object={'9': {'rotation': _yaw(30.0)}})
    with pytest.raises(RuntimeError, match='requires the recorded item shifts'):
        color_geometry.export_color_geometry(source, target, decisions,
                                             fit_by_object={'1': {'rotation': _yaw(30.0)}})
    with pytest.raises(RuntimeError, match='authored scale'):
        color_geometry.export_color_geometry(source, target, decisions, item_shifts=verdict['shifts'],
                                             fit_by_object={'1': {'rotation': _yaw(30.0), 'scale': 0.5}})
    with pytest.raises(RuntimeError, match='mirror'):
        color_geometry.export_color_geometry(source, target, decisions, item_shifts=verdict['shifts'],
                                             fit_by_object={'1': {'rotation': [-1, 0, 0, 0, 1, 0, 0, 0, 1]}})
    with pytest.raises(RuntimeError, match='requires a rotation'):
        color_geometry.export_color_geometry(source, target, decisions, item_shifts=verdict['shifts'],
                                             fit_by_object={'1': {}})
    with pytest.raises(RuntimeError, match='resized export'):
        color_geometry.export_color_geometry(source, target, decisions, item_shifts=verdict['shifts'],
                                             scale=0.9, fit_by_object={'1': {'rotation': _yaw(30.0)}})


def test_multi_slot_yawed_merge_slices_with_the_recorded_turn(tmp_path, monkeypatch):
    from print_handy.core import color_geometry
    from print_handy.core.orca_adapter import _rotate, _yaw
    source = tmp_path / 'yaw-merge.3mf'
    _yaw_merge_small_source(source)
    verdict = _plan(source)
    assert verdict['yaw_by_object'] == {'1': 45.0}
    manifest = source_colors.inventory(source)
    decisions = _two_slot_decisions(manifest)
    job = {'downloaded_file': str(source), 'audit': {'plates': 2}, 'color_decisions': decisions}
    job_path = tmp_path / 'JOB.json'
    job_path.write_text(json.dumps(job))
    monkeypatch.setattr(plate_merge, 'plan', lambda *a, **k: verdict)
    calls = []

    def fake_slice(source_arg, folder, state, brim=None, first_layer=False, color_decisions=None,
                   preserve_layout=False, filament_slot=None, item_shifts=None, fit_by_object=None):
        # Only Orca itself is faked: the real multicolor export runs with the
        # exact arguments _slice_merged passes, so the geometry report is
        # the exporter's own record of the turned placement.
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        _mapping, geometry = color_geometry.export_color_geometry(
            source_arg, folder / 'geometry.3mf', color_decisions,
            item_shifts=item_shifts, fit_by_object=fit_by_object)
        calls.append({'item_shifts': item_shifts, 'fit_by_object': fit_by_object, 'geometry': geometry})
        out = folder / 'plate_1.gcode'
        labels = ''.join(f'; printing object Part {p["object_id"]} id:{i} copy 0\n'
                         for i, p in enumerate(manifest['parts']))
        slot = state['filament_slots']['1']
        out.write_text(f'; filament_colour = {slot["hex"]}\n; nozzle_diameter = 0.4\n'
                       '; estimated printing time (normal mode) = 2h 0m 0s\n'
                       f'M190 S{state["temps_c"]["bed"]}\nM104 S{state["temps_c"]["nozzle"]} T0\n'
                       f'START_PRINT BED_TEMP={state["temps_c"]["bed"]} EXTRUDER_TEMP={state["temps_c"]["nozzle"]}\n'
                       f'{labels}G1 X10 Y10 Z0.2\nEND_PRINT\n')
        return out, {'geometry': geometry}, []
    monkeypatch.setattr('print_handy.core.orca_adapter.slice_model', fake_slice)
    out = model_job.slice_job(job_path)
    saved = json.loads(job_path.read_text())
    assert isinstance(out, (str, Path)) and len(calls) == 1
    assert calls[0]['fit_by_object'] == {'1': {'rotation': _yaw(45.0)}}
    assert saved['plate_merge']['mode'] == 'merge'
    assert saved['plate_merge']['yaw_by_object'] == {'1': 45.0}
    geometry = calls[0]['geometry']
    assert geometry['fit_by_object'] == {'1': {'rotation': _yaw(45.0)}}
    records = {row['part_key']: row for row in geometry['parts']}
    object_of_key = {p['key']: str(p['object_id']) for p in manifest['parts']}
    with zipfile.ZipFile(source) as archive:
        points = color_geometry._world_points(archive, {'1'})
    rotated = [_rotate(p, _yaw(45.0)) for p in points]
    row = next(r for k, r in records.items() if object_of_key[k] == '1')
    for a in range(3):
        assert row['bounds_mm'][a][0] == pytest.approx(
            min(p[a] for p in rotated) + verdict['shifts']['1'][a], abs=2e-3)


def test_polygon_merge_slices_to_one_gcode_at_the_recorded_corners(tmp_path, monkeypatch):
    from print_handy.core import color_geometry
    source = tmp_path / 'tri.3mf'
    _prism_source(source, {'1': (TRI_A, 10, (20, 30)), '2': (TRI_B, 10, (100, 90))}, [['1'], ['2']])
    verdict = _plan(source)
    assert verdict['mode'] == 'merge' and verdict['packing'] == 'polygon'
    job_path = _job(tmp_path, source)
    fake = _fake_slice()
    monkeypatch.setattr('print_handy.core.orca_adapter.slice_model', fake)
    out = model_job.slice_job(job_path)
    saved = json.loads(job_path.read_text())
    assert isinstance(out, (str, Path)) and len(fake.calls) == 1
    assert sorted(fake.calls[0]['printable']) == ['1', '2']
    assert saved['plate_merge']['mode'] == 'merge'
    # Re-read the derived plate: every object landed at the planned corner
    # and the placed hulls still keep the merge spacing.
    derived = Path(saved['merged']['plate'])
    placed = {}
    with zipfile.ZipFile(derived) as archive:
        for oid in ('1', '2'):
            hull, bounds = color_geometry._world_hull_xy(archive, {oid})[0]
            placed[oid] = hull
            corner = (verdict['placed_bounds_mm'][oid][0][0], verdict['placed_bounds_mm'][oid][1][0])
            assert bounds[0][0] == pytest.approx(corner[0]) and bounds[1][0] == pytest.approx(corner[1])
    assert plate_merge.hull_distance(placed['1'], placed['2']) >= plate_merge.SPACING_MM - 1e-6


# ---------------------------------------------------------------------------
# DATA-3 (P1-72): the merge planner's capacity is the job printer's position
# count, not the configured printer's IFS slot count. A multi_tool model keeps
# filament_slots=1 by design, so a Creator-configured process imported
# plate_merge with FILAMENT_SLOTS=1 and refused every multi-toolhead merge
# before the merged slicer ever ran.

def _two_single_part_plates(tmp_path):
    source = tmp_path / 'two.3mf'
    _source(source, {'1': (20, 20, 10), '2': (20, 20, 10)}, [['1'], ['2']])
    return source


def test_plan_toolhead_capacity_comes_from_the_caller(tmp_path, monkeypatch):
    monkeypatch.setattr(plate_merge, 'FILAMENT_SLOTS', 1)  # a Creator-configured process
    source = _two_single_part_plates(tmp_path)
    manifest = source_colors.inventory(source)
    plates = [{'id': 1, 'object_ids': [1]}, {'id': 2, 'object_ids': [2]}]
    slots = {part['key']: slot for part, slot in zip(manifest['parts'], [2, 4])}
    state = {'bed_mm': [256, 256], 'max_print_height_mm': 256,
             'filament_slots': {'2': {'material': 'PLA'}, '4': {'material': 'PLA'}}}
    refused = plate_merge.plan(source, manifest, plates, slots, state)
    assert refused['mode'] == 'split'
    assert refused['reason'] == 'needs 2 filament slots; one IFS job has 1'
    merged = plate_merge.plan(source, manifest, plates, slots, state, capacity=4, multi_tool=True)
    assert merged['mode'] == 'merge', merged['reason']


def test_plan_multi_tool_allows_mixed_materials_across_toolheads(tmp_path, monkeypatch):
    monkeypatch.setattr(plate_merge, 'FILAMENT_SLOTS', 1)
    source = _two_single_part_plates(tmp_path)
    manifest = source_colors.inventory(source)
    plates = [{'id': 1, 'object_ids': [1]}, {'id': 2, 'object_ids': [2]}]
    slots = {part['key']: slot for part, slot in zip(manifest['parts'], [2, 4])}
    state = {'bed_mm': [256, 256], 'max_print_height_mm': 256,
             'filament_slots': {'2': {'material': 'PLA'}, '4': {'material': 'PETG'}}}
    # One extruder fed by slots cannot change material mid-job; two toolheads
    # carry one filament each and can.
    refused = plate_merge.plan(source, manifest, plates, slots, state, capacity=4)
    assert refused['mode'] == 'split' and 'mixed materials across slots' in refused['reason']
    merged = plate_merge.plan(source, manifest, plates, slots, state, capacity=4, multi_tool=True)
    assert merged['mode'] == 'merge', merged['reason']
