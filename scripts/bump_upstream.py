#!/usr/bin/env python3
"""Open draft bump PRs for manually pinned upstream tools.

Phase A covers the two Tier 1 components whose bumps are fully
mechanical:

- go2rtc (AlexxIT/go2rtc): version, tag-commit revision and per-arch
  SHA256s in docker/go2rtc-prebuilt.sh.
- Spoolman (Donkie/Spoolman): the commit pin in the Dockerfile's
  SPOOLMAN_REF argument.

The script is run by .github/workflows/upstream-bump.yml with a
GitHub App installation token (the print-handy-upstream-bot app), so
the PRs it opens trigger CI like a human's. With no identity
configured it is deliberately inert: --check-identity reports
configured=false and write mode exits 0 without changing anything.

A bump moves every surface that records the pin in the same commit
- for go2rtc the container contract test and three doc records,
for Spoolman the SETTINGS.md row and the DOCKER.md build sentence -
exactly like a hand-run bump; if a recorded surface does not hold
the value the bump expects, planning fails for a human instead of
guessing. Historical records that name old versions on purpose
(acceptance reports, migration notes) are never rewritten.

A bump PR is opened as a draft; .github/workflows/
upstream-bump-ready.yml flips it to ready once its CI suite is
green (see --mark-ready). Nothing here ever merges a PR.

Only the Python standard library is used. Run with --dry-run to see
the pins read, the upstream state and the intended actions without
changing anything (read-only; needs no identity).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

API_ROOT = "https://api.github.com"
DEFAULT_REPO = "catduckgnaf/print-handy"
MIN_RELEASE_AGE_DAYS = 3
LABELS = ["dependencies", "upstream-bump"]
BOT_NAME = "print-handy-upstream-bot[bot]"
BOT_EMAIL = "print-handy-upstream-bot[bot]@users.noreply.github.com"
INERT_NOTICE = (
    "upstream bot identity not configured - see the setup checklist "
    "(secrets UPSTREAM_BOT_APP_ID / UPSTREAM_BOT_PRIVATE_KEY); "
    "the bump automation is inert and nothing was changed."
)

GO2RTC_REPO = "AlexxIT/go2rtc"
GO2RTC_FILE = "docker/go2rtc-prebuilt.sh"
SPOOLMAN_REPO = "Donkie/Spoolman"
SPOOLMAN_FILE = "Dockerfile"


class BumpError(Exception):
    """A component-level failure; logged, isolated, never fatal to peers."""


# --------------------------------------------------------------------------
# Version ordering (prerelease-aware: alpha < beta < rc < final)
# --------------------------------------------------------------------------

_PRERELEASE_RANK = {"alpha": 0, "beta": 1, "rc": 2}


def version_key(text):
    """Return a comparable key for versions like 1.9.15 or 2.5.0-rc."""
    text = str(text).strip().lstrip("vV")
    core, _, pre = text.partition("-")
    nums = []
    for part in core.split("."):
        match = re.match(r"\d+", part)
        nums.append(int(match.group()) if match else 0)
    while len(nums) < 3:
        nums.append(0)
    if not pre:
        rank, num = 3, 0
    else:
        match = re.match(r"([A-Za-z]+)\.?(\d+)?", pre)
        if match:
            rank = _PRERELEASE_RANK.get(match.group(1).lower(), 0)
            num = int(match.group(2)) if match.group(2) else 0
        else:
            rank, num = 0, 0
    return (tuple(nums), rank, num)


def is_newer(candidate, current):
    return version_key(candidate) > version_key(current)


# --------------------------------------------------------------------------
# Identity (the inert-until-configured gate)
# --------------------------------------------------------------------------


def identity_configured(env):
    """True only when both upstream-bot secrets are present."""
    return bool(env.get("UPSTREAM_BOT_APP_ID")) and bool(
        env.get("UPSTREAM_BOT_PRIVATE_KEY")
    )


def check_identity(env, out, err):
    """Print configured=true|false for $GITHUB_OUTPUT; notice on stderr."""
    configured = identity_configured(env)
    if not configured:
        print(INERT_NOTICE, file=err)
    print(f"configured={'true' if configured else 'false'}", file=out)
    return 0


# --------------------------------------------------------------------------
# GitHub API (stdlib transport; injectable for tests)
# --------------------------------------------------------------------------


@dataclass
class Release:
    tag: str
    version: str
    name: str
    published_at: datetime
    html_url: str
    assets: list = field(default_factory=list)

    def asset(self, name):
        for asset in self.assets:
            if asset.get("name") == name:
                return asset
        return None


def _parse_time(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


class GitHubApi:
    def __init__(self, token=None, opener=None):
        self.token = token
        self.opener = opener or urllib.request.urlopen

    def request(self, method, path, payload=None):
        url = path if path.startswith("http") else API_ROOT + path
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "print-handy-bump-upstream",
        }
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener(req) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            raise BumpError(f"GitHub API {method} {path} failed: HTTP {exc.code}")
        if not body:
            return None
        return json.loads(body)

    def download(self, url):
        req = urllib.request.Request(
            url, headers={"User-Agent": "print-handy-bump-upstream"}
        )
        with self.opener(req) as resp:
            return resp.read()

    def get_latest_release(self, repo):
        """The newest published non-draft, non-prerelease release, or None."""
        releases = self.request("GET", f"/repos/{repo}/releases?per_page=30")
        finals = [
            r for r in releases or [] if not r.get("draft") and not r.get("prerelease")
        ]
        if not finals:
            return None
        latest = max(finals, key=lambda r: r.get("published_at") or "")
        return Release(
            tag=latest["tag_name"],
            version=latest["tag_name"].lstrip("vV"),
            name=latest.get("name") or latest["tag_name"],
            published_at=_parse_time(latest["published_at"]),
            html_url=latest["html_url"],
            assets=latest.get("assets") or [],
        )

    def resolve_tag_commit(self, repo, tag):
        """Full commit SHA a tag points at, dereferencing annotated tags."""
        obj = self.request("GET", f"/repos/{repo}/git/ref/tags/{tag}")["object"]
        hops = 0
        while obj.get("type") == "tag":
            hops += 1
            if hops > 5:
                raise BumpError(f"tag {tag} dereferences too many levels")
            obj = self.request("GET", f"/repos/{repo}/git/tags/{obj['sha']}")["object"]
        if obj.get("type") != "commit":
            raise BumpError(f"tag {tag} resolves to a {obj.get('type')}, not a commit")
        return obj["sha"]

    def compare_status(self, repo, base, head):
        """GitHub compare status of head relative to base."""
        data = self.request("GET", f"/repos/{repo}/compare/{base}...{head}")
        return (data or {}).get("status")

    def list_open_prs(self, repo):
        return self.request("GET", f"/repos/{repo}/pulls?state=open&per_page=100") or []

    def find_open_pr_for_branch(self, repo, branch):
        for pr in self.list_open_prs(repo):
            if (pr.get("head") or {}).get("ref") == branch:
                return pr
        return None

    def create_pr(self, repo, title, head, base, body):
        return self.request(
            "POST",
            f"/repos/{repo}/pulls",
            {"title": title, "head": head, "base": base, "body": body, "draft": True},
        )

    def add_labels(self, repo, number, labels):
        return self.request(
            "POST", f"/repos/{repo}/issues/{number}/labels", {"labels": labels}
        )

    def comment(self, repo, number, body):
        return self.request(
            "POST", f"/repos/{repo}/issues/{number}/comments", {"body": body}
        )

    def close_pr(self, repo, number):
        return self.request(
            "PATCH", f"/repos/{repo}/pulls/{number}", {"state": "closed"}
        )

    def check_runs_for_ref(self, repo, ref):
        data = self.request(
            "GET", f"/repos/{repo}/commits/{ref}/check-runs?per_page=100"
        )
        return (data or {}).get("check_runs") or []

    def mark_ready(self, node_id):
        mutation = (
            "mutation($id: ID!) { markPullRequestReadyForReview("
            "input: {pullRequestId: $id}) { pullRequest { number } } }"
        )
        return self.request(
            "POST", "/graphql", {"query": mutation, "variables": {"id": node_id}}
        )


# --------------------------------------------------------------------------
# Pin surfaces
# --------------------------------------------------------------------------


@dataclass
class Go2rtcPin:
    version: str
    revision: str
    sha256_amd64: str
    sha256_arm64: str
    asset_amd64: str
    asset_arm64: str


_GO2RTC_KEYS = ("version", "revision", "sha256_amd64", "sha256_arm64", "asset_amd64", "asset_arm64")


def parse_go2rtc(text):
    values = {}
    for key in _GO2RTC_KEYS:
        match = re.search(rf"^{key}=(.+)$", text, flags=re.MULTILINE)
        if not match:
            raise BumpError(f"go2rtc pin file is missing {key}=")
        values[key] = match.group(1).strip()
    return Go2rtcPin(**values)


def render_go2rtc(text, pin):
    for key in _GO2RTC_KEYS:
        text, count = re.subn(
            rf"^{key}=.*$",
            f"{key}={getattr(pin, key)}",
            text,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise BumpError(f"go2rtc pin file has no {key}= line to update")
    return text


_SPOOLMAN_RE = re.compile(r"^ARG SPOOLMAN_REF=([0-9a-f]{40})$", flags=re.MULTILINE)


def parse_spoolman(text):
    match = _SPOOLMAN_RE.search(text)
    if not match:
        raise BumpError("Dockerfile is missing the ARG SPOOLMAN_REF pin")
    return match.group(1)


def render_spoolman(text, ref):
    new_text, count = _SPOOLMAN_RE.subn(f"ARG SPOOLMAN_REF={ref}", text)
    if count != 1:
        raise BumpError("Dockerfile has no SPOOLMAN_REF line to update")
    return new_text


# --------------------------------------------------------------------------
# Bump planning
# --------------------------------------------------------------------------


GO2RTC_GUARD_TEST = "tests/test_go2rtc_container.py"
DOC_COMPONENTS = "docs/COMPONENTS.md"
DOC_DOCKER = "docs/DOCKER.md"
DOC_SETTINGS = "docs/SETTINGS.md"


def replace_exact(text, old, new, what, count=1):
    """Replace an exact recorded value; a missing or multiplied match
    means the surface drifted from what the bump expects, which is a
    human's problem, not something to guess around."""
    found = text.count(old)
    if found != count:
        raise BumpError(f"{what}: expected {count} occurrence(s) of {old!r}, found {found}")
    return text.replace(old, new)


@dataclass
class Plan:
    component: str
    action: str  # "bump" plans carry the fields below; others are skips
    version: str = ""
    reason: str = ""
    files: list = field(default_factory=list)
    provenance: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    release: Release | None = None
    edits: list = field(default_factory=list)  # [(relative path, new content)]

    def apply(self, root):
        for relpath, content in self.edits:
            (root / relpath).write_text(content)


def _age_gate(release, now):
    """A final release younger than MIN_RELEASE_AGE_DAYS waits."""
    age_days = (now - release.published_at).total_seconds() / 86400.0
    return age_days >= MIN_RELEASE_AGE_DAYS, age_days


def _asset_sha256(api, release, asset_name):
    """(sha256, provenance) from the asset digest field, else a download."""
    asset = release.asset(asset_name)
    if asset is None:
        raise BumpError(f"release {release.tag} has no asset named {asset_name}")
    digest = (asset.get("digest") or "").strip()
    if digest.startswith("sha256:") and len(digest) == len("sha256:") + 64:
        return digest.split(":", 1)[1], "GitHub release asset digest field"
    data = api.download(asset["browser_download_url"])
    return hashlib.sha256(data).hexdigest(), "downloaded asset, hashed locally"


def plan_go2rtc(root, api, now):
    text = (root / GO2RTC_FILE).read_text()
    pin = parse_go2rtc(text)
    release = api.get_latest_release(GO2RTC_REPO)
    if release is None:
        raise BumpError("go2rtc has no published final release")
    if not is_newer(release.version, pin.version):
        return Plan("go2rtc", "current", version=pin.version,
                    reason=f"pin {pin.version} is at or ahead of the latest final release {release.version}")
    ok, age = _age_gate(release, now)
    if not ok:
        return Plan("go2rtc", "too-young", version=release.version,
                    reason=f"release is {age:.1f} days old; the gate is {MIN_RELEASE_AGE_DAYS} days")
    commit = api.resolve_tag_commit(GO2RTC_REPO, release.tag)
    revision = commit[: len(pin.revision)]
    sha_amd64, prov_amd64 = _asset_sha256(api, release, pin.asset_amd64)
    sha_arm64, prov_arm64 = _asset_sha256(api, release, pin.asset_arm64)
    new_pin = Go2rtcPin(
        version=release.version,
        revision=revision,
        sha256_amd64=sha_amd64,
        sha256_arm64=sha_arm64,
        asset_amd64=pin.asset_amd64,
        asset_arm64=pin.asset_arm64,
    )
    provenance = [
        f"Release: {release.html_url} (published {release.published_at.date().isoformat()})",
        f"revision {revision}: first {len(pin.revision)} characters of the tag commit {commit}",
        f"sha256_amd64 ({pin.asset_amd64}): {prov_amd64}",
        f"sha256_arm64 ({pin.asset_arm64}): {prov_arm64}",
    ]
    changes = [
        f"version: {pin.version} -> {release.version}",
        f"revision: {pin.revision} -> {revision}",
        f"sha256_amd64: {pin.sha256_amd64} -> {sha_amd64}",
        f"sha256_arm64: {pin.sha256_arm64} -> {sha_arm64}",
    ]
    # The pin is recorded on more surfaces than the build script: the
    # container contract test pins the exact table, and three docs
    # name the version. A bump moves every recorded surface in the
    # same commit, exactly like a hand-run bump - a stale guard test
    # would fail the bump PR's own CI, and stale docs would drift.
    guard = (root / GO2RTC_GUARD_TEST).read_text()
    guard = replace_exact(guard, f"version={pin.version}", f"version={release.version}",
                          "go2rtc guard test version")
    guard = replace_exact(guard, f'AMD64_SHA = "{pin.sha256_amd64}"', f'AMD64_SHA = "{sha_amd64}"',
                          "go2rtc guard test amd64 digest")
    guard = replace_exact(guard, f'ARM64_SHA = "{pin.sha256_arm64}"', f'ARM64_SHA = "{sha_arm64}"',
                          "go2rtc guard test arm64 digest")
    components = replace_exact(
        (root / DOC_COMPONENTS).read_text(),
        f"`{pin.version}`, revision `{pin.revision}`",
        f"`{release.version}`, revision `{revision}`",
        "docs/COMPONENTS.md go2rtc row",
    )
    docker_doc = replace_exact(
        (root / DOC_DOCKER).read_text(),
        f"go2rtc v{pin.version}", f"go2rtc v{release.version}",
        "docs/DOCKER.md go2rtc version",
    )
    settings_doc = replace_exact(
        (root / DOC_SETTINGS).read_text(),
        f"(v{pin.version},", f"(v{release.version},",
        "docs/SETTINGS.md go2rtc version",
    )
    edits = [
        (GO2RTC_FILE, render_go2rtc(text, new_pin)),
        (GO2RTC_GUARD_TEST, guard),
        (DOC_COMPONENTS, components),
        (DOC_DOCKER, docker_doc),
        (DOC_SETTINGS, settings_doc),
    ]
    changes.append("guard test (tests/test_go2rtc_container.py) and the COMPONENTS/DOCKER/SETTINGS doc records updated to the same values")
    return Plan("go2rtc", "bump", version=release.version,
                files=[relpath for relpath, _ in edits],
                provenance=provenance, changes=changes, release=release, edits=edits)


_SPOOLMAN_SETTINGS_RE = re.compile(r"Pinned Spoolman v(\d+\.\d+\.\d+) source commit")


def _spoolman_recorded_version(root, pin_ref):
    """The Spoolman version the docs record for the pinned ref.

    The pin itself is a bare commit, so the current version is read
    from the SETTINGS.md row and cross-checked against the DOCKER.md
    build sentence; if the recorded surfaces disagree with each
    other or with the Dockerfile, the bump stops for a human.
    """
    settings = (root / DOC_SETTINGS).read_text()
    match = _SPOOLMAN_SETTINGS_RE.search(settings)
    if not match:
        raise BumpError("docs/SETTINGS.md has no 'Pinned Spoolman vX.Y.Z source commit' record")
    version = match.group(1)
    if pin_ref not in settings:
        raise BumpError("docs/SETTINGS.md does not record the Dockerfile's SPOOLMAN_REF")
    docker_doc = (root / DOC_DOCKER).read_text()
    sentence = f"Spoolman {version} is built from exact upstream revision `{pin_ref}`"
    if sentence not in docker_doc:
        raise BumpError("docs/DOCKER.md's Spoolman build sentence does not match the SETTINGS.md record and the Dockerfile pin")
    return version


def plan_spoolman(root, api, now):
    text = (root / SPOOLMAN_FILE).read_text()
    pin_ref = parse_spoolman(text)
    release = api.get_latest_release(SPOOLMAN_REPO)
    if release is None:
        raise BumpError("Spoolman has no published final release")
    commit = api.resolve_tag_commit(SPOOLMAN_REPO, release.tag)
    if commit == pin_ref:
        return Plan("spoolman", "current", version=release.version,
                    reason=f"pin already resolves to {release.tag}")
    status = api.compare_status(SPOOLMAN_REPO, pin_ref, commit)
    if status != "ahead":
        return Plan("spoolman", "not-behind", version=release.version,
                    reason=f"compare of the pin against {release.tag} is {status!r}, not 'ahead'; the pin is not behind the latest release")
    ok, age = _age_gate(release, now)
    if not ok:
        return Plan("spoolman", "too-young", version=release.version,
                    reason=f"release is {age:.1f} days old; the gate is {MIN_RELEASE_AGE_DAYS} days")
    old_version = _spoolman_recorded_version(root, pin_ref)
    provenance = [
        f"Release: {release.html_url} (published {release.published_at.date().isoformat()})",
        f"SPOOLMAN_REF: the full commit {release.tag} resolves to, {commit}",
        "The Dockerfile build fetches and verifies this exact ref, so CI is the judge of the bump.",
    ]
    changes = [f"SPOOLMAN_REF: {pin_ref} -> {commit}"]
    # The recorded version/ref also lives in docs/SETTINGS.md and in
    # docs/DOCKER.md's build sentence; both move in the same commit.
    # (Historical records - acceptance reports, migration notes for
    # old databases - name old versions on purpose and stay.)
    settings = (root / DOC_SETTINGS).read_text()
    settings = replace_exact(settings, f"`{pin_ref}`", f"`{commit}`",
                             "docs/SETTINGS.md SPOOLMAN_REF row")
    settings = replace_exact(settings, f"Pinned Spoolman v{old_version} source commit",
                             f"Pinned Spoolman v{release.version} source commit",
                             "docs/SETTINGS.md Spoolman version")
    docker_doc = (root / DOC_DOCKER).read_text()
    docker_doc = replace_exact(
        docker_doc,
        f"Spoolman {old_version} is built from exact upstream revision `{pin_ref}`",
        f"Spoolman {release.version} is built from exact upstream revision `{commit}`",
        "docs/DOCKER.md Spoolman build sentence",
    )
    edits = [
        (SPOOLMAN_FILE, render_spoolman(text, commit)),
        (DOC_SETTINGS, settings),
        (DOC_DOCKER, docker_doc),
    ]
    changes.append("the SETTINGS.md row and DOCKER.md build sentence updated to the same version and ref")
    return Plan("spoolman", "bump", version=release.version,
                files=[relpath for relpath, _ in edits],
                provenance=provenance, changes=changes, release=release, edits=edits)


PLANNERS = {"go2rtc": plan_go2rtc, "spoolman": plan_spoolman}


# --------------------------------------------------------------------------
# Dedupe / supersede decisions
# --------------------------------------------------------------------------


def branch_for(component, version):
    return f"upstream/{component}-{version}"


def _pr_branch(pr):
    return (pr.get("head") or {}).get("ref") or ""


def _pr_labels(pr):
    return {label.get("name") for label in pr.get("labels") or []}


def decide_existing(open_prs, component, version):
    """Return ('dedupe', pr) | ('supersede', [pr, ...]) | ('fresh', []).

    Only PRs carrying the upstream-bump label on upstream/<component>-*
    branches count; anything else is a human's PR and is never touched.
    """
    target = branch_for(component, version)
    prefix = f"upstream/{component}-"
    superseded = []
    for pr in open_prs:
        branch = _pr_branch(pr)
        if "upstream-bump" not in _pr_labels(pr):
            continue
        if branch == target:
            return ("dedupe", pr)
        if branch.startswith(prefix):
            other = branch[len(prefix):]
            if is_newer(version, other):
                superseded.append(pr)
    if superseded:
        return ("supersede", superseded)
    return ("fresh", [])


# --------------------------------------------------------------------------
# PR body (repo template shape)
# --------------------------------------------------------------------------


def build_pr_body(component, plan):
    changes = "\n".join(f"- `{line}`" for line in plan.changes)
    provenance = "\n".join(f"- {line}" for line in plan.provenance)
    files = ", ".join(f"`{name}`" for name in plan.files)
    return f"""## Summary and user impact

Automated bump of {component} to {plan.version}, opened by the upstream bump automation (scripts/bump_upstream.py) because a newer final upstream release exists and the repository pin is behind it. Merging stays a human decision; this PR never merges itself.

Upstream release: {plan.release.html_url} (published {plan.release.published_at.date().isoformat()}).

## Problem and cause

Not applicable - this is routine dependency maintenance, not a bug fix. The pin is behind the latest final upstream release; see the provenance below.

## Proposed changes

- {files} updated:
{changes}

No other files change. Confirmation, readiness, profile, clearance and artifact guards are untouched.

## Type of change

- [ ] Bug fix
- [ ] User facing feature
- [x] Maintenance or refactor
- [ ] Documentation
- [ ] CI or tests
- [ ] Breaking change

## Verification and test results

- Checks run: the bump script verified every new value against the upstream release before opening this PR:
{provenance}
- Checks not run and why: this PR's own CI suite is pending at open time (the PR was opened as a draft for exactly that reason). It is marked ready automatically once every check on its head is SUCCESS; a red suite stays draft and is the honest verdict on the bump.

## Risks, limitations, and follow up

The bump is exactly the upstream release named above; if its suite fails, close this PR - the next weekly run will propose the next release. Rollback is reverting this PR.

## Related issues

- Related: None
- Fixes: None

## Scope and acceptance criteria

- Acceptance criteria: the pin diff above is the whole change, and CI passes on the PR head.
- Out of scope / remaining work: any behavior change in the upstream tool itself; other pinned components are bumped by their own PRs.

## User-facing release note

Not applicable - internal dependency pin; no operator-facing behavior is intended to change.

## Safety, compatibility and deployment

- Safety/compatibility impact: None identified; no guard, profile or confirmation surface changes.
- Live checks or deployments performed (or "None"): None

## Documentation and attribution

- [x] Relevant setup, configuration and user documentation updated, or reason given below.
- [x] Contributor credit and third-party licenses preserved where applicable; no invented coauthors.
- [x] No credentials, private model files or personal runtime records included.

## Documentation impact

Documentation: not needed
Reason: a pin value change in an existing build surface; no setup, configuration or user documentation describes the pinned version.
Wiki: not applicable
Wiki reason: versioned docs cover the build surfaces; no wiki page tracks pinned dependency versions.

## Surfaces and variables

Variables: not applicable
Variables reason: no Print Handy configuration or settings variable is added or changed; the pinned upstream version is a build-time value in {files}, not an application setting, so the defaults tree, environment loader, Web UI catalog, SETTINGS.md and Unraid templates do not apply.

## Reviewer checklist

- [ ] User-visible behavior, configuration and limitations match the docs and release note.
- [ ] Documentation impact and wiki impact were reviewed, including any exemption or deferral.
- [ ] Tests cover the changed behavior; reported checks match actual evidence.
- [ ] Safety/authorization guards and contributor attribution are preserved.
- [ ] Behavior changes were checked against every adapter/connection in their scope (the printer-scope rule: a change for one printer family is verified for all families it touches, not only the one at hand).
- [ ] Registry/data changes keep their parity and drift tests green, or the exemption list is updated with a tracking reference.
- [ ] Required current-head CI checks pass before merge; no deployment or live action is implied.
"""


# --------------------------------------------------------------------------
# Git + execution
# --------------------------------------------------------------------------


class Git:
    def __init__(self, root):
        self.root = Path(root)

    def run(self, *args):
        result = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True
        )
        if result.returncode != 0:
            raise BumpError(
                f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
            )
        return result.stdout.strip()

    def current_ref(self):
        return self.run("rev-parse", "--abbrev-ref", "HEAD")

    def create_branch(self, branch):
        self.run("switch", "-c", branch)

    def commit_files(self, files, message):
        self.run("config", "user.name", BOT_NAME)
        self.run("config", "user.email", BOT_EMAIL)
        self.run("add", *files)
        self.run("commit", "-m", message)

    def push(self, branch):
        self.run("push", "--set-upstream", "origin", branch)

    def switch(self, ref):
        self.run("switch", ref)


def execute_plan(root, api, repo, plan, dry_run, git=None, log=print):
    """Carry out a 'bump' plan; returns an outcome string."""
    branch = branch_for(plan.component, plan.version)
    title = f"chore(deps): bump {plan.component} to {plan.version}"
    open_prs = api.list_open_prs(repo)
    decision, found = decide_existing(open_prs, plan.component, plan.version)
    if decision == "dedupe":
        log(f"{plan.component}: open PR #{found['number']} already proposes {plan.version}; no-op")
        return "dedupe"
    if dry_run:
        log(f"{plan.component}: WOULD open draft PR '{title}' on branch {branch}")
        for line in plan.changes:
            log(f"  {line}")
        for pr in found if decision == "supersede" else []:
            log(f"  and close superseded PR #{pr['number']} ({_pr_branch(pr)})")
        return "dry-run"
    git = git or Git(root)
    original = git.current_ref()
    try:
        git.create_branch(branch)
        plan.apply(root)
        git.commit_files(plan.files, title)
        git.push(branch)
    finally:
        git.switch(original)
    pr = api.create_pr(repo, title, branch, "main", build_pr_body(plan.component, plan))
    api.add_labels(repo, pr["number"], LABELS)
    log(f"{plan.component}: opened draft PR #{pr['number']} for {plan.version} on {branch}")
    if decision == "supersede":
        for old in found:
            api.comment(
                repo,
                old["number"],
                f"Superseded by #{pr['number']}, which proposes {plan.component} {plan.version}. Closing this one.",
            )
            api.close_pr(repo, old["number"])
            log(f"{plan.component}: closed superseded PR #{old['number']}")
    return "opened"


# --------------------------------------------------------------------------
# Draft -> ready flip (used by upstream-bump-ready.yml)
# --------------------------------------------------------------------------


def latest_checks(check_runs):
    """Latest run per check name, by start time."""
    latest = {}
    for run in check_runs:
        name = run.get("name")
        if name is None:
            continue
        if name not in latest or (run.get("started_at") or "") > (
            latest[name].get("started_at") or ""
        ):
            latest[name] = run
    return latest


def ready_decision(pr, check_runs):
    """Conservative guards for flipping a bump PR from draft to ready."""
    if pr is None:
        return False, "no open PR for the branch"
    if not pr.get("draft"):
        return False, "PR is not a draft"
    if "upstream-bump" not in _pr_labels(pr):
        return False, "PR does not carry the upstream-bump label"
    author = (pr.get("user") or {}).get("login") or ""
    if not author.endswith("[bot]"):
        return False, f"PR author {author!r} is not the bot app"
    latest = latest_checks(check_runs)
    if not latest:
        return False, "no checks recorded for the head"
    failing = [
        name
        for name, run in latest.items()
        if run.get("status") != "completed" or run.get("conclusion") != "success"
    ]
    if failing:
        return False, f"checks not all SUCCESS: {', '.join(sorted(failing))}"
    return True, "label, bot author, draft state and all checks check out"


def mark_ready(root, api, repo, branch, log=print):
    pr = api.find_open_pr_for_branch(repo, branch)
    if pr is None:
        log(f"no open PR for branch {branch}; nothing to do")
        return 0
    checks = api.check_runs_for_ref(repo, pr["head"]["sha"])
    ok, reason = ready_decision(pr, checks)
    if not ok:
        log(f"PR #{pr['number']} stays draft: {reason}")
        return 0
    api.mark_ready(pr["node_id"])
    log(f"PR #{pr['number']} marked ready: {reason}")
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def repo_root():
    return Path(__file__).resolve().parents[1]


def run_components(root, api, repo, components, dry_run, now, log=print):
    """Plan and execute per component; failures are isolated per component."""
    failures = 0
    for component in components:
        try:
            plan = PLANNERS[component](root, api, now)
            if plan.action != "bump":
                log(f"{component}: skipped ({plan.action}) - {plan.reason}")
                continue
            execute_plan(root, api, repo, plan, dry_run, log=log)
        except Exception as exc:  # per-component isolation is the point
            failures += 1
            log(f"{component}: ERROR {exc}")
    return 1 if failures else 0


def main(argv=None, env=None, now=None, out=None, err=None):
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", choices=["all", "go2rtc", "spoolman"], default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repo", default=env.get("GITHUB_REPOSITORY") or DEFAULT_REPO)
    parser.add_argument("--check-identity", action="store_true")
    parser.add_argument("--mark-ready", action="store_true")
    parser.add_argument("--branch", default="")
    args = parser.parse_args(argv)

    if args.check_identity:
        return check_identity(env, out, err)

    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    root = repo_root()
    now = now or datetime.now(timezone.utc)

    if args.mark_ready:
        if not token:
            print(INERT_NOTICE, file=err)
            return 0
        if not args.branch:
            print("--mark-ready needs --branch", file=err)
            return 2
        api = GitHubApi(token)
        return mark_ready(root, api, args.repo, args.branch,
                          log=lambda msg: print(msg, file=out))

    if not args.dry_run and not token:
        # No app token means the identity is not configured in this run;
        # stay inert rather than pushing with the workflow's own token,
        # whose PRs would not trigger CI.
        print(INERT_NOTICE, file=err)
        return 0

    api = GitHubApi(token)
    components = ["go2rtc", "spoolman"] if args.component == "all" else [args.component]
    return run_components(root, api, args.repo, components, args.dry_run, now,
                          log=lambda msg: print(msg, file=out))


if __name__ == "__main__":
    raise SystemExit(main())
