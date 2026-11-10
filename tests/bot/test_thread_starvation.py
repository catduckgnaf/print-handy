"""Open did-you-mean prompts and queued model asks hold no worker threads.

On a small container the default executor has a handful of threads. Each
open did-you-mean prompt used to park one on the registry's blocking wait
for up to a minute, and each ask queued behind the same session's model
call parked another on the session lock, so a few messages froze every
other background call. These tests run the waits on a one-thread executor
and check that unrelated work still gets through.
"""
import asyncio
import concurrent.futures
import threading
import time

from print_handy.bot import runtime
from print_handy.core import prompts


def _one_thread_loop_run(coro):
    async def main():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=1))
        return await coro()
    return asyncio.run(main())


def test_open_prompts_do_not_occupy_worker_threads():
    ids = [f"starve-{n}" for n in range(8)]
    for cid in ids:
        prompts.register(cid, "s", "Did you mean?", ["Yes", "No"])

    async def scenario():
        waits = [asyncio.ensure_future(runtime.await_prompt(prompts, cid, 5)) for cid in ids]
        await asyncio.sleep(0.05)
        started = time.monotonic()
        # The only worker thread is free: the waits sit on the event loop.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "ran"), 1) == "ran"
        elapsed = time.monotonic() - started
        for cid in ids:
            prompts.resolve_gateway_clarify(cid, "Yes")
        answers = await asyncio.wait_for(asyncio.gather(*waits), 2)
        return elapsed, answers

    elapsed, answers = _one_thread_loop_run(scenario)
    assert elapsed < 1 and answers == ["Yes"] * len(ids)
    assert not any(prompts.is_open(cid) for cid in ids)


def test_a_prompt_wait_times_out_and_closes_the_prompt():
    prompts.register("starve-timeout", "s", "Did you mean?", ["Yes", "No"])

    async def scenario():
        return await runtime.await_prompt(prompts, "starve-timeout", 0.2)

    assert _one_thread_loop_run(scenario) is None
    assert not prompts.is_open("starve-timeout")
    assert prompts.resolve_gateway_clarify("starve-timeout", "Yes") is False  # a late press finds nothing


def test_queued_turns_for_one_session_run_in_order_without_threads():
    turns = runtime.SessionTurns()
    gate = threading.Event()
    order = []

    def blocking_call(label):
        if label == "first":
            gate.wait(5)
        order.append(label)
        return label

    async def ask(label, key="session-a"):
        async with turns.turn(key):
            return await asyncio.to_thread(blocking_call, label)

    async def scenario():
        first = asyncio.ensure_future(ask("first"))
        await asyncio.sleep(0.05)
        queued = [asyncio.ensure_future(ask(f"q{n}")) for n in range(5)]
        await asyncio.sleep(0.05)
        # Five asks wait behind the first one's network call, yet the
        # single worker thread is busy only with that call: the queue holds
        # no thread, so releasing the call drains everything in order.
        assert not any(task.done() for task in queued)
        gate.set()
        return await asyncio.wait_for(asyncio.gather(first, *queued), 5)

    results = _one_thread_loop_run(scenario)
    assert results == ["first", "q0", "q1", "q2", "q3", "q4"]
    assert order == results


def test_the_turn_store_is_bounded_and_never_drops_a_held_lock():
    turns = runtime.SessionTurns(max_sessions=3)

    async def scenario():
        held = asyncio.Event()
        release = asyncio.Event()

        async def hold():
            async with turns.turn("busy"):
                held.set()
                await release.wait()

        holder = asyncio.ensure_future(hold())
        await held.wait()
        busy_lock = turns._locks["busy"][0]
        for n in range(10):
            async with turns.turn(f"idle-{n}"):
                pass
        assert len(turns) <= 3
        assert turns._locks["busy"][0] is busy_lock  # still the one lock for that key
        release.set()
        await holder

    asyncio.run(scenario())
