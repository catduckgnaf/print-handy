"""Contract tests for the nightly GHCR channel (.github/workflows/nightly.yml).

Nightly is a floating channel: the `nightly` branch is a pointer that only
ever names a commit on `main` whose CI run completed successfully, and the
workflow publishes that commit as the `nightly` GHCR tag.
These tests pin that contract so the channel cannot quietly start publishing
unverified code or overwriting release tags.
"""

import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "nightly.yml"


def load_workflow():
    return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


class NightlyWorkflowContract(unittest.TestCase):
    def test_triggers_are_schedule_and_dispatch_only(self):
        workflow = load_workflow()
        self.assertEqual(sorted(workflow["on"]), ["schedule", "workflow_dispatch"])
        self.assertEqual(workflow["on"]["schedule"], [{"cron": "17 6 * * *"}])
        inputs = workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(sorted(inputs), ["promote", "publish"])
        for name in ("promote", "publish"):
            self.assertEqual(inputs[name]["type"], "boolean")
            self.assertEqual(inputs[name]["default"], "true")

    def test_promotion_only_accepts_ci_green_main_commits(self):
        text = WORKFLOW_PATH.read_text()
        promote = load_workflow()["jobs"]["promote"]
        self.assertEqual(promote["permissions"], {"actions": "read", "contents": "write"})
        run = promote["steps"][0]["run"]
        # The promotion source is a successful CI run on main, nothing else.
        # event=push is load-bearing (P1-67): ci.yml also runs on
        # pull_request, and the branch filter alone would accept a green PR
        # run from a fork branch that happens to be named main — unmerged
        # code must never become the nightly pointer.
        self.assertIn("actions/workflows/ci.yml/runs?branch=main&event=push&status=success", run)
        # The branch is only ever fast-forwarded: ancestry is proven first and
        # the ref update is never forced.
        self.assertIn("compare/$nightly_sha...$green", run)
        self.assertIn('[[ "$status" == "ahead" ]]', run)
        self.assertIn("git/refs/heads/nightly", run)
        self.assertIn("-F force=false", run)
        self.assertNotIn("force=true", text)

    def test_build_checks_out_the_promoted_commit(self):
        workflow = load_workflow()
        build = workflow["jobs"]["build"]
        self.assertEqual(build["needs"], "promote")
        self.assertIn("needs.promote.outputs.changed == 'true'", build["if"])
        checkout = next(s for s in build["steps"] if "uses" in s
                        and s["uses"].startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["ref"], "${{ needs.promote.outputs.sha }}")
        self.assertEqual(build["env"]["NIGHTLY_SHA"], "${{ needs.promote.outputs.sha }}")

    def test_build_reuses_the_release_matrix_and_acceptance(self):
        publish = yaml.load(
            (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(),
            Loader=yaml.BaseLoader)
        nightly = load_workflow()
        self.assertEqual(nightly["jobs"]["build"]["strategy"],
                         publish["jobs"]["build"]["strategy"])
        steps = nightly["jobs"]["build"]["steps"]
        acceptance = next(i for i, s in enumerate(steps) if s.get("id") == "acceptance")
        push = next(i for i, s in enumerate(steps) if s.get("id") == "publish")
        self.assertLess(acceptance, push)
        gate = "needs.promote.outputs.publish == 'true' && steps.acceptance.outcome == 'success'"
        self.assertEqual(steps[push]["if"], gate)
        self.assertIn("push-by-digest=true", steps[push]["run"])
        self.assertNotIn("docker push", steps[push]["run"])
        self.assertNotIn("docker tag", steps[push]["run"])

    def test_manifest_publishes_only_the_nightly_tags(self):
        workflow = load_workflow()
        manifest = workflow["jobs"]["manifest"]
        self.assertEqual(manifest["needs"], ["promote", "build"])
        self.assertEqual(manifest["if"], "needs.promote.outputs.publish == 'true'")
        run = next(s["run"] for s in manifest["steps"] if "run" in s
                   and "imagetools create" in s["run"])
        self.assertIn('-t "$IMAGE:nightly"', run)
        self.assertNotIn("nightly-light", run)
        self.assertNotIn("-light", run)
        self.assertNotIn(":latest", run)
        self.assertNotIn("$PUBLISH_VERSION", run)

    def test_publishers_use_a_driver_that_can_push_by_digest(self):
        # Regression pin for nightly run 40330775729: every leg passed
        # acceptance and then failed at the push step with "push-by-digest is
        # currently not implemented for docker driver". Both workflows that
        # push by digest must configure a docker-container builder.
        for name in ("nightly.yml", "docker-publish.yml"):
            workflow = yaml.load(
                (ROOT / ".github" / "workflows" / name).read_text(),
                Loader=yaml.BaseLoader)
            setup = next(s for s in workflow["jobs"]["build"]["steps"]
                         if s.get("uses", "").startswith("docker/setup-buildx-action@"))
            self.assertEqual(setup["with"]["driver"], "docker-container", name)

    def test_default_permissions_are_read_only_and_actions_pinned(self):
        workflow = load_workflow()
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    self.assertRegex(step["uses"], r"^[^@]+@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
