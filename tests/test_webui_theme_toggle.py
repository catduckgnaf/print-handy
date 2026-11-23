"""The Web UI is dark only (#1750): no toggle and no light palette. The old
``/theme`` redirect is gone (#1817)."""
import http.client
import tempfile
import threading
from pathlib import Path

from print_handy import settings
from print_handy.webui import shell, server

TOKEN = "dark-only-1750"


def test_shell_is_dark_whatever_the_argument():
    assert shell.DEFAULT_THEME == "dark"
    assert not hasattr(shell, "theme_from_cookies")
    assert not hasattr(shell, "THEME_COOKIE")
    for theme in ("dark", "light", "bogus"):
        out = shell.page("Status", "<p>body</p>", path="/", theme=theme)
        assert '<html lang="en" data-theme="dark">' in out
        assert 'data-theme="light"' not in out
        assert '<meta name="theme-color" content="#12110f">' in out


def test_stylesheet_has_one_dark_palette_and_no_light_variant():
    css = shell.CSS
    assert "color-scheme: dark;" in css
    assert "color-scheme: light" not in css
    assert 'html[data-theme="light"]' not in css
    assert "--color-bg: #12110f;" in css
    assert "#f3efe6;\n  --color-surface" not in css


def test_settings_and_notifications_are_quick_links_without_a_toggle():
    out = shell.page("Settings", "<p>body</p>", path="/config")
    assert '<nav class="header-quicklinks" aria-label="Quick links">' in out
    assert out.count('href="/config"') == 1
    assert out.count('href="/notifications"') == 1
    assert 'class="active">Settings</a>' in out
    assert "theme-toggle" not in out
    assert "/theme" not in out
    assert not hasattr(shell, "theme_cookie_header")


def test_phone_header_rules_keep_quicklinks_and_tabs_in_flow():
    assert "@media (max-width: 40rem)" in shell.CSS
    assert ".header-quicklinks { margin-left: auto; }" in shell.CSS
    assert "nav.pills { width: 100%; }" in shell.CSS
    assert "position: absolute; top: 0.75rem; right: 1rem;" not in shell.CSS


def _serve(tmp):
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  state_home: {tmp}/hermes\n  records_vault: {tmp}/vault\n"
                     "webui:\n  enabled: true\n  port: 8765\n")
    environ = {"WEBUI_ACCESS_TOKEN": TOKEN}
    data = settings.load(local=local, environ=environ)
    httpd = server.make_server(data, environ=environ, port=0, snapshot_func=lambda: {})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _get(httpd, path, cookie=None):
    headers = {"X-WebUI-Token": TOKEN}
    if cookie:
        headers["Cookie"] = cookie
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def test_served_pages_are_dark():
    with tempfile.TemporaryDirectory() as tmp:
        httpd = _serve(tmp)
        try:
            for path in ("/config", "/printers", "/cost"):
                code, _h, body = _get(httpd, path)
                assert code == 200, (path, code)
                assert 'data-theme="dark"' in body, path
                assert 'data-theme="light"' not in body, path
                assert "theme-toggle" not in body, path
        finally:
            httpd.shutdown()
            httpd.server_close()


def test_the_old_theme_route_is_gone():
    with tempfile.TemporaryDirectory() as tmp:
        httpd = _serve(tmp)
        try:
            code, headers, _b = _get(httpd, "/theme?set=light&back=/config")
            assert code == 404
            assert "Set-Cookie" not in headers
        finally:
            httpd.shutdown()
            httpd.server_close()
