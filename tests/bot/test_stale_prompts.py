"""A button that outlives its question: restart, timeout, answered, replayed, racing.

Nothing is restored or replayed from the ledger. A stale press retires the dead buttons, says that
nothing changed and how to start again, and resolves nothing: no start, no upload, no approval.
"""
import asyncio
import json
from unittest import mock

import pytest

from corpus import fake_discord
from print_handy.bot import adapter as adapter_mod, prompt_ledger, runtime
from print_handy.bot.adapter import parse_custom_id
from print_handy.core import prompts

CHAT = 100000000000000001
OPERATOR = 200000000000000002
APPROVER = 200000000000000009
OTHER = 200000000000000003
SESSION = f"discord:{CHAT}:0:{OPERATOR}"
IID = "a" * 32
LABELS = ["Print anyway", "Cancel"]


@pytest.fixture
def rig(cfg, tmp_path, monkeypatch):
    state = tmp_path / "state"
    (state / "model-link-intakes").mkdir(parents=True)
    monkeypatch.setattr(prompt_ledger, "_path", lambda: state / prompt_ledger.FILE_NAME)
    monkeypatch.setattr("print_handy.runtime._config.state_dir", lambda: state)
    cfg["discord"]["approval_user_id"] = str(APPROVER)
    discord = fake_discord.FakeDiscord(auto=False, chat_id=CHAT, operator_id=OPERATOR)
    core = runtime.BotCore(discord.client, cfg)
    return discord, core, state


def run(coro):
    return asyncio.run(coro)


def receipt(state, status="SLICED_NOT_STARTED", started=False):
    path = state / "model-link-intakes" / f"{IID}.json"
    path.write_text(json.dumps({"intake_id": IID, "status": status, "started": started,
                                "uploaded": False}))
    return path


async def post(discord, core, *, approval=True, session=SESSION):
    cid = "c" * 32
    prompts.register(cid, session, "Support coverage is low.", LABELS)
    if approval:
        prompts.mark_approval(cid)
    sent = await core.adapter.send_clarify(str(CHAT), "Support coverage is low.", LABELS, cid,
                                           f"model-link:{IID}")
    assert sent.success
    message = discord.last_question()
    return cid, message, dict((label, c) for label, c in discord.prompt_buttons(message))


def restart(cid):
    """What a restart does to this process: the in-memory registry is gone, the message is not."""
    prompts._prompts.pop(cid, None)
    prompts._approval_marks.pop(cid, None)


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    prompts._prompts.clear()
    prompts._approval_marks.clear()


def test_after_a_restart_a_dead_approval_is_retired_and_explained(rig, monkeypatch):
    discord, core, state = rig
    receipt(state)

    async def flow():
        cid, message, buttons = await post(discord, core)
        restart(cid)
        monkeypatch.setattr(prompt_ledger, "BOOT_ID", "another-boot")
        starts = mock.Mock()
        with mock.patch.object(prompts, "resolve_gateway_clarify", starts):
            approver = fake_discord.FakeUser(APPROVER, "approver")
            clicked = await discord.press(message, buttons["Print anyway"], user=approver)
        return cid, message, clicked, starts

    cid, message, clicked, resolve = run(flow())
    text = clicked.replies[-1]["content"]
    assert clicked.replies[-1]["ephemeral"] is True
    assert "restarted" in text and "Nothing changed" in text
    assert "no print was started" in text and "nothing was uploaded" in text
    assert "approval was not recorded" in text
    assert "saved but not printed" in text and "send the model link or file again" in text
    assert message.view is None  # the dead buttons are gone
    assert prompt_ledger.lookup(cid) is None
    resolve.assert_not_called()
    assert not prompts.is_open(cid)


def test_the_saved_receipt_is_untouched_and_never_started(rig, monkeypatch):
    discord, core, state = rig
    path = receipt(state)
    before = path.read_text()

    async def flow():
        cid, message, buttons = await post(discord, core)
        restart(cid)
        monkeypatch.setattr(prompt_ledger, "BOOT_ID", "another-boot")
        return await discord.press(message, buttons["Print anyway"],
                                   user=fake_discord.FakeUser(APPROVER, "approver"))

    with mock.patch("print_handy.launch.print_now.main", create=True) as start:
        clicked = run(flow())
    assert path.read_text() == before  # exact state: SLICED_NOT_STARTED, no upload, no start
    start.assert_not_called()
    assert json.loads(before)["status"] == "SLICED_NOT_STARTED"
    assert clicked.acks == []  # never acknowledged as a decision


def test_a_non_approver_cannot_learn_or_retire_an_approval_message(rig, monkeypatch):
    discord, core, state = rig

    async def flow():
        cid, message, buttons = await post(discord, core)
        restart(cid)
        monkeypatch.setattr(prompt_ledger, "BOOT_ID", "another-boot")
        stranger = fake_discord.FakeUser(OPERATOR, "operator")  # allowed to chat, not an approver
        return message, await discord.press(message, buttons["Print anyway"], user=stranger)

    message, clicked = run(flow())
    assert clicked.replies[-1]["content"] == "Not allowed. Nothing changed."
    assert message.view is not None  # an unauthorized press retires nothing


def test_a_press_through_another_channel_is_refused_and_retires_nothing(rig, monkeypatch):
    discord, core, state = rig

    async def flow():
        cid, message, buttons = await post(discord, core)
        restart(cid)
        monkeypatch.setattr(prompt_ledger, "BOOT_ID", "another-boot")
        other = fake_discord.FakeChannel(discord, CHAT + 7)
        interaction = fake_discord.FakeInteraction(
            discord, fake_discord.FakeUser(APPROVER, "approver"), custom_id=buttons["Print anyway"],
            message=message, channel=other)
        await discord.client.dispatch_interaction(interaction)
        return message, interaction

    message, interaction = run(flow())
    assert interaction.replies[-1]["content"] == "Not allowed. Nothing changed."
    assert message.view is not None


def test_an_ordinary_stale_question_belongs_to_its_initiator(rig, monkeypatch):
    discord, core, state = rig

    async def flow():
        cid, message, buttons = await post(discord, core, approval=False)
        restart(cid)
        monkeypatch.setattr(prompt_ledger, "BOOT_ID", "another-boot")
        other = await discord.press(message, buttons["Cancel"], user=fake_discord.FakeUser(OTHER, "x"))
        mine = await discord.press(message, buttons["Cancel"])
        return message, other, mine

    message, other, mine = run(flow())
    # OTHER is not on the allow list at all: refused before any stale report.
    assert other.replies[-1]["content"] == "Not allowed. Nothing changed."
    assert "Nothing changed" in mine.replies[-1]["content"] and message.view is None


def test_a_timed_out_question_says_it_expired_not_that_print_handy_restarted(rig):
    discord, core, state = rig
    receipt(state)

    async def flow():
        cid, message, buttons = await post(discord, core)
        # the wait timed out in this same process: the registry closes the prompt
        assert await prompts.await_answer(cid, 0.01) is None
        clicked = await discord.press(message, buttons["Print anyway"],
                                      user=fake_discord.FakeUser(APPROVER, "approver"))
        return message, clicked

    message, clicked = run(flow())
    text = clicked.replies[-1]["content"]
    assert "expired or was already answered" in text and "restarted" not in text
    assert "no print was started" in text and message.view is None


def test_a_replayed_click_on_an_answered_question_stays_generic(rig):
    discord, core, state = rig

    async def flow():
        cid, message, buttons = await post(discord, core)
        first = await discord.press(message, buttons["Cancel"],
                                    user=fake_discord.FakeUser(APPROVER, "approver"))
        again = await discord.press(message, buttons["Cancel"],
                                    user=fake_discord.FakeUser(APPROVER, "approver"))
        return cid, first, again

    cid, first, again = run(flow())
    assert prompts._prompts[cid].answer == "Cancel"
    assert again.replies[-1]["content"] == "That question already closed. Nothing changed."


def test_a_losing_racer_reports_stale_and_the_winner_keeps_its_answer(rig):
    discord, core, state = rig
    receipt(state)

    async def flow():
        cid, message, buttons = await post(discord, core)
        approver = fake_discord.FakeUser(APPROVER, "approver")
        real = prompts.resolve_gateway_clarify

        def lose(clarify_id, answer):
            # another press wins between the open check and this resolve
            real(clarify_id, "Cancel")
            return real(clarify_id, answer)

        with mock.patch.object(prompts, "resolve_gateway_clarify", side_effect=lose):
            clicked = await discord.press(message, buttons["Print anyway"], user=approver)
        return cid, clicked

    cid, clicked = run(flow())
    assert prompts._prompts[cid].answer == "Cancel"  # the first answer stands
    assert "Nothing changed" in clicked.replies[-1]["content"]


def test_stale_batched_question_preserves_live_sibling_select(rig, monkeypatch):
    discord, core, _state = rig

    async def flow():
        stale, live = "d" * 32, "e" * 32
        prompts.register(stale, SESSION, "Pick colors", ["Red", "Blue"])
        prompts.register(live, SESSION, "Pick colors", ["Red", "Blue"])
        sent = await core.adapter.send_clarify_batch(
            str(CHAT), "Pick colors", [
                {"label": "Part A", "choices": ["Red", "Blue"], "clarify_id": stale},
                {"label": "Part B", "choices": ["Red", "Blue"], "clarify_id": live},
            ], f"model-link:{IID}")
        assert sent.success
        message = next(m for m in reversed(discord.messages) if m.selects())
        stale_cid = next(cid for _placeholder, cid, _options in message.selects()
                         if parse_custom_id(cid) and parse_custom_id(cid)[0] == stale)
        prompts._prompts.pop(stale, None)
        prompts._approval_marks.pop(stale, None)
        monkeypatch.setattr(prompt_ledger, "BOOT_ID", "another-boot")
        clicked = await discord.press(message, stale_cid, user=fake_discord.FakeUser(OPERATOR))
        return message, clicked, live

    message, clicked, live = run(flow())
    assert message.view is not None
    assert len(message.selects()) == 2
    assert prompts.is_open(live)
    assert "Nothing changed" in clicked.replies[-1]["content"]


def test_a_live_question_still_resolves_normally(rig):
    discord, core, state = rig

    async def flow():
        cid, message, buttons = await post(discord, core)
        clicked = await discord.press(message, buttons["Print anyway"],
                                      user=fake_discord.FakeUser(APPROVER, "approver"))
        return cid, message, clicked

    cid, message, clicked = run(flow())
    assert prompts._prompts[cid].answer == "Print anyway"
    assert clicked.replies == [] and "chose" in message.content
    assert prompt_ledger.lookup(cid) is None


def test_an_unknown_message_keeps_the_generic_reply(rig):
    discord, core, state = rig

    async def flow():
        cid, message, buttons = await post(discord, core)
        restart(cid)
        prompt_ledger.forget(cid)  # a message this install has no record of
        return message, await discord.press(message, buttons["Print anyway"],
                                            user=fake_discord.FakeUser(APPROVER, "approver"))

    message, clicked = run(flow())
    assert clicked.replies[-1]["content"] == "That question already closed. Nothing changed."
    assert message.view is not None


def test_a_started_job_points_to_status_not_a_resend(rig, monkeypatch):
    discord, core, state = rig
    receipt(state, status="START_REQUESTED", started=True)

    async def flow():
        cid, message, buttons = await post(discord, core)
        restart(cid)
        monkeypatch.setattr(prompt_ledger, "BOOT_ID", "another-boot")
        return await discord.press(message, buttons["Print anyway"],
                                   user=fake_discord.FakeUser(APPROVER, "approver"))

    clicked = run(flow())
    text = clicked.replies[-1]["content"]
    assert "already started" in text and "/print-status" in text
    assert "no print was started" not in text and "nothing was uploaded" not in text
    assert "This click did not upload anything, start a print, or record approval." in text
    assert "send the model link" not in text


def test_the_public_text_names_no_identifier(rig, monkeypatch):
    discord, core, state = rig
    receipt(state)

    async def flow():
        cid, message, buttons = await post(discord, core)
        restart(cid)
        monkeypatch.setattr(prompt_ledger, "BOOT_ID", "another-boot")
        return await discord.press(message, buttons["Print anyway"],
                                   user=fake_discord.FakeUser(APPROVER, "approver"))

    text = run(flow()).replies[-1]["content"]
    for private in (IID, str(CHAT), str(OPERATOR), str(APPROVER), "model-link"):
        assert private not in text


def test_ledger_survives_a_new_process_and_prunes(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_ledger, "_path", lambda: tmp_path / "ledger.json")
    assert prompt_ledger.record("x1", channel_id=1, message_id=2, approval=True, initiator=3)
    assert (prompt_ledger.lookup("x1") or {}).get("approval") is True
    data = json.loads((tmp_path / "ledger.json").read_text())
    data["x1"]["posted"] = 1  # a week old or more
    (tmp_path / "ledger.json").write_text(json.dumps(data))
    prompt_ledger.record("x2", channel_id=1, message_id=3)
    assert prompt_ledger.lookup("x1") is None and prompt_ledger.lookup("x2") is not None
    (tmp_path / "ledger.json").write_text("{not json")
    assert prompt_ledger.lookup("x2") is None  # a corrupt file is a miss, never an error
    assert adapter_mod.parse_custom_id("pbq:abc:1") == ("abc", 1) and parse_custom_id


@pytest.mark.parametrize("body", ["[]", "null", "\"x\"", "7"])
def test_a_non_object_receipt_does_not_break_the_stale_reply(tmp_path, monkeypatch, body):
    state = tmp_path / "state"
    (state / "model-link-intakes").mkdir(parents=True)
    (state / "model-link-intakes" / "abc123.json").write_text(body)
    monkeypatch.setattr("print_handy.runtime._config.state_dir", lambda: state)
    assert prompt_ledger.saved_job_state("abc123") is None
