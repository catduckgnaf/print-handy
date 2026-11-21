"""The Data tab answers a maker's everyday questions first, in plain words."""
from print_handy.core import print_stats
from print_handy.webui import data as data_module
from print_handy.webui import data_page


def _rec(n, status="completed", printer="Garage AD5X", started="2026-05-01T10:00:00Z",
         seconds=3600, grams=40.0, material="PLA", color="Black", reason=None, title=None):
    record = {"print_number": n, "title": title or f"Part {n}", "status": status,
              "printer": printer, "started_at": started, "print_duration_seconds": seconds,
              "filament_used_g": grams, "filament_material": material, "filament_color": color}
    if reason:
        record["stop_reason"] = reason
    return record


RECORDS = [
    _rec(1, started="2026-03-02T10:00:00Z", seconds=12000, grams=820.0),
    _rec(2, status="failed", started="2026-03-09T10:00:00Z", reason="Filament runout detected"),
    _rec(3, status="failed", printer="Voron 2.4", started="2026-04-01T10:00:00Z",
         reason="MCU 'mcu' shutdown: Timer too close"),
    _rec(4, status="failed", printer="Voron 2.4", started="2026-04-03T10:00:00Z",
         reason="Nozzle glowed purple"),
    _rec(5, status="failed", started="2026-04-05T10:00:00Z"),
    _rec(6, status="canceled", printer="Voron 2.4", started="2026-04-07T10:00:00Z"),
    _rec(7, started="2026-04-09T10:00:00Z", title="Benchy", grams=4000.0),
]


def _page(records, **kwargs):
    stats = print_stats.aggregate(records, **kwargs)
    return data_module.render_data_page({}, {"ok": True, **stats, "skipped_records": []})


def _lead(page):
    """The everyday part of the page: everything before the All the numbers expander."""
    return page.split('id="data-all"')[0]


def test_the_page_leads_with_the_everyday_sections_in_order():
    page = _page(RECORDS)
    markers = ['id="data-overview"', 'id="data-trend"', 'id="data-recent"',
               'id="data-failures"', 'id="data-printers"', 'id="data-materials"',
               'id="data-all"']
    positions = [page.index(marker) for marker in markers]
    assert positions == sorted(positions)
    assert "<summary>All the numbers</summary>" in page
    assert '<style>' in page and ".data-page" in page


def test_the_everyday_part_shows_no_internal_keys():
    lead = _lead(_page(RECORDS))
    for key in ("by_printer", "pasta_state", "stop_reasons", "unknown", "canceled",
                "DERIVED", "Intake joins", "Spoolman"):
        assert key not in lead, key


def test_numbers_are_formatted_for_people():
    assert data_page.duration(59 * 60) == "59 min"
    assert data_page.duration(3 * 3600 + 20 * 60) == "3 h 20 min"
    assert data_page.duration(2 * 3600) == "2 h"
    assert data_page.duration(212 * 3600 + 7 * 60) == "212 h"
    assert data_page.duration(None) == "not recorded"
    assert data_page.weight(820) == "820 g"
    assert data_page.weight(4885) == "4.9 kg"
    assert data_page.weight(3.5) == "3.5 g"
    assert data_page.percent(0.666) == "67%"
    assert data_page.percent(None) == "no result yet"
    assert data_page.money(3.5, "USD") == "$3.50"
    assert data_page.money(3.5, "EUR") == "EUR 3.50"
    lead = _lead(_page(RECORDS))
    assert "5.0 kg" in lead                       # 820 + 5 x 40 + 4000 grams
    assert "33%" in lead                          # 2 finished of 6 finished or failed
    assert "Apr 9, 2026" in lead


def test_the_success_rate_and_outcomes_read_plainly():
    lead = _lead(_page(RECORDS))
    assert "2 finished, 4 failed, 1 stopped" in lead
    assert "2 of 6 finished" in lead


def test_failure_causes_are_grouped_in_plain_words_with_a_tip():
    stats = print_stats.aggregate(RECORDS)
    causes = {entry["cause"]: entry for entry in stats["failure_causes"]}
    assert causes["Ran out of filament"]["count"] == 1
    assert causes["Printer lost contact with its controller"]["tip"]
    # An unrecognized reason keeps its own words; nothing is guessed.
    assert causes["Nozzle glowed purple"]["tip"] == ""
    # Failures with no recorded reason are counted, and listed last.
    assert stats["failure_causes"][-1]["cause"] == "No reason recorded"
    lead = _lead(_page(RECORDS))
    assert "Ran out of filament" in lead and "Check the spool" in lead
    assert "Timer too close" not in lead


def test_recent_prints_are_newest_first_with_their_outcome():
    stats = print_stats.aggregate(RECORDS)
    recent = stats["recent_prints"]
    assert [row["print_number"] for row in recent[:3]] == [7, 6, 5]
    assert recent[0]["outcome"] == "finished" and recent[1]["outcome"] == "stopped"
    lead = _lead(_page(RECORDS))
    assert "Benchy" in lead and "Stopped" in lead and "Failed" in lead


def test_each_printer_gets_its_own_record():
    stats = print_stats.aggregate(RECORDS)
    by_name = {row["printer"]: row for row in stats["printers"]}
    assert by_name["Voron 2.4"]["prints"] == 3
    assert by_name["Voron 2.4"]["failed"] == 2 and by_name["Voron 2.4"]["stopped"] == 1
    assert by_name["Voron 2.4"]["success_rate"] == 0.0
    assert by_name["Garage AD5X"]["success_rate"] == 0.5
    lead = _lead(_page(RECORDS))
    assert "Voron 2.4" in lead and 'href="/printers"' in lead


def test_the_month_by_month_trend_and_links_to_cost_and_spools():
    stats = print_stats.aggregate(RECORDS)
    assert stats["monthly_outcomes"]["2026-03"] == {
        "prints": 2, "finished": 1, "failed": 1, "stopped": 0,
        "printing": 0, "unknown": 0, "success_rate": 0.5}
    lead = _lead(_page(RECORDS))
    assert "Mar 2026" in lead and "Apr 2026" in lead
    assert "Apr 2026 had more failures than the months before it" in lead
    assert 'href="/cost"' in lead and 'href="/spools"' in lead


def test_a_new_install_gets_an_empty_state_not_zeros():
    page = _page([])
    assert 'id="data-empty"' in page and "No prints recorded yet." in page
    assert 'id="data-overview"' not in page and 'id="data-all"' not in page
    assert "0 kg" not in page
    year = _page([_rec(1, started="2025-06-01T10:00:00Z")], year=2026)
    assert "No prints were recorded in 2026." in year


def test_the_year_switcher_links_every_recorded_year():
    page = _page(RECORDS + [_rec(9, started="2025-06-01T10:00:00Z")], year=2026)
    assert 'href="/data?year=2025"' in page
    assert '<a href="/data?year=2026" aria-current="page">2026</a>' in page


def test_the_api_payload_only_gains_fields():
    stats = print_stats.aggregate(RECORDS)
    for key in ("total_prints", "total_hours", "total_filament_used_kg", "success_rate",
                "breakdowns", "failures", "outcomes", "repeats", "monthly", "costs"):
        assert key in stats, key
    for key in ("recent_prints", "printers", "monthly_outcomes", "failure_causes"):
        assert key in stats, key
