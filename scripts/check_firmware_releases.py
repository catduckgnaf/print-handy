#!/usr/bin/env python3
"""Weekly firmware release watch for printer firmware Print Handy audits.

The upstream watch (``scripts/check_upstream_releases.py``) covers the
tools Print Handy pins and bundles. Printer firmware is different: it
is not a dependency, there is no pin to compare against, and a release
matters because it can move a contract Print Handy relies on. What
Print Handy compares against instead is the **audited baseline**: the
firmware version this repository last audited feature by feature,
recorded in the docs. That difference is why this is a separate watch
(issue #1271), not another component in the upstream one.

Watched today:

- ZMod (``ghzserg/zmod``, releases): baseline = the audited-against
  version in ``docs/ZMOD.md``. The baseline moves only when an audit
  lands (the pattern of #562 for 1.7.5 and #1269 for 1.7.6), never
  automatically.
- Every entry in ``docs/firmware-baselines.toml`` (issue #1280,
  sections F to H): Klipper and Moonraker by **tags** (Klipper has
  never published a GitHub Release and Moonraker's releases feed
  stopped in 2021, so a releases check would be silently empty; the
  script refuses one for those repositories), OctoPrint and Marlin by
  releases, Prusa Buddy by releases **per printer line** (one
  repository ships divergent version lines; a release belongs to a
  line when its title lists one of the line's printer names), and
  QIDI by releases **per model repository** (the version is read from
  the end of each model's own tag shape, ``Q1_Pro_V4.4.24``). The
  same rule applies: a baseline moves only when an audit lands.

The remaining vendor firmware (stock Flashforge, Bambu Lab, Sovol,
Elegoo, Creality, Anycubic) has no reliable machine-readable feed
(#1271) and is not scraped.

The script reads the baselines, queries each watched firmware's
GitHub releases or tags, and reports through issues in this repository,
deduplicated by the ``firmware-watch`` label and titles of the form
``Firmware release: <Firmware> <version>``:

- a newer-than-baseline release with no matching issue opens one,
  naming the firmware, the version and the release link, and pointing
  at the audit pattern as the follow-up. A matching issue in either
  state counts: a notice a maintainer closed by hand is never
  reopened as a new issue the next week;
- an open issue whose version the baseline has caught up with is
  closed with a short comment;
- when nothing is newer, nothing is created or changed; a quiet
  week is a successful no-op.

Notification only: the script never edits the baseline, never opens a
bump PR, never downloads firmware and never contacts a printer. A
notice is not an audit: it says a release exists, nothing about what
the release changes for Print Handy.

Only final releases count. Firmware audits follow shipped releases;
a prerelease upstream (or, for a tags feed, a tag with a lettered
suffix such as ``v0.14.0-rc1``) opens no issue here. A feed that has
entries but none this watch can read (no tag parses as a version, or
no release title names a watched printer line) fails the run instead
of passing quietly: that is a format change to look at, not a quiet
week.

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
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://api.github.com"
LABEL = "firmware-watch"
TITLE_PREFIX = "Firmware release: "
DEFAULT_REPO = "catduckgnaf/print-handy"

# Seconds before one API call gives up. A hung connection must fail
# the run, not hold the runner until the job timeout.
REQUEST_TIMEOUT = 30
# A rate-limited call is retried once when the limit lifts within
# this many seconds; a longer wait fails the run, naming the wait.
RATE_LIMIT_MAX_WAIT = 60

ZMOD_REPO = "ghzserg/zmod"
BASELINES_FILE = "docs/firmware-baselines.toml"
ZMOD_BASELINE_FILE = "docs/ZMOD.md"

# Repositories that publish no usable GitHub Releases. A releases feed
# pointed at one is empty (Klipper) or frozen in 2021 (Moonraker) and
# would report "nothing newer" forever, so these are only ever read
# by tags (#1275).
TAG_ONLY_REPOS = frozenset({"Klipper3d/klipper", "Arksine/moonraker"})
FEEDS = ("releases", "tags")


class WatchError(Exception):
    """Any failure that must abort the run before issues are touched."""


class _RateLimited(Exception):
    """A rate-limited API call; ``wait`` is seconds until it lifts."""

    def __init__(self, wait: float, message: str):
        super().__init__(message)
        self.wait = wait
        self.message = message


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
    value = text.strip().lstrip("vV")
    main, _, suffix = value.partition("-")
    glued = re.match(r"^([\d.]*\d)([a-zA-Z].*)$", main)
    if glued and not suffix:
        # ``2.0.0rc7`` (OctoPrint's shape): the letters start a
        # prerelease suffix even without a hyphen.
        main, suffix = glued.group(1), glued.group(2)
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


_TAG_VERSION = re.compile(r"(\d+(?:\.\d+)+(?:-?[0-9A-Za-z][0-9A-Za-z.]*)?)$")


def version_from_tag(tag: str) -> str | None:
    """The version at the end of a tag, or None when there is none.

    Covers every tag shape the watch reads: ``1.7.5-2`` (ZMod),
    ``v0.13.0`` (Klipper), ``2.1.2.9`` (Marlin), ``2.0.0rc7``
    (OctoPrint), ``v8.0.1`` (Prusa) and QIDI's model-prefixed tags
    (``Q1_Pro_V4.4.24``, ``Plus4_v1.7.1``, ``V4.4.18``). A tag with no
    dotted version at its end (Moonraker's stray ``v.08``) is None and
    is skipped, never ranked as version 0.
    """
    match = _TAG_VERSION.search(tag.strip())
    return match.group(1) if match else None


def is_prerelease_version(version: str) -> bool:
    """A lettered suffix marks a prerelease (``-rc1``, ``rc7``, ``-beta``);
    a numeric build suffix (``1.7.5-2``) does not."""
    return firmware_version_key(version)[4] == 0


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
    # "releases" (GitHub Releases) or "tags" (Git tags).
    feed: str = "releases"
    # Printer names selecting one printer line's releases from a shared
    # repository (Prusa Buddy); empty watches the whole feed.
    line: tuple[str, ...] = ()
    # The file whose baseline an audit moves (named in each notice).
    baseline_home: str = ZMOD_BASELINE_FILE
    # Notice name shared by the printer lines of one firmware ("Prusa
    # Buddy"); empty means the entry's own name.
    group: str = ""


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


_RECORD_FIELDS = {"name", "repo", "feed", "baseline", "basis", "line", "group"}


def parse_recorded_baselines(text: str) -> list[WatchedFirmware]:
    """Parse ``docs/firmware-baselines.toml`` into watched entries.

    Fails closed on anything malformed: a missing or unknown field, an
    unknown feed, a baseline that is not a version, and above all a
    ``releases`` feed pointed at a tags-only repository
    (``TAG_ONLY_REPOS``), which would never report anything.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise WatchError(f"{BASELINES_FILE}: not valid TOML: {exc}") from exc
    records = data.get("firmware")
    if not isinstance(records, list) or not records:
        raise WatchError(f"{BASELINES_FILE}: no [[firmware]] entries")
    watched: list[WatchedFirmware] = []
    for index, record in enumerate(records, start=1):
        where = f"{BASELINES_FILE} entry {index}"
        unknown = set(record) - _RECORD_FIELDS
        if unknown:
            raise WatchError(f"{where}: unknown field(s) {sorted(unknown)}")
        for key in ("name", "repo", "feed", "baseline", "basis"):
            if not isinstance(record.get(key), str) or not record[key].strip():
                raise WatchError(f"{where}: missing or empty '{key}'")
        name, repo, feed = record["name"], record["repo"], record["feed"]
        where = f"{BASELINES_FILE} entry '{name}'"
        if feed not in FEEDS:
            raise WatchError(f"{where}: feed must be one of {FEEDS}, not '{feed}'")
        if feed == "releases" and repo in TAG_ONLY_REPOS:
            raise WatchError(
                f"{where}: {repo} publishes no usable GitHub releases; a "
                f'releases feed would stay silent forever. Use feed = "tags".'
            )
        baseline = record["baseline"].strip().lstrip("vV")
        if version_from_tag(baseline) != baseline:
            raise WatchError(f"{where}: baseline '{record['baseline']}' is not a version")
        line = record.get("line", [])
        if not isinstance(line, list) or not all(
            isinstance(item, str) and item.strip() for item in line
        ):
            raise WatchError(f"{where}: 'line' must be a list of printer names")
        if line and feed != "releases":
            raise WatchError(
                f'{where}: a printer line selects releases by title; it needs feed = "releases"'
            )
        group = record.get("group", "")
        if not isinstance(group, str) or (group and not line):
            raise WatchError(f"{where}: 'group' is a name shared by printer lines; "
                             "it needs 'line'")
        watched.append(
            WatchedFirmware(
                name=name,
                repo=repo,
                baseline=baseline,
                baseline_source=record["basis"],
                feed=feed,
                line=tuple(item.strip() for item in line),
                baseline_home=BASELINES_FILE,
                group=group.strip(),
            )
        )
    return watched


def _check_names(watched: list[WatchedFirmware]) -> None:
    """Notice names must be unambiguous: entry names are unique, a group
    is never also an entry's own name, and a group's lines share one
    repository and feed."""
    names = [entry.name for entry in watched]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise WatchError(f"duplicate watched firmware name(s): {duplicates}")
    groups: dict[str, set[tuple[str, str]]] = {}
    for entry in watched:
        if entry.group:
            groups.setdefault(entry.group, set()).add((entry.repo, entry.feed))
    for group, sources in groups.items():
        if group in names:
            raise WatchError(f"group '{group}' is also a watched firmware name")
        if len(sources) > 1:
            raise WatchError(f"group '{group}' spans more than one repository feed")


def read_baselines(root: Path) -> list[WatchedFirmware]:
    """ZMod's audited baseline from docs/ZMOD.md, then every recorded
    baseline from docs/firmware-baselines.toml. Both files are required:
    a missing one fails the run rather than silently watching less."""
    paths = (ZMOD_BASELINE_FILE, BASELINES_FILE)
    texts = {}
    for relative in paths:
        try:
            texts[relative] = (root / relative).read_text(encoding="utf-8")
        except OSError as exc:
            raise WatchError(f"cannot read {relative} under {root}: {exc}") from exc
    watched = [
        WatchedFirmware(
            name="ZMod",
            repo=ZMOD_REPO,
            baseline=parse_zmod_baseline(texts[ZMOD_BASELINE_FILE]),
            baseline_source="docs/ZMOD.md audited baseline",
        )
    ]
    watched.extend(parse_recorded_baselines(texts[BASELINES_FILE]))
    _check_names(watched)
    return watched


# ---------------------------------------------------------------------------
# Upstream state and the plan


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    prerelease: bool
    # None for a tags feed: the Tags API carries no date.
    published_at: datetime | None
    url: str
    # The release title (empty for tags); Prusa printer lines match on it.
    name: str = ""


def release_printers(title: str) -> set[str]:
    """The printer names a Prusa Buddy release title lists.

    ``8.0.1 Firmware for Prusa XL+, XL, MINI+, MINI`` gives
    {"XL+", "XL", "MINI+", "MINI"}; older titles use ``Original
    Prusa`` and ``&`` (``... for Original Prusa MK4, MK4S, MINI & XL``).
    A spaced ``CORE One +`` reads as ``CORE One+``. A title without
    ``Firmware for`` lists nothing.
    """
    _, found, rest = title.partition("Firmware for ")
    if not found:
        return set()
    rest = re.sub(r"^\s*(?:Original\s+)?Prusa\s+", "", rest)
    names = set()
    for part in re.split(r",|&", rest):
        name = re.sub(r"\s+\+", "+", part).strip()
        if name:
            names.add(name)
    return names


def in_line(release: Release, line: tuple[str, ...]) -> bool:
    """Whether a release belongs to a watched printer line. Names match
    exactly (``CORE One`` does not match ``CORE One INDX``); an empty
    line takes every release."""
    return not line or bool(release_printers(release.name) & set(line))


@dataclass(frozen=True)
class Finding:
    """One watched firmware: its baseline and the newest final release."""

    firmware: str
    baseline_description: str
    baseline_home: str = ZMOD_BASELINE_FILE
    baseline: str = ""
    # The newest final release newer than the baseline, or None.
    newer_release: Release | None = None
    # The newest final release, newer or not (for reporting).
    latest_release: Release | None = None
    # Catch-up test for open issues of this firmware: called with the
    # issue's version -> True when the baseline has caught up with it.
    baseline_caught_up: object = field(default=None, repr=False)
    # The notice (issue title) name; printer lines of one firmware share
    # it, so one release covering several lines opens one notice.
    group: str = ""
    # For a printer line: the versions its releases carry. None means the
    # entry watches the whole feed and covers every version.
    line_versions: frozenset | None = None

    @property
    def notice_name(self) -> str:
        return self.group or self.firmware

    def covers(self, version: str) -> bool:
        return self.line_versions is None or version in self.line_versions


@dataclass(frozen=True)
class WatchIssue:
    number: int
    firmware: str
    version: str
    # Closed issues still deduplicate: a maintainer who closes a notice
    # without moving the baseline has decided on it, and the watch must
    # not reopen the same release as a fresh issue every week.
    open: bool = True


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
    selected = [r for r in releases if in_line(r, watched.line)]
    if releases and not selected:
        # A shared feed with entries, none of them for this line: the
        # release titles changed shape. Fail loudly, never report quiet.
        raise WatchError(
            f"{watched.name}: none of the {len(releases)} entries in "
            f"{watched.repo}'s {watched.feed} names this printer line "
            f"({', '.join(watched.line)}); the release title format may have changed"
        )
    latest = select_latest_final(selected)
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
        baseline_home=watched.baseline_home,
        baseline=watched.baseline,
        newer_release=newer,
        latest_release=latest,
        baseline_caught_up=lambda version: firmware_version_key(version)
        <= baseline_key,
        group=watched.group,
        line_versions=(
            frozenset(r.version for r in selected) if watched.line else None
        ),
    )


def build_findings(
    watched: list[WatchedFirmware], releases: dict[str, list[Release]]
) -> list[Finding]:
    return [_finding(w, releases.get(w.name, [])) for w in watched]


def plan_actions(findings: list[Finding], open_issues: list[WatchIssue]) -> list[Action]:
    """Decide issue creates/closes. Pure: no I/O, fully unit-tested.

    ``open_issues`` is every watch issue, open or closed. An issue for
    a release in either state suppresses a new one for that release;
    only open issues are closed when the baseline catches up.
    """
    actions: list[Action] = []
    reported = {(i.firmware, i.version) for i in open_issues}
    for finding in findings:
        release = finding.newer_release
        key = (finding.notice_name, release.version) if release else None
        if key is not None and key not in reported:
            # Printer lines sharing a notice name (Prusa Buddy) whose
            # newest release is the same one get a single notice.
            reported.add(key)
            actions.append(
                Action(
                    kind="create",
                    firmware=finding.notice_name,
                    version=release.version,
                    release=release,
                )
            )
    for issue in open_issues:
        if not issue.open:
            continue
        members = [f for f in findings if f.notice_name == issue.firmware]
        # A notice closes once every watched line that release covers
        # has its baseline at or past it. A line the release does not
        # cover never holds a notice open (a ZMod-style entry with no
        # printer line covers every version).
        if members and all(
            f.baseline_caught_up(issue.version) or not f.covers(issue.version)
            for f in members
        ) and any(f.covers(issue.version) for f in members):
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

    def __init__(self, token: str | None, *, sleep=time.sleep, now=time.time):
        self._token = token
        self._sleep = sleep
        self._now = now

    def _rate_limit_wait(self, exc: urllib.error.HTTPError) -> float | None:
        """Seconds until a rate limit lifts, or None when ``exc`` is
        not a rate limit (a plain 403 is a permission failure)."""
        headers = exc.headers or {}
        retry_after = (headers.get("Retry-After") or "").strip()
        if exc.code in (403, 429) and retry_after.isdigit():
            return float(retry_after)
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if exc.code in (403, 429) and remaining == "0" and reset:
            try:
                return max(0.0, float(reset) - self._now())
            except ValueError:
                return None
        if exc.code == 429:
            return float(RATE_LIMIT_MAX_WAIT)
        return None

    def request(self, method: str, path: str, payload: dict | None = None):
        """One API call. A rate limit that lifts within
        ``RATE_LIMIT_MAX_WAIT`` seconds is waited out and retried once;
        any other failure raises ``WatchError``."""
        try:
            return self._request_once(method, path, payload)
        except _RateLimited as limited:
            if limited.wait > RATE_LIMIT_MAX_WAIT:
                raise WatchError(limited.message) from None
            self._sleep(limited.wait + 1)
            try:
                return self._request_once(method, path, payload)
            except _RateLimited as again:
                raise WatchError(again.message) from None

    def _request_once(self, method: str, path: str, payload: dict | None):
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
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                body = response.read().decode()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            wait = self._rate_limit_wait(exc)
            if wait is not None:
                raise _RateLimited(
                    wait,
                    f"GitHub API {method} {url} is rate limited (HTTP "
                    f"{exc.code}); the limit lifts in about {int(wait)} s. "
                    f"Rerun the workflow after that.",
                ) from exc
            raise WatchError(
                f"GitHub API {method} {url} failed: HTTP {exc.code}"
            ) from exc
        except OSError as exc:
            # URLError, a timeout, or a reset connection.
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
        """Every published release. A tag with no version at its end is
        skipped; a feed whose every entry is unreadable fails the run."""
        if repo in TAG_ONLY_REPOS:
            raise WatchError(
                f"{repo} publishes no usable GitHub releases; read it by tags"
            )
        raw = self._list_all(f"/repos/{repo}/releases")
        releases = []
        for item in raw:
            if item.get("draft"):
                continue
            version = version_from_tag(item["tag_name"])
            if version is None:
                continue
            releases.append(
                Release(
                    version=version,
                    tag=item["tag_name"],
                    # Upstream's flag, or a lettered suffix: Marlin
                    # publishes its 2.1.3-b3 beta as a full release.
                    prerelease=bool(item.get("prerelease"))
                    or is_prerelease_version(version),
                    published_at=parse_datetime(item["published_at"]),
                    url=item["html_url"],
                    name=item.get("name") or "",
                )
            )
        if raw and not releases and any(not item.get("draft") for item in raw):
            raise WatchError(
                f"{repo}: none of its {len(raw)} releases has a version at the "
                "end of its tag; the tag format may have changed"
            )
        return releases

    def list_tags(self, repo: str) -> list[Release]:
        """Every version tag. The Tags API has no dates and no prerelease
        flag, so a lettered suffix in the version marks a prerelease."""
        raw = self._list_all(f"/repos/{repo}/tags")
        tags = []
        for item in raw:
            version = version_from_tag(item["name"])
            if version is None:
                continue
            tags.append(
                Release(
                    version=version,
                    tag=item["name"],
                    prerelease=is_prerelease_version(version),
                    published_at=None,
                    url=f"https://github.com/{repo}/tree/{item['name']}",
                )
            )
        if raw and not tags:
            raise WatchError(
                f"{repo}: none of its {len(raw)} tags is a version; the tag "
                "format may have changed"
            )
        return tags

    def list_feed(self, repo: str, feed: str) -> list[Release]:
        if feed == "tags":
            return self.list_tags(repo)
        if feed == "releases":
            return self.list_releases(repo)
        raise WatchError(f"unknown feed '{feed}' for {repo}")

    def list_watch_issues(self, repo: str) -> list[WatchIssue]:
        """Every ``firmware-watch`` issue, open and closed."""
        raw = self._list_all(
            f"/repos/{repo}/issues?labels={LABEL}&state=all"
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
                        open=item.get("state", "open") == "open",
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


def _when(release: Release) -> str:
    if release.published_at is None:
        return "tagged"
    return f"published {release.published_at.date().isoformat()}"


def issue_body(
    finding: Finding, release: Release, members: list[Finding] | None = None
) -> str:
    """The notice text. ``members`` are the printer lines sharing the
    notice (Prusa Buddy); each line the release covers is listed with
    its own baseline."""
    name = finding.notice_name
    covered = [m for m in (members or [finding]) if m.covers(release.version)]
    if not covered:
        covered = [finding]
    if len(covered) == 1 and covered[0].group == "":
        baselines = f"- Recorded baseline: {covered[0].baseline_description}\n"
        catch_up = "the recorded baseline catches up"
    else:
        baselines = "- Recorded baselines, per watched printer line this release covers:\n" + "".join(
            f"  - {m.firmware}: {m.baseline_description}\n" for m in covered
        )
        catch_up = "every listed line's recorded baseline catches up"
    return (
        f"A newer {name} release exists than the version "
        f"Print Handy last checked its contracts against.\n\n"
        f"- Firmware: {name}\n"
        f"{baselines}"
        f"- Upstream: {release.version} (tag `{release.tag}`), "
        f"{_when(release)}: {release.url}\n\n"
        f"The follow-up is a feature-by-feature audit of {release.version} "
        f"against Print Handy's {name} contracts, in the "
        f"pattern of the ZMod audits (1.7.5 in #562, 1.7.6 in #1269), "
        f"ending with the recorded baseline in {finding.baseline_home} "
        f"moving to {release.version}. "
        f"This notice is not an audit: it says the release exists, "
        f"nothing about what it changes for Print Handy.\n\n"
        f"Notification only: this watch never edits the baseline and "
        f"never opens a bump PR. This issue closes automatically once "
        f"{catch_up} with {release.version}.\n"
    )


def run(root: Path, repo: str, dry_run: bool, client: GitHubClient) -> int:
    watched = read_baselines(root)
    print("Baselines read from this checkout:")
    for entry in watched:
        scope = f", printer line {' / '.join(entry.line)}" if entry.line else ""
        print(
            f"  {entry.name}: {entry.baseline} ({entry.repo} {entry.feed}{scope}; "
            f"recorded in {entry.baseline_home})"
        )

    # Fetch everything before touching any issue: a failure here
    # aborts the run with no issue created or closed from partial
    # data. Prusa Buddy's printer lines share one repository, so each
    # (repository, feed) is fetched once.
    feeds: dict[tuple[str, str], list[Release]] = {}
    for entry in watched:
        key = (entry.repo, entry.feed)
        if key not in feeds:
            feeds[key] = client.list_feed(entry.repo, entry.feed)
    releases = {entry.name: feeds[(entry.repo, entry.feed)] for entry in watched}

    findings = build_findings(watched, releases)
    print("Upstream state:")
    for finding in findings:
        latest = finding.latest_release
        state = (
            f"latest final release {latest.version} ({_when(latest)})"
            if latest is not None
            else "no final releases found"
        )
        marker = (
            f" -> NEWER than the baseline: {finding.newer_release.version}"
            if finding.newer_release is not None
            else ""
        )
        print(
            f"  {finding.firmware}: baseline {finding.baseline}; "
            f"{state}{marker}"
        )

    open_issues: list[WatchIssue] = []
    issues_known = True
    try:
        open_issues = client.list_watch_issues(repo)
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
    members_by_notice: dict[str, list[Finding]] = {}
    for finding in findings:
        members_by_notice.setdefault(finding.notice_name, []).append(finding)
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
                    issue_body(
                        members_by_notice[action.firmware][0],
                        action.release,
                        members_by_notice[action.firmware],
                    ),
                )
                print(f"  Created issue #{number}: {title}")
        else:
            comment = (
                f"The recorded baseline for {action.firmware} has caught "
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
