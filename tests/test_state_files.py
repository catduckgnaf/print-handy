"""Stray Print Handy state files: detection, safe removal, and the doctor line. Uses a temp HOME only."""
import io
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from print_handy import doctor, settings, state_files

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


class StrayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.home = self.base / "home"
        self.root = self.home / ".hermes"
        self.profile = self.root / "profiles" / "main"
        self.sibling = self.root / "profiles" / "other"
        self.state = self.profile / "state"
        for folder in (self.home, self.state, self.root / "state", self.sibling / "state"):
            folder.mkdir(parents=True, exist_ok=True)
        patch = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)
        self.config = settings.load(local=Path(os.devnull), environ={"PRINT_HANDY__PATHS__STATE_HOME": str(self.profile)})

    def paths(self, strays):
        return {s.path for s in strays}

    def test_clean_install_has_no_strays(self):
        (self.state / "print_alert_buttons.json").write_text('{"a": 1}')
        self.assertEqual([], state_files.find_strays(self.config))

    def test_finds_strays_in_every_scanned_place(self):
        (self.root / "state" / "print_alert_buttons.json").write_text("{}")
        (self.sibling / "state" / "printer_hardware.json").write_text('{"x": 1}')
        (self.sibling / "state" / "gcode-bounds-cache").mkdir()
        (self.home / ".print_bitch_context.json").write_text('{"x": 1}')
        (self.state / "print_handy_context.json").write_text('{"x": 2}')
        (self.state / "print_bitch_context.json").write_text('{"x": 3}')
        found = {s.path: s for s in state_files.find_strays(self.config)}
        self.assertEqual({self.root / "state" / "print_alert_buttons.json",
                          self.sibling / "state" / "printer_hardware.json",
                          self.sibling / "state" / "gcode-bounds-cache",
                          self.home / ".print_bitch_context.json",
                          self.state / "print_bitch_context.json"}, set(found))
        self.assertEqual(self.state / "print_alert_buttons.json", found[self.root / "state" / "print_alert_buttons.json"].canonical)
        self.assertEqual(self.state / "print_handy_context.json", found[self.home / ".print_bitch_context.json"].canonical)

    def test_unknown_names_and_no_recursive_scan(self):
        (self.root / "state" / "notes.txt").write_text("mine")
        (self.home / "projects").mkdir()
        (self.home / "projects" / "print_alert_buttons.json").write_text("{}")
        self.assertEqual([], state_files.find_strays(self.config))

    def test_profile_outside_a_profiles_folder_scans_only_home_and_old_names(self):
        solo = self.base / "solo"
        (solo / "state").mkdir(parents=True)
        config = settings.load(local=Path(os.devnull), environ={"PRINT_HANDY__PATHS__STATE_HOME": str(solo)})
        (self.base / "state").mkdir()
        (self.base / "state" / "print_alert_buttons.json").write_text("{}")
        self.assertEqual([], state_files.find_strays(config))

    def test_removal_needs_a_profile_copy_or_empty_content(self):
        covered = self.root / "state" / "print_alert_buttons.json"
        covered.write_text('{"real": 2}')
        (self.state / "print_alert_buttons.json").write_text('{"real": 2}')
        lonely = self.sibling / "state" / "printer_hardware.json"
        lonely.write_text('{"only": "copy"}')
        trivial = self.root / "state" / "maintenance.json"
        trivial.write_text("{}")
        empty_dir = self.root / "state" / "timelapse_plans"
        empty_dir.mkdir()
        removed, kept = state_files.remove_strays(state_files.find_strays(self.config))
        self.assertEqual({covered, trivial, empty_dir}, self.paths(s for s, _ in removed))
        self.assertEqual({lonely}, self.paths(s for s, _ in kept))
        self.assertIn("another profile", kept[0][1])
        self.assertFalse(covered.exists() or trivial.exists() or empty_dir.exists())
        self.assertEqual('{"only": "copy"}', lonely.read_text())
        self.assertEqual('{"real": 2}', (self.state / "print_alert_buttons.json").read_text())

    def test_other_profiles_state_is_never_deleted(self):
        # A sibling may be another Print Handy install: even an empty or identical copy stays.
        identical = self.sibling / "state" / "printer_hardware.json"
        identical.write_text('{"same": 1}')
        (self.state / "printer_hardware.json").write_text('{"same": 1}')
        empty = self.sibling / "state" / "maintenance.json"
        empty.write_text("{}")
        removed, kept = state_files.remove_strays(state_files.find_strays(self.config))
        self.assertEqual([], removed)
        self.assertEqual({identical, empty}, self.paths(s for s, _ in kept))
        self.assertTrue(identical.exists() and empty.exists())

    def test_root_state_differing_file_is_kept(self):
        theirs = self.root / "state" / "maintenance.json"
        theirs.write_text('{"their": "data"}')
        (self.state / "maintenance.json").write_text('{"ours": 1}')
        removed, kept = state_files.remove_strays(state_files.find_strays(self.config))
        self.assertEqual([], removed)
        self.assertIn("differs", kept[0][1])

    def test_legacy_and_old_name_removed_once_new_copy_exists(self):
        (self.home / ".moonraker_discord_announcements.json").write_text('{"m": 1}')
        (self.state / "moonraker_discord_announcements.json").write_text('{"m": 1}')
        (self.state / "print_bitch_start_claims.json").write_text('{"c": 1}')
        removed, kept = state_files.remove_strays(state_files.find_strays(self.config))
        self.assertEqual(1, len(removed))
        self.assertEqual([self.state / "print_bitch_start_claims.json"], [s.path for s, _ in kept])
        self.assertFalse((self.home / ".moonraker_discord_announcements.json").exists())

    def test_dry_run_touches_nothing(self):
        stray = self.root / "state" / "print_alert_buttons.json"
        stray.write_text("{}")
        removed, _ = state_files.remove_strays(state_files.find_strays(self.config), dry_run=True)
        self.assertEqual(1, len(removed))
        self.assertTrue(stray.exists())

    def test_symlinks_are_unlinked_not_followed(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        (self.state / "model-link-intakes").mkdir()
        link = self.root / "state" / "model-link-intakes"
        link.symlink_to(outside, target_is_directory=True)
        removed, kept = state_files.remove_strays(state_files.find_strays(self.config))
        self.assertEqual([link], [s.path for s, _ in removed])
        self.assertFalse(link.is_symlink())
        self.assertEqual("keep", (outside / "keep.txt").read_text())

    def test_root_state_linked_to_the_profile_state_is_not_a_stray(self):
        (self.root / "state").rmdir()
        (self.root / "state").symlink_to(self.state, target_is_directory=True)
        (self.state / "print_alert_buttons.json").write_text('{"a": 1}')
        self.assertEqual([], state_files.find_strays(self.config))

    def test_cli_reports_then_removes(self):
        stray = self.root / "state" / "print_alert_buttons.json"
        stray.write_text("{}")
        with mock.patch.object(state_files.settings, "current", return_value=self.config):
            out = io.StringIO()
            self.assertEqual(0, state_files.main([], out))
            self.assertIn(f"stray {stray}", out.getvalue())
            self.assertTrue(stray.exists())
            out = io.StringIO()
            state_files.main(["--remove", "--dry-run"], out)
            self.assertIn(f"would remove {stray}", out.getvalue())
            self.assertTrue(stray.exists())
            out = io.StringIO()
            state_files.main(["--remove"], out)
            self.assertIn(f"removed {stray}", out.getvalue())
            self.assertFalse(stray.exists())
            out = io.StringIO()
            state_files.main([], out)
            self.assertIn("No stray", out.getvalue())

    def test_doctor_line(self):
        self.assertIsNone(doctor.check_strays(self.config))
        stray = self.root / "state" / "print_alert_buttons.json"
        stray.write_text("{}")
        warning = doctor.check_strays(self.config)
        self.assertIn(str(stray), warning)
        self.assertIn("1 outside", warning)


if __name__ == "__main__":
    unittest.main()
