"""Yolo: print an operator-vouched file, reporting checks instead of enforcing them."""
import asyncio
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy.core import model_job, prepared_intake, yolo
from .helpers import external
from .test_intake_support_precheck import Adapter, env, run_flow, _warning_slice, V12_FIXTURE, _copy  # noqa: F401

print_next = external("print_next")


# ---------------------------------------------------------------- yolo.py

@pytest.mark.parametrize('text, want', [
    ('yolo', True), ('YOLO print it', True), ('https://x/y yolo', False), ('yolo!', False),
    ('yolo https://x/y', True), ('yolo! print it', False), ('This is a yolo print', False),
    ('I said yolo earlier', False), ('yolomode', False), ('no-yolo-here', False),
    ('', False), ('print it', False),
])
def test_requested_is_a_whole_word(text, want):
    assert yolo.requested(text) is want


def test_strip_leaves_the_rest_of_the_message():
    assert yolo.strip('https://www.printables.com/model/1-ghost  yolo ') == 'https://www.printables.com/model/1-ghost'
    assert yolo.strip('YOLO shrink it by 20%') == 'shrink it by 20%'


@pytest.mark.parametrize('choices, want', [
    (['Slice and print with supports (bed is clear)', 'Slice and print without supports (bed is clear)',
      'Print anyway (accept the risk)', 'Slice only', 'Cancel'], 2),
    (['Print anyway', 'Cancel'], 0),
    (['Re-slice with supports', 'Print without supports', 'Cancel'], 0),
    (['Cancel', 'One color for everything'], 1),
    (['Do not print (Cancel)'], 0),
    (['Start next plate · bed is clear', 'Cancel'], 0),
])
def test_auto_answer_takes_the_go_option_never_cancel(choices, want):
    assert yolo.auto_answer(choices) == want


def test_may_yolo_needs_a_configured_approver():
    # S6: with no approver configured nobody may yolo, approval_all_users or not.
    assert yolo.may_yolo('1', cfg={'discord': {}}) is False
    assert yolo.may_yolo('1', cfg={'discord': {'approval_all_users': True}}) is False
    assert yolo.may_yolo('1', cfg={'telegram': {}}, platform='telegram') is False
    assert yolo.may_yolo('1', cfg={'discord': {'approval_user_id': '1, 2'}}) is True
    assert yolo.may_yolo('3', cfg={'discord': {'approval_user_id': '1, 2'}}) is False
    assert yolo.may_yolo(None, cfg={'discord': {'approval_user_id': '1'}}) is False
    # With an approver configured, approval_all_users keeps meaning "everyone approves".
    assert yolo.may_yolo('3', cfg={'discord': {'approval_user_id': '1', 'approval_all_users': True}}) is True


def test_web_chat_operator_may_yolo_as_its_approval_user():
    # The web chat is built with its operator as the approval user (bot/web.py).
    assert yolo.may_yolo('webui', platform='webui', cfg={}) is True
    assert yolo.may_yolo('someone', platform='webui', cfg={}) is False


def test_refusal_says_how_to_configure_an_approver():
    text = yolo.refusal('discord', cfg={'discord': {}})
    assert 'approval_user_id' in text and 'none is configured for discord' in text
    assert 'PRINT_HANDY__DISCORD__APPROVAL_USER_ID' in text
    assert 'approval_user_id' not in yolo.refusal('discord', cfg={'discord': {'approval_user_id': '1'}})


def _within_limits_gcode(tmp_path, body):
    g = tmp_path / 'vouched.gcode'
    g.write_text('G90\nG1 X10 Y10 Z0.2\n' + body)
    return g


@pytest.mark.parametrize('body, problem', [
    ('G1 X400\n', 'X bounds 10.000..400.000 exceed 0.000..223.000'),
    ('G91;rel\nG1 X100\nG1 X100\nG1 X100\n', 'X bounds 10.000..310.000 exceed'),
    ('SET_GCODE_OFFSET X=300\n', 'toolpath bounds unavailable: line 3: SET_GCODE_OFFSET'),
    ('M104 S350\n', 'nozzle target 350 C exceeds'),
    ('M190 S200\n', 'bed target 200 C exceeds'),
    ('SET_HEATER_TEMPERATURE HEATER=extruder TARGET=999\n', 'nozzle target 999 C'),
    ('START_PRINT BED_TEMP=60 EXTRUDER_TEMP=480\n', 'nozzle target 480 C'),
])
def test_mechanical_limits_refuse(tmp_path, monkeypatch, body, problem):
    monkeypatch.setattr(model_job, 'BOUNDS_CACHE', tmp_path / 'cache')
    adapter = S(fact=lambda name: {'max_hotend_c': 300, 'max_bed_c': 110}[name])
    with pytest.raises(RuntimeError, match='mechanical limits are never waived') as caught:
        model_job.require_mechanical_limits(_within_limits_gcode(tmp_path, body),
                                            {'nozzle': {'type': 'hardened_steel'}}, adapter)
    assert problem in str(caught.value)


def test_mechanical_limits_pass_an_ordinary_file(tmp_path, monkeypatch):
    monkeypatch.setattr(model_job, 'BOUNDS_CACHE', tmp_path / 'cache')
    adapter = S(fact=lambda name: {'max_hotend_c': 300, 'max_bed_c': 110}[name])
    g = _within_limits_gcode(tmp_path, 'M140 S60\nM104 S220 T0\nM109 R215\nG1 X200 Y200 Z30\n')
    assert model_job.require_mechanical_limits(g, {'nozzle': {'type': 'brass'}}, adapter) is None


def test_gcode_file_and_package_extraction(tmp_path):
    g = tmp_path / 'doc_0123456789ab_part.gcode'
    g.write_text('G1 X1\n')
    assert yolo.gcode_file(S(media_urls=[str(g)])) == g
    assert yolo.gcode_file(S(media_urls=[str(tmp_path / 'part.stl')])) is None
    pkg = tmp_path / 'hook.gcode.3mf'
    with zipfile.ZipFile(pkg, 'w') as z:
        z.writestr('Metadata/plate_2.gcode', 'G1 X2\n')
        z.writestr('Metadata/plate_1.gcode', 'G1 X1\n')
    out, plates = yolo.extract_gcode(pkg, tmp_path / 'out')
    assert plates == 2 and out.name == 'hook.gcode' and out.read_text() == 'G1 X1\n'



@pytest.mark.parametrize('choices', [
    ['Force restart printer now', 'Cancel restart'],
    ['Bigger', 'Smaller', 'Cancel scaling'],
])
def test_auto_answer_leaves_printer_actions_and_rescales_to_a_person(choices):
    assert yolo.auto_answer(choices) is None


def test_ask_under_yolo_hands_a_restart_question_to_a_person(monkeypatch):
    asked = []

    async def ask(adapter, source, key, question, choices, timeout, **kwargs):
        asked.append(list(choices))
        return 1
    monkeypatch.setattr(prepared_intake, 'ask', ask)
    sent = []

    class _A:
        async def send(self, chat_id, content, metadata=None):
            sent.append(content)
    token = yolo.ACTIVE.set(True)
    try:
        pick = asyncio.run(prepared_intake._ask(_A(), S(chat_id='c', thread_id=None), 'k', 'Printer stuck?',
                                                ['Force restart printer now', 'Cancel restart'], 1))
    finally:
        yolo.ACTIVE.reset(token)
    assert pick == 1 and asked == [['Force restart printer now', 'Cancel restart']]
    assert any('a person has to' in m for m in sent)


def test_package_extraction_refuses_an_oversized_plate(tmp_path, monkeypatch):
    monkeypatch.setattr(yolo, 'MAX_GCODE_BYTES', 64)
    pkg = tmp_path / 'bomb.gcode.3mf'
    with zipfile.ZipFile(pkg, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('Metadata/plate_1.gcode', 'G1 X1\n' * 100)
    with pytest.raises(ValueError, match='limit'):
        yolo.extract_gcode(pkg, tmp_path / 'out')
    assert not (tmp_path / 'out' / 'bomb.gcode').exists()


# ---------------------------------------------------------------- model_job gates

def test_waive_refuses_normally_and_reports_under_yolo(capsys):
    with pytest.raises(SystemExit, match='REFUSED: nope'):
        model_job._refuse('demo', 'REFUSED: nope. Nothing uploaded or started.')
    token = model_job._YOLO.set(True)
    try:
        model_job._refuse('demo', 'REFUSED: nope. Nothing uploaded or started.')
        assert model_job._gate('raiser', lambda: (_ for _ in ()).throw(RuntimeError('bad temp'))) is None
    finally:
        model_job._YOLO.reset(token)
    out = capsys.readouterr().out
    assert model_job.yolo_waivers(out) == ['demo: nope', 'raiser: bad temp']


def test_gate_never_swallows_a_non_refusal_exit():
    token = model_job._YOLO.set(True)
    try:
        with pytest.raises(SystemExit):
            model_job._gate('exit', lambda: (_ for _ in ()).throw(SystemExit(2)))
        with pytest.raises(KeyError):
            model_job._gate('bug', lambda: {}['missing'])
    finally:
        model_job._YOLO.reset(token)


def test_print_now_cli_and_print_next_carry_the_flag(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(model_job, '_print_now_cli', lambda target, **kw: seen.update(target=target, **kw))
    monkeypatch.setattr(model_job.sys, 'argv', ['model_job', 'print-now', 'x.gcode', '--yolo'])
    model_job._main()
    assert seen['yolo'] is True and seen['target'] == 'x.gcode'
    monkeypatch.setattr(model_job.sys, 'argv', ['model_job', 'print-now', 'x.gcode'])
    seen.clear()
    model_job._main()
    assert 'yolo' not in seen


def test_print_now_yolo_waives_checks_but_keeps_the_busy_printer_refusal(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(model_job, 'BOUNDS_CACHE', tmp_path / 'cache')
    g = tmp_path / 'part.gcode'
    g.write_text('G90\nG1 X1 Y1 Z0.2\n')
    adapter = S(transport=None, connection='lan', multi_slot=False, label='AD5X',
                native=None, config={'printer': {}}, model=S(vendor_key='generic'))
    monkeypatch.setattr(model_job.printers, 'current', lambda: adapter)
    monkeypatch.setattr(model_job.printer_state, 'load', lambda *a, **k: {})
    monkeypatch.setattr(model_job, '_printer_phase', lambda st: ('printing', 'other.gcode', 'Printing'))
    for yolo_flag in (False, True):
        with pytest.raises(SystemExit, match='printer is busy'):
            model_job.print_now(str(g), yolo=yolo_flag)

    calls = []
    monkeypatch.setattr(model_job, '_printer_phase', lambda st: ('standby', '', 'Ready'))
    monkeypatch.setattr(model_job, 'mesh_readiness', lambda st: {'problem': 'could not read the bed mesh',
                                                                 'display_off': None})
    monkeypatch.setattr(model_job, '_verify_routed',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('BED_TEMP 60 != spool 65')))
    monkeypatch.setattr(model_job, '_refuse_multicolor', lambda text: None)
    monkeypatch.setattr(model_job, '_verify_filament_readiness',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('filament readiness refused: x')))

    class Stop(Exception):
        pass

    def _upload(*a, **k):
        calls.append('upload')
        raise Stop()
    monkeypatch.setattr(model_job, '_zmod_upload_artifact', _upload)
    with pytest.raises(SystemExit, match='bed mesh'):
        model_job.print_now(str(g))
    assert calls == []
    with pytest.raises(Stop):
        model_job.print_now(str(g), yolo=True)
    assert calls == ['upload']  # every waivable gate reported, the upload reached
    waived = model_job.yolo_waivers(capsys.readouterr().out)
    assert any(w.startswith('bed mesh: could not read the bed mesh') for w in waived)
    assert any(w.startswith('G-code safety checks: BED_TEMP 60 != spool 65') for w in waived)
    assert any(w.startswith('filament readiness: ') for w in waived)
    assert model_job.yolo_active() is False  # the context never leaks past the run


def test_print_now_yolo_never_waives_the_axis_limits(monkeypatch, tmp_path, capsys):
    """S6: the G-code checks are waived under yolo, the axis limits are not."""
    monkeypatch.setattr(model_job, 'BOUNDS_CACHE', tmp_path / 'cache')
    g = tmp_path / 'part.gcode'
    g.write_text('G90\nG1 X10 Y10 Z0.2\nG91;rel\nG1 X100\nG1 X100\nG1 X100\n')
    adapter = S(transport=None, connection='lan', multi_slot=False, label='AD5X',
                native=None, config={'printer': {}}, model=S(vendor_key='generic'))
    monkeypatch.setattr(model_job.printers, 'current', lambda: adapter)
    monkeypatch.setattr(model_job.printer_state, 'load', lambda *a, **k: {})
    monkeypatch.setattr(model_job, '_printer_phase', lambda st: ('standby', '', 'Ready'))
    monkeypatch.setattr(model_job, 'mesh_readiness', lambda st: {'problem': None, 'display_off': None})
    monkeypatch.setattr(model_job, '_verify_routed',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('X bounds exceed')))
    monkeypatch.setattr(model_job, '_refuse_multicolor', lambda text: None)
    uploads = []
    monkeypatch.setattr(model_job, '_zmod_upload_artifact', lambda *a, **k: uploads.append(1))
    with pytest.raises(SystemExit, match='mechanical limits are never waived: X bounds 10.000..310.000'):
        model_job.print_now(str(g), yolo=True)
    assert uploads == []
    assert any(w.startswith('G-code safety checks:') for w in model_job.yolo_waivers(capsys.readouterr().out))


# ---------------------------------------------------------------- chat: sliced G-code drops

def _gcode_event(tmp_path, text, name='doc_0123456789ab_hook.gcode', body='G1 X1 E1\n'):
    g = tmp_path / name
    g.write_text(body)
    return S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u',
                      platform=S(value='discord')),
             message_id='m-gcode', text=text, media_urls=[str(g)], metadata={})


@pytest.fixture
def gcode_env(tmp_path, monkeypatch):
    monkeypatch.setattr(model_job, 'ARCHIVE', tmp_path / 'archive')
    monkeypatch.setattr(yolo, 'may_yolo', lambda user_id, platform='discord', cfg=None: user_id == 'u')
    starts = []

    async def start(target):
        starts.append(dict(target))
        return True, ('YOLO WAIVED: G-code safety checks: no END_PRINT\n'
                      'MAPPED START READBACK {"phase": "executing", "outcome": "printing", "started": true}\n')
    monkeypatch.setattr(print_next, 'start', start)
    return S(tmp=tmp_path, starts=starts, root=tmp_path / 'receipts')


def test_yolo_gcode_drop_prints_as_sent_and_names_the_overrides(gcode_env):
    event = _gcode_event(gcode_env.tmp, 'yolo')
    adapter = Adapter([])
    result = asyncio.run(prepared_intake.print_gcode_file(event, adapter, gcode_env.root, event.media_urls[0]))
    assert adapter.questions == []  # said yolo: nothing asked
    (target,) = gcode_env.starts
    assert target['yolo'] is True and Path(target['gcode']).read_text() == 'G1 X1 E1\n'
    assert any('Yolo overrode these checks:\n- G-code safety checks: no END_PRINT' in m for m in adapter.sent)
    assert any(m.startswith('Print started: hook.gcode') for m in adapter.sent)
    receipt = json.loads(next(gcode_env.root.glob('*.json')).read_text())
    assert receipt['status'] == 'STARTED' and receipt['yolo'] is True
    assert receipt['yolo_waivers'] == ['G-code safety checks: no END_PRINT']
    assert result['action'] == 'skip'


@pytest.mark.parametrize('pick, started', [(0, True), (1, False)])
def test_plain_gcode_drop_asks_instead_of_being_ignored(gcode_env, pick, started):
    event = _gcode_event(gcode_env.tmp, '')
    adapter = Adapter([pick])
    asyncio.run(prepared_intake.print_gcode_file(event, adapter, gcode_env.root, event.media_urls[0]))
    q = adapter.questions[0]
    assert q['choices'] == [prepared_intake.YOLO_GCODE_LABEL, print_next.CANCEL_LABEL]
    assert 'already-sliced G-code' in q['question']
    assert bool(gcode_env.starts) is started


def test_gcode_drop_from_a_non_approver_is_refused(gcode_env):
    event = _gcode_event(gcode_env.tmp, 'yolo')
    event.source.user_id = 'stranger'
    adapter = Adapter([])
    asyncio.run(prepared_intake.print_gcode_file(event, adapter, gcode_env.root, event.media_urls[0]))
    assert gcode_env.starts == []
    assert any('only a print approver' in m for m in adapter.sent)


def test_yolo_gcode_3mf_package_prints_plate_one(gcode_env):
    pkg = gcode_env.tmp / 'hook_supports_AD5X.gcode.3mf'
    with zipfile.ZipFile(pkg, 'w') as z:
        z.writestr('Metadata/plate_1.gcode', 'G1 X9 E1\n')
    event = _gcode_event(gcode_env.tmp, 'yolo')
    event.media_urls = [str(pkg)]
    adapter = Adapter([])
    asyncio.run(prepared_intake.print_gcode_file(event, adapter, gcode_env.root, str(pkg)))
    (target,) = gcode_env.starts
    assert Path(target['gcode']).name == 'hook_supports_AD5X.gcode'
    assert Path(target['gcode']).read_text() == 'G1 X9 E1\n'


# ---------------------------------------------------------------- chat: models

def _yolo_flow(env, text_prefix='yolo ', picks=()):
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u',
                       platform=S(value='discord')),
              message_id='m-yolo', text=text_prefix + 'https://www.printables.com/model/1-ghost')
    adapter = Adapter(list(picks))
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, __import__(
        'tests.pipeline.test_color_intake', fromlist=['state']).state(), env.root, timeout=1))
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    return adapter, result, receipt, event


def test_yolo_model_answers_every_question_out_loud_and_starts_with_the_flag(env, monkeypatch):
    monkeypatch.setattr(yolo, 'may_yolo', lambda user_id, platform='discord', cfg=None: True)
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy(V12_FIXTURE, dest))
    from print_handy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 1)
    started_targets = []
    real_start = print_next.start

    async def start(target):
        started_targets.append(dict(target))
        return await real_start(target)
    monkeypatch.setattr(print_next, 'start', start)
    adapter, result, receipt, event = _yolo_flow(env)
    assert adapter.questions == []  # no question reached the operator
    assert event.text == 'https://www.printables.com/model/1-ghost'
    answered = [m for m in adapter.sent if m.startswith('Yolo: ') and ' -> ' in m]
    assert answered, 'every auto-answer is posted'
    assert any(m.endswith('-> Print anyway (accept the risk)') for m in answered)
    assert receipt['status'] == 'STARTED'
    assert started_targets and started_targets[-1].get('yolo') is True
    assert yolo.ACTIVE.get() is False


def test_yolo_from_a_non_approver_runs_the_normal_intake(env, monkeypatch):
    monkeypatch.setattr(yolo, 'may_yolo', lambda user_id, platform='discord', cfg=None: False)
    # Normal flow answers: one color (1), slot 1 (0), Slice and print now (0).
    adapter, result, receipt, event = _yolo_flow(env, picks=[1, 0, 0])
    assert any('only a print approver can use it' in m for m in adapter.sent)
    assert adapter.questions, 'the normal questions were asked'
    assert receipt['status'] == 'STARTED'
