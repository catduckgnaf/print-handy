"""AI is for designing models (#1376): the shipped defaults, pinned.

A stock install makes no stray-chat model call on any platform
(``discord.chat_llm_enabled`` and ``webui.chat_llm_enabled`` default
false; Telegram and Matrix are pure bots by adapter) and sends no camera
frame or photo to a vision model endpoint (``vision.ai_enabled`` defaults
false), while ``/print-design`` still reaches its AI route. Every check
here goes through the entry an operator reaches: the settings loader,
BotCore's message path, the web chat service, the listener's camera-watch
gate and verdict call, the purchase-intake gate and ``bot check``.
"""
import asyncio
import json
import time
from pathlib import Path
from unittest import mock

import pytest

from corpus import fake_discord
from print_handy import ai_routes, settings
from print_handy.bot import __main__ as bot_main
from print_handy.bot import agent, runtime, web
from print_handy.bot.matrix import StandaloneAdapter as MatrixAdapter
from print_handy.bot.router import Router
from print_handy.bot.telegram import StandaloneAdapter as TelegramAdapter
from print_handy.core import design
from print_handy.runtime import _config, local_empty, local_vision, purchase_intake, visual_watch
from print_handy.runtime import moonraker_event_listener as mel

ROOT = Path(__file__).parents[2]
LLM_URL = "http://llm.invalid/v1"
VISION_URL = "http://vision.invalid/v1/chat/completions"


def run(coro):
    return asyncio.run(coro)


def exploding(calls, what):
    def call(*args, **kwargs):
        calls.append((what, args, kwargs))
        raise AssertionError(f"{what} was called with AI off by default")
    return call


@pytest.fixture
def stock_cfg(cfg):
    """The test settings with every model endpoint configured and no AI switch touched.

    [llm] and [agent] are set (as a /print-design install leaves them),
    and so is the vision endpoint: the defaults alone must keep chat and
    camera checks away from them.
    """
    cfg["llm"].update(chat_url=LLM_URL, model="luna")
    cfg.setdefault("agent", {}).update(chat_url="http://agent.invalid/v1/chat/completions", model="a")
    cfg["services"]["vision_chat_url"] = VISION_URL
    for section, key in (("discord", "chat_llm_enabled"), ("webui", "chat_llm_enabled"),
                         ("vision", "ai_enabled")):
        cfg.setdefault(section, {}).pop(key, None)
    return cfg


# ---------------------------------------------------------------------- the loader


def test_stock_settings_load_with_chat_ai_and_vision_ai_off():
    data = settings.load(local=False, environ={})
    assert data["discord"]["chat_llm_enabled"] is False
    assert data["webui"]["chat_llm_enabled"] is False
    assert data["vision"]["ai_enabled"] is False
    # The operator can still turn each one back on, from the environment too.
    data = settings.load(local=False, environ={"PRINT_HANDY__DISCORD__CHAT_LLM_ENABLED": "true",
                                               "PRINT_HANDY__WEBUI__CHAT_LLM_ENABLED": "true",
                                               "PRINT_HANDY__VISION__AI_ENABLED": "true"})
    assert data["discord"]["chat_llm_enabled"] is True
    assert data["webui"]["chat_llm_enabled"] is True
    assert data["vision"]["ai_enabled"] is True
    with pytest.raises(settings.SettingsError, match="AI_ENABLED must be true or false"):
        settings.load(local=False, environ={"PRINT_HANDY__VISION__AI_ENABLED": "sometimes"})


@pytest.mark.parametrize("name", ["docker.toml", "docker.yaml"])
def test_shipped_docker_configs_keep_chat_ai_and_vision_ai_off(name):
    data = settings.load(local=ROOT / "config" / name, environ={})
    assert data["discord"]["chat_llm_enabled"] is False
    assert data["webui"]["chat_llm_enabled"] is False
    assert data["vision"]["ai_enabled"] is False


def test_compose_does_not_hard_default_the_ai_switches():
    """A ${...:-true} Compose entry would undo the new defaults (P2-88 rule)."""
    import yaml
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    env = compose["services"]["combined"]["environment"]
    assert "PRINT_HANDY__DISCORD__CHAT_LLM_ENABLED" not in env
    assert "PRINT_HANDY__VISION__AI_ENABLED" not in env


# ---------------------------------------------------------------------- stray chat


def test_discord_unmatched_text_makes_no_model_call_by_default(stock_cfg):
    """The #1373 symptom: "Cancel the print" style free text never reaches a model."""
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(stock_cfg["discord"]["chat_id"]),
                                       operator_id=int(stock_cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, stock_cfg, router=Router())
    assert core.chat_llm_enabled is False and core.use_llm is False
    calls = []
    with mock.patch.object(runtime.llm, "ask", exploding(calls, "llm.ask")), \
            mock.patch.object(agent, "ask", exploding(calls, "agent.ask")):
        out = run(core.handle_message(discord.message("can you make my printer go brrr")))
    assert calls == []
    assert out == {"action": "skip", "reason": "command list"}
    assert discord.sent == [core.unmatched]


def test_discord_chat_ai_comes_back_when_turned_on(stock_cfg):
    stock_cfg["discord"]["chat_llm_enabled"] = True
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(stock_cfg["discord"]["chat_id"]),
                                       operator_id=int(stock_cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, stock_cfg, router=Router())
    assert core.chat_llm_enabled is True and core.use_llm is True


def test_web_chat_unmatched_text_makes_no_model_call_by_default(stock_cfg, monkeypatch):
    monkeypatch.setattr(_config, "_override", stock_cfg)
    service = web.WebChatService(stock_cfg)
    assert service.core.use_llm is False and web.chat_llm_enabled(stock_cfg) is False
    calls = []
    with mock.patch.object(runtime.llm, "ask", exploding(calls, "llm.ask")), \
            mock.patch.object(agent, "ask", exploding(calls, "agent.ask")):
        assert service.submit("can you make my printer go brrr") is True
        deadline = time.time() + 10
        while time.time() < deadline and not any(
                e["kind"] == "message" for e in service.entries()):
            time.sleep(0.02)
    assert calls == []
    assert any("didn't understand" in (e.get("content") or "").casefold()
               for e in service.entries() if e["kind"] == "message")


def test_telegram_and_matrix_never_use_a_chat_model():
    assert TelegramAdapter.uses_llm is False and MatrixAdapter.uses_llm is False


def test_bot_check_names_the_chat_switch_off_by_default(stock_cfg, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x" * 20)
    rows = {label: (ok, detail) for label, ok, detail in bot_main.checks(stock_cfg)}
    assert rows["llm"] == (True, "off (discord.chat_llm_enabled is false: no chat LLM; "
                                 "unmatched messages get the short not-understood reply)")
    assert rows["agent"][1].startswith("off (discord.chat_llm_enabled is false")


# ---------------------------------------------------------------------- /print-design keeps AI


def test_print_design_reaches_its_ai_route_with_chat_and_vision_ai_off(stock_cfg, monkeypatch):
    """Design is the one AI surface left on: the model is called through web chat's /print-design."""
    stock_cfg.setdefault("design", {})["enabled"] = None  # auto: on once an endpoint resolves
    monkeypatch.setattr(_config, "_override", stock_cfg)
    assert design.effective_enabled(stock_cfg) is True
    seen = []

    def fake_complete(cfg, messages):
        seen.append(messages)
        raise RuntimeError("model reached; stop the turn here")

    monkeypatch.setattr(design, "http_complete", fake_complete)
    service = web.WebChatService(stock_cfg)
    assert service.core.use_llm is False
    assert service.submit("/print-design a small storage box") is True
    deadline = time.time() + 15
    while time.time() < deadline and not seen:
        time.sleep(0.02)
    assert seen, f"/print-design never called its model; entries: {service.entries()}"
    assert "small storage box" in json.dumps(seen[0])


# ---------------------------------------------------------------------- camera vision


@pytest.fixture
def vision_endpoint(stock_cfg, monkeypatch):
    """A fully configured vision endpoint (URL, key and PRINT_VISION_URL) with the default switch."""
    monkeypatch.setattr(_config, "_override", stock_cfg)
    monkeypatch.setenv("PRINT_VISION_URL", "http://detector.invalid/judge")
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: "key" if name == "CLIPROXY_API_KEY" else "")
    monkeypatch.setattr(visual_watch, "_vision_missing_logged", False)
    monkeypatch.setattr(local_vision, "prefetch_model", lambda *a, **k: None)
    return stock_cfg


def _no_network(calls):
    return mock.patch.multiple(
        "urllib.request", urlopen=exploding(calls, "urllib.request.urlopen")), \
        mock.patch.object(ai_routes, "_post", exploding(calls, "ai_routes._post"))


def test_camera_watch_is_off_and_sends_nothing_with_vision_ai_off(vision_endpoint):
    assert visual_watch.vision_ai_enabled() is False
    # The listener's own gate: the watch does not run on the configured endpoint.
    assert mel._vision_ready() is False
    reason = visual_watch.watch_unavailable_reason(lambda: "key", prefetch=False)
    assert visual_watch.VISION_AI_OFF in reason
    calls = []
    urlopen_patch, post_patch = _no_network(calls)
    with urlopen_patch, post_patch:
        verdict = visual_watch.visual_verdict(
            b"jpeg", visual_watch.IDLE_CHECK_PROMPT_TEXT, chat_url=mel._vision_chat_url,
            model="m", api_key=lambda: "key", context="")
        extracted = visual_watch.visual_extract(
            b"jpeg", "read the label", chat_url=mel._vision_chat_url, model="m",
            api_key=lambda: "key", context="")
    assert calls == []
    assert verdict == ("uncertain", visual_watch.VISION_AI_OFF)
    assert extracted is None
    # Label and purchase reads are not offered either.
    assert purchase_intake.ai_route_configured() is False


def test_local_routes_still_answer_with_vision_ai_off(vision_endpoint, monkeypatch):
    vision_endpoint["vision"]["local_detector_enabled"] = True
    calls = []
    monkeypatch.setattr(local_vision, "local_verdict", lambda image, streak=None: ("ok", "local"))
    monkeypatch.setattr(local_empty, "idle_verdict", lambda image: ("empty_idle", "reference"))
    urlopen_patch, post_patch = _no_network(calls)
    with urlopen_patch, post_patch:
        watch = visual_watch.visual_verdict(b"jpeg", "p", chat_url=mel._vision_chat_url,
                                            model="m", api_key=lambda: "key", context="")
        idle = visual_watch.visual_verdict(b"jpeg", "p", chat_url=mel._vision_chat_url,
                                           model="m", api_key=lambda: "key", context="",
                                           idle=True)
    assert calls == []
    assert watch == ("ok", "local")
    assert idle == ("empty_idle", "reference")


def test_vision_endpoint_is_used_again_when_vision_ai_is_on(vision_endpoint, monkeypatch):
    vision_endpoint["vision"]["ai_enabled"] = True
    monkeypatch.delenv("PRINT_VISION_URL")
    assert visual_watch.watch_unavailable_reason(lambda: "key", prefetch=False) == ""
    assert mel._vision_ready() is True
    assert purchase_intake.ai_route_configured() is True
    sent = []
    monkeypatch.setattr(ai_routes, "_post", lambda leg, messages, images, *a: sent.append(leg.url)
                        or '{"verdict": "ok", "reason": "fine"}')
    verdict = visual_watch.visual_verdict(b"jpeg", "p", chat_url=mel._vision_chat_url,
                                          model="m", api_key=lambda: "key", context="")
    assert verdict == ("ok", "fine")
    assert sent == [VISION_URL]


def test_bot_check_reports_vision_ai_off_with_an_endpoint_configured(stock_cfg, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x" * 20)
    rows = {label: (ok, detail) for label, ok, detail in bot_main.checks(stock_cfg)}
    ok, detail = rows["vision ai"]
    assert ok is True and detail.startswith("off (AI camera checks are off")
    assert "set vision.ai_enabled to true" in detail
    stock_cfg["vision"]["ai_enabled"] = True
    rows = {label: (ok, detail) for label, ok, detail in bot_main.checks(stock_cfg)}
    assert rows["vision ai"][1].startswith("on (vision.ai_enabled")
