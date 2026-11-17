"""Tranche 5: firmware answers keep the seven outcome states honest (#1524).

A published release never proves a printer is behind, an unreadable
version state is "unknown" — never "up to date" — and applying an
update stays on the existing guarded flow.
"""
import asyncio

import pytest

from corpus import fake_discord
from print_handy import firmware_update
from print_handy.bot import natural_language as nl, runtime
from print_handy.runtime import spool_sync


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def discord_core(cfg):
    discord = fake_discord.FakeDiscord(
        auto=False, chat_id=int(cfg["discord"]["chat_id"]),
        operator_id=int(cfg["discord"]["allowed_user_ids"]))
    return discord, runtime.BotCore(discord.client, cfg)


def _parsed(text):
    return nl.parse(text, nl.build_vocabulary())


def _moonraker(components):
    """A Moonraker transport in the real shape: callable, full response."""
    def call(method, path, body=None, timeout=None):
        if path.startswith("/machine/update/status"):
            return {"result": {"busy": False, "version_info": components}}
        if path.startswith("/machine/system_info"):
            return {"result": {"system_info": {}}}
        raise AssertionError(f"unexpected {method} {path}")
    return call


def _offline(method, path, body=None, timeout=None):
    raise OSError("printer unreachable")


def _zmod(installed, remote):
    return {"zmod": {"version": installed, "remote_version": remote,
                     "configured_type": "git_repo", "is_valid": True}}


def test_unreadable_state_is_unknown_never_current(discord_core, monkeypatch):
    monkeypatch.setattr(spool_sync, "moonraker_for", lambda printer_id=None: _offline)
    reply = nl.firmware_answer(discord_core[1], _parsed("Are there ZMod updates?"))
    assert "unknown" in reply and "up to date" not in reply and "current" not in reply


def test_confirmed_update_available(discord_core, monkeypatch):
    monkeypatch.setattr(spool_sync, "moonraker_for",
                        lambda printer_id=None: _moonraker(_zmod("1.7.5", "1.7.6")))
    reply = nl.firmware_answer(discord_core[1], _parsed("Are there ZMod updates?"))
    assert "confirmed available" in reply and "1.7.5" in reply and "1.7.6" in reply


def test_verified_current(discord_core, monkeypatch):
    monkeypatch.setattr(spool_sync, "moonraker_for",
                        lambda printer_id=None: _moonraker(_zmod("1.7.6", "1.7.6")))
    reply = nl.firmware_answer(discord_core[1], _parsed("Is ZMod current?"))
    assert "verified current" in reply


def test_unreadable_remote_is_unknown_not_current(discord_core, monkeypatch):
    # Moonraker reports "?" when it cannot read the remote: that is
    # unknown, never "verified current".
    monkeypatch.setattr(spool_sync, "moonraker_for",
                        lambda printer_id=None: _moonraker(_zmod("1.7.6", "?")))
    reply = nl.firmware_answer(discord_core[1], _parsed("Is ZMod current?"))
    assert "unknown" in reply and "verified current" not in reply


def test_what_firmware_am_i_on_lists_installed_versions(discord_core, monkeypatch):
    components = dict(_zmod("1.7.6-17", "1.7.6-36"))
    components["helixscreen"] = {"version": "v1.0.8", "remote_version": "v1.0.9",
                                 "configured_type": "web", "is_valid": True}
    monkeypatch.setattr(spool_sync, "moonraker_for", lambda printer_id=None: _moonraker(components))
    reply = nl.firmware_answer(discord_core[1], _parsed("What firmware am I on?"))
    assert "1.7.6-17" in reply and "v1.0.8" in reply
    assert "unknown" not in reply


def test_what_about_helixscreen_follows_a_firmware_question(discord_core, monkeypatch):
    components = {"helixscreen": {"version": "v1.0.8", "remote_version": "v1.0.9",
                                  "configured_type": "web", "is_valid": True}}
    monkeypatch.setattr(spool_sync, "moonraker_for", lambda printer_id=None: _moonraker(components))
    nl.CONTEXT.clear()
    nl.parse_with_context("Are there ZMod updates?", "k", nl.build_vocabulary())
    parsed = nl.parse_with_context("What about HelixScreen?", "k", nl.build_vocabulary())
    assert parsed.capability_id == "firmware_updates"
    reply = nl.firmware_answer(discord_core[1], parsed)
    assert "v1.0.8" in reply and "v1.0.9" in reply


def test_feed_only_family_never_claims_comparison():
    # OpenCreator / Forge-X: a published release is release_unverified,
    # never "update available" and never "current".
    state = nl.classify_firmware_state("opencreator", feed_latest="0.0.8")
    assert state == "release_unverified"
    text = nl.firmware_state_text("OpenCreator", "opencreator", state)
    assert "not known whether this printer needs it" in text
    assert nl.FIRMWARE_FAMILIES["forge-x"]["applies"] is False
    assert nl.FIRMWARE_FAMILIES["bambu"]["applies"] is False


def test_pipeline_question_gets_the_state_answer(discord_core, monkeypatch):
    discord, bot = discord_core
    monkeypatch.setattr(spool_sync, "moonraker_for", lambda printer_id=None: _offline)
    run(bot.handle_message(discord.message("Are there ZMod updates?")))
    assert any("unknown" in text for text in discord.sent), discord.sent


def test_update_zmod_is_not_executed_by_the_nl_layer(discord_core):
    discord, bot = discord_core
    parsed = _parsed("Update ZMod")
    assert parsed.allowed_to_mutate()  # the guarded flow may run it...
    assert nl.answer(bot, _event("Update ZMod"), {}) is None  # ...but never this layer


def _event(text):
    from types import SimpleNamespace
    return SimpleNamespace(
        text=text, source={"platform": "discord", "user_id": "u1", "channel_id": "c1"})
