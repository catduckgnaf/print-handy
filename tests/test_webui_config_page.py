"""Web UI stage 3 (#286): the picker configuration page.

GET /config renders the stage 1 catalog as a form; saving is
preview (diff, writes nothing) -> confirm (the stage 1 validated write).
Everything runs offline against a temp settings file named by
PRINT_HANDY_CONFIG, with the server on an ephemeral loopback port.
"""
import html as html_lib
import http.client
import re
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from print_handy import settings
from print_handy.webui import config as config_api
from print_handy.webui import pages as pages_module
from print_handy.webui import server
from print_handy.webui.catalog import field_by_id

TOKEN = "test-webui-token-12345"
OTHER_SECRET = "test-discord-token-67890"
FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}

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


def hidden_inputs(body):
    return re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', body)


def row_segment(body, field_id):
    """The rendered row for one field, for error-placement assertions."""
    return body.split(f'id="row-{field_id}"', 1)[1].split("</div>", 1)[0]


class ConfigPageTests(unittest.TestCase):
    def start(self, tmp, extra="", environ=None):
        local = Path(tmp) / "local.yaml"
        local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n"
                         "webui:\n  enabled: true\n  port: 8765\n" + extra)
        environ = {"PRINT_HANDY_CONFIG": str(local), **(environ or {})}
        data = settings.load(local=local, environ=environ)
        httpd = server.make_server(data, environ=environ, port=0,
                                   snapshot_func=lambda: FAKE_SNAPSHOT)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        return httpd.server_address[1], local, environ

    def request(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read().decode()
        finally:
            conn.close()

    def get_config(self, port, headers=None, section=None):
        path = "/config" if section is None else f"/config?section={section}"
        code, body = self.request(port, "GET", path, headers=headers)
        csrf = dict(hidden_inputs(body)).get("_csrf", "")
        return code, body, csrf

    def browser_form(self, environ, csrf, **overrides):
        """The (name, value) pairs a browser would submit for the current
        effective settings, with per-field overrides applied (None removes
        the field, as an unchecked checkbox or emptied select does)."""
        form = []
        for item in config_api.read_fields(environ=environ):
            if item.env_overridden:
                continue
            value = item.value
            if item.field.kind == "toggle":
                if value:
                    form.append((item.id, "true"))
            elif isinstance(value, (list, tuple)):
                form += [(item.id, str(part)) for part in value]
            elif isinstance(value, float) and value == int(value):
                form.append((item.id, str(int(value))))
            else:
                form.append((item.id, "" if value is None else str(value)))
        for field_id, value in overrides.items():
            form = [(k, v) for k, v in form if k != field_id]
            if value is not None:
                form.append((field_id, value))
        form.append(("_csrf", csrf))
        return form

    def post(self, port, path, pairs, headers=None):
        return self.request(port, "POST", path, body=urlencode(pairs),
                            headers={**FORM_HEADERS, **(headers or {})})

    def save_flow(self, port, environ, csrf, **overrides):
        """Preview + confirm with the preview's own hidden inputs."""
        code, body = self.post(port, "/config/preview",
                               self.browser_form(environ, csrf, **overrides))
        self.assertEqual(code, 200, body)
        return self.post(port, "/config", hidden_inputs(body))

    # -- rendering ---------------------------------------------------------

    def test_config_page_renders_groups_fields_badges_and_pickers(self):
        section_names = ("printers", "platform", "spools", "ai", "camera",
                         "wrapped", "slicing", "home_assistant",
                         "notifications", "logging", "webui")
        with tempfile.TemporaryDirectory() as tmp:
            port, _local, _environ = self.start(tmp, "discord:\n  chat_id: '12345'\n")
            code, body, csrf = self.get_config(port)
            bodies = {}
            for name in section_names:
                _c, bodies[name], _t = self.get_config(port, section=name)
        self.assertEqual(code, 200)
        self.assertTrue(csrf)
        self.assertIn('action="/config/preview"', body)
        # Stage 3 sub-tabs: the default page shows the first section
        # only; every section is one server-side link away, and unshown
        # sections ride along as hidden inputs, never as controls.
        self.assertIn("<h2>Printers</h2>", body)
        self.assertNotIn("<h2>Logging</h2>", body)
        for name in section_names:
            self.assertIn(f'href="/config?section={name}"', body)
        self.assertIn('name="printer.model"', body)
        self.assertIn('type="hidden" name="logging.level"', body)
        self.assertNotIn('id="row-logging.level"', body)
        for name, heading in (("printers", "Printers"),
                              ("platform", "Platform and chat"),
                              ("spools", "Spools"), ("ai", "AI and models"),
                              ("camera", "Camera, timelapse and images"),
                              ("wrapped", "Wrapped"),
                              ("slicing", "Slicing and delivery"),
                              ("home_assistant", "Home Assistant"),
                              ("notifications", "Notifications"),
                              ("logging", "Logging"), ("webui", "Web UI")):
            self.assertIn(f"<h2>{heading}</h2>", bodies[name])
        joined = "".join(bodies.values())
        for name in ("printer.model", "bot.platform", "discord.chat_id",
                     "telegram.chat_id", "matrix.room_id",
                     "services.moonraker_url", "services.spoolman_url",
                     "services.camera_source", "services.camera_snapshot_url",
                     "visual_watch.sensitivity", "timelapse.mode",
                     "timelapse.render_wait_s", "printer.slicer",
                     "delivery.methods", "logging.level", "webui.port",
                     "ai.allowed_users", "ai.edit_requests_per_hour"):
            self.assertIn(f'name="{name}"', joined, name)
        # The AI section carries its file-only note (the [ai] tier tables
        # have no fields); it renders only inside the selected section.
        self.assertIn("File-only in this section: the [ai] tier tables",
                      bodies["ai"])
        # Provenance badges: the file value, the home value, a model default.
        self.assertIn("settings file", row_segment(bodies["platform"], "discord.chat_id"))
        self.assertIn("home config", row_segment(bodies["printers"], "printer.model"))
        self.assertIn("model default", row_segment(bodies["slicing"], "printer.slicer"))
        # Pickers carry the real choice sets with the effective value selected.
        self.assertIn('<option value="flashforge.ad5x" selected>', bodies["printers"])
        for choice in ("rendered", "container", "off"):
            self.assertIn(f'<option value="{choice}"', bodies["camera"])
        # No Moonraker URL is configured here, so the derived mode is off.
        self.assertIn('<option value="off" selected>', bodies["camera"])
        for choice in ("auto", "moonraker", "bambu", "go2rtc", "none"):
            self.assertIn(f'<option value="{choice}"', bodies["camera"])
        self.assertIn('<option value="auto" selected>', bodies["camera"])
        # The sensitivity number input carries the catalog bounds.
        self.assertIn('min="1" max="5"', row_segment(bodies["camera"], "visual_watch.sensitivity"))
        # Every picker is A–Z by the text shown, not by the stored key order.
        platform = re.findall(r'<option value="([^"]*)"', row_segment(bodies["platform"], "bot.platform"))
        self.assertEqual(platform, ["discord", "matrix", "telegram"])
        model_labels = re.findall(r"<option[^>]*>([^<]*)</option>", row_segment(bodies["printers"], "printer.model"))
        self.assertEqual(model_labels, sorted(model_labels, key=str.casefold))
        self.assertGreater(len(model_labels), 1)
        # Default home model is Flashforge ZMod: Moonraker is the Printer URL
        # field, and the OctoPrint address stays in the form as a hidden input.
        self.assertIn('name="services.moonraker_url"', bodies["printers"])
        self.assertIn('type="hidden" name="services.octoprint_url" value=""', bodies["printers"])
        # Hidden inputs carry raw values, never the display text: a posted
        # "(empty)" would read as a change and fail to save.
        self.assertNotRegex(body, r'type="hidden" name="[^"]+" value="\(empty\)"')
        self.assertIn('name="printer.zmod_delivery_format"', bodies["printers"])
        self.assertNotIn('type="hidden" name="printer.zmod_delivery_format"', bodies["printers"])

    def test_unconfigured_chat_platforms_hide_behind_a_reveal_row(self):
        # Stage 4: on a Discord install with no Telegram or Matrix
        # identity, the platform tab hides those fields (still submitted
        # as hidden inputs) behind named Set up rows; following one
        # draws the fields, and the other platform stays hidden.
        with tempfile.TemporaryDirectory() as tmp:
            port, _local, _environ = self.start(tmp, "discord:\n  chat_id: '12345'\n")
            _code, body, _csrf = self.get_config(port, section="platform")
            self.assertIn('type="hidden" name="telegram.chat_id"', body)
            self.assertNotIn('id="row-telegram.chat_id"', body)
            self.assertIn("Set up Telegram", body)
            self.assertIn('href="/config?section=platform&setup=telegram"', body)
            self.assertIn("Set up Matrix", body)
            code, body = self.request(port, "GET",
                                      "/config?section=platform&setup=telegram")
            self.assertEqual(code, 200)
            self.assertIn('id="row-telegram.chat_id"', body)
            self.assertNotIn('id="row-matrix.room_id"', body)
            self.assertIn("Set up Matrix", body)

    def test_configured_chat_platform_fields_stay_visible_and_round_trip(self):
        # A Matrix identity in the file counts as configured even on a
        # Discord install: its fields draw on the platform tab, and a
        # save from that tab keeps the value.
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(
                tmp, "matrix:\n  room_id: '!room:example.org'\n")
            _code, body, csrf = self.get_config(port, section="platform")
            self.assertIn('id="row-matrix.room_id"', body)
            self.assertNotIn("Set up Matrix", body)
            code, body = self.save_flow(port, environ, csrf,
                                        **{"discord.chat_id": "777"})
            self.assertEqual(code, 200, body)
            data = settings.load(local=local, environ={})
            self.assertEqual(data["matrix"]["room_id"], "!room:example.org")
            self.assertEqual(data["discord"]["chat_id"], "777")

    def test_save_from_a_non_first_tab_preserves_unshown_sections(self):
        # Stage 3's required guard: a save submitted from the camera tab
        # (the browser posts that tab's controls plus every hidden input
        # the page rendered) changes the camera field and leaves an
        # unshown section's file value exactly as it was.
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(
                tmp, "discord:\n  chat_id: '12345'\nlogging:\n  level: debug\n")
            _code, body, csrf = self.get_config(port, section="camera")
            form = [(k, v) for k, v in hidden_inputs(body)]
            # The tab's own visible controls, as a browser submits them:
            # browser_form values restricted to the camera group's fields,
            # with the one edit applied.
            from print_handy.webui.catalog import field_by_id
            visible = [(k, v) for k, v in self.browser_form(environ, csrf)
                       if k == "_csrf" or (field_by_id(k) is not None
                                           and field_by_id(k).group == "camera")]
            form += [(k, v) for k, v in visible if k != "_csrf"]
            form = [(k, v) for k, v in form if k != "visual_watch.sensitivity"]
            form.append(("visual_watch.sensitivity", "4"))
            code, body = self.post(port, "/config/preview", form)
            self.assertEqual(code, 200, body)
            code, body = self.post(port, "/config", hidden_inputs(body))
            self.assertEqual(code, 200, body)
            data = settings.load(local=local, environ={})
            self.assertEqual(data["visual_watch"]["sensitivity"], 4)
            # The loader canonicalizes the level to upper case on read.
            self.assertEqual(data["logging"]["level"], "DEBUG")
            self.assertEqual(data["discord"]["chat_id"], "12345")

    def test_wrapped_on_save_pins_the_following_data_switch(self):
        # Stage 5 follow rule through the settings page: data_enabled is
        # unset, so the form shows it as wrapped_enabled's value (off).
        # A save that turns Wrapped on must pin the Data value the form
        # showed — otherwise the restart silently turns the Data tab on
        # even though the submitted form had it off, and the followed
        # value is lost exactly as if it had been written explicitly.
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(
                tmp, "timelapse:\n  wrapped_enabled: false\n")
            _code, _body, csrf = self.get_config(port, section="wrapped")
            code, body = self.save_flow(
                port, environ, csrf, **{"timelapse.wrapped_enabled": "true"})
            self.assertEqual(code, 200, body)
            saved = settings._read(local)
            self.assertIs(saved["timelapse"]["wrapped_enabled"], True)
            self.assertIs(saved["timelapse"]["data_enabled"], False)
            reloaded = settings.load(local=local, environ={})
            self.assertIs(reloaded["timelapse"]["wrapped_enabled"], True)
            self.assertIs(reloaded["timelapse"]["data_enabled"], False)
            self.assertIs(reloaded["timelapse"]["data_enabled_explicit"], True)

    def test_wrapped_off_save_pins_the_following_data_switch(self):
        # The mirror direction: data_enabled unset and shown on (it
        # follows wrapped_enabled: true); turning Wrapped off in a save
        # must not drag the Data tab off with it — the Data value the
        # form showed (on) is pinned by the same save.
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(
                tmp, "timelapse:\n  wrapped_enabled: true\n")
            _code, _body, csrf = self.get_config(port, section="wrapped")
            code, body = self.save_flow(
                port, environ, csrf, **{"timelapse.wrapped_enabled": None})
            self.assertEqual(code, 200, body)
            saved = settings._read(local)
            self.assertIs(saved["timelapse"]["wrapped_enabled"], False)
            self.assertIs(saved["timelapse"]["data_enabled"], True)
            reloaded = settings.load(local=local, environ={})
            self.assertIs(reloaded["timelapse"]["wrapped_enabled"], False)
            self.assertIs(reloaded["timelapse"]["data_enabled"], True)
            self.assertIs(reloaded["timelapse"]["data_enabled_explicit"], True)

    def test_status_page_links_to_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, _local, _environ = self.start(tmp)
            code, body = self.request(port, "GET", "/")
        self.assertEqual(code, 200)
        self.assertIn('href="/config"', body)

    def test_env_overridden_field_is_disabled_and_never_written(self):
        environ = {"PRINT_HANDY__VISUAL_WATCH__SENSITIVITY": "4"}
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(tmp, environ=environ)
            code, body, csrf = self.get_config(port, section="camera")
            self.assertEqual(code, 200)
            row = row_segment(body, "visual_watch.sensitivity")
            self.assertIn("disabled", row)
            self.assertIn("Overridden by the environment", row)
            self.assertIn("environment override", row)
            before = local.read_bytes()
            # Even a crafted submission naming the field changes nothing.
            form = self.browser_form(environ, csrf)
            form.append(("visual_watch.sensitivity", "2"))
            code, body = self.post(port, "/config/preview", form)
            self.assertEqual(code, 200)
            self.assertIn("No changes", body)
            self.assertEqual(local.read_bytes(), before)

    def test_env_overridden_field_in_an_unshown_group_is_never_written(self):
        # The env-override test above shows the field on its own tab,
        # where it renders as a disabled control (never submitted). In
        # an unshown group the same field rides as a hidden input, which
        # IS submitted — a save from another tab must still never write
        # it (parse_form skips overridden fields entirely).
        environ = {"PRINT_HANDY__VISUAL_WATCH__SENSITIVITY": "4"}
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(tmp, environ=environ)
            _code, body, csrf = self.get_config(port, section="printers")
            self.assertIn(
                'type="hidden" name="visual_watch.sensitivity" value="4"', body)
            from print_handy.webui.catalog import field_by_id
            form = [(k, v) for k, v in hidden_inputs(body)]
            visible = [(k, v) for k, v in self.browser_form(environ, csrf)
                       if k == "_csrf" or (field_by_id(k) is not None
                                           and field_by_id(k).group == "printers")]
            form += [(k, v) for k, v in visible if k != "_csrf"]
            form = [(k, v) for k, v in form if k != "printer.name"]
            form.append(("printer.name", "Workshop"))
            code, body = self.post(port, "/config/preview", form)
            self.assertEqual(code, 200, body)
            code, body = self.post(port, "/config", hidden_inputs(body))
            self.assertEqual(code, 200, body)
            data = settings.load(local=local, environ={})
            self.assertEqual(data["printer"]["name"], "Workshop")
            # The file holds no sensitivity, so the default (3) applies;
            # the environment value (4) was never written.
            self.assertEqual(data["visual_watch"]["sensitivity"], 3)
            self.assertNotIn("sensitivity: 4", local.read_text())

    def test_preview_error_in_another_group_lands_on_that_group(self):
        # A browser submit always posts _section for the tab it was on.
        # An error blamed on a field in another group must re-render on
        # that group: on the posting tab the errored row exists only as
        # a hidden input, so the message would render nowhere and the
        # bad value could not be edited.
        cases = [
            # A parse error (camera group) posted from the printers tab.
            ("printers", {"visual_watch.sensitivity": "9"},
             "visual_watch.sensitivity", "Camera, timelapse and images"),
            # A validator error (printers group) posted from the camera tab.
            ("camera", {"services.moonraker_url": "printer.lan"},
             "services.moonraker_url", "Printers"),
        ]
        for tab, overrides, field_id, heading in cases:
            with self.subTest(field=field_id), tempfile.TemporaryDirectory() as tmp:
                port, local, environ = self.start(tmp)
                _code, _body, csrf = self.get_config(port, section=tab)
                form = self.browser_form(environ, csrf, **overrides)
                form.append(("_section", tab))
                before = local.read_bytes()
                code, body = self.post(port, "/config/preview", form)
                self.assertEqual(code, 400, body)
                self.assertIn(f"<h2>{heading}</h2>", body)
                self.assertIn(f'id="row-{field_id}"', body)
                self.assertIn('class="error"', row_segment(body, field_id))
                self.assertEqual(local.read_bytes(), before)

    def test_preview_confirm_form_keeps_the_posting_section(self):
        # The confirm form reposts _section, so a save re-render (for
        # example a general write refusal) lands back on the tab the
        # user was working on instead of silently jumping to the first.
        with tempfile.TemporaryDirectory() as tmp:
            port, _local, environ = self.start(tmp)
            _code, _body, csrf = self.get_config(port, section="camera")
            form = self.browser_form(
                environ, csrf, **{"visual_watch.sensitivity": "5"})
            form.append(("_section", "camera"))
            code, body = self.post(port, "/config/preview", form)
            self.assertEqual(code, 200, body)
            self.assertIn(("_section", "camera"), hidden_inputs(body))

    def test_config_page_warns_before_a_tab_switch_discards_edits(self):
        # Tab links are plain GETs: without a guard, clicking one throws
        # away every edit made on the current tab (impossible before
        # stage 3, when all groups shared one page).
        with tempfile.TemporaryDirectory() as tmp:
            port, _local, _environ = self.start(tmp)
            _code, body, _csrf = self.get_config(port)
        self.assertIn("Unsaved changes on this tab will be lost", body)
        self.assertIn("config-tabs", body)
        # The Set up links navigate away the same way, so the guard
        # covers them too.
        self.assertIn(".config-tabs a, .setup-reveal a", body)

    def test_revealed_platform_stays_revealed_after_a_validation_error(self):
        # ?setup= exists only on the GET: the form reposts it as _setup,
        # so an error re-render keeps the revealed fields on screen with
        # whatever the user typed instead of hiding them behind Set up.
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(tmp, "discord:\n  chat_id: '12345'\n")
            code, body = self.request(port, "GET",
                                      "/config?section=platform&setup=telegram")
            self.assertEqual(code, 200)
            self.assertIn(("_setup", "telegram"), hidden_inputs(body))
            csrf = dict(hidden_inputs(body))["_csrf"]
            before = local.read_bytes()
            form = self.browser_form(environ, csrf, **{"bot.platform": "nope"})
            form += [("_section", "platform"), ("_setup", "telegram")]
            code, body = self.post(port, "/config/preview", form)
            self.assertEqual(code, 400, body)
            self.assertIn('class="error"', row_segment(body, "bot.platform"))
            self.assertIn('id="row-telegram.chat_id"', body)
            self.assertNotIn("Set up Telegram", body)
            self.assertNotIn('id="row-matrix.room_id"', body)
            self.assertIn("Set up Matrix", body)
            self.assertIn(("_setup", "telegram"), hidden_inputs(body))
            self.assertEqual(local.read_bytes(), before)

    def test_preview_confirm_form_keeps_the_reveal(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, _local, environ = self.start(tmp, "discord:\n  chat_id: '12345'\n")
            _code, _body, csrf = self.get_config(port, section="platform")
            form = self.browser_form(environ, csrf, **{"discord.chat_id": "999"})
            form += [("_section", "platform"), ("_setup", "telegram")]
            code, body = self.post(port, "/config/preview", form)
            self.assertEqual(code, 200, body)
            self.assertIn(("_setup", "telegram"), hidden_inputs(body))

    # -- save flow -----------------------------------------------------------

    def test_preview_shows_exact_diff_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(tmp, "discord:\n  chat_id: '12345'\n")
            _code, _body, csrf = self.get_config(port)
            before = local.read_bytes()
            code, body = self.post(port, "/config/preview", self.browser_form(
                environ, csrf, **{"discord.chat_id": "999",
                                  "visual_watch.sensitivity": "5"}))
            self.assertEqual(code, 200)
            self.assertIn("discord.chat_id", body)
            self.assertIn("12345", body)  # the current value
            self.assertIn("999", body)    # the new value
            self.assertIn("visual_watch.sensitivity", body)
            self.assertEqual(local.read_bytes(), before)
            self.assertFalse(list(Path(tmp).glob("*.bak-*")))

    def test_confirmed_post_round_trips_through_the_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(tmp, "discord:\n  chat_id: '12345'\n")
            _code, _body, csrf = self.get_config(port)
            code, body = self.save_flow(port, environ, csrf,
                                        **{"discord.chat_id": "999",
                                           "visual_watch.sensitivity": "5"})
            self.assertEqual(code, 200)
            self.assertIn("discord.chat_id", body)
            self.assertIn("visual_watch.sensitivity", body)
            self.assertIn("Restart required", body)
            data = settings.load(local=local, environ={})
            self.assertEqual(data["discord"]["chat_id"], "999")
            self.assertEqual(data["visual_watch"]["sensitivity"], 5)
            self.assertEqual(len(list(Path(tmp).glob("local.yaml.bak-*"))), 1)

    def test_type_pickers_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(tmp)
            _code, _body, csrf = self.get_config(port)
            code, body = self.save_flow(port, environ, csrf,
                                        **{"timelapse.mode": "container",
                                           "services.camera_source": "go2rtc"})
            self.assertEqual(code, 200)
            data = settings.load(local=local, environ={})
            self.assertEqual(data["timelapse"]["mode"], "container")
            self.assertEqual(data["services"]["camera_source"], "go2rtc")

    def test_toggle_off_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(tmp)
            _code, _body, csrf = self.get_config(port)
            code, _body = self.save_flow(port, environ, csrf,
                                         **{"visual_watch.enabled": None})
            self.assertEqual(code, 200)
            self.assertIs(settings.load(local=local, environ={})["visual_watch"]["enabled"],
                          False)

    def test_invalid_values_are_rejected_against_the_field(self):
        cases = [
            ({"visual_watch.sensitivity": "9"}, "visual_watch.sensitivity", "at most 5"),
            ({"printer.model": "nope.model"}, "printer.model", None),
            ({"services.moonraker_url": "printer.lan"}, "services.moonraker_url",
             "must be a URL"),
            ({"timelapse.mode": "sometimes"}, "timelapse.mode", None),
        ]
        for overrides, field_id, needle in cases:
            with self.subTest(field=field_id), tempfile.TemporaryDirectory() as tmp:
                port, local, environ = self.start(tmp)
                _code, _body, csrf = self.get_config(port)
                before = local.read_bytes()
                code, body = self.post(port, "/config/preview",
                                       self.browser_form(environ, csrf, **overrides))
                self.assertEqual(code, 400)
                self.assertIn('class="error"', row_segment(body, field_id))
                if needle:
                    self.assertIn(needle, row_segment(body, field_id))
                self.assertEqual(local.read_bytes(), before)
                self.assertFalse(list(Path(tmp).glob("*.bak-*")))

    def test_cross_section_error_lands_on_the_errored_section(self):
        # A save submitted from one tab carries every other section's
        # fields as hidden inputs; an invalid hidden value errors against
        # a field the submitted tab does not show. The error re-render
        # must land on the errored field's section — the submitted tab
        # renders that section as hidden inputs only, so staying on it
        # would show no error anywhere.
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(tmp)
            _code, _body, csrf = self.get_config(port, section="printers")
            before = local.read_bytes()
            form = self.browser_form(environ, csrf,
                                     **{"visual_watch.sensitivity": "9"})
            form.append(("_section", "printers"))
            code, body = self.post(port, "/config/preview", form)
            self.assertEqual(code, 400)
            self.assertIn("<h2>Camera, timelapse and images</h2>", body)
            self.assertIn('class="error"', row_segment(body, "visual_watch.sensitivity"))
            self.assertIn("at most 5", row_segment(body, "visual_watch.sensitivity"))
            self.assertEqual(dict(hidden_inputs(body)).get("_section"), "camera")
            # The previously shown section now rides along as hidden
            # inputs, so a corrected save still cannot clear it.
            self.assertIn('type="hidden" name="printer.model"', body)
            self.assertEqual(local.read_bytes(), before)

    def test_unknown_form_keys_are_never_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(tmp)
            _code, _body, csrf = self.get_config(port)
            form = self.browser_form(environ, csrf, **{"discord.chat_id": "777"})
            form += [("discord.token_env", "PWNED"), ("evil.key", "1"),
                     ("services.camera_source_extra", "bambu")]
            code, body = self.post(port, "/config/preview", form)
            self.assertEqual(code, 200)
            code, body = self.post(port, "/config", hidden_inputs(body))
            self.assertEqual(code, 200)
            text = local.read_text()
            self.assertNotIn("token_env", text)
            self.assertNotIn("PWNED", text)
            self.assertNotIn("evil", text)
            self.assertEqual(settings.load(local=local, environ={})["discord"]["chat_id"],
                             "777")

    # -- auth / CSRF ---------------------------------------------------------

    def test_post_requires_the_form_token_or_the_header(self):
        environ = {"WEBUI_ACCESS_TOKEN": TOKEN}
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(tmp, environ=environ)
            code, _body = self.request(port, "GET", "/config")
            self.assertEqual(code, 401)  # stage 2's header rule still gates GETs
            code, _body, csrf = self.get_config(
                port, headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 200)
            form = self.browser_form(environ, csrf, **{"discord.chat_id": "999"})
            bare = [(k, v) for k, v in form if k != "_csrf"]
            # Neither header nor form token: refused, nothing written.
            before = local.read_bytes()
            code, _body = self.post(port, "/config/preview", bare)
            self.assertEqual(code, 403)
            # A wrong form token is refused too.
            code, _body = self.post(port, "/config/preview",
                                    bare + [("_csrf", "wrong-token")])
            self.assertEqual(code, 403)
            self.assertEqual(local.read_bytes(), before)
            # The header alone authorizes a POST.
            code, _body = self.post(port, "/config/preview", bare,
                                    headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 200)
            # And so does the embedded form token alone.
            code, _body = self.post(port, "/config/preview", form)
            self.assertEqual(code, 200)

    def test_loopback_without_a_token_still_requires_the_form_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(tmp)
            _code, _body, csrf = self.get_config(port)
            form = self.browser_form(environ, csrf, **{"discord.chat_id": "999"})
            bare = [(k, v) for k, v in form if k != "_csrf"]
            code, _body = self.post(port, "/config/preview", bare)
            self.assertEqual(code, 403)
            code, _body = self.post(port, "/config/preview", form)
            self.assertEqual(code, 200)

    def test_save_without_the_preview_confirm_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(tmp)
            _code, _body, csrf = self.get_config(port)
            before = local.read_bytes()
            form = self.browser_form(environ, csrf, **{"discord.chat_id": "999"})
            code, _body = self.post(port, "/config", form)
            self.assertEqual(code, 400)
            self.assertEqual(local.read_bytes(), before)

    def test_non_config_posts_stay_405(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, _local, _environ = self.start(tmp)
            for path in ("/", "/api/status", "/healthz"):
                code, _body = self.post(port, path, [("_csrf", "x")])
                self.assertEqual(code, 405, path)

    # -- secrecy ---------------------------------------------------------------

    def test_no_secret_strings_in_any_config_response(self):
        environ = {"WEBUI_ACCESS_TOKEN": TOKEN, "DISCORD_BOT_TOKEN": OTHER_SECRET}
        with tempfile.TemporaryDirectory() as tmp:
            port, _local, environ = self.start(tmp, environ=environ)
            headers = {server.TOKEN_HEADER: TOKEN}
            bodies = [self.request(port, "GET", "/", headers=headers)[1]]
            code, body, csrf = self.get_config(port, headers=headers)
            bodies.append(body)
            code, body = self.post(port, "/config/preview", self.browser_form(
                environ, csrf, **{"discord.chat_id": "999"}))
            bodies.append(body)
            code, body = self.post(port, "/config", hidden_inputs(body))
            bodies.append(body)
            bodies.append(self.request(port, "GET", "/config")[1])  # the 401 body
        for body in bodies:
            self.assertNotIn(TOKEN, body)
            self.assertNotIn(OTHER_SECRET, body)


class ResultPageWarningTests(unittest.TestCase):
    """Review of #1103: the save-result page must name every tolerated
    closed-table key and say the next restart will fail until it is
    removed — a tolerated save otherwise looks like a clean success
    right up to the restart it breaks."""

    def test_result_page_names_the_tolerated_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "local.yaml"
            local.write_text("notify:\n  mqtt_password: hunter2\n")
            config_api._last_write_warnings = []
            changed = config_api.write_changes(
                {"vision.roast_enabled": False}, config_path=local, environ={})
            self.assertEqual(changed, [("vision", "roast_enabled")])
            self.assertEqual(config_api.last_write_warnings(),
                             ["[notify] mqtt_password"])
            body = pages_module.render_result_page(changed)
        self.assertIn("[notify] mqtt_password", body)
        self.assertIn("next restart will fail", body)
        self.assertIn("until it is removed", body)
        self.assertNotIn("hunter2", body)  # the secret is never echoed

    def test_result_page_names_a_violation_a_noop_save_leaves_in_place(self):
        # Review of #1103 (head b0d2d1a6): a save of the values the file
        # already holds writes nothing; its result page must still name
        # a tolerated violation the file still carries.
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "local.yaml"
            local.write_text("notify:\n  mqtt_password: hunter2\n")
            config_api._last_write_warnings = []
            changed = config_api.write_changes(
                {"vision.roast_enabled": False}, config_path=local, environ={})
            self.assertEqual(changed, [("vision", "roast_enabled")])
            changed = config_api.write_changes(
                {"vision.roast_enabled": False}, config_path=local, environ={})
            self.assertEqual(changed, [])
            self.assertEqual(config_api.last_write_warnings(),
                             ["[notify] mqtt_password"])
            body = pages_module.render_result_page(changed)
        self.assertIn("Nothing changed", body)
        self.assertIn("[notify] mqtt_password", body)
        self.assertIn("next restart will fail", body)
        self.assertNotIn("hunter2", body)

    def test_result_page_without_tolerated_keys_has_no_restart_failure_warning(self):
        body = pages_module.render_result_page(
            [("vision", "roast_enabled")], warnings=[])
        self.assertNotIn("next restart will fail", body)


if __name__ == "__main__":
    unittest.main()


class DnsRebindingTests(unittest.TestCase):
    """A tokenless loopback Web UI answers only to loopback names.

    A hostile page that rebinds its own hostname to 127.0.0.1 is "same
    origin" with the Web UI, so without a Host check it could read the form
    token from GET /config and post settings changes (for example adding
    itself to discord.allowed_user_ids). Its requests still carry the
    attacker's hostname in Host and Origin.
    """

    # The page helpers only, not ConfigPageTests' own tests.
    start = ConfigPageTests.start
    request = ConfigPageTests.request
    get_config = ConfigPageTests.get_config
    browser_form = ConfigPageTests.browser_form
    post = ConfigPageTests.post

    def test_rebound_host_cannot_read_or_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, local, environ = self.start(tmp)
            evil = {"Host": f"rebind.attacker.example:{port}"}
            code, _body = self.request(port, "GET", "/config", headers=evil)
            self.assertEqual(code, 403)
            _code, _body, csrf = self.get_config(port)  # the operator's own page
            before = local.read_bytes()
            form = self.browser_form(environ, csrf, **{"discord.chat_id": "999"})
            code, _body = self.post(port, "/config/preview", form, headers=evil)
            self.assertEqual(code, 403)
            code, _body = self.post(port, "/config/preview", form,
                                    headers={"Origin": "http://rebind.attacker.example"})
            self.assertEqual(code, 403)
            self.assertEqual(local.read_bytes(), before)
            # Loopback names still work, and /healthz is never gated.
            for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
                code, _body = self.request(port, "GET", "/config", headers={"Host": host})
                self.assertEqual(code, 200, host)
            code, _body = self.request(port, "GET", "/healthz", headers=evil)
            self.assertNotEqual(code, 403)


class UnnamedGroupTests(unittest.TestCase):
    """A group with an empty or missing name still gets a working tab.

    The page anticipates such groups (the "Other" heading, the
    unknown-group branch), but its tab links to ?section=other while the
    section list holds "" or None, so the selection never matched and
    the group's fields could only ever ride as hidden inputs.
    """

    def render(self, section=None, errors=None):
        from print_handy.webui import pages
        from print_handy.webui.catalog import Field
        fields = [
            config_api.FieldValue(
                field=Field("logging", "level", "Log level", "text",
                            group="logging"),
                value="INFO", provenance=config_api.DEFAULT),
            config_api.FieldValue(
                field=Field("misc", "thing", "Misc thing", "text", group=""),
                value="x", provenance=config_api.DEFAULT),
            config_api.FieldValue(
                field=Field("misc", "other_thing", "Other misc thing", "text",
                            group=None),
                value="y", provenance=config_api.DEFAULT),
        ]
        return pages.render_config_page(fields, data={}, form_token="tok",
                                        section=section, errors=errors)

    def test_unnamed_group_tab_shows_its_fields(self):
        body = self.render(section="other")
        self.assertIn("<h2>Other</h2>", body)
        self.assertIn('id="row-misc.thing"', body)
        self.assertIn('id="row-misc.other_thing"', body)
        self.assertNotIn('id="row-logging.level"', body)

    def test_error_in_unnamed_group_lands_on_it(self):
        body = self.render(section="logging",
                           errors={"misc.thing": "bad value"})
        self.assertIn("<h2>Other</h2>", body)
        self.assertIn('class="error"', row_segment(body, "misc.thing"))


class HiddenFieldRoundTripTests(unittest.TestCase):
    """A hidden field posts back exactly what the page read, so it is never a change."""

    def test_hidden_values_and_absent_optional_keys_parse_as_no_change(self):
        from print_handy.webui import pages
        from print_handy.webui.catalog import field_by_id
        cases = [("services.octoprint_url", None), ("services.octoprint_url", ""),
                 ("discord.allowed_user_ids", ["1", "2"]), ("discord.allowed_user_ids", []),
                 ("vision.roast_enabled", True), ("vision.roast_enabled", False),
                 ("timelapse.render_wait_s", 900)]
        for field_id, value in cases:
            with self.subTest(field=field_id, value=value):
                field = field_by_id(field_id)
                html = pages._hidden_inputs(field, value)
                form = {}
                for name, raw in re.findall(r'name="([^"]+)" value="([^"]*)"', html):
                    form.setdefault(name, []).append(raw)
                fv = config_api.FieldValue(field=field, value=value, provenance=config_api.DEFAULT)
                changes, _submitted, errors = pages.parse_form(form, [fv])
                self.assertEqual((changes, errors), ({}, {}))


class SectionLinkEncodingTests(unittest.TestCase):
    """Tab links URL-encode the section name (duckcatgnaf review of #1101).

    A group name comes from config and can contain query metacharacters;
    interpolated raw, ``Fish & Chips`` would split the query value at the
    ampersand and ``Q #1`` would truncate it at the fragment marker, so
    selecting that tab falls back to the first section.
    """

    @staticmethod
    def _page(group, section=None):
        from print_handy.webui import pages
        field = replace(field_by_id("logging.level"), group=group)
        fv = config_api.FieldValue(field=field, value="INFO",
                                   provenance=config_api.DEFAULT)
        return pages.render_config_page([fv], data={}, form_token="tok",
                                        section=section)

    @staticmethod
    def _tab_href(body, label):
        match = re.search(r'<a href="([^"]+)"[^>]*>' + re.escape(label) + r"</a>",
                          body)
        assert match is not None, f"no tab link labelled {label!r}"
        return html_lib.unescape(match.group(1))

    def test_ampersand_group_tab_link_round_trips(self):
        body = self._page("Fish & Chips")
        href = self._tab_href(body, "Fish &amp; Chips")
        self.assertNotIn("section=Fish &", href)
        section = parse_qs(urlparse(href).query)["section"][0]
        self.assertEqual(section, "Fish & Chips")
        selected = self._page("Fish & Chips", section=section)
        self.assertIn('aria-current="page"', selected)
        self.assertIn('id="row-logging.level"', selected)

    def test_hash_group_tab_link_round_trips(self):
        body = self._page("Q #1")
        href = self._tab_href(body, "Q #1")
        self.assertEqual(urlparse(href).fragment, "")
        section = parse_qs(urlparse(href).query)["section"][0]
        self.assertEqual(section, "Q #1")
        selected = self._page("Q #1", section=section)
        self.assertIn('aria-current="page"', selected)
        self.assertIn('id="row-logging.level"', selected)
