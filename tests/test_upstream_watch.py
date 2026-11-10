"""Tests for scripts/check_upstream_releases.py and its workflow.

The weekly upstream watch reads Print Handy's manual pins (the single
OrcaSlicer pin, go2rtc, the Spoolman and Bambu Studio commit pins),
compares them with upstream GitHub releases and reports through
deduplicated ``upstream-watch`` issues. These tests pin the parsing,
the prerelease-aware version ordering, the single-component
OrcaSlicer logic and the create/close decisions — all offline, with
fixture payloads.
"""

import importlib.util
import pathlib
import re
import sys
import unittest
from datetime import datetime, timezone

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_upstream_releases.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "upstream-watch.yml"

spec = importlib.util.spec_from_file_location("check_upstream_releases", SCRIPT)
watch = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = watch
spec.loader.exec_module(watch)


def release(version, *, prerelease=False, published="2026-01-01T00:00:00Z"):
    return watch.Release(
        version=version,
        tag=f"v{version}",
        prerelease=prerelease,
        published_at=watch.parse_datetime(published),
        url=f"https://example.invalid/releases/{version}",
    )


def pins(
    *,
    orca="2.5.0-beta",
    go2rtc="1.9.15",
    bambu_ref="b" * 40,
    spoolman_ref="5" * 40,
):
    return watch.Pins(
        orca=watch.OrcaPin(orca, "a" * 40),
        go2rtc_version=go2rtc,
        bambu_ref=bambu_ref,
        spoolman_ref=spoolman_ref,
    )


def findings_for(
    *,
    pin=None,
    orca=(),
    go2rtc=(),
    spoolman=(),
    bambu=(),
    spoolman_contains=(),
    bambu_contains=(),
):
    # The commit-pinned components are compared by commit, not date:
    # ``*_contains`` names the release tags whose commits the pin
    # already contains (the tag commit itself or a descendant of it).
    return watch.build_findings(
        pin or pins(),
        list(orca),
        list(go2rtc),
        list(spoolman),
        lambda tag: tag in spoolman_contains,
        list(bambu),
        lambda tag: tag in bambu_contains,
    )


ORCA_SCRIPT = """#!/bin/sh
case "$channel" in
    stable)
        version=2.5.0-rc
        revision=af2ffb8cddd2f3c0bdadc56d9cc3945fb23577ea
        sha256_amd64=aaaa
        ;;
    *) echo 'usage' >&2; exit 2 ;;
esac
"""


class PinParsing(unittest.TestCase):
    def test_orca_single_pin_parses(self):
        parsed = watch.parse_orca_pins(ORCA_SCRIPT)
        self.assertEqual(parsed.version, "2.5.0-rc")
        self.assertEqual(
            parsed.revision,
            "af2ffb8cddd2f3c0bdadc56d9cc3945fb23577ea",
        )

    def test_orca_missing_block_fails_closed(self):
        with self.assertRaises(watch.WatchError):
            watch.parse_orca_pins("#!/bin/sh\necho nothing pinned here\n")

    def test_orca_block_missing_a_pin_fails_closed(self):
        with self.assertRaises(watch.WatchError):
            watch.parse_orca_pins(
                "case x in\n    stable)\n        version=2.5.0-rc\n        ;;\nesac\n"
            )

    def test_script_version_and_dockerfile_args(self):
        self.assertEqual(
            watch.parse_script_version("set -eu\nversion=1.9.15\n", "x"),
            "1.9.15",
        )
        dockerfile = "ARG BAMBU_REF=" + "b" * 40 + "\nARG SPOOLMAN_REF=" + "5" * 40 + "\n"
        self.assertEqual(watch.parse_dockerfile_arg(dockerfile, "BAMBU_REF"), "b" * 40)
        self.assertEqual(
            watch.parse_dockerfile_arg(dockerfile, "SPOOLMAN_REF"), "5" * 40
        )
        with self.assertRaises(watch.WatchError):
            watch.parse_dockerfile_arg(dockerfile, "MISSING_REF")

    def test_real_repo_pins_parse(self):
        # The shipped files themselves must stay parseable: this is the
        # fixture that catches a pin-file reshape breaking the watch.
        parsed = watch.read_pins(ROOT)
        self.assertRegex(parsed.orca.version, r"^\d+\.\d+\.\d+")
        self.assertRegex(parsed.orca.revision, r"^[0-9a-f]{40}$")
        self.assertRegex(parsed.go2rtc_version, r"^\d+\.\d+\.\d+$")
        self.assertRegex(parsed.bambu_ref, r"^[0-9a-f]{40}$")
        self.assertRegex(parsed.spoolman_ref, r"^[0-9a-f]{40}$")


class VersionOrdering(unittest.TestCase):
    def test_prerelease_suffixes_order_below_the_final(self):
        ordered = ["2.5.0-alpha", "2.5.0-beta", "2.5.0-rc", "2.5.0"]
        keys = [watch.version_key(v) for v in ordered]
        self.assertEqual(keys, sorted(keys))
        self.assertLess(watch.version_key("2.5.0-rc"), watch.version_key("2.5.0"))

    def test_serial_and_numeric_parts(self):
        self.assertLess(watch.version_key("2.5.0-rc"), watch.version_key("2.5.0-rc1"))
        self.assertLess(watch.version_key("2.5.0-rc1"), watch.version_key("2.5.0-rc2"))
        self.assertLess(watch.version_key("1.9.9"), watch.version_key("1.9.15"))
        self.assertLess(watch.version_key("2.4.9"), watch.version_key("2.5.0-alpha"))

    def test_leading_v_is_ignored(self):
        self.assertEqual(watch.version_key("v2.5.0"), watch.version_key("2.5.0"))


class SelectLatest(unittest.TestCase):
    def test_stable_and_prerelease_selected_independently(self):
        releases = [
            release("2.4.0"),
            release("2.5.0-beta", prerelease=True),
            release("2.5.0-rc", prerelease=True),
        ]
        stable, pre = watch.select_latest(releases)
        self.assertEqual(stable.version, "2.4.0")
        self.assertEqual(pre.version, "2.5.0-rc")


class PlanActions(unittest.TestCase):
    def test_newer_prerelease_creates_one_issue(self):
        # The pin is the beta; the RC prerelease is newer. The single
        # OrcaSlicer component counts prereleases because the shipped
        # build is itself one, so exactly one issue is planned.
        findings = findings_for(orca=[release("2.5.0-rc", prerelease=True)])
        actions = watch.plan_actions(findings, [])
        creates = [(a.component, a.version) for a in actions if a.kind == "create"]
        self.assertEqual(creates, [("OrcaSlicer", "2.5.0-rc")])
        self.assertEqual(
            watch.issue_title("OrcaSlicer", "2.5.0-rc"),
            "Upstream release: OrcaSlicer 2.5.0-rc",
        )

    def test_older_prerelease_does_not_shadow_a_newer_final(self):
        # Both the final and its older prerelease are newer than the
        # pin: the final is the finding, and there is exactly one
        # planned issue — the prerelease is not reported separately.
        findings = findings_for(
            orca=[release("2.5.0"), release("2.5.0-rc", prerelease=True)],
        )
        by_component = {f.component: f for f in findings}
        self.assertEqual(by_component["OrcaSlicer"].newer_release.version, "2.5.0")
        actions = watch.plan_actions(findings, [])
        creates = [(a.component, a.version) for a in actions if a.kind == "create"]
        self.assertEqual(creates, [("OrcaSlicer", "2.5.0")])

    def test_newer_prerelease_beats_an_older_final(self):
        findings = findings_for(
            orca=[release("2.4.2"), release("2.5.0-rc", prerelease=True)],
        )
        by_component = {f.component: f for f in findings}
        self.assertEqual(
            by_component["OrcaSlicer"].newer_release.version, "2.5.0-rc"
        )

    def test_existing_open_issue_dedupes(self):
        findings = findings_for(orca=[release("2.5.0-rc", prerelease=True)])
        open_issues = [watch.WatchIssue(7, "OrcaSlicer", "2.5.0-rc")]
        self.assertEqual(watch.plan_actions(findings, open_issues), [])

    def test_issue_closes_when_the_pin_catches_up(self):
        findings = findings_for(pin=pins(orca="2.5.0-rc"))
        open_issues = [watch.WatchIssue(7, "OrcaSlicer", "2.5.0-rc")]
        actions = watch.plan_actions(findings, open_issues)
        self.assertEqual(
            [(a.kind, a.issue_number) for a in actions], [("close", 7)]
        )

    def test_quiet_week_plans_nothing(self):
        findings = findings_for(
            orca=[release("2.5.0-beta", prerelease=True)],
            go2rtc=[release("1.9.15")],
        )
        self.assertEqual(watch.plan_actions(findings, []), [])

    def test_go2rtc_newer_release(self):
        findings = findings_for(go2rtc=[release("1.9.16")])
        actions = watch.plan_actions(findings, [])
        self.assertEqual(
            [(a.kind, a.component, a.version) for a in actions],
            [("create", "go2rtc", "1.9.16")],
        )

    def test_commit_pin_flags_a_release_the_pin_does_not_contain(self):
        findings = findings_for(
            spoolman=[release("0.20.0", published="2026-06-01T00:00:00Z")]
        )
        by_component = {f.component: f for f in findings}
        self.assertEqual(
            by_component["Spoolman"].newer_release.version, "0.20.0"
        )
        actions = watch.plan_actions(findings, [])
        self.assertIn(
            ("create", "Spoolman", "0.20.0"),
            [(a.kind, a.component, a.version) for a in actions],
        )

    def test_commit_pin_ignores_a_release_the_pin_contains(self):
        findings = findings_for(
            spoolman=[release("0.20.0", published="2024-06-01T00:00:00Z")],
            spoolman_contains={"v0.20.0"},
        )
        by_component = {f.component: f for f in findings}
        self.assertIsNone(by_component["Spoolman"].newer_release)

    def test_commit_pin_exact_tag_release_published_later_is_not_newer(self):
        # Regression for #1130 / #1131: the pin sits exactly on the
        # release's tag commit, but the release was published hours
        # after that commit was written (Spoolman 0.27.1: commit
        # 09:39 UTC, release published 14:12 UTC). Comparing publish
        # time against the pinned commit's date made the pin read as
        # older than its own release; publish time must not matter.
        findings = findings_for(
            spoolman=[release("0.27.1", published="2026-10-31T14:12:51Z")],
            spoolman_contains={"v0.27.1"},
        )
        by_component = {f.component: f for f in findings}
        self.assertIsNone(by_component["Spoolman"].newer_release)
        self.assertEqual(watch.plan_actions(findings, []), [])

    def test_commit_pin_issue_closes_when_the_pin_contains_the_release(self):
        # The pin contains the release's tag commit: nothing newer,
        # and the still-open issue for that release closes.
        findings = findings_for(
            spoolman=[release("0.20.0")],
            spoolman_contains={"v0.20.0"},
        )
        open_issues = [watch.WatchIssue(9, "Spoolman", "0.20.0")]
        actions = watch.plan_actions(findings, open_issues)
        self.assertEqual(
            [(a.kind, a.issue_number) for a in actions], [("close", 9)]
        )

    def test_commit_pin_issue_stays_open_for_a_release_the_pin_lacks(self):
        # The pin contains an older Bambu tag only: the open issue
        # for the newer release neither closes nor duplicates.
        findings = findings_for(
            bambu=[release("02.09.01.70"), release("02.08.00.00")],
            bambu_contains={"v02.08.00.00"},
        )
        open_issues = [watch.WatchIssue(11, "Bambu Studio", "02.09.01.70")]
        self.assertEqual(watch.plan_actions(findings, open_issues), [])


class FakeClient(watch.GitHubClient):
    """Records requests instead of performing them.

    ``responses`` maps (method, path-without-query) to the single page
    of results; ``pages`` maps the same key to {page_number: results}
    for pagination tests.
    """

    def __init__(self, responses=None, pages=None):
        super().__init__(token=None)
        self.responses = responses or {}
        self.pages = pages or {}
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        base = path.split("?")[0]
        match = re.search(r"[?&]page=(\d+)", path)
        page = int(match.group(1)) if match else 1
        if (method, base) in self.pages:
            return self.pages[(method, base)].get(page, [])
        if page > 1:
            return []
        return self.responses.get((method, base))


class ClientBehavior(unittest.TestCase):
    def test_drafts_are_filtered_and_tags_normalized(self):
        client = FakeClient(
            {
                ("GET", "/repos/X/Y/releases"): [
                    {
                        "tag_name": "v1.0.0",
                        "prerelease": False,
                        "draft": False,
                        "published_at": "2026-01-01T00:00:00Z",
                        "html_url": "https://example.invalid/1",
                    },
                    {
                        "tag_name": "v1.1.0",
                        "prerelease": True,
                        "draft": True,
                        "published_at": "2026-02-01T00:00:00Z",
                        "html_url": "https://example.invalid/2",
                    },
                ]
            }
        )
        releases = client.list_releases("X/Y")
        self.assertEqual([r.version for r in releases], ["1.0.0"])

    def test_open_watch_issues_paginate_past_the_first_hundred(self):
        # Regression: with more than 100 open watch issues, page 2+
        # must still be read — otherwise dedupe misses existing
        # reports and catch-up closes never reach the older issues.
        page_one = [
            {"number": n, "title": f"Upstream release: go2rtc 1.0.{n}"}
            for n in range(1, 101)
        ]
        page_two = [
            {
                "number": 101,
                "title": "Upstream release: OrcaSlicer 2.5.0-rc",
            }
        ]
        client = FakeClient(
            pages={("GET", "/repos/me/repo/issues"): {1: page_one, 2: page_two}}
        )
        issues = client.list_open_watch_issues("me/repo")
        self.assertEqual(len(issues), 101)
        self.assertEqual(issues[-1].component, "OrcaSlicer")
        self.assertEqual(issues[-1].version, "2.5.0-rc")

    def test_releases_paginate(self):
        def item(version):
            return {
                "tag_name": f"v{version}",
                "prerelease": False,
                "draft": False,
                "published_at": "2026-01-01T00:00:00Z",
                "html_url": "https://example.invalid/x",
            }

        page_one = [item(f"1.0.{n}") for n in range(100)]
        client = FakeClient(
            pages={
                ("GET", "/repos/X/Y/releases"): {1: page_one, 2: [item("0.9.0")]}
            }
        )
        releases = client.list_releases("X/Y")
        self.assertEqual(len(releases), 101)

    def test_commit_is_ancestor_reads_the_compare_status(self):
        # The compare endpoint takes the would-be ancestor (a release
        # tag) as its base: "identical" and "ahead" mean the head —
        # the pin — contains it; "behind" and "diverged" mean it
        # does not.
        for status, expected in [
            ("identical", True),
            ("ahead", True),
            ("behind", False),
            ("diverged", False),
        ]:
            client = FakeClient(
                {
                    ("GET", "/repos/X/Y/compare/v1.0.0...abc123"): {
                        "status": status
                    }
                }
            )
            self.assertEqual(
                client.commit_is_ancestor("X/Y", "v1.0.0", "abc123"),
                expected,
                f"compare status {status}",
            )

    def test_commit_is_ancestor_missing_compare_fails_closed(self):
        client = FakeClient({})
        with self.assertRaises(watch.WatchError):
            client.commit_is_ancestor("X/Y", "v1.0.0", "abc123")

    def test_open_watch_issues_parse_titles(self):
        client = FakeClient(
            {
                ("GET", "/repos/me/repo/issues"): [
                    {
                        "number": 3,
                        "title": "Upstream release: OrcaSlicer 2.5.0-rc",
                    },
                    {"number": 4, "title": "Some unrelated issue"},
                ]
            }
        )
        issues = client.list_open_watch_issues("me/repo")
        self.assertEqual(
            [(i.number, i.component, i.version) for i in issues],
            [(3, "OrcaSlicer", "2.5.0-rc")],
        )


class WorkflowContract(unittest.TestCase):
    def load_workflow(self):
        return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)

    def test_triggers_are_weekly_schedule_and_dispatch(self):
        workflow = self.load_workflow()
        self.assertEqual(sorted(workflow["on"]), ["schedule", "workflow_dispatch"])
        self.assertEqual(workflow["on"]["schedule"], [{"cron": "37 13 * * 1"}])

    def test_permissions_are_issues_write_only(self):
        workflow = self.load_workflow()
        self.assertEqual(
            workflow["permissions"], {"contents": "read", "issues": "write"}
        )

    def test_job_runs_the_script_with_the_workflow_token(self):
        workflow = self.load_workflow()
        job = workflow["jobs"]["check-upstream-releases"]
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        runs = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("python3 scripts/check_upstream_releases.py", runs)
        envs = [step.get("env", {}) for step in job["steps"]]
        self.assertTrue(
            any("GITHUB_TOKEN" in env for env in envs),
            "the script step must receive the workflow token",
        )


if __name__ == "__main__":
    unittest.main()
