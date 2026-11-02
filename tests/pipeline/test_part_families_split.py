"""Part families, single-plate per-part routing, tower fallback and split plans (t_07b8d012)."""
import json
import sys
from pathlib import Path

import pytest

from print_handy.core import model_job  # noqa: E402
from print_handy.core import plate_merge  # noqa: E402
from print_handy.core import prepared_intake  # noqa: E402
from .helpers import external  # noqa: E402
print_next = external("print_next")
from print_handy.core import source_colors  # noqa: E402

from print_handy import settings

ARCHIVE = settings.current()['paths']['archive']


def manifest(*names):
    return {'source_sha256': 'x' * 64, 'painted': False, 'filaments': [],
            'parts': [{'key': f'{2 * i}:{2 * i - 1}', 'object_id': str(2 * i), 'name': n, 'source_extruder': 1}
                      for i, n in enumerate(names, 1)]}


POSTIT = ('Post-It Note Holder - Staggered Tabs.stl', 'Stencil 1 (Square) - Staggered Tabs.stl',
          'Stencil 2 (Circle) - Staggered Tabs.stl', 'Stencil 3 (Circle 2 Columns) - Staggered Tabs.stl')


def test_numbered_stencils_form_one_family_and_holder_stays_alone():
    fams = source_colors.part_families(manifest(*POSTIT))
    assert [len(f['keys']) for f in fams] == [1, 3]
    assert fams[1]['label'] == 'all 3 stencils'
    assert fams[0]['names'] == ['Post-It Note Holder - Staggered Tabs']


def test_distinct_names_never_group():
    fams = source_colors.part_families(manifest('Base.stl', 'Lid.stl', 'Hinge.stl'))
    assert [len(f['keys']) for f in fams] == [1, 1, 1]


def test_identical_unnumbered_copies_group():
    fams = source_colors.part_families(manifest('Peg.stl', 'Peg.stl', 'Base.stl'))
    assert [len(f['keys']) for f in fams] == [2, 1]


def test_grouped_prompts_cover_every_canonical_key_exactly_once():
    m = manifest(*POSTIT)
    keys = [k for row in source_colors.grouped_prompts(m) for k in row['keys']]
    assert sorted(keys) == sorted(r['key'] for r in source_colors.canonical_prompts(m))
    assert len(source_colors.grouped_prompts(m)) == 2


def test_single_plate_3mf_per_part_routes_to_merge_planner():
    import inspect
    body = inspect.getsource(model_job.slice_job)
    route = body.index("if src.suffix.lower() == '.3mf':")
    assert 'return slice_multi_plate(' in body[route:route + 400]
    assert 'except ImplicitPlateSplit' in body[route:route + 600]
    # Unmergeable implicit plates and non-3MF sources fall through to the per-slot split.
    assert body.index('single_color_groups(') > route


def test_tower_reserve_is_only_a_fast_path():
    import inspect
    body = inspect.getsource(plate_merge.plan)
    assert 'pack(extents, bed, spacing, edge=edge, reserve_y=reserve)' in body
    fallback = body.index('loose = pack(extents, bed, spacing, edge=edge)')
    assert 'color_geometry.prime_tower_xy(loose_bounds, bed=bed, tower=tower_mm)' in body[fallback:]


def test_postit_merges_on_one_plate_with_tower():
    src = next(ARCHIVE.glob('holder-for-post-it-notes-stencils-included--bbb*/source/*.3mf'), None)
    if src is None:
        pytest.skip('post-it reference model not archived on this host')
    from print_handy.core import printer_state
    m = source_colors.inventory(src)
    slots = {p['key']: (2 if 'Holder' in p['name'] else 1) for p in m['parts']}
    plates = [{'id': 1, 'name': 'Plate 1', 'status': 'planned',
               'object_ids': sorted({int(p['object_id']) for p in m['parts']})}]
    verdict = plate_merge.plan(src, m, plates, slots, printer_state.load(), job={})
    assert verdict['mode'] == 'merge' and verdict['reason'] is None
    assert sorted(verdict['objects_out']) == ['2', '4', '6', '8']
    assert len(verdict['prime_tower_xy_mm']) == 2


def _split_job(tmp_path):
    job_dir = tmp_path / 'job'
    (job_dir / 'sliced').mkdir(parents=True)
    g1, g2 = job_dir / 'sliced' / 'slot1.gcode', job_dir / 'sliced' / 'slot2.gcode'
    g1.write_text('; a\n')
    g2.write_text('; b\n')
    m = manifest(*POSTIT)
    job = {'title': 'Post-it', 'gcode': str(g1),
           'plate_merge': {'mode': 'split', 'reason': 'the source says to print the stencils separately'},
           'derived_plates': [{'gcode': str(g1), 'slot': 1, 'part_keys': [p['key'] for p in m['parts'][1:]]},
                              {'gcode': str(g2), 'slot': 2, 'part_keys': [m['parts'][0]['key']]}]}
    jp = job_dir / 'JOB.json'
    jp.write_text(json.dumps(job))
    return jp, job, m, [g1, g2]


def test_split_plan_is_resolvable_by_print_next_and_states_reason(tmp_path):
    jp, job, m, gcodes = _split_job(tmp_path)
    plan = prepared_intake.write_split_plan(jp, job, m, gcodes, 'intake-abc', plans_dir=tmp_path)
    assert plan['split_reason'] == 'the source says to print the stencils separately'
    assert [p['label'] for p in plan['parts']] == [
        'Stencil 1 (Square) - Staggered Tabs, Stencil 2 (Circle) - Staggered Tabs, '
        'Stencil 3 (Circle 2 Columns) - Staggered Tabs (slot 1)',
        'Post-It Note Holder - Staggered Tabs (slot 2)']
    loaded = print_next.load_plan('intake-abc', plans_dir=tmp_path)
    nxt = print_next.resolve_next(loaded, archive=tmp_path)
    assert nxt['part_id'] == 'plate1' and Path(nxt['gcode']) == gcodes[0].resolve()
    stored = json.loads(jp.read_text())['multipart_plan']
    assert stored['plan_id'] == 'intake-abc' and len(stored['parts']) == 2


def test_print_context_carries_multipart_plan_for_split_plate(tmp_path):
    jp, job, m, gcodes = _split_job(tmp_path)
    prepared_intake.write_split_plan(jp, job, m, gcodes, 'intake-abc', plans_dir=tmp_path)
    job = json.loads(jp.read_text())
    plan = job['multipart_plan']
    second = [p for p in plan['parts'] if Path(p['gcode_path']) == gcodes[1].resolve()][0]
    assert second['id'] == 'plate2' and second['label'].endswith('(slot 2)')
    src = Path(model_job.__file__).read_text()
    assert "ctx.update(multipart_plan_id=plan['plan_id'], part_id=part['id'], required_parts=plan['parts'])" in src


def test_record_start_binds_plate_two_of_a_split_job_to_its_plan(tmp_path, monkeypatch):
    """#48: JOB.json["gcode"] is plate 1 only; plate 2 must still reach the plan and advance it."""
    print_records = external("print_records")
    jp, job, m, gcodes = _split_job(tmp_path)
    plans = tmp_path / 'Plans'
    prepared_intake.write_split_plan(jp, job, m, gcodes, 'intake-abc', plans_dir=plans)
    monkeypatch.setattr(model_job, 'ARCHIVE', tmp_path)
    monkeypatch.setattr(model_job, 'CONTEXT', tmp_path / 'context.json')
    monkeypatch.setattr(print_records, 'PLANS_DIR', plans)
    monkeypatch.setattr(print_records, 'write_start_record', lambda ctx: str(tmp_path / 'start.md'))
    model_job.record_start(gcodes[1], moonraker_name='slot2.gcode')
    ctx = json.loads((tmp_path / 'context.json').read_text())['jobs']['slot2.gcode']
    assert ctx['multipart_plan_id'] == 'intake-abc' and ctx['part_id'] == 'plate2'
    assert ctx['title'] == 'Post-it'
    plan_id, complete = print_records.update_multipart_plan(
        {**ctx, 'gcode_file': 'slot2.gcode', 'completed_at': '2026-10-19T05:00:00Z'})
    saved = json.loads((plans / 'intake-abc.json').read_text())
    status = {p['id']: p['status'] for p in saved['parts']}
    assert plan_id == 'intake-abc' and not complete
    assert status == {'plate1': 'planned', 'plate2': 'completed'}
