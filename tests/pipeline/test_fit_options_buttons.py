"""Fit options below 100%: offered as buttons, honest hint, measured size, brim only when needed.

Live report (2.2.0, AD5X, Printables "Desk organizer", one 231 x 157 x 135 mm
STL): the question described "Turn 126 degrees fits at 96.3%" but offered only
"Shrink to 91.6%" and Cancel, listed "split" in its hint with nothing to split,
said "measured dimensions unavailable", and spent 49 s, 30 of them rearranging
and splitting a single solid body. A tight-fit 3 mm brim, chosen for a wide
flat base that never needed one, also took 6 mm off the usable bed.
"""
import asyncio
import json
import struct
from types import SimpleNamespace as S

import pytest

from print_handy.core import model_job, orca_adapter as oa, prepared_intake as pi
from .test_color_intake import state
from .test_prepared_intake import Adapter, env  # noqa: F401  (pytest fixture)

TURN = {'name': 'Turn 126° on the bed', 'axis': '+Z', 'scale': 0.963, 'supports': False, 'brim': False,
        'rotation': [-0.588, -0.809, 0, 0.809, -0.588, 0, 0, 0, 1], 'dims_mm': [213.8, 213.9, 130.0],
        'dims_full_mm': [222.0, 222.1, 135.0], 'label': 'Turn 126° on the bed, 96.3%'}
END = {'name': 'Rotate 90° on Y', 'axis': '-X', 'scale': 0.951, 'supports': True, 'brim': True,
       'rotation': [0, -0.12, 0.99, 0, 0.99, 0.12, -1, 0, 0], 'dims_mm': [133.1, 149.1, 220.0],
       'dims_full_mm': [140.0, 156.8, 231.3], 'label': 'Rotate 90° on Y, 95.1% (needs supports + brim)'}
DESK = {'scales': [0.916, 0.75, 0.5], 'rotations': [TURN, END], 'extent_mm': [231.31, 157.01, 135.0],
        'part_name': 'Desk organizer', 'brim_policy': {'type': 'no_brim', 'width_mm': 0.0, 'label': 'No brim'}}


def test_the_better_rotation_is_a_button_and_the_dominated_shrink_is_not():
    rows = pi.fit_choice_rows(DESK)
    assert [row['label'] for row in rows] == ['96.3% · turned 126°', 'Cancel']
    assert rows[0]['kind'] == 'rotate' and rows[0]['scale'] == 0.963 and rows[0]['rotation'] == TURN['rotation']
    assert pi._fit_choice_reply('96.3%', rows) == 0
    assert pi._fit_choice_reply('shrink', rows) == 0  # "shrink" means the largest smaller size on offer
    assert pi._fit_choice_reply('cancel', rows) == 1


def test_shrink_stays_when_no_rotation_beats_it_and_supports_need_to_win():
    low_turn = dict(TURN, scale=0.9)
    rows = pi.fit_choice_rows({**DESK, 'rotations': [low_turn, END]})
    # 91.6% shrink beats the 90% turn; the supports rotation (95.1%) beats both.
    assert [row['label'] for row in rows] == [
        '95.1% · rotated 90° on Y + supports', 'Shrink to 91.6%', '90% · turned 126°', 'Cancel']
    assert [row['scale'] for row in rows[:-1]] == sorted((row['scale'] for row in rows[:-1]), reverse=True)
    assert len(rows) <= 4
    # A supports rotation smaller than a no-support one is not worth a button.
    rows = pi.fit_choice_rows({**DESK, 'rotations': [TURN, dict(END, scale=0.9)], 'scales': [0.8]})
    assert [row['label'] for row in rows] == ['96.3% · turned 126°', 'Cancel']


def test_full_size_rows_keep_their_place_ahead_of_smaller_sizes():
    rows = pi.fit_choice_rows({**DESK, 'full_size_split': True})
    assert [row['label'] for row in rows] == ['Split plates at 100%', '96.3% · turned 126°', 'Cancel']
    assert pi._fit_choice_reply('split', rows) == 0


def test_hint_lists_only_offered_actions():
    rows = pi.fit_choice_rows(DESK)
    assert pi.fit_hint(rows) == 'Pick a fit option, or type shrink / cancel.'
    with_split = pi.fit_choice_rows({**DESK, 'full_size_split': True})
    assert pi.fit_hint(with_split) == 'Pick a fit option, or type split / 100% / shrink / cancel.'
    assert pi.fit_hint([{'kind': 'cancel', 'label': 'Cancel'}]) == 'Pick a fit option, or type cancel.'
    text = pi.fit_question('Desk organizer', DESK)
    assert text.endswith('Pick a fit option, or type shrink / cancel.')
    assert 'split' not in text.splitlines()[-1] and '100%' not in text.splitlines()[-1]


def test_question_shows_measured_size_and_hides_a_brim_that_is_not_in_play():
    text = pi.fit_question('Desk organizer', DESK)
    assert text.startswith('Desk organizer (231.31 × 157.01 × 135 mm) does not fit')
    assert 'measured dimensions unavailable' not in text
    assert 'Rotated: Turn 126° on the bed fits at 96.3% (213.8 x 213.9 x 130 mm)' in text
    assert 'brim policy' not in text
    braced = pi.fit_question('Desk organizer', {**DESK, 'rotations': [],
                                                 'brim_policy': {'type': 'outer_only', 'width_mm': 3.0,
                                                                 'label': 'Outer-only brim, 3 mm'}})
    assert 'Tight-fit brim policy: Outer-only brim, 3 mm.' in braced


def test_a_turned_row_never_inherits_the_authored_layouts_brim():
    shrink_policy = {'type': 'outer_only', 'width_mm': 3.0, 'label': 'Outer-only brim, 3 mm'}
    options = {'brim_policy': shrink_policy}
    assert pi.row_brim_policy({'kind': 'scale'}, options) == shrink_policy
    assert pi.row_brim_policy({'kind': 'rotate', 'brim': False}, options)['width_mm'] == 0.0
    assert pi.row_brim_policy({'kind': 'rotate', 'brim': True}, options) is None  # keeps the 5 mm orientation brim


def _stl(path, boxes):
    """Binary STL of axis-aligned boxes (x0, y0, z0, x1, y1, z1)."""
    tris = []
    for x0, y0, z0, x1, y1, z1 in boxes:
        v = [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
        for a, b, c, d in ((0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)):
            tris += [(v[a], v[c], v[b]), (v[a], v[d], v[c])]
    with open(path, 'wb') as out:
        out.write(b'\0' * 80 + struct.pack('<I', len(tris)))
        for t in tris:
            out.write(struct.pack('<12fH', 0, 0, 0, *(c for p in t for c in p), 0))


def test_brim_is_for_small_contact_parts_not_wide_flat_ones(tmp_path):
    flat = tmp_path / 'flat.stl'
    pole = tmp_path / 'pole.stl'
    _stl(flat, [(0, 0, 0, 230, 160, 40)])
    _stl(pole, [(0, 0, 0, 10, 10, 200)])
    assert oa.authored_needs_brim(flat) is False
    assert oa.authored_needs_brim(pole) is True
    assert oa.authored_needs_brim(tmp_path / 'missing.stl') is True  # unknown: keep the old widest-brim ladder


def test_fit_options_picks_no_brim_for_a_wide_flat_base_and_keeps_the_ladder_for_a_pole(tmp_path, monkeypatch):
    seen = []

    def verify(source, st, brim_policy=None):
        seen.append(brim_policy['width_mm'])
        return [0.94, 0.75, 0.5]

    monkeypatch.setattr(oa, 'verified_scale_choices', verify)
    monkeypatch.setattr(oa, 'rotation_alternatives', lambda *a, **k: [])
    flat = tmp_path / 'flat.stl'
    pole = tmp_path / 'pole.stl'
    _stl(flat, [(0, 0, 0, 230, 160, 40)])
    _stl(pole, [(0, 0, 0, 10, 10, 200)])
    st = {'fit_rearrange_checked': True}
    assert oa.fit_options(flat, st)['brim_policy']['type'] == 'no_brim'
    assert seen == [0.0]
    seen.clear()
    assert oa.fit_options(pole, st)['brim_policy']['width_mm'] == 3.0
    assert seen == [3.0]


def test_small_contact_rule():
    assert oa.small_contact(157, 20000) and oa.small_contact(1000, 100000) and not oa.small_contact(5000, 20000)
    assert not oa.small_contact(None, 1)


def test_hull_prune_never_changes_the_hull():
    import random
    rng = random.Random(7)
    points = [(rng.gauss(0, 50), rng.gauss(0, 30)) for _ in range(6000)]
    points += [(round(x), round(y)) for x, y in points[:2000]]
    pruned = oa._hull(points)
    keep = oa._drop_interior
    oa._drop_interior = lambda pts: pts
    try:
        assert pruned == oa._hull(points)
    finally:
        oa._drop_interior = keep


def test_single_body_detection_is_conservative():
    one = {'audit': {'plates': 1, 'mesh_audit': {'shells': 1, 'dimensions_mm': [231.31, 157.01, 135.0]}}}
    assert pi._single_body(one, {'parts': []}) is True
    assert pi._single_body(one, {'parts': [{'key': '1:1'}]}) is True
    assert pi._single_body(one, {'parts': [{'key': '1:1'}, {'key': '2:2'}]}) is False
    assert pi._single_body({'audit': {'mesh_audit': {'shells': 2}}}, {'parts': []}) is False
    assert pi._single_body({'audit': {'plates': 2, 'mesh_audit': {'shells': 1}}}, {'parts': []}) is False
    assert pi._single_body({'audit': {'plates': 1}}, {'parts': []}) is False  # shell count unknown
    assert pi._single_body({}, None) is False
    assert pi.audit_dimensions(one) == [231.31, 157.01, 135.0]
    assert pi.audit_dimensions({'audit': {}}) is None


def test_single_body_flow_skips_rearrange_and_split_and_offers_the_turned_size(env, monkeypatch):
    from print_handy.core import orca_adapter, plate_merge, split_fit
    calls = []
    monkeypatch.setattr(model_job, 'printables_inventory', lambda url: {
        'site': 'printables', 'slug': 'desk', 'title': 'Desk organizer',
        'sources': [{'name': 'desk_oganizer.stl', 'instance_id': 1}]})

    def download(job, chosen, dest):
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / 'desk_oganizer.stl'
        _stl(out, [(0, 0, 0, 231.31, 157.01, 135.0)])
        return out

    monkeypatch.setattr(model_job, 'printables_download', download)
    monkeypatch.setattr(model_job, 'audit', lambda path: {
        'plates': 1, 'mesh_audit': {'shells': 1, 'dimensions_mm': [231.31, 157.01, 135.0]}})
    monkeypatch.setattr(model_job, 'full_size_one_plate', lambda *a, **k: calls.append('rearrange'))
    monkeypatch.setattr(split_fit, 'split_plan_100', lambda *a, **k: calls.append('split_plan'))
    monkeypatch.setattr(plate_merge, 'full_size_split_possible', lambda *a, **k: calls.append('split'))
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity',
                        lambda path, st, scale=1.0, copy_limit=16, **kw: 0 if scale == 1.0 and not kw.get('rotation') else 1)
    seen_state = []

    def fit_options(path, st):
        seen_state.append(st)
        return {'scales': [0.916, 0.75, 0.5], 'rotations': [TURN, END],
                'brim_policy': {'type': 'no_brim', 'width_mm': 0.0, 'label': 'No brim'}}

    monkeypatch.setattr(orca_adapter, 'fit_options', fit_options)
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='desk-organizer', text='https://www.printables.com/model/1-ghost')
    adapter = Adapter([0, 0, 1, 1])
    asyncio.run(pi.prepare_link(event, adapter, state(), env.root, timeout=1))
    prompt = next(q for q in adapter.questions if 'does not fit the' in q['question'])
    assert calls == []
    assert seen_state[0].get('fit_rearrange_checked') is True
    assert prompt['choices'] == ['96.3% · turned 126°', 'Cancel']
    assert '(231.31 × 157.01 × 135 mm)' in prompt['question']
    assert prompt['question'].splitlines()[-1] == 'Pick a fit option, or type shrink / cancel.'
    job = json.loads(next(env.root.parent.glob('archive/*/JOB.json')).read_text())
    assert job['scale'] == 0.963 and job['orientation']['axis'] == '+Z'
    assert job['fit_brim_policy']['width_mm'] == 0.0  # the chosen turn prints without a brim


def test_chosen_rotation_is_confirmed_with_the_exact_single_probe(env, monkeypatch):
    """P1-115: rotation_alternatives verifies a turn with the exact-single
    probe (copy_limit=1: one centered copy, no arranging). The confirmation
    of the chosen turn must reuse that same probe — with the default
    multi-copy limit the probe arranges instead, and a part that only fits
    as one centered copy is refused as 'no longer verifies a fit'."""
    from print_handy.core import orca_adapter, plate_merge, split_fit
    monkeypatch.setattr(model_job, 'printables_inventory', lambda url: {
        'site': 'printables', 'slug': 'desk', 'title': 'Desk organizer',
        'sources': [{'name': 'desk_oganizer.stl', 'instance_id': 1}]})

    def download(job, chosen, dest):
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / 'desk_oganizer.stl'
        _stl(out, [(0, 0, 0, 231.31, 157.01, 135.0)])
        return out

    monkeypatch.setattr(model_job, 'printables_download', download)
    monkeypatch.setattr(model_job, 'audit', lambda path: {
        'plates': 1, 'mesh_audit': {'shells': 1, 'dimensions_mm': [231.31, 157.01, 135.0]}})
    monkeypatch.setattr(model_job, 'full_size_one_plate', lambda *a, **k: None)
    monkeypatch.setattr(split_fit, 'split_plan_100', lambda *a, **k: None)
    monkeypatch.setattr(plate_merge, 'full_size_split_possible', lambda *a, **k: False)

    def capacity(path, st, scale=1.0, copy_limit=16, **kw):
        if kw.get('rotation'):
            # The turned part fits only as the exact single the search probed.
            return 1 if copy_limit == 1 else 0
        return 0 if scale == 1.0 else 1

    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', capacity)

    def fit_options(path, st):
        return {'scales': [0.916, 0.75, 0.5], 'rotations': [TURN, END],
                'brim_policy': {'type': 'no_brim', 'width_mm': 0.0, 'label': 'No brim'}}

    monkeypatch.setattr(orca_adapter, 'fit_options', fit_options)
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='desk-organizer-exact', text='https://www.printables.com/model/1-ghost')
    adapter = Adapter([0, 0, 1, 1])
    asyncio.run(pi.prepare_link(event, adapter, state(), env.root, timeout=1))
    assert not any('no longer verifies a fit' in text for text in adapter.sent)
    job = json.loads(next(env.root.parent.glob('archive/*/JOB.json')).read_text())
    assert job['scale'] == 0.963 and job['orientation']['axis'] == '+Z'
