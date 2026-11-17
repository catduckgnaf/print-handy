"""#1524 audit: checklist phrases checked against real-shaped data.

Each test here failed on main before the audit fix. The first two use
the shapes a live Spoolman and the live roster actually return; the
original tranche tests used hand-built shapes that hid both bugs.
"""
import asyncio

import pytest

from corpus import fake_discord
from print_handy.bot import natural_language as nl, runtime
from print_handy.bot.handlers import loaded_spools
from print_handy.core import edit_plan

# Spoolman returns extra-field values JSON-encoded: the quotes are part
# of the string (the live AD5X spools read '"flashforge-ad5x:3"').
LIVE_SHAPED = [
    {"id": 16, "remaining_weight": 1000.0, "location": "IFS Slot 1",
     "filament": {"name": "Matte White", "material": "PLA", "color_hex": "FFFFFF"},
     "extra": {"ph_slot": '"flashforge-ad5x:1"'}},
    {"id": 21, "remaining_weight": 787.1, "location": "IFS Slot 3",
     "filament": {"name": "Black", "material": "PLA+", "color_hex": "161616",
                  "vendor": {"name": "SUNLU"}},
     "extra": {"ph_slot": '"flashforge-ad5x:3"'}},
    {"id": 5, "remaining_weight": 98.0, "location": "Shelf",
     "filament": {"name": "Olive Green", "material": "PLA+", "color_hex": "708238"}},
    {"id": 1, "remaining_weight": 1000.0, "location": "Shelf",
     "filament": {"name": "Natural", "material": "PLA+", "color_hex": "F5F0E1"}},
]


def test_json_encoded_ph_slot_marks_the_spool_loaded_in_its_slot():
    records = [nl.normalize_spool(raw, set()) for raw in LIVE_SHAPED]
    by_id = {rec["id"]: rec for rec in records}
    assert by_id[21]["loaded"] and by_id[21]["slot"] == 3
    assert by_id[21]["slot_printer"] == "flashforge-ad5x"
    assert by_id[16]["slot"] == 1
    assert not by_id[5]["loaded"] and by_id[5]["slot"] is None


def test_shelf_answer_leaves_out_the_loaded_spools():
    records = [nl.normalize_spool(raw, set()) for raw in LIVE_SHAPED]
    parsed = nl.parse("Which spools are on the shelf?", nl.build_vocabulary(records))
    text = nl.inventory_answer_text(records, parsed.slots, capability_id=parsed.capability_id)
    assert "#5" in text and "#1" in text
    assert "#21" not in text and "#16" not in text


def test_misspelled_color_is_named_not_silently_dropped():
    records = [nl.normalize_spool(raw, set()) for raw in LIVE_SHAPED]
    vocab = nl.build_vocabulary(records)
    parsed = nl.parse("how much purpel pla+ do i have", vocab)
    text = nl.inventory_answer_text(records, parsed.slots)
    assert "purpel" in text and "not used as a filter" in text


def test_known_words_are_not_flagged_unrecognized():
    records = [nl.normalize_spool(raw, set()) for raw in LIVE_SHAPED]
    vocab = nl.build_vocabulary(records)
    for phrase in ("How much olive green PLA+ do I have?", "How much filament do I have?",
                   "How much sunlu pla+ do i have", "How much green PLA+ do I have?"):
        assert "unrecognized" not in nl.parse(phrase, vocab).slots, phrase


def test_what_about_with_no_filter_word_does_not_repeat_the_old_total():
    vocab = nl.build_vocabulary([nl.normalize_spool(raw, set()) for raw in LIVE_SHAPED])
    nl.CONTEXT.clear()
    nl.parse_with_context("How much PLA+ do I have?", "k", vocab)
    unknown = nl.parse_with_context("What about zzz?", "k", vocab)
    assert unknown.slots.get("unrecognized") == "zzz"
    follow = nl.parse_with_context("What about black?", "k", vocab)
    assert follow.capability_id == "inventory_total"
    assert follow.slots["color"] == "black" and follow.slots["material"] == "pla+"


@pytest.mark.parametrize("text,command", [
    ("Pause my print", "/print-pause"),          # the checklist's own phrase
    ("Stop this job", "/print-stop"),
    ("Resume my print", "/print-resume"),
    ("cancel my print job", "/print-stop"),
])
def test_my_print_and_this_job_reach_the_print_commands(text, command):
    assert runtime.command_suggestion(text) == command


@pytest.mark.parametrize("text", ["Cancel my meeting", "Don't stop the print",
                                  "Pause isn't working", "My printer isn't printing"])
def test_control_negatives_stay_unmatched(text):
    assert runtime.command_suggestion(text) is None


@pytest.mark.parametrize("text", ["Add a spool", "Add a roll", "change the spool",
                                  "add new filament"])
def test_spool_requests_are_never_model_edits(text):
    assert edit_plan.resolve_request(text) is None
    assert not edit_plan.looks_like_edit(text)


@pytest.mark.parametrize("text,plan_kind", [("add supports", "supports"), ("add a brim", "brim"),
                                            ("rotate 90 on z", "rotate")])
def test_model_edits_still_resolve(text, plan_kind):
    assert edit_plan.resolve_request(text)[1]["kind"] == plan_kind


def test_load_a_spool_is_not_offered_the_loaded_view():
    assert not loaded_spools._loaded_spools_fuzzy("Load a spool")
    assert not loaded_spools._loaded_spools_fuzzy("load the spool")
    assert loaded_spools._loaded_spools_fuzzy("what colours are loded")


@pytest.fixture
def discord_core(cfg):
    discord = fake_discord.FakeDiscord(
        auto=False, chat_id=int(cfg["discord"]["chat_id"]),
        operator_id=int(cfg["discord"]["allowed_user_ids"]))
    return discord, runtime.BotCore(discord.client, cfg)


def test_whats_in_slot_answers_from_live_shaped_spoolman(discord_core, monkeypatch):
    from print_handy.runtime import spool_sync
    discord, bot = discord_core
    monkeypatch.setattr(spool_sync, "spoolman", lambda *a, **k: LIVE_SHAPED)
    monkeypatch.setattr(spool_sync, "load_state", lambda *a, **k: {})
    records, _loaded = nl._fetch_spool_records(bot)
    slot3 = [rec for rec in records if rec["slot"] == 3]
    assert [rec["id"] for rec in slot3] == [21]


def test_what_about_an_unknown_word_says_so_through_the_bot(discord_core, monkeypatch):
    from print_handy.runtime import spool_sync
    discord, bot = discord_core
    monkeypatch.setattr(spool_sync, "spoolman", lambda *a, **k: LIVE_SHAPED)
    monkeypatch.setattr(spool_sync, "load_state", lambda *a, **k: {})
    nl.CONTEXT.clear()
    asyncio.run(bot.handle_message(discord.message("How much PLA+ do I have?")))
    first = len(discord.sent)
    asyncio.run(bot.handle_message(discord.message("What about zzz?")))
    replies = discord.sent[first:]
    assert any("zzz" in text and "recognize" in text for text in replies), replies
    assert not any("across" in text for text in replies), replies
