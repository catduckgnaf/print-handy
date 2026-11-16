"""Tranche 6: the conversational lane on typed read-only tools (#1524).

The model may call one validated read-only tool per ask and may propose
an action as a capability plus typed slots; it never supplies command
text. The lane is rate-limited per user, and a one-time notice says
mentioned messages go to the configured endpoint.
"""
import asyncio
import io
import json

import pytest

from corpus import fake_discord
from print_handy.bot import ai_tools, llm, natural_language as nl, runtime

LLM_URL = "http://llm.test/v1/chat/completions"
MENTION = "<@300000000000000003>"


class ScriptedLLM:
    """Answers with a scripted sequence of model payloads."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.requests = []

    def __call__(self, req, timeout=None):
        assert req.full_url == LLM_URL
        self.requests.append(json.loads(req.data))
        payload = self.payloads.pop(0) if self.payloads else {"reply": "Done."}
        return io.BytesIO(json.dumps(
            {"choices": [{"message": {"content": json.dumps(payload)}}]}).encode())


@pytest.fixture
def llm_cfg(cfg):
    cfg["discord"]["chat_llm_enabled"] = True
    cfg["llm"].update(chat_url=LLM_URL, model="small-model")
    return cfg


def make(cfg, opener):
    discord = fake_discord.FakeDiscord(
        auto=False, chat_id=int(cfg["discord"]["chat_id"]),
        operator_id=int(cfg["discord"]["allowed_user_ids"]))
    return discord, runtime.BotCore(discord.client, cfg, llm_opener=opener)


def run(coro):
    return asyncio.run(coro)


RECORDS = [nl.normalize_spool(
    {"id": 1, "filament": {"name": "Purple", "material": "PLA",
                           "vendor": {"name": "Polymaker"}}, "remaining_weight": 800})]


def test_tool_call_runs_the_read_only_tool_and_quotes_its_result(llm_cfg, monkeypatch):
    # A phrasing the deterministic layer does not claim, so the lane is
    # what answers — through its one typed tool.
    monkeypatch.setattr(nl, "_fetch_spool_records", lambda core: (RECORDS, set()))
    opener = ScriptedLLM(
        {"reply": None, "tool": {"name": "inventory_facts",
                                 "arguments": {"color": "purple", "material": "pla"}}},
        {"reply": "You have 800 g of purple PLA."})
    discord, core = make(llm_cfg, opener)
    out = run(core.handle_message(discord.message(f"{MENTION} can you check my filament stock for me?")))
    assert out == {"action": "skip", "reason": "llm reply"}
    assert discord.sent[-1] == "You have 800 g of purple PLA."
    # The second request carried the tool result, marked untrusted.
    second = opener.requests[1]["messages"][-1]["content"]
    assert "untrusted" in second and "800 g" in second


def test_unvalidated_tool_call_is_dropped(llm_cfg):
    opener = ScriptedLLM(
        {"reply": "I cannot do that.", "tool": {"name": "run_gcode",
                                                "arguments": {"code": "M112"}}})
    discord, core = make(llm_cfg, opener)
    out = run(core.handle_message(discord.message(f"{MENTION} emergency stop the printer")))
    assert out == {"action": "skip", "reason": "llm reply"}
    assert discord.sent[-1] == "I cannot do that."
    assert len(opener.requests) == 1  # no second call: the tool never ran


def test_tool_validation_shapes():
    assert ai_tools.validate_tool_call("inventory_facts", {"color": "purple"}) == {"color": "purple"}
    assert ai_tools.validate_tool_call("inventory_facts", {"color": "purple; rm"}) is None
    assert ai_tools.validate_tool_call("inventory_facts", {"evil": "x"}) is None
    assert ai_tools.validate_tool_call("shell", {}) is None
    assert ai_tools.validate_tool_call("doc_lookup", {"query": "what can you do"}) == \
        {"query": "what can you do"}


def test_proposal_builds_commands_from_slots_only():
    assert ai_tools.proposal_command("print_stop", {}) == "/print-stop"
    assert ai_tools.proposal_command("print_pause", {}) == "/print-pause"
    assert ai_tools.proposal_command("print_again", {"target": "57"}) == "/print-again 57"
    assert ai_tools.proposal_command("print_again", {"target": "57; rm -rf /"}) is None
    assert ai_tools.proposal_command("spool_load", {"spool_id": 1, "slot": 2}) is None
    assert ai_tools.proposal_command("firmware_update_apply", {}) is None
    assert ai_tools.validate_proposal({"capability": "print_stop", "slots": {}}) == \
        ("print_stop", {})
    assert ai_tools.validate_proposal({"capability": "nuke", "slots": {}}) is None
    assert ai_tools.validate_proposal("print_stop") is None


def test_proposal_commands_pass_the_allowlist_gate():
    for capability_id, slots in (("print_stop", {}), ("print_pause", {}),
                                 ("print_again", {"target": "57"})):
        built = ai_tools.proposal_command(capability_id, slots)
        assert llm.allowed_command(built) == built


def test_rate_limit_stops_the_lane(llm_cfg, monkeypatch):
    opener = ScriptedLLM({"reply": "One."}, {"reply": "Two."})
    discord, core = make(llm_cfg, opener)
    core._ai_rate = ai_tools.RateLimiter(max_calls=1, window_s=3600)
    run(core.handle_message(discord.message(f"{MENTION} hello there")))
    out = run(core.handle_message(discord.message(f"{MENTION} hello again")))
    assert out == {"action": "skip", "reason": "ai rate limited"}
    assert any("rate limit" in text for text in discord.sent)
    assert len(opener.requests) == 1


def test_endpoint_notice_is_sent_once_per_user(llm_cfg):
    opener = ScriptedLLM({"reply": "One."}, {"reply": "Two."})
    discord, core = make(llm_cfg, opener)
    run(core.handle_message(discord.message(f"{MENTION} hello there")))
    run(core.handle_message(discord.message(f"{MENTION} hello again")))
    notices = [t for t in discord.sent if t == runtime.ai_endpoint_notice_text()]
    assert len(notices) == 1


def test_per_channel_off_switch_blocks_the_lane(llm_cfg):
    llm_cfg["natural_language"] = {
        "ai_disabled_channel_ids": [str(llm_cfg["discord"]["chat_id"])]}
    opener = ScriptedLLM({"reply": "should not be asked"})
    discord, core = make(llm_cfg, opener)
    out = run(core.handle_message(discord.message(f"{MENTION} hello there")))
    assert opener.requests == []
    assert out == {"action": "skip", "reason": "command list"}


def test_doc_lookup_tool_uses_the_registry():
    text = ai_tools.run_tool(None, "doc_lookup", {"query": "firmware updates"})
    assert "firmware" in text.casefold()
    text = ai_tools.run_tool(None, "inventory_facts", {"color": "purple"})
    assert isinstance(text, str) and text  # unavailable or factual, never an exception
