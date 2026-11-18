"""Chat statistics handler tests (#1647): /print-stats selections.

The acceptance scenario, driven through the handler's own answer
function: a channel history with prints from two users on two
printers, plus unattributed prints. Each query form must return only
the matching totals, verified against the underlying records; the
unfiltered answer keeps today's totals, unattributed included.
"""
import asyncio
import json
from types import SimpleNamespace

from print_handy.bot import runtime
from print_handy.bot.handlers import stats as stats_handler


def _record(number, starter=None, printer="AD5X", status="completed"):
    record = {"print_number": number, "title": f"print {number}", "status": status,
              "started_at": f"2025-03-{number:02d}T10:00:00+00:00",
              "printer": printer, "filament_used_g": 100.0,
              "print_duration_seconds": 3600.0,
              "filament_material": "PLA", "filament_color": "Black"}
    if starter is not None:
        record["started_by"] = starter
    return record


ALICE = {"platform": "discord", "user_id": "111", "display_name": "Alice"}
BOB = {"platform": "discord", "user_id": "222", "display_name": "Bob"}
RECORDS = [
    _record(1, ALICE, "AD5X"),
    _record(2, ALICE, "AD5X"),
    _record(3, ALICE, "Voron", status="failed"),
    _record(4, BOB, "AD5X"),
    _record(5, BOB, "Voron"),
    _record(6, None, "Voron"),
    _record(7, None, "AD5X", status="canceled"),
]


def _data(tmp_path):
    prints = tmp_path / "Prints"
    prints.mkdir(parents=True)
    for record in RECORDS:
        (prints / f"print-{record['print_number']:04d}.json").write_text(
            json.dumps(record))
    return {"paths": {"records_vault": str(tmp_path)}}


def test_unfiltered_answer_is_todays_totals(tmp_path):
    text = stats_handler.stats_answer("/print-stats", _data(tmp_path))
    assert text.startswith("Print statistics — all prints:")
    assert "- 7 prints: 5 completed, 1 failed, 1 stopped" in text
    assert "By starter: Alice 3, Bob 2, unattributed 2." in text


def test_own_stats_answer_only_the_requesters_prints(tmp_path):
    text = stats_handler.stats_answer(
        "/print-stats me", _data(tmp_path),
        requester_id="111", requester_name="Alice")
    assert text.startswith("Print statistics — Alice:")
    assert "- 3 prints: 2 completed, 1 failed, 0 stopped" in text


def test_mention_answer_is_that_users_prints(tmp_path):
    text = stats_handler.stats_answer("/print-stats <@222>", _data(tmp_path))
    assert text.startswith("Print statistics — Bob:")
    assert "- 2 prints: 2 completed, 0 failed, 0 stopped" in text


def test_name_answer_resolves_the_recorded_starter(tmp_path):
    text = stats_handler.stats_answer("/print-stats bob", _data(tmp_path))
    assert text.startswith("Print statistics — Bob:")


def test_printer_answer_only_that_printers_prints(tmp_path):
    text = stats_handler.stats_answer("/print-stats printer AD5X", _data(tmp_path))
    assert text.startswith("Print statistics — AD5X:")
    assert "- 4 prints: 3 completed, 0 failed, 1 stopped" in text


def test_combined_answer_is_that_user_on_that_printer(tmp_path):
    text = stats_handler.stats_answer(
        "/print-stats alice on the voron", _data(tmp_path))
    assert text.startswith("Print statistics — Alice on Voron:")
    assert "- 1 print: 0 completed, 1 failed, 0 stopped" in text


def test_unknown_name_and_printer_are_named_answers(tmp_path):
    data = _data(tmp_path)
    text = stats_handler.stats_answer("/print-stats nobody", data)
    assert 'could not tell who or which printer "nobody" means' in text
    assert "Recorded starters: Alice, Bob." in text
    text = stats_handler.stats_answer("/print-stats printer Nope", data)
    assert 'No printer named "Nope"' in text


def test_wrapped_answers_name_the_selection(tmp_path):
    data = _data(tmp_path)
    text = stats_handler.stats_answer("/print-stats wrapped alice", data)
    assert text.startswith("Pasta Wrapped 2025 — Alice made 3 prints.")
    text = stats_handler.stats_answer("/print-stats wrapped printer Voron", data)
    assert text.startswith("Pasta Wrapped 2025 — Voron made 3 prints.")
    text = stats_handler.stats_answer("/print-stats wrapped <@111> on the AD5X", data)
    assert text.startswith("Pasta Wrapped 2025 — Alice on AD5X made 2 prints.")


def test_wrapped_unfiltered_keeps_the_classic_script(tmp_path):
    text = stats_handler.stats_answer("/print-stats wrapped", _data(tmp_path))
    assert text.startswith("Pasta Wrapped 2025: you made 7 prints.")


def test_matrix_user_reference_resolves(tmp_path):
    prints = tmp_path / "Prints"
    prints.mkdir(parents=True, exist_ok=True)
    record = _record(8, {"platform": "matrix", "user_id": "@carol:example.org"})
    (prints / "print-0008.json").write_text(json.dumps(record))
    data = {"paths": {"records_vault": str(tmp_path)}}
    selection = stats_handler.parse_selection(
        "/print-stats @carol:example.org", [record], data,
        platform="matrix", requester_id="@me:example.org")
    assert selection["starter_key"] == "matrix:@carol:example.org"
    assert selection["error"] is None


def test_dispatch_sends_the_answer():
    sent = []

    class Adapter:
        async def send(self, chat_id=None, metadata=None, content=None):
            sent.append(content)
            return SimpleNamespace(success=True)

    gateway = SimpleNamespace(
        _is_user_authorized_for_source=lambda source: True,
        _delivery_adapter_for=lambda source: Adapter())
    source = runtime.make_source("111", "chan-1", platform_name="discord",
                                 display_name="Alice")
    event = SimpleNamespace(source=source, text="/print-stats me", internal=False)
    result = asyncio.run(stats_handler.stats_dispatch(event, gateway))
    assert result == {"action": "skip", "reason": "print stats handled"}
    # The answer text itself is covered by the stats_answer tests; the
    # dispatch contract is that exactly one answer is sent.
    assert len(sent) == 1 and sent[0]
