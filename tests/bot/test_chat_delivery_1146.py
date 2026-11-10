"""#1146 leftovers: platform-named system prompt, initiator-only buttons,
the fuzzy dictionary's logged fallback, the retired roast_model setting and
the print-feedback handlers failing loudly when they are missing."""
import asyncio
import json
import logging

import pytest

from print_handy import settings
from print_handy.bot import llm, platform
from print_handy.bot.platform import SendResult
from print_handy.bot.router import load_plugin
from print_handy.core import fuzzy_vocabulary, prompts

OPERATOR = "200000000000000002"
MEMBER = "200000000000000777"
APPROVER = "200000000000000555"


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- system prompt

def test_system_prompt_names_the_platform_it_serves():
    prompt = llm.system_prompt("help", platform="telegram")
    assert "Telegram" in prompt
    assert "Discord" not in prompt


def test_system_prompt_without_a_platform_names_none():
    prompt = llm.system_prompt("help")
    for name in ("Discord", "Telegram", "Matrix"):
        assert name not in prompt


def test_ask_names_the_session_platform_in_the_system_prompt(monkeypatch):
    seen = {}

    def fake_call(tier, messages, **kwargs):
        seen["messages"] = messages
        return json.dumps({"reply": "hi", "command": None}), "fake"

    monkeypatch.setattr(llm.ai_routes, "call", fake_call)
    llm.clear_history()
    try:
        reply, command = llm.ask("hello", "help", session="matrix:room::user1")
    finally:
        llm.clear_history()
    assert (reply, command) == ("hi", None)
    system = seen["messages"][0]["content"]
    assert "Matrix" in system
    assert "Discord" not in system


# --------------------------------------------------------------- button presses

class _Adapter(platform.BasePlatformAdapter):
    name = "fake"
    channel_is_boundary = True
    supports_buttons = True
    max_buttons = 10

    def __init__(self, allowed=(), *, approver=None):
        super().__init__(allowed, approver=approver)
        self.clarifies = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(True, message_id="1")

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        labels = [str(c) for c in choices]
        self.clarifies.append(str(clarify_id))
        self._record_prompt(clarify_id, {"choices": labels, "question": question})
        return SendResult(True, message_id=str(clarify_id))

    async def close_prompt(self, clarify_id, note):
        self._prompts.pop(str(clarify_id), None)


def _register(adapter, cid, session_key, *, approval=False):
    prompts.register(cid, session_key, "Which one?", ["Keep", "Cancel"], approval=approval)
    adapter._record_prompt(cid, {"choices": ["Keep", "Cancel"], "question": "Which one?"})


def test_only_the_initiator_or_an_approver_can_press_a_prompts_buttons():
    adapter = _Adapter(approver=APPROVER)
    session = f"fake:chan::{OPERATOR}"
    _register(adapter, "e5" * 16, session)
    # Another allowed user pressing Cancel resolves nothing (#1146).
    assert run(adapter.answer_choice("e5" * 16, 1, user_id=MEMBER)) is None
    assert prompts.is_open("e5" * 16)
    # An approver may press for someone else.
    assert run(adapter.answer_choice("e5" * 16, 0, user_id=APPROVER)) == "Keep"


def test_the_initiator_can_press_their_own_prompts_buttons():
    adapter = _Adapter(approver=APPROVER)
    _register(adapter, "f6" * 16, f"fake:chan::{OPERATOR}")
    assert run(adapter.answer_choice("f6" * 16, 1, user_id=OPERATOR)) == "Cancel"


def test_a_prompt_with_no_recorded_initiator_keeps_channel_behavior():
    adapter = _Adapter(approver=APPROVER)
    _register(adapter, "a7" * 16, "sess")  # legacy key: no user segment
    assert run(adapter.answer_choice("a7" * 16, 0, user_id=MEMBER)) == "Keep"


# --------------------------------------------------------------- fuzzy dictionary

def test_corrupt_dictionary_logs_once_and_falls_back(tmp_path, caplog):
    bad = tmp_path / "dictionary.json"
    bad.write_text("{not json", encoding="utf-8")
    terms, categories = {}, {}
    with caplog.at_level(logging.WARNING, logger="print_handy.core.fuzzy_vocabulary"):
        fuzzy_vocabulary._load_dictionary_file(bad, terms, categories)
    assert terms == {} and categories == {}
    assert len([r for r in caplog.records if "dictionary" in r.getMessage()]) == 1
    # The built-in vocabulary still classifies without the file's additions.
    assert fuzzy_vocabulary.classify_domain_term("benchy") is not None


def test_valid_dictionary_entries_are_merged(tmp_path):
    good = tmp_path / "dictionary.json"
    good.write_text(json.dumps({"categories": {"models": [
        {"term": "wibble", "aliases": ["wib"], "typos": ["wible"]}]}}), encoding="utf-8")
    terms, categories = {}, {}
    fuzzy_vocabulary._load_dictionary_file(good, terms, categories)
    assert terms["wibble"] == "wibble" and terms["wib"] == "wibble"
    assert categories["wibble"] == "models"


# --------------------------------------------------------------- roast_model retired

def test_roast_model_is_no_longer_a_setting():
    assert ("vision", "roast_model") not in settings.OPTIONAL_KEYS


def test_a_config_still_carrying_roast_model_loads_with_a_warning():
    settings.load(local=False, environ={"PRINT_HANDY__VISION__ROAST_MODEL": "luna-latest"})
    assert "roast_model" in settings.unknown_settings_warning()


# --------------------------------------------------------------- missing handlers

def test_missing_print_feedback_handlers_fail_loudly(tmp_path):
    with pytest.raises(RuntimeError, match="print-feedback"):
        load_plugin(path=tmp_path / "plugins" / "print-feedback" / "__init__.py",
                    name="print_handy_print_feedback_missing_test")
