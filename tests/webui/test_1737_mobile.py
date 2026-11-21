"""#1737 mobile Web UI regressions: Firmware intro, wide tables, the
printer card, Print COST naming, and the password gate for a phone.

Offline: settings from temp YAML, every printer read and the screen are
stubs. Nothing here touches a printer.
"""
import http.client
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlencode

from print_handy import settings
from print_handy.webui import cost_page, firmware_page, pages, printers_page, server, shell, sources

TOKEN = "mobile-gate-1737"
PHONE = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")
DESKTOP = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
JPEG = b"\xff\xd8\xff\xe0stub-screen"


def _cfg(tmp):
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  state_home: {tmp}/hermes\n  records_vault: {tmp}/vault\n"
                     "webui:\n  enabled: true\n  port: 8765\n  controls_enabled: true\n")
    return local


def test_firmware_intro_is_a_short_list_with_commands_in_help():
    with tempfile.TemporaryDirectory() as tmp:
        data = settings.load(local=_cfg(tmp), environ={})
    html = firmware_page.render_firmware_page(data, feed=None)
    intro = html.split("<details")[0]
    assert '<ul class="firmware-intro">' in intro
    # No inline command or setting key in the visible intro.
    assert "/print-firmware" not in intro and "[firmware]" not in intro
    # Commands live in the help block, each on its own definition line.
    help_block = html.split('<details class="firmware-help">')[1].split("</details>")[0]
    for token in ("/print-firmware update", "/print-firmware channel",
                  "[firmware] update_alerts", "[firmware] update_channel"):
        assert f"<code>{token}</code>" in help_block


def test_firmware_and_sources_tables_scroll_instead_of_stacking_letters():
    with tempfile.TemporaryDirectory() as tmp:
        data = settings.load(local=_cfg(tmp), environ={})
    fw = firmware_page.render_firmware_page(data)
    assert "firmware-table-scroll" in fw or "<table" not in fw
    src = sources.render_sources_page()
    assert '<div class="table-scroll"><table>' in src
    css = shell.CSS if hasattr(shell, "CSS") else shell.page("x", "")
    assert ".firmware-table-scroll th, .firmware-table-scroll td { white-space: nowrap; }" in css
    assert ".table-scroll th { white-space: nowrap; }" in css


def test_change_spool_actions_share_one_row_height_on_phones():
    css = shell.page("x", "")
    mobile = css.split("@media (max-width: 40rem)")[1]
    assert ".slot-link { display: flex; flex-direction: column;" in mobile
    assert "min-height" in mobile.split(".slot-link {")[1].split("}")[0]
    assert ".slot-link .slot-action { margin-top: auto;" in mobile
    assert ".slots { align-items: stretch; }" in mobile


def test_printer_card_has_one_title_no_screen_button_and_guarded_taps():
    roster = [{"id": "flashforge-ad5x", "name": "Flashforge AD5X",
               "model": "flashforge.ad5x", "default": True}]
    page = printers_page.render(roster, form_token="tok", screen_for=lambda pid: True,
                                screen_touch=True)
    card = page.split('class="card printer-card"')[1].split("Add a printer")[0]
    assert card.count("Flashforge AD5X") == 2  # the title and the alt text, not a caption
    assert card.count("<h3>Flashforge AD5X") == 1
    assert not re.search(r'class="button"[^>]*>\s*Printer screen\s*<', card)
    assert "Printer settings</a>" in card and "Open spools</a>" in card
    # Tap feedback and gesture differentiation are in the page script.
    assert "printer-tap-marker" in page
    assert "Math.hypot(e.clientX-g.x,e.clientY-g.y)>10" in page
    assert "Date.now()-g.started>500" in page
    assert "pointercancel" in page


def test_cost_is_named_print_cost_in_nav_groups_and_page():
    assert ("/cost", "Print COST") in shell.NAV
    assert ("cost", "Print COST") in pages.GROUPS
    with tempfile.TemporaryDirectory() as tmp:
        data = settings.load(local=_cfg(tmp), environ={})
    html = cost_page.render_cost_page(data)
    assert "<title>Print COST" in html or "Print COST" in html.split("</head>")[0]
    assert "Cost calculator" not in html and "Print cost" not in html


def _serve(tmp, environ, **funcs):
    data = settings.load(local=_cfg(tmp), environ=environ)
    httpd = server.make_server(data, environ=environ, port=0, snapshot_func=lambda: {}, **funcs)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _call(httpd, method, path, ua, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
    try:
        conn.request(method, path, body=body,
                     headers={"User-Agent": ua, **(headers or {})})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def test_phone_and_desktop_get_the_same_password_gate_including_the_screen():
    touched = []
    pages_checked = ("/", "/printers", "/printers/flashforge-ad5x/screen",
                     "/api/printers/flashforge-ad5x/screen/snapshot", "/chat",
                     "/chat?view=design", "/cost", "/firmware", "/config")
    with tempfile.TemporaryDirectory() as tmp:
        httpd = _serve(tmp, {"WEBUI_ACCESS_TOKEN": TOKEN},
                       screen_snapshot_func=lambda pid: (200, JPEG, "image/jpeg"),
                       screen_probe_func=lambda pid: (True, "", "zmod"),
                       screen_touch_func=lambda pid, x, y: touched.append((pid, x, y)) or (200, {"ok": True}))
        try:
            results = {}
            for label, ua in (("phone", PHONE), ("desktop", DESKTOP)):
                for path in pages_checked:
                    code, headers, body = _call(httpd, "GET", path, ua)
                    results[(label, path)] = code
                    assert code in (303, 401), (label, path, code)
                    assert b"stub-screen" not in body
                code, _h, _b = _call(httpd, "POST", "/printers/flashforge-ad5x/screen/touch", ua,
                                     {"Content-Type": "application/x-www-form-urlencoded"},
                                     urlencode({"x": "1", "y": "1"}))
                assert code in (401, 403)
            # The gate is the same for both agents, path by path.
            for path in pages_checked:
                assert results[("phone", path)] == results[("desktop", path)], path
            assert touched == []
            # A wrong password is refused for a phone too; the right one signs in.
            code, _h, _b = _call(httpd, "POST", "/login", PHONE,
                                 {"Content-Type": "application/x-www-form-urlencoded"},
                                 urlencode({"token": "wrong"}))
            assert code != 303 or "webui_session" not in str(_h)
            code, headers, _b = _call(httpd, "POST", "/login", PHONE,
                                      {"Content-Type": "application/x-www-form-urlencoded"},
                                      urlencode({"token": TOKEN}))
            cookie = headers.get("Set-Cookie", "").split(";")[0]
            assert code in (200, 303) and cookie.startswith("webui_session=")
            code, _h, _b = _call(httpd, "GET", "/printers", PHONE, {"Cookie": cookie})
            assert code == 200
        finally:
            httpd.shutdown()
            httpd.server_close()


def test_a_forged_or_expired_session_cookie_does_not_pass_on_a_phone():
    with tempfile.TemporaryDirectory() as tmp:
        httpd = _serve(tmp, {"WEBUI_ACCESS_TOKEN": TOKEN})
        try:
            for cookie in ("webui_session=" + "a" * 43, "webui_session=", "ph-theme=light"):
                code, _h, _b = _call(httpd, "GET", "/printers", PHONE, {"Cookie": cookie})
                assert code in (303, 401), cookie
        finally:
            httpd.shutdown()
            httpd.server_close()
