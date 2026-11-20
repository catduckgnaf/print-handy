"""#1719: Data highlights, the stale-"printing" fix, the Spools card
layout, and the sanitized diagnostics download.

The stale-printing tests are regression tests: on the base they fail
because a record left at status "printing" was counted and badged as
still printing forever, whatever the rest of the history proved.
"""
from print_handy import diagnostics_bundle
from print_handy.core import print_stats
from print_handy.webui import data as data_module
from print_handy.webui import diagnostics, pages, spools


def _rec(n, status="completed", printer="Garage AD5X", started="2026-05-01T10:00:00Z",
         title=None, **extra):
    record = {"print_number": n, "title": title or f"Part {n}", "status": status,
              "printer": printer, "started_at": started,
              "print_duration_seconds": 3600, "filament_used_g": 40.0,
              "filament_material": "PLA", "filament_color": "Black"}
    record.update(extra)
    return record


def _page(records, **kwargs):
    stats = print_stats.aggregate(records, **kwargs)
    return data_module.render_data_page({}, {"ok": True, **stats, "skipped_records": []})


def _lead(page):
    return page.split('id="data-all"')[0]


# ------------------------------------------------------- stale printing

def test_a_stale_printing_record_is_not_counted_or_badged_as_printing():
    records = [
        _rec(1, status="printing", started="2026-05-01T10:00:00Z", title="Old print"),
        _rec(2, status="completed", started="2026-05-03T10:00:00Z"),
    ]
    stats = print_stats.aggregate(records)
    assert stats["in_progress"] == 0
    assert stats["stale_printing_records"] == 1
    recent = {row["title"]: row for row in stats["recent_prints"]}
    assert recent["Old print"]["outcome"] == "unknown"
    lead = _lead(_page(records))
    assert "Not recorded" in lead
    assert "1 older records were left marked printing" in lead


def test_the_newest_printing_record_on_a_printer_stays_printing():
    records = [
        _rec(1, status="completed", started="2026-05-01T10:00:00Z"),
        _rec(2, status="printing", started="2026-05-03T10:00:00Z"),
    ]
    stats = print_stats.aggregate(records)
    assert stats["in_progress"] == 1
    assert stats["stale_printing_records"] == 0
    recent = {row["print_number"]: row for row in stats["recent_prints"]}
    assert recent[2]["outcome"] == "printing"


def test_a_printing_record_with_a_terminal_native_state_counts_by_it():
    records = [_rec(1, status="printing", moonraker_state="complete"),
               _rec(2, status="printing", moonraker_state="error"),
               _rec(3, status="printing", moonraker_state="cancelled")]
    stats = print_stats.aggregate(records)
    assert stats["completed"] == 1
    assert stats["failed"] == 1
    assert stats["canceled"] == 1
    assert stats["in_progress"] == 0


def test_a_printing_record_with_an_end_time_counts_by_its_success_flag():
    records = [_rec(1, status="printing", completed_at="2026-05-01T11:00:00Z",
                    success=True),
               _rec(2, status="printing", completed_at="2026-05-02T11:00:00Z",
                    success=False)]
    stats = print_stats.aggregate(records)
    assert stats["completed"] == 1
    assert stats["failed"] == 1
    assert stats["in_progress"] == 0


def test_a_later_print_on_another_printer_does_not_end_this_one():
    records = [
        _rec(1, status="printing", printer="Garage AD5X",
             started="2026-05-01T10:00:00Z"),
        _rec(2, status="completed", printer="Voron 2.4",
             started="2026-05-03T10:00:00Z"),
    ]
    stats = print_stats.aggregate(records)
    assert stats["in_progress"] == 1
    assert stats["stale_printing_records"] == 0


# ------------------------------------------------------------ highlights

def test_the_source_breakdown_reads_the_recorded_platform():
    records = [
        _rec(1, source_platform="makerworld"),
        _rec(2, source_platform="upload"),
        _rec(3, source_platform="direct"),
        _rec(4),
    ]
    by_source = print_stats.aggregate(records)["breakdowns"]["by_source"]
    assert by_source["MakerWorld"]["count"] == 1
    assert by_source["Uploaded files"]["count"] == 1
    assert by_source["Direct links"]["count"] == 1
    assert by_source[print_stats.UNKNOWN]["count"] == 1


def test_the_source_breakdown_falls_back_to_the_recorded_url_host():
    records = [_rec(1, source_url="https://www.printables.com/model/123-part"),
               _rec(2, source_url="https://files.example.com/part.3mf")]
    by_source = print_stats.aggregate(records)["breakdowns"]["by_source"]
    assert by_source["Printables"]["count"] == 1
    assert by_source["files.example.com"]["count"] == 1


def test_the_source_breakdown_uses_the_intake_join_url():
    records = [_rec(7)]
    enrichments = {"per_print": {7: {"source_url": "https://makerworld.com/en/models/9"}}}
    by_source = print_stats.aggregate(
        records, enrichments=enrichments)["breakdowns"]["by_source"]
    assert by_source["MakerWorld"]["count"] == 1


def test_highlights_show_pasta_sources_and_popular_files():
    records = [
        _rec(1, started="2026-05-01T10:00:00Z", title="Benchy",
             gcode_file="benchy.gcode", source_platform="makerworld"),
        _rec(2, started="2026-05-02T10:00:00Z", title="Benchy",
             gcode_file="benchy.gcode", source_platform="makerworld"),
        _rec(3, started="2026-05-03T10:00:00Z"),
    ]
    events = [{"verdict": "spaghetti", "timestamp": "2026-05-02T10:30:00Z",
               "print_number": 2, "streak": 2}]
    lead = _lead(_page(records, events=events))
    assert 'id="data-highlights"' in lead
    assert "You made pasta 1 time" in lead
    assert "Where your prints came from" in lead
    assert "MakerWorld" in lead and "2 prints · 67%" in lead
    assert "Source not recorded" in lead
    assert "Most popular files" in lead and "Benchy" in lead and "2 times" in lead


def test_highlights_say_pasta_is_not_recorded_when_it_is_not():
    lead = _lead(_page([_rec(1)]))
    assert "Pasta is not counted yet" in lead


# ------------------------------------------------------------ spools page

VIEW = {"id": "ad5x", "name": "AD5X",
        "slots": [{"slot": 1, "spool": {"id": 3, "name": "Orange PLA", "color": "Orange",
                                        "material": "PLA", "brand": "eSun",
                                        "archived": False, "remaining_weight": 500.0,
                                        "initial_weight": 1000.0,
                                        "remaining_percent": 50.0,
                                        "color_hex": "FF7A00", "location": None}},
                  {"slot": 2, "spool": None}],
        "shelf": [{"id": 9, "name": "Blue PLA", "color": "Blue", "material": "PLA",
                   "brand": "eSun", "archived": False, "remaining_weight": 100.0,
                   "initial_weight": 1000.0, "remaining_percent": 10.0,
                   "color_hex": "1E64C8", "location": "Shelf"}],
        "spools": [], "conflicts": []}
VIEW["spools"] = [VIEW["slots"][0]["spool"], VIEW["shelf"][0]]


def test_spools_page_renders_cards_with_discs_and_remaining_bars():
    page = spools.render_spools_page(VIEW, form_token="token")
    assert 'class="spools-page"' in page
    assert page.count('class="sp-card') >= 4  # 2 slots + 2 spools
    assert 'id="slot-1"' in page and 'id="slot-2"' in page
    assert 'data-color="#FF7A00"' in page and 'data-color="#1E64C8"' in page
    assert 'style="width:50%"' in page  # the loaded spool's remaining bar
    assert 'style="width:10%"' in page  # the shelf spool's remaining bar
    assert "<table>" not in page.split("<h2>Assign a spool")[0]
    # The one-step actions ride along on the cards, unchanged.
    for label in ("Replace with", "Rename", "Put back on shelf", "Archive",
                  "Load into"):
        assert label in page


def test_spools_page_keeps_an_empty_slot_card():
    page = spools.render_spools_page(VIEW, form_token="token")
    assert 'class="sp-card empty" id="slot-2"' in page
    assert "empty" in page


# ------------------------------------------------------------- diagnostics

SECRET_VALUES = ("tok-live-1234567890", "sk-live-abcdef123456",
                 "hunter2-password", "discord-bot-token-xyz")


def _settings_with_secrets(tmp_path, monkeypatch):
    """A real loaded settings dict with secrets planted in it: in
    secret-shaped keys (which the config summary must redact) and in a
    log line (which the export sanitizer must redact, as the value of
    a known token environment variable)."""
    from print_handy import settings
    local = tmp_path / "local.toml"
    local.write_text("")
    data = settings.load(local=local, environ={})
    data.setdefault("webui", {})["access_token"] = SECRET_VALUES[0]
    data.setdefault("ai", {})["api_key"] = SECRET_VALUES[1]
    data.setdefault("notify", {})["password"] = SECRET_VALUES[2]
    log = tmp_path / "pb.log"
    log.write_text(f"starting up token={SECRET_VALUES[3]}\nall good\n")
    data["logging"] = {"file": str(log)}
    monkeypatch.setenv("SOME_TOKEN", SECRET_VALUES[3])
    monkeypatch.setenv("PRINT_HANDY_TEST_TOKEN", "env-token-value-987654")
    data.setdefault("discord", {})["token_env"] = "PRINT_HANDY_TEST_TOKEN"
    return data


def test_the_diagnostics_download_leaks_no_secret(tmp_path, monkeypatch):
    code, text = diagnostics.build_download(
        _settings_with_secrets(tmp_path, monkeypatch))
    assert code == 200
    assert "# Print Handy diagnostics bundle" in text
    for secret in SECRET_VALUES + ("env-token-value-987654",):
        assert secret not in text


def test_the_bundle_itself_leaks_no_secret(tmp_path, monkeypatch):
    text = diagnostics_bundle.build_bundle(
        _settings_with_secrets(tmp_path, monkeypatch), include_doctor=False)
    for secret in SECRET_VALUES + ("env-token-value-987654",):
        assert secret not in text


def test_the_download_route_serves_the_bundle_as_an_attachment(tmp_path):
    import http.client
    import threading
    from pathlib import Path

    from print_handy import settings
    from print_handy.webui import server

    token = "webui-access-token-1719"
    environ = {"WEBUI_ACCESS_TOKEN": token}
    local = Path(tmp_path) / "local.yaml"
    local.write_text(f"paths:\n  state_home: {tmp_path}/state\n"
                     "webui:\n  enabled: true\n  port: 8765\n")
    data = settings.load(local=local, environ=environ)
    httpd = server.make_server(data, environ=environ, port=0,
                               snapshot_func=lambda: {})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_address[1], timeout=10)
        try:
            conn.request("GET", "/diagnostics/download",
                         headers={server.TOKEN_HEADER: token})
            response = conn.getresponse()
            body = response.read().decode()
            disposition = response.getheader("Content-Disposition") or ""
            status = response.status
        finally:
            conn.close()
        conn = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_address[1], timeout=10)
        try:
            conn.request("GET", "/diagnostics/download")
            anon_response = conn.getresponse()
            anon_status = anon_response.status
            anon_response.read()
        finally:
            conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert status == 200
    assert 'attachment; filename="diagnostics-bundle.md"' in disposition
    assert "# Print Handy diagnostics bundle" in body
    assert token not in body  # the access token itself never leaks
    assert anon_status != 200  # the route sits behind the session rule


def test_the_config_page_links_the_download_under_logging():
    from print_handy.webui import config as config_api
    from print_handy.webui.catalog import Field
    fields = [config_api.FieldValue(
        field=Field("logging", "level", "Log level", "text", group="logging"),
        value="INFO", provenance=config_api.DEFAULT)]
    html = pages.render_config_page(fields, data={}, form_token="token",
                                    section="logging")
    assert 'href="/diagnostics/download"' in html
    assert "Download diagnostics" in html
    logging_section = html.split('id="group-logging"')[1].split("</section>")[0]
    assert "/diagnostics/download" in logging_section
