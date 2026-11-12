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
        self.assertEqual(parsed[0].name, "ZMod")
        self.assertEqual(parsed[0].repo, "ghzserg/zmod")
        self.assertEqual(parsed[0].baseline, "1.7.6")
        self.assertEqual(parsed[0].feed, "releases")


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


KLIPPER_ONLY = """
[[firmware]]
name = "Klipper"
repo = "Klipper3d/klipper"
feed = "tags"
baseline = "0.13.0"
basis = "test basis"
"""


def run_in_tree(client, *, recorded, dry_run=False, zmod_doc=None):
    with tempfile.TemporaryDirectory() as tmp:
        docs = pathlib.Path(tmp) / "docs"
        docs.mkdir()
        (docs / "ZMOD.md").write_text(zmod_doc or ZMOD_DOC, encoding="utf-8")
        (docs / "firmware-baselines.toml").write_text(recorded, encoding="utf-8")
        return watch.run(pathlib.Path(tmp), "me/repo", dry_run, client)


RELEASE_177 = {
    "tag_name": "1.7.7",
    "prerelease": False,
    "draft": False,
    "published_at": "2026-12-01T00:00:00Z",
    "html_url": "https://example.invalid/1",
}


class RunBehavior(unittest.TestCase):
    """run() end to end against a fake client: what gets written."""

    def run_watch(self, client, *, dry_run=False, recorded=KLIPPER_ONLY):
        # The recorded-baselines file is required; these ZMod-focused
        # runs carry one Klipper entry whose tags feed is quiet.
        client.responses.setdefault(("GET", "/repos/Klipper3d/klipper/tags"), [])
        return run_in_tree(client, recorded=recorded, dry_run=dry_run)

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


# ---------------------------------------------------------------------------
# Issue #1280 sections F to H: recorded baselines, tags, printer lines,
# per-model repositories.


def tag(name):
    return {"name": name, "commit": {"sha": "0" * 40}}


def gh_release(tag_name, *, title="", prerelease=False, draft=False,
               published="2026-11-03T00:00:00Z"):
    return {
        "tag_name": tag_name,
        "name": title,
        "prerelease": prerelease,
        "draft": draft,
        "published_at": published,
        "html_url": f"https://example.invalid/releases/{tag_name}",
    }


def recorded(*entries):
    return "\n".join(
        "[[firmware]]\n" + "".join(
            f"{key} = {value!r}\n".replace("'", '"') if isinstance(value, str)
            else f"{key} = {list(value)!r}\n".replace("'", '"')
            for key, value in entry.items()
        )
        for entry in entries
    )


def entry(**fields):
    base = {"name": "OctoPrint", "repo": "OctoPrint/OctoPrint",
            "feed": "releases", "baseline": "1.11.8", "basis": "test"}
    base.update(fields)
    return base


class TagVersions(unittest.TestCase):
    def test_every_watched_tag_shape_yields_its_version(self):
        cases = {
            "1.7.6": "1.7.6",
            "1.7.5-2": "1.7.5-2",
            "v0.13.0": "0.13.0",
            "v0.11.0": "0.11.0",
            "2.1.2.9": "2.1.2.9",
            "2.0.0rc7": "2.0.0rc7",
            "v8.0.1": "8.0.1",
            "v6.4.1-RC": "6.4.1-RC",
            "Q1_Pro_V4.4.24": "4.4.24",
            "V4.4.18": "4.4.18",
            "Plus4_v1.7.1": "1.7.1",
            "Max3_V4.3.15": "4.3.15",
            "Smart_V4.1.13": "4.1.13",
            "Q2_V1.1.1": "1.1.1",
        }
        for tag_name, expected in cases.items():
            self.assertEqual(watch.version_from_tag(tag_name), expected, tag_name)

    def test_a_tag_with_no_version_is_skipped_not_ranked_as_zero(self):
        # Arksine/moonraker carries a stray "v.08" tag.
        for tag_name in ("v.08", "R", "latest", "nightly"):
            self.assertIsNone(watch.version_from_tag(tag_name), tag_name)

    def test_lettered_suffixes_are_prereleases_numeric_builds_are_not(self):
        for version in ("2.0.0rc7", "2.1.3-b3", "6.4.1-RC", "0.14.0-rc1", "1.8.0-beta"):
            self.assertTrue(watch.is_prerelease_version(version), version)
        for version in ("1.7.5-2", "0.13.0", "2.1.2.9", "8.0.1"):
            self.assertFalse(watch.is_prerelease_version(version), version)

    def test_glued_release_candidate_ranks_below_its_final(self):
        # OctoPrint's 2.0.0rc7: above 1.11.8, below 2.0.0.
        key = watch.firmware_version_key
        self.assertLess(key("1.11.8"), key("2.0.0rc7"))
        self.assertLess(key("2.0.0rc6"), key("2.0.0rc7"))
        self.assertLess(key("2.0.0rc7"), key("2.0.0"))


class RecordedBaselines(unittest.TestCase):
    def test_entries_parse(self):
        parsed = watch.parse_recorded_baselines(recorded(
            entry(),
            entry(name="Klipper", repo="Klipper3d/klipper", feed="tags",
                  baseline="v0.13.0"),
        ))
        self.assertEqual([(w.name, w.feed, w.baseline) for w in parsed],
                         [("OctoPrint", "releases", "1.11.8"),
                          ("Klipper", "tags", "0.13.0")])
        self.assertEqual(parsed[0].baseline_home, "docs/firmware-baselines.toml")
        self.assertEqual(parsed[0].baseline_source, "test")

    def test_a_releases_feed_is_never_pointed_at_klipper_or_moonraker(self):
        # The failure mode #1275 exists to prevent: Klipper has no
        # releases and Moonraker's stopped in 2021, so a releases check
        # would report "nothing newer" forever. It must fail, loudly.
        for repo in ("Klipper3d/klipper", "Arksine/moonraker"):
            with self.subTest(repo=repo):
                with self.assertRaises(watch.WatchError) as caught:
                    watch.parse_recorded_baselines(recorded(
                        entry(name="X", repo=repo, feed="releases", baseline="0.13.0")
                    ))
                self.assertIn("tags", str(caught.exception))

    def test_the_client_refuses_a_releases_read_of_a_tags_only_repo(self):
        # Defense in depth: even a caller that bypasses the baseline
        # file cannot read those repositories by releases.
        for repo in sorted(watch.TAG_ONLY_REPOS):
            client = FakeClient()
            with self.assertRaises(watch.WatchError):
                client.list_releases(repo)
            self.assertEqual(client.calls, [])

    def test_malformed_entries_fail_closed(self):
        bad = [
            recorded(entry(feed="rss")),
            recorded(entry(baseline="latest")),
            recorded(entry(basis="")),
            recorded(entry(colour="red")),
            recorded(entry(feed="tags", repo="Klipper3d/klipper", line=["MK4S"])),
            recorded(entry(group="Prusa Buddy")),
            "[[firmware]]\nname = \"x\"\n",
            "not toml = = =",
            "# nothing recorded\n",
        ]
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaises(watch.WatchError):
                    watch.parse_recorded_baselines(text)

    def test_names_and_groups_are_unambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "ZMOD.md").write_text(ZMOD_DOC, encoding="utf-8")
            cases = [
                recorded(entry(), entry()),
                recorded(entry(name="ZMod")),
                recorded(entry(), entry(name="A", line=["XL"], group="OctoPrint")),
                recorded(entry(name="A", line=["XL"], group="G"),
                         entry(name="B", line=["MINI"], group="G", repo="other/repo")),
            ]
            for text in cases:
                with self.subTest(text=text):
                    (root / "docs" / "firmware-baselines.toml").write_text(text, encoding="utf-8")
                    with self.assertRaises(watch.WatchError):
                        watch.read_baselines(root)

    def test_a_missing_baselines_file_fails_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "ZMOD.md").write_text(ZMOD_DOC, encoding="utf-8")
            with self.assertRaises(watch.WatchError):
                watch.read_baselines(root)


class ShippedBaselines(unittest.TestCase):
    """docs/firmware-baselines.toml as shipped: every family #1280 F to H
    names, on the feed its evidence requires."""

    @classmethod
    def setUpClass(cls):
        cls.watched = {w.name: w for w in watch.read_baselines(ROOT)}

    def test_klipper_and_moonraker_are_read_by_tags(self):
        self.assertEqual(self.watched["Klipper"].repo, "Klipper3d/klipper")
        self.assertEqual(self.watched["Moonraker"].repo, "Arksine/moonraker")
        for name in ("Klipper", "Moonraker"):
            self.assertEqual(self.watched[name].feed, "tags", name)
        for entry_ in self.watched.values():
            if entry_.repo in watch.TAG_ONLY_REPOS:
                self.assertEqual(entry_.feed, "tags", entry_.name)

    def test_recorded_versions_and_their_evidence(self):
        expected = {
            "Klipper": "0.13.0", "Moonraker": "0.11.0",
            "OctoPrint": "1.11.8", "Marlin": "2.1.2.9",
            "Prusa Buddy (MK4S)": "6.5.7", "Prusa Buddy (CORE One)": "6.8.1",
            "Prusa Buddy (XL)": "6.10.3", "Prusa Buddy (MINI+)": "6.4.2",
            "QIDI Q1 Pro": "4.4.24", "QIDI X-Plus 3": "4.2.15",
            "QIDI X-Plus 4": "1.7.1", "QIDI X-Max 3": "4.3.15",
            "QIDI X-Smart 3": "4.1.13",
        }
        self.assertEqual(
            {name: w.baseline for name, w in self.watched.items() if name != "ZMod"},
            expected,
        )
        for name, entry_ in self.watched.items():
            if name != "ZMod":
                # Every recorded baseline names its evidence (an issue
                # or PR number), which each notice repeats.
                self.assertRegex(entry_.baseline_source, r"#\d+", name)

    def test_octoprint_and_marlin_use_their_releases(self):
        self.assertEqual(self.watched["OctoPrint"].repo, "OctoPrint/OctoPrint")
        self.assertEqual(self.watched["Marlin"].repo, "MarlinFirmware/Marlin")
        for name in ("OctoPrint", "Marlin"):
            self.assertEqual(self.watched[name].feed, "releases")

    def test_prusa_buddy_is_watched_per_supported_printer_line(self):
        lines = {w.line: w for w in self.watched.values()
                 if w.repo == "prusa3d/Prusa-Firmware-Buddy"}
        self.assertEqual(
            set(lines), {("MK4S",), ("CORE One",), ("XL",), ("MINI+", "MINI")}
        )
        self.assertEqual({w.group for w in lines.values()}, {"Prusa Buddy"})
        self.assertEqual({w.feed for w in lines.values()}, {"releases"})

    def test_qidi_is_watched_per_model_repository_for_every_supported_model(self):
        from print_handy import printers

        repos = {w.repo for w in self.watched.values() if w.repo.startswith("QIDITECH/")}
        supported = {key for key in printers.MODELS if key.startswith("qidi.")}
        model_repo = {
            "qidi.q1pro": "QIDITECH/QIDI_Q1_Pro",
            "qidi.xplus3": "QIDITECH/QIDI_PLUS3",
            "qidi.xplus4": "QIDITECH/QIDI_PLUS4",
            "qidi.xmax3": "QIDITECH/QIDI_MAX3",
            "qidi.xsmart3": "QIDITECH/QIDI_SMART3",
        }
        self.assertEqual(supported, set(model_repo))
        self.assertEqual(repos, set(model_repo.values()))


class TagFeed(unittest.TestCase):
    def test_tags_parse_into_final_and_prerelease_versions(self):
        client = FakeClient({("GET", "/repos/Arksine/moonraker/tags"): [
            tag("v.08"), tag("v0.11.0"), tag("v0.10.0"), tag("v0.12.0-rc1"),
        ]})
        tags = client.list_tags("Arksine/moonraker")
        self.assertEqual(
            [(t.version, t.prerelease) for t in tags],
            [("0.11.0", False), ("0.10.0", False), ("0.12.0-rc1", True)],
        )
        self.assertIsNone(tags[0].published_at)
        self.assertEqual(tags[0].url, "https://github.com/Arksine/moonraker/tree/v0.11.0")
        self.assertEqual(watch.select_latest_final(tags).version, "0.11.0")

    def test_a_tag_feed_with_no_version_tag_fails_loudly(self):
        client = FakeClient({("GET", "/repos/Klipper3d/klipper/tags"): [tag("nightly")]})
        with self.assertRaises(watch.WatchError):
            client.list_tags("Klipper3d/klipper")

    def test_tags_paginate(self):
        page_one = [tag(f"v0.{n}.0") for n in range(100)]
        client = FakeClient(pages={("GET", "/repos/Klipper3d/klipper/tags"): {
            1: page_one, 2: [tag("v0.200.0")]}})
        tags = client.list_tags("Klipper3d/klipper")
        self.assertEqual(len(tags), 101)
        self.assertEqual(watch.select_latest_final(tags).version, "0.200.0")

    def test_releases_read_qidi_tag_shapes_and_unflagged_betas(self):
        client = FakeClient({("GET", "/repos/QIDITECH/QIDI_Q1_Pro/releases"): [
            gh_release("Q1_Pro_V4.4.24"), gh_release("V4.4.18"),
        ], ("GET", "/repos/MarlinFirmware/Marlin/releases"): [
            gh_release("2.1.2.9"), gh_release("2.1.3-b3"),
        ]})
        self.assertEqual(
            [r.version for r in client.list_releases("QIDITECH/QIDI_Q1_Pro")],
            ["4.4.24", "4.4.18"],
        )
        # Marlin publishes its 2.1.3-b3 beta as a full release; the
        # version decides, so it opens no notice.
        marlin = client.list_releases("MarlinFirmware/Marlin")
        self.assertEqual(watch.select_latest_final(marlin).version, "2.1.2.9")

    def test_a_releases_feed_with_no_readable_tag_fails_loudly(self):
        client = FakeClient({("GET", "/repos/QIDITECH/QIDI_Q2/releases"): [
            gh_release("final-release"),
        ]})
        with self.assertRaises(watch.WatchError):
            client.list_releases("QIDITECH/QIDI_Q2")


PRUSA_TITLES = {
    "v8.0.1": "8.0.1 Firmware for Prusa XL+, XL, MINI+, MINI",
    "v8.0.0": ("8.0.0 Firmware for Prusa CORE One L+, CORE One L, CORE One+ (Gen 2) "
               "INDX, CORE One INDX, CORE One+ (Gen 2), CORE One, XL+, XL, MK4S, MK4, "
               "MK3.9S, MK3.9, MK3.5S, MK3.5, MINI+, MINI"),
    "v7.0.1": "7.0.1 Firmware for Prusa CORE One+ (Gen 2) INDX, CORE One INDX",
    "v6.10.3": "6.10.3 Firmware for Prusa XL+, XL ",
    "v6.8.1": "6.8.1 Firmware for Prusa CORE One L+, CORE One L, CORE One+ (Gen 2), CORE One",
    "v6.5.7": ("6.5.7 Firmware for Prusa CORE One L, CORE One+, CORE One, MK4S, MK4, "
               "MK3.9S, MK3.9, MK3.5S, MK3.5"),
    "v6.4.2": "6.4.2 Firmware for Prusa XL & MINI",
    "v6.2.4": ("6.2.4 Firmware for Original Prusa MK4, MK4S, MK3.9, MK3.9S, MK3.5, "
               "MK3.5S, MINI & XL"),
    "v6.9.1-beta": "6.9.1-beta Firmware for Prusa CORE One+ (Gen 2) INDX, CORE One/+ INDX",
}


def prusa_feed(tags=None):
    return [
        gh_release(t, title=PRUSA_TITLES[t], prerelease="beta" in t)
        for t in (tags or PRUSA_TITLES)
    ]


def prusa_line(name, line, baseline):
    return watch.WatchedFirmware(
        name=f"Prusa Buddy ({name})", repo="prusa3d/Prusa-Firmware-Buddy",
        baseline=baseline, baseline_source="test", feed="releases",
        line=tuple(line), baseline_home="docs/firmware-baselines.toml",
        group="Prusa Buddy",
    )


PRUSA_LINES = [
    ("MK4S", ["MK4S"], "6.5.7"),
    ("CORE One", ["CORE One"], "6.8.1"),
    ("XL", ["XL"], "6.10.3"),
    ("MINI+", ["MINI+", "MINI"], "6.4.2"),
]


def prusa_findings(baselines=None, tags=None):
    client = FakeClient({("GET", "/repos/prusa3d/Prusa-Firmware-Buddy/releases"):
                         prusa_feed(tags)})
    feed = client.list_releases("prusa3d/Prusa-Firmware-Buddy")
    watched = [prusa_line(n, l, (baselines or {}).get(n, b)) for n, l, b in PRUSA_LINES]
    return watch.build_findings(watched, {w.name: feed for w in watched})


class PrusaLines(unittest.TestCase):
    def test_release_titles_list_their_printers(self):
        self.assertEqual(
            watch.release_printers(PRUSA_TITLES["v8.0.1"]), {"XL+", "XL", "MINI+", "MINI"}
        )
        self.assertEqual(
            watch.release_printers(PRUSA_TITLES["v6.2.4"]),
            {"MK4", "MK4S", "MK3.9", "MK3.9S", "MK3.5", "MK3.5S", "MINI", "XL"},
        )
        self.assertIn("CORE One+", watch.release_printers("6.4.0 Firmware for Prusa CORE One +, CORE One"))
        self.assertEqual(watch.release_printers("Some other title"), set())

    def test_printer_names_match_exactly(self):
        core_one = ("CORE One",)
        indx_only = watch.Release("7.0.1", "v7.0.1", False, None, "u", PRUSA_TITLES["v7.0.1"])
        self.assertFalse(watch.in_line(indx_only, core_one))
        both = watch.Release("6.8.1", "v6.8.1", False, None, "u", PRUSA_TITLES["v6.8.1"])
        self.assertTrue(watch.in_line(both, core_one))
        self.assertFalse(watch.in_line(both, ("XL",)))

    def test_each_line_sees_its_own_newest_release(self):
        findings = {f.firmware: f for f in prusa_findings()}
        self.assertEqual(
            {name: f.latest_release.version for name, f in findings.items()},
            {"Prusa Buddy (MK4S)": "8.0.0", "Prusa Buddy (CORE One)": "8.0.0",
             "Prusa Buddy (XL)": "8.0.1", "Prusa Buddy (MINI+)": "8.0.1"},
        )
        # 7.0.1 (CORE One INDX only) is never the CORE One's newest.
        self.assertFalse(findings["Prusa Buddy (CORE One)"].covers("7.0.1"))

    def test_lines_sharing_a_release_get_one_notice(self):
        actions = watch.plan_actions(prusa_findings(), [])
        self.assertEqual(
            [(a.kind, a.firmware, a.version) for a in actions],
            [("create", "Prusa Buddy", "8.0.0"), ("create", "Prusa Buddy", "8.0.1")],
        )
        self.assertEqual(watch.issue_title("Prusa Buddy", "8.0.0"),
                         "Firmware release: Prusa Buddy 8.0.0")

    def test_a_closed_notice_is_not_reopened_for_any_line(self):
        closed = [watch.WatchIssue(5, "Prusa Buddy", "8.0.0", open=False),
                  watch.WatchIssue(6, "Prusa Buddy", "8.0.1", open=False)]
        self.assertEqual(watch.plan_actions(prusa_findings(), closed), [])

    def test_a_notice_closes_only_when_every_covered_line_caught_up(self):
        notice = [watch.WatchIssue(5, "Prusa Buddy", "8.0.1")]
        # 8.0.1 covers XL and MINI+; MK4S and CORE One never got it.
        partly = prusa_findings({"XL": "8.0.1"})
        self.assertNotIn("close", [a.kind for a in watch.plan_actions(partly, notice)])
        caught_up = prusa_findings({"XL": "8.0.1", "MINI+": "8.0.1"})
        closes = [a for a in watch.plan_actions(caught_up, notice) if a.kind == "close"]
        self.assertEqual([(a.issue_number, a.version) for a in closes], [(5, "8.0.1")])

    def test_notice_body_lists_each_covered_line_with_its_baseline(self):
        findings = prusa_findings()
        release = next(f.newer_release for f in findings if f.firmware == "Prusa Buddy (XL)")
        body = watch.issue_body(findings[2], release, findings)
        self.assertIn("Prusa Buddy (XL): 6.10.3", body)
        self.assertIn("Prusa Buddy (MINI+): 6.4.2", body)
        self.assertNotIn("Prusa Buddy (MK4S)", body)
        self.assertIn("docs/firmware-baselines.toml", body)
        self.assertIn("not an audit", body)

    def test_a_line_no_release_title_names_fails_loudly(self):
        client = FakeClient({("GET", "/repos/prusa3d/Prusa-Firmware-Buddy/releases"):
                             [gh_release("v9.0.0", title="9.0.0 for the XL")]})
        feed = client.list_releases("prusa3d/Prusa-Firmware-Buddy")
        watched = [prusa_line("XL", ["XL"], "6.10.3")]
        with self.assertRaises(watch.WatchError):
            watch.build_findings(watched, {watched[0].name: feed})


class RecordedRunBehavior(unittest.TestCase):
    """run() end to end with recorded baselines: tags, lines, models."""

    def writes(self, client):
        return [c for c in client.calls if c[0] != "GET"]

    def base_responses(self):
        return {
            ("GET", "/repos/ghzserg/zmod/releases"): [],
            ("GET", "/repos/me/repo/issues"): [],
            ("GET", "/repos/me/repo/labels/firmware-watch"): {"name": "x"},
            ("POST", "/repos/me/repo/issues"): {"number": 11},
        }

    def test_a_newer_klipper_tag_opens_one_notice(self):
        responses = self.base_responses()
        responses[("GET", "/repos/Klipper3d/klipper/tags")] = [
            tag("v0.13.1"), tag("v0.13.0"), tag("v0.14.0-rc1"),
        ]
        client = FakeClient(responses)
        run_in_tree(client, recorded=KLIPPER_ONLY)
        writes = self.writes(client)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][2]["title"], "Firmware release: Klipper 0.13.1")
        self.assertIn("tagged", writes[0][2]["body"])
        self.assertIn("docs/firmware-baselines.toml", writes[0][2]["body"])
        # Klipper was read by tags, never by releases.
        self.assertNotIn("/repos/Klipper3d/klipper/releases",
                         " ".join(c[1] for c in client.calls))

    def test_a_manually_closed_klipper_notice_stays_closed(self):
        responses = self.base_responses()
        responses[("GET", "/repos/Klipper3d/klipper/tags")] = [tag("v0.13.1")]
        responses[("GET", "/repos/me/repo/issues")] = [
            {"number": 4, "title": "Firmware release: Klipper 0.13.1", "state": "closed"},
        ]
        client = FakeClient(responses)
        run_in_tree(client, recorded=KLIPPER_ONLY)
        self.assertEqual(self.writes(client), [])

    def test_the_klipper_notice_closes_when_the_baseline_catches_up(self):
        responses = self.base_responses()
        responses[("GET", "/repos/Klipper3d/klipper/tags")] = [tag("v0.13.1")]
        responses[("GET", "/repos/me/repo/issues")] = [
            {"number": 4, "title": "Firmware release: Klipper 0.13.1", "state": "open"},
        ]
        client = FakeClient(responses)
        run_in_tree(client, recorded=KLIPPER_ONLY.replace('"0.13.0"', '"0.13.1"'))
        self.assertEqual([(c[0], c[1]) for c in self.writes(client)], [
            ("POST", "/repos/me/repo/issues/4/comments"),
            ("PATCH", "/repos/me/repo/issues/4"),
        ])

    def test_prusa_lines_share_one_fetch_and_open_one_notice_per_release(self):
        responses = self.base_responses()
        responses[("GET", "/repos/prusa3d/Prusa-Firmware-Buddy/releases")] = prusa_feed()
        lines = recorded(*[
            {"name": f"Prusa Buddy ({n})", "group": "Prusa Buddy",
             "repo": "prusa3d/Prusa-Firmware-Buddy", "feed": "releases",
             "line": l, "baseline": b, "basis": "test"}
            for n, l, b in PRUSA_LINES
        ])
        client = FakeClient(responses)
        run_in_tree(client, recorded=lines)
        fetches = [c for c in client.calls
                   if c[1].startswith("/repos/prusa3d/Prusa-Firmware-Buddy/releases")]
        self.assertEqual(len(fetches), 1)
        self.assertEqual(
            [w[2]["title"] for w in self.writes(client)],
            ["Firmware release: Prusa Buddy 8.0.0", "Firmware release: Prusa Buddy 8.0.1"],
        )

    def test_quiet_week_for_every_recorded_family_writes_nothing(self):
        # The shipped baselines against feeds whose newest final entry
        # is the baseline itself: nothing to report.
        responses = self.base_responses()
        responses[("GET", "/repos/Klipper3d/klipper/tags")] = [tag("v0.13.0")]
        responses[("GET", "/repos/Arksine/moonraker/tags")] = [tag("v0.11.0"), tag("v.08")]
        responses[("GET", "/repos/OctoPrint/OctoPrint/releases")] = [
            gh_release("1.11.8"), gh_release("2.0.0rc7", prerelease=True)]
        responses[("GET", "/repos/MarlinFirmware/Marlin/releases")] = [
            gh_release("2.1.2.9"), gh_release("2.1.3-b3")]
        responses[("GET", "/repos/prusa3d/Prusa-Firmware-Buddy/releases")] = prusa_feed(
            ["v6.10.3", "v6.8.1", "v6.5.7", "v6.4.2"])
        for repo, tag_name in (("QIDI_Q1_Pro", "Q1_Pro_V4.4.24"),
                               ("QIDI_PLUS3", "Plus3_V4.2.15"),
                               ("QIDI_PLUS4", "Plus4_v1.7.1"),
                               ("QIDI_MAX3", "Max3_V4.3.15"),
                               ("QIDI_SMART3", "Smart_V4.1.13")):
            responses[("GET", f"/repos/QIDITECH/{repo}/releases")] = [gh_release(tag_name)]
        client = FakeClient(responses)
        shipped = (ROOT / "docs" / "firmware-baselines.toml").read_text(encoding="utf-8")
        self.assertEqual(run_in_tree(client, recorded=shipped), 0)
        self.assertEqual(self.writes(client), [])


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
