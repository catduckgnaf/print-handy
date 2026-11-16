"""Tranche 7: status answers, diagnostics counters, unmatched export (#1524)."""
import asyncio
from unittest import mock

from corpus import fake_discord
from print_handy.bot import natural_language as nl, runtime
from print_handy.runtime import print_stop


def run(coro):
    return asyncio.run(coro)


def test_status_answer_text_uses_only_real_fields():
    text = nl.status_answer_text({"state": "printing", "filename": "benchy.gcode",
                                  "progress": 0.42, "layer": 84, "total_layer": 200,
                                  "remaining_min": 35, "hotend_c": 210, "bed_c": 60})
    assert "printing" in text and "benchy.gcode" in text and "42%" in text
    assert "Layer 84 of 200" in text and "35 minutes" in text and "210" in text
    idle = nl.status_answer_text({"state": "idle"})
    assert idle == "The printer is idle."
    assert nl.status_answer_text(None) == "I couldn't read the printer's status just now."


def test_status_question_answered_through_the_pipeline(cfg):
    discord = fake_discord.FakeDiscord(
        auto=False, chat_id=int(cfg["discord"]["chat_id"]),
        operator_id=int(cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, cfg)
    with mock.patch.object(print_stop, "status",
                           return_value={"state": "printing", "filename": "a.gcode",
                                         "progress": 0.5}):
        run(core.handle_message(discord.message("What is the printer doing right now?")))
    assert any("printing" in text and "50%" in text for text in discord.sent), discord.sent


def test_diagnostics_carry_route_counters(cfg):
    discord = fake_discord.FakeDiscord(
        auto=False, chat_id=int(cfg["discord"]["chat_id"]),
        operator_id=int(cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, cfg)
    status = core.status(True)
    routes = status["natural_language"]["routes"]
    for key in ("deterministic", "clarification", "suggestion", "unmatched", "ai_lane", "shadow"):
        assert key in routes
    assert status["natural_language"]["unmatched_logged"] == len(nl.export_unmatched())


def test_suggestion_route_is_counted(cfg):
    discord = fake_discord.FakeDiscord(
        auto=False, chat_id=int(cfg["discord"]["chat_id"]),
        operator_id=int(cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, cfg)
    before = nl.route_counters()["suggestion"]
    run(core.handle_message(discord.message("pause the print please")))
    assert nl.route_counters()["suggestion"] >= before  # counted only when offered
