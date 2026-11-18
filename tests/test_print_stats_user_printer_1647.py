"""Per-user and per-printer statistics core tests (#1647).

The shared filtering in print_handy.core.print_stats: starter
attribution reads, filter_records, the by_starter breakdown, and the
Wrapped selection label. The scenario is the acceptance one: prints
from two users on two printers, plus unattributed prints (one
pre-feature record with no stamp at all).
"""
from print_handy.core import print_stats


def _record(number, starter=None, printer="AD5X", status="completed", **extra):
    record = {"print_number": number, "title": f"print {number}", "status": status,
              "started_at": f"2025-03-{number:02d}T10:00:00+00:00",
              "printer": printer, "filament_used_g": 100.0,
              "print_duration_seconds": 3600.0,
              "filament_material": "PLA", "filament_color": "Black"}
    if starter is not None:
        record["started_by"] = starter
    record.update(extra)
    return record


ALICE = {"platform": "discord", "user_id": "111", "display_name": "Alice"}
BOB = {"platform": "discord", "user_id": "222", "display_name": "Bob"}

RECORDS = [
    _record(1, ALICE, "AD5X"),
    _record(2, ALICE, "AD5X"),
    _record(3, ALICE, "Voron", status="failed"),
    _record(4, BOB, "AD5X"),
    _record(5, BOB, "Voron"),
    _record(6, None, "Voron"),                      # no stamp: unattributed
    _record(7, {"platform": "discord"}, "AD5X"),    # malformed stamp: unattributed
]


def test_record_starter_normalizes_the_stamp():
    assert print_stats.record_starter(RECORDS[0]) == ALICE
    id_only = _record(9, {"platform": "matrix", "user_id": "@a:b.c"})
    assert print_stats.record_starter(id_only) == {"platform": "matrix",
                                                   "user_id": "@a:b.c"}
    assert print_stats.starter_key(RECORDS[0]) == "discord:111"
    assert print_stats.starter_label(RECORDS[0]) == "Alice"
    assert print_stats.starter_label(id_only) == "@a:b.c"


def test_record_starter_missing_or_malformed_is_unattributed():
    for record in (RECORDS[5], RECORDS[6], _record(9, {"user_id": "  "}),
                   _record(9, "not-a-dict"), {}, None):
        assert print_stats.record_starter(record) is None
        assert print_stats.starter_key(record) is None
        assert print_stats.starter_label(record) == print_stats.UNATTRIBUTED


def test_filter_records_by_starter_printer_and_both():
    assert [r["print_number"] for r in print_stats.filter_records(
        RECORDS, starter="discord:111")] == [1, 2, 3]
    assert [r["print_number"] for r in print_stats.filter_records(
        RECORDS, starter=ALICE)] == [1, 2, 3]
    assert [r["print_number"] for r in print_stats.filter_records(
        RECORDS, starter=print_stats.UNATTRIBUTED)] == [6, 7]
    assert [r["print_number"] for r in print_stats.filter_records(
        RECORDS, printer="voron")] == [3, 5, 6]
    assert [r["print_number"] for r in print_stats.filter_records(
        RECORDS, starter="discord:111", printer="Voron")] == [3]
    assert print_stats.filter_records(RECORDS) == RECORDS


def test_filter_records_printer_id_fallback():
    record = _record(9, ALICE)
    record.pop("printer")
    record["printer_id"] = "ad5x-main"
    assert print_stats.filter_records([record], printer="AD5X-Main") == [record]
    assert print_stats.filter_records([record], printer="Voron") == []


def test_aggregate_names_starters_and_unattributed():
    stats = print_stats.aggregate(RECORDS)
    assert stats["total_prints"] == 7
    assert stats["unattributed_prints"] == 2
    by_starter = stats["breakdowns"]["by_starter"]
    assert by_starter["Alice"]["count"] == 3
    assert by_starter["Bob"]["count"] == 2
    assert by_starter[print_stats.UNATTRIBUTED]["count"] == 2
    # The unattributed prints stay in every total.
    assert stats["completed"] == 6 and stats["failed"] == 1


def test_aggregate_filtered_matches_the_records():
    stats = print_stats.aggregate(
        print_stats.filter_records(RECORDS, starter="discord:222", printer="AD5X"))
    assert stats["total_prints"] == 1
    assert stats["unattributed_prints"] == 0


def test_options_come_from_the_records():
    starters = print_stats.starter_options(RECORDS)
    assert [(row["key"], row["label"], row["prints"]) for row in starters] == [
        ("discord:111", "Alice", 3), ("discord:222", "Bob", 2)]
    printers = print_stats.printer_options(RECORDS)
    assert [(row["name"], row["prints"]) for row in printers] == [
        ("AD5X", 4), ("Voron", 3)]


def test_same_display_name_on_two_platforms_stays_two_starters():
    rows = [_record(1, {"platform": "discord", "user_id": "1", "display_name": "Sam"}),
            _record(2, {"platform": "telegram", "user_id": "2", "display_name": "Sam"})]
    by_starter = print_stats.aggregate(rows)["breakdowns"]["by_starter"]
    assert by_starter["Sam (discord)"]["count"] == 1
    assert by_starter["Sam (telegram)"]["count"] == 1


def test_filter_events_for_records_keeps_only_the_selections_prints():
    events = [{"print_number": 1, "verdict": "spaghetti"},
              {"print_number": 4, "verdict": "spaghetti"},
              {"job_key": "job-six", "verdict": "spaghetti"},
              {"print_number": 99, "verdict": "spaghetti"}]
    rows = [dict(RECORDS[0]), dict(RECORDS[5], job_key="job-six")]
    kept = print_stats.filter_events_for_records(events, rows)
    assert kept == [events[0], events[2]]


def test_selection_descriptor_labels():
    assert print_stats.selection_descriptor() is None
    assert print_stats.selection_descriptor(user_label="Alice") == {
        "user": "Alice", "printer": None, "label": "Alice"}
    assert print_stats.selection_descriptor(printer="AD5X") == {
        "user": None, "printer": "AD5X", "label": "AD5X"}
    assert print_stats.selection_descriptor(
        user_label="Alice", printer="AD5X")["label"] == "Alice on AD5X"


def test_wrapped_selection_names_the_coverage():
    selection = print_stats.selection_descriptor(user_label="Alice", printer="Voron")
    rows = print_stats.filter_records(RECORDS, starter="discord:111", printer="Voron")
    payload = print_stats.build_wrapped(rows, 2025, selection=selection)
    assert payload["selection"] == selection
    assert payload["headline"]["total_prints"] == 1
    assert payload["script"][0].startswith("Pasta Wrapped 2025 — Alice on Voron")
    assert "covers only Alice on Voron" in payload["script"][0]


def test_wrapped_unfiltered_payload_is_unchanged():
    payload = print_stats.build_wrapped(RECORDS, 2025)
    assert payload["selection"] is None
    assert payload["script"][0].startswith("Pasta Wrapped 2025: you made 7 prints.")


def test_wrapped_selection_with_no_prints_names_the_selection():
    selection = print_stats.selection_descriptor(user_label="Carol")
    payload = print_stats.build_wrapped(RECORDS, 2024, selection=selection)
    assert "no prints were recorded this year for Carol" in payload["script"][0]
