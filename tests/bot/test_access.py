"""Discord access (#386): the channel is the default boundary, lists are opt-in, refusals are never silent.

Every acceptance checkbox of the issue has a test here or in test_runtime.py
(the stranger-notice contract). The cfg fixture (tests/bot/conftest.py) is a
standalone config with one whitelisted operator; each test restates the
access section it exercises.
"""
import asyncio
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import pytest

from corpus import fake_discord
from print_handy import settings
from print_handy.bot import runtime
from print_handy.bot.adapter import StandaloneAdapter
from print_handy.live_activity import allowlist_from_settings

OPERATOR = "200000000000000002"
MEMBER = "200000000000000777"    # a second household member, on no list
APPROVER = "200000000000000555"


def run(coro):
    return asyncio.run(coro)


def make(cfg):
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(OPERATOR))
    return discord, runtime.BotCore(discord.client, cfg)


def member_message(discord, text):
    return discord.message(text, author=fake_discord.FakeUser(int(MEMBER), "member"))


# ---------------------------------------------------------------------- the default: channel mode

def test_empty_allow_list_lets_any_channel_member_start_intake(cfg):
    cfg["discord"]["allowed_user_ids"] = ""
    discord, core = make(cfg)
    seen = []

    async def fake_prepare(event, adapter, state, root, reuse=None, **kwargs):
        seen.append(event.text)
        return {"action": "skip", "reason": "test intake"}

    url = "https://makerworld.com/en/models/931302-holder"
    with mock.patch("print_handy.core.prepared_intake.prepare_link", fake_prepare), \
            mock.patch("print_handy.core.printer_state.load", return_value={}):
        run(core.handle_message(member_message(discord, url)))
    assert seen == [url]
    assert core.adapter.access_description() == "channel (everyone)"
    assert core.status(True)["access"] == "channel (everyone)"


def test_empty_allow_list_lets_any_channel_member_use_commands(cfg):
    cfg["discord"]["allowed_user_ids"] = ""
    discord, core = make(cfg)
    out = run(core.handle_message(member_message(discord, "/print-help")))
    assert out == {"action": "skip", "reason": "help"}
    assert discord.sent and discord.sent[-1].startswith("**Print Handy**")


def test_setting_only_an_approval_user_does_not_create_a_whitelist(cfg):
    """The approver is unioned into the allowed set for action, but the mode
    latches on the raw list: approval-only config stays channel mode."""
    cfg["discord"].update(allowed_user_ids="", approval_user_id=APPROVER)
    discord, core = make(cfg)
    assert core.adapter.channel_open is True
    assert core.adapter.access_description() == "channel (everyone)"
    assert core.adapter.is_allowed_id(APPROVER)  # the approver can always act
    out = run(core.handle_message(member_message(discord, "/print-help")))
    assert out == {"action": "skip", "reason": "help"}


# ---------------------------------------------------------------------- opt-in lists

def test_whitelist_refuses_a_non_listed_user_with_notice_log_and_no_intake(cfg, caplog):
    cfg["discord"]["allowed_user_ids"] = OPERATOR
    discord, core = make(cfg)
    seen = []

    async def fake_prepare(event, adapter, state, root, reuse=None, **kwargs):
        seen.append(event.text)
        return {"action": "skip", "reason": "test intake"}

    with caplog.at_level(logging.INFO, logger="print_handy.bot.runtime"), \
            mock.patch("print_handy.core.prepared_intake.prepare_link", fake_prepare), \
            mock.patch("print_handy.core.printer_state.load", return_value={}):
        out = run(core.handle_message(member_message(
            discord, "https://makerworld.com/en/models/931302-holder")))
    assert out is None and seen == []  # no intake
    assert discord.sent == [runtime.REFUSAL_NOTICE]  # exactly one in-channel notice
    assert f"ignoring message from discord user {MEMBER}" in caplog.text
    assert "user is not on the allowed list" in caplog.text
    assert core.adapter.access_description() == "whitelist (1 user)"


def test_denied_user_is_refused_in_channel_mode_while_everyone_else_works(cfg, caplog):
    cfg["discord"].update(allowed_user_ids="", denied_user_ids=MEMBER)
    discord, core = make(cfg)
    with caplog.at_level(logging.INFO, logger="print_handy.bot.runtime"):
        assert run(core.handle_message(member_message(discord, "/print-help"))) is None
    assert discord.sent == [runtime.REFUSAL_NOTICE]
    assert "user is on the denied list" in caplog.text
    out = run(core.handle_message(discord.message("/print-help")))  # the operator still works
    assert out == {"action": "skip", "reason": "help"}
    assert core.adapter.access_description() == "channel (everyone), 1 denied"


def test_denied_beats_the_whitelist_and_the_approver(cfg):
    cfg["discord"].update(allowed_user_ids=OPERATOR, approval_user_id=APPROVER,
                          denied_user_ids=f"{OPERATOR}, {APPROVER}")
    _, core = make(cfg)
    assert not core.adapter.is_allowed_id(OPERATOR)
    assert not core.adapter.is_allowed_id(APPROVER)
    assert core.adapter.refusal_reason(OPERATOR) == "user is on the denied list"


def test_refusal_notice_repeats_only_after_the_period(cfg):
    cfg["discord"]["allowed_user_ids"] = OPERATOR
    discord, core = make(cfg)
    run(core.handle_message(member_message(discord, "/print-help")))
    run(core.handle_message(member_message(discord, "/print-help")))
    assert discord.sent == [runtime.REFUSAL_NOTICE]
    core._refusal_notices[MEMBER] -= runtime.REFUSAL_NOTICE_PERIOD_S + 1
    run(core.handle_message(member_message(discord, "/print-help")))
    assert discord.sent == [runtime.REFUSAL_NOTICE, runtime.REFUSAL_NOTICE]


# ---------------------------------------------------------------------- approvals stay separate

def test_approver_not_on_the_whitelist_can_still_act(cfg):
    """allowed_users() has always unioned the approver in; that survives."""
    cfg["discord"].update(allowed_user_ids=OPERATOR, approval_user_id=APPROVER)
    discord, core = make(cfg)
    approver = fake_discord.FakeUser(int(APPROVER), "approver")
    out = run(core.handle_message(discord.message("/print-help", author=approver)))
    assert out == {"action": "skip", "reason": "help"}


def test_channel_mode_does_not_widen_the_live_activity_allowlist(cfg):
    """The live camera's trust set reads approval + allowed straight from
    settings: widening intake to the channel must not widen it (#386)."""
    data = {"discord": {"allowed_user_ids": "", "approval_user_id": APPROVER,
                        "denied_user_ids": MEMBER}}
    assert allowlist_from_settings(data) == (APPROVER,)


# ---------------------------------------------------------------------- startup log + duplicate token

def test_startup_log_states_the_access_mode(cfg, caplog):
    from print_handy.bot import client

    async def ready(bot):
        with mock.patch.object(bot.tree, "sync", mock.AsyncMock(return_value=[])):
            await bot.setup_hook()
        try:
            await bot.on_ready()
        finally:
            for task in bot._background:
                task.cancel()

    cfg["discord"]["allowed_user_ids"] = ""
    with caplog.at_level(logging.INFO, logger="print_handy.bot.client"):
        run(ready(client.PrintHandyBot(cfg)))
    assert "access: channel (everyone)" in caplog.text

    caplog.clear()
    cfg["discord"]["allowed_user_ids"] = OPERATOR
    with caplog.at_level(logging.INFO, logger="print_handy.bot.client"):
        run(ready(client.PrintHandyBot(cfg)))
    assert "access: whitelist (1 user)" in caplog.text


def test_a_second_ready_on_a_live_session_warns_about_a_duplicate_token(cfg, caplog):
    from print_handy.bot import client

    def ours():
        # Only this module's warnings: discord.py logs a one-time "PyNaCl is not installed" warning on the
        # first client a process builds, which lands here when this test runs first (a CI shard).
        return [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "print_handy.bot.client"]

    async def flow(bot):
        with mock.patch.object(bot.tree, "sync", mock.AsyncMock(return_value=[])):
            await bot.setup_hook()
        try:
            await bot.on_ready()
            await bot.on_ready()  # READY again, no disconnect in between
            warned = ours()
            await bot.on_disconnect()
            caplog.clear()
            await bot.on_ready()  # an ordinary reconnect: no warning
            assert not ours()
            return warned
        finally:
            for task in bot._background:
                task.cancel()

    with caplog.at_level(logging.WARNING, logger="print_handy.bot.client"):
        warned = run(flow(client.PrintHandyBot(cfg)))
    assert warned and "no two instances serve the same print channel" in warned[0].getMessage()


# ---------------------------------------------------------------------- settings surface

def test_denied_user_ids_come_from_the_environment_and_the_file():
    data = settings.load(environ={"PRINT_HANDY__DISCORD__DENIED_USER_IDS": "123, 456"})
    assert runtime.denied_users(data) == ["123", "456"]
    with pytest.raises(settings.SettingsError, match="denied_user_ids must be numeric"):
        settings.load(environ={"PRINT_HANDY__DISCORD__DENIED_USER_IDS": "me"})


def test_unraid_template_ships_no_access_lists():
    """The slimmed template carries no Discord fields: the allow/deny
    lists are config-file / environment settings (pinned by
    test_denied_user_ids_come_from_the_environment_and_the_file above),
    defaulting to empty — everyone in the channel, nobody denied."""
    root = ET.parse(Path(__file__).parents[2] / "docker" / "unraid" / "print-handy.xml").getroot()
    by_target = {c.get("Target"): c for c in root.iter("Config")}
    assert "PRINT_HANDY__DISCORD__ALLOWED_USER_IDS" not in by_target
    assert "PRINT_HANDY__DISCORD__DENIED_USER_IDS" not in by_target


# ---------------------------------------------------------------------- Hermes mode semantics

def test_gateway_authorizes_channel_participants_under_the_same_policy(cfg):
    """The plugin handlers gate on gateway._is_user_authorized_for_source in
    both modes; with the Discord policy in channel mode a participant who is
    on no list is authorized, and a denied one is not."""
    cfg["discord"].update(allowed_user_ids="", denied_user_ids=MEMBER)
    _, core = make(cfg)
    member = runtime.make_source(MEMBER, cfg["discord"]["chat_id"])
    operator = runtime.make_source(OPERATOR, cfg["discord"]["chat_id"])
    denied_gateway = runtime.Gateway(core.adapter, core.chat_id)
    assert not denied_gateway._is_user_authorized_for_source(member)
    cfg["discord"]["denied_user_ids"] = ""
    _, core2 = make(cfg)
    gateway = runtime.Gateway(core2.adapter, core2.chat_id)
    assert gateway._is_user_authorized_for_source(member)
    assert gateway._is_user_authorized_for_source(operator)


def test_adapter_policy_directly():
    adapter = StandaloneAdapter(None, [])
    assert adapter.channel_open and adapter.is_allowed_id("42")
    assert adapter.access_description() == "channel (everyone)"
    listed = StandaloneAdapter(None, ["42"], approver="7")
    assert not listed.channel_open and listed.is_allowed_id("7") and not listed.is_allowed_id("9")
    assert listed.access_description() == "whitelist (2 users)"
