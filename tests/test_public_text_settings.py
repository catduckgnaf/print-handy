"""[public_text] denylist settings (#1020 C3): shape, defaults, accessor."""
import tempfile
import unittest
from pathlib import Path

from print_handy import settings


def _data(tmp, extra_toml=""):
    path = Path(tmp) / "local.toml"
    path.write_text(extra_toml)
    return settings.load(local=path, environ={})


class PublicTextSettingsTests(unittest.TestCase):
    def test_default_is_an_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _data(tmp)
            self.assertEqual([], settings.public_text_denylist(data))

    def test_names_load_and_are_trimmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _data(tmp, '[public_text]\ndenylist = [" MyServer ", "", "nas-box"]\n')
            self.assertEqual(["MyServer", "nas-box"], settings.public_text_denylist(data))

    def test_a_non_list_shape_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(settings.SettingsError):
                _data(tmp, "[public_text]\ndenylist = 42\n")


if __name__ == "__main__":
    unittest.main()
