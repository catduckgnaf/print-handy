"""Web UI favicon and branding icons (#1456).

The browser asks for /favicon.ico and the icons named in the page head
on every page, before it holds any session, so the routes are public
and the login page shows the icon too. The served files live in
print_handy/webui/static and are byte copies of assets/branding; the
last test here pins them equal so the two sets cannot drift apart.

Everything runs offline against a loopback server on an ephemeral
port, in both access shapes: open (no token configured) and claimed
(a token configured, where a page request without it is refused).
"""
import http.client
import tempfile
import threading
import unittest
from pathlib import Path

from print_handy import settings
from print_handy.webui import routes, server, shell

TOKEN = "test-webui-token-12345"
ROOT = Path(__file__).resolve().parent.parent
BRANDING = ROOT / "assets" / "branding"
STATIC = ROOT / "print_handy" / "webui" / "static"
ICON_NAMES = ("favicon.ico", "icon-16.png", "icon-32.png", "icon-64.png",
              "icon-180.png", "icon-192.png", "icon-512.png")

FAKE_SNAPSHOT = {
    "version": "0.test", "host": "testhost", "ok": True,
    "printer": {"model": "flashforge.ad5x", "name": "Flashforge AD5X",
                "vendor": "flashforge", "connection": "moonraker"},
    "bot": {"platform": "discord", "mode": "standalone", "ok": True,
            "reason": "connected", "heartbeat": {"connected": True}},
    "listener": {"required": True, "ok": True, "reason": "alive"},
    "spoolman": {"bundled": True, "url": "http://127.0.0.1:8000", "ok": True,
                 "detail": "HTTP 200"},
    "services": {"spoolman_url": "http://127.0.0.1:8000"},
    "doctor": [{"label": "settings", "status": "ok", "detail": "fine"}],
}


def start(tmp, environ=None):
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  state_home: {tmp}/hermes\n"
                     "webui:\n  enabled: true\n  port: 8765\n")
    env = {"PRINT_HANDY_CONFIG": str(local), **(environ or {})}
    data = settings.load(local=local, environ=env)
    httpd = server.make_server(data, environ=env, port=0,
                               snapshot_func=lambda: FAKE_SNAPSHOT)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


def request(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        return (response.status,
                {name.lower(): value for name, value in response.getheaders()},
                response.read())
    finally:
        conn.close()


class FaviconRouteTests(unittest.TestCase):
    def test_routes_are_declared_public(self):
        route, _capture = routes.resolve("GET", "/favicon.ico")
        self.assertEqual(route.policy, routes.PUBLIC)
        route, capture = routes.resolve("GET", "/static/icon-32.png")
        self.assertEqual(route.policy, routes.PUBLIC)
        self.assertEqual(capture, "icon-32.png")

    def test_favicon_served_anonymously_on_a_claimed_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = start(tmp, environ={"WEBUI_ACCESS_TOKEN": TOKEN})
            self.addCleanup(httpd.server_close)
            self.addCleanup(httpd.shutdown)
            # The install really is claimed: a page without a session
            # is turned away to the login form.
            code, headers, _body = request(
                port, "/", headers={"Accept": "text/html"})
            self.assertEqual(code, 303)
            self.assertEqual(headers.get("location"), "/login")
            # The icon still answers, with the branding bytes.
            code, headers, body = request(port, "/favicon.ico")
            self.assertEqual(code, 200)
            self.assertEqual(headers.get("content-type"), "image/x-icon")
            self.assertEqual(body, (STATIC / "favicon.ico").read_bytes())
            self.assertEqual(body, (BRANDING / "favicon.ico").read_bytes())

    def test_every_icon_size_is_served_with_the_branding_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = start(tmp)
            self.addCleanup(httpd.server_close)
            self.addCleanup(httpd.shutdown)
            for name in ICON_NAMES:
                with self.subTest(icon=name):
                    path = "/favicon.ico" if name == "favicon.ico" \
                        else f"/static/{name}"
                    code, headers, body = request(port, path)
                    self.assertEqual(code, 200)
                    self.assertEqual(body, (BRANDING / name).read_bytes())
                    expected_type = "image/x-icon" if name.endswith(".ico") \
                        else "image/png"
                    self.assertEqual(headers.get("content-type"), expected_type)
                    self.assertIn("max-age=86400",
                                  headers.get("cache-control", ""))

    def test_unknown_static_names_are_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = start(tmp)
            self.addCleanup(httpd.server_close)
            self.addCleanup(httpd.shutdown)
            for path in ("/static/icon-999.png", "/static/shell.py",
                         "/static/../server.py", "/static/"):
                with self.subTest(path=path):
                    code, _headers, _body = request(port, path)
                    self.assertEqual(code, 404)


class FaviconHeadTests(unittest.TestCase):
    def test_icon_links_are_in_the_shell_head(self):
        body = shell.page("status", "<p>x</p>")
        for tag in ('<link rel="icon" type="image/x-icon" href="/favicon.ico">',
                    '<link rel="icon" type="image/png" sizes="32x32" '
                    'href="/static/icon-32.png">',
                    '<link rel="icon" type="image/png" sizes="16x16" '
                    'href="/static/icon-16.png">',
                    '<link rel="apple-touch-icon" sizes="180x180" '
                    'href="/static/icon-180.png">'):
            self.assertIn(tag, body)

    def test_login_page_and_a_chrome_page_carry_the_icon_links(self):
        # The bare login page (no chrome, on a claimed install where it
        # is served) and a normal chrome page (the walkthrough, on an
        # open install) both render through shell.page, so both carry
        # the links.
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = start(tmp, environ={"WEBUI_ACCESS_TOKEN": TOKEN})
            self.addCleanup(httpd.server_close)
            self.addCleanup(httpd.shutdown)
            code, _headers, body = request(
                port, "/login", headers={"Accept": "text/html"})
            self.assertEqual(code, 200)
            text = body.decode()
            self.assertIn('href="/favicon.ico"', text)
            self.assertIn('href="/static/icon-180.png"', text)
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = start(tmp)
            self.addCleanup(httpd.server_close)
            self.addCleanup(httpd.shutdown)
            code, _headers, body = request(
                port, "/walkthrough", headers={"Accept": "text/html"})
            self.assertEqual(code, 200)
            text = body.decode()
            self.assertIn('href="/favicon.ico"', text)
            self.assertIn('href="/static/icon-180.png"', text)


class BrandingCopyTests(unittest.TestCase):
    def test_static_copies_match_the_branding_assets(self):
        for name in ICON_NAMES:
            with self.subTest(icon=name):
                self.assertEqual((STATIC / name).read_bytes(),
                                 (BRANDING / name).read_bytes())
        # And the allowlist names exactly the files on disk: a new
        # static file is not served until it is listed, and a listed
        # name always has a file.
        self.assertEqual(set(server.ICON_FILES), set(ICON_NAMES))
        self.assertEqual({p.name for p in STATIC.iterdir()}, set(ICON_NAMES))


if __name__ == "__main__":
    unittest.main()
