"""Web UI polish: the Chat tab, color swatches, the Spoolman gate, plain
error wording, and tables and header that work at phone width.

Everything is offline: settings come from temp YAML, Spoolman and the
printer reads are injected fakes.
"""
import http.client
import re
import tempfile
import threading
from pathlib import Path

from print_handy import settings
from print_handy.webui import dashboard, server, shell, slice_result, sliced, spools, swatch
from print_handy.webui import data as data_module

PRINTER = "flashforge-ad5x"
TOKEN = "polish-test-token"
SNAP = {
    "version": "0.test", "host": "testhost", "ok": True,
    "printer": {"model": "flashforge.ad5x", "name": "Flashforge AD5X",
                "vendor": "flashforge", "connection": "moonraker"},
    "bot": {"platform": "discord", "mode": "standalone", "ok": True,
            "reason": "connected", "heartbeat": {"connected": True}},
    "listener": {"required": True, "ok": True, "reason": "alive"},
    "spoolman": {"bundled": True, "url": "http://127.0.0.1:8000", "ok": True,
                 "detail": "HTTP 200"},
    "services": {},
    "doctor": [{"label": "settings", "status": "ok", "detail": "fine"}],
    "components": {"ready": True},
}
# Spoolman's own form: no leading '#'.
SPOOL = {"id": 3, "name": "Signal Orange PETG", "color": "Signal Orange", "material": "PETG",
         "brand": "Polymaker", "color_hex": "FF7A00", "remaining_weight": 420.0,
         "initial_weight": 1000.0, "remaining_percent": 42.0,
         "archived": False, "location": None}


def load(tmp, extra=""):
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  state_home: {tmp}/hermes\n  records_vault: {tmp}/vault\n{extra}")
    return settings.load(local=local, environ={})


def _status_for(live):
    def status_for(printer_id):
        return 200, {"ok": True, "id": printer_id, "name": "Garage AD5X",
                     "model": "flashforge.ad5x", "status": dict(live)}
    return status_for


def _spools_for(printer_id):
    return 200, {"ok": True, "id": printer_id,
                 "slots": [{"slot": 1, "spool": SPOOL}, {"slot": 2, "spool": None}],
                 "shelf": [dict(SPOOL, id=9, name="Ocean Blue", color_hex="1E64C8")],
                 "spools": [], "conflicts": []}


def _status_page(tmp, status_for, spools_for=_spools_for):
    data = load(tmp)
    dash = dashboard.build_dashboard(data, status_for=status_for, spools_for=spools_for,
                                     history=dashboard.TempHistory())
    return server.render_status_html(SNAP, dashboard=dash)


# ------------------------------------------------------------------ Chat tab

def test_chat_is_in_the_nav_inside_the_designs_group():
    assert ("/designs", "Chat / Design") in shell.NAV
    assert "/chat" in shell.NAV_GROUPS["/designs"][1]
    with tempfile.TemporaryDirectory() as tmp:
        assert ("/designs", "Chat / Design") in data_module.nav_for(load(tmp))
    html = shell.page("chat", "", path="/chat")
    top = html.split('aria-label="Web UI pages"')[1].split("</nav>")[0]
    # The Chat page shares the Designs header destination; Chat itself is
    # not a header tab or a Designs subnav item in the shared shell.
    assert '<a href="/designs">Chat / Design</a>' in top
    assert 'href="/chat"' not in top
    assert 'aria-label="Designs pages"' not in html


# ------------------------------------------------------------------ swatches

def test_swatch_writes_valid_css_for_every_hex_form_and_nothing_for_names():
    for value in ("FF7A00", "#ff7a00", "0xFF7A00", "FF7A00,1E64C8"):
        assert 'style="background: #FF7A00"' in swatch.swatch(value), value
    assert swatch.swatch("Signal Orange") == ""
    assert swatch.swatch(None) == ""
    assert "disc small empty" in swatch.swatch(None, empty=True)
    assert swatch.labeled("FF7A00", "Orange <b>") == (
        '<span class="swatch-label"><span class="disc small" style="background: #FF7A00" '
        'data-color="#FF7A00" aria-hidden="true"></span>Orange &lt;b&gt;</span>')


def test_status_dashboard_paints_spoolman_colors_as_valid_css():
    with tempfile.TemporaryDirectory() as tmp:
        page = _status_page(tmp, _status_for({"state": "idle"}))
    assert "background: #FF7A00" in page  # loaded slot disc
    assert "background: #1E64C8" in page  # shelf disc
    assert not re.search(r"background: [0-9A-Fa-f]{6}\b", page)  # never a bare code


def test_spools_page_shows_a_swatch_beside_each_spool():
    view = {"id": PRINTER, "name": "AD5X",
            "slots": [{"slot": 1, "spool": SPOOL}, {"slot": 2, "spool": None}],
            "shelf": [dict(SPOOL, id=9, color_hex="1E64C8")],
            "spools": [SPOOL, dict(SPOOL, id=9, color_hex="1E64C8")], "conflicts": []}
    page = spools.render_spools_page(view, form_token="t")
    loaded = page.split('id="slot-1"')[1].split('id="slot-2"')[0]
    assert 'data-color="#FF7A00"' in loaded
    # #1719: the spool list is cards, not a table; each spool's card
    # still carries its color disc.
    cards = page.split("<h2>Spools</h2>")[1]
    assert 'data-color="#FF7A00"' in cards and 'data-color="#1E64C8"' in cards


def test_slice_result_shows_a_swatch_beside_the_color_code():
    artifacts = [{"plate_id": "plate_1.gcode", "filaments": [
        {"logical_filament": 1, "material": "PLA", "color": "#FF7A00"},
        {"logical_filament": 2, "material": "PETG", "color": None}]}]
    page = slice_result.render_result_page(artifacts, "job")
    assert ('<span class="swatch-label"><span class="disc small" style="background: #FF7A00"'
            in page)
    assert "#FF7A00</span>" in page
    assert "<td>unknown</td>" in page  # no color: no swatch, never a made-up one


# ------------------------------------------------------------------ Spoolman gate

def test_spools_page_explains_spoolman_is_not_set_up_and_links_the_setting():
    with tempfile.TemporaryDirectory() as tmp:
        data = load(tmp, "services:\n  spoolman_url: ''\n")
        code, body = spools.spools_view(data, PRINTER)
    assert code == 503
    assert body["reason"] == spools.NOT_CONFIGURED_REASON
    page = spools.render_spools_error(PRINTER, code, body["reason"])
    assert "Set up Spoolman to manage spools" in page
    assert 'href="/config?section=spools"' in page
    assert 'class="error"' not in page  # a state to explain, not an alarm
    assert "did not answer" not in page


def test_spools_page_for_an_unreachable_spoolman_links_the_setting():
    page = spools.render_spools_error(PRINTER, 502, spools.UNREACHABLE_REASON)
    assert "Spoolman did not answer" in page
    assert 'href="/config?section=spools"' in page
    assert "cannot be built" not in page


def test_a_configured_spoolman_is_still_asked():
    asked = []

    def client(method, path, body=None):
        asked.append(path)
        return []

    with tempfile.TemporaryDirectory() as tmp:
        data = load(tmp, "services:\n  spoolman_url: http://spoolman.test:7912\n")
        assert spools.spoolman_configured(data, PRINTER)
        code, _body = spools.spools_view(data, PRINTER, client=client,
                                         state_path=Path(tmp) / "state.json")
    assert code == 200 and asked


# ------------------------------------------------------------------ error wording

def test_status_card_never_shows_raw_exception_text():
    raw = {"state": "unavailable",
           "detail": "URLError: <urlopen error [Errno 111] Connection refused>"}
    with tempfile.TemporaryDirectory() as tmp:
        page = _status_page(tmp, _status_for(raw))
    assert "URLError" not in page and "Errno" not in page and "urlopen" not in page
    assert "Print Handy could not reach this printer." in page


def test_a_named_reason_keeps_its_words_without_the_exception_class():
    named = {"state": "unavailable", "detail": "ValueError: no Moonraker URL is configured"}
    with tempfile.TemporaryDirectory() as tmp:
        page = _status_page(tmp, _status_for(named))
    assert "no Moonraker URL is configured" in page
    assert "ValueError" not in page


def test_a_failing_read_is_plain_words_on_the_card_and_detail_in_the_log(caplog):
    def boom(printer_id):
        raise RuntimeError("read exploded at 0x7f")

    with tempfile.TemporaryDirectory() as tmp:
        page = _status_page(tmp, boom, spools_for=boom)
    assert "RuntimeError" not in page and "0x7f" not in page
    assert "Print Handy could not reach this printer." in page
    assert "Spoolman did not answer." in page
    assert "read exploded" in caplog.text  # the detail is kept for the log


# ------------------------------------------------------------------ narrow screens

def test_sliced_and_slice_result_tables_scroll_instead_of_squeezing():
    rows = [{"job_id": "job", "title": "A_really_long_title_with_no_spaces_at_all.3mf",
             "when": "2026-11-18 10:00", "mtime": 0, "has_source": False,
             "settings_report": False,
             "plates": [{"index": 0, "name": "plate_1.gcode", "size": 1000}]}]
    assert '<div class="table-scroll"><table>' in sliced.render_sliced_page(rows)
    artifacts = [{"plate_id": "p", "filaments": [{"logical_filament": 1}]}]
    result = slice_result.render_result_page(artifacts, "job")
    assert result.count('<div class="table-scroll">') == 2


def _phone_css():
    css = shell.CSS
    return css[css.index("@media (max-width: 40rem)"):].split("\n}\n")[0]


def test_phone_width_gives_the_tabs_the_full_header_width():
    phone = _phone_css()
    # The desktop rule reserves 12rem on the right for the theme toggle;
    # at phone width that squeezed the tabs into a narrow column.
    assert "header.site .wrap { padding-right: 1rem;" in phone
    assert ".chrome-right { flex-basis: 100%; }" in phone
    # Light-only pages have no theme toggle to overlap the header.
    page = shell.page("status", "", path="/")
    assert 'data-theme="light"' in page
    assert "theme-toggle" not in page


def test_phone_width_scrolls_tables_rather_than_breaking_words_into_letters():
    phone = _phone_css()
    assert ".table-scroll th { white-space: nowrap; }" in phone
    assert ".table-scroll td { min-width: 7rem; }" in phone


# ------------------------------------------------------------------ through the server

def _serve(tmp, extra=""):
    environ = {"WEBUI_ACCESS_TOKEN": TOKEN}
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  state_home: {tmp}/hermes\n"
                     f"webui:\n  enabled: true\n  port: 8765\n{extra}")
    data = settings.load(local=local, environ=environ)
    httpd = server.make_server(data, environ=environ, port=0, snapshot_func=lambda: SNAP)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _get(httpd, path):
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    try:
        conn.request("GET", path, headers={server.TOKEN_HEADER: TOKEN})
        response = conn.getresponse()
        return response.status, response.read().decode()
    finally:
        conn.close()


def test_the_spools_route_without_spoolman_shows_the_setup_page():
    with tempfile.TemporaryDirectory() as tmp:
        httpd = _serve(tmp, "services:\n  spoolman_url: ''\n")
        try:
            code, page = _get(httpd, f"/spools/{PRINTER}")
            api_code, api = _get(httpd, f"/api/printers/{PRINTER}/spools")
        finally:
            httpd.shutdown()
            httpd.server_close()
    assert code == 503
    assert "Set up Spoolman to manage spools" in page
    assert 'href="/config?section=spools"' in page
    assert api_code == 503 and "Spoolman is not set up yet" in api


def test_every_page_header_reaches_chat():
    with tempfile.TemporaryDirectory() as tmp:
        httpd = _serve(tmp)
        try:
            _code, chat = _get(httpd, "/chat")
            _code, sliced_page = _get(httpd, "/sliced")
        finally:
            httpd.shutdown()
            httpd.server_close()
    assert 'href="/designs">Chat / Design</a>' in chat
    assert 'href="/chat"' not in chat
    sub = sliced_page.split('aria-label="Designs pages"')[1].split("</nav>")[0]
    assert 'href="/sliced" class="active" aria-current="page">Sliced files</a>' in sub
    assert 'href="/chat"' not in sub
