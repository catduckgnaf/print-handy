"""One job card per job, end to end (#1472).

The scripted simple job from test_prepared_intake (one color, one copy,
"Slice and print") runs through the real prepare_link flow, but the
adapter is card-capable, so the intake wraps it in a JobCardSession.
Counting the adapter's channel posts from drop to "Print started" must
give at most 3 — in fact exactly 1: the card. Every question attaches
to the card, every progress phase edits it, and the start lands on it.

The same flow on the plain scripted adapter (no card support) is the
control: it is not wrapped and presents exactly as before.
"""
import asyncio
import json
from types import SimpleNamespace as S

from print_handy.bot.job_card import JobCardSession
from print_handy.bot.platform import SendResult
from print_handy.core import prepared_intake

from .test_color_intake import state
from .test_prepared_intake import Adapter, env  # noqa: F401  (pytest fixture)


class CardAdapter:
    """A card-capable scripted adapter: answers questions from picks,
    renders the card through the primitives the session calls, and
    counts every new channel message it is asked to post."""

    name = "discord"
    supports_job_card = True
    supports_embeds = True

    def __init__(self, picks):
        self._pick_iter = iter(picks)
        self.log = []          # the env fixture logs _with_stop texts here
        self.questions = []
        self.posts = 0         # new channel messages: the card + any fallback post
        self.card = None
        self.job_card_thread = False

    # ---------------------------------------------------------- card primitives

    async def post_card(self, chat_id, card, metadata=None):
        self.posts += 1
        self.card = card
        card.message_id = "card-1"
        return SendResult(True, message_id="card-1")

    async def update_card(self, card, view=None, clear_view=False, metadata=None):
        return SendResult(True, message_id=card.message_id)

    async def attach_card_prompt(self, chat_id, card, question, choices, clarify_id, session_key,
                                 metadata=None):
        self.questions.append({"question": question, "choices": list(choices)})
        if list(choices) == ["Stop"]:
            return SendResult(True, message_id=card.message_id)  # never pressed in a script
        answer = choices[next(self._pick_iter)]
        import tools.clarify_gateway as cm
        cm.resolve_gateway_clarify(clarify_id, answer)
        return SendResult(True, message_id=card.message_id)

    async def attach_card_stop(self, card, text, clarify_id, metadata=None):
        return SendResult(True, message_id=card.message_id)

    async def open_card_thread(self, chat_id, card, metadata=None):
        return None

    # ---------------------------------------------------------- plain delivery (fallbacks)

    async def send(self, chat_id, content, reply_to=None, metadata=None, files=None):
        self.posts += 1
        return SendResult(True, message_id=f"m{self.posts}")

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        self.posts += 1
        return SendResult(True, message_id=f"m{self.posts}")

    async def edit_message(self, chat_id, message_id, content, metadata=None):
        return SendResult(True, message_id=str(message_id))

    async def close_prompt(self, clarify_id, note):
        return None


def _run_card_flow(env, picks):
    event = S(source=S(profile="3dprinting", chat_id="c1", thread_id=None, user_id="u"),
              message_id="m1", text="https://www.printables.com/model/1-ghost")
    adapter = CardAdapter(picks)
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    receipt = json.loads(next(env.root.glob("*.json")).read_text())
    return adapter, result, receipt


def test_simple_job_posts_at_most_three_messages_to_print_started(env):
    # picks: one color for everything (2 on the #1473 offer), slot 1, then "Slice and print now (bed is clear)"
    adapter, result, receipt = _run_card_flow(env, [2, 0, 0])
    assert receipt["status"] == "STARTED" and receipt["started"] is True
    assert len(env.starts) == 1
    # The whole intake — three questions, the fit/slice/upload progress,
    # the start — is the one card, edited in place.
    assert adapter.posts == 1
    assert adapter.posts <= 3
    assert len(adapter.questions) == 3
    card = adapter.card
    assert card is not None
    assert card.title == "Ghost Stand"
    assert card.state == "printing"
    assert card.status.startswith("Print started")


def test_the_same_flow_on_a_plain_adapter_is_not_wrapped(env):
    event = S(source=S(profile="3dprinting", chat_id="c1", thread_id=None, user_id="u"),
              message_id="m1", text="https://www.printables.com/model/1-ghost")
    adapter = Adapter([2, 0, 0])
    assert JobCardSession.maybe(adapter, event.source) is adapter
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    receipt = json.loads(next(env.root.glob("*.json")).read_text())
    assert receipt["status"] == "STARTED"
    # Unchanged presentation: questions and the start post are separate
    # messages, exactly as before #1472.
    assert len(adapter.questions) == 3
    assert any(str(text).startswith("Print started") for text in adapter.sent)
