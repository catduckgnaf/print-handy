"""Telegram standalone platform (#164 stage 2): offline, stubbed Bot API, no network.

The stub opener answers Bot API calls from a script: it records every
request (method, JSON or multipart fields) and returns canned results, so
the adapter, the polling client, BotCore and the real plugin handlers all
run with no Telegram account and no discord import in the Telegram path.
Telegram is a pure bot: one test proves the LLM fallback is never called
for it, even with [llm] configured — the #73 no-LLM roll-test.
"""
import asyncio
import copy
import io
import json
import re
from pathlib import Path
from unittest import mock

import pytest

from print_handy import settings
from print_handy.bot import __main__ as bot_main
from print_handy.bot import runtime, telegram, telegram_rest
from print_handy.bot.router import Router
from print_handy.core import link_intake, prompts
from print_handy.runtime import _config
from print_handy.runtime import print_alert_buttons as pab

TOKEN = "123456789:AA" + "x" * 33  # the shape logs.py redacts; never a real token
CHAT = "-1001234567890"
USER = "424242"


# ---------------------------------------------------------------------- stub Bot API


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class BotApiStub:
    """A urllib opener stand-in: records calls, returns scripted Bot API results."""

    def __init__(self, file_bytes=b"solid model"):
        self.calls = []  # (method-or-url-kind, fields dict)
        self.file_bytes = file_bytes
        self._next_id = 100

    def __call__(self, req, timeout=None):
        url = req.full_url
        if "/file/bot" in url:
            self.calls.append(("download", {"url": url}))
            return _Response(self.file_bytes)
        method = url.rsplit("/", 1)[-1]
        fields = self._fields(req)
        self.calls.append((method, fields))
        self._next_id += 1
        result = {"message_id": self._next_id}
        if method == "getMe":
            result = {"id": 999, "username": "printhandybot"}
        elif method == "getFile":
            result = {"file_path": "documents/file_1.stl"}
        elif method == "getUpdates":
            result = []
        return _Response(json.dumps({"ok": True, "result": result}).encode())

    @staticmethod
    def _fields(req):
        body = req.data or b""
        content_type = req.headers.get("Content-type") or req.headers.get("Content-Type") or ""
        if "application/json" in content_type:
            return json.loads(body or b"{}")
        fields = {}
        for match in re.finditer(rb'name="([^"]+)"(?:; filename="([^"]+)")?\r\n(?:[^\r]+\r\n)?\r\n(.*?)\r\n--', body, re.S):
            fields[match.group(1).decode()] = (match.group(2) or b"").decode() or match.group(3).decode(errors="replace")
        return fields

    def methods(self):
        return [name for name, _ in self.calls]

    def payloads(self, method):
        return [fields for name, fields in self.calls if name == method]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def cfg(tmp_path):
    data = copy.deepcopy(_config.cfg())
    data["bot"].update(mode="standalone", platform="telegram")
    data["telegram"].update(chat_id=CHAT, allowed_user_ids=USER, approval_user_id="")
    data["paths"]["cache"] = tmp_path / "cache"
    data["llm"].update(chat_url="", model="")
    return data


def make_core(cfg, stub, router=None):
    client = telegram.TelegramClient(cfg, TOKEN, opener=stub)
    core = runtime.BotCore(client, cfg, router=router if router is not None else Router())
    client.core = core
    return client, core


def test_registered_commands_omit_the_discord_only_live_command(cfg, tmp_path):
    """Telegram's command menu must not offer /print_live (P2-123): live
    viewing is a Discord Activity, the handler deliberately does not run
    here, so registration is filtered by the platform capability."""
    stub = BotApiStub()
    client, core = make_core(cfg, stub)
    client._register_commands()
    [payload] = stub.payloads("setMyCommands")
    names = [entry["command"] for entry in payload["commands"]]
    assert "print_live" not in names
    assert "print_stop" in names and "print_handy" in names


# ---------------------------------------------------------------------- REST sender


def test_rest_send_chunks_at_the_telegram_limit():
    stub = BotApiStub()
    ok, first = telegram_rest.send(TOKEN, CHAT, "x" * 9000, opener=stub)
    assert ok and first == "101"
    pieces = stub.payloads("sendMessage")
    assert [len(p["text"]) for p in pieces] == [4096, 4096, 808]
    assert all(p["chat_id"] == CHAT and "parse_mode" not in p for p in pieces)


def test_rest_send_document_and_photo(tmp_path):
    doc = tmp_path / "plate.gcode"
    doc.write_bytes(b"gcode")
    photo = tmp_path / "frame.jpg"
    photo.write_bytes(b"jpeg")
    stub = BotApiStub()
    ok, _ = telegram_rest.send(TOKEN, CHAT, f"done\nMEDIA:{doc}\nMEDIA:{photo}", opener=stub)
    assert ok
    assert stub.payloads("sendDocument")[0]["document"] == "plate.gcode"
    assert stub.payloads("sendPhoto")[0]["photo"] == "frame.jpg"


def test_rest_send_needs_a_token_and_a_chat():
    stub = BotApiStub()
    assert telegram_rest.send("", CHAT, "hi", opener=stub)[0] is False
    assert telegram_rest.send(TOKEN, "", "hi", opener=stub)[0] is False
    assert stub.calls == []


# ---------------------------------------------------------------------- update parsing


def test_parse_message_keeps_the_text_and_maps_forum_threads():
    """The bot suffix is the mention step's to judge (it knows the bot's own username)."""
    parsed = telegram.parse_message({
        "message_id": 7, "from": {"id": int(USER), "is_bot": False},
        "chat": {"id": int(CHAT)}, "message_thread_id": 55,
        "text": "/print-next@PrintHandyBot plan-1"})
    assert parsed.content == "/print-next@PrintHandyBot plan-1"
    assert parsed.channel.parent_id == int(CHAT) and parsed.channel.id == 55
    assert runtime.channel_scope(parsed.channel) == (CHAT, "55")


def test_parse_message_document_becomes_an_attachment():
    parsed = telegram.parse_message({
        "message_id": 8, "from": {"id": int(USER)}, "chat": {"id": int(CHAT)},
        "document": {"file_id": "F1", "file_name": "cube.stl", "file_size": 12},
        "caption": ""})
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].filename == "cube.stl" and parsed.attachments[0].size == 12


def test_the_bot_suffix_is_stripped_only_for_this_bot(cfg):
    stub = BotApiStub()
    client, core = make_core(cfg, stub)
    client.user = telegram.S(id=999, username="printhandybot")
    assert core.addressed("print it @someone").text == "print it @someone"
    assert core.addressed("/spool@PrintHandyBot list").text == "/spool list"
    assert core.addressed("/spool@OtherBot list").elsewhere
    client.user = None  # getMe never answered: a suffixed command is never guessed to be ours
    assert core.addressed("/spool@Bot list").elsewhere


# ---------------------------------------------------------------------- adapter prompts


def test_send_clarify_builds_an_inline_keyboard_within_the_callback_limit():
    stub = BotApiStub()
    adapter = telegram.StandaloneAdapter(None, [USER], token=TOKEN, opener=stub)
    cid = "ab" * 16
    prompts.register(cid, "telegram:chat::user", "Which color?", ["Black", "White"])
    result = run(adapter.send_clarify(chat_id=CHAT, question="Which color?", choices=["Black", "White"],
                                      clarify_id=cid, session_key="flow"))
    assert result.success
    payload = stub.payloads("sendMessage")[-1]
    keyboard = payload["reply_markup"]["inline_keyboard"]
    data = [row[0]["callback_data"] for row in keyboard]
    assert data == [f"pbq:{cid}:0", f"pbq:{cid}:1"]
    assert all(len(d.encode()) <= 64 for d in data)


def _callback(cid, index, user=USER, query_id="q1"):
    return {"id": query_id, "from": {"id": int(user), "first_name": "Cat"},
            "data": f"pbq:{cid}:{index}",
            "message": {"message_id": 5, "chat": {"id": int(CHAT)}}}


def test_callback_resolves_the_prompt_and_a_stranger_is_refused():
    stub = BotApiStub()
    adapter = telegram.StandaloneAdapter(None, [USER], token=TOKEN, opener=stub)
    cid = "cd" * 16
    key = f"telegram:chat::{USER}"  # the asked user (#1146 initiator rule)
    prompts.register(cid, key, "Which color?", ["Black", "White"])
    run(adapter.send_clarify(chat_id=CHAT, question="Which color?", choices=["Black", "White"],
                             clarify_id=cid, session_key="flow"))

    async def flow():
        assert await adapter.handle_callback_query(_callback(cid, 0, user="999")) is True
        assert prompts.is_open(cid)  # the stranger's press changed nothing
        assert await adapter.handle_callback_query(_callback(cid, 1)) is True
        return await asyncio.to_thread(prompts.wait_for_response, cid, 1)

    assert run(flow()) == "White"
    answers = stub.payloads("answerCallbackQuery")
    assert answers[0]["text"] == "Not allowed. Nothing changed."
    edits = stub.payloads("editMessageText")
    assert edits and "chose White" in edits[0]["text"] and "reply_markup" not in edits[0]


def test_adapter_sends_media_as_documents_and_photos(tmp_path):
    gcode = tmp_path / "a.gcode"
    gcode.write_bytes(b"g")
    stub = BotApiStub()
    adapter = telegram.StandaloneAdapter(None, [USER], token=TOKEN, opener=stub)
    result = run(adapter.send(chat_id=CHAT, content=f"Files:\nMEDIA:{gcode}"))
    assert result.success and "sendDocument" in stub.methods()


# ---------------------------------------------------------------------- BotCore wiring


def test_telegram_botcore_never_calls_the_llm(cfg):
    """The #73 roll-test: [llm] configured, an unmatched message, zero LLM calls."""
    cfg["llm"].update(chat_url="http://llm.invalid/v1", model="m")
    stub = BotApiStub()
    client, core = make_core(cfg, stub)
    assert core.platform_name == "telegram" and core.chat_id == CHAT
    assert core.use_llm is False
    status = core.status(True)
    assert status["platform"] == "telegram" and status["llm"] is False
    calls = []

    def exploding_ask(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("the Telegram bot called the LLM")

    source = runtime.make_source(USER, CHAT, platform_name="telegram")
    with mock.patch.object(runtime.llm, "ask", exploding_ask), \
            mock.patch.object(runtime.llm, "configured", return_value=True):
        out = run(core.process(runtime.make_event(source, "what is the printer doing?", 9)))
    assert calls == []
    assert out == {"action": "skip", "reason": "command list"}
    texts = [p["text"] for p in stub.payloads("sendMessage")]
    assert any("didn't understand" in t and "/print-help" in t for t in texts)  # the short unmatched reply (#467), not an LLM reply
    assert not any("/print-next" in t for t in texts)  # never the full command list as an error response


def test_session_keys_are_telegram_scoped(cfg):
    source = runtime.make_source(USER, CHAT, platform_name="telegram")
    assert runtime.session_key(source) == f"telegram:{CHAT}::{USER}"


def test_bot_check_rows_for_telegram(cfg, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    rows = {label: (ok, detail) for label, ok, detail in bot_main.checks(cfg)}
    assert rows["platform"] == (True, "telegram")
    assert rows["token"][0] is True
    assert rows["print channel"] == (True, CHAT)  # the negative group ID is accepted
    assert rows["allowed users"] == (True, "1 user(s)")
    assert rows["telegram"] == (True, "standard library client")
    assert rows["llm"][0] is True and "pure bot" in rows["llm"][1]
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    rows = {label: (ok, detail) for label, ok, detail in bot_main.checks(cfg)}
    assert rows["token"][0] is False


def test_bot_check_roast_row_for_telegram(cfg, monkeypatch):
    """The roast is not the chat LLM fallback: Telegram stays a pure bot, and
    its check still reports the roast — on by default from the local corpus
    (no LLM, #1002), off when vision.roast_enabled is false."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    rows = {label: (ok, detail) for label, ok, detail in bot_main.checks(cfg)}
    assert rows["roast"][0] is True and rows["roast"][1].startswith("on (local corpus, ")
    cfg["vision"]["roast_enabled"] = False
    rows = {label: (ok, detail) for label, ok, detail in bot_main.checks(cfg)}
    assert rows["roast"] == (True, "off (vision.roast_enabled is false)")


def test_document_download_uses_the_doc_prefix_the_intake_expects(cfg, tmp_path):
    stub = BotApiStub(file_bytes=b"solid cube")
    client, core = make_core(cfg, stub)
    parsed = telegram.parse_message({
        "message_id": 8, "from": {"id": int(USER)}, "chat": {"id": int(CHAT)},
        "document": {"file_id": "F1", "file_name": "cube.stl", "file_size": 10}})
    client._bind(parsed, None)
    paths = run(core.save_attachments(parsed))
    assert len(paths) == 1
    saved = Path(paths[0])
    assert saved.parent == core.upload_dir and saved.name.startswith("doc_") and saved.name.endswith("_cube.stl")
    assert saved.read_bytes() == b"solid cube"
    assert "getFile" in stub.methods() and "download" in stub.methods()


# ---------------------------------------------------------------------- full intake


def test_telegram_adapter_drives_a_full_intake_through_the_real_handlers(cfg):
    stub = BotApiStub()
    client, core = make_core(cfg, stub)
    seen = {}

    async def fake_prepare(event, adapter_arg, state, root, reuse=None, **kwargs):
        seen["adapter"] = adapter_arg
        source = event.source
        key = runtime.session_key(source)
        color = await link_intake.ask(adapter_arg, source, key, "Which color?", ["Black", "White"])
        await adapter_arg.send(chat_id=source.chat_id, content=f"Sliced in {['Black', 'White'][color]}.")
        start = await link_intake.ask(adapter_arg, source, key, "Start the print?", ["Print it"])
        seen["answers"] = (color, start)
        await adapter_arg.send(chat_id=source.chat_id, content="Started.")
        return {"action": "skip", "reason": "test intake"}

    def message_update(text):
        return {"update_id": 1, "message": {"message_id": 42, "from": {"id": int(USER), "is_bot": False},
                                           "chat": {"id": int(CHAT)}, "text": text}}

    async def flow():
        task = asyncio.create_task(client.handle_update(
            message_update("https://makerworld.com/en/models/931302-holder")))
        for expected in ("Which color?", "Start the print?"):
            cid = None
            for _ in range(200):
                cid = next((c for c, row in core.adapter._prompts.items()
                            if row["question"].startswith(expected)), None)
                if cid is not None:
                    break
                await asyncio.sleep(0.01)
            assert cid is not None, f"no prompt {expected!r}: {core.adapter._prompts}"
            index = 1 if expected == "Which color?" else 0
            await client.handle_update({"update_id": 2, "callback_query": _callback(cid, index)})
        return await task

    with mock.patch("print_handy.core.prepared_intake.prepare_link", fake_prepare), \
            mock.patch("print_handy.core.printer_state.load", return_value={}):
        out = run(flow())
    assert out == {"action": "skip", "reason": "test intake"}
    assert seen["adapter"] is core.adapter and seen["answers"] == (1, 0)
    assert core.adapter.name == "telegram"
    texts = [p.get("text", "") for p in stub.payloads("sendMessage")]
    assert any("Sliced in White." in t for t in texts) and texts[-1] == "Started."


class _ScriptedStub(BotApiStub):
    """BotApiStub whose getUpdates answers come from a mutable script of batches."""

    def __init__(self, script=()):
        super().__init__()
        self.script = list(script)

    def __call__(self, req, timeout=None):
        if req.full_url.rsplit("/", 1)[-1] == "getUpdates":
            self.calls.append(("getUpdates", self._fields(req)))
            result = self.script.pop(0) if self.script else []
            return _Response(json.dumps({"ok": True, "result": result}).encode())
        return super().__call__(req, timeout=timeout)


def test_poll_once_keeps_polling_while_a_handler_awaits_an_answer(cfg):
    """Bonus (found during DATA-7): Telegram's poll loop had the P1-65 stall
    in the same shape — poll_once awaited handle_update inline, so while an
    intake handler waited for a prompt answer, the getUpdates round that
    would deliver the callback answer never ran. (The full-intake test
    above bypasses poll_once for exactly this reason.)"""
    link = {"update_id": 1, "message": {"message_id": 42, "from": {"id": int(USER), "is_bot": False},
                                       "chat": {"id": int(CHAT)},
                                       "text": "https://makerworld.com/en/models/931302-holder"}}
    stub = _ScriptedStub([[link]])
    client, core = make_core(cfg, stub)
    seen = {}

    async def fake_prepare(event, adapter_arg, state, root, reuse=None, **kwargs):
        source = event.source
        key = runtime.session_key(source)
        color = await link_intake.ask(adapter_arg, source, key, "Which color?", ["Black", "White"])
        await adapter_arg.send(chat_id=source.chat_id, content=f"Sliced in {['Black', 'White'][color]}.")
        start = await link_intake.ask(adapter_arg, source, key, "Start the print?", ["Print it"])
        seen["answers"] = (color, start)
        await adapter_arg.send(chat_id=source.chat_id, content="Started.")
        return {"action": "skip", "reason": "test intake"}

    async def wait_prompt(question):
        for _ in range(200):
            cid = next((c for c, row in core.adapter._prompts.items()
                        if row["question"].startswith(question)), None)
            if cid is not None:
                return cid
            await asyncio.sleep(0.01)
        raise AssertionError(f"no prompt {question!r}: {core.adapter._prompts}")

    async def flow():
        # Before the fix this await only returned once the intake handler
        # finished, and the handler could not finish until a later
        # getUpdates delivered its answer — bounded here so it fails fast.
        assert await asyncio.wait_for(client.poll_once(), timeout=5) == 1
        assert client._offset == 2  # the update is confirmed at receipt, not after handling
        cid = await wait_prompt("Which color?")
        stub.script.append([{"update_id": 2, "callback_query": _callback(cid, 1)}])
        await client.poll_once()
        cid = await wait_prompt("Start the print?")
        stub.script.append([{"update_id": 3, "callback_query": _callback(cid, 0, query_id="q2")}])
        await client.poll_once()
        assert await client.drain_handlers(timeout=5)

    with mock.patch("print_handy.core.prepared_intake.prepare_link", fake_prepare), \
            mock.patch("print_handy.core.printer_state.load", return_value={}):
        run(flow())
    assert seen["answers"] == (1, 0)
    assert client._offset == 4
    texts = [p.get("text", "") for p in stub.payloads("sendMessage")]
    assert any("Sliced in White." in t for t in texts) and texts[-1] == "Started."


# ---------------------------------------------------------------------- settings


def test_telegram_settings_defaults_and_validation():
    data = settings.load(local=False, environ={})
    assert data["telegram"] == {"token_env": "TELEGRAM_BOT_TOKEN", "chat_id": "",
                                "allowed_user_ids": "", "approval_user_id": "",
                                "upload_max_bytes": 50_000_000}
    data = settings.load(local=False, environ={"PRINT_HANDY__TELEGRAM__CHAT_ID": "-100999",
                                               "PRINT_HANDY__TELEGRAM__ALLOWED_USER_IDS": "1, 2"})
    assert data["telegram"]["chat_id"] == "-100999"
    assert settings.user_ids(data["telegram"]["allowed_user_ids"], "telegram", "Telegram") == ["1", "2"]
    for env, match in (({"PRINT_HANDY__TELEGRAM__CHAT_ID": "abc"}, "chat_id"),
                       ({"PRINT_HANDY__TELEGRAM__ALLOWED_USER_IDS": "me"}, "numeric Telegram user IDs"),
                       ({"PRINT_HANDY__TELEGRAM__TOKEN_ENV": "not a name"}, "token_env"),
                       ({"PRINT_HANDY__TELEGRAM__UPLOAD_MAX_BYTES": "0"}, "upload_max_bytes")):
        with pytest.raises(settings.SettingsError, match=match):
            settings.load(local=False, environ=env)


def test_a_config_without_a_telegram_section_loads_unchanged(tmp_path):
    home = Path(settings.HOME).read_text()
    legacy = tmp_path / "home.yaml"
    start = home.index("telegram:\n")
    end = home.index("bot:\n")
    legacy.write_text(home[:start] + home[end:])
    data = settings.load(home=legacy, local=False, environ={})
    assert data["bot"]["platform"] == "discord"
    assert data["telegram"]["chat_id"] == "" and data["telegram"]["token_env"] == "TELEGRAM_BOT_TOKEN"


# ---------------------------------------------------------------------- listener + delivery


def test_listener_post_routes_to_telegram_in_standalone_mode(monkeypatch):
    from print_handy.runtime import moonraker_event_listener as mel
    sent = []
    monkeypatch.setitem(_config.cfg()["bot"], "mode", "standalone")
    monkeypatch.setitem(_config.cfg()["bot"], "platform", "telegram")
    monkeypatch.setitem(_config.cfg()["telegram"], "chat_id", CHAT)
    monkeypatch.setattr(mel, "_profile_env_value",
                        lambda name: TOKEN if name == "TELEGRAM_BOT_TOKEN" else "")
    monkeypatch.setattr("print_handy.bot.telegram_rest.send",
                        lambda token, chat, text: sent.append((token, chat, text)) or (True, "7"))
    monkeypatch.setattr("print_handy.bot.rest.send",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Discord REST used")))
    with mock.patch.object(mel.subprocess, "run", side_effect=AssertionError("hermes send was called")):
        assert mel.post("Print started: cube") is True
    assert sent == [(TOKEN, CHAT, "Print started: cube")]


def test_listener_alert_buttons_degrade_to_plain_text_on_telegram(monkeypatch):
    from print_handy.runtime import moonraker_event_listener as mel
    monkeypatch.setitem(_config.cfg()["bot"], "mode", "standalone")
    monkeypatch.setitem(_config.cfg()["bot"], "platform", "telegram")
    monkeypatch.setattr(mel, "post", lambda message: True)
    with mock.patch("print_handy.runtime.print_alert_buttons.post_with_buttons",
                    side_effect=AssertionError("Discord buttons used")):
        assert mel.post_alert("Print paused", "paused", "cube.gcode") is True
    assert mel.post_plate_picker("plan-1") is True


def test_delivery_upload_limit_follows_the_platform(cfg):
    from print_handy.core import delivery
    assert delivery.upload_limit(cfg) == 50_000_000
    cfg["bot"]["platform"] = "discord"
    assert delivery.upload_limit(cfg) == cfg["discord"]["upload_max_bytes"]


# ------------------------------------------------- listener alert buttons (#1291 item 15)


def _alert_callback(action, aid, user=USER, query_id="q9", chat=CHAT):
    return {"id": query_id, "from": {"id": int(user), "first_name": "Cat"},
            "data": f"pbalert:{action}:{aid}",
            "message": {"message_id": 9, "chat": {"id": int(chat)}}}


def _alert_adapter(monkeypatch, stub, *, alert, take_result="same", click_text="Resumed. The printer now reports printing."):
    """An adapter whose alert store serves ``alert``; returns the click log."""
    clicks = []
    monkeypatch.setattr(pab, "peek", lambda aid: alert)
    monkeypatch.setattr(pab, "take", lambda aid: alert if take_result == "same" else take_result)
    monkeypatch.setattr(pab, "handle_click",
                        lambda action, a: clicks.append((action, a)) or click_text)
    return telegram.StandaloneAdapter(None, [USER], token=TOKEN, opener=stub), clicks


def test_alert_callback_runs_the_action_strips_the_keyboard_and_posts_the_outcome(monkeypatch):
    stub = BotApiStub()
    alert = {"kind": "paused", "filename": "f.gcode", "telegram_chat_id": CHAT}
    adapter, clicks = _alert_adapter(monkeypatch, stub, alert=alert)
    assert run(adapter.handle_callback_query(_alert_callback("resume", "ab12cd34"))) is True
    assert clicks == [("resume", alert)]
    markups = stub.payloads("editMessageReplyMarkup")
    assert markups and markups[0]["reply_markup"] == {"inline_keyboard": []}
    sent = [p["text"] for p in stub.payloads("sendMessage")]
    assert any("Resumed. The printer now reports printing." in text for text in sent)


def test_alert_callback_stranger_is_refused_before_any_claim(monkeypatch):
    stub = BotApiStub()
    alert = {"kind": "paused", "filename": "f.gcode", "telegram_chat_id": CHAT}
    adapter, clicks = _alert_adapter(monkeypatch, stub, alert=alert)
    taken = []
    monkeypatch.setattr(pab, "take", lambda aid: taken.append(aid) or alert)
    assert run(adapter.handle_callback_query(_alert_callback("cancel", "ab12cd34", user="999"))) is True
    assert clicks == [] and taken == []
    answers = stub.payloads("answerCallbackQuery")
    assert answers[0]["text"] == "Not allowed. Nothing changed."


def test_alert_callback_from_another_chat_is_refused(monkeypatch):
    stub = BotApiStub()
    alert = {"kind": "paused", "filename": "f.gcode", "telegram_chat_id": "-1009999999999"}
    adapter, clicks = _alert_adapter(monkeypatch, stub, alert=alert)
    taken = []
    monkeypatch.setattr(pab, "take", lambda aid: taken.append(aid) or alert)
    assert run(adapter.handle_callback_query(_alert_callback("cancel", "ab12cd34"))) is True
    assert clicks == [] and taken == []
    answers = stub.payloads("answerCallbackQuery")
    assert answers[0]["text"] == "Not allowed. Nothing changed."


def test_alert_callback_without_its_message_is_refused(monkeypatch):
    # Telegram can omit `message` from a callback query; such a press cannot
    # show it came from the alert's chat, so it must not act.
    stub = BotApiStub()
    alert = {"kind": "paused", "filename": "f.gcode", "telegram_chat_id": CHAT}
    adapter, clicks = _alert_adapter(monkeypatch, stub, alert=alert)
    taken = []
    monkeypatch.setattr(pab, "take", lambda aid: taken.append(aid) or alert)
    query = _alert_callback("cancel", "ab12cd34")
    del query["message"]
    assert run(adapter.handle_callback_query(query)) is True
    assert clicks == [] and taken == []
    answers = stub.payloads("answerCallbackQuery")
    assert answers[0]["text"] == "Not allowed. Nothing changed."


def test_alert_callback_stale_alert_answers_old_and_acts_on_nothing(monkeypatch):
    stub = BotApiStub()
    adapter, clicks = _alert_adapter(monkeypatch, stub, alert=None)
    assert run(adapter.handle_callback_query(_alert_callback("resume", "ab12cd34"))) is True
    assert clicks == []
    answers = stub.payloads("answerCallbackQuery")
    assert "old or was already answered" in answers[0]["text"]


def test_alert_callback_second_press_loses_the_claim_race(monkeypatch):
    stub = BotApiStub()
    alert = {"kind": "paused", "filename": "f.gcode", "telegram_chat_id": CHAT}
    adapter, clicks = _alert_adapter(monkeypatch, stub, alert=alert, take_result=None)
    assert run(adapter.handle_callback_query(_alert_callback("resume", "ab12cd34"))) is True
    assert clicks == []
    answers = stub.payloads("answerCallbackQuery")
    assert "old or was already answered" in answers[0]["text"]
