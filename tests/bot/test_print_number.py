"""'print 57' / 'print #57' are '/print-again 57' in plain text (#436).

Deterministic, no LLM: the forms resolve against the same print-again
registry, offer the same target behind the same bed-clear confirmation,
and an unknown number gets the no-such-print reply — never the generic
help dump. A bare number alone is not a print request.
"""
import asyncio
import json
import os
import time
from unittest import mock

import pytest

from corpus import fake_discord
from print_handy.bot import runtime
from print_handy.bot.router import Router
from print_handy.hermes import discord_print_summary as summary
from print_handy.hermes import print_control as pc
from print_handy.hermes import print_next


def run(coro):
    return asyncio.run(coro)


def _printed(tmp_path, slug, title, age_s):
    """One archived job with one launch record, launched age_s seconds ago."""
    d = tmp_path / slug / "sliced/orca/x1"
    d.mkdir(parents=True)
    (d / "plate_1.gcode").write_text(f";{slug}\nG1 X1\n")
    (tmp_path / slug / "JOB.json").write_text(json.dumps({"title": title, "gcode": str(d / "plate_1.gcode")}))
    rec = d / "plate_1.launch.json"
    rec.write_text(json.dumps({"file": f"{slug}.gcode"}))
    t = time.time() - age_s
    os.utime(rec, (t, t))
    return d / "plate_1.gcode"


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """Two archived prints in an isolated archive: Older Widget is #1, Newer Widget is #2."""
    monkeypatch.setattr(pc, "ARCHIVE", tmp_path)
    monkeypatch.setattr(pc, "PRINT_NUMBERS", tmp_path / "print_numbers.json")
    monkeypatch.setattr(pc, "REPRINT_QUEUE", tmp_path / "reprint_queue.json")
    older = _printed(tmp_path, "older-widget--a", "Older Widget", 200)
    newer = _printed(tmp_path, "newer-widget--b", "Newer Widget", 100)
    return older, newer


def make(cfg, router=None):
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, cfg, router=router if router is not None else Router())
    return discord, core


def drive(cfg, text, confirm_label):
    """Send one message with the print-again confirmation and start stubbed.

    The LLM is configured and its ask explodes: a deterministic form must
    never reach it. Returns (discord, out, confirm, start, started_targets, ask).
    """
    router = Router()
    discord, core = make(cfg, router=router)
    started = []

    async def fake_start(target):
        started.append(target)
        return True, "started"

    confirm = mock.AsyncMock(return_value=confirm_label)
    ask = mock.Mock(side_effect=AssertionError("the LLM was called for a deterministic form"))
    with mock.patch.object(router.plugin, "_confirm", confirm), \
            mock.patch.object(print_next, "start", side_effect=fake_start) as start, \
            mock.patch.object(summary, "gcode_metadata", return_value=(60, 1.0)), \
            mock.patch.object(summary, "_print_number", return_value=2), \
            mock.patch.object(runtime.llm, "ask", ask):
        out = run(core.handle_message(discord.message(text)))
    return discord, core, out, confirm, start, started, ask


@pytest.mark.parametrize("text", ["print 2", "print #2", "/print-again 2", "reprint 2", "print 2 again"])
def test_print_number_forms_all_offer_the_same_print(cfg, archive, text):
    _older, newer = archive
    cfg["llm"].update(chat_url="http://llm.invalid/v1", model="m")  # configured, must not be used
    discord, core, out, confirm, start, started, ask = drive(cfg, text, pc.AGAIN_LABEL)
    assert out == {"action": "skip", "reason": "deterministic print again handled"}
    ask.assert_not_called()
    confirm.assert_awaited_once()
    assert confirm.await_args.args[4] == "Print Newer Widget again from the start?\nConfirm the bed is clear."
    start.assert_called_once()
    assert started[0]["title"] == "Newer Widget" and started[0]["gcode"] == str(newer.resolve())
    # The reprint gets its own new number when it starts (#979 review): the
    # confirmation names the print it repeats instead of reusing its number.
    assert discord.sent == ["Preparing to start Newer Widget. A reprint of print #2; it gets its own "
                            "new print number when it starts. "
                            "estimated print time ~1m, plus warmup ~6m total.",
                            # The handler posts the start announcement itself:
                            # the start claim suppresses the listener's (P2-56).
                            "Print started: Print\nPrint #2\nEstimated total: unknown"]
    assert core.help not in discord.sent


def test_print_unknown_number_replies_no_such_print_and_no_help(cfg, archive):
    cfg["llm"].update(chat_url="http://llm.invalid/v1", model="m")
    discord, core, out, confirm, start, started, ask = drive(cfg, "print 9999", pc.AGAIN_LABEL)
    assert out == {"action": "skip", "reason": "deterministic print again handled"}
    ask.assert_not_called()
    confirm.assert_not_awaited()
    start.assert_not_called() and started == []
    assert discord.sent == ["Nothing to print again: there is no print #9999. "
                            "Type /print-again list. Nothing uploaded or started."]
    assert core.help not in discord.sent


def test_print_number_cancelled_at_the_confirmation_starts_nothing(cfg, archive):
    discord, core, out, confirm, start, started, ask = drive(cfg, "print 2", pc.AGAIN_CANCEL_LABEL)
    assert out == {"action": "skip", "reason": "deterministic print again handled"}
    confirm.assert_awaited_once()
    start.assert_not_called() and started == []
    assert discord.sent == ["Cancelled or expired. Nothing uploaded or started."]


def test_a_bare_number_alone_is_not_a_print_request(cfg, archive):
    discord, core = make(cfg)
    with mock.patch.object(print_next, "start") as start:
        out = run(core.handle_message(discord.message("2")))
    assert out == {"action": "skip", "reason": "command list"}
    assert discord.sent == [core.unmatched]
    start.assert_not_called()


@pytest.mark.parametrize(("text", "expected"), [
    ("print 57", 57), ("print #57", 57), ("Print 57", 57), ("print 57.", 57),
    ("57", None), ("print it", None), ("print the last one again", None),
    ("/print-again 57", None), ("print 57 copies", None), ("please print 57", None), ("", None),
])
def test_plain_print_number_matching_is_exact(text, expected):
    assert Router().plugin._plain_print_number(text) == expected


def test_print_again_x3_question_and_start_post_quote_all_copies(cfg, archive):
    """#438: '/print-again 2 x3' confirms and starts with all-copies figures, not one copy's."""
    discord, core, out, confirm, start, started, ask = drive(cfg, "/print-again 2 x3", pc.AGAIN_LABEL)
    assert out == {"action": "skip", "reason": "deterministic print again handled"}
    ask.assert_not_called()
    assert confirm.await_args.args[4] == (
        "Print Newer Widget again from the start? Copy 1 of 3; after each one finishes I'll ask you to "
        "clear the bed for the next. Each copy: ~1m printing, ~1 g filament. "
        "All 3 copies: ~3m printing (plus ~5m warmup per copy), ~3 g filament.\n"
        "Confirm the bed is clear.")
    assert started[0]["copies"] == 3
    assert discord.sent == ["Preparing to start Newer Widget. A reprint of print #2; it gets its own "
                            "new print number when it starts. "
                            "estimated print time ~1m, plus warmup ~6m total for this copy; "
                            "all 3 copies: ~3m printing (plus ~5m warmup per copy), ~3 g filament.",
                            "Print started: Print\nPrint #2\nEstimated total: unknown"]
