"""#478: debug log mode — lifecycle, capture, listener coverage, export."""
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from print_handy import debug_mode, diagnostics_bundle, logs, sanitize_logs, settings

T0 = 1_760_000_000.0


def _data(tmp):
    path = Path(tmp) / "local.toml"
    path.write_text("")
    data = settings.load(local=path, environ={})
    data["paths"]["state_home"] = Path(tmp) / "profile"
    return data


class DebugModeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = _data(self._tmp.name)
        debug_mode._sync_cache.update({"stamp": None, "started": set(), "active": None})

    def tearDown(self):
        debug_mode.end(self.data)
        debug_mode._sync_cache.update({"stamp": None, "started": set(), "active": None})
        logging.getLogger("print_handy").setLevel(logging.NOTSET)
        logging.getLogger("discord").setLevel(logging.NOTSET)
        self._tmp.cleanup()

    # ------------------------------------------------------------ lifecycle
    def test_lifecycle_on_status_off_and_default_duration(self):
        self.assertIsNone(debug_mode.status(self.data, now=T0))
        state = debug_mode.start(self.data, now=T0)
        self.assertEqual(state["minutes"], debug_mode.DEFAULT_MINUTES)
        self.assertEqual(state["expires_at"], T0 + 30 * 60)
        live = debug_mode.status(self.data, now=T0 + 60)
        self.assertEqual(live["session"], state["session"])
        self.assertIn(state["session"], debug_mode.status_text(self.data, now=T0 + 60))
        row = debug_mode.check_row(self.data, now=T0 + 60)
        self.assertIn("ON", row)
        self.assertIn(debug_mode.MARKER, row)
        ended = debug_mode.end(self.data, now=T0 + 120)
        self.assertEqual(ended["session"], state["session"])
        self.assertIsNone(debug_mode.status(self.data, now=T0 + 120))
        self.assertFalse(debug_mode.state_path(self.data).exists())
        self.assertIsNone(debug_mode.check_row(self.data, now=T0 + 120))

    def test_restart_replaces_the_session_cleanly(self):
        first = debug_mode.start(self.data, now=T0)
        second = debug_mode.start(self.data, minutes=45, now=T0 + 10)
        self.assertNotEqual(first["session"], second["session"])
        self.assertEqual(debug_mode.status(self.data, now=T0 + 20)["session"], second["session"])
        self.assertEqual(second["minutes"], 45)

    def test_auto_off_at_the_default_duration(self):
        debug_mode.start(self.data, now=T0)
        self.assertIsNotNone(debug_mode.status(self.data, now=T0 + 29 * 60))
        self.assertIsNone(debug_mode.status(self.data, now=T0 + 30 * 60 + 1))
        self.assertFalse(debug_mode.state_path(self.data).exists())

    # -------------------------------------------------------------- capture
    def test_capture_markers_and_debug_lines_without_a_log_file(self):
        """Docker-style: no [logging] file configured, capture still works."""
        self.assertEqual(str((self.data.get("logging") or {}).get("file") or ""), "")
        state = debug_mode.start(self.data, now=T0)
        logging.getLogger("print_handy.testprobe").debug("probe line at debug")
        logging.getLogger("discord").debug("noisy library line must not be captured")
        for handler in logging.getLogger().handlers:
            handler.flush()
        debug_mode.end(self.data, now=T0 + 60)
        text = debug_mode.capture_path(self.data).read_text()
        self.assertIn(f"DEBUG_SESSION_START session={state['session']}", text)
        self.assertIn(f"DEBUG_SESSION_END session={state['session']}", text)
        self.assertIn("probe line at debug", text)
        self.assertNotIn("noisy library line", text)
        self.assertEqual(logging.getLogger("discord").level, logging.INFO)

    def test_window_is_the_session_lines_only(self):
        state = debug_mode.start(self.data, now=T0)
        logging.getLogger("print_handy.testprobe").info("inside the window")
        for handler in logging.getLogger().handlers:
            handler.flush()
        debug_mode.end(self.data, now=T0 + 60)
        window = debug_mode.session_window(self.data, session=state["session"])
        self.assertIn("inside the window", window)
        self.assertTrue(window.startswith(
            next(line for line in window.splitlines() if "DEBUG_SESSION_START" in line)))

    # ---------------------------------------------------- listener coverage
    def test_a_fresh_process_observes_the_shared_state_via_poll(self):
        """Simulates a listener process: no apply() ran there, poll() joins."""
        debug_mode.start(self.data, now=T0)
        # Fresh-process state: no sync memory, no capture handler, no level.
        debug_mode._detach_capture()
        debug_mode._sync_cache.update({"stamp": None, "started": set(), "active": None})
        logging.getLogger("print_handy").setLevel(logging.NOTSET)
        self.assertTrue(debug_mode.poll(self.data, now=T0 + 5))
        self.assertEqual(logging.getLogger("print_handy").level, logging.DEBUG)
        self.assertIsNotNone(debug_mode._find_capture(logging.getLogger()))
        # The state file disappearing (another process turned it off) is
        # observed on the next poll too.
        debug_mode.state_path(self.data).unlink()
        debug_mode._sync_cache["stamp"] = ("changed", 0)
        self.assertFalse(debug_mode.poll(self.data, now=T0 + 6))
        self.assertEqual(logging.getLogger("print_handy").level, logging.NOTSET)

    # ---------------------------------------------------------------- export
    def test_export_is_sanitized_named_and_carries_the_report(self):
        state = debug_mode.start(self.data, now=T0)
        logging.getLogger("print_handy.testprobe").info(
            "probe http://192.168.7.7:7125/info for user 123456789012345678 token=abc123def456")
        for handler in logging.getLogger().handlers:
            handler.flush()
        target, report, ended = debug_mode.off_and_export(self.data, now=T0 + 60)
        self.assertEqual(ended["session"], state["session"])
        self.assertRegex(target.name, r"^print-handy-debug-\d{4}-\d{2}-\d{2}-.+\.md$")
        self.assertIn(state["session"], target.name)
        text = target.read_text()
        self.assertNotIn("192.168.7.7", text)
        self.assertNotIn("123456789012345678", text)
        self.assertNotIn("abc123def456", text)
        self.assertIn("Sanitization report:", text)
        self.assertIn(report, text)
        self.assertIn("Debug session log", text)
        self.assertEqual(sanitize_logs.find_residuals(text), {})

    def test_export_after_auto_off_still_finds_the_session(self):
        state = debug_mode.start(self.data, now=T0)
        logging.getLogger("print_handy.testprobe").info("auto-off window line")
        for handler in logging.getLogger().handlers:
            handler.flush()
        self.assertIsNone(debug_mode.status(self.data, now=T0 + 31 * 60))  # auto-off lands
        target, _report, ended = debug_mode.off_and_export(self.data, now=T0 + 31 * 60)
        self.assertEqual(ended["session"], state["session"])
        self.assertIn("auto-off window line", target.read_text())

    def test_export_without_a_session_falls_back_to_the_tail(self):
        _name, text, _report = debug_mode.build_export(self.data, state={}, now=T0)
        self.assertIn("no debug session capture found", text)

    def test_export_sanitizes_every_platforms_ids(self):
        # Telegram/Matrix/approval IDs named in the config must reach the
        # sanitizer too, and a list-valued allowed_user_ids must not be
        # mangled by string splitting (P1-102).
        self.data["telegram"] = {"allowed_user_ids": [555444333222111],
                                 "chat_id": "-1001234567890",
                                 "approval_user_id": "999888777666555"}
        self.data["matrix"] = {"allowed_user_ids": "@ops.lead:matrix.example.org",
                               "room_id": "!Ab3dEf6hI:matrix.example.org",
                               "approval_user_id": "@approver:matrix.example.org"}
        debug_mode.start(self.data, now=T0)
        logging.getLogger("print_handy.testprobe").info(
            "ids 555444333222111 -1001234567890 999888777666555 "
            "@ops.lead:matrix.example.org @approver:matrix.example.org")
        for handler in logging.getLogger().handlers:
            handler.flush()
        target, _report, _ended = debug_mode.off_and_export(self.data, now=T0 + 60)
        text = target.read_text()
        for leaked in ("555444333222111", "1001234567890", "999888777666555",
                       "ops.lead", "approver", "Ab3dEf6hI"):
            self.assertNotIn(leaked, text)
        self.assertEqual(sanitize_logs.find_residuals(text), {})

    def test_cli_off_after_auto_expiry_still_exports(self):
        # The state file is already gone after an auto-off; `off` must
        # still produce the export instead of saying "already off" (P2-188).
        debug_mode.start(self.data, minutes=30, now=T0)
        logging.getLogger("print_handy.testprobe").info("expired session line")
        for handler in logging.getLogger().handlers:
            handler.flush()
        # A status check (the bot's, or a listener poll) lands the auto-off
        # and removes the state file before the operator runs `off`.
        self.assertIsNone(debug_mode.status(self.data, now=T0 + 31 * 60))
        self.assertFalse(debug_mode.state_path(self.data).exists())
        out = []
        with mock.patch.object(debug_mode.settings, "current", lambda: self.data), \
                mock.patch("builtins.print",
                           lambda *a: out.append(" ".join(map(str, a)))):
            self.assertEqual(debug_mode.main(["off"]), 0)
        self.assertIn("Sanitized export:", out[-1])

    def test_cli_off_twice_says_already_off_after_a_manual_off(self):
        # A session that was ended and exported by `off` must not be
        # re-exported by the next `off` (2.2.5 review).
        debug_mode.start(self.data, minutes=30, now=T0)
        out = []
        with mock.patch.object(debug_mode.settings, "current", lambda: self.data), \
                mock.patch("builtins.print",
                           lambda *a: out.append(" ".join(map(str, a)))):
            self.assertEqual(debug_mode.main(["off"]), 0)
            self.assertIn("Sanitized export:", out[-1])
            self.assertEqual(debug_mode.main(["off"]), 0)
            self.assertEqual(out[-1], "Debug mode is already off.")

    def test_cli_status_and_on_off(self):
        out = []
        with mock.patch.object(debug_mode.settings, "current", lambda: self.data), \
                mock.patch("builtins.print",
                                    lambda *a: out.append(" ".join(map(str, a)))):
            self.assertEqual(debug_mode.main(["status"]), 0)
            self.assertIn("Debug mode is off", out[-1])
            self.assertEqual(debug_mode.main(["on", "10"]), 0)
            self.assertIn("Debug mode ON", out[-1])
            self.assertIsNotNone(debug_mode.status(self.data))
            self.assertEqual(debug_mode.main(["off"]), 0)
            self.assertIn("Sanitized export:", out[-1])
            self.assertIsNone(debug_mode.status(self.data))

    # ---------------------------------------------------------------- bundle
    def test_bundle_uses_the_sanitizer_and_report(self):
        log = Path(self._tmp.name) / "pb.log"
        log.write_text("hit http://192.168.9.9:7912/api from /home/someone/x "
                       "for ops@example.com user 123456789012345678\n")
        self.data["logging"] = {"file": str(log)}
        text = diagnostics_bundle.build_bundle(self.data, include_doctor=False)
        self.assertIn("## Sanitization", text)
        self.assertIn("Sanitization report:", text)
        for bad in ("192.168.9.9", "/home/someone", "ops@example.com", "123456789012345678"):
            self.assertNotIn(bad, text)
        self.assertEqual(sanitize_logs.find_residuals(text), {})


if __name__ == "__main__":
    unittest.main()
