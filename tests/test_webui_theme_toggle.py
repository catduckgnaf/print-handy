"""The Web UI is dark only (#1750): no toggle, no light palette, and a stale
``ph-theme`` cookie or the legacy ``/theme`` route cannot change the look."""
import http.client
import tempfile
import threading
from pathlib import Path

from print_handy import settings
from print_handy.webui import shell, server

TOKEN = "dark-only-1750"
STALE = ("ph-theme=light", "ph-theme=dark", "ph-theme=bogus")


def test_shell_is_dark_whatever_the_cookie_or_argument():
    assert shell.DEFAULT_THEME == "dark"
    for cookie in (None, "", *STALE, "ph-theme=light; other=1", "\x00garbage"):
        assert shell.theme_from_cookies(cookie) == "dark", cookie
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


def test_served_pages_are_dark_with_no_cookie_and_with_a_stale_cookie():
    with tempfile.TemporaryDirectory() as tmp:
        httpd = _serve(tmp)
        try:
            for path in ("/config", "/printers", "/cost"):
                for cookie in (None, *STALE):
                    code, _h, body = _get(httpd, path, cookie)
                    assert code == 200, (path, cookie, code)
                    assert 'data-theme="dark"' in body, (path, cookie)
                    assert 'data-theme="light"' not in body, (path, cookie)
                    assert "theme-toggle" not in body, (path, cookie)
        finally:
            httpd.shutdown()
            httpd.server_close()


def test_legacy_theme_route_redirects_without_setting_a_cookie_or_changing_the_page():
    with tempfile.TemporaryDirectory() as tmp:
        httpd = _serve(tmp)
        try:
            for name in ("light", "dark"):
                code, headers, _b = _get(httpd, f"/theme?set={name}&back=/config")
                assert code == 303
                assert headers["Location"] == "/config"
                assert "Set-Cookie" not in headers
            # Open redirects and header injection still fall back to the status page.
            for back in ("//evil.example", "https://evil.example", "/x%0d%0aSet-Cookie:a=b"):
                code, headers, _b = _get(httpd, f"/theme?set=light&back={back}")
                assert code == 303 and headers["Location"] == "/", back
                assert "Set-Cookie" not in headers
            code, _h, _b = _get(httpd, "/theme?set=sepia")
            assert code == 400
            # Following the legacy redirect with a light cookie in hand still renders dark.
            code, _h, body = _get(httpd, "/config", "ph-theme=light")
            assert code == 200 and 'data-theme="dark"' in body
        finally:
            httpd.shutdown()
            httpd.server_close()
