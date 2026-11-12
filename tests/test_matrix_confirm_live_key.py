"""Regression tests: Matrix confirmation prompts in the print-feedback flows.

Follow-up to PR #426, which fixed this for the design prompt only. Every
other `_confirm` caller in plugins/print-feedback registered its prompt
under the flow key (`<prefix>:<chat>:<thread>`), but the bot core routes
typed answers by the live session key and the adapters match that key
exactly. Matrix has no buttons (`supports_buttons=False`), so its numbered
replies are typed answers: they never matched the flow key, the prompt
waited out its timeout, and the flow reported a cancel — the affected
flows (cold pull, maintenance, macros, troubleshoot, resume, print-again,
force restart) were unusable from Matrix.

These tests run the whole path for real — the actual Matrix adapter, the
actual prompt registry, no mocked wait — because mocked-wait dispatch
tests cannot see a registration-key mismatch. Each test fails on the
pre-fix code (the numbered reply returns False and the confirmation times
out as a cancel) and passes with the live_key pass-through. One test
covers a direct dispatch (macros); the other covers the helper-threaded
shape (cold pull, whose `_coldpull_workflow` helper has no gateway in
scope and receives the key from `coldpull_dispatch`).

The last three tests cover the flows that register inline instead of
going through `_confirm` — /print-next, its spool-change follow-up
(`_offer_spool_change`, which receives the key from `print_next_dispatch`
exactly like the cold pull helper does) and /print-stop — which kept the
flow-key registration after the `_confirm` callers were fixed (#524).
"""
import asyncio
import importlib.util
import io
import json
import urllib.parse
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy import macros, printers
from print_handy.core import prompts as core_prompts
from print_handy.runtime import _config, maintenance, print_next, print_stop, spool_sync
from print_handy.runtime import discord_print_summary as print_summary
from print_handy.runtime import print_control as pc

ROOT = Path(__file__).parents[1]
PLUGIN_PATH = ROOT / "plugins/print-feedback/__init__.py"
spec = importlib.util.spec_from_file_location("print_feedback_matrix_confirm", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

LIVE_KEY = "matrix:c::42"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class HomeserverStub:
    """Records every room message body the Matrix adapter posts."""

    def __init__(self):
        self.bodies = []

    def __call__(self, req, timeout=None):
        path = urllib.parse.urlsplit(req.full_url).path
        body = (json.loads(req.data) if req.data and
                req.headers.get("Content-type") == "application/json" else None)
        if "/send/m.room.message/" in path:
            self.bodies.append((body or {}).get("body", ""))
            return _Response(json.dumps({"event_id": "$evt"}).encode())
        if path.endswith("/media/v3/upload"):
            return _Response(json.dumps({"content_uri": "mxc://example.org/m"}).encode())
        raise AssertionError(f"unexpected homeserver call: {req.method} {req.full_url}")

    def prompt_count(self):
        return sum("Reply with the number" in body for body in self.bodies)


def _matrix_adapter(stub):
    from print_handy.bot import matrix as matrix_mod
    return matrix_mod.StandaloneAdapter(None, ["42"], homeserver="https://matrix.example.org",
                                        token="mx-test-access", opener=stub)


def _event(text):
    source = S(platform=S(value="matrix"), profile="3dprinting", is_bot=False,
               profile_route_rejected=False, user_id="42", chat_id="c", thread_id=None)
    return S(source=source, text=text, internal=False, message_id="m1", metadata={})


def _gateway(adapter):
    return S(_is_user_authorized_for_source=lambda s: True,
             _delivery_adapter_for=lambda s: adapter,
             _session_key_for_source=lambda s: LIVE_KEY)


@pytest.fixture
def confirm_env():
    # Short prompt windows: a prompt that can never be answered (the bug)
    # times out in seconds instead of the production minutes.
    previous = _config.cfg()  # the session test settings from tests/conftest.py; restore, never clear
    _config.set_override({"discord": {"question_timeout_s": 5,
                                      "safety_confirm_timeout_s": 5,
                                      "cold_pull_outcome_s": 5}})
    yield
    _config.set_override(previous)


async def _wait_for_prompt(stub, count):
    for _ in range(600):
        if stub.prompt_count() >= count:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"prompt {count} never reached the room")


# ------------------------------------------------------------- direct dispatch: macros

OBJECTS = {"result": {"objects": ["toolhead", "extruder", "heater_bed",
                                  "gcode_macro WIPE_NOZZLE_CUSTOM"]}}


class MacroTransport:
    """The spool_sync.moonraker call shape: objects list, print state, one script POST."""

    def __init__(self):
        self.calls = []
        self.posts = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == macros.PRINT_STATE_PATH:
            return {"result": {"status": {"print_stats": {"state": "standby"}}}}
        if method == "POST":
            self.posts.append(body)
            return {"result": "ok"}
        return OBJECTS


def test_macros_prompt_answers_a_numbered_reply_through_the_real_matrix_adapter(confirm_env):
    stub = HomeserverStub()
    adapter = _matrix_adapter(stub)
    transport = MacroTransport()

    async def scenario():
        with mock.patch.object(printers, "current", return_value=S(connection="moonraker")), \
                mock.patch.object(spool_sync, "moonraker", transport), \
                mock.patch.object(core_prompts, "gateway", return_value=core_prompts):
            task = asyncio.create_task(plugin.macros_dispatch(
                _event("/print-macros run WIPE_NOZZLE_CUSTOM"), _gateway(adapter)))
            await _wait_for_prompt(stub, 1)
            answered = await adapter.answer_typed(LIVE_KEY, "1")
            out = await asyncio.wait_for(task, 15)
            return answered, out

    answered, out = asyncio.run(scenario())
    assert answered is True
    assert out == {"action": "skip", "reason": "print macros run handled"}
    # The confirmation was honoured: exactly one script POST, the exact line.
    assert transport.posts == [{"script": "WIPE_NOZZLE_CUSTOM"}]
    assert any(body.startswith("> Answered by message:") for body in stub.bodies)
    assert any(body.startswith("Sent `WIPE_NOZZLE_CUSTOM`") for body in stub.bodies)


# ------------------------------------------------------- helper-threaded: cold pull

def test_coldpull_prompts_answer_numbered_replies_through_the_real_matrix_adapter(confirm_env):
    stub = HomeserverStub()
    adapter = _matrix_adapter(stub)
    appended = []

    def fake_coldpull(temp, cold, *, confirmed_at):
        return True, "Cold pull finished; heater target is 0."

    async def scenario():
        with mock.patch.object(core_prompts, "gateway", return_value=core_prompts), \
                mock.patch.object(maintenance, "load", lambda *a, **k: {"runs": []}), \
                mock.patch.object(maintenance, "append_run",
                                  lambda record, **k: appended.append(record) or record), \
                mock.patch.object(pc, "coldpull", fake_coldpull), \
                mock.patch.object(plugin, "_maintenance_hours_now", lambda: 12.0):
            task = asyncio.create_task(plugin.coldpull_dispatch(
                _event("/print-coldpull"), _gateway(adapter)))
            results = []
            seen = 0
            while not task.done():
                if stub.prompt_count() > seen:
                    seen = stub.prompt_count()
                    results.append(await adapter.answer_typed(LIVE_KEY, "1"))
                await asyncio.sleep(0.02)
            out = await asyncio.wait_for(task, 15)
            return results, out

    results, out = asyncio.run(scenario())
    # Material, actuate and tip prompts — every one answered by number.
    assert results == [True, True, True]
    assert out == {"action": "skip", "reason": "cold pull handled"}
    assert appended and appended[-1]["cold_pull"]["result"] == "clean"
    assert any("Cold pull finished; heater target is 0." in body for body in stub.bodies)
    assert sum(body.startswith("> Answered by message:") for body in stub.bodies) == 3


# ------------------------------------------- inline registration: /print-stop

def test_print_stop_prompt_answers_a_numbered_reply_through_the_real_matrix_adapter(confirm_env):
    stub = HomeserverStub()
    adapter = _matrix_adapter(stub)
    cancelled = []
    state = {"state": "printing", "filename": "a.gcode", "layer": None, "total_layer": None,
             "progress": None, "print_duration": None, "camera_image_path": None}

    async def scenario():
        with mock.patch.object(core_prompts, "gateway", return_value=core_prompts), \
                mock.patch.object(print_stop, "status", lambda **kw: dict(state)), \
                mock.patch.object(print_stop, "cancel",
                                  lambda filename, **kw: cancelled.append(filename)
                                  or (True, "Stopped. Printer now reports cancelled.")), \
                mock.patch.object(print_summary, "format_event", lambda *a, **k: "Started: a.gcode"):
            task = asyncio.create_task(plugin.print_stop_dispatch(
                _event("/print-stop"), _gateway(adapter)))
            await _wait_for_prompt(stub, 1)
            answered = await adapter.answer_typed(LIVE_KEY, "1")
            out = await asyncio.wait_for(task, 15)
            return answered, out

    answered, out = asyncio.run(scenario())
    assert answered is True
    assert out == {"action": "skip", "reason": "deterministic print stop handled"}
    # The confirmation was honoured: exactly one cancel, for the confirmed file.
    assert cancelled == ["a.gcode"]
    assert any(body.startswith("> Answered by message:") for body in stub.bodies)
    assert any("Stopped. Printer now reports cancelled." in body for body in stub.bodies)


# ------------------------------------------- inline registration: /print-next

NEXT_TARGET = {"label": "Tail", "title": "Widget", "copy": 1, "quantity": 1, "where": "",
               "gcode": "/tmp/tail.gcode", "job_json": "/tmp/JOB.json"}


def _print_next_env(start):
    return mock.patch.multiple(
        print_next, load_plan=lambda plan_id: {"plan_id": plan_id},
        resolve_next=lambda plan, part_id=None: dict(NEXT_TARGET), start=start)


def test_print_next_prompt_answers_a_numbered_reply_through_the_real_matrix_adapter(confirm_env):
    stub = HomeserverStub()
    adapter = _matrix_adapter(stub)
    started = []

    async def fake_start(target):
        started.append(target)
        return True, "START REQUEST ACCEPTED"

    async def scenario():
        with mock.patch.object(core_prompts, "gateway", return_value=core_prompts), \
                _print_next_env(fake_start), \
                mock.patch.object(print_summary, "find_job",
                                  lambda gcode: ({"derived_plates": []}, {"seconds": 600})), \
                mock.patch.object(print_summary, "_print_number", lambda job: 3):
            task = asyncio.create_task(plugin.print_next_dispatch(
                _event("/print-next ghost"), _gateway(adapter)))
            await _wait_for_prompt(stub, 1)
            answered = await adapter.answer_typed(LIVE_KEY, "1")
            out = await asyncio.wait_for(task, 15)
            return answered, out

    answered, out = asyncio.run(scenario())
    assert answered is True
    assert out == {"action": "skip", "reason": "deterministic next plate handled"}
    # The confirmation was honoured: exactly one start, for the exact plate.
    assert [target["gcode"] for target in started] == ["/tmp/tail.gcode"]
    assert any(body.startswith("Preparing to start Tail") for body in stub.bodies)


def test_print_next_spool_change_prompt_answers_a_numbered_reply_through_the_real_matrix_adapter(confirm_env):
    stub = HomeserverStub()
    adapter = _matrix_adapter(stub)
    started = []
    results = [(False, "slot 0 spool changed since slicing (was 11, now 22)"),
               (True, "START REQUEST ACCEPTED")]

    async def fake_start(target):
        started.append(target)
        return results[len(started) - 1]

    async def scenario():
        with mock.patch.object(core_prompts, "gateway", return_value=core_prompts), \
                _print_next_env(fake_start), \
                mock.patch.object(print_summary, "find_job",
                                  lambda gcode: ({"derived_plates": []}, {"seconds": 600})), \
                mock.patch.object(print_summary, "_print_number", lambda job: 3):
            task = asyncio.create_task(plugin.print_next_dispatch(
                _event("/print-next ghost"), _gateway(adapter)))
            await _wait_for_prompt(stub, 1)
            first = await adapter.answer_typed(LIVE_KEY, "1")
            await _wait_for_prompt(stub, 2)
            second = await adapter.answer_typed(LIVE_KEY, "1")
            out = await asyncio.wait_for(task, 15)
            return first, second, out

    first, second, out = asyncio.run(scenario())
    # Both the start confirmation and the spool-change follow-up answered by number.
    assert (first, second) == (True, True)
    assert out == {"action": "skip", "reason": "deterministic next plate handled"}
    assert len(started) == 2
    assert started[1].get("accept_spool_change") is True
