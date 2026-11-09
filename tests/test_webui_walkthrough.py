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
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
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

    def test_loopback_install_is_offered_the_walkthrough_once(self):
        # #875: a loopback install never ran the setup page but still owes
        # the walkthrough. It is offered once; after that only the banner
        # reminds, so an existing install is never trapped in it.
        with tempfile.TemporaryDirectory() as tmp:
            _, port, _, _, _ = self.start(tmp, host="127.0.0.1")
            code, headers, _ = self.request(port, "GET", "/", headers=HTML)
            self.assertEqual((code, headers["location"]), (303, "/walkthrough"))
            code, _, body = self.request(port, "GET", "/", headers=HTML)
            self.assertEqual(code, 200)
            self.assertIn("Finish setting up", body)
            code, _, _ = self.request(port, "GET", "/walkthrough")
            self.assertEqual(code, 200)

    def test_pending_rule_follows_the_access_file_and_the_done_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, data, _, _ = self.start(tmp)
            self.assertTrue(walkthrough.pending(data))  # #875: no access file needed
            access.save_open(data, True)
            self.assertTrue(walkthrough.pending(data))
            walkthrough.save_state(data, {"step": 4, "done": True})
            self.assertFalse(walkthrough.pending(data))

    # -- the steps ----------------------------------------------------------

    def test_first_step_is_the_printer_with_an_editable_name(self):
        # A legacy install (no declared roster): the name is the catalog's
        # printer.name field, rendered and posted like every other field.
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, _, local, environ = self.start(tmp, setup_done=True)
            code, _, body = self.request(port, "GET", "/walkthrough")
            self.assertEqual(code, 200)
            self.assertIn("First boot", body)
            self.assertIn('<li class="current">1. Printer</li>', body)
            self.assertIn('name="printer.name"', body)
            self.assertIn('name="printer.model"', body)
            self.assertIn('name="services.moonraker_url"', body)  # AD5X connects by Moonraker
            code, headers, _ = self.post_step(
                port, environ, httpd.form_token, "printer",
                **{"printer.name": "Bench Printer"})
            self.assertEqual((code, headers["location"]), (303, "/walkthrough"))
            reloaded = settings.load(local=local, environ={})
            self.assertEqual(reloaded["printer"]["name"], "Bench Printer")
            self.assertEqual(settings.printers(reloaded)[0]["name"], "Bench Printer")

    def test_printer_step_asks_for_spoolman_and_saves_it(self):
        # The one service chat setup asked about that this walkthrough
        # used to skip: the Spoolman URL rides the Printer step and the
        # Review step shows it.
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, _, local, environ = self.start(tmp, setup_done=True)
            code, _, body = self.request(port, "GET", "/walkthrough")
            self.assertEqual(code, 200)
            self.assertIn('name="services.spoolman_url"', body)
            code, headers, _ = self.post_step(
                port, environ, httpd.form_token, "printer",
                **{"services.spoolman_url": "http://spoolman.example:7912"})
            self.assertEqual((code, headers["location"]), (303, "/walkthrough"))
            reloaded = settings.load(local=local, environ={})
            self.assertEqual(reloaded["services"]["spoolman_url"],
                             "http://spoolman.example:7912")
            self.assertIn("services.spoolman_url", walkthrough.REVIEW_FIELDS)

    def test_chat_step_does_not_ask_for_bot_mode(self):
        # Since the Hermes removal the only legal bot.mode is
        # "standalone": the Chat step must not offer a one-answer field.
        self.assertNotIn("bot.mode", walkthrough.STEP_FIELDS["chat"])
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, data, _, _ = self.start(tmp, setup_done=True)
            walkthrough.save_state(data, {"step": 1})
            code, _, body = self.request(port, "GET", "/walkthrough")
            self.assertEqual(code, 200)
            self.assertIn('name="bot.platform"', body)
            self.assertNotIn('name="bot.mode"', body)

    def test_a_bambu_install_gets_its_connection_fields_on_the_printer_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, _, local, environ = self.start(
                tmp, setup_done=True, extra="printer:\n  model: bambulab.a1\n")
            code, _, body = self.request(port, "GET", "/walkthrough")
            self.assertEqual(code, 200)
            self.assertIn('id="f-bambu.lan_host"', body)
            self.assertIn('id="f-bambu.lan_serial"', body)  # the pair the validator requires
            self.assertNotIn('id="f-services.moonraker_url"', body)
            code, headers, _ = self.post_step(
                port, environ, httpd.form_token, "printer",
                **{"bambu.lan_host": "192.168.1.60",
                   "bambu.lan_serial": "00M00A000000000"})
            self.assertEqual((code, headers["location"]), (303, "/walkthrough"))
            reloaded = settings.load(local=local, environ={})
            self.assertEqual(reloaded["bambu"]["lan_host"], "192.168.1.60")
            self.assertEqual(reloaded["bambu"]["lan_serial"], "00M00A000000000")

    def test_a_saved_model_change_refreshes_the_connection_fields(self):
        # Field visibility follows the model that was saved, not the one
        # the server started with: switch Moonraker -> OctoPrint, come
        # Back, and the step must offer the OctoPrint URL.
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, _, _, environ = self.start(tmp, setup_done=True)
            code, _, body = self.request(port, "GET", "/walkthrough")
            self.assertIn('id="f-services.moonraker_url"', body)  # AD5X: Moonraker
            code, headers, _ = self.post_step(
                port, environ, httpd.form_token, "printer",
                **{"printer.model": "octoprint.ender3"})
            self.assertEqual((code, headers["location"]), (303, "/walkthrough"))
            code, _, _ = self.post_step(port, environ, httpd.form_token,
                                        "chat", action="back")
            self.assertEqual(code, 303)
            code, _, body = self.request(port, "GET", "/walkthrough")
            self.assertEqual(code, 200)
            self.assertIn('id="f-services.octoprint_url"', body)
            self.assertNotIn('id="f-services.moonraker_url"', body)

    def test_status_page_shows_the_default_printers_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port, _, _, _ = self.start(tmp, host="127.0.0.1")
            live = (200, {"ok": True, "id": "ad5x", "status": {
                "state": "printing", "filename": "cube.gcode", "progress": 0.5,
                "layer": 4, "total_layers": 9, "remaining_min": 12}})
            with mock.patch.object(server.status_module, "printer_status",
                                   return_value=live):
                code, _, body = self.request(port, "GET", "/")
            self.assertEqual(code, 200)
            self.assertIn("cube.gcode", body)
            self.assertIn("Layer 4 of 9", body)
            self.assertIn("about 12 min left", body)

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
            walkthrough.save_state(data, {"step": 4})
            code, _, body = self.request(port, "GET", "/walkthrough")
            self.assertEqual(code, 200)
            self.assertIn("MakerWorld", body)
            self.assertIn("not connected", body)
            self.assertIn("Printables", body)
            self.assertNotIn('name="services.', body)  # no fields on this step
            code, headers, _ = self.post_step(port, environ, httpd.form_token, "providers")
            self.assertEqual((code, headers["location"]), (303, "/walkthrough"))
            self.assertEqual(walkthrough.load_state(data)["step"], 5)

    def login_step(self, port, csrf, **fields):
        body = urlencode({"step": "login", "action": "continue", pages.CSRF_FIELD: csrf, **fields})
        return self.request(port, "POST", "/walkthrough", body=body, headers=FORM)

    def test_login_step_sets_a_username_and_password_and_signs_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, data, _, _ = self.start(tmp, setup_done=True)
            walkthrough.save_state(data, {"step": 3})
            code, _, body = self.request(port, "GET", "/walkthrough")
            self.assertEqual(code, 200)
            self.assertIn('autocomplete="username"', body)
            self.assertIn("This Web UI is open", body)
            code, headers, _ = self.login_step(port, httpd.form_token, username="catduck",
                                               password="long enough pw",
                                               password_again="long enough pw")
            self.assertEqual((code, headers["location"]), (303, "/walkthrough"))
            self.assertIn("set-cookie", headers)  # the chooser stays signed in
            self.assertEqual(httpd.access.mode, access.PASSWORD)
            self.assertEqual(httpd.access.username, "catduck")
            self.assertEqual(walkthrough.load_state(data)["step"], 4)
            cookie = headers["set-cookie"].split(";", 1)[0]
            self.assertEqual(self.request(port, "GET", "/api/status")[0], 401)
            self.assertEqual(self.request(port, "GET", "/api/status",
                                          headers={"Cookie": cookie})[0], 200)

    def test_login_step_left_empty_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, data, _, _ = self.start(tmp, setup_done=True)
            walkthrough.save_state(data, {"step": 3})
            code, headers, _ = self.login_step(port, httpd.form_token)
            self.assertEqual(code, 303)
            self.assertNotIn("set-cookie", headers)
            self.assertEqual(httpd.access.mode, access.OPEN)
            self.assertEqual(walkthrough.load_state(data)["step"], 4)

    def test_login_step_refuses_mismatched_passwords_and_keeps_the_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, data, _, _ = self.start(tmp, setup_done=True)
            walkthrough.save_state(data, {"step": 3})
            code, _, body = self.login_step(port, httpd.form_token, username="catduck",
                                            password="long enough pw", password_again="other pw!")
            self.assertEqual(code, 200)
            self.assertIn("do not match", body)
            self.assertIn('value="catduck"', body)
            self.assertNotIn("long enough pw", body)
            self.assertEqual(httpd.access.mode, access.OPEN)
            self.assertEqual(walkthrough.load_state(data)["step"], 3)

    def test_login_step_can_remove_a_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, data, _, _ = self.start(tmp, setup_done=True)
            access.save_password(data, "long enough pw", "long enough pw", username="catduck")
            httpd.access = access.resolve(data, "0.0.0.0", {})
            walkthrough.save_state(data, {"step": 3})
            code, _, _ = self.login_step(port, httpd.form_token, confirm_open="yes")
            self.assertEqual(code, 303)
            self.assertEqual(httpd.access.mode, access.OPEN)

    def test_setting_a_login_rotates_the_form_token(self):
        """#880 review (P1): the form token was served to anyone who
        visited the open Web UI, and _post_authorized accepts it on its
        own — if it survived the Login step, a retained copy would keep
        authorizing protected writes under the new password, including
        removing that login again."""
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, data, _, _ = self.start(tmp, setup_done=True)
            walkthrough.save_state(data, {"step": 3})
            retained = httpd.form_token
            code, headers, _ = self.login_step(port, retained, username="catduck",
                                               password="long enough pw",
                                               password_again="long enough pw")
            self.assertEqual((code, headers["location"]), (303, "/walkthrough"))
            self.assertEqual(httpd.access.mode, access.PASSWORD)
            self.assertNotEqual(httpd.form_token, retained)
            # The retained token can no longer remove the login it predates.
            code, _, _ = self.login_step(port, retained, confirm_open="yes")
            self.assertEqual(code, 403)
            self.assertEqual(httpd.access.mode, access.PASSWORD)

    def test_removing_a_login_rotates_the_form_token(self):
        """The removal direction changes the access rule too: a token
        retained from the password era must not authorize writes once
        the Web UI is open again."""
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, data, _, _ = self.start(tmp, setup_done=True)
            access.save_password(data, "long enough pw", "long enough pw",
                                 username="catduck")
            httpd.access = access.resolve(data, "0.0.0.0", {})
            walkthrough.save_state(data, {"step": 3})
            retained = httpd.form_token
            code, _, _ = self.login_step(port, retained, confirm_open="yes")
            self.assertEqual(code, 303)
            self.assertEqual(httpd.access.mode, access.OPEN)
            self.assertNotEqual(httpd.form_token, retained)
            code, _, _ = self.login_step(port, retained)
            self.assertEqual(code, 403)

    def test_login_step_inputs_are_optional(self):
        """#880 review (P2): the walkthrough documents leaving the login
        fields empty to keep the current login, and ticking the box to
        remove it — `required` inputs make the browser refuse both
        before the form is ever sent."""
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, data, _, _ = self.start(tmp, setup_done=True)
            walkthrough.save_state(data, {"step": 3})
            code, _, body = self.request(port, "GET", "/walkthrough")
            self.assertEqual(code, 200)
            names = (pages.SETUP_USER_FIELD, pages.SETUP_PASSWORD_FIELD,
                     pages.SETUP_CONFIRM_FIELD)
            for name in names:
                tag = re.search(rf'<input[^>]*name="{name}"[^>]*>', body)
                self.assertIsNotNone(tag, name)
                self.assertNotIn("required", tag.group(0), name)

    def test_finish_on_review_marks_done_and_lifts_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, data, _, environ = self.start(tmp, setup_done=True)
            walkthrough.save_state(data, {"step": 5})
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

    # -- the "No login is set" banner must not dead-end on / ----------------

    LOGIN = walkthrough.STEPS.index(("login", "Login"))

    def assert_login_step(self, port):
        code, _, body = self.request(port, "GET", "/walkthrough")
        self.assertEqual(code, 200)
        self.assertIn('name="step" value="login"', body)

    def test_closed_walkthrough_on_unclaimed_install_serves_login(self):
        closed = {"skipped": {"step": 0, "done": True, "skipped": True, "offered": True},
                  "finished": {"step": 5, "done": True, "offered": True},
                  "finished after a reopen": {"step": 5, "done": True, "offered": True,
                                              "reopened": True}}
        for name, state in closed.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                httpd, port, data, _, _ = self.start(tmp)
                self.assertTrue(httpd.access.unclaimed)
                walkthrough.save_state(data, state)
                self.assert_login_step(port)
                self.assertEqual(walkthrough.load_state(data),
                                 {"step": self.LOGIN, "offered": True, "reopened": True})

    def test_repeat_get_after_moving_on_keeps_the_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, data, _, environ = self.start(tmp)
            walkthrough.save_state(data, {"step": 0, "done": True, "skipped": True})
            self.assert_login_step(port)
            code, headers, _ = self.post_step(port, environ, httpd.form_token, "login", action="skip")
            self.assertEqual((code, headers["location"]), (303, "/walkthrough"))
            moved_on = self.LOGIN + 1
            self.assertEqual(walkthrough.load_state(data)["step"], moved_on)
            code, _, body = self.request(port, "GET", "/walkthrough")
            self.assertEqual(code, 200)
            self.assertIn('name="step" value="providers"', body)
            self.assertEqual(walkthrough.load_state(data)["step"], moved_on)
            # pending() can be false with no done flag in the state file
            # (a completion recorded elsewhere): still no reset.
            with mock.patch.object(walkthrough, "pending", return_value=False):
                code, _, body = self.request(port, "GET", "/walkthrough")
            self.assertEqual(code, 200)
            self.assertIn('name="step" value="providers"', body)
            self.assertEqual(walkthrough.load_state(data)["step"], moved_on)

    def test_pending_false_without_done_flag_still_reopens_at_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port, data, _, _ = self.start(tmp)
            walkthrough.save_state(data, {"step": 5, "offered": True})
            with mock.patch.object(walkthrough, "pending", return_value=False):
                self.assert_login_step(port)

    def test_install_with_a_credential_still_redirects(self):
        token = {"WEBUI_ACCESS_TOKEN": "a-long-enough-test-token"}
        cases = {"saved open choice": ({"setup_done": True}, {}),
                 "access token": ({"environ": token}, {server.TOKEN_HEADER: token["WEBUI_ACCESS_TOKEN"]})}
        for name, (start, headers) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                httpd, port, data, _, _ = self.start(tmp, **start)
                self.assertFalse(httpd.access.unclaimed)
                state = {"step": 5, "done": True, "offered": True}
                walkthrough.save_state(data, state)
                code, response, _ = self.request(port, "GET", "/walkthrough", headers=headers)
                self.assertEqual((code, response["location"]), (303, "/"))
                self.assertEqual(walkthrough.load_state(data), state)

    def test_banner_wording_follows_the_bind(self):
        wide = "anyone who can reach it can use it"
        for host, says_wide in (("0.0.0.0", True), ("127.0.0.1", False)):
            with self.subTest(host), tempfile.TemporaryDirectory() as tmp:
                _, port, _, _, _ = self.start(tmp, host=host)
                code, _, body = self.request(port, "GET", "/")
                self.assertEqual(code, 200)
                self.assertIn("No login is set", body)
                self.assertEqual(wide in body, says_wide)
                self.assertEqual("listens only on this machine" in body, not says_wide)


class ThemeTests(unittest.TestCase):
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
            self.assertNotIn("Secure", headers["set-cookie"])

    def test_theme_cookie_is_secure_only_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = self.start(tmp, host="127.0.0.1", extra="  cookie_secure: true\n")
            code, headers, _ = self.request(port, "GET", "/theme?set=dark&back=/config")
            self.assertEqual(code, 303)
            self.assertIn("ph-theme=dark", headers["set-cookie"])
            self.assertIn("; Secure", headers["set-cookie"])

    def test_theme_route_rejects_bad_values_and_foreign_backs(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = self.start(tmp, host="127.0.0.1")
            self.assertEqual(self.request(port, "GET", "/theme?set=sepia")[0], 400)
            code, headers, _ = self.request(
                port, "GET", "/theme?set=dark&back=https://evil.example/")
            self.assertEqual((code, headers["location"]), (303, "/"))

    def test_theme_route_rejects_control_characters_in_back(self):
        # A decoded CR/LF in back would otherwise reach the Location
        # header verbatim and inject response headers (Codex P1).
        with tempfile.TemporaryDirectory() as tmp:
            port = self.start(tmp, host="127.0.0.1")
            code, headers, _ = self.request(
                port, "GET", "/theme?set=dark&back=/%0d%0aX-Injected:%20yes")
            self.assertEqual(code, 303)
            self.assertEqual(headers["location"], "/")
            self.assertNotIn("x-injected", headers)

    def test_theme_route_is_reachable_while_unclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = self.start(tmp)  # LAN bind, nothing chosen: open, unclaimed
            code, headers, _ = self.request(port, "GET", "/theme?set=light", headers=HTML)
            self.assertEqual(code, 303)
            self.assertNotEqual(headers["location"], "/setup")
            self.assertEqual(self.request(port, "GET", "/theme?set=light")[0], 303)

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

    def test_submit_script_restores_controls_on_a_bfcache_return(self):
        # A page restored from the back/forward cache keeps the DOM as
        # it was at submit time: submit buttons disabled, the clicked
        # one still showing its busy label, and the hidden submitter
        # mirror still in the form. The shared script must undo all
        # three on a persisted pageshow, or e.g. Back to the
        # Configuration page leaves "Preview changes" unusable.
        script = shell.SUBMIT_SCRIPT
        self.assertIn("pageshow", script)
        self.assertIn("persisted", script)
        self.assertIn("b.disabled=false", script)
        # The clicked button's original label is saved before the
        # busy label replaces it, and restored on the bfcache return.
        self.assertIn("b.dataset.phLabel=b.textContent", script)
        self.assertIn("b.textContent=b.dataset.phLabel", script)
        # Injected submitter mirrors are marked at submit time and
        # removed on the bfcache return, so a stale action value
        # cannot be submitted twice.
        self.assertIn("data-ph-mirror", script)
        self.assertIn("input[data-ph-mirror]", script)
        self.assertIn("h.remove()", script)


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

    def test_an_active_job_renders_its_line_bar_and_layers(self):
        job = {"state": "printing", "filename": "benchy.gcode", "progress": 0.42,
               "layer": 3, "total_layers": 10, "remaining_min": 25}
        body = server.render_status_html(FAKE_SNAPSHOT, job=job)
        self.assertIn("benchy.gcode", body)
        self.assertIn('<div class="progress"><i style="width: 42%"></i></div>', body)
        self.assertIn("Printing · 42% done · Layer 3 of 10 · about 25 min left", body)

    def test_a_job_without_progress_or_layers_omits_those_parts(self):
        job = {"state": "paused", "filename": "cube.gcode", "progress": None,
               "layer": None, "total_layers": None, "remaining_min": None}
        body = server.render_status_html(FAKE_SNAPSHOT, job=job)
        self.assertIn("cube.gcode", body)
        self.assertIn("Paused", body)
        self.assertNotIn('class="progress"', body)
        self.assertNotIn("Layer ", body)
        self.assertNotIn("min left", body)

    def test_no_job_no_job_block(self):
        body = server.render_status_html(FAKE_SNAPSHOT)
        self.assertNotIn('class="progress"', body)
        self.assertNotIn('class="job-line"', body)


ROSTER_YAML = (
    "printers:\n"
    "  - id: garage-ad5x\n"
    "    name: Garage AD5X\n"
    "    model: flashforge.ad5x\n"
    "    moonraker_url: http://192.168.1.61:7125\n")


class PrinterNameShapeTests(unittest.TestCase):
    """The walkthrough Printer step's name, per settings shape (#813)."""

    def start(self, tmp, *, extra="", environ=None, user_text=None):
        local = Path(tmp) / "local.yaml"
        local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n"
                         "webui:\n  enabled: true\n  host: 127.0.0.1\n  port: 8765\n"
                         + extra)
        environ = {"PRINT_HANDY_CONFIG": str(local), **(environ or {})}
        if user_text is not None:
            user = Path(tmp) / "user.yaml"
            user.write_text(user_text)
            environ["PRINT_HANDY_USER_CONFIG"] = str(user)
        data = settings.load(local=local, environ=environ)
        access.save_open(data, True)
        httpd = server.make_server(data, environ=environ, host="127.0.0.1", port=0,
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

    def post_printer_step(self, port, environ, csrf, name):
        pairs = [("step", "printer"), ("action", "continue"), (pages.CSRF_FIELD, csrf)]
        by_id = {item.id: item for item in config_api.read_fields(environ=environ)}
        for field_id in walkthrough.STEP_FIELDS["printer"]:
            item = by_id[field_id]
            if item.overridden or field_id == "printer.name":
                continue
            value = item.value
            if isinstance(value, (list, tuple)):
                pairs += [(field_id, str(part)) for part in value]
            else:
                pairs.append((field_id, "" if value is None else str(value)))
        if name is not None:
            pairs.append(("printer_name", name))
        return self.request(port, "POST", "/walkthrough",
                            body=urlencode(pairs), headers=FORM)

    def test_a_roster_name_is_edited_in_place_and_the_id_stays(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, _, local, environ = self.start(tmp, extra=ROSTER_YAML)
            code, _, body = self.request(port, "GET", "/walkthrough")
            self.assertEqual(code, 200)
            self.assertIn('name="printer_name"', body)
            self.assertIn('value="Garage AD5X"', body)
            self.assertNotIn('name="printer.name"', body)  # never the legacy key
            code, headers, _ = self.post_printer_step(
                port, environ, httpd.form_token, "Workshop AD5X")
            self.assertEqual((code, headers["location"]), (303, "/walkthrough"))
            reloaded = settings.load(local=local, environ={})  # the file still loads
            entry = settings.printers(reloaded)[0]
            self.assertEqual(entry["name"], "Workshop AD5X")
            self.assertEqual(entry["id"], "garage-ad5x")  # renaming never moves the id
            self.assertEqual(entry["model"], "flashforge.ad5x")
            self.assertEqual(reloaded["services"]["moonraker_url"],
                             "http://192.168.1.61:7125")
            raw = settings._read(local)
            self.assertNotIn("name", raw.get("printer", {}))  # no legacy mixture

    def test_an_env_overridden_roster_name_is_disabled_and_untouched(self):
        env = {"PRINT_HANDY__PRINTERS__GARAGE-AD5X__NAME": "Env Name"}
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, _, local, environ = self.start(
                tmp, extra=ROSTER_YAML, environ=env)
            code, _, body = self.request(port, "GET", "/walkthrough")
            self.assertEqual(code, 200)
            self.assertIn("PRINT_HANDY__PRINTERS__GARAGE-AD5X__NAME", body)
            self.assertNotIn('name="printer_name"', body)  # a disabled input submits nothing
            code, _, _ = self.post_printer_step(
                port, environ, httpd.form_token, "Sneaky Rename")
            self.assertEqual(code, 303)
            reloaded = settings.load(local=local, environ={})
            self.assertEqual(settings.printers(reloaded)[0]["name"], "Garage AD5X")

    def test_a_user_file_roster_name_is_disabled_with_its_badge(self):
        user_roster = ROSTER_YAML.replace("Garage AD5X", "User AD5X")
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port, _, local, environ = self.start(tmp, user_text=user_roster)
            code, _, body = self.request(port, "GET", "/walkthrough")
            self.assertEqual(code, 200)
            self.assertIn("your settings file", body)
            self.assertNotIn('name="printer_name"', body)
            code, _, _ = self.post_printer_step(
                port, environ, httpd.form_token, "Sneaky Rename")
            self.assertEqual(code, 303)
            reloaded = settings.load(local=local, environ=environ)
            self.assertEqual(settings.printers(reloaded)[0]["name"], "User AD5X")
            self.assertNotIn("printers", settings._read(local))


class PrinterNameWriterTests(unittest.TestCase):
    """config.write_printer_name and printer_name_state, file-level (#813)."""

    def write(self, tmp, text, name="local.yaml"):
        path = Path(tmp) / name
        path.write_text(text)
        return path

    def test_rename_loads_after_write_and_keeps_a_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = self.write(tmp, ROSTER_YAML)
            changed = config_api.write_printer_name(
                "garage-ad5x", "Workshop AD5X", config_path=local, environ={})
            self.assertEqual(changed, [("printers", "garage-ad5x")])
            data = settings.load(local=local, environ={})
            entry = settings.printers(data)[0]
            self.assertEqual((entry["id"], entry["name"]), ("garage-ad5x", "Workshop AD5X"))
            self.assertTrue(list(Path(tmp).glob("local.yaml.bak-*")))

    def test_a_blank_name_clears_back_to_the_models_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = self.write(tmp, ROSTER_YAML)
            config_api.write_printer_name("garage-ad5x", "  ", config_path=local, environ={})
            raw = settings._read(local)
            self.assertNotIn("name", raw["printers"][0])
            data = settings.load(local=local, environ={})
            self.assertTrue(settings.printers(data)[0]["name"])  # the model's name fills in

    def test_no_change_is_no_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = self.write(tmp, ROSTER_YAML)
            self.assertEqual(config_api.write_printer_name(
                "garage-ad5x", "Garage AD5X", config_path=local, environ={}), [])
            self.assertFalse(list(Path(tmp).glob("local.yaml.bak-*")))

    def test_refusals_write_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = self.write(tmp, "printer:\n  model: flashforge.ad5x\n", "legacy.yaml")
            with self.assertRaises(config_api.WebUIConfigError):
                config_api.write_printer_name("printer", "Name",
                                              config_path=legacy, environ={})
            local = self.write(tmp, ROSTER_YAML)
            with self.assertRaises(config_api.WebUIConfigError):  # unknown entry id
                config_api.write_printer_name("nope", "Name", config_path=local, environ={})
            with self.assertRaises(config_api.WebUIConfigError):  # environment owns it
                config_api.write_printer_name(
                    "garage-ad5x", "Name", config_path=local,
                    environ={"PRINT_HANDY__PRINTERS__GARAGE-AD5X__NAME": "Env"})
            user = self.write(tmp, ROSTER_YAML, "user.yaml")
            with self.assertRaises(config_api.WebUIConfigError):  # the user file declares it
                config_api.write_printer_name(
                    "garage-ad5x", "Name", config_path=local,
                    environ={"PRINT_HANDY_USER_CONFIG": str(user)})
            for path in (legacy, local):
                self.assertFalse(list(Path(tmp).glob(path.name + ".bak-*")))

    def test_a_toml_roster_stays_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = self.write(
                tmp, '[[printers]]\nid = "garage-ad5x"\nname = "Garage AD5X"\n'
                     'model = "flashforge.ad5x"\n'
                     'moonraker_url = "http://192.168.1.61:7125"\n', "local.toml")
            data = settings.load(local=local, environ={})
            state = config_api.printer_name_state(data, local=local, environ={})
            self.assertEqual(state.shape, "roster_toml")
            with self.assertRaises(config_api.WebUIConfigError):
                config_api.write_printer_name("garage-ad5x", "Name",
                                              config_path=local, environ={})

    def test_read_fields_survives_an_indexed_env_override(self):
        # Regression (#813): _layers resolved the environment without the
        # declared roster, so every field read raised on installs using
        # PRINT_HANDY__PRINTERS__<ID>__<KEY> overrides.
        with tempfile.TemporaryDirectory() as tmp:
            local = self.write(tmp, ROSTER_YAML)
            environ = {"PRINT_HANDY_CONFIG": str(local),
                       "PRINT_HANDY__PRINTERS__GARAGE-AD5X__NAME": "Env Name"}
            fields = config_api.read_fields(environ=environ)
            self.assertTrue(fields)
            state = config_api.printer_name_state(
                settings.load(local=local, environ=environ), environ=environ)
            self.assertEqual(state.shape, "environment")

    def test_the_legacy_name_is_a_catalog_field_with_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = self.write(tmp, "printer:\n  model: flashforge.ad5x\n")
            environ = {"PRINT_HANDY_CONFIG": str(local)}
            item = config_api.read_field("printer.name", environ=environ)
            self.assertIsNotNone(item)
            config_api.write_changes({"printer.name": "Bench"}, environ=environ)
            data = settings.load(local=local, environ={})
            self.assertEqual(data["printer"]["name"], "Bench")
            env = {**environ, "PRINT_HANDY__PRINTER__NAME": "Env Bench"}
            item = config_api.read_field("printer.name", environ=env)
            self.assertEqual(item.provenance, config_api.ENVIRONMENT)
            with self.assertRaises(config_api.WebUIConfigError):
                config_api.write_changes({"printer.name": "Nope"}, environ=env)


if __name__ == "__main__":
    unittest.main()
