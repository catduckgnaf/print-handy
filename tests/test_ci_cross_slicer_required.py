"""CI-shape contract for the cross-slicer smoke (#1525 completion).

The cross-slicer tests used to run only in the slicer tier's sharded
collection and in a scheduled workflow, so no required branch check
provably executed them. The fix is in the workflow, not in branch
protection: the required Slicer test (stable) job gains a named,
unsharded step that runs tests/slicer/test_cross_slicer_real.py in
full, and the pure contract tests live under tests/core, which the
required offline-tests job collects. These tests pin that shape so
the wiring cannot silently drift back out of the required checks.
"""
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
STEP_NAME = "Cross-slicer contract smoke with real Orca (#1525)"


def load_workflow():
    return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


def steps_by_name(job):
    return {step.get("name"): step for step in job["steps"]}


def test_stable_job_runs_the_cross_slicer_module_as_a_named_step():
    job = load_workflow()["jobs"]["slicer-test-stable"]
    # The module's own gate: inside PRINT_HANDY_REAL_SLICER=1 there are
    # no further skips, and this job is where that env is set.
    assert job["env"]["PRINT_HANDY_REAL_SLICER"] == "1"
    step = steps_by_name(job)[STEP_NAME]
    assert "tests/slicer/test_cross_slicer_real.py" in step["run"]
    # Unsharded on purpose: shard assignment alone never proved a given
    # test executed, so the step must not take the shard env.
    assert "PRINT_HANDY_TEST_SHARD" not in (step.get("env") or {})


def test_the_required_aggregator_still_reports_the_stable_job():
    jobs = load_workflow()["jobs"]
    aggregator = jobs["slicer-test-stable-result"]
    assert aggregator["name"] == "Slicer test (stable)"
    needs = aggregator["needs"]
    assert "slicer-test-stable" in (needs if isinstance(needs, list) else [needs])


def test_offline_job_collects_the_pure_contract_tests():
    job = load_workflow()["jobs"]["offline-tests"]
    runs = "\n".join(step.get("run") or "" for step in job["steps"])
    assert "pytest" in runs and "tests" in runs
    # The contract module lives where that collection reaches it.
    assert (ROOT / "tests" / "core" / "test_cross_slicer_contract_1525.py").is_file()
