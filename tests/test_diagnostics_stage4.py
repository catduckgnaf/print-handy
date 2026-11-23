"""#390 Stage 4: per-subsystem log levels and their settings validation."""
import logging
import tempfile
import unittest
from pathlib import Path

import toml_twin

from print_handy import logs, settings


class LevelsSettingsTests(unittest.TestCase):
    def _load(self, toml):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.toml"
            path.write_text(toml)
            return settings.load(local=toml_twin.yaml_fixture(path), environ={})

    def test_levels_are_validated_and_normalised(self):
        data = self._load('[logging]\nlevels = { "print_handy.core.orca_adapter" = "debug" }\n')
        self.assertEqual(data["logging"]["levels"],
                         {"print_handy.core.orca_adapter": "DEBUG"})

    def test_bad_level_is_a_settings_error_naming_the_key(self):
        with self.assertRaises(settings.SettingsError) as ctx:
            self._load('[logging]\nlevels = { "print_handy.core" = "CHATTY" }\n')
        self.assertIn("levels.print_handy.core", str(ctx.exception))

    def test_levels_default_to_empty(self):
        self.assertEqual(self._load("")["logging"]["levels"], {})


class ConfigureLevelsTests(unittest.TestCase):
    def tearDown(self):
        logging.getLogger("print_handy.core.orca_adapter").setLevel(logging.NOTSET)
        logs.configure({"level": "INFO", "format": "text"})

    def test_configure_applies_the_subsystem_level_only_there(self):
        logs.configure({"level": "INFO", "format": "text",
                        "levels": {"print_handy.core.orca_adapter": "DEBUG"}})
        self.assertEqual(logging.getLogger("print_handy.core.orca_adapter").level,
                         logging.DEBUG)
        self.assertEqual(logging.getLogger("print_handy.core.model_job").level,
                         logging.NOTSET)


if __name__ == "__main__":
    unittest.main()
