#!/usr/bin/env python3
"""Weekly firmware release watch for printer firmware Print Handy audits.

The upstream watch (``scripts/check_upstream_releases.py``) covers the
tools Print Handy pins and bundles. Printer firmware is different: it
is not a dependency, there is no pin to compare against, and a release
matters because it can move a contract Print Handy relies on. What
Print Handy compares against instead is the **audited baseline** — the
firmware version this repository last audited feature by feature —
recorded in the docs. That difference is why this is a separate watch
(issue #1271), not another component in the upstream one.

Watched today:

- ZMod (``ghzserg/zmod``): baseline = the audited-against version in
  ``docs/ZMOD.md``. The baseline moves only when an audit lands (the
  pattern of #562 for 1.7.5 and #1269 for 1.7.6), never automatically.

Other supported families were evaluated in #1271: OctoPrint, Marlin,
Prusa Buddy and QIDI publish GitHub releases but have no audited
baseline recorded in this repository yet, Klipper and Moonraker
version by tags rather than GitHub releases, and the remaining vendor
firmware (stock Flashforge, Bambu Lab, Sovol, Elegoo, Creality,
Anycubic) has no reliable machine-readable feed. Each watchable family
joins this watch only when its baseline concept is recorded; until
then it is named in #1271, not scraped.

The script reads the baselines, queries each watched firmware's
GitHub releases, and reports through issues in this repository,
deduplicated by the ``firmware-watch`` label and titles of the form
``Firmware release: <Firmware> <version>``:

- a newer-than-baseline release with no matching open issue opens
  one, naming the firmware, the version and the release link, and
  pointing at the audit pattern as the follow-up;
- an open issue whose version the baseline has caught up with is
  closed with a short comment;
- when nothing is newer, nothing is created or changed — a quiet
  week is a successful no-op.

Notification only: the script never edits the baseline, never opens a
bump PR, never downloads firmware and never contacts a printer. A
notice is not an audit: it says a release exists, nothing about what
the release changes for Print Handy.

Only final releases count. Firmware audits follow shipped releases;
a prerelease upstream opens no issue here.

All upstream data is fetched before any issue is touched, so a
failed API call aborts the run instead of creating issues from
partial data. API calls use ``GITHUB_TOKEN`` when set (the workflow
token reads public repositories fine and avoids the low
unauthenticated rate limit on shared runner IPs); issue writes to
this repository require it.

``--dry-run`` prints the baselines read, the upstream state found and
the issue actions that would be taken, and changes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://api.github.com"
LABEL = "firmware-watch"
TITLE_PREFIX = "Firmware release: "
DEFAULT_REPO = "catduckgnaf/print-handy"

ZMOD_REPO = "ghzserg/zmod"


class WatchError(Exception):
    """Any failure that must abort the run before issues are touched."""


# ---------------------------------------------------------------------------
# Versions


_STAGE_RANK = {"dev": 0, "alpha": 1, "beta": 2, "rc": 3}
_FINAL_RANK = 4


def firmware_version_key(text: str) -> tuple:
    """Ordering key for firmware versions like ``1.7.5-2`` or ``1.7.6``.

    ZMod ships suffixed builds of a release: ``1.7.5-2`` is a later
    build of the 1.7.5 release (catduck's own AD5X runs ``1.7.5-2``),
    so a numeric ``-N`` suffix ranks **above** the plain release of
    the same numbers and below the next release:
    ``1.7.5 < 1.7.5-2 < 1.7.6``. That is the opposite of the upstream
    watch's prerelease convention, so this watch has its own key.
    A lettered suffix (``-rc1``) is a prerelease and ranks below the
    final, ordered ``alpha < beta < rc < final``. Numeric parts
    compare numerically (``1.9.15`` > ``1.9.9``).
    """
    value = text.strip().lstrip("v")
    main, _, suffix = value.partition("-")
    numbers = []
    for part in main.split("."):
        match = re.match(r"\d+", part)
        numbers.append(int(match.group()) if match else 0)
    while len(numbers) < 4:
        numbers.append(0)
    if not suffix:
        return (*numbers, 1, _FINAL_RANK, 0)
    if suffix.isdigit():
        return (*numbers, 1, _FINAL_RANK, int(suffix))
    match = re.match(r"([a-zA-Z]+)\.?(\d*)", suffix)
    stage = match.group(1).lower() if match else ""
    serial = int(match.group(2)) if match and match.group(2) else 0
    return (*numbers, 0, _STAGE_RANK.get(stage, 0), serial)


def parse_datetime(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


# ---------------------------------------------------------------------------
# Baselines


@dataclass(frozen=True)
class WatchedFirmware:
    """One watched firmware: where its releases live, and the audited
    baseline recorded in this repository that they compare against."""

    name: str
    repo: str
    baseline: str
    baseline_source: str


def parse_zmod_baseline(text: str) -> str:
    """Parse the audited-against ZMod version out of docs/ZMOD.md.

    The baseline sentence is ``audited against **ZMod 1.7.6**`` (a
    suffixed build like ``1.7.5-2`` is a valid baseline too). A
    missing sentence fails closed: the watch exists to compare
    against a recorded audit, not to guess one.
    """
    match = re.search(
        r"audited against \*\*ZMod (\d+\.\d+\.\d+(?:-\d+)?)\*\*", text
    )
    if not match:
        raise WatchError(
            "docs/ZMOD.md: no 'audited against **ZMod X.Y.Z**' baseline found"
        )
    return match.group(1)


def read_baselines(root: Path) -> list[WatchedFirmware]:
    try:
        zmod_text = (root / "docs" / "ZMOD.md").read_text()
    except OSError as exc:
        raise WatchError(f"cannot read docs/ZMOD.md under {root}: {exc}") from exc
    return [
        WatchedFirmware(
            name="ZMod",
            repo=ZMOD_REPO,
            baseline=parse_zmod_baseline(zmod_text),
            baseline_source="docs/ZMOD.md audited baseline",
        )
    ]


# ---------------------------------------------------------------------------
# Upstream state and the plan


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    prerelease: bool
    published_at: datetime
    url: str


@dataclass(frozen=True)
class Finding:
    """One watched firmware: its baseline and the newest final release."""

    firmware: str
    baseline_description: str
    # The newest final release newer than the baseline, or None.
    newer_release: Release | None = None
    # The newest final release, newer or not (for reporting).
    latest_release: Release | None = None
    # Catch-up test for open issues of this firmware: called with the
    # issue's version -> True when the baseline has caught up with it.
    baseline_caught_up: object = field(default=None, repr=False)


@dataclass(frozen=True)
class WatchIssue:
    number: int
    firmware: str
    version: str


@dataclass(frozen=True)
class Action:
    kind: str  # "create" or "close"
    firmware: str
    version: str
    issue_number: int | None = None
    release: Release | None = None


def select_latest_final(releases: list[Release]) -> Release | None:
    """The newest final (non-prerelease) release by version ordering."""
    finals = [r for r in releases if not r.prerelease]
    return max(finals, key=lambda r: firmware_version_key(r.version), default=None)


def _finding(watched: WatchedFirmware, releases: list[Release]) -> Finding:
    latest = select_latest_final(releases)
    baseline_key = firmware_version_key(watched.baseline)
    newer = (
        latest
        if latest is not None
        and firmware_version_key(latest.version) > baseline_key
        else None
    )
    return Finding(
        firmware=watched.name,
        baseline_description=f"{watched.baseline} ({watched.baseline_source})",
        newer_release=newer,
        latest_release=latest,
        baseline_caught_up=lambda version: firmware_version_key(version)
        <= baseline_key,
    )


def build_findings(
    watched: list[WatchedFirmware], releases: dict[str, list[Release]]
) -> list[Finding]:
    return [_finding(w, releases.get(w.name, [])) for w in watched]


def plan_actions(findings: list[Finding], open_issues: list[WatchIssue]) -> list[Action]:
    """Decide issue creates/closes. Pure: no I/O, fully unit-tested."""
    actions: list[Action] = []
    open_by_key = {(i.firmware, i.version): i for i in open_issues}
    for finding in findings:
        release = finding.newer_release
        if release is not None and (finding.firmware, release.version) not in open_by_key:
            actions.append(
                Action(
                    kind="create",
                    firmware=finding.firmware,
                    version=release.version,
                    release=release,
                )
            )
    for issue in open_issues:
        finding = next(
            (f for f in findings if f.firmware == issue.firmware), None
        )
        if finding is not None and finding.baseline_caught_up(issue.version):
            actions.append(
                Action(
                    kind="close",
                    firmware=issue.firmware,
                    version=issue.version,
                    issue_number=issue.number,
                )
            )
    return actions


# ---------------------------------------------------------------------------
# GitHub API


class GitHubClient:
    """Minimal GitHub REST client over urllib (stdlib only)."""

    def __init__(self, token: str | None):
        self._token = token

    def request(self, method: str, path: str, payload: dict | None = None):
        url = path if path.startswith("https://") else f"{API_BASE}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "print-handy-firmware-watch")
        if self._token:
            request.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as response:
                body = response.read().decode()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise WatchError(
                f"GitHub API {method} {url} failed: HTTP {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise WatchError(f"GitHub API {method} {url} failed: {exc}") from exc
        return json.loads(body) if body else None

    def _list_all(self, path: str) -> list:
        """Fetch every page of a list endpoint.

        A single page silently truncates at 100 entries: for the
        watch issues that would blind deduplication and catch-up
        closes once reports accumulate, so always walk the pages.
        """
        items: list = []
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            batch = self.request(
                "GET", f"{path}{separator}per_page=100&page={page}"
            )
            if batch is None:
                raise WatchError(f"GitHub API returned no list for {path}")
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

    def list_releases(self, repo: str) -> list[Release]:
        raw = self._list_all(f"/repos/{repo}/releases")
        releases = []
        for item in raw:
            if item.get("draft"):
                continue
            releases.append(
                Release(
                    version=item["tag_name"].lstrip("v"),
                    tag=item["tag_name"],
                    prerelease=bool(item.get("prerelease")),
                    published_at=parse_datetime(item["published_at"]),
                    url=item["html_url"],
                )
            )
        return releases

    def list_open_watch_issues(self, repo: str) -> list[WatchIssue]:
        raw = self._list_all(
            f"/repos/{repo}/issues?labels={LABEL}&state=open"
        )
        issues = []
        for item in raw:
            if "pull_request" in item:
                continue
            match = re.match(
                rf"^{re.escape(TITLE_PREFIX)}(.+) (\S+)$", item["title"]
            )
            if match:
                issues.append(
                    WatchIssue(
                        number=item["number"],
                        firmware=match.group(1),
                        version=match.group(2),
                    )
                )
        return issues

    def ensure_label(self, repo: str) -> None:
        found = self.request("GET", f"/repos/{repo}/labels/{LABEL}")
        if found is None:
            self.request(
                "POST",
                f"/repos/{repo}/labels",
                {
                    "name": LABEL,
                    "color": "fbca04",
                    "description": (
                        "A newer printer firmware release exists than "
                        "the baseline Print Handy last audited"
                    ),
                },
            )

    def create_issue(self, repo: str, title: str, body: str) -> int:
        raw = self.request(
            "POST",
            f"/repos/{repo}/issues",
            {"title": title, "body": body, "labels": [LABEL]},
        )
        return raw["number"]

    def close_issue(self, repo: str, number: int, comment: str) -> None:
        self.request(
            "POST", f"/repos/{repo}/issues/{number}/comments", {"body": comment}
        )
        self.request(
            "PATCH", f"/repos/{repo}/issues/{number}", {"state": "closed"}
        )


# ---------------------------------------------------------------------------
# Reporting


def issue_title(firmware: str, version: str) -> str:
    return f"{TITLE_PREFIX}{firmware} {version}"


def issue_body(finding: Finding, release: Release) -> str:
    return (
        f"A newer {finding.firmware} release exists than the version "
        f"Print Handy last audited.\n\n"
        f"- Firmware: {finding.firmware}\n"
        f"- Audited baseline: {finding.baseline_description}\n"
        f"- Upstream: {release.version} — published "
        f"{release.published_at.date().isoformat()} — {release.url}\n\n"
        f"The follow-up is a feature-by-feature audit of {release.version} "
        f"against Print Handy's contracts, in the pattern of the 1.7.5 "
        f"audit (#562) and the 1.7.6 audit (#1269), ending with the "
        f"audited baseline in docs/ZMOD.md moving to {release.version}. "
        f"This notice is not an audit: it says the release exists, "
        f"nothing about what it changes for Print Handy.\n\n"
        f"Notification only: this watch never edits the baseline and "
        f"never opens a bump PR. This issue closes automatically once "
        f"the audited baseline catches up with {release.version}.\n"
    )


def run(root: Path, repo: str, dry_run: bool, client: GitHubClient) -> int:
    watched = read_baselines(root)
    print("Audited baselines read from this checkout:")
    for entry in watched:
        print(f"  {entry.name}: {entry.baseline} ({entry.baseline_source})")

    # Fetch everything before touching any issue: a failure here
    # aborts the run with no issue created or closed from partial
    # data.
    releases = {entry.name: client.list_releases(entry.repo) for entry in watched}

    findings = build_findings(watched, releases)
    print("Upstream state:")
    for finding in findings:
        latest = finding.latest_release
        state = (
            f"latest final release {latest.version} "
            f"(published {latest.published_at.date().isoformat()})"
            if latest is not None
            else "no final releases found"
        )
        marker = (
            f" -> NEWER than the baseline: {finding.newer_release.version}"
            if finding.newer_release is not None
            else ""
        )
        print(
            f"  {finding.firmware}: baseline {finding.baseline_description}; "
            f"{state}{marker}"
        )

    open_issues: list[WatchIssue] = []
    issues_known = True
    try:
        open_issues = client.list_open_watch_issues(repo)
    except WatchError:
        if not dry_run:
            raise
        issues_known = False
        print(
            "  (could not list existing firmware-watch issues; "
            "planning against an empty list)"
        )
    actions = plan_actions(findings, open_issues)
    if not actions:
        print("No issue actions: every baseline is current or already reported.")
        return 0
    findings_by_name = {f.firmware: f for f in findings}
    for action in actions:
        if action.kind == "create":
            title = issue_title(action.firmware, action.version)
            if dry_run:
                print(f"  WOULD CREATE issue: {title}")
            else:
                client.ensure_label(repo)
                number = client.create_issue(
                    repo,
                    title,
                    issue_body(findings_by_name[action.firmware], action.release),
                )
                print(f"  Created issue #{number}: {title}")
        else:
            comment = (
                f"The audited baseline for {action.firmware} has caught "
                f"up with {action.version}. Closing."
            )
            if dry_run:
                print(
                    f"  WOULD CLOSE issue #{action.issue_number}: "
                    f"{issue_title(action.firmware, action.version)}"
                )
            else:
                client.close_issue(repo, action.issue_number, comment)
                print(
                    f"  Closed issue #{action.issue_number}: "
                    f"{issue_title(action.firmware, action.version)}"
                )
    if dry_run and not issues_known:
        print("Dry run only; existing issues were not readable, nothing changed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print baselines, upstream state and planned issue actions; change nothing",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository checkout to read baselines from (default: this repo)",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO),
        help="repository that receives the watch issues",
    )
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    client = GitHubClient(token)
    try:
        return run(args.root, args.repo, args.dry_run, client)
    except WatchError as exc:
        print(f"firmware watch aborted: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
