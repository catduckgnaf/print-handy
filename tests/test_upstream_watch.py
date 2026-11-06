"""Tests for scripts/check_upstream_releases.py and its workflow.

The weekly upstream watch reads Print Handy's manual pins (OrcaSlicer
channels, go2rtc, the Spoolman and Bambu Studio commit pins), compares
them with upstream GitHub releases and reports through deduplicated
``upstream-watch`` issues. These tests pin the parsing, the
prerelease-aware version ordering, the two-channel OrcaSlicer logic
and the create/close decisions — all offline, with fixture payloads.
"""

import importlib.util
import pathlib
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
    orca_stable="2.5.0-beta",
    orca_preview="2.5.0-beta",
    go2rtc="1.9.15",
    bambu_ref="b" * 40,
    spoolman_ref="5" * 40,
):
    return watch.Pins(
        orca_stable=watch.ChannelPin(orca_stable, "a" * 40),
        orca_preview=watch.ChannelPin(orca_preview, "a" * 40),
        go2rtc_version=go2rtc,
        bambu_ref=bambu_ref,
        spoolman_ref=spoolman_ref,
    )


OLD = watch.parse_datetime("2025-01-01T00:00:00Z")


def findings_for(*, pin=None, orca=(), go2rtc=(), spoolman=(), bambu=()):
    return watch.build_findings(
        pin or pins(),
        list(orca),
        list(go2rtc),
        list(spoolman),
        OLD,
        list(bambu),
        OLD,
    )


ORCA_SCRIPT = """#!/bin/sh
case "$channel" in
    stable)
        version=2.5.0-beta
        revision=345efec64c7b95a05ce5cf96df0dde66a158835c
        sha256_amd64=aaaa
        ;;
    preview)
        version=2.5.0-rc
        revision=af2ffb80000000000000000000000000000000
        sha256_amd64=bbbb
        ;;
    *) echo 'usage' >&2; exit 2 ;;
esac
"""


class PinParsing(unittest.TestCase):
    def test_orca_channels_parse_separately(self):
        parsed = watch.parse_orca_pins(ORCA_SCRIPT)
        self.assertEqual(parsed["stable"].version, "2.5.0-beta")
        self.assertEqual(
            parsed["stable"].revision,
            "345efec64c7b95a05ce5cf96df0dde66a158835c",
        )
        self.assertEqual(parsed["preview"].version, "2.5.0-rc")
        self.assertEqual(
            parsed["preview"].revision,
            "af2ffb80000000000000000000000000000000",
        )

    def test_orca_missing_channel_fails_closed(self):
        with self.assertRaises(watch.WatchError):
            watch.parse_orca_pins("case x in\n stable)\n version=1\n revision=2\n ;;\nesac\n")

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
        self.assertRegex(parsed.orca_stable.version, r"^\d+\.\d+\.\d+")
        self.assertRegex(parsed.orca_stable.revision, r"^[0-9a-f]{40}$")
        self.assertRegex(parsed.orca_preview.revision, r"^[0-9a-f]{40}$")
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
    def test_newer_release_creates_one_issue(self):
        findings = findings_for(orca=[release("2.5.0-rc", prerelease=True)])
        actions = watch.plan_actions(findings, [])
        creates = [(a.component, a.version) for a in actions if a.kind == "create"]
        # The RC is newer than the preview pin (still on the beta);
        # the stable channel ignores prereleases entirely.
        self.assertEqual(creates, [("OrcaSlicer (preview)", "2.5.0-rc")])

    def test_stable_channel_ignores_prereleases(self):
        findings = findings_for(
            pin=pins(orca_stable="2.4.0"),
            orca=[release("2.5.0-rc", prerelease=True)],
        )
        by_component = {f.component: f for f in findings}
        self.assertIsNone(by_component["OrcaSlicer (stable)"].newer_release)

    def test_new_final_counts_for_the_preview_channel(self):
        findings = findings_for(
            pin=pins(orca_preview="2.5.0-rc"),
            orca=[release("2.5.0"), release("2.5.0-rc", prerelease=True)],
        )
        by_component = {f.component: f for f in findings}
        self.assertEqual(
            by_component["OrcaSlicer (preview)"].newer_release.version, "2.5.0"
        )

    def test_existing_open_issue_dedupes(self):
        findings = findings_for(orca=[release("2.5.0-rc", prerelease=True)])
        open_issues = [watch.WatchIssue(7, "OrcaSlicer (preview)", "2.5.0-rc")]
        self.assertEqual(watch.plan_actions(findings, open_issues), [])

    def test_issue_closes_when_the_pin_catches_up(self):
        findings = findings_for(pin=pins(orca_preview="2.5.0-rc"))
        open_issues = [watch.WatchIssue(7, "OrcaSlicer (preview)", "2.5.0-rc")]
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

    def test_commit_pin_flags_a_release_that_postdates_it(self):
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

    def test_commit_pin_ignores_a_release_older_than_the_pin(self):
        findings = findings_for(
            spoolman=[release("0.20.0", published="2024-06-01T00:00:00Z")]
        )
        by_component = {f.component: f for f in findings}
        self.assertIsNone(by_component["Spoolman"].newer_release)

    def test_commit_pin_issue_closes_once_the_pin_passes_the_release(self):
        # Pin commit dated after the release: nothing newer, and the
        # still-open issue for that release closes.
        findings = watch.build_findings(
            pins(),
            [],
            [],
            [release("0.20.0", published="2024-06-01T00:00:00Z")],
            watch.parse_datetime("2025-06-01T00:00:00Z"),
            [],
            OLD,
        )
        open_issues = [watch.WatchIssue(9, "Spoolman", "0.20.0")]
        actions = watch.plan_actions(findings, open_issues)
        self.assertEqual(
            [(a.kind, a.issue_number) for a in actions], [("close", 9)]
        )


class FakeClient(watch.GitHubClient):
    """Records requests instead of performing them."""

    def __init__(self, responses):
        super().__init__(token=None)
        self.responses = responses
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        return self.responses.get((method, path.split("?")[0]))


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

    def test_open_watch_issues_parse_titles(self):
        client = FakeClient(
            {
                ("GET", "/repos/me/repo/issues"): [
                    {
                        "number": 3,
                        "title": "Upstream release: OrcaSlicer (preview) 2.5.0-rc",
                    },
                    {"number": 4, "title": "Some unrelated issue"},
                ]
            }
        )
        issues = client.list_open_watch_issues("me/repo")
        self.assertEqual(
            [(i.number, i.component, i.version) for i in issues],
            [(3, "OrcaSlicer (preview)", "2.5.0-rc")],
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
