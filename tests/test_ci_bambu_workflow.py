"""Contract tests for the Slicer test (bambu) CI job (.github/workflows/ci.yml).

Bambu Studio is the Bambu Lab vendor default slicer and the one-shot fallback
when Orca execution fails, but it is not in the published image and was, until
this job, exercised only with the slicer process mocked. These tests pin the
leg that closes that gap: it runs the A1 Bambu production tests against the
real Flathub Bambu Studio at a pinned ostree commit, with templates generated
from the same pinned OrcaSlicer bundle as the other real-slicer legs — and it
must never change what ships (no WITH_BAMBU in CI).
"""

import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
ORCA_BUNDLE_SHA256 = "2d2f6979a3379d5fe77c62bbdff0ea36ec415ef43c9557e118ed45b95d39de03"


def load_workflow():
    return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


def bambu_job():
    return load_workflow()["jobs"]["slicer-test-bambu"]


def job_runs(job):
    return "\n".join(step.get("run", "") for step in job["steps"])


class RealSlicerBambuWorkflowContract(unittest.TestCase):
    def test_job_exists_on_the_host_runner_with_both_real_flags(self):
        job = bambu_job()
        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        self.assertEqual(job["env"]["PRINT_HANDY_REAL_SLICER"], "1")
        self.assertEqual(job["env"]["PRINT_HANDY_REAL_BAMBU"], "1")
        self.assertEqual(job["env"]["BAMBU_FLATPAK_APP_ID"], "com.bambulab.BambuStudio")

    def test_bambu_commit_is_pinned_and_verified_after_install(self):
        job = bambu_job()
        commit = job["env"]["BAMBU_FLATPAK_COMMIT"]
        self.assertRegex(commit, r"^[0-9a-f]{64}$")
        runs = job_runs(job)
        self.assertIn('--commit="$BAMBU_FLATPAK_COMMIT"', runs)
        self.assertIn("flatpak info --user --show-commit", runs)
        self.assertIn('test "$deployed" = "$BAMBU_FLATPAK_COMMIT"', runs)

    def test_runs_only_the_bambu_subset_under_xvfb(self):
        runs = job_runs(bambu_job())
        self.assertIn("xvfb-run -a python -m pytest -v -s tests/slicer/test_bambu_real_a1.py", runs)
        # The Orca corpus belongs to the real-slicer legs; this leg must not
        # silently grow into a second full-corpus run.
        self.assertNotIn("tests/corpus", runs)

    def test_templates_come_from_the_pinned_orca_bundle(self):
        # The bundle and runtime pins themselves live once in ci.yml's env (tests/test_orca_pins.py).
        runs = job_runs(bambu_job())
        self.assertIn("sh scripts/install-orca-flatpak.sh", runs)
        self.assertEqual(load_workflow()["env"]["ORCA_BUNDLE_SHA256"], ORCA_BUNDLE_SHA256)

    def test_leg_does_not_change_what_ships(self):
        # CI never builds the Docker image, and the source-build stage switch
        # belongs to the image workflows, where it stays off. If either shows
        # up in this job, the test leg has started reaching into what ships.
        job = bambu_job()
        self.assertNotIn("WITH_BAMBU", yaml.dump(job))
        for step in job["steps"]:
            self.assertNotIn("docker", (step.get("run", "") + step.get("name", "")).lower())

    def test_existing_real_slicer_legs_are_untouched(self):
        jobs = load_workflow()["jobs"]
        for name in ("slicer-test-stable", "slicer-test-preview"):
            self.assertIn(name, jobs)
            self.assertEqual(jobs[name]["env"]["PRINT_HANDY_REAL_SLICER"], "1")
            self.assertNotIn("PRINT_HANDY_REAL_BAMBU", jobs[name]["env"])

    def test_slicer_job_names_are_stage_neutral(self):
        # #600: job display names describe the channel's role, never the
        # upstream stage; these names are the GitHub check contexts, and
        # branch protection requires "Slicer test (stable)".
        jobs = load_workflow()["jobs"]
        self.assertEqual(jobs["slicer-test-stable"]["name"], "Slicer test (stable)")
        self.assertEqual(jobs["slicer-test-preview"]["name"], "Slicer test (preview)")
        self.assertEqual(jobs["slicer-test-bambu"]["name"], "Slicer test (bambu)")
        for job_id in jobs:
            self.assertNotIn("alpha", job_id)
            self.assertNotIn("beta", job_id)


if __name__ == "__main__":
    unittest.main()
