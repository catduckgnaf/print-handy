"""One job card per job on Discord (#1472).

The card session (print_handy.bot.job_card) wraps a card-capable adapter
for one intake: the first thing shown posts the card, questions attach
to it as buttons, progress edits its status line, a repeated summary or
warning replaces its earlier copy, and long detail can move to a thread
on the card. Adapters without card support are never wrapped.

The Discord half drives the real adapter against the corpus fake
Discord, including the safety rules: card buttons go through the same
handle_click as standalone prompts, so the allowed check, the approver
rule (P1-93) and the initiator rule (#1146) must hold identically.
"""
import asyncio
import uuid
from types import SimpleNamespace as S

import pytest

from corpus import fake_discord
from print_handy.bot import job_card, platform
from print_handy.bot.adapter import StandaloneAdapter as DiscordAdapter
from print_handy.core import prompts
from print_handy.bot.job_card import JobCard, JobCardSession
from print_handy.bot.platform import SendResult

OPERATOR = "200000000000000002"
MEMBER = "200000000000000777"
APPROVER = "200000000000000555"
CHAT = "100000000000000001"


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------- the card data

def test_card_embed_spec_carries_facts_and_state_color():
    card = JobCard("Ghost Stand")
    card.material = "White PLA · Slot 1"
    card.estimate = "about 1h 0m"
    card.state = "asking"
    spec = card.embed_spec()
    assert spec["title"] == "Ghost Stand"
    assert spec["color"] == job_card.STATE_COLORS["asking"]
    assert {"name": "Material", "value": "White PLA · Slot 1", "inline": True} in spec["fields"]
    assert {"name": "Estimate", "value": "about 1h 0m", "inline": True} in spec["fields"]
    assert spec["footer"] == "Waiting for an answer"
    card.state = "failed"
    assert card.embed_spec()["color"] == job_card.STATE_COLORS["failed"]
    card.state = "done"
    assert card.embed_spec()["color"] == job_card.STATE_COLORS["done"]


def test_card_thumbnail_spec_uses_attachment_name():
    card = JobCard("Ghost Stand")
    card.thumbnail_path = "/tmp/job/thumb.png"
    assert card.embed_spec()["thumbnail"] == "attachment://thumb.png"
    card.thumbnail_url = "https://example.org/t.png"
    assert card.embed_spec()["thumbnail"] == "https://example.org/t.png"


# ---------------------------------------------------------------------- a fake card adapter

class FakeCardAdapter:
    """The card primitives the session renders through, counting posts.

    A "post" is a new channel message: post_card, a fallback send or a
    fallback clarify. Edits (update_card, attach_*) never count.
    """

    name = "discord"
    supports_job_card = True
    supports_embeds = True

    def __init__(self, *, thread=False):
        self.job_card_thread = thread
        self.posts = 0
        self.posted_texts = []
        self.thread_posts = []
        self.thread_edits = []
        self.attached = []
        self.card = None

    async def post_card(self, chat_id, card, metadata=None):
        self.posts += 1
        self.card = card
        card.message_id = "card-1"
        return SendResult(True, message_id="card-1")

    async def update_card(self, card, view=None, clear_view=False, metadata=None):
        return SendResult(True, message_id=card.message_id)

    async def attach_card_prompt(self, chat_id, card, question, choices, clarify_id, session_key,
                                 metadata=None):
        self.attached.append({"question": question, "choices": list(choices), "cid": str(clarify_id)})
        return SendResult(True, message_id=card.message_id)

    async def attach_card_stop(self, card, text, clarify_id, metadata=None):
        self.attached.append({"question": text, "choices": ["Stop"], "cid": str(clarify_id)})
        return SendResult(True, message_id=card.message_id)

    async def open_card_thread(self, chat_id, card, metadata=None):
        return "thread-1"

    async def send(self, chat_id, content, reply_to=None, metadata=None, files=None):
        if str(chat_id) == "thread-1":
            self.thread_posts.append(content)
            return SendResult(True, message_id="t1")
        self.posts += 1
        self.posted_texts.append((str(chat_id), content))
        return SendResult(True, message_id=f"m{self.posts}")

    async def edit_message(self, chat_id, message_id, content, metadata=None):
        self.thread_edits.append((str(chat_id), str(message_id), content))
        return SendResult(True, message_id=str(message_id))

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        self.posts += 1
        self.posted_texts.append((str(chat_id), question))
        return SendResult(True, message_id=f"m{self.posts}")

    async def close_prompt(self, clarify_id, note):
        if self.card is not None:
            self.card.add_answered(self.card.question or "", note)


def session_for(adapter):
    source = S(chat_id=CHAT, thread_id=None, user_id=OPERATOR)
    return JobCardSession(adapter, source)


def test_session_posts_one_card_and_edits_it_for_progress():
    adapter = FakeCardAdapter()

    async def flow():
        session = session_for(adapter)
        await session.send(CHAT, "That looks like Ghost Stand, a stand. Filament: White PLA. "
                                 "The AD5X estimated print time about 1h 0m, plus ~5 min warmup.")
        await session.send(CHAT, "Checking whether it fits the bed")
        await session.send(CHAT, "Slicing")
        await session.send(CHAT, "Uploading to the printer")
        return session

    session = run(flow())
    assert adapter.posts == 1
    assert session.card.title == "Ghost Stand"
    assert session.card.material == "White PLA"
    assert session.card.estimate == "about 1h 0m"
    assert session.card.status == "Uploading to the printer"
    assert session.card.state == "working"


def test_repeated_summary_and_warning_replace_their_copy():
    adapter = FakeCardAdapter()
    warning = ("Sliced with supports, but the support toolpaths do not cover every flagged "
               "region near the base.")

    async def flow():
        session = session_for(adapter)
        await session.send(CHAT, "Ready to slice: Ghost Stand in White PLA, about 1h 0m.")
        await session.send(CHAT, warning)
        await session.send(CHAT, warning)  # the same warning shown again
        await session.send(CHAT, "Ready to slice: Ghost Stand in White PLA, about 1h 0m.")
        return session

    session = run(flow())
    assert adapter.posts == 1
    texts = list(session.card.notes.values())
    assert sum("support toolpaths do not cover" in t for t in texts) == 1
    assert sum(t.startswith("Ready to slice") for t in texts) == 1


def test_question_attaches_to_the_card_and_collapses_when_answered():
    adapter = FakeCardAdapter()

    async def flow():
        session = session_for(adapter)
        await session.send_clarify(CHAT, "Choose the color for Shell, Eyes",
                                    ["White PLA", "Black PLA", "Cancel"], "cid-color", "sess")
        assert session.card.state == "asking"
        assert session.card.question == "Choose the color for Shell, Eyes"
        await session.close_prompt("cid-color", "operator chose **White PLA**")
        return session

    session = run(flow())
    assert adapter.posts == 1  # the card only; the question added no message
    assert adapter.attached[0]["choices"] == ["White PLA", "Black PLA", "Cancel"]
    assert session.card.question is None
    assert any("White PLA" in line for line in session.card.answered)


def test_stop_prompt_rides_the_card_status_line():
    adapter = FakeCardAdapter()

    async def flow():
        session = session_for(adapter)
        result = await session.send_clarify(
            CHAT, "Waiting for the background slice to finish. Press Stop to cancel.",
            ["Stop"], "cid-stop", "sess")
        return session, result

    session, result = run(flow())
    assert result.success and result.message_id == "card-1"
    assert adapter.posts == 1
    assert session.card.status.startswith("Waiting for the background slice")


def test_thumbnail_image_becomes_the_card_thumbnail_not_a_message(tmp_path):
    thumb = tmp_path / "thumb.png"
    thumb.write_bytes(b"\x89PNG")
    adapter = FakeCardAdapter()

    async def flow():
        session = session_for(adapter)
        await session.send(CHAT, f"Source model preview (from the supplied 3MF).\nMEDIA:{thumb}")
        return session

    session = run(flow())
    assert adapter.posts == 1
    assert session.card.thumbnail_path == str(thumb)
    assert session.card.embed_spec()["thumbnail"] == "attachment://thumb.png"


def test_a_file_handoff_still_posts_as_its_own_message(tmp_path):
    gcode = tmp_path / "plate_1.gcode"
    gcode.write_text("G1 X1\n")
    adapter = FakeCardAdapter()

    async def flow():
        session = session_for(adapter)
        await session.send(CHAT, "That looks like Ghost Stand, a stand.")
        await session.send(CHAT, f"Here is the slice.\nMEDIA:{gcode}")
        return session

    run(flow())
    assert adapter.posts == 2  # the card plus the file message, as today


def test_thread_setting_moves_long_detail_to_a_thread_on_the_card():
    adapter = FakeCardAdapter(thread=True)
    detail = ("Support analysis: " + "the flagged region near the base is steep. " * 12 +
              "The support toolpaths do not cover every flagged region.")

    async def flow():
        session = session_for(adapter)
        await session.send(CHAT, "That looks like Ghost Stand, a stand.")
        await session.send(CHAT, detail)
        await session.send(CHAT, detail)  # shown again: edited in the thread, not reposted
        return session

    session = run(flow())
    assert adapter.thread_posts == [detail]
    assert len(adapter.thread_edits) == 1
    assert all("Support analysis" not in text for text in session.card.notes.values())
    assert adapter.posts == 1


def test_long_detail_stays_on_the_card_without_the_thread_setting():
    adapter = FakeCardAdapter(thread=False)
    detail = "Support analysis: " + "detail. " * 60

    async def flow():
        session = session_for(adapter)
        await session.send(CHAT, detail)
        return session

    session = run(flow())
    assert adapter.thread_posts == []
    assert any("Support analysis" in text for text in session.card.notes.values())


def test_print_started_fills_the_card_facts():
    adapter = FakeCardAdapter()
    started = ("Print started: Ghost Stand\nParts: Ghost Stand\nPlate 1\n"
               "Slot 1: white PLA\nEstimated total: ~1h 5m (printing: 1h 0m)")

    async def flow():
        session = session_for(adapter)
        await session.send(CHAT, started)
        return session

    session = run(flow())
    assert adapter.posts == 1
    assert session.card.title == "Ghost Stand"
    assert session.card.material == "white PLA"
    assert session.card.estimate.startswith("~1h 5m")
    assert session.card.state == "printing"


def test_adapters_without_card_support_are_never_wrapped():
    class PlainAdapter:
        name = "telegram"
        supports_job_card = False

    plain = PlainAdapter()
    assert JobCardSession.maybe(plain, S(chat_id="1", thread_id=None)) is plain

    class NoFlagAdapter:
        name = "matrix"

    bare = NoFlagAdapter()
    assert JobCardSession.maybe(bare, S(chat_id="1", thread_id=None)) is bare
    assert platform.BasePlatformAdapter.supports_job_card is False


# ---------------------------------------------------------------------- the real Discord adapter

def discord_env():
    return fake_discord.FakeDiscord(auto=False, chat_id=int(CHAT), operator_id=int(OPERATOR))


def _card_with_prompt(adapter, fake, *, approval=False, session_key=None, question="Start the print?",
                      choices=("Print it", "Cancel")):
    card = JobCard("Ghost Stand")
    fake.client.add_listener(adapter.handle_click, "on_interaction")

    async def flow():
        posted = await adapter.post_card(CHAT, card)
        assert posted.success
        cid = uuid.uuid4().hex
        prompts.register(cid, session_key or "sess", question, list(choices), approval=approval)
        if approval:
            prompts.mark_approval(cid)
        attached = await adapter.attach_card_prompt(CHAT, card, question, list(choices), cid,
                                                    session_key or "sess")
        assert attached.success
        return cid

    cid = run(flow())
    message = fake.messages[-1]
    return card, message, cid


def test_discord_card_posts_one_embed_message_with_thumbnail(tmp_path):
    fake = discord_env()
    adapter = DiscordAdapter(fake.client, [OPERATOR])
    thumb = tmp_path / "thumb.png"
    thumb.write_bytes(b"\x89PNG")
    card = JobCard("Ghost Stand")
    card.thumbnail_path = str(thumb)
    card.state = "slicing"
    card.status = "Slicing"
    result = run(adapter.post_card(CHAT, card))
    assert result.success
    assert len(fake.messages) == 1
    message = fake.messages[0]
    assert message.embed is not None
    assert message.embed.title == "Ghost Stand"
    assert message.embed.thumbnail.url == "attachment://thumb.png"
    assert message.files == ["thumb.png"]


def test_discord_card_button_press_collapses_the_question():
    fake = discord_env()
    adapter = DiscordAdapter(fake.client, [OPERATOR])
    card, message, cid = _card_with_prompt(adapter, fake)
    label, custom = next((t, c) for t, c in fake.prompt_buttons(message) if t == "Print it")
    interaction = run(fake.press(message, custom))
    assert not prompts.is_open(cid)
    assert card.question is None
    assert any("Print it" in line for line in card.answered)
    assert message.view is None
    assert message.embed is not None  # the card re-rendered, not a text post


def test_discord_card_approval_button_refuses_a_member_and_takes_the_approver():
    fake = discord_env()
    adapter = DiscordAdapter(fake.client, [], approver=APPROVER)
    card, message, cid = _card_with_prompt(adapter, fake, approval=True)
    _label, custom = next((t, c) for t, c in fake.prompt_buttons(message) if t == "Print it")
    member = fake_discord.FakeUser(int(MEMBER), "member")
    refused = run(fake.press(message, custom, user=member))
    assert refused.replies == [{"content": "Not allowed. Nothing changed.", "ephemeral": True}]
    assert prompts.is_open(cid)  # the refusal changed nothing
    approver = fake_discord.FakeUser(int(APPROVER), "approver")
    taken = run(fake.press(message, custom, user=approver))
    assert not prompts.is_open(cid)
    assert any("Print it" in line for line in card.answered)


def test_discord_card_prompt_belongs_to_its_initiator():
    fake = discord_env()
    adapter = DiscordAdapter(fake.client, [], approver=APPROVER)
    key = f"discord:{CHAT}:none:{OPERATOR}"
    card, message, cid = _card_with_prompt(adapter, fake, session_key=key,
                                           question="Choose the color", choices=("White", "Cancel"))
    _label, custom = next((t, c) for t, c in fake.prompt_buttons(message) if t == "White")
    other = fake_discord.FakeUser(int(MEMBER), "member")
    refused = run(fake.press(message, custom, user=other))
    assert refused.replies == [{"content": "That question was asked of someone else. "
                                            "Nothing changed.", "ephemeral": True}]
    assert prompts.is_open(cid)
    initiator = fake_discord.FakeUser(int(OPERATOR), "operator")
    taken = run(fake.press(message, custom, user=initiator))
    assert not prompts.is_open(cid)
    assert any("White" in line for line in card.answered)


def test_discord_card_typed_answer_collapses_through_close_prompt():
    fake = discord_env()
    adapter = DiscordAdapter(fake.client, [OPERATOR])
    card, message, cid = _card_with_prompt(adapter, fake, question="Choose the color",
                                           choices=("White", "Cancel"))
    run(adapter.close_prompt(cid, "Answered by message: White"))
    assert card.question is None
    assert any("Answered by message: White" in line for line in card.answered)
    assert message.view is None


def test_discord_card_thread_opens_on_the_card_message():
    fake = discord_env()
    adapter = DiscordAdapter(fake.client, [OPERATOR])
    card = JobCard("Ghost Stand")
    run(adapter.post_card(CHAT, card))
    thread_id = run(adapter.open_card_thread(CHAT, card))
    assert thread_id is not None
    assert fake.threads and str(fake.threads[0].id) == thread_id
    assert fake.threads[0].name == "Ghost Stand"


def test_discord_card_stop_button_retires_when_the_step_ends():
    """The card's Stop button is recorded like any prompt, so retire_prompt
    takes it off the card when the step ends (review of #1488: it stayed)."""
    fake = discord_env()
    adapter = DiscordAdapter(fake.client, [OPERATOR])
    card = JobCard("Ghost Stand")

    async def flow():
        assert (await adapter.post_card(CHAT, card)).success
        cid = uuid.uuid4().hex
        prompts.register(cid, "sess", "Slicing. Press Stop to cancel.", ["Stop"])
        assert (await adapter.attach_card_stop(card, "Slicing. Press Stop to cancel.", cid)).success
        message = fake.messages[-1]
        assert message.view is not None
        await adapter.retire_prompt(cid)
        return message

    message = run(flow())
    assert message.view is None
    assert message.embed is not None
    assert not card.answered  # a progress step's Stop is not an answered question
