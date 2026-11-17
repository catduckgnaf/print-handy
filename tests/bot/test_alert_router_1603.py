"""Alert action buttons are ordinary router rows (#1603).

The pause, resume, cancel, acknowledge and mute buttons used to be a
special final case after the _BUTTON_ROUTES table because their
approver rule depends on the parsed action. The table's needs_approver
now accepts a callable of the parsed row, so the alert row dispatches
like every other row: acknowledge and mute need only an allowed user,
every other action needs an approver (may_start includes the allowed
check, so the old allowed-then-approver order is preserved).
"""
import asyncio
import sys
from types import SimpleNamespace as S

from print_handy.runtime import print_alert_buttons as pab
from tests.runtime.test_print_next import Interaction, load_plugin


def _install(monkeypatch, adapter):
    class Button:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class View:
        def __init__(self, timeout=None):
            self.items = []

        def add_item(self, item):
            self.items.append(item)
    monkeypatch.setitem(sys.modules, 'discord', S(ui=S(View=View, Button=Button),
                                                  ButtonStyle=S(success=3, danger=4, primary=1)))
    handlers = {}
    bot = S(add_listener=lambda fn, name: handlers.update({name: fn}))
    load_plugin().alert_button_handlers(bot, adapter)
    return handlers['on_interaction']


def _adapter():
    return S(_is_allowed_user=lambda uid, user, guild=None, channel_ids=None: True,
             may_approve_id=lambda uid: uid == '42')


def _click(custom_id, user_id):
    click = Interaction(custom_id)
    click.user = S(id=user_id, bot=False, display_name='op')

    async def _noop(*a, **k):
        return None
    click.message = S(id='m1', edit=_noop)
    return click


def _rig(tmp_path, monkeypatch):
    monkeypatch.setattr(pab, 'STATE', tmp_path / 'alerts.json')
    on_interaction = _install(monkeypatch, _adapter())
    calls = []

    def handle_click(action, alert):
        calls.append((action, alert))
        return 'Done.'
    monkeypatch.setattr(pab, 'handle_click', handle_click)
    return on_interaction, calls


def test_alert_resume_refuses_a_non_approver_before_claiming(tmp_path, monkeypatch):
    on_interaction, calls = _rig(tmp_path, monkeypatch)
    aid = pab.record('paused', '')
    click = _click(f'pbalert:resume:{aid}', user_id=7)
    asyncio.run(on_interaction(click))
    assert calls == []
    assert ('send', 'Not allowed. Nothing changed.') in click.out
    # The refusal happens before the claim: the alert is still takeable.
    assert pab.take(aid)


def test_alert_acknowledge_needs_only_an_allowed_user(tmp_path, monkeypatch):
    on_interaction, calls = _rig(tmp_path, monkeypatch)
    aid = pab.record('paused', '')
    click = _click(f'pbalert:acknowledge:{aid}', user_id=7)
    asyncio.run(on_interaction(click))
    assert [action for action, _alert in calls] == ['acknowledge']
    assert ('followup', 'Done.') in click.out


def test_alert_resume_runs_for_an_approver(tmp_path, monkeypatch):
    on_interaction, calls = _rig(tmp_path, monkeypatch)
    aid = pab.record('paused', '')
    click = _click(f'pbalert:resume:{aid}', user_id=42)
    asyncio.run(on_interaction(click))
    assert [action for action, _alert in calls] == ['resume']
    assert ('followup', 'Done.') in click.out
