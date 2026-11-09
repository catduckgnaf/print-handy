"""Retirements on the bot's own question path (#524 stage 2).

The bot's standing prompt is its own question message — it carries a
message id and there is no Stop view to retire — so prepared_intake
completes it in place: its buttons are retired through the adapter,
the same end state a click produces. (The by-id refusal editor that
used to stand beside this path belonged to the removed Hermes clarify
renderer.)
"""
import asyncio
import json
import urllib.request
from types import SimpleNamespace as S

from print_handy.bot.platform import SendResult
from print_handy.core import prepared_intake
from print_handy.hermes import print_alert_buttons as pab


class ClarifyAdapter:
    """The message-id-carrying adapter shape the clarify path completes."""

    def __init__(self):
        self.retired = []
        self.clarified = []

    async def send(self, **kw):
        raise AssertionError("the clarify path never posts a second message")

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        self.clarified.append(clarify_id)
        return SendResult(True, message_id="m9")

    async def retire_prompt(self, clarify_id):
        self.retired.append(clarify_id)
        return True


SOURCE = S(chat_id="chan1", thread_id=None)
KEY = "live:chan1:user1"


def run_stop(adapter, work, refusal_context=None, monkeypatch=None):
    monkeypatch.setattr(prepared_intake, "STOP_SENDER", None)
    return asyncio.run(prepared_intake._with_stop(
        adapter, SOURCE, KEY, None, "Working…", work,
        allow_stop=True, refusal_context=refusal_context))


def test_clarify_path_completion_retires_the_question(monkeypatch):
    adapter = ClarifyAdapter()

    async def work():
        return "done"

    assert run_stop(adapter, work(), monkeypatch=monkeypatch) == "done"
    assert adapter.retired == adapter.clarified  # the standing question was retired in place


def test_clarify_path_refusal_retires_the_question(monkeypatch):
    adapter = ClarifyAdapter()
    context = {"receipt": {"intake_id": "b" * 32}}

    async def work():
        return False, "REFUSED: printer is still busy. Nothing uploaded or started."

    result = run_stop(adapter, work(), refusal_context=context, monkeypatch=monkeypatch)
    assert result[0] is False
    assert context["prompt_handled"] is False  # no by-id editor exists anymore
    assert context["start_prompt_message_id"] == "m9"
    assert adapter.retired == adapter.clarified  # the refusal retires the question


# ---------------------------------------------------------------------- pab.edit_message components


def test_edit_message_omits_components_unless_given(monkeypatch):
    seen = []

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        seen.append(json.loads(req.data.decode()))
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert pab.edit_message("tok", "chan1", "m9", "hello") is True
    assert "components" not in seen[0]  # pre-existing callers: payload unchanged
    components = [{"type": 1, "components": [{"type": 2, "style": 1, "label": "Retry",
                                              "custom_id": "pbretry:x"}]}]
    assert pab.edit_message("tok", "chan1", "m9", "hello", components) is True
    assert seen[1]["components"] == components
    assert pab.edit_message("tok", "chan1", "m9", "hello", []) is True
    assert seen[2]["components"] == []  # explicit empty list strips the buttons
