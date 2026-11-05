"""The standalone bot on a fake Discord: who may act, where, typed and slash commands, follow-ups, gates."""
import asyncio
from unittest import mock

import pytest

from corpus import fake_discord
from print_handy.bot import commands, recent_links, runtime
from print_handy.bot.adapter import parse_custom_id
from print_handy.bot.router import Router
from print_handy.core import prompts
from print_handy.hermes import print_stop


def make(cfg, auto=False, router=None):
    discord = fake_discord.FakeDiscord(auto=auto, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, cfg, router=router)
    return discord, core


def run(coro):
    return asyncio.run(coro)


def printing(state="printing"):
    return {"state": state, "filename": "a.gcode", "layer": None, "total_layer": None,
            "progress": None, "print_duration": None}


async def press(discord, label):
    """Wait for the question with this button, then press it as the operator."""
    for _ in range(200):
        message = discord.last_question()
        if message is not None:
            for text, custom_id in discord.prompt_buttons(message):
                if text == label and prompts.is_open(parse_custom_id(custom_id)[0]):
                    return await discord.press(message, custom_id)
        await asyncio.sleep(0.01)
    raise AssertionError(f"no open question with {label!r}: {[m.content for m in discord.messages]}")


def test_router_runs_the_plugin_handlers_in_plugin_order_without_the_agent_receipt():
    router = Router()
    names = router.handler_names()
    assert names[:11] == ["guided_dispatch", "design_dispatch", "maintenance_dispatch",
                          "coldpull_dispatch", "lookup_dispatch", "source_dispatch", "troubleshoot_dispatch",
                          "macros_dispatch", "firmware_dispatch", "calibration_dispatch",
                          "typed_quantity_dispatch"]
    assert "receive_ack" not in names and "stray_chat_dispatch" not in names and len(names) == 27  # +loaded_spools_dispatch (#744), +purchase_dispatch (#801), +edit_dispatch (#903)
    assert router.platform_handlers["discord"], "listener button handlers were not registered"


def test_unmatched_text_without_an_llm_gets_the_short_reply_and_starts_nothing(cfg):
    discord, core = make(cfg)
    with mock.patch("print_handy.hermes.print_next.start") as start, \
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
        out = run(core.handle_message(discord.message("can you make my printer go brrr")))
    assert out == {"action": "skip", "reason": "command list"}
    assert discord.sent == [runtime.unmatched_text(cfg["discord"]["bot_name"])]
    assert discord.sent != [commands.help_text(cfg["discord"]["bot_name"])]
    start.assert_not_called()


@pytest.mark.parametrize("who", ["bot", "elsewhere"])
def test_messages_from_bots_and_other_channels_are_ignored(cfg, who):
    discord, core = make(cfg)
    author = discord.operator
    channel = discord.channel
    if who == "bot":
        author = fake_discord.FakeUser(998, "other bot", bot=True)
    else:
        channel = discord.add_channel(555)
    assert run(core.handle_message(discord.message("/print-help", author=author, channel=channel))) is None
    assert discord.messages == []


def test_a_strangers_message_is_refused_with_one_notice_not_silence(cfg, caplog):
    """#386: a list refusal is never silent — one notice in channel, an INFO log, no intake."""
    discord, core = make(cfg)
    stranger = fake_discord.FakeUser(999, "stranger")
    with caplog.at_level("INFO", logger="print_handy.bot.runtime"):
        assert run(core.handle_message(discord.message("/print-help", author=stranger))) is None
        assert run(core.handle_message(discord.message("/print-help", author=stranger))) is None
    assert discord.sent == [runtime.REFUSAL_NOTICE]  # throttled: the second message adds none
    assert "ignoring message from discord user 999" in caplog.text
    assert "user is not on the allowed list" in caplog.text


def test_empty_allow_list_refuses_everyone(cfg):
    cfg["discord"]["allowed_user_ids"] = "200000000000000002"
    discord, core = make(cfg)
    core.adapter.allowed = set()
    assert run(core.handle_message(discord.message("/print-help"))) is None


def test_thread_in_the_print_channel_replies_in_the_thread(cfg):
    discord, core = make(cfg)
    thread = discord.add_channel(777, parent_id=discord.channel.id)
    run(core.handle_message(discord.message("/print-help", channel=thread)))
    assert discord.messages and all(m.channel is thread for m in discord.messages)


def test_typed_stop_asks_first_and_sends_one_cancel_only_after_the_button(cfg):
    discord, core = make(cfg)

    async def flow():
        task = asyncio.create_task(core.handle_message(discord.message("/print-stop")))
        await press(discord, print_stop.KEEP_LABEL)
        assert await task == {"action": "skip", "reason": "deterministic print stop handled"}
        cancel.assert_not_called()
        task = asyncio.create_task(core.handle_message(discord.message("/print-stop")))
        await press(discord, print_stop.STOP_LABEL)
        await task

    with mock.patch.object(print_stop, "status", return_value=printing()), \
            mock.patch.object(print_stop, "cancel", return_value=(True, "Stopped.")) as cancel:
        run(flow())
    cancel.assert_called_once_with("a.gcode")
    assert discord.sent[-1] == "Stopped." and "Kept printing. Nothing sent." in discord.sent


def test_a_stranger_cannot_press_the_operators_button(cfg):
    discord, core = make(cfg)
    stranger = fake_discord.FakeUser(999, "stranger")

    async def flow():
        task = asyncio.create_task(core.handle_message(discord.message("/print-stop")))
        for _ in range(200):
            if discord.last_question():
                break
            await asyncio.sleep(0.01)
        message = discord.last_question()
        stop_id = next(cid for label, cid in message.buttons() if label == print_stop.STOP_LABEL)
        clicked = await discord.press(message, stop_id, user=stranger)
        assert clicked.replies == [{"content": "Not allowed. Nothing changed.", "ephemeral": True}]
        assert prompts.is_open(parse_custom_id(stop_id)[0])
        await press(discord, print_stop.KEEP_LABEL)
        await task

    with mock.patch.object(print_stop, "status", return_value=printing()), \
            mock.patch.object(print_stop, "cancel") as cancel:
        run(flow())
    cancel.assert_not_called()


def test_a_second_click_on_an_answered_question_changes_nothing(cfg):
    discord, core = make(cfg)

    async def flow():
        task = asyncio.create_task(core.handle_message(discord.message("/print-stop")))
        first = await press(discord, print_stop.KEEP_LABEL)
        await task
        message = first.message
        again = await discord.press(message, f"pbq:{parse_custom_id(first.data['custom_id'])[0]}:0")
        return again

    with mock.patch.object(print_stop, "status", return_value=printing()), \
            mock.patch.object(print_stop, "cancel") as cancel:
        again = run(flow())
    assert again.replies[-1]["content"] == "That question already closed. Nothing changed."
    cancel.assert_not_called()


def test_a_press_is_acknowledged_before_the_waiting_flow_wakes(cfg):
    # Discord shows "This interaction failed" when a press is not acknowledged within 3 s.
    # Resolving the prompt wakes the waiting flow (slice start, file reads), so the ack comes first.
    discord, core = make(cfg)
    order = []
    real_defer, real_resolve = fake_discord._Response.defer, prompts.resolve_gateway_clarify

    async def defer(response, **kwargs):
        order.append("ack")
        await real_defer(response, **kwargs)

    def resolve(cid, answer):
        order.append("resolve")
        return real_resolve(cid, answer)

    async def flow():
        task = asyncio.create_task(core.handle_message(discord.message("/print-stop")))
        clicked = await press(discord, print_stop.KEEP_LABEL)
        await task
        return clicked

    with mock.patch.object(print_stop, "status", return_value=printing()), \
            mock.patch.object(print_stop, "cancel") as cancel, \
            mock.patch.object(fake_discord._Response, "defer", defer), \
            mock.patch.object(prompts, "resolve_gateway_clarify", side_effect=resolve):
        clicked = run(flow())
    assert order[:2] == ["ack", "resolve"]
    assert clicked.acks and clicked.replies == []  # no ephemeral error for a good press
    assert f"chose **{print_stop.KEEP_LABEL}**" in clicked.message.content
    cancel.assert_not_called()


def test_slash_command_runs_as_its_typed_text(cfg):
    discord, core = make(cfg)
    interaction = fake_discord.FakeInteraction(discord, discord.operator, channel=discord.channel)
    with mock.patch("print_handy.hermes.spool_sync.list_text", return_value="Slot 1: #7 black PLA"):
        run(core.run_slash(interaction, "spool", {"args": "list"}))
    assert interaction.replies[0] == {"content": "`/spool list`", "ephemeral": False}
    assert discord.sent[-1] == "Slot 1: #7 black PLA"


def test_slash_command_from_a_stranger_is_refused(cfg):
    discord, core = make(cfg)
    stranger = fake_discord.FakeUser(999, "stranger")
    interaction = fake_discord.FakeInteraction(discord, stranger, channel=discord.channel)
    with mock.patch("print_handy.hermes.spool_sync.list_text") as listing:
        run(core.run_slash(interaction, "spool", {"args": "list"}))
    assert interaction.replies == [{"content": "Not allowed. Nothing changed.", "ephemeral": True}]
    listing.assert_not_called()


def test_typed_text_answers_only_the_askers_open_question(cfg):
    discord, core = make(cfg)
    asker = runtime.make_source(discord.operator.id, discord.channel.id)

    async def flow():
        cid = "typed-1"
        prompts.register(cid, runtime.session_key(asker), "How many copies?", ["1", "2"])
        await core.adapter.send_clarify(chat_id=str(discord.channel.id), question="How many copies?",
                                        choices=["1", "2"], clarify_id=cid, session_key="flow")
        other = runtime.make_source(424242, discord.channel.id)
        assert not await core.adapter.answer_typed(runtime.session_key(other), "2")
        out = await core.process(runtime.make_event(asker, "2", 1))
        assert out == {"action": "skip", "reason": "typed answer to an open question"}
        return await asyncio.to_thread(prompts.wait_for_response, cid, 1)

    assert run(flow()) == "2"


def test_print_it_follow_up_reuses_the_last_link_from_print_handy_state(cfg):
    discord, core = make(cfg)
    url = "https://makerworld.com/en/models/931302-holder"
    seen = []

    async def fake_prepare(event, adapter, state, root, reuse=None, **kwargs):
        seen.append(event.text)
        return {"action": "skip", "reason": "test intake"}

    with mock.patch("print_handy.core.prepared_intake.prepare_link", fake_prepare), \
            mock.patch("print_handy.core.printer_state.load", return_value={}):
        run(core.handle_message(discord.message(url)))
        assert recent_links.recall(str(discord.channel.id)) == url
        run(core.handle_message(discord.message("print it")))
    assert seen[0] == url and seen[1].startswith(url) and seen[1].endswith("print it")


def test_dropped_model_file_is_saved_where_the_intake_reads_it(cfg, tmp_path):
    discord, core = make(cfg)
    model = tmp_path / "Bracket.stl"
    model.write_bytes(b"solid x\nendsolid x\n")
    note = tmp_path / "notes.txt"
    note.write_text("ignored")
    message = discord.message(attachments=[fake_discord.FakeAttachment(model), fake_discord.FakeAttachment(note)])
    paths = run(core.save_attachments(message))
    assert len(paths) == 1 and paths[0].endswith("_Bracket.stl")
    from print_handy.core.prepared_intake import upload_job
    assert upload_job(paths[0])["title"] == "Bracket"
    import os
    stale = core.upload_dir / "doc_000000000000_old.stl"
    stale.write_text("x")
    os.utime(stale, (1, 1))
    run(core.save_attachments(discord.message()))
    assert not stale.exists() and all(os.path.exists(p) for p in paths)


def test_dropped_scad_is_saved_for_design_but_intake_ignores_it(cfg, tmp_path):
    # #388 Stage 2: a dropped .scad is cached like a model file so a design
    # message can edit it, but prepared intake's own filter still accepts
    # only .3mf/.stl — a plain .scad drop starts no intake.
    discord, core = make(cfg)
    scad = tmp_path / "widget.scad"
    scad.write_text("cube([1, 1, 1]);\n")
    message = discord.message(attachments=[fake_discord.FakeAttachment(scad)])
    paths = run(core.save_attachments(message))
    assert len(paths) == 1 and paths[0].endswith("_widget.scad")
    from types import SimpleNamespace
    from print_handy.core.prepared_intake import model_file
    assert model_file(SimpleNamespace(media_urls=paths)) is None


def test_status_reports_no_hermes_modules(cfg):
    discord, core = make(cfg)
    status = core.status(True)
    assert status["mode"] == "standalone" and status["handlers"] == 27  # +loaded_spools_dispatch (#744), +purchase_dispatch (#801), +edit_dispatch (#903)
    assert status["chat_id"] == cfg["discord"]["chat_id"] and status["allowed_users"] == 1


def test_listener_alert_buttons_are_wired_and_checked(cfg):
    """A listener alert button (posted over REST) is answered by the plugin's handler through our client."""
    discord, core = make(cfg)
    message = fake_discord.FakeMessage(discord.channel, discord.bot_user, "Paused", None)
    stranger = fake_discord.FakeUser(999, "stranger")
    with mock.patch("print_handy.hermes.print_alert_buttons.handle_click") as handle_click:
        refused = run(discord.press(message, "pbalert:resume:abcd1234", user=stranger))
        handle_click.assert_not_called()
        assert refused.replies[0]["content"] == "Not allowed. Nothing changed."
        handle_click.return_value = "Resumed."
        with mock.patch("print_handy.hermes.print_alert_buttons.take", return_value={"kind": "paused"}):
            clicked = run(discord.press(message, "pbalert:resume:abcd1234"))
    handle_click.assert_called_once()
    assert clicked.replies[-1]["content"] == "Resumed."


def test_discord_client_registers_commands_and_reports_connection(cfg):
    """The real discord.py Bot subclass, set up offline: slash commands, handlers, heartbeat, disconnects."""
    from print_handy.bot import client, health

    async def flow():
        bot = client.PrintHandyBot(cfg)
        with mock.patch.object(bot.tree, "sync", mock.AsyncMock(return_value=[])) as sync:
            await bot.setup_hook()
        try:
            sync.assert_awaited_once()
            assert sorted(c.name for c in bot.tree.get_commands()) == \
                sorted(name for name, _c in commands.registrations())  # canonical + aliases (/design)
            assert len(bot.extra_events["on_interaction"]) == 2  # prompt buttons + listener alert buttons
            await bot.on_ready()
            assert health.evaluate(health.read())[0] is True
            await bot.on_disconnect()
            assert bot.core.status(bot.connected)["connected"] is False
            await bot.on_resumed()
            assert bot.connected is True
        finally:
            for task in bot._background:
                task.cancel()

    run(flow())


@pytest.mark.parametrize("name", ["Print Bitch", "Print Handy"])
def test_custom_name_is_used_in_help_and_wrong_channel_refusal(cfg, name):
    cfg["discord"]["bot_name"] = name
    discord, core = make(cfg)
    run(core.handle_message(discord.message("/print-help")))
    assert discord.sent[-1].startswith(f"**{name}**")
    elsewhere = discord.add_channel(555)
    interaction = fake_discord.FakeInteraction(discord, discord.operator, channel=elsewhere)
    assert run(core.run_slash(interaction, "print-help", {})) is None
    assert interaction.replies == [{"content": f"{name} only works in its print channel.", "ephemeral": True}]


# ------------------------------------------------------- #468 stage 3

def _recording_router(seen):
    """A stub plugin whose only handler takes the corrected command and records it."""
    class _Plugin:
        def register(self, router):
            async def record(event, gateway):
                if event.text.startswith("/spool"):
                    seen.append(event.text)
                    return {"action": "skip", "reason": "recorded"}
                return None
            router.register_hook("pre_gateway_dispatch", record)
    return Router(plugin=_Plugin())


@pytest.mark.parametrize("text,expected", [
    ("/spol list", "/spool list"),          # the audit's example shape
    ("/prnt-stop", "/print-stop"),
    ("/stop", "/print-stop"),               # window match on the name's tail
    ("/desing a benchy", "/print-design a benchy"),   # alias target, args carried over
    ("/print-agani benchy", "/print-again benchy"),
    ("/print-stop", None),                  # exact is not a near miss
    ("/prnt", None),                        # ties every print-* command: no coin flip
    ("spol list", None),                    # not command-shaped: chat never suggests
    ("hello there", None),                  # would score 67 on the "help" window ungated
    ("can you make my printer go brrr", None),
    ("I need to change a filament roll", "/spool"),
    ("please swap the roll", "/spool"),
    ("I need to replace a filament roll", "/spool"),
    ("change spool", "/spool"),
    ("stop the print", "/print-stop"),
    ("pause the print", "/print-pause"),
    ("what filaments are loaded", "/print-spools"),
    ("printer status", "/print-status"),
    ("how is the print doing", "/print-status"),
    ("print progress", "/print-status"),
    ("print again", "/print-again"),
    ("bed leveling", "/print-calibrate"),
    ("can you stop the print", "/print-stop"),
    ("could you pause the print?", "/print-pause"),
    ("how do i change the filament", "/spool"),
    ("change the nozzle", "/nozzle change"),
    ("is the print done", "/print-status"),
    ("how long left", "/print-status"),
    ("when will it finish", "/print-status"),
    ("the print failed", "/print-status"),
    ("run maintenance", "/print-maintenance"),
    ("what maintenance is due", "/print-maintenance"),
    ("update the firmware", "/print-firmware"),
    ("is there a firmware update?", "/print-firmware"),
    ("the nozzle is clogged", "/print-troubleshoot clog"),
    ("filament is stuck", "/print-troubleshoot clog"),
    ("extruder is clicking", "/print-troubleshoot clog"),
    ("first layer not sticking", "/print-troubleshoot first-layer"),
    ("bed adhesion issues", "/print-troubleshoot first-layer"),
    ("the print is spaghetti", "/print-troubleshoot first-layer"),
    ("heat creep", "/print-troubleshoot heat-creep"),
    ("the bed is not level", "/print-troubleshoot bed-level"),
    ("uneven first layer", "/print-troubleshoot bed-level"),
    ("ringing", "/print-troubleshoot ringing"),
    ("buy more filament", "/spool"),
    ("i need to buy more filament", "/spool"),
    ("need more filament", "/spool"),
    ("where can i buy filament", "/spool"),
    ("running low on filament", "/print-spools"),
    ("what sites can you print from", "/print-source"),
    ("can you print from printables", "/print-source"),
    ("design a bracket", "/print-design a bracket"),
    ("design me a phone stand", "/print-design a phone stand"),
    ("what does thermal runaway mean", "/print-lookup thermal runaway"),
    ("what is error 0300 8003", "/print-lookup error 0300 8003"),
    ("thermal runaway", "/print-lookup thermal runaway"),
    ("layer shift", "/print-lookup layer shift"),
    ("i got a heating error", "/print-lookup heating error"),
    ("how to level the bed", "/print-lookup level the bed"),
    ("why does my print keep failing", "/print-lookup my print keep failing"),
    ("ask ai why is my print stringing", None),   # invocation is the fallback's job, not a suggestion
    ("what is the printer doing?", None),
    ("make me a coffee", None),
    ("", None),
])
def test_command_suggestion_pins(text, expected):
    assert runtime.command_suggestion(text) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("ask ai: why is my print stringing", "why is my print stringing"),
    ("hey ai, what is stringing", "what is stringing"),
    ("ai: stop the print", "stop the print"),
    ("ask the ai to stop the print", "to stop the print"),
    ("please ask ai what temperature for pla", "what temperature for pla"),
    ("ask ai", None),
    ("ai", None),
    ("aioli is great", None),
    ("the ai said so", None),
    ("use a different filament", None),
])
def test_ai_invocation_pins(text, expected):
    assert runtime._ai_invocation(text) == expected


def test_ai_invocation_wearing_a_task_gets_the_suggestion_without_an_llm(cfg):
    """"ask ai change a filament roll" with no lane enabled: the stripped
    question still gets its deterministic suggestion — an explicit
    invocation is not a dead end on a no-LLM platform."""
    seen = []
    discord, core = make(cfg, router=_recording_router(seen))

    async def flow():
        task = asyncio.create_task(core.handle_message(discord.message("ask ai change a filament roll")))
        await press(discord, runtime.SUGGEST_YES_LABEL)
        return await task

    out = run(flow())
    assert seen == ["/spool"]
    assert out == {"action": "skip", "reason": "recorded"}


def test_did_you_mean_fires_with_no_llm_and_yes_dispatches_the_correction(cfg):
    """The #466 no-LLM posture keeps the suggestion (it sits after both lanes
    decline), and a yes re-dispatches the corrected text as if typed."""
    seen = []
    discord, core = make(cfg, router=_recording_router(seen))

    async def flow():
        task = asyncio.create_task(core.handle_message(discord.message("/spol list")))
        await press(discord, runtime.SUGGEST_YES_LABEL)
        return await task

    out = run(flow())
    assert seen == ["/spool list"]
    assert out == {"action": "skip", "reason": "recorded"}


def test_did_you_mean_no_sends_the_unmatched_reply_and_dispatches_nothing(cfg):
    seen = []
    discord, core = make(cfg, router=_recording_router(seen))

    async def flow():
        task = asyncio.create_task(core.handle_message(discord.message("/spol list")))
        await press(discord, runtime.SUGGEST_NO_LABEL)
        return await task

    out = run(flow())
    assert seen == []
    assert out == {"action": "skip", "reason": "command list"}
    assert discord.sent[-1] == runtime.unmatched_text(cfg["discord"]["bot_name"])


def test_live_command_filtered_from_unsupported_platform_surfaces(cfg):
    """print-live is Discord-only (P2-123): the shared command table keeps it
    for Discord registration and help, and filters it out of both surfaces
    for platforms whose adapter reports no live-stream support."""
    assert "print-live" in dict(commands.registrations())
    assert "print-live" not in dict(commands.registrations(supports_live_stream=False))
    assert "/print-live" in commands.help_text()
    assert "/print-live" not in commands.help_text(supports_live_stream=False)
    assert "/print-live" in commands.help_text("Print Handy")  # Hermes/Discord default unchanged
    _discord, core = make(cfg)
    assert "/print-live" in core.help  # the Discord core keeps advertising it


def _event_router(seen, prefix):
    """A stub plugin recording every event whose text starts with ``prefix``."""
    class _Plugin:
        def register(self, router):
            async def record(event, gateway):
                if event.text.startswith(prefix):
                    seen.append(event)
                    return {"action": "skip", "reason": "recorded"}
                return None
            router.register_hook("pre_gateway_dispatch", record)
    return Router(plugin=_Plugin())


def test_did_you_mean_keeps_the_attachment_on_the_corrected_dispatch(cfg):
    """Accepting a correction behaves like typing the correct command with
    the same attachment (P2-222): the dropped .scad rides the original
    event's media into the corrected dispatch, so design edits the upload
    instead of treating the instruction as a request for a new model."""
    seen = []
    discord, core = make(cfg, router=_event_router(seen, "/print-design"))
    source = runtime.make_source(discord.operator.id, discord.channel.id)

    async def flow():
        event = runtime.make_event(source, "/desing make it taller", "m1",
                                   media_urls=["/tmp/uploads/model.scad"])
        task = asyncio.create_task(core.process(event))
        await press(discord, runtime.SUGGEST_YES_LABEL)
        return await task

    out = run(flow())
    assert out == {"action": "skip", "reason": "recorded"}
    assert [e.text for e in seen] == ["/print-design make it taller"]
    assert seen[0].media_urls == ["/tmp/uploads/model.scad"]


def test_did_you_mean_pick_list_keeps_the_attachment(cfg):
    """The pick-list dispatch preserves the original media the same way (P2-222)."""
    seen = []
    discord, core = make(cfg, router=_event_router(seen, "/print-"))
    source = runtime.make_source(discord.operator.id, discord.channel.id)

    async def flow():
        event = runtime.make_event(source, "/prnt", "m1", media_urls=["/tmp/uploads/part.stl"])
        task = asyncio.create_task(core.process(event))
        await press(discord, "/print-stop")
        return await task

    out = run(flow())
    assert out == {"action": "skip", "reason": "recorded"}
    assert [e.text for e in seen] == ["/print-stop"]
    assert seen[0].media_urls == ["/tmp/uploads/part.stl"]


def test_print_handy_slash_carries_debug_arguments():
    """The /print-handy slash command must reach 'debug on [minutes]' (P2-186):
    a free-form second option carries the words after the action, so the
    advertised debug on/off flow works through the slash command, not only
    as a typed message."""
    assert commands.as_text("print-handy", {"action": "debug", "args": "on 5"}) == "/print-handy debug on 5"
    assert commands.as_text("print-handy", {"action": "debug", "args": "off"}) == "/print-handy debug off"
    assert commands.as_text("print-handy", {"action": "setup"}) == "/print-handy setup"
    command = commands.by_name("print-handy")
    assert [o.name for o in command.options] == ["action", "args"]
    assert not command.options[1].choices  # free-form, not another fixed choice list


# ---------------------------------------------------------------------- upload names

def test_safe_name_truncation_keeps_the_extension():
    """A valid model file whose name exceeds the cap keeps its suffix (P2-39):
    truncating through the extension made save_attachments skip the upload
    silently, because the saved name no longer ended in a model suffix."""
    name = runtime.safe_name("a" * 130 + ".stl")
    assert name.endswith(".stl") and len(name) == 120
    name = runtime.safe_name("a" * 130 + ".3mf")
    assert name.endswith(".3mf") and len(name) == 120
    assert runtime.safe_name("b" * 200) == "b" * 120  # no extension: plain truncation
    assert runtime.safe_name("benchy.3mf") == "benchy.3mf"  # short names untouched
    assert runtime.safe_name("my model!.stl") == "my_model_.stl"  # sanitizing untouched


def test_did_you_mean_accepts_a_typed_yes(cfg):
    seen = []
    discord, core = make(cfg, router=_recording_router(seen))

    async def flow():
        task = asyncio.create_task(core.handle_message(discord.message("/spol list")))
        for _ in range(200):
            if discord.last_question() is not None:
                break
            await asyncio.sleep(0.01)
        await core.handle_message(discord.message("yes"))
        return await task

    run(flow())
    assert seen == ["/spool list"]


def test_did_you_mean_expires_quietly_and_dispatches_nothing(cfg, monkeypatch):
    monkeypatch.setattr(runtime, "SUGGESTION_TIMEOUT_S", 0.2)
    seen = []
    discord, core = make(cfg, router=_recording_router(seen))
    out = run(core.handle_message(discord.message("/spol list")))
    assert out == {"action": "skip", "reason": "did-you-mean suggestion expired"}
    assert seen == []
    assert any("Did you mean `/spool list`?" in q["question"] for q in discord.questions)
    assert runtime.unmatched_text(cfg["discord"]["bot_name"]) not in discord.sent


# ------------------------------------- confidence/conflict policy (decide)

def _recording_print_router(seen):
    """A stub plugin recording any corrected /print-* command it is handed."""
    class _Plugin:
        def register(self, router):
            async def record(event, gateway):
                if event.text.startswith("/print-"):
                    seen.append(event.text)
                    return {"action": "skip", "reason": "recorded"}
                return None
            router.register_hook("pre_gateway_dispatch", record)
    return Router(plugin=_Plugin())


def test_suggestion_candidates_only_for_contested_near_misses():
    # "/prnt" sits within the clear margin of several print-* commands:
    # no single suggestion (the pin above), but real contenders to offer.
    candidates = runtime.command_suggestion_candidates("/prnt")
    assert len(candidates) >= 2 and "/print-stop" in candidates
    assert all(c.startswith("/print-") for c in candidates)
    # A clear near miss has a single suggestion and no candidate list.
    assert runtime.command_suggestion_candidates("/spol list") == []
    # Nothing scoring, or not command-shaped: no candidates either.
    assert runtime.command_suggestion_candidates("/zzzz") == []
    assert runtime.command_suggestion_candidates("hello there") == []


def test_did_you_mean_pick_list_dispatches_the_picked_command(cfg):
    seen = []
    discord, core = make(cfg, router=_recording_print_router(seen))

    async def flow():
        task = asyncio.create_task(core.handle_message(discord.message("/prnt")))
        await press(discord, "/print-stop")
        return await task

    out = run(flow())
    assert seen == ["/print-stop"]
    assert out == {"action": "skip", "reason": "recorded"}


def test_did_you_mean_pick_list_none_of_these_dispatches_nothing(cfg):
    seen = []
    discord, core = make(cfg, router=_recording_print_router(seen))

    async def flow():
        task = asyncio.create_task(core.handle_message(discord.message("/prnt")))
        await press(discord, runtime.SUGGEST_NONE_LABEL)
        return await task

    out = run(flow())
    assert seen == []
    assert out == {"action": "skip", "reason": "command list"}
    assert discord.sent[-1] == runtime.unmatched_text(cfg["discord"]["bot_name"])
