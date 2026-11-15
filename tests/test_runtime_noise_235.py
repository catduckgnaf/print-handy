"""Offline regressions for expected runtime failures in 2.3.5."""
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from print_handy.runtime import print_control as pc, panic, spool_sync
from print_handy.runtime import discord_print_summary as summary


class RuntimeNoiseTests(unittest.TestCase):
    def test_force_reboot_warns_once_then_debugs_repeated_poll_failures(self):
        reader = Mock(side_effect=OSError("offline"))
        with self.assertLogs(pc.__name__, level="DEBUG") as captured, \
                patch.object(pc.time, "sleep"), \
                patch.object(pc.time, "monotonic", side_effect=[0, 0, 0, 0, 2]):
            result = pc.force_reboot(
                wait_grace_s=0, max_wait_s=1, poll_s=0,
                state_reader=lambda: {"state": "idle"}, gcode_sender=Mock(),
                moonraker_reader=reader, native_reader=Mock())
        self.assertFalse(result[0])
        self.assertEqual([r.levelno for r in captured.records],
                         [logging.WARNING, logging.DEBUG, logging.DEBUG])

    def test_panic_reboot_warns_once_then_debugs(self):
        with self.assertLogs(panic.__name__, level="DEBUG") as captured, \
                patch.object(panic.time, "sleep"), \
                patch.object(panic.time, "monotonic", side_effect=[0, 1, 1, 1, 1, 1, 3]):
            result = panic.force_restart(
                caller=Mock(), status_reader=lambda: {}, wait_s=0,
                reboot_grace_s=0, reboot_wait_s=1, poll_s=0,
                moonraker_reader=Mock(side_effect=OSError("offline")),
                native_reader=Mock())
        self.assertFalse(result["ok"])
        records = [r for r in captured.records if "idle check failed" in r.getMessage()]
        self.assertEqual([r.levelno for r in records],
                         [logging.WARNING, logging.DEBUG, logging.DEBUG])

    def test_normal_bind_without_per_printer_copy_is_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "printer_hardware.json"
            target.write_text("{}")
            with patch.object(spool_sync._config, "cfg", return_value={"printers": [{"id": "default"}]}), \
                    self.assertNoLogs(spool_sync.__name__, level="WARNING"):
                self.assertIsNone(spool_sync.spool_loaded_elsewhere(42, state_path=target))

    def test_non_zmod_missing_object_is_quiet_but_auth_failure_warns(self):
        for code in (400, 404, 401):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "printer_hardware.json"
                target.write_text(json.dumps({"filament_slots": {"1": {"spoolman_id": 42}}}))
                mr = Mock(side_effect=HTTPError("http://printer.example", code, "unavailable", {}, None))
                context = (self.assertLogs(spool_sync.__name__, level="WARNING") if code == 401
                           else self.assertNoLogs(spool_sync.__name__, level="WARNING"))
                with context:
                    spool_sync.backfill_slot_colors(
                        state_path=target, client=Mock(return_value={"filament": {"id": 1, "color_hex": "FFFFFF"}}), mr=mr)
                mr.assert_called_once()

    def test_missing_sidecar_is_quiet_but_invalid_sidecar_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sidecar = root / "missing.json"
            with self.assertNoLogs(summary.__name__, level="WARNING"):
                self.assertEqual(summary.find_job("unknown.gcode", archive=root, context_path=sidecar), (None, None))
            sidecar.write_text("invalid json")
            with self.assertLogs(summary.__name__, level="WARNING"):
                self.assertEqual(summary.find_job("unknown.gcode", archive=root, context_path=sidecar), (None, None))
