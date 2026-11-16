"""Tranche 4: inventory answers live in the real pipeline (#1524).

An unmentioned plain-language inventory question gets the deterministic
Spoolman aggregate — no model call, no writes — with unknown weights
kept unknown and Spoolman failures answered as unavailable.
"""
import asyncio

import pytest

from corpus import fake_discord
from print_handy.bot import natural_language as nl, runtime

RAW = [
    {"id": 1, "filament": {"name": "Purple", "material": "PLA",
                           "vendor": {"name": "Polymaker"}}, "remaining_weight": 800},
    {"id": 2, "filament": {"name": "Purple", "material": "PLA",
                           "vendor": {"name": "Generic"}}, "remaining_weight": 450},
    {"id": 3, "filament": {"name": "Purple", "material": "PLA",
                           "vendor": {"name": "Generic"}}, "remaining_weight": None},
    {"id": 7, "filament": {"name": "Black", "material": "PLA",
                           "vendor": {"name": "Generic"}}, "remaining_weight": 620,
     "extra": {"ph_slot": "ad5x:2"}},
]
RECORDS = [nl.normalize_spool(r, {7}) for r in RAW]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def core(cfg, monkeypatch):
    monkeypatch.setattr(nl, "_fetch_spool_records", lambda c: (RECORDS, {7}))
    discord = fake_discord.FakeDiscord(
        auto=False, chat_id=int(cfg["discord"]["chat_id"]),
        operator_id=int(cfg["discord"]["allowed_user_ids"]))

    def forbidden_opener(req, timeout=None):  # pragma: no cover - must never run
        raise AssertionError("the conversational lane was called for a plain question")

    return discord, runtime.BotCore(discord.client, cfg, llm_opener=forbidden_opener)


def test_unmentioned_inventory_question_gets_the_verified_aggregate(core):
    discord, bot = core
    run(bot.handle_message(discord.message("How much purple PLA do I have?")))
    assert any("1250 g" in text and "incomplete" in text for text in discord.sent), discord.sent


def test_slot_question_resolves_the_loaded_spool(core):
    discord, bot = core
    run(bot.handle_message(discord.message("What's in slot 2?")))
    assert any("#7" in text and "620 g" in text for text in discord.sent), discord.sent


def test_spool_detail_has_only_real_fields(core):
    discord, bot = core
    run(bot.handle_message(discord.message("Tell me about spool #7")))
    text = next(t for t in discord.sent if "#7" in t)
    assert "620 g" in text and "slot 2" in text
    assert "$" not in text and "bought" not in text


def test_spoolman_failure_says_unavailable(core, monkeypatch):
    discord, bot = core

    def boom(c):
        raise RuntimeError("spoolman down")
    monkeypatch.setattr(nl, "_fetch_spool_records", boom)
    run(bot.handle_message(discord.message("How much purple PLA do I have?")))
    assert any("unavailable" in text for text in discord.sent), discord.sent


def test_followup_narrows_without_a_new_question(core):
    discord, bot = core
    run(bot.handle_message(discord.message("How much purple PLA do I have?")))
    run(bot.handle_message(discord.message("Only Polymaker")))
    assert any("800 g" in text for text in discord.sent[1:]), discord.sent
