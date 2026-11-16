"""Tests for the weekly upstream issue watch."""

import importlib.util
import pathlib
import sys
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_upstream_issues.py"
WORKFLOW = ROOT / ".github" / "workflows" / "upstream-issue-watch.yml"
spec = importlib.util.spec_from_file_location("check_upstream_issues", SCRIPT)
watch = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = watch
spec.loader.exec_module(watch)


class FakeClient:
    def __init__(self, upstream=(), existing=()):
        self.upstream = list(upstream)
        self.existing = list(existing)
        self.calls = []

    def list_issues(self, repo, *, since=None, state="open"):
        self.calls.append(("list", repo, since, state))
        if repo == watch.DEFAULT_REPO:
            return self.existing
        return [issue for issue in self.upstream if repo.casefold() in issue.get("html_url", "").casefold()]

    def ensure_label(self, repo, name):
        self.calls.append(("label", repo, name))

    def create_issue(self, repo, title, body, labels):
        self.calls.append(("create", repo, title, body, labels))


def upstream_issue(number=23, title="Add Moonraker integration", body="Use spoolman", **extra):
    return {
        "number": number,
        "title": title,
        "body": body,
        "html_url": f"https://github.com/DrA1ex/ff5m/issues/{number}",
        **extra,
    }


class UpstreamIssueWatch(unittest.TestCase):
    def repository(self):
        return {"name": "Forge-X", "repo": "DrA1ex/ff5m"}

    def run_watch(self, upstream, existing=(), *, dry_run=False):
        client = FakeClient(upstream, existing)
        actions = watch.run(ROOT, client, dry_run=dry_run)
        return actions, client

    def test_matching_issue_creates_exactly_one_tracking_issue(self):
        actions, client = self.run_watch([upstream_issue()])
        self.assertEqual(len(actions), 1)
        create_calls = [call for call in client.calls if call[0] == "create"]
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(create_calls[0][2], "Upstream issue: Forge-X #23 Add Moonraker integration")
        self.assertEqual(create_calls[0][4], [watch.LABEL])
        self.assertIn("spoolman", create_calls[0][3])
        self.assertIn("notify-only", create_calls[0][3])
        self.assertIn("upstream-issue-watch", [call[2] for call in client.calls if call[0] == "label"])

    def test_existing_open_tracking_issue_dedupes(self):
        existing = [{"title": "Upstream issue: Forge-X #23 Add Moonraker integration", "state": "open"}]
        actions, client = self.run_watch([upstream_issue()], existing)
        self.assertEqual(actions, [])
        self.assertFalse(any(call[0] == "create" for call in client.calls))

    def test_existing_closed_tracking_issue_stays_closed(self):
        existing = [{"title": "Upstream issue: Forge-X #23 Add Moonraker integration", "state": "closed"}]
        actions, client = self.run_watch([upstream_issue()], existing)
        self.assertEqual(actions, [])
        self.assertFalse(any(call[0] == "create" for call in client.calls))

    def test_nonmatching_issue_and_pull_request_are_ignored(self):
        unrelated = upstream_issue(title="Fix typo", body="Documentation only")
        pull_request = upstream_issue(number=24, title="Moonraker API support", pull_request={"url": "..."})
        actions, client = self.run_watch([unrelated, pull_request])
        self.assertEqual(actions, [])
        self.assertFalse(any(call[0] == "create" for call in client.calls))

    def test_toml_lists_both_repositories(self):
        repositories, keywords = watch.read_config(ROOT)
        self.assertEqual({(repo["name"], repo["repo"]) for repo in repositories}, {
            ("Forge-X", "DrA1ex/ff5m"),
            ("OpenCreator", "FlashForge-C5-Modding-Group/OpenCreator"),
        })
        self.assertIn("moonraker", keywords)

    def test_dedup_is_by_title_across_same_issue_returned_for_both_repos(self):
        issue = upstream_issue()
        issue["html_url"] = "https://github.com/DrA1ex/ff5m/issues/23"
        self.assertEqual(
            watch.plan_actions([(self.repository(), issue), (self.repository(), issue)], [], ["moonraker"]),
            [
                {
                    "title": "Upstream issue: Forge-X #23 Add Moonraker integration",
                    "url": issue["html_url"],
                    "keywords": ["moonraker"],
                }
            ],
        )

    def test_workflow_has_weekly_schedule_dispatch_and_write_permission(self):
        document = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(document["on"]["schedule"][0]["cron"], "17 14 * * 3")
        self.assertIn("workflow_dispatch", document["on"])
        self.assertEqual(document["permissions"]["issues"], "write")

    def test_dry_run_never_writes_and_uses_recent_window(self):
        actions, client = self.run_watch([upstream_issue()], dry_run=True)
        self.assertEqual(len(actions), 1)
        self.assertTrue(client.calls[0][2].endswith("Z"))
        self.assertFalse(any(call[0] in {"create", "label"} for call in client.calls))


if __name__ == "__main__":
    unittest.main()
