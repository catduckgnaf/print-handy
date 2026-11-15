"""Regression tests for atomic Web UI printer-settings saves (#235)."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from print_handy import settings
from print_handy.webui import config as config_api

ROSTER = (
    "printers:\n"
    "  - id: ad5x\n    model: flashforge.ad5x\n    name: Garage AD5X\n"
    "    moonraker_url: http://ad5x.local:7125\n"
)


class PrinterSettingsTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "local.yaml"
        self.config.write_text(ROSTER, encoding="utf-8")
        self.environ = {"PRINT_HANDY_CONFIG": str(self.config)}

    def test_invalid_entry_keeps_original_bytes_and_leaves_no_stage_files(self):
        before = self.config.read_bytes()
        with self.assertRaises(config_api.WebUIConfigError):
            config_api.write_printer_settings(
                "ad5x", {"model": "not-a-printer-model"},
                {"delivery.methods": ["printer"]}, environ=self.environ,
                timestamp="atomicity-test")

        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in Path(self.tmp.name).iterdir()),
                         ["local.yaml"])

    def test_valid_entry_and_catalog_changes_are_validated_as_one_candidate(self):
        real_validate = config_api._validate_candidate
        validated = []

        def capture(candidate, target, **kwargs):
            validated.append(candidate)
            return real_validate(candidate, target, **kwargs)

        with patch.object(config_api, "_validate_candidate", side_effect=capture):
            catalog_pairs, _warnings, entry_pairs = config_api.write_printer_settings(
                "ad5x", {"name": "Workshop AD5X"},
                {"delivery.methods": ["printer"]}, environ=self.environ,
                timestamp="combined-test")

        self.assertEqual(len(validated), 1)
        self.assertEqual(validated[0]["printers"][0]["name"], "Workshop AD5X")
        self.assertEqual(validated[0]["delivery"]["methods"], ["printer"])
        self.assertEqual(catalog_pairs, [("delivery", "methods")])
        self.assertEqual(entry_pairs, [("ad5x", "name")])
        actual = settings._read(self.config)
        self.assertEqual(actual["printers"][0]["name"], "Workshop AD5X")
        self.assertEqual(actual["delivery"]["methods"], ["printer"])

    def test_save_rejects_combined_candidate_without_partial_catalog_write(self):
        from print_handy.webui import printer_settings
        from print_handy.webui.catalog import field_by_id
        data = settings.load(local=self.config, environ=self.environ)
        real_validate = config_api._validate_candidate

        def reject_combination(candidate, target, **kwargs):
            if (candidate.get("delivery", {}).get("methods") == ["printer"]
                    and candidate["printers"][0].get("name") == "New name"):
                raise config_api.WebUIConfigError("combined candidate refused")
            return real_validate(candidate, target, **kwargs)

        before = self.config.read_bytes()
        with patch.object(printer_settings, "parse_submission", return_value=(
                {"name": "New name"}, {field_by_id("delivery.methods"): ["printer"]}, {}, {})), \
                patch.object(config_api, "_validate_candidate", side_effect=reject_combination):
            result = printer_settings.save(data, "ad5x", {}, environ=self.environ)
        self.assertEqual(result[0], "form")
        self.assertEqual(self.config.read_bytes(), before)

    def test_catalog_and_entry_writers_serialize_without_lost_updates(self):
        first_validation = threading.Event()
        second_validation = threading.Event()
        first_write_done = threading.Event()
        order_lock = threading.Lock()
        call_count = 0
        real_validate = config_api._validate_candidate
        errors = []

        def controlled_validate(candidate, target, **kwargs):
            nonlocal call_count
            with order_lock:
                call_count += 1
                call_number = call_count
            if call_number == 1:
                first_validation.set()
                # With the shared transaction lock, the second writer cannot
                # validate until this call returns and its write has landed.
                second_validation.wait(1)
            else:
                second_validation.set()
                # If writers race, make the second writer validate its stale
                # candidate only after the first write, reproducing a lost update.
                first_write_done.wait(2)
            return real_validate(candidate, target, **kwargs)

        def write_catalog():
            try:
                config_api.write_changes({"delivery.methods": ["printer"]},
                                         environ=self.environ,
                                         timestamp="concurrent-catalog")
            except BaseException as exc:
                errors.append(exc)
            finally:
                first_write_done.set()

        def write_entry():
            try:
                config_api.write_printer_entry("ad5x", {"name": "Concurrent AD5X"},
                                               environ=self.environ,
                                               timestamp="concurrent-entry")
            except BaseException as exc:
                errors.append(exc)

        with patch.object(config_api, "_validate_candidate",
                          side_effect=controlled_validate):
            first = threading.Thread(target=write_catalog)
            second = threading.Thread(target=write_entry)
            first.start()
            self.assertTrue(first_validation.wait(2), "catalog writer never reached validation")
            second.start()
            first.join(5)
            second.join(5)

        self.assertFalse(first.is_alive(), "catalog writer did not finish")
        self.assertFalse(second.is_alive(), "entry writer did not finish")
        self.assertEqual(errors, [])
        actual = settings._read(self.config)
        self.assertEqual(actual["delivery"]["methods"], ["printer"])
        self.assertEqual(actual["printers"][0]["name"], "Concurrent AD5X")


if __name__ == "__main__":
    unittest.main()
