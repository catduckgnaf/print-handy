"""Offline tests for scripts/bump_upstream.py and its two workflows.

Phase A of the upstream bump automation: the script plans and opens
draft bump PRs for go2rtc and Spoolman, and flips them to ready
under conservative guards. Everything here runs on fixtures - no
network, no real git.
"""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bump_upstream.py"
BUMP_WORKFLOW = ROOT / ".github" / "workflows" / "upstream-bump.yml"
READY_WORKFLOW = ROOT / ".github" / "workflows" / "upstream-bump-ready.yml"

spec = importlib.util.spec_from_file_location("bump_upstream", SCRIPT)
bump = importlib.util.module_from_spec(spec)
sys.modules["bump_upstream"] = bump  # dataclasses resolve the module by name
spec.loader.exec_module(bump)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)

# Frozen fixture copies of the pin surfaces (tests/fixtures/bump_upstream).
# Parser and planner tests run against these, never against the live
# production pins: every bump PR changes the production files, so a
# test that hardcoded the live values would fail the bump's own CI.
FIXTURES = ROOT / "tests" / "fixtures" / "bump_upstream"
GO2RTC_TEXT = (FIXTURES / "docker" / "go2rtc-prebuilt.sh").read_text()
DOCKERFILE_TEXT = (FIXTURES / "Dockerfile").read_text()


def fixture_root(base):
    """Copy the fixture tree into a temp root with production layout."""
    import shutil

    root = Path(base)
    for src in FIXTURES.rglob("*"):
        if src.is_file():
            dest = root / src.relative_to(FIXTURES)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
    return root


def make_release(version, age_days=10, assets=None, tag=None):
    return bump.Release(
        tag=tag or f"v{version}",
        version=version,
        name=f"v{version}",
        published_at=NOW - timedelta(days=age_days),
        html_url=f"https://example.test/releases/{version}",
        assets=assets or [],
    )


def asset(name, digest):
    return {"name": name, "digest": digest, "browser_download_url": f"https://example.test/dl/{name}"}


class FakeApi:
    """Records calls; serves prepared releases, tag commits and PRs."""

    def __init__(self, releases=None, commits=None, compare="ahead", prs=None, download_data=b"binary"):
        self.releases = releases or {}
        self.commits = commits or {}
        self.compare = compare
        self.prs = prs or []
        self.download_data = download_data
        self.created = []
        self.labeled = []
        self.comments = []
        self.closed = []
        self.marked_ready = []
        self.checks = {}

    def get_latest_release(self, repo):
        return self.releases.get(repo)

    def resolve_tag_commit(self, repo, tag):
        return self.commits[(repo, tag)]

    def compare_status(self, repo, base, head):
        return self.compare

    def list_open_prs(self, repo):
        return list(self.prs)

    def find_open_pr_for_branch(self, repo, branch):
        for pr in self.prs:
            if (pr.get("head") or {}).get("ref") == branch:
                return pr
        return None

    def download(self, url):
        return self.download_data

    def create_pr(self, repo, title, head, base, body):
        self.created.append({"title": title, "head": head, "base": base, "body": body})
        return {"number": 4242}

    def add_labels(self, repo, number, labels):
        self.labeled.append((number, labels))

    def comment(self, repo, number, body):
        self.comments.append((number, body))

    def close_pr(self, repo, number):
        self.closed.append(number)

    def check_runs_for_ref(self, repo, ref):
        return self.checks.get(ref, [])

    def mark_ready(self, node_id):
        self.marked_ready.append(node_id)


class FakeGit:
    def __init__(self):
        self.calls = []

    def current_ref(self):
        return "main"

    def create_branch(self, branch):
        self.calls.append(("branch", branch))

    def commit_files(self, files, message):
        self.calls.append(("commit", tuple(files), message))

    def push(self, branch):
        self.calls.append(("push", branch))

    def switch(self, ref):
        self.calls.append(("switch", ref))


def pr_dict(number, branch, labels=("upstream-bump",), draft=True, author="print-handy-upstream-bot[bot]", sha="a" * 40):
    return {
        "number": number,
        "draft": draft,
        "labels": [{"name": name} for name in labels],
        "user": {"login": author},
        "head": {"ref": branch, "sha": sha},
        "node_id": f"PR_node_{number}",
    }


class VersionOrdering(unittest.TestCase):
    def test_final_beats_prereleases(self):
        self.assertTrue(bump.is_newer("2.5.0", "2.5.0-rc"))
        self.assertTrue(bump.is_newer("2.5.0-rc", "2.5.0-beta"))
        self.assertTrue(bump.is_newer("2.5.0-beta", "2.5.0-alpha"))
        self.assertFalse(bump.is_newer("2.5.0-rc", "2.5.0"))

    def test_numeric_segments(self):
        self.assertTrue(bump.is_newer("1.9.16", "1.9.15"))
        self.assertTrue(bump.is_newer("0.27.1", "0.27.0"))
        self.assertFalse(bump.is_newer("1.9.15", "1.9.15"))
        self.assertFalse(bump.is_newer("1.9.9", "1.9.15"))

    def test_v_prefix_and_rc_numbers(self):
        self.assertTrue(bump.is_newer("v1.10.0", "1.9.99"))
        self.assertTrue(bump.is_newer("2.5.0-rc2", "2.5.0-rc1"))


class PinSurfaces(unittest.TestCase):
    """Against the frozen fixtures; the values below are the fixture's,
    frozen on purpose so future production bumps cannot break them."""

    def test_go2rtc_parse_fixture(self):
        pin = bump.parse_go2rtc(GO2RTC_TEXT)
        self.assertEqual(pin.version, "1.9.15")
        self.assertEqual(pin.revision, "7c05066")
        self.assertEqual(len(pin.sha256_amd64), 64)
        self.assertEqual(pin.asset_amd64, "go2rtc_linux_amd64")
        self.assertEqual(pin.asset_arm64, "go2rtc_linux_arm64")

    def test_go2rtc_round_trip(self):
        pin = bump.parse_go2rtc(GO2RTC_TEXT)
        pin.version = "1.9.16"
        pin.revision = "abc1234"
        out = bump.render_go2rtc(GO2RTC_TEXT, pin)
        again = bump.parse_go2rtc(out)
        self.assertEqual(again.version, "1.9.16")
        self.assertEqual(again.revision, "abc1234")
        # Nothing else in the file moved.
        self.assertEqual(out.replace("version=1.9.16", "version=1.9.15").replace("revision=abc1234", "revision=7c05066"), GO2RTC_TEXT)

    def test_spoolman_parse_and_round_trip(self):
        ref = bump.parse_spoolman(DOCKERFILE_TEXT)
        self.assertEqual(ref, "bed3f14ee77a3a571d31b14215d54b0b88a7519c")
        new_ref = "f" * 40
        out = bump.render_spoolman(DOCKERFILE_TEXT, new_ref)
        self.assertEqual(bump.parse_spoolman(out), new_ref)
        self.assertEqual(out.count(new_ref), 1)

    def test_missing_pin_is_an_error(self):
        with self.assertRaises(bump.BumpError):
            bump.parse_go2rtc("version=1.0\n")

    def test_live_pin_files_keep_the_expected_shape(self):
        # Format-only assertions over the PRODUCTION files: whatever
        # values future bumps land, the shapes the parsers rely on
        # must hold. No live value is hardcoded here.
        pin = bump.parse_go2rtc((ROOT / "docker" / "go2rtc-prebuilt.sh").read_text())
        self.assertRegex(pin.version, r"^\d+\.\d+\.\d+$")
        self.assertRegex(pin.revision, r"^[0-9a-f]{7}$")
        self.assertRegex(pin.sha256_amd64, r"^[0-9a-f]{64}$")
        self.assertRegex(pin.sha256_arm64, r"^[0-9a-f]{64}$")
        self.assertTrue(pin.asset_amd64 and pin.asset_arm64)
        ref = bump.parse_spoolman((ROOT / "Dockerfile").read_text())
        self.assertRegex(ref, r"^[0-9a-f]{40}$")


class Go2rtcPlanning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = fixture_root(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def api_for(self, release, commit="b" * 40):
        return FakeApi(
            releases={bump.GO2RTC_REPO: release},
            commits={(bump.GO2RTC_REPO, release.tag): commit},
        )

    def test_current_pin_skips(self):
        plan = bump.plan_go2rtc(self.root, self.api_for(make_release("1.9.15")), NOW)
        self.assertEqual(plan.action, "current")

    def test_young_release_waits(self):
        plan = bump.plan_go2rtc(self.root, self.api_for(make_release("1.9.16", age_days=1)), NOW)
        self.assertEqual(plan.action, "too-young")

    def test_bump_uses_asset_digests_and_tag_commit(self):
        release = make_release("1.9.16", assets=[
            asset("go2rtc_linux_amd64", "sha256:" + "1" * 64),
            asset("go2rtc_linux_arm64", "sha256:" + "2" * 64),
        ])
        plan = bump.plan_go2rtc(self.root, self.api_for(release, commit="c" * 40), NOW)
        self.assertEqual(plan.action, "bump")
        plan.apply(self.root)
        pin = bump.parse_go2rtc((self.root / "docker" / "go2rtc-prebuilt.sh").read_text())
        self.assertEqual(pin.version, "1.9.16")
        self.assertEqual(pin.revision, "c" * 7)  # same 7-char form as the file
        self.assertEqual(pin.sha256_amd64, "1" * 64)
        self.assertEqual(pin.sha256_arm64, "2" * 64)
        self.assertTrue(any("digest field" in line for line in plan.provenance))

    def test_bump_moves_every_recorded_surface(self):
        release = make_release("1.9.16", assets=[
            asset("go2rtc_linux_amd64", "sha256:" + "1" * 64),
            asset("go2rtc_linux_arm64", "sha256:" + "2" * 64),
        ])
        plan = bump.plan_go2rtc(self.root, self.api_for(release, commit="c" * 40), NOW)
        plan.apply(self.root)
        guard = (self.root / "tests" / "test_go2rtc_container.py").read_text()
        self.assertIn("version=1.9.16", guard)
        self.assertIn('AMD64_SHA = "' + "1" * 64 + '"', guard)
        self.assertIn('ARM64_SHA = "' + "2" * 64 + '"', guard)
        self.assertIn("`1.9.16`, revision `ccccccc`", (self.root / "docs" / "COMPONENTS.md").read_text())
        self.assertIn("go2rtc v1.9.16", (self.root / "docs" / "DOCKER.md").read_text())
        self.assertIn("(v1.9.16,", (self.root / "docs" / "SETTINGS.md").read_text())
        self.assertEqual(
            set(plan.files),
            {"docker/go2rtc-prebuilt.sh", "tests/test_go2rtc_container.py",
             "docs/COMPONENTS.md", "docs/DOCKER.md", "docs/SETTINGS.md"},
        )

    def test_drifted_recorded_surface_stops_the_bump(self):
        components = self.root / "docs" / "COMPONENTS.md"
        components.write_text(components.read_text().replace("`1.9.15`, revision `7c05066`", "`1.9.15`"))
        release = make_release("1.9.16", assets=[
            asset("go2rtc_linux_amd64", "sha256:" + "1" * 64),
            asset("go2rtc_linux_arm64", "sha256:" + "2" * 64),
        ])
        with self.assertRaises(bump.BumpError):
            bump.plan_go2rtc(self.root, self.api_for(release), NOW)

    def test_missing_digest_falls_back_to_download_and_hash(self):
        import hashlib

        release = make_release("1.9.16", assets=[
            asset("go2rtc_linux_amd64", None),
            asset("go2rtc_linux_arm64", ""),
        ])
        api = self.api_for(release)
        plan = bump.plan_go2rtc(self.root, api, NOW)
        expected = hashlib.sha256(b"binary").hexdigest()
        self.assertIn(f"sha256_amd64: {bump.parse_go2rtc(GO2RTC_TEXT).sha256_amd64} -> {expected}", plan.changes)
        self.assertTrue(any("hashed locally" in line for line in plan.provenance))

    def test_missing_asset_is_an_error(self):
        release = make_release("1.9.16", assets=[])
        with self.assertRaises(bump.BumpError):
            bump.plan_go2rtc(self.root, self.api_for(release), NOW)


class SpoolmanPlanning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = fixture_root(self.tmp.name)
        self.pin_ref = bump.parse_spoolman(DOCKERFILE_TEXT)

    def tearDown(self):
        self.tmp.cleanup()

    def api_for(self, release, commit, compare="ahead"):
        return FakeApi(
            releases={bump.SPOOLMAN_REPO: release},
            commits={(bump.SPOOLMAN_REPO, release.tag): commit},
            compare=compare,
        )

    def test_pin_at_latest_release_is_current(self):
        plan = bump.plan_spoolman(self.root, self.api_for(make_release("0.27.0"), self.pin_ref), NOW)
        self.assertEqual(plan.action, "current")

    def test_bump_to_newer_release(self):
        new_ref = "e" * 40
        plan = bump.plan_spoolman(self.root, self.api_for(make_release("0.27.1"), new_ref), NOW)
        self.assertEqual(plan.action, "bump")
        plan.apply(self.root)
        self.assertEqual(bump.parse_spoolman((self.root / "Dockerfile").read_text()), new_ref)
        # The recorded surfaces move with the pin.
        settings = (self.root / "docs" / "SETTINGS.md").read_text()
        self.assertIn(new_ref, settings)
        self.assertIn("Pinned Spoolman v0.27.1 source commit", settings)
        docker_doc = (self.root / "docs" / "DOCKER.md").read_text()
        self.assertIn(f"Spoolman 0.27.1 is built from exact upstream revision `{new_ref}`", docker_doc)
        self.assertEqual(set(plan.files), {"Dockerfile", "docs/SETTINGS.md", "docs/DOCKER.md"})

    def test_disagreeing_recorded_surfaces_stop_the_bump(self):
        settings = self.root / "docs" / "SETTINGS.md"
        settings.write_text(settings.read_text().replace("v0.27.0", "v0.26.9"))
        with self.assertRaises(bump.BumpError):
            bump.plan_spoolman(self.root, self.api_for(make_release("0.27.1"), "e" * 40), NOW)

    def test_pin_ahead_of_release_is_not_a_bump(self):
        plan = bump.plan_spoolman(
            self.root, self.api_for(make_release("0.27.1"), "e" * 40, compare="behind"), NOW
        )
        self.assertEqual(plan.action, "not-behind")

    def test_young_release_waits(self):
        plan = bump.plan_spoolman(
            self.root, self.api_for(make_release("0.27.1", age_days=2), "e" * 40), NOW
        )
        self.assertEqual(plan.action, "too-young")


class DedupeSupersede(unittest.TestCase):
    def test_same_version_open_pr_dedupes(self):
        prs = [pr_dict(7, "upstream/go2rtc-1.9.16")]
        decision, found = bump.decide_existing(prs, "go2rtc", "1.9.16")
        self.assertEqual(decision, "dedupe")
        self.assertEqual(found["number"], 7)

    def test_older_open_pr_is_superseded(self):
        prs = [pr_dict(7, "upstream/go2rtc-1.9.16")]
        decision, found = bump.decide_existing(prs, "go2rtc", "1.9.17")
        self.assertEqual(decision, "supersede")
        self.assertEqual([p["number"] for p in found], [7])

    def test_unlabeled_or_foreign_prs_are_never_touched(self):
        prs = [
            pr_dict(7, "upstream/go2rtc-1.9.16", labels=()),
            pr_dict(8, "upstream/spoolman-0.27.1"),
            pr_dict(9, "feat/something-else"),
        ]
        decision, _ = bump.decide_existing(prs, "go2rtc", "1.9.17")
        self.assertEqual(decision, "fresh")

    def test_execute_supersede_closes_old_with_pointer(self):
        release = make_release("0.27.1")
        plan = bump.Plan("spoolman", "bump", version="0.27.1", files=["Dockerfile"],
                         release=release)
        api = FakeApi(prs=[pr_dict(7, "upstream/spoolman-0.27.0")])
        git = FakeGit()
        outcome = bump.execute_plan(Path("."), api, "catduckgnaf/print-handy", plan, False, git=git, log=lambda m: None)
        self.assertEqual(outcome, "opened")
        self.assertEqual(api.closed, [7])
        self.assertIn("4242", api.comments[0][1])
        self.assertEqual(api.labeled, [(4242, ["dependencies", "upstream-bump"])])

    def test_execute_dedupe_noops(self):
        plan = bump.Plan("go2rtc", "bump", version="1.9.16", files=["docker/go2rtc-prebuilt.sh"],
                         release=make_release("1.9.16"))
        api = FakeApi(prs=[pr_dict(9, "upstream/go2rtc-1.9.16")])
        outcome = bump.execute_plan(Path("."), api, "catduckgnaf/print-handy", plan, False, git=FakeGit(), log=lambda m: None)
        self.assertEqual(outcome, "dedupe")
        self.assertEqual(api.created, [])


class IdentityGate(unittest.TestCase):
    def test_both_secrets_required(self):
        self.assertFalse(bump.identity_configured({}))
        self.assertFalse(bump.identity_configured({"UPSTREAM_BOT_APP_ID": "1"}))
        self.assertFalse(bump.identity_configured({"UPSTREAM_BOT_PRIVATE_KEY": "k"}))
        self.assertTrue(bump.identity_configured(
            {"UPSTREAM_BOT_APP_ID": "1", "UPSTREAM_BOT_PRIVATE_KEY": "k"}))

    def test_check_identity_output(self):
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(bump.check_identity({}, out, err), 0)
        self.assertEqual(out.getvalue().strip(), "configured=false")
        self.assertIn("not configured", err.getvalue())
        out, err = io.StringIO(), io.StringIO()
        bump.check_identity({"UPSTREAM_BOT_APP_ID": "1", "UPSTREAM_BOT_PRIVATE_KEY": "k"}, out, err)
        self.assertEqual(out.getvalue().strip(), "configured=true")

    def test_write_mode_without_token_is_inert(self):
        err = io.StringIO()
        rc = bump.main([], env={}, out=io.StringIO(), err=err)
        self.assertEqual(rc, 0)
        self.assertIn("inert", err.getvalue())

    def test_mark_ready_without_token_is_inert(self):
        err = io.StringIO()
        rc = bump.main(["--mark-ready", "--branch", "upstream/go2rtc-1.9.16"], env={}, out=io.StringIO(), err=err)
        self.assertEqual(rc, 0)
        self.assertIn("inert", err.getvalue())


class ReadyDecision(unittest.TestCase):
    def checks(self, *specs):
        return [
            {"name": name, "status": status, "conclusion": conclusion, "started_at": "2026-11-05T00:00:00Z"}
            for name, status, conclusion in specs
        ]

    def test_all_guards_pass(self):
        ok, _ = bump.ready_decision(
            pr_dict(3, "upstream/go2rtc-1.9.16"),
            self.checks(("CI", "completed", "success"), ("docker", "completed", "success")),
        )
        self.assertTrue(ok)

    def test_any_failure_blocks(self):
        ok, reason = bump.ready_decision(
            pr_dict(3, "upstream/go2rtc-1.9.16"),
            self.checks(("CI", "completed", "failure")),
        )
        self.assertFalse(ok)
        self.assertIn("CI", reason)

    def test_running_check_blocks(self):
        ok, _ = bump.ready_decision(
            pr_dict(3, "upstream/go2rtc-1.9.16"),
            self.checks(("CI", "in_progress", None)),
        )
        self.assertFalse(ok)

    def test_label_and_author_and_draft_required(self):
        good = self.checks(("CI", "completed", "success"))
        self.assertFalse(bump.ready_decision(pr_dict(3, "b", labels=()), good)[0])
        self.assertFalse(bump.ready_decision(pr_dict(3, "b", author="catduck"), good)[0])
        self.assertFalse(bump.ready_decision(pr_dict(3, "b", draft=False), good)[0])
        self.assertFalse(bump.ready_decision(None, good)[0])
        self.assertFalse(bump.ready_decision(pr_dict(3, "b"), [])[0])

    def test_latest_run_per_name_wins(self):
        runs = self.checks(("CI", "completed", "failure")) + [
            {"name": "CI", "status": "completed", "conclusion": "success", "started_at": "2026-11-05T01:00:00Z"}
        ]
        self.assertTrue(bump.ready_decision(pr_dict(3, "b"), runs)[0])

    def test_mark_ready_flow_marks_only_when_guards_pass(self):
        pr = pr_dict(3, "upstream/go2rtc-1.9.16")
        api = FakeApi(prs=[pr])
        api.checks[pr["head"]["sha"]] = self.checks(("CI", "completed", "success"))
        bump.mark_ready(Path("."), api, "catduckgnaf/print-handy", "upstream/go2rtc-1.9.16", log=lambda m: None)
        self.assertEqual(api.marked_ready, ["PR_node_3"])


class TransportLevelApi(unittest.TestCase):
    """GitHubApi against a canned opener: release picking and tag deref."""

    def canned(self, mapping):
        def opener(req):
            payload = mapping[req.full_url]
            body = json.dumps(payload).encode()

            class Resp:
                def read(self):
                    return body

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            return Resp()

        return opener

    def test_latest_release_skips_prereleases_and_drafts(self):
        url = "https://api.github.com/repos/X/y/releases?per_page=30"
        api = bump.GitHubApi(opener=self.canned({url: [
            {"tag_name": "v2.0.0-rc", "name": "rc", "draft": False, "prerelease": True,
             "published_at": "2026-11-04T00:00:00Z", "html_url": "u1", "assets": []},
            {"tag_name": "v1.5.0", "name": "draft", "draft": True, "prerelease": False,
             "published_at": "2026-11-03T00:00:00Z", "html_url": "u2", "assets": []},
            {"tag_name": "v1.4.0", "name": "final", "draft": False, "prerelease": False,
             "published_at": "2026-11-01T00:00:00Z", "html_url": "u3", "assets": []},
        ]}))
        release = api.get_latest_release("X/y")
        self.assertEqual(release.version, "1.4.0")

    def test_annotated_tag_dereferences_to_commit(self):
        base = "https://api.github.com/repos/X/y"
        api = bump.GitHubApi(opener=self.canned({
            f"{base}/git/ref/tags/v1.0.0": {"object": {"type": "tag", "sha": "t" * 40}},
            f"{base}/git/tags/{'t' * 40}": {"object": {"type": "commit", "sha": "c" * 40}},
        }))
        self.assertEqual(api.resolve_tag_commit("X/y", "v1.0.0"), "c" * 40)


class WorkflowShapes(unittest.TestCase):
    def load(self, path):
        return yaml.load(path.read_text(), Loader=yaml.BaseLoader)

    def test_bump_workflow_schedule_dispatch_and_identity(self):
        wf = self.load(BUMP_WORKFLOW)
        self.assertEqual(wf["permissions"]["contents"], "read")
        triggers = wf.get(True) or wf.get("on")  # 'on' key, however the loader spells it
        self.assertEqual(triggers["schedule"][0]["cron"], "7 15 * * 1")
        options = triggers["workflow_dispatch"]["inputs"]["component"]["options"]
        self.assertEqual(options, ["all", "go2rtc", "spoolman"])
        steps = wf["jobs"]["bump"]["steps"]
        text = BUMP_WORKFLOW.read_text()
        self.assertIn("actions/create-github-app-token@v3", text)
        self.assertIn("UPSTREAM_BOT_APP_ID", text)
        self.assertIn("UPSTREAM_BOT_PRIVATE_KEY", text)
        token_step = next(s for s in steps if s.get("id") == "app-token")
        self.assertIn("configured", token_step["if"])
        run_step = next(s for s in steps if "bump_upstream.py --component" in s.get("run", ""))
        self.assertIn("configured", run_step["if"])
        # The checkout must not persist the workflow token as a git
        # auth extraheader: it would override the app-token remote
        # URL and pushes would go out as the read-only workflow token.
        checkout = next(s for s in steps if "actions/checkout" in s.get("uses", ""))
        self.assertEqual(checkout["with"]["persist-credentials"], "false")

    def test_ready_workflow_checkout_does_not_persist_credentials(self):
        wf = self.load(READY_WORKFLOW)
        steps = wf["jobs"]["mark-ready"]["steps"]
        checkout = next(s for s in steps if "actions/checkout" in s.get("uses", ""))
        self.assertEqual(checkout["with"]["persist-credentials"], "false")

    def test_ready_workflow_trigger_and_guards(self):
        wf = self.load(READY_WORKFLOW)
        triggers = wf.get(True) or wf.get("on")
        self.assertEqual(triggers["workflow_run"]["workflows"], ["CI"])
        self.assertEqual(triggers["workflow_run"]["branches"], ["upstream/**"])
        job = wf["jobs"]["mark-ready"]
        self.assertIn("conclusion == 'success'", job["if"])
        text = READY_WORKFLOW.read_text()
        self.assertIn("actions/create-github-app-token@v3", text)
        self.assertIn("--mark-ready", text)


class PrBodyShape(unittest.TestCase):
    def test_body_carries_policy_verdicts_and_provenance(self):
        plan = bump.Plan(
            "go2rtc", "bump", version="1.9.16", files=["docker/go2rtc-prebuilt.sh"],
            changes=["version: 1.9.15 -> 1.9.16"],
            provenance=["Release: https://example.test (published 2026-10-26)"],
            release=make_release("1.9.16"),
        )
        body = bump.build_pr_body("go2rtc", plan)
        self.assertIn("Documentation: not needed", body)
        self.assertIn("Wiki: not applicable", body)
        self.assertIn("Variables: not applicable", body)
        self.assertIn("1.9.15 -> 1.9.16", body)
        self.assertIn("pending at open time", body)


if __name__ == "__main__":
    unittest.main()
