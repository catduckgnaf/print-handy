"""Contract tests for the develop GHCR channel (.github/workflows/develop.yml).

Develop is a floating channel built from the `develop` branch, a standing
development branch: a maintainer dispatch builds its tip exactly as pushed
and publishes it as the `develop` GHCR tag. There is no promotion and no
publication pointer. These tests pin that contract so the channel cannot
quietly start publishing on a schedule, moving branches, or overwriting
release tags.
"""

import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "develop.yml"


def load_workflow():
    return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


class DevelopWorkflowContract(unittest.TestCase):
    def test_trigger_is_dispatch_only_with_a_publish_input(self):
        workflow = load_workflow()
        self.assertEqual(sorted(workflow["on"]), ["workflow_dispatch"])
        inputs = workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(sorted(inputs), ["publish"])
        self.assertEqual(inputs["publish"]["type"], "boolean")
        self.assertEqual(inputs["publish"]["default"], "true")
        self.assertEqual(workflow["concurrency"]["group"], "docker-develop")

    def test_resolve_reads_the_develop_tip_and_never_moves_a_ref(self):
        text = WORKFLOW_PATH.read_text()
        resolve = load_workflow()["jobs"]["resolve"]
        run = resolve["steps"][0]["run"]
        # The only source is the tip of the standing develop branch.
        self.assertIn("git/ref/heads/develop", run)
        self.assertIn('[[ "$develop_sha" =~ ^[0-9a-f]{40}$ ]]', run)
        # No job in this workflow mutates any git ref: there is no
        # promotion and no publication pointer to advance.
        self.assertNotIn("-X PATCH", text)
        self.assertNotIn("-X POST", text)
        self.assertNotIn("force=true", text)
        # No step reads or writes a publication pointer (the header comment
        # names nightly's only to explain why develop has none).
        for job in load_workflow()["jobs"].values():
            for step in job["steps"]:
                self.assertNotIn("nightly-published", step.get("run", ""))
        # The resolve job needs no write permission of any kind.
        self.assertNotIn("permissions", resolve)

    def test_build_checks_out_the_resolved_commit(self):
        workflow = load_workflow()
        build = workflow["jobs"]["build"]
        self.assertEqual(build["needs"], "resolve")
        checkout = next(s for s in build["steps"] if "uses" in s
                        and s["uses"].startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["ref"], "${{ needs.resolve.outputs.sha }}")
        self.assertEqual(build["env"]["DEVELOP_SHA"], "${{ needs.resolve.outputs.sha }}")

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
        gate = "needs.resolve.outputs.publish == 'true' && steps.acceptance.outcome == 'success'"
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
        self.assertEqual(manifest["needs"], ["resolve", "build"])
        self.assertEqual(manifest["if"], "needs.resolve.outputs.publish == 'true'")
        # No publication pointer is recorded, so the manifest job needs no
        # contents permission at all — packages: write is its whole grant.
        self.assertEqual(manifest["permissions"], {"packages": "write"})
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
