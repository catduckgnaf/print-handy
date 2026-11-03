"""#813: the shared shell, the theme, and the first-run walkthrough.

The walkthrough is owed only by installs that resolved access through
the setup page (webui_access.json exists) and have not finished it;
every other install — environment token, loopback, require_auth =
false — must never see it. Its steps write through the existing config
path, so these tests assert on the real settings file afterwards.

Offline: temp settings and state folder, the server on an ephemeral
port, a fabricated status snapshot.
"""
import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from print_handy import settings
from print_handy.webui import access, pages, server, shell, walkthrough
from print_handy.webui import config as config_api

FORM = {"Content-Type": "application/x-www-form-urlencoded"}
HTML = {"Accept": "text/html"}

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


class WalkthroughServerTests(unittest.TestCase):
    def start(self, tmp, *, setup_done=False, host="0.0.0.0", environ=None, extra=""):
        local = Path(tmp) / "local.yaml"
        local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n"
                         "webui:\n  enabled: true\n"
                         f"  host: {host}\n  port: 8765\n" + extra)
        environ = {"PRINT_HANDY_CONFIG": str(local), **(environ or {})}
        data = settings.load(local=local, environ=environ)
        if setup_done:
            access.save_open(data, True)
        httpd = server.make_server(data, environ=environ, host=host, port=0,
                                   snapshot_func=lambda: FAKE_SNAPSHOT)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        return httpd, httpd.server_address[1], data, local, environ

    def request(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return (response.status, {k.lower(): v for k, v in response.getheaders()},
                    response.read().decode())
        finally:
            conn.close()

    def step_form(self, environ, csrf, step_id, action="continue", **overrides):
        """The pairs a browser would submit for one step: every control's
        current value (hidden inputs included), plus the overrides."""
        pairs = [("step", step_id), ("action", action), (pages.CSRF_FIELD, csrf)]
        by_id = {item.id: item for item in config_api.read_fields(environ=environ)}
        for field_id in walkthrough.STEP_FIELDS[step_id]:
            item = by_id[field_id]
            if item.overridden:
                continue
            value = item.value
            if item.field.kind == "toggle":
                if value:
                    pairs.append((field_id, "true"))
            elif isinstance(value, (list, tuple)):
                pairs += [(field_id, str(part)) for part in value]
            elif isinstance(value, float) and value == int(value):
                pairs.append((field_id, str(int(value))))
            else:
                pairs.append((field_id, "" if value is None else str(value)))
        for field_id, value in overrides.items():
            pairs = [(k, v) for k, v in pairs if k != field_id]
            if value is not None:
                pairs.append((field_id, value))
        return pairs

    def post_step(self, port, environ, csrf, step_id, **kwargs):
        return self.request(port, "POST", "/walkthrough",
                            body=urlencode(self.step_form(environ, csrf, step_id, **kwargs)),
                            headers=FORM)

    # -- the gate ---------------------------------------------------------

    def test_root_browser_navigation_redirects_to_the_walkthrough(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port, _, _, _ = self.start(tmp, setup_done=True)
            code, headers, _ = self.request(port, "GET", "/", headers=HTML)
            self.assertEqual((code, headers["location"]), (303, "/walkthrough"))

    def test_root_for_non_browsers_serves_status_with_the_banner(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port, _, _, _ = self.start(tmp, setup_done=True)
            code, _, body = self.request(port, "GET", "/")
            self.assertEqual(code, 200)
            self.assertIn("Finish setting up Print Handy", body)
            self.assertIn('href="/walkthrough"', body)

    def test_the_gate_touches_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port, _, _, _ = self.start(tmp, setup_done=True)
            self.assertEqual(self.request(port, "GET", "/api/status")[0], 200)
            self.assertEqual(self.request(port, "GET", "/config")[0], 200)
            self.assertEqual(self.request(port, "GET", "/designs")[0], 200)

    def test_no_gate_and_no_banner_without_an_access_file(self):
        # A loopback install never ran the setup page: no access file,
        # no walkthrough, whatever the browser asks for.
        with tempfile.TemporaryDirectory() as tmp:
            _, port, _, _, _ = self.start(tmp, host="127.0.0.1")
            code, _, body = self.request(port, "GET", "/", headers=HTML)
            self.assertEqual(code, 200)
            self.assertNotIn("Finish setting up", body)
            code, headers, _ = self.request(port, "GET", "/walkthrough")
            self.assertEqual((code, headers["location"]), (303, "/"))

    def test_pending_rule_follows_the_access_file_and_the_done_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, data, _, _ = self.start(tmp)
            self.assertFalse(walkthrough.pending(data))
            access.save_open(data, True)
            self.assertTrue(walkthrough.pending(data))
            walkthrough.save_state(data, {"step": 4, "done": True})
            self.assertFalse(walkthrough.pending(data))

    # -- the steps ----------------------------------------------------------

    def test_first_step_is_the_printer_with_a_read_only_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port, _, _, _ = self.start(tmp, setup_done=True)
            code, _, body = self.request(port, "GET", "/walkthrough")
            self.assertEqual(code, 200)
            self.assertIn("First boot", body)
            self.assertIn('<li class="current">1. Printer</li>', body)
            self.assertIn("Printer name", body)  # shown, with no input of its own
            self.assertNotIn('name="printer.name"', body)
            self.assertIn('name="printer.model"', body)
            self.assertIn('name="services.moonraker_url"', body)  # AD5X connects by Moonraker

    def test_resume_opens_the_saved_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port, data, _, _ = self.start(tmp, setup_done=True)
            walkthrough.save_state(data, {"step": 2})
            code, _, body = self.request(port, "GET", "/walkthrough")
            self.assertEqual(code, 200)
            self.assertIn('<li class="current">3. Camera</li>', body)
            self.assertIn('name="services.camera_source"', body)

    def test_continue_writes_through_and_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, data, local, environ = self.start(tmp, setup_done=True)
            code, headers, _ = self.post_step(
                port, environ, httpd.form_token, "printer",
                **{"services.moonraker_url": "http://printer.example:7125"})
            self.assertEqual((code, headers["location"]), (303, "/walkthrough"))
            reloaded = settings.load(local=local, environ={})
            self.assertEqual(reloaded["services"]["moonraker_url"],
                             "http://printer.example:7125")
            self.assertEqual(walkthrough.load_state(data)["step"], 1)

    def test_an_environment_overridden_field_is_refused(self):
        env = {"PRINT_HANDY__SERVICES__MOONRAKER_URL": "http://env.example:7125"}
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, _, local, environ = self.start(tmp, setup_done=True, environ=env)
            code, _, body = self.request(port, "GET", "/walkthrough")
            self.assertEqual(code, 200)
            self.assertIn("environment override", body)  # the badge on the disabled row
            code, _, _ = self.post_step(
                port, environ, httpd.form_token, "printer",
                **{"services.moonraker_url": "http://other.example:7125"})
            self.assertEqual(code, 303)
            reloaded = settings.load(local=local, environ={})
            self.assertNotEqual(reloaded["services"].get("moonraker_url"),
                                "http://other.example:7125")

    def test_skip_advances_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, data, local, environ = self.start(tmp, setup_done=True)
            before = settings.load(local=local, environ={})["services"].get("moonraker_url")
            code, _, _ = self.post_step(
                port, environ, httpd.form_token, "printer", action="skip",
                **{"services.moonraker_url": "http://printer.example:7125"})
            self.assertEqual(code, 303)
            after = settings.load(local=local, environ={})["services"].get("moonraker_url")
            self.assertEqual(after, before)
            self.assertEqual(walkthrough.load_state(data)["step"], 1)

    def test_back_returns_to_the_previous_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, data, _, environ = self.start(tmp, setup_done=True)
            walkthrough.save_state(data, {"step": 1})
            code, _, _ = self.post_step(port, environ, httpd.form_token, "chat", action="back")
            self.assertEqual(code, 303)
            self.assertEqual(walkthrough.load_state(data)["step"], 0)

    def test_providers_step_lists_sites_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, data, _, environ = self.start(tmp, setup_done=True)
            walkthrough.save_state(data, {"step": 3})
            code, _, body = self.request(port, "GET", "/walkthrough")
            self.assertEqual(code, 200)
            self.assertIn("MakerWorld", body)
            self.assertIn("not connected", body)
            self.assertIn("Printables", body)
            self.assertNotIn('name="services.', body)  # no fields on this step
            code, headers, _ = self.post_step(port, environ, httpd.form_token, "providers")
            self.assertEqual((code, headers["location"]), (303, "/walkthrough"))
            self.assertEqual(walkthrough.load_state(data)["step"], 4)

    def test_finish_on_review_marks_done_and_lifts_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, data, _, environ = self.start(tmp, setup_done=True)
            walkthrough.save_state(data, {"step": 4})
            code, _, body = self.request(port, "GET", "/walkthrough")
            self.assertEqual(code, 200)
            self.assertIn("Model sites", body)  # the review's summary rows
            code, headers, _ = self.post_step(port, environ, httpd.form_token,
                                              "review", action="finish")
            self.assertEqual((code, headers["location"]), (303, "/"))
            self.assertTrue(walkthrough.load_state(data)["done"])
            code, _, body = self.request(port, "GET", "/", headers=HTML)
            self.assertEqual(code, 200)
            self.assertNotIn("Finish setting up", body)
            code, headers, _ = self.request(port, "GET", "/walkthrough")
            self.assertEqual((code, headers["location"]), (303, "/"))

    def test_skip_setup_marks_done_as_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, data, _, environ = self.start(tmp, setup_done=True)
            code, headers, _ = self.post_step(port, environ, httpd.form_token,
                                              "printer", action="skip_setup")
            self.assertEqual((code, headers["location"]), (303, "/"))
            state = walkthrough.load_state(data)
            self.assertTrue(state["done"])
            self.assertTrue(state["skipped"])

    def test_a_step_post_needs_the_form_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port, _, _, environ = self.start(tmp, setup_done=True)
            code, _, _ = self.post_step(port, environ, "not-the-token", "printer")
            self.assertEqual(code, 403)


class ThemeTests(unittest.TestCase):
    def start(self, tmp, *, setup_done=False, host="0.0.0.0", environ=None):
        local = Path(tmp) / "local.yaml"
        local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n"
                         "webui:\n  enabled: true\n"
                         f"  host: {host}\n  port: 8765\n")
        environ = {"PRINT_HANDY_CONFIG": str(local), **(environ or {})}
        data = settings.load(local=local, environ=environ)
        if setup_done:
            access.save_open(data, True)
        httpd = server.make_server(data, environ=environ, host=host, port=0,
                                   snapshot_func=lambda: FAKE_SNAPSHOT)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        return httpd.server_address[1]

    def request(self, port, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return (response.status, {k.lower(): v for k, v in response.getheaders()},
                    response.read().decode())
        finally:
            conn.close()

    def test_pages_default_to_dark_with_the_shared_stylesheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = self.start(tmp, host="127.0.0.1")
            code, _, body = self.request(port, "GET", "/config")
            self.assertEqual(code, 200)
            self.assertIn('<html lang="en"><head>', body)  # no data-theme: dark
            self.assertIn("--color-bg: #12110f", body)
            self.assertIn('<nav class="pills"', body)

    def test_the_light_cookie_switches_the_theme(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = self.start(tmp, host="127.0.0.1")
            code, _, body = self.request(port, "GET", "/config",
                                         headers={"Cookie": "ph-theme=light"})
            self.assertEqual(code, 200)
            self.assertIn('<html lang="en" data-theme="light">', body)

    def test_theme_route_sets_the_cookie_and_goes_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = self.start(tmp, host="127.0.0.1")
            code, headers, _ = self.request(port, "GET", "/theme?set=light&back=/config")
            self.assertEqual((code, headers["location"]), (303, "/config"))
            self.assertTrue(headers["set-cookie"].startswith("ph-theme=light"))
            self.assertIn("SameSite=Lax", headers["set-cookie"])
            self.assertIn("Path=/", headers["set-cookie"])

    def test_theme_route_rejects_bad_values_and_foreign_backs(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = self.start(tmp, host="127.0.0.1")
            self.assertEqual(self.request(port, "GET", "/theme?set=sepia")[0], 400)
            code, headers, _ = self.request(
                port, "GET", "/theme?set=dark&back=https://evil.example/")
            self.assertEqual((code, headers["location"]), (303, "/"))

    def test_theme_route_is_unreachable_before_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = self.start(tmp)  # LAN bind, nothing chosen: setup mode
            code, headers, _ = self.request(port, "GET", "/theme?set=light", headers=HTML)
            self.assertEqual((code, headers["location"]), (303, "/setup"))
            self.assertEqual(self.request(port, "GET", "/theme?set=light")[0], 503)

    def test_theme_route_needs_auth_on_a_token_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = self.start(tmp, environ={"WEBUI_ACCESS_TOKEN": "sekrit"})
            self.assertEqual(self.request(port, "GET", "/theme?set=light")[0], 401)
            code, _, _ = self.request(port, "GET", "/theme?set=light",
                                      headers={server.TOKEN_HEADER: "sekrit"})
            self.assertEqual(code, 303)

    def test_shell_function_reads_the_cookie(self):
        self.assertEqual(shell.theme_from_cookies("ph-theme=light"), "light")
        self.assertEqual(shell.theme_from_cookies("ph-theme=dark"), "dark")
        self.assertEqual(shell.theme_from_cookies("ph-theme=bogus"), "dark")
        self.assertEqual(shell.theme_from_cookies(""), "dark")
        self.assertEqual(shell.theme_from_cookies(None), "dark")


class StatusOverallCardTests(unittest.TestCase):
    """The overall card derives only from the snapshot's own fields."""

    def test_all_good_with_no_warnings(self):
        body = server.render_status_html(FAKE_SNAPSHOT)
        self.assertIn("All good", body)
        self.assertIn("all checks ok", body)
        self.assertIn("<h1>Print Handy status</h1>", body)

    def test_attention_with_warning_and_failure_counts(self):
        snap = {**FAKE_SNAPSHOT, "ok": False, "doctor": [
            {"label": "a", "status": "warn", "detail": "x"},
            {"label": "b", "status": "warn", "detail": "y"},
            {"label": "c", "status": "fail", "detail": "z"},
            {"label": "d", "status": "ok", "detail": "fine"}]}
        body = server.render_status_html(snap)
        self.assertIn("Attention needed", body)
        self.assertIn("2 warnings", body)
        self.assertIn("1 failing", body)

    def test_the_banner_is_opt_in(self):
        self.assertNotIn("Finish setting up", server.render_status_html(FAKE_SNAPSHOT))
        body = server.render_status_html(FAKE_SNAPSHOT, walkthrough_pending=True)
        self.assertIn("Finish setting up Print Handy", body)


if __name__ == "__main__":
    unittest.main()
