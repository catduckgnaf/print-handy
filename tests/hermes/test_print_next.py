"""Offline tests for /print-next. Fake transport and fake print-now; no printer contact."""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

HERE = Path(__file__).resolve().parent
from print_handy.hermes import print_next
from print_handy.hermes import print_records


def make_job(tmp_path, plates=2):
    folder = tmp_path / 'archive' / 'ghost--abc'
    sliced = folder / 'sliced' / 'orca' / 'run1'
    sliced.mkdir(parents=True)
    rows = []
    for n in range(1, plates + 1):
        g = sliced / f'plate_{n}.gcode'
        g.write_text(f'; plate {n}\n')
        rows.append({'slot': n, 'gcode': str(g)})
    job = {'title': 'Ghost', 'gcode': rows[0]['gcode'], 'derived_plates': rows}
    (folder / 'JOB.json').write_text(json.dumps(job))
    return folder, rows


def make_plan(tmp_path, folder, rows, **second):
    plan = {'plan_id': 'ghost', 'parts': [
        {'id': 'body', 'label': 'Body', 'status': 'completed', 'quantity': 1, 'completed_quantity': 1,
         'run_id': 1, 'plate_id': 1, 'gcode_file': 'plate_1.gcode'},
        {'id': 'tail', 'label': 'Tail', 'quantity': 1, 'completed_quantity': 0, 'run_id': 2, 'plate_id': 2,
         'gcode_file': 'plate_2.gcode', 'job_json': str(folder / 'JOB.json'), 'gcode_path': rows[1]['gcode'],
         **second},
    ]}
    plans = tmp_path / 'Plans'
    plans.mkdir(exist_ok=True)
    (plans / 'ghost.json').write_text(json.dumps(plan))
    return plans


def test_resolves_exact_next_plate(tmp_path):
    folder, rows = make_job(tmp_path)
    plans = make_plan(tmp_path, folder, rows)
    target = print_next.resolve_next(print_next.load_plan('ghost', plans), tmp_path / 'archive')
    assert target['gcode'] == rows[1]['gcode']
    assert target['label'] == 'Tail' and target['reprint'] is False
    assert 'Run 2, Plate 2' in print_next.question_text(target)


@pytest.mark.parametrize('bad', [
    {'job_json': None},
    {'gcode_path': '/etc/passwd'},
    {'gcode_file': 'plate_9.gcode'},
])
def test_refuses_inexact_targets(tmp_path, bad):
    folder, rows = make_job(tmp_path)
    plans = make_plan(tmp_path, folder, rows, **bad)
    with pytest.raises(print_next.NextPlateError):
        print_next.resolve_next(print_next.load_plan('ghost', plans), tmp_path / 'archive')


def test_refuses_artifact_not_current_in_job(tmp_path):
    folder, rows = make_job(tmp_path)
    stale = Path(rows[1]['gcode']).with_name('old_plate_2.gcode')
    stale.write_text('; stale\n')
    plans = make_plan(tmp_path, folder, rows, gcode_path=str(stale), gcode_file='old_plate_2.gcode')
    with pytest.raises(print_next.NextPlateError, match='not a current artifact'):
        print_next.resolve_next(print_next.load_plan('ghost', plans), tmp_path / 'archive')


def test_refuses_path_traversal_plan_id(tmp_path):
    with pytest.raises(print_next.NextPlateError):
        print_next.load_plan('../etc/x', tmp_path)


def test_second_copy_uses_reprint(tmp_path):
    folder, rows = make_job(tmp_path)
    plans = make_plan(tmp_path, folder, rows, quantity=2, completed_quantity=1)
    target = print_next.resolve_next(print_next.load_plan('ghost', plans), tmp_path / 'archive')
    assert target['reprint'] is True and target['copy'] == 2


def test_start_refuses_changed_gcode_without_running(tmp_path):
    folder, rows = make_job(tmp_path)
    plans = make_plan(tmp_path, folder, rows)
    target = print_next.resolve_next(print_next.load_plan('ghost', plans), tmp_path / 'archive')
    Path(target['gcode']).write_text('; tampered\n')
    with mock.patch('asyncio.create_subprocess_exec') as spawn:
        ok, detail = asyncio.run(print_next.start(target))
    assert not ok and 'changed' in detail
    spawn.assert_not_called()


def test_start_runs_print_now_once_with_exact_path(tmp_path):
    folder, rows = make_job(tmp_path)
    plans = make_plan(tmp_path, folder, rows)
    target = print_next.resolve_next(print_next.load_plan('ghost', plans), tmp_path / 'archive')
    fake = tmp_path / 'fake_model_job.sh'
    log = tmp_path / 'calls.txt'
    fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\necho "START REQUEST ACCEPTED"\n')
    fake.chmod(0o755)
    ok, detail = asyncio.run(print_next.start(target, model_job=fake, idle_check=lambda: None,
                                              claims_dir=tmp_path / 'claims'))
    assert ok and 'START REQUEST ACCEPTED' in detail
    assert log.read_text().splitlines() == [f"print-now {rows[1]['gcode']}"]


def _fake_job(tmp_path, body):
    fake = tmp_path / 'fake_model_job.sh'
    log = tmp_path / 'calls.txt'
    fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\n{body}\n')
    fake.chmod(0o755)
    return fake, log


def _target(tmp_path):
    folder, rows = make_job(tmp_path)
    plans = make_plan(tmp_path, folder, rows)
    return print_next.resolve_next(print_next.load_plan('ghost', plans), tmp_path / 'archive')


def test_replayed_button_cannot_start_same_plate_twice(tmp_path):
    target = _target(tmp_path)
    fake, log = _fake_job(tmp_path, 'echo "START REQUEST ACCEPTED"')
    kw = dict(model_job=fake, idle_check=lambda: None, claims_dir=tmp_path / 'claims')
    assert asyncio.run(print_next.start(target, **kw))[0] is True
    ok, detail = asyncio.run(print_next.start(target, **kw))
    assert not ok and 'already started' in detail
    assert len(log.read_text().splitlines()) == 1


def test_busy_or_unreadable_printer_refuses_before_spawn(tmp_path):
    target = _target(tmp_path)
    fake, log = _fake_job(tmp_path, 'echo ok')

    def busy():
        raise RuntimeError('printer is not idle (printing, x.gcode, Printing)')
    ok, detail = asyncio.run(print_next.start(target, model_job=fake, idle_check=busy,
                                              claims_dir=tmp_path / 'claims'))
    assert not ok and 'fresh printer check failed' in detail
    assert not log.exists() and not (tmp_path / 'claims').exists()


def test_clean_refusal_releases_claim_but_failure_keeps_it(tmp_path):
    target = _target(tmp_path)
    claims = tmp_path / 'claims'
    fake, _ = _fake_job(tmp_path, 'echo "REFUSED: mesh stale. Nothing uploaded or started."; exit 1')
    ok, _ = asyncio.run(print_next.start(target, model_job=fake, idle_check=lambda: None, claims_dir=claims))
    assert not ok and list(claims.glob('*.json')) == []
    fake2, _ = _fake_job(tmp_path, 'echo "UPLOAD FAILED: boom"; exit 1')
    ok, _ = asyncio.run(print_next.start(target, model_job=fake2, idle_check=lambda: None, claims_dir=claims))
    kept = list(claims.glob('*.json'))
    assert not ok and len(kept) == 1 and json.loads(kept[0].read_text())['outcome'] == 'failed_uncertain'


def test_timeout_stops_print_now_and_keeps_claim(tmp_path):
    target = _target(tmp_path)
    claims = tmp_path / 'claims'
    marker = tmp_path / 'late_start'
    fake, _ = _fake_job(tmp_path, f'sleep 5; touch {marker}')
    ok, detail = asyncio.run(print_next.start(target, model_job=fake, idle_check=lambda: None,
                                              claims_dir=claims, timeout=0.5))
    import time as _t
    _t.sleep(5.5)
    assert not ok and 'UNCERTAIN' in detail and not marker.exists()
    assert json.loads(next(claims.glob('*.json')).read_text())['outcome'] == 'uncertain_timeout'


def test_summary_names_command_only_with_exact_target(tmp_path):
    folder, rows = make_job(tmp_path)
    plan = json.loads(make_plan(tmp_path, folder, rows).joinpath('ghost.json').read_text())
    summary = print_records.format_multipart_summary(plan, 'plate_1.gcode')
    assert '/print-next ghost' in summary
    assert str(folder) not in summary
    plan['parts'][1].pop('job_json')
    assert '/print-next' not in print_records.format_multipart_summary(plan, 'plate_1.gcode')


def test_completion_binds_sibling_plate_from_same_job(tmp_path, monkeypatch):
    folder, rows = make_job(tmp_path)
    monkeypatch.setattr(print_records, 'PLANS_DIR', tmp_path / 'Plans2')
    data = {'multipart_plan_id': 'ghost', 'gcode_file': 'plate_1.gcode', 'part_id': 'body',
            'completed_at': '2026-10-06T22:00:00Z', 'slicer_settings': {'local_gcode': rows[0]['gcode']},
            'required_parts': [
                {'id': 'body', 'label': 'Body', 'gcode_file': 'plate_1.gcode'},
                {'id': 'tail', 'label': 'Tail', 'gcode_file': 'plate_2.gcode'},
                {'id': 'other', 'label': 'Other', 'gcode_file': 'plate_7.gcode'}]}
    plan_id, complete = print_records.update_multipart_plan(data)
    saved = json.loads((tmp_path / 'Plans2' / 'ghost.json').read_text())
    tail = next(p for p in saved['parts'] if p['id'] == 'tail')
    other = next(p for p in saved['parts'] if p['id'] == 'other')
    assert plan_id == 'ghost' and not complete
    assert tail['gcode_path'] == rows[1]['gcode'] and tail['job_json'] == str(folder / 'JOB.json')
    assert 'job_json' not in other


# Plugin hook ---------------------------------------------------------------

def load_plugin():
    plugin = HERE.parents[1] / 'plugins/print-feedback/__init__.py'
    spec = importlib.util.spec_from_file_location('print_feedback_next', plugin)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Transport:
    def __init__(self, pick):
        self.pick, self.questions, self.sent = pick, [], []

    async def send_clarify(self, **kwargs):
        self.questions.append(kwargs)
        import tools.clarify_gateway as cm
        cm.resolve_gateway_clarify(kwargs['clarify_id'], kwargs['choices'][self.pick])
        return S(success=True)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        # Mirrors DiscordAdapter.send exactly; a text= keyword raises TypeError like production.
        self.sent.append({'chat_id': chat_id, 'content': content, 'reply_to': reply_to, 'metadata': metadata})
        return S(success=True)


def run_hook(tmp_path, text, pick=0, authorized=True, start_result=(True, 'START REQUEST ACCEPTED'), plugin_patch=None):
    folder, rows = make_job(tmp_path)
    plans = make_plan(tmp_path, folder, rows)
    mod = load_plugin()
    for name, value in (plugin_patch or {}).items():
        setattr(mod, name, value)
    adapter = Transport(pick)
    src = S(platform=S(value='discord'), profile='3dprinting', user_id='42', chat_id='c', thread_id=None)
    gateway = S(_is_user_authorized_for_source=lambda s: authorized, _delivery_adapter_for=lambda s: adapter,
                _session_key_for_source=lambda s: 'session-key')
    started = []

    async def fake_start(target):
        started.append(target['gcode'])
        return start_result
    real_resolve, real_load = print_next.resolve_next, print_next.load_plan
    with mock.patch.object(print_next, 'load_plan', lambda pid: real_load(pid, plans)), \
            mock.patch.object(print_next, 'start', fake_start), \
            mock.patch.object(print_next, 'resolve_next',
                              lambda plan: real_resolve(plan, tmp_path / 'archive')):
        result = asyncio.run(mod.print_next_dispatch(S(source=src, text=text, internal=False), gateway))
    return result, adapter, started, rows


def test_hook_button_starts_exact_plate_once(tmp_path):
    result, adapter, started, rows = run_hook(tmp_path, '/print-next ghost', pick=0)
    assert result['action'] == 'skip'
    assert adapter.questions[0]['choices'] == [print_next.START_LABEL, print_next.CANCEL_LABEL]
    assert started == [rows[1]['gcode']]
    assert any(message['content'].startswith('Preparing to start Tail') for message in adapter.sent)
    # The handler posts the one start announcement itself: the start claim
    # suppresses the listener's fallback post (P2-56).
    assert sum(message['content'].startswith('Print started') for message in adapter.sent) == 1


def test_hook_cancel_starts_nothing(tmp_path):
    result, adapter, started, _ = run_hook(tmp_path, '/print-next ghost', pick=1)
    assert started == [] and 'Nothing uploaded or started' in adapter.sent[-1]['content']


def test_hook_unknown_plan_has_no_button(tmp_path):
    result, adapter, started, _ = run_hook(tmp_path, '/print-next nope')
    assert adapter.questions == [] and started == []
    assert 'No next plate' in adapter.sent[-1]['content']


def test_hook_unauthorized_is_silent(tmp_path):
    result, adapter, started, _ = run_hook(tmp_path, '/print-next ghost', authorized=False)
    assert result is None and adapter.questions == [] and adapter.sent == [] and started == []


def test_hook_ignores_other_text(tmp_path):
    result, adapter, _, _ = run_hook(tmp_path, '/print-nextghost')
    assert result is None and adapter.sent == []


def test_hook_refused_start_offers_force_restart(tmp_path):
    async def offer(adapter, source, metadata, pc, reason, **kwargs):
        return 'RESTART OFFERED'
    _result, adapter, _started, _ = run_hook(
        tmp_path, '/print-next ghost', pick=0, start_result=(False, 'Refused: printer state is paused'),
        plugin_patch={'_offer_force_restart': offer})
    assert 'RESTART OFFERED' in adapter.sent[-1]['content']


# Plate choice and interactive continuation (issue 16) ----------------------------------

def three_plate_plan(tmp_path):
    folder, rows = make_job(tmp_path, plates=3)
    parts = [{'id': f'plate{n}', 'label': f'Part {n}', 'plate': n, 'quantity': 1, 'completed_quantity': 0,
              'status': 'planned', 'gcode_file': f'plate_{n}.gcode', 'job_json': str(folder / 'JOB.json'),
              'gcode_path': rows[n - 1]['gcode']} for n in (1, 2, 3)]
    plans = tmp_path / 'Plans'
    plans.mkdir(exist_ok=True)
    (plans / 'trio.json').write_text(json.dumps({'plan_id': 'trio', 'parts': parts}))
    return plans, rows


def test_resolve_explicit_part_defaults_to_next_required(tmp_path):
    plans, rows = three_plate_plan(tmp_path)
    plan = print_next.load_plan('trio', plans)
    archive = tmp_path / 'archive'
    assert print_next.resolve_next(plan, archive)['part_id'] == 'plate1'
    third = print_next.resolve_next(plan, archive, part_id='plate3')
    assert third['part_id'] == 'plate3' and third['gcode'] == rows[2]['gcode']
    with pytest.raises(print_next.NextPlateError, match='already complete or is not part'):
        print_next.resolve_next(plan, archive, part_id='nope')
    plan['parts'][2]['status'] = 'completed'
    with pytest.raises(print_next.NextPlateError, match='already complete'):
        print_next.resolve_next(plan, archive, part_id='plate3')


def test_plate_choice_label_uses_plan_facts_and_says_unknown(tmp_path):
    plans, rows = three_plate_plan(tmp_path)
    part = print_next.load_plan('trio', plans)['parts'][1]
    Path(rows[1]['gcode']).write_text('; estimated printing time (normal mode) = 2h 5m 0s\n')
    with mock.patch('print_handy.hermes.discord_print_summary.find_job', return_value=(None, None)):
        assert print_next.plate_choice_label(part) == 'Plate 2: Part 2 · colors unknown · ~2h 5m'
        Path(rows[1]['gcode']).write_text('G1 X1\n')
        assert print_next.plate_choice_label(part) == 'Plate 2: Part 2 · colors unknown · time unknown'


def test_summary_with_picker_has_no_command_text(tmp_path):
    plans, _rows = three_plate_plan(tmp_path)
    plan = print_next.load_plan('trio', plans)
    text = print_records.format_multipart_summary(plan, 'plate1', picker=True)
    assert '/print-next' not in text and 'picker below' in text
    assert '/print-next trio' in print_records.format_multipart_summary(plan, 'plate1')
    assert [p['id'] for p in print_records.remaining_plate_choices(plan)] == ['plate1', 'plate2', 'plate3']
    plan['parts'][0]['status'] = 'completed'
    assert [p['id'] for p in print_records.remaining_plate_choices(plan)] == ['plate2', 'plate3']


def test_hook_accepts_explicit_part_id(tmp_path):
    folder, rows = make_job(tmp_path)
    plans = make_plan(tmp_path, folder, rows)
    plan = json.loads((plans / 'ghost.json').read_text())
    plan['parts'][0].update(status='planned', completed_quantity=0, job_json=str(folder / 'JOB.json'),
                            gcode_path=rows[0]['gcode'])
    (plans / 'ghost.json').write_text(json.dumps(plan))
    mod = load_plugin()
    adapter = Transport(0)
    src = S(platform=S(value='discord'), profile='3dprinting', user_id='42', chat_id='c', thread_id=None)
    gateway = S(_is_user_authorized_for_source=lambda s: True, _delivery_adapter_for=lambda s: adapter,
                _session_key_for_source=lambda s: 'session-key')
    started = []

    async def fake_start(target):
        started.append(target['part_id'])
        return True, 'START REQUEST ACCEPTED'
    real_resolve, real_load = print_next.resolve_next, print_next.load_plan
    with mock.patch.object(print_next, 'load_plan', lambda pid: real_load(pid, plans)), \
            mock.patch.object(print_next, 'start', fake_start), \
            mock.patch.object(print_next, 'resolve_next',
                              lambda plan, part_id=None: real_resolve(plan, tmp_path / 'archive', part_id)):
        asyncio.run(mod.print_next_dispatch(S(source=src, text='/print-next ghost tail', internal=False), gateway))
        asyncio.run(mod.print_next_dispatch(S(source=src, text='/print-next ghost', internal=False), gateway))
    assert started == ['tail', 'body']


def run_announcer(monkeypatch, lines):
    from print_handy.hermes import moonraker_event_listener as mel
    sent, pickers = [], []
    monkeypatch.setattr(mel, 'log', lambda _: None)
    monkeypatch.setattr(mel, 'post', lambda text: sent.append(text) or True)
    monkeypatch.setattr(mel, 'post_plate_picker', lambda plan_id: pickers.append(plan_id) or True)
    monkeypatch.setattr(mel.announcer, 'main', lambda: print(lines))
    mel.run_announcer('completion')
    return sent, pickers


def test_completion_posts_picker_not_command_text(monkeypatch):
    marker = '\n'.join(['Print complete: Job', '- Start option: choose the next plate from the picker below',
                        'PLATE_PICKER:trio'])
    sent, pickers = run_announcer(monkeypatch, marker)
    assert pickers == ['trio']
    assert len(sent) == 1 and '/print-next' not in sent[0] and 'PLATE_PICKER' not in sent[0]


def test_picker_follows_timelapse_post(monkeypatch):
    from print_handy.hermes import moonraker_event_listener as mel
    order = []
    monkeypatch.setattr(mel, 'log', lambda _: None)
    monkeypatch.setattr(mel, 'post', lambda text: order.append('post') or True)
    monkeypatch.setattr(mel, 'post_plate_picker', lambda plan_id: order.append('picker') or True)
    monkeypatch.setattr(mel.announcer, 'main', lambda: print('TIMELAPSE_POSTPROCESS:j.gcode\nDone\nPLATE_PICKER:trio'))
    monkeypatch.setattr(mel.announcer, '_timelapse_job_key', lambda token: 'j.gcode')
    monkeypatch.setattr(mel.announcer, 'archive_completed_timelapse',
                        lambda key: mel.announcer.ArchiveOutcome(mel.announcer.ARCHIVE_NOT_RENDERED))

    class Inline:
        def __init__(self, target=None, args=(), **kw):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)
    monkeypatch.setattr(mel.threading, 'Thread', Inline)
    mel.run_announcer('completion')
    assert order == ['post', 'picker']


def test_post_plate_picker_posts_select_then_falls_back_to_command_text(tmp_path, monkeypatch):
    from print_handy.hermes import moonraker_event_listener as mel
    from print_handy.hermes import print_alert_buttons as pab
    plans, _rows = three_plate_plan(tmp_path)
    real_load = print_next.load_plan
    monkeypatch.setattr(print_next, 'load_plan', lambda pid: real_load(pid, plans))
    monkeypatch.setattr(mel, 'log', lambda _: None)
    monkeypatch.setattr(mel, '_profile_env_value', lambda name: 'token')
    monkeypatch.setattr(pab, 'STATE', tmp_path / 'alerts.json')
    bodies, texts = [], []
    monkeypatch.setattr(mel, 'post', lambda text: texts.append(text) or True)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(request, timeout):
        bodies.append(json.loads(request.data))
        return Response()
    monkeypatch.setattr(pab.urllib.request, 'urlopen', opener)
    assert mel.post_plate_picker('trio') and texts == []
    select = bodies[0]['components'][0]['components'][0]
    assert select['type'] == 3 and select['custom_id'].startswith('pbplate:pick:')
    assert [o['value'] for o in select['options']] == ['plate1', 'plate2', 'plate3']
    assert select['options'][0]['label'].startswith('Plate 1: ')
    assert pab.parse_plate_custom_id(select['custom_id'])[0] == 'pick'

    def refuse(request, timeout):
        raise OSError('no route')
    monkeypatch.setattr(pab.urllib.request, 'urlopen', refuse)
    mel.post_plate_picker('trio')
    assert texts == ['To start the next plate, type `/print-next trio`.']
    monkeypatch.setattr(mel, '_profile_env_value', lambda name: '')
    texts.clear()
    mel.post_plate_picker('trio')
    assert texts == ['To start the next plate, type `/print-next trio`.']


class Interaction:
    def __init__(self, custom_id, values=None):
        self.data = {'custom_id': custom_id, **({'values': values} if values else {})}
        self.user = S(id=1, bot=False, display_name='op')
        self.guild = None
        self.channel_id = 'c'
        self.out = []
        interaction = self

        class Response:
            async def send_message(self, content, ephemeral=False, **kw):
                interaction.out.append(('send', content))

            async def edit_message(self, content=None, view=None, **kw):
                interaction.out.append(('edit', content, view))
        self.response = Response()
        self.followup = S(send=self._followup)

    async def _followup(self, content, view=None):
        self.out.append(('followup', content) if view is None else ('followup', content, view))


def plate_clicks(tmp_path, monkeypatch, starts, start_result=(True, 'START ACCEPTED')):
    from print_handy.hermes import print_alert_buttons as pab
    plans, rows = three_plate_plan(tmp_path)
    real_load, real_resolve = print_next.load_plan, print_next.resolve_next
    monkeypatch.setattr(print_next, 'load_plan', lambda pid: real_load(pid, plans))
    monkeypatch.setattr(print_next, 'resolve_next',
                        lambda plan, archive=None, part_id=None: real_resolve(plan, tmp_path / 'archive', part_id))

    async def fake_start(target):
        starts.append(target['part_id'])
        return start_result
    monkeypatch.setattr(print_next, 'start', fake_start)
    monkeypatch.setattr(pab, 'STATE', tmp_path / 'alerts.json')

    class Button:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class View:
        def __init__(self, timeout=None):
            self.items = []

        def add_item(self, item):
            self.items.append(item)
    fake = S(ui=S(View=View, Button=Button), ButtonStyle=S(success=3))
    monkeypatch.setitem(sys.modules, 'discord', fake)
    handlers = {}
    load_plugin().alert_button_handlers(S(add_listener=lambda fn, name: handlers.update({name: fn})), S())
    picker = pab.record('plate-picker', '', plan_id='trio')
    return handlers['on_interaction'], picker, plans


def test_picker_click_offers_exactly_one_confirm_and_confirm_starts_once(tmp_path, monkeypatch):
    starts = []
    on_interaction, picker, _ = plate_clicks(tmp_path, monkeypatch, starts)
    pick = Interaction(f'pbplate:pick:{picker}', ['plate3'])
    asyncio.run(on_interaction(pick))
    kind, content, view = pick.out[0]
    assert kind == 'edit' and 'Part 3' in content and starts == []
    assert len(view.items) == 1 and view.items[0].label == 'Confirm bed is clear & print'
    go_id = view.items[0].custom_id
    first, second = Interaction(go_id), Interaction(go_id)
    asyncio.run(on_interaction(first))
    asyncio.run(on_interaction(second))
    assert starts == ['plate3']
    assert 'already used' in second.out[0][1]
    # The picker itself is single use as well.
    again = Interaction(f'pbplate:pick:{picker}', ['plate2'])
    asyncio.run(on_interaction(again))
    assert 'old or was already used' in again.out[0][1] and starts == ['plate3']


def test_stale_picker_for_finished_plate_starts_nothing(tmp_path, monkeypatch):
    starts = []
    on_interaction, picker, plans = plate_clicks(tmp_path, monkeypatch, starts)
    plan = json.loads((plans / 'trio.json').read_text())
    plan['parts'][1]['status'] = 'completed'
    (plans / 'trio.json').write_text(json.dumps(plan))
    pick = Interaction(f'pbplate:pick:{picker}', ['plate2'])
    asyncio.run(on_interaction(pick))
    assert 'Nothing started' in pick.out[0][1] and starts == []


def test_confirm_for_plate_finished_after_pick_starts_nothing(tmp_path, monkeypatch):
    starts = []
    on_interaction, picker, plans = plate_clicks(tmp_path, monkeypatch, starts)
    pick = Interaction(f'pbplate:pick:{picker}', ['plate2'])
    asyncio.run(on_interaction(pick))
    go_id = pick.out[0][2].items[0].custom_id
    plan = json.loads((plans / 'trio.json').read_text())
    plan['parts'][1]['status'] = 'completed'
    (plans / 'trio.json').write_text(json.dumps(plan))
    go = Interaction(go_id)
    asyncio.run(on_interaction(go))
    assert starts == [] and 'Nothing started' in go.out[0][1]


def test_refused_start_reports_and_names_fresh_button_command(tmp_path, monkeypatch):
    starts = []
    on_interaction, picker, _ = plate_clicks(tmp_path, monkeypatch, starts, (False, 'REFUSED: printer is busy'))
    pick = Interaction(f'pbplate:pick:{picker}', ['plate1'])
    asyncio.run(on_interaction(pick))
    go = Interaction(pick.out[0][2].items[0].custom_id)
    asyncio.run(on_interaction(go))
    assert starts == ['plate1']
    assert go.out[-1][0] == 'followup' and "Didn't start:" in go.out[-1][1]
    assert 'REFUSED:' not in go.out[-1][1]  # the raw transport tail is translated for chat (#467)


SWAPPED = ('REFUSED: slot 2 spool changed since slicing (was 3, now 7); reslice or confirm. '
           'Nothing uploaded or started.')


def test_picker_spool_change_offers_one_single_use_loaded_spool_button(tmp_path, monkeypatch):
    on_interaction, picker, _ = plate_clicks(tmp_path, monkeypatch, [])
    calls = []

    async def start(target):
        calls.append((target['part_id'], bool(target.get('accept_spool_change'))))
        return (True, 'START ACCEPTED') if target.get('accept_spool_change') else (False, SWAPPED)
    monkeypatch.setattr(print_next, 'start', start)
    pick = Interaction(f'pbplate:pick:{picker}', ['plate2'])
    asyncio.run(on_interaction(pick))
    go = Interaction(pick.out[0][2].items[0].custom_id)
    asyncio.run(on_interaction(go))
    kind, content, view = go.out[-1]
    assert kind == 'followup' and calls == [('plate2', False)]
    assert 'Slot 2 now holds spool #7 (it was #3 at slicing)' in content and 'Same material family' in content
    assert len(view.items) == 1 and view.items[0].label == 'Use the loaded spool \u00b7 bed is clear'
    spool_id = view.items[0].custom_id
    assert spool_id.startswith('pbplate:spool:')
    first, second = Interaction(spool_id), Interaction(spool_id)
    asyncio.run(on_interaction(first))
    asyncio.run(on_interaction(second))
    assert calls == [('plate2', False), ('plate2', True)]
    assert 'already used' in second.out[0][1]
    # accepted: the handler posts the one "Print started" announcement itself,
    # because the start claim suppresses the listener's fallback post (P2-56).
    followups = [row for row in first.out if row[0] == 'followup']
    assert len(followups) == 1 and followups[0][1].startswith('Print started')


def test_picker_spool_button_refusal_does_not_offer_another(tmp_path, monkeypatch):
    on_interaction, picker, _ = plate_clicks(tmp_path, monkeypatch, [])

    async def start(target):
        return False, SWAPPED
    monkeypatch.setattr(print_next, 'start', start)
    pick = Interaction(f'pbplate:pick:{picker}', ['plate2'])
    asyncio.run(on_interaction(pick))
    go = Interaction(pick.out[0][2].items[0].custom_id)
    asyncio.run(on_interaction(go))
    again = Interaction(go.out[-1][2].items[0].custom_id)
    asyncio.run(on_interaction(again))
    assert again.out[-1][0] == 'followup' and len(again.out[-1]) == 2 and 'Not retrying' in again.out[-1][1]


def test_picker_other_refusal_offers_no_spool_button(tmp_path, monkeypatch):
    on_interaction, picker, _ = plate_clicks(tmp_path, monkeypatch, [], (False, 'REFUSED: printer is busy'))
    pick = Interaction(f'pbplate:pick:{picker}', ['plate1'])
    asyncio.run(on_interaction(pick))
    go = Interaction(pick.out[0][2].items[0].custom_id)
    asyncio.run(on_interaction(go))
    assert len(go.out[-1]) == 2


def test_start_spawns_print_now_from_the_repo_root(tmp_path):
    from print_handy import settings
    folder, rows = make_job(tmp_path)
    plans = make_plan(tmp_path, folder, rows)
    target = print_next.resolve_next(print_next.load_plan('ghost', plans), tmp_path / 'archive')
    seen = {}

    async def fake_spawn(*argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        raise OSError('stop here')

    with mock.patch('asyncio.create_subprocess_exec', fake_spawn):
        ok, _ = asyncio.run(print_next.start(target, idle_check=lambda: None, claims_dir=tmp_path / 'claims'))
    assert not ok
    assert seen['kwargs']['cwd'] == str(settings.ROOT)
    assert seen['argv'][1:3] == ('-m', print_next.MODEL_JOB_MODULE)


def _adapter_with(monkeypatch, transport, check):
    """Route live_idle_check through a fake adapter transport and a fake printer_state.check."""
    from print_handy import printers
    from print_handy.core import model_job, printer_state
    monkeypatch.setattr(printers, 'current', lambda: S(transport=transport))
    monkeypatch.setattr(printer_state, 'load', lambda: {'printer': 'fake'})
    monkeypatch.setattr(printer_state, 'check', lambda st: check)
    monkeypatch.setattr(model_job, '_printer_phase',
                        lambda st: pytest.fail('Moonraker queried for a non-Moonraker printer'))


def test_live_idle_check_uses_the_adapter_status_for_bambu_lan(monkeypatch):
    # #269: a Bambu printer has no Moonraker; the idle check must read the adapter's live status.
    _adapter_with(monkeypatch, 'bambulab_lan', (True, [], {'lan_configured': True, 'bambu': {'state': 'standby'}}))
    print_next.live_idle_check()


def test_live_idle_check_refuses_bambu_readiness_problems(monkeypatch):
    _adapter_with(monkeypatch, 'bambulab_lan', (False, ['printer reports 1 active HMS error(s)'], {'lan_configured': True}))
    with pytest.raises(RuntimeError, match='HMS'):
        print_next.live_idle_check()


def test_live_idle_check_refuses_an_unconfigured_bambu_lan(monkeypatch):
    _adapter_with(monkeypatch, 'bambulab_lan', (True, [], {'lan_configured': False}))
    with pytest.raises(RuntimeError, match='not configured'):
        print_next.live_idle_check()


def test_live_idle_check_uses_the_adapter_status_for_octoprint(monkeypatch):
    _adapter_with(monkeypatch, 'octoprint', (True, [], {'octoprint': {'state': 'Operational'}}))
    print_next.live_idle_check()


def test_live_idle_check_still_queries_moonraker_for_moonraker_printers(monkeypatch):
    from print_handy import printers
    from print_handy.core import model_job, printer_state
    monkeypatch.setattr(printers, 'current', lambda: S(transport='moonraker'))
    monkeypatch.setattr(printer_state, 'load', lambda: {})
    monkeypatch.setattr(printer_state, 'check', lambda st: pytest.fail('adapter status used for a Moonraker printer'))
    monkeypatch.setattr(model_job, '_printer_phase', lambda st: ('standby', '', 'Ready'))
    print_next.live_idle_check()
    monkeypatch.setattr(model_job, '_printer_phase', lambda st: ('printing', 'a.gcode', 'Printing'))
    with pytest.raises(RuntimeError, match='not idle'):
        print_next.live_idle_check()


def test_hook_successful_start_posts_start_announcement(tmp_path):
    """A print-now start claims the announcement in model_job, which
    suppresses the listener's fallback post; the handler must therefore
    post the canonical start announcement itself. Before the fix a
    successful /print-next start was never announced (P2-56)."""
    result, adapter, started, rows = run_hook(tmp_path, '/print-next ghost', pick=0,
                                              plugin_patch={'_listener_roast': lambda job: ''})
    assert started == [rows[1]['gcode']]
    starts = [m['content'] for m in adapter.sent if m['content'].startswith('Print started')]
    assert len(starts) == 1


def test_hook_warming_start_registers_message_for_listener_edit(tmp_path):
    """A still-preparing command start registers its announcement's id so
    the listener edits that post on the warmup transition instead of the
    start going unannounced (P2-56)."""
    folder, rows = make_job(tmp_path)
    plans = make_plan(tmp_path, folder, rows)
    mod = load_plugin()
    mod._listener_roast = lambda job: ''

    class IdTransport(Transport):
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.sent.append({'chat_id': chat_id, 'content': content,
                              'reply_to': reply_to, 'metadata': metadata})
            return S(success=True, message_id='msg-42')

    adapter = IdTransport(0)
    src = S(platform=S(value='discord'), profile='3dprinting', user_id='42', chat_id='c', thread_id=None)
    gateway = S(_is_user_authorized_for_source=lambda s: True, _delivery_adapter_for=lambda s: adapter,
                _session_key_for_source=lambda s: 'session-key')
    detail = 'START SUBMITTED; AWAITING CONFIRMATION\n{"file": "plate_2.gcode"}'

    async def fake_start(target):
        return True, detail

    recorded = []
    real_resolve, real_load = print_next.resolve_next, print_next.load_plan
    with mock.patch.object(print_next, 'load_plan', lambda pid: real_load(pid, plans)), \
            mock.patch.object(print_next, 'start', fake_start), \
            mock.patch.object(print_next, 'record_start_message',
                              lambda filename, mid, **kw: recorded.append((filename, mid, kw))), \
            mock.patch.object(print_next, 'resolve_next',
                              lambda plan: real_resolve(plan, tmp_path / 'archive')):
        result = asyncio.run(mod.print_next_dispatch(
            S(source=src, text='/print-next ghost', internal=False), gateway))
    assert result['action'] == 'skip'
    # The originating channel rides along so the warmup edit lands there (2.2.5 review, P2-11).
    assert recorded == [('plate_2.gcode', 'msg-42', {'chat_id': 'c', 'thread_id': None})]
    assert any(m['content'].startswith('Print started') for m in adapter.sent)
def _routed_adapter_with(monkeypatch, connection, phase, native=False):
    """Route live_idle_check(printer_id) through a fake roster adapter.

    printer_state.check passes (hardware/spool readiness is fine); the
    execution state comes from the fake _printer_phase.
    """
    from print_handy import printers, settings
    from print_handy.core import model_job, printer_state
    adapter = S(transport=None, connection=connection, has_native_screen=native)
    monkeypatch.setattr(settings, 'by_id', lambda pid: {'model': 'klipper.generic'})
    monkeypatch.setattr(settings, 'view_for', lambda pid: {})
    monkeypatch.setattr(printers, 'get', lambda model, view: adapter)
    monkeypatch.setattr(printer_state, 'load', lambda pid=None: {'printer': 'routed'})
    monkeypatch.setattr(printer_state, 'check', lambda st, printer_id=None, adapter=None: (True, [], {}))
    monkeypatch.setattr(model_job, '_printer_phase', phase)


def test_live_idle_check_routed_moonraker_refuses_a_paused_printer(monkeypatch):
    # P1-80: printer_state.check never queries print_stats for Moonraker, so a
    # routed check must still ask for the execution state; a paused printer
    # is not idle even though its hardware readiness passes.
    _routed_adapter_with(monkeypatch, 'moonraker', lambda st, native=None: ('paused', 'a.gcode', 'Moonraker reports active execution'))
    with pytest.raises(RuntimeError, match='not idle'):
        print_next.live_idle_check('bench')


def test_live_idle_check_routed_moonraker_accepts_standby(monkeypatch):
    _routed_adapter_with(monkeypatch, 'moonraker', lambda st, native=None: ('standby', '', 'Ready'))
    print_next.live_idle_check('bench')


def test_live_idle_check_routed_bambu_does_not_query_moonraker(monkeypatch):
    _routed_adapter_with(monkeypatch, 'bambulab_lan',
                         lambda st, native=None: pytest.fail('Moonraker queried for a Bambu printer'))
    print_next.live_idle_check('bench')


def test_live_idle_check_routed_moonraker_uses_the_routed_printers_native_flag(monkeypatch):
    # 2.2.5 review: the phase query follows the routed adapter, not the default
    # printer's has_native_screen (a native default made a plain Klipper idle
    # check raise "display_off mode is None").
    seen = []
    _routed_adapter_with(monkeypatch, 'moonraker',
                         lambda st, native=None: seen.append(native) or ('standby', '', 'Ready'))
    print_next.live_idle_check('bench')
    assert seen == [False]
