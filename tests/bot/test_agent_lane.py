"""The stray-chat agent lane (#524 stage 3): a configured [agent] endpoint
answers Discord free text the handlers did not take, ahead of the bounded
[llm] call.

The lane's contract is the opposite of bot/llm.py's on purpose: no bot
system prompt, no JSON demand and no command channel — the endpoint has
its own persona, its reply is posted as-is, and its free text can never
name a command into execution. The bot stays stateless and sends the live
session key as a header the endpoint may key its own conversation on.
Every test runs against a fake endpoint in the FakeLLM opener pattern; no
real endpoint is involved.
"""
import asyncio
import io
import json
from unittest import mock

import pytest

from corpus import fake_discord
from print_handy import settings
from print_handy.bot import __main__ as bot_main
from print_handy.bot import agent, runtime
from print_handy.core import prompts

AGENT_URL = "http://agent.test/v1/chat/completions"
LLM_URL = "http://llm.test/v1/chat/completions"


class FakeAgent:
    """An opener double in the FakeLLM pattern: asserts the URL, captures
    the request body, headers and timeout, returns canned choices JSON."""

    def __init__(self, content):
        self.content = content
        self.requests = []

    def __call__(self, req, timeout=None):
        assert req.full_url == AGENT_URL, f"the agent path reached {req.full_url}"
        headers = {key.lower(): value for key, value in req.header_items()}
        self.requests.append({"body": json.loads(req.data), "headers": headers, "timeout": timeout})
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": self.content}}]}).encode())


class FakeLLM:
    def __init__(self, reply, command=None):
        self.content = json.dumps({"reply": reply, "command": command})
        self.requests = []

    def __call__(self, req, timeout=None):
        assert req.full_url == LLM_URL, f"the LLM path reached {req.full_url}"
        self.requests.append(json.loads(req.data))
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": self.content}}]}).encode())


@pytest.fixture
def agent_cfg(cfg):
    cfg["agent"].update(chat_url=AGENT_URL)
    return cfg


def make(cfg, agent_opener=None, llm_opener=None):
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, cfg, llm_opener=llm_opener, agent_opener=agent_opener)
    return discord, core


def exploding(name):
    def ask(*args, **kwargs):
        raise AssertionError(f"{name} was called when it must not be")
    return ask


# ---------------------------------------------------------------------- settings (S1)


def test_agent_settings_defaults():
    data = settings.load(local=False, environ={})
    assert data["agent"] == {"chat_url": "", "model": "", "api_key_env": "AGENT_API_KEY",
                             "timeout_s": 120,
                             # The #902 identity layer, all inert by default.
                             "identities": {}, "users": {}, "allowlist": [],
                             "fallback_identity": "", "idle_timeout_s": 1800,
                             "rate_limit_per_minute": 10, "thread_replies": False,
                             "dm_enabled": False, "mention_enabled": False}
    assert agent.configured(data) is False


def test_agent_settings_load_from_env_and_model_stays_optional():
    data = settings.load(local=False, environ={"PRINT_HANDY__AGENT__CHAT_URL": AGENT_URL})
    assert data["agent"]["chat_url"] == AGENT_URL and data["agent"]["model"] == ""
    assert agent.configured(data) is True  # chat_url alone configures the lane; no model required
    data = settings.load(local=False, environ={"PRINT_HANDY__AGENT__TIMEOUT_S": "45"})
    assert data["agent"]["timeout_s"] == 45


def test_agent_settings_validate():
    with pytest.raises(settings.SettingsError, match=r"\[agent\] chat_url must be a URL"):
        settings.load(local=False, environ={"PRINT_HANDY__AGENT__CHAT_URL": "agent.test/v1"})
    with pytest.raises(settings.SettingsError, match=r"\[agent\] timeout_s must be a positive number"):
        settings.load(local=False, environ={"PRINT_HANDY__AGENT__TIMEOUT_S": "0"})


# ---------------------------------------------------------------------- the request contract (S2)


def test_agent_request_shape_no_model_no_json_demand_session_header(agent_cfg):
    opener = FakeAgent("The printer finished at 6 and the plate is clear.")
    reply = agent.ask("did my print finish?", "discord:1::2", cfg=agent_cfg, opener=opener)
    assert reply == "The printer finished at 6 and the plate is clear."
    (request,) = opener.requests
    # No model key at all when [agent] model is unset — the endpoint is not
    # necessarily model-addressable.
    assert "model" not in request["body"]
    # One user message, no bot system prompt (so no JSON demand and no
    # command list), stream off, and the live session key rides as a header
    # while the body stays strictly chat-completions-shaped.
    assert request["body"] == {"messages": [{"role": "user", "content": "did my print finish?"}],
                               "stream": False}
    assert request["headers"]["x-print-handy-session"] == "discord:1::2"
    assert request["timeout"] == 120.0  # [agent] timeout_s, not [llm]'s 20


def test_agent_request_includes_model_and_key_when_set(agent_cfg, monkeypatch):
    agent_cfg["agent"].update(model="hermes-3dprinting", timeout_s=7)
    monkeypatch.setenv("AGENT_API_KEY", "sekrit")
    opener = FakeAgent("ok")
    agent.ask("hi", "discord:1::2", cfg=agent_cfg, opener=opener)
    (request,) = opener.requests
    assert request["body"]["model"] == "hermes-3dprinting"
    assert request["headers"]["authorization"] == "Bearer sekrit"
    assert request["timeout"] == 7.0


def test_agent_ask_raises_on_empty_reply(agent_cfg):
    with pytest.raises(ValueError, match="empty reply"):
        agent.ask("hi", "discord:1::2", cfg=agent_cfg, opener=FakeAgent("  "))


# ---------------------------------------------------------------------- BotCore wiring


def test_agent_reply_is_posted_as_is_with_a_stable_conversation_id(agent_cfg):
    """#902 supersedes the #524 live-session-key header: the lane now
    carries a per-user conversation id (the OpenAI ``user`` field and the
    session header agree), stable across messages inside the idle
    window, so the endpoint keeps per-user context."""
    opener = FakeAgent("All quiet — the last print finished cleanly.")
    discord, core = make(agent_cfg, agent_opener=opener)
    out = asyncio.run(core.handle_message(discord.message("how did the print go?")))
    assert out == {"action": "skip", "reason": "agent reply"}
    assert discord.sent == ["All quiet — the last print finished cleanly."]
    asyncio.run(core.handle_message(discord.message("and the one before that?")))
    first, second = opener.requests
    conversation = first["headers"]["x-print-handy-session"]
    assert conversation and conversation == second["headers"]["x-print-handy-session"]
    assert first["body"]["user"] == conversation
    assert core.status(True)["agent"] is True


def test_agent_and_llm_replies_can_name_a_file_but_never_attach_it(agent_cfg, tmp_path):
    """#532: a model reply is untrusted; MEDIA:/proc/self/environ must not upload the bot's secrets."""
    secret = tmp_path / "environ"
    secret.write_text("DISCORD_TOKEN=sekrit\n")
    for lane in ("agent", "llm"):
        reply = f"Here you go\nMEDIA:{secret}"
        if lane == "agent":
            discord, core = make(agent_cfg, agent_opener=FakeAgent(reply))
        else:
            agent_cfg["agent"]["chat_url"] = ""
            agent_cfg["llm"].update(chat_url=LLM_URL, model="small-model")
            discord, core = make(agent_cfg, llm_opener=FakeLLM(reply))
        asyncio.run(core.handle_message(discord.message("send me the logs")))
        assert [m.files for m in discord.messages] == [[]], lane
        assert discord.sent == [f"Here you go\nMEDIA​:{secret}"], lane


def test_agent_reply_that_looks_like_a_command_executes_nothing(agent_cfg):
    """No command channel: a reply naming a command — bare or in the [llm]
    JSON shape — is posted verbatim and runs nothing."""
    for content in ("/print-stop", '{"reply": "stopping", "command": "/print-stop"}'):
        opener = FakeAgent(content)
        discord, core = make(agent_cfg, agent_opener=opener)
        with mock.patch("print_handy.hermes.print_stop._call") as moonraker:
            out = asyncio.run(core.handle_message(discord.message("chat with me")))
        moonraker.assert_not_called()
        assert out == {"action": "skip", "reason": "agent reply"}
        assert discord.sent == [content]
        assert prompts.open_count() == 0  # no confirmation question was opened


def test_agent_lane_wins_over_llm_when_both_are_configured(agent_cfg):
    agent_cfg["llm"].update(chat_url=LLM_URL, model="small-model")
    agent_opener, llm_opener = FakeAgent("agent words"), FakeLLM("llm words")
    discord, core = make(agent_cfg, agent_opener=agent_opener, llm_opener=llm_opener)
    out = asyncio.run(core.handle_message(discord.message("hello there")))
    assert out == {"action": "skip", "reason": "agent reply"}
    assert discord.sent == ["agent words"]
    assert len(agent_opener.requests) == 1 and llm_opener.requests == []


def test_agent_endpoint_down_posts_the_honest_line_then_falls_back_to_the_llm(agent_cfg):
    """#902 supersedes the #524 no-lane-swap rule: an unreachable agent
    produces one clear failure line, then the [llm] tier answers."""
    agent_cfg["llm"].update(chat_url=LLM_URL, model="small-model")
    llm_opener = FakeLLM("llm words")

    def broken(req, timeout=None):
        raise OSError("agent down")

    discord, core = make(agent_cfg, agent_opener=broken, llm_opener=llm_opener)
    out = asyncio.run(core.handle_message(discord.message("hello?")))
    assert out == {"action": "skip", "reason": "llm reply"}
    assert discord.sent == [core.agent_unavailable, "llm words"]
    assert core.agent_unavailable.startswith("The chat agent didn't answer")
    assert len(llm_opener.requests) == 1


def test_agent_endpoint_down_without_an_llm_still_ends_on_the_honest_line(agent_cfg):
    def broken(req, timeout=None):
        raise OSError("agent down")

    discord, core = make(agent_cfg, agent_opener=broken)
    out = asyncio.run(core.handle_message(discord.message("hello?")))
    assert out == {"action": "skip", "reason": "agent unavailable"}
    assert discord.sent == [core.agent_unavailable]


def test_agent_empty_reply_posts_the_honest_line(agent_cfg):
    discord, core = make(agent_cfg, agent_opener=FakeAgent(""))
    out = asyncio.run(core.handle_message(discord.message("hello?")))
    assert out == {"action": "skip", "reason": "agent unavailable"}
    assert discord.sent == [core.agent_unavailable]


def test_agent_unconfigured_keeps_the_llm_then_unmatched_behaviour(cfg):
    """No [agent]: the exploding stub proves the lane is never called, the
    [llm] call still answers when configured, and the unmatched reply is
    still the last resort."""
    cfg["llm"].update(chat_url=LLM_URL, model="small-model")
    discord, core = make(cfg, llm_opener=FakeLLM("llm words"))
    with mock.patch.object(runtime.agent, "ask", exploding("the agent lane")):
        out = asyncio.run(core.handle_message(discord.message("hello there")))
    assert out == {"action": "skip", "reason": "llm reply"} and discord.sent == ["llm words"]
    assert core.status(True)["agent"] is False

    cfg["llm"].update(chat_url="", model="")
    discord, core = make(cfg)
    with mock.patch.object(runtime.agent, "ask", exploding("the agent lane")):
        out = asyncio.run(core.handle_message(discord.message("hello there")))
    assert out == {"action": "skip", "reason": "command list"} and discord.sent == [core.unmatched]


def test_chat_llm_switch_off_makes_no_model_call_of_any_kind(agent_cfg):
    """#466's switch, one consistent meaning (#524 stage 3): with
    chat_llm_enabled=false, exploding stubs on BOTH lanes prove no
    stray-chat model call happens, agent lane included."""
    agent_cfg["llm"].update(chat_url=LLM_URL, model="small-model")
    agent_cfg["discord"]["chat_llm_enabled"] = False
    discord, core = make(agent_cfg)
    assert core.use_llm is False
    status = core.status(True)
    assert status["agent"] is False and status["llm"] is False
    with mock.patch.object(runtime.agent, "ask", exploding("the agent lane")), \
            mock.patch.object(runtime.llm, "ask", exploding("the chat LLM")):
        out = asyncio.run(core.handle_message(discord.message("can you make my printer go brrr")))
    assert out == {"action": "skip", "reason": "command list"}
    assert discord.sent == [core.unmatched]


# ---------------------------------------------------------------------- bot check row (S3)


def test_bot_check_agent_row_reports_each_state(cfg, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x" * 20)
    rows = {label: detail for label, ok, detail in bot_main.checks(cfg)}
    assert rows["agent"].startswith("not configured")
    cfg["agent"].update(chat_url=AGENT_URL)
    rows = {label: detail for label, ok, detail in bot_main.checks(cfg)}
    assert rows["agent"] == "endpoint at agent.test (no model set; the endpoint chooses)"
    cfg["agent"].update(model="hermes-3dprinting")
    rows = {label: detail for label, ok, detail in bot_main.checks(cfg)}
    assert rows["agent"] == "hermes-3dprinting at agent.test"  # host only, never the URL path
    cfg["discord"]["chat_llm_enabled"] = False
    rows = {label: detail for label, ok, detail in bot_main.checks(cfg)}
    assert rows["agent"].startswith("off (discord.chat_llm_enabled is false")
