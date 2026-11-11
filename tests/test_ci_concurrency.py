"""Pull request runs cancel their superseded run; main and dispatched runs always finish.

Every workflow a pull request push starts shares one concurrency group per PR, so a new push
cancels the run still going for the old head instead of queuing behind it. Runs on `main`
(and dispatches) must never be cancelled or replaced while queued: the docker-publish guard
(scripts/check_publish_ref.py) refuses to publish unless CI concluded successfully on the exact
tagged commit. GitHub keeps only one pending run per group and cancels the older pending one even
with cancel-in-progress off, so those events get a group of their own (the run id).
"""

import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
PR_ONLY_CANCEL = "${{ github.event_name == 'pull_request' }}"


def load(path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def triggers(workflow):
    on = workflow["on"]
    return [on] if isinstance(on, str) else list(on)


def pr_workflows():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = load(path)
        if "pull_request" in triggers(workflow):
            yield path, workflow


def expected_group(stem):
    return (
        f"{stem}-${{{{ github.event_name == 'pull_request' && "
        "format('pr-{0}', github.event.pull_request.number) || "
        "format('run-{0}', github.run_id) }}"
    )


class PullRequestConcurrency(unittest.TestCase):
    def test_the_per_push_workflows_are_all_covered(self):
        names = {path.name for path, _ in pr_workflows()}
        for name in ("ci.yml", "contribution.yml", "docker-static.yml",
                     "compose-clean-start.yml", "gpu-flavor-build.yml", "release-check.yml"):
            self.assertIn(name, names)

    def test_every_pull_request_workflow_has_one_group_per_pr(self):
        for path, workflow in pr_workflows():
            with self.subTest(workflow=path.name):
                concurrency = workflow.get("concurrency")
                self.assertIsInstance(concurrency, dict, "no top-level concurrency group")
                self.assertIn("github.event.pull_request.number", concurrency["group"])
                self.assertTrue(concurrency["group"].startswith(path.stem + "-"),
                                "group must be prefixed with the workflow file name")
                self.assertNotIn("github.ref", concurrency["group"])

    def test_non_pr_events_get_their_own_group_and_are_never_cancelled(self):
        for path, workflow in pr_workflows():
            others = [t for t in triggers(workflow) if t != "pull_request"]
            with self.subTest(workflow=path.name):
                concurrency = workflow["concurrency"]
                if not others:
                    self.assertEqual(concurrency["group"],
                                     f"{path.stem}-pr-${{{{ github.event.pull_request.number }}}}")
                    self.assertEqual(concurrency["cancel-in-progress"], "true")
                    continue
                self.assertEqual(concurrency["group"], expected_group(path.stem))
                self.assertEqual(concurrency["cancel-in-progress"], PR_ONLY_CANCEL)

    def test_ci_runs_on_main_pushes_that_the_publish_guard_reads(self):
        workflow = load(WORKFLOWS / "ci.yml")
        self.assertEqual(workflow["on"]["push"]["branches"], ["main"])
        self.assertIn('CI_WORKFLOW = "ci.yml"',
                      (ROOT / "scripts" / "check_publish_ref.py").read_text(encoding="utf-8"))
        self.assertEqual(workflow["concurrency"]["cancel-in-progress"], PR_ONLY_CANCEL)
        self.assertIn("github.run_id", workflow["concurrency"]["group"])

    def test_no_job_level_group_overrides_the_workflow_rule(self):
        for path, workflow in pr_workflows():
            for name, job in workflow["jobs"].items():
                with self.subTest(workflow=path.name, job=name):
                    self.assertNotIn("concurrency", job)


if __name__ == "__main__":
    unittest.main()
