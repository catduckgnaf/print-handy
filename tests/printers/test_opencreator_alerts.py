"""Offline fixtures exercise the listener and common button safety gate."""
import asyncio
from types import SimpleNamespace

import pytest

from print_handy.printers.flashforge_opencreator import alerts as oc
from print_handy.runtime import klipper_report, print_alert_buttons as pab


def adapter(key="flashforge_opencreator.creator5"):
    return SimpleNamespace(model=SimpleNamespace(key=key))


def test_prompt_protocol_and_exact_choices():
    reader = klipper_report.ActionPrompt()
    assert reader.feed('RESPOND TYPE=command MSG="action:prompt_begin fake"') is None
    assert reader.feed('// action:prompt_begin Runout') is None
    reader.feed('// action:prompt_button Load|_OC_RUNOUT_LOAD_CONTINUE|primary')
    reader.feed('// action:prompt_button T2|_OC_RUNOUT_SWITCH TO=2|primary')
    kind = oc.runout_kind(reader.feed('// action:prompt_show'))
    assert oc.choices(kind) == [("ocload", "Load and continue"), ("ocswitch2", "Switch to T2")]
    assert reader.feed('// action:prompt_show') is None
    reader.feed('// action:prompt_end')
    assert reader.feed('// action:prompt_show') is None
    assert oc.runout_kind(['_OC_RUNOUT_SWITCH TO=9']) is None
    assert oc.runout_kind(['_OC_RUNOUT_LOAD_CONTINUE\nG28']) is None


@pytest.mark.parametrize('kind', [oc.RECOVERY_KIND, 'oc-runout-31', 'oc-runout-1'])
def test_transport_buttons_and_unverified_click_send_nothing(monkeypatch, kind):
    from print_handy import printers
    monkeypatch.setattr(printers, 'current', lambda: adapter())
    monkeypatch.setattr(pab, '_alert_printer', lambda alert: (None, None))
    monkeypatch.setattr(pab._config, 'moonraker_url', lambda: '')
    monkeypatch.setattr(pab, '_call', lambda *a, **k: pytest.fail('printer call'))
    monkeypatch.setattr('print_handy.runtime.print_control.status', lambda *a, **k: pytest.fail('printer read'))
    buttons = pab.components('test', kind)[0]['components']
    assert len(buttons) == len(oc.choices(kind))
    for button in buttons:
        action, aid = pab.parse_custom_id(button['custom_id'])
        assert aid == 'test'
        assert 'not hardware verified' in pab.handle_click(action, {'kind': kind})
    assert pab.telegram_keyboard('test', kind)
    assert pab.matrix_reaction_choices(kind)
    from print_handy.bot.web_feed import alert_spec
    assert alert_spec(kind, 'part.gcode')['kind'] == kind


def test_listener_prompt_and_recovery_dedupe(monkeypatch):
    from print_handy.runtime import moonraker_event_listener as mel
    listener = object.__new__(mel.Listener)
    listener.filename = 'part.gcode'
    monkeypatch.setattr(listener, '_clock_adapter', lambda: adapter())
    posts = []
    monkeypatch.setattr(mel, 'post_alert', lambda *a: posts.append(a) or True)
    row = {'recovery_available': True, 'filename': 'part.gcode', 'file_position': 25,
           'file_size': 100, 'holding_bed': True}
    monkeypatch.setattr(mel.print_control, '_call', lambda path: {'result': {'status': {'power_loss_recovery': row}}})
    async def exercise():
        for line in ['action:prompt_begin Runout', 'action:prompt_button Load|_OC_RUNOUT_LOAD_CONTINUE',
                     'action:prompt_button T3|_OC_RUNOUT_SWITCH TO=3', 'action:prompt_show']:
            await listener.opencreator_prompt(line)
        await listener.opencreator_recovery()
        await listener.opencreator_recovery()
    asyncio.run(exercise())
    assert len(posts) == 2
    assert oc.choices(posts[0][1]) == [('ocload', 'Load and continue'), ('ocswitch3', 'Switch to T3')]
    assert posts[1][1] == oc.RECOVERY_KIND
    assert '25.0%' in posts[1][0] and 'held hot' in posts[1][0]
    assert not hasattr(listener, 'print_number')  # alert detection never allocates a print record


def test_recovery_unknown_and_invalid_fields():
    assert oc.recovery({}) is None
    assert oc.recovery({'power_loss_recovery': {'recovery_available': 'true'}}) is None
    found = oc.recovery({'power_loss_recovery': {'recovery_available': True, 'file_size': 0}})
    assert 'progress unknown' in found[1] and 'hold state unknown' in found[1]


def test_backup_display_in_readiness_and_job_details(monkeypatch, capsys):
    from print_handy import printers
    from print_handy.launch import gates, print_now
    # Advisory data must survive into the job card without becoming a refusal.
    fake = SimpleNamespace(moonraker_objects=lambda: ['AFC'],
                           moonraker_followup_objects=lambda status: [],
                           backup_lane_summary=lambda *a: 'T0: laneA runout_lane=laneB\nWarning: mismatch',
                           readiness_problems=lambda *a: [], label='OpenCreator')
    monkeypatch.setattr(gates, 'request_json', lambda *a: {'result': {'status': {}}})
    state = {'moonraker_url': 'http://example.invalid'}
    gates._verify_readiness_adapter(fake, state)
    assert 'runout_lane=laneB' in capsys.readouterr().out
    monkeypatch.setattr(print_now, '_maintenance_detail_line', lambda: '')
    assert 'runout_lane=laneB' in print_now.job_details({}, hardware=state)
