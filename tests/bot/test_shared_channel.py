"""Shared channels (#536): two printers, one channel — the bot asks.

Pins the issue's acceptance properties offline: a duplicate claim is a
shared channel (served, undecided, never silently the default printer),
the which-printer question runs before any printer state loads, a job's
own printer resolves job-bound commands without asking, "current print"
commands follow activity, typed answers cannot cross printers, lifecycle
announcements name the printer, and the completion retry state is keyed
by printer. The dedicated-channel shapes stay byte-identical.
"""
import asyncio
import copy
import json
import sys
from types import SimpleNamespace as S
from unittest import mock

import pytest

from corpus import fake_discord
from print_handy.bot import routing, runtime
from print_handy.bot.router import Router
from print_handy.core import link_intake, prompts
from print_handy.runtime import _config, print_next, print_stop

OPERATOR = "200000000000000002"
HOME = "100000000000000001"
GARAGE = "flashforge-ad5x"
WORKSHOP = "workshop-ad5x"
LINK = "https://www.printables.com/model/123456-shared-channel-test"


def run(coro):
    return asyncio.run(coro)


def two_printer_cfg(cfg):
    base = copy.deepcopy(cfg["printers"][0])
    garage = {**base, "id": GARAGE, "name": "Garage AD5X", "chat_id": ""}
    workshop = {**base, "id": WORKSHOP, "name": "Workshop AD5X", "chat_id": HOME}
    cfg["printers"] = [garage, workshop]  # both claim the home chat: shared
    return cfg


@pytest.fixture
def plugin_cfg(monkeypatch):
    """The two-printer shared roster as the process config the plugin reads."""
    data = two_printer_cfg(copy.deepcopy(_config.cfg()))
    monkeypatch.setattr(_config, "_override", data)
    return data


def _plugin():
    from print_handy.bot.router import load_plugin
    return load_plugin()


class SharedGateway:
    """The gateway surface the plugin handlers use, with a shared channel."""

    def __init__(self, adapter, shared=None, routed=None):
        self._adapter = adapter
        self._shared = shared or {}
        self._routed = routed or {}

    def _is_user_authorized_for_source(self, source):
        return str(source.chat_id) in self._shared or str(source.chat_id) in self._routed

    def _delivery_adapter_for(self, source):
        return self._adapter

    def _session_key_for_source(self, source):
        return f"{source.chat_id}:{source.user_id}"

    def printer_id_for(self, source):
        return self._routed.get(str(source.chat_id))

    def shared_printer_ids_for(self, source):
        ids = self._shared.get(str(source.chat_id))
        return list(ids) if ids else None


class PickingAdapter:
    """Records questions; answers each clarify with the label `pick` chooses."""

    def __init__(self, pick=None):
        self.sent = []
        self.questions = []
        self.targets = []
        self.pick = pick or (lambda question, choices: choices[0])

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((str(chat_id), content))
        return S(success=True)

    async def send_clarify(self, chat_id, question, choices, clarify_id=None, session_key=None,
                           metadata=None):
        self.questions.append((question, list(choices)))
        # Where the real adapters would post it: the thread when the
        # delivery metadata names one, else the channel (clarify_ui's
        # target rule). Recorded so a test can pin the destination.
        self.targets.append(str((metadata or {}).get("thread_id") or chat_id))
        prompts.resolve_gateway_clarify(clarify_id, self.pick(question, list(choices)))
        return S(success=True)


def shared_event(text, chat_id=HOME):
    source = runtime.make_source(OPERATOR, chat_id, platform_name="discord",
                                 shared_printer_ids=[GARAGE, WORKSHOP])
    return S(source=source, text=text, internal=False, metadata={}, media_urls=[], message_id="m1")


def printing(state="printing"):
    return {"state": state, "filename": "a.gcode", "layer": None, "total_layer": None,
            "progress": None, "print_duration": None}


# ----------------------------------------------------------------------
# the claim model


def test_both_fallback_and_explicit_claims_share_the_home_chat():
    cfg = {"printers": [{"id": GARAGE, "chat_id": ""}, {"id": WORKSHOP, "chat_id": "111"}],
           "discord": {"chat_id": "111"}}
    assert routing.channel_claims(cfg, "discord") == {"111": [GARAGE, WORKSHOP]}
    assert routing.channel_map(cfg, "discord") == {}
    assert routing.shared_channel_map(cfg, "discord") == {"111": [GARAGE, WORKSHOP]}
    assert routing.printer_for_chat(cfg, "discord", "111") is None
    assert routing.chat_for_printer(cfg, "discord", WORKSHOP) == "111"


# ----------------------------------------------------------------------
# the which-printer question in the intake


def _intake_stubs(monkeypatch, calls):
    from print_handy.core import prepared_intake, printer_state

    def fake_load(**kwargs):
        calls.append(("load", kwargs))
        return S()

    async def fake_prepare(event, adapter, state, root, **kwargs):
        calls.append(("prepare", kwargs))
        return {"action": "skip", "reason": "recorded intake"}

    monkeypatch.setattr(printer_state, "load", fake_load)
    monkeypatch.setattr(prepared_intake, "prepare_link", fake_prepare)
    monkeypatch.setattr("print_handy.runtime.print_control.status",
                        lambda **kwargs: {"state": "standby"})


def test_shared_channel_link_asks_before_loading_printer_state(plugin_cfg, monkeypatch):
    plugin = _plugin()
    calls = []
    _intake_stubs(monkeypatch, calls)
    adapter = PickingAdapter(pick=lambda q, choices: "Workshop AD5X")
    gateway = SharedGateway(adapter, shared={HOME: [GARAGE, WORKSHOP]})
    event = shared_event(LINK)
    out = run(plugin.model_link_dispatch(event, gateway))
    assert out == {"action": "skip", "reason": "recorded intake"}
    assert any("Which printer" in q for q, _choices in adapter.questions)
    # The picker ran first: prepare got the chosen printer, in routed form.
    assert calls[0][0] == "load" and calls[0][1] == {"printer_id": WORKSHOP}
    assert calls[1] == ("prepare", {"reuse": None, "printer_id": WORKSHOP})
    # And the intake's questions are keyed to the chosen printer.
    assert event.metadata["live_session_key"].endswith(f":printer:{WORKSHOP}")


def test_shared_channel_link_picker_can_choose_the_default_printer(plugin_cfg, monkeypatch):
    plugin = _plugin()
    calls = []
    _intake_stubs(monkeypatch, calls)
    adapter = PickingAdapter(pick=lambda q, choices: "Garage AD5X")
    gateway = SharedGateway(adapter, shared={HOME: [GARAGE, WORKSHOP]})
    event = shared_event(LINK)
    out = run(plugin.model_link_dispatch(event, gateway))
    assert out == {"action": "skip", "reason": "recorded intake"}
    # The default printer keeps the exact legacy call shapes (no printer_id).
    assert calls == [("load", {}), ("prepare", {"reuse": None})]
    assert event.metadata["live_session_key"].endswith(f":printer:{GARAGE}")


def test_shared_channel_link_without_a_choice_never_touches_the_default(plugin_cfg, monkeypatch):
    plugin = _plugin()
    calls = []
    _intake_stubs(monkeypatch, calls)
    adapter = PickingAdapter(pick=lambda q, choices: prompts.CANCELLED)
    gateway = SharedGateway(adapter, shared={HOME: [GARAGE, WORKSHOP]})
    out = run(plugin.model_link_dispatch(shared_event(LINK), gateway))
    assert out["reason"] == "shared channel: no printer chosen for intake"
    assert calls == []  # no state loaded, nothing prepared: undecided never defaults
    assert any("No printer chosen" in text for _chat, text in adapter.sent)


def test_dedicated_channel_link_never_asks(plugin_cfg, monkeypatch):
    plugin = _plugin()
    calls = []
    _intake_stubs(monkeypatch, calls)
    adapter = PickingAdapter()
    gateway = SharedGateway(adapter, routed={HOME: WORKSHOP})
    event = shared_event(LINK)
    event.source.shared_printer_ids = None
    event.source.printer_id = WORKSHOP
    out = run(plugin.model_link_dispatch(event, gateway))
    assert out == {"action": "skip", "reason": "recorded intake"}
    assert adapter.questions == []  # routing decided; no picker
    assert calls == [("load", {"printer_id": WORKSHOP}), ("prepare", {"reuse": None, "printer_id": WORKSHOP})]


def test_host_without_a_channel_map_asks_from_the_roster(plugin_cfg, monkeypatch):
    # Hermes posture: the gateway has no printer_id_for at all, so a
    # multi-printer roster makes an unrouted event undecided (#536).
    plugin = _plugin()
    calls = []
    _intake_stubs(monkeypatch, calls)
    adapter = PickingAdapter(pick=lambda q, choices: "Workshop AD5X")
    gateway = S(_is_user_authorized_for_source=lambda source: True,
                _delivery_adapter_for=lambda source: adapter,
                _session_key_for_source=lambda source: f"{source.chat_id}:{source.user_id}")
    source = runtime.make_source(OPERATOR, HOME, platform_name="discord")
    event = S(source=source, text=LINK, internal=False, metadata={}, media_urls=[], message_id="m1")
    out = run(plugin.model_link_dispatch(event, gateway))
    assert out == {"action": "skip", "reason": "recorded intake"}
    assert any("Which printer" in q for q, _choices in adapter.questions)
    assert calls[-1] == ("prepare", {"reuse": None, "printer_id": WORKSHOP})


def test_picker_raised_in_a_thread_is_posted_to_the_thread(plugin_cfg, monkeypatch):
    # The picker must carry the caller's delivery metadata like every
    # other _confirm prompt: the adapter addresses a clarify from
    # metadata['thread_id'], so a picker dropped in a thread belongs in
    # the thread — posted to the parent channel it could never be
    # answered by a typed reply where it appeared (#536 close-out).
    plugin = _plugin()
    calls = []
    _intake_stubs(monkeypatch, calls)
    adapter = PickingAdapter(pick=lambda q, choices: "Workshop AD5X")
    gateway = SharedGateway(adapter, shared={HOME: [GARAGE, WORKSHOP]})
    thread_id = "777000111222333444"
    source = runtime.make_source(OPERATOR, HOME, thread_id=thread_id, platform_name="discord",
                                 shared_printer_ids=[GARAGE, WORKSHOP])
    event = S(source=source, text=LINK, internal=False, metadata={}, media_urls=[], message_id="m1")
    out = run(plugin.model_link_dispatch(event, gateway))
    assert out == {"action": "skip", "reason": "recorded intake"}
    assert adapter.targets[0] == thread_id  # not HOME, the parent channel
    # And it was answerable there: the pick decided the intake's printer.
    assert calls[-1] == ("prepare", {"reuse": None, "printer_id": WORKSHOP})


# ----------------------------------------------------------------------
# current-print commands follow activity


def _stop_stubs(monkeypatch, states, calls):
    def fake_status(**kwargs):
        pid = kwargs.get("printer_id") or GARAGE
        calls.append(("status", kwargs))
        return dict(states[pid])

    def fake_cancel(filename, **kwargs):
        calls.append(("cancel", filename, kwargs))
        return True, "Stopped."

    monkeypatch.setattr(print_stop, "status", fake_status)
    monkeypatch.setattr(print_stop, "cancel", fake_cancel)
    monkeypatch.setattr("print_handy.runtime.discord_print_summary.format_event",
                        lambda kind, filename, **kw: f"Print started: {filename}")


def test_stop_acts_on_the_one_printing_printer_without_asking(plugin_cfg, monkeypatch):
    plugin = _plugin()
    calls = []
    _stop_stubs(monkeypatch, {GARAGE: printing("standby"), WORKSHOP: printing()}, calls)
    adapter = PickingAdapter(pick=lambda q, choices: print_stop.STOP_LABEL)
    gateway = SharedGateway(adapter, shared={HOME: [GARAGE, WORKSHOP]})
    out = run(plugin.print_stop_dispatch(shared_event("/print-stop"), gateway))
    assert out == {"action": "skip", "reason": "deterministic print stop handled"}
    assert not any("Which printer" in q or "Stop which" in q for q, _c in adapter.questions)
    assert ("cancel", "a.gcode", {"printer_id": WORKSHOP}) in calls


def test_stop_with_two_printing_printers_asks_and_never_guesses(plugin_cfg, monkeypatch):
    plugin = _plugin()
    calls = []
    _stop_stubs(monkeypatch, {GARAGE: printing(), WORKSHOP: printing()}, calls)
    picks = {"n": 0}

    def pick(question, choices):
        picks["n"] += 1
        if "Stop which" in question:
            return next(c for c in choices if c.startswith("Garage AD5X"))
        return print_stop.STOP_LABEL

    adapter = PickingAdapter(pick=pick)
    gateway = SharedGateway(adapter, shared={HOME: [GARAGE, WORKSHOP]})
    out = run(plugin.print_stop_dispatch(shared_event("/print-stop"), gateway))
    assert out == {"action": "skip", "reason": "deterministic print stop handled"}
    assert any("Stop which" in q for q, _c in adapter.questions)
    # The default printer was CHOSEN, so cancel keeps the legacy shape.
    assert ("cancel", "a.gcode", {}) in calls
    assert not any(kwargs.get("printer_id") == WORKSHOP for _op, *_rest, kwargs in
                   [c for c in calls if c[0] == "cancel"])


def test_stop_with_nothing_printing_refuses_naming_both_printers(plugin_cfg, monkeypatch):
    plugin = _plugin()
    calls = []
    _stop_stubs(monkeypatch, {GARAGE: printing("standby"), WORKSHOP: printing("standby")}, calls)
    adapter = PickingAdapter()
    gateway = SharedGateway(adapter, shared={HOME: [GARAGE, WORKSHOP]})
    out = run(plugin.print_stop_dispatch(shared_event("/print-stop"), gateway))
    assert out == {"action": "skip", "reason": "shared channel: no stop target decided"}
    assert not any(op == "cancel" for op, *_ in calls)
    text = adapter.sent[-1][1]
    assert "Nothing to stop" in text and "Garage AD5X" in text and "Workshop AD5X" in text


# ----------------------------------------------------------------------
# job-bound commands resolve from the job record


def test_print_again_in_a_shared_channel_uses_the_jobs_printer(plugin_cfg, monkeypatch, tmp_path):
    plugin = _plugin()
    from print_handy.runtime import print_control as pc

    job_json = tmp_path / "JOB.json"
    job_json.write_text(json.dumps({"printer_id": WORKSHOP, "title": "Workshop job"}))
    target = {"job_json": str(job_json), "gcode": str(tmp_path / "a.gcode"), "title": "Workshop job"}
    started = []
    monkeypatch.setattr(pc, "resolve_print", lambda query: dict(target))
    monkeypatch.setattr(pc, "split_count", lambda query: (query, 1))
    monkeypatch.setattr(pc, "again_question", lambda target: "Print it again?")
    monkeypatch.setattr(pc, "queue_copies", lambda target, **kw: None)
    monkeypatch.setattr(print_next, "start", lambda target: started.append(target) or (True, ""))
    monkeypatch.setattr("print_handy.runtime.discord_print_summary.gcode_metadata",
                        lambda gcode: (60, 10))
    monkeypatch.setattr("print_handy.runtime.discord_print_summary._print_number",
                        lambda job: None)
    adapter = PickingAdapter(pick=lambda q, choices: pc.AGAIN_LABEL)
    gateway = SharedGateway(adapter, shared={HOME: [GARAGE, WORKSHOP]})
    out = run(plugin.print_again_dispatch(shared_event("/print-again 12"), gateway))
    assert out == {"action": "skip", "reason": "deterministic print again handled"}
    assert started, "the job's own printer starts it — no asking, no defaulting"
    assert not any("Which printer" in q for q, _c in adapter.questions)


def test_print_again_next_and_stop_in_a_shared_channel_read_the_owners_queue(plugin_cfg, monkeypatch):
    # 2.2.5 review: copies are queued under the job owner's printer, but a
    # shared channel decides no printer, so next/stop read the default file.
    plugin = _plugin()
    from print_handy.runtime import print_control as pc

    queued = {WORKSHOP: {"job_json": "j", "gcode": "g"}}
    seen = []
    monkeypatch.setattr(pc, "pending_copy", lambda path=None, printer_id=None: queued.get(printer_id))
    monkeypatch.setattr(pc, "cancel_copies",
                        lambda path=None, printer_id=None: seen.append(("cancel", printer_id)) or bool(queued.get(printer_id)))
    monkeypatch.setattr(pc, "next_copy_target",
                        lambda path=None, archive=None, printer_id=None: seen.append(("next", printer_id)) or (_ for _ in ()).throw(
                            print_next.NextPlateError("stop here")))
    adapter = PickingAdapter(pick=lambda q, choices: pc.AGAIN_CANCEL_LABEL)
    gateway = SharedGateway(adapter, shared={HOME: [GARAGE, WORKSHOP]})
    run(plugin.print_again_dispatch(shared_event("/print-again stop"), gateway))
    assert seen == [("cancel", WORKSHOP)]
    assert "Remaining copies dropped" in adapter.sent[-1][1]
    seen.clear()
    run(plugin.print_again_dispatch(shared_event("/print-again next"), gateway))
    assert seen == [("next", WORKSHOP)]


def test_print_again_stop_in_a_shared_channel_refuses_ambiguous_queues(plugin_cfg, monkeypatch):
    plugin = _plugin()
    from print_handy.runtime import print_control as pc

    monkeypatch.setattr(pc, "pending_copy", lambda path=None, printer_id=None: {"job_json": "j", "gcode": "g"})
    cancelled = []
    monkeypatch.setattr(pc, "cancel_copies", lambda path=None, printer_id=None: cancelled.append(printer_id) or True)
    adapter = PickingAdapter(pick=lambda q, choices: None)
    gateway = SharedGateway(adapter, shared={HOME: [GARAGE, WORKSHOP]})
    out = run(plugin.print_again_dispatch(shared_event("/print-again stop"), gateway))
    assert out["reason"] == "print again queue ambiguous across printers"
    assert not cancelled
    assert "Garage AD5X" in adapter.sent[-1][1] and "Workshop AD5X" in adapter.sent[-1][1]


# ----------------------------------------------------------------------
# spool slot binds ask; inventory stays global


def test_spool_bind_in_a_shared_channel_asks_first(plugin_cfg, monkeypatch):
    plugin = _plugin()
    from print_handy.runtime import spool_sync

    bound = []
    monkeypatch.setattr(spool_sync, "moonraker_for", lambda pid=None: f"mr:{pid}")
    monkeypatch.setattr(spool_sync, "bind_slot",
                        lambda slot, spool, **kw: bound.append((slot, spool, kw)) or {"ok": True})
    monkeypatch.setattr(spool_sync, "bind_text", lambda result: "Bound.")
    adapter = PickingAdapter(pick=lambda q, choices: "Workshop AD5X")
    gateway = SharedGateway(adapter, shared={HOME: [GARAGE, WORKSHOP]})
    out = run(plugin.spool_dispatch(shared_event("/spool change 1 2"), gateway))
    assert out["reason"] == "spool command handled"
    assert any("Which printer" in q for q, _c in adapter.questions)
    assert bound == [(1, 2, {"mr": f"mr:{WORKSHOP}", "printer_id": WORKSHOP})]


def test_spool_list_in_a_shared_channel_names_each_printer(plugin_cfg, monkeypatch):
    plugin = _plugin()
    from print_handy.runtime import spool_sync

    listed = []
    monkeypatch.setattr(spool_sync, "list_text",
                        lambda **kw: listed.append(kw) or "slots here")
    adapter = PickingAdapter()
    gateway = SharedGateway(adapter, shared={HOME: [GARAGE, WORKSHOP]})
    out = run(plugin.spool_dispatch(shared_event("/spool list"), gateway))
    assert out["reason"] == "spool command handled"
    assert adapter.questions == []  # inventory is global: no picker
    assert listed == [{"printer_id": GARAGE}, {"printer_id": WORKSHOP}]
    text = adapter.sent[-1][1]
    assert "Garage AD5X" in text and "Workshop AD5X" in text


# ----------------------------------------------------------------------
# typed answers cannot cross printers


def test_registry_scoped_lookup_needs_a_single_printer():
    base = "sess:1"
    prompts.register("cid-a", f"{base}:printer:{GARAGE}", "q", ["x"])
    prompts.register("cid-b", f"{base}:printer:{WORKSHOP}", "q", ["x"])
    try:
        assert prompts.open_prompt_scoped(base) is None  # two printers: ambiguous
        prompts.resolve_gateway_clarify("cid-b", "x")
        assert prompts.open_prompt_scoped(base) == "cid-a"  # one printer left
        prompts.register("cid-c", base, "q", ["x"])
        assert prompts.open_prompt_scoped(base) == "cid-c"  # exact key wins
    finally:
        for cid in ("cid-a", "cid-b", "cid-c"):
            prompts.resolve_gateway_clarify(cid, "x")


def test_adapter_open_prompt_scoped_lookup():
    from print_handy.bot.platform import BasePlatformAdapter

    adapter = BasePlatformAdapter.__new__(BasePlatformAdapter)
    adapter.registry = prompts
    adapter._prompts = {}
    base = "sess:2"
    for cid, key in (("p-a", f"{base}:printer:{GARAGE}"), ("p-b", f"{base}:printer:{WORKSHOP}")):
        prompts.register(cid, key, "q", ["x"])
        adapter._prompts[cid] = {}
    try:
        assert adapter.open_prompt(base) is None
        prompts.resolve_gateway_clarify("p-b", "x")
        assert adapter.open_prompt(base) == "p-a"
    finally:
        for cid in ("p-a", "p-b"):
            prompts.resolve_gateway_clarify(cid, "x")


def test_typed_reply_with_two_printers_open_resolves_neither(monkeypatch):
    source = S(chat_id="c1", thread_id=None, user_id="u1")
    scope = ("c1", "", "u1")
    base = "sess:3"
    entries = {}
    for iid, pid in (("i1", GARAGE), ("i2", WORKSHOP)):
        cid = f"cid-{iid}"
        prompts.register(cid, f"{base}:printer:{pid}", "How many?", ["1"])
        entries[scope + (f"model-link:{iid}",)] = (cid, lambda text: str(int(text)))
    link_intake._typed_prompts.update(entries)
    try:
        assert link_intake.typed_reply(source, "3") is None  # would cross printers
        prompts.resolve_gateway_clarify("cid-i2", "1")
        link_intake._typed_prompts.pop(scope + ("model-link:i2",))
        assert link_intake.typed_reply(source, "3") == ""  # one printer left: routes
    finally:
        link_intake._typed_prompts.pop(scope + ("model-link:i1",), None)
        link_intake._typed_prompts.pop(scope + ("model-link:i2",), None)
        prompts.resolve_gateway_clarify("cid-i1", "x")


class _HoldingAdapter:
    """Records questions and leaves them open (no immediate answer)."""

    def __init__(self):
        self.asked = []

    async def send_clarify(self, **kw):
        self.asked.append(kw)
        return S(success=True)


def test_typed_reply_counts_an_open_button_only_prompt_for_another_printer(monkeypatch):
    # The ambiguity check must see every open prompt, not only the ones
    # that registered a typed parser: a button-only colour question for
    # the workshop printer is open beside a typed quantity question for
    # the garage one, and a typed "2" could be answering either — so it
    # resolves neither (#536 close-out; before, it fed the garage parser).
    monkeypatch.setattr(link_intake, "_typed_prompts", {})
    monkeypatch.setattr(link_intake, "_open_prompts", {})
    source = runtime.make_source(OPERATOR, HOME, platform_name="discord")
    base = "sess:536-closeout"

    async def flow():
        adapter = _HoldingAdapter()

        async def open_prompt(flow_key, question, choices, pid, parser=None):
            before = len(adapter.asked)
            task = asyncio.create_task(link_intake.ask(
                adapter, source, flow_key, question, choices, timeout=30, parser=parser,
                live_key=prompts.printer_scoped_key(base, pid)))
            for _ in range(200):
                if len(adapter.asked) > before:
                    return task, adapter.asked[-1]["clarify_id"]
                await asyncio.sleep(0)
            raise AssertionError("question was not sent")

        garage_task, garage_cid = await open_prompt(
            "model-link:g1", "How many copies?", ["1", "2"], GARAGE,
            parser=lambda text: int(text))
        shop_task, shop_cid = await open_prompt(
            "model-link:w1", "Which colour?", ["Red", "Blue"], WORKSHOP)
        try:
            assert link_intake.typed_reply(source, "2") is None  # ambiguous: resolves nothing
            assert prompts.is_open(garage_cid) and prompts.is_open(shop_cid)
            # The colour question closes by button; the reply has one home left.
            assert prompts.resolve_gateway_clarify(shop_cid, "Red")
            assert await asyncio.wait_for(shop_task, 2) == 0
            assert link_intake.typed_reply(source, "2") == ""
            assert await asyncio.wait_for(garage_task, 2) == 1
        finally:
            for task in (garage_task, shop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(garage_task, shop_task, return_exceptions=True)

    run(flow())


# ----------------------------------------------------------------------
# announcements name the printer


def test_format_event_names_the_printers_on_a_multi_printer_roster(plugin_cfg, monkeypatch):
    from print_handy.runtime import discord_print_summary as summary

    job = {"printer_id": WORKSHOP, "title": "Bench", "derived_plates": []}
    monkeypatch.setattr(summary, "find_job", lambda *a, **k: (job, {"seconds": 60}))
    text = summary.format_event("complete", "bench.gcode")
    assert text.splitlines()[-1] == "Printer: Workshop AD5X"
    assert summary.format_started(job).splitlines()[-1] == "Printer: Workshop AD5X"


def test_format_event_is_unchanged_on_a_single_printer_roster(monkeypatch):
    from print_handy.runtime import discord_print_summary as summary

    data = copy.deepcopy(_config.cfg())
    data["printers"] = data["printers"][:1]
    monkeypatch.setattr(_config, "_override", data)
    job = {"printer_id": GARAGE, "title": "Bench", "derived_plates": []}
    monkeypatch.setattr(summary, "find_job", lambda *a, **k: (job, {"seconds": 60}))
    assert "Printer:" not in summary.format_event("complete", "bench.gcode")


# ----------------------------------------------------------------------
# the completion retry state is keyed by printer


def test_pending_completion_is_keyed_by_serving_printer(monkeypatch, tmp_path):
    from print_handy import listeners
    from print_handy.runtime import moonraker_discord_announcements as announcer

    serving = {"pid": WORKSHOP}
    monkeypatch.setattr(listeners, "instance_printer_id", lambda: serving["pid"])
    state = {}
    announcer.set_pending_completion_message(state, "Workshop done")
    announcer.set_pending_timelapse_job_key(state, "workshop-job")
    serving["pid"] = GARAGE
    assert announcer.pending_completion_message(state) is None  # not the other printer's
    announcer.set_pending_completion_message(state, "Garage done")
    assert announcer.pending_completion_message(state) == "Garage done"
    assert state["pending_completion_messages"] == {WORKSHOP: "Workshop done", GARAGE: "Garage done"}
    announcer.clear_pending_completion_message(state, "Garage done")
    assert state["pending_completion_messages"] == {WORKSHOP: "Workshop done"}
    serving["pid"] = WORKSHOP
    assert announcer.pending_completion_message(state) == "Workshop done"
    assert announcer.pending_timelapse_job_key(state) == "workshop-job"


def test_pending_completion_adopts_the_legacy_single_slot(monkeypatch):
    from print_handy import listeners
    from print_handy.runtime import moonraker_discord_announcements as announcer

    monkeypatch.setattr(listeners, "instance_printer_id", lambda: WORKSHOP)
    state = {"pending_completion_message": "Print complete: Stand"}
    assert announcer.pending_completion_message(state) == "Print complete: Stand"
    announcer.set_pending_completion_message(state, "New completion")
    assert "pending_completion_message" not in state
    assert state["pending_completion_messages"] == {WORKSHOP: "New completion"}
    announcer.clear_pending_completion_message(state, "New completion")
    assert announcer.pending_completion_message(state) is None


# ----------------------------------------------------------------------
# corpus-harness parity: the same picker flow on both transports


def test_picker_flow_through_corpus_harness_fakes(plugin_cfg, monkeypatch):
    from corpus import harness

    plugin = harness.load_plugin()
    calls = []
    _intake_stubs(monkeypatch, calls)
    adapter = harness.FakeAdapter([{"question": "Which printer", "button": "Workshop"}])
    gateway = S(_is_user_authorized_for_source=lambda source: True,
                _delivery_adapter_for=lambda source: adapter,
                _session_key_for_source=lambda source: f"{source.chat_id}:{source.user_id}",
                printer_id_for=lambda source: None,
                shared_printer_ids_for=lambda source: [GARAGE, WORKSHOP])
    out = run(plugin.model_link_dispatch(shared_event(LINK), gateway))
    assert out == {"action": "skip", "reason": "recorded intake"}
    assert adapter.questions and adapter.questions[0]["picked"] == "Workshop AD5X"
    assert calls[-1] == ("prepare", {"reuse": None, "printer_id": WORKSHOP})


def test_picker_flow_through_the_standalone_bot(cfg, monkeypatch):
    cfg = two_printer_cfg(cfg)
    monkeypatch.setattr(_config, "_override", cfg)  # the plugin reads the process config
    calls = []
    _intake_stubs(monkeypatch, calls)
    discord = fake_discord.FakeDiscord(
        clicks=[{"question": "Which printer", "button": "Workshop"}],
        chat_id=int(cfg["discord"]["chat_id"]), operator_id=int(OPERATOR))
    core = runtime.BotCore(discord.client, cfg, router=Router())
    assert core.shared_printers == {cfg["discord"]["chat_id"]: [GARAGE, WORKSHOP]}
    run(core.handle_message(discord.message(LINK)))
    assert discord.questions and discord.questions[0]["picked"] == "Workshop AD5X"
    assert calls[-1] == ("prepare", {"reuse": None, "printer_id": WORKSHOP})
