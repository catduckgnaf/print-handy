"""The state file name registry (state_files.KNOWN) matches the names the code uses."""
import re
import unittest
from pathlib import Path

from print_handy import state_files

ROOT = Path(__file__).resolve().parents[1]
SOURCE_NAME = re.compile(
    r"""(?:state_file\(\s*(?:None|["'][A-Za-z_]+["'])\s*,\s*|state_dir\([^)]*\)\s*/\s*)["']([^"']+)["']""")


class KnownNamesTests(unittest.TestCase):
    def source_names(self):
        names = set()
        for folder in ("print_handy", "plugins"):
            for path in (ROOT / folder).rglob("*.py"):
                names.update(SOURCE_NAME.findall(path.read_text()))
        return names

    def test_every_state_name_in_the_code_is_known(self):
        missing = self.source_names() - set(state_files.KNOWN)
        self.assertFalse(missing, f"add to print_handy/state_files.py KNOWN: {sorted(missing)}")

    def test_known_has_no_stale_names(self):
        stale = set(state_files.KNOWN) - self.source_names()
        self.assertFalse(stale, f"no longer used by the code: {sorted(stale)}")


if __name__ == "__main__":
    unittest.main()
