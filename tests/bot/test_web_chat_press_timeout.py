"""A web chat button press either runs or never runs (#1145, duckcatgnaf on #1106).

The page waits a bounded time for the press to reach the chat loop. Before
#1145 the wait gave up but left the press scheduled, so after the page said
"that answer didn't reach the bot, the question is still open", the press
could still run later and approve the action, a print included. The press
now claims the right to run on the loop, or the timeout abandons it first.
"""
import asyncio
import concurrent.futures
import threading
import time

import pytest

from print_handy.bot import web
from print_handy.core import prompts


def _open_prompt(service, scope, cid):
    prompts.register(cid, f"webui:{web.CHAT_ID}:{scope}:{web.OPERATOR_ID}",
                     "Start the print?", ["Start", "Cancel"])
    asyncio.run_coroutine_threadsafe(service._in_scope(
        scope, lambda: service.adapter.send_clarify(
            web.CHAT_ID, "Start the print?", ["Start", "Cancel"], cid, "flow-key")),
        service._loop).result(timeout=10)


def _spy_presses(service, delay=0.0):
    pressed = []
    real = service.adapter.answer_choice

    async def spy(clarify_id, index, **kwargs):
        pressed.append((clarify_id, index))
        if delay:
            await asyncio.sleep(delay)
        return await real(clarify_id, index, **kwargs)

    service.adapter.answer_choice = spy
    return pressed


def _drain(service):
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), service._loop).result(timeout=10)


def test_a_press_that_times_out_on_a_blocked_loop_never_applies(cfg):
    service = web.WebChatService(cfg)
    service.press_wait_s = 0.2
    scope = web.scope_for("operator")
    cid = "cid-press-timeout-1"
    _open_prompt(service, scope, cid)
    pressed = _spy_presses(service)
    release = threading.Event()

    async def hog():
        release.wait(10)  # blocks the chat loop thread itself

    hogged = asyncio.run_coroutine_threadsafe(hog(), service._loop)
    with pytest.raises(concurrent.futures.TimeoutError):
        service.view(scope).answer(cid, 0)
    release.set()
    hogged.result(timeout=10)
    _drain(service)
    time.sleep(0.2)
    _drain(service)
    assert pressed == []
    assert prompts.is_open(cid)
    # The question really is still open: a fresh press now works.
    assert service.view(scope).answer(cid, 1) == "Cancel"


def test_a_press_that_started_before_the_timeout_reports_its_real_result(cfg):
    service = web.WebChatService(cfg)
    service.press_wait_s = 0.1
    scope = web.scope_for("operator")
    cid = "cid-press-timeout-2"
    _open_prompt(service, scope, cid)
    pressed = _spy_presses(service, delay=0.5)  # runs past the wait
    assert service.view(scope).answer(cid, 0) == "Start"
    assert pressed == [(cid, 0)]
    assert not prompts.is_open(cid)


def test_the_gate_admits_exactly_one_side():
    gate = web._PressGate()
    assert gate.claim() and not gate.abandon() and not gate.claim()
    gate = web._PressGate()
    assert gate.abandon() and not gate.claim()
