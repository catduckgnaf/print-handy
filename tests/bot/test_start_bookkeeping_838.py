"""#838 item 8: a launch lease left by a hard kill gets one startup notice
naming the file and the reconcile command. Offline: fake Discord, isolated
launch database (the root conftest points PRINT_HANDY_LAUNCH_DB at tmp).
"""
import asyncio

from corpus import fake_discord
from print_handy.bot import runtime
from print_handy.core import launch_state

OPERATOR = "200000000000000002"


def run(coro):
    return asyncio.run(coro)


def make(cfg):
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(OPERATOR))
    return discord, runtime.BotCore(discord.client, cfg)


def test_pending_launch_posts_one_notice_naming_file_and_command(cfg):
    discord, core = make(cfg)
    lease = launch_state.acquire("printer-a", remote="widget_abc123.gcode")
    lease.submitting()  # the hard-kill window: submitted, never settled
    try:
        run(core.announce_pending_launch())
        run(core.announce_pending_launch())  # a second startup pass in one process stays at one post
    finally:
        lease.release()
        launch_state.reset_submitted()  # submitting() sets a process global; do not leak it
    notices = [text for text in discord.sent if "widget_abc123.gcode" in text]
    assert len(notices) == 1
    assert "reconcile-launch --not-started" in notices[0]


def test_no_pending_launch_posts_nothing(cfg):
    discord, core = make(cfg)
    run(core.announce_pending_launch())
    assert discord.sent == []
