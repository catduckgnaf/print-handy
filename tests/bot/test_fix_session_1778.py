"""Repair tools must change geometry or say so, and a fix session accepts only a real new file (#1778)."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import numpy as np

from print_handy.bot import fix_session
from print_handy.core import mesh_repair, support_clearance
from print_handy.skills.print_repair import tools
from tests.core.test_layer_scan import box


@pytest.fixture(autouse=True)
def _clear_sessions():
    from print_handy.core import ai_fix
    fix_session._SESSIONS.clear()
    ai_fix.AI_CALLS.clear()
    yield
    fix_session._SESSIONS.clear()
    ai_fix.AI_CALLS.clear()


def _files(folder):
    return sorted(path.relative_to(folder).as_posix() for path in folder.rglob('*') if path.is_file())


def _workspace(tmp_path):
    folder = tmp_path / 'job'
    (folder / 'source').mkdir(parents=True)
    stl = folder / 'source' / 'part.stl'
    support_clearance._write_binary_stl(stl, box(0, 0, 0, 10, 10, 10))
    job_path = folder / 'JOB.json'
    job_path.write_text(json.dumps({
        'slug': 'part',
        'downloaded_file': str(stl.resolve()),
    }) + '\n', encoding='utf-8')
    return folder, job_path, stl


def _session(tmp_path):
    folder, job_path, stl = _workspace(tmp_path)
    session = fix_session.start_session('user', 'chat', 'thread', job_path, stl)
    assert session.skill_session is not None
    return folder, job_path, stl, session


def test_unimplemented_repair_does_not_report_success_for_an_unchanged_copy(tmp_path):
    """A stub repair returns ok false and does not leave a copy of the source."""
    folder, _job, stl, _session_obj = _session(tmp_path)
    before = stl.read_bytes()
    names = _files(folder)
    reply = tools.Session(folder).run('gusset_45', {
        'mesh': 'source/part.stl', 'overhang_region': [0, 0, 10, 10], 'wall_x': 0})
    assert reply['ok'] is False
    assert reply['result']['ok'] is False
    assert 'new_mesh' not in reply['result']
    assert 'nothing was changed' in reply['error']
    assert stl.read_bytes() == before
    assert _files(folder) == names

    repair = tools.Session(folder).run('repair_mesh', {'mesh': 'source/part.stl'})
    assert repair['ok'] is False
    assert 'nothing' in repair['error']
    assert 'repair_mesh' not in repair['error']  # the missing attribute must not be what we call
    assert stl.read_bytes() == before
    assert _files(folder) == names


def test_extend_with_nothing_to_move_does_not_leave_a_copy(tmp_path):
    folder, _job, stl, _session_obj = _session(tmp_path)
    before = stl.read_bytes()
    names = _files(folder)
    reply = tools.Session(folder).run('extend_underside', {'mesh': 'source/part.stl', 'mm': 0.4})
    assert reply['ok'] is False
    assert 'nothing was changed' in reply['error']
    assert 'new_mesh' not in reply.get('result', {})
    assert stl.read_bytes() == before
    assert _files(folder) == names


def test_sink_and_trim_change_geometry_and_keep_the_original(tmp_path):
    folder, _job, stl, _session_obj = _session(tmp_path)
    before = stl.read_bytes()
    sink = tools.Session(folder).run('sink', {'mesh': 'source/part.stl', 'mm': 1})
    assert sink['ok'] is True, sink
    result = sink['result']
    assert result['ok'] is True
    assert result['volume_removed_mm3'] == pytest.approx(100, abs=1)
    sunk = folder / result['new_mesh']
    assert sunk.is_file() and sunk.resolve() != stl.resolve()
    assert sunk.read_bytes() != before
    assert mesh_repair.load_triangles(sunk)[:, :, 2].min() == pytest.approx(-1, abs=0.05)
    assert stl.read_bytes() == before

    trim = tools.Session(folder).run('trim_below_plane', {
        'mesh': 'source/part.stl', 'region': [0, 0, 10, 10], 'z': 2})
    assert trim['ok'] is True, trim
    assert trim['result']['volume_removed_mm3'] == pytest.approx(200, abs=1)
    trimmed = folder / trim['result']['new_mesh']
    assert trimmed.is_file() and trimmed.resolve() != stl.resolve()
    assert mesh_repair.load_triangles(trimmed)[:, :, 2].min() == pytest.approx(2, abs=0.05)
    assert stl.read_bytes() == before

    partial = tools.Session(folder).run('trim_below_plane', {
        'mesh': 'source/part.stl', 'region': [0, 0, 1, 1], 'z': 2})
    assert partial['ok'] is False
    assert 'nothing was changed' in partial['error']
    assert stl.read_bytes() == before
    assert not list(folder.rglob('*partial*'))


def test_trim_refuses_triangle_crossing_the_region_at_the_cut_plane(tmp_path):
    folder, _job, stl, _session_obj = _session(tmp_path)
    # Two vertices are under z=2 and inside [0,10] but the clipped edge
    # intersects z=2 at x=11, outside the requested local region.
    support_clearance._write_binary_stl(stl, np.array([[
        [0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [15.0, 5.0, 10.0]]]))
    before = stl.read_bytes()
    result = tools.Session(folder).run('trim_below_plane', {
        'mesh': 'source/part.stl', 'region': [0, 0, 10, 10], 'z': 2})
    assert result['ok'] is False
    assert 'outside the requested region' in result['error']
    assert stl.read_bytes() == before
    assert not list(stl.parent.glob('part-trimmed*'))


def test_process_turn_not_ok_tool_does_not_claim_the_file_changed(tmp_path):
    _folder, _job, stl, session = _session(tmp_path)
    before = stl.read_bytes()

    def ai_complete(bound, message, tool_schemas):
        assert bound is session
        assert message == 'please fix the sag'
        assert any(item['function']['name'] == 'sink' for item in tool_schemas)

        def run(name, arguments=None):
            assert name == 'sink'
            return {'ok': False, 'error': 'sink did not change the mesh',
                    'result': {'ok': False, 'error': 'sink did not change the mesh'}}

        bound.skill_session.run = run
        return {'content': 'Done, the file is fixed.',
                'tool_calls': [{'name': 'sink', 'arguments': {'mesh': 'source/part.stl', 'mm': 1}}]}

    out = fix_session.process_turn(session, 'please fix the sag', ai_complete=ai_complete)
    assert out['accepted'] is False and out['cancelled'] is False
    assert 'did not change the file' in out['reply'].lower()
    assert 'was accepted' not in out['reply'].lower()
    assert 'is fixed' not in out['reply'].lower()
    assert fix_session._same_path(session.current_file, stl)
    assert session.accepted is False and session.active()
    assert stl.read_bytes() == before


def test_process_turn_accept_with_no_new_file_does_not_accept(tmp_path):
    _folder, job_path, stl, session = _session(tmp_path)
    before = stl.read_bytes()
    job_before = job_path.read_text(encoding='utf-8')

    def boom(*_args, **_kwargs):
        raise AssertionError('accept with no fixed file must not call the model or a tool')

    out = fix_session.process_turn(session, 'accept', ai_complete=boom)
    assert out['accepted'] is False and out['cancelled'] is False
    assert 'no fixed file' in out['reply'].lower()
    assert 'unchanged' in out['reply'].lower()
    assert fix_session.SESSION_END_ACCEPT not in out['reply']
    assert 'was accepted' not in out['reply'].lower()
    assert session.accepted is False and session.active()
    assert stl.read_bytes() == before
    assert job_path.read_text(encoding='utf-8') == job_before


def test_process_turn_accept_after_a_successful_tool_updates_the_job(tmp_path):
    _folder, job_path, stl, session = _session(tmp_path)
    before = stl.read_bytes()

    def ai_complete(_bound, _message, tool_schemas):
        assert any(item['function']['name'] == 'sink' for item in tool_schemas)
        return {'content': 'Sinking the model onto the plate.',
                'tool_calls': [{'name': 'sink', 'arguments': {'mesh': 'source/part.stl', 'mm': 1}}]}

    turn = fix_session.process_turn(session, 'sink it', ai_complete=ai_complete)
    assert turn['accepted'] is False
    assert 'source/part-sunk.stl' in turn['reply']
    assert not fix_session._same_path(session.current_file, stl)
    assert session.current_file.is_file()
    assert stl.read_bytes() == before

    def boom(*_args, **_kwargs):
        raise AssertionError('accept must not call the model')

    # No direct acceptance before the shared model intake has verified the copy.
    premature = fix_session.process_turn(session, 'accept', ai_complete=boom)
    assert premature['accepted'] is False and session.active()
    assert Path(json.loads(job_path.read_text())['downloaded_file']).resolve() == stl.resolve()
    done = fix_session.process_turn(session, 'accept', ai_complete=boom,
                                    resume_intake=lambda _session: True)
    assert done['accepted'] is True and done['cancelled'] is False
    assert done['reply'] == fix_session.SESSION_END_ACCEPT
    assert session.accepted is True and not session.active()
    job = json.loads(job_path.read_text(encoding='utf-8'))
    assert Path(job['downloaded_file']).resolve() == session.current_file.resolve()
    assert Path(job['downloaded_file']).resolve() != stl.resolve()
    assert stl.read_bytes() == before


def test_process_turn_ai_failure_leaves_the_original_unchanged(tmp_path, caplog):
    _folder, _job, stl, session = _session(tmp_path)
    before = stl.read_bytes()

    def ai_complete(_bound, _message, _tools):
        raise RuntimeError('model down')

    with caplog.at_level('ERROR'):
        out = fix_session.process_turn(session, 'fix the sag', ai_complete=ai_complete)
    assert out['accepted'] is False
    assert 'repair step failed' in out['reply'].lower()
    assert 'unchanged' in out['reply'].lower()
    assert 'was accepted' not in out['reply'].lower()
    assert fix_session._same_path(session.current_file, stl)
    assert stl.read_bytes() == before
    assert 'AI fix step failed' in caplog.text


def test_process_turn_accept_does_not_claim_success_when_the_job_write_fails(tmp_path, monkeypatch):
    _folder, job_path, stl, session = _session(tmp_path)
    before = stl.read_bytes()

    def ai_complete(_bound, _message, _tools):
        return {'content': '', 'tool_calls': [
            {'name': 'sink', 'arguments': {'mesh': 'source/part.stl', 'mm': 1}}]}

    turn = fix_session.process_turn(session, 'sink it', ai_complete=ai_complete)
    assert 'source/part-sunk.stl' in turn['reply']

    def fail(_folder, _job):
        raise OSError('read only')

    monkeypatch.setattr('print_handy.jobs.store.write_job', fail)
    out = fix_session.process_turn(session, 'accept', ai_complete=ai_complete,
                                   resume_intake=lambda _session: True)
    assert out['accepted'] is False
    assert session.accepted is False and session.active()
    assert 'not accepted' in out['reply'].lower()
    assert fix_session.SESSION_END_ACCEPT not in out['reply']
    assert 'was accepted' not in out['reply'].lower()
    job = json.loads(job_path.read_text(encoding='utf-8'))
    assert Path(job['downloaded_file']).resolve() == stl.resolve()
    assert stl.read_bytes() == before


def test_tools_unavailable_does_not_claim_a_repair(tmp_path):
    _folder, _job, stl, session = _session(tmp_path)
    session.skill_session = None

    def boom(*_args, **_kwargs):
        raise AssertionError('no skill session must not call the model')

    out = fix_session.process_turn(session, 'fix this', ai_complete=boom)
    assert out['accepted'] is False
    assert 'unavailable' in out['reply'].lower()
    assert 'repaired file' not in out['reply'].lower()
    assert 'was accepted' not in out['reply'].lower()
    assert fix_session._same_path(session.current_file, stl)


def test_cross_platform_fix_handler_routes_followups_and_resumes_normal_intake(tmp_path, monkeypatch):
    from print_handy.bot.handlers import fix as handler
    from print_handy.bot.handlers import model_link

    folder, job_path, stl = _workspace(tmp_path)
    original = stl.read_bytes()
    source = SimpleNamespace(user_id='owner', chat_id='channel-1', thread_id='thread-9')
    gateway = SimpleNamespace(_is_user_authorized_for_source=lambda _source: True)
    messages = []

    class Adapter:
        async def send(self, **kwargs):
            messages.append(kwargs.get('content'))
            return True

    monkeypatch.setattr(handler, '_is_print_discord_event', lambda _event: True)
    monkeypatch.setattr(handler, '_adapter_for', lambda _gateway, _source: Adapter())
    monkeypatch.setattr(handler.ai_routes, 'ready', lambda _tier: True)
    monkeypatch.setattr(handler, '_new_session', lambda _event:
                        fix_session.start_session('owner', 'channel-1', 'thread-9',
                                                  job_path, stl))
    monkeypatch.setattr(handler, '_complete', lambda bound, msg, schemas: {
        'content': 'Making a copy.', 'tool_calls': [
            {'name': 'sink', 'arguments': {'mesh': 'source/part.stl', 'mm': 1}}]})

    def event(text, as_user=source):
        return SimpleNamespace(source=as_user, text=text, media_urls=[], internal=False)

    assert asyncio.run(handler.fix_dispatch(event('/fix'), gateway))['reason'] == 'repair session started'
    owner_session = fix_session.get_session('owner', 'channel-1', 'thread-9')
    assert owner_session is not None
    assert asyncio.run(handler.fix_dispatch(event('sink it'), gateway))['reason'] == 'repair session handled'
    assert owner_session.current_file.is_file()
    assert owner_session.current_file.read_bytes() != original
    assert stl.read_bytes() == original

    other = SimpleNamespace(user_id='stranger', chat_id='channel-1', thread_id='thread-9')
    assert asyncio.run(handler.fix_dispatch(event('accept', other), gateway)) is None

    calls = []

    async def confirmed_intake(reentry, _gateway):
        calls.append(reentry.media_urls)
        assert reentry.text == ''
        assert reentry.source is source
        return {'action': 'skip', 'reason': 'sliced and start handled by button flow'}

    monkeypatch.setattr(model_link, 'model_link_dispatch', confirmed_intake)
    assert asyncio.run(handler.fix_dispatch(event('accept'), gateway))['reason'] == 'repair accepted'
    assert calls == [[str(owner_session.current_file)]]
    assert owner_session.accepted and not owner_session.active()
    assert Path(json.loads(job_path.read_text())['downloaded_file']).resolve() == owner_session.current_file.resolve()
    assert stl.read_bytes() == original


def test_fix_handler_does_not_accept_a_repaired_file_when_intake_refuses(tmp_path, monkeypatch):
    from print_handy.bot.handlers import fix as handler
    from print_handy.bot.handlers import model_link

    _folder, job_path, stl, session = _session(tmp_path)
    session.current_file = stl.parent / 'part-sunk.stl'
    support_clearance._write_binary_stl(session.current_file, box(0, 0, -1, 10, 10, 10))
    source = SimpleNamespace(user_id='user', chat_id='chat', thread_id='thread')
    gateway = SimpleNamespace(_is_user_authorized_for_source=lambda _source: True)
    messages = []

    class Adapter:
        async def send(self, **kwargs):
            messages.append(kwargs['content'])

    monkeypatch.setattr(handler, '_is_print_discord_event', lambda _event: True)
    monkeypatch.setattr(handler, '_adapter_for', lambda _gateway, _source: Adapter())

    async def rejected(_event, _gateway):
        return {'action': 'skip', 'reason': 'slice refused: failed'}

    monkeypatch.setattr(model_link, 'model_link_dispatch', rejected)
    out = asyncio.run(handler.fix_dispatch(
        SimpleNamespace(source=source, text='accept', media_urls=[]), gateway))
    assert out['reason'] == 'repair intake incomplete'
    assert not session.accepted and session.active()
    assert Path(json.loads(job_path.read_text())['downloaded_file']).resolve() == stl.resolve()
    assert messages and 'nothing was accepted' in messages[-1]


def test_failed_uploaded_model_offers_repair_then_starts_scoped_session(tmp_path, monkeypatch):
    from print_handy.bot.handlers import fix as handler

    _folder, job_path, stl = _workspace(tmp_path)
    source = SimpleNamespace(user_id='owner', chat_id='private', thread_id='thread')
    event = SimpleNamespace(source=source, text='', media_urls=[str(stl)])
    gateway = SimpleNamespace(_session_key_for_source=lambda _source: 'key')
    sent = []

    class Adapter:
        async def send(self, **kwargs):
            sent.append(kwargs['content'])

    choices = []

    async def choice(adapter, src, metadata, kind, prompt, labels, *args, **kwargs):
        choices.append((kind, labels))
        return fix_session.WORK_WITH_AI_LABEL

    monkeypatch.setattr(handler.ai_routes, 'ready', lambda _tier: True)
    monkeypatch.setattr(handler, '_source_file', lambda _event: stl)
    monkeypatch.setattr(handler, '_new_session',
                        lambda _event: fix_session.start_session('owner', 'private', 'thread',
                                                                  job_path, stl))
    monkeypatch.setattr(handler, '_confirm', choice)
    result = asyncio.run(handler.offer_failed_intake(event, gateway, Adapter()))
    assert result['reason'] == 'repair session started after slice refusal'
    assert choices[0][1][0] == fix_session.WORK_WITH_AI_LABEL
    assert fix_session.get_session('owner', 'private', 'thread') is not None
    assert sent and 'until you accept or cancel' in sent[-1]


def test_failed_intake_never_offers_an_unavailable_original_or_unconfigured_ai(tmp_path, monkeypatch):
    from print_handy.bot.handlers import fix as handler
    source = SimpleNamespace(user_id='owner', chat_id='private', thread_id='thread')
    event = SimpleNamespace(source=source, text='', media_urls=[])
    monkeypatch.setattr(handler.ai_routes, 'ready', lambda _tier: False)
    monkeypatch.setattr(handler, '_source_file', lambda _event: None)
    result = asyncio.run(handler.offer_failed_intake(event, SimpleNamespace(), None))
    assert result is None
    assert not fix_session._SESSIONS


def test_one_release_plugin_imports_and_registers_the_fix_handler():
    """The copied legacy handler path must not execute stripped interface imports."""
    import importlib.util
    old_plugin = Path(__file__).parents[2] / "plugins" / "print-feedback" / "__init__.py"
    spec = importlib.util.spec_from_file_location("print_feedback_fix_1778", old_plugin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.fix_dispatch in module._DISPATCH_HOOKS
    assert callable(module.offer_failed_intake)


def test_acceptance_waits_out_a_running_turn_and_cannot_repoint_under_it(tmp_path):
    """While a turn holds the session lock the chat acceptance refuses and changes nothing (#1778)."""
    from print_handy.bot.handlers import fix as handler

    _folder, job_path, stl, session = _session(tmp_path)
    session.current_file = stl.parent / 'part-sunk.stl'
    support_clearance._write_binary_stl(session.current_file, box(0, 0, -1, 10, 10, 10))
    messages = []

    class Adapter:
        async def send(self, **kwargs):
            messages.append(kwargs['content'])

    event = SimpleNamespace(source=SimpleNamespace(user_id='user', chat_id='chat', thread_id='thread'),
                            text='accept', media_urls=[])
    with session.lock:
        out = asyncio.run(handler._accept(event, SimpleNamespace(), Adapter(), session))
    assert out['reason'] == 'repair step still running'
    assert 'nothing was accepted' in messages[-1].lower()
    assert not session.accepted and session.active()
    assert Path(json.loads(job_path.read_text())['downloaded_file']).resolve() == stl.resolve()
    assert not session.lock.locked()


# ------------------------------------------------------------ #1778 review blockers

def _discord_source(user='owner', chat='channel-1', thread='thread-9'):
    return SimpleNamespace(user_id=user, chat_id=chat, thread_id=thread,
                           platform=SimpleNamespace(value='discord'), is_bot=False)


class _Recorder:
    def __init__(self):
        self.messages = []

    async def send(self, **kwargs):
        self.messages.append(kwargs.get('content'))
        return True


def _gateway(adapter, authorized=True):
    return SimpleNamespace(_is_user_authorized_for_source=lambda _source: authorized,
                           _delivery_adapter_for=lambda _source: adapter,
                           _session_key_for_source=lambda source: f'key:{source.chat_id}')


def _sink_plan(*_args):
    return {'content': 'Sinking it 1 mm. Shall I also trim the base?', 'tool_calls': [
        {'name': 'sink', 'arguments': {'mesh': 'source/part.stl', 'mm': 1}}]}


@pytest.mark.parametrize('phrase', [
    'how do I fix this layer shift?',
    'How do I fix this layer shift?',
    'why did this happen, can you fix this print?',
    'fix this printer',
    'repair the printer',
    'can you make it print faster',
    'make it print',
    'what causes a layer shift?',
])
def test_fix_instruction_keeps_the_985_question_and_printer_guards(phrase):
    """Only the shared #978 repair reading starts a session; no substring shortcut."""
    from print_handy.core import ai_fix
    assert fix_session.is_fix_instruction(phrase) is False
    assert fix_session.is_fix_instruction(phrase) == ai_fix.split_repair_request(phrase)[0]


@pytest.mark.parametrize('phrase', ['fix this', 'fix this for my printer', 'repair it',
                                    'fix it and make it 20% bigger'])
def test_fix_instruction_accepts_the_978_repair_requests(phrase):
    assert fix_session.is_fix_instruction(phrase) is True


def _router_with(real_names, monkeypatch=None):
    """The real dispatch order, with every hook except ``real_names`` a recorder."""
    from print_handy.bot.router import Router
    real = Router()
    calls = []

    def recorder(name):
        def hook(_event, _gateway):
            calls.append(name)
            return None
        hook.__name__ = name
        return hook

    hooks = [hook if hook.__name__ in real_names else recorder(hook.__name__)
             for hook in real.dispatch_hooks]

    class _Plugin:
        def register(self, ctx):
            for hook in hooks:
                ctx.register_hook('pre_gateway_dispatch', hook)

    router = Router(plugin=_Plugin())
    assert router.handler_names() == real.handler_names()
    return router, calls


def test_router_order_puts_fix_before_troubleshoot_and_edit():
    from print_handy.bot.router import Router
    names = Router().handler_names()
    assert names.index('fix_dispatch') < names.index('troubleshoot_dispatch')
    assert names.index('fix_dispatch') < names.index('model_link_dispatch')
    assert names.index('fix_dispatch') < names.index('edit_dispatch')


@pytest.mark.parametrize('attach', [False, True])
def test_a_question_about_a_defect_passes_the_fix_handler_in_the_real_order(tmp_path, monkeypatch, attach):
    """"How do I fix this layer shift?" reaches troubleshoot and the later handlers,
    with or without a model attached and with the AI ready."""
    from print_handy.bot.handlers import fix as handler
    _folder, _job, stl = _workspace(tmp_path)
    adapter = _Recorder()
    monkeypatch.setattr(handler.ai_routes, 'ready', lambda _tier, *_a: True)
    monkeypatch.setattr(handler, '_new_session', lambda _event: pytest.fail('no session for a question'))
    router, calls = _router_with({'fix_dispatch'})
    event = SimpleNamespace(source=_discord_source(), text='How do I fix this layer shift?',
                            media_urls=[str(stl)] if attach else [], internal=False, metadata={})
    assert asyncio.run(router.dispatch(event, _gateway(adapter))) is None
    assert 'troubleshoot_dispatch' in calls and 'edit_dispatch' in calls
    assert adapter.messages == []
    assert not fix_session._SESSIONS


@pytest.mark.parametrize('text', ['fix this printer', 'can you make it print faster'])
def test_printer_requests_are_not_claimed_by_the_fix_handler(tmp_path, monkeypatch, text):
    from print_handy.bot.handlers import fix as handler
    _folder, _job, stl = _workspace(tmp_path)
    adapter = _Recorder()
    monkeypatch.setattr(handler.ai_routes, 'ready', lambda _tier, *_a: True)
    event = SimpleNamespace(source=_discord_source(), text=text, media_urls=[str(stl)],
                            internal=False, metadata={})
    assert asyncio.run(handler.fix_dispatch(event, _gateway(adapter))) is None
    assert adapter.messages == [] and not fix_session._SESSIONS


def test_plain_fix_this_on_the_current_job_reaches_the_978_chat_repair(monkeypatch):
    """No live session and no model attached: the edit handler's repair branch owns it."""
    from print_handy.bot import edit_chat
    from print_handy.bot.handlers import fix as handler
    adapter = _Recorder()
    monkeypatch.setattr(handler.ai_routes, 'ready', lambda _tier, *_a: True)
    monkeypatch.setattr(handler, '_new_session', lambda _event: pytest.fail('no session without a model'))
    reached = []

    async def repair_flow(event, _gateway, source, _adapter):
        reached.append(event.text)
        return {'action': 'skip', 'reason': 'repair: chat repair ran'}

    monkeypatch.setattr(edit_chat, '_repair_flow', repair_flow)
    router, calls = _router_with({'fix_dispatch', 'edit_dispatch'})
    event = SimpleNamespace(source=_discord_source(), text='fix this', media_urls=[],
                            internal=False, metadata={})
    out = asyncio.run(router.dispatch(event, _gateway(adapter)))
    assert out == {'action': 'skip', 'reason': 'repair: chat repair ran'}
    assert reached == ['fix this']
    assert not fix_session._SESSIONS


def test_ai_not_ready_lets_everything_but_the_command_fall_through(tmp_path, monkeypatch):
    from print_handy.bot.handlers import fix as handler
    _folder, _job, stl = _workspace(tmp_path)
    adapter = _Recorder()
    monkeypatch.setattr(handler.ai_routes, 'ready', lambda _tier, *_a: False)
    gateway = _gateway(adapter)
    drop = SimpleNamespace(source=_discord_source(), text='fix this', media_urls=[str(stl)],
                           internal=False, metadata={})
    assert asyncio.run(handler.fix_dispatch(drop, gateway)) is None
    command = SimpleNamespace(source=_discord_source(), text='/fix', media_urls=[str(stl)],
                              internal=False, metadata={})
    assert asyncio.run(handler.fix_dispatch(command, gateway))['reason'] == 'repair AI unavailable'
    assert not fix_session._SESSIONS


def test_fix_this_with_a_model_attached_starts_a_session(tmp_path, monkeypatch):
    from print_handy.bot.handlers import fix as handler
    _folder, job_path, stl = _workspace(tmp_path)
    adapter = _Recorder()
    monkeypatch.setattr(handler.ai_routes, 'ready', lambda _tier, *_a: True)
    monkeypatch.setattr(handler, '_new_session', lambda _event: fix_session.start_session(
        'owner', 'channel-1', 'thread-9', job_path, stl))
    seen = []

    def complete(bound, message, schemas):
        seen.append(message)
        return _sink_plan()

    monkeypatch.setattr(handler, '_complete', complete)
    event = SimpleNamespace(source=_discord_source(), text='fix this', media_urls=[str(stl)],
                            internal=False, metadata={})
    assert asyncio.run(handler.fix_dispatch(event, _gateway(adapter)))['reason'] == 'repair session started'
    # The request that opened the session is its first turn, not dropped.
    assert seen == ['fix this']
    session = fix_session.get_session('owner', 'channel-1', 'thread-9')
    assert session is not None and not fix_session._same_path(session.current_file, stl)
    assert 'The repaired file is source/part-sunk.stl.' in adapter.messages[-1]


def test_fix_command_with_an_instruction_runs_it_as_the_first_turn(tmp_path, monkeypatch):
    from print_handy.bot.handlers import fix as handler
    _folder, job_path, stl = _workspace(tmp_path)
    adapter = _Recorder()
    seen = []
    monkeypatch.setattr(handler.ai_routes, 'ready', lambda _tier, *_a: True)
    monkeypatch.setattr(handler, '_new_session', lambda _event: fix_session.start_session(
        'owner', 'channel-1', 'thread-9', job_path, stl))
    monkeypatch.setattr(handler, '_complete', lambda b, m, s: seen.append(m) or _sink_plan())
    for text in ('/fix', fix_session.WORK_WITH_AI_LABEL):
        fix_session._SESSIONS.clear()
        event = SimpleNamespace(source=_discord_source(), text=text, media_urls=[str(stl)],
                                internal=False, metadata={})
        assert asyncio.run(handler.fix_dispatch(event, _gateway(adapter)))['reason'] == 'repair session started'
    assert seen == []
    fix_session._SESSIONS.clear()
    event = SimpleNamespace(source=_discord_source(), text='/fix sink it 1 mm', media_urls=[str(stl)],
                            internal=False, metadata={})
    assert asyncio.run(handler.fix_dispatch(event, _gateway(adapter)))['reason'] == 'repair session started'
    assert seen == ['sink it 1 mm']


def _ai_cfg(monkeypatch, **ai):
    from print_handy import settings
    monkeypatch.setattr(settings, 'current', lambda: {'ai': ai, 'paths': {'cache': 'unused'}})


def test_a_user_off_the_ai_allowlist_cannot_start_or_be_offered(tmp_path, monkeypatch):
    from print_handy.bot.handlers import fix as handler
    _folder, job_path, stl = _workspace(tmp_path)
    _ai_cfg(monkeypatch, allowed_users=['someone-else'])
    adapter = _Recorder()
    monkeypatch.setattr(handler.ai_routes, 'ready', lambda _tier, *_a: True)
    monkeypatch.setattr(handler.ai_routes, 'call', lambda *a, **k: pytest.fail('no model call'))
    monkeypatch.setattr(handler, '_new_session', lambda _event: pytest.fail('no session'))

    async def confirm(*_a, **_k):
        pytest.fail('no offer for a user off the allowlist')

    monkeypatch.setattr(handler, '_confirm', confirm)
    for text in ('/fix', 'fix this'):
        event = SimpleNamespace(source=_discord_source(), text=text, media_urls=[str(stl)],
                                internal=False, metadata={})
        out = asyncio.run(handler.fix_dispatch(event, _gateway(adapter)))
        assert out['reason'] == 'repair: not on the AI allowlist'
    assert 'not on the AI allowlist' in adapter.messages[-1]
    failed = SimpleNamespace(source=_discord_source(), text='', media_urls=[str(stl)], metadata={})
    assert asyncio.run(handler.offer_failed_intake(failed, _gateway(adapter), adapter)) is None
    assert not fix_session._SESSIONS


def test_a_turn_past_the_hourly_limit_is_refused_and_calls_nothing(tmp_path, monkeypatch):
    from print_handy.bot.handlers import fix as handler
    _folder, job_path, stl = _workspace(tmp_path)
    _ai_cfg(monkeypatch, edit_requests_per_hour=1)
    adapter = _Recorder()
    calls = []

    def call(tier, messages, **_kwargs):
        calls.append(tier)
        return json.dumps(_sink_plan()), 'heavy'

    monkeypatch.setattr(handler.ai_routes, 'ready', lambda _tier, *_a: True)
    monkeypatch.setattr(handler.ai_routes, 'call', call)
    monkeypatch.setattr(handler, '_new_session', lambda _event: fix_session.start_session(
        'owner', 'channel-1', 'thread-9', job_path, stl))
    gateway = _gateway(adapter)

    def event(text):
        return SimpleNamespace(source=_discord_source(), text=text, media_urls=[],
                               internal=False, metadata={})

    # Starting the session does not spend the one hourly call.
    assert asyncio.run(handler.fix_dispatch(event('/fix'), gateway))['reason'] == 'repair session started'
    assert asyncio.run(handler.fix_dispatch(event('sink it'), gateway))['reason'] == 'repair session handled'
    assert calls == ['heavy']
    out = asyncio.run(handler.fix_dispatch(event('sink it more'), gateway))
    assert out['reason'] == 'repair: AI edit rate limit reached'
    assert calls == ['heavy']
    assert 'rate limit' in adapter.messages[-1] and 'Nothing was changed' in adapter.messages[-1]


def test_a_held_session_lock_answers_busy_at_once_without_a_worker(tmp_path, monkeypatch):
    """A turn or cancel during a slow acceptance never blocks a shared worker thread."""
    import threading
    from print_handy.bot.handlers import fix as handler
    _folder, job_path, stl, session = _session(tmp_path)
    adapter = _Recorder()
    monkeypatch.setattr(handler, '_complete', lambda *a: pytest.fail('no model call while busy'))
    real_turn = fix_session.process_turn
    monkeypatch.setattr(fix_session, 'process_turn',
                        lambda *a, **k: pytest.fail('no worker turn while busy'))
    gateway = _gateway(adapter)
    source = _discord_source('user', 'chat', 'thread')
    before = set(threading.enumerate())
    with session.lock:
        for text in ('sink it', 'cancel'):
            event = SimpleNamespace(source=source, text=text, media_urls=[], internal=False, metadata={})
            out = asyncio.run(asyncio.wait_for(handler.fix_dispatch(event, gateway), 2))
            assert out['reason'] == 'repair step still running'
            assert adapter.messages[-1] == fix_session.BUSY_REPLY
        # process_turn itself never waits on the lock either.
        result = {}
        worker = threading.Thread(target=lambda: result.update(
            real_turn(session, 'cancel', ai_complete=lambda *_: None)))
        worker.start()
        worker.join(2)
        assert not worker.is_alive()
        assert result['busy'] is True and result['reply'] == fix_session.BUSY_REPLY
    assert not [t for t in set(threading.enumerate()) - before if t.is_alive()]
    assert session.active() and not session.cancelled
    assert fix_session._same_path(session.current_file, stl)


def test_accept_during_a_running_turn_is_refused_and_cannot_repoint_under_it(tmp_path, monkeypatch):
    """A real concurrent fix_dispatch("accept") while the model call is in flight."""
    import threading
    from print_handy.bot.handlers import fix as handler
    from print_handy.bot.handlers import model_link
    _folder, job_path, stl = _workspace(tmp_path)
    adapter = _Recorder()
    entered, release = threading.Event(), threading.Event()

    def slow_complete(bound, message, schemas):
        entered.set()
        assert release.wait(10)
        return _sink_plan()

    async def intake(_event, _gateway):
        pytest.fail('accept must not run intake while a turn is running')

    monkeypatch.setattr(handler.ai_routes, 'ready', lambda _tier, *_a: True)
    monkeypatch.setattr(handler, '_complete', slow_complete)
    monkeypatch.setattr(model_link, 'model_link_dispatch', intake)
    session = fix_session.start_session('owner', 'channel-1', 'thread-9', job_path, stl)
    gateway = _gateway(adapter)

    def event(text):
        return SimpleNamespace(source=_discord_source(), text=text, media_urls=[],
                               internal=False, metadata={})

    async def scenario():
        turn = asyncio.create_task(handler.fix_dispatch(event('sink it'), gateway))
        assert await asyncio.to_thread(entered.wait, 10)
        accepted = await asyncio.wait_for(handler.fix_dispatch(event('accept'), gateway), 2)
        release.set()
        return accepted, await asyncio.wait_for(turn, 20)

    accepted, turned = asyncio.run(scenario())
    assert accepted['reason'] == 'repair step still running'
    assert turned['reason'] == 'repair session handled'
    assert session.active() and not session.accepted
    assert not fix_session._same_path(session.current_file, stl)
    assert Path(json.loads(job_path.read_text())['downloaded_file']).resolve() == stl.resolve()
    assert not session.lock.locked()


def test_a_failed_slice_during_accept_does_not_offer_a_second_session(tmp_path, monkeypatch):
    """The real model_link offer path sees "slice failed:" on the re-entered intake."""
    from print_handy.bot.handlers import fix as handler
    from print_handy.bot.handlers import model_link
    from print_handy.core import prepared_intake, printer_state
    _folder, job_path, stl = _workspace(tmp_path)
    session = fix_session.start_session('owner', 'channel-1', 'thread-9', job_path, stl)
    session.current_file = stl.parent / 'part-sunk.stl'
    support_clearance._write_binary_stl(session.current_file, box(0, 0, -1, 10, 10, 10))
    adapter = _Recorder()
    offers, intakes = [], []

    async def confirm(*args, **kwargs):
        offers.append(args)
        return fix_session.WORK_WITH_AI_LABEL

    async def prepare_link(event, *_args, **_kwargs):
        intakes.append(list(event.media_urls))
        return {'action': 'skip', 'reason': 'slice failed: no layers'}

    monkeypatch.setattr(handler.ai_routes, 'ready', lambda _tier, *_a: True)
    monkeypatch.setattr(handler, '_confirm', confirm)
    monkeypatch.setattr(handler, '_new_session', lambda _event: pytest.fail('no second session'))
    monkeypatch.setattr(prepared_intake, 'prepare_link', prepare_link)
    monkeypatch.setattr(printer_state, 'load', lambda **_kwargs: {})
    monkeypatch.setattr(model_link, '_state_dir', lambda: tmp_path / 'state')
    monkeypatch.setattr(model_link, '_undecided_printer_ids', lambda *_a: [])
    monkeypatch.setattr(model_link, '_routed_printer_id', lambda *_a: None)
    event = SimpleNamespace(source=_discord_source(), text='accept', media_urls=[],
                            internal=False, metadata={})
    out = asyncio.run(handler.fix_dispatch(event, _gateway(adapter)))
    assert intakes == [[str(session.current_file)]]
    assert out['reason'] == 'repair intake incomplete'
    assert offers == []
    assert not any('Work on a repaired copy' in (m or '') for m in adapter.messages)
    assert fix_session._SESSIONS[session.key] is session and session.active()
    assert 'stays open' in adapter.messages[-1]
    assert Path(json.loads(job_path.read_text())['downloaded_file']).resolve() == stl.resolve()


def test_yes_after_a_repair_is_an_ordinary_turn_not_an_accept(tmp_path, monkeypatch):
    from print_handy.bot.handlers import fix as handler
    from print_handy.bot.handlers import model_link
    _folder, job_path, stl = _workspace(tmp_path)
    adapter = _Recorder()
    seen = []

    def complete(bound, message, schemas):
        seen.append(message)
        return _sink_plan() if len(seen) == 1 else {'content': 'Done thinking.', 'tool_calls': []}

    async def intake(_event, _gateway):
        pytest.fail('"yes" must not re-enter intake')

    monkeypatch.setattr(handler.ai_routes, 'ready', lambda _tier, *_a: True)
    monkeypatch.setattr(handler, '_complete', complete)
    monkeypatch.setattr(model_link, 'model_link_dispatch', intake)
    session = fix_session.start_session('owner', 'channel-1', 'thread-9', job_path, stl)
    gateway = _gateway(adapter)
    for text in ('sink it', 'yes', 'do it'):
        event = SimpleNamespace(source=_discord_source(), text=text, media_urls=[],
                                internal=False, metadata={})
        assert asyncio.run(handler.fix_dispatch(event, gateway))['reason'] == 'repair session handled'
    assert seen == ['sink it', 'yes', 'do it']
    assert session.active() and not session.accepted
    assert fix_session._ACCEPT == frozenset({'accept'})


def test_model_reply_is_cleaned_and_errors_never_show_absolute_paths(tmp_path):
    _folder, _job, stl, session = _session(tmp_path)

    def complete(bound, _message, _schemas):
        def run(name, arguments=None):
            raise RuntimeError(f'cannot open {stl.resolve()}')
        bound.skill_session.run = run
        return {'content': 'Hi <@123> @everyone see https://evil.example/x',
                'tool_calls': [{'name': 'sink', 'arguments': {}}]}

    out = fix_session.process_turn(session, 'sink it', ai_complete=complete)
    assert str(stl.resolve().parent) not in out['reply']
    assert 'sink failed' in out['reply']
    plain = fix_session.process_turn(
        session, 'say hi', ai_complete=lambda *_: {
            'content': 'Hi <@123> @everyone see https://evil.example/x', 'tool_calls': []})
    assert '<@123>' not in plain['reply'] and '@everyone' not in plain['reply']
    assert 'https://' not in plain['reply']


def test_complete_tolerates_a_fenced_json_plan(tmp_path, monkeypatch):
    from print_handy.bot.handlers import fix as handler
    _folder, _job, _stl, session = _session(tmp_path)
    fenced = '```json\n' + json.dumps(_sink_plan()) + '\n```'
    monkeypatch.setattr(handler.ai_routes, 'call', lambda *a, **k: (fenced, 'heavy'))
    plan = handler._complete(session, 'sink it', [])
    assert plan['tool_calls'][0]['name'] == 'sink'


def test_a_failed_uploaded_slice_offers_the_session_through_the_real_intake(tmp_path, monkeypatch):
    """A normal (not re-entered) upload whose slice fails gets the one offer."""
    from print_handy.bot.handlers import fix as handler
    from print_handy.bot.handlers import model_link
    from print_handy.core import prepared_intake, printer_state
    _folder, job_path, stl = _workspace(tmp_path)
    adapter = _Recorder()
    offers = []

    async def confirm(adapter_, source, metadata, kind, prompt, labels, *args, **kwargs):
        offers.append((kind, prompt, labels))
        return 'Not now'

    async def prepare_link(event, *_args, **_kwargs):
        return {'action': 'skip', 'reason': 'slice failed: no layers'}

    monkeypatch.setattr(handler.ai_routes, 'ready', lambda _tier, *_a: True)
    monkeypatch.setattr(handler, '_confirm', confirm)
    monkeypatch.setattr(prepared_intake, 'prepare_link', prepare_link)
    monkeypatch.setattr(printer_state, 'load', lambda **_kwargs: {})
    monkeypatch.setattr(model_link, '_state_dir', lambda: tmp_path / 'state')
    monkeypatch.setattr(model_link, '_undecided_printer_ids', lambda *_a: [])
    monkeypatch.setattr(model_link, '_routed_printer_id', lambda *_a: None)
    event = SimpleNamespace(source=_discord_source(), text='', media_urls=[str(stl)],
                            internal=False, metadata={})
    out = asyncio.run(model_link.model_link_dispatch(event, _gateway(adapter)))
    assert out['reason'] == 'slice failed: no layers'
    assert [(kind, labels[0]) for kind, _prompt, labels in offers] == [
        ('fix-problem-file', fix_session.WORK_WITH_AI_LABEL)]
    assert not fix_session._SESSIONS


def test_each_turn_sends_the_users_message_to_the_model_once(tmp_path, monkeypatch):
    from print_handy.bot.handlers import fix as handler
    _folder, _job, _stl, session = _session(tmp_path)
    sent = []

    def call(tier, messages, **_kwargs):
        sent.append([m for m in messages if m['role'] != 'system'])
        return json.dumps({'content': 'Noted.', 'tool_calls': []}), 'heavy'

    monkeypatch.setattr(handler.ai_routes, 'call', call)
    fix_session.process_turn(session, 'sink it', ai_complete=handler._complete)
    fix_session.process_turn(session, 'now trim it', ai_complete=handler._complete)
    assert [m['content'] for m in sent[0]] == ['sink it']
    assert [m['role'] for m in sent[1]] == ['user', 'assistant', 'user']
    assert [m['content'] for m in sent[1] if m['role'] == 'user'] == ['sink it', 'now trim it']
