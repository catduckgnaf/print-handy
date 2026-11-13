"""Mentioning the bot instead of typing a slash command, on every platform.

"@Print Handy spool list" runs /spool list. Who the bot is comes from the
platform's logged-in account and the configured bot_name, never a
hard-coded name: a bot renamed "Shop Bot" answers to that name, and plain
"Print Handy" text does not wake it. A message with no mention routes
exactly as before, and a mention outside the print channels gets the
one-line "only works in its print channel" notice, throttled.
"""
import asyncio
import json
import time
from types import SimpleNamespace as S

import pytest

from corpus import fake_discord
from print_handy.bot import matrix, mention, runtime, telegram, web
from print_handy.bot.router import Router

from . import test_matrix as tm
from . import test_telegram as tt


def run(coro):
    return asyncio.run(coro)


def recording_router(seen):
    """Records every /spool command a handler would see; takes nothing else."""
    class _Plugin:
        def register(self, router):
            async def record(event, gateway):
                if event.text.startswith("/spool"):
                    seen.append(event.text)
                    return {"action": "skip", "reason": "recorded"}
                return None
            router.register_hook("pre_gateway_dispatch", record)
    return Router(plugin=_Plugin())


# ---------------------------------------------------------------------- the shared step

@pytest.mark.parametrize("text,expected", [
    ("spool list", "/spool list"),
    ("/spool list", "/spool list"),
    ("stop", "/print-stop"),
    ("print-stop", "/print-stop"),
    ("print_stop", "/print-stop"),
    ("handy setup", "/print-handy setup"),
    ("design a 20 mm cube", "/design a 20 mm cube"),
    ("help", "/print-help"),
    ("what is loaded", "what is loaded"),
    ("print it", "print it"),
])
def test_a_command_word_after_a_mention_becomes_that_command(text, expected):
    assert mention.command_text(text) == expected


def test_no_mention_leaves_the_text_exactly_as_typed():
    identity = mention.Identity(names=("Print Handy",), tokens=("<@3>",))
    for text in ("spool list", "  print it  ", "stop", "print handy is great", "hey @someone"):
        assert mention.address(text, identity) == mention.Addressed(text)


def test_the_plain_name_counts_only_in_front_of_a_command():
    identity = mention.Identity(names=("Print Handy",))
    assert mention.address("Print Handy, spool list", identity).text == "/spool list"
    assert mention.address("print handy: help", identity).text == "/print-help"
    said = mention.address("print handy is great", identity)
    assert said.text == "print handy is great" and not said.mentioned
    assert mention.address("print-handy setup", identity).text == "print-handy setup"


def test_an_explicit_mention_strips_and_a_bare_one_asks_for_help():
    identity = mention.Identity(names=("Print Handy",), tokens=("<@3>",))
    assert mention.address("<@3>", identity) == mention.Addressed("/print-help", mentioned=True)
    assert mention.address("@Print Handy", identity).text == "/print-help"
    assert mention.address("@print handy what is loaded", identity).text == "what is loaded"
    assert mention.address("what is <@3> doing", identity).text == "what is doing"


def test_a_mention_keeps_the_rest_of_a_multi_line_message():
    identity = mention.Identity(names=("Print Handy",), tokens=("<@3>",))
    assert mention.address("<@3> design a box\nwith a lid", identity).text == "/design a box\nwith a lid"


# ---------------------------------------------------------------------- Discord

def discord_core(cfg, seen=None, name=None):
    if name:
        cfg["discord"]["bot_name"] = name
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    if name:
        discord.bot_user.name = discord.bot_user.display_name = name
    router = recording_router(seen) if seen is not None else None
    return discord, runtime.BotCore(discord.client, cfg, router=router)


@pytest.mark.parametrize("form", [
    "<@{id}> /spool list", "<@!{id}> spool list", "<@{id}>  spool   list", "spool list <@{id}>",
    "@Print Handy spool list", "@print handy /spool list", "Print Handy, spool list",
    "@Print Handy: spool list",
])
def test_discord_mention_forms_run_the_command(cfg, form):
    seen = []
    discord, core = discord_core(cfg, seen)
    run(core.handle_message(discord.message(form.format(id=discord.bot_user.id))))
    assert seen == ["/spool list"]


def test_discord_mention_of_the_bots_managed_role_counts(cfg):
    seen = []
    discord, core = discord_core(cfg, seen)
    message = discord.message("<@&4242> spool list")
    message.guild = S(id=1, me=S(nick="Printy", display_name="Printy"), self_role=S(id=4242))
    run(core.handle_message(message))
    message = discord.message("Printy, spool list")
    message.guild = S(id=1, me=S(nick="Printy", display_name="Printy"), self_role=S(id=4242))
    run(core.handle_message(message))
    other_role = discord.message("<@&5555> spool list")
    other_role.guild = S(id=1, me=S(nick=None, display_name="Print Handy"), self_role=S(id=4242))
    run(core.handle_message(other_role))
    assert seen == ["/spool list", "/spool list"]


def test_a_mention_of_someone_else_is_not_the_bot(cfg):
    seen = []
    discord, core = discord_core(cfg, seen)
    run(core.handle_message(discord.message("<@123456789> spool list")))
    assert seen == []


def test_without_a_mention_a_bare_command_word_is_not_a_command(cfg):
    seen = []
    discord, core = discord_core(cfg, seen)
    out = run(core.handle_message(discord.message("spool list")))
    assert seen == [] and out == {"action": "skip", "reason": "command list"}
    assert discord.sent == [core.unmatched]


def test_a_renamed_bot_answers_to_its_own_name_only(cfg):
    seen = []
    discord, core = discord_core(cfg, seen, name="Shop Bot")
    run(core.handle_message(discord.message("@Shop Bot spool list")))
    run(core.handle_message(discord.message("shop bot, spool list")))
    assert seen == ["/spool list", "/spool list"]
    run(core.handle_message(discord.message("@Print Handy spool list")))
    run(core.handle_message(discord.message("Print Handy, spool list")))
    assert seen == ["/spool list", "/spool list"]
    assert core.help.startswith("**Shop Bot**") and '"@Shop Bot spool list"' in core.help


def test_set_up_intent_follows_the_configured_name():
    assert runtime.command_suggestion("set up shop bot", "Shop Bot") == "/print-handy setup"
    assert runtime.command_suggestion("please setup Shop Bot", "Shop Bot") == "/print-handy setup"
    assert runtime.command_suggestion("set up print handy", "Shop Bot") == "/print-handy setup"
    assert runtime.command_suggestion("set up print handy") == "/print-handy setup"
    assert runtime.command_suggestion("set up shop bot") is None


def test_discord_mention_outside_the_print_channel_gets_one_notice(cfg):
    seen = []
    discord, core = discord_core(cfg, seen)
    elsewhere = discord.add_channel(555)
    bot = discord.bot_user.id
    assert run(core.handle_message(discord.message(f"<@{bot}> spool list", channel=elsewhere))) is None
    assert run(core.handle_message(discord.message(f"<@{bot}> spool list", channel=elsewhere))) is None
    assert run(core.handle_message(discord.message("spool list", channel=elsewhere))) is None
    assert run(core.handle_message(discord.message("Print Handy, spool list", channel=elsewhere))) is None
    assert seen == []
    assert [m.content for m in discord.messages] == ["Print Handy only works in its print channel."]
    assert discord.messages[0].channel is elsewhere


def test_a_failed_notice_is_logged_not_raised(cfg, caplog):
    discord, core = discord_core(cfg)
    elsewhere = discord.add_channel(556)

    async def refuse(*args, **kwargs):
        raise PermissionError("missing permissions")

    elsewhere.send = refuse
    with caplog.at_level("INFO"):
        out = run(core.handle_message(discord.message(f"<@{discord.bot_user.id}> help", channel=elsewhere)))
    assert out is None and "missing permissions" in caplog.text


# ---------------------------------------------------------------------- Telegram

def telegram_update(text, chat=tt.CHAT, user=tt.USER, update_id=1):
    return {"update_id": update_id, "message": {"message_id": update_id, "from": {"id": int(user)},
                                                 "chat": {"id": int(chat)}, "text": text}}


def telegram_core(seen, cfg, username="printhandybot", name=None):
    cfg["bot"]["platform"] = "telegram"
    cfg["telegram"].update(chat_id=tt.CHAT, allowed_user_ids=tt.USER, approval_user_id="")
    if name:
        cfg["telegram"]["bot_name"] = name
    stub = tt.BotApiStub()
    client, core = tt.make_core(cfg, stub, router=recording_router(seen))
    client.user = S(id=999, username=username)
    return stub, client, core


@pytest.mark.parametrize("form", [
    "@printhandybot spool list", "@PrintHandyBot /spool list", "/spool@printhandybot list",
    "/spool@PrintHandyBot list", "Print Handy, spool list", "/spool list",
])
def test_telegram_mention_forms_run_the_command(cfg, form):
    seen = []
    _stub, client, _core = telegram_core(seen, cfg)
    run(client.handle_update(telegram_update(form)))
    assert seen == ["/spool list"]


def test_telegram_command_for_another_bot_is_ignored(cfg):
    seen = []
    stub, client, _core = telegram_core(seen, cfg)
    assert run(client.handle_update(telegram_update("/spool@OtherBot list"))) is None
    assert run(client.handle_update(telegram_update("/print-help@OtherBot"))) is None
    assert seen == [] and stub.payloads("sendMessage") == []


def test_telegram_mention_of_another_bot_is_ignored(cfg):
    """Every Telegram bot username ends in "bot": "@OtherBot ..." is for a different bot."""
    seen = []
    stub, client, _core = telegram_core(seen, cfg)
    assert run(client.handle_update(telegram_update("@OtherBot spool list"))) is None
    assert run(client.handle_update(telegram_update("@other_bot, help"))) is None
    assert seen == [] and stub.payloads("sendMessage") == []
    run(client.handle_update(telegram_update("@PrintHandyBot spool list")))
    assert seen == ["/spool list"]


def test_telegram_mention_of_a_person_keeps_todays_behavior(cfg):
    seen = []
    stub, client, core = telegram_core(seen, cfg)
    assert core.addressed("@SomePerson spool list") == mention.Addressed("@SomePerson spool list")
    out = run(client.handle_update(telegram_update("@SomePerson spool list")))
    assert seen == [] and out == {"action": "skip", "reason": "command list"}
    assert [p["text"] for p in stub.payloads("sendMessage")] == [core.unmatched]


def test_telegram_renamed_bot_and_unrouted_chat(cfg):
    seen = []
    stub, client, core = telegram_core(seen, cfg, username="shopbot", name="Shop Bot")
    run(client.handle_update(telegram_update("Shop Bot: spool list")))
    run(client.handle_update(telegram_update("@printhandybot spool list")))
    assert seen == ["/spool list"]
    run(client.handle_update(telegram_update("@shopbot spool list", chat="-1009")))
    run(client.handle_update(telegram_update("@shopbot spool list", chat="-1009")))
    run(client.handle_update(telegram_update("spool list", chat="-1009")))
    notices = [(p["chat_id"], p["text"]) for p in stub.payloads("sendMessage")]
    assert seen == ["/spool list"]
    assert notices[-1] == ("-1009", "Shop Bot only works in its print channel.")
    assert [text for _chat, text in notices].count("Shop Bot only works in its print channel.") == 1


# ---------------------------------------------------------------------- Matrix

def matrix_event(body, formatted=None, mentions=None, reply_to=None, event_id="$m1"):
    content = {"msgtype": "m.text", "body": body}
    if formatted is not None:
        content.update(format="org.matrix.custom.html", formatted_body=formatted)
    if mentions is not None:
        content["m.mentions"] = {"user_ids": list(mentions)}
    if reply_to is not None:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
    return {"type": "m.room.message", "sender": tm.USER, "event_id": event_id,
            "origin_server_ts": 1, "content": content}


def matrix_cfg(cfg):
    cfg["bot"]["platform"] = "matrix"
    cfg["matrix"].update(homeserver_url=tm.HOMESERVER, room_id=tm.ROOM, allowed_user_ids=tm.USER,
                         approval_user_id="", bot_user_id=tm.BOT)
    return cfg


def matrix_core(cfg, tmp_path, seen, name=None, display_name=None):
    matrix_cfg(cfg)
    if name:
        cfg["matrix"]["bot_name"] = name
    stub = tm.HomeserverStub()
    client, core = tm.make_core(cfg, stub, tmp_path, router=recording_router(seen))
    client.display_name = display_name
    return stub, client, core


PILL = f'<a href="https://matrix.to/#/{tm.BOT}">Printer Pal</a>: spool list'


@pytest.mark.parametrize("event", [
    matrix_event("@printhandy spool list"),
    matrix_event(f"{tm.BOT} spool list"),
    matrix_event("Printer Pal: spool list", formatted=PILL),
    matrix_event("Printer Pal: spool list",
                 formatted=PILL.replace(tm.BOT, tm.BOT.replace("@", "%40").replace(":", "%3A"))),
    matrix_event("Print Handy: spool list", mentions=[tm.BOT]),
    matrix_event("Print Handy, spool list"),
    matrix_event("Shelf: /spool list", mentions=[tm.BOT]),
])
def test_matrix_mention_forms_run_the_command(cfg, tmp_path, event):
    seen = []
    _stub, client, _core = matrix_core(cfg, tmp_path, seen, display_name="Shelf")
    run(client.handle_event(event))
    assert seen == ["/spool list"]


def test_matrix_reply_to_the_bot_is_not_a_mention(cfg, tmp_path):
    parsed = matrix.parse_event(matrix_event("spool list", mentions=[tm.BOT], reply_to="$bot1"),
                                tm.ROOM, tm.BOT)
    assert parsed.mentions_bot is False
    parsed = matrix.parse_event(matrix_event("spool list", mentions=["@someone:example.org"]),
                                tm.ROOM, tm.BOT)
    assert parsed.mentions_bot is False and parsed.mention_names == ()


def test_matrix_renamed_bot_ignores_the_old_name(cfg, tmp_path):
    seen = []
    _stub, client, _core = matrix_core(cfg, tmp_path, seen, name="Shop Bot")
    run(client.handle_event(matrix_event("Print Handy, spool list", event_id="$a")))
    assert seen == []
    run(client.handle_event(matrix_event("Shop Bot, spool list", event_id="$b")))
    assert seen == ["/spool list"]


def test_matrix_mention_in_another_joined_room_gets_one_notice(cfg, tmp_path):
    seen = []
    stub, client, _core = matrix_core(cfg, tmp_path, seen)
    other = "!lounge:example.org"
    data = {"next_batch": "s2", "rooms": {"join": {other: {"timeline": {"events": [
        matrix_event("@printhandy spool list", event_id="$x1"),
        matrix_event("spool list", event_id="$x2"),
        matrix_event("@printhandy spool list", event_id="$x3"),
    ]}}}}}
    assert [event["event_id"] for _room, event in client._room_events(data)] == ["$x1", "$x3"]
    for room, event in client._room_events(data):
        run(client.handle_event(event, room))
    sends = stub.payloads("send")
    assert seen == []
    assert [(s["path"].split("/rooms/")[1].split("/")[0], s["body"]["body"]) for s in sends] == [
        ("%21lounge%3Aexample.org", "Print Handy only works in its print channel.")]


def test_matrix_start_reads_the_display_name(cfg, tmp_path):
    class ProfileStub(tm.HomeserverStub):
        def __call__(self, req, timeout=None):
            if "/profile/" in req.full_url:
                self.calls.append(("profile", {"url": req.full_url}))
                return tm._Response(json.dumps({"displayname": "Printer Pal"}).encode())
            return super().__call__(req, timeout)

    stub = ProfileStub()
    matrix_cfg(cfg)
    client = matrix.MatrixClient(cfg, tm.HOMESERVER, tm.TOKEN, opener=stub, state_path=tmp_path / "since")

    async def flow():
        await client.start()
        try:
            return client.display_name, client.core.addressed("Printer Pal: spool list").text
        finally:
            await client.close()

    assert run(flow()) == ("Printer Pal", "/spool list")
    assert "%40printhandy%3Aexample.org/displayname" in stub.payloads("profile")[0]["url"]


def test_matrix_start_survives_a_profile_failure(cfg, tmp_path):
    stub = tm.HomeserverStub()  # answers /profile with an error
    matrix_cfg(cfg)
    client = matrix.MatrixClient(cfg, tm.HOMESERVER, tm.TOKEN, opener=stub, state_path=tmp_path / "since")

    async def flow():
        await client.start()
        try:
            return client.display_name, client.core.addressed("@printhandy spool list").text
        finally:
            await client.close()

    assert run(flow()) == (None, "/spool list")


# ---------------------------------------------------------------------- web chat

def test_web_chat_mention_of_the_bot_name_runs_the_command(cfg):
    cfg.setdefault("webui", {})["bot_name"] = "Shop Bot"
    service = web.WebChatService(cfg)
    try:
        assert service.core.addressed("@Shop Bot spool list").text == "/spool list"
        assert service.core.addressed("@Print Handy spool list").text == "@Print Handy spool list"
        assert service.core.addressed("spool list").text == "spool list"
        assert service.submit("@Shop Bot help") is True
        end = time.time() + 10
        while time.time() < end:
            if any(e["kind"] == "message" and str(e.get("content", "")).startswith("**Shop Bot**")
                   for e in service.entries()):
                break
            time.sleep(0.02)
        else:
            raise AssertionError(f"no help reply: {service.entries()}")
    finally:
        service.close()
