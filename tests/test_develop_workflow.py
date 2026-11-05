"""Contract tests for the develop GHCR channel (.github/workflows/develop.yml).

Develop is a floating channel built from the `develop` branch, and it
follows `main` the way nightly does: a promote job fast-forwards the
branch to the newest CI-green main commit on a daily schedule (or on
dispatch), and a `develop-published` pointer records what has actually
been published. The channels differ in tag, version scheme and app
channel only. These tests pin that contract so the channel cannot
quietly start publishing unverified code, moving refs outside the two
jobs scoped for it, or overwriting release tags.
"""

import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "develop.yml"
NIGHTLY_PATH = ROOT / ".github" / "workflows" / "nightly.yml"


def load_workflow():
    return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


class DevelopWorkflowContract(unittest.TestCase):
    def test_triggers_are_schedule_and_dispatch_with_both_inputs(self):
        workflow = load_workflow()
        self.assertEqual(sorted(workflow["on"]), ["schedule", "workflow_dispatch"])
        self.assertEqual(workflow["on"]["schedule"], [{"cron": "47 6 * * *"}])
        inputs = workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(sorted(inputs), ["promote", "publish"])
        for name in ("promote", "publish"):
            self.assertEqual(inputs[name]["type"], "boolean")
            self.assertEqual(inputs[name]["default"], "true")
        self.assertEqual(workflow["concurrency"]["group"], "docker-develop")
        # The daily run is offset from nightly's 06:17 so the two
        # channels do not contend for runners.
        nightly = yaml.load(NIGHTLY_PATH.read_text(), Loader=yaml.BaseLoader)
        self.assertNotEqual(workflow["on"]["schedule"],
                            nightly["on"]["schedule"])

    def test_promotion_only_accepts_ci_green_main_commits(self):
        text = WORKFLOW_PATH.read_text()
        promote = load_workflow()["jobs"]["promote"]
        self.assertEqual(promote["permissions"], {"actions": "read", "contents": "write"})
        run = promote["steps"][0]["run"]
        # The promotion source is a successful CI run on main, nothing else.
        # event=push is load-bearing (P1-67): ci.yml also runs on
        # pull_request, and the branch filter alone would accept a green PR
        # run from a fork branch that happens to be named main — unmerged
        # code must never become the develop pointer.
        self.assertIn("actions/workflows/ci.yml/runs?branch=main&event=push&status=success", run)
        # The branch is only ever fast-forwarded: ancestry is proven first and
        # the ref update is never forced.
        self.assertIn("compare/$develop_sha...$green", run)
        self.assertIn('[[ "$status" == "ahead" ]]', run)
        self.assertIn("git/refs/heads/develop", run)
        self.assertIn("-F force=false", run)
        self.assertNotIn("force=true", text)

    def test_unpublished_commits_stay_eligible_for_publication(self):
        # P2-97: the gate is successful publication of the target commit,
        # not movement of the develop branch. A failed build/publication —
        # or a promote-only (publish=false) run — must not strand the
        # commit: develop already points at it, so without the published
        # pointer the next scheduled run would report changed=false and
        # skip the build forever.
        workflow = load_workflow()
        run = workflow["jobs"]["promote"]["steps"][0]["run"]
        self.assertIn("git/ref/heads/develop-published", run)
        self.assertIn('[[ "$target" != "$published_sha" ]]', run)
        # The manifest job records the commit only after its manifest push
        # succeeds, and needs contents: write to move that ref.
        manifest = workflow["jobs"]["manifest"]
        self.assertEqual(manifest["permissions"].get("contents"), "write")
        steps = manifest["steps"]
        create = next(i for i, s in enumerate(steps)
                      if "imagetools create" in s.get("run", ""))
        record = next(i for i, s in enumerate(steps)
                      if "git/refs/heads/develop-published" in s.get("run", ""))
        self.assertGreater(record, create)
        self.assertIn("$DEVELOP_SHA", steps[record]["run"])

    def test_publication_pointer_is_written_only_by_the_manifest_job(self):
        # Ref writes are scoped to exactly two jobs: promote fast-forwards
        # the develop branch itself, and the manifest job records the
        # develop-published pointer after a successful publication. The
        # build job never writes a ref, and no other job writes the
        # pointer.
        workflow = load_workflow()
        for name, job in workflow["jobs"].items():
            for step in job["steps"]:
                run = step.get("run", "")
                if name != "manifest":
                    self.assertNotIn("git/refs/heads/develop-published", run,
                                     f"{name} must not write the pointer")
                if name != "promote":
                    self.assertNotIn("git/refs/heads/develop\"", run,
                                     f"{name} must not move the develop branch")
                if name == "build":
                    self.assertNotIn("-X PATCH", run)
                    self.assertNotIn("-X POST", run)

    def test_build_checks_out_the_promoted_commit(self):
        workflow = load_workflow()
        build = workflow["jobs"]["build"]
        self.assertEqual(build["needs"], "promote")
        self.assertIn("needs.promote.outputs.changed == 'true'", build["if"])
        checkout = next(s for s in build["steps"] if "uses" in s
                        and s["uses"].startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["ref"], "${{ needs.promote.outputs.sha }}")
        self.assertEqual(build["env"]["DEVELOP_SHA"], "${{ needs.promote.outputs.sha }}")

    def test_build_labels_the_image_as_a_development_build(self):
        # The Dockerfile validates APP_CHANNEL as stable|development and
        # stamps it as the org.print-handy.app-channel identity label; a
        # build of the development branch carries the development value,
        # where nightly carries stable.
        build = load_workflow()["jobs"]["build"]
        self.assertEqual(build["env"]["APP_CHANNEL"], "development")
        prepare = next(s["run"] for s in build["steps"]
                       if s.get("name") == "Prepare develop evidence")
        self.assertIn('develop-$(git log -1 --format=%cs HEAD)-${DEVELOP_SHA:0:7}', prepare)
        build_step = next(s["run"] for s in build["steps"]
                          if s.get("name") == "Build image once (no push)")
        self.assertIn("--label \"org.opencontainers.image.revision=$DEVELOP_SHA\"", build_step)

    def test_build_reuses_the_release_matrix_and_acceptance(self):
        publish = yaml.load(
            (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(),
            Loader=yaml.BaseLoader)
        develop = load_workflow()
        self.assertEqual(develop["jobs"]["build"]["strategy"],
                         publish["jobs"]["build"]["strategy"])
        steps = develop["jobs"]["build"]["steps"]
        acceptance = next(i for i, s in enumerate(steps) if s.get("id") == "acceptance")
        push = next(i for i, s in enumerate(steps) if s.get("id") == "publish")
        self.assertLess(acceptance, push)
        gate = "needs.promote.outputs.publish == 'true' && steps.acceptance.outcome == 'success'"
        self.assertEqual(steps[push]["if"], gate)
        self.assertIn("push-by-digest=true", steps[push]["run"])
        self.assertNotIn("docker push", steps[push]["run"])
        self.assertNotIn("docker tag", steps[push]["run"])

    def test_publish_step_verifies_the_pushed_image_is_the_accepted_one(self):
        # Same accepted-image identity proof as nightly's push step: the
        # push re-runs the build after evidence/ was written, so the digest
        # handed to the manifest job must be proven to be the accepted image.
        steps = load_workflow()["jobs"]["build"]["steps"]
        push = next(s for s in steps if s.get("id") == "publish")
        run = push["run"]
        self.assertIn("docker image inspect", run)
        self.assertIn("pushed_image_ids.py", run)
        self.assertIn("evidence/identity-check.txt", run)
        self.assertLess(run.index("pushed_image_ids.py"), run.index("digest-out"))
        ignored = (ROOT / ".dockerignore").read_text().splitlines()
        self.assertIn("evidence", ignored)

    def test_manifest_publishes_only_the_develop_tag(self):
        workflow = load_workflow()
        manifest = workflow["jobs"]["manifest"]
        self.assertEqual(manifest["needs"], ["promote", "build"])
        self.assertEqual(manifest["if"], "needs.promote.outputs.publish == 'true'")
        # contents: write is granted on this job alone, and only so the
        # record step can advance the develop-published pointer after a
        # successful publication; packages: write publishes the manifests.
        self.assertEqual(manifest["permissions"],
                         {"contents": "write", "packages": "write"})
        run = next(s["run"] for s in manifest["steps"] if "run" in s
                   and "imagetools create" in s["run"])
        self.assertIn('-t "$IMAGE:develop"', run)
        self.assertNotIn(":nightly", run)
        self.assertNotIn("-light", run)
        self.assertNotIn(":latest", run)
        self.assertNotIn("$PUBLISH_VERSION", run)

    def test_publisher_uses_a_driver_that_can_push_by_digest(self):
        # Regression pin for nightly run 40330775729: legs that push by
        # digest need a docker-container builder, not the docker driver.
        workflow = load_workflow()
        setup = next(s for s in workflow["jobs"]["build"]["steps"]
                     if s.get("uses", "").startswith("docker/setup-buildx-action@"))
        self.assertEqual(setup["with"]["driver"], "docker-container")

    def test_default_permissions_are_read_only_and_actions_pinned(self):
        workflow = load_workflow()
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    self.assertRegex(step["uses"], r"^[^@]+@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
