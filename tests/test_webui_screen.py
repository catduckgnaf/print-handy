"""#1303 printer screen view: GET /api/printers/<id>/screen/snapshot and
GET /printers/<id>/screen.

The printer screen is the ZMod web screen service's framebuffer still,
discovered through Moonraker's registered webcams (the entry named
``screen``). The still is read-only: the only addresses it ever
requests are Moonraker's webcam list and the one registered snapshot
path, ZMod's ``/screen/snapshot``. The bed camera (the ``video``
webcam, ``camera_snapshot_url``) is never a screen source, and the
service's own page is never embedded. Since #1626 the module also
carries the gated tap: with ``[webui] controls_enabled`` on, one
request to the service's click path per tap, and nothing at all
while controls are off.

Offline: fake discovery and fetch throughout; no printer is contacted.
"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

import pytest

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
    local.write_text(f"paths:\n  state_home: {tmp}/hermes\n{extra}")
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


CLICK_URL = f"http://{HOST}/screen/click"


def touch_data():
    return load(f"services:\n  moonraker_url: {MOONRAKER}\n"
                "webui:\n  controls_enabled: true\n")


class ScreenTouchTests(unittest.TestCase):
    """#1626: the gated tap. Exactly one click request per accepted
    tap, and no request at all on any refusal."""

    def setUp(self):
        self.data = touch_data()
        self.printer_id = settings.default_printer_id(self.data)

    def test_tap_sends_exactly_one_click_with_the_frame_coordinates(self):
        seen = []
        code, body = screen.touch(
            self.data, self.printer_id, "400", "240",
            discover=discover_with(WEBCAMS),
            send=lambda url: seen.append(url) or b"OK")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(seen, [CLICK_URL + "?x=400&y=240"])
        # A service 200 never proves a tap landed (#1624), so the
        # answer says the tap was sent and nothing stronger.
        self.assertIn("Tap sent", body["detail"])
        self.assertNotIn("landed", body["detail"])
        self.assertNotIn(HOST, body["detail"])

    def test_tap_accepts_ints_and_the_frame_corners(self):
        seen = []
        for x, y in ((0, 0), (800, 480), (0, 480), (800, 0)):
            code, _ = screen.touch(
                self.data, self.printer_id, x, y,
                discover=discover_with(WEBCAMS),
                send=lambda url: seen.append(url) or b"OK")
            self.assertEqual(code, 200, (x, y))
        self.assertEqual(seen, [CLICK_URL + "?x=0&y=0",
                                CLICK_URL + "?x=800&y=480",
                                CLICK_URL + "?x=0&y=480",
                                CLICK_URL + "?x=800&y=0"])

    def test_controls_off_is_403_and_sends_nothing(self):
        code, body = screen.touch(
            moonraker_data(), self.printer_id, 10, 10,
            discover=lambda url: self.fail("discovered"),
            send=lambda url: self.fail("sent"))
        self.assertEqual(code, 403)
        self.assertFalse(body["ok"])
        self.assertIn("controls_enabled", body["reason"])

    def test_unknown_printer_is_404_and_sends_nothing(self):
        code, body = screen.touch(
            self.data, "ghost", 10, 10,
            discover=lambda url: self.fail("discovered"),
            send=lambda url: self.fail("sent"))
        self.assertEqual(code, 404)
        self.assertFalse(body["ok"])

    def test_printer_without_moonraker_is_404_and_sends_nothing(self):
        data = load("webui:\n  controls_enabled: true\n")
        printer_id = settings.default_printer_id(data)
        code, body = screen.touch(
            data, printer_id, 10, 10,
            discover=lambda url: self.fail("discovered"),
            send=lambda url: self.fail("sent"))
        self.assertEqual(code, 404)
        self.assertIn("Moonraker", body["reason"])

    def test_no_published_screen_is_404_and_sends_nothing(self):
        code, body = screen.touch(
            self.data, self.printer_id, 10, 10,
            discover=discover_with(WEBCAMS[:1]),
            send=lambda url: self.fail("sent"))
        self.assertEqual(code, 404)
        self.assertIn("screen", body["reason"])

    def test_malformed_coordinates_are_400_and_send_nothing(self):
        for bad in ("", "abc", "1.5", "10px", "1e2", None, 1.5, True):
            with self.subTest(bad=bad):
                code, body = screen.touch(
                    self.data, self.printer_id, bad, 10,
                    discover=lambda url: self.fail("discovered"),
                    send=lambda url: self.fail("sent"))
                self.assertEqual(code, 400)
                self.assertFalse(body["ok"])

    def test_out_of_frame_coordinates_are_400_and_send_nothing(self):
        for x, y in ((-1, 0), (801, 0), (0, -1), (0, 481), (900, 900)):
            with self.subTest(x=x, y=y):
                code, body = screen.touch(
                    self.data, self.printer_id, x, y,
                    discover=lambda url: self.fail("discovered"),
                    send=lambda url: self.fail("sent"))
                self.assertEqual(code, 400)
                self.assertIn("outside", body["reason"])

    def test_unreachable_service_is_502_without_the_address(self):
        def send(url):
            raise TimeoutError("timed out fetching " + url)

        code, body = screen.touch(
            self.data, self.printer_id, 10, 10,
            discover=discover_with(WEBCAMS), send=send)
        self.assertEqual(code, 502)
        self.assertNotIn(HOST, body["reason"])

    def test_unexpected_service_answer_is_502(self):
        code, body = screen.touch(
            self.data, self.printer_id, 10, 10,
            discover=discover_with(WEBCAMS),
            send=lambda url: b"<html>not the screen service</html>")
        self.assertEqual(code, 502)
        self.assertFalse(body["ok"])

    def test_unexpected_registration_shape_is_refused_and_sends_nothing(self):
        webcams = [dict(WEBCAMS[1], snapshot_url="/screen/click?x=1&y=1")]
        code, body = screen.touch(
            self.data, self.printer_id, 10, 10,
            discover=discover_with(webcams),
            send=lambda url: self.fail("sent"))
        self.assertEqual(code, 502)
        self.assertNotIn(HOST, body["reason"])


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
        # The page's own content carries no click handler: nothing in
        # <main> can press the screen. (The shared shell's PWA script
        # outside <main> clicks only its own Install/Dismiss buttons,
        # #450; it never touches the printer.)
        main = html.split("<main>", 1)[1].split("</main>", 1)[0]
        self.assertNotIn("click", main)

    def test_page_explains_an_unavailable_screen(self):
        html = screen.render_page("Bench AD5X", "bench-ad5x", ok=False,
                                  reason="no printer screen is registered")
        self.assertIn("no printer screen is registered", html)
        self.assertNotIn("<img", html)

    def test_page_with_controls_off_carries_no_touch_route(self):
        # The default render is the #1303 page, byte shape unchanged:
        # read-only wording and nothing that posts a tap.
        html = screen.render_page("Bench AD5X", "bench-ad5x", ok=True,
                                  reason="")
        self.assertIn("nothing on this page can press", html)
        self.assertNotIn("screen/touch", html)
        self.assertNotIn("printer-screen-csrf", html)

    def test_page_with_controls_on_offers_taps_and_names_the_limits(self):
        html = screen.render_page("Bench AD5X", "bench-ad5x", ok=True,
                                  reason="", touch=True,
                                  form_token="tok-1626")
        self.assertIn("press the screen from here", html)
        self.assertIn("Taps are all this page sends", html)
        self.assertIn("no drags and no long presses", html)
        self.assertIn("/printers/bench-ad5x/screen/touch", html)
        self.assertIn('id="printer-screen-csrf" value="tok-1626"', html)
        self.assertIn("/api/printers/bench-ad5x/screen/snapshot", html)
        self.assertNotIn("nothing on this page can press", html)
        self.assertNotIn("read-only", html)
        self.assertNotIn(HOST, html)


class ScreenRouteTests(unittest.TestCase):
    """The real server: the screen routes sit behind the same Web UI
    authorization as the camera snapshot, and serve its bytes/errors."""

    def start(self, tmp, **funcs):
        local = Path(tmp) / "local.yaml"
        local.write_text(
            f"paths:\n  state_home: {tmp}/hermes\n"
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
                screen_probe_func=lambda printer_id: (True, "", screen.SOURCE_ZMOD))
            pid = settings.default_printer_id(httpd.settings_data)
            code, _, raw = self.request(port, f"/printers/{pid}/screen",
                                        headers=self.auth())
            self.assertEqual(code, 200)
            html = raw.decode("utf-8", "replace")
            self.assertIn("Printer screen", html)
            self.assertIn(f"/api/printers/{pid}/screen/snapshot", html)


class ScreenTouchRouteTests(unittest.TestCase):
    """The touch route on the real server: session plus CSRF, like
    the dashboard E-stop. A bare cookie, a stale token or no session
    sends nothing; the printer id in the path is all the browser
    names, and the form carries only coordinates."""

    def call(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return (response.status,
                    {k.lower(): v for k, v in response.getheaders()},
                    response.read().decode())
        finally:
            conn.close()

    def test_touch_route_needs_the_session_and_the_pages_token(self):
        import re
        from urllib.parse import urlencode

        taps = []

        def fake_touch(printer_id, x, y):
            taps.append((printer_id, x, y))
            return 200, {"ok": True, "detail": "Tap sent."}

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "local.yaml"
            local.write_text(
                f"paths:\n  state_home: {tmp}/hermes\n"
                "webui:\n  enabled: true\n  port: 8765\n"
                "  controls_enabled: true\n")
            data = settings.load(local=local,
                                 environ={"WEBUI_ACCESS_TOKEN": TOKEN})
            httpd = server.make_server(
                data, environ={"WEBUI_ACCESS_TOKEN": TOKEN}, port=0,
                snapshot_func=lambda: {},
                screen_probe_func=lambda printer_id: (True, "", screen.SOURCE_ZMOD),
                screen_touch_func=fake_touch)
            thread = threading.Thread(target=httpd.serve_forever,
                                      daemon=True)
            thread.start()
            self.addCleanup(httpd.server_close)
            self.addCleanup(httpd.shutdown)
            port = httpd.server_address[1]
            pid = settings.default_printer_id(httpd.settings_data)
            form = {"Content-Type": "application/x-www-form-urlencoded"}
            code, headers, _ = self.call(
                port, "POST", "/login",
                body=urlencode({"token": TOKEN}), headers=form)
            self.assertEqual(code, 303)
            cookie = {"Cookie": headers["set-cookie"].split(";", 1)[0].strip()}
            code, _, page = self.call(port, "GET", f"/printers/{pid}/screen",
                                      headers=cookie)
            self.assertEqual(code, 200)
            csrf = re.search(r'id="printer-screen-csrf" value="([^"]*)"',
                             page)
            self.assertIsNotNone(csrf, page)
            path = f"/printers/{pid}/screen/touch"
            tap = {"x": "400", "y": "240"}
            # No session at all: refused, nothing sent.
            code, _, _ = self.call(
                port, "POST", path,
                body=urlencode({"_csrf": csrf.group(1), **tap}),
                headers=form)
            self.assertIn(code, (401, 403))
            # The cookie alone, and a stale token: refused, nothing sent.
            for body in (urlencode(tap),
                         urlencode({"_csrf": "stale", **tap})):
                code, _, _ = self.call(port, "POST", path, body=body,
                                       headers={**form, **cookie})
                self.assertEqual(code, 403)
            self.assertEqual(taps, [])
            # Session plus the page's token: exactly one tap, carrying
            # only the coordinates the form named.
            code, _, raw = self.call(
                port, "POST", path,
                body=urlencode({"_csrf": csrf.group(1), **tap}),
                headers={**form, **cookie})
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(raw)["ok"])
            self.assertEqual(taps, [(pid, "400", "240")])


def helix_entry(**overrides):
    """The registration HelixScreen 1.1 creates for its Moonraker-proxied
    screen sharing (view-only shape): the name, the extra_data marker,
    and the nginx route's snapshot address."""
    entry = {"name": "HelixScreen Screen", "service": "mjpegstreamer",
             "stream_url": "/helixscreen/screen/stream.mjpg",
             "snapshot_url": "/helixscreen/screen/snapshot.jpg",
             "extra_data": {"helixscreen_screen": True}}
    entry.update(overrides)
    return entry


class ScreenHelixScreenTests(unittest.TestCase):
    """HelixScreen Screen as a second screen source (#1626 addition).

    Viewing accepts only the Moonraker-proxied registration shape,
    built from the printer's configured host; the pairing-code
    registration (an absolute address on the screen device) is
    refused by name and never followed. The ZMod registration keeps
    precedence when both are present. Since #1720 a HelixScreen
    screen is also pressable, through its own sharing protocol once
    paired — see tests/test_webui_screen_helix_1720.py.
    """

    def setUp(self):
        self.data = moonraker_data()
        self.printer_id = settings.default_printer_id(self.data)

    def snapshot(self, webcams):
        fetched = []
        code, body, ctype = screen.snapshot(
            self.data, self.printer_id,
            discover=lambda url: webcams,
            fetch=lambda url: fetched.append(url) or JPEG)
        self.assertEqual(code, 200)
        return body, ctype, fetched

    def test_route_mode_registration_serves_the_still(self):
        body, ctype, fetched = self.snapshot([helix_entry()])
        self.assertEqual(body, JPEG)
        self.assertEqual(ctype, "image/jpeg")
        self.assertEqual(
            fetched, [f"http://{HOST}/helixscreen/screen/snapshot.jpg"])

    def test_marker_recognizes_a_renamed_entry(self):
        body, _ctype, fetched = self.snapshot(
            [helix_entry(name="Workshop display")])
        self.assertEqual(body, JPEG)
        self.assertEqual(
            fetched, [f"http://{HOST}/helixscreen/screen/snapshot.jpg"])

    def test_host_suffixed_name_is_recognized(self):
        _body, _ctype, fetched = self.snapshot(
            [helix_entry(name="HelixScreen Screen (printpi)",
                         extra_data={})])
        self.assertEqual(
            fetched, [f"http://{HOST}/helixscreen/screen/snapshot.jpg"])

    def test_pairing_code_registration_is_refused_by_name(self):
        fetched = []
        code, body, _ctype = screen.snapshot(
            self.data, self.printer_id,
            discover=lambda url: [helix_entry(
                snapshot_url="http://192.0.2.77:7130/snapshot.jpg",
                stream_url="http://192.0.2.77:7130/")],
            fetch=lambda url: fetched.append(url) or JPEG)
        self.assertEqual(code, 404)
        self.assertIn("pairing-code", body["reason"])
        self.assertNotIn("192.0.2.77", body["reason"])
        self.assertEqual(fetched, [])

    def test_drifted_helixscreen_shape_is_refused(self):
        fetched = []
        code, body, _ctype = screen.snapshot(
            self.data, self.printer_id,
            discover=lambda url: [helix_entry(
                snapshot_url="/helixscreen/screen/snap.jpg")],
            fetch=lambda url: fetched.append(url) or JPEG)
        self.assertEqual(code, 502)
        self.assertIn("unexpected shape", body["reason"])
        self.assertEqual(fetched, [])

    def test_zmod_registration_wins_when_both_are_present(self):
        _body, _ctype, fetched = self.snapshot(
            [helix_entry(), WEBCAMS[1]])
        self.assertEqual(fetched, [f"http://{HOST}/screen/snapshot"])
        available, reason, source = screen.probe_source(
            self.data, self.printer_id,
            discover=lambda url: [helix_entry(), WEBCAMS[1]])
        self.assertTrue(available)
        self.assertEqual(source, screen.SOURCE_ZMOD)

    def test_probe_source_names_the_helixscreen_source(self):
        available, reason, source = screen.probe_source(
            self.data, self.printer_id,
            discover=lambda url: [helix_entry()])
        self.assertTrue(available)
        self.assertEqual(reason, "")
        self.assertEqual(source, screen.SOURCE_HELIXSCREEN)

    def test_touch_refuses_an_unpaired_helixscreen_screen_by_name(self):
        # #1720: a HelixScreen tap goes through its screen sharing,
        # but only once paired — an unpaired tap refuses by name and
        # sends no touch event. (Before #1720 every HelixScreen tap
        # refused; the paired path is covered in
        # tests/test_webui_screen_helix_1720.py.)
        sent = []
        code, body = screen.touch(
            touch_data(), self.printer_id, 400, 240,
            discover=lambda url: [helix_entry()],
            send=lambda url: sent.append(url),
            helix_info=lambda url, token: (200, {"control": True,
                                                 "paired": False}),
            helix_post=lambda url, payload, token: sent.append(url) or (200, {}),
            token_for=lambda data, printer_id: "")
        self.assertEqual(code, 403)
        self.assertIn("not paired", body["reason"])
        self.assertEqual(sent, [])

    def test_touch_still_uses_zmod_when_both_are_present(self):
        sent = []
        code, body = screen.touch(
            touch_data(), self.printer_id, 400, 240,
            discover=lambda url: [helix_entry(), WEBCAMS[1]],
            send=lambda url: sent.append(url))
        self.assertEqual(code, 200)
        self.assertEqual(
            sent, [f"http://{HOST}/screen/click?x=400&y=240"])

    def test_helixscreen_page_with_controls_on_unpaired_offers_pairing(self):
        # #1720: with controls on and no pairing stored, the page
        # offers the one-time pairing form instead of a tappable
        # still — and still never names the printer's address.
        page = screen.render_page(
            "AD5X", self.printer_id, ok=True, reason="",
            touch=True, form_token="tok",
            source=screen.SOURCE_HELIXSCREEN)
        self.assertIn("not paired with Print Handy yet", page)
        self.assertIn(f"/printers/{self.printer_id}/screen/pair", page)
        self.assertIn('name="code"', page)
        self.assertIn(f"/api/printers/{self.printer_id}/screen/snapshot", page)
        self.assertNotIn("addEventListener('click'", page)
        self.assertNotIn(HOST, page)
        self.assertNotIn("7130", page)

    def test_helixscreen_page_with_controls_off_is_read_only(self):
        page = screen.render_page(
            "AD5X", self.printer_id, ok=True, reason="",
            source=screen.SOURCE_HELIXSCREEN)
        self.assertIn("shown read-only", page)
        self.assertIn("HelixScreen's screen sharing", page)
        self.assertNotIn("click", page.split("<main>", 1)[1].split("</main>")[0])

    def test_helixscreen_names_stay_screen_feeds_not_cameras(self):
        # The #1306 separation: these names classify as screen feeds,
        # so no camera path can select them.
        from print_handy import live_stream
        self.assertTrue(
            live_stream.is_screen_feed_name("HelixScreen Screen"))
        self.assertTrue(
            live_stream.is_screen_feed_name("HelixScreen Screen (printpi)"))


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------- review hardening

def test_screen_fetches_refuse_redirects():
    """A printer answering a screen request with a redirect is refused,
    never followed: the view only requests the addresses it builds."""
    import io
    import urllib.error
    import urllib.request
    from print_handy.webui import screen as screen_mod

    handler = screen_mod._NoRedirects()
    request = urllib.request.Request("http://printer.local/screen/snapshot")
    with pytest.raises(ValueError):
        handler.redirect_request(request, io.BytesIO(b""), 302, "Found", {},
                                 "http://169.254.169.254/latest/meta-data")
    assert any(isinstance(h, screen_mod._NoRedirects)
               for h in screen_mod._OPENER.handlers)


def test_default_snapshot_path_uses_the_no_redirect_fetch(monkeypatch):
    from print_handy.webui import screen as screen_mod
    import inspect
    source = inspect.getsource(screen_mod.snapshot)
    assert "fetch_screen_jpeg" in source
    assert "camera_module.fetch_jpeg" not in source


def test_touch_send_uses_the_no_redirect_opener(monkeypatch):
    """#1626: the tap goes through the same no-redirect opener as the
    still, so a printer answering the click path with a redirect is
    refused, never followed."""
    from print_handy.webui import screen as screen_mod
    import inspect
    assert "send_click" in inspect.getsource(screen_mod.touch)
    assert "_OPENER.open" in inspect.getsource(screen_mod.send_click)


def test_discovery_answers_are_cached_briefly_and_failures_are_not(monkeypatch):
    from print_handy.webui import screen as screen_mod
    calls = []
    clock = [1000.0]

    def fake_discover(url, **_):
        calls.append(url)
        if len(calls) == 3:
            raise OSError("down")
        return [{"name": "screen", "snapshot_url": "/screen/snapshot"}]

    monkeypatch.setattr(screen_mod, "discover_webcams", fake_discover)
    monkeypatch.setattr(screen_mod, "_discovery_cache", {})
    now = lambda: clock[0]
    screen_mod._cached_discover("http://p:7125", now=now)
    screen_mod._cached_discover("http://p:7125/", now=now)
    assert len(calls) == 1
    clock[0] += screen_mod._DISCOVERY_TTL_S + 1
    screen_mod._cached_discover("http://p:7125", now=now)
    assert len(calls) == 2
    clock[0] += screen_mod._DISCOVERY_TTL_S + 1
    with pytest.raises(OSError):
        screen_mod._cached_discover("http://p:7125", now=now)
    screen_mod._cached_discover("http://p:7125", now=now)
    assert len(calls) == 4


def test_page_backs_off_and_pauses_while_hidden():
    from print_handy.webui import screen as screen_mod
    page = screen_mod.render_page("AD5X", "ad5x", ok=True, reason="")
    assert "document.hidden" in page
    assert "img.onerror" in page
    assert "60000" in page
