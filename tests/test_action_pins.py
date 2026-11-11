"""Every third-party GitHub Action is pinned to a commit SHA (#1141).

``scripts/check_action_pins.py`` scans ``.github/workflows`` and
``.github/actions``. This test runs it on the repository, so a tag-pinned
``uses:`` line fails ``offline-tests`` (a required check), and pins the
checker's verdicts on the forms it must reject.
"""

import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_action_pins", ROOT / "scripts" / "check_action_pins.py")
pins = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pins)

SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"


class RepositoryIsPinned(unittest.TestCase):
    def test_every_workflow_uses_line_is_sha_pinned(self):
        self.assertEqual(pins.problems(ROOT), [])

    def test_the_scan_sees_the_workflows(self):
        files = pins.workflow_files(ROOT)
        self.assertIn(ROOT / ".github" / "workflows" / "ci.yml", files)
        self.assertIn(ROOT / ".github" / "workflows" / "docker-publish.yml", files)


class CheckerVerdicts(unittest.TestCase):
    def test_sha_with_version_comment_passes(self):
        self.assertIsNone(pins.line_problem(f"actions/checkout@{SHA}  # v7.0.1"))
        self.assertIsNone(pins.line_problem(f"github/codeql-action/init@{SHA} # v3"))
        self.assertIsNone(pins.line_problem(f"'actions/checkout@{SHA}'  # v7.0.1"))

    def test_local_action_is_exempt(self):
        self.assertIsNone(pins.line_problem("./.github/actions/setup"))

    def test_major_tag_is_rejected(self):
        self.assertIn("not a 40-character commit SHA",
                      pins.line_problem("actions/create-github-app-token@v2"))

    def test_branch_and_short_sha_are_rejected(self):
        self.assertIsNotNone(pins.line_problem("actions/checkout@main"))
        self.assertIsNotNone(pins.line_problem("actions/checkout@3d3c42e  # v7"))

    def test_sha_without_version_comment_is_rejected(self):
        self.assertIn("version comment", pins.line_problem(f"actions/checkout@{SHA}"))
        self.assertIn("version comment",
                      pins.line_problem(f"actions/checkout@{SHA}  # pinned"))

    def test_docker_reference_needs_a_digest(self):
        self.assertIsNotNone(pins.line_problem("docker://debian:13-slim"))
        self.assertIsNone(pins.line_problem("docker://debian:13-slim@sha256:" + "a" * 64))

    def test_unpinned_line_in_a_workflow_is_reported_with_its_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            wf = pathlib.Path(tmp) / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "x.yml").write_text(
                "jobs:\n  a:\n    steps:\n"
                f"      - uses: actions/checkout@{SHA}  # v7.0.1\n"
                "      - uses: actions/create-github-app-token@v2\n",
                encoding="utf-8")
            found = pins.problems(pathlib.Path(tmp))
            self.assertEqual(len(found), 1)
            self.assertTrue(found[0].startswith(".github/workflows/x.yml:5:"), found)
            self.assertEqual(pins.main(["check_action_pins.py", tmp]), 1)


if __name__ == "__main__":
    unittest.main()
