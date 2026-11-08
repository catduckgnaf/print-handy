"""Contract tests for the real-slicer timing leg (.github/workflows/ci.yml).

The slicer-timing-real job (#734) is the slicing-speed gate's real-Orca
leg. It is deliberately reporting-only today: tests/README.md
(Slicing-speed gate) records that its measured runner noise is too large
for any blocking limit in the tested range, and the job header in ci.yml
says to promote it only after a few releases of recorded data. These
tests pin the leg's current shape so neither half of that bargain can
drift silently:

- the hermes-transport cases are timed in 3 repeats after an untimed
  warm-up slice and compared against
  tests/corpus/timing_baseline_real_slicer.json with --warn-only, and
  the job stays continue-on-error until the promotion criteria are met
  (a change that drops --warn-only or continue-on-error must also
  update these tests and the README's noise record);
- the standalone Discord transport — the other Discord surface a user
  waits on — times single-part in 3 repeats too (deep-dive expansion),
  recorded as a proposed baseline for review, but stays OUT of the
  baseline comparison: it has no recorded baseline yet and the gate
  treats a baselineless case as a problem.
"""

import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"


def load_workflow():
    return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


def timing_job():
    return load_workflow()["jobs"]["slicer-timing-real"]


def steps_by_name(job):
    return {step.get("name"): step for step in job["steps"]}


class SlicerTimingWorkflowContract(unittest.TestCase):
    def test_job_is_reporting_only_until_promotion_criteria_are_met(self):
        job = timing_job()
        self.assertEqual(job.get("continue-on-error"), "true")
        compare = steps_by_name(job)["Compare with the real-slicer baseline (warnings only)"]
        self.assertIn("--warn-only", compare["run"])
        self.assertIn("timing_baseline_real_slicer.json", compare["run"])

    def test_hermes_cases_are_warmed_up_and_timed_in_three_repeats(self):
        steps = steps_by_name(timing_job())
        warm_up = steps["Warm-up slice (not timed; first flatpak start pays one-off costs)"]
        self.assertIn("--transport hermes", warm_up["run"])
        timed = steps["Time the real-slicer cases (3 repeats, hermes transport)"]
        self.assertIn("for i in 1 2 3", timed["run"])
        self.assertIn("--transport hermes", timed["run"])
        self.assertEqual(timing_job()["env"].get("PRINT_HANDY_REAL_SLICER"), "1")

    def test_standalone_discord_case_is_timed_but_not_compared(self):
        steps = steps_by_name(timing_job())
        step = steps["Time one Discord standalone-transport case (3 repeats, reporting only)"]
        self.assertIn("--transport standalone", step["run"])
        self.assertIn("--case single-part", step["run"])
        self.assertIn("for i in 1 2 3", step["run"])
        compare = steps["Compare with the real-slicer baseline (warnings only)"]
        self.assertNotIn("standalone-run", compare["run"])

    def test_standalone_proposed_baseline_is_recorded_for_review(self):
        steps = steps_by_name(timing_job())
        step = steps[
            "Proposed standalone baseline from this runner (for review, never applied automatically)"]
        self.assertIn("standalone-run-", step["run"])
        self.assertIn("--record", step["run"])
        self.assertIn("proposed-baseline-standalone.json", step["run"])

    def test_standalone_proposed_baseline_is_guarded_on_timing_data(self):
        # #1060: with no standalone timing files the glob stays literal,
        # timing_gate fails reading it (hidden by || true) and the output
        # redirection still creates a 0-byte proposed baseline that the
        # artifact step uploads. The step must check at least one input
        # file exists before invoking the gate, expand the matched files
        # (not the literal glob) when it does, and say plainly that no
        # proposal was written when there is no data.
        steps = steps_by_name(timing_job())
        run = steps[
            "Proposed standalone baseline from this runner (for review, never applied automatically)"]["run"]
        self.assertIn("files=(slicer-timing/standalone-run-*.json)", run)
        self.assertIn('[ -f "${files[0]}" ]', run)
        self.assertLess(run.index('[ -f "${files[0]}" ]'), run.index("timing_gate"))
        self.assertIn('timing_gate "${files[@]}"', run)
        self.assertIn("No standalone timing data from this runner", run)
        self.assertIn("no proposed standalone baseline was written", run)


if __name__ == "__main__":
    unittest.main()
