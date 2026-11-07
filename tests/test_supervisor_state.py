"""Supervisor listener state (#1020): the record supervise.sh writes and
the bundle and outage alerts read."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from print_handy import settings, supervisor_state  # noqa: E402


def _data(tmp):
    path = Path(tmp) / "local.toml"
    path.write_text(f'[paths]\nhermes_home = "{tmp}/hermes"\n')
    return settings.load(local=path, environ={})


class SupervisorStateTests(unittest.TestCase):
    def test_write_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _data(tmp)
            printer_id = settings.default_printer_id(data)
            target = supervisor_state.write(
                printer_id, restarts=3, budget=5, window_s=600,
                last_exit_status=1, now=1234.5, data=data)
            self.assertTrue(target.is_file())
            record = supervisor_state.read(printer_id, data=data)
            self.assertEqual(3, record["restarts"])
            self.assertEqual(5, record["budget"])
            self.assertEqual(600, record["window_s"])
            self.assertEqual(1, record["last_exit_status"])
            self.assertFalse(record["exhausted"])
            self.assertEqual(1234.5, record["updated_at"])

    def test_exhausted_flag_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _data(tmp)
            printer_id = settings.default_printer_id(data)
            supervisor_state.write(printer_id, restarts=6, budget=5, window_s=600,
                                   exhausted=True, data=data)
            self.assertTrue(supervisor_state.read(printer_id, data=data)["exhausted"])

    def test_missing_and_broken_records_read_as_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _data(tmp)
            printer_id = settings.default_printer_id(data)
            self.assertIsNone(supervisor_state.read(printer_id, data=data))
            target = supervisor_state.path_for(printer_id, data)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{not json")
            self.assertIsNone(supervisor_state.read(printer_id, data=data))

    def test_read_all_covers_only_printers_with_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _data(tmp)
            printer_id = settings.default_printer_id(data)
            self.assertEqual({}, supervisor_state.read_all(data))
            supervisor_state.write(printer_id, restarts=1, budget=5, window_s=600, data=data)
            self.assertEqual([printer_id], sorted(supervisor_state.read_all(data)))

    def test_cli_writes_the_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _data(tmp)
            printer_id = settings.default_printer_id(data)
            with mock.patch.object(supervisor_state.settings, "current",
                                   return_value=data):
                rc = supervisor_state.main(["--printer", printer_id, "--restarts", "2",
                                            "--budget", "5", "--window", "600",
                                            "--status", "0"])
            self.assertEqual(0, rc)
            self.assertEqual(2, supervisor_state.read(printer_id, data=data)["restarts"])


if __name__ == "__main__":
    unittest.main()
