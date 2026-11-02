"""Cancelling a print from a pending spool question: button, typed reply, cleanup.

Offline; the printer is print_stop's module functions, replaced per test.
The bug pinned here: while a spool question was pending, "Cancel the print"
got the "spool question is still open" rejection and the question message
carried no cancel button, so there was no way to end the print.
"""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.hermes import moonraker_event_listener as mel
from print_handy.hermes import print_alert_buttons as pab
from print_handy.hermes import print_stop
from print_handy.hermes import spool_sync

SPEC = importlib.util.spec_from_file_location(
    "print_feedback_cancel", Path(__file__).parents[2] / "plugins/print-feedback/__init__.py")
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)

RECORD = {"filename": "all parts one plate.gcode", "slot": 2, "prior_spool_id": 8,
          "cause": "runout", "usage_suspended": True, "suspended_spool_id": 8}


def _usage_tripwire():
    """spool_sync stand-in: any inventory or usage call fails the test.

    A cancel finalises usage exactly as the existing cancelled-print path
    does — by touching nothing: Moonraker already charged in real time up to
    the suspension, and nothing may be resumed, re-charged or rebound.
    """
    boom = mock.Mock(side_effect=AssertionError("cancel must not touch spool inventory or usage"))
    return S(SpoolError=spool_sync.SpoolError, resume_usage=boom, bind_slot=boom, spoolman=boom)


def _listener(record=RECORD):
    return S(load_runout_pause=lambda: record, clear_runout_pause=mock.Mock())


def _print_stop(monkeypatch, states, cancel_result=None):
    queue = list(states)
    monkeypatch.setattr(print_stop, "status", lambda: queue.pop(0))
    cancel = mock.Mock(return_value=cancel_result or (True, "Stopped. Printer now reports cancelled."))
    monkeypatch.setattr(print_stop, "cancel", cancel)
    return cancel


def test_parse_cancel_print_reply_accepts_only_unambiguous_instructions():
    for text in ("Cancel the print", "cancel", "CANCEL PRINT", "cancel print.",
                 "stop the print", "stop print", "cancel it", "  cancel  "):
        assert mel.parse_cancel_print_reply(text), text
    for text in ("should I cancel the print?", "please cancel the print", "cancel culture",
                 "cancel, use spool 12", "12", "same", "Sunlu, PLA+, White, 1000", "", "stop nagging"):
        assert not mel.parse_cancel_print_reply(text), text


def test_spool_question_text_offers_cancel():
    text = mel.spool_question_text(dict(RECORD))
    assert "cancel the print" in text and "Cancel print" in text


def test_cancel_helper_cancels_via_print_stop_and_closes_question(monkeypatch):
    listener = _listener()
    cancel = _print_stop(monkeypatch,
                         [{"state": "paused", "filename": RECORD["filename"]},
                          {"state": "cancelled", "filename": ""}])
    message = plugin._cancel_spool_question_print(listener, _usage_tripwire(), dict(RECORD))
    cancel.assert_called_once_with(RECORD["filename"])
    listener.clear_runout_pause.assert_called_once_with()
    assert "Print cancelled" in message and "reminders are off" in message


def test_cancel_helper_closes_question_when_print_already_over(monkeypatch):
    listener = _listener()
    cancel = _print_stop(monkeypatch, [{"state": "standby", "filename": ""}])
    message = plugin._cancel_spool_question_print(listener, _usage_tripwire(), dict(RECORD))
    cancel.assert_not_called()
    listener.clear_runout_pause.assert_called_once_with()
    assert "already over" in message and "reminders are off" in message


def test_cancel_helper_failed_cancel_leaves_question_open(monkeypatch):
    listener = _listener()
    _print_stop(monkeypatch,
                [{"state": "paused", "filename": RECORD["filename"]},
                 {"state": "paused", "filename": RECORD["filename"]}],
                cancel_result=(True, "Cancel sent, but the printer still reports paused after 20s."))
    with pytest.raises(spool_sync.SpoolError):
        plugin._cancel_spool_question_print(listener, _usage_tripwire(), dict(RECORD))
    listener.clear_runout_pause.assert_not_called()


def test_cancel_helper_refused_cancel_leaves_question_open(monkeypatch):
    listener = _listener()
    _print_stop(monkeypatch,
                [{"state": "paused", "filename": "other.gcode"},
                 {"state": "paused", "filename": "other.gcode"}],
                cancel_result=(False, "A different print is running now than the one you confirmed. Nothing sent."))
    with pytest.raises(spool_sync.SpoolError):
        plugin._cancel_spool_question_print(listener, _usage_tripwire(), dict(RECORD))
    listener.clear_runout_pause.assert_not_called()


def test_cancel_helper_already_answered_question_changes_nothing(monkeypatch):
    listener = _listener(record=None)
    cancel = _print_stop(monkeypatch, [{"state": "paused", "filename": RECORD["filename"]}])
    with pytest.raises(spool_sync.SpoolError):
        plugin._cancel_spool_question_print(listener, _usage_tripwire(), dict(RECORD))
    cancel.assert_not_called()
    listener.clear_runout_pause.assert_not_called()


class _Adapter:
    def __init__(self):
        self.sent = []

    async def send(self, **kw):
        self.sent.append(kw.get("content"))
        return S(success=True)


def _dispatch(monkeypatch, text, states, cancel_result=None):
    adapter = _Adapter()
    source = S(platform=S(value="discord"), profile="3dprinting", user_id="u", chat_id="c",
               thread_id=None, is_bot=False, profile_route_rejected=False)
    gateway = S(_is_user_authorized_for_source=lambda _s: True,
                _delivery_adapter_for=lambda _s: adapter)
    event = S(source=source, text=text, internal=False)
    cleared = mock.Mock()
    monkeypatch.setattr(mel, "load_runout_pause", lambda: dict(RECORD))
    monkeypatch.setattr(mel, "clear_runout_pause", cleared)
    cancel = _print_stop(monkeypatch, states, cancel_result)
    result = asyncio.run(plugin.runout_intake_dispatch(event, gateway))
    return result, adapter, cancel, cleared


def test_typed_cancel_the_print_routes_to_cancel_not_rejection(monkeypatch):
    result, adapter, cancel, cleared = _dispatch(
        monkeypatch, "Cancel the print",
        [{"state": "paused", "filename": RECORD["filename"]},
         {"state": "cancelled", "filename": ""}])
    assert result == {"action": "skip", "reason": "spool question print cancelled"}
    cancel.assert_called_once_with(RECORD["filename"])
    cleared.assert_called_once_with()
    assert "Print cancelled" in adapter.sent[-1]
    assert not any("Spool question is still open" in (m or "") for m in adapter.sent)


def test_typed_non_answer_still_gets_the_still_open_rejection(monkeypatch):
    result, adapter, cancel, cleared = _dispatch(
        monkeypatch, "hello there",
        [{"state": "paused", "filename": RECORD["filename"]}])
    assert result == {"action": "skip", "reason": "spool reply needs a choice"}
    cancel.assert_not_called()
    cleared.assert_not_called()
    assert "Spool question is still open" in adapter.sent[-1]
    assert "cancel the print" in adapter.sent[-1]


def test_typed_cancel_failure_keeps_question_open(monkeypatch):
    result, adapter, cancel, cleared = _dispatch(
        monkeypatch, "cancel the print",
        [{"state": "paused", "filename": RECORD["filename"]},
         {"state": "paused", "filename": RECORD["filename"]}],
        cancel_result=(True, "Cancel sent, but the printer still reports paused after 20s."))
    assert result == {"action": "skip", "reason": "spool question cancel failed, question left open"}
    cleared.assert_not_called()
    assert "was not cancelled" in adapter.sent[-1] and "still open" in adapter.sent[-1]


def test_reminders_stop_once_cancel_clears_the_question(tmp_path, monkeypatch):
    state = tmp_path / "runout.json"
    monkeypatch.setattr(mel, "RUNOUT_PAUSE_STATE", state)
    posted = mock.Mock(return_value=True)
    monkeypatch.setattr(mel, "post_spool_question", posted)
    record = mel.open_spool_question(RECORD["filename"], 2, 8, "runout", 8, path=state)
    record["last_nag_at"] = 0  # a reminder is due
    mel.atomic_write_json(state, record)
    listener = object.__new__(mel.Listener)
    asyncio.run(listener.nag_if_due())
    assert posted.call_count == 1
    mel.clear_runout_pause(path=state)
    asyncio.run(listener.nag_if_due())
    assert posted.call_count == 1  # no record, no reminder


class _Interaction:
    def __init__(self, custom_id):
        self.data = {"custom_id": custom_id}
        self.user = S(id=1, bot=False, display_name="op")
        self.guild = None
        self.channel_id = "c"
        self.out = []
        interaction = self

        class _Message:
            async def edit(self, view=None, **kw):
                interaction.out.append(("strip", view))

        class _Response:
            async def send_message(self, content, ephemeral=False, **kw):
                interaction.out.append(("send", content, ephemeral))

            async def edit_message(self, content=None, view=None, **kw):
                interaction.out.append(("edit", content, view))
        self.message = _Message()
        self.response = _Response()
        self.followup = S(send=self._followup)

    async def _followup(self, content, view=None, ephemeral=False):
        self.out.append(("followup", content))


def _on_interaction(tmp_path, monkeypatch):
    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    handlers = {}
    adapter = S(_is_allowed_user=lambda uid, user, guild=None, channel_ids=None: True)
    plugin.alert_button_handlers(
        S(add_listener=lambda fn, name: handlers.update({name: fn})), adapter)
    return handlers["on_interaction"]


def test_cancel_button_cancels_and_closes_question(tmp_path, monkeypatch):
    on_interaction = _on_interaction(tmp_path, monkeypatch)
    cleared = mock.Mock()
    monkeypatch.setattr(mel, "load_runout_pause", lambda: dict(RECORD))
    monkeypatch.setattr(mel, "clear_runout_pause", cleared)
    cancel = _print_stop(monkeypatch,
                         [{"state": "paused", "filename": RECORD["filename"]},
                          {"state": "cancelled", "filename": ""}])
    aid = pab.record("spool-question", "", context=dict(RECORD))
    interaction = _Interaction(f"pbspool:cancel:{aid}")
    asyncio.run(on_interaction(interaction))
    cancel.assert_called_once_with(RECORD["filename"])
    cleared.assert_called_once_with()
    # The tap is acknowledged first (P2-164); the outcome arrives by follow-up.
    sends = [entry for entry in interaction.out if entry[0] == "send"]
    assert sends and "tapped Cancel print" in sends[0][1]
    followups = [entry for entry in interaction.out if entry[0] == "followup"]
    assert followups and "Print cancelled" in followups[-1][1] and "reminders are off" in followups[-1][1]


def test_cancel_button_failure_keeps_question_answerable(tmp_path, monkeypatch):
    on_interaction = _on_interaction(tmp_path, monkeypatch)
    cleared = mock.Mock()
    monkeypatch.setattr(mel, "load_runout_pause", lambda: dict(RECORD))
    monkeypatch.setattr(mel, "clear_runout_pause", cleared)
    _print_stop(monkeypatch,
                [{"state": "paused", "filename": RECORD["filename"]},
                 {"state": "paused", "filename": RECORD["filename"]}],
                cancel_result=(True, "Cancel sent, but the printer still reports paused after 20s."))
    aid = pab.record("spool-question", "", context=dict(RECORD))
    interaction = _Interaction(f"pbspool:cancel:{aid}")
    asyncio.run(on_interaction(interaction))
    cleared.assert_not_called()
    followups = [entry for entry in interaction.out if entry[0] == "followup"]
    assert followups and "was not cancelled" in followups[-1][1] and "still open" in followups[-1][1]


def _typed_answer(monkeypatch, error):
    monkeypatch.setattr(plugin, "_answer_spool_question", mock.Mock(side_effect=error))
    return _dispatch(monkeypatch, "Sunlu, PLA+, Black, 1000",
                     [{"state": "paused", "filename": RECORD["filename"]}])


def test_typed_new_spool_created_before_failure_is_reported(monkeypatch):
    error = spool_sync.SpoolError("Spool #31 was created and is on the shelf, but loading it failed.")
    error.spool_created = 31
    result, adapter, _cancel, cleared = _typed_answer(monkeypatch, error)
    assert result == {"action": "skip", "reason": "spool answer failed closed"}
    assert adapter.sent[-1] == str(error)
    cleared.assert_not_called()


def test_typed_answer_failure_without_a_created_spool_keeps_the_fail_closed_copy(monkeypatch):
    result, adapter, _cancel, cleared = _typed_answer(monkeypatch, spool_sync.SpoolError("mapped elsewhere"))
    assert result == {"action": "skip", "reason": "spool answer failed closed"}
    assert adapter.sent[-1] == ("That answer did not work, so nothing was changed. "
                                "Usage is still paused; please answer again.")
    cleared.assert_not_called()
