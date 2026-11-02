"""Unit tests for offline full-size split planning."""
import sys
import zipfile
from pathlib import Path

from print_handy.core import split_fit  # noqa: E402
from print_handy.core import plate_merge  # noqa: E402
from .test_plate_merge import _source  # noqa: E402


def _mesh_source(path, meshes, plates):
    """A 3MF from explicit vertex lists: meshes {object_id: [(x, y, z), ...]}.

    Same package shape as test_plate_merge._source, but the mesh vertices are
    the caller's, so non-rectangular footprints (whose bounding rectangle is
    larger than the mesh ever occupies) can be tested honestly.
    """
    resources, items, objects = [], [], []
    for oid, points in meshes.items():
        body = ''.join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in points)
        model = (f'<model unit="millimeter"><resources><object id="1"><mesh><vertices>{body}</vertices>'
                 f'<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources></model>')
        with zipfile.ZipFile(path, 'a') as archive:
            archive.writestr(f'3D/Objects/object_{oid}.model', model)
        resources.append(f'<object id="{oid}" name="Part {oid}"><components><component p:path="/3D/Objects/object_{oid}.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>')
        items.append(f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {20 + 5 * int(oid)} 30 0"/>')
        objects.append(f'<object id="{oid}"><metadata key="name" value="Part {oid}"/><metadata key="extruder" value="1"/><part id="1"><metadata key="name" value="Part {oid}"/><metadata key="extruder" value="1"/></part></object>')
    plate_xml = ''.join(
        f'<plate><metadata key="plater_id" value="{i}"/>' +
        ''.join(f'<model_instance><metadata key="object_id" value="{o}"/></model_instance>' for o in ids) + '</plate>'
        for i, ids in enumerate(plates, 1))
    with zipfile.ZipFile(path, 'a') as archive:
        archive.writestr('3D/3dmodel.model', f'<model xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" unit="millimeter"><resources>{"".join(resources)}</resources><build>{"".join(items)}</build></model>')
        archive.writestr('Metadata/model_settings.config', f'<config>{"".join(objects)}{plate_xml}</config>')
        archive.writestr('Metadata/project_settings.config', '{\n  "filament_colour": [\n    "#FFFFFF"\n  ],\n  "filament_type": [\n    "PLA"\n  ]\n}')
        archive.writestr('3D/_rels/3dmodel.model.rels', '<Relationships>' + ''.join(f'<Relationship Target="/3D/Objects/object_{o}.model" Id="r{o}"/>' for o in meshes) + '</Relationships>')
        for i in range(1, len(plates) + 1):
            archive.writestr(f'Metadata/plate_{i}.png', b'png')


def _rect_points(w, d, h):
    corners = [(0, 0), (w, 0), (w, d), (0, d)]
    return [(x, y, 0) for x, y in corners] + [(x, y, h) for x, y in corners]


def _plus_points(half_length=115, half_arm=20, h=10):
    outline = [(half_arm, half_length), (-half_arm, half_length), (-half_arm, half_arm),
               (-half_length, half_arm), (-half_length, -half_arm), (-half_arm, -half_arm),
               (-half_arm, -half_length), (half_arm, -half_length), (half_arm, -half_arm),
               (half_length, -half_arm), (half_length, half_arm), (half_arm, half_arm)]
    return [(x, y, 0) for x, y in outline] + [(x, y, h) for x, y in outline]


def test_two_large_objects_pack_into_two_full_size_plates(tmp_path):
    source = tmp_path / 'two-boxes.3mf'
    _source(source, {'1': (150, 150, 10), '2': (150, 150, 10)}, [['1', '2']])
    plan = split_fit.split_plan_100(source, 220)
    assert plan and len(plan['plates']) == 2
    assert sorted(key for plate in plan['plates'] for key in plate) == ['1:1', '2:1']
    assert all(plate_merge.pack({key: (150, 150)}, 220) is not None for group in plan['plates'] for key in group)


def test_individually_oversized_object_has_no_split_plan(tmp_path):
    source = tmp_path / 'wide-box.3mf'
    _source(source, {'1': (300, 10, 10)}, [['1']])
    assert split_fit.split_plan_100(source, 220) is None


def test_yaw_envelope_is_dimension_preserving():
    w, d = split_fit.yaw_envelope(230, 130, 90)
    assert (round(w, 6), round(d, 6)) == (130, 230)
    w, d = split_fit.yaw_envelope(100, 40, 45)
    assert round(w, 1) == round(d, 1) == 99.0


def test_per_part_fit_names_the_stand_from_issue_76_as_fitting_only_diagonally(tmp_path):
    # The retained case from #76: a 130 x 230 mm stand on a 220 mm bed fits
    # at no in-plane yaw, so separate plates are not a remedy for it in the
    # bed plane — the in-plane facts below are unchanged. What changed in
    # #221's diagonal stage is the last preference: stood on its end at a
    # 45-degree bed turn it fits reoriented, so it is no longer named
    # blocking (the full proof is pinned in
    # test_reorientation_with_a_diagonal_turn_cures_the_issue_76_stand).
    source = tmp_path / 'stand.3mf'
    _solid_source(source, {'1': _box_tris(0, 130, 0, 230, 0, 30),
                           '2': _box_tris(0, 195, 0, 200, 0, 119.8)}, [['1'], ['2']])
    rows = {row['object_id']: row for row in split_fit.per_part_fit(source, 220)}
    stand, base = rows['1'], rows['2']
    assert stand['fits_authored'] is False and stand['fits_full_size'] is True
    assert stand['blocking_reason'] is None
    assert stand['reorientation']['yaw_deg'] == 45.0
    assert stand['usable_mm'] == [204.0, 204.0]  # 8 mm edge clearance, applied once per axis
    assert min(stand['best_extent_mm'][:2]) >= 130 and max(stand['best_extent_mm'][:2]) > 204
    assert base['fits_full_size'] is True
    assert split_fit.split_plan_100(source, 220) is not None


def test_per_part_fit_finds_a_yaw_the_authored_extent_misses(tmp_path):
    source = tmp_path / 'long-thin.3mf'
    _source(source, {'1': (230, 50, 20)}, [['1']])
    (row,) = split_fit.per_part_fit(source, 220)
    assert row['fits_authored'] is False
    assert row['fits_full_size'] is True
    assert 0 < row['best_yaw_deg'] < 90
    assert max(row['best_extent_mm'][:2]) <= 204


def test_per_part_fit_uses_the_real_height_limit_not_the_bed_size(tmp_path):
    # A real 50 x 50 x 250 box: stood on a face its 50 x 250 footprint
    # fits the 204 mm usable bed at no bed turn, cardinal or diagonal
    # (its tightest envelope is 212 mm at 45 degrees), so the height
    # limit alone decides every assertion below.
    source = tmp_path / 'tall.3mf'
    _solid_source(source, {'1': _box_tris(0, 50, 0, 50, 0, 250)}, [['1']])
    (row,) = split_fit.per_part_fit(source, bed=220, height=220)
    assert row['fits_full_size'] is False
    assert 'height 250' in row['blocking_reason']
    assert split_fit.split_plan_100(source, bed=220, height=220) is None
    # A shorter printer limit is honored even on a larger bed: at a 200 mm
    # limit the part fits only lying on its side (#221 R1), and it is the
    # reoriented height the limit checks...
    (row,) = split_fit.per_part_fit(source, bed=300, height=200)
    assert row['fits_full_size'] is True
    assert row['reorientation'] is not None
    assert row['reorientation']['dims_mm'][2] <= 200
    # ...while a limit below every orientation's height still blocks.
    (row,) = split_fit.per_part_fit(source, bed=300, height=40)
    assert row['fits_full_size'] is False


def test_fit_options_reports_blocking_parts_before_any_scale(tmp_path, monkeypatch):
    from print_handy.core import orca_adapter as oa
    # A 250 mm part blocks at every orientation: no in-plane yaw fits, and
    # reoriented it would stand 250 tall, over the 220 mm limit. (The #76
    # Stand was this test's fixture until #221's diagonal stage: it now
    # fits reoriented at a 45-degree turn, so it can no longer stand in
    # for a blocking part — see the diagonal Stand tests.)
    source = tmp_path / 'stand.3mf'
    _source(source, {'1': (250, 250, 10)}, [['1']])
    monkeypatch.setattr(oa, 'verified_scale_choices', lambda *a, **k: [])
    monkeypatch.setattr(oa, 'rotation_alternatives', lambda *a, **k: [])
    options = oa.fit_options(source, {'bed_mm': [220, 220], 'max_print_height_mm': 220,
                                      'resolved_hop_types': ['Normal Lift']})
    (part,) = options['parts']
    assert part['fits_full_size'] is False
    assert part['blocking_reason']
    assert options['blocking_parts'] == [part['name']]
    assert 'yaw sweep 0-90 degrees' in part['orientations_checked']


def test_split_plan_gives_a_yaw_only_part_its_own_turned_plate(tmp_path):
    # A part that only fits when turned in the bed plane is offered a split
    # plan now: it sits alone on its own plate and the plan records the exact
    # yaw slicing must apply, instead of no plan at all (#76 follow-up to #191).
    source = tmp_path / 'yaw-only.3mf'
    _source(source, {'1': (230, 50, 20), '2': (150, 150, 10)}, [['1', '2']])
    plan = split_fit.split_plan_100(source, 220)
    assert plan is not None
    assert sorted(plan['plates']) == [['1:1'], ['2:1']]
    (row,) = [r for r in split_fit.per_part_fit(source, 220) if r['object_id'] == '1']
    assert plan['yaw_by_object'] == {'1': row['best_yaw_deg']}
    assert 0 < plan['yaw_by_object']['1'] < 90
    # The turned part never shares its plate, and no other part is turned.
    turned_plate = next(plate for plate in plan['plates'] if '1:1' in plate)
    assert turned_plate == ['1:1']


def test_split_plan_without_yaw_records_no_yaw(tmp_path):
    source = tmp_path / 'two-boxes.3mf'
    _source(source, {'1': (150, 150, 10), '2': (150, 150, 10)}, [['1', '2']])
    plan = split_fit.split_plan_100(source, 220)
    assert plan is not None and 'yaw_by_object' not in plan


def test_split_plan_yaw_does_not_cure_the_issue_76_stand(tmp_path):
    # The retained Stand fits at no in-plane yaw, so yaw alone still does
    # not cure it: the plan that exists since #221's diagonal stage
    # carries no yaw record for it — it fits only reoriented, stood on its
    # end at a 45-degree bed turn (see the diagonal Stand test).
    source = tmp_path / 'stand.3mf'
    _solid_source(source, {'1': _box_tris(0, 130, 0, 230, 0, 30),
                           '2': _box_tris(0, 150, 0, 150, 0, 10)}, [['1'], ['2']])
    plan = split_fit.split_plan_100(source, 220)
    assert plan is not None
    assert '1' not in plan.get('yaw_by_object', {})
    assert plan['reorientation_by_object']['1']['yaw_deg'] == 45.0


def test_fit_choice_rows_carry_the_split_yaw():
    from print_handy.core.prepared_intake import fit_choice_rows
    rows = fit_choice_rows({'scales': [], 'rotations': [],
                            'split_plan': {'plates': [['1:1'], ['2:1']],
                                           'yaw_by_object': {'1': 43.0}}})
    (split,) = [row for row in rows if row['kind'] == 'split']
    assert split['yaw_by_object'] == {'1': 43.0}
    assert split['plates'] == [['1:1'], ['2:1']]


def _split_job(tmp_path, source, plan):
    import json

    from .test_plate_merge import _job
    job_path = _job(tmp_path, source)
    data = json.loads(job_path.read_text())
    data['full_size_split_plan'] = plan
    job_path.write_text(json.dumps(data))
    return job_path


def test_slicing_a_yaw_split_applies_the_recorded_turn_to_its_plate(tmp_path, monkeypatch):
    import json

    from print_handy.core import model_job, orca_adapter
    from .test_plate_merge import _fake_slice
    source = tmp_path / 'yaw.3mf'
    _source(source, {'1': (230, 50, 20), '2': (150, 150, 10)}, [['1', '2']])
    plan = split_fit.split_plan_100(source, 220)
    assert plan and plan['yaw_by_object']
    job_path = _split_job(tmp_path, source, plan)
    fake = _fake_slice()
    monkeypatch.setattr(orca_adapter, 'slice_model', fake)
    paths = model_job.slice_job(job_path)
    assert len(paths) == 2 and len(fake.calls) == 2
    saved = json.loads(job_path.read_text())
    by_plate = {row['plate_id']: row for row in saved['derived_plates']}
    turned = next(row for row in saved['derived_plates'] if row['part_keys'] == ['1:1'])
    assert turned['yaw_deg'] == plan['yaw_by_object']['1']
    # The derived plate was exported with the turn, not as authored: its
    # reported fit carries the rotation and its placed bounds fit the bed.
    fit = turned['plate_report']['fit']
    assert fit['scale'] == 1.0 and fit['rotation'] is not None
    placed = turned['plate_report']['placed_bounds_mm']
    assert all(0 <= low and high <= 220 for low, high in placed)
    other = next(row for row in saved['derived_plates'] if row['part_keys'] == ['2:1'])
    assert 'yaw_deg' not in other and 'fit' not in other['plate_report']
    assert by_plate[turned['plate_id']] is turned


def test_slicing_refuses_a_turn_recorded_for_a_shared_plate(tmp_path, monkeypatch):
    import pytest

    from print_handy.core import model_job, orca_adapter
    from .test_plate_merge import _fake_slice
    source = tmp_path / 'shared.3mf'
    _source(source, {'1': (150, 150, 10), '2': (150, 150, 10)}, [['1', '2']])
    plan = {'plates': [['1:1', '2:1']], 'yaw_by_object': {'1': 30.0}}
    job_path = _split_job(tmp_path, source, plan)
    monkeypatch.setattr(orca_adapter, 'slice_model', _fake_slice())
    with pytest.raises(RuntimeError, match='not that part alone'):
        model_job.slice_job(job_path)


def test_per_part_fit_judges_a_plus_part_by_its_mesh_not_its_rectangle(tmp_path):
    # A plus/cross part spans 230 x 230 mm, so its bounding rectangle fits the
    # 204 mm usable bed at no yaw; the actual mesh, turned 45 degrees, occupies
    # about 191 x 191 mm and does fit (#221). Rectangle evaluation blocked it.
    source = tmp_path / 'plus.3mf'
    _mesh_source(source, {'1': _plus_points(), '2': _rect_points(150, 150, 10)}, [['1', '2']])
    rows = {row['object_id']: row for row in split_fit.per_part_fit(source, 220)}
    plus = rows['1']
    assert plus['extent_mm'] == [230.0, 230.0, 10]
    assert plus['fits_authored'] is False
    rect_yaw, rect_env = split_fit.best_yaw(230, 230, 204, 204)
    assert max(rect_env) > 204  # the old rectangle verdict: blocking
    assert plus['fits_full_size'] is True
    assert plus['best_yaw_deg'] == 45.0
    assert max(plus['best_extent_mm'][:2]) <= 204
    # And the split plan can use that verdict, with the turn recorded (#220).
    plan = split_fit.split_plan_100(source, 220)
    assert plan is not None and plan['yaw_by_object'] == {'1': 45.0}
    assert next(plate for plate in plan['plates'] if '1:1' in plate) == ['1:1']


def test_per_part_fit_rectangle_mesh_matches_the_rectangle_sweep(tmp_path):
    # For a truly rectangular mesh the vertex-extrema sweep must agree with
    # the rectangle sweep exactly: rectangular verdicts do not change (#221).
    source = tmp_path / 'rect.3mf'
    _mesh_source(source, {'1': _rect_points(230, 50, 20)}, [['1']])
    (row,) = split_fit.per_part_fit(source, 220)
    yaw, envelope = split_fit.best_yaw(230, 50, 204, 204)
    assert row['best_yaw_deg'] == yaw
    assert row['best_extent_mm'][:2] == [round(envelope[0], 3), round(envelope[1], 3)]
    assert row['fits_full_size'] is True


def test_per_part_fit_exclusion_blocks_when_no_placement_avoids_it(tmp_path):
    # A 200 x 200 mm part fits the 204 mm usable area, but on a bed whose
    # front-left 18 x 28 mm is excluded every legal placement overlaps the
    # exclusion, so it must not count as fitting (#221). The part is a cube,
    # so a face change cannot help either: its reoriented footprint is the
    # same 200 x 200 mm and is just as unplaceable (#221 R3).
    source = tmp_path / 'nearly-full.3mf'
    _mesh_source(source, {'1': _rect_points(200, 200, 200)}, [['1']])
    (open_row,) = split_fit.per_part_fit(source, 220, exclusions=[])
    assert open_row['fits_full_size'] is True and open_row['placement_mm'] is None
    (row,) = split_fit.per_part_fit(source, 220, exclusions=[(0, 0, 18, 28)])
    assert row['fits_full_size'] is False
    assert row['placement_mm'] is None
    assert 'exclusion' in row['blocking_reason']
    assert row['exclusions_mm'] == [[0.0, 0.0, 18.0, 28.0]]


def test_per_part_fit_exclusion_reports_an_avoiding_placement(tmp_path):
    source = tmp_path / 'small.3mf'
    _mesh_source(source, {'1': _rect_points(100, 50, 10)}, [['1']])
    (row,) = split_fit.per_part_fit(source, 220, exclusions=[(0, 0, 18, 28)])
    assert row['fits_full_size'] is True
    # Placed flush against the exclusion's right edge, on the bed edge.
    assert row['placement_mm'] == [18.0, 8.0]


def test_split_plan_on_an_exclusion_bed_records_avoiding_placements(tmp_path):
    # Stage 2 (#221): the exporter can now realize an avoiding placement, so
    # a plan IS offered on an exclusion bed. Each placed part sits alone (the
    # exporter places a whole plate rigidly) and the plan records the exact
    # placement per_part_fit proved.
    source = tmp_path / 'two-boxes.3mf'
    _mesh_source(source, {'1': _rect_points(150, 150, 10), '2': _rect_points(150, 150, 10)},
                 [['1', '2']])
    rows = {row['object_id']: row for row in split_fit.per_part_fit(source, 220, exclusions=[(0, 0, 18, 28)])}
    assert all(row['fits_full_size'] for row in rows.values())
    plan = split_fit.split_plan_100(source, 220, exclusions=[(0, 0, 18, 28)])
    assert plan is not None
    assert sorted(plan['plates']) == [['1:1'], ['2:1']]
    assert plan['placement_by_object'] == {oid: row['placement_mm'] for oid, row in rows.items()}
    # The recorded placements really avoid the 18 x 28 exclusion.
    for oid, (px, py) in plan['placement_by_object'].items():
        w, d = rows[oid]['best_extent_mm'][:2]
        assert px + w <= 0 or px >= 18 or py + d <= 0 or py >= 28
    # Without exclusions the same source keeps its old plan shape: no placements.
    open_plan = split_fit.split_plan_100(source, 220, exclusions=[])
    assert open_plan is not None and 'placement_by_object' not in open_plan


def test_split_plan_on_an_exclusion_bed_still_blocks_without_a_placement(tmp_path):
    # A part whose footprint fits the usable bed only inside the exclusion
    # has no avoiding placement: it stays blocking (naming the exclusion) and
    # no split plan is offered (#221). The blocking part is a cube, so a face
    # change cannot place it either (#221 R3).
    source = tmp_path / 'blocked.3mf'
    _mesh_source(source, {'1': _rect_points(200, 200, 200), '2': _rect_points(150, 150, 10)},
                 [['1', '2']])
    rows = {row['object_id']: row for row in split_fit.per_part_fit(source, 220, exclusions=[(0, 0, 18, 28)])}
    assert rows['1']['fits_full_size'] is False
    assert 'exclusion' in rows['1']['blocking_reason']
    assert rows['2']['fits_full_size'] is True
    assert split_fit.split_plan_100(source, 220, exclusions=[(0, 0, 18, 28)]) is None


# The Qidi X-Plus 4 exclusion chain, as recorded in qidi/facts.py: one polygon
# list tracing two disjoint zones (a top-left strip and a bottom-right strip),
# joined by zero-area runs along the bed edges.
_XPLUS4_EXCLUSION = ((0, 305), (0, 302), (35, 302), (35, 305), (305, 305), (305, 305),
                     (305, 305), (305, 20), (293, 20), (293, 0), (305, 0), (305, 20),
                     (305, 305))


def test_exclusion_rectangles_split_disjoint_zones_instead_of_the_bounding_box():
    # P2-193: collapsing the X-Plus 4 chain to its bounding rectangle excludes
    # the whole 305 mm bed; the decomposition must keep only the two zones.
    zones = split_fit._exclusion_rectangles(_XPLUS4_EXCLUSION)
    assert sorted(zones) == [(0.0, 302.0, 35.0, 305.0), (293.0, 0.0, 305.0, 20.0)]
    # A single rectangle polygon stays the one rectangle it declares.
    assert split_fit._exclusion_rectangles(((0, 0), (18, 0), (18, 28), (0, 28))) == [
        (0.0, 0.0, 18.0, 28.0)]
    # The Qidi Q1 Pro chain traces two back-edge strips the same way.
    q1pro = ((25, 245), (25, 240), (115, 240), (115, 245), (208, 245), (208, 240),
             (245, 240), (245, 245), (208, 245))
    assert sorted(split_fit._exclusion_rectangles(q1pro)) == [
        (25.0, 240.0, 115.0, 245.0), (208.0, 240.0, 245.0, 245.0)]
    # A polygon with a diagonal edge keeps the conservative bounding rectangle.
    assert split_fit._exclusion_rectangles(((0, 0), (10, 0), (0, 10))) == [(0.0, 0.0, 10.0, 10.0)]
    assert split_fit._exclusion_rectangles(()) == []


def test_declared_exclusions_for_the_xplus4_are_its_two_zones(monkeypatch):
    from print_handy import settings as settings_module
    monkeypatch.setattr(settings_module, 'current',
                        lambda: {'printer': {'model': 'qidi.xplus4'}})
    zones = split_fit._declared_exclusions()
    assert sorted(zones) == [(0.0, 302.0, 35.0, 305.0), (293.0, 0.0, 305.0, 20.0)]


def test_per_part_fit_fits_a_small_part_on_the_xplus4_bed(tmp_path):
    # P2-193's example: even a 10 x 10 mm part was rejected on the X-Plus 4
    # because the collapsed exclusion covered the whole bed.
    source = tmp_path / 'small.3mf'
    _mesh_source(source, {'1': _rect_points(10, 10, 10)}, [['1']])
    zones = split_fit._exclusion_rectangles(_XPLUS4_EXCLUSION)
    (row,) = split_fit.per_part_fit(source, 305, exclusions=zones)
    assert row['fits_full_size'] is True
    px, py = row['placement_mm']
    for x0, y0, x1, y1 in zones:
        assert px + 10 <= x0 or px >= x1 or py + 10 <= y0 or py >= y1
    # Against the old whole-bed bounding rectangle the same part cannot fit.
    (blocked,) = split_fit.per_part_fit(source, 305, exclusions=[(0, 0, 305, 305)])
    assert blocked['fits_full_size'] is False


def test_split_plan_records_the_yaw_that_proved_an_exclusion_avoiding_placement(tmp_path):
    # P2-78: a 200 x 190 mm part fits the 204 mm usable bed as authored, but
    # every authored placement overlaps the (0, 0) to (18, 28) exclusion; only
    # the 90-degree turn can be placed, at [18, 8]. The plan must record that
    # turn alongside the placement — recording the placement alone would
    # slice the unturned 200 mm width at x=18, past the proved usable area.
    source = tmp_path / 'turn-to-avoid.3mf'
    _mesh_source(source, {'1': _rect_points(200, 190, 10), '2': _rect_points(50, 50, 10)},
                 [['1', '2']])
    rows = {row['object_id']: row for row in
            split_fit.per_part_fit(source, 220, exclusions=[(0, 0, 18, 28)])}
    assert rows['1']['fits_authored'] is True
    assert rows['1']['best_yaw_deg'] == 90.0
    assert rows['1']['placement_mm'] == [18.0, 8.0]
    plan = split_fit.split_plan_100(source, 220, exclusions=[(0, 0, 18, 28)])
    assert plan is not None
    assert plan['yaw_by_object'] == {'1': 90.0}
    assert plan['placement_by_object']['1'] == [18.0, 8.0]


def test_slicing_a_turned_exclusion_split_applies_the_turn_and_the_placement(tmp_path, monkeypatch):
    # P2-78 end to end: the derived plate for the turned part is exported
    # with the recorded 90-degree turn composed with the recorded placement,
    # so its placed bounds are the proved 190 x 200 footprint at [18, 8],
    # clear of the exclusion — not the authored 200 mm width at x=18.
    import json

    from print_handy.core import model_job, orca_adapter
    from .test_plate_merge import _fake_slice
    source = tmp_path / 'turn-to-avoid.3mf'
    _mesh_source(source, {'1': _rect_points(200, 190, 10), '2': _rect_points(50, 50, 10)},
                 [['1', '2']])
    plan = split_fit.split_plan_100(source, 220, exclusions=[(0, 0, 18, 28)])
    assert plan and plan['yaw_by_object'] == {'1': 90.0}
    job_path = _split_job(tmp_path, source, plan)
    fake = _fake_slice()
    monkeypatch.setattr(orca_adapter, 'slice_model', fake)
    paths = model_job.slice_job(job_path)
    assert len(paths) == 2 and len(fake.calls) == 2
    saved = json.loads(job_path.read_text())
    turned = next(row for row in saved['derived_plates'] if row['part_keys'] == ['1:1'])
    assert turned['yaw_deg'] == 90.0
    assert turned['placement_mm'] == [18.0, 8.0]
    placed = turned['plate_report']['placed_bounds_mm']
    assert placed[0] == [18.0, 208.0] and placed[1] == [8.0, 208.0]


def test_fit_choice_rows_carry_the_split_placement():
    from print_handy.core.prepared_intake import fit_choice_rows
    rows = fit_choice_rows({'scales': [], 'rotations': [],
                            'split_plan': {'plates': [['1:1'], ['2:1']],
                                           'placement_by_object': {'1': [18.0, 8.0]}}})
    (split,) = [row for row in rows if row['kind'] == 'split']
    assert split['placement_by_object'] == {'1': [18.0, 8.0]}


def test_slicing_an_exclusion_split_lands_the_plate_at_the_recorded_placement(tmp_path, monkeypatch):
    import json

    from print_handy.core import model_job, orca_adapter
    from .test_plate_merge import _fake_slice
    source = tmp_path / 'placed.3mf'
    _source(source, {'1': (150, 150, 10), '2': (150, 150, 10)}, [['1', '2']])
    plan = split_fit.split_plan_100(source, 220, exclusions=[(0, 0, 18, 28)])
    assert plan and plan['placement_by_object'] == {'1': [18.0, 8.0], '2': [18.0, 8.0]}
    job_path = _split_job(tmp_path, source, plan)
    fake = _fake_slice()
    monkeypatch.setattr(orca_adapter, 'slice_model', fake)
    paths = model_job.slice_job(job_path)
    assert len(paths) == 2 and len(fake.calls) == 2
    saved = json.loads(job_path.read_text())
    for row in saved['derived_plates']:
        assert row['placement_mm'] == [18.0, 8.0]
        placed = row['plate_report']['placed_bounds_mm']
        # The derived plate was exported at the recorded placement, not
        # centered or as authored, and the placed bounds avoid the exclusion.
        assert placed[0][0] == 18.0 and placed[1][0] == 8.0
        assert placed[0][1] == 168.0 and placed[1][1] == 158.0
        assert row['plate_report']['placement_mm'] == [18.0, 8.0]


def test_slicing_refuses_a_placement_recorded_for_a_shared_plate(tmp_path, monkeypatch):
    import pytest

    from print_handy.core import model_job, orca_adapter
    from .test_plate_merge import _fake_slice
    source = tmp_path / 'shared.3mf'
    _source(source, {'1': (150, 150, 10), '2': (150, 150, 10)}, [['1', '2']])
    plan = {'plates': [['1:1', '2:1']], 'placement_by_object': {'1': [18.0, 8.0]}}
    job_path = _split_job(tmp_path, source, plan)
    monkeypatch.setattr(orca_adapter, 'slice_model', _fake_slice())
    with pytest.raises(RuntimeError, match='not that part alone'):
        model_job.slice_job(job_path)


def test_slicing_refuses_an_out_of_range_or_unknown_placement(tmp_path, monkeypatch):
    import pytest

    from print_handy.core import model_job, orca_adapter
    from .test_plate_merge import _fake_slice
    source = tmp_path / 'bad-placement.3mf'
    _source(source, {'1': (150, 150, 10), '2': (150, 150, 10)}, [['1', '2']])
    for bad in ({'1': [220.0, 8.0]}, {'1': [-1.0, 8.0]}, {'9': [18.0, 8.0]}):
        plan = {'plates': [['1:1'], ['2:1']], 'placement_by_object': bad}
        job_path = _split_job(tmp_path, source, plan)
        monkeypatch.setattr(orca_adapter, 'slice_model', _fake_slice())
        with pytest.raises(RuntimeError, match='placement'):
            model_job.slice_job(job_path)


# --- #221 stage 3: brim/feature extents in the fit offer -------------------
# A supplied brim policy is resolved through orca_adapter.required_feature_edge
# against the printer state; Normal Lift keeps the travel sweep at zero so the
# budget below is exactly the brim width.
_BRIM_STATE = {'resolved_hop_types': ['Normal Lift']}
_BRIM_10 = {'type': 'outer_only', 'width_mm': 10.0, 'label': 'Outer-only brim, 10 mm'}


def test_feature_edge_budget_is_zero_without_a_policy_and_includes_travel_sweep():
    assert split_fit._feature_edge_mm() == 0.0
    assert split_fit._feature_edge_mm(state=_BRIM_STATE) == 0.0
    assert split_fit._feature_edge_mm(brim_policy=_BRIM_10, state=_BRIM_STATE) == 10.0
    spiral = {'resolved_hop_types': ['Spiral Lift'], 'z_hop': 0.4, 'travel_slope': 3.0}
    budget = split_fit._feature_edge_mm(brim_policy={'type': 'outer_only', 'width_mm': 5.0},
                                        state=spiral)
    assert 6.21 < budget < 6.22  # 5 mm brim + ~1.215 mm resolved travel sweep


def test_per_part_fit_brim_blocks_a_part_that_fits_only_without_it(tmp_path):
    # A 200 mm cube: every orientation has a 200 x 200 footprint, so no
    # yaw — cardinal or diagonal, in-plane or reoriented — can rescue it
    # from the brimmed usable area. (A flat 200 x 200 x 10 plate was this
    # fixture until #221's diagonal stage: it now fits with the brim by
    # standing on edge at a 45-degree turn, so it no longer pins the
    # brim as the blocker.)
    source = tmp_path / 'brim-tight.3mf'
    _mesh_source(source, {'1': _rect_points(200, 200, 200)}, [['1']])
    (plain,) = split_fit.per_part_fit(source, 220)
    assert plain['fits_full_size'] is True and plain['feature_edge_mm'] == 0.0
    assert plain['usable_mm'] == [204.0, 204.0]
    (row,) = split_fit.per_part_fit(source, 220, brim_policy=_BRIM_10, state=_BRIM_STATE)
    assert row['fits_full_size'] is False
    assert row['feature_edge_mm'] == 10.0
    # Budget applied exactly once: usable = 220 - 2 * (8 edge + 10 brim).
    assert row['usable_mm'] == [184.0, 184.0]
    assert 'brim' in row['blocking_reason'] and 'without a brim' in row['blocking_reason']
    # And its split plan goes with it, while the no-brim plan shape is moot here
    # (a single part has no split plan either way).
    assert split_fit.split_plan_100(source, 220, brim_policy=_BRIM_10, state=_BRIM_STATE) is None


def test_split_plan_brim_blocks_where_no_brim_plan_exists(tmp_path):
    # The no-brim split plan exists (two plates); with the brim there is
    # still no split plan — but since #221's diagonal stage the reason is
    # not a block: the 200 mm part fits only reoriented at a diagonal
    # turn, and in that orientation the pair packs onto ONE proved shared
    # plate, so the planner returns None by its not-a-split rule (a
    # one-plate plan is no split), not because any part fails to fit.
    # Both rows fitting is what distinguishes the two outcomes.
    source = tmp_path / 'brim-split.3mf'
    _source(source, {'1': (200, 200, 10), '2': (150, 150, 10)}, [['1', '2']])
    plan = split_fit.split_plan_100(source, 220)
    assert plan is not None and len(plan['plates']) == 2
    rows = split_fit.per_part_fit(source, 220, brim_policy=_BRIM_10, state=_BRIM_STATE)
    assert all(row['fits_full_size'] for row in rows)
    assert split_fit.split_plan_100(source, 220, brim_policy=_BRIM_10, state=_BRIM_STATE) is None


def test_per_part_fit_brim_still_fits_with_the_budget_accounted(tmp_path):
    source = tmp_path / 'brim-roomy.3mf'
    _mesh_source(source, {'1': _rect_points(150, 150, 10)}, [['1']])
    (row,) = split_fit.per_part_fit(source, 220, brim_policy=_BRIM_10, state=_BRIM_STATE)
    assert row['fits_full_size'] is True and row['fits_authored'] is True
    assert row['feature_edge_mm'] == 10.0 and row['usable_mm'] == [184.0, 184.0]
    assert row['blocking_reason'] is None


def test_per_part_fit_brim_off_is_unchanged(tmp_path):
    source = tmp_path / 'brim-off.3mf'
    _mesh_source(source, {'1': _rect_points(200, 200, 10)}, [['1']])
    (plain,) = split_fit.per_part_fit(source, 220)
    for kwargs in ({'brim_policy': {'type': 'no_brim', 'width_mm': 0.0}, 'state': _BRIM_STATE},
                   {'state': _BRIM_STATE}):
        (row,) = split_fit.per_part_fit(source, 220, **kwargs)
        assert row['feature_edge_mm'] == 0.0
        for key in ('fits_full_size', 'fits_authored', 'usable_mm', 'best_yaw_deg',
                    'best_extent_mm', 'placement_mm', 'blocking_reason'):
            assert row[key] == plain[key]


def test_per_part_fit_brim_and_exclusion_compose(tmp_path):
    exclusions = [(0, 0, 18, 28)]
    # Placement: the brimmed footprint avoids the exclusion, so the recorded
    # placement is the grown footprint's spot shifted back by the budget.
    source = tmp_path / 'brim-placed.3mf'
    _mesh_source(source, {'1': _rect_points(100, 50, 10)}, [['1']])
    (row,) = split_fit.per_part_fit(source, 220, exclusions=exclusions,
                                    brim_policy={'type': 'outer_only', 'width_mm': 5.0},
                                    state=_BRIM_STATE)
    assert row['fits_full_size'] is True and row['feature_edge_mm'] == 5.0
    assert row['placement_mm'] == [23.0, 13.0]
    px, py = row['placement_mm']
    assert px - 5.0 >= 18  # the brim itself clears the exclusion's right edge
    # Blocking: a 190 mm cube has an avoiding placement without a brim, but
    # the brim budget alone makes it not fit, and the reason names the brim.
    # (A flat 190 x 190 x 10 plate was this fixture until #221's diagonal
    # stage: stood on edge at a diagonal turn its footprint fits the
    # brimmed usable area and places clear of the exclusion, so it no
    # longer pins the brim as the blocker; the cube's footprint is
    # 190 x 190 in every orientation.)
    tight = tmp_path / 'brim-exclusion-tight.3mf'
    _mesh_source(tight, {'1': _rect_points(190, 190, 190)}, [['1']])
    (plain,) = split_fit.per_part_fit(tight, 220, exclusions=exclusions)
    assert plain['fits_full_size'] is True and plain['placement_mm'] is not None
    (row,) = split_fit.per_part_fit(tight, 220, exclusions=exclusions,
                                    brim_policy=_BRIM_10, state=_BRIM_STATE)
    assert row['fits_full_size'] is False
    assert 'brim' in row['blocking_reason']
    assert split_fit.split_plan_100(tight, 220, exclusions=exclusions,
                                    brim_policy=_BRIM_10, state=_BRIM_STATE) is None


# --- #221 P2: polygon (convex hull) bins on the split path -----------------
# Complementary right triangles: each is 180 x 110 mm, so rectangle packing
# cannot bin the pair (two 180 mm widths / 110 mm depths never shelf-pack
# the 204 mm usable bed), but their hulls interlock along the diagonal with
# the merge spacing to spare — the same shapes as the P1 merge tests.
_TRI_A = [(0, 0), (180, 0), (0, 110)]
_TRI_B = [(0, 110), (180, 0), (180, 110)]


def _tri_prism(poly, h=10):
    return [(x, y, 0) for x, y in poly] + [(poly[0][0], poly[0][1], h)]


def _tri_source(path):
    _mesh_source(path, {'1': _tri_prism(_TRI_A), '2': _tri_prism(_TRI_B),
                        '3': _rect_points(150, 150, 10)}, [['1', '2'], ['3']])


def _placed_hulls(source, shifts):
    from print_handy.core import color_geometry
    placed = {}
    with zipfile.ZipFile(source) as archive:
        for oid, shift in shifts.items():
            hull, _bounds = color_geometry._world_hull_xy(archive, {oid})[0]
            placed[oid] = [(x + shift[0], y + shift[1]) for x, y in hull]
    return placed


def test_split_plan_polygon_bins_triangles_onto_a_shared_plate(tmp_path):
    source = tmp_path / 'tri-split.3mf'
    _tri_source(source)
    # Rectangle binning alone gives the authored group two plates.
    assert plate_merge.pack({'1': (180, 110), '2': (180, 110)}, 220) is None
    plan = split_fit.split_plan_100(source, 220)
    assert plan is not None
    assert plan['plates'] == [['1:1', '2:1'], ['3:1']]
    shifts, solo = plan['shifts_by_plate']
    assert set(shifts) == {'1', '2'} and solo is None
    placed = _placed_hulls(source, shifts)
    assert plate_merge.hull_distance(placed['1'], placed['2']) >= plate_merge.SPACING_MM - 1e-9
    for poly in placed.values():
        assert all(plate_merge.EDGE_MARGIN_MM - 1e-9 <= v <= 220 - plate_merge.EDGE_MARGIN_MM + 1e-9
                   for point in poly for v in point)


def test_split_plan_polygon_plate_supersedes_solo_placements_on_an_exclusion_bed(tmp_path):
    # Deliberate change (#221 P2): on an exclusion bed the triangles would
    # each have been solo-binned with a recorded placement (Stage 2). A
    # proved shared polygon placement that also avoids the exclusion wins
    # for them; the untouched rectangle keeps its Stage 2 solo placement.
    source = tmp_path / 'tri-exclusion.3mf'
    _tri_source(source)
    plan = split_fit.split_plan_100(source, 220, exclusions=[(0, 0, 18, 28)])
    assert plan is not None
    assert plan['plates'] == [['1:1', '2:1'], ['3:1']]
    assert plan['placement_by_object'] == {'3': [18.0, 8.0]}
    shifts = plan['shifts_by_plate'][0]
    assert set(shifts) == {'1', '2'}
    exclusion = plate_merge._rect_polygon((0, 0, 18, 28))
    for poly in _placed_hulls(source, shifts).values():
        assert not plate_merge._polygons_overlap(poly, exclusion)


def test_split_plan_polygon_sharing_charges_the_brim_budget_once(tmp_path):
    source = tmp_path / 'tri-brim.3mf'
    _tri_source(source)
    plan = split_fit.split_plan_100(source, 220, brim_policy=_BRIM_10, state=_BRIM_STATE)
    assert plan is not None and plan['plates'][0] == ['1:1', '2:1']
    placed = _placed_hulls(source, plan['shifts_by_plate'][0])
    # Hull-to-hull is spacing + 2 * feature and hull-to-edge is edge +
    # feature: the budget replaces the rectangle growth, never stacks.
    assert plate_merge.hull_distance(placed['1'], placed['2']) >= \
        plate_merge.SPACING_MM + 2 * 10.0 - 1e-9
    for poly in placed.values():
        assert all(v >= plate_merge.EDGE_MARGIN_MM + 10.0 - 1e-9 for point in poly for v in point)


def test_split_plan_brim_budget_can_remove_the_shared_polygon_plate(tmp_path):
    # 185 x 115 mm complementary triangles share a polygon plate with no
    # brim; under a 10 mm brim budget the shared plate is no longer offered.
    def big(poly):
        return [(x * 185 / 180, y * 115 / 110) for x, y in poly]
    source = tmp_path / 'tri-brim-tight.3mf'
    _mesh_source(source, {'1': _tri_prism(big(_TRI_A)), '2': _tri_prism(big(_TRI_B)),
                          '3': _rect_points(150, 150, 10)}, [['1', '2'], ['3']])
    plain = split_fit.split_plan_100(source, 220)
    assert plain is not None and 'shifts_by_plate' in plain
    brimmed = split_fit.split_plan_100(source, 220, brim_policy=_BRIM_10, state=_BRIM_STATE)
    assert brimmed is None or 'shifts_by_plate' not in brimmed


def test_split_plan_rectangle_only_sources_record_no_shifts(tmp_path):
    source = tmp_path / 'two-boxes.3mf'
    _source(source, {'1': (150, 150, 10), '2': (150, 150, 10)}, [['1', '2']])
    plan = split_fit.split_plan_100(source, 220)
    assert plan is not None and 'shifts_by_plate' not in plan


def test_fit_choice_rows_carry_the_split_shifts():
    from print_handy.core.prepared_intake import fit_choice_rows
    shifts = [{'1': [1.0, 2.0, 0.0], '2': [3.0, 4.0, 0.0]}, None]
    rows = fit_choice_rows({'scales': [], 'rotations': [],
                            'split_plan': {'plates': [['1:1', '2:1'], ['3:1']],
                                           'shifts_by_plate': shifts}})
    (split,) = [row for row in rows if row['kind'] == 'split']
    assert split['shifts_by_plate'] == shifts


def test_slicing_a_polygon_split_lands_at_the_recorded_shifts(tmp_path, monkeypatch):
    import json

    import pytest

    from print_handy.core import color_geometry, model_job, orca_adapter
    from .test_plate_merge import _fake_slice
    source = tmp_path / 'tri-split.3mf'
    _tri_source(source)
    plan = split_fit.split_plan_100(source, 220)
    assert plan and plan['shifts_by_plate'][0]
    job_path = _split_job(tmp_path, source, plan)
    fake = _fake_slice()
    monkeypatch.setattr(orca_adapter, 'slice_model', fake)
    paths = model_job.slice_job(job_path)
    assert len(paths) == 2 and len(fake.calls) == 2
    saved = json.loads(job_path.read_text())
    shared = next(row for row in saved['derived_plates'] if row['part_keys'] == ['1:1', '2:1'])
    assert shared['shifts_by_object'] == plan['shifts_by_plate'][0]
    assert shared['plate_report']['merged_placement'] is True
    solo = next(row for row in saved['derived_plates'] if row['part_keys'] == ['3:1'])
    assert 'shifts_by_object' not in solo
    # Derived-plate re-read proof: every object landed at the recorded
    # corner (authored bounds minimum + shift) and hull spacing survives.
    derived = Path(shared['plate'])
    placed = {}
    with zipfile.ZipFile(derived) as archive:
        for oid in ('1', '2'):
            hull, bounds = color_geometry._world_hull_xy(archive, {oid})[0]
            placed[oid] = hull
            shift = plan['shifts_by_plate'][0][oid]
            with zipfile.ZipFile(source) as src_archive:
                authored = color_geometry._world_bounds(src_archive, {oid})[0]
            assert bounds[0][0] == pytest.approx(authored[0][0] + shift[0], abs=2e-3)
            assert bounds[1][0] == pytest.approx(authored[1][0] + shift[1], abs=2e-3)
    assert plate_merge.hull_distance(placed['1'], placed['2']) >= plate_merge.SPACING_MM - 1e-6


def test_slicing_refuses_unproved_or_malformed_shifts(tmp_path, monkeypatch):
    import pytest

    from print_handy.core import model_job, orca_adapter
    from .test_plate_merge import _fake_slice
    source = tmp_path / 'tri-split.3mf'
    _tri_source(source)
    good = {'1': [0.0, 0.0, 0.0], '2': [0.0, 0.0, 0.0]}
    plates = [['1:1', '2:1'], ['3:1']]
    bad_plans = [
        ({'plates': plates, 'shifts_by_plate': [{**good, '9': [0.0, 0.0, 0.0]}, None]}, 'shifts'),
        ({'plates': plates, 'shifts_by_plate': [{'1': [0.0, 0.0, 0.0]}, None]}, 'do not name exactly'),
        ({'plates': plates, 'shifts_by_plate': [{'1': [0.0, 0.0], '2': [0.0, 0.0, 0.0]}, None]}, 'triple'),
        # A non-finite shift fails closed: either the split validation's
        # finite check or the recipe serializer's strict JSON encoding.
        ({'plates': plates, 'shifts_by_plate': [{'1': [float('inf'), 0.0, 0.0], '2': [0.0, 0.0, 0.0]}, None]},
         '(finite|Out of range float)'),
        ({'plates': plates, 'shifts_by_plate': [good]}, 'parallel'),
        ({'plates': [['1:1'], ['2:1'], ['3:1']],
          'shifts_by_plate': [{'1': [0.0, 0.0, 0.0]}, None, None]}, 'not shared'),
        ({'plates': plates, 'shifts_by_plate': [{'1': [500.0, 0.0, 0.0], '2': [0.0, 0.0, 0.0]}, None]}, 'does not fit'),
    ]
    for plan, match in bad_plans:
        job_path = _split_job(tmp_path, source, plan)
        monkeypatch.setattr(orca_adapter, 'slice_model', _fake_slice())
        with pytest.raises((RuntimeError, ValueError), match=match):
            model_job.slice_job(job_path)


# --- #221 R1: reorientation (face change) for solo split plates ------------
# These fixtures need real triangulated solids: the supports/brim record is
# measured from the part's faces (_surface_stats), which the vertex-only
# _mesh_source/_source meshes (one token triangle) cannot express.

def _quad_tris(a, b, c, d):
    return [(a, b, c), (a, c, d)]


def _box_tris(x0, x1, y0, y1, z0, z1):
    """The 12 outward-wound triangles of a solid box."""
    tris = []
    tris += _quad_tris((x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0))  # bottom -z
    tris += _quad_tris((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1))  # top +z
    tris += _quad_tris((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1))  # front -y
    tris += _quad_tris((x0, y1, z0), (x0, y1, z1), (x1, y1, z1), (x1, y1, z0))  # back +y
    tris += _quad_tris((x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0))  # left -x
    tris += _quad_tris((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1))  # right +x
    return tris


def _frame_tris(x1, y1, z1, hx0, hx1, hy0, hy1):
    """A flat plate (x1 x y1 x z1) with a rectangular through-hole along Z."""
    tris = []
    for rx0, rx1, ry0, ry1 in ((0, hx0, 0, y1), (hx1, x1, 0, y1),
                               (hx0, hx1, 0, hy0), (hx0, hx1, hy1, y1)):
        tris += _quad_tris((rx0, ry0, z1), (rx1, ry0, z1), (rx1, ry1, z1), (rx0, ry1, z1))  # top
        tris += _quad_tris((rx0, ry0, 0), (rx0, ry1, 0), (rx1, ry1, 0), (rx1, ry0, 0))      # bottom
    tris += _quad_tris((0, 0, 0), (x1, 0, 0), (x1, 0, z1), (0, 0, z1))        # outer front -y
    tris += _quad_tris((0, y1, 0), (0, y1, z1), (x1, y1, z1), (x1, y1, 0))    # outer back +y
    tris += _quad_tris((0, 0, 0), (0, 0, z1), (0, y1, z1), (0, y1, 0))        # outer left -x
    tris += _quad_tris((x1, 0, 0), (x1, y1, 0), (x1, y1, z1), (x1, 0, z1))    # outer right +x
    tris += _quad_tris((hx0, hy0, 0), (hx0, hy1, 0), (hx0, hy1, z1), (hx0, hy0, z1))  # hole wall +x
    tris += _quad_tris((hx1, hy0, 0), (hx1, hy0, z1), (hx1, hy1, z1), (hx1, hy1, 0))  # hole wall -x
    tris += _quad_tris((hx0, hy0, 0), (hx0, hy0, z1), (hx1, hy0, z1), (hx1, hy0, 0))  # hole wall +y
    tris += _quad_tris((hx0, hy1, 0), (hx1, hy1, 0), (hx1, hy1, z1), (hx0, hy1, z1))  # hole wall -y
    return tris


def _solid_source(path, solids, plates):
    """A 3MF whose meshes are explicit triangle lists {object_id: [(a, b, c), ...]}.

    Same package shape as _mesh_source, but with complete triangulated
    solids, so face-based measurements (bed contact, overhang) are real.
    """
    resources, items, objects = [], [], []
    for oid, tris in solids.items():
        vertices = sorted({p for tri in tris for p in tri})
        index = {p: i for i, p in enumerate(vertices)}
        body = ''.join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in vertices)
        faces = ''.join(f'<triangle v1="{index[a]}" v2="{index[b]}" v3="{index[c]}"/>'
                        for a, b, c in tris)
        model = (f'<model unit="millimeter"><resources><object id="1"><mesh><vertices>{body}</vertices>'
                 f'<triangles>{faces}</triangles></mesh></object></resources></model>')
        with zipfile.ZipFile(path, 'a') as archive:
            archive.writestr(f'3D/Objects/object_{oid}.model', model)
        resources.append(f'<object id="{oid}" name="Part {oid}"><components><component p:path="/3D/Objects/object_{oid}.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>')
        items.append(f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {20 + 5 * int(oid)} 30 0"/>')
        objects.append(f'<object id="{oid}"><metadata key="name" value="Part {oid}"/><metadata key="extruder" value="1"/><part id="1"><metadata key="name" value="Part {oid}"/><metadata key="extruder" value="1"/></part></object>')
    plate_xml = ''.join(
        f'<plate><metadata key="plater_id" value="{i}"/>' +
        ''.join(f'<model_instance><metadata key="object_id" value="{o}"/></model_instance>' for o in ids) + '</plate>'
        for i, ids in enumerate(plates, 1))
    with zipfile.ZipFile(path, 'a') as archive:
        archive.writestr('3D/3dmodel.model', f'<model xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" unit="millimeter"><resources>{"".join(resources)}</resources><build>{"".join(items)}</build></model>')
        archive.writestr('Metadata/model_settings.config', f'<config>{"".join(objects)}{plate_xml}</config>')
        archive.writestr('Metadata/project_settings.config', '{\n  "filament_colour": [\n    "#FFFFFF"\n  ],\n  "filament_type": [\n    "PLA"\n  ]\n}')
        archive.writestr('3D/_rels/3dmodel.model.rels', '<Relationships>' + ''.join(f'<Relationship Target="/3D/Objects/object_{o}.model" Id="r{o}"/>' for o in solids) + '</Relationships>')
        for i in range(1, len(plates) + 1):
            archive.writestr(f'Metadata/plate_{i}.png', b'png')


def _frame_source(path):
    """The supports fixture: a 210 x 190 x 40 plate with a through-hole.

    As authored its 210 mm side exceeds the 204 mm usable bed at every yaw.
    Stood on an end (the +/-X face change) its footprint is 40 x 190 and it
    stands 210 tall; one hole wall then faces down away from the bed, which
    the surface measurement records as overhang needing supports.
    """
    _solid_source(path, {'1': _frame_tris(210, 190, 40, 80, 130, 60, 140),
                         '2': _box_tris(0, 150, 0, 150, 0, 10)}, [['1'], ['2']])


def _fake_slice_recording_supports():
    from .test_plate_merge import _fake_slice
    base = _fake_slice()
    calls = []

    def fake(source, folder, state, **kwargs):
        supports = kwargs.pop('supports', None)
        result = base(source, folder, state, **kwargs)
        calls.append({'supports': supports, 'printable': base.calls[-1]['printable']})
        return result

    fake.calls = calls
    return fake


def test_part_that_fits_only_reoriented_gets_a_solo_plate(tmp_path):
    # The worked fixture: a 230 x 190 x 30 box on a 220 bed (height limit
    # 250) fits at no bed-plane yaw; stood on its end (about Y) its
    # footprint is 30 x 190 and it stands 230 tall. About X its footprint
    # keeps the 230 mm side in the bed plane at either cardinal turn.
    source = tmp_path / 'side-box.3mf'
    _solid_source(source, {'1': _box_tris(0, 230, 0, 190, 0, 30),
                           '2': _box_tris(0, 150, 0, 150, 0, 10)}, [['1'], ['2']])
    rows = {row['object_id']: row for row in split_fit.per_part_fit(source, bed=220, height=250)}
    box = rows['1']
    assert box['fits_authored'] is False and box['fits_full_size'] is True
    record = box['reorientation']
    assert record['axis'] == '-X' and record['yaw_deg'] == 0.0
    assert record['dims_mm'] == [30.0, 190.0, 230.0]
    assert record['supports'] is False and record['brim_recommended'] is False
    plan = split_fit.split_plan_100(source, bed=220, height=250)
    assert plan is not None and sorted(plan['plates']) == [['1:1'], ['2:1']]
    assert plan['reorientation_by_object'] == {'1': record}
    # One transform record per object: never also a yaw or a placement.
    assert '1' not in plan.get('yaw_by_object', {})
    assert '1' not in plan.get('placement_by_object', {})


def test_reorientation_never_chosen_when_authored_or_yaw_fits(tmp_path):
    source = tmp_path / 'no-reorientation.3mf'
    _source(source, {'1': (230, 50, 20), '2': (150, 150, 10)}, [['1', '2']])
    rows = {row['object_id']: row for row in split_fit.per_part_fit(source, 220)}
    assert rows['1']['fits_full_size'] is True and rows['1']['best_yaw_deg'] > 0
    assert rows['2']['fits_authored'] is True
    assert all(row['reorientation'] is None for row in rows.values())


def test_reorientation_with_a_diagonal_turn_cures_the_issue_76_stand(tmp_path):
    # THE #221 DIAGONAL VERDICT CHANGE, named and deliberate. Through R3
    # this test asserted the retained Stand stays blocking at every
    # orientation, and it did: about X it would stand 230 tall — over
    # the 220 mm limit — and about Y its 30 x 230 end footprint fits at
    # neither cardinal bed turn (230 mm against the 204 mm usable bed).
    # With diagonal bed turns the about-Y candidate fits: at 45 degrees
    # the end footprint's envelope is 183.8 x 183.8 mm, inside the usable
    # bed with the edge margin charged exactly once, standing 130 tall.
    # The Stand is therefore offered reoriented — a feature, announced
    # here and on #221/#76, not a smuggled relaxation: every proof
    # (flat landing, height, footprint, once-only margins) is the R1
    # proof; only the bed turn is no longer restricted to cardinals.
    source = tmp_path / 'stand.3mf'
    _solid_source(source, {'1': _box_tris(0, 130, 0, 230, 0, 30),
                           '2': _box_tris(0, 195, 0, 200, 0, 119.8)}, [['1'], ['2']])
    rows = {row['object_id']: row for row in split_fit.per_part_fit(source, 220)}
    stand = rows['1']
    assert stand['fits_authored'] is False and stand['fits_full_size'] is True
    record = stand['reorientation']
    assert record['axis'] == '-X' and record['yaw_deg'] == 45.0
    assert record['dims_mm'] == [183.848, 183.848, 130.0]
    assert record['supports'] is False and record['brim_recommended'] is False
    # The proof in one place, from the same sweep the planner checks: the
    # 30 x 230 end footprint at 45 degrees, and at the cardinal turns.
    w, d = split_fit.yaw_envelope(30, 230, 45)
    assert (round(w, 3), round(d, 3)) == (183.848, 183.848) == tuple(record['dims_mm'][:2])
    assert split_fit.yaw_envelope(30, 230, 0)[1] > 204
    assert split_fit.yaw_envelope(30, 230, 90)[0] > 204
    # About X still yields no candidate at any turn: the binding
    # constraint there is the height proof (it would stand 230 tall),
    # not the turn — exactly one candidate is proved for the Stand.
    from print_handy.core import color_geometry
    with zipfile.ZipFile(source) as archive:
        items = color_geometry._world_item_points(archive, {'1'})
    proved = split_fit._reorientation_candidates(source, '1', items, (204.0, 204.0), 220.0)
    assert [c['axis'] for c in proved] == ['-X']
    plan = split_fit.split_plan_100(source, 220)
    assert plan is not None and sorted(plan['plates']) == [['1:1'], ['2:1']]
    assert plan['reorientation_by_object'] == {'1': record}
    # One transform record per object: never also a yaw or a placement.
    assert '1' not in plan.get('yaw_by_object', {})
    assert '1' not in plan.get('placement_by_object', {})


def test_rank_reorientation_candidates_orders_by_fit_quality():
    # catduck's steering — fit as much as possible — as a deterministic
    # order over proved candidates: bed margin first, then no-supports
    # over supports, then larger bed contact; axis name breaks exact ties.
    base = {'axis': '+X', 'rotation': [1, 0, 0, 0, 1, 0, 0, 0, 1], 'yaw_deg': 0.0,
            'dims_mm': [10.0, 10.0, 10.0], 'supports': False, 'brim_recommended': False,
            'bed_contact_mm2': 100, 'overhang_mm2': 0, 'margin_mm': 10.0}
    roomy_supported = {**base, 'axis': '+Y', 'margin_mm': 30.0, 'supports': True,
                       'bed_contact_mm2': 50}
    snug_plain = {**base, 'axis': '-X', 'margin_mm': 20.0, 'bed_contact_mm2': 5000}
    snug_supported = {**base, 'axis': '-Y', 'margin_mm': 20.0, 'supports': True,
                      'bed_contact_mm2': 9000}
    ranked = split_fit._rank_reorientation_candidates(
        [snug_supported, snug_plain, roomy_supported])
    assert [c['axis'] for c in ranked] == ['+Y', '-X', '-Y']
    tied = split_fit._rank_reorientation_candidates(
        [{**base, 'axis': '-X', 'bed_contact_mm2': 200},
         {**base, 'axis': '+X', 'bed_contact_mm2': 200},
         {**base, 'axis': '-Y', 'bed_contact_mm2': 300}])
    assert [c['axis'] for c in tied] == ['-Y', '+X', '-X']


def test_reorientation_candidates_prove_each_item_of_a_multi_item_object(tmp_path):
    # R3 eligibility: the candidate proof is per build item, so a multi-item
    # object is proved exactly like a single-item one — the one rigid
    # rotation is checked against every item's own rotated floor. Two copies
    # of the same item, both landing flat, get the same candidates as one;
    # a second item offset so it would hover under the turn (shifted in X
    # and Y, so every face change leaves one item in the air) gets none.
    from print_handy.core import color_geometry
    source = tmp_path / 'box.3mf'
    _solid_source(source, {'1': _box_tris(0, 230, 0, 190, 0, 30)}, [['1']])
    with zipfile.ZipFile(source) as archive:
        items = color_geometry._world_item_points(archive, {'1'})
    assert len(items) == 1
    single = split_fit._reorientation_candidates(source, '1', items, (204.0, 204.0), 250.0)
    assert single
    doubled = split_fit._reorientation_candidates(
        source, '1', items + items, (204.0, 204.0), 250.0)
    assert [c['axis'] for c in doubled] == [c['axis'] for c in single]
    assert [c['dims_mm'] for c in doubled] == [c['dims_mm'] for c in single]
    hovering = [[(x + 30.0, y + 30.0, z) for x, y, z in item] for item in items]
    assert split_fit._reorientation_candidates(
        source, '1', items + hovering, (204.0, 204.0), 250.0) == []


def test_reorientation_records_supports_and_slicing_passes_them_for_that_plate_only(tmp_path, monkeypatch):
    import json

    from print_handy.core import model_job, orca_adapter
    source = tmp_path / 'frame.3mf'
    _frame_source(source)
    (row,) = [r for r in split_fit.per_part_fit(source, 220) if r['object_id'] == '1']
    record = row['reorientation']
    assert record is not None and record['supports'] is True
    assert record['overhang_mm2'] > 100  # the hole wall that ends up facing down, floating
    plan = split_fit.split_plan_100(source, 220)
    assert plan is not None and plan['reorientation_by_object']['1']['supports'] is True
    job_path = _split_job(tmp_path, source, plan)
    fake = _fake_slice_recording_supports()
    monkeypatch.setattr(orca_adapter, 'slice_model', fake)
    paths = model_job.slice_job(job_path)
    assert len(paths) == 2 and len(fake.calls) == 2
    by_printable = {tuple(call['printable']): call['supports'] for call in fake.calls}
    # The recorded decision reaches the slicer for the reoriented plate
    # only; the authored plate keeps today's default (no supports argument).
    assert by_printable[('1',)] is True and by_printable[('2',)] is None
    saved = json.loads(job_path.read_text())
    assert saved['derived_plates']


def test_slicing_a_reoriented_split_applies_the_recorded_rotation(tmp_path, monkeypatch):
    import json

    import pytest

    from print_handy.core import color_geometry, model_job, orca_adapter
    source = tmp_path / 'frame.3mf'
    _frame_source(source)
    plan = split_fit.split_plan_100(source, 220)
    record = plan['reorientation_by_object']['1']
    job_path = _split_job(tmp_path, source, plan)
    monkeypatch.setattr(orca_adapter, 'slice_model', _fake_slice_recording_supports())
    model_job.slice_job(job_path)
    saved = json.loads(job_path.read_text())
    row = next(r for r in saved['derived_plates'] if r['part_keys'] == ['1:1'])
    assert row['reorientation']['axis'] == record['axis']
    assert row['reorientation']['rotation'] == pytest.approx(record['rotation'])
    assert row['plate_report']['fit']['rotation'] is not None
    # Independent re-read: the derived plate's part measures exactly the
    # rotated authored extents and sits on the bed.
    with zipfile.ZipFile(Path(row['plate'])) as archive:
        box = color_geometry._world_bounds(archive, {'1'})[0]
    extents = [box[a][1] - box[a][0] for a in range(3)]
    assert extents == pytest.approx(record['dims_mm'], abs=2e-3)
    assert abs(box[2][0]) <= 2e-3
    other = next(r for r in saved['derived_plates'] if r['part_keys'] == ['2:1'])
    assert 'reorientation' not in other and 'fit' not in other['plate_report']


def test_slicing_refuses_unproved_or_mixed_reorientations(tmp_path, monkeypatch):
    import pytest

    from print_handy.core import model_job, orca_adapter
    source = tmp_path / 'frame.3mf'
    _frame_source(source)
    plan = split_fit.split_plan_100(source, 220)
    record = plan['reorientation_by_object']['1']
    plates = plan['plates']
    mirrored = {**record, 'rotation': [1, 0, 0, 0, 1, 0, 0, 0, -1]}
    scaled = {**record, 'rotation': [2, 0, 0, 0, 1, 0, 0, 0, 1]}
    wrong_axis = {**record, 'axis': '+Y'}
    short = {**record, 'rotation': [1, 0, 0, 0, 1, 0]}
    unflagged = {**record, 'supports': 'yes'}
    bad_plans = [
        ({'plates': plates, 'reorientation_by_object': {'9': record}}, 'unknown object'),
        ({'plates': [['1:1', '2:1']], 'reorientation_by_object': {'1': record}},
         'not that part alone'),
        ({'plates': plates, 'yaw_by_object': {'1': 30.0},
          'reorientation_by_object': {'1': record}}, 'also records a turn'),
        ({'plates': plates, 'shifts_by_plate': [{'1': [0.0, 0.0, 0.0]}, None],
          'reorientation_by_object': {'1': record}}, 'also records shifts'),
        ({'plates': plates, 'reorientation_by_object': {'1': mirrored}}, 'mirror'),
        ({'plates': plates, 'reorientation_by_object': {'1': scaled}}, 'orthonormal'),
        ({'plates': plates, 'reorientation_by_object': {'1': wrong_axis}},
         'not the recorded face change'),
        ({'plates': plates, 'reorientation_by_object': {'1': short}}, '9-value'),
        ({'plates': plates, 'reorientation_by_object': {'1': unflagged}}, 'boolean supports'),
    ]
    for bad_plan, match in bad_plans:
        job_path = _split_job(tmp_path, source, bad_plan)
        monkeypatch.setattr(orca_adapter, 'slice_model', _fake_slice_recording_supports())
        with pytest.raises((RuntimeError, ValueError), match=match):
            model_job.slice_job(job_path)


def test_fit_choice_rows_carry_the_split_reorientation():
    from print_handy.core.prepared_intake import fit_choice_rows
    record = {'axis': '-X', 'rotation': [0, 0, 1, 0, 1, 0, -1, 0, 0], 'yaw_deg': 0.0,
              'dims_mm': [40.0, 190.0, 210.0], 'supports': True, 'brim_recommended': False,
              'bed_contact_mm2': 7600, 'overhang_mm2': 3200}
    rows = fit_choice_rows({'scales': [], 'rotations': [],
                            'split_plan': {'plates': [['1:1'], ['2:1']],
                                           'reorientation_by_object': {'1': record}}})
    (split,) = [row for row in rows if row['kind'] == 'split']
    assert split['reorientation_by_object'] == {'1': record}


def test_exclusion_bed_offers_a_placed_reorientation(tmp_path):
    # R3 lifts the R1 withholding: on an exclusion bed a reorientation
    # candidate is proved only with a placement of the reoriented footprint
    # that avoids the exclusion, and the plan records that placement for the
    # reoriented part exactly as for any placed part.
    source = tmp_path / 'side-box.3mf'
    _solid_source(source, {'1': _box_tris(0, 230, 0, 190, 0, 30),
                           '2': _box_tris(0, 150, 0, 150, 0, 10)}, [['1'], ['2']])
    rows = {row['object_id']: row for row in split_fit.per_part_fit(
        source, bed=220, height=250, exclusions=[(0, 0, 18, 28)])}
    assert rows['1']['fits_full_size'] is True and rows['1']['reorientation'] is not None
    assert rows['1']['reorientation']['dims_mm'] == [30.0, 190.0, 230.0]
    # The 30 x 190 reoriented footprint lands flush against the exclusion's
    # right edge, on the bed edge — the same spot the yaw path would choose.
    assert rows['1']['placement_mm'] == [18.0, 8.0]
    plan = split_fit.split_plan_100(source, bed=220, height=250,
                                    exclusions=[(0, 0, 18, 28)])
    assert plan is not None and sorted(plan['plates']) == [['1:1'], ['2:1']]
    assert plan['reorientation_by_object']['1'] == rows['1']['reorientation']
    assert plan['placement_by_object']['1'] == [18.0, 8.0]


def test_exclusion_bed_reorientation_stays_blocking_without_a_placement(tmp_path):
    # The other half of the R3 rule: when no placement of the reoriented
    # footprint avoids the exclusion either, the part stays blocking and its
    # reason names the exclusion — no offer the plan cannot land.
    source = tmp_path / 'side-box.3mf'
    _solid_source(source, {'1': _box_tris(0, 230, 0, 190, 0, 30),
                           '2': _box_tris(0, 150, 0, 150, 0, 10)}, [['1'], ['2']])
    rows = {row['object_id']: row for row in split_fit.per_part_fit(
        source, bed=220, height=250, exclusions=[(0, 0, 200, 220)])}
    assert rows['1']['fits_full_size'] is False and rows['1']['reorientation'] is None
    assert 'only reoriented' in rows['1']['blocking_reason']
    assert 'exclusion' in rows['1']['blocking_reason']
    assert split_fit.split_plan_100(source, bed=220, height=250,
                                    exclusions=[(0, 0, 200, 220)]) is None


def test_brim_budget_can_remove_the_reorientation_offer(tmp_path):
    source = tmp_path / 'side-box.3mf'
    _solid_source(source, {'1': _box_tris(0, 230, 0, 190, 0, 30),
                           '2': _box_tris(0, 150, 0, 150, 0, 10)}, [['1'], ['2']])
    (plain,) = [r for r in split_fit.per_part_fit(source, bed=220, height=250)
                if r['object_id'] == '1']
    assert plain['reorientation'] is not None
    # With a 10 mm brim the usable area is 184 mm: the 190 mm side of the
    # rotated footprint no longer fits at a cardinal turn — but since
    # #221's diagonal stage the offer survives as a diagonal turn (the
    # 30 x 190 footprint at 45 degrees occupies 155.6 mm), which is the
    # budget applied once, inside the same usable area as every verdict.
    (row,) = [r for r in split_fit.per_part_fit(source, bed=220, height=250,
                                               brim_policy=_BRIM_10, state=_BRIM_STATE)
              if r['object_id'] == '1']
    assert row['fits_full_size'] is True and row['reorientation'] is not None
    assert row['reorientation']['yaw_deg'] == 45.0
    assert row['reorientation']['dims_mm'] == [155.563, 155.563, 230.0]
    # A 30 mm brim shrinks the usable area to 144 mm, below even the
    # diagonal envelope, so the offer does go away (again: budget applied
    # exactly once).
    brim_30 = {'type': 'outer_only', 'width_mm': 30.0, 'label': 'Outer-only brim, 30 mm'}
    (row,) = [r for r in split_fit.per_part_fit(source, bed=220, height=250,
                                               brim_policy=brim_30, state=_BRIM_STATE)
              if r['object_id'] == '1']
    assert row['fits_full_size'] is False and row['reorientation'] is None
    assert split_fit.split_plan_100(source, bed=220, height=250,
                                    brim_policy=brim_30, state=_BRIM_STATE) is None


def test_changing_the_reorientation_record_changes_the_slice_recipe_sha():
    from print_handy.core.background_slice import slice_binding
    record = {'axis': '-X', 'rotation': [0, 0, 1, 0, 1, 0, -1, 0, 0], 'yaw_deg': 0.0,
              'dims_mm': [40.0, 190.0, 210.0], 'supports': True, 'brim_recommended': False,
              'bed_contact_mm2': 7600, 'overhang_mm2': 3200}
    base = {'color_decisions': {'mode': 'single_color', 'source_sha256': 'source-a',
                                 'selection': {'slot': 1, 'color': 'white', 'material': 'PLA'}},
            'scale': 1.0, 'copies': 1, 'plate_mode': 'separate',
            'full_size_split_plan': {'plates': [['1:1'], ['2:1']]}}
    changed = {**base, 'full_size_split_plan': {
        **base['full_size_split_plan'], 'reorientation_by_object': {'1': record}}}
    assert slice_binding(changed, 'source-a') != slice_binding(base, 'source-a')


# --- #221 diagonal: free-yaw bed turns in reorientation ------------------
# In-plane yaw has recorded sweep angles (diagonals included) since the
# mesh-hull evaluation; the diagonal stage lifts the one cardinal-only
# confinement left, the bed turn composed into a reorientation record.
# The rules pinned below: a diagonal turn is an additional candidate
# class — proved only when no cardinal turn fits, ranked after every
# cardinal-turn candidate — and slicing accepts exactly the records the
# planner can prove (finite yaw in [0, 90], matrix matching the record).


def test_diagonal_reorientation_never_displaces_a_cardinal_candidate(tmp_path):
    # The side box proves two reorientation candidates: stood on its end
    # at a cardinal turn (30 x 190 footprint, 14 mm margin) and — only
    # since the diagonal stage — the other face at 45 degrees, whose
    # margin is LARGER (20.2 mm). The recorded winner is still the
    # cardinal one: diagonal turns add fits, they never replace a verdict
    # a cardinal plan already achieved.
    from print_handy.core import color_geometry
    source = tmp_path / 'side-box.3mf'
    _solid_source(source, {'1': _box_tris(0, 230, 0, 190, 0, 30),
                           '2': _box_tris(0, 150, 0, 150, 0, 10)}, [['1'], ['2']])
    with zipfile.ZipFile(source) as archive:
        items = color_geometry._world_item_points(archive, {'1'})
    proved = split_fit._reorientation_candidates(source, '1', items, (204.0, 204.0), 250.0)
    by_yaw = {c['yaw_deg']: c for c in proved}
    assert set(by_yaw) == {0.0, 45.0}
    assert by_yaw[45.0]['margin_mm'] > by_yaw[0.0]['margin_mm']
    ranked = split_fit._ranked_reorientation_candidates(proved)
    assert ranked[0]['yaw_deg'] == 0.0
    (row,) = [r for r in split_fit.per_part_fit(source, bed=220, height=250)
              if r['object_id'] == '1']
    assert row['reorientation']['yaw_deg'] == 0.0
    assert row['reorientation']['dims_mm'] == [30.0, 190.0, 230.0]


def test_slicing_a_diagonal_reorientation_lands_at_the_recorded_rotation(tmp_path, monkeypatch):
    import json

    import pytest

    from print_handy.core import color_geometry, model_job, orca_adapter
    source = tmp_path / 'stand.3mf'
    _solid_source(source, {'1': _box_tris(0, 130, 0, 230, 0, 30),
                           '2': _box_tris(0, 195, 0, 200, 0, 119.8)}, [['1'], ['2']])
    plan = split_fit.split_plan_100(source, 220)
    record = plan['reorientation_by_object']['1']
    assert record['yaw_deg'] == 45.0
    job_path = _split_job(tmp_path, source, plan)
    fake = _fake_slice_recording_supports()
    monkeypatch.setattr(orca_adapter, 'slice_model', fake)
    paths = model_job.slice_job(job_path)
    assert len(paths) == 2 and len(fake.calls) == 2
    # The record's supports decision (False for the plain Stand) reaches
    # the slicer for the diagonal plate only.
    by_printable = {tuple(call['printable']): call['supports'] for call in fake.calls}
    assert by_printable[('1',)] is False and by_printable[('2',)] is None
    saved = json.loads(job_path.read_text())
    row = next(r for r in saved['derived_plates'] if r['part_keys'] == ['1:1'])
    assert row['reorientation']['yaw_deg'] == 45.0
    assert row['reorientation']['rotation'] == pytest.approx(record['rotation'])
    assert row['plate_report']['fit']['rotation'] is not None
    # Independent re-read: the derived plate's part measures exactly the
    # diagonally rotated authored extents and sits on the bed.
    with zipfile.ZipFile(Path(row['plate'])) as archive:
        box = color_geometry._world_bounds(archive, {'1'})[0]
    extents = [box[a][1] - box[a][0] for a in range(3)]
    assert extents == pytest.approx(record['dims_mm'], abs=2e-3)
    assert abs(box[2][0]) <= 2e-3


def test_slicing_refuses_diagonal_reorientation_records_that_do_not_prove(tmp_path, monkeypatch):
    import pytest

    from print_handy.core import model_job, orca_adapter
    from print_handy.core.orca_adapter import UP_AXES, _matmul, _yaw
    source = tmp_path / 'stand.3mf'
    _solid_source(source, {'1': _box_tris(0, 130, 0, 230, 0, 30),
                           '2': _box_tris(0, 195, 0, 200, 0, 119.8)}, [['1'], ['2']])
    plan = split_fit.split_plan_100(source, 220)
    record = plan['reorientation_by_object']['1']
    plates = plan['plates']
    up = UP_AXES[record['axis']][1]
    mismatched = {**record, 'rotation': _matmul(_yaw(0.0), up)}
    bad_plans = [
        # A 45-degree record whose matrix is the cardinal composition.
        ({'plates': plates, 'reorientation_by_object': {'1': mismatched}},
         'not the recorded face change'),
        # Angles outside the legal set refuse, however tidy the matrix.
        ({'plates': plates, 'reorientation_by_object': {
            '1': {**record, 'yaw_deg': 120.0, 'rotation': _matmul(_yaw(120.0), up)}}},
         'between 0 and 90 degrees'),
        ({'plates': plates, 'reorientation_by_object': {
            '1': {**record, 'yaw_deg': -5.0, 'rotation': _matmul(_yaw(-5.0), up)}}},
         'between 0 and 90 degrees'),
    ]
    for bad_plan, match in bad_plans:
        job_path = _split_job(tmp_path, source, bad_plan)
        monkeypatch.setattr(orca_adapter, 'slice_model', _fake_slice_recording_supports())
        with pytest.raises((RuntimeError, ValueError), match=match):
            model_job.slice_job(job_path)


def test_split_plan_diagonal_reorientation_can_share_a_plate(tmp_path, monkeypatch):
    # R3's shared machinery carries a diagonal record unchanged: the
    # Stand, reoriented at 45 degrees, packs by its reoriented hull
    # alongside the authored 60 mm box on one plate (the corner beside
    # the diagonal bar is genuinely free — hull-true packing, not AABB).
    import json

    from print_handy.core import model_job, orca_adapter
    source = tmp_path / 'diagonal-share.3mf'
    _solid_source(source, {'1': _box_tris(0, 130, 0, 230, 0, 30),
                           '2': _box_tris(0, 60, 0, 60, 0, 10),
                           '3': _box_tris(0, 150, 0, 150, 0, 10)}, [['1', '2'], ['3']])
    plan = split_fit.split_plan_100(source, 220)
    assert plan is not None
    assert {frozenset(plate) for plate in plan['plates']} == \
        {frozenset({'1:1', '2:1'}), frozenset({'3:1'})}
    assert plan['reorientation_by_object']['1']['yaw_deg'] == 45.0
    assert set(_shared_shifts(plan, {'1:1', '2:1'})) == {'1', '2'}
    job_path = _split_job(tmp_path, source, plan)
    monkeypatch.setattr(orca_adapter, 'slice_model', _fake_slice_recording_supports())
    paths = model_job.slice_job(job_path)
    assert len(paths) == 2
    saved = json.loads(job_path.read_text())
    shared = next(row for row in saved['derived_plates']
                  if set(row['part_keys']) == {'1:1', '2:1'})
    # The re-read proof inside slice_job has verified the diagonal
    # member landed at its rotated bounds plus its recorded shift.
    assert shared['reorientation_by_object']['1']['yaw_deg'] == 45.0
    assert set(shared['plate_report']['fit_by_object']) == {'1'}
    assert shared['shifts_by_object'] == _shared_shifts(plan, {'1:1', '2:1'})


# --- #221 R2: per-object yaw on shared split plates (the deferred P3) -----
# A 215 x 35 mm bar fits a 220 mm bed only turned (45 degrees, a 177 mm
# envelope); a 60 mm square fits as authored but cannot shelf-pack with the
# 150 mm square. Before R2 the bar always went solo: three plates. The
# rotated bar and the small square pack by their true hulls, so R2 shares
# one plate between them and leaves the big square to the other.

def _yaw_share_source(path):
    _mesh_source(path, {'1': _rect_points(215, 35, 10), '2': _rect_points(60, 60, 10),
                        '3': _rect_points(150, 150, 10)}, [['1', '2'], ['3']])


def _rotated_hull(source, oid, degrees):
    import math

    from print_handy.core import color_geometry
    with zipfile.ZipFile(source) as archive:
        hull = color_geometry._world_hull_xy(archive, {oid})[0][0]
    return split_fit._rotate_hull_xy(hull, degrees)


def _shared_shifts(plan, keys):
    index = next(i for i, plate in enumerate(plan['plates']) if set(plate) == set(keys))
    return plan['shifts_by_plate'][index]


def test_split_plan_yaw_part_can_share_a_plate(tmp_path):
    source = tmp_path / 'yaw-share.3mf'
    _yaw_share_source(source)
    plan = split_fit.split_plan_100(source, 220)
    assert plan is not None
    assert {frozenset(plate) for plate in plan['plates']} == \
        {frozenset({'1:1', '2:1'}), frozenset({'3:1'})}
    assert plan['yaw_by_object'] == {'1': 45.0}
    shifts = _shared_shifts(plan, {'1:1', '2:1'})
    assert set(shifts) == {'1', '2'}
    # The proved placement keeps true hull spacing: the bar's hull at its
    # recorded turn, the square's as authored, each moved by its shift.
    placed = {'1': [(x + shifts['1'][0], y + shifts['1'][1])
                    for x, y in _rotated_hull(source, '1', 45.0)],
              '2': [(x + shifts['2'][0], y + shifts['2'][1])
                    for x, y in _rotated_hull(source, '2', 0.0)]}
    assert plate_merge.hull_distance(placed['1'], placed['2']) >= \
        plate_merge.SPACING_MM - 2e-3
    for poly in placed.values():
        # Recorded shifts are rounded to 0.001 mm, so the placed hull can
        # sit that far inside the proved margin (the 2e-3 proof tolerance).
        assert all(plate_merge.EDGE_MARGIN_MM - 2e-3 <= v
                   <= 220 - plate_merge.EDGE_MARGIN_MM + 2e-3
                   for point in poly for v in point)


def test_split_plan_prefers_a_shared_yaw_plate_over_solo_plates(tmp_path):
    # Fit ranking (#221): a proved shared plate fits more parts per plate
    # than the solo fallback, so it wins — this fixture's pre-R2 plan was
    # three plates (turned bar solo, both squares solo).
    source = tmp_path / 'yaw-share.3mf'
    _yaw_share_source(source)
    plan = split_fit.split_plan_100(source, 220)
    assert plan is not None and len(plan['plates']) == 2
    shared = next(plate for plate in plan['plates'] if '1:1' in plate)
    assert shared == ['2:1', '1:1'] or set(shared) == {'1:1', '2:1'}


def test_split_plan_two_yaw_parts_can_share_a_plate(tmp_path):
    # Two identical 215 x 30 mm right triangles each fit only turned
    # (49 degrees); turned together, their hulls pack onto one plate.
    tri = [(0, 0), (215, 0), (0, 30)]
    source = tmp_path / 'yaw-pair.3mf'
    _mesh_source(source, {'1': _tri_prism(tri), '2': _tri_prism(tri),
                          '3': _rect_points(150, 150, 10)}, [['1', '2'], ['3']])
    rows = {row['object_id']: row for row in split_fit.per_part_fit(source, 220)}
    assert rows['1']['fits_authored'] is False and rows['1']['best_yaw_deg'] > 0
    plan = split_fit.split_plan_100(source, 220)
    assert plan is not None
    assert {frozenset(plate) for plate in plan['plates']} == \
        {frozenset({'1:1', '2:1'}), frozenset({'3:1'})}
    assert plan['yaw_by_object'] == {'1': rows['1']['best_yaw_deg'],
                                     '2': rows['2']['best_yaw_deg']}
    assert set(_shared_shifts(plan, {'1:1', '2:1'})) == {'1', '2'}


def test_split_plan_shared_yaw_composes_with_exclusions(tmp_path):
    # On an exclusion bed the shared rotated placement supersedes the solo
    # placements of its members (the P2 rule), the untouched big square
    # keeps its Stage 2 placement, and no placed hull overlaps the exclusion.
    source = tmp_path / 'yaw-share-exclusion.3mf'
    _yaw_share_source(source)
    plan = split_fit.split_plan_100(source, 220, exclusions=[(0, 0, 18, 28)])
    assert plan is not None
    assert {frozenset(plate) for plate in plan['plates']} == \
        {frozenset({'1:1', '2:1'}), frozenset({'3:1'})}
    assert plan['placement_by_object'] == {'3': [18.0, 8.0]}
    shifts = _shared_shifts(plan, {'1:1', '2:1'})
    exclusion = plate_merge._rect_polygon((0, 0, 18, 28))
    for oid, degrees in (('1', 45.0), ('2', 0.0)):
        placed = [(x + shifts[oid][0], y + shifts[oid][1])
                  for x, y in _rotated_hull(source, oid, degrees)]
        assert not plate_merge._polygons_overlap(placed, exclusion)


def test_slicing_a_shared_yaw_split_lands_at_the_recorded_turn(tmp_path, monkeypatch):
    import json
    import math

    import pytest

    from print_handy.core import color_geometry, model_job, orca_adapter
    from .test_plate_merge import _fake_slice
    source = tmp_path / 'yaw-share.3mf'
    _yaw_share_source(source)
    plan = split_fit.split_plan_100(source, 220)
    assert plan and plan['yaw_by_object'] == {'1': 45.0}
    job_path = _split_job(tmp_path, source, plan)
    fake = _fake_slice()
    monkeypatch.setattr(orca_adapter, 'slice_model', fake)
    paths = model_job.slice_job(job_path)
    assert len(paths) == 2 and len(fake.calls) == 2
    saved = json.loads(job_path.read_text())
    shared = next(row for row in saved['derived_plates']
                  if set(row['part_keys']) == {'1:1', '2:1'})
    assert shared['yaw_by_object'] == {'1': 45.0}
    assert shared['shifts_by_object'] == _shared_shifts(plan, {'1:1', '2:1'})
    assert set(shared['plate_report']['fit_by_object']) == {'1'}
    solo = next(row for row in saved['derived_plates'] if row['part_keys'] == ['3:1'])
    assert 'yaw_by_object' not in solo and 'shifts_by_object' not in solo
    # Derived-plate re-read proof, per object: the bar landed at its
    # rotated authored bounds plus its recorded shift; the square landed
    # at its authored bounds plus its shift.
    derived = Path(shared['plate'])
    shifts = shared['shifts_by_object']
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
        assert landed_square[a][0] == pytest.approx(authored_square[a][0] + shifts['2'][a],
                                                    abs=2e-3)


def test_slicing_refuses_unproved_shared_yaws(tmp_path, monkeypatch):
    import pytest

    from print_handy.core import model_job, orca_adapter
    from .test_plate_merge import _fake_slice
    source = tmp_path / 'yaw-share.3mf'
    _yaw_share_source(source)
    plates = [['1:1', '2:1'], ['3:1']]
    good_shifts = {'1': [0.0, 0.0, 0.0], '2': [0.0, 0.0, 0.0]}
    record = {'axis': '-X', 'rotation': [0, 0, 1, 0, 1, 0, -1, 0, 0], 'yaw_deg': 0.0,
              'supports': False, 'brim_recommended': False}
    bad_plans = [
        # A shared turn with no recorded shifts to realize it.
        ({'plates': plates, 'yaw_by_object': {'1': 30.0}}, 'not that part alone'),
        # A turn for an object the source does not have.
        ({'plates': plates, 'yaw_by_object': {'9': 30.0},
          'shifts_by_plate': [good_shifts, None]}, 'unknown'),
        # A non-finite turn fails closed (split validation or the recipe
        # serializer's strict JSON encoding, as for non-finite shifts).
        ({'plates': plates, 'yaw_by_object': {'1': float('inf')},
          'shifts_by_plate': [good_shifts, None]}, '(between 0 and 90|Out of range float)'),
        # One transform record per object: a turn plus a reorientation.
        ({'plates': plates, 'yaw_by_object': {'1': 30.0},
          'shifts_by_plate': [good_shifts, None],
          'reorientation_by_object': {'1': record}}, 'also records a turn'),
    ]
    for plan, match in bad_plans:
        job_path = _split_job(tmp_path, source, plan)
        monkeypatch.setattr(orca_adapter, 'slice_model', _fake_slice())
        with pytest.raises((RuntimeError, ValueError), match=match):
            model_job.slice_job(job_path)
    # Swapped shifts: each object would land somewhere its recorded turn
    # was never proved for — the exporter refuses the off-bed placement.
    plan = split_fit.split_plan_100(source, 220)
    index = next(i for i, plate in enumerate(plan['plates']) if set(plate) == {'1:1', '2:1'})
    shifts = plan['shifts_by_plate'][index]
    plan['shifts_by_plate'][index] = {'1': shifts['2'], '2': shifts['1']}
    job_path = _split_job(tmp_path, source, plan)
    monkeypatch.setattr(orca_adapter, 'slice_model', _fake_slice())
    with pytest.raises(RuntimeError, match='(does not fit|did not land)'):
        model_job.slice_job(job_path)


def test_exporter_refuses_a_per_object_fit_combined_with_a_whole_plate_fit(tmp_path):
    import pytest

    from print_handy.core import color_geometry
    source = tmp_path / 'yaw-share.3mf'
    _yaw_share_source(source)
    rotation = [0.70710678, -0.70710678, 0.0, 0.70710678, 0.70710678, 0.0, 0.0, 0.0, 1.0]
    with pytest.raises(RuntimeError, match='per-object fit cannot be combined'):
        color_geometry.export_single_color_plate(
            source, tmp_path / 'out.3mf', {}, [],
            fit={'scale': 1.0, 'rotation': rotation},
            fit_by_object={'1': {'rotation': rotation}})


# --- #221 R3: reorientation beyond solo plates ------------------------------
# The frame fixture (210 x 190 x 40, fits only stood on its -X face at
# 40 x 190, with supports) shares a plate with a 150 mm box when both are on
# the same authored plate; before R3 the frame always went solo.

def _reorientation_share_source(path):
    _solid_source(path, {'1': _frame_tris(210, 190, 40, 80, 130, 60, 140),
                         '2': _box_tris(0, 150, 0, 150, 0, 10),
                         '3': _box_tris(0, 150, 0, 150, 0, 10)}, [['1', '2'], ['3']])


def test_split_plan_reoriented_part_can_share_a_plate(tmp_path):
    source = tmp_path / 'reorientation-share.3mf'
    _reorientation_share_source(source)
    plan = split_fit.split_plan_100(source, 220)
    assert plan is not None
    assert {frozenset(plate) for plate in plan['plates']} == \
        {frozenset({'1:1', '2:1'}), frozenset({'3:1'})}
    assert plan['reorientation_by_object']['1']['axis'] == '-X'
    shifts = _shared_shifts(plan, {'1:1', '2:1'})
    assert set(shifts) == {'1', '2'}
    # The reoriented member's shift is measured from its reoriented bounds:
    # its Z shift is the recorded face change's floor (+235 for the frame's
    # authored placement), not its authored Z minimum (0).
    assert shifts['1'][2] == 235.0 and shifts['2'][2] == 0.0
    # One transform record per object: the shared member keeps its
    # reorientation record and gains no yaw or placement entry.
    assert '1' not in plan.get('yaw_by_object', {})
    assert '1' not in plan.get('placement_by_object', {})


def test_split_plan_prefers_a_shared_reorientation_over_solo_plates(tmp_path):
    # Fit ranking (#221): a proved shared plate fits more parts per plate
    # than the solo fallback, so it wins — this fixture's pre-R3 plan was
    # three plates (frame solo reoriented, both boxes solo).
    source = tmp_path / 'reorientation-share.3mf'
    _reorientation_share_source(source)
    plan = split_fit.split_plan_100(source, 220)
    assert plan is not None and len(plan['plates']) == 2


def test_split_plan_two_reoriented_parts_can_share_a_plate(tmp_path):
    # Two identical 230 x 190 x 30 boxes each fit only stood on an end
    # (30 x 190); reoriented together, their footprints pack onto one plate.
    source = tmp_path / 'reorientation-pair.3mf'
    _solid_source(source, {'1': _box_tris(0, 230, 0, 190, 0, 30),
                           '2': _box_tris(0, 230, 0, 190, 0, 30),
                           '3': _box_tris(0, 150, 0, 150, 0, 10)}, [['1', '2'], ['3']])
    plan = split_fit.split_plan_100(source, bed=220, height=250)
    assert plan is not None
    assert {frozenset(plate) for plate in plan['plates']} == \
        {frozenset({'1:1', '2:1'}), frozenset({'3:1'})}
    assert set(plan['reorientation_by_object']) == {'1', '2'}
    assert set(_shared_shifts(plan, {'1:1', '2:1'})) == {'1', '2'}


def test_split_plan_reoriented_part_falls_back_to_solo_when_sharing_cannot_pack(tmp_path):
    # A 190 mm box fills its bin too far for the frame's 40 x 190 reoriented
    # footprint to join it, and no other reoriented part can pair with it:
    # the frame keeps its R1 solo reoriented plate, with no shifts recorded.
    source = tmp_path / 'reorientation-solo.3mf'
    _solid_source(source, {'1': _frame_tris(210, 190, 40, 80, 130, 60, 140),
                           '2': _box_tris(0, 190, 0, 190, 0, 10),
                           '3': _box_tris(0, 150, 0, 150, 0, 10)}, [['1', '2'], ['3']])
    plan = split_fit.split_plan_100(source, 220)
    assert plan is not None
    assert sorted(plan['plates']) == [['1:1'], ['2:1'], ['3:1']]
    assert set(plan['reorientation_by_object']) == {'1'}
    assert 'shifts_by_plate' not in plan


def test_slicing_a_shared_reorientation_lands_at_the_recorded_rotation(tmp_path, monkeypatch):
    import json

    import pytest

    from print_handy.core import color_geometry, model_job, orca_adapter
    from print_handy.core.orca_adapter import _rotate
    source = tmp_path / 'reorientation-share.3mf'
    _reorientation_share_source(source)
    plan = split_fit.split_plan_100(source, 220)
    record = plan['reorientation_by_object']['1']
    assert record['supports'] is True
    job_path = _split_job(tmp_path, source, plan)
    fake = _fake_slice_recording_supports()
    monkeypatch.setattr(orca_adapter, 'slice_model', fake)
    paths = model_job.slice_job(job_path)
    assert len(paths) == 2 and len(fake.calls) == 2
    saved = json.loads(job_path.read_text())
    shared = next(row for row in saved['derived_plates']
                  if set(row['part_keys']) == {'1:1', '2:1'})
    assert shared['reorientation_by_object']['1']['axis'] == record['axis']
    assert shared['reorientation_by_object']['1']['rotation'] == \
        pytest.approx(record['rotation'])
    assert set(shared['plate_report']['fit_by_object']) == {'1'}
    assert shared['shifts_by_object'] == _shared_shifts(plan, {'1:1', '2:1'})
    # The frame's recorded supports decision reaches the slicer for the
    # shared plate (its only reoriented member needs supports); the other
    # plate keeps today's default.
    by_printable = {tuple(call['printable']): call['supports'] for call in fake.calls}
    assert by_printable[('1', '2')] is True and by_printable[('3',)] is None
    # Independent re-read: the frame landed at its rotated authored bounds
    # plus its recorded shift; the box at its authored bounds plus its shift.
    shifts = shared['shifts_by_object']
    with zipfile.ZipFile(source) as archive:
        authored_points = color_geometry._world_points(archive, {'1'})
        authored_box = color_geometry._world_bounds(archive, {'2'})[0]
    rotated = [_rotate(point, record['rotation']) for point in authored_points]
    rotated_min = [min(p[a] for p in rotated) for a in range(3)]
    rotated_extent = [max(p[a] for p in rotated) - rotated_min[a] for a in range(3)]
    with zipfile.ZipFile(Path(shared['plate'])) as archive:
        landed_frame = color_geometry._world_bounds(archive, {'1'})[0]
        landed_box = color_geometry._world_bounds(archive, {'2'})[0]
    for a in range(3):
        assert landed_frame[a][0] == pytest.approx(rotated_min[a] + shifts['1'][a], abs=2e-3)
        assert landed_frame[a][1] - landed_frame[a][0] == \
            pytest.approx(rotated_extent[a], abs=2e-3)
        assert landed_box[a][0] == pytest.approx(authored_box[a][0] + shifts['2'][a], abs=2e-3)


def test_slicing_refuses_unproved_shared_reorientations(tmp_path, monkeypatch):
    import pytest

    from print_handy.core import model_job, orca_adapter
    from .test_plate_merge import _fake_slice
    source = tmp_path / 'reorientation-share.3mf'
    _reorientation_share_source(source)
    plan = split_fit.split_plan_100(source, 220)
    record = plan['reorientation_by_object']['1']
    plates = [['1:1', '2:1'], ['3:1']]
    good_shifts = {'1': [0.0, 0.0, 0.0], '2': [0.0, 0.0, 0.0]}
    bad_plans = [
        # A shared reorientation with no recorded shifts to realize it.
        ({'plates': plates, 'reorientation_by_object': {'1': record}}, 'not that part alone'),
        # Shifts that do not name exactly the plate's objects.
        ({'plates': plates, 'reorientation_by_object': {'1': record},
          'shifts_by_plate': [{'1': [0.0, 0.0, 0.0]}, None]}, 'do not name exactly'),
        # A reorientation record for an unknown object, shared form.
        ({'plates': plates, 'reorientation_by_object': {'9': record},
          'shifts_by_plate': [good_shifts, None]}, 'unknown object'),
    ]
    for bad_plan, match in bad_plans:
        job_path = _split_job(tmp_path, source, bad_plan)
        monkeypatch.setattr(orca_adapter, 'slice_model', _fake_slice())
        with pytest.raises((RuntimeError, ValueError), match=match):
            model_job.slice_job(job_path)
    # Swapped shifts: each object would land somewhere its recorded
    # orientation was never proved for — refused at export or re-read.
    index = next(i for i, plate in enumerate(plan['plates']) if set(plate) == {'1:1', '2:1'})
    shifts = plan['shifts_by_plate'][index]
    plan['shifts_by_plate'][index] = {'1': shifts['2'], '2': shifts['1']}
    job_path = _split_job(tmp_path, source, plan)
    monkeypatch.setattr(orca_adapter, 'slice_model', _fake_slice())
    with pytest.raises(RuntimeError, match='(does not fit|did not land)'):
        model_job.slice_job(job_path)


def test_slicing_a_placed_reorientation_lands_at_the_recorded_placement(tmp_path, monkeypatch):
    import json

    import pytest

    from print_handy.core import model_job, orca_adapter
    source = tmp_path / 'placed-frame.3mf'
    _solid_source(source, {'1': _frame_tris(210, 190, 40, 80, 130, 60, 140),
                           '2': _box_tris(0, 150, 0, 150, 0, 10)}, [['1'], ['2']])
    plan = split_fit.split_plan_100(source, 220, exclusions=[(0, 0, 18, 28)])
    assert plan is not None and plan['placement_by_object']['1'] == [18.0, 8.0]
    record = plan['reorientation_by_object']['1']
    job_path = _split_job(tmp_path, source, plan)
    fake = _fake_slice_recording_supports()
    monkeypatch.setattr(orca_adapter, 'slice_model', fake)
    paths = model_job.slice_job(job_path)
    assert len(paths) == 2 and len(fake.calls) == 2
    saved = json.loads(job_path.read_text())
    row = next(r for r in saved['derived_plates'] if r['part_keys'] == ['1:1'])
    assert row['placement_mm'] == [18.0, 8.0] and row['reorientation']['axis'] == record['axis']
    # One fit carries both the recorded rotation and the recorded placement,
    # and the plate lands exactly there at the rotated extents.
    fit = row['plate_report']['fit']
    assert fit['rotation'] == pytest.approx(record['rotation'])
    assert fit['placement_mm'] == [18.0, 8.0]
    placed = row['plate_report']['placed_bounds_mm']
    assert placed[0][0] == 18.0 and placed[1][0] == 8.0
    assert placed[0][1] - placed[0][0] == pytest.approx(record['dims_mm'][0], abs=2e-3)
    assert placed[1][1] - placed[1][0] == pytest.approx(record['dims_mm'][1], abs=2e-3)
    # The recorded supports decision still reaches the slicer for this plate
    # only; the placed authored plate keeps today's default.
    by_printable = {tuple(call['printable']): call['supports'] for call in fake.calls}
    assert by_printable[('1',)] is True and by_printable[('2',)] is None


def test_split_plan_reoriented_part_can_share_with_a_turned_part(tmp_path):
    # A turned part that settled for a solo yaw plate can still pair with a
    # reoriented part on a fresh plate: the bar (205 x 20, fits only turned
    # 45 degrees) and the thin frame (fits only stood on its -X face,
    # 25 x 190) pack by their recorded-orientation hulls onto one plate.
    source = tmp_path / 'mixed-share.3mf'
    _solid_source(source, {'1': _box_tris(0, 205, 0, 20, 0, 10),
                           '2': _frame_tris(210, 190, 25, 80, 130, 60, 140),
                           '3': _box_tris(0, 150, 0, 150, 0, 10)}, [['1', '2'], ['3']])
    plan = split_fit.split_plan_100(source, 220)
    assert plan is not None
    assert {frozenset(plate) for plate in plan['plates']} == \
        {frozenset({'1:1', '2:1'}), frozenset({'3:1'})}
    assert plan['yaw_by_object'] == {'1': 45.0}
    assert set(plan['reorientation_by_object']) == {'2'}
    assert set(_shared_shifts(plan, {'1:1', '2:1'})) == {'1', '2'}


def test_slicing_a_mixed_turned_and_reoriented_plate(tmp_path, monkeypatch):
    import json

    from print_handy.core import model_job, orca_adapter
    source = tmp_path / 'mixed-share.3mf'
    _solid_source(source, {'1': _box_tris(0, 205, 0, 20, 0, 10),
                           '2': _frame_tris(210, 190, 25, 80, 130, 60, 140),
                           '3': _box_tris(0, 150, 0, 150, 0, 10)}, [['1', '2'], ['3']])
    plan = split_fit.split_plan_100(source, 220)
    assert plan['reorientation_by_object']['2']['supports'] is True
    job_path = _split_job(tmp_path, source, plan)
    fake = _fake_slice_recording_supports()
    monkeypatch.setattr(orca_adapter, 'slice_model', fake)
    paths = model_job.slice_job(job_path)
    assert len(paths) == 2 and len(fake.calls) == 2
    saved = json.loads(job_path.read_text())
    shared = next(row for row in saved['derived_plates']
                  if set(row['part_keys']) == {'1:1', '2:1'})
    # Both transform kinds are realized per object on the one plate, and
    # the re-read proof inside slice_job has verified both landings.
    assert shared['yaw_by_object'] == {'1': 45.0}
    assert set(shared['reorientation_by_object']) == {'2'}
    assert set(shared['plate_report']['fit_by_object']) == {'1', '2'}
    by_printable = {tuple(call['printable']): call['supports'] for call in fake.calls}
    assert by_printable[('1', '2')] is True and by_printable[('3',)] is None


def test_slicing_a_shared_reorientation_without_supports_keeps_the_plate_default(tmp_path, monkeypatch):
    # Two plain boxes fit only stood on an end (20 x 100 footprints, no
    # supports measured) and join the authored 100 mm box's plate. Their
    # records govern only themselves: with a non-reoriented member on the
    # plate, the slicer keeps today's default for the plate — a co-tenant's
    # no-supports record never takes supports away from a part that has no
    # record of its own.
    import json

    from print_handy.core import model_job, orca_adapter
    source = tmp_path / 'shared-no-supports.3mf'
    _solid_source(source, {'1': _box_tris(0, 210, 0, 100, 0, 20),
                           '2': _box_tris(0, 210, 0, 100, 0, 20),
                           '3': _box_tris(0, 100, 0, 100, 0, 10),
                           '4': _box_tris(0, 150, 0, 150, 0, 10)}, [['1', '2', '3'], ['4']])
    plan = split_fit.split_plan_100(source, 220)
    assert plan is not None
    assert {frozenset(plate) for plate in plan['plates']} == \
        {frozenset({'1:1', '2:1', '3:1'}), frozenset({'4:1'})}
    assert {oid: row['supports'] for oid, row in plan['reorientation_by_object'].items()} == \
        {'1': False, '2': False}
    job_path = _split_job(tmp_path, source, plan)
    fake = _fake_slice_recording_supports()
    monkeypatch.setattr(orca_adapter, 'slice_model', fake)
    paths = model_job.slice_job(job_path)
    assert len(paths) == 2 and len(fake.calls) == 2
    by_printable = {tuple(call['printable']): call['supports'] for call in fake.calls}
    assert by_printable[('1', '2', '3')] is None and by_printable[('4',)] is None
    saved = json.loads(job_path.read_text())
    shared = next(row for row in saved['derived_plates']
                  if set(row['part_keys']) == {'1:1', '2:1', '3:1'})
    assert set(shared['reorientation_by_object']) == {'1', '2'}
