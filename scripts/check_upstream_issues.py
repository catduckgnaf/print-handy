#!/usr/bin/env python3
"""Weekly, notify-only watch of relevant upstream issue trackers.

Reads the watched repositories and case-insensitive title/body keywords
from ``docs/upstream-issue-watch.toml``. Matching issues in the recent
activity window open one deduplicated tracking issue in this repository;
issues closed by a maintainer remain closed. Upstream repositories are
read-only: this script never comments, reacts, or writes there.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_BASE = "https://api.github.com"
DEFAULT_REPO = "catduckgnaf/print-handy"
CONFIG_FILE = "docs/upstream-issue-watch.toml"
LABEL = "upstream-issue-watch"
SINCE_DAYS = 8
REQUEST_TIMEOUT = 30


class WatchError(Exception):
    """A failure that should stop the watch without partial assumptions."""


def read_config(root: Path) -> tuple[list[dict[str, str]], list[str]]:
    try:
        config = tomllib.loads((root / CONFIG_FILE).read_text())
        repositories = config["repositories"]
        keywords = config["keywords"]["terms"]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise WatchError(f"Cannot read {CONFIG_FILE}: {exc}") from exc
    if not repositories or not keywords or any(not term.strip() for term in keywords):
        raise WatchError(f"{CONFIG_FILE} must list repositories and nonempty keywords")
    return repositories, keywords


def issue_title(name: str, number: int, title: str) -> str:
    return f"Upstream issue: {name} #{number} {title}"


def match_keywords(issue: dict, keywords: list[str]) -> list[str]:
    """Keywords found in the issue as whole words or phrases, case-insensitive.

    Word boundaries keep a short term such as ``api`` from matching inside
    another word (``rapid``).
    """
    text = f"{issue.get('title', '')}\n{issue.get('body') or ''}"
    return [term for term in keywords
            if re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", text, re.IGNORECASE)]


_TRACKED = re.compile(r"^Upstream issue: (?P<name>.+?) #(?P<number>\d+)(?: |$)")


def tracking_key(issue: dict) -> tuple[str, int] | None:
    """(watched name, upstream number) of a tracking issue here, or None.

    The key ignores the upstream title, so a renamed upstream issue does
    not open a second tracking issue.
    """
    match = _TRACKED.match(issue.get("title", ""))
    return (match["name"], int(match["number"])) if match else None


def plan_actions(upstream: list[tuple[dict, dict]], existing: list[dict], keywords: list[str]) -> list[dict]:
    tracked = {key for key in (tracking_key(issue) for issue in existing) if key}
    actions = []
    for repo, issue in upstream:
        if issue.get("pull_request"):
            continue
        matched = match_keywords(issue, keywords)
        key = (repo["name"], int(issue["number"]))
        if matched and key not in tracked:
            title = issue_title(repo["name"], issue["number"], issue["title"])
            actions.append({"title": title, "url": issue["html_url"], "keywords": matched})
            tracked.add(key)
    return actions


def issue_body(action: dict) -> str:
    words = ", ".join(f"`{word}`" for word in action["keywords"])
    return (
        f"Relevant upstream issue: {action['url']}\n\n"
        f"Matched keywords: {words}.\n\n"
        "This is a notify-only tracker. Please review the upstream issue as needed; "
        "the watch does not comment on, react to, or write to upstream repositories."
    )


class GitHubClient:
    """Small GitHub REST client; upstream calls are GET-only."""

    def __init__(self, token: str | None):
        self.token = token

    def request(self, method: str, path: str, payload: dict | None = None):
        url = path if path.startswith("https://") else f"{API_BASE}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "print-handy-upstream-issue-watch")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise WatchError(f"GitHub API {method} {url} failed: HTTP {exc.code}") from exc
        except OSError as exc:
            raise WatchError(f"GitHub API {method} {url} failed: {exc}") from exc

    def list_issues(self, repo: str, *, since: str | None = None, state: str = "open") -> list[dict]:
        items = []
        page = 1
        while True:
            query = f"?state={state}&per_page=100&page={page}"
            if since:
                query += f"&since={since}"
            batch = self.request("GET", f"/repos/{repo}/issues{query}")
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

    def ensure_label(self, repo: str, name: str) -> None:
        labels = self.request("GET", f"/repos/{repo}/labels?per_page=100")
        if any(label["name"].casefold() == name.casefold() for label in labels):
            return
        self.request("POST", f"/repos/{repo}/labels", {"name": name, "color": "1d76db"})

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> dict:
        return self.request("POST", f"/repos/{repo}/issues", {"title": title, "body": body, "labels": labels})


def run(root: Path, client: GitHubClient, *, dry_run: bool = False, now: datetime | None = None,
        repo: str = DEFAULT_REPO, all_open: bool = False) -> list[dict]:
    """Plan (and unless dry_run, open) tracking issues.

    By default only upstream issues updated in the last SINCE_DAYS days are
    read. ``all_open`` reads every open upstream issue instead, for a first
    run or a catch-up after a missed week.
    """
    repositories, keywords = read_config(root)
    if all_open:
        since = None
    else:
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=SINCE_DAYS)
        since = cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")
    upstream = []
    for watched in repositories:
        for issue in client.list_issues(watched["repo"], since=since):
            if not issue.get("pull_request"):
                upstream.append((watched, issue))
    existing = client.list_issues(repo, state="all")
    actions = plan_actions(upstream, existing, keywords)
    if dry_run:
        for action in actions:
            print(f"Would create: {action['title']}")
        return actions
    if actions:
        client.ensure_label(repo, LABEL)
        for action in actions:
            client.create_issue(repo, action["title"], issue_body(action), [LABEL])
    return actions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="show planned issues without writing")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="tracking repository")
    parser.add_argument("--all-open", action="store_true",
                        help="read every open upstream issue, not only the last week's activity")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    token = os.environ.get("GITHUB_TOKEN")
    if not token and not args.dry_run:
        print("GITHUB_TOKEN is required to create tracking issues", file=sys.stderr)
        return 2
    try:
        actions = run(root, GitHubClient(token), dry_run=args.dry_run, repo=args.repo,
                      all_open=args.all_open)
    except WatchError as exc:
        print(f"upstream-issue-watch: {exc}", file=sys.stderr)
        return 1
    print(f"Upstream issue watch: {len(actions)} issue(s) {'would be created' if args.dry_run else 'created'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
