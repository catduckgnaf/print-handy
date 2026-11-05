"""Offline regression coverage for the US-spelling prose check."""
import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_us_spelling.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"


def load_checker():
    spec = importlib.util.spec_from_file_location("us_spelling", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_diff(added=None, context=None, removed=None, path="docs/GUIDE.md"):
    lines = [
        f"diff --git a/{path} b/{path}",
        f"--- a/{path}",
        f"+++ b/{path}",
        "@@ -10,3 +10,4 @@",
    ]
    for line in context or []:
        lines.append(" " + line)
    for line in removed or []:
        lines.append("-" + line)
    for line in added or []:
        lines.append("+" + line)
    return "\n".join(lines) + "\n"


class SpellingTests(unittest.TestCase):
    def setUp(self):
        self.checker = load_checker()

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.checker.main(argv)
        return code, out.getvalue() + err.getvalue()

    def write_temp(self, directory, name, text):
        path = Path(directory) / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_added_colour_fails_with_actionable_message(self):
        diff = make_diff(added=["Pick the colour you like."])
        violations = self.checker.diff_violations(diff)
        self.assertEqual(1, len(violations))
        message = self.checker.format_violation(violations[0])
        self.assertIn("docs/GUIDE.md:10", message)
        self.assertIn("'colour'", message)
        self.assertIn("'color'", message)
        with tempfile.TemporaryDirectory() as directory:
            code, output = self.run_main(
                ["--diff-file", self.write_temp(directory, "change.diff", diff)]
            )
        self.assertEqual(1, code)
        self.assertIn("docs/GUIDE.md:10", output)

    def test_added_color_passes(self):
        diff = make_diff(added=["Pick the color you like."])
        self.assertEqual([], self.checker.diff_violations(diff))
        with tempfile.TemporaryDirectory() as directory:
            code, _ = self.run_main(
                ["--diff-file", self.write_temp(directory, "change.diff", diff)]
            )
        self.assertEqual(0, code)

    def test_untouched_existing_british_text_passes(self):
        # Context lines are already in the file; the change did not add them.
        diff = make_diff(
            context=["The old behaviour is documented here."],
            added=["A brand new sentence."],
        )
        self.assertEqual([], self.checker.diff_violations(diff))

    def test_removed_british_line_passes(self):
        diff = make_diff(removed=["Delete this honour note."])
        self.assertEqual([], self.checker.diff_violations(diff))

    def test_identifier_containing_the_letters_passes(self):
        diff = make_diff(added=["The `colour_code` field stays as-is."])
        self.assertEqual([], self.checker.diff_violations(diff))

    def test_non_markdown_files_are_not_scanned(self):
        diff = make_diff(added=["colour = pick_colour()"], path="print_handy/core/x.py")
        self.assertEqual([], self.checker.diff_violations(diff))

    def test_case_insensitive_with_capitalized_replacement(self):
        violations = self.checker.diff_violations(make_diff(added=["Colour first."]))
        self.assertEqual(1, len(violations))
        self.assertEqual("Color", violations[0][3])

    def test_exception_marker_allows_the_named_word_only(self):
        allowed = make_diff(
            added=["Upstream calls it colour. <!-- us-spelling: allow colour -->"]
        )
        self.assertEqual([], self.checker.diff_violations(allowed))
        mixed = make_diff(
            added=["Upstream colour and behaviour. <!-- us-spelling: allow colour -->"]
        )
        violations = self.checker.diff_violations(mixed)
        self.assertEqual(1, len(violations))
        self.assertEqual("behaviour", violations[0][2])

    def test_exception_marker_blanket_allow(self):
        diff = make_diff(
            added=["Quoted: colour, honour, behaviour. <!-- us-spelling: allow -->"]
        )
        self.assertEqual([], self.checker.diff_violations(diff))

    def test_body_file_is_scanned(self):
        with tempfile.TemporaryDirectory() as directory:
            body = self.write_temp(directory, "body.txt", "This recognises the gap.\n")
            diff = self.write_temp(directory, "change.diff", make_diff(added=["Fine."]))
            code, output = self.run_main(["--diff-file", diff, "--body-file", body])
        self.assertEqual(1, code)
        self.assertIn("PR description:1", output)
        self.assertIn("'recognises'", output)
        self.assertIn("'recognizes'", output)

    def test_word_list_covers_the_named_family(self):
        for british in ("colour", "honour", "behaviour", "favour", "neighbour",
                        "recognise", "organise"):
            self.assertIn(british, self.checker.BRITISH_TO_US)


class WorkflowTests(unittest.TestCase):
    def test_ci_has_a_spelling_job_running_the_checker(self):
        workflow = yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)
        job = workflow["jobs"]["spelling"]
        runs = [step.get("run", "") for step in job["steps"]]
        self.assertTrue(any("check_us_spelling.py" in run for run in runs))

    def test_ci_does_not_trigger_on_description_edits(self):
        # A PR body edit must not start a CI run: its required checks would all
        # report skipped on the newer run and branch protection would block a
        # head that already passed. Contribution checks owns body edits.
        workflow = yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)
        types = workflow["on"]["pull_request"]["types"]
        self.assertNotIn("edited", types)
        for expected in ("opened", "synchronize", "reopened", "ready_for_review"):
            self.assertIn(expected, types)
        for name, job in workflow["jobs"].items():
            with self.subTest(job=name):
                self.assertNotIn("edited", job.get("if", ""))

    def test_contribution_checks_still_run_on_description_edits(self):
        path = WORKFLOW_PATH.parent / "contribution.yml"
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertIn("edited", workflow["on"]["pull_request"]["types"])


if __name__ == "__main__":
    unittest.main()
