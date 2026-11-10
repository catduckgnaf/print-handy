"""Contract and behaviour tests for the release surfaces gate.

The gate (scripts/check_release_surfaces.py, run as the first job of
.github/workflows/docker-publish.yml) makes current release surfaces a
requirement for publishing a version release: candidates only warn, a
publish=true dispatch fails before any image is built when the README /
package version disagree with the release or the Pages site has drifted
from docs/. Nightly (.github/workflows/nightly.yml) is a separate
workflow and must never be gated.
"""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
PUBLISH_PATH = ROOT / ".github" / "workflows" / "docker-publish.yml"
NIGHTLY_PATH = ROOT / ".github" / "workflows" / "nightly.yml"
SCRIPT_PATH = ROOT / "scripts" / "check_release_surfaces.py"


def load_workflow(path):
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


class DockerPublishGateContract(unittest.TestCase):
    def test_gate_job_runs_first_and_build_needs_it(self):
        workflow = load_workflow(PUBLISH_PATH)
        self.assertIn("surfaces-gate", workflow["jobs"])
        self.assertEqual(workflow["jobs"]["build"]["needs"], "surfaces-gate")
        # The gate has no job-level condition: it runs on candidate AND
        # publication dispatches (its script decides warn vs fail).
        self.assertNotIn("if", workflow["jobs"]["surfaces-gate"])

    def test_gate_uses_the_docs_repos_own_drift_definition(self):
        gate = load_workflow(PUBLISH_PATH)["jobs"]["surfaces-gate"]
        runs = [step.get("run", "") for step in gate["steps"]]
        self.assertTrue(any("print-handy-docs" in run for run in runs))
        self.assertTrue(
            any("scripts/check_release_surfaces.py" in run for run in runs))
        script = SCRIPT_PATH.read_text()
        # Drift is delegated to the docs repo's Docs sync check, so the
        # gate can never disagree with it about what drift means.
        self.assertIn("check-sync.sh", script)
        self.assertIn("PRINT_HANDY_DIR", script)
        for prefix in ("CHANGED:", "ADDED:", "REMOVED:"):
            self.assertIn(prefix, script)

    def test_gate_failure_names_the_remedy(self):
        script = SCRIPT_PATH.read_text()
        self.assertIn("docs-auto-sync.yml", script)
        self.assertIn("print-handy-docs", script)

    def test_triggers_and_inputs_unchanged(self):
        workflow = load_workflow(PUBLISH_PATH)
        self.assertEqual(sorted(workflow["on"]), ["workflow_dispatch"])
        inputs = workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(sorted(inputs), ["publish", "version"])

    def test_nightly_is_never_gated(self):
        nightly_text = NIGHTLY_PATH.read_text()
        self.assertNotIn("surfaces-gate", nightly_text)
        self.assertNotIn("check_release_surfaces", nightly_text)
        self.assertNotIn("print-handy-docs", nightly_text)
        nightly = load_workflow(NIGHTLY_PATH)
        for job in nightly["jobs"].values():
            self.assertNotEqual(job.get("needs"), "surfaces-gate")


STUB_CHECK_SYNC = """\
#!/usr/bin/env bash
# Test stub for the docs repo's check-sync.sh: the drift verdict is
# driven by STUB_DRIFT (0 = none, 1 = drift, 2 = setup error).
case "${STUB_DRIFT:-0}" in
  0) echo "NO DRIFT"; exit 0 ;;
  1) echo "CHANGED: FOO.md (site: docs/foo.md)"; echo "REMOVED: BAR.md (site: docs/bar.md)"; exit 1 ;;
  *) echo "ERROR: stub setup failure" >&2; exit 2 ;;
esac
"""


class GateScriptBehaviour(unittest.TestCase):
    """Run the real gate script against fixture checkouts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.root = base / "print-handy"
        (self.root / "print_handy").mkdir(parents=True)
        self.docs = base / "print-handy-docs"
        (self.docs / "scripts").mkdir(parents=True)
        stub = self.docs / "scripts" / "check-sync.sh"
        stub.write_text(STUB_CHECK_SYNC)
        stub.chmod(0o755)
        self.write_root(version="1.2.3", readme="Ships with 1.2.3. "
                                               "[Guide](docs/README.md) "
                                               "[Site](https://catduckgnaf.github.io/print-handy-docs/)")

    def tearDown(self):
        self.tmp.cleanup()

    def write_root(self, version, readme):
        (self.root / "print_handy" / "__init__.py").write_text(
            f'__version__ = "{version}"\n')
        (self.root / "README.md").write_text(readme + "\n")
        docs = self.root / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "README.md").write_text("# docs\n")

    def run_gate(self, publish, drift="0", version="1.2.3"):
        env = dict(os.environ)
        # Never write into a real CI step's output/summary files.
        env.pop("GITHUB_OUTPUT", None)
        env.pop("GITHUB_STEP_SUMMARY", None)
        env.update({
            "PUBLISH_VERSION": version,
            "PUBLISH": "true" if publish else "false",
            "DOCS_DIR": str(self.docs),
            "STUB_DRIFT": drift,
            # The script resolves the repo root from its own path, which
            # in these tests is the real repo — point its checks at the
            # fixture by running a copy of the script inside the fixture.
        })
        # Mirror the production layout (scripts/<name>) so the script's
        # repo-root resolution (its parent's parent) lands on the fixture.
        scripts_dir = self.root / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        script_copy = scripts_dir / "check_release_surfaces.py"
        script_copy.write_text(SCRIPT_PATH.read_text())
        return subprocess.run(
            [sys.executable, str(script_copy)],
            env=env, capture_output=True, text=True)

    def test_current_surfaces_pass_for_candidate_and_publish(self):
        for publish in (False, True):
            result = self.run_gate(publish)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("All release surfaces are current", result.stdout)

    def test_drift_warns_a_candidate_but_blocks_a_publish(self):
        candidate = self.run_gate(False, drift="1")
        self.assertEqual(candidate.returncode, 0)
        self.assertIn("CHANGED: FOO.md", candidate.stdout)
        self.assertIn("REMOVED: BAR.md", candidate.stdout)
        # The blocked releaser is told the remedy.
        self.assertIn("docs-auto-sync.yml", candidate.stdout)

        publish = self.run_gate(True, drift="1")
        self.assertEqual(publish.returncode, 1)
        self.assertIn("CHANGED: FOO.md", publish.stdout)

    def test_drift_check_setup_error_blocks_a_publish(self):
        publish = self.run_gate(True, drift="2")
        self.assertEqual(publish.returncode, 1)
        self.assertIn("Docs sync check itself failed", publish.stdout)

    def test_version_mismatch_blocks_a_publish(self):
        self.write_root(version="1.2.2",
                        readme="Ships with 1.2.3. [Guide](docs/README.md)")
        result = self.run_gate(True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("__version__ is 1.2.2", result.stdout)

    def test_readme_must_mention_the_release_and_link_inside_the_repo(self):
        self.write_root(version="1.2.3",
                        readme="No version here. [Gone](docs/NOPE.md)")
        result = self.run_gate(True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not mention the version being published",
                      result.stdout)
        self.assertIn("link target does not exist: docs/NOPE.md",
                      result.stdout)

    def test_readme_links_must_point_at_real_headings(self):
        (self.root / "docs" / "GUIDE.md").write_text(
            "# Guide\n\n## Real heading\n\n## Install [OrcaSlicer](https://example.com)\n"
        )
        self.write_root(version="1.2.3",
                        readme="Ships with 1.2.3. [Ok](docs/GUIDE.md#real-heading) "
                               "[Install](docs/GUIDE.md#install-orcaslicer) "
                               "[Stale](docs/GUIDE.md#real-heading-264)")
        result = self.run_gate(True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("link anchor does not exist: docs/GUIDE.md#real-heading-264",
                      result.stdout)
        self.assertNotIn("anchor does not exist: docs/GUIDE.md#real-heading)", result.stdout)


if __name__ == "__main__":
    unittest.main()
