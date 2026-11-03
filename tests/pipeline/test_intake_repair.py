"""Offline regression tests for model questions, profile confirmation and quantities."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace as S

import pytest

from print_handy.core import link_intake
from print_handy.core import model_job
from print_handy.core import prepared_intake
from .test_color_intake import state
from .test_prepared_intake import Adapter, env

URL = 'https://makerworld.com/en/models/3382409-miniature-baroque-frame#profileId-3848581'


@pytest.mark.parametrize('text', [URL + ' how long would this take?',
                                 'How many fit? ' + URL,
                                 '<' + URL + '> How long?',
                                 '[' + 'frame' + '](' + URL + ') how long?'])
def test_model_question_reaches_intake(text):
    assert link_intake.model_link(text) == URL


@pytest.mark.parametrize('text', ['retry ' + URL, 'resume ' + URL,
                                 URL + ' https://www.printables.com/model/2-other',
                                 'https://evil.test/en/models/1 how long?'])
def test_recovery_and_ambiguous_links_not_intercepted(text):
    assert link_intake.model_link(text) is None


def test_linked_profile_is_selected_automatically(env, monkeypatch):
    # Contract changed 2026-10-12: MakerWorld profile choice is automated.
    # A profileId in the link wins and is announced; no profile button.
    rows = [{'instance_id': 3848581, 'title': '0.2mm layer, 2 walls, 8% infill'},
            {'instance_id': 3873478, 'title': 'Optimize parameters'}]
    monkeypatch.setattr(model_job, 'maker_inventory', lambda url: {
        'site': 'makerworld', 'slug': 'frame', 'title': 'Frame', 'sources': rows})
    selected = []

    def download(job, chosen, dest):
        selected.append(chosen['instance_id'])
        return model_job.printables_download(job, chosen, dest)
    monkeypatch.setattr(model_job, 'maker_download', download)
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None),
              message_id='profiles', text=URL)
    adapter = Adapter([1, 0, 3])
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    assert not any('print profiles' in q['question'] for q in adapter.questions)
    assert not any('Selected the profile linked in the URL' in str(m) for m in adapter.sent)
    assert selected == [3848581]
    assert env.sliced == [] and env.starts == []


def test_quantity_buttons_calculation():
    from print_handy.core import orca_adapter
    assert orca_adapter.quantity_buttons(0) == [1]
    assert orca_adapter.quantity_buttons(1) == [1]
    assert orca_adapter.quantity_buttons(2) == [1, 2]
    assert orca_adapter.quantity_buttons(3) == [1, 2, 3]
    assert orca_adapter.quantity_buttons(4) == [1, 2, 4]
    assert orca_adapter.quantity_buttons(5) == [1, 2, 5]
    assert orca_adapter.quantity_buttons(6) == [1, 2, 6]


def test_duration_question_detection():
    from print_handy.core import link_intake
    assert link_intake.has_duration_question('https://makerworld.com/en/models/123 how long would this take?') is True
    assert link_intake.has_duration_question('How much time will this take? https://makerworld.com/en/models/123') is True
    assert link_intake.has_duration_question('https://makerworld.com/en/models/123 what is the duration?') is True
    assert link_intake.has_duration_question('https://makerworld.com/en/models/123') is False
    assert link_intake.has_duration_question('https://makerworld.com/en/models/123 print this please') is False


def test_intake_flow_quantity_prompt_and_duration_answer(env, monkeypatch):
    from print_handy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 3)
    slice_calls = []

    def mock_slice(job_path, copies=None, recipe_sha256=None):
        slice_calls.append(copies)
        gcode = Path(job_path).parent / 'sliced' / 'plate_1.gcode'
        gcode.parent.mkdir(parents=True, exist_ok=True)
        # 2 copies slice is ~2h 11m
        gcode.write_text('; estimated printing time (normal mode) = 2h 11m 12s\nG1 X1\n')
        data = json.loads(Path(job_path).read_text())
        # The real single plate path: Orca clones the copies and records how many.
        data['gcode'] = str(gcode)
        data['sliced_copies'] = {'gcode': str(gcode), 'copies': copies or 1}
        data['copies'] = copies
        model_job.write_job(Path(job_path).parent, data)
        env.sliced.append(str(gcode))
        return [gcode]

    monkeypatch.setattr(model_job, 'slice_job', mock_slice)

    # User message contains model link and duration question
    text = 'https://www.printables.com/model/1-ghost how long would this take?'  # mocked source, never live
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='m_dur', text=text)

    # picks: one color (1), slot 1 (0), quantity 2 copies (1), confirm 2 copies (0), slice only (1), then cancel (1)
    adapter = Adapter([1, 0, 1, 0, 1, 1])
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))

    # Check quantity question was asked with 3 choices: 1 copy, 2 copies, 3 copies
    qty_q = [q for q in adapter.questions if 'how many copies' in q['question'].lower()]
    assert len(qty_q) == 1
    assert qty_q[0]['choices'] == ['One copy', 'Two copies', 'Max (3 copies)', 'Cancel']

    # Slicing called with chosen quantity = 2
    assert slice_calls == [2]

    # Duration question was answered with actual slice time and separate warmup
    dur_msgs = [m for m in adapter.sent if '2 copies' in m and 'warmup' in m.lower()]
    assert len(dur_msgs) >= 1
    assert '2h 11m' in dur_msgs[0]
    assert '5' in dur_msgs[0] and 'warmup' in dur_msgs[0].lower()

