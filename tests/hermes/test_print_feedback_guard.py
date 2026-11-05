"""Offline regression tests for capable printer ownership and loop protection."""
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest

from print_handy.hermes import _config

SPEC = importlib.util.spec_from_file_location(
    "print_feedback_guard", Path(__file__).parents[2] / "plugins/print-feedback/__init__.py")
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


def setup_function():
    plugin._tool_turns.clear()


def invoke(name='terminal', args=None, profile='3dprinting', session='s', turn='t'):
    with mock.patch.object(plugin, '_session', side_effect=lambda key: profile if key == 'HERMES_SESSION_PROFILE' else 'discord'):
        return plugin.guard_terminal_loop(tool_name=name, args=args or {}, session_id=session, turn_id=turn)


def test_printer_can_read_edit_and_research_its_stack():
    for name, args in [
        ('read_file', {'path': str(_config.hermes_home() / 'scripts/source.py')}),
        ('patch', {'path': 'source.py'}),
        ('write_file', {'path': 'config.yaml'}),
        ('write_file', {'path': 'JOB.json'}),
        ('search_files', {'path': str(_config.hermes_home()), 'target': 'content', 'pattern': 'offset'}),
        ('web_search', {'query': 'ZMod calibration'}),
        ('session_search', {'query': 'ghost print'}),
    ]:
        for _ in range(15):
            assert invoke(name, args) is None


def test_distinct_terminal_commands_have_no_arbitrary_call_cap():
    for number in range(50):
        assert invoke(args={'command': f'test-command {number}'}) is None


def test_third_identical_terminal_command_remains_blocked():
    args = {'command': 'status'}
    assert invoke(args=args) is None
    assert invoke(args=args) is None
    assert 'repeat' in invoke(args=args)['message']
    assert invoke(args=args, turn='new') is None
    assert invoke(args=args, session='new') is None


def test_missing_identity_and_other_profiles_are_not_conflated():
    for _ in range(20):
        assert invoke(args={'command': 'status'}, turn='') is None
        assert invoke(args={'command': 'status'}, profile='default') is None


def test_verified_slot_bookkeeping_macro_is_not_name_banned():
    assert invoke(args={'command': "moonrakerctl gcode 'SET_EXTRUDER_SLOT SLOT=4'"}) is None


def test_gcode_path_protection_remains():
    for name in ('patch', 'write_file'):
        assert invoke(name, {'path': str(_config.archive_dir() / 'test/sliced/edited.gcode')}) is None
        assert invoke(name, {'path': str(_config.archive_dir().parent / 'elsewhere.gcode')})['action'] == 'block'


def test_reboot_dispatch_requires_safe_job_and_controller_states(monkeypatch):
    import asyncio
    import sys
    from types import SimpleNamespace as S
    from print_handy.hermes import print_control as pc
    class Adapter:
        def __init__(self): self.sent = []
        async def send(self, **kw): self.sent.append(kw.get('content')); return S(success=True)
        async def send_clarify(self, **kw): raise AssertionError('unsafe state must not offer restart button')
    adapter = Adapter()
    source = S(platform=S(value='discord'), profile='3dprinting', user_id='u', chat_id='c',
               thread_id=None, is_bot=False, profile_route_rejected=False)
    gateway = S(_is_user_authorized_for_source=lambda _s: True,
                _delivery_adapter_for=lambda _s: adapter)
    event = S(source=source, text='/print-reboot', internal=False)
    for status in ({'state': 'printing', 'klippy': 'ready'},
                   {'state': 'unknown', 'klippy': 'ready'},
                   {'state': 'printing', 'klippy': 'shutdown'}):
        adapter.sent.clear()
        with mock.patch.object(pc, 'status', return_value=status), mock.patch.object(pc, 'force_reboot') as reboot:
            asyncio.run(plugin.print_reboot_dispatch(event, gateway))
        reboot.assert_not_called()
        assert 'Refused:' in adapter.sent[-1]


def test_reboot_during_emergency_stop_offers_confirmed_firmware_restart():
    import asyncio
    import sys
    from types import SimpleNamespace as S
    from print_handy.hermes import print_control as pc
    from print_handy.hermes import print_alert_buttons as pab
    class Adapter:
        def __init__(self): self.sent = []
        async def send(self, **kw): self.sent.append(kw.get('content')); return S(success=True)
    adapter = Adapter()
    source = S(platform=S(value='discord'), profile='3dprinting', user_id='u', chat_id='c',
               thread_id=None, is_bot=False, profile_route_rejected=False)
    gateway = S(_is_user_authorized_for_source=lambda _s: True, _delivery_adapter_for=lambda _s: adapter,
                _session_key_for_source=lambda _s: 'session-key')
    event = S(source=source, text='/print-reboot', internal=False)
    msg = 'Nozzle hit bed or part detachment. Weight 1512>1500.0. Z=10.'
    for answer, expect_clear in ((pc.CANCEL_REBOOT_LABEL, False), (pc.REBOOT_LABEL, True)):
        adapter.sent.clear()
        async def confirm(*a, **k): return answer
        with mock.patch.object(pc, 'status', return_value={'state': 'paused', 'klippy': 'shutdown'}), \
             mock.patch.object(pc, 'force_reboot') as reboot, \
             mock.patch.object(plugin, '_confirm', side_effect=confirm) as conf, \
             mock.patch.object(pab, 'shutdown_message', return_value=msg), \
             mock.patch.object(pab, 'clear_shutdown', return_value=(True, 'Emergency stop cleared.')) as clear:
            asyncio.run(plugin.print_reboot_dispatch(event, gateway))
        reboot.assert_not_called()
        assert '1512' in conf.call_args[0][4]
        assert clear.called is expect_clear
        assert ('cleared' in adapter.sent[-1]) is expect_clear


def test_runout_new_spool_carries_slot_hex_into_created_filament():
    import sys
    from types import SimpleNamespace

    scripts = Path(__file__).parents[1] / 'scripts'
    from print_handy.hermes import spool_sync

    created_filaments = []

    def client(method, path, body):
        if method == 'GET' and path == '/api/v1/filament':
            return []
        if method == 'GET' and path == '/api/v1/vendor':
            return []
        if method == 'POST' and path == '/api/v1/vendor':
            return {'id': 17}
        if method == 'POST' and path == '/api/v1/filament':
            created_filaments.append(body)
            return {'id': 18}
        if method == 'POST' and path == '/api/v1/spool':
            return {'id': 19}
        raise AssertionError(f'unexpected Spoolman call: {method} {path}')

    context = {'slot': 3, 'prior_spool_id': 8, 'cause': 'swap'}
    listener = SimpleNamespace(
        load_runout_pause=lambda: context,
        clear_runout_pause=mock.Mock(),
    )
    runout_sync = SimpleNamespace(
        load_state=lambda: {'filament_slots': {'3': {'spoolman_id': 8, 'hex': '#161616'}}},
        screen_hex=spool_sync.screen_hex,
        create_spool=lambda spec: spool_sync.create_spool(spec, client=client),
        bind_slot=lambda *_args, **_kwargs: {'slot': 3},
        bind_text=lambda _result: 'Bound.',
        resume_usage=mock.Mock(),
    )

    result = plugin._answer_spool_question(
        listener, runout_sync, context,
        {'status': 'new', 'brand': 'SUNLU', 'material': 'PLA+', 'color': 'Black', 'grams': 1000},
    )

    assert created_filaments == [{
        'name': 'Black', 'material': 'PLA+', 'diameter': 1.75, 'density': 1.24,
        'weight': 1000, 'color_hex': '161616', 'vendor_id': 17,
    }]
    assert 'Created spool #19.' in result
    listener.clear_runout_pause.assert_called_once_with()



def test_print_again_success_posts_its_own_start_announcement(monkeypatch):
    """The start claim suppresses the listener's fallback post, so a
    successful /print-again posts and registers the canonical start
    announcement itself (P2-56); before the fix it posted nothing."""
    import asyncio
    import sys
    from types import SimpleNamespace as S

    scripts = Path(__file__).parents[1] / 'scripts'
    from print_handy.hermes import print_control as pc
    from print_handy.hermes import print_next
    from print_handy.hermes import discord_print_summary as summary

    class Adapter:
        def __init__(self):
            self.sent = []
        async def send(self, **kwargs):
            self.sent.append(kwargs['content'])
            return S(success=True)

    adapter = Adapter()
    source = S(platform=S(value='discord'), profile='3dprinting', user_id='u', chat_id='c',
               thread_id=None, is_bot=False, profile_route_rejected=False)
    gateway = S(_is_user_authorized_for_source=lambda _s: True, _delivery_adapter_for=lambda _s: adapter,
                _session_key_for_source=lambda _s: 'session-key')
    event = S(source=source, text='/print-again widget', internal=False)
    target = {'title': 'Widget', 'gcode': '/archive/widget.gcode', 'job_json': '/archive/JOB.json'}

    async def accepted(_target):
        return True, 'PRINT STARTED {"started": true}'

    with mock.patch.object(plugin, '_confirm', return_value=pc.AGAIN_LABEL), \
         mock.patch.object(pc, 'split_count', return_value=('widget', 1)), \
         mock.patch.object(pc, 'resolve_print', return_value=target.copy()), \
         mock.patch.object(pc, 'queue_copies') as queued, \
         mock.patch.object(print_next, 'start', side_effect=accepted), \
         mock.patch.object(summary, 'gcode_metadata', return_value=(60, 1.0)), \
         mock.patch.object(summary, '_print_number', return_value=8):
        result = asyncio.run(plugin.print_again_dispatch(event, gateway))

    assert result['reason'] == 'deterministic print again handled'
    queued.assert_called_once()
    assert adapter.sent[0] == 'Preparing to start Widget. Print #8; estimated print time ~1m, plus warmup ~6m total.'
    assert any(message.startswith('Print started') for message in adapter.sent)


def test_parallel_duplicate_commands_are_bounded():
    with mock.patch.object(plugin, '_session', return_value='3dprinting'):
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: plugin.guard_terminal_loop(
                tool_name='terminal', args={'command': 'status'}, session_id='s', turn_id='t'), range(40)))
    assert sum(result is None for result in results) == 2


def test_discord_clarify_cap_preserves_four_or_fewer_choices():
    choices = ['one', 'two', 'three', 'four']
    with mock.patch.object(plugin, '_session', side_effect=lambda key: {
            'HERMES_SESSION_PLATFORM': 'discord', 'HERMES_SESSION_PROFILE': '3dprinting'}.get(key, '')):
        assert plugin.cap_discord_clarifications(tool_name='clarify', args={
            'questions': [{'question': 'Pick', 'choices': choices}]}) is None


def test_discord_clarify_cap_limits_overflow_and_keeps_remainder_path():
    choices = ['one', 'two', 'three', 'four', 'five', 'six']
    with mock.patch.object(plugin, '_session', side_effect=lambda key: {
            'HERMES_SESSION_PLATFORM': 'discord', 'HERMES_SESSION_PROFILE': '3dprinting'}.get(key, '')):
        result = plugin.cap_discord_clarifications(tool_name='clarify', args={
            'questions': [{'question': 'Pick', 'choices': choices},
                          {'question': 'Already short', 'choices': ['yes', 'no']}],
            'unrelated': 'kept'})
    assert result['action'] == 'modify'
    args = result['args']
    assert args['unrelated'] == 'kept'
    assert args['questions'][0]['choices'] == choices[:4]
    assert 'Additional options exist' in args['questions'][0]['question']
    assert 'Other' in args['questions'][0]['question']
    assert args['questions'][1] == {'question': 'Already short', 'choices': ['yes', 'no']}


def test_discord_clarify_cap_is_scoped_to_3dprinting_discord():
    args = {'questions': [{'question': 'Pick', 'choices': list('abcde')}]}
    for platform, profile in [('matrix', '3dprinting'), ('discord', 'default')]:
        with mock.patch.object(plugin, '_session', side_effect=lambda key: {
                'HERMES_SESSION_PLATFORM': platform, 'HERMES_SESSION_PROFILE': profile}.get(key, '')):
            assert plugin.cap_discord_clarifications(tool_name='clarify', args=args) is None


    ctx = mock.Mock()
    plugin.register(ctx)
    assert [item.args for item in ctx.register_hook.call_args_list] == [
        ('pre_gateway_dispatch', plugin.guided_dispatch),
        ('pre_gateway_dispatch', plugin.design_dispatch),
        ('pre_gateway_dispatch', plugin.maintenance_dispatch),
        ('pre_gateway_dispatch', plugin.coldpull_dispatch),
        ('pre_gateway_dispatch', plugin.lookup_dispatch),
        ('pre_gateway_dispatch', plugin.source_dispatch),
        ('pre_gateway_dispatch', plugin.troubleshoot_dispatch),
        ('pre_gateway_dispatch', plugin.macros_dispatch),
        ('pre_gateway_dispatch', plugin.firmware_dispatch),
        ('pre_gateway_dispatch', plugin.calibration_dispatch),
        ('pre_gateway_dispatch', plugin.typed_quantity_dispatch),
        ('pre_gateway_dispatch', plugin.upload_wait_dispatch),
        ('pre_gateway_dispatch', plugin.model_link_dispatch),
        ('pre_gateway_dispatch', plugin.color_selection_dispatch),
        ('pre_gateway_dispatch', plugin.print_next_dispatch),
        ('pre_gateway_dispatch', plugin.runout_intake_dispatch),
        ('pre_gateway_dispatch', plugin.exclude_dispatch),
        ('pre_gateway_dispatch', plugin.print_stop_dispatch),
        ('pre_gateway_dispatch', plugin.print_resume_dispatch),
        ('pre_gateway_dispatch', plugin.print_again_dispatch),
        ('pre_gateway_dispatch', plugin.edit_dispatch),
        ('pre_gateway_dispatch', plugin.loaded_spools_dispatch),
        ('pre_gateway_dispatch', plugin.purchase_dispatch),
        ('pre_gateway_dispatch', plugin.spool_dispatch),
        ('pre_gateway_dispatch', plugin.print_reboot_dispatch),
        ('pre_gateway_dispatch', plugin.live_dispatch),
        ('pre_gateway_dispatch', plugin.nozzle_dispatch),
        ('pre_gateway_dispatch', plugin.receive_ack),
        ('pre_gateway_dispatch', plugin.stray_chat_dispatch),
        ('post_tool_call', plugin.clarify_ack),
        ('pre_tool_call', plugin.cap_discord_clarifications),
        ('pre_tool_call', plugin.guard_terminal_loop),
    ]
    assert plugin.receipt_text('https://makerworld.com/model bed clear') is None  # preview + picker acknowledge links
    assert plugin.clarify_receipt({'outcome': 'cancelled'}) is None


def test_upload_wait_suppresses_dispatch_until_explicit_resume(monkeypatch):
    plugin._upload_waits.clear()
    src = mock.Mock(platform=mock.Mock(value='discord'), profile='3dprinting', chat_id='c', thread_id=None,
                    user_id='u', is_bot=False, profile_route_rejected=False)
    gateway = mock.Mock(_is_user_authorized_for_source=lambda source: True)
    wait = mock.Mock(source=src, text='Wait while I upload files', internal=False)
    ordinary = mock.Mock(source=src, text='here is another attachment', internal=False)
    resume = mock.Mock(source=src, text='Uploads are done, resume', internal=False)
    assert __import__('asyncio').run(plugin.upload_wait_dispatch(wait, gateway))['reason'] == 'upload wait active'
    assert __import__('asyncio').run(plugin.upload_wait_dispatch(ordinary, gateway))['reason'].startswith('upload wait active')
    assert __import__('asyncio').run(plugin.upload_wait_dispatch(resume, gateway)) is None


def test_clarify_receipt_never_claims_action_started():
    text = plugin.clarify_receipt({'outcome': 'submitted', 'responses': [
        {'status': 'answered', 'user_response': '2 copies'}]})
    assert text == 'You chose: 2 copies. Nothing has started.'


@pytest.mark.usefixtures("hermes_cli_plugins")
def test_actual_dispatcher_applies_discord_clarify_cap():
    from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
    args = {'questions': [{'question': 'Pick', 'choices': ['one', 'two', 'three', 'four', 'five']}]}
    with mock.patch.object(plugin, '_session', side_effect=lambda key: {
            'HERMES_SESSION_PLATFORM': 'discord', 'HERMES_SESSION_PROFILE': '3dprinting'}.get(key, '')), mock.patch(
        'hermes_cli.lifecycle.invoke_hook', side_effect=lambda event, **kw: [plugin.cap_discord_clarifications(**kw)]):
        message, modified = _dispatch_pre_tool_call_hooks('clarify', args, session_id='s', turn_id='t')
    assert message is None
    assert modified is not None
    assert modified['questions'][0]['choices'] == ['one', 'two', 'three', 'four']
    assert 'Additional options exist' in modified['questions'][0]['question']


@pytest.mark.usefixtures("hermes_cli_plugins")
def test_actual_dispatcher_enforces_duplicate_guard():
    from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
    with mock.patch.object(plugin, '_session', return_value='3dprinting'), mock.patch(
        'hermes_cli.lifecycle.invoke_hook', side_effect=lambda event, **kw: [plugin.guard_terminal_loop(**kw)]):
        for _ in range(2):
            message, _ = _dispatch_pre_tool_call_hooks('terminal', {'command': 'status'}, session_id='s', turn_id='t')
            assert message is None
        message, _ = _dispatch_pre_tool_call_hooks('terminal', {'command': 'status'}, session_id='s', turn_id='t')
        assert message is not None and 'repeat' in message


@pytest.mark.parametrize('command, expected', [
    ('python3 -m print_handy.core.model_job slice JOB.json --approved', True),
    ('python3 /x/model_job.py slice JOB.json', True),
    ('ls -la', False),
])
def test_terminal_receipt_matches_module_and_script_invocations(command, expected):
    session = {'HERMES_SESSION_PLATFORM': 'discord', 'HERMES_SESSION_PROFILE': '3dprinting',
               'HERMES_SESSION_CHAT_ID': 'chat-ack-test'}
    with mock.patch.object(plugin, '_session', side_effect=lambda key: session.get(key, '')), \
            mock.patch.object(plugin, 'workflow_receipt', return_value='ok') as receipt:
        plugin.clarify_ack(tool_name='terminal', args={'command': command}, result={}, tool_call_id=command)
    assert receipt.called is expected


def test_runout_new_spool_multi_color_name_keeps_its_palette():
    """A multi-color name answered to a spool question must record the
    full palette (P2-234): the answer path used to pass its single
    derived swatch as an explicit hex, and spec_color returns an empty
    palette whenever a hex is given — the blend silently became one color."""
    from types import SimpleNamespace

    from print_handy.hermes import spool_sync

    created_filaments = []
    patches = []

    def client(method, path, body):
        if method == 'GET' and path == '/api/v1/filament':
            return []
        if method == 'GET' and path == '/api/v1/vendor':
            return [{'id': 17, 'name': 'SUNLU'}]
        if method == 'POST' and path == '/api/v1/filament':
            created_filaments.append(body)
            return {'id': 18}
        if method == 'PATCH' and path == '/api/v1/filament/18':
            patches.append(body)
            return {'id': 18}
        if method == 'POST' and path == '/api/v1/spool':
            return {'id': 19}
        raise AssertionError(f'unexpected Spoolman call: {method} {path}')

    context = {'slot': 3, 'prior_spool_id': 8, 'cause': 'swap'}
    listener = SimpleNamespace(
        load_runout_pause=lambda: context,
        clear_runout_pause=mock.Mock(),
    )
    runout_sync = SimpleNamespace(
        load_state=lambda: {'filament_slots': {'3': {'spoolman_id': 8}}},
        screen_hex=spool_sync.screen_hex,
        create_spool=lambda spec: spool_sync.create_spool(spec, client=client),
        bind_slot=lambda *_args, **_kwargs: {'slot': 3},
        bind_text=lambda _result: 'Bound.',
        resume_usage=mock.Mock(),
    )

    result = plugin._answer_spool_question(
        listener, runout_sync, context,
        {'status': 'new', 'brand': 'SUNLU', 'material': 'Silk PLA',
         'color': 'Rose Red Blue Green', 'grams': 1000, 'color_confirmed': True},
    )

    assert 'Created spool #19.' in result
    assert created_filaments and created_filaments[0]['color_hex'] == 'C2185B'
    assert {'multi_color_hexes': 'C2185B,1E88E5,388E3C',
            'multi_color_direction': 'longitudinal'} in patches
