"""Full-size one-plate rearrangement: found first, applied without asking, sliced as planned.

Live report (2026-10-29, AD5X, a 9-part Duplo swing): the author's layout did
not fit the 220 mm bed, the fit check spent ~2.5 minutes before asking, the
question said "does not fit ... as one plate" while offering "100% · rearrange
1 plate", and the chosen rearrangement then failed to slice with per-part
colors ("part group does not fit the target bed without cropping") because
the multicolor slice path ignored the recorded plan.
"""
import asyncio
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy.core import color_geometry, model_job, plate_merge, prepared_intake, source_colors
from .test_plate_merge import STATE, _fake_slice, _prism_source, _two_slot_decisions
from .test_prepared_intake import Adapter, env  # noqa: F401  (pytest fixture)
from .test_color_intake import state

FLAG = {'full_size_rearrange_plan': {'rearrange_plate': True}}


def _rect(w, d):
    return [(0, 0), (w, 0), (w, d), (0, d)]


def _turn_needed_source(path):
    """Five parts laid out in one 1 m row: no authored-orientation merge with a
    prime tower exists on a 220 mm bed, but turning two parts by 90 degrees fits."""
    sizes = [(140, 20), (100, 40), (100, 20), (140, 40), (120, 120)]
    _prism_source(path, {str(i + 1): (_rect(w, d), 10, (i * 200, 0)) for i, (w, d) in enumerate(sizes)},
                  [[str(i + 1) for i in range(len(sizes))]])


def _plan(source, job):
    st = json.loads(STATE.read_text())
    manifest = source_colors.inventory(source)
    decisions = _two_slot_decisions(manifest)
    view = model_job._per_part_view(manifest, decisions)
    slots = model_job._part_slots(view, manifest)
    return plate_merge.plan(source, manifest, model_job.plate_manifest(source)['plates'], slots, st, job=job)


def _placed_rects(verdict):
    return {oid: (b[0][0], b[1][0], b[0][1] - b[0][0], b[1][1] - b[1][0])
            for oid, b in verdict['placed_bounds_mm'].items()}


def test_pack_turned_turns_parts_only_when_needed():
    assert plate_merge.pack_turned({'a': (100, 20), 'b': (100, 20)}, 220, 220) == {
        'a': (8.0, 8.0, False), 'b': (8.0, 38.0, False)}
    placed = plate_merge.pack_turned({'tall': (20, 200)}, 220, 100)
    assert placed == {'tall': (8.0, 8.0, True)}
    assert plate_merge.pack_turned({'huge': (300, 10)}, 220, 220) is None


def test_merge_turns_parts_only_for_a_chosen_rearrangement(tmp_path):
    source = tmp_path / 'turns.3mf'
    _turn_needed_source(source)
    assert _plan(source, {})['mode'] == 'split'
    verdict = _plan(source, FLAG)
    assert verdict['mode'] == 'merge' and verdict['packing'] == 'turned'
    assert verdict['yaw_by_object'] and set(verdict['yaw_by_object'].values()) == {90.0}
    rects = _placed_rects(verdict)
    edge, gap = plate_merge.EDGE_MARGIN_MM, plate_merge.SPACING_MM
    for x, y, w, d in rects.values():
        assert x >= edge - 1e-6 and y >= edge - 1e-6 and x + w <= 220 - edge + 1e-6 and y + d <= 220 - edge + 1e-6
    items = list(rects.values())
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            assert (a[0] + a[2] + gap <= b[0] + 1e-6 or b[0] + b[2] + gap <= a[0] + 1e-6
                    or a[1] + a[3] + gap <= b[1] + 1e-6 or b[1] + b[3] + gap <= a[1] + 1e-6)
    # Multi-slot: a prime tower spot is proved (or the reserved strip is free).
    assert verdict.get('prime_tower_xy_mm') or all(y + d <= 220 - edge - 66 + 1e-6 for _x, y, _w, d in items)


def test_full_size_one_plate_per_part_uses_the_merge_planner(tmp_path):
    source = tmp_path / 'turns.3mf'
    _turn_needed_source(source)
    st = json.loads(STATE.read_text())
    decisions = _two_slot_decisions(source_colors.inventory(source))
    plan = model_job.full_size_one_plate(source, st, decisions)
    assert plan['rearrange_plate'] is True and plan['via'] == 'merge_planner'
    assert plan['part_count'] == 5 and plan['turned_parts'] >= 1 and plan['scale'] == 1.0
    # Two 150 mm squares truly need two plates: no one-plate plan, split stays.
    two = tmp_path / 'two.3mf'
    _prism_source(two, {'1': (_rect(150, 150), 10, (0, 0)), '2': (_rect(150, 150), 10, (160, 0))}, [['1', '2']])
    assert model_job.full_size_one_plate(two, st, _two_slot_decisions(source_colors.inventory(two))) is None
    assert model_job.full_size_one_plate(two, st, None) is None


def _per_part_job(tmp_path, source):
    manifest = source_colors.inventory(source)
    decisions = _two_slot_decisions(manifest)
    job = {'downloaded_file': str(source), 'audit': {'plates': 1}, 'color_decisions': decisions,
           'scale': 1.0, 'full_size_rearrange_plan': {'id': 'rearrange_100', 'rearrange_plate': True,
                                                      'scale': 1.0, 'part_count': len(manifest['parts'])}}
    job_path = tmp_path / 'JOB.json'
    job_path.write_text(json.dumps(job))
    return job_path, manifest


def test_per_part_rearranged_job_slices_one_multicolor_plate(tmp_path, monkeypatch):
    # The live failure: per-part colors + "100% · rearrange 1 plate" must
    # slice the rearranged plate, not the author's overflowing layout.
    source = tmp_path / 'turns.3mf'
    _turn_needed_source(source)
    job_path, manifest = _per_part_job(tmp_path, source)
    calls = []

    def fake_slice(source_arg, folder, state, brim=None, first_layer=False, color_decisions=None,
                   preserve_layout=False, filament_slot=None, item_shifts=None, fit_by_object=None):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        _mapping, geometry = color_geometry.export_color_geometry(
            source_arg, folder / 'geometry.3mf', color_decisions, item_shifts=item_shifts,
            fit_by_object=fit_by_object)
        calls.append({'fit_by_object': fit_by_object, 'geometry': geometry})
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
    model_job.slice_job(job_path)
    saved = json.loads(job_path.read_text())
    assert len(calls) == 1 and calls[0]['fit_by_object']
    assert saved['plate_merge']['mode'] == 'merge'
    assert set(saved['plate_merge']['yaw_by_object'].values()) == {90.0}
    for row in calls[0]['geometry']['parts']:
        (x0, x1), (y0, y1), _z = row['bounds_mm']
        assert 0 <= x0 and x1 <= 220 and 0 <= y0 and y1 <= 220


def test_per_part_rearrangement_that_cannot_share_a_plate_packs_each_color_plate(tmp_path, monkeypatch):
    # Six 200 mm bars in a row: two colors cannot share one bed with a prime
    # tower, so slicing splits by slot. Each slot plate must be packed at 100%
    # instead of keeping the author's 1.2 m layout (the live crop failure).
    source = tmp_path / 'bars.3mf'
    _prism_source(source, {str(i): (_rect(200, 28), 10, (i * 210, 0)) for i in range(1, 7)},
                  [[str(i) for i in range(1, 7)]])
    job_path, _manifest = _per_part_job(tmp_path, source)
    assert _plan(source, FLAG)['mode'] == 'split'
    fake = _fake_slice()
    monkeypatch.setattr('print_handy.core.orca_adapter.slice_model', fake)
    model_job.slice_job(job_path)
    saved = json.loads(job_path.read_text())
    plates = saved['derived_plates']
    assert sorted(row['slot'] for row in plates) == [1, 2]
    for row in plates:
        with zipfile.ZipFile(row['plate']) as archive:
            model = archive.read('3D/3dmodel.model').decode()
            import re
            printable = re.findall(r'<item objectid="(\d+)"[^>]*printable="1"', model)
            for oid in printable:
                (x0, x1), (y0, y1), _z = color_geometry._world_bounds(archive, {oid})[0]
                assert 0 <= x0 and x1 <= 220 and 0 <= y0 and y1 <= 220


def test_without_a_rearrangement_the_per_slot_split_keeps_its_old_behavior(tmp_path, monkeypatch):
    source = tmp_path / 'bars.3mf'
    _prism_source(source, {str(i): (_rect(200, 28), 10, (i * 210, 0)) for i in range(1, 7)},
                  [[str(i) for i in range(1, 7)]])
    job_path, _manifest = _per_part_job(tmp_path, source)
    data = json.loads(job_path.read_text())
    data.pop('full_size_rearrange_plan')
    job_path.write_text(json.dumps(data))
    monkeypatch.setattr('print_handy.core.orca_adapter.slice_model', _fake_slice())
    with pytest.raises(RuntimeError, match='without cropping'):
        model_job.slice_job(job_path)


# Intake: the one-plate rearrangement is applied without a question.

def _oversized(env, monkeypatch, one_plate):
    from print_handy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st, **kw: 0)
    searched = []

    def fit_options(path, st):
        searched.append(st)
        return {'scales': [0.8], 'rotations': []}
    monkeypatch.setattr(orca_adapter, 'fit_options', fit_options)
    monkeypatch.setattr(model_job, 'per_part_fit_options', lambda path, decisions, options, bed: options)
    checked = []

    def full_size_one_plate(path, st, decisions):
        checked.append(decisions)
        return one_plate
    monkeypatch.setattr(model_job, 'full_size_one_plate', full_size_one_plate)
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='rearrange', text='https://www.printables.com/model/1-ghost')
    adapter = Adapter([0, 0, 0, 0, 0, 0])
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    return adapter, searched, checked


def test_one_plate_rearrangement_is_applied_without_asking(env, monkeypatch):
    plan = {'id': 'rearrange_100', 'scale': 1.0, 'rearrange_plate': True, 'part_count': 9, 'turned_parts': 2,
            'size_mm': [207.55, 204.0, 16.6], 'via': 'merge_planner'}
    adapter, searched, checked = _oversized(env, monkeypatch, plan)
    assert len(checked) == 1
    assert searched == []  # no rotation/shrink/split search after a full-size one-plate fit
    assert not any('does not fit' in q['question'] for q in adapter.questions)
    notice = [text for text in adapter.sent if 'onto one plate at 100%' in text]
    assert len(notice) == 1
    assert notice[0].startswith('Ghost Stand does not fit the ')
    assert notice[0].endswith(' bed as the author laid it out, so I rotated and rearranged the 9 parts '
                              'onto one plate at 100%.')
    job = json.loads(next(env.root.parent.glob('archive/*/JOB.json')).read_text())
    assert job['full_size_rearrange_plan'] == plan and job['scale'] == 1.0
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert receipt['fit_auto_rearranged'] is True


def test_without_a_one_plate_rearrangement_the_fit_question_is_still_asked(env, monkeypatch):
    adapter, searched, checked = _oversized(env, monkeypatch, None)
    assert len(checked) == 1 and len(searched) == 1
    assert searched[0].get('fit_rearrange_checked') is True  # the probe is not repeated
    question = next(q for q in adapter.questions if 'does not fit' in q['question'])
    assert 'as the author laid it out' in question['question']
    assert not any('onto one plate at 100%' in text for text in adapter.sent)


def test_an_explicit_size_is_never_overridden_by_auto_rearrange():
    assert prepared_intake._auto_rearrange_eligible({}, {}) is True
    assert prepared_intake._auto_rearrange_eligible({}, {'scale': 0.8}) is False
    assert prepared_intake._auto_rearrange_eligible({}, {'orientation': {'rotation': [1] * 9}}) is False
    assert prepared_intake._auto_rearrange_eligible({'unit_decision': {'units': 'inch'}}, {}) is False


def test_notice_distinguishes_author_layout_from_rearranged():
    text = prepared_intake.auto_rearrange_notice('Swing', {'part_count': 9, 'turned_parts': 0}, printer='AD5X')
    assert text == 'Swing does not fit the AD5X bed as the author laid it out, so I rearranged the 9 parts onto one plate at 100%.'
    turned = prepared_intake.auto_rearrange_notice('Swing', {'part_count': 9, 'turned_parts': 3}, printer='AD5X')
    assert 'rotated and rearranged the 9 parts' in turned
    question = prepared_intake.fit_question('Swing', {'scales': [0.8], 'rearrange_100': {'part_count': 9}})
    assert 'as the author laid it out' in question and 'Rearranged: all 9 parts fit one plate at full size.' in question


# Progress: one informative status line, edited in place when the adapter can.

class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class _EditingAdapter:
    def __init__(self, can_edit=True):
        self.sent, self.edits = [], []
        if not can_edit:
            self.edit_message = None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return S(success=True, message_id=str(len(self.sent)))

    async def edit_message(self, chat_id, message_id, content):
        self.edits.append((chat_id, message_id, content))
        return S(success=True, message_id=message_id)


def _run_phases(adapter, monkeypatch, durations):
    clock = _Clock()
    monkeypatch.setattr(prepared_intake, 'FIT_HEARTBEAT_S', 0.01)
    status = prepared_intake.FitStatus(adapter, S(chat_id='c1', thread_id='t9'), {'thread_id': 't9'}, clock=clock)

    async def work(seconds):
        # Each heartbeat tick advances the fake clock by 10 s of "work".
        for _ in range(seconds // 10):
            await asyncio.sleep(0.02)
            clock.now += 10
        return seconds

    async def main():
        await status.run('checking the layout as the author laid it out', work(0),
                         first_text='Checking whether it fits the bed.')
        for phase, seconds in durations:
            await status.run(phase, work(seconds))
        await status.finish('choose how to print it.')
    asyncio.run(main())
    return clock


def test_progress_edits_one_message_with_phase_and_elapsed_time(monkeypatch):
    adapter = _EditingAdapter()
    _run_phases(adapter, monkeypatch, [('trying rotations and smaller sizes', 40),
                                       ('trying a split onto separate plates at 100%', 30)])
    assert adapter.sent == ['Checking whether it fits the bed.']
    texts = [content for _chat, _mid, content in adapter.edits]
    assert all(chat == 't9' and mid == '1' for chat, mid, _ in adapter.edits)
    assert texts[0] == 'Checking the fit: trying rotations and smaller sizes (0 s).'
    assert any(t.startswith('Still checking the fit: trying rotations and smaller sizes (') for t in texts)
    assert any('trying a split onto separate plates at 100%' in t for t in texts)
    assert texts[-1] == 'Fit check done in 1 min 10 s: choose how to print it.'
    assert len(texts) == len(set(texts))  # never the same text twice in a row or at all


def test_progress_without_edits_posts_phase_changes_and_rare_heartbeats(monkeypatch):
    adapter = _EditingAdapter(can_edit=False)
    _run_phases(adapter, monkeypatch, [('trying rotations and smaller sizes', 130)])
    assert adapter.sent[0] == 'Checking whether it fits the bed.'
    assert adapter.sent[1] == 'Checking the fit: trying rotations and smaller sizes (0 s).'
    heartbeats = [t for t in adapter.sent if t.startswith('Still checking')]
    # 130 s of work with 10 s ticks: at most one post per FIT_FALLBACK_HEARTBEAT_S (60 s).
    assert 1 <= len(heartbeats) <= 2
    assert adapter.sent[-1] == 'Fit check done in 2 min 10 s: choose how to print it.'
    assert len(adapter.sent) == len(set(adapter.sent))


def test_progress_falls_back_to_posting_when_an_edit_fails(monkeypatch):
    adapter = _EditingAdapter()

    async def refuse(chat_id, message_id, content):
        adapter.edits.append((chat_id, message_id, content))
        return S(success=False, error='Not supported')
    adapter.edit_message = refuse
    _run_phases(adapter, monkeypatch, [('trying rotations and smaller sizes', 0)])
    assert adapter.sent == ['Checking whether it fits the bed.',
                            'Checking the fit: trying rotations and smaller sizes (0 s).',
                            'Fit check done in 0 s: choose how to print it.']
    assert len(adapter.edits) == 1  # one refused edit, then plain posts


def test_duplo_shaped_per_part_job_is_rearranged_without_asking_and_slices(env, monkeypatch):
    # End to end for the live case: per-part colors over two slots, the
    # author's layout overflows the bed, every part fits one plate at 100%
    # once some are turned. No fit question, one notice line, and the real
    # slice path (only Orca faked) exports one multicolor plate in the bed.
    from print_handy.core import orca_adapter
    source = env.tmp / 'turns.3mf'
    _turn_needed_source(source)
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy_to(source, dest))
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st, **kw: 0)
    monkeypatch.setattr(orca_adapter, 'fit_options',
                        lambda path, st: pytest.fail('the option search ran after a one-plate fit'))
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='duplo-shaped', text='https://www.printables.com/model/1-ghost')
    # Every part individually, alternating White / Black, then Slice only and Cancel.
    adapter = Adapter([2, 0, 1, 0, 1, 0, 1, 1, 1, 1])
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    assert not any('does not fit' in q['question'] for q in adapter.questions)
    assert sum('onto one plate at 100%' in text for text in adapter.sent) == 1
    job_path = next(env.root.parent.glob('archive/*/JOB.json'))
    job = json.loads(job_path.read_text())
    assert job['color_decisions']['mode'] == 'per_part'
    assert len({c['slot'] for k, c in job['color_decisions']['choices'].items() if k.startswith('part:')}) == 2
    assert job['full_size_rearrange_plan']['via'] == 'merge_planner'
    # Now slice the saved job through the real slice path; only Orca is faked.
    st = json.loads(STATE.read_text())
    # The slots the intake state offered, with the hardware fixture's verified fields.
    st['filament_slots'].update({k: {**st['filament_slots'].get(k, {}), **v} for k, v in state()['filament_slots'].items()})
    monkeypatch.setattr(model_job.printer_state, 'load', lambda *a, **k: st)
    exported = []

    def fake_slice_model(source_arg, folder, state_, brim=None, first_layer=False, color_decisions=None,
                         preserve_layout=False, filament_slot=None, item_shifts=None, fit_by_object=None):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        _mapping, geometry = color_geometry.export_color_geometry(
            source_arg, folder / 'geometry.3mf', color_decisions, item_shifts=item_shifts,
            fit_by_object=fit_by_object)
        exported.append({'fit_by_object': fit_by_object, 'geometry': geometry})
        raise RuntimeError('stop after the multicolor export')
    monkeypatch.setattr('print_handy.core.orca_adapter.slice_model', fake_slice_model)
    data = json.loads(job_path.read_text())
    data.pop('derived_plates', None)
    job_path.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match='stop after the multicolor export'):
        _real_slice_job(job_path)
    assert len(exported) == 1 and exported[0]['fit_by_object']
    for row in exported[0]['geometry']['parts']:
        (x0, x1), (y0, y1), _z = row['bounds_mm']
        assert 0 <= x0 and x1 <= 220 and 0 <= y0 and y1 <= 220


_real_slice_job = model_job.slice_job


def _copy_to(src, dest):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / 'ghost.3mf'
    out.write_bytes(Path(src).read_bytes())
    return out


def test_pruned_hull_checks_match_the_exact_distance():
    # The polygon packer's fast checks must give the exact verdicts.
    import math
    import random
    from print_handy.core.orca_adapter import _hull
    rng = random.Random(7)
    for _ in range(300):
        a = _hull([(rng.uniform(0, 60), rng.uniform(0, 60)) for _ in range(rng.randint(3, 80))])
        dx, dy = rng.uniform(-40, 90), rng.uniform(-40, 90)
        b = _hull([(rng.uniform(0, 50) + dx, rng.uniform(0, 50) + dy) for _ in range(rng.randint(3, 80))])
        exact = plate_merge.hull_distance(a, b)
        for limit in (0.5, 3.0, 10.0):
            assert plate_merge._closer_than(a, b, limit) == (exact < limit)
        assert plate_merge._aabb_gap(a, b) <= exact + 1e-9
    # The outer K-DOP contains the hull and keeps its exact AABB.
    pts = [(50 + 40 * math.cos(t / 100), 30 + 20 * math.sin(t / 100)) for t in range(628)]
    hull = _hull(pts)
    dop = plate_merge._outer_polygon(hull)
    assert len(dop) == plate_merge.OUTER_DOP_K
    for axis in (0, 1):
        assert min(p[axis] for p in dop) == pytest.approx(min(p[axis] for p in hull))
        assert max(p[axis] for p in dop) == pytest.approx(max(p[axis] for p in hull))
    assert plate_merge._polygon_area(dop) >= plate_merge._polygon_area(hull)
    assert plate_merge._polygon_area(dop) <= plate_merge._polygon_area(hull) * 1.02
    assert all(plate_merge._polygons_intersect([p, p, p], dop) for p in hull)


# The shipped default is the "minimum" spacing preset (the suites pin "medium"
# for their older fixtures, see tests/conftest.py). These tests apply the
# minimum values the way settings resolution does.

def _use_spacing(monkeypatch, preset):
    from print_handy import settings
    values = settings.SPACING_PRESETS[preset]
    monkeypatch.setattr(plate_merge, 'SPACING_MM', values[('layout', 'merge_spacing_mm')])
    monkeypatch.setattr(plate_merge, 'EDGE_MARGIN_MM', values[('layout', 'merge_edge_margin_mm')])
    monkeypatch.setattr(plate_merge, 'TOWER_CLEARANCE_MM', values[('prime_tower', 'part_clearance_mm')])
    monkeypatch.setattr(plate_merge, 'TOWER_SLACK_MM', values[('prime_tower', 'merge_slack_mm')])
    monkeypatch.setattr(color_geometry, 'TOWER_MARGIN_MM', values[('prime_tower', 'part_clearance_mm')])
    monkeypatch.setattr(color_geometry, 'TOWER_EDGE_MM', values[('prime_tower', 'edge_mm')])


# Footprints measured from the live model (MakerWorld "Duplo swing - Two seats"),
# as rectangles; laid out by the author in one ~1.2 m row here.
DUPLO_PARTS = [('frameLeg', 95.4, 103.6), ('frameLeg', 95.4, 103.6), ('seat-brick', 61.8, 31.8),
               ('chain', 15.0, 87.73), ('chain', 15.0, 87.73), ('chain', 15.0, 87.73), ('chain', 15.0, 87.73),
               ('seat-brick', 61.8, 31.8), ('double-frame', 162.0, 12.15)]


def _duplo_source(path):
    x, shapes = 0.0, {}
    for i, (_name, w, d) in enumerate(DUPLO_PARTS, 1):
        shapes[str(i)] = (_rect(w, d), 16.6, (x, 0))
        x += w + 10
    _prism_source(path, shapes, [[str(i) for i in range(1, len(DUPLO_PARTS) + 1)]])
    return path


def _slots_by_name(manifest, rule):
    st = json.loads(STATE.read_text())
    sel = {s: model_job.resolve_filament_selection(st, dict(st['filament_slots'][str(s)], slot=s)) for s in (1, 2, 4)}
    name_of = {str(i): name for i, (name, _w, _d) in enumerate(DUPLO_PARTS, 1)}
    choices = {}
    for part in manifest['parts']:
        choices['part:' + part['key']] = sel[rule(name_of[str(part['object_id'])])]
    for row in source_colors.canonical_prompts(manifest):
        choices.setdefault(row['key'], choices.get(row['key']) or sel[1])
    return {'schema': 'part_slots_v1', 'mode': 'per_part', 'source_sha256': manifest['source_sha256'],
            'choices': choices}


TWO_COLORS = lambda name: 4 if name == 'double-frame' else 1  # the live attempt
THREE_COLORS = lambda name: 4 if name.startswith('seat') else 2 if name == 'chain' else 1


@pytest.mark.parametrize('rule', [TWO_COLORS, THREE_COLORS], ids=['two-colors', 'three-colors'])
def test_duplo_shape_is_one_multicolor_plate_at_the_minimum_preset(tmp_path, monkeypatch, rule):
    # Both live forms: white + one green part (the cropping error) and three
    # color groups (silently three per-slot plates). At the minimum preset the
    # nine parts and a prime tower fit one bed, so the per-part job is offered,
    # and slices, as one merged multicolor plate.
    _use_spacing(monkeypatch, 'minimum')
    source = _duplo_source(tmp_path / 'duplo.3mf')
    st = json.loads(STATE.read_text())
    decisions = _slots_by_name(source_colors.inventory(source), rule)
    plan = model_job.full_size_one_plate(source, st, decisions)
    assert plan and plan['via'] == 'merge_planner' and plan['part_count'] == 9
    manifest = source_colors.inventory(source)
    view = model_job._per_part_view(manifest, decisions)
    verdict = plate_merge.plan(source, manifest, model_job.plate_manifest(source)['plates'],
                               model_job._part_slots(view, manifest), st, job=FLAG)
    assert verdict['mode'] == 'merge'
    tower = verdict.get('prime_tower_xy_mm')
    bounds = list(verdict['placed_bounds_mm'].values())
    if tower:
        size = plate_merge.TOWER_MM
        clear = plate_merge.TOWER_CLEARANCE_MM
        for (x0, x1), (y0, y1), _z in bounds:
            assert (tower[0] >= x1 + clear - 1e-6 or tower[0] + size <= x0 - clear + 1e-6
                    or tower[1] >= y1 + clear - 1e-6 or tower[1] + size <= y0 - clear + 1e-6)


@pytest.mark.parametrize('rule', [TWO_COLORS, THREE_COLORS], ids=['two-colors', 'three-colors'])
def test_duplo_shape_at_the_standard_preset_is_not_offered_as_one_plate(tmp_path, rule):
    # The suites run at "medium" (Standard): 10 mm gaps, 8 mm edges and the
    # tower clearances leave no room, so no one-plate multicolor offer exists.
    source = _duplo_source(tmp_path / 'duplo.3mf')
    st = json.loads(STATE.read_text())
    decisions = _slots_by_name(source_colors.inventory(source), rule)
    assert model_job.full_size_one_plate(source, st, decisions) is None


def test_merge_and_rearrange_packing_keep_the_brim_on_the_bed():
    # With layout.spacing minimum the merge edge margin is 1 mm; a 5 mm brim
    # on a part packed there crosses the bed edge. The merge planner and the
    # per-slot rearranger now pack with the edge margin plus the brim, like
    # the single-plate arranger already did.
    from print_handy.core import model_job, plate_merge
    assert model_job._merge_edge(None) == plate_merge.EDGE_MARGIN_MM
    assert model_job._merge_edge(5) == plate_merge.EDGE_MARGIN_MM + 5
    edge = plate_merge.EDGE_MARGIN_MM + 5
    placed = plate_merge.pack({'a': (100.0, 100.0), 'b': (60.0, 60.0)}, 220, 2.0, edge=edge)
    assert placed is not None
    for key, (x, y) in placed.items():
        w = {'a': 100.0, 'b': 60.0}[key]
        assert x >= edge and y >= edge and x + w <= 220 - edge and y + w <= 220 - edge
    import inspect
    body = inspect.getsource(plate_merge.plan)
    # Every packer the planner tries receives the edge.
    for call in ('pack(extents, bed, spacing, edge=edge', '_turned_attempt(hulls_all, float(bed), spacing, reserve, edge=edge)',
                 'pack_polygon(hulls, bed, spacing, edge=edge', '_yawed_merge_attempt(source, object_ids, extents, bed, spacing, reserve, edge=edge)'):
        assert call in body, call


def test_prefetched_fit_check_already_done_posts_nothing(monkeypatch):
    # #734: the author-layout probe starts while the color question is open; when the
    # operator answers after it finished, the fit check has nothing to wait for.
    adapter = _EditingAdapter()
    status = prepared_intake.FitStatus(adapter, S(chat_id='c1', thread_id='t9'), {'thread_id': 't9'},
                                       clock=_Clock())

    async def probe():
        return 3

    async def main():
        task = prepared_intake._prefetch(probe())
        await asyncio.sleep(0)  # the operator is still answering; the probe completes
        await asyncio.sleep(0)
        got = await status.run('checking the layout as the author laid it out', task,
                               first_text='Checking whether it fits the bed.')
        await status.finish('fits.')
        return got
    assert asyncio.run(main()) == 3
    assert adapter.sent == [] and adapter.edits == []


def test_prefetched_fit_check_still_running_shows_the_usual_status(monkeypatch):
    adapter = _EditingAdapter()
    status = prepared_intake.FitStatus(adapter, S(chat_id='c1', thread_id='t9'), {'thread_id': 't9'},
                                       clock=_Clock())

    async def probe():
        await asyncio.sleep(0.05)
        return 1

    async def main():
        task = prepared_intake._prefetch(probe())
        return await status.run('checking the layout as the author laid it out', task,
                                first_text='Checking whether it fits the bed.')
    assert asyncio.run(main()) == 1
    assert adapter.sent == ['Checking whether it fits the bed.']


def test_prefetched_fit_check_failure_surfaces_when_awaited():
    async def probe():
        raise RuntimeError('orca probe failed')

    async def main():
        task = prepared_intake._prefetch(probe())
        await asyncio.sleep(0)
        status = prepared_intake.FitStatus(_EditingAdapter(), S(chat_id='c1', thread_id=None), {})
        with pytest.raises(RuntimeError, match='orca probe failed'):
            await status.run('checking the layout as the author laid it out', task)
    asyncio.run(main())
