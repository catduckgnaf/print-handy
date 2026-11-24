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

import os
import pathlib
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "develop.yml"
NIGHTLY_PATH = ROOT / ".github" / "workflows" / "nightly.yml"


def load_workflow():
    return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


class DevelopWorkflowContract(unittest.TestCase):
    def test_triggers_are_schedule_and_dispatch_with_optional_sha(self):
        workflow = load_workflow()
        self.assertEqual(sorted(workflow["on"]), ["schedule", "workflow_dispatch"])
        self.assertEqual(workflow["on"]["schedule"], [{"cron": "47 6 * * *"}])
        inputs = workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(sorted(inputs), ["promote", "publish", "sha"])
        self.assertEqual(inputs["sha"]["type"], "string")
        self.assertEqual(inputs["sha"]["required"], "false")
        self.assertEqual(inputs["sha"]["default"], "")
        self.assertEqual(workflow["jobs"]["promote"]["env"]["REQUESTED_SHA"],
                         "${{ inputs.sha }}")
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


    def run_promotion(self, requested, *, main_status="ahead", ci_green=True,
                      develop_status="ahead", promote="true", missing_develop=False,
                      event="workflow_dispatch"):
        """Execute the actual workflow shell step with a fake, non-writing gh."""
        old = "a" * 40
        pinned = "b" * 40
        newest = "c" * 40
        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            gh = temp / "gh"
            gh.write_text("""#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$CALLS"
case "$*" in
  *"git/ref/heads/develop-published"*) echo "$OLD" ;;
  *"git/ref/heads/develop"*) [[ "$MISSING_DEVELOP" != true ]] || exit 1; echo "$OLD" ;;
  *"git/ref/heads/main"*) echo "$NEWEST" ;;
  *"compare/$PINNED...$NEWEST"*) echo "$MAIN_STATUS" ;;
  *"compare/$OLD..."*) echo "$DEVELOP_STATUS" ;;
  *"head_sha=$PINNED"*)
    [[ "$*" == *'select(.event == "push" or .event == "workflow_dispatch")'* ]]
    [[ "$*" == *'select(.conclusion == "success")'* ]]
    if [[ "$CI_GREEN" == true ]]; then echo "$PINNED"; fi ;;
  *"branch=main&event=push&status=success"*) echo "$NEWEST" ;;
  *"-X PATCH"*) echo "$PINNED" ;;
  *"-X POST"*) echo "$PINNED" ;;
  *) exit 99 ;;
esac
""")
            gh.chmod(0o755)
            output = temp / "output"
            calls = temp / "calls"
            env = dict(os.environ, PATH=str(temp) + os.pathsep + os.environ["PATH"],
                       GITHUB_REPOSITORY="owner/repo", GITHUB_EVENT_NAME=event,
                       GITHUB_OUTPUT=str(output), GITHUB_STEP_SUMMARY=str(temp / "summary"),
                       PROMOTE=promote, PUBLISH_INPUT="false", REQUESTED_SHA=requested,
                       OLD=old, PINNED=pinned, NEWEST=newest, CALLS=str(calls),
                       MAIN_STATUS=main_status, DEVELOP_STATUS=develop_status,
                       CI_GREEN=str(ci_green).lower(),
                       MISSING_DEVELOP=str(missing_develop).lower())
            run = load_workflow()["jobs"]["promote"]["steps"][0]["run"]
            proc = subprocess.run(["bash", "-c", run], env=env,
                                  capture_output=True, text=True, timeout=10)
            return (proc, calls.read_text() if calls.exists() else "",
                    output.read_text() if output.exists() else "")

    def test_exact_sha_behind_main_is_promoted_not_latest_green(self):
        pinned = "b" * 40
        proc, calls, output = self.run_promotion(pinned)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"-X PATCH repos/owner/repo/git/refs/heads/develop -f sha={pinned} -F force=false", calls)
        self.assertNotIn("branch=main&event=push", calls)
        self.assertIn(f"sha={pinned}\n", output)
        self.assertIn("changed=true\n", output)

    def test_exact_sha_off_main_is_refused_without_writes(self):
        for status in ("diverged", "behind"):
            with self.subTest(status=status):
                proc, calls, output = self.run_promotion("b" * 40, main_status=status)
                self.assertNotEqual(proc.returncode, 0)
                self.assertNotIn("-X PATCH", calls)
                self.assertNotIn("-X POST", calls)
                self.assertEqual(output, "")

    def test_exact_sha_requires_success_and_fast_forward(self):
        for kwargs in ({"ci_green": False}, {"develop_status": "behind"},
                       {"develop_status": "diverged"}, {"promote": "false"}):
            with self.subTest(kwargs=kwargs):
                proc, calls, output = self.run_promotion("b" * 40, **kwargs)
                self.assertNotEqual(proc.returncode, 0)
                self.assertNotIn("-X PATCH", calls)
                self.assertNotIn("-X POST", calls)
                self.assertEqual(output, "")

    def test_exact_sha_rejects_invalid_input(self):
        for requested in ("b" * 39, "main", "$(echo injected)"):
            with self.subTest(requested=requested):
                proc, calls, output = self.run_promotion(requested)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(calls, "")
                self.assertEqual(output, "")

    def test_exact_sha_can_create_missing_develop(self):
        pinned = "b" * 40
        proc, calls, output = self.run_promotion(pinned, missing_develop=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"-X POST repos/owner/repo/git/refs -f ref=refs/heads/develop -f sha={pinned}", calls)
        self.assertIn(f"sha={pinned}\n", output)

    def test_no_sha_preserves_floating_and_no_promote_modes(self):
        proc, calls, output = self.run_promotion("", event="schedule")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("branch=main&event=push&status=success", calls)
        self.assertIn("sha=" + "c" * 40 + "\n", output)
        self.assertIn("publish=true\n", output)
        proc, calls, output = self.run_promotion("", promote="false")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("-X PATCH", calls)
        self.assertNotIn("-X POST", calls)
        self.assertIn("sha=" + "a" * 40 + "\n", output)

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
