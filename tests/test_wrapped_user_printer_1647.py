"""Web UI per-user / per-printer filters (#1647).

The Data tab and Pasta Wrapped page filter through the same
print_stats.filter_records the chat /print-stats handler uses, so
this suite pins the agreement directly: for each selection, the Web
UI totals equal the totals computed from the records, and the chat
answer for the same selection names the same print count.
"""
import json

from print_handy.bot.handlers import stats as stats_handler
from print_handy.core import print_stats
from print_handy.webui import data as data_module
from print_handy.webui import share_export


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
    return {"paths": {"records_vault": str(tmp_path)},
            "timelapse": {"data_enabled": True, "wrapped_enabled": True}}


def test_data_view_filters_match_the_records(tmp_path):
    data = _data(tmp_path)
    for user, printer in ((None, None), ("discord:111", None), (None, "Voron"),
                          ("discord:111", "Voron"), ("unattributed", None)):
        code, body = data_module.data_view(data, user=user, printer=printer)
        assert code == 200
        expected = print_stats.aggregate(print_stats.filter_records(
            RECORDS, starter=user, printer=printer))
        assert body["total_prints"] == expected["total_prints"]
        assert body["completed"] == expected["completed"]
        assert body["total_filament_used_g"] == expected["total_filament_used_g"]


def test_data_view_carries_selection_and_options(tmp_path):
    data = _data(tmp_path)
    code, body = data_module.data_view(data, user="discord:111", printer="Voron")
    assert body["selection"] == {"user": "Alice", "printer": "Voron",
                                  "label": "Alice on Voron"}
    assert [row["key"] for row in body["filter_options"]["starters"]] == [
        "discord:111", "discord:222"]
    assert body["filter_options"]["unattributed_prints"] == 2
    code, body = data_module.data_view(data)
    assert body["selection"] is None


def test_data_page_shows_the_filter_and_starter_breakdown(tmp_path):
    data = _data(tmp_path)
    code, body = data_module.data_view(data, user="discord:222")
    page = data_module.render_data_page(data, body)
    assert "Showing only Bob" in page
    assert '<select name="user">' in page and '<select name="printer">' in page
    code, body = data_module.data_view(data)
    page = data_module.render_data_page(data, body)
    assert "Showing only" not in page
    assert "unattributed" in page  # the by-starter breakdown names it


def test_wrapped_view_filters_and_labels(tmp_path):
    data = _data(tmp_path)
    code, body = data_module.wrapped_view(data, year=2025, user="discord:111")
    assert code == 200
    assert body["headline"]["total_prints"] == 3
    assert body["selection"]["label"] == "Alice"
    assert body["script"][0].startswith("Pasta Wrapped 2025 — Alice made 3 prints.")
    page = data_module.render_wrapped_page(data, body, form_token="tok")
    assert "Showing only Alice" in page
    assert 'name="user" value="discord:111"' in page  # share form keeps the filter


def test_chat_and_webui_totals_agree_for_the_same_selection(tmp_path):
    data = _data(tmp_path)
    code, body = data_module.data_view(data, user="discord:111", printer="AD5X")
    chat = stats_handler.stats_answer("/print-stats alice on the AD5X", data)
    assert body["total_prints"] == 2
    assert "- 2 prints:" in chat


def test_filtered_wrapped_offers_no_whole_year_video(tmp_path):
    data = _data(tmp_path)
    code, body = data_module.wrapped_view(data, year=2025, user="discord:111")
    assert body["video_available"] is False
    assert body["reel_available"] is False


def test_share_export_of_filtered_wrapped_names_the_selection(tmp_path):
    data = _data(tmp_path)
    code, body = data_module.wrapped_view(data, year=2025, user="discord:111",
                                          printer="Voron")
    result = share_export.export_bundle(body, tmp_path / "out",
                                        visibility="public", include_printers=True)
    assert result["ok"]
    assert result["manifest"]["selection"] == "Alice on Voron"
    page = (tmp_path / "out" / "exports" / "wrapped-2025" / "index.html").read_text()
    assert "This recap covers only Alice on Voron." in page
    lines = share_export.share_card_lines(
        share_export.redact_payload(body, include_printers=True))
    assert ("named", "For Alice on Voron") in lines


def test_share_export_withholds_the_selection_printer_by_default(tmp_path):
    data = _data(tmp_path)
    code, body = data_module.wrapped_view(data, year=2025, user="discord:111",
                                          printer="Voron")
    redacted = share_export.redact_payload(body)
    assert redacted["selection"] == {"user": "Alice", "printer": None,
                                     "label": "Alice", "partial": True}
    page = share_export.render_share_page(redacted)
    assert "This recap covers only Alice." in page
    assert "left out of this export" in page
    assert "Voron" not in page
    lines = share_export.share_card_lines(redacted)
    assert ("named", "For Alice") in lines
    assert all("Voron" not in text for _role, text in lines)
