"""Real-browser Web UI smoke set (deep-dive item 10, P2) -- harness only.

GATE: every test in this file is skipped unless BOTH hold:

* the environment variable ``PRINT_HANDY_BROWSER_SMOKE=1`` is set, and
* a browser driver is importable (``playwright`` -- the sync API).

This mirrors how the repo gates its real-slicer tests
(``tests/slicer/test_orca_real.py``: ``PRINT_HANDY_REAL_SLICER=1`` plus
the real binary). The normal offline suite therefore collects this
file and skips every test with the reason below; nothing here imports
playwright at module level, and no browser is ever launched outside
the gate.

What the browser CI job provides:

* a dedicated blocking job (separate from the offline legs) with
  ``PRINT_HANDY_BROWSER_SMOKE=1`` in its environment;
* ``playwright`` installed (``pip install playwright``) and a headless
  Chromium for it (``playwright install --with-deps chromium``);
* the repo importable in that job (``pip install ".[test]"`` is
  enough -- the server under test is the in-process Web UI on an
  ephemeral loopback port, the same seam ``tests/test_webui_login.py``
  uses; no printer, slicer, network or real camera is involved: the
  camera source is the server's injected fake snapshot function).

Inside the gate the smoke set walks the real served pages in a real
browser: the login page renders, logging in lands on the status page,
the first-run walkthrough renders and advances one step, actual settings
search filters using JavaScript and a control POST without authorization is refused;
the camera snapshot route loads, and every page is asserted free of
uncaught JavaScript errors (``pageerror``) with its expected markers
present. A page that returns 200 with broken JavaScript is exactly
the failure this set exists to catch -- the in-process Web UI tests
cannot see it.
"""
import os
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GATE_ENV = "PRINT_HANDY_BROWSER_SMOKE"
GATE_REASON = ("set PRINT_HANDY_BROWSER_SMOKE=1 and install playwright plus a headless "
               "Chromium (playwright install chromium) to run the real-browser Web UI "
               "smoke set; see this file's docstring for the CI browser job requirements")


def _playwright_importable():
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


BROWSER_SMOKE = os.environ.get(GATE_ENV) == "1" and _playwright_importable()

# A 1x1 white JPEG-shaped payload is unnecessary: the fake snapshot seam
# takes any bytes; the browser only needs the page and its <img> wiring.
FAKE_SNAPSHOT = {
    # Match the real snapshot contract. A tiny dict missing bot/listener
    # reaches a Web UI 500 rather than exercising the actual page.
    "version": "0.browser", "host": "browser-fixture", "ok": True,
    "printer": {"name": "Flashforge AD5X", "model": "flashforge.ad5x",
                "vendor": "flashforge", "connection": "moonraker"},
    "bot": {"platform": "discord", "mode": "standalone",
            "ok": True, "reason": "connected",
            "heartbeat": {"connected": True}},
    "listener": {"required": True, "ok": True, "reason": "alive"},
    "spoolman": {"bundled": True, "url": "http://127.0.0.1:8000", "ok": True,
                 "detail": "HTTP 200"},
    "services": {"spoolman_url": "http://127.0.0.1:8000"},
    "doctor": [{"label": "settings", "status": "ok", "detail": "fine"}],
}
TOKEN = "smoke-token-value"
JPEG_BYTES = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
              b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\x09\x09\x08\x0a\x0c\x14\x0d\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.,\"wxyz\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01\x7d\x01\x02\x03\x00\x04\x11\x05\x12\x21\x31\x41\x06\x13\x51\x61\x07\x61\x71\x14\x22\x32\x81\x91\xa1\x08\x23\x42\xb1\xc1\x15\x52\xd1\xf0\x24\x33\x62\x72\x82\x09\x0a\x16\x17\x18\x19\x1a\x25\x26\x27\x28\x29\x2a\x34\x35\x36\x37\x38\x39\x3a\x43\x44\x45\x46\x47\x48\x49\x4a\x53\x54\x55\x56\x57\x58\x59\x5a\x63\x64\x65\x66\x67\x68\x69\x6a\x73\x74\x75\x76\x77\x78\x79\x7a\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\xfb\xd2\x8a\x28\x03\xff\xd9")


@unittest.skipUnless(BROWSER_SMOKE, GATE_REASON)
class WebUIBrowserSmokeTests(unittest.TestCase):
    """The gated smoke set. Each test gets a fresh server + browser page."""

    def setUp(self):
        from playwright.sync_api import sync_playwright

        from print_handy import settings
        from print_handy.webui import server, walkthrough

        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="ph-browser-smoke-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        local = self.tmp / "local.yaml"
        local.write_text(f"paths:\n  hermes_home: {self.tmp}/hermes\n"
                         "webui:\n  enabled: true\n  port: 8765\n")
        self.environ = {"PRINT_HANDY_CONFIG": str(local), "WEBUI_ACCESS_TOKEN": TOKEN}
        data = settings.load(local=local, environ=self.environ)
        # The status/login cases must not be redirected by the first-run
        # walkthrough. The walkthrough test explicitly re-enables it.
        walkthrough.save_state(data, {"step": 0, "done": True})
        self.settings_data = data
        self.httpd = server.make_server(
            data, environ=self.environ, port=0,
            snapshot_func=lambda: FAKE_SNAPSHOT,
            camera_snapshot_func=lambda printer_id=None: JPEG_BYTES)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch()
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page()
        self.page_errors = []
        self.page.on("pageerror", lambda exc: self.page_errors.append(str(exc)))

    def tearDown(self):
        self.assertEqual(self.page_errors, [],
                         "no uncaught JavaScript errors on any visited page")

    # -- helpers -------------------------------------------------------

    def login(self):
        self.page.goto(self.base + "/login")
        self.page.fill('input[name="token"]', TOKEN)
        self.page.click('button[type="submit"], input[type="submit"]')
        self.page.wait_for_load_state("load")

    # -- the smoke set ---------------------------------------------------

    def test_login_page_renders_with_a_token_form(self):
        self.page.goto(self.base + "/login")
        self.assertEqual(self.page.title() != "", True)
        self.assertIsNotNone(self.page.query_selector('input[name="token"]'),
                             "the login page carries the token form")
        self.assertIn("token", self.page.content().lower())

    def test_login_lands_on_the_status_page(self):
        self.login()
        self.assertTrue(self.page.url.rstrip("/").endswith(
            (":%d" % self.httpd.server_address[1], "/")), self.page.url)
        self.assertIn("Flashforge AD5X", self.page.content(),
                      "the status page must show the real snapshot contract")
        self.assertNotIn("Print Handy error", self.page.content())

    def test_walkthrough_renders_and_advances(self):
        from print_handy.webui import walkthrough

        self.login()
        walkthrough.restart(self.settings_data)
        self.page.goto(self.base + "/walkthrough")
        self.assertTrue(self.page.url.endswith("/walkthrough"), self.page.url)
        self.assertEqual(self.page.locator('input[name="step"]').input_value(), "printer")
        self.assertIn("Printer", self.page.content())
        self.page.locator('button[name="action"][value="skip"]').click()
        self.assertEqual(self.page.locator('input[name="step"]').input_value(), "chat")

    def test_configuration_search_runs_javascript_and_restores_rows(self):
        self.login()
        self.page.goto(self.base + "/config")
        rows = self.page.locator(".field")
        self.assertGreater(rows.count(), 5, "configuration must render real fields")
        search = self.page.locator("#field-search")
        self.assertEqual(search.count(), 1)
        search.fill("zz-this-setting-does-not-exist-zz")
        self.assertEqual(self.page.locator(".field:visible").count(), 0)
        search.fill("")
        self.assertEqual(self.page.locator(".field:visible").count(), rows.count())

    def test_control_post_refuses_browser_cookie_without_api_header(self):
        self.login()
        response = self.page.request.post(
            self.base + "/api/printers/default/emergency_stop",
            data={"confirm": True})
        self.assertEqual(response.status, 401,
                         "browser login cookies may never authorize printer commands")

    def test_camera_snapshot_serves_the_fake_frame(self):
        self.login()
        # The camera surface is the per-printer snapshot endpoint; a real
        # browser fetch of it must return the fake source's JPEG bytes.
        response = self.page.request.get(
            self.base + "/api/printers/default/camera/snapshot")
        self.assertEqual(response.status, 200)
        self.assertTrue(response.body().startswith(b"\xff\xd8"),
                        "the snapshot is a JPEG from the fake source")


if __name__ == "__main__":
    unittest.main()
