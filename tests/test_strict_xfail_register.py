"""The strict xfail register matches the markers, and rejects a stale one."""
import importlib.util
import json
import tempfile
import textwrap
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_strict_xfails", ROOT / "scripts" / "check_strict_xfails.py")
assert SPEC is not None and SPEC.loader is not None
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class RegisterTests(unittest.TestCase):
    def test_the_repository_register_matches_its_markers(self):
        self.assertEqual([], checker.check(ROOT, date(2026, 11, 4)))

    def write_tree(self, directory, register, test_text, case=None):
        root = Path(directory)
        (root / "tests").mkdir()
        (root / "tests" / "test_sample.py").write_text(test_text, encoding="utf-8")
        if case is not None:
            folder = root / "tests" / "corpus" / "cases" / "sample-case"
            folder.mkdir(parents=True)
            (folder / "case.toml").write_text(case, encoding="utf-8")
        (root / "tests" / "strict_xfail_register.json").write_text(
            json.dumps({"_doc": "test register", "entries": register}), encoding="utf-8")
        return root

    def test_an_unregistered_marker_and_corpus_case_fail(self):
        with self.temporary() as directory:
            root = self.write_tree(directory, [], textwrap.dedent("""\
                import pytest
                @pytest.mark.xfail(strict=True, reason="open defect")
                def test_open():
                    assert False
                """), case='known_failure = "#1 still open"\n')
            errors = checker.check(root, date(2026, 11, 4))
        self.assertIn("strict xfail has no register entry: tests/test_sample.py::test_open", errors)
        self.assertIn("strict xfail has no register entry: corpus:sample-case", errors)

    def test_a_registered_marker_passes_until_its_expiry(self):
        entry = {"node_id": "tests/test_sample.py::test_open", "owner": "print-handy",
                 "expiry": "2026-12-01", "reason": "Tracked defect, marker removed with the fix."}
        text = textwrap.dedent("""\
            import pytest
            @pytest.mark.xfail(strict=True, reason="open defect")
            def test_open():
                assert False
            """)
        with self.temporary() as directory:
            root = self.write_tree(directory, [entry], text)
            self.assertEqual([], checker.check(root, date(2026, 11, 4)))
            self.assertTrue(any("expired" in error for error in checker.check(root, date(2026, 12, 2))))

    def test_a_register_entry_without_a_marker_fails(self):
        entry = {"node_id": "tests/test_sample.py::test_gone", "owner": "print-handy",
                 "expiry": "2026-12-01", "reason": "The marker was removed with the fix."}
        with self.temporary() as directory:
            root = self.write_tree(directory, [entry], "def test_other():\n    assert True\n")
            errors = checker.check(root, date(2026, 11, 4))
        self.assertIn("register entry has no strict xfail: tests/test_sample.py::test_gone", errors)

    def test_a_non_strict_xfail_needs_no_entry(self):
        text = textwrap.dedent("""\
            import pytest
            @pytest.mark.xfail(reason="flaky, not strict")
            def test_soft():
                assert False
            """)
        with self.temporary() as directory:
            root = self.write_tree(directory, [], text)
            self.assertEqual([], checker.check(root, date(2026, 11, 4)))

    def temporary(self):
        return tempfile.TemporaryDirectory()


if __name__ == "__main__":
    unittest.main()
