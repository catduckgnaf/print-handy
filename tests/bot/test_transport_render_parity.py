"""Cross-transport render parity matrix.

One event, every surface: for each terminal kind (complete / cancelled /
error) and the key lifecycle kinds (started / paused), the same archive
job is rendered through the shared ``format_event`` renderer as each
platform's install would render it, and through each platform's real
REST sender. The parity contract asserted here is about facts, not
bytes — formats legitimately differ:

* the same core facts on every surface: the outcome verb, the model
  title (the file fact), and the printer name on a multi-printer roster;
* no rendered text carries a starter mention on any platform (#1095):
  the ping is a send-path decision by the starter pings classifier,
  and only Discord's payload carries ``allowed_mentions``;
* each sender delivers the rendered text intact within its own message
  limit, and only Discord's payload carries ``allowed_mentions``;
* unmatched text on the no-LLM transports (Telegram, Matrix) never
  reaches an LLM: BotCore answers with the short unmatched reply and
  the ``command list`` skip reason. (The reply is the short #467 text
  offering ``/print-help``, not the full command list dumped as an
  error — the reason string is where "command list" lives today.)

Gap named: there is no distinct CLI renderer in the tree. The
standalone/CLI surface posts this same shared ``format_event`` text
through the platform REST senders, so it is covered by the sender rows
below rather than by a fourth renderer.
"""
import asyncio
import copy
import io
import json
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.bot import matrix, matrix_rest, rest, runtime, telegram, telegram_rest
from print_handy.bot.platform import BasePlatformAdapter, SendResult
from print_handy.bot.rest import split_media
from print_handy.bot.router import Router
from print_handy.runtime import _config
from print_handy.runtime import discord_print_summary as summary

STARTER = "111222333444555666"
GARAGE = "garage-ad5x"
WORKSHOP = "workshop-ad5x"
VERBS = {"complete": "Print complete", "cancelled": "Print cancelled",
         "error": "Print stopped with an error",
         "started": "Print started", "paused": "Print paused"}
PLATFORMS = ("discord", "telegram", "matrix")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def archive(tmp_path, monkeypatch):
    folder = tmp_path / "archive" / "widget--abc"
    folder.mkdir(parents=True)
    gcode = folder / "plate.gcode"
    gcode.write_text("; estimated printing time (normal mode) = 1h 0m\n"
                     "; total filament used [g] = 12\n")
    job = {"title": "Widget", "gcode": str(gcode), "printer_id": WORKSHOP,
           "started_by": {"platform": "discord", "user_id": STARTER}}
    (folder / "JOB.json").write_text(json.dumps(job))
    monkeypatch.setattr(summary, "ARCHIVE", tmp_path / "archive")
    monkeypatch.setattr(summary, "CONTEXT_PATH", tmp_path / "no-context.json")
    return tmp_path / "archive"


def _install(platform, monkeypatch):
    """The process config as an install of `platform` on a two-printer roster."""
    data = copy.deepcopy(_config.cfg())
    base = data["printers"][0]
    data["printers"] = [{**base, "id": GARAGE, "name": "Garage AD5X", "chat_id": ""},
                        {**base, "id": WORKSHOP, "name": "Workshop AD5X", "chat_id": ""}]
    data["bot"]["platform"] = platform
    monkeypatch.setattr(_config, "_override", data)
    return data


def _render(archive, kind):
    return summary.format_event(kind, "plate.gcode", archive=archive, emit=False)


# ---------------------------------------------------------- the render matrix

@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("kind", ["complete", "cancelled", "error"])
def test_terminal_render_parity(archive, monkeypatch, platform, kind):
    _install(platform, monkeypatch)
    text = _render(archive, kind)
    # Same core facts on every surface.
    assert text.startswith(f"{VERBS[kind]}: Widget"), text
    assert "Printer: Workshop AD5X" in text
    # The rendered text carries no mention on any platform (#1095): the
    # classifier decides the ping at the send path (next test).
    assert "<@" not in text


@pytest.mark.parametrize("kind", ["complete", "cancelled", "error"])
def test_terminal_ping_decision_is_discord_only(archive, monkeypatch, kind):
    """The starter pings classifier, asked the way the listener's send
    path asks it: on a Discord install the terminal kind pings the
    starter; on Telegram/Matrix installs it names nobody."""
    from print_handy.runtime import starter_pings
    monkeypatch.setattr(starter_pings, "STATE", archive.parent / "pings.json")
    _install("discord", monkeypatch)
    assert starter_pings.ping_for(kind, job_key="plate.gcode") == STARTER
    for platform in ("telegram", "matrix"):
        _install(platform, monkeypatch)
        assert starter_pings.ping_for(kind, job_key="plate.gcode") == ""


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("kind", ["started", "paused"])
def test_non_terminal_render_parity_mentions_nobody(archive, monkeypatch, platform, kind):
    _install(platform, monkeypatch)
    text = _render(archive, kind)
    assert text.startswith(f"{VERBS[kind]}: Widget"), text
    assert "Printer: Workshop AD5X" in text
    assert "<@" not in text  # mentions are terminal-only, on every surface


def test_the_notify_copy_never_carries_the_mention(archive, monkeypatch):
    """The MQTT/ntfy copy of an event is the same plain text the chat
    post carries (#1095): no consumer of format_event ever sees a
    mention, on a Discord install too."""
    _install("discord", monkeypatch)
    emitted = []
    with mock.patch("print_handy.notify.emit_queued",
                    lambda name, message: emitted.append((name, message))):
        text = summary.format_event("complete", "plate.gcode", archive=archive, emit=True)
    assert "<@" not in text
    assert emitted and all("<@" not in message for _name, message in emitted)


# ---------------------------------------------------------- the sender matrix

class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _DiscordOpener:
    def __init__(self):
        self.payloads = []

    def __call__(self, req, timeout=None):
        self.payloads.append(json.loads(req.data))
        return _Response(json.dumps({"id": "1"}).encode())


class _TelegramOpener:
    def __init__(self):
        self.payloads = []

    def __call__(self, req, timeout=None):
        body = req.data or b""
        if "application/json" in (req.headers.get("Content-type") or ""):
            self.payloads.append(json.loads(body or b"{}"))
        else:  # multipart: the text rides as a form field
            import re
            match = re.search(rb'name="text"\r\n\r\n(.*?)\r\n--', body, re.S)
            self.payloads.append({"text": match.group(1).decode() if match else ""})
        return _Response(json.dumps({"ok": True, "result": {"message_id": 101}}).encode())


class _MatrixOpener:
    def __init__(self):
        self.payloads = []

    def __call__(self, req, timeout=None):
        self.payloads.append(json.loads(req.data) if req.data else {})
        return _Response(json.dumps({"event_id": "$evt1"}).encode())


def test_each_sender_delivers_the_rendered_text_within_its_limit(archive, monkeypatch):
    _install("discord", monkeypatch)
    text = _render(archive, "complete")
    assert "<@" not in text

    # The Discord send carries the classifier's decision (#1095): the
    # rendered text stays mention-free, and the Discord sender puts the one
    # allowed ping in the posted content (allowed_mentions alone notifies
    # nobody) with allowed_mentions restricted to that user.
    from print_handy.runtime import starter_pings
    monkeypatch.setattr(starter_pings, "STATE", archive.parent / "pings.json")
    mention = starter_pings.ping_for("complete", job_key="plate.gcode")
    assert mention == STARTER
    discord = _DiscordOpener()
    ok, _ = rest.send("tok", "123", text, opener=discord,
                      mention_user_id=mention)
    assert ok and [p["content"] for p in discord.payloads] == [f"<@{STARTER}> {text}"]
    assert discord.payloads[0]["allowed_mentions"] == {"parse": [], "users": [STARTER]}

    telegram_stub = _TelegramOpener()
    ok, _ = telegram_rest.send("tok", "-1001", text, opener=telegram_stub)
    assert ok and [p["text"] for p in telegram_stub.payloads] == [text]
    assert all("allowed_mentions" not in p and "parse_mode" not in p
               for p in telegram_stub.payloads)

    matrix_stub = _MatrixOpener()
    ok, _ = matrix_rest.send("https://matrix.example.org", "tok", "!room:example.org",
                             text, opener=matrix_stub)
    assert ok and [p["body"] for p in matrix_stub.payloads] == [text]
    assert all(p["msgtype"] == "m.text" for p in matrix_stub.payloads)


@pytest.mark.parametrize("limit,module", [(2000, rest), (4096, telegram_rest),
                                          (16000, matrix_rest)])
def test_sender_chunk_limits_differ_but_never_lose_the_headline(limit, module):
    headline = "Print complete: Widget"
    text = headline + "\n" + ("detail line\n" * 3000)
    pieces = module.chunks(text, module.LIMIT) if hasattr(module, "chunks") else None
    if pieces is None:  # telegram_rest/matrix_rest import chunks from rest
        from print_handy.bot.rest import chunks
        pieces = chunks(text, module.LIMIT)
    assert module.LIMIT == limit
    assert len(pieces) > 1 and all(len(p) <= limit for p in pieces)
    assert pieces[0].startswith(headline)


# ------------------------------------------------- unmatched text, no-LLM lanes

class _FakeClient:
    def __init__(self):
        self.user = S(id=300000000000000003)
        self.listeners = {}

    def add_listener(self, func, name=None):
        self.listeners.setdefault(name or func.__name__, []).append(func)


class _FakeAdapter(BasePlatformAdapter):
    """An in-memory Discord-shaped adapter (the test_platform pattern)."""

    name = "discord"
    message_limit = 2000
    supports_buttons = True

    def __init__(self, allowed):
        super().__init__(allowed)
        self.sent = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        body, _files = split_media(content)
        self.sent.append(body)
        return SendResult(True, message_id="1")

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        self._record_prompt(clarify_id, {"choices": list(choices), "question": question})
        return SendResult(True, message_id=str(clarify_id))

    async def close_prompt(self, clarify_id, note):
        self._prompts.pop(str(clarify_id), None)


def _exploding_llm(monkeypatch):
    calls = []

    def boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("a no-LLM transport called the LLM")

    monkeypatch.setattr(runtime.llm, "ask", boom)
    monkeypatch.setattr(runtime.llm, "configured", lambda *a, **k: True)
    return calls


def test_llm_fallback_is_a_discord_only_capability(cfg, monkeypatch):
    """With [llm] fully configured, only the Discord-shaped core may use
    it; Telegram and Matrix cores report use_llm False (#73 posture)."""
    cfg["llm"].update(chat_url="http://llm.invalid/v1", model="m")
    # Chat AI is off by default (#1376); on, it still reaches Discord only.
    cfg["discord"]["chat_llm_enabled"] = True
    discord_core = runtime.BotCore(_FakeClient(), cfg, adapter=_FakeAdapter(["1"]),
                                   router=Router())
    assert discord_core.use_llm is True

    tg_cfg = copy.deepcopy(cfg)
    tg_cfg["bot"].update(mode="standalone", platform="telegram")
    tg_cfg["telegram"].update(chat_id="-1001", allowed_user_ids="42", approval_user_id="")
    tg_core = runtime.BotCore(telegram.TelegramClient(tg_cfg, "t" * 10, opener=_TelegramOpener()),
                              tg_cfg, router=Router())
    assert tg_core.use_llm is False

    mx_cfg = copy.deepcopy(cfg)
    mx_cfg["bot"].update(mode="standalone", platform="matrix")
    mx_cfg["matrix"].update(homeserver_url="https://matrix.example.org",
                            room_id="!r:example.org", allowed_user_ids="@u:example.org",
                            approval_user_id="", bot_user_id="@bot:example.org")
    mx_client = matrix.MatrixClient(mx_cfg, "https://matrix.example.org", "tok",
                                    opener=_MatrixOpener(), state_path=None)
    mx_core = runtime.BotCore(mx_client, mx_cfg, router=Router(), chat_id="!r:example.org")
    assert mx_core.use_llm is False


def test_unmatched_text_on_telegram_gets_the_short_reply_and_no_llm(cfg, monkeypatch):
    cfg["bot"].update(mode="standalone", platform="telegram")
    cfg["telegram"].update(chat_id="-1001", allowed_user_ids="424242", approval_user_id="")
    cfg["llm"].update(chat_url="http://llm.invalid/v1", model="m")
    stub = _TelegramOpener()
    client = telegram.TelegramClient(cfg, "123456789:AA" + "x" * 33, opener=stub)
    core = runtime.BotCore(client, cfg, router=Router())
    client.core = core
    calls = _exploding_llm(monkeypatch)
    source = runtime.make_source("424242", "-1001", platform_name="telegram")
    out = run(core.process(runtime.make_event(source, "what is the printer doing?", 9)))
    assert calls == []
    assert out == {"action": "skip", "reason": "command list"}
    texts = [p["text"] for p in stub.payloads if "text" in p]
    assert any(t == runtime.unmatched_text() for t in texts)
    # The short reply offers the list on request; it is not the list itself.
    assert "/print-help" in runtime.unmatched_text()
    assert "/print-coldpull" not in runtime.unmatched_text()


def test_unmatched_text_on_matrix_gets_the_short_reply_and_no_llm(cfg, tmp_path, monkeypatch):
    cfg["bot"].update(mode="standalone", platform="matrix")
    cfg["matrix"].update(homeserver_url="https://matrix.example.org", room_id="!r:example.org",
                         allowed_user_ids="@u:example.org", approval_user_id="",
                         bot_user_id="@bot:example.org")
    cfg["llm"].update(chat_url="http://llm.invalid/v1", model="m")
    stub = _MatrixOpener()
    client = matrix.MatrixClient(cfg, "https://matrix.example.org", "tok",
                                 opener=stub, state_path=tmp_path / "since")
    core = runtime.BotCore(client, cfg, router=Router(), chat_id="!r:example.org")
    client.core = core
    client.user = S(id="@bot:example.org")
    calls = _exploding_llm(monkeypatch)
    source = runtime.make_source("@u:example.org", "!r:example.org", platform_name="matrix")
    out = run(core.process(runtime.make_event(source, "what is the printer doing?", "$e7")))
    assert calls == []
    assert out == {"action": "skip", "reason": "command list"}
    bodies = [p.get("body", "") for p in stub.payloads]
    assert any(b == runtime.unmatched_text() for b in bodies)


def test_unmatched_text_on_discord_without_an_llm_gets_the_same_short_reply(cfg):
    # Discord's pure-bot posture is its own operator switch (#466):
    # chat_llm_enabled=false closes the fallback even with [llm] set.
    cfg["discord"]["chat_llm_enabled"] = False
    adapter = _FakeAdapter([cfg["discord"]["allowed_user_ids"]])
    core = runtime.BotCore(_FakeClient(), cfg, adapter=adapter, router=Router())
    assert core.use_llm is False
    source = runtime.make_source(cfg["discord"]["allowed_user_ids"],
                                 cfg["discord"]["chat_id"], platform_name="discord")
    out = run(core.process(runtime.make_event(source, "what is the printer doing?", 9)))
    assert out == {"action": "skip", "reason": "command list"}
    assert adapter.sent and adapter.sent[-1] == runtime.unmatched_text()
