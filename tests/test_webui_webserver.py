"""Web UI webserver coverage: the real HTTP surface, end to end.

The sibling test_webui_* files pin the modules (status snapshot, controls,
spools, access, the login flow) and many individual routes. This file is
the systematic real-HTTP layer on top: it starts the actual server on an
ephemeral loopback port and drives it with real requests, sweeping every
route for the auth rule, the page/API shapes, the controls and spools
route plumbing, malformed-input robustness, concurrency, and the startup
config rules (including #1042: a port bind conflict is a named refusal).

Everything is offline: settings come from temp YAML files, the printer
backends are injected fakes through make_server's own function slots,
and the only sockets are loopback.
"""
import http.client
import io
import json
import os
import re
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from print_handy import settings
from print_handy.webui import pages, server

TOKEN = "test-webui-token-12345"
WRONG_TOKEN = "test-webui-token-99999"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-body"

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

CONTROL_VERBS = ("pause", "resume", "cancel", "temperature", "target",
                 "emergency_stop", "firmware_restart", "server_restart",
                 "gcode", "exclude")


def load_settings(tmp, extra="", environ=None):
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  state_home: {tmp}/hermes\n{extra}")
    return settings.load(local=local, environ={} if environ is None else environ)


def spools_view(printer_id):
    """The full Spools view shape (mirrors tests/test_webui_spools.py)."""
    spool = {"id": 3, "name": "Pink PLA", "color": "Pink", "material": "PLA",
             "brand": "Sunlu", "color_hex": "F95D73", "remaining_weight": 500.0,
             "initial_weight": 1000.0, "remaining_percent": 50.0,
             "archived": False, "location": "IFS Slot 2"}
    shelf = {"id": 7, "name": "Coffee PLA+", "color": "Coffee",
             "material": "PLA+", "brand": "eSun", "color_hex": "6F4E37",
             "remaining_weight": 800.0, "initial_weight": 1000.0,
             "remaining_percent": 80.0, "archived": False, "location": "Shelf"}
    return {"ok": True, "id": printer_id, "name": "AD5X",
            "model": "flashforge_ad5x",
            "slots": [{"slot": 1, "spool": None}, {"slot": 2, "spool": spool},
                      {"slot": 3, "spool": None}, {"slot": 4, "spool": None}],
            "shelf": [shelf], "spools": [spool, shelf], "conflicts": []}


def fresh_heartbeat(**overrides):
    record = {"heartbeat_at": time.time(), "pid": os.getpid(),
              "mode": "standalone",
              "connected": True, "user": "printhandy#0001", "handlers": 12}
    record.update(overrides)
    return record


class WebServerCase(unittest.TestCase):
    """Start the real server on an ephemeral loopback port per test."""

    def start(self, tmp, extra="", environ=None, **funcs):
        environ = {"WEBUI_ACCESS_TOKEN": TOKEN} if environ is None else environ
        data = load_settings(tmp, "webui:\n  enabled: true\n  port: 8765\n" + extra,
                             environ=environ)
        funcs.setdefault("snapshot_func", lambda: dict(FAKE_SNAPSHOT))
        httpd = server.make_server(data, environ=environ, port=0, **funcs)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd, httpd.server_address[1]

    def request(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def get(self, port, path, headers=None):
        code, headers_out, raw = self.request(port, "GET", path, headers=headers)
        return code, headers_out, raw.decode("utf-8", "replace")

    def auth(self, **extra):
        return {server.TOKEN_HEADER: TOKEN, **extra}

    def login_cookie(self, port):
        form = urlencode({pages.LOGIN_TOKEN_FIELD: TOKEN})
        code, headers, _ = self.request(
            port, "POST", "/login", form,
            {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(code, 303)
        cookie = headers.get("Set-Cookie", "")
        self.assertIn(server.SESSION_COOKIE + "=", cookie)
        return cookie.split(";")[0]

    def printer_id(self, httpd):
        return settings.default_printer_id(httpd.settings_data)


class AuthSweepTests(WebServerCase):
    """Every route answers to the same auth rule, over real HTTP."""

    def get_routes(self, pid):
        return ["/", "/api/status", "/readyz", "/config", "/printers",
                "/spools", f"/spools/{pid}", "/designs", "/layers",
                "/data", "/wrapped", "/api/data", "/api/wrapped",
                "/wrapped/video-file?year=2026", f"/api/printers/{pid}/status",
                f"/api/printers/{pid}/spools",
                f"/api/printers/{pid}/camera/snapshot",
                f"/api/printers/{pid}/bogus", "/walkthrough",
                "/theme?set=dark", "/nope"]

    def test_every_get_route_refuses_without_a_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp)
            pid = self.printer_id(httpd)
            for path in self.get_routes(pid):
                code, _, raw = self.request(port, "GET", path)
                self.assertEqual(code, 401, path)
                self.assertFalse(json.loads(raw)["ok"], path)

    def test_every_get_route_answers_with_a_token(self):
        def fake_status(printer_id):
            return 200, {"ok": True, "id": printer_id,
                         "status": {"state": "printing", "progress": 0.5}}

        def fake_camera(printer_id):
            return 200, JPEG, "image/jpeg"

        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(
                tmp, printer_status_func=fake_status,
                camera_snapshot_func=fake_camera,
                spools_view_func=lambda pid: (200, spools_view(pid)),
                readiness_func=lambda: {"ready": True, "components": {}})
            pid = self.printer_id(httpd)
            for path in self.get_routes(pid):
                code, _, _ = self.request(port, "GET", path, headers=self.auth())
                self.assertNotEqual(code, 401, path)

    def test_wrong_token_and_bearer_scheme_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port = self.start(tmp)
            for headers in ({server.TOKEN_HEADER: WRONG_TOKEN},
                            {"Authorization": f"Bearer {TOKEN}"},
                            {"Authorization": TOKEN}):
                code, _, raw = self.request(port, "GET", "/api/status",
                                            headers=headers)
                self.assertEqual(code, 401, headers)
                payload = json.loads(raw)
                self.assertFalse(payload["ok"])
                self.assertIn(server.TOKEN_HEADER, payload["reason"])

    def test_healthz_is_the_one_open_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port = self.start(tmp)
            with mock.patch.object(server.bot_health, "read",
                                   return_value=fresh_heartbeat()):
                code, _, raw = self.request(port, "GET", "/healthz")
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(raw)["ok"])
            # …while its authenticated sibling on the same server refuses.
            code, _, _ = self.request(port, "GET", "/readyz")
            self.assertEqual(code, 401)

    def test_session_cookie_authorizes_gets_but_never_a_control_post(self):
        seen = []

        def fake_control(printer_id, verb, body):
            seen.append((printer_id, verb, body))
            return 200, {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp, printer_control_func=fake_control)
            pid = self.printer_id(httpd)
            cookie = self.login_cookie(port)
            for path in ("/api/status", "/config", "/"):
                code, _, _ = self.request(port, "GET", path,
                                          headers={"Cookie": cookie})
                self.assertEqual(code, 200, path)
            code, _, raw = self.request(
                port, "POST", f"/api/printers/{pid}/pause", b"{}",
                {"Content-Type": "application/json", "Cookie": cookie})
            self.assertEqual(code, 401)
            self.assertFalse(json.loads(raw)["ok"])
            code, _, _ = self.request(
                port, "POST", f"/api/printers/{pid}/spools/assign", b"{}",
                {"Content-Type": "application/json", "Cookie": cookie})
            self.assertEqual(code, 401)
        self.assertEqual(seen, [])

    def test_the_token_value_appears_in_no_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp)
            pid = self.printer_id(httpd)
            paths = ["/", "/api/status", "/config", "/printers", "/designs",
                     "/layers", "/login", f"/api/printers/{pid}/status",
                     "/nope", "/api/printers/x/bogus"]
            bodies = [self.get(port, path, headers=self.auth())[2]
                      for path in paths]
            bodies.append(self.get(port, "/api/status")[2])  # the 401 body
            bodies.append(self.get(
                port, "/api/status",
                headers={server.TOKEN_HEADER: WRONG_TOKEN})[2])
        for body in bodies:
            self.assertNotIn(TOKEN, body)
            self.assertNotIn(WRONG_TOKEN, body)


class RouteShapeTests(WebServerCase):
    """Pages, API shapes, and the routes the module tests reach only
    in-process (camera proxy, Data/Wrapped, readyz contract)."""

    def test_pages_render_their_own_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(
                tmp, "timelapse:\n  wrapped_enabled: true\n",
                spools_view_func=lambda pid: (200, spools_view(pid)))
            pid = self.printer_id(httpd)
            pages_expected = [
                ("/", "Print Handy status"),
                ("/config", f'name="{pages.CSRF_FIELD}"'),
                ("/printers", "Add a printer"),
                ("/designs", "Print Handy designs"),
                ("/layers", "Print Handy layers"),
                ("/data", "Print Handy data"),
                ("/wrapped", "Print Handy wrapped"),
                ("/login", f'name="{pages.LOGIN_TOKEN_FIELD}"'),
                (f"/spools/{pid}", "Pink · PLA · #3"),
            ]
            for path, marker in pages_expected:
                code, headers, body = self.get(port, path, headers=self.auth())
                self.assertEqual(code, 200, path)
                self.assertIn("text/html", headers["Content-Type"], path)
                self.assertIn(marker, body, path)
            # One printer in the roster: /spools goes straight to its page.
            code, headers, _ = self.request(port, "GET", "/spools",
                                            headers=self.auth())
            self.assertEqual(code, 303)
            self.assertEqual(headers["Location"], f"/spools/{pid}")

    def test_api_status_shape_and_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port = self.start(tmp)
            code, headers, raw = self.request(port, "GET", "/api/status",
                                              headers=self.auth())
            self.assertEqual(code, 200)
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertEqual(int(headers["Content-Length"]), len(raw))
            payload = json.loads(raw)
            for key in ("version", "host", "ok", "printer", "bot",
                        "listener", "spoolman", "services", "doctor"):
                self.assertIn(key, payload)
            self.assertEqual(payload["printer"]["model"], "flashforge.ad5x")
            self.assertTrue(payload["ok"])

    def test_printer_status_route_passes_the_backend_through(self):
        def fake_status(printer_id):
            if printer_id == "ghost":
                return 404, {"ok": False, "reason": "no such printer"}
            return 200, {"ok": True, "id": printer_id,
                         "status": {"state": "printing", "progress": 0.5,
                                    "filename": "benchy.gcode"}}

        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp, printer_status_func=fake_status)
            pid = self.printer_id(httpd)
            code, _, raw = self.request(port, "GET",
                                        f"/api/printers/{pid}/status",
                                        headers=self.auth())
            self.assertEqual(code, 200)
            payload = json.loads(raw)
            self.assertEqual(payload["status"]["filename"], "benchy.gcode")
            code, _, raw = self.request(port, "GET",
                                        "/api/printers/ghost/status",
                                        headers=self.auth())
            self.assertEqual(code, 404)
            self.assertFalse(json.loads(raw)["ok"])

    def test_camera_snapshot_route_serves_bytes_and_json_errors(self):
        def fake_camera(printer_id):
            if printer_id == "ghost":
                return 404, {"ok": False, "reason": "no such printer"}, \
                    "application/json"
            return 200, JPEG, "image/jpeg"

        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp, camera_snapshot_func=fake_camera)
            pid = self.printer_id(httpd)
            code, headers, raw = self.request(
                port, "GET", f"/api/printers/{pid}/camera/snapshot",
                headers=self.auth())
            self.assertEqual(code, 200)
            self.assertEqual(raw, JPEG)
            self.assertEqual(headers["Content-Type"], "image/jpeg")
            self.assertEqual(headers["Cache-Control"], "no-store")
            code, headers, raw = self.request(
                port, "GET", "/api/printers/ghost/camera/snapshot",
                headers=self.auth())
            self.assertEqual(code, 404)
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertFalse(json.loads(raw)["ok"])

    def test_readyz_follows_the_readiness_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port = self.start(
                tmp, readiness_func=lambda: {"ready": True, "components": {}})
            code, _, raw = self.request(port, "GET", "/readyz",
                                        headers=self.auth())
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(raw)["ready"])
        with tempfile.TemporaryDirectory() as tmp:
            _, port = self.start(
                tmp, readiness_func=lambda: {"ready": False,
                                             "components": {"spoolman": {}}})
            code, _, raw = self.request(port, "GET", "/readyz",
                                        headers=self.auth())
            self.assertEqual(code, 503)
            self.assertFalse(json.loads(raw)["ready"])

    def test_unknown_routes_are_404_and_name_what_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp)
            pid = self.printer_id(httpd)
            for path in ("/api/bogus",):
                code, _, raw = self.request(port, "GET", path,
                                            headers=self.auth())
                self.assertEqual(code, 404, path)
                payload = json.loads(raw)
                self.assertFalse(payload["ok"], path)
                self.assertIn("/api/status", payload["reason"], path)
            # Under /api/printers/ the 404 names the routes that live there.
            for path in ("/api/printers/", f"/api/printers/{pid}/bogus",
                         f"/api/printers/{pid}/status/extra"):
                code, _, raw = self.request(port, "GET", path,
                                            headers=self.auth())
                self.assertEqual(code, 404, path)
                payload = json.loads(raw)
                self.assertFalse(payload["ok"], path)
                self.assertIn("/api/printers/<id>/status",
                              payload["reason"], path)
            # A browser navigation to nowhere gets the themed refusal.
            code, headers, body = self.get(
                port, "/nope", headers=self.auth(Accept="text/html"))
            self.assertEqual(code, 404)
            self.assertIn("text/html", headers["Content-Type"])
            self.assertIn("No page lives at this address.", body)

    def test_data_and_wrapped_refuse_by_name_while_the_gate_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Explicitly off (stage 5: on by default, but an explicit
            # false is never overridden; data_enabled follows it).
            _, port = self.start(tmp, "timelapse:\n  wrapped_enabled: false\n")
            for path, key in (("/data", "data_enabled"),
                              ("/wrapped", "wrapped_enabled"),
                              ("/api/data", "data_enabled"),
                              ("/api/wrapped", "wrapped_enabled"),
                              ("/wrapped/video-file?year=2026", "wrapped_enabled")):
                code, _, raw = self.request(port, "GET", path,
                                            headers=self.auth())
                self.assertEqual(code, 403, path)
                payload = json.loads(raw)
                self.assertFalse(payload["ok"], path)
                self.assertIn(key, payload["reason"], path)

    def test_data_and_wrapped_serve_pages_and_json_when_the_gate_is_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port = self.start(tmp, "timelapse:\n  wrapped_enabled: true\n")
            code, _, raw = self.request(port, "GET", "/api/data",
                                        headers=self.auth())
            self.assertEqual(code, 200)
            payload = json.loads(raw)
            self.assertIn("total_prints", payload)
            self.assertIn("counts_by_status", payload)
            code, _, raw = self.request(port, "GET", "/api/wrapped",
                                        headers=self.auth())
            self.assertEqual(code, 200)
            payload = json.loads(raw)
            self.assertTrue(payload["ok"])
            self.assertIn("headline", payload)
            # A non-whole year is a clean 400 on page and API alike.
            for path in ("/api/data?year=abc", "/api/wrapped?year=abc"):
                code, _, raw = self.request(port, "GET", path,
                                            headers=self.auth())
                self.assertEqual(code, 400, path)
                self.assertIn("whole number", json.loads(raw)["reason"], path)
            # The video file route: no year is a 400, an uncompiled year a 404.
            code, _, raw = self.request(port, "GET", "/wrapped/video-file",
                                        headers=self.auth())
            self.assertEqual(code, 400)
            code, _, raw = self.request(port, "GET",
                                        "/wrapped/video-file?year=2026",
                                        headers=self.auth())
            self.assertEqual(code, 404)
            self.assertIn("no compiled Wrapped video",
                          json.loads(raw)["reason"])

    def test_wrapped_posts_need_the_form_token_and_a_whole_year(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp, "timelapse:\n  wrapped_enabled: true\n")
            # A claimed install: the page's token posts only with a session.
            session_id = "wrapped-test-session"
            httpd.sessions[session_id] = time.time() + 3600
            cookie = {"Cookie": f"{server.SESSION_COOKIE}={session_id}"}
            _, _, page = self.get(port, "/wrapped", headers=cookie)
            match = re.search(rf'name="{pages.CSRF_FIELD}" value="([^"]+)"',
                              page)
            self.assertIsNotNone(match)
            form_token = match.group(1)
            form_headers = {"Content-Type":
                            "application/x-www-form-urlencoded", **cookie}
            for path in ("/wrapped/video", "/wrapped/share"):
                code, _, _ = self.request(
                    port, "POST", path,
                    urlencode({pages.CSRF_FIELD: form_token, "year": "2026"}),
                    {"Content-Type": "application/x-www-form-urlencoded"})
                self.assertEqual(code, 403, path)  # the token without a session
                code, _, _ = self.request(
                    port, "POST", path, urlencode({"year": "2026"}),
                    form_headers)
                self.assertEqual(code, 403, path)
                code, _, body = self.request(
                    port, "POST", path,
                    urlencode({pages.CSRF_FIELD: form_token, "year": "abc"}),
                    form_headers)
                self.assertEqual(code, 400, path)
                self.assertIn("whole number", body.decode(), path)


class ControlRouteHttpTests(WebServerCase):
    """The control/spools route plumbing itself, over real HTTP."""

    def test_controls_off_refuses_every_verb_and_sends_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp)  # controls_enabled defaults off
            pid = self.printer_id(httpd)
            for verb in CONTROL_VERBS:
                code, _, raw = self.request(
                    port, "POST", f"/api/printers/{pid}/{verb}",
                    b'{"confirm": true}',
                    self.auth(**{"Content-Type": "application/json"}))
                self.assertEqual(code, 403, verb)
                payload = json.loads(raw)
                self.assertFalse(payload["ok"], verb)
                self.assertIn("controls_enabled", payload["reason"], verb)

    def test_body_plumbing_oversized_bad_length_and_charset(self):
        seen = []

        def fake_control(printer_id, verb, body):
            seen.append((printer_id, verb, body))
            return 200, {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp, printer_control_func=fake_control)
            pid = self.printer_id(httpd)
            path = f"/api/printers/{pid}/pause"
            headers = self.auth(**{"Content-Type": "application/json"})
            # Oversized: the route's own 4096-byte cap, a clean 400.
            code, _, raw = self.request(port, "POST", path,
                                        b'{"x": "' + b"a" * 5000 + b'"}',
                                        headers)
            self.assertEqual(code, 400)
            self.assertIn("oversized", json.loads(raw)["reason"])
            # A charset parameter on the JSON content type is accepted.
            code, _, _ = self.request(
                port, "POST", path, b"{}",
                self.auth(**{"Content-Type":
                             "application/json; charset=utf-8"}))
            self.assertEqual(code, 200)
            # A non-numeric Content-Length is a clean 400, not a crash.
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.putrequest("POST", path)
                conn.putheader("Content-Type", "application/json")
                conn.putheader("Content-Length", "banana")
                conn.putheader(server.TOKEN_HEADER, TOKEN)
                conn.endheaders()
                response = conn.getresponse()
                self.assertEqual(response.status, 400)
                self.assertIn("oversized",
                              json.loads(response.read())["reason"])
            finally:
                conn.close()
            # The control surface is POST-only: a GET there is the status
            # route's 404, naming the GET routes that do exist.
            code, _, raw = self.request(port, "GET", path, headers=self.auth())
            self.assertEqual(code, 404)
            self.assertIn("camera/snapshot", json.loads(raw)["reason"])
        self.assertEqual(seen, [(pid, "pause", {})])

    def test_spools_route_oversized_body_is_a_clean_400(self):
        seen = []

        def fake_action(printer_id, action, body):
            seen.append((printer_id, action, body))
            return 200, {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp, spools_action_func=fake_action)
            pid = self.printer_id(httpd)
            code, _, raw = self.request(
                port, "POST", f"/api/printers/{pid}/spools/assign",
                b'{"x": "' + b"a" * 70000 + b'"}',
                self.auth(**{"Content-Type": "application/json"}))
            self.assertEqual(code, 400)
            payload = json.loads(raw)
            self.assertFalse(payload["ok"])
            self.assertIn("Nothing changed.", payload["reason"])
        self.assertEqual(seen, [])


class RobustnessTests(WebServerCase):
    """Malformed input and load never produce a stack trace or a hang."""

    def test_non_dict_json_bodies_never_500_on_any_control_verb(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp, "  controls_enabled: true\n")
            pid = self.printer_id(httpd)
            headers = self.auth(**{"Content-Type": "application/json"})
            for verb in CONTROL_VERBS:
                for body in (b"[1, 2]", b'"x"', b"42", b"null"):
                    code, _, raw = self.request(
                        port, "POST", f"/api/printers/{pid}/{verb}",
                        body, headers)
                    # Never the handler's unhandled-exception 500: the
                    # designed answers are a 400 refusal (a verb that needs
                    # fields or a confirm) or the printer read's own 502
                    # for this printer, which names no Moonraker URL.
                    self.assertNotEqual(code, 500, (verb, body))
                    self.assertIn(code, (400, 502), (verb, body))
                    self.assertFalse(json.loads(raw)["ok"], (verb, body))

    def test_form_posts_reject_oversized_and_wrong_type_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port = self.start(tmp)
            code, _, raw = self.request(
                port, "POST", "/login", "x=" + "a" * 70000,
                {"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(code, 413)
            self.assertFalse(json.loads(raw)["ok"])
            code, _, raw = self.request(
                port, "POST", "/login", b"{}",
                {"Content-Type": "application/json"})
            self.assertEqual(code, 415)
            self.assertFalse(json.loads(raw)["ok"])

    def test_a_broken_snapshot_is_a_clean_500_not_a_dropped_connection(self):
        def boom():
            raise RuntimeError("snapshot exploded")

        with tempfile.TemporaryDirectory() as tmp:
            _, port = self.start(tmp, snapshot_func=boom)
            code, _, raw = self.request(port, "GET", "/api/status",
                                        headers=self.auth())
            self.assertEqual(code, 500)
            payload = json.loads(raw)
            self.assertFalse(payload["ok"])
            # Generic: the exception is logged, never echoed.
            self.assertEqual(payload["reason"], server.INTERNAL_ERROR_REASON)
            self.assertNotIn(b"exploded", raw)
            # A browser gets the themed page; the exception text stays
            # out. The walkthrough's first-run redirect is stubbed at its
            # own seam so the request reaches the snapshot render.
            with mock.patch.object(server.walkthrough_module, "offer_once",
                                   return_value=False):
                code, headers, body = self.get(
                    port, "/", headers=self.auth(Accept="text/html"))
            self.assertEqual(code, 500)
            self.assertIn("text/html", headers["Content-Type"])
            self.assertIn("Something went wrong rendering this page.", body)
            self.assertNotIn("snapshot exploded", body)

    def test_concurrent_mixed_requests_all_answer_correctly(self):
        def fake_status(printer_id):
            return 200, {"ok": True, "id": printer_id,
                         "status": {"state": "printing", "progress": 0.5}}

        def fake_camera(printer_id):
            return 200, JPEG, "image/jpeg"

        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(
                tmp, printer_status_func=fake_status,
                camera_snapshot_func=fake_camera,
                spools_view_func=lambda pid: (200, spools_view(pid)))
            pid = self.printer_id(httpd)
            plan = [("/api/status", 200), ("/config", 200),
                    (f"/api/printers/{pid}/status", 200),
                    (f"/api/printers/{pid}/camera/snapshot", 200),
                    (f"/spools/{pid}", 200)]
            failures = []

            def worker(rounds):
                try:
                    for _ in range(rounds):
                        for path, expected in plan:
                            code, _, raw = self.request(
                                port, "GET", path, headers=self.auth())
                            if code != expected:
                                failures.append((path, code))
                            elif path.endswith("camera/snapshot") \
                                    and raw != JPEG:
                                failures.append((path, "bad jpeg"))
                except Exception as exc:  # collected, asserted below
                    failures.append(("worker", repr(exc)))

            with mock.patch.object(server.bot_health, "read",
                                   return_value=fresh_heartbeat()):
                threads = [threading.Thread(target=worker, args=(3,))
                           for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=60)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(failures, [])
            code, _, _ = self.request(port, "GET", "/api/status",
                                      headers=self.auth())
            self.assertEqual(code, 200)


class StartupConfigTests(WebServerCase):
    """Start rules over the real factory: bind, env names, open mode."""

    def test_port_bind_conflict_is_a_named_refusal(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        taken = blocker.getsockname()[1]
        self.addCleanup(blocker.close)
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(
                tmp, "webui:\n  enabled: true\n  port: 8765\n")
            with self.assertRaises(server.WebUIServerError) as caught:
                server.make_server(data, environ={}, port=taken)
            message = str(caught.exception)
            self.assertIn("could not bind", message)
            self.assertIn(str(taken), message)
            self.assertIn("port", message)

    def test_serve_returns_2_and_prints_the_bind_refusal(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        taken = blocker.getsockname()[1]
        self.addCleanup(blocker.close)
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(
                tmp, f"webui:\n  enabled: true\n  port: {taken}\n")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = server.serve(data, environ={})
            self.assertEqual(code, 2)
            self.assertIn("could not bind", stderr.getvalue())

    def test_serve_configures_shared_logging_and_logs_the_version(self):
        # #1404: serve() joins the shared logging (with crash capture for
        # the webui role) and logs its startup line with the version,
        # like every other Print Handy process.
        from types import SimpleNamespace

        from print_handy import __version__

        fake = SimpleNamespace(server_address=("127.0.0.1", 8765),
                               serve_forever=lambda: None,
                               server_close=lambda: None)
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(
                tmp, "webui:\n  enabled: true\n  port: 8765\n")
            with mock.patch.object(server, "make_server", return_value=fake), \
                    mock.patch.object(server, "start_web_alerts", lambda srv: None), \
                    mock.patch.object(server.logs, "configure") as configure, \
                    mock.patch.object(server, "log") as log:
                code = server.serve(data, environ={})
        self.assertEqual(code, 0)
        configure.assert_called_once()
        self.assertEqual(configure.call_args.kwargs.get("role"), "webui")
        self.assertEqual(configure.call_args.args[0], data.get("logging"))
        startup = [call for call in log.info.call_args_list
                   if call.args and "webui starting" in str(call.args[0])]
        self.assertTrue(startup)
        self.assertIn(__version__, startup[0].args)

    def test_custom_access_token_env_name_is_honored(self):
        environ = {"MY_WEBUI_TOKEN": TOKEN, "WEBUI_ACCESS_TOKEN": WRONG_TOKEN}
        with tempfile.TemporaryDirectory() as tmp:
            _, port = self.start(
                tmp, "  access_token_env: MY_WEBUI_TOKEN\n", environ=environ)
            code, _, _ = self.request(port, "GET", "/api/status",
                                      headers=self.auth())
            self.assertEqual(code, 200)
            code, _, _ = self.request(
                port, "GET", "/api/status",
                headers={server.TOKEN_HEADER: WRONG_TOKEN})
            self.assertEqual(code, 401)

    def test_require_auth_false_opens_routes_even_with_a_token_set(self):
        seen = []

        def fake_control(printer_id, verb, body):
            seen.append((printer_id, verb, body))
            return 200, {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp, "  require_auth: false\n",
                                     printer_control_func=fake_control)
            pid = self.printer_id(httpd)
            code, _, _ = self.request(port, "GET", "/api/status")
            self.assertEqual(code, 200)
            code, _, _ = self.request(
                port, "POST", f"/api/printers/{pid}/pause", b"{}",
                {"Content-Type": "application/json"})
            self.assertEqual(code, 200)
        self.assertEqual(seen, [(pid, "pause", {})])

    def test_ephemeral_port_zero_binds_a_real_reachable_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp)
            self.assertGreater(port, 0)
            self.assertEqual(httpd.server_address[1], port)
            code, _, _ = self.request(port, "GET", "/api/status",
                                      headers=self.auth())
            self.assertEqual(code, 200)


if __name__ == "__main__":
    unittest.main()
