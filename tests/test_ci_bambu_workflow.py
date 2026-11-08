"""Contract tests for the Slicer test (bambu) CI job (.github/workflows/ci.yml).

Bambu Studio is the Bambu Lab vendor default slicer and the one-shot fallback
when Orca execution fails, but it is not in the published image and was, until
this job, exercised only with the slicer process mocked. These tests pin the
leg that closes that gap: it runs the A1 Bambu production tests against the
real Flathub Bambu Studio at a pinned ostree commit, with templates generated
from the same pinned OrcaSlicer bundle as the other real-slicer legs — and it
must never change what ships (no WITH_BAMBU in CI).
"""

import importlib.util
import pathlib
import sys
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
ORCA_BUNDLE_SHA256 = "58c10feaa2df81f6385db628185ee6c5c656391de57974646a5f4dd40e7947b3"

spec = importlib.util.spec_from_file_location(
    "bump_upstream", ROOT / "scripts" / "bump_upstream.py")
bump = importlib.util.module_from_spec(spec)
sys.modules["bump_upstream"] = bump  # dataclasses resolve the module by name
spec.loader.exec_module(bump)


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
        # One bundled Orca only: the stable leg remains, the preview leg is gone.
        self.assertNotIn("slicer-test-preview", jobs)
        self.assertIn("slicer-test-stable", jobs)
        self.assertEqual(jobs["slicer-test-stable"]["env"]["PRINT_HANDY_REAL_SLICER"], "1")
        self.assertNotIn("PRINT_HANDY_REAL_BAMBU", jobs["slicer-test-stable"]["env"])

    def test_slicer_job_names_are_stage_neutral(self):
        # #600: job display names describe the channel's role, never the
        # upstream stage; these names are the GitHub check contexts, and
        # branch protection requires "Slicer test (stable)".
        jobs = load_workflow()["jobs"]
        # The stable leg is sharded; its result job carries the required check name.
        self.assertEqual(jobs["slicer-test-stable-result"]["name"], "Slicer test (stable)")
        self.assertEqual(jobs["slicer-test-stable-result"]["needs"], "slicer-test-stable")
        self.assertEqual(jobs["slicer-test-bambu"]["name"], "Slicer test (bambu)")
        for job_id in jobs:
            self.assertNotIn("alpha", job_id)
            self.assertNotIn("beta", job_id)


class BambuFallbackGenerationVerdict(unittest.TestCase):
    """The fallback leg's Bambu generation vs the shipped pin's generation.

    This leg runs the Flathub Bambu Studio build recorded below; the
    shipped BAMBU_REF pin (and the HMS table generated from it) can be
    a different Bambu generation - PR #990 moves the pin to
    02.09.01.70 while this leg stays on the 2.8.2.61 Flathub build.
    That divergence is deliberate (the leg proves the fallback slicer
    path, not the HMS data), but it must never be silent: when the
    generations differ, an exception naming the exact generation pair
    must be recorded here, dated, with the reason. A future step that
    diverges without a recorded verdict fails this test.
    """

    # The Flathub build this leg deploys, recorded against the pinned
    # ostree commit in ci.yml (whose comment names the same version).
    FLATPAK_BAMBU_VERSION = "2.8.2.61"
    FLATPAK_BAMBU_COMMIT = "68a634d4d733ce7acf79009095db6f7ca76625f0eb810eef1e92dc7adeba7852"

    # Recorded divergence verdicts: (flatpak generation, pin
    # generation) -> (date recorded, reason). Generations are the
    # first two numeric components under the bump automation's
    # normalized-tuple comparison (the step-size policy).
    RECORDED_DIVERGENCES = {
        ((2, 8), (2, 9)): (
            "2026-11-07",
            "PR #990 moves BAMBU_REF to Bambu Studio 02.09.01.70, the "
            "source of the shipped HMS table; the fallback leg stays on "
            "the Flathub 2.8.2.61 build because no Flathub build of the "
            "2.9 line was pinned and verified when the verdict was "
            "recorded. The leg proves the Bambu fallback slicing path "
            "against a real Studio; HMS content integrity for the 2.9 "
            "table is pinned by tests/printers/test_bambu_hms.py, not "
            "by this leg. Revisit when a 2.9 Flathub build is pinned.",
        ),
    }

    def _pin_version(self):
        settings = (ROOT / "docs" / "SETTINGS.md").read_text()
        match = bump._BAMBU_SETTINGS_RE.search(settings)
        self.assertIsNotNone(match, "docs/SETTINGS.md lost its BAMBU_REF row")
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertEqual(match.group(1), bump.parse_bambu(dockerfile))
        return match.group(2)

    def test_fallback_leg_generation_verdict_is_recorded(self):
        job = bambu_job()
        self.assertEqual(job["env"]["BAMBU_FLATPAK_COMMIT"], self.FLATPAK_BAMBU_COMMIT)
        ci_text = WORKFLOW_PATH.read_text()
        self.assertIn(f"com.bambulab.BambuStudio v{self.FLATPAK_BAMBU_VERSION}", ci_text)
        flatpak_gen = bump.numeric_tuple(self.FLATPAK_BAMBU_VERSION)[:2]
        pin_version = self._pin_version()
        pin_gen = bump.numeric_tuple(pin_version)[:2]
        if flatpak_gen == pin_gen:
            return  # Same generation: the leg tracks the pin line; no verdict needed.
        verdict = self.RECORDED_DIVERGENCES.get((flatpak_gen, pin_gen))
        self.assertIsNotNone(
            verdict,
            f"the Bambu fallback leg runs generation {flatpak_gen} "
            f"(Flathub v{self.FLATPAK_BAMBU_VERSION}) while BAMBU_REF ships "
            f"generation {pin_gen} ({pin_version}), and no divergence verdict "
            "is recorded in BambuFallbackGenerationVerdict.RECORDED_DIVERGENCES. "
            "Either move the leg, or record a dated verdict naming this pair "
            "and what still proves the shipped generation's data.",
        )
        recorded, reason = verdict
        self.assertRegex(recorded, r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(reason.strip())


if __name__ == "__main__":
    unittest.main()
