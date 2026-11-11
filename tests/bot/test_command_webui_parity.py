"""Web UI parity for every command and every proactive alert: the merge-time guard.

The rule (catduck, 2026-11-11): "make sure the update keeps parity with
webui, that should also be a check that is done on merging". Web chat is
a chat platform like the others (print_handy/bot/web.py), so parity is
checked the way a person would notice its absence:

* **Commands.** This suite enumerates ``commands.COMMANDS`` itself. Each
  command's typed form goes through a chat transport (the fake Discord
  client with the real Discord adapter) and through web chat (the real
  WebChatService, its own loop thread, its real adapter and button
  press path). Both must reach a handler, never the unmatched reply,
  and end with the same outcome kind (BotCore's skip reason). A button
  question either side asks is answered the safe way on both (Cancel,
  Not now, ...), so nothing is ever applied. A new command that web chat
  cannot answer like chat fails here until it can, or until it is
  exempted below with the reason.
* **Proactive alerts.** The bot heartbeat on Discord, Telegram and
  Matrix runs BotCore's ``check_*`` alert methods; every one of them
  must run in web chat too (``web.HEARTBEAT_CHECKS``) or be exempted
  below with the reason. The ZMod update alert, the one alert web chat
  delivers, is driven end to end in tests/bot/test_firmware_alert_transports.py.

It runs in the offline-tests job, a required check, so it gates merges.
CONTRIBUTING.md states the rule beside the fuzzy-equivalent one.
"""
import asyncio
import inspect
import re
import time

import pytest

from corpus import fake_discord
from print_handy.bot import client, commands, matrix, platform, runtime, telegram, web
from print_handy.hermes import _config

# Commands whose web chat outcome legitimately differs from chat's. Each
# entry names why; an entry is still driven through web chat and must get
# an honest answer there (never the unmatched reply), only the outcome
# kind is not compared.
COMMAND_EXEMPTIONS = {
    "print-live": ("Live viewing is a Discord Activity in a voice channel (#311). Web chat's "
                   "adapter reports supports_live_stream=False like Telegram's and Matrix's, "
                   "so, as there, the command does not exist (P2-123: no stub, not in help, "
                   "tests/test_live_dispatch.py) and typing it gets the unmatched reply. The "
                   "Web UI's own camera view is the browser equivalent."),
}

# Heartbeat alert checks a chat platform runs that web chat does not.
HEARTBEAT_EXEMPTIONS = {
    "check_listener_outages": (
        "The outage notice exists because the listener is what posts every print alert "
        "to the chat, so its death is the one failure it cannot report there (#1020). "
        "The Web UI does not get its alerts from the listener: its status page reads "
        "each listener's state live (webui/status.py _listener_section). The notice's "
        "delivered and escalation record is one shared file in the state folder, so a "
        "second process ticking it would race the bot's."),
}

# Proactive posts that do not come from the BotCore heartbeat at all,
# named so the inventory is complete. Not enumerable from code here, so
# listed by hand with the reason.
NON_HEARTBEAT_ALERTS = {
    "listener print-event posts": (
        "Print started, paused, complete, cancelled and error posts are sent by the "
        "listener process through the platform REST senders (bot/rest.py, telegram_rest, "
        "matrix_rest), not through BotCore. The Web UI shows the same print state live on "
        "its status page; a web chat delivery for them is not built yet."),
}

SAFE_LABELS = ("cancel", "not now", "keep", "skip", "no", "later", "done")


def _safe_index(labels):
    for want in SAFE_LABELS:
        for index, label in enumerate(labels):
            if want in str(label).casefold():
                return index
    return None


def _command_text(command):
    """The typed form a person sends with no arguments."""
    return "/" + command.name


# ------------------------------------------------------------------ chat side

def _expire(registry, clarify_id):
    """What a question with no safe button does when nobody answers: expire."""
    registry.resolve_gateway_clarify(clarify_id, registry.CANCELLED)


class _SafeDiscord(fake_discord.FakeDiscord):
    """The fake Discord pressing the safe button, or letting the question expire."""

    registry = None

    def on_send(self, message):
        buttons = self.prompt_buttons(message)
        index = _safe_index([label for label, _ in buttons]) if buttons else None
        if not buttons or index is not None:
            self.clicks = [{"question": "", "button": buttons[index][0]}] if buttons else []
            return super().on_send(message)
        self.messages.append(message)
        self.questions.append({"question": message.content,
                               "choices": [label for label, _ in buttons], "picked": None})
        clarify_id, _index = platform.parse_custom_id(buttons[0][1])
        asyncio.get_running_loop().call_soon(_expire, self.registry, clarify_id)


def _chat_outcome(cfg, text):
    discord = _SafeDiscord(chat_id=int(cfg["discord"]["chat_id"]),
                           operator_id=int(cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, cfg)
    discord.registry = core.adapter.registry

    async def flow():
        out = await asyncio.wait_for(core.handle_message(discord.message(text)), 60)
        for _ in range(200):
            if not discord.pending:
                break
            await asyncio.sleep(0.01)
        return out

    out = asyncio.run(flow())
    replies = [m.content for m in discord.messages if m.content]
    return out, replies


# ------------------------------------------------------------------ web side

class _WebDriver:
    """One WebChatService; records every turn's outcome and presses safely."""

    def __init__(self, cfg):
        self.service = web.WebChatService(cfg)
        self.results = []
        process = self.service.core.process

        async def recording(event, **kwargs):
            out = await process(event, **kwargs)
            self.results.append(out)
            return out

        self.service.core.process = recording
        self.scope = web.scope_for("parity-session")
        self.view = self.service.view(self.scope)

    def outcome(self, text, timeout=60.0):
        seen = len(self.results)
        since = (self.view.entries() or [{"seq": 0}])[-1]["seq"]
        assert self.view.submit(text)
        deadline = time.time() + timeout
        handled = set()
        while time.time() < deadline:
            prompt = self.view.open_prompt()
            if prompt and prompt["clarify_id"] not in handled:
                handled.add(prompt["clarify_id"])
                index = _safe_index(prompt["choices"])
                if index is None:
                    _expire(self.service.adapter.registry, prompt["clarify_id"])
                else:
                    self.view.answer(prompt["clarify_id"], index)
            elif not prompt and len(self.results) > seen and not self.view.busy():
                break
            time.sleep(0.02)
        else:
            raise AssertionError(f"web chat never finished {text!r}: {self.view.entries()}")
        replies = [e.get("content") or e.get("question") or ""
                   for e in self.view.entries(since) if e["kind"] in ("message", "prompt")]
        return self.results[seen], replies


@pytest.fixture
def parity_cfg(cfg, tmp_path, monkeypatch):
    cfg["paths"]["hermes_home"] = tmp_path / "home"
    cfg.setdefault("firmware", {})["update_alerts"] = False
    monkeypatch.setattr(_config, "_override", cfg)
    return cfg


@pytest.fixture
def web_driver(parity_cfg):
    return _WebDriver(parity_cfg)


def _unmatched(core_text, replies):
    return any(core_text in reply for reply in replies)


def test_the_guard_enumerates_the_registry():
    names = {c.name for c in commands.COMMANDS}
    assert set(COMMAND_EXEMPTIONS) <= names, (
        f"stale exemptions: {sorted(set(COMMAND_EXEMPTIONS) - names)}")
    for name, reason in COMMAND_EXEMPTIONS.items():
        assert len(reason) > 40, f"exemption for {name} must say why"


@pytest.mark.parametrize("command", commands.COMMANDS, ids=lambda c: c.name)
def test_every_command_answers_in_web_chat_like_chat(command, parity_cfg, web_driver):
    text = _command_text(command)
    chat_out, chat_replies = _chat_outcome(parity_cfg, text)
    web_out, web_replies = web_driver.outcome(text)
    unmatched = web_driver.service.core.unmatched
    assert web_replies, f"{text} got no answer in web chat"
    assert web_out and web_out.get("reason"), f"{text}: no outcome in web chat: {web_out!r}"
    if command.name in COMMAND_EXEMPTIONS:
        # Web chat behaves like the other platforms without the capability
        # (Telegram, Matrix): no command there, so the short unmatched
        # reply, never a crash and never a half-run flow.
        assert (web_out or {}).get("reason") == "command list", web_out
        return
    assert not _unmatched(unmatched, web_replies), (
        f"{text} fell through to the unmatched reply in web chat: {web_replies}")
    assert chat_replies and not _unmatched(unmatched, chat_replies), (
        f"{text} did not answer on the chat transport: {chat_replies}")
    assert (web_out or {}).get("reason") == (chat_out or {}).get("reason"), (
        f"{text}: web chat ended {web_out!r}, chat ended {chat_out!r}")


# ------------------------------------------------------------------ alerts

_CHECK_CALL = re.compile(r"await\s+self\.core\.(check_\w+)\(\)")


def _chat_heartbeat_checks():
    found = {}
    for module in (client, telegram, matrix):
        found[module.__name__] = set(_CHECK_CALL.findall(inspect.getsource(module)))
    return found


def test_every_heartbeat_alert_check_reaches_web_chat():
    per_platform = _chat_heartbeat_checks()
    assert all(per_platform.values()), per_platform  # the scan found the heartbeats
    union = set().union(*per_platform.values())
    covered = set(web.HEARTBEAT_CHECKS) | set(HEARTBEAT_EXEMPTIONS)
    assert union <= covered, (
        f"alert checks chat runs that web chat neither runs nor exempts: "
        f"{sorted(union - covered)}; add them to web.HEARTBEAT_CHECKS or, with "
        "the reason, to HEARTBEAT_EXEMPTIONS here")
    assert not set(web.HEARTBEAT_CHECKS) & set(HEARTBEAT_EXEMPTIONS)
    assert set(HEARTBEAT_EXEMPTIONS) <= union, "stale heartbeat exemptions"
    for name in web.HEARTBEAT_CHECKS:
        assert inspect.iscoroutinefunction(getattr(runtime.BotCore, name)), name
    for reason in (*HEARTBEAT_EXEMPTIONS.values(), *NON_HEARTBEAT_ALERTS.values()):
        assert len(reason) > 40


def test_web_chat_heartbeat_runs_the_alert_checks(parity_cfg, web_driver):
    service = web_driver.service
    calls = []

    async def fake_check():
        calls.append("check_firmware_updates")
        return []

    service.core.check_firmware_updates = fake_check
    future = asyncio.run_coroutine_threadsafe(service.heartbeat_once(), service._loop)
    future.result(timeout=10)
    # (The service's own background heartbeat may run a pass too.)
    assert set(calls) == set(web.HEARTBEAT_CHECKS)
    # Off, the page shows nothing, so the heartbeat delivers nothing.
    parity_cfg.setdefault("webui", {})["chat_enabled"] = False
    calls.clear()
    asyncio.run_coroutine_threadsafe(service.heartbeat_once(), service._loop).result(timeout=10)
    assert calls == []
