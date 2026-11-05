"""The stray-chat claim hook (#524 stage 3, design §2e): in Hermes mode the
bot's own stray-chat lanes answer otherwise-unmatched Discord text, so
`discord.chat_llm_enabled` means the same thing in every mode.

The hook is registered last among the pre_gateway_dispatch hooks and is
never run by the standalone Router (BotCore's fallback is the same
behaviour there). These tests drive the hook directly with the established
stand-ins and fake endpoints in the FakeLLM pattern — no real endpoint,
no gateway and no network are involved.
"""
import asyncio
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

from print_handy.bot import agent, commands, llm, runtime

SPEC = importlib.util.spec_from_file_location(
    "print_feedback_stray_chat", Path(__file__).parents[2] / "plugins/print-feedback/__init__.py")
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)

AGENT_URL = "http://agent.test/v1/chat/completions"
LLM_URL = "http://llm.test/v1/chat/completions"
SESSION_KEY = "discord:99::7"


class Adapter:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return S(success=True)


class FakeEndpoint:
    """One urlopen double for both lanes: routes by URL, records requests."""

    def __init__(self, agent_content=None, llm_content=None, fail=None):
        self.agent_content = agent_content
        self.llm_content = llm_content
        self.fail = fail
        self.requests = []

    def __call__(self, req, timeout=None):
        headers = {key.lower(): value for key, value in req.header_items()}
        self.requests.append({"url": req.full_url, "body": json.loads(req.data),
                              "headers": headers, "timeout": timeout})
        if self.fail is not None:
            raise self.fail
        content = self.agent_content if req.full_url == AGENT_URL else self.llm_content
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())


def make_event(text="hello printer", platform="discord"):
    source = S(platform=S(value=platform), profile="3dprinting", user_id="7", chat_id="99",
               thread_id=None, is_bot=False, profile_route_rejected=False)
    return S(source=source, text=text, internal=False, message_id="m1", metadata={})


def make_gateway(adapter, authorized=True):
    return S(_is_user_authorized_for_source=lambda _s: authorized,
             _delivery_adapter_for=lambda _s: adapter,
             _session_key_for_source=lambda _s: SESSION_KEY)


def cfg_for(chat=True, agent_url="", llm_url=""):
    return {
        "discord": {"chat_llm_enabled": chat, "bot_name": "Print Handy"},
        "agent": {"chat_url": agent_url, "model": "", "api_key_env": "AGENT_API_KEY",
                  "timeout_s": 120},
        "llm": {"chat_url": llm_url, "model": "test-model" if llm_url else "",
                "api_key_env": "LLM_API_KEY", "timeout_s": 20},
    }


def run_hook(event, gateway, cfg):
    # The stand-in carries the real hosting_profile (#574): under the test
    # settings it derives nothing, so the gates expect the events' stamped
    # '3dprinting' identity exactly as the literal comparison did.
    from print_handy.hermes import _config as real_config
    stand_in = S(cfg=lambda: cfg, hosting_profile=real_config.hosting_profile)
    with mock.patch.object(plugin, "_config", stand_in):
        return asyncio.run(plugin.stray_chat_dispatch(event, gateway))


def exploding(name):
    def ask(*args, **kwargs):
        raise AssertionError(f"{name} was called when it must not be")
    return ask


# ---------------------------------------------------------------- the switch is off

def test_switch_off_claims_with_the_short_reply_and_calls_no_model():
    adapter = Adapter()
    with mock.patch.object(agent, "ask", exploding("agent.ask")), \
            mock.patch.object(llm, "ask", exploding("llm.ask")):
        result = run_hook(make_event(), make_gateway(adapter), cfg_for(chat=False))
    assert result == {"action": "skip", "reason": "chat llm disabled"}
    assert [s["content"] for s in adapter.sent] == [runtime.unmatched_text("Print Handy")]


def test_switch_off_still_answers_help_words_with_the_command_list():
    adapter = Adapter()
    with mock.patch.object(agent, "ask", exploding("agent.ask")), \
            mock.patch.object(llm, "ask", exploding("llm.ask")):
        result = run_hook(make_event("help"), make_gateway(adapter), cfg_for(chat=False))
    assert result == {"action": "skip", "reason": "help"}
    assert [s["content"] for s in adapter.sent] == [commands.help_text("Print Handy")]


# ---------------------------------------------------------------- the agent lane

def test_agent_lane_answers_first_and_keys_the_session_by_the_live_key():
    adapter = Adapter()
    endpoint = FakeEndpoint(agent_content="The agent says hi.")
    with mock.patch("urllib.request.urlopen", endpoint):
        result = run_hook(make_event(), make_gateway(adapter),
                          cfg_for(agent_url=AGENT_URL, llm_url=LLM_URL))
    assert result == {"action": "skip", "reason": "agent reply"}
    assert [s["content"] for s in adapter.sent] == ["The agent says hi."]
    assert len(endpoint.requests) == 1
    request = endpoint.requests[0]
    assert request["url"] == AGENT_URL
    assert request["headers"]["x-print-handy-session"] == SESSION_KEY
    assert request["body"]["messages"] == [{"role": "user", "content": "hello printer"}]
    assert "model" not in request["body"]


def test_agent_endpoint_failure_posts_the_honest_unavailable_line():
    adapter = Adapter()
    endpoint = FakeEndpoint(fail=ConnectionError("down"))
    with mock.patch("urllib.request.urlopen", endpoint):
        result = run_hook(make_event(), make_gateway(adapter), cfg_for(agent_url=AGENT_URL))
    assert result == {"action": "skip", "reason": "agent unavailable"}
    assert [s["content"] for s in adapter.sent] == [runtime.agent_unavailable_text()]


# ---------------------------------------------------------------- the llm lane

def test_llm_lane_posts_its_reply_when_no_agent_is_configured():
    adapter = Adapter()
    endpoint = FakeEndpoint(llm_content=json.dumps({"reply": "Try /print-help.",
                                                     "command": None}))
    with mock.patch("urllib.request.urlopen", endpoint):
        result = run_hook(make_event(), make_gateway(adapter), cfg_for(llm_url=LLM_URL))
    assert result == {"action": "skip", "reason": "llm reply"}
    assert [s["content"] for s in adapter.sent] == ["Try /print-help."]


def test_llm_failure_falls_back_to_the_short_reply():
    adapter = Adapter()
    endpoint = FakeEndpoint(fail=TimeoutError("slow"))
    with mock.patch("urllib.request.urlopen", endpoint):
        result = run_hook(make_event(), make_gateway(adapter), cfg_for(llm_url=LLM_URL))
    assert result == {"action": "skip", "reason": "command list"}
    assert [s["content"] for s in adapter.sent] == [runtime.unmatched_text("Print Handy")]


def test_llm_named_command_is_redispatched_through_the_handlers():
    adapter = Adapter()
    seen = {}

    async def fake_handler(event, gateway):
        seen["text"] = event.text
        seen["metadata"] = event.metadata
        return {"action": "skip", "reason": "fake handled"}

    endpoint = FakeEndpoint(llm_content=json.dumps({"reply": "Listing your spools.",
                                                    "command": "/spool list"}))
    with mock.patch("urllib.request.urlopen", endpoint), \
            mock.patch.object(plugin, "_DISPATCH_HOOKS", (fake_handler,)), \
            mock.patch.object(plugin, "_ACTIVE_DISPATCH_HOOKS", (fake_handler,)):
        result = run_hook(make_event("what filament do I have?"), make_gateway(adapter),
                          cfg_for(llm_url=LLM_URL))
    assert result == {"action": "skip", "reason": "fake handled"}
    assert seen == {"text": "/spool list", "metadata": {"llm_command": True}}
    assert [s["content"] for s in adapter.sent] == ["Listing your spools."]


def test_llm_named_help_command_posts_the_list_not_the_model_reply():
    adapter = Adapter()
    endpoint = FakeEndpoint(llm_content=json.dumps({"reply": "Sure thing.",
                                                    "command": "/print-help"}))
    with mock.patch("urllib.request.urlopen", endpoint):
        result = run_hook(make_event("what can you do?"), make_gateway(adapter),
                          cfg_for(llm_url=LLM_URL))
    assert result == {"action": "skip", "reason": "help"}
    assert [s["content"] for s in adapter.sent] == [commands.help_text("Print Handy")]


def test_agent_replies_never_gain_a_command_channel():
    # The agent endpoint answers in free text; even a command-shaped reply
    # is posted as-is and never re-dispatched (design §2b).
    adapter = Adapter()
    endpoint = FakeEndpoint(agent_content="/spool list")
    with mock.patch("urllib.request.urlopen", endpoint):
        result = run_hook(make_event(), make_gateway(adapter), cfg_for(agent_url=AGENT_URL))
    assert result == {"action": "skip", "reason": "agent reply"}
    assert [s["content"] for s in adapter.sent] == ["/spool list"]


# ---------------------------------------------------------------- ships dark / guards

def test_nothing_configured_leaves_the_text_to_the_profile_agent():
    adapter = Adapter()
    endpoint = FakeEndpoint(fail=AssertionError("no endpoint may be called"))
    with mock.patch("urllib.request.urlopen", endpoint):
        result = run_hook(make_event(), make_gateway(adapter), cfg_for())
    assert result is None
    assert adapter.sent == []
    assert endpoint.requests == []


def test_non_discord_platforms_are_untouched():
    adapter = Adapter()
    result = run_hook(make_event(platform="telegram"), make_gateway(adapter),
                    cfg_for(chat=False))
    assert result is None
    assert adapter.sent == []


def test_unauthorized_users_are_never_answered_or_claimed():
    adapter = Adapter()
    with mock.patch.object(agent, "ask", exploding("agent.ask")), \
            mock.patch.object(llm, "ask", exploding("llm.ask")):
        result = run_hook(make_event(), make_gateway(adapter, authorized=False),
                          cfg_for(chat=False))
    assert result is None
    assert adapter.sent == []


def test_empty_text_is_not_claimed():
    adapter = Adapter()
    result = run_hook(make_event("   "), make_gateway(adapter), cfg_for(chat=False))
    assert result is None
    assert adapter.sent == []
