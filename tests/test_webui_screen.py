"""#1303 printer screen view: GET /api/printers/<id>/screen/snapshot and
GET /printers/<id>/screen.

The printer screen is the ZMod web screen service's framebuffer still,
discovered through Moonraker's registered webcams (the entry named
``screen``). The view is read-only by construction: the only addresses
ever requested are Moonraker's webcam list and the one registered
snapshot path, ZMod's ``/screen/snapshot``. The bed camera (the ``video``
webcam, ``camera_snapshot_url``) is never a screen source, and the
screen service's touch path is never requested or embedded.

Offline: fake discovery and fetch throughout; no printer is contacted.
"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from print_handy import settings
from print_handy.webui import camera, screen, server

JPEG = b"\xff\xd8\xff\xe0fake-screen-jpeg"
HOST = "192.0.2.10"
MOONRAKER = f"http://{HOST}:7125"
DISCOVERY_URL = MOONRAKER + "/server/webcams/list"
SNAPSHOT_URL = f"http://{HOST}/screen/snapshot"
TOKEN = "screen-test-token"


def load(extra=""):
    tmp = tempfile.mkdtemp()
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n{extra}")
    return settings.load(local=local, environ={})


def moonraker_data():
    return load(f"services:\n  moonraker_url: {MOONRAKER}\n")


WEBCAMS = [
    {"name": "video", "service": "mjpegstreamer-adaptive",
     "stream_url": "/webcam/?action=stream",
     "snapshot_url": "/webcam/?action=snapshot"},
    {"name": "screen", "service": "iframe",
     "stream_url": "/screen/stream", "snapshot_url": "/screen/snapshot"},
]


def discover_with(webcams, seen=None):
    def discover(url):
        if seen is not None:
            seen.append(url)
        return webcams
    return discover


class ScreenSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.data = moonraker_data()
        self.printer_id = settings.default_printer_id(self.data)

    def test_registered_screen_snapshot_is_served(self):
        seen_discovery, seen_fetch = [], []
        code, body, content_type = screen.snapshot(
            self.data, self.printer_id,
            discover=discover_with(WEBCAMS, seen_discovery),
            fetch=lambda url: seen_fetch.append(url) or JPEG)
        self.assertEqual((code, body, content_type), (200, JPEG, "image/jpeg"))
        self.assertEqual(seen_discovery, [MOONRAKER])
        self.assertEqual(seen_fetch, [SNAPSHOT_URL])

    def test_only_the_two_read_addresses_are_ever_requested(self):
        # End to end through the real discovery: the recorded requests
        # are Moonraker's webcam list and the one snapshot address.
        requested = []

        def fetch_json(url):
            requested.append(url)
            return {"result": {"webcams": WEBCAMS}}

        code, _, _ = screen.snapshot(
            self.data, self.printer_id,
            discover=lambda url: screen.discover_webcams(
                url, fetch_json=fetch_json),
            fetch=lambda url: requested.append(url) or JPEG)
        self.assertEqual(code, 200)
        self.assertEqual(requested, [DISCOVERY_URL, SNAPSHOT_URL])
        for url in requested:
            self.assertNotIn("click", url)
            self.assertNotIn("webcam/?", url)

    def test_unknown_printer_is_404(self):
        code, body, _ = screen.snapshot(
            self.data, "ghost", discover=discover_with(WEBCAMS),
            fetch=lambda url: self.fail("fetched"))
        self.assertEqual(code, 404)
        self.assertFalse(body["ok"])

    def test_printer_without_moonraker_is_404_and_discovers_nothing(self):
        data = load()
        printer_id = settings.default_printer_id(data)
        code, body, _ = screen.snapshot(
            data, printer_id,
            discover=lambda url: self.fail("discovered"),
            fetch=lambda url: self.fail("fetched"))
        self.assertEqual(code, 404)
        self.assertIn("Moonraker", body["reason"])

    def test_moonraker_unreachable_is_502_without_the_address(self):
        def discover(url):
            raise ConnectionError("refused at " + url)

        code, body, _ = screen.snapshot(
            self.data, self.printer_id, discover=discover,
            fetch=lambda url: self.fail("fetched"))
        self.assertEqual(code, 502)
        self.assertNotIn(HOST, body["reason"])
        self.assertNotIn("7125", body["reason"])

    def test_no_screen_webcam_is_404_and_the_bed_camera_is_never_fetched(self):
        seen_fetch = []
        code, body, _ = screen.snapshot(
            self.data, self.printer_id,
            discover=discover_with(WEBCAMS[:1]),
            fetch=lambda url: seen_fetch.append(url) or JPEG)
        self.assertEqual(code, 404)
        self.assertIn("screen", body["reason"])
        self.assertEqual(seen_fetch, [])

    def test_unexpected_registered_snapshot_shapes_are_refused(self):
        shapes = [
            "/webcam/?action=snapshot",
            "/screen/click?x=10&y=10",
            "/screen/streams",
            "/screen/stream",
            "http://198.51.100.99/screen/snapshot",
            "//198.51.100.99/screen/snapshot",
            "",
        ]
        for shape in shapes:
            with self.subTest(shape=shape):
                webcams = [dict(WEBCAMS[1], snapshot_url=shape)]
                code, body, _ = screen.snapshot(
                    self.data, self.printer_id,
                    discover=discover_with(webcams),
                    fetch=lambda url: self.fail("fetched " + url))
                self.assertEqual(code, 502)
                self.assertNotIn("198.51.100.99", body["reason"])
                if shape:
                    self.assertNotIn(shape, body["reason"])

    def test_screen_snapshot_failure_is_a_graceful_502(self):
        def fetch(url):
            raise TimeoutError("timed out fetching " + url)

        code, body, _ = screen.snapshot(
            self.data, self.printer_id,
            discover=discover_with(WEBCAMS), fetch=fetch)
        self.assertEqual(code, 502)
        self.assertNotIn(HOST, body["reason"])

    def test_non_jpeg_screen_answer_is_502(self):
        code, body, _ = screen.snapshot(
            self.data, self.printer_id,
            discover=discover_with(WEBCAMS),
            fetch=lambda url: b"<html>not a jpeg</html>")
        self.assertEqual(code, 502)
        self.assertIn("not a JPEG", body["reason"])

    def test_probe_reports_availability_without_fetching_a_still(self):
        # probe takes no fetch at all: checking availability can never
        # pull a frame, only read Moonraker's webcam list.
        ok, reason = screen.probe(self.data, self.printer_id,
                                  discover=discover_with(WEBCAMS))
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        ok, reason = screen.probe(self.data, self.printer_id,
                                   discover=discover_with(WEBCAMS[:1]))
        self.assertFalse(ok)
        self.assertIn("screen", reason)


class DiscoveryParsingTests(unittest.TestCase):
    def test_default_discovery_reads_moonrakers_webcam_list(self):
        payload = {"result": {"webcams": WEBCAMS}}
        seen = []

        def fetch_json(url):
            seen.append(url)
            return payload

        webcams = screen.discover_webcams(MOONRAKER, fetch_json=fetch_json)
        self.assertEqual(seen, [DISCOVERY_URL])
        self.assertEqual([w["name"] for w in webcams], ["video", "screen"])

    def test_default_discovery_tolerates_a_bare_list_result(self):
        webcams = screen.discover_webcams(
            MOONRAKER, fetch_json=lambda url: {"result": WEBCAMS})
        self.assertEqual(len(webcams), 2)

    def test_default_discovery_rejects_an_unreadable_payload(self):
        with self.assertRaises(ValueError):
            screen.discover_webcams(MOONRAKER,
                                    fetch_json=lambda url: {"error": "nope"})

    def test_default_discovery_without_webcams_key_is_an_empty_list(self):
        webcams = screen.discover_webcams(
            MOONRAKER, fetch_json=lambda url: {"result": {}})
        self.assertEqual(webcams, [])


class ScreenPageRenderTests(unittest.TestCase):
    def test_page_labels_the_screen_and_marks_it_read_only(self):
        html = screen.render_page("Bench AD5X", "bench-ad5x", ok=True, reason="")
        self.assertIn("Printer screen", html)
        self.assertIn("Bench AD5X", html)
        self.assertIn("read-only", html)
        self.assertIn("/api/printers/bench-ad5x/screen/snapshot", html)
        self.assertNotIn(HOST, html)
        self.assertNotIn("click", html)

    def test_page_explains_an_unavailable_screen(self):
        html = screen.render_page("Bench AD5X", "bench-ad5x", ok=False,
                                  reason="no printer screen is registered")
        self.assertIn("no printer screen is registered", html)
        self.assertNotIn("<img", html)


class ScreenRouteTests(unittest.TestCase):
    """The real server: the screen routes sit behind the same Web UI
    authorization as the camera snapshot, and serve its bytes/errors."""

    def start(self, tmp, **funcs):
        local = Path(tmp) / "local.yaml"
        local.write_text(
            f"paths:\n  hermes_home: {tmp}/hermes\n"
            "webui:\n  enabled: true\n  port: 8765\n")
        data = settings.load(local=local,
                             environ={"WEBUI_ACCESS_TOKEN": TOKEN})
        funcs.setdefault("snapshot_func", lambda: {})
        httpd = server.make_server(
            data, environ={"WEBUI_ACCESS_TOKEN": TOKEN}, port=0, **funcs)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd, httpd.server_address[1]

    def request(self, port, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", path, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def auth(self):
        return {server.TOKEN_HEADER: TOKEN}

    def test_snapshot_route_serves_bytes_and_json_errors(self):
        def fake_screen(printer_id):
            if printer_id == "ghost":
                return 404, {"ok": False, "reason": "no such printer"}, \
                    "application/json"
            return 200, JPEG, "image/jpeg"

        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp, screen_snapshot_func=fake_screen)
            pid = settings.default_printer_id(httpd.settings_data)
            code, headers, raw = self.request(
                port, f"/api/printers/{pid}/screen/snapshot",
                headers=self.auth())
            self.assertEqual(code, 200)
            self.assertEqual(raw, JPEG)
            self.assertEqual(headers["Content-Type"], "image/jpeg")
            self.assertEqual(headers["Cache-Control"], "no-store")
            code, headers, raw = self.request(
                port, "/api/printers/ghost/screen/snapshot",
                headers=self.auth())
            self.assertEqual(code, 404)
            self.assertFalse(json.loads(raw)["ok"])

    def test_screen_routes_require_the_webui_authorization(self):
        def fake_screen(printer_id):  # pragma: no cover - must not run
            raise AssertionError("route ran without authorization")

        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp, screen_snapshot_func=fake_screen)
            pid = settings.default_printer_id(httpd.settings_data)
            for path in (f"/api/printers/{pid}/screen/snapshot",
                         f"/printers/{pid}/screen"):
                code, _, raw = self.request(port, path)
                self.assertEqual(code, 401, path)
                self.assertFalse(json.loads(raw)["ok"])

    def test_screen_page_renders_through_the_server(self):
        def fake_screen(printer_id):
            return 200, JPEG, "image/jpeg"

        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(
                tmp, screen_snapshot_func=fake_screen,
                screen_probe_func=lambda printer_id: (True, ""))
            pid = settings.default_printer_id(httpd.settings_data)
            code, _, raw = self.request(port, f"/printers/{pid}/screen",
                                        headers=self.auth())
            self.assertEqual(code, 200)
            html = raw.decode("utf-8", "replace")
            self.assertIn("Printer screen", html)
            self.assertIn(f"/api/printers/{pid}/screen/snapshot", html)


if __name__ == "__main__":
    unittest.main()
