"""Tests for scripts/check_firmware_releases.py and its workflow.

The weekly firmware watch (issue #1271) compares each watched
firmware's latest GitHub release against the audited baseline
recorded in this repository (for ZMod, the audited-against version
in docs/ZMOD.md) and reports through deduplicated ``firmware-watch``
issues. These tests pin the baseline parsing, the ZMod version
ordering (a numeric ``-N`` suffix is a later build of the same
release, not a prerelease), the final-release selection and the
create/close decisions, all offline, with fixture payloads.
"""

import email.message
import importlib.util
import io
import pathlib
import re
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_firmware_releases.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "firmware-watch.yml"

spec = importlib.util.spec_from_file_location("check_firmware_releases", SCRIPT)
watch = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = watch
spec.loader.exec_module(watch)


def release(version, *, prerelease=False, published="2026-01-01T00:00:00Z"):
    return watch.Release(
        version=version,
        tag=version,
        prerelease=prerelease,
        published_at=watch.parse_datetime(published),
        url=f"https://example.invalid/releases/{version}",
    )


def zmod(baseline="1.7.6"):
    return watch.WatchedFirmware(
        name="ZMod",
        repo="ghzserg/zmod",
        baseline=baseline,
        baseline_source="docs/ZMOD.md audited baseline",
    )


def findings_for(*, baseline="1.7.6", releases=()):
    return watch.build_findings([zmod(baseline)], {"ZMod": list(releases)})


ZMOD_DOC = """# ZMod printers

Print Handy's ZMod contracts were last audited against **ZMod 1.7.6**
(released 2026-11-11; the feature-by-feature audit is recorded in the
project's issue tracker).
"""


class BaselineParsing(unittest.TestCase):
    def test_baseline_parses_from_the_doc_sentence(self):
        self.assertEqual(watch.parse_zmod_baseline(ZMOD_DOC), "1.7.6")

    def test_suffixed_build_baseline_parses(self):
        doc = ZMOD_DOC.replace("1.7.6", "1.7.5-2")
        self.assertEqual(watch.parse_zmod_baseline(doc), "1.7.5-2")

    def test_missing_baseline_fails_closed(self):
        with self.assertRaises(watch.WatchError):
            watch.parse_zmod_baseline("# ZMod printers\n\nNo baseline here.\n")

    def test_real_repo_baseline_parses(self):
        # The shipped doc itself must stay parseable, and Part A of
        # #1271 moved the baseline to the 1.7.6 audit: this is the
        # fixture that catches a doc reshape breaking the watch, and
        # it pins that the watch starts in sync with the audit.
        parsed = watch.read_baselines(ROOT)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].name, "ZMod")
        self.assertEqual(parsed[0].repo, "ghzserg/zmod")
        self.assertEqual(parsed[0].baseline, "1.7.6")


class VersionOrdering(unittest.TestCase):
    def test_numeric_build_suffix_ranks_above_the_plain_release(self):
        # ZMod's suffixed builds: 1.7.5-2 is a later build of 1.7.5
        # (it is the build the maintainer's own AD5X runs), so it must
        # order above 1.7.5, the opposite of a prerelease suffix.
        self.assertLess(
            watch.firmware_version_key("1.7.5"),
            watch.firmware_version_key("1.7.5-2"),
        )
        self.assertLess(
            watch.firmware_version_key("1.7.5-2"),
            watch.firmware_version_key("1.7.5-10"),
        )

    def test_build_suffix_ranks_below_the_next_release(self):
        self.assertLess(
            watch.firmware_version_key("1.7.5-2"),
            watch.firmware_version_key("1.7.6"),
        )
        self.assertLess(
            watch.firmware_version_key("1.7.5-2"),
            watch.firmware_version_key("1.8.0"),
        )

    def test_lettered_suffix_is_a_prerelease_below_the_final(self):
        self.assertLess(
            watch.firmware_version_key("1.7.6-rc1"),
            watch.firmware_version_key("1.7.6"),
        )
        self.assertLess(
            watch.firmware_version_key("1.7.6-beta"),
            watch.firmware_version_key("1.7.6-rc1"),
        )
        self.assertLess(
            watch.firmware_version_key("1.7.6-rc1"),
            watch.firmware_version_key("1.7.6-1"),
        )

    def test_numeric_parts_compare_numerically(self):
        self.assertLess(
            watch.firmware_version_key("1.9.9"),
            watch.firmware_version_key("1.9.15"),
        )
        self.assertLess(
            watch.firmware_version_key("1.7.9"),
            watch.firmware_version_key("1.7.15"),
        )

    def test_leading_v_is_ignored(self):
        self.assertEqual(
            watch.firmware_version_key("v1.7.6"),
            watch.firmware_version_key("1.7.6"),
        )


class SelectLatestFinal(unittest.TestCase):
    def test_newest_final_wins_over_older_releases(self):
        latest = watch.select_latest_final(
            [release("1.7.5"), release("1.7.6"), release("1.7.5-2")]
        )
        self.assertEqual(latest.version, "1.7.6")

    def test_prereleases_are_not_selected(self):
        # Firmware audits follow shipped releases: a newer prerelease
        # alone opens no issue.
        latest = watch.select_latest_final(
            [release("1.7.6"), release("1.8.0-rc1", prerelease=True)]
        )
        self.assertEqual(latest.version, "1.7.6")
        self.assertIsNone(
            watch.select_latest_final([release("1.8.0-rc1", prerelease=True)])
        )

    def test_no_releases(self):
        self.assertIsNone(watch.select_latest_final([]))


class PlanActions(unittest.TestCase):
    def test_newer_release_creates_one_issue(self):
        findings = findings_for(releases=[release("1.7.7")])
        actions = watch.plan_actions(findings, [])
        self.assertEqual(
            [(a.kind, a.firmware, a.version) for a in actions],
            [("create", "ZMod", "1.7.7")],
        )
        self.assertEqual(
            watch.issue_title("ZMod", "1.7.7"), "Firmware release: ZMod 1.7.7"
        )

    def test_baseline_equal_to_latest_plans_nothing(self):
        findings = findings_for(releases=[release("1.7.6"), release("1.7.5-2")])
        self.assertEqual(watch.plan_actions(findings, []), [])

    def test_suffixed_build_of_the_baseline_release_is_newer(self):
        # Baseline 1.7.5, upstream ships the 1.7.5-2 build: that is a
        # newer build of the audited release line and is reported.
        findings = findings_for(baseline="1.7.5", releases=[release("1.7.5-2")])
        actions = watch.plan_actions(findings, [])
        self.assertEqual(
            [(a.kind, a.version) for a in actions], [("create", "1.7.5-2")]
        )

    def test_newer_prerelease_only_plans_nothing(self):
        findings = findings_for(releases=[release("1.8.0-rc1", prerelease=True)])
        self.assertEqual(watch.plan_actions(findings, []), [])

    def test_existing_open_issue_dedupes(self):
        findings = findings_for(releases=[release("1.7.7")])
        open_issues = [watch.WatchIssue(7, "ZMod", "1.7.7")]
        self.assertEqual(watch.plan_actions(findings, open_issues), [])

    def test_issue_closes_when_the_baseline_catches_up(self):
        findings = findings_for(baseline="1.7.7", releases=[release("1.7.7")])
        open_issues = [watch.WatchIssue(7, "ZMod", "1.7.7")]
        actions = watch.plan_actions(findings, open_issues)
        self.assertEqual([(a.kind, a.issue_number) for a in actions], [("close", 7)])

    def test_issue_stays_open_until_the_baseline_reaches_it(self):
        findings = findings_for(baseline="1.7.6", releases=[release("1.7.7")])
        open_issues = [watch.WatchIssue(7, "ZMod", "1.7.7")]
        self.assertEqual(watch.plan_actions(findings, open_issues), [])

    def test_closed_issue_for_the_release_is_not_reopened(self):
        # A maintainer closed the 1.7.7 notice without moving the
        # baseline. The next weekly run must not open it again.
        findings = findings_for(releases=[release("1.7.7")])
        closed = [watch.WatchIssue(7, "ZMod", "1.7.7", open=False)]
        self.assertEqual(watch.plan_actions(findings, closed), [])

    def test_closed_issue_is_not_closed_again(self):
        findings = findings_for(baseline="1.7.7", releases=[release("1.7.7")])
        closed = [watch.WatchIssue(7, "ZMod", "1.7.7", open=False)]
        self.assertEqual(watch.plan_actions(findings, closed), [])

    def test_closed_older_notice_does_not_hide_a_newer_release(self):
        findings = findings_for(releases=[release("1.7.8"), release("1.7.7")])
        closed = [watch.WatchIssue(7, "ZMod", "1.7.7", open=False)]
        actions = watch.plan_actions(findings, closed)
        self.assertEqual(
            [(a.kind, a.version) for a in actions], [("create", "1.7.8")]
        )

    def test_issue_body_names_the_audit_follow_up(self):
        findings = findings_for(releases=[release("1.7.7")])
        body = watch.issue_body(findings[0], release("1.7.7"))
        self.assertIn("ZMod", body)
        self.assertIn("1.7.7", body)
        self.assertIn("https://example.invalid/releases/1.7.7", body)
        self.assertIn("#1269", body)
        self.assertIn("not an audit", body)


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
                ("GET", "/repos/ghzserg/zmod/releases"): [
                    {
                        "tag_name": "1.7.6",
                        "prerelease": False,
                        "draft": False,
                        "published_at": "2026-11-11T11:08:42Z",
                        "html_url": "https://example.invalid/1",
                    },
                    {
                        "tag_name": "v1.7.7",
                        "prerelease": False,
                        "draft": True,
                        "published_at": "2026-12-01T00:00:00Z",
                        "html_url": "https://example.invalid/2",
                    },
                ]
            }
        )
        releases = client.list_releases("ghzserg/zmod")
        self.assertEqual([r.version for r in releases], ["1.7.6"])

    def test_open_watch_issues_parse_titles(self):
        client = FakeClient(
            {
                ("GET", "/repos/me/repo/issues"): [
                    {"number": 3, "title": "Firmware release: ZMod 1.7.7"},
                    {"number": 4, "title": "Upstream release: go2rtc 1.9.16"},
                    {"number": 5, "title": "Some unrelated issue"},
                ]
            }
        )
        issues = client.list_watch_issues("me/repo")
        self.assertEqual(
            [(i.number, i.firmware, i.version) for i in issues],
            [(3, "ZMod", "1.7.7")],
        )

    def test_watch_issues_include_closed_ones(self):
        client = FakeClient(
            {
                ("GET", "/repos/me/repo/issues"): [
                    {"number": 3, "title": "Firmware release: ZMod 1.7.7",
                     "state": "closed"},
                    {"number": 4, "title": "Firmware release: ZMod 1.7.8",
                     "state": "open"},
                ]
            }
        )
        issues = client.list_watch_issues("me/repo")
        self.assertEqual(
            [(i.number, i.open) for i in issues], [(3, False), (4, True)]
        )
        self.assertIn("state=all", client.calls[0][1])

    def test_open_watch_issues_paginate_past_the_first_hundred(self):
        page_one = [
            {"number": n, "title": f"Firmware release: ZMod 1.0.{n}"}
            for n in range(1, 101)
        ]
        page_two = [{"number": 101, "title": "Firmware release: ZMod 1.7.7"}]
        client = FakeClient(
            pages={("GET", "/repos/me/repo/issues"): {1: page_one, 2: page_two}}
        )
        issues = client.list_watch_issues("me/repo")
        self.assertEqual(len(issues), 101)
        self.assertEqual(issues[-1].version, "1.7.7")

    def test_releases_paginate(self):
        def item(version):
            return {
                "tag_name": version,
                "prerelease": False,
                "draft": False,
                "published_at": "2026-01-01T00:00:00Z",
                "html_url": "https://example.invalid/x",
            }

        page_one = [item(f"1.0.{n}") for n in range(100)]
        client = FakeClient(
            pages={
                ("GET", "/repos/ghzserg/zmod/releases"): {
                    1: page_one,
                    2: [item("0.9.0")],
                }
            }
        )
        releases = client.list_releases("ghzserg/zmod")
        self.assertEqual(len(releases), 101)


def http_error(code, headers=None):
    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "error", message, io.BytesIO(b"")
    )


class FakeResponse:
    def __init__(self, body=b"[]"):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RequestFailures(unittest.TestCase):
    """The real request path: timeouts, rate limits, plain failures."""

    def client(self, now=1000.0):
        self.sleeps = []
        return watch.GitHubClient(
            None, sleep=self.sleeps.append, now=lambda: now
        )

    def test_every_call_has_a_timeout(self):
        client = self.client()
        with mock.patch.object(
            watch.urllib.request, "urlopen", return_value=FakeResponse()
        ) as urlopen:
            client.request("GET", "/repos/ghzserg/zmod/releases")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], watch.REQUEST_TIMEOUT)

    def test_short_rate_limit_is_waited_out_and_retried_once(self):
        client = self.client(now=1000.0)
        limited = http_error(
            403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1010"}
        )
        with mock.patch.object(
            watch.urllib.request,
            "urlopen",
            side_effect=[limited, FakeResponse(b"[1]")],
        ) as urlopen:
            self.assertEqual(client.request("GET", "/x"), [1])
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(self.sleeps, [11.0])

    def test_long_rate_limit_fails_the_run_without_waiting(self):
        client = self.client(now=1000.0)
        limited = http_error(
            403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "4600"}
        )
        with mock.patch.object(
            watch.urllib.request, "urlopen", side_effect=[limited]
        ):
            with self.assertRaises(watch.WatchError) as caught:
                client.request("GET", "/x")
        self.assertIn("rate limited", str(caught.exception))
        self.assertEqual(self.sleeps, [])

    def test_rate_limit_twice_fails_the_run(self):
        client = self.client()
        with mock.patch.object(
            watch.urllib.request,
            "urlopen",
            side_effect=[
                http_error(429, {"Retry-After": "5"}),
                http_error(429, {"Retry-After": "5"}),
            ],
        ):
            with self.assertRaises(watch.WatchError):
                client.request("GET", "/x")
        self.assertEqual(self.sleeps, [6.0])

    def test_plain_forbidden_is_not_retried(self):
        client = self.client()
        with mock.patch.object(
            watch.urllib.request, "urlopen", side_effect=[http_error(403)]
        ) as urlopen:
            with self.assertRaises(watch.WatchError) as caught:
                client.request("POST", "/repos/me/repo/issues", {"title": "t"})
        self.assertEqual(urlopen.call_count, 1)
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertEqual(self.sleeps, [])

    def test_timeout_fails_the_run(self):
        client = self.client()
        with mock.patch.object(
            watch.urllib.request, "urlopen", side_effect=TimeoutError("timed out")
        ):
            with self.assertRaises(watch.WatchError):
                client.request("GET", "/x")


RELEASE_177 = {
    "tag_name": "1.7.7",
    "prerelease": False,
    "draft": False,
    "published_at": "2026-12-01T00:00:00Z",
    "html_url": "https://example.invalid/1",
}


class RunBehavior(unittest.TestCase):
    """run() end to end against a fake client: what gets written."""

    def run_watch(self, client, *, dry_run=False):
        with tempfile.TemporaryDirectory() as tmp:
            docs = pathlib.Path(tmp) / "docs"
            docs.mkdir()
            (docs / "ZMOD.md").write_text(ZMOD_DOC)
            return watch.run(pathlib.Path(tmp), "me/repo", dry_run, client)

    def writes(self, client):
        return [c for c in client.calls if c[0] != "GET"]

    def test_upstream_with_no_releases_changes_nothing(self):
        client = FakeClient(
            {
                ("GET", "/repos/ghzserg/zmod/releases"): [],
                ("GET", "/repos/me/repo/issues"): [],
            }
        )
        self.assertEqual(self.run_watch(client), 0)
        self.assertEqual(self.writes(client), [])

    def test_missing_upstream_repo_aborts_before_any_write(self):
        # A 404 on the releases list (request() returns None).
        client = FakeClient({("GET", "/repos/me/repo/issues"): []})
        with self.assertRaises(watch.WatchError):
            self.run_watch(client)
        self.assertEqual(self.writes(client), [])

    def test_a_week_after_a_manual_close_writes_nothing(self):
        client = FakeClient(
            {
                ("GET", "/repos/ghzserg/zmod/releases"): [RELEASE_177],
                ("GET", "/repos/me/repo/issues"): [
                    {"number": 9, "title": "Firmware release: ZMod 1.7.7",
                     "state": "closed"},
                ],
            }
        )
        self.run_watch(client)
        self.assertEqual(self.writes(client), [])

    def test_newer_release_opens_exactly_one_issue(self):
        client = FakeClient(
            {
                ("GET", "/repos/ghzserg/zmod/releases"): [RELEASE_177],
                ("GET", "/repos/me/repo/issues"): [],
                ("GET", "/repos/me/repo/labels/firmware-watch"): {"name": "x"},
                ("POST", "/repos/me/repo/issues"): {"number": 10},
            }
        )
        self.run_watch(client)
        writes = self.writes(client)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][2]["title"], "Firmware release: ZMod 1.7.7")
        self.assertEqual(writes[0][2]["labels"], ["firmware-watch"])

    def test_dry_run_writes_nothing(self):
        client = FakeClient(
            {
                ("GET", "/repos/ghzserg/zmod/releases"): [RELEASE_177],
                ("GET", "/repos/me/repo/issues"): [],
            }
        )
        self.run_watch(client, dry_run=True)
        self.assertEqual(self.writes(client), [])
        self.assertEqual(
            [c for c in client.calls if "/labels/" in c[1]], []
        )


class WorkflowContract(unittest.TestCase):
    def load_workflow(self):
        return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)

    def test_triggers_are_weekly_schedule_and_dispatch(self):
        workflow = self.load_workflow()
        self.assertEqual(sorted(workflow["on"]), ["schedule", "workflow_dispatch"])
        self.assertEqual(workflow["on"]["schedule"], [{"cron": "47 13 * * 2"}])

    def test_permissions_are_issues_write_only(self):
        workflow = self.load_workflow()
        self.assertEqual(
            workflow["permissions"], {"contents": "read", "issues": "write"}
        )

    def test_checkout_does_not_keep_the_token(self):
        job = self.load_workflow()["jobs"]["check-firmware-releases"]
        checkout = [
            step for step in job["steps"]
            if "actions/checkout@" in step.get("uses", "")
        ]
        self.assertEqual(len(checkout), 1)
        self.assertEqual(checkout[0]["with"]["persist-credentials"], "false")

    def test_runs_never_overlap_and_are_bounded(self):
        workflow = self.load_workflow()
        self.assertEqual(workflow["concurrency"]["group"], "firmware-watch")
        self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "false")
        job = workflow["jobs"]["check-firmware-releases"]
        self.assertLessEqual(int(job["timeout-minutes"]), 15)

    def test_workflow_never_pushes_or_uses_other_secrets(self):
        text = WORKFLOW_PATH.read_text()
        self.assertNotIn("git push", text)
        self.assertEqual(re.findall(r"secrets\.(\w+)", text), ["GITHUB_TOKEN"])

    def test_job_runs_the_script_with_the_workflow_token(self):
        workflow = self.load_workflow()
        job = workflow["jobs"]["check-firmware-releases"]
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        runs = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("python3 scripts/check_firmware_releases.py", runs)
        envs = [step.get("env", {}) for step in job["steps"]]
        self.assertTrue(
            any("GITHUB_TOKEN" in env for env in envs),
            "the script step must receive the workflow token",
        )


if __name__ == "__main__":
    unittest.main()
