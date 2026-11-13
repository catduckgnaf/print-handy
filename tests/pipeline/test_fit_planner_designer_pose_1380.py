"""The fit planner prefers the designer's pose for multi-plate projects (#1380).

A full-size split may stand a designer-placed part on another face, often onto
supports. Since #1380:
- a reoriented pose that needs no supports beats one that does;
- each reoriented part records why it was tipped, and the question shows it;
- the plan carries an as-designed alternative: the same split at the largest
  uniform size where no part is reoriented, offered beside the full-size split
  and sliced through the whole-plate fit at that size;
- the question says plainly what full size costs and what the smaller size keeps.
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy.core import model_job, orca_adapter, prepared_intake, split_fit
from print_handy.intake.fit_flow import _fit_choice_reply, fit_choice_rows

from .test_plate_merge import _fake_slice
from .test_prepared_intake import Adapter, _copy, env, state  # noqa: F401  (env is a fixture)
from .test_split_fit import _box_tris, _solid_source, _split_job


class _AsDesigned(Adapter):
    """The scripted picks, except the fit question, which takes the as-designed split."""

    async def send_clarify(self, **kw):
        if 'does not fit the' in kw.get('question', ''):
            choices = kw['choices']
            self.picks = iter([choices.index(next(c for c in choices if c.endswith('as designed')))]
                              + list(self.picks)[1:])
        return await super().send_clarify(**kw)


def _stand_and_base(path):
    # The #76 Stand (130 x 230 x 30) fits no bed turn lying as designed (230 mm over the
    # 204 mm usable bed); full size stands it on its end at 45 degrees. The base fits as is.
    _solid_source(path, {'1': _box_tris(0, 130, 0, 230, 0, 30),
                         '2': _box_tris(0, 150, 0, 150, 0, 10)}, [['1'], ['2']])
    return path


def _candidate(axis, yaw, supports, margin):
    return {'axis': axis, 'yaw_deg': yaw, 'supports': supports, 'margin_mm': margin,
            'bed_contact_mm2': 100.0}


def test_a_pose_without_supports_beats_one_with_them_whatever_the_margin():
    braced = _candidate('+Y', 0.0, True, 40.0)       # cardinal, most margin, needs supports
    clean = _candidate('+X', 45.0, False, 2.0)       # diagonal, little margin, no supports
    ranked = split_fit._ranked_reorientation_candidates([braced, clean])
    assert ranked[0] is clean
    # Within one supports class the cardinal-first order is unchanged.
    cardinal = _candidate('-X', 90.0, False, 1.0)
    assert split_fit._ranked_reorientation_candidates([clean, cardinal])[0] is cardinal


def test_the_plan_records_why_each_part_was_tipped_and_an_as_designed_split(tmp_path):
    plan = split_fit.split_plan_100(_stand_and_base(tmp_path / 'stand.3mf'), 220)
    assert set(plan['reorientation_by_object']) == {'1'}
    assert plan['turn_reason_by_object']['1'].startswith('too big for the bed lying as designed')
    designed = plan['designer_pose']
    # 230 mm against the usable 204 mm: the largest as-designed size, floored to 0.1%.
    rows = {row['object_id']: row for row in split_fit.per_part_fit(tmp_path / 'stand.3mf', bed=(220, 220))}
    assert designed['scale'] == rows['1']['designer_pose_scale']
    assert 0.85 < designed['scale'] < 0.9
    assert sorted(designed['plates']) == [['1:1'], ['2:1']]
    assert 'reorientation_by_object' not in designed


def test_a_too_tall_part_says_so():
    reason = split_fit.turn_reason([50, 50, 240], 220, (204, 204), (50, 50))
    assert reason == 'too tall to stand as designed (240 mm, the printer takes 220 mm)'
    assert split_fit.designer_pose_scale([50, 50, 240], 0.25, 220) == 0.916


def test_no_as_designed_split_below_half_size(tmp_path):
    path = tmp_path / 'long.3mf'
    _solid_source(path, {'1': _box_tris(0, 30, 0, 500, 0, 30),
                         '2': _box_tris(0, 150, 0, 150, 0, 10)}, [['1'], ['2']])
    plan = split_fit.split_plan_100(path, 220)
    if plan is not None:
        assert 'designer_pose' not in plan


def test_the_buttons_offer_both_and_typed_words_pick_the_right_one(tmp_path):
    plan = split_fit.split_plan_100(_stand_and_base(tmp_path / 'stand.3mf'), 220)
    rows = fit_choice_rows({'scales': [0.8], 'rotations': [], 'split_plan': plan})
    labels = [row['label'] for row in rows]
    pct = f"{round(plan['designer_pose']['scale'] * 100, 1):g}%"
    assert labels[:2] == ['100% · split 2 plates', f'{pct} · split 2 plates as designed']
    assert labels[-1] == 'Cancel'
    assert _fit_choice_reply('split', rows) == 0
    assert _fit_choice_reply('as designed', rows) == 1
    assert _fit_choice_reply('100%', rows) == 0
    assert _fit_choice_reply('shrink', rows) == 1                 # the larger of the smaller sizes
    text = prepared_intake.designer_pose_text(plan, rows)
    assert text == (' Full size stands 1 part on another face, because it does not fit the bed '
                    f'the way the designer laid it out. At {pct}, every part prints as designed, on 2 plates.')
    # One reoriented object can hold several listed parts: the count follows the parts listed.
    assert prepared_intake.designer_pose_text(plan, rows, parts_count=2).startswith(
        ' Full size stands 2 parts on another face, because they do not fit the bed '
        'the way the designer laid them out.')


def test_without_an_as_designed_split_the_text_points_at_the_shrink():
    plan = {'plates': [['1:1'], ['2:1']],
            'reorientation_by_object': {'1': {'supports': True}, '2': {'supports': False}}}
    rows = [{'kind': 'split', 'scale': 1.0}, {'kind': 'scale', 'scale': 0.74}]
    assert prepared_intake.designer_pose_text(plan, rows) == (
        ' Full size stands 2 parts on another face with supports, because they do not fit the bed '
        "the way the designer laid them out. Shrinking to 74% keeps the designer's orientation.")
    assert prepared_intake.designer_pose_text({'plates': [['1:1']]}, rows) == ''


def test_slicing_the_as_designed_split_scales_every_plate_and_reorients_nothing(tmp_path, monkeypatch):
    source = _stand_and_base(tmp_path / 'stand.3mf')
    designed = split_fit.split_plan_100(source, 220)['designer_pose']
    job_path = _split_job(tmp_path, source, designed)
    fake = _fake_slice()
    monkeypatch.setattr(orca_adapter, 'slice_model', fake)
    paths = model_job.slice_job(job_path)
    assert len(paths) == 2
    saved = json.loads(job_path.read_text())
    for row in saved['derived_plates']:
        assert 'reorientation' not in row
        assert row['plate_report']['fit']['scale'] == designed['scale']
        placed = row['plate_report']['placed_bounds_mm']
        assert all(0 <= low and high <= 220 for low, high in placed)
    stand = next(row for row in saved['derived_plates'] if row['part_keys'] == ['1:1'])
    # Lying as designed: its 130 x 230 mm face down, scaled, never stood on its end (30 mm wide).
    footprint = sorted(high - low for low, high in stand['plate_report']['placed_bounds_mm'])
    assert footprint == pytest.approx([130 * designed['scale'], 230 * designed['scale']], abs=0.05)
    assert saved['plate_merge']['full_size'] is False and saved['plate_merge']['scale'] == designed['scale']


def test_slicing_refuses_a_scaled_plan_that_still_reorients(tmp_path, monkeypatch):
    source = _stand_and_base(tmp_path / 'stand.3mf')
    plan = dict(split_fit.split_plan_100(source, 220))
    plan.pop('designer_pose')
    plan['scale'] = 0.9
    job_path = _split_job(tmp_path, source, plan)
    monkeypatch.setattr(orca_adapter, 'slice_model', _fake_slice())
    with pytest.raises(RuntimeError, match='scaled split plan may not carry reorientations'):
        model_job.slice_job(job_path)


def test_the_intake_offers_the_as_designed_split_and_records_it(env, monkeypatch):
    path = _stand_and_base(env.tmp / 'stand-and-base.3mf')
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy(path, dest))
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity',
                        lambda path, st, scale=1.0, copy_limit=16, **kw: 0 if scale == 1.0 else 1)
    monkeypatch.setattr(orca_adapter, 'fit_options',
                        lambda path, st: {'scales': [0.98, 0.75, 0.5], 'rotations': []})

    def fake_split_slice(job_path, copies=None, recipe_sha256=None):
        job_path = Path(job_path)
        data = json.loads(job_path.read_text())
        gcodes, rows = [], []
        for index, keys in enumerate(data['full_size_split_plan']['plates'], 1):
            gcode = job_path.parent / 'sliced' / f'plate_{index}.gcode'
            gcode.parent.mkdir(parents=True, exist_ok=True)
            gcode.write_text(f'; plate {index}\nG1 X1\n')
            gcodes.append(gcode)
            rows.append({'gcode': str(gcode), 'plate_id': index, 'slot': 1, 'part_keys': keys})
        data['derived_plates'] = rows
        model_job.write_job(job_path.parent, data)
        env.sliced.extend(map(str, gcodes))
        return gcodes

    monkeypatch.setattr(model_job, 'slice_job', fake_split_slice)
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='as-designed', text='https://www.printables.com/model/1-ghost')
    adapter = _AsDesigned([1, 0, 0, 1, 2])  # the fit question picks the as-designed split
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    prompt = next(q for q in adapter.questions if 'does not fit the' in q['question'])
    assert prompt['choices'][1].endswith('split 2 plates as designed')
    assert 'Full size stands 1 part on another face' in prompt['question']
    assert 'every part prints as designed, on 2 plates.' in prompt['question']
    assert '; too big for the bed lying as designed' in prompt['question']
    job = json.loads(next(env.root.parent.glob('archive/*/JOB.json')).read_text())
    plan = job['full_size_split_plan']
    assert 'reorientation_by_object' not in plan and 0.85 < plan['scale'] < 0.9
    assert job['scale'] == plan['scale'] and job['plate_mode'] == 'separate'
