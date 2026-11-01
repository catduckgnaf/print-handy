"""Web UI stage 1 (#286): the [webui] settings section, the field catalog, and
the provenance read / validated write-back API over the YAML settings file."""
import tempfile
import unittest
from pathlib import Path

from print_handy import printers, settings
from print_handy.bot import guided
from print_handy.bot.platform import KNOWN_PLATFORMS
from print_handy.webui import (CATALOG, ENVIRONMENT, HOME_CONFIG, MODEL_DEFAULT,
                               SETTINGS_FILE, WebUIConfigError, choices_for,
                               config_page_visible, field_by_id, read_fields,
                               settings_file_path, write_changes)

# Guided-setup step keys -> the catalog field that covers the same setting.
# token_stored is deliberately absent: it collects no setting (the token is
# never written anywhere), so it must not become a catalog field.
GUIDED_FIELDS = {
    "model": "printer.model",
    "mode": "bot.mode",
    "chat_id": "discord.chat_id",
    "user_ids": "discord.allowed_user_ids",
    "moonraker_url": "services.moonraker_url",
    "spoolman_url": "services.spoolman_url",
}

SECRET_KEYS = {"token", "token_env", "access_token", "access_token_env", "api_key", "api_key_env",
               "lan_access_code_env", "password", "secret"}


def values_by_id(**kwargs):
    return {item.id: item for item in read_fields(**kwargs)}


class WebUISettingsTests(unittest.TestCase):
    def test_absent_section_yields_the_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "local.yaml"
            local.write_text("visual_watch:\n  sensitivity: 3\n")
            data = settings.load(local=local, environ={})
        self.assertEqual(data["webui"], {"enabled": False, "host": "127.0.0.1", "port": 0,
                                         "access_token_env": "WEBUI_ACCESS_TOKEN",
                                         "require_auth": True, "controls_enabled": False})

    def test_section_values_and_env_override_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "local.yaml"
            local.write_text("webui:\n  enabled: true\n  host: 0.0.0.0\n  port: 8080\n")
            data = settings.load(local=local, environ={})
            self.assertEqual(data["webui"]["port"], 8080)
            env = {"PRINT_HANDY__WEBUI__PORT": "9090", "PRINT_HANDY__WEBUI__ENABLED": "false"}
            data = settings.load(local=local, environ=env)
            self.assertEqual((data["webui"]["port"], data["webui"]["enabled"]), (9090, False))

    def test_bad_port_host_and_env_name_are_rejected(self):
        for text in ("webui:\n  port: 65536\n", "webui:\n  port: -1\n", "webui:\n  port: 1.5\n",
                     "webui:\n  enabled: sometimes\n", "webui:\n  host: http://example:8080\n",
                     "webui:\n  access_token_env: 1BAD\n", "webui:\n  access_token_env: 'has space'\n"):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                local = Path(tmp) / "local.yaml"
                local.write_text(text)
                with self.assertRaises(settings.SettingsError):
                    settings.load(local=local, environ={})
        with self.assertRaisesRegex(settings.SettingsError, "port"):
            settings.load(local=False, environ={"PRINT_HANDY__WEBUI__PORT": "70000"})


class CatalogTests(unittest.TestCase):
    def test_every_guided_setup_field_is_in_the_catalog(self):
        step_keys = {step.key for step in guided.steps()}
        self.assertEqual(set(GUIDED_FIELDS) | {"token_stored"}, step_keys)
        for step_key, field_id in GUIDED_FIELDS.items():
            with self.subTest(step=step_key):
                self.assertIsNotNone(field_by_id(field_id))

    def test_required_extra_fields_are_in_the_catalog(self):
        for field_id in ("bot.platform", "discord.approval_user_id", "discord.timezone",
                         "visual_watch.enabled", "visual_watch.sensitivity",
                         "webui.enabled", "webui.host", "webui.port"):
            with self.subTest(field=field_id):
                self.assertIsNotNone(field_by_id(field_id))

    def test_timelapse_and_image_fields_are_in_the_catalog(self):
        # #286 scope addition: timelapses and images get first-class fields.
        numbers = {"timelapse.render_wait_s": 1, "timelapse.render_poll_s": 1,
                   "timelapse.max_bytes": 1, "timelapse.local_retention_days": 0,
                   "timelapse.plan_clip_retention_days": 0}
        for field_id, minimum in numbers.items():
            with self.subTest(field=field_id):
                field = field_by_id(field_id)
                self.assertIsNotNone(field)
                self.assertEqual(field.kind, "number")
                self.assertEqual(field.minimum, minimum)
        for field_id, kind in (("timelapse.archive_host", "host"),
                               ("timelapse.archive_dir", "text"),
                               # A path only, never key contents (see catalog comment).
                               ("timelapse.ssh_key", "text"),
                               ("services.camera_snapshot_url", "url"),
                               ("services.vision_chat_url", "url"),
                               ("vision.model", "text"),
                               ("vision.roast_model", "text"),
                               ("vision.roast_enabled", "toggle")):
            with self.subTest(field=field_id):
                field = field_by_id(field_id)
                self.assertIsNotNone(field)
                self.assertEqual(field.kind, kind)
        # The type/source pickers bind to the [timelapse] mode and
        # [services] camera_source settings (#246/#294/#295 contracts):
        # pickers carrying the settings-owned choice sets.
        mode = field_by_id("timelapse.mode")
        self.assertIsNotNone(mode)
        self.assertEqual(mode.kind, "picker")
        self.assertEqual(choices_for(mode), tuple(settings.TIMELAPSE_MODES))
        source = field_by_id("services.camera_source")
        self.assertIsNotNone(source)
        self.assertEqual(source.kind, "picker")
        self.assertEqual(choices_for(source), tuple(settings.CAMERA_SOURCES))

    def test_live_stream_fields_are_in_the_catalog_under_the_live_group(self):
        # #311 stage 4: the whole [live_stream] section is editable here —
        # the Web UI is the settings surface — with the loader's own
        # bounds, and the encoder picker carries the settings choice set.
        numbers = {"live_stream.max_height": (144, 2160), "live_stream.fps": (1, 60),
                   "live_stream.max_bitrate_kbps": (100, 20000),
                   "live_stream.empty_timeout_s": (1, None),
                   "live_stream.port": (0, 65535)}
        for field_id, (minimum, maximum) in numbers.items():
            with self.subTest(field=field_id):
                field = field_by_id(field_id)
                self.assertIsNotNone(field)
                self.assertEqual(field.kind, "number")
                self.assertEqual((field.minimum, field.maximum), (minimum, maximum))
                self.assertEqual(field.group, "live")
        for field_id, kind in (("live_stream.enabled", "toggle"),
                               ("live_stream.auto_start_on_print", "toggle"),
                               ("live_stream.voice_channel_id", "text"),
                               ("live_stream.activity_origin", "url")):
            with self.subTest(field=field_id):
                field = field_by_id(field_id)
                self.assertIsNotNone(field)
                self.assertEqual(field.kind, kind)
                self.assertEqual(field.group, "live")
        encoder = field_by_id("live_stream.encoder")
        self.assertIsNotNone(encoder)
        self.assertEqual(encoder.kind, "picker")
        self.assertEqual(choices_for(encoder), tuple(settings.LIVE_STREAM_ENCODERS))

    def test_catalog_groups_cover_every_field_and_pin_the_slicing_group(self):
        # Stage 3 renders the catalog by group: every field has exactly one,
        # and the slicing group is exactly the vendor-choice pair plus the
        # layout spacing preset (2.2.2 settings-surface fix).
        for field in CATALOG:
            with self.subTest(field=field.id):
                self.assertTrue(field.group)
        slicing = {field.id for field in CATALOG if field.group == "slicing"}
        self.assertEqual(slicing, {"printer.slicer", "delivery.methods", "layout.spacing"})
        self.assertEqual(field_by_id("timelapse.mode").group, "timelapse")
        self.assertEqual(field_by_id("services.camera_source").group, "images")
        self.assertEqual(field_by_id("printer.model").group, "printer")

    def test_settings_surface_fix_fields_are_in_the_catalog(self):
        # The 2.2.2 settings-surface fix: every consumed, user-facing
        # setting the 2.2.1 audit found missing from the catalog is now
        # offered, in its group, with the kind its value needs.
        expected = {
            "bambu.lan_serial": ("services", "text"),
            "timelapse.capture_enabled": ("timelapse", "toggle"),
            "layout.spacing": ("slicing", "picker"),
            "notify.mqtt_enabled": ("services", "toggle"),
            "notify.mqtt_host": ("services", "host"),
            "notify.mqtt_port": ("services", "number"),
            "notify.mqtt_topic": ("services", "text"),
            "notify.mqtt_username": ("services", "text"),
            "notify.ntfy_enabled": ("services", "toggle"),
            "notify.ntfy_url": ("services", "url"),
            "notify.ntfy_topic": ("services", "text"),
            "llm.chat_url": ("endpoints", "url"),
            "llm.model": ("endpoints", "text"),
            "design.chat_url": ("endpoints", "url"),
            "design.model": ("endpoints", "text"),
            "agent.chat_url": ("endpoints", "url"),
            "agent.model": ("endpoints", "text"),
        }
        for field_id, (group, kind) in expected.items():
            with self.subTest(field=field_id):
                field = field_by_id(field_id)
                self.assertIsNotNone(field)
                self.assertEqual(field.group, group)
                self.assertEqual(field.kind, kind)
        spacing = field_by_id("layout.spacing")
        self.assertEqual(choices_for(spacing), tuple(settings.SPACING_PRESETS))
        mqtt_port = field_by_id("notify.mqtt_port")
        self.assertEqual((mqtt_port.minimum, mqtt_port.maximum), (0, 65535))

    def test_wired_catalog_fields_change_their_production_consumers(self):
        # Regression guard for the 2.2.1 audit's six dead options: every
        # one of them is in the catalog AND changes a production
        # consumer's behavior. If a future change unwires a field —
        # drops the consumer call, not just the field — this fails even
        # though the field is still offered.
        from print_handy import go2rtc, live_stream

        def data(services=None, timelapse=None, live=None):
            return {
                "services": {"camera_snapshot_url":
                             "http://printer.example/webcam/?action=snapshot",
                             **(services or {})},
                "timelapse": {"capture_enabled": False, "capture_source_url": "",
                              **(timelapse or {})},
                "live_stream": {"enabled": True, "max_height": 720, "fps": 15,
                                "max_bitrate_kbps": 2500, "encoder": "auto",
                                **(live or {})},
                "paths": {"cache": "/data/cache", "ffmpeg": "/nonexistent/ffmpeg"},
            }

        for field_id in ("services.camera_source", "timelapse.mode",
                         "live_stream.max_height", "live_stream.fps",
                         "live_stream.max_bitrate_kbps", "live_stream.encoder"):
            self.assertIsNotNone(field_by_id(field_id), field_id)
        # services.camera_source: "none" resolves no fan-out source,
        # "auto" resolves the derived one.
        self.assertIsNone(go2rtc.resolve_source(data(services={"camera_source": "none"})))
        self.assertIsNotNone(go2rtc.resolve_source(data(services={"camera_source": "auto"})))
        # timelapse.mode: off vetoes an enabled capture; container opens
        # a disabled one; the rendered path follows the mode too.
        self.assertFalse(go2rtc.capture_active(
            data(timelapse={"mode": "off", "capture_enabled": True})))
        self.assertTrue(go2rtc.capture_active(data(timelapse={"mode": "container"})))
        self.assertFalse(go2rtc.rendered_active(data(timelapse={"mode": "container"})))
        # live_stream four: any one away from its default engages the
        # transcode, renames the feed the Activity plays, and lands in
        # the generated go2rtc config as an ffmpeg exec stream.
        for changed in ({"max_height": 1080}, {"fps": 30},
                        {"max_bitrate_kbps": 6000}, {"encoder": "cpu"}):
            with self.subTest(changed=changed):
                tuned = data(live=changed)
                self.assertTrue(live_stream.transcode_engaged(tuned["live_stream"]))
                self.assertEqual(live_stream.feed_stream_name(tuned["live_stream"]),
                                 "camera-live")
                plan = go2rtc.transcode_plan(tuned)
                config = go2rtc.render_config(go2rtc.resolve_source(tuned), transcode=plan)
                self.assertIn("camera-live:", config)
                self.assertIn("exec:ffmpeg", config)
        default_config = go2rtc.render_config(
            go2rtc.resolve_source(data()), transcode=go2rtc.transcode_plan(data()))
        self.assertNotIn("camera-live", default_config)

    def test_no_secret_or_env_name_fields_in_the_catalog(self):
        for field in CATALOG:
            with self.subTest(field=field.id):
                self.assertNotIn(field.key, SECRET_KEYS)
                self.assertFalse(field.key.endswith("_env"), field.id)
                self.assertNotIn("token", field.key)
                self.assertNotIn("password", field.key)
                self.assertNotIn("secret", field.key)

    def test_catalog_fields_are_real_settings_with_valid_kinds(self):
        data = settings.load(local=False, environ={})
        for field in CATALOG:
            with self.subTest(field=field.id):
                self.assertIn(field.section, data)
                # An optional key (settings.OPTIONAL_KEYS) is a real setting absent from the defaults tree.
                if (field.section, field.key) not in settings.OPTIONAL_KEYS:
                    self.assertIn(field.key, data[field.section])
                self.assertTrue(field.label)
                self.assertTrue(field.help)
                self.assertTrue(field.restart_required)

    def test_choices_come_from_their_sources(self):
        self.assertEqual(
            choices_for(field_by_id("printer.model")),
            tuple(sorted(printers.MODELS, key=lambda key: (printers.MODELS[key].label.casefold(), key))),
        )
        self.assertEqual(choices_for(field_by_id("bot.platform")), tuple(KNOWN_PLATFORMS))
        self.assertEqual(choices_for(field_by_id("bot.mode")), tuple(settings.BOT_MODES))
        self.assertEqual(choices_for(field_by_id("printer.slicer"), model_key="bambulab.a1"),
                         printers.MODELS["bambulab.a1"].vendor.slicers)
        self.assertEqual(choices_for(field_by_id("delivery.methods"), model_key="bambulab.a1"),
                         printers.MODELS["bambulab.a1"].vendor.delivery)
        self.assertIn("America/New_York", choices_for(field_by_id("discord.timezone")))

    def test_printer_url_and_rtsp_fields_follow_the_model(self):
        rtsp = field_by_id("timelapse.capture_source_url")
        self.assertEqual(rtsp.label, "RTSP stream URL")
        self.assertEqual(rtsp.group, "images")
        self.assertEqual(field_by_id("services.moonraker_url").label, "Printer URL")
        self.assertEqual(field_by_id("services.octoprint_url").label, "Printer URL")
        data = settings.load(local=False, environ={})
        self.assertTrue(config_page_visible("services.moonraker_url", data))
        self.assertFalse(config_page_visible("services.octoprint_url", data))
        self.assertTrue(config_page_visible("printer.zmod_delivery_format", data))
        data["printer"]["model"] = "sovol_marlin.sv06"
        self.assertTrue(config_page_visible("services.octoprint_url", data))
        self.assertFalse(config_page_visible("services.moonraker_url", data))
        self.assertFalse(config_page_visible("printer.zmod_delivery_format", data))
        with self.assertRaises(ValueError):
            choices_for(field_by_id("printer.slicer"))


class ReadAPITests(unittest.TestCase):
    def test_provenance_for_default_home_file_and_env_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "local.yaml"
            local.write_text("discord:\n  chat_id: '123'\n")
            values = values_by_id(local=local, environ={})
            # From the printer model defaults: no file layer names a slicer.
            self.assertEqual((values["printer.slicer"].value, values["printer.slicer"].provenance),
                             ("orca", MODEL_DEFAULT))
            # From config/home.yaml.
            self.assertEqual((values["printer.model"].value, values["printer.model"].provenance),
                             ("flashforge.ad5x", HOME_CONFIG))
            # From the settings file.
            self.assertEqual((values["discord.chat_id"].value, values["discord.chat_id"].provenance),
                             ("123", SETTINGS_FILE))
            # Environment wins over the file and is reported as overridden.
            env = {"PRINT_HANDY__DISCORD__CHAT_ID": "456"}
            values = values_by_id(local=local, environ=env)
            self.assertEqual((values["discord.chat_id"].value, values["discord.chat_id"].provenance),
                             ("456", ENVIRONMENT))
            self.assertTrue(values["discord.chat_id"].env_overridden)

    def test_settings_file_path_matches_settings_load_resolution(self):
        self.assertEqual(settings_file_path({}), settings.LOCAL)
        with tempfile.TemporaryDirectory() as tmp:
            named = Path(tmp) / "chosen.yaml"
            self.assertEqual(settings_file_path({"PRINT_HANDY_CONFIG": str(named)}), named)


class WriteAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.local = Path(self.tmp.name) / "local.yaml"

    def write_file(self, text):
        self.local.write_text(text)
        return self.local.read_bytes()

    def test_write_round_trips_through_the_unmodified_loader(self):
        self.write_file("services:\n  spoolman_url: ''\n")
        changed = write_changes({"visual_watch.sensitivity": 5, "discord.chat_id": "123",
                                 "webui.enabled": True, "webui.port": 8080},
                                config_path=self.local, environ={}, timestamp="20261026-120000")
        self.assertEqual(changed, [("visual_watch", "sensitivity"), ("discord", "chat_id"),
                                   ("webui", "enabled"), ("webui", "port")])
        data = settings.load(local=self.local, environ={})
        self.assertEqual(data["visual_watch"]["sensitivity"], 5)
        self.assertEqual(data["discord"]["chat_id"], "123")
        self.assertEqual((data["webui"]["enabled"], data["webui"]["port"]), (True, 8080))
        values = values_by_id(local=self.local, environ={})
        self.assertEqual(values["visual_watch.sensitivity"].provenance, SETTINGS_FILE)

    def test_write_creates_the_file_and_preserves_uncatalogued_data(self):
        changed = write_changes({"bot.platform": "telegram"}, config_path=self.local, environ={})
        self.assertEqual(changed, [("bot", "platform")])
        self.assertEqual(settings.load(local=self.local, environ={})["bot"]["platform"], "telegram")
        self.write_file("material_temps:\n  PLA: {nozzle: 200, bed: 55}\n"
                        "cold_pull:\n  mine: [220, 90]\n")
        write_changes({"logging.level": "DEBUG"}, config_path=self.local, environ={})
        data = settings.load(local=self.local, environ={})
        self.assertEqual(data["material_temps"]["PLA"], {"nozzle": 200, "bed": 55})
        self.assertEqual(data["logging"]["level"], "DEBUG")

    def test_backup_of_the_previous_file_is_created(self):
        before = self.write_file("discord:\n  chat_id: '123'\n")
        write_changes({"discord.chat_id": "456"}, config_path=self.local, environ={},
                      timestamp="20261026-120000")
        backups = list(Path(self.tmp.name).glob("local.yaml.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)
        self.assertIn("20261026-120000", backups[0].name)

    def test_invalid_values_are_rejected_and_the_file_is_unchanged(self):
        before = self.write_file("visual_watch:\n  sensitivity: 3\n")
        for changes in ({"printer.model": "nope.model"},
                        {"visual_watch.sensitivity": 9},
                        {"services.moonraker_url": "printer.lan"},
                        {"printer.slicer": "bambu_studio"},  # not offered for flashforge.ad5x
                        {"delivery.methods": ["bambu_handoff"]},
                        {"unknown.field": 1},
                        {"discord.token_env": "OTHER"}):  # not a catalog field
            with self.subTest(changes=changes):
                with self.assertRaises(WebUIConfigError):
                    write_changes(changes, config_path=self.local, environ={})
                self.assertEqual(self.local.read_bytes(), before)
                self.assertFalse(list(Path(self.tmp.name).glob("*.bak-*")))

    def test_timelapse_and_image_write_round_trips_through_the_loader(self):
        self.write_file("timelapse:\n  render_wait_s: 900\\n")
        changed = write_changes(
            {"timelapse.render_wait_s": 600, "timelapse.render_poll_s": 5,
             "timelapse.max_bytes": 100_000_000, "timelapse.local_retention_days": 3,
             "timelapse.plan_clip_retention_days": 7,
             "timelapse.archive_host": "nas.local", "timelapse.archive_dir": "/backups/prints",
             "timelapse.ssh_key": "/data/keys/timelapse_ed25519",
             "services.camera_snapshot_url": "http://cam.local/snapshot.jpg",
             "services.vision_chat_url": "http://vision.local/v1/chat",
             "vision.model": "vision-test-model"},
            config_path=self.local, environ={}, timestamp="20261026-120000")
        self.assertEqual(len(changed), 11)
        data = settings.load(local=self.local, environ={})
        self.assertEqual(data["timelapse"]["render_wait_s"], 600)
        self.assertEqual(data["timelapse"]["render_poll_s"], 5)
        self.assertEqual(data["timelapse"]["max_bytes"], 100_000_000)
        self.assertEqual(data["timelapse"]["local_retention_days"], 3)
        self.assertEqual(data["timelapse"]["plan_clip_retention_days"], 7)
        self.assertEqual(data["timelapse"]["archive_host"], "nas.local")
        self.assertEqual(data["timelapse"]["archive_dir"], "/backups/prints")
        self.assertEqual(data["timelapse"]["ssh_key"], "/data/keys/timelapse_ed25519")
        self.assertEqual(data["services"]["camera_snapshot_url"],
                         "http://cam.local/snapshot.jpg")
        self.assertEqual(data["services"]["vision_chat_url"], "http://vision.local/v1/chat")
        self.assertEqual(data["vision"]["model"], "vision-test-model")
        values = values_by_id(local=self.local, environ={})
        self.assertEqual(values["timelapse.render_wait_s"].provenance, SETTINGS_FILE)
        self.assertEqual(values["services.camera_snapshot_url"].provenance, SETTINGS_FILE)

    def test_invalid_timelapse_and_image_values_are_rejected_unchanged(self):
        before = self.write_file("timelapse:\n  render_wait_s: 900\\n")
        for changes in ({"timelapse.render_wait_s": "soon"},   # not a number
                        {"timelapse.render_poll_s": True},     # bool is not a number here
                        {"timelapse.max_bytes": "200MB"},
                        {"timelapse.local_retention_days": "a week"},
                        {"timelapse.plan_clip_retention_days": None},
                        {"timelapse.archive_host": 42},        # not a string
                        {"services.camera_snapshot_url": "cam.local/snap"},  # no scheme
                        {"services.vision_chat_url": "vision.local"}):
            with self.subTest(changes=changes):
                with self.assertRaises(WebUIConfigError):
                    write_changes(changes, config_path=self.local, environ={})
                self.assertEqual(self.local.read_bytes(), before)
                self.assertFalse(list(Path(self.tmp.name).glob("*.bak-*")))
        # A legacy environment name overrides a timelapse field too.
        with self.assertRaisesRegex(WebUIConfigError, "overridden by the environment"):
            write_changes({"timelapse.render_wait_s": 60}, config_path=self.local,
                          environ={"PRINT_TIMELAPSE_RENDER_WAIT_SECONDS": "60"})
        self.assertEqual(self.local.read_bytes(), before)

    def test_env_overridden_field_write_is_refused(self):
        before = self.write_file("visual_watch:\n  sensitivity: 3\n")
        env = {"PRINT_HANDY__VISUAL_WATCH__SENSITIVITY": "4"}
        with self.assertRaisesRegex(WebUIConfigError, "overridden by the environment"):
            write_changes({"visual_watch.sensitivity": 2}, config_path=self.local, environ=env)
        self.assertEqual(self.local.read_bytes(), before)
        # A legacy environment name overrides too.
        with self.assertRaisesRegex(WebUIConfigError, "overridden by the environment"):
            write_changes({"services.moonraker_url": "http://printer.test:7125"},
                          config_path=self.local, environ={"MOONRAKER_URL": "http://printer.test:7125"})
        self.assertEqual(self.local.read_bytes(), before)

    def test_noop_change_writes_nothing_and_makes_no_backup(self):
        before = self.write_file("visual_watch:\n  sensitivity: 3\n")
        self.assertEqual(write_changes({"visual_watch.sensitivity": 3},
                                       config_path=self.local, environ={}), [])
        self.assertEqual(self.local.read_bytes(), before)
        self.assertFalse(list(Path(self.tmp.name).glob("*.bak-*")))

    def test_toml_target_is_refused_and_unchanged(self):
        target = Path(self.tmp.name) / "local.toml"
        target.write_text("[visual_watch]\nsensitivity = 3\n")
        before = target.read_bytes()
        with self.assertRaisesRegex(WebUIConfigError, "YAML"):
            write_changes({"visual_watch.sensitivity": 2}, config_path=target, environ={})
        self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
