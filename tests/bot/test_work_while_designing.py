"""Printing while an AI design runs.

A design turn can sit in its model call for minutes and its "What next for
this design?" question can stay open for half an hour. Neither may stop the
designer, or anyone else, from starting a print or another design:

* a model link or file from the designer starts the intake, during the turn,
  at the open question, and while the design waits for a change;
* a print on another printer proceeds while the turn is blocked;
* another person's design runs alongside;
* open questions hold no worker thread, so with the default executor capped
  the way a 1-CPU container caps it, a print start still gets a thread.

Every message goes through the bot's Router (the real handler order), with a
design model that waits on an event and an intake that records its start.
"""
import asyncio
import copy
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.bot import runtime
from print_handy.bot.router import Router, load_plugin
from print_handy.core import design, prepared_intake, printer_state, prompts, resources
from print_handy.runtime import _config

USER_A = "200000000000000002"
USER_B = "200000000000000003"
HOME = "100000000000000771"    # the Garage AD5X channel
OTHER = "100000000000000779"   # the Workshop AD5X channel
GARAGE = "garage-ad5x"
WORKSHOP = "workshop-ad5x"
LINK = "https://www.printables.com/model/123456-while-designing"
BUSY = "design turn already running"


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = copy.deepcopy(_config.cfg())
    base = copy.deepcopy(cfg["printers"][0])
    cfg["printers"] = [{**base, "id": GARAGE, "name": "Garage AD5X", "chat_id": HOME},
                       {**base, "id": WORKSHOP, "name": "Workshop AD5X", "chat_id": OTHER}]
    cfg["design"] = {**(cfg.get("design") or {}), "enabled": True}
    # Recent links, intake folders and design sessions stay in this test.
    cfg["paths"] = {**cfg["paths"], "cache": tmp_path / "cache", "state_home": tmp_path}
    cfg["discord"] = {**cfg["discord"], "question_timeout_s": 60}
    monkeypatch.setattr(_config, "_override", cfg)
    return cfg


class Gateway:
    """The gateway surface the handlers use: one printer per channel."""

    def __init__(self, adapter):
        self.adapter = adapter
        self.routed = {HOME: GARAGE, OTHER: WORKSHOP}

    def _is_user_authorized_for_source(self, source):
        return True

    def _delivery_adapter_for(self, source):
        return self.adapter

    def _session_key_for_source(self, source):
        return f"{source.chat_id}:{source.user_id}"

    def printer_id_for(self, source):
        return self.routed.get(str(source.chat_id))

    def shared_printer_ids_for(self, source):
        return None


class Adapter:
    """Records posts and questions; answers nothing on its own."""

    def __init__(self):
        self.sent = []
        self.questions = []  # (clarify_id, session_key, question)

    async def send(self, chat_id=None, content=None, reply_to=None, metadata=None):
        self.sent.append(content or "")
        return S(success=True)

    async def send_clarify(self, chat_id=None, question=None, choices=None, clarify_id=None,
                           session_key=None, metadata=None):
        self.questions.append((clarify_id, session_key, question))
        return S(success=True)

    async def answer_typed(self, session_key, text, user_id=None):
        # The bot core's typed-answer route: the newest open prompt of the session.
        cid = prompts.open_prompt_for(session_key)
        return bool(cid) and prompts.resolve_gateway_clarify(cid, text)

    def busy_replies(self):
        return [text for text in self.sent if "already running" in text]


def event(text, user=USER_A, chat=HOME):
    printer = {HOME: GARAGE, OTHER: WORKSHOP}[chat]
    source = runtime.make_source(user, chat, platform_name="discord", printer_id=printer)
    return runtime.make_event(source, text, "m1")


class Harness:
    """The Router over the real handlers, a gated design model and a recording intake."""

    def __init__(self, monkeypatch, tmp_path):
        self.adapter = Adapter()
        self.gateway = Gateway(self.adapter)
        self.router = Router(load_plugin())
        self.gate = asyncio.Event()
        self.model_calls = []   # instruction per design model call
        self.intakes = []       # (text, printer kwargs) per intake start
        self.tmp_path = tmp_path
        plugin = load_plugin()

        async def complete(messages):
            self.model_calls.append(messages[-1]["content"])
            await self.gate.wait()
            return "cube([40, 20, 3]);"

        async def fake_run_turn(engine, session, instruction):
            await engine._call([{"role": "user", "content": instruction}])
            session["turns"] = int(session.get("turns") or 0) + 1
            return {"session": session, "scad_path": tmp_path / "turn.scad",
                    "stl_path": tmp_path / "turn.stl", "preview_path": None,
                    "preview_error": "renderer unavailable", "param_changes": [],
                    "geometry": {"dimensions_mm": [40.0, 20.0, 3.0], "abs_volume_mm3": 2400.0,
                                 "shell_count": 1, "watertight": True, "manifold": True}}

        async def fake_prepare(event, adapter, state, root, **kwargs):
            # The start's own blocking work (upload, printer calls) needs a
            # default-pool thread, like every print start does.
            await asyncio.to_thread(lambda: None)
            self.intakes.append((event.text, {k: v for k, v in kwargs.items() if k == "printer_id"}))
            return {"action": "skip", "reason": "recorded intake"}

        monkeypatch.setattr(plugin, "_design_transport", lambda cfg: (complete, "http"))
        monkeypatch.setattr(design.DesignEngine, "run_turn", fake_run_turn)
        monkeypatch.setattr(prepared_intake, "prepare_link", fake_prepare)
        monkeypatch.setattr(printer_state, "load", lambda **kwargs: S())

    async def send(self, text, user=USER_A, chat=HOME):
        return await self.router.dispatch(event(text, user, chat), self.gateway)

    def start(self, text, user=USER_A, chat=HOME):
        return asyncio.ensure_future(self.send(text, user, chat))

    async def until(self, predicate, what):
        for _ in range(500):
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"timed out waiting for {what}")

    def open_design_questions(self):
        return [cid for cid, _key, question in self.adapter.questions
                if question == "What next for this design?" and prompts.is_open(cid)]

    def cancel_all(self):
        for cid, _key, _question in self.adapter.questions:
            prompts.resolve_gateway_clarify(cid, prompts.CANCELLED)


@pytest.fixture
def harness(env, monkeypatch, tmp_path):
    return Harness(monkeypatch, tmp_path)


def test_a_pasted_link_during_a_design_turn_starts_the_intake(harness):
    """Regression: the designer's model link used to get "A design turn is
    already running" and was dropped while the turn waited on the model."""
    async def scenario():
        design_task = harness.start("/print-design a bracket")
        try:
            await harness.until(lambda: harness.model_calls, "the design model call")
            out = await asyncio.wait_for(harness.send(LINK), 10)
            assert out == {"action": "skip", "reason": "recorded intake"}
            assert harness.intakes == [(LINK, {})]
            assert harness.adapter.busy_replies() == []
            assert not design_task.done()  # the design is still in its model call
            # A dropped model file is a new intake too.
            stl = harness.tmp_path / "part.stl"
            stl.write_text("solid part\nendsolid part\n")
            drop = event("")
            drop.media_urls = [str(stl)]
            out = await asyncio.wait_for(harness.router.dispatch(drop, harness.gateway), 10)
            assert out == {"action": "skip", "reason": "recorded intake"}
            # Plain text that could be a change still gets the busy reply.
            busy = await harness.send("make it 5 mm taller")
            assert busy == {"action": "skip", "reason": BUSY}
            harness.gate.set()
            await harness.until(harness.open_design_questions, "the design question")
        finally:
            harness.gate.set()
            harness.cancel_all()
        assert (await asyncio.wait_for(design_task, 10))["reason"] == "design cancelled"
        assert harness.model_calls == ["a bracket"]

    asyncio.run(scenario())


def test_a_print_on_another_printer_and_another_design_run_during_a_turn(harness):
    async def scenario():
        design_task = harness.start("/print-design a bracket")
        other_design = None
        try:
            await harness.until(lambda: harness.model_calls, "the design model call")
            # (b) The designer starts a print on the Workshop printer.
            out = await asyncio.wait_for(harness.send(LINK, chat=OTHER), 10)
            assert out == {"action": "skip", "reason": "recorded intake"}
            assert harness.intakes == [(LINK, {"printer_id": WORKSHOP})]
            # A print request naming a printer is not taken for a design change.
            out = await asyncio.wait_for(harness.send("print this on the Workshop AD5X"), 10)
            assert (out or {}).get("reason") != BUSY
            # (c) Another person starts a design in the same channel.
            other_design = harness.start("/print-design a hook", user=USER_B)
            await harness.until(lambda: len(harness.model_calls) == 2, "the second design's model call")
            assert harness.model_calls == ["a bracket", "a hook"]
            assert harness.adapter.busy_replies() == []
            # The same person's second design is still refused.
            again = await harness.send("/print-design a shelf")
            assert again == {"action": "skip", "reason": BUSY}
            assert not design_task.done()
            harness.gate.set()
            await harness.until(lambda: len(harness.open_design_questions()) == 2, "both questions")
        finally:
            harness.gate.set()
            harness.cancel_all()
        assert (await asyncio.wait_for(design_task, 10))["reason"] == "design cancelled"
        assert (await asyncio.wait_for(other_design, 10))["reason"] == "design cancelled"

    asyncio.run(scenario())


def test_a_link_at_the_open_question_or_a_waiting_design_is_a_new_intake(harness):
    """At "What next for this design?" a pasted link used to become the
    answer, so the design ran it as a change; it is a new print instead."""
    async def scenario():
        harness.gate.set()
        design_task = harness.start("/print-design a bracket")
        try:
            await harness.until(harness.open_design_questions, "the design question")
            (cid,) = harness.open_design_questions()
            out = await asyncio.wait_for(harness.send(LINK), 10)
            assert out == {"action": "skip", "reason": "recorded intake"}
            assert harness.intakes == [(LINK, {})]
            assert prompts.is_open(cid)  # the design question was not answered
            assert prompts.resolve_gateway_clarify(cid, design.CHANGE_LABEL)
            out = await asyncio.wait_for(design_task, 10)
            assert out["reason"] == "design awaiting revision"
            # Waiting for the change: a link is still a new print.
            out = await asyncio.wait_for(harness.send(LINK), 10)
            assert out == {"action": "skip", "reason": "recorded intake"}
            assert len(harness.intakes) == 2
            assert harness.model_calls == ["a bracket"]  # no revision ran
        finally:
            harness.cancel_all()

    asyncio.run(scenario())


def test_open_questions_do_not_starve_a_print_start_on_a_capped_executor(harness):
    """Regression: every open question parked a default-pool thread for up to
    question_timeout_s. On a 1-CPU container the pool has 5 threads, so a few
    open design questions left no thread for a print start, which hung."""
    async def scenario():
        workers = resources.cap_default_executor(asyncio.get_running_loop(), budget=1)
        assert workers == 5
        harness.gate.set()
        users = [str(300000000000000000 + n) for n in range(workers + 1)]
        designs = [harness.start(f"/print-design part {n}", user=user)
                   for n, user in enumerate(users)]
        try:
            await harness.until(lambda: len(harness.open_design_questions()) == len(users),
                                "every design question")
            out = await asyncio.wait_for(harness.send(LINK, chat=OTHER), 5)
            assert out == {"action": "skip", "reason": "recorded intake"}
            assert harness.intakes == [(LINK, {"printer_id": WORKSHOP})]
        finally:
            harness.cancel_all()
        for task in designs:
            assert (await asyncio.wait_for(task, 10))["reason"] == "design cancelled"

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# the on-loop prompt wait keeps wait_for_response's contract


def test_await_answer_returns_the_answer_and_closes_the_prompt():
    async def scenario():
        prompts.register("await-1", "k", "q?", ["Yes", "No"])
        waiter = asyncio.ensure_future(prompts.await_answer("await-1", 5))
        await asyncio.sleep(0.05)
        assert prompts.resolve_gateway_clarify("await-1", "Yes")
        assert await asyncio.wait_for(waiter, 2) == "Yes"
        assert not prompts.is_open("await-1")
        assert prompts.resolve_gateway_clarify("await-1", "No") is False  # closed

    asyncio.run(scenario())


def test_await_answer_times_out_to_none_and_a_late_press_finds_nothing():
    async def scenario():
        prompts.register("await-2", "k", "q?", ["Yes"])
        assert await prompts.await_answer("await-2", 0.2) is None
        assert prompts.resolve_gateway_clarify("await-2", "Yes") is False

    asyncio.run(scenario())


def test_await_answer_cancelled_closes_the_prompt():
    async def scenario():
        prompts.register("await-3", "k", "q?", ["Yes"])
        waiter = asyncio.ensure_future(prompts.await_answer("await-3", 30))
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not prompts.is_open("await-3")
        assert prompts.resolve_gateway_clarify("await-3", "Yes") is False

    asyncio.run(scenario())


def test_await_answer_unknown_prompt_is_none():
    assert asyncio.run(prompts.await_answer("never-registered", 1)) is None


def test_await_answer_keeps_a_substituted_blocking_wait():
    """A stand-in registry or a replaced wait keeps the thread wait it was written for."""
    stand_in = S(wait_for_response=lambda cid, timeout: f"{cid}:{timeout}")
    assert asyncio.run(prompts.await_answer("x", 3, registry=stand_in)) == "x:3"
    with mock.patch.object(prompts, "wait_for_response", return_value="patched"):
        assert asyncio.run(prompts.await_answer("y", 3, registry=prompts)) == "patched"


# ----------------------------------------------------------------------
# what counts as "clearly not a design instruction"


@pytest.mark.parametrize("text,expected", [
    ("print 57", True),
    ("print the last one again", True),
    ("print this on the Workshop AD5X", True),
    ("print this on the ad5x", True),
    ("send it to workshop-ad5x", True),
    ("print it", False),
    ("make it 5 mm taller", False),
    ("make it fit the Workshop AD5X bed", False),
    ("design me a holder for the Garage AD5X", False),
])
def test_print_requests_during_a_turn(env, text, expected):
    plugin = load_plugin()
    assert plugin._starts_a_print(text) is expected
