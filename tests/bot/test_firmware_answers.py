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


def test_unreadable_state_is_unknown_never_current(discord_core, monkeypatch):
    monkeypatch.setattr(spool_sync, "moonraker_for", lambda state=None: None)
    reply = nl.firmware_answer(discord_core[1], _parsed("Are there ZMod updates?"))
    assert "unknown" in reply and "up to date" not in reply and "current" not in reply


def test_confirmed_update_available(discord_core, monkeypatch):
    monkeypatch.setattr(spool_sync, "load_state", lambda *a, **k: {})
    monkeypatch.setattr(spool_sync, "moonraker_for",
                        lambda state=None: ("http://printer", "key", None))
    monkeypatch.setattr(firmware_update, "firmware_report", lambda *a, **k: {
        "components": [{"name": "zmod", "installed_version": "1.7.5",
                        "available_version": "1.7.6", "status": "idle"}]})
    reply = nl.firmware_answer(discord_core[1], _parsed("Are there ZMod updates?"))
    assert "confirmed available" in reply and "1.7.5" in reply and "1.7.6" in reply


def test_verified_current(discord_core, monkeypatch):
    monkeypatch.setattr(spool_sync, "load_state", lambda *a, **k: {})
    monkeypatch.setattr(spool_sync, "moonraker_for",
                        lambda state=None: ("http://printer", "key", None))
    monkeypatch.setattr(firmware_update, "firmware_report", lambda *a, **k: {
        "components": [{"name": "zmod", "installed_version": "1.7.6",
                        "available_version": "1.7.6", "status": "idle"}]})
    reply = nl.firmware_answer(discord_core[1], _parsed("Is ZMod current?"))
    assert "verified current" in reply


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
    monkeypatch.setattr(spool_sync, "moonraker_for", lambda state=None: None)
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
