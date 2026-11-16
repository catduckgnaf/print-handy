"""Discord job posts are plain messages again (catduck, 2026-11-16).

2.3.4 (#1472) drew each Discord job as one embed "card" with a colored
bar, edited in place. catduck asked for it back out: a job posts its
questions, progress and start as ordinary messages on every platform, so
Discord, Telegram, Matrix and web chat read the same way.

The flow test runs the real ``prepare_link`` intake with an adapter that
still offers the old card primitives: nothing may call them.
"""
import asyncio
import importlib.util
import json
from types import SimpleNamespace as S

from print_handy import settings
from print_handy.bot import platform
from print_handy.bot.adapter import StandaloneAdapter as DiscordAdapter
from print_handy.core import prepared_intake

from .test_color_intake import state
from .test_prepared_intake import Adapter, env  # noqa: F401  (pytest fixture)


class AdapterOfferingCards(Adapter):
    """The scripted adapter, plus the card hooks 2.3.4 used. Any call fails the test."""

    name = "discord"
    supports_job_card = True
    supports_embeds = True

    def __init__(self, picks):
        super().__init__(picks)
        self.card_calls = []

    def __getattr__(self, name):
        if name in {"post_card", "update_card", "attach_card_prompt", "attach_card_stop",
                    "open_card_thread"}:
            async def record(*args, **kwargs):
                self.card_calls.append(name)
                raise AssertionError(f"the intake drew a job card ({name})")
            return record
        raise AttributeError(name)


def test_the_intake_posts_plain_messages_and_never_draws_a_card(env):
    event = S(source=S(profile="3dprinting", chat_id="c1", thread_id=None, user_id="u"),
              message_id="m1", text="https://www.printables.com/model/1-ghost")
    adapter = AdapterOfferingCards([2, 0, 0])
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    receipt = json.loads(next(env.root.glob("*.json")).read_text())
    assert receipt["status"] == "STARTED"
    assert adapter.card_calls == []
    # Each question is its own message, and the start is its own post.
    assert len(adapter.questions) >= 3
    assert any(str(text).startswith("Print started") for text in adapter.sent)


def test_the_discord_adapter_has_no_card_rendering():
    assert importlib.util.find_spec("print_handy.bot.job_card") is None
    for name in ("post_card", "update_card", "attach_card_prompt", "attach_card_stop",
                 "open_card_thread", "_card_embed"):
        assert not hasattr(DiscordAdapter, name), name
    assert not getattr(DiscordAdapter, "supports_job_card", False)
    assert not getattr(platform.BasePlatformAdapter, "supports_job_card", False)


def test_job_card_thread_is_not_a_setting_any_more():
    assert ("discord", "job_card_thread") not in settings.OPTIONAL_KEYS
