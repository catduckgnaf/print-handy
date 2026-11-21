"""#1720: printer screen interaction and main page fixes (from #1717).

- A HelixScreen screen is pressable from the Web UI screen page
  through HelixScreen's own screen-sharing protocol: pair once with
  the six-digit code (exchanged for a token, the code discarded, the
  token kept in a 0600 state file — never in the settings file),
  then a tap is a down/up event pair carrying the token header.
- The printer screen itself is back under Printers: settings on the
  left, the live screen still on the right; the main page links to
  it instead of embedding a second copy.
- Latest Timelapse searches every local clip root (the in-container
  capture root and the mounted local archive included), skips
  partial and empty files, and accepts a .MP4.
- The main page shows the latest insults: the last five roast
  lines, re-derived deterministically from the finished records.
- The login form carries a username field in every mode, so a
  browser or password manager can save the pair (before #1720 the
  token and legacy password modes were never offered a save).

Offline throughout: fake discovery, info, post and fetch throughout;
no printer is contacted.
"""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from print_handy import settings
from print_handy.webui import dashboard, pages, printers_page, screen

JPEG = b"\xff\xd8\xff\xe0fake-screen-jpeg"
HOST = "192.0.2.10"
MOONRAKER = f"http://{HOST}:7125"
HELIX = f"http://{HOST}/helixscreen/screen"
TOKEN = "pair-token-abc123"


def load(extra=""):
    tmp = tempfile.mkdtemp()
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  state_home: {tmp}/hermes\n{extra}")
    return settings.load(local=local, environ={}), tmp


def helix_data(controls=True):
    extra = f"services:\n  moonraker_url: {MOONRAKER}\n"
    if controls:
        extra += "webui:\n  controls_enabled: true\n"
    return load(extra)


def helix_entry(**overrides):
    entry = {"name": "HelixScreen Screen", "service": "mjpegstreamer",
             "stream_url": "/helixscreen/screen/stream.mjpg",
             "snapshot_url": "/helixscreen/screen/snapshot.jpg",
             "extra_data": {"helixscreen_screen": True}}
    entry.update(overrides)
    return entry


def paired_info(url, token):
    return 200, {"control": True, "paired": True, "w": 800, "h": 480}


class HelixTouchTests(unittest.TestCase):
    def setUp(self):
        self.data, _tmp = helix_data()
        self.printer_id = settings.default_printer_id(self.data)

    def touch(self, x=400, y=240, **kwargs):
        kwargs.setdefault("discover", lambda url: [helix_entry()])
        kwargs.setdefault("token_for", lambda data, pid: TOKEN)
        return screen.touch(self.data, self.printer_id, x, y, **kwargs)

    def test_paired_tap_sends_down_then_up_with_the_token(self):
        posts = []
        code, body = self.touch(
            helix_info=paired_info,
            helix_post=lambda url, payload, token:
                posts.append((url, payload, token)) or (200, {}))
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(posts, [
            (HELIX + "/touch", {"type": "down", "x": 400, "y": 240}, TOKEN),
            (HELIX + "/touch", {"type": "up", "x": 400, "y": 240}, TOKEN)])

    def test_view_only_sharing_is_501_and_sends_nothing(self):
        posts = []
        code, body = self.touch(
            helix_info=lambda url, token: (200, {"control": False,
                                                 "paired": True}),
            helix_post=lambda url, payload, token:
                posts.append(url) or (200, {}))
        self.assertEqual(code, 501)
        self.assertIn("Allow Remote Control", body["reason"])
        self.assertEqual(posts, [])

    def test_unpaired_tap_is_403_and_sends_nothing(self):
        posts = []
        code, body = self.touch(
            helix_info=lambda url, token: (200, {"control": True,
                                                 "paired": False}),
            helix_post=lambda url, payload, token:
                posts.append(url) or (200, {}))
        self.assertEqual(code, 403)
        self.assertIn("pairing code", body["reason"])
        self.assertEqual(posts, [])

    def test_tap_validates_against_the_info_frame(self):
        # A 480x320 frame (the U1 shape): x=500 is inside the generic
        # 800-wide frame but outside this screen's own frame.
        code, body = self.touch(
            x=500, y=10,
            helix_info=lambda url, token: (200, {"control": True,
                                                 "paired": True,
                                                 "w": 480, "h": 320}),
            helix_post=lambda url, payload, token: (200, {}))
        self.assertEqual(code, 400)
        self.assertIn("outside", body["reason"])

    def test_stale_pairing_is_refused_by_name(self):
        calls = []

        def post(url, payload, token):
            calls.append(payload["type"])
            return 403, {}

        code, body = self.touch(helix_info=paired_info, helix_post=post)
        self.assertEqual(code, 403)
        self.assertIn("pair again", body["reason"])
        self.assertEqual(calls, ["down"])  # the up event is never sent

    def test_controls_off_is_403_and_sends_nothing(self):
        data, _tmp = helix_data(controls=False)
        code, _body = screen.touch(
            data, self.printer_id, 10, 10,
            discover=lambda url: self.fail("discovered"),
            helix_info=lambda url, token: self.fail("info"),
            helix_post=lambda url, payload, token: self.fail("posted"))
        self.assertEqual(code, 403)


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.data, self.tmp = helix_data()
        self.printer_id = settings.default_printer_id(self.data)

    def test_pair_exchanges_the_code_and_stores_only_the_token(self):
        posts = []
        code, body = screen.pair(
            self.data, self.printer_id, "123456",
            discover=lambda url: [helix_entry()],
            helix_post=lambda url, payload, token:
                posts.append((url, payload, token)) or (200, {"token": TOKEN}))
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(posts, [(HELIX + "/pair", {"code": "123456"}, "")])
        # The token round-trips through the store; the code is nowhere.
        self.assertEqual(
            screen.load_pairing_token(self.data, self.printer_id), TOKEN)
        path = screen.pairing_path(self.data)
        raw = path.read_text(encoding="utf-8")
        self.assertIn(TOKEN, raw)
        self.assertNotIn("123456", raw)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        # And never in the settings file.
        self.assertNotIn(TOKEN, Path(self.tmp, "local.yaml").read_text())

    def test_pair_wrong_code_is_401_and_stores_nothing(self):
        code, body = screen.pair(
            self.data, self.printer_id, "000000",
            discover=lambda url: [helix_entry()],
            helix_post=lambda url, payload, token: (403, {}))
        self.assertEqual(code, 401)
        self.assertIn("did not match", body["reason"])
        self.assertEqual(
            screen.load_pairing_token(self.data, self.printer_id), "")

    def test_pair_too_many_tries_is_429(self):
        code, _body = screen.pair(
            self.data, self.printer_id, "123456",
            discover=lambda url: [helix_entry()],
            helix_post=lambda url, payload, token: (429, {}))
        self.assertEqual(code, 429)

    def test_pair_bad_code_shape_is_400_and_posts_nothing(self):
        posts = []
        for bad in ("12345", "1234567", "12 456", "abcdef", "", None):
            code, _body = screen.pair(
                self.data, self.printer_id, bad,
                discover=lambda url: [helix_entry()],
                helix_post=lambda url, payload, token:
                    posts.append(url) or (200, {"token": TOKEN}))
            self.assertEqual(code, 400, bad)
        self.assertEqual(posts, [])

    def test_pair_on_a_zmod_screen_refuses_by_name(self):
        webcams = [{"name": "screen", "service": "iframe",
                    "stream_url": "/screen/stream",
                    "snapshot_url": "/screen/snapshot"}]
        code, body = screen.pair(
            self.data, self.printer_id, "123456",
            discover=lambda url: webcams,
            helix_post=lambda url, payload, token: self.fail("posted"))
        self.assertEqual(code, 400)
        self.assertIn("not a HelixScreen screen", body["reason"])

    def test_pair_controls_off_is_403(self):
        data, _tmp = helix_data(controls=False)
        code, _body = screen.pair(
            data, self.printer_id, "123456",
            discover=lambda url: self.fail("discovered"),
            helix_post=lambda url, payload, token: self.fail("posted"))
        self.assertEqual(code, 403)

    def test_snapshot_carries_the_stored_pairing_token(self):
        screen.save_pairing_token(self.data, self.printer_id, TOKEN)
        captured = {}

        def fake_fetch(url, timeout=screen._TIMEOUT_S, *, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return JPEG

        original = screen.fetch_screen_jpeg
        screen.fetch_screen_jpeg = fake_fetch
        try:
            code, body, _ctype = screen.snapshot(
                self.data, self.printer_id,
                discover=lambda url: [helix_entry()])
        finally:
            screen.fetch_screen_jpeg = original
        self.assertEqual(code, 200)
        self.assertEqual(body, JPEG)
        self.assertEqual(captured["url"], HELIX + "/snapshot.jpg")
        self.assertEqual(captured["headers"], {"X-Helix-Pair": TOKEN})


class PrintersPageScreenTests(unittest.TestCase):
    ROSTER = [{"id": "ad5x", "name": "Garage AD5X",
               "model": "flashforge.ad5x"},
              {"id": "mini", "name": "Mini", "model": "prusa.mini"}]

    def test_screen_on_the_right_settings_on_the_left(self):
        page = printers_page.render(
            self.ROSTER, form_token="tok",
            screen_for=lambda printer_id: True)
        self.assertIn('class="printer-layout"', page)
        self.assertLess(page.index("printer-settings-col"),
                        page.index("printer-screen-col"))
        self.assertIn('data-printer-screen src="/api/printers/ad5x/screen/snapshot"',
                      page)
        self.assertIn("not the bed camera", page)
        self.assertIn('aria-describedby="printer-preview-note-ad5x"', page)
        self.assertIn('id="printer-preview-note-ad5x" role="status" aria-live="polite"', page)
        self.assertIn("Preview only. Tap or click", page)
        self.assertIn("this preview does not send touches to the printer", page)
        self.assertIn("img.addEventListener('pointerup'", page)
        self.assertNotIn('data-touch-url=', page)
        self.assertIn("No printer action was sent", page)
        self.assertNotIn("/screen/touch", page)
        self.assertIn('href="/printer-settings?printer=ad5x"', page)
        # The non-Moonraker printer gets no screen column.
        self.assertEqual(page.count('class="printer-screen-col"'), 1)

    def test_touch_enabled_embedded_screen_uses_guarded_touch_route(self):
        page = printers_page.render(
            self.ROSTER, form_token="session-csrf-token",
            screen_for=lambda printer_id: True, screen_touch=True)
        self.assertIn('data-touch-url="/printers/ad5x/screen/touch"', page)
        self.assertIn('data-csrf-id="printer-preview-csrf-ad5x"', page)
        self.assertIn('value="session-csrf-token"', page)
        self.assertIn('class="printer-tap-marker"', page)
        self.assertIn("fetch(touchUrl", page)
        self.assertIn("encodeURIComponent(csrf?csrf.value:'')", page)
        self.assertIn("'&x='+x+'&y='+y", page)
        self.assertIn("touch-action:manipulation", page)
        self.assertIn('role="button" tabindex="0"', page)
        self.assertIn("addEventListener('pointerdown'", page)
        self.assertIn("addEventListener('pointerup'", page)
        self.assertIn("addEventListener('pointercancel'", page)
        self.assertIn("Date.now()-g.started>500", page)
        self.assertIn("Math.hypot(e.clientX-g.x,e.clientY-g.y)>10", page)
        self.assertIn("addEventListener('keydown'", page)
        self.assertNotIn("img.addEventListener('click'", page)

    def test_touch_disabled_embedded_screen_stays_preview_only(self):
        page = printers_page.render(
            self.ROSTER, form_token="session-csrf-token",
            screen_for=lambda printer_id: True, screen_touch=False)
        self.assertNotIn('data-touch-url="/printers/ad5x/screen/touch"', page)
        self.assertNotIn('id="printer-preview-csrf-ad5x"', page)
        self.assertIn("Preview only. No printer action was sent", page)

    def test_no_published_screen_renders_the_note(self):
        page = printers_page.render(
            self.ROSTER, form_token="tok",
            screen_for=lambda printer_id: False)
        self.assertIn("No printer screen is published", page)
        self.assertNotIn("data-printer-screen", page)


def dash_data(tmp, extra=""):
    local = Path(tmp) / "local.yaml"
    local.write_text(
        f"paths:\n  state_home: {tmp}/hermes\n  records_vault: {tmp}/vault\n"
        f"  cache: {tmp}/cache\n{extra}")
    return settings.load(local=local, environ={})


class LatestTimelapseRootsTests(unittest.TestCase):
    def setUp(self):
        dashboard._timelapse_cache.clear()

    def tearDown(self):
        dashboard._timelapse_cache.clear()
        os.environ.pop("PRINT_TIMELAPSE_LOCAL_ARCHIVE_DIR", None)

    def test_capture_root_clip_wins_over_an_old_announcement(self):
        # The reported shape: an old clip in the finder roots, and
        # yesterday's clip in the in-container capture root the
        # dashboard never used to search.
        with tempfile.TemporaryDirectory() as tmp:
            data = dash_data(tmp)
            old = Path(tmp) / "cache" / "moonraker-announcements"
            old.mkdir(parents=True)
            stale = old / "print-1_old.mp4"
            stale.write_bytes(b"x" * 10)
            os.utime(stale, (1_000_000, 1_000_000))
            job = Path(tmp) / "cache" / "timelapse" / "print-99"
            job.mkdir(parents=True)
            fresh = job / "timelapse.mp4"
            fresh.write_bytes(b"x" * 2048)
            os.utime(fresh, (2_000_000, 2_000_000))
            found = dashboard.latest_timelapse(data, now=5000.0)
        self.assertEqual(found["name"], "timelapse.mp4")
        self.assertEqual(found["size"], 2048)

    def test_local_archive_root_and_uppercase_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = dash_data(tmp)
            archive = Path(tmp) / "archive"
            archive.mkdir()
            clip = archive / "Print_7.MP4"
            clip.write_bytes(b"x" * 100)
            os.utime(clip, (3_000_000, 3_000_000))
            os.environ["PRINT_TIMELAPSE_LOCAL_ARCHIVE_DIR"] = str(archive)
            found = dashboard.latest_timelapse(data, now=6000.0)
        self.assertEqual(found["name"], "Print_7.MP4")

    def test_partial_and_empty_files_are_not_the_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = dash_data(tmp)
            clips = Path(tmp) / "cache" / "moonraker-announcements"
            clips.mkdir(parents=True)
            good = clips / "print-1_good.mp4"
            good.write_bytes(b"x" * 10)
            os.utime(good, (1_000_000, 1_000_000))
            partial = clips / ".print-2.partial.mp4"
            partial.write_bytes(b"x" * 999)
            os.utime(partial, (9_000_000, 9_000_000))
            empty = clips / "print-3_empty.mp4"
            empty.write_bytes(b"")
            os.utime(empty, (9_500_000, 9_500_000))
            found = dashboard.latest_timelapse(data, now=7000.0)
        self.assertEqual(found["name"], "print-1_good.mp4")


def write_record(tmp, name, payload):
    prints = Path(tmp) / "vault" / "Prints"
    prints.mkdir(parents=True, exist_ok=True)
    (prints / name).write_text(json.dumps(payload), encoding="utf-8")


class LatestInsultsTests(unittest.TestCase):
    def test_last_five_newest_first_and_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = dash_data(tmp)
            for index in range(7):
                write_record(tmp, f"print-{index}.json", {
                    "status": "completed", "title": f"Print {index}",
                    "job_key": f"job-{index}",
                    "completed_at": f"2026-11-{10 + index}T10:00:00Z",
                    "filament_material": "PLA", "filament_used_g": 12.0,
                    "print_duration_seconds": 3600})
            write_record(tmp, "running.json", {
                "status": "printing", "title": "Still running",
                "started_at": "2026-11-18T10:00:00Z"})
            rows = dashboard.latest_insults(data)
            again = dashboard.latest_insults(data)
        self.assertEqual(len(rows), 5)
        self.assertEqual([row["title"] for row in rows],
                         ["Print 6", "Print 5", "Print 4", "Print 3", "Print 2"])
        self.assertTrue(all(row["line"] for row in rows))
        self.assertEqual([row["line"] for row in rows],
                         [row["line"] for row in again])

    def test_roasts_off_is_no_insults(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = dash_data(tmp, "vision:\n  roast_enabled: false\n")
            write_record(tmp, "print-1.json", {
                "status": "failed", "title": "Boom",
                "completed_at": "2026-11-10T10:00:00Z"})
            self.assertEqual(dashboard.latest_insults(data), [])

    def test_insults_section_renders_on_the_dashboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = dash_data(tmp)
            write_record(tmp, "print-1.json", {
                "status": "completed", "title": "Benchy",
                "job_key": "job-1",
                "completed_at": "2026-11-10T10:00:00Z",
                "filament_material": "PLA", "filament_used_g": 8.0,
                "print_duration_seconds": 1800})
            dash = dashboard.build_dashboard(
                data, status_for=lambda pid: (200, {"ok": True, "status": {}}),
                spools_for=lambda pid: (200, {"ok": False, "reason": "down"}),
                screen_for=lambda pid: False)
            page = dashboard.render_dashboard(dash, {"components": {}, "doctor": []},
                                              now=1_800_000_000.0)
        self.assertIn("<h2>Latest insults</h2>", page)
        self.assertIn("Benchy", page)


class PairRouteTests(unittest.TestCase):
    """The pair route on the real server: session plus CSRF, like the
    tap route. No session or a stale token pairs nothing."""

    def call(self, port, method, path, body=None, headers=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read().decode()
        finally:
            conn.close()

    def test_pair_route_needs_the_session_and_the_pages_token(self):
        import re
        import threading
        from urllib.parse import urlencode

        from print_handy.webui import server

        paired = []

        def fake_pair(printer_id, code):
            paired.append((printer_id, code))
            return 200, {"ok": True, "detail": "Paired."}

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "local.yaml"
            local.write_text(
                f"paths:\n  state_home: {tmp}/hermes\n"
                "webui:\n  enabled: true\n  port: 8765\n"
                "  controls_enabled: true\n")
            data = settings.load(
                local=local, environ={"WEBUI_ACCESS_TOKEN": "pair-route-token"})
            httpd = server.make_server(
                data, environ={"WEBUI_ACCESS_TOKEN": "pair-route-token"},
                port=0, snapshot_func=lambda: {},
                screen_probe_func=lambda printer_id: (
                    True, "", screen.SOURCE_HELIXSCREEN),
                screen_pair_func=fake_pair)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(httpd.server_close)
            self.addCleanup(httpd.shutdown)
            port = httpd.server_address[1]
            pid = settings.default_printer_id(httpd.settings_data)
            form = {"Content-Type": "application/x-www-form-urlencoded"}
            path = f"/printers/{pid}/screen/pair"
            # No session at all: refused, nothing paired.
            code, _raw = self.call(
                port, "POST", path,
                body=urlencode({"code": "123456"}), headers=form)
            self.assertIn(code, (401, 403))
            self.assertEqual(paired, [])
            # Sign in, read the page's token from the pairing form.
            code, _raw = self.call(
                port, "POST", "/login",
                body=urlencode({"token": "pair-route-token", "username": "catduck"}),
                headers=form)
            self.assertEqual(code, 303)
            # The login answer carries the session cookie; reuse it.
            import http.client
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.request("POST", "/login",
                             body=urlencode({"token": "pair-route-token"}),
                             headers=form)
                response = conn.getresponse()
                response.read()
                cookie = {"Cookie": response.getheader("Set-Cookie").split(";")[0]}
            finally:
                conn.close()
            code, page = self.call(port, "GET", f"/printers/{pid}/screen",
                                   headers=cookie)
            self.assertEqual(code, 200)
            self.assertIn('id="printer-screen-pair"', page)
            csrf = re.search(r'id="printer-screen-csrf" value="([^"]*)"', page)
            self.assertIsNotNone(csrf, page)
            # A stale token pairs nothing.
            code, _raw = self.call(
                port, "POST", path,
                body=urlencode({"_csrf": "stale", "code": "123456"}),
                headers={**form, **cookie})
            self.assertEqual(code, 403)
            self.assertEqual(paired, [])
            # Session plus the page's token: exactly one pairing,
            # carrying only the code the form named.
            code, raw = self.call(
                port, "POST", path,
                body=urlencode({"_csrf": csrf.group(1), "code": "123456"}),
                headers={**form, **cookie})
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(raw)["ok"])
            self.assertEqual(paired, [(pid, "123456")])

    def test_touch_route_requires_session_and_page_csrf_token(self):
        import re
        import threading
        from urllib.parse import urlencode

        from print_handy.webui import server

        touched = []

        def fake_touch(printer_id, x, y):
            touched.append((printer_id, x, y))
            return 200, {"ok": True, "detail": "Tap sent."}

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "local.yaml"
            local.write_text(
                f"paths:\n  state_home: {tmp}/hermes\n"
                "webui:\n  enabled: true\n  port: 8765\n"
                "  controls_enabled: true\n")
            environ = {"WEBUI_ACCESS_TOKEN": "touch-route-token"}
            data = settings.load(local=local, environ=environ)
            httpd = server.make_server(
                data, environ=environ, port=0,
                screen_probe_func=lambda printer_id: (True, "", screen.SOURCE_ZMOD),
                screen_touch_func=fake_touch)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            self.addCleanup(httpd.server_close)
            self.addCleanup(httpd.shutdown)
            port = httpd.server_address[1]
            pid = settings.default_printer_id(data)
            form = {"Content-Type": "application/x-www-form-urlencoded"}
            path = f"/printers/{pid}/screen/touch"

            code, _raw = self.call(
                port, "POST", path,
                body=urlencode({"x": "12", "y": "34"}), headers=form)
            self.assertIn(code, (401, 403))
            self.assertEqual(touched, [])

            import http.client
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.request("POST", "/login",
                             body=urlencode({"token": "touch-route-token"}),
                             headers=form)
                response = conn.getresponse()
                response.read()
                cookie = {"Cookie": response.getheader("Set-Cookie").split(";")[0]}
            finally:
                conn.close()
            code, page = self.call(port, "GET", f"/printers/{pid}/screen",
                                   headers=cookie)
            self.assertEqual(code, 200)
            csrf = re.search(r'id="printer-screen-csrf" value="([^"]*)"', page)
            self.assertIsNotNone(csrf, page)
            assert csrf is not None
            code, _raw = self.call(
                port, "POST", path,
                body=urlencode({"_csrf": "stale", "x": "12", "y": "34"}),
                headers={**form, **cookie})
            self.assertEqual(code, 403)
            self.assertEqual(touched, [])
            code, raw = self.call(
                port, "POST", path,
                body=urlencode({"_csrf": csrf.group(1), "x": "12", "y": "34"}),
                headers={**form, **cookie})
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(raw)["ok"])
            self.assertEqual(touched, [(pid, "12", "34")])

    def test_paired_helixscreen_page_is_tappable(self):
        page = screen.render_page(
            "AD5X", "ad5x", ok=True, reason="", touch=True,
            form_token="tok", source=screen.SOURCE_HELIXSCREEN,
            paired=True)
        self.assertIn("addEventListener('click'", page)
        self.assertIn("/printers/ad5x/screen/touch", page)
        self.assertNotIn('id="printer-screen-pair"', page)

    def test_screen_page_has_one_visible_title(self):
        page = screen.render_page(
            "AD5X", "ad5x", ok=True, reason="", touch=True,
            form_token="tok", source=screen.SOURCE_ZMOD)
        self.assertIn("<title>Print Handy — Printer screen</title>", page)
        self.assertIn("<h2>Printer screen — AD5X</h2>", page)
        self.assertNotIn("<h1>Print Handy Printer screen</h1>", page)


class LoginSavePasswordTests(unittest.TestCase):
    def test_token_mode_login_form_has_a_username_field(self):
        page = pages.render_login_page(password=False, username=False)
        self.assertIn('name="username"', page)
        self.assertIn('autocomplete="username"', page)
        self.assertIn('autocomplete="current-password"', page)
        self.assertNotIn('name="username" autocomplete="username" '
                         'autocapitalize="none" spellcheck="false" required', page)

    def test_legacy_password_mode_login_form_has_a_username_field(self):
        page = pages.render_login_page(password=True, username=False)
        self.assertIn('name="username"', page)
        self.assertIn('autocomplete="username"', page)

    def test_username_mode_login_form_still_requires_the_username(self):
        page = pages.render_login_page(password=True, username=True)
        self.assertIn('spellcheck="false" required', page)


if __name__ == "__main__":
    unittest.main()
