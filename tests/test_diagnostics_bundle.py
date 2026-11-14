"""#390 Stage 3: the diagnostics bundle — complete, bounded, and secret-free."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from print_handy import diagnostics_bundle, settings


def _data(tmp, extra_toml=""):
    path = Path(tmp) / "local.toml"
    path.write_text(extra_toml)
    return settings.load(local=path, environ={})


class BundleTests(unittest.TestCase):
    def test_bundle_has_every_section_and_no_secret_value(self):
        secret = "bundle-secret-value-123"
        with tempfile.TemporaryDirectory() as tmp:
            data = _data(tmp)
            with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": secret,
                                              "PRINT_HANDY_IMAGE_DIGEST": "sha256:deadbeef"}):
                text = diagnostics_bundle.build_bundle(data, include_doctor=False)
        for heading in ("# Print Handy diagnostics bundle", "## Configuration summary",
                        "## Printers", "## Doctor", "## Recent log tail"):
            self.assertIn(heading, text)
        self.assertIn("sha256:deadbeef", text)
        self.assertNotIn(secret, text)

    def test_secret_shaped_values_render_as_env_indirection_only(self):
        summary = diagnostics_bundle._summarise(
            {"token_env": "DISCORD_BOT_TOKEN", "api_key": "never-rendered"}, "discord")
        self.assertIn("DISCORD_BOT_TOKEN", str(summary))
        self.assertNotIn("never-rendered", str(summary))

    def test_log_tail_is_bounded_and_redacted(self):
        secret = "tail-secret-value-456"
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "pb.log"
            log.write_text("\n".join(f"line {i} token={secret}" for i in range(50)))
            data = _data(tmp)
            data["logging"] = {"file": str(log)}
            with mock.patch.dict(os.environ, {"SOME_TOKEN": secret}):
                tail = diagnostics_bundle.log_tail(data, lines=5)
        self.assertEqual(len(tail.splitlines()), 5)
        self.assertNotIn(secret, tail)

    def test_long_url_credentials_redact_before_truncation(self):
        # The userinfo @ sits past the 77-char truncation point: truncating
        # first would strand the password with no @ left for the credential
        # pattern in logs.redact to match (P1-95).
        value = "http://" + "a" * 65 + ":s3cretpw@cam.local:7125/" + "x" * 30
        summary = diagnostics_bundle._summarise({"moonraker_url": value}, "services")
        self.assertNotIn("s3cretpw", str(summary))
        self.assertNotIn("s3cr", str(summary))

    def test_zero_log_lines_returns_no_lines_not_the_whole_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "pb.log"
            log.write_text("first line\nsecond line\n")
            data = _data(tmp)
            data["logging"] = {"file": str(log)}
            for count in (0, -3):
                tail = diagnostics_bundle.log_tail(data, lines=count)
                self.assertNotIn("first line", tail)
                self.assertIn("no log lines", tail)

    def test_bundle_is_produced_when_settings_do_not_load(self):
        with mock.patch.object(diagnostics_bundle.settings, "current",
                               side_effect=settings.SettingsError("[printer] model is bad")):
            text = diagnostics_bundle.build_bundle(include_doctor=False)
        for heading in ("# Print Handy diagnostics bundle", "## Configuration summary",
                        "## Printers", "## Recent log"):
            self.assertIn(heading, text)
        self.assertIn("could not be loaded", text)

    def test_write_bundle_with_explicit_path_when_settings_do_not_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "bundle.md"
            with mock.patch.object(diagnostics_bundle.settings, "current",
                                   side_effect=settings.SettingsError("bad config")):
                written = diagnostics_bundle.write_bundle(target)
            self.assertEqual(written, target)
            self.assertIn("diagnostics bundle", target.read_text())

    def test_known_ids_cover_approval_and_bot_ids(self):
        users, channels = diagnostics_bundle._known_ids({
            "discord": {"allowed_user_ids": "111, 222", "approval_user_id": "333",
                        "chat_id": "444"},
            "telegram": {"allowed_user_ids": [555], "approval_user_id": "666",
                         "chat_id": "-777"},
            "matrix": {"allowed_user_ids": "@a:example.org",
                       "approval_user_id": "@b:example.org",
                       "bot_user_id": "@bot:example.org", "room_id": "!r:example.org"},
        })
        for expected in ("111", "222", "333", "555", "666", "@a:example.org",
                         "@b:example.org", "@bot:example.org"):
            self.assertIn(expected, users)
        for expected in ("444", "-777", "!r:example.org"):
            self.assertIn(expected, channels)

    def test_missing_log_file_setting_is_explained_not_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tail = diagnostics_bundle.log_tail(_data(tmp))
        self.assertIn("no [logging] file is configured", tail)

    def test_write_bundle_uses_state_dir_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _data(tmp)
            target = diagnostics_bundle.write_bundle(data=data)
            self.assertTrue(target.is_file())
            self.assertEqual(target.name, diagnostics_bundle.BUNDLE_NAME)
            self.assertIn("diagnostics bundle", target.read_text())


if __name__ == "__main__":
    unittest.main()


class CrashAndFallbackTests(unittest.TestCase):
    """#1404: the bundle's Crashes section, debug header, capture tail
    fallback, and the runtime section's exit status / heartbeat honesty."""

    def _data(self, tmp, extra_toml=""):
        path = Path(tmp) / "local.toml"
        path.write_text(f'[paths]\nstate_home = "{tmp}/hermes"\ncache = "{tmp}/cache"\n'
                        + extra_toml)
        return settings.load(local=path, environ={})

    def _record_crash(self, data, message="kaput in the worker"):
        import sys

        from print_handy import crash_report

        try:
            raise ValueError(message)
        except ValueError:
            return crash_report.record(*sys.exc_info(), role="bot", data=data, now=1000.0)

    def test_bundle_carries_a_planted_crash_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp)
            self._record_crash(data)
            text = diagnostics_bundle.build_bundle(data, include_doctor=False)
        self.assertIn("## Crashes", text)
        self.assertIn("ValueError", text)
        self.assertIn("kaput in the worker", text)

    def test_bundle_without_crashes_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp)
            text = diagnostics_bundle.build_bundle(data, include_doctor=False)
        self.assertIn("## Crashes", text)
        self.assertIn("(no crashes recorded)", text)

    def test_bundle_header_shows_the_debug_mode_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp)
            text = diagnostics_bundle.build_bundle(data, include_doctor=False)
        self.assertIn("- Debug mode:", text)

    def test_runtime_section_shows_the_last_exit_status(self):
        from print_handy import supervisor_state

        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp)
            printer_id = settings.default_printer_id(data)
            supervisor_state.write(printer_id, restarts=1, budget=5, window_s=600,
                                   last_exit_status=1, data=data)
            section = diagnostics_bundle.runtime_section(data, now=1000.0)
        self.assertIn("last exit status 1", section)

    def test_runtime_section_names_an_unreadable_heartbeat(self):
        from print_handy import healthcheck

        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp)
            printer_id = settings.default_printer_id(data)
            beat_path = healthcheck.heartbeat_path(data, printer_id)
            beat_path.parent.mkdir(parents=True, exist_ok=True)
            beat_path.write_text("{not json")
            section = diagnostics_bundle.runtime_section(data, now=1000.0)
        self.assertIn("heartbeat file unreadable", section)
        self.assertNotIn("no heartbeat file", section)

    def test_log_tail_falls_back_to_the_debug_capture_file(self):
        from print_handy import debug_mode

        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp)
            capture = debug_mode.capture_path(data)
            capture.parent.mkdir(parents=True, exist_ok=True)
            capture.write_text("capture line one\ncapture line two\n")
            tail = diagnostics_bundle.log_tail(data)
        self.assertIn("debug capture file", tail)
        self.assertIn("capture line one", tail)
        self.assertIn("capture line two", tail)

    def test_log_tail_without_a_capture_names_the_debug_session_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp)
            tail = diagnostics_bundle.log_tail(data)
        self.assertIn("no [logging] file is configured", tail)
        self.assertIn("/print-handy debug on", tail)
        self.assertIn("build the bundle again", tail)


class RuntimeAndPrinterLogsTests(unittest.TestCase):
    """#1020 C1/C2: runtime status in the bundle, and --printer-logs."""

    def _data(self, tmp, extra_toml=""):
        path = Path(tmp) / "local.toml"
        path.write_text(f'[paths]\nstate_home = "{tmp}/hermes"\ncache = "{tmp}/cache"\n'
                        + extra_toml)
        return settings.load(local=path, environ={})

    def test_runtime_section_reports_heartbeat_supervisor_and_captures(self):
        import json as _json

        from print_handy import healthcheck, supervisor_state

        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp)
            printer_id = settings.default_printer_id(data)
            beat_path = healthcheck.heartbeat_path(data, printer_id)
            beat_path.parent.mkdir(parents=True, exist_ok=True)
            beat_path.write_text(_json.dumps(
                {"time": 900.0, "loop_lag_ms": 2500, "runtime": "x", "pid": 1}))
            supervisor_state.write(printer_id, restarts=6, budget=5, window_s=600,
                                   exhausted=True, data=data)
            long_name = "p" * 120
            (Path(tmp) / "cache" / "timelapse" / long_name).mkdir(parents=True)
            with mock.patch.dict(os.environ, {"PRINT_HANDY_IMAGE_TAG": "nightly-2026-11-07",
                                              "PRINT_HANDY_IMAGE_SHA": "7c3a2bd"}):
                section = diagnostics_bundle.runtime_section(data, now=1000.0)
        self.assertIn("tag nightly-2026-11-07, commit 7c3a2bd", section)
        self.assertIn("heartbeat age 100s, loop lag 2.5s", section)
        self.assertIn("supervisor restarts 6 of budget 5 in 600s", section)
        self.assertIn("BUDGET EXHAUSTED", section)
        self.assertIn(f"{long_name} (120)", section)

    def test_runtime_section_survives_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp)
            section = diagnostics_bundle.runtime_section(data, now=1000.0)
        self.assertIn("no heartbeat file", section)
        self.assertIn("not recorded", section)

    def test_bundle_carries_the_runtime_and_stack_dump_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp)
            text = diagnostics_bundle.build_bundle(data, include_doctor=False)
        self.assertIn("## Runtime status", text)
        self.assertIn("## Listener stack dumps", text)

    def _printer_data(self, tmp):
        return self._data(tmp, '[[printers]]\nid = "ad5x"\nmodel = "flashforge.ad5x"\n'
                               'moonraker_url = "http://printer.invalid:7125"\n')

    def test_printer_logs_pull_console_klippy_and_zmod_with_clock_labels(self):
        import json as _json

        store = _json.dumps({"result": {"gcode_store": [
            {"message": "// hello", "time": 1762470000.0, "type": "response"},
            {"message": "!! Filament jam detected (IFS)", "time": 1762470061.0, "type": "response"},
        ]}}).encode()
        listing = _json.dumps({"result": {"dirs_files": [
            {"path": "klippy.log"}, {"path": "console.log"}, {"path": "moonraker.log"}]}}).encode()
        files = {
            "/server/gcode_store?count=200": store,
            "/server/files/directory?path=logs": listing,
            "/server/files/logs/klippy.log": b"klippy line one\nklippy line two\n",
            "/server/files/logs/console.log": b"12:40:31 jam on console clock\n",
        }

        def fetch(url):
            for suffix, payload in files.items():
                if url.endswith(suffix):
                    return payload
            return None

        with tempfile.TemporaryDirectory() as tmp:
            data = self._printer_data(tmp)
            section = diagnostics_bundle.printer_logs_section(data, fetch=fetch)
        self.assertIn("Filament jam detected (IFS)", section)
        self.assertIn("epoch-anchored and shown in UTC", section)
        self.assertIn("klippy line two", section)
        self.assertIn("10 hours behind Moonraker time", section)
        self.assertIn("jam on console clock", section)

    def test_printer_logs_degrade_when_the_printer_is_unreachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._printer_data(tmp)
            section = diagnostics_bundle.printer_logs_section(data, fetch=lambda url: None)
        self.assertIn("Console history: unavailable", section)
        self.assertIn("no klippy, console or screen log", section)

    def test_printer_logs_skip_non_moonraker_printers(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp)
            section = diagnostics_bundle.printer_logs_section(data, fetch=lambda url: None)
        self.assertIn("not a Moonraker printer", section)

