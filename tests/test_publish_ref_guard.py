"""Tests for the publish ref guard (scripts/check_publish_ref.py).

A publishing docker-publish run must come from a release tag that is on
main, with CI green on that exact commit and a closed release-checklist
issue. Only the highest released version moves :latest. The retag
workflow reuses the reachability check.
"""

import importlib.util
import json
import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_publish_ref", ROOT / "scripts" / "check_publish_ref.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)

SHA = "a" * 40


class FakeWorld:
    """Answers the git and gh calls the guard makes."""

    def __init__(self, on_main=(SHA,), tags=None, ci=("success",), issues=None,
                 releases=None):
        self.on_main = set(on_main)
        # GitHub releases by tag: {"isDraft": bool, "isPrerelease": bool}.
        # Default: every tag has a published, non-prerelease release.
        self.releases = releases
        self.tags = tags if tags is not None else {"2.2.7": "b" * 40}
        self.ci = list(ci)
        self.issues = issues if issues is not None else [
            {"number": 9, "title": "Release checklist: 2.3.0", "state": "CLOSED"}]
        self.on_main |= {c for c in self.tags.values() if c in on_main}

    def __call__(self, args):
        if args[:3] == ["git", "merge-base", "--is-ancestor"]:
            return (0 if args[3] in self.on_main else 1), ""
        if args[:2] == ["git", "tag"]:
            return 0, "\n".join(self.tags) + "\n"
        if args[:2] == ["git", "rev-parse"]:
            tag = args[2].split("refs/tags/")[1].split("^")[0]
            return (0, self.tags[tag] + "\n") if tag in self.tags else (1, "")
        if args[:3] == ["gh", "api", "--paginate"]:
            return 0, "\n".join(self.ci) + ("\n" if self.ci else "")
        if args[:3] == ["gh", "issue", "list"]:
            return 0, json.dumps(self.issues)
        if args[:3] == ["gh", "release", "view"]:
            tag = args[3]
            releases = self.releases
            if releases is None:
                releases = {t: {"isDraft": False, "isPrerelease": False} for t in self.tags}
            if tag not in releases:
                return 1, ""
            return 0, json.dumps(releases[tag])
        raise AssertionError(f"unexpected command {args}")


def env(**over):
    base = {
        "PUBLISH_VERSION": "2.3.0", "PUBLISH": "true",
        "GITHUB_REF": "refs/tags/2.3.0", "GITHUB_SHA": SHA,
        "GITHUB_REPOSITORY": "owner/repo",
    }
    base.update(over)
    return base


class PublishProblems(unittest.TestCase):
    def problems(self, world, **over):
        e = env(**over)
        return guard.publish_problems(
            e["PUBLISH_VERSION"], e["GITHUB_REF"], e["GITHUB_SHA"],
            e["GITHUB_REPOSITORY"], world)

    def test_clean_release_has_no_problems(self):
        self.assertEqual(self.problems(FakeWorld()), [])

    def test_branch_ref_is_refused(self):
        found = self.problems(FakeWorld(), GITHUB_REF="refs/heads/release/2.2.8-test")
        self.assertEqual(len(found), 1)
        self.assertIn("not a tag", found[0])

    def test_tag_must_match_the_version(self):
        found = self.problems(FakeWorld(), GITHUB_REF="refs/tags/2.2.9")
        self.assertTrue(any("2.2.9" in p and "2.3.0" in p for p in found))

    def test_commit_off_main_is_refused(self):
        found = self.problems(FakeWorld(on_main=()))
        self.assertTrue(any("not reachable from main" in p for p in found))

    def test_ci_must_have_succeeded_on_that_commit(self):
        for conclusions in ([], ["failure"], ["cancelled", "skipped"]):
            found = self.problems(FakeWorld(ci=conclusions))
            self.assertTrue(any("no successful run" in p for p in found), conclusions)
        self.assertEqual(self.problems(FakeWorld(ci=["failure", "success"])), [])

    def test_missing_checklist_issue_is_refused(self):
        found = self.problems(FakeWorld(issues=[]))
        self.assertTrue(any("no \"Release checklist: 2.3.0\"" in p for p in found))

    def test_checklist_for_another_version_does_not_count(self):
        other = [{"number": 3, "title": "Release checklist: 2.30.0", "state": "CLOSED"}]
        self.assertTrue(self.problems(FakeWorld(issues=other)))

    def test_open_checklist_is_refused(self):
        open_issue = [{"number": 9, "title": "Release checklist: 2.3.0", "state": "OPEN"}]
        found = self.problems(FakeWorld(issues=open_issue))
        self.assertTrue(any("still open" in p for p in found))


class LatestDecision(unittest.TestCase):
    def test_highest_version_moves_latest(self):
        self.assertTrue(guard.should_move_latest("2.3.0", ["2.2.7", "2.2.10"]))
        self.assertTrue(guard.should_move_latest("2.2.8", []))

    def test_older_version_does_not_move_latest(self):
        self.assertFalse(guard.should_move_latest("2.2.7", ["2.2.8"]))
        self.assertFalse(guard.should_move_latest("2.2.9", ["2.2.10"]))  # numeric, not lexical

    def test_tags_off_main_are_not_released(self):
        world = FakeWorld(
            on_main=("b" * 40,),
            tags={"2.2.7": "b" * 40, "2.2.8": "c" * 40, "nightly-x": "d" * 40})
        self.assertEqual(guard.released_versions(world), ["2.2.7"])


class EntryPoints(unittest.TestCase):
    def run_publish(self, world, tmp_output, **over):
        import os
        old = os.environ.get("GITHUB_OUTPUT")
        os.environ["GITHUB_OUTPUT"] = tmp_output
        try:
            code = guard.check_publish(env(**over), world)
        finally:
            if old is None:
                os.environ.pop("GITHUB_OUTPUT")
            else:
                os.environ["GITHUB_OUTPUT"] = old
        return code, pathlib.Path(tmp_output).read_text() if os.path.exists(tmp_output) else ""

    def test_candidate_from_any_ref_passes_and_never_moves_latest(self, ):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_publish(
                FakeWorld(on_main=(), ci=[], issues=[]), f"{tmp}/o",
                PUBLISH="false", GITHUB_REF="refs/heads/anything")
        self.assertEqual(code, 0)
        self.assertIn("move_latest=false", out)

    def test_publish_from_branch_fails_without_touching_latest(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_publish(
                FakeWorld(), f"{tmp}/o", GITHUB_REF="refs/heads/release/2.2.8-test")
        self.assertEqual(code, 1)
        self.assertIn("move_latest=false", out)

    def test_publishing_an_older_patch_keeps_latest(self):
        import tempfile
        world = FakeWorld(tags={"2.3.0": "b" * 40},
                          on_main=(SHA, "b" * 40),
                          issues=[{"number": 1, "title": "Release checklist: 2.2.9", "state": "CLOSED"}])
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_publish(
                world, f"{tmp}/o", PUBLISH_VERSION="2.2.9", GITHUB_REF="refs/tags/2.2.9")
        self.assertEqual(code, 0)
        self.assertIn("move_latest=false", out)

    def test_publishing_the_newest_release_moves_latest(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_publish(FakeWorld(), f"{tmp}/o")
        self.assertEqual(code, 0)
        self.assertIn("move_latest=true", out)

    def test_retag_requires_an_existing_tag_on_main(self):
        good = FakeWorld(on_main=("b" * 40,))
        self.assertEqual(guard.check_retag({"RETAG_VERSION": "2.2.7"}, good), 0)
        self.assertEqual(guard.check_retag({"RETAG_VERSION": "2.2.6"}, good), 1)
        off_main = FakeWorld(on_main=(), tags={"2.2.8": "c" * 40})
        self.assertEqual(guard.check_retag({"RETAG_VERSION": "2.2.8"}, off_main), 1)
        self.assertEqual(guard.check_retag({"RETAG_VERSION": "latest"}, good), 2)

    def test_retag_requires_a_published_github_release(self):
        # #1141: a tag on main is not enough. :latest must only ever point at a
        # version that was actually released, so the tag needs a published
        # GitHub release that is neither a draft nor a prerelease.
        tags = {"2.2.7": "b" * 40}
        env_ = {"RETAG_VERSION": "2.2.7", "GITHUB_REPOSITORY": "owner/repo"}
        published = FakeWorld(on_main=("b" * 40,), tags=tags)
        self.assertEqual(guard.check_retag(env_, published), 0)
        no_release = FakeWorld(on_main=("b" * 40,), tags=tags, releases={})
        self.assertEqual(guard.check_retag(env_, no_release), 1)
        draft = FakeWorld(on_main=("b" * 40,), tags=tags,
                          releases={"2.2.7": {"isDraft": True, "isPrerelease": False}})
        self.assertEqual(guard.check_retag(env_, draft), 1)
        pre = FakeWorld(on_main=("b" * 40,), tags=tags,
                        releases={"2.2.7": {"isDraft": False, "isPrerelease": True}})
        self.assertEqual(guard.check_retag(env_, pre), 1)


class WorkflowContract(unittest.TestCase):
    def load(self, name):
        return yaml.load((ROOT / ".github" / "workflows" / name).read_text(),
                         Loader=yaml.BaseLoader)

    def test_guard_runs_first_in_the_gate_job_with_the_needed_permissions(self):
        gate = self.load("docker-publish.yml")["jobs"]["surfaces-gate"]
        steps = gate["steps"]
        guard_index = next(i for i, s in enumerate(steps) if s.get("id") == "guard")
        self.assertLess(guard_index, next(
            i for i, s in enumerate(steps) if "check_release_surfaces" in s.get("run", "")))
        self.assertEqual(steps[0]["with"]["fetch-depth"], "0")
        self.assertIn("check_publish_ref.py", steps[guard_index]["run"])
        self.assertEqual(gate["permissions"]["actions"], "read")
        self.assertEqual(gate["permissions"]["issues"], "read")
        self.assertIn("move_latest", gate["outputs"])

    def test_latest_is_only_pushed_behind_the_guard_output(self):
        manifest = self.load("docker-publish.yml")["jobs"]["manifest"]
        self.assertIn("surfaces-gate", manifest["needs"])
        run = next(s["run"] for s in manifest["steps"] if "imagetools create" in s.get("run", ""))
        self.assertIn('"$MOVE_LATEST" == "true"', run)
        before = run.split('"$MOVE_LATEST" == "true"')[0]
        self.assertNotIn('-t "$IMAGE:latest"', before)
        self.assertEqual(manifest["env"]["MOVE_LATEST"],
                         "${{ needs.surfaces-gate.outputs.move_latest }}")

    def test_retag_workflow_is_manual_digest_copy_behind_the_main_check(self):
        wf = self.load("retag-latest.yml")
        self.assertEqual(list(wf["on"]), ["workflow_dispatch"])
        steps = wf["jobs"]["retag"]["steps"]
        runs = [s.get("run", "") for s in steps]
        check = next(i for i, r in enumerate(runs) if "--retag-latest-to" in r)
        copy = next(i for i, r in enumerate(runs) if "imagetools create" in r)
        self.assertLess(check, copy)
        self.assertFalse(any("buildx build" in r for r in runs))
        # The release lookup needs the repository and a token (#1141).
        check_env = steps[check].get("env", {})
        self.assertEqual(check_env.get("GH_TOKEN"), "${{ github.token }}")
        self.assertEqual(wf["permissions"], {"contents": "read", "packages": "write"})


if __name__ == "__main__":
    unittest.main()
