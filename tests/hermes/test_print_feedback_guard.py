"""Offline regression tests for the handler module's dispatch handlers.

(The agent tool-call guards that used to share this file — the terminal
loop guard and the Discord clarify cap — were deleted with the Hermes
host that ran them.)
"""
import importlib.util
from pathlib import Path
from unittest import mock

from print_handy.runtime import _config  # noqa: F401  (kept: handler tests read config helpers)

SPEC = importlib.util.spec_from_file_location(
    "print_feedback_guard", Path(__file__).parents[2] / "plugins/print-feedback/__init__.py")
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


def test_reboot_dispatch_requires_safe_job_and_controller_states(monkeypatch):
    import asyncio
    import sys
    from types import SimpleNamespace as S
    from print_handy.runtime import print_control as pc
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
    from print_handy.runtime import print_control as pc
    from print_handy.runtime import print_alert_buttons as pab
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
    from print_handy.runtime import spool_sync

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
    from print_handy.runtime import print_control as pc
    from print_handy.runtime import print_next
    from print_handy.runtime import discord_print_summary as summary

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
         mock.patch.object(pc, 'latest_number_for_job', return_value=8):
        result = asyncio.run(plugin.print_again_dispatch(event, gateway))

    assert result['reason'] == 'deterministic print again handled'
    queued.assert_called_once()
    assert adapter.sent[0] == ('Preparing to start Widget. A reprint of print #8; it gets its own new print number '
                               'when it starts. Estimated print time ~1m, plus warmup ~6m total.')
    assert any(message.startswith('Print started') for message in adapter.sent)


def test_register_installs_only_the_dispatch_list_and_platform_handler():
    """register() binds the dispatch handlers in order, the Discord
    platform handler, and nothing else — the tool-call hooks and the
    agent-only dispatch hooks are gone with their host."""
    ctx = mock.Mock()
    plugin.register(ctx)
    assert [item.args for item in ctx.register_hook.call_args_list] == [
        ('pre_gateway_dispatch', hook) for hook in plugin._DISPATCH_HOOKS]
    assert [item.args for item in ctx.register_platform_handler.call_args_list] == [
        ('discord', plugin.alert_button_handlers)]


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


def test_runout_new_spool_multi_color_name_keeps_its_palette():
    """A multi-color name answered to a spool question must record the
    full palette (P2-234): the answer path used to pass its single
    derived swatch as an explicit hex, and spec_color returns an empty
    palette whenever a hex is given — the blend silently became one color."""
    from types import SimpleNamespace

    from print_handy.runtime import spool_sync

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
