"""Typed replies answer only the operator who was asked, in that channel or thread, for that open intake.

Two allowed operators share one print channel. A typed number from one must never answer the other's
quantity or fit question, and a question that already closed must not swallow the next typed message.
Fake prompts only: no Discord login, no printer, no slicing.
"""
import asyncio
from types import SimpleNamespace as S
from unittest import mock

import pytest

from corpus import fake_discord
from print_handy.bot import runtime
from print_handy.core import link_intake, prompts

A = "200000000000000002"
B = "200000000000000003"
CHAT = "100000000000000001"
CHOICES = ["One copy", "Two copies"]


@pytest.fixture(autouse=True)
def _core_registry(monkeypatch):
    monkeypatch.setattr(link_intake, "_typed_prompts", {})


class Adapter:
    """Records each question; with ``inner``, still sends it (the bot's real adapter on fake Discord)."""

    def __init__(self, inner=None):
        self.inner = inner
        self.asked = []

    async def send_clarify(self, **kw):
        self.asked.append(kw)
        return await self.inner.send_clarify(**kw) if self.inner else S(success=True)


def quantity(text):
    value = int(str(text).strip())  # ValueError keeps the prompt open, like parse_quantity
    if not 1 <= value <= 4:
        raise ValueError("pick 1 to 4")
    return value


def run(coro):
    return asyncio.run(coro)


async def ask_quantity(adapter, source, intake):
    """Open a typed quantity question the way prepared_intake does; return (task, clarify_id)."""
    before = len(adapter.asked)
    task = asyncio.create_task(link_intake.ask(
        adapter, source, "model-link:" + intake, "How many copies?", CHOICES, 5,
        parser=quantity, live_key=runtime.session_key(source)))
    for _ in range(200):
        if len(adapter.asked) > before:
            return task, adapter.asked[-1]["clarify_id"]
        await asyncio.sleep(0)
    raise AssertionError("question was not sent")


async def settle(*tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("thread", [None, "300000000000000009"])
def test_another_operator_cannot_answer_a_typed_question(thread):
    async def flow():
        adapter = Adapter()
        asker = runtime.make_source(A, CHAT, thread)
        task, cid = await ask_quantity(adapter, asker, "a1")
        try:
            assert link_intake.typed_reply(runtime.make_source(B, CHAT, thread), "2") is None
            assert prompts.is_open(cid), "the other operator's text answered this question"
            assert link_intake.typed_reply(asker, "3") == ""
            return await asyncio.wait_for(task, 2)
        finally:
            await settle(task)

    assert run(flow()) == 3


def test_two_operators_in_one_channel_each_answer_only_their_own_question():
    async def flow():
        adapter = Adapter()
        first, second = runtime.make_source(A, CHAT), runtime.make_source(B, CHAT)
        task_a, _ = await ask_quantity(adapter, first, "a1")
        task_b, _ = await ask_quantity(adapter, second, "b1")
        try:
            assert link_intake.typed_reply(first, "1") == ""
            assert link_intake.typed_reply(second, "2") == ""
            return await asyncio.wait_for(asyncio.gather(task_a, task_b), 2)
        finally:
            await settle(task_a, task_b)

    assert run(flow()) == [1, 2]


def test_a_reply_in_another_thread_does_not_answer_the_channel_question():
    async def flow():
        adapter = Adapter()
        asker = runtime.make_source(A, CHAT)
        task, cid = await ask_quantity(adapter, asker, "a1")
        try:
            assert link_intake.typed_reply(runtime.make_source(A, CHAT, "300000000000000009"), "2") is None
            assert prompts.is_open(cid)
        finally:
            await settle(task)

    run(flow())


def test_one_operator_with_two_open_intakes_answers_the_newest_then_the_older():
    async def flow():
        adapter = Adapter()
        asker = runtime.make_source(A, CHAT)
        older, _ = await ask_quantity(adapter, asker, "a1")
        newer, _ = await ask_quantity(adapter, asker, "a2")
        try:
            assert link_intake.typed_reply(asker, "2") == ""
            assert await asyncio.wait_for(newer, 2) == 2
            assert link_intake.typed_reply(asker, "1") == ""
            return await asyncio.wait_for(older, 2)
        finally:
            await settle(older, newer)

    assert run(flow()) == 1


def test_an_answered_question_does_not_capture_the_next_typed_reply():
    async def flow():
        adapter = Adapter()
        asker = runtime.make_source(A, CHAT)
        task, cid = await ask_quantity(adapter, asker, "a1")
        try:
            assert prompts.resolve_gateway_clarify(cid, "Two copies")  # a button press closes it
            # The intake has not resumed yet; the typed text must fall through, not vanish.
            assert link_intake.typed_reply(asker, "3") is None
            picked = await asyncio.wait_for(task, 2)
            assert link_intake.typed_reply(asker, "3") is None
            return picked
        finally:
            await settle(task)

    assert run(flow()) == CHOICES.index("Two copies")


def test_a_cancelled_intake_leaves_no_typed_prompt_behind():
    async def flow():
        adapter = Adapter()
        asker = runtime.make_source(A, CHAT)
        task, cid = await ask_quantity(adapter, asker, "a1")
        assert prompts.resolve_gateway_clarify(cid, link_intake.CANCEL_LABEL)
        with pytest.raises(link_intake.PromptCancelled):
            await asyncio.wait_for(task, 2)
        assert link_intake.typed_reply(asker, "2") is None
        assert link_intake._typed_prompts == {}

    run(flow())


def test_bot_routes_a_typed_quantity_only_to_the_operator_who_was_asked(cfg):
    """The reviewer's case on the standalone bot: both operators are allowed, only the asker answers."""
    cfg["discord"]["allowed_user_ids"] = f"{A},{B}"
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(CHAT), operator_id=int(A))
    core = runtime.BotCore(discord.client, cfg)
    other = fake_discord.FakeUser(int(B), "second operator")

    async def flow():
        asker = runtime.make_source(A, CHAT)
        task, cid = await ask_quantity(Adapter(core.adapter), asker, "a1")
        try:
            out = await core.handle_message(discord.message("2", author=other))
            assert out != {"action": "skip", "reason": "typed print quantity handled"}
            assert prompts.is_open(cid), "the second operator's typed text answered the first one's question"
            out = await core.handle_message(discord.message("3"))
            assert out == {"action": "skip", "reason": "typed print quantity handled"}
            return await asyncio.wait_for(task, 2)
        finally:
            await settle(task)

    with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
        assert run(flow()) == 3


async def _ask_secret(adapter, source, parser, intake):
    """Open a connect-style typed prompt (its parser saves a pasted secret)."""
    before = len(adapter.asked)
    task = asyncio.create_task(link_intake.ask(
        adapter, source, "model-link:" + intake, "Paste the access code", ["Save", "Cancel"], 5,
        parser=parser, live_key=runtime.session_key(source)))
    for _ in range(200):
        if len(adapter.asked) > before:
            return task, adapter.asked[-1]["clarify_id"]
        await asyncio.sleep(0.005)
    raise AssertionError("the prompt was never sent")


def test_a_closed_connect_prompt_never_runs_its_credential_parser():
    """P2-253: the prompt closed (a button answered it) but the intake has not
    resumed yet, so the typed-prompt entry still exists. The parser — which
    for a connect prompt writes the pasted credential to config — must not
    run at all; the text falls through as an ordinary message."""
    async def flow():
        adapter = Adapter()
        asker = runtime.make_source(A, CHAT)
        calls = []

        def secret_parser(text):
            calls.append(text)
            link_intake.note_secret_reply()
            return "saved"

        task, cid = await _ask_secret(adapter, asker, secret_parser, "closed")
        try:
            assert prompts.resolve_gateway_clarify(cid, "Cancel")  # a button closes it first
            assert link_intake.typed_reply(asker, "the-pasted-token") is None
            assert calls == []
            assert link_intake.take_secret_reply() is False
        finally:
            await settle(task)

    run(flow())


def test_a_secret_saved_as_its_prompt_closes_is_still_consumed():
    """P2-253: the prompt was open when the reply arrived, the parser saved
    the secret, and a button won the race before the resolution landed. The
    message is still consumed (and flagged for deletion), never handed on
    to ordinary chat handling with a live credential in it."""
    async def flow():
        adapter = Adapter()
        asker = runtime.make_source(A, CHAT)
        holder = {}

        def racing_parser(text):
            link_intake.note_secret_reply()
            prompts.resolve_gateway_clarify(holder["cid"], "Save")  # the button wins mid-parse
            return "saved"

        task, cid = await _ask_secret(adapter, asker, racing_parser, "raced")
        holder["cid"] = cid
        try:
            assert link_intake.typed_reply(asker, "the-pasted-token") == ""
            assert link_intake.take_secret_reply() is True
        finally:
            await settle(task)

    run(flow())
