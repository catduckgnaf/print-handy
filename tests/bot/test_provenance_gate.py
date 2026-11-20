"""Invocation provenance and the conversational AI gate (#1524 tranche 1).

The loose ``mentioned`` boolean is replaced, for AI purposes, by a
provenance value set by the platform adapter and carried unchanged
through handle_message / WebChatService / make_event / process /
fallback. One policy table decides which provenance unlocks the
conversational lane per platform. Deterministic matches always beat the
AI lane, edited messages never re-trigger, @everyone/@here/role mentions
and mentions inside quote blocks never count, and a different bot's
mention never counts.
"""
import ast
import asyncio
import io
import json
import socket
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from corpus import fake_discord
from print_handy.bot import adapter as discord_adapter, mention, provenance, runtime, web

BOT_ID = 300000000000000003
ROOT = Path(__file__).resolve().parents[2]


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- policy table

@pytest.mark.parametrize("value,platform,kind,expected", [
    ("platform_entity", "discord", "shared", True),
    ("platform_entity", "discord", "dm", True),
    ("command_suffix", "telegram", "shared", True),
    ("command_suffix", "discord", "shared", False),
    ("typed_at_name", "discord", "shared", False),
    ("typed_at_name", "discord", "dm", True),
    ("typed_name", "discord", "dm", False),
    ("typed_ai_phrase", "discord", "shared", False),   # owner decision: shared channels no
    ("typed_ai_phrase", "discord", "dm", True),
    ("typed_ai_phrase", "webui", "webui", True),
    ("ui_flag", "webui", "webui", True),
    ("ui_flag", "discord", "shared", False),
    ("reply_to_bot", "discord", "shared", False),      # owner decision: a reply alone is not addressing
    ("reply_to_bot", "discord", "dm", False),
    ("none", "discord", "dm", False),
    ("none", "webui", "webui", False),
])
def test_policy_table(value, platform, kind, expected):
    prov = provenance.Provenance(value=value, platform=platform, channel_kind=kind)
    assert prov.unlocks_conversation() is expected


def test_edited_message_never_unlocks():
    prov = provenance.Provenance(value="platform_entity", platform="discord",
                                 channel_kind="shared", edited=True)
    assert prov.unlocks_conversation() is False


def test_per_channel_off_switch_wins():
    prov = provenance.Provenance(value="platform_entity", platform="discord", channel_kind="shared")
    assert prov.unlocks_for_channel("42", ai_disabled_channels=["42"]) is False
    assert prov.unlocks_for_channel("43", ai_disabled_channels=["42"]) is True


# ---------------------------------------------------------------- mention evidence

def _identity(**kw):
    return mention.Identity(names=("Print Handy",), tokens=(f"<@{BOT_ID}>",), **kw)


def test_platform_markup_is_platform_entity():
    out = mention.address(f"<@{BOT_ID}> hello there", _identity())
    assert out.mentioned and out.provenance == "platform_entity"


def test_typed_at_name_is_typed_at_name_not_entity():
    out = mention.address("@Print Handy hello there", _identity())
    assert out.mentioned and out.provenance == "typed_at_name"


def test_typed_plain_name_is_typed_name():
    out = mention.address("Print Handy, spool list", _identity())
    assert out.named and out.provenance == "typed_name"


def test_everyone_and_here_never_count():
    for text in ("@everyone hello", "@here hello"):
        out = mention.address(text, _identity())
        assert not out.mentioned and out.provenance == "none"


def test_role_mention_routes_but_is_not_entity_provenance():
    identity = mention.Identity(names=("Print Handy",), tokens=("<@&4242>",))
    out = mention.address("<@&4242> spool list", identity)
    assert out.mentioned and out.provenance == "none"


def test_mention_inside_quote_block_never_counts():
    out = mention.address(f"> <@{BOT_ID}> spool list\njust quoting that", _identity())
    assert not out.mentioned and out.provenance == "none"


def test_different_bot_mention_never_counts():
    out = mention.address("<@999999> spool list", _identity())
    assert not out.mentioned and out.provenance == "none"


def test_reply_to_bot_is_recorded_but_not_addressing():
    identity = mention.Identity(names=("Print Handy",), tokens=(f"<@{BOT_ID}>",), reply_to_bot=True)
    out = mention.address("thanks, that worked", identity)
    assert not out.mentioned and out.provenance == "reply_to_bot"


def test_telegram_command_suffix_provenance():
    identity = mention.Identity(names=("Print Handy",), handles=("PrintHandyBot",), suffixes=True)
    out = mention.address("/spool@PrintHandyBot list", identity)
    assert out.mentioned and out.provenance == "command_suffix"
    other = mention.address("/spool@OtherBot list", identity)
    assert other.elsewhere


# ---------------------------------------------------------------- end to end gate

def make(cfg, llm_opener=None, agent_opener=None):
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    # Use the Web UI AI policy with a trusted test-only Discord transport,
    # retaining FakeDiscord's entity mention semantics without a Web adapter.
    transport = discord_adapter.StandaloneAdapter(
        discord.client, [str(cfg["discord"]["allowed_user_ids"])])
    core = runtime.BotCore(discord.client, cfg, platform_name="discord", adapter=transport,
                           llm_opener=llm_opener, agent_opener=agent_opener)
    core.addressed = lambda text, message=None: mention.address(
        text, mention.identity_for("discord", core.client, core.configured_bot_name, message))
    core.use_llm = bool(cfg["webui"]["chat_llm_enabled"])
    return discord, core


class FakeLLM:
    def __init__(self, reply="A model answer.", command=None):
        self.content = json.dumps({"reply": reply, "command": command})
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req.full_url)
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": self.content}}]}).encode())


@pytest.fixture
def llm_cfg(cfg):
    cfg.setdefault("webui", {})["chat_llm_enabled"] = True
    cfg["llm"].update(chat_url="http://llm.test/v1/chat/completions", model="m")
    return cfg


def test_unmentioned_chat_makes_no_model_call(llm_cfg):
    opener = FakeLLM()
    discord, core = make(llm_cfg, llm_opener=opener)
    run(core.handle_message(discord.message("hello there, how is it going?")))
    assert opener.requests == []
    assert any("didn't understand" in text for text in discord.sent)


def test_real_mention_unlocks_the_model_lane(llm_cfg):
    opener = FakeLLM()
    discord, core = make(llm_cfg, llm_opener=opener)
    run(core.handle_message(discord.message(f"<@{BOT_ID}> hello there, how is it going?")))
    assert opener.requests, "a real platform mention should reach the configured lane"
    assert any("A model answer." in text for text in discord.sent)


def test_typed_name_does_not_unlock_in_shared_channel(llm_cfg):
    opener = FakeLLM()
    discord, core = make(llm_cfg, llm_opener=opener)
    run(core.handle_message(discord.message("@Print Handy hello there")))
    assert opener.requests == []


def test_quoted_mention_does_not_unlock(llm_cfg):
    opener = FakeLLM()
    discord, core = make(llm_cfg, llm_opener=opener)
    run(core.handle_message(discord.message(f"> <@{BOT_ID}> hello there")))
    assert opener.requests == []


def test_mentioned_deterministic_question_beats_the_model(llm_cfg, monkeypatch):
    opener = FakeLLM()
    discord, core = make(llm_cfg, llm_opener=opener)
    monkeypatch.setattr("print_handy.runtime.spool_sync.list_text",
                        lambda **kw: "Slot 1: #7 black PLA")
    run(core.handle_message(discord.message(f"<@{BOT_ID}> spool list")))
    assert opener.requests == []
    assert discord.sent == ["Slot 1: #7 black PLA"]


def test_no_llm_with_sockets_blocked(llm_cfg, monkeypatch):
    """Unmentioned chat runs with network sockets blocked: any model call
    would fail loudly instead of silently succeeding."""
    def blocked(*args, **kwargs):
        raise AssertionError("a socket was opened during unmentioned chat")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    opener = FakeLLM()
    discord, core = make(llm_cfg, llm_opener=opener)
    run(core.handle_message(discord.message("hello there")))
    assert opener.requests == []


def test_provenance_survives_make_event_and_process(llm_cfg):
    prov = provenance.Provenance(value="ui_flag", platform="webui", channel_kind="webui")
    source = runtime.make_source("webui", "webui", platform_name="webui")
    event = runtime.make_event(source, "hello", "1", prov=prov)
    assert provenance.from_event(event).value == "ui_flag"


# ---------------------------------------------------------------- web UI Ask AI

def test_webui_ask_ai_flag_is_server_side(cfg, monkeypatch):
    seen = {}

    async def fake_process(self, event, **kwargs):
        seen["provenance"] = provenance.from_event(event).value
        return {"action": "skip", "reason": "test"}

    monkeypatch.setattr(runtime.BotCore, "process", fake_process)
    service = web.WebChatService(cfg)
    try:
        assert service.submit("hello there", ask_ai=True) is True
        import time
        for _ in range(100):
            if "provenance" in seen:
                break
            time.sleep(0.05)
        assert seen.get("provenance") == "ui_flag"
        seen.clear()
        assert service.submit("hello there") is True
        for _ in range(100):
            if "provenance" in seen:
                break
            time.sleep(0.05)
        assert seen.get("provenance") == "none"
    finally:
        service.close()


def test_webui_chat_page_has_ask_ai_control():
    from print_handy.webui import chat as chat_page
    import inspect
    assert 'name="ask_ai"' in inspect.getsource(chat_page.render_chat_page)


# ---------------------------------------------------------------- import allowlist

# Modules allowed to import a model client (ai_routes, bot.llm, bot.agent),
# by purpose (#1524 tranche 1): the conversational lane, design, edit
# repair, vision, purchase intake and status reporting. Anything else
# importing one fails here, so new code cannot quietly add a model call.
MODEL_IMPORT_ALLOWLIST = {
    "print_handy/ai_routes.py": "the model client itself",
    "print_handy/bot/llm.py": "conversational lane",
    "print_handy/bot/agent.py": "conversational lane (agent endpoint)",
    "print_handy/bot/runtime.py": "conversational lane dispatch",
    "print_handy/bot/__main__.py": "status reporting only",
    "print_handy/bot/natural_language.py": "typed read-only tools for the conversational lane",
    "print_handy/core/ai_fix.py": "edit repair",
    "print_handy/runtime/visual_watch.py": "vision",
    "print_handy/runtime/moonraker_event_listener.py": "vision/watchdog orchestration",
    "print_handy/runtime/purchase_intake.py": "purchase intake",
}


def _model_imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[-1] in ("ai_routes", "openai") or alias.name.endswith((".llm", ".agent")):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[-1] == "ai_routes" or module.endswith("bot.llm") or module.endswith("bot.agent"):
                found.add(module)
            for alias in node.names:
                if alias.name in ("ai_routes",):
                    found.add(alias.name)
    return found


def test_model_import_allowlist():
    offenders = []
    for path in sorted((ROOT / "print_handy").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in MODEL_IMPORT_ALLOWLIST:
            continue
        found = _model_imports(path)
        if found:
            offenders.append(f"{rel}: {sorted(found)}")
    assert offenders == [], "model clients imported outside the allowlist:\n" + "\n".join(offenders)
