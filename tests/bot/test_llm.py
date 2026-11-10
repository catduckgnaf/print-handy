"""The optional LLM words replies and may name a command. It can never start, stop or write anything itself."""
import asyncio
import io
import json
import threading
from unittest import mock

import pytest

from corpus import fake_discord
from print_handy.bot import llm, runtime
from print_handy.bot.adapter import parse_custom_id
from print_handy.core import prompts
from print_handy.hermes import discord_print_summary as summary
from print_handy.hermes import print_control, print_next, print_stop

LLM_URL = "http://llm.test/v1/chat/completions"


class FakeLLM:
    def __init__(self, reply, command=None, raw=None):
        self.content = raw if raw is not None else json.dumps({"reply": reply, "command": command})
        self.requests = []

    def __call__(self, req, timeout=None):
        assert req.full_url == LLM_URL, f"the LLM path reached {req.full_url}"
        self.requests.append(json.loads(req.data))
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": self.content}}]}).encode())


@pytest.fixture(autouse=True)
def isolated_llm_history():
    """Keep process-local conversation state from coupling independent tests."""
    llm.clear_history()
    yield
    llm.clear_history()


@pytest.fixture
def llm_cfg(cfg):
    cfg["llm"].update(chat_url=LLM_URL, model="small-model")
    return cfg


def make(cfg, opener):
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    return discord, runtime.BotCore(discord.client, cfg, llm_opener=opener)


@pytest.mark.parametrize(("command", "allowed"), [
    ("/print-stop", "/print-stop"), ("/print-next abc123 2", "/print-next abc123 2"),
    ("/PRINT-AGAIN list", "/print-again list"), ("/spool", "/spool"), ("/spool list", "/spool list"),
    ("/print-maintenance quick", "/print-maintenance quick"),
    ("/spool sync", None), ("/spool change 1 12", None), ("/spool new a, PLA, red, 1000", None),
    ("/print-again stop", None), ("/print-colors /x/JOB.json", None), ("M112", None), ("G28", None),
    # /print-design asks for its own Slice it / Change it / Cancel buttons (#388 Stage 2).
    ("/print-design", "/print-design"),
    ("/print-design a 40 mm bracket with two holes", "/print-design a 40 mm bracket with two holes"),
    ("/print-design; rm -rf /", None),
    # /design is the same command's short alias (#388 close-out).
    ("/design", "/design"),
    ("/design a 40 mm bracket with two holes", "/design a 40 mm bracket with two holes"),
    ("/design; rm -rf /", None),
    ("https://makerworld.com/en/models/1", None), ("/print-stop; rm -rf /", None), ("", None), (None, None),
])
def test_only_read_only_or_button_gated_commands_are_allowed(command, allowed):
    assert llm.allowed_command(command) == allowed


def test_llm_picked_stop_still_waits_for_the_button_and_writes_nothing_without_it(llm_cfg):
    opener = FakeLLM("I can stop it, press the button to confirm.", "/print-stop")
    discord, core = make(llm_cfg, opener)

    async def flow():
        task = asyncio.create_task(core.handle_message(discord.message("the print is spaghetti, kill it")))
        for _ in range(300):
            message = discord.last_question()
            if message:
                break
            await asyncio.sleep(0.01)
        assert message is not None, discord.sent
        moonraker.assert_not_called()
        keep = next(cid for label, cid in message.buttons() if label == print_stop.KEEP_LABEL)
        await discord.press(message, keep)
        return await task

    with mock.patch.object(print_stop, "status", return_value={"state": "printing", "filename": "a.gcode"}), \
            mock.patch.object(print_stop, "_call") as moonraker:
        out = asyncio.run(flow())
    assert out["reason"] == "deterministic print stop handled"
    moonraker.assert_not_called()  # no Moonraker cancel: the button said keep printing
    assert discord.sent[0].startswith("I can stop it") and discord.sent[-1] == "Kept printing. Nothing sent."
    assert len(opener.requests) == 1 and opener.requests[0]["model"] == "small-model"


def test_llm_keeps_bounded_context_per_session(llm_cfg):
    """A follow-up gets its own short context, never another user's turn."""
    llm.clear_history()
    opener = FakeLLM("First answer")
    llm.ask("My printer is noisy", "help", session="discord:one", cfg=llm_cfg, opener=opener)
    opener.content = json.dumps({"reply": "Second answer", "command": None})
    llm.ask("What should I check first?", "help", session="discord:one", cfg=llm_cfg, opener=opener)
    llm.ask("Unrelated question", "help", session="discord:two", cfg=llm_cfg, opener=opener)
    second = opener.requests[1]["messages"]
    assert [(m["role"], m["content"]) for m in second[1:3]] == [
        ("user", "My printer is noisy"), ("assistant", "First answer")]
    assert [m["content"] for m in opener.requests[2]["messages"]] == [
        mock.ANY, "Unrelated question"]
    llm.clear_history()


def test_llm_overlapping_asks_for_one_session_keep_both_turns_in_order(llm_cfg):
    """Two asks for one session overlap (the bot serves messages on threads):
    the second ask must still be answered with the first ask's completed turn
    in its context, and the stored history must keep both turns in the order
    they were asked — not in the order their replies happened to land."""
    first_in_flight = threading.Event()
    release_first = threading.Event()
    second_in_flight = threading.Event()
    payloads = {}
    replies = {"first question": "First answer", "second question": "Second answer"}

    class GatedLLM:
        def __call__(self, req, timeout=None):
            assert req.full_url == LLM_URL
            body = json.loads(req.data)
            user = body["messages"][-1]["content"]
            payloads[user] = body["messages"]
            if user == "first question":
                first_in_flight.set()
                assert release_first.wait(timeout=30)
            elif user == "second question":
                second_in_flight.set()
            content = json.dumps({"reply": replies.get(user, "Quick answer"), "command": None})
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

    opener = GatedLLM()
    results = {}
    first = threading.Thread(
        target=lambda: results.__setitem__(
            "first", llm.ask("first question", "help", session="discord:race",
                             cfg=llm_cfg, opener=opener)))
    first.start()
    assert first_in_flight.wait(timeout=10)
    # A different session is never serialized behind the blocked one.
    other = llm.ask("other session question", "help", session="discord:other",
                    cfg=llm_cfg, opener=opener)
    assert other == ("Quick answer", None)
    second = threading.Thread(
        target=lambda: results.__setitem__(
            "second", llm.ask("second question", "help", session="discord:race",
                              cfg=llm_cfg, opener=opener)))
    second.start()
    # If the second ask can overlap, it reaches the provider now, while the
    # first is still in flight; if asks are serialized per session it cannot,
    # and this probe simply expires. Either way the first ask is then released
    # and both complete — the assertions below are what separate the two.
    second_in_flight.wait(timeout=1.0)
    release_first.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive() and not second.is_alive()
    assert results["first"][0] == "First answer" and results["second"][0] == "Second answer"
    second_context = [(m["role"], m["content"]) for m in payloads["second question"]]
    assert ("user", "first question") in second_context
    assert ("assistant", "First answer") in second_context
    # A third ask observes the stored history: both turns, in ask order.
    llm.ask("third question", "help", session="discord:race", cfg=llm_cfg, opener=opener)
    stored = [(m["role"], m["content"]) for m in payloads["third question"][1:-1]]
    assert stored == [("user", "first question"), ("assistant", "First answer"),
                      ("user", "second question"), ("assistant", "Second answer")]


def test_llm_history_is_bounded_by_session_count_and_age(llm_cfg, monkeypatch):
    """The process-local history forgets the least recently used sessions
    past the cap, and any session idle past the time-to-live; the lock
    store shrinks with it."""
    monkeypatch.setattr(llm, "MAX_SESSIONS", 3)
    clock = [1000.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock[0])
    opener = FakeLLM("ok")
    for n in range(10):
        llm.ask(f"q{n}", "help", session=f"discord:{n}", cfg=llm_cfg, opener=opener)
    assert list(llm._HISTORY) == ["discord:7", "discord:8", "discord:9"]
    assert set(llm._HISTORY_LOCKS) <= set(llm._HISTORY)
    llm.ask("fresh", "help", session="discord:fresh", cfg=llm_cfg, opener=opener)
    clock[0] += llm.HISTORY_TTL_S + 1
    llm.ask("again", "help", session="discord:fresh", cfg=llm_cfg, opener=opener)
    # The expired turn is not in the context of the next ask.
    assert [m["content"] for m in opener.requests[-1]["messages"][1:]] == ["again"]
    assert list(llm._HISTORY) == ["discord:fresh"]


def test_clearing_history_never_drops_a_held_session_lock(llm_cfg):
    """clear_history used to pop the session's lock while an ask held it, so
    the next ask took a fresh lock and ran beside the one in flight."""
    in_flight = threading.Event()
    release = threading.Event()
    overlap = threading.Event()
    active = []

    class Gated:
        def __call__(self, req, timeout=None):
            active.append(1)
            if len(active) > 1:
                overlap.set()
            user = json.loads(req.data)["messages"][-1]["content"]
            if user == "first":
                in_flight.set()
                assert release.wait(timeout=30)
            active.pop()
            content = json.dumps({"reply": "ok", "command": None})
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

    opener = Gated()
    first = threading.Thread(target=lambda: llm.ask("first", "help", session="discord:held",
                                                    cfg=llm_cfg, opener=opener))
    first.start()
    assert in_flight.wait(timeout=10)
    llm.clear_history("discord:held")
    llm.clear_history()
    assert "discord:held" in llm._HISTORY_LOCKS  # held: kept
    second = threading.Thread(target=lambda: llm.ask("second", "help", session="discord:held",
                                                     cfg=llm_cfg, opener=opener))
    second.start()
    assert not overlap.wait(timeout=0.5)  # the second ask waits for the first
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not overlap.is_set()
    llm.clear_history()
    assert llm._HISTORY_LOCKS == {}  # nobody holds it now: cleared


def test_llm_named_command_keeps_the_message_attachments(llm_cfg):
    """A command the LLM names is dispatched with the message's attachments
    (bonus, same defect as P2-222): dropping the dropped .scad made design
    treat an edit instruction as a new-model request."""
    from print_handy.bot.router import Router

    seen = []

    class _Plugin:
        def register(self, router):
            async def record(event, gateway):
                if event.text.startswith("/print-design"):
                    seen.append(event)
                    return {"action": "skip", "reason": "recorded"}
                return None
            router.register_hook("pre_gateway_dispatch", record)

    opener = FakeLLM("Editing it now.", "/print-design make it taller")
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(llm_cfg["discord"]["chat_id"]),
                                       operator_id=int(llm_cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, llm_cfg, llm_opener=opener, router=Router(plugin=_Plugin()))
    source = runtime.make_source(discord.operator.id, discord.channel.id)
    event = runtime.make_event(source, "make it taller", "m1", media_urls=["/tmp/uploads/model.scad"])
    out = asyncio.run(core.process(event))
    assert out == {"action": "skip", "reason": "recorded"}
    assert [e.text for e in seen] == ["/print-design make it taller"]
    assert seen[0].media_urls == ["/tmp/uploads/model.scad"]


def test_llm_naming_a_write_command_runs_nothing(llm_cfg):
    opener = FakeLLM("Syncing the screens now!", "/spool sync")
    discord, core = make(llm_cfg, opener)
    with mock.patch("print_handy.hermes.spool_sync.sync_all_screens") as sync, \
            mock.patch("print_handy.hermes.spool_sync.bind_slot") as bind:
        out = asyncio.run(core.handle_message(discord.message("sync the spools on the screen")))
    sync.assert_not_called()
    bind.assert_not_called()
    assert out == {"action": "skip", "reason": "llm reply"}
    assert discord.sent == ["Syncing the screens now!"]
    assert prompts.open_count() == 0


def test_llm_gcode_or_prose_is_only_text(llm_cfg):
    opener = FakeLLM(None, raw="Sure! Send M112 and G28 to the printer.")
    discord, core = make(llm_cfg, opener)
    with mock.patch("urllib.request.urlopen", side_effect=AssertionError("no other network")), \
            mock.patch("print_handy.hermes.print_next.start") as start:
        out = asyncio.run(core.handle_message(discord.message("home the printer")))
    start.assert_not_called()
    assert out["reason"] == "llm reply" and discord.sent == ["Sure! Send M112 and G28 to the printer."]


def test_llm_failure_falls_back_to_the_unmatched_reply(llm_cfg):
    def broken(req, timeout=None):
        raise OSError("llm down")
    discord, core = make(llm_cfg, broken)
    out = asyncio.run(core.handle_message(discord.message("hello?")))
    assert out == {"action": "skip", "reason": "command list"} and discord.sent == [core.unmatched]


def test_llm_is_never_asked_about_commands_or_links(llm_cfg):
    opener = FakeLLM("should not be asked")
    discord, core = make(llm_cfg, opener)
    with mock.patch("print_handy.hermes.spool_sync.list_text", return_value="spools"):
        asyncio.run(core.handle_message(discord.message("/spool list")))
    assert opener.requests == [] and discord.sent == ["spools"]


def test_typed_answer_to_an_llm_routed_question_is_not_sent_to_the_llm(llm_cfg):
    opener = FakeLLM("Stopping needs a button.", "/print-stop")
    discord, core = make(llm_cfg, opener)

    async def flow():
        task = asyncio.create_task(core.handle_message(discord.message("stop the print")))
        for _ in range(300):
            if discord.last_question():
                break
            await asyncio.sleep(0.01)
        message = discord.last_question()
        cid = parse_custom_id(message.buttons()[0][1])[0]
        prompts.resolve_gateway_clarify(cid, print_stop.KEEP_LABEL)
        return await task

    with mock.patch.object(print_stop, "status", return_value={"state": "printing", "filename": "a.gcode"}), \
            mock.patch.object(print_stop, "_call") as moonraker:
        asyncio.run(flow())
    moonraker.assert_not_called()
    assert len(opener.requests) == 1


# ---------------------------------------------------------------------- #437: LLM first, help last

def test_llm_named_print_again_runs_through_the_normal_handler_with_its_confirmation(llm_cfg):
    """The #437 acceptance example, on a phrase no deterministic handler takes:
    the stub LLM names /print-again 57, the normal handler runs it (bed-clear
    confirmation included) and no help text is sent."""
    opener = FakeLLM("Running print 57 again — it still needs its button press.", "/print-again 57")
    discord, core = make(llm_cfg, opener)
    target = {"title": "Widget", "gcode": "/archive/widget.gcode", "job_json": "/archive/JOB.json"}
    started = []

    async def accepted(target_):
        started.append(target_)
        return True, "started"

    confirm = mock.AsyncMock(return_value=print_control.AGAIN_LABEL)
    with mock.patch.object(core.router.plugin, "_confirm", confirm), \
            mock.patch.object(print_control, "split_count", return_value=("57", 1)), \
            mock.patch.object(print_control, "resolve_print", return_value=target.copy()) as resolve, \
            mock.patch.object(print_control, "queue_copies"), \
            mock.patch.object(print_next, "start", side_effect=accepted), \
            mock.patch.object(summary, "gcode_metadata", return_value=(60, 1.0)), \
            mock.patch.object(summary, "_print_number", return_value=57):
        out = asyncio.run(core.handle_message(discord.message("could you run number 57 again?")))
    assert out == {"action": "skip", "reason": "deterministic print again handled"}
    assert len(opener.requests) == 1
    resolve.assert_called_once_with("57")
    confirm.assert_awaited_once()
    assert len(started) == 1 and started[0]["title"] == "Widget"
    assert core.help not in discord.sent
    assert discord.sent[0].startswith("Running print 57 again")


def test_llm_answer_alone_is_the_only_reply_no_help_after_it(llm_cfg):
    opener = FakeLLM("The printer is idle right now.", None)
    discord, core = make(llm_cfg, opener)
    out = asyncio.run(core.handle_message(discord.message("what does the printer dream about")))
    assert out == {"action": "skip", "reason": "llm reply"}
    assert discord.sent == ["The printer is idle right now."]
    assert len(opener.requests) == 1


def test_llm_declining_falls_back_to_the_unmatched_reply_once(llm_cfg):
    opener = FakeLLM("", None)
    discord, core = make(llm_cfg, opener)
    out = asyncio.run(core.handle_message(discord.message("what is the meaning of layer adhesion")))
    assert out == {"action": "skip", "reason": "command list"}
    assert discord.sent == [core.unmatched]
    assert len(opener.requests) == 1


def test_ai_invocation_asks_the_llm_the_bare_question(llm_cfg):
    """An explicit "ask ai: …" strips the invocation: the lane is asked the
    question itself, and its answer is the reply."""
    opener = FakeLLM("Stringing usually means wet filament or too much retraction.")
    discord, core = make(llm_cfg, opener)
    out = asyncio.run(core.handle_message(discord.message("ask ai: why is my print stringing")))
    assert out == {"action": "skip", "reason": "llm reply"}
    assert discord.sent == ["Stringing usually means wet filament or too much retraction."]
    assert opener.requests[0]["messages"][-1]["content"] == "why is my print stringing"


def test_ai_invocation_the_llm_declines_gets_the_ai_wording(llm_cfg):
    """The lane was there and produced nothing: the reply names the AI, not
    the generic unmatched wording."""
    opener = FakeLLM("", None)
    discord, core = make(llm_cfg, opener)
    out = asyncio.run(core.handle_message(discord.message("ask ai: tell me a joke")))
    assert out == {"action": "skip", "reason": "ai no answer"}
    assert discord.sent == [runtime.ai_no_answer_text()]
    assert len(opener.requests) == 1


def test_ai_invocation_without_an_llm_gets_the_not_enabled_reply(cfg):
    discord, core = make(cfg, None)
    out = asyncio.run(core.handle_message(discord.message("ask ai: tell me a joke")))
    assert out == {"action": "skip", "reason": "ai not enabled"}
    assert discord.sent == [runtime.ai_not_enabled_text()]


def test_llm_timeout_falls_back_to_the_unmatched_reply_once(llm_cfg):
    def timing_out(req, timeout=None):
        raise TimeoutError("llm timed out")
    discord, core = make(llm_cfg, timing_out)
    out = asyncio.run(core.handle_message(discord.message("hello?")))
    assert out == {"action": "skip", "reason": "command list"} and discord.sent == [core.unmatched]


def test_llm_naming_print_help_sends_the_help_once_without_its_own_reply(llm_cfg):
    """Never both for one message (#437): /print-help's own output is the help,
    so the LLM's wording is not sent alongside it."""
    opener = FakeLLM("Here is everything I can do.", "/print-help")
    discord, core = make(llm_cfg, opener)
    out = asyncio.run(core.handle_message(discord.message("hello?")))
    assert out == {"action": "skip", "reason": "help"}
    assert discord.sent == [core.help]
    assert len(opener.requests) == 1


def test_no_llm_configured_never_calls_the_llm(cfg):
    """Exploding stub for the disabled path (#437): no [llm], no call, unmatched reply only."""
    discord, core = make(cfg, None)
    calls = []

    def exploding_ask(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("the LLM was called with no LLM configured")

    with mock.patch.object(runtime.llm, "ask", exploding_ask):
        out = asyncio.run(core.handle_message(discord.message("can you make my printer go brrr")))
    assert calls == []
    assert out == {"action": "skip", "reason": "command list"}
    assert discord.sent == [core.unmatched]


def test_use_llm_false_never_calls_the_llm_even_when_configured(llm_cfg):
    """Exploding stub for the pure-bot path (#437): uses_llm=False wins over a configured [llm]."""
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(llm_cfg["discord"]["chat_id"]),
                                       operator_id=int(llm_cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, llm_cfg, llm_opener=None, use_llm=False)
    assert core.use_llm is False
    calls = []

    def exploding_ask(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("the LLM was called on a uses_llm=False platform")

    with mock.patch.object(runtime.llm, "ask", exploding_ask):
        out = asyncio.run(core.handle_message(discord.message("can you make my printer go brrr")))
    assert calls == []
    assert out == {"action": "skip", "reason": "command list"}
    assert discord.sent == [core.unmatched]
