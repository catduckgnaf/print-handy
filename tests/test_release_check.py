"""Tests for the release check (scripts/check_release.py).

The check exists because of named past failures (docs/RELEASE-CHECKLIST.md,
audit rows A2/A10/A12/A13): notes assembled from memory missed merged PRs,
and release notes were never spelling-scanned because they are not a PR
diff. These tests pin the fail-closed behavior on fixture checkouts.
"""

import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_release.py"
sys.path.insert(0, str(ROOT / "scripts"))
import check_release  # noqa: E402


class SubjectParsing(unittest.TestCase):
    def test_trailing_squash_numbers_only(self):
        subjects = [
            "feat: thing (#123)",
            "fix: other (#45) ",
            "docs: mentions #999 in the middle",
            "chore: no number at all",
            "merge: (#123) duplicate",
        ]
        self.assertEqual(
            check_release.pr_numbers_from_subjects(subjects), [45, 123])

    def test_internal_section_bounds(self):
        text = ("# Unreleased\n\n- x\n\n# Version 1.2.3\n\n- a #10\n\n"
                "# Version 1.2.2\n\n- b #20\n")
        section = check_release.internal_section(text, "1.2.3")
        self.assertIn("#10", section)
        self.assertNotIn("#20", section)
        self.assertIsNone(check_release.internal_section(text, "9.9.9"))


class ReleaseCheckBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.root = base / "repo"
        (self.root / "print_handy").mkdir(parents=True)
        (self.root / "docs").mkdir(parents=True)
        self.write_tree(version="1.2.3", internal_has_section=True)
        self.notes = base / "notes.md"
        self.notes.write_text("Covers #10 and #20, in color.\n")
        self.range_file = base / "range.txt"
        self.range_file.write_text("feat: a (#10)\nfix: b (#20)\n")

    def tearDown(self):
        self.tmp.cleanup()

    def write_tree(self, version, internal_has_section):
        (self.root / "print_handy" / "__init__.py").write_text(
            f'__version__ = "{version}"\n')
        (self.root / "README.md").write_text(
            f"Current release: **{version}**\n")
        section = (f"# Version {version}\n\n- a #10\n- b #20\n"
                   if internal_has_section else "# Unreleased\n\n- x\n")
        (self.root / "docs" / "INTERNAL_RELEASE_NOTES.md").write_text(
            "# Print Handy Internal Release Notes\n\n" + section)

    def run_cli(self, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--version", "1.2.3",
             "--notes", str(self.notes), "--root", str(self.root),
             "--internal-notes",
             str(self.root / "docs" / "INTERNAL_RELEASE_NOTES.md"),
             "--range-file", str(self.range_file), *extra],
            capture_output=True, text=True)

    def test_complete_release_passes(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_missing_pr_in_notes_fails_closed(self):
        self.notes.write_text("Covers #10 only.\n")
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("#20", result.stdout)

    def test_missing_internal_section_fails_closed(self):
        self.write_tree(version="1.2.3", internal_has_section=False)
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no '# Version 1.2.3' section", result.stdout)

    def test_version_mismatch_fails_closed(self):
        self.write_tree(version="1.2.2", internal_has_section=True)
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("__version__ is 1.2.2", result.stdout)
        self.assertIn("Current release is 1.2.2", result.stdout)

    def test_british_spelling_in_notes_fails_closed(self):
        self.notes.write_text("Covers #10 and #20, in colour.\n")
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("color", result.stdout)

    def test_empty_range_fails_closed(self):
        self.range_file.write_text("no numbers here\n")
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("empty ancestry range", result.stdout)


if __name__ == "__main__":
    unittest.main()
