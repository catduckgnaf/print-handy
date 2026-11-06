#!/usr/bin/env python3
"""Weekly upstream release watch for the tools Print Handy pins manually.

Renovate covers GitHub Actions, pip requirements and Dockerfile base
images, but it cannot see the pins this script watches:

- OrcaSlicer, stable and preview channels: ``version=`` / ``revision=``
  blocks in ``docker/orca-prebuilt.sh``.
- go2rtc: ``version=`` in ``docker/go2rtc-prebuilt.sh``.
- Bambu Studio and Spoolman: commit pins (``ARG BAMBU_REF`` /
  ``ARG SPOOLMAN_REF``) in the ``Dockerfile``.

The script reads the pins from those files, queries each upstream's
GitHub releases, and reports through issues in this repository,
deduplicated by the ``upstream-watch`` label and titles of the form
``Upstream release: <Component> <version>``:

- a newer-than-pin release with no matching open issue opens one;
- an open issue whose version the pin has caught up with is closed
  with a short comment;
- when nothing is newer, nothing is created or changed — a quiet
  week is a successful no-op.

Notification only: the script never edits a pin and never opens a
bump PR. Bumps stay separate decisions (an OrcaSlicer bump also needs
the per-architecture SHA256 and vendor-profile identity updates a
plain version edit cannot do).

All upstream data is fetched before any issue is touched, so a
failed API call aborts the run instead of creating issues from
partial data. API calls use ``GITHUB_TOKEN`` when set (the workflow
token reads public repositories fine and avoids the low
unauthenticated rate limit on shared runner IPs); issue writes to
this repository require it.

``--dry-run`` prints the pins read, the upstream state found and the
issue actions that would be taken, and changes nothing.
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
LABEL = "upstream-watch"
TITLE_PREFIX = "Upstream release: "
DEFAULT_REPO = "catduckgnaf/print-handy"

ORCA_REPO = "OrcaSlicer/OrcaSlicer"
GO2RTC_REPO = "AlexxIT/go2rtc"
SPOOLMAN_REPO = "Donkie/Spoolman"
BAMBU_REPO = "bambulab/BambuStudio"


class WatchError(Exception):
    """Any failure that must abort the run before issues are touched."""


# ---------------------------------------------------------------------------
# Versions


_STAGE_RANK = {"dev": 0, "alpha": 1, "beta": 2, "rc": 3}
_FINAL_RANK = 4


def version_key(text: str) -> tuple:
    """Ordering key for versions like ``2.5.0-beta`` or ``v1.9.15``.

    Numeric parts compare numerically (``1.9.15`` > ``1.9.9``); a
    prerelease suffix ranks below the final release of the same
    numbers, ordered ``alpha < beta < rc < final``.
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
        return (*numbers, _FINAL_RANK, 0)
    match = re.match(r"([a-zA-Z]+)\.?(\d*)", suffix)
    stage = match.group(1).lower() if match else ""
    serial = int(match.group(2)) if match and match.group(2) else 0
    return (*numbers, _STAGE_RANK.get(stage, 0), serial)


def parse_datetime(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


# ---------------------------------------------------------------------------
# Pin parsing


@dataclass(frozen=True)
class ChannelPin:
    version: str
    revision: str


@dataclass(frozen=True)
class Pins:
    orca_stable: ChannelPin
    orca_preview: ChannelPin
    go2rtc_version: str
    bambu_ref: str
    spoolman_ref: str


def parse_orca_pins(text: str) -> dict[str, ChannelPin]:
    """Parse the ``stable)`` / ``preview)`` case blocks of orca-prebuilt.sh."""
    channels: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line in text.splitlines():
        label = re.match(r"\s+(stable|preview)\)\s*$", line)
        if label:
            current = label.group(1)
            channels[current] = {}
            continue
        if re.match(r"\s+\*\)", line):
            current = None
            continue
        if current is not None:
            assign = re.match(r"\s+(version|revision)=(\S+)\s*$", line)
            if assign:
                channels[current][assign.group(1)] = assign.group(2)
    pins = {}
    for channel in ("stable", "preview"):
        found = channels.get(channel, {})
        if "version" not in found or "revision" not in found:
            raise WatchError(
                f"orca-prebuilt.sh: no version/revision pin found for "
                f"the {channel} channel"
            )
        pins[channel] = ChannelPin(
            version=found["version"], revision=found["revision"]
        )
    return pins


def parse_script_version(text: str, source: str) -> str:
    match = re.search(r"^version=(\S+)\s*$", text, re.MULTILINE)
    if not match:
        raise WatchError(f"{source}: no version= pin found")
    return match.group(1)


def parse_dockerfile_arg(text: str, name: str) -> str:
    match = re.search(rf"^ARG {name}=(\S+)\s*$", text, re.MULTILINE)
    if not match:
        raise WatchError(f"Dockerfile: no ARG {name}= pin found")
    return match.group(1)


def read_pins(root: Path) -> Pins:
    try:
        orca_text = (root / "docker" / "orca-prebuilt.sh").read_text()
        go2rtc_text = (root / "docker" / "go2rtc-prebuilt.sh").read_text()
        dockerfile_text = (root / "Dockerfile").read_text()
    except OSError as exc:
        raise WatchError(f"cannot read pin files under {root}: {exc}") from exc
    orca = parse_orca_pins(orca_text)
    return Pins(
        orca_stable=orca["stable"],
        orca_preview=orca["preview"],
        go2rtc_version=parse_script_version(go2rtc_text, "go2rtc-prebuilt.sh"),
        bambu_ref=parse_dockerfile_arg(dockerfile_text, "BAMBU_REF"),
        spoolman_ref=parse_dockerfile_arg(dockerfile_text, "SPOOLMAN_REF"),
    )


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
    """One watched component: its pin and the newest relevant release."""

    component: str
    pin_description: str
    # Set for version-pinned components: the release newer than the
    # pin, or None when the pin is current.
    newer_release: Release | None = None
    # Release the pin is compared with, newer or not (for reporting).
    latest_release: Release | None = None
    # Catch-up test for open issues of this component: called with
    # (issue version, its release or None) -> True when the pin has
    # caught up with that version.
    pin_caught_up: object = field(default=None, repr=False)


@dataclass(frozen=True)
class WatchIssue:
    number: int
    component: str
    version: str


@dataclass(frozen=True)
class Action:
    kind: str  # "create" or "close"
    component: str
    version: str
    issue_number: int | None = None
    release: Release | None = None


def select_latest(releases: list[Release]) -> tuple[Release | None, Release | None]:
    """(latest stable, latest prerelease) by version ordering."""
    stable = [r for r in releases if not r.prerelease]
    prerelease = [r for r in releases if r.prerelease]
    latest_stable = max(stable, key=lambda r: version_key(r.version), default=None)
    latest_pre = max(prerelease, key=lambda r: version_key(r.version), default=None)
    return latest_stable, latest_pre


def _version_finding(
    component: str,
    pin_version: str,
    pin_source: str,
    candidates: list[Release | None],
) -> Finding:
    pool = [r for r in candidates if r is not None]
    latest = max(pool, key=lambda r: version_key(r.version), default=None)
    newer = (
        latest
        if latest is not None and version_key(latest.version) > version_key(pin_version)
        else None
    )
    pin_key = version_key(pin_version)
    return Finding(
        component=component,
        pin_description=f"{pin_version} ({pin_source})",
        newer_release=newer,
        latest_release=latest,
        pin_caught_up=lambda version, _release: version_key(version) <= pin_key,
    )


def _commit_finding(
    component: str,
    pin_ref: str,
    pin_date: datetime,
    pin_source: str,
    latest: Release | None,
    releases: list[Release],
) -> Finding:
    newer = (
        latest
        if latest is not None and latest.published_at > pin_date
        else None
    )
    by_version = {r.version: r for r in releases}

    def caught_up(version: str, _release: Release | None) -> bool:
        release = by_version.get(version)
        return release is not None and release.published_at <= pin_date

    return Finding(
        component=component,
        pin_description=(
            f"commit {pin_ref[:12]} committed "
            f"{pin_date.date().isoformat()} ({pin_source})"
        ),
        newer_release=newer,
        latest_release=latest,
        pin_caught_up=caught_up,
    )


def build_findings(
    pins: Pins,
    orca_releases: list[Release],
    go2rtc_releases: list[Release],
    spoolman_releases: list[Release],
    spoolman_pin_date: datetime,
    bambu_releases: list[Release],
    bambu_pin_date: datetime,
) -> list[Finding]:
    orca_stable_latest, orca_pre_latest = select_latest(orca_releases)
    go2rtc_stable, go2rtc_pre = select_latest(go2rtc_releases)
    spoolman_stable, spoolman_pre = select_latest(spoolman_releases)
    bambu_stable, bambu_pre = select_latest(bambu_releases)

    def newest(*candidates: Release | None) -> Release | None:
        pool = [r for r in candidates if r is not None]
        return max(pool, key=lambda r: r.published_at, default=None)

    return [
        _version_finding(
            "OrcaSlicer (stable)",
            pins.orca_stable.version,
            "docker/orca-prebuilt.sh, stable channel",
            [orca_stable_latest],
        ),
        # A new final release counts for the preview channel too, so
        # its candidates are the newest stable and prerelease alike.
        _version_finding(
            "OrcaSlicer (preview)",
            pins.orca_preview.version,
            "docker/orca-prebuilt.sh, preview channel",
            [orca_stable_latest, orca_pre_latest],
        ),
        _version_finding(
            "go2rtc",
            pins.go2rtc_version,
            "docker/go2rtc-prebuilt.sh",
            [go2rtc_stable, go2rtc_pre],
        ),
        _commit_finding(
            "Spoolman",
            pins.spoolman_ref,
            spoolman_pin_date,
            "Dockerfile, ARG SPOOLMAN_REF",
            newest(spoolman_stable, spoolman_pre),
            spoolman_releases,
        ),
        _commit_finding(
            "Bambu Studio",
            pins.bambu_ref,
            bambu_pin_date,
            "Dockerfile, ARG BAMBU_REF",
            newest(bambu_stable, bambu_pre),
            bambu_releases,
        ),
    ]


def plan_actions(findings: list[Finding], open_issues: list[WatchIssue]) -> list[Action]:
    """Decide issue creates/closes. Pure: no I/O, fully unit-tested."""
    actions: list[Action] = []
    open_by_key = {(i.component, i.version): i for i in open_issues}
    for finding in findings:
        release = finding.newer_release
        if release is not None and (finding.component, release.version) not in open_by_key:
            actions.append(
                Action(
                    kind="create",
                    component=finding.component,
                    version=release.version,
                    release=release,
                )
            )
    for issue in open_issues:
        finding = next(
            (f for f in findings if f.component == issue.component), None
        )
        if finding is not None and finding.pin_caught_up(issue.version, None):
            actions.append(
                Action(
                    kind="close",
                    component=issue.component,
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
        request.add_header("User-Agent", "print-handy-upstream-watch")
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

    def list_releases(self, repo: str) -> list[Release]:
        raw = self.request("GET", f"/repos/{repo}/releases?per_page=100")
        if raw is None:
            raise WatchError(f"GitHub API returned no release list for {repo}")
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

    def commit_date(self, repo: str, ref: str) -> datetime:
        raw = self.request("GET", f"/repos/{repo}/commits/{ref}")
        if raw is None:
            raise WatchError(f"commit {ref} not found in {repo}")
        return parse_datetime(raw["commit"]["committer"]["date"])

    def list_open_watch_issues(self, repo: str) -> list[WatchIssue]:
        raw = self.request(
            "GET",
            f"/repos/{repo}/issues?labels={LABEL}&state=open&per_page=100",
        )
        if raw is None:
            raise WatchError(f"GitHub API returned no issue list for {repo}")
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
                        component=match.group(1),
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
                        "A newer upstream release exists than the "
                        "version Print Handy pins"
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


def issue_title(component: str, version: str) -> str:
    return f"{TITLE_PREFIX}{component} {version}"


def issue_body(finding: Finding, release: Release) -> str:
    return (
        f"A newer upstream release exists than the version Print Handy pins.\n\n"
        f"- Component: {finding.component}\n"
        f"- Pinned: {finding.pin_description}\n"
        f"- Upstream: {release.version} — published "
        f"{release.published_at.date().isoformat()} — {release.url}\n\n"
        f"Notification only: this watch never edits pins and never opens "
        f"bump PRs. Bumping is a separate decision; for OrcaSlicer a bump "
        f"also needs the per-architecture SHA256 and vendor-profile "
        f"identity updates a plain version edit cannot do. This issue "
        f"closes automatically once the pin catches up with "
        f"{release.version}.\n"
    )


def run(root: Path, repo: str, dry_run: bool, client: GitHubClient) -> int:
    pins = read_pins(root)
    print("Pins read from this checkout:")
    print(
        f"  OrcaSlicer (stable): {pins.orca_stable.version} "
        f"(revision {pins.orca_stable.revision[:12]})"
    )
    print(
        f"  OrcaSlicer (preview): {pins.orca_preview.version} "
        f"(revision {pins.orca_preview.revision[:12]})"
    )
    print(f"  go2rtc: {pins.go2rtc_version}")
    print(f"  Spoolman: commit {pins.spoolman_ref[:12]}")
    print(f"  Bambu Studio: commit {pins.bambu_ref[:12]}")

    # Fetch everything before touching any issue: a failure here
    # aborts the run with no issue created or closed from partial
    # data.
    orca_releases = client.list_releases(ORCA_REPO)
    go2rtc_releases = client.list_releases(GO2RTC_REPO)
    spoolman_releases = client.list_releases(SPOOLMAN_REPO)
    bambu_releases = client.list_releases(BAMBU_REPO)
    spoolman_pin_date = client.commit_date(SPOOLMAN_REPO, pins.spoolman_ref)
    bambu_pin_date = client.commit_date(BAMBU_REPO, pins.bambu_ref)

    findings = build_findings(
        pins,
        orca_releases,
        go2rtc_releases,
        spoolman_releases,
        spoolman_pin_date,
        bambu_releases,
        bambu_pin_date,
    )
    print("Upstream state:")
    for finding in findings:
        latest = finding.latest_release
        state = (
            f"latest {latest.version} "
            f"(published {latest.published_at.date().isoformat()})"
            if latest is not None
            else "no releases found"
        )
        marker = (
            f" -> NEWER than the pin: {finding.newer_release.version}"
            if finding.newer_release is not None
            else ""
        )
        print(f"  {finding.component}: pin {finding.pin_description}; {state}{marker}")

    open_issues: list[WatchIssue] = []
    issues_known = True
    try:
        open_issues = client.list_open_watch_issues(repo)
    except WatchError:
        if not dry_run:
            raise
        issues_known = False
        print(
            "  (could not list existing upstream-watch issues; "
            "planning against an empty list)"
        )
    actions = plan_actions(findings, open_issues)
    if not actions:
        print("No issue actions: every pin is current or already reported.")
        return 0
    findings_by_component = {f.component: f for f in findings}
    for action in actions:
        if action.kind == "create":
            title = issue_title(action.component, action.version)
            if dry_run:
                print(f"  WOULD CREATE issue: {title}")
            else:
                client.ensure_label(repo)
                number = client.create_issue(
                    repo,
                    title,
                    issue_body(
                        findings_by_component[action.component], action.release
                    ),
                )
                print(f"  Created issue #{number}: {title}")
        else:
            comment = (
                f"The Print Handy pin for {action.component} has caught "
                f"up with {action.version}. Closing."
            )
            if dry_run:
                print(
                    f"  WOULD CLOSE issue #{action.issue_number}: "
                    f"{issue_title(action.component, action.version)}"
                )
            else:
                client.close_issue(repo, action.issue_number, comment)
                print(
                    f"  Closed issue #{action.issue_number}: "
                    f"{issue_title(action.component, action.version)}"
                )
    if dry_run and not issues_known:
        print("Dry run only; existing issues were not readable, nothing changed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print pins, upstream state and planned issue actions; change nothing",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository checkout to read pins from (default: this repo)",
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
        print(f"upstream watch aborted: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
