"""Matrix standalone platform (#164 stage 3): offline, stubbed homeserver, no network.

The stub opener answers Client-Server API calls from a script: it records
every request (route, JSON body, query) and returns canned results, so the
adapter, the /sync client, BotCore and the real plugin handlers all run
with no Matrix account. Matrix is a pure bot: one test proves the LLM
fallback is never called for it, even with [llm] configured — the same
#73 no-LLM posture as Telegram.
"""
import asyncio
import copy
import io
import json
import urllib.parse
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy import settings
from print_handy.bot import __main__ as bot_main
from print_handy.bot import matrix, matrix_rest, runtime
from print_handy.bot.router import Router
from print_handy.core import link_intake, prompts
from print_handy.hermes import _config

HOMESERVER = "https://matrix.example.org"
TOKEN = "mx-test-access-token"
ROOM = "!prints:example.org"
USER = "@catduck:example.org"
BOT = "@printhandy:example.org"
MXC = "mxc://example.org/media123"


# ---------------------------------------------------------------------- stub homeserver


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class HomeserverStub:
    """A urllib opener stand-in: records calls, returns scripted homeserver results."""

    def __init__(self, file_bytes=b"solid model", upload_size=None, sync_script=()):
        self.calls = []  # (route, details dict)
        self.file_bytes = file_bytes
        self.upload_size = upload_size
        self.sync_script = list(sync_script)
        self._next = 100

    def __call__(self, req, timeout=None):
        url = req.full_url
        path = urllib.parse.urlsplit(url).path
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        body = json.loads(req.data) if req.data and req.headers.get("Content-type") == "application/json" else None
        self._next += 1
        if path.endswith("/account/whoami"):
            self.calls.append(("whoami", {}))
            return _Response(json.dumps({"user_id": BOT, "device_id": "PB"}).encode())
        if path.endswith("/media/v3/config"):
            self.calls.append(("config", {}))
            result = {"m.upload.size": self.upload_size} if self.upload_size else {}
            return _Response(json.dumps(result).encode())
        if path.endswith("/media/v3/upload"):
            self.calls.append(("upload", {"filename": query.get("filename"),
                                          "content_type": req.headers.get("Content-type"),
                                          "bytes": len(req.data or b"")}))
            return _Response(json.dumps({"content_uri": MXC}).encode())
        if "/media/download/" in path:
            self.calls.append(("download", {"url": url,
                                            "auth": req.headers.get("Authorization")}))
            return _Response(self.file_bytes)
        if path.endswith("/join"):
            self.calls.append(("join", {"path": path}))
            return _Response(json.dumps({"room_id": ROOM}).encode())
        if path.endswith("/sync"):
            self.calls.append(("sync", query))
            result = self.sync_script.pop(0) if self.sync_script else {"next_batch": f"s{self._next}", "rooms": {"join": {}}}
            return _Response(json.dumps(result).encode())
        if "/send/m.room.message/" in path:
            self.calls.append(("send", {"path": path, "body": body}))
            return _Response(json.dumps({"event_id": f"$evt{self._next}"}).encode())
        if "/send/m.reaction/" in path:
            self.calls.append(("reaction", {"path": path, "body": body}))
            return _Response(json.dumps({"event_id": f"$evt{self._next}"}).encode())
        raise AssertionError(f"unexpected homeserver call: {req.method} {url}")

    def routes(self):
        return [name for name, _ in self.calls]

    def payloads(self, route):
        return [details for name, details in self.calls if name == route]

    def sent_bodies(self):
        return [(details["body"] or {}).get("body", "") for details in self.payloads("send")]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def cfg(tmp_path):
    data = copy.deepcopy(_config.cfg())
    data["bot"].update(mode="standalone", platform="matrix")
    data["matrix"].update(homeserver_url=HOMESERVER, room_id=ROOM, allowed_user_ids=USER,
                          approval_user_id="", bot_user_id=BOT)
    data["paths"]["cache"] = tmp_path / "cache"
    data["llm"].update(chat_url="", model="")
    return data


def make_core(cfg, stub, tmp_path, router=None):
    client = matrix.MatrixClient(cfg, HOMESERVER, TOKEN, opener=stub, state_path=tmp_path / "since")
    core = runtime.BotCore(client, cfg, router=router if router is not None else Router(), chat_id=ROOM)
    client.core = core
    client.user = S(id=BOT)
    return client, core


def text_event(text, sender=USER, event_id="$e1", room=ROOM):
    return {"type": "m.room.message", "sender": sender, "event_id": event_id,
            "origin_server_ts": 1, "content": {"msgtype": "m.text", "body": text}}


# ---------------------------------------------------------------------- REST sender


def test_rest_send_chunks_at_the_matrix_limit():
    stub = HomeserverStub()
    ok, first = matrix_rest.send(HOMESERVER, TOKEN, ROOM, "x" * 40000, opener=stub)
    assert ok and first == "$evt101"
    assert [len(b) for b in stub.sent_bodies()] == [16000, 16000, 8000]


def test_rest_send_uploads_media_and_posts_a_file_event(tmp_path):
    doc = tmp_path / "plate.gcode"
    doc.write_bytes(b"gcode")
    photo = tmp_path / "frame.jpg"
    photo.write_bytes(b"jpeg")
    stub = HomeserverStub()
    ok, _ = matrix_rest.send(HOMESERVER, TOKEN, ROOM, f"done\nMEDIA:{doc}\nMEDIA:{photo}", opener=stub)
    assert ok
    assert [u["filename"] for u in stub.payloads("upload")] == ["plate.gcode", "frame.jpg"]
    kinds = [(details["body"] or {}).get("msgtype") for details in stub.payloads("send")]
    assert kinds == ["m.text", "m.file", "m.image"]
    file_event = stub.payloads("send")[1]["body"]
    assert file_event["url"] == MXC and file_event["body"] == "plate.gcode"
    assert file_event["info"]["size"] == 5


def test_every_post_carries_an_explicit_empty_m_mentions(tmp_path):
    """Intentional mentions: with m.mentions present a homeserver skips the
    legacy body rules, so an @room or a display name inside a model title
    or filename notifies nobody."""
    doc = tmp_path / "@room plate.gcode"
    doc.write_bytes(b"gcode")
    stub = HomeserverStub()
    ok, _ = matrix_rest.send(HOMESERVER, TOKEN, ROOM,
                             f"Print complete: @room catduck widget\nMEDIA:{doc}", opener=stub)
    assert ok
    events = [details["body"] for details in stub.payloads("send")]
    assert [e["msgtype"] for e in events] == ["m.text", "m.file"]
    assert all(e["m.mentions"] == {} for e in events)
    matrix_rest.edit_message(HOMESERVER, TOKEN, ROOM, "$evt1", "@room edited", opener=stub)
    edit = stub.payloads("send")[-1]["body"]
    assert edit["m.mentions"] == {} and edit["m.new_content"]["m.mentions"] == {}


def test_a_named_matrix_user_is_the_only_one_mentioned():
    stub = HomeserverStub()
    ok, _ = matrix_rest.send(HOMESERVER, TOKEN, ROOM, "x" * 20000 + " @room",
                             opener=stub, mention_user_id=USER)
    assert ok
    events = [details["body"] for details in stub.payloads("send")]
    assert events[0]["body"].startswith(f"{USER} ")
    assert events[0]["m.mentions"] == {"user_ids": [USER]}
    assert events[1]["m.mentions"] == {} and USER not in events[1]["body"]
    for bad in ("catduck", "@room", "<@1>", "@a:b c", "123"):
        stub = HomeserverStub()
        matrix_rest.send(HOMESERVER, TOKEN, ROOM, "done", opener=stub, mention_user_id=bad)
        event = stub.payloads("send")[0]["body"]
        assert event == {"msgtype": "m.text", "body": "done", "m.mentions": {}}


def test_rest_send_needs_a_token_a_homeserver_and_a_room():
    stub = HomeserverStub()
    assert matrix_rest.send(HOMESERVER, "", ROOM, "hi", opener=stub)[0] is False
    assert matrix_rest.send("", TOKEN, ROOM, "hi", opener=stub)[0] is False
    assert matrix_rest.send(HOMESERVER, TOKEN, "", "hi", opener=stub)[0] is False
    assert stub.calls == []


def test_media_config_reads_the_server_upload_limit():
    assert matrix_rest.media_config(HOMESERVER, TOKEN, opener=HomeserverStub(upload_size=1234)) == 1234
    assert matrix_rest.media_config(HOMESERVER, TOKEN, opener=HomeserverStub()) is None


# ---------------------------------------------------------------------- event parsing


def test_parse_event_accepts_room_text_and_files():
    parsed = matrix.parse_event(text_event("hello"), ROOM, BOT)
    assert parsed.content == "hello" and parsed.author.id == USER
    assert runtime.channel_scope(parsed.channel) == (ROOM, None)
    file_event = {"type": "m.room.message", "sender": USER, "event_id": "$e9",
                  "content": {"msgtype": "m.file", "body": "cube.stl", "url": MXC,
                              "info": {"size": 42}}}
    parsed = matrix.parse_event(file_event, ROOM, BOT)
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].filename == "cube.stl" and parsed.attachments[0].size == 42


def test_parse_event_ignores_own_encrypted_edits_and_other_types():
    assert matrix.parse_event(text_event("hi", sender=BOT), ROOM, BOT) is None
    encrypted = {"type": "m.room.encrypted", "sender": USER, "event_id": "$x",
                 "content": {"algorithm": "m.megolm.v1.aes-sha2"}}
    assert matrix.parse_event(encrypted, ROOM, BOT) is None
    edit = text_event("edited")
    edit["content"]["m.relates_to"] = {"rel_type": "m.replace", "event_id": "$e0"}
    edit["content"]["m.new_content"] = {"msgtype": "m.text", "body": "edited"}
    assert matrix.parse_event(edit, ROOM, BOT) is None
    reaction = {"type": "m.reaction", "sender": USER, "event_id": "$r", "content": {}}
    assert matrix.parse_event(reaction, ROOM, BOT) is None


# ---------------------------------------------------------------------- sync client


def _sync_result(events, room=ROOM, next_batch="s2"):
    return {"next_batch": next_batch, "rooms": {"join": {room: {"timeline": {"events": events}}}}}


def test_first_run_starts_from_now_and_persists_the_token(cfg, tmp_path):
    stub = HomeserverStub(sync_script=[_sync_result([text_event("old history")], next_batch="s100")])
    client = matrix.MatrixClient(cfg, HOMESERVER, TOKEN, opener=stub, state_path=tmp_path / "since")

    async def boot():
        with mock.patch.object(matrix.health, "write", lambda status: None):
            await client.start()
            # The snapshot sync was called with no since and its events were NOT replayed.
            assert stub.payloads("sync")[0].get("since") is None
            assert client._since == "s100"
            assert (tmp_path / "since").read_text() == "s100"
            assert client.core is not None and client.core.chat_id == ROOM
            assert client.user.id == BOT  # bot_user_id was configured: no whoami needed
            assert "whoami" not in stub.routes()
            await client.close()

    run(boot())


def test_start_asks_whoami_when_the_bot_id_is_not_configured(cfg, tmp_path):
    cfg["matrix"]["bot_user_id"] = ""
    stub = HomeserverStub()
    client = matrix.MatrixClient(cfg, HOMESERVER, TOKEN, opener=stub, state_path=tmp_path / "since")

    async def boot():
        with mock.patch.object(matrix.health, "write", lambda status: None):
            await client.start()
            assert client.bot_user_id == BOT and "whoami" in stub.routes()
            await client.close()

    run(boot())


def test_poll_once_serves_only_the_configured_room_and_allowed_senders(cfg, tmp_path):
    events = [text_event("from another room", event_id="$a"),
              text_event("hello", sender="@mallory:example.org", event_id="$b"),
              text_event("hello", sender=BOT, event_id="$c"),
              text_event("/print-help", event_id="$d")]
    rooms = {"!other:example.org": {"timeline": {"events": events[:1]}},
             ROOM: {"timeline": {"events": events[1:]}}}
    script = [{"next_batch": "s9", "rooms": {"join": rooms}}]
    stub = HomeserverStub(sync_script=script)
    client, core = make_core(cfg, stub, tmp_path)
    client._since = "s8"

    async def poll():
        dispatched = await client.poll_once()
        await client.drain_handlers()
        return dispatched

    dispatched = run(poll())
    assert stub.payloads("sync")[0]["since"] == "s8"
    assert client._since == "s9" and (tmp_path / "since").read_text() == "s9"
    # Both real messages in the print room were dispatched as handler tasks
    # (P1-65); the bot's own message and the other room's were not. Only the
    # allowed user's /print-help produced a post (the help text).
    assert dispatched == 2
    assert any("/print-next" in body for body in stub.sent_bodies())


def test_poll_once_joins_the_print_room_when_invited(cfg, tmp_path):
    script = [{"next_batch": "s3", "rooms": {"invite": {ROOM: {"invite_state": {"events": []}}}}}]
    stub = HomeserverStub(sync_script=script)
    client, core = make_core(cfg, stub, tmp_path)
    run(client.poll_once())
    assert "join" in stub.routes()
    assert urllib.parse.quote(ROOM, safe="") in stub.payloads("join")[0]["path"]


def test_poll_once_keeps_syncing_while_a_handler_awaits_an_answer(cfg, tmp_path):
    """P1-65: an intake handler parks on a prompt answer that only a LATER
    /sync batch can deliver. poll_once must dispatch the handler and return,
    so the next poll can deliver the answer; the old serial loop awaited the
    handler inline, so the answer could never arrive and every interactive
    flow stalled until its prompt timed out."""
    stub = HomeserverStub(sync_script=[
        _sync_result([text_event("https://makerworld.com/en/models/931302-holder",
                                 event_id="$m1")], next_batch="s2"),
        _sync_result([text_event("2", event_id="$a1")], next_batch="s3"),
        _sync_result([text_event("1", event_id="$a2")], next_batch="s4"),
    ])
    client, core = make_core(cfg, stub, tmp_path)
    client._since = "s1"
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
        # The regression in one line: before the fix this await only returns
        # once the intake handler finishes, and the handler cannot finish
        # until a later poll delivers its answer — a deadlock but for the
        # prompt timeout. Bounded here so the failure is quick.
        dispatched = await asyncio.wait_for(client.poll_once(), timeout=5)
        assert dispatched == 1
        await wait_prompt("Which color?")
        await client.poll_once()  # delivers the typed answer "2"
        await wait_prompt("Start the print?")
        await client.poll_once()  # delivers the typed answer "1"
        assert await client.drain_handlers(timeout=5)

    with mock.patch("print_handy.core.prepared_intake.prepare_link", fake_prepare), \
            mock.patch("print_handy.core.printer_state.load", return_value={}):
        run(flow())
    assert seen["answers"] == (1, 0)
    bodies = stub.sent_bodies()
    assert any("Sliced in White." in b for b in bodies) and bodies[-1] == "Started."
    assert client._since == "s4"


# ---------------------------------------------------------------------- adapter prompts


def test_send_clarify_posts_a_numbered_choice_list(cfg, tmp_path):
    stub = HomeserverStub()
    adapter = matrix.StandaloneAdapter(None, [USER], homeserver=HOMESERVER, token=TOKEN, opener=stub)
    assert adapter.supports_buttons is False and adapter.uses_llm is False
    cid = "ab" * 16
    prompts.register(cid, "matrix:room::user", "Which color?", ["Black", "White"])
    result = run(adapter.send_clarify(chat_id=ROOM, question="Which color?", choices=["Black", "White"],
                                      clarify_id=cid, session_key="flow"))
    assert result.success
    body = stub.sent_bodies()[-1]
    assert "Which color?" in body and "1. Black" in body and "2. White" in body
    assert "Reply with the number, or the choice text." in body


def test_numeric_and_text_answers_resolve_the_prompt(cfg, tmp_path):
    async def scenario(answer):
        stub = HomeserverStub()
        adapter = matrix.StandaloneAdapter(None, [USER], homeserver=HOMESERVER, token=TOKEN, opener=stub)
        cid = "ef" * 16
        key = f"matrix:{ROOM}::{USER}"
        prompts.register(cid, key, "Which color?", ["Black", "White"])
        result = await adapter.send_clarify(chat_id=ROOM, question="Which color?",
                                            choices=["Black", "White"], clarify_id=cid, session_key="flow")
        assert result.success
        assert await adapter.answer_typed(key, answer) is True
        # Closing posts a follow-up note naming the resolved choice text.
        assert any("White" in body for body in stub.sent_bodies()[1:])
        return await asyncio.to_thread(prompts.wait_for_response, cid, 1)

    assert run(scenario("2")) == "White"       # the bare number maps to the second choice
    assert run(scenario("2.")) == "White"      # a trailing full stop is fine
    assert run(scenario("White")) == "White"   # the choice text itself still works


def test_a_typed_answer_posts_its_note_before_it_unblocks_the_flow(cfg, tmp_path):
    """#416 regression pin: resolving a prompt wakes the waiting flow, whose
    next message must not overtake the answer note in the room. The stub
    holds the note's send on a gate; while it is held, the waiter must still
    be blocked. On the pre-fix resolve-then-close sequence the waiter
    returns immediately and this fails deterministically."""
    import threading
    gate = threading.Event()
    entered = threading.Event()

    class GatedStub(HomeserverStub):
        def __call__(self, req, timeout=None):
            body = None
            if req.data and req.headers.get("Content-type") == "application/json":
                body = json.loads(req.data)
            if body and str(body.get("body", "")).startswith("> Answered"):
                entered.set()
                gate.wait(10)
            return super().__call__(req, timeout=timeout)

    async def scenario():
        stub = GatedStub()
        adapter = matrix.StandaloneAdapter(None, [USER], homeserver=HOMESERVER, token=TOKEN, opener=stub)
        cid = "ab" * 16
        key = f"matrix:{ROOM}::{USER}"
        prompts.register(cid, key, "Start the print?", ["Print it"])
        result = await adapter.send_clarify(chat_id=ROOM, question="Start the print?",
                                            choices=["Print it"], clarify_id=cid, session_key="flow")
        assert result.success
        waiter = asyncio.create_task(asyncio.to_thread(prompts.wait_for_response, cid, 10))
        answer = asyncio.create_task(adapter.answer_typed(key, "1"))
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set(), "the answer note was never sent"
        await asyncio.sleep(0.2)
        assert not waiter.done(), "the flow unblocked before the answer note posted (#416)"
        gate.set()
        assert await answer is True
        assert await waiter == "Print it"
        assert stub.sent_bodies()[-1] == "> Answered by message: Print it"

    try:
        run(scenario())
    finally:
        gate.set()


def test_a_timely_answer_survives_a_slow_close_out_note(cfg, tmp_path):
    """P2-166: the answer is reserved before the close-out note posts, so a
    note send that outlasts the prompt's remaining timeout cannot discard a
    timely answer as expired. The stub holds the note for 0.6 s; the waiter
    only has 0.3 s — before the fix the wait expired, the prompt was dropped
    and answer_typed returned False after posting its note."""
    import time as time_module

    class SlowNoteStub(HomeserverStub):
        def __call__(self, req, timeout=None):
            body = None
            if req.data and req.headers.get("Content-type") == "application/json":
                body = json.loads(req.data)
            if body and str(body.get("body", "")).startswith("> Answered"):
                time_module.sleep(0.6)
            return super().__call__(req, timeout=timeout)

    async def scenario():
        stub = SlowNoteStub()
        adapter = matrix.StandaloneAdapter(None, [USER], homeserver=HOMESERVER, token=TOKEN, opener=stub)
        cid = "cd" * 16
        key = f"matrix:{ROOM}::{USER}"
        prompts.register(cid, key, "Start the print?", ["Print it"])
        result = await adapter.send_clarify(chat_id=ROOM, question="Start the print?",
                                            choices=["Print it"], clarify_id=cid, session_key="flow")
        assert result.success
        waiter = asyncio.create_task(asyncio.to_thread(prompts.wait_for_response, cid, 0.3))
        assert await adapter.answer_typed(key, "1") is True
        assert await waiter == "Print it"

    run(scenario())


def test_a_number_with_no_open_prompt_is_not_an_answer(cfg, tmp_path):
    adapter = matrix.StandaloneAdapter(None, [USER], homeserver=HOMESERVER, token=TOKEN,
                                       opener=HomeserverStub())
    assert run(adapter.answer_typed(f"matrix:{ROOM}::{USER}", "2")) is False


def test_a_near_miss_prompt_does_not_block_the_sync_loop(cfg, tmp_path, monkeypatch):
    """P1-109: the did-you-mean wait must not run inside message handling on
    a polling platform — the same loop has to deliver the answer. The
    handler returns at once, the prompt stays open in a background task,
    and the typed answer (the next event) still dispatches the correction."""
    monkeypatch.setattr(runtime, "SUGGESTION_TIMEOUT_S", 30)
    seen = []

    class _Plugin:
        def register(self, router):
            async def record(event, gateway):
                if event.text.startswith("/spool"):
                    seen.append(event)
                    return {"action": "skip", "reason": "recorded"}
                return None
            router.register_hook("pre_gateway_dispatch", record)

    def text_event(body, event_id):
        return {"type": "m.room.message", "sender": USER, "event_id": event_id,
                "content": {"msgtype": "m.text", "body": body}}

    stub = HomeserverStub()
    client, core = make_core(cfg, stub, tmp_path, router=Router(plugin=_Plugin()))

    async def flow():
        out = await asyncio.wait_for(client.handle_event(text_event("/spol list", "$e1"), ROOM),
                                     timeout=5)
        assert out == {"action": "skip", "reason": "did-you-mean suggestion pending"}
        await client.handle_event(text_event("1", "$e2"), ROOM)  # "1" = Yes — run it
        for _ in range(200):
            if seen:
                break
            await asyncio.sleep(0.01)
        assert [e.text for e in seen] == ["/spool list"]

    run(flow())


def test_matrix_help_omits_the_discord_only_live_command(cfg, tmp_path):
    """Help is a platform surface too (P2-123): Matrix help must not advertise
    /print-live, which only the Discord adapter can run."""
    stub = HomeserverStub()
    client, core = make_core(cfg, stub, tmp_path)
    assert "/print-live" not in core.help
    assert "/print-stop" in core.help


# ---------------------------------------------------------------------- BotCore wiring


def test_matrix_botcore_never_calls_the_llm(cfg, tmp_path):
    """The pure-bot posture: [llm] configured, an unmatched message, zero LLM calls."""
    cfg["llm"].update(chat_url="http://llm.invalid/v1", model="m")
    stub = HomeserverStub()
    client, core = make_core(cfg, stub, tmp_path)
    assert core.platform_name == "matrix" and core.chat_id == ROOM
    assert core.use_llm is False
    status = core.status(True)
    assert status["platform"] == "matrix" and status["llm"] is False
    calls = []

    def exploding_ask(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("the Matrix bot called the LLM")

    source = runtime.make_source(USER, ROOM, platform_name="matrix")
    with mock.patch.object(runtime.llm, "ask", exploding_ask), \
            mock.patch.object(runtime.llm, "configured", return_value=True):
        out = run(core.process(runtime.make_event(source, "what is the printer doing?", "$e7")))
    assert calls == []
    assert out == {"action": "skip", "reason": "command list"}
    assert any("didn't understand" in body and "/print-help" in body for body in stub.sent_bodies())
    assert not any("/print-next" in body for body in stub.sent_bodies())


def test_session_keys_are_matrix_scoped(cfg):
    source = runtime.make_source(USER, ROOM, platform_name="matrix")
    assert runtime.session_key(source) == f"matrix:{ROOM}::{USER}"


def test_bot_check_rows_for_matrix(cfg, monkeypatch):
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", TOKEN)
    rows = {label: (ok, detail) for label, ok, detail in bot_main.checks(cfg)}
    assert rows["platform"] == (True, "matrix")
    assert rows["token"][0] is True
    assert rows["homeserver"] == (True, HOMESERVER)
    assert rows["print room"] == (True, ROOM)
    assert rows["allowed users"] == (True, "1 user(s)")
    assert rows["matrix"] == (True, "standard library client")
    assert rows["llm"][0] is True and "pure bot" in rows["llm"][1]
    monkeypatch.delenv("MATRIX_ACCESS_TOKEN")
    rows = {label: (ok, detail) for label, ok, detail in bot_main.checks(cfg)}
    assert rows["token"][0] is False


def test_file_download_uses_the_doc_prefix_the_intake_expects(cfg, tmp_path):
    stub = HomeserverStub(file_bytes=b"solid cube")
    client, core = make_core(cfg, stub, tmp_path)
    parsed = matrix.parse_event({"type": "m.room.message", "sender": USER, "event_id": "$f1",
                                 "content": {"msgtype": "m.file", "body": "cube.stl", "url": MXC,
                                             "info": {"size": 10}}}, ROOM, BOT)
    client._bind(parsed)
    paths = run(core.save_attachments(parsed))
    assert len(paths) == 1
    saved = Path(paths[0])
    assert saved.parent == core.upload_dir and saved.name.startswith("doc_") and saved.name.endswith("_cube.stl")
    assert saved.read_bytes() == b"solid cube"
    downloads = stub.payloads("download")
    assert downloads and "/_matrix/client/v1/media/download/example.org/media123" in downloads[0]["url"]
    assert downloads[0]["auth"] == f"Bearer {TOKEN}"


def test_plugin_gate_serves_matrix_sources():
    from print_handy.bot.router import load_plugin
    assert "matrix" in load_plugin().BOT_PLATFORMS


# ---------------------------------------------------------------------- full intake


def test_matrix_adapter_drives_a_full_intake_through_the_real_handlers(cfg, tmp_path):
    stub = HomeserverStub()
    client, core = make_core(cfg, stub, tmp_path)
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

    async def flow():
        task = asyncio.create_task(client.handle_event(
            text_event("https://makerworld.com/en/models/931302-holder", event_id="$m1")))
        # Answers go by number, as the numbered list invites: a typed
        # "Print it" would be a command in its own right, not an answer.
        for expected, answer in (("Which color?", "2"), ("Start the print?", "1")):
            cid = None
            for _ in range(200):
                cid = next((c for c, row in core.adapter._prompts.items()
                            if row["question"].startswith(expected)), None)
                if cid is not None:
                    break
                await asyncio.sleep(0.01)
            assert cid is not None, f"no prompt {expected!r}: {core.adapter._prompts}"
            await client.handle_event(text_event(answer, event_id=f"$a-{expected}"))
        return await task

    with mock.patch("print_handy.core.prepared_intake.prepare_link", fake_prepare), \
            mock.patch("print_handy.core.printer_state.load", return_value={}):
        out = run(flow())
    assert out == {"action": "skip", "reason": "test intake"}
    assert seen["adapter"] is core.adapter and seen["answers"] == (1, 0)
    assert core.adapter.name == "matrix"
    bodies = stub.sent_bodies()
    assert any("Sliced in White." in b for b in bodies) and bodies[-1] == "Started."


# ---------------------------------------------------------------------- settings


def test_matrix_settings_defaults_and_validation():
    data = settings.load(local=False, environ={})
    assert data["matrix"] == {"homeserver_url": "", "access_token_env": "MATRIX_ACCESS_TOKEN",
                              "room_id": "", "allowed_user_ids": "", "approval_user_id": "",
                              "bot_user_id": "", "upload_max_bytes": 50_000_000}
    data = settings.load(local=False, environ={"PRINT_HANDY__MATRIX__ROOM_ID": ROOM,
                                               "PRINT_HANDY__MATRIX__ALLOWED_USER_IDS": f"{USER}, @other:example.org",
                                               "PRINT_HANDY__MATRIX__HOMESERVER_URL": "https://matrix.org/"})
    assert data["matrix"]["room_id"] == ROOM and data["matrix"]["homeserver_url"] == "https://matrix.org"
    assert settings.matrix_user_ids(data["matrix"]["allowed_user_ids"]) == [USER, "@other:example.org"]
    for env, match in (({"PRINT_HANDY__MATRIX__ALLOWED_USER_IDS": "424242"}, "full Matrix user IDs"),
                       ({"PRINT_HANDY__MATRIX__ROOM_ID": "prints:example.org"}, "starting with '!'"),
                       ({"PRINT_HANDY__MATRIX__HOMESERVER_URL": "matrix.org"}, "homeserver_url"),
                       ({"PRINT_HANDY__MATRIX__ACCESS_TOKEN_ENV": "not a name"}, "access_token_env"),
                       ({"PRINT_HANDY__MATRIX__BOT_USER_ID": "printhandy"}, "bot_user_id"),
                       ({"PRINT_HANDY__MATRIX__UPLOAD_MAX_BYTES": "0"}, "upload_max_bytes")):
        with pytest.raises(settings.SettingsError, match=match):
            settings.load(local=False, environ=env)


def test_a_config_without_a_matrix_section_loads_unchanged(tmp_path):
    home = Path(settings.HOME).read_text()
    legacy = tmp_path / "home.yaml"
    start = home.index("matrix:\n")
    end = home.index("bot:\n")
    legacy.write_text(home[:start] + home[end:])
    data = settings.load(home=legacy, local=False, environ={})
    assert data["bot"]["platform"] == "discord"
    assert data["matrix"]["room_id"] == "" and data["matrix"]["access_token_env"] == "MATRIX_ACCESS_TOKEN"


# ---------------------------------------------------------------------- listener + delivery


def test_listener_post_routes_to_matrix_in_standalone_mode(monkeypatch):
    from print_handy.hermes import moonraker_event_listener as mel
    sent = []
    monkeypatch.setitem(_config.cfg()["bot"], "mode", "standalone")
    monkeypatch.setitem(_config.cfg()["bot"], "platform", "matrix")
    monkeypatch.setitem(_config.cfg()["matrix"], "room_id", ROOM)
    monkeypatch.setitem(_config.cfg()["matrix"], "homeserver_url", HOMESERVER)
    monkeypatch.setattr(mel, "_profile_env_value",
                        lambda name: TOKEN if name == "MATRIX_ACCESS_TOKEN" else "")
    monkeypatch.setattr("print_handy.bot.matrix_rest.send",
                        lambda homeserver, token, room, text: sent.append((homeserver, token, room, text)) or (True, "$e1"))
    monkeypatch.setattr("print_handy.bot.rest.send",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Discord REST used")))
    with mock.patch.object(mel.subprocess, "run", side_effect=AssertionError("hermes send was called")):
        assert mel.post("Print started: cube") is True
    assert sent == [(HOMESERVER, TOKEN, ROOM, "Print started: cube")]


def test_listener_alert_buttons_degrade_to_plain_text_on_matrix(monkeypatch):
    # The reaction post is the Matrix alert path (#1291); when it cannot
    # run (here: no token in this config), the alert still goes out as
    # plain text, and the Discord button post is never touched.
    from print_handy.hermes import moonraker_event_listener as mel
    monkeypatch.setitem(_config.cfg()["bot"], "mode", "standalone")
    monkeypatch.setitem(_config.cfg()["bot"], "platform", "matrix")
    monkeypatch.setattr(mel, "post", lambda message: True)
    with mock.patch("print_handy.hermes.print_alert_buttons.post_with_buttons",
                    side_effect=AssertionError("Discord buttons used")):
        assert mel.post_alert("Print paused", "paused", "cube.gcode") is True
    assert mel.post_plate_picker("plan-1") is True


def test_delivery_upload_limit_follows_the_platform(cfg):
    from print_handy.core import delivery
    cfg["matrix"]["upload_max_bytes"] = 12_345
    assert delivery.upload_limit(cfg) == 12_345
    cfg["bot"]["platform"] = "discord"
    assert delivery.upload_limit(cfg) == cfg["discord"]["upload_max_bytes"]


def test_a_number_cannot_approve_a_print_for_a_non_approver(cfg, tmp_path):
    """2.2.5 review of P1-93: Matrix has no buttons, so its typed "1" is the approval."""
    other = "@member:example.org"

    async def scenario(sender):
        stub = HomeserverStub()
        adapter = matrix.StandaloneAdapter(None, [USER, other], approver=USER, homeserver=HOMESERVER,
                                           token=TOKEN, opener=stub)
        cid = "de" * 16
        key = f"matrix:{ROOM}::{other}"
        prompts.register(cid, key, "Start the print?", ["Print it"], approval=True)
        await adapter.send_clarify(chat_id=ROOM, question="Start the print?", choices=["Print it"],
                                   clarify_id=cid, session_key="flow")
        answered = await adapter.answer_typed(key, "1", user_id=sender)
        still_open = prompts.is_open(cid)
        prompts.resolve_gateway_clarify(cid, prompts.CANCELLED)
        return answered, still_open

    assert run(scenario(other)) == (False, True)
    assert run(scenario(USER)) == (True, False)


# ------------------------------------------------- alert reactions (#1291 item 15)


def reaction_event(key="▶️", target="$alert1", sender=USER, event_id="$r1"):
    return {"type": "m.reaction", "sender": sender, "event_id": event_id,
            "content": {"m.relates_to": {"rel_type": "m.annotation",
                                         "event_id": target, "key": key}}}


def test_parse_reaction_event_accepts_annotations_and_ignores_the_rest():
    parsed = matrix.parse_reaction_event(reaction_event(), ROOM, BOT)
    assert parsed.sender_id == USER and parsed.room_id == ROOM
    assert parsed.target_event_id == "$alert1" and parsed.key == "▶️"
    # The bot's own seeded reactions never act.
    assert matrix.parse_reaction_event(reaction_event(sender=BOT), ROOM, BOT) is None
    # A message is not a reaction, and a reaction without its target or
    # key is not actionable.
    assert matrix.parse_reaction_event(text_event("hi"), ROOM, BOT) is None
    broken = reaction_event()
    del broken["content"]["m.relates_to"]["key"]
    assert matrix.parse_reaction_event(broken, ROOM, BOT) is None
    # parse_event still treats a reaction as no message at all.
    assert matrix.parse_event(reaction_event(), ROOM, BOT) is None


def _alert_adapter(monkeypatch, stub, *, alert, take_result="same",
                   click_text="Resumed. The printer now reports printing."):
    """An adapter whose alert store serves ``alert`` for $alert1; returns the click log."""
    from print_handy.hermes import print_alert_buttons as pab
    clicks = []
    monkeypatch.setattr(pab, "matrix_alert",
                        lambda event_id: ("ab12cd34", alert) if alert and event_id == "$alert1"
                        else (None, None))
    monkeypatch.setattr(pab, "take", lambda aid: alert if take_result == "same" else take_result)
    monkeypatch.setattr(pab, "handle_click",
                        lambda action, a: clicks.append((action, a)) or click_text)
    adapter = matrix.StandaloneAdapter(None, [USER], homeserver=HOMESERVER,
                                       token=TOKEN, opener=stub)
    return adapter, clicks


def test_alert_reaction_runs_the_action_and_posts_the_outcome(monkeypatch):
    stub = HomeserverStub()
    alert = {"kind": "paused", "filename": "f.gcode", "matrix_room_id": ROOM}
    adapter, clicks = _alert_adapter(monkeypatch, stub, alert=alert)
    assert run(adapter.handle_alert_reaction(USER, ROOM, "$alert1", "▶️")) is True
    assert clicks == [("resume", alert)]
    assert any("Resumed. The printer now reports printing." in b for b in stub.sent_bodies())


def test_alert_reaction_without_variation_selector_still_maps(monkeypatch):
    stub = HomeserverStub()
    alert = {"kind": "paused", "filename": "f.gcode", "matrix_room_id": ROOM}
    adapter, clicks = _alert_adapter(monkeypatch, stub, alert=alert)
    assert run(adapter.handle_alert_reaction(USER, ROOM, "$alert1", "⏹")) is True
    assert clicks == [("cancel", alert)]


def test_alert_reaction_stranger_is_refused_before_any_claim(monkeypatch):
    stub = HomeserverStub()
    alert = {"kind": "paused", "filename": "f.gcode", "matrix_room_id": ROOM}
    adapter, clicks = _alert_adapter(monkeypatch, stub, alert=alert)
    from print_handy.hermes import print_alert_buttons as pab
    taken = []
    monkeypatch.setattr(pab, "take", lambda aid: taken.append(aid) or alert)
    assert run(adapter.handle_alert_reaction("@mallory:example.org", ROOM, "$alert1", "⏹️")) is True
    assert clicks == [] and taken == []
    assert any("Not allowed. Nothing changed." in b for b in stub.sent_bodies())


def test_alert_reaction_in_another_room_is_refused(monkeypatch):
    stub = HomeserverStub()
    alert = {"kind": "paused", "filename": "f.gcode", "matrix_room_id": "!other:example.org"}
    adapter, clicks = _alert_adapter(monkeypatch, stub, alert=alert)
    from print_handy.hermes import print_alert_buttons as pab
    taken = []
    monkeypatch.setattr(pab, "take", lambda aid: taken.append(aid) or alert)
    assert run(adapter.handle_alert_reaction(USER, ROOM, "$alert1", "⏹️")) is True
    assert clicks == [] and taken == []
    assert any("Not allowed. Nothing changed." in b for b in stub.sent_bodies())


def test_alert_reaction_second_press_loses_the_claim_race(monkeypatch):
    stub = HomeserverStub()
    alert = {"kind": "paused", "filename": "f.gcode", "matrix_room_id": ROOM}
    adapter, clicks = _alert_adapter(monkeypatch, stub, alert=alert, take_result=None)
    assert run(adapter.handle_alert_reaction(USER, ROOM, "$alert1", "▶️")) is True
    assert clicks == []
    assert any("old or was already answered" in b for b in stub.sent_bodies())


def test_reactions_that_are_not_alert_actions_are_ignored_silently(monkeypatch):
    stub = HomeserverStub()
    alert = {"kind": "paused", "filename": "f.gcode", "matrix_room_id": ROOM}
    adapter, clicks = _alert_adapter(monkeypatch, stub, alert=alert)
    # An unrelated emoji on the alert, and an action emoji on some other
    # message: neither posts anything and neither claims the alert.
    assert run(adapter.handle_alert_reaction(USER, ROOM, "$alert1", "🎉")) is False
    assert run(adapter.handle_alert_reaction(USER, ROOM, "$someone-elses", "▶️")) is False
    assert clicks == [] and stub.sent_bodies() == []


def test_client_routes_reaction_events_from_sync(cfg, tmp_path):
    stub = HomeserverStub(sync_script=[_sync_result([reaction_event()])])
    client, core = make_core(cfg, stub, tmp_path)
    seen = []

    async def fake_handle(sender_id, room_id, target, key):
        seen.append((sender_id, room_id, target, key))
        return True

    core.adapter.handle_alert_reaction = fake_handle
    assert run(client.poll_once()) == 1
    assert seen == [(USER, ROOM, "$alert1", "▶️")]
