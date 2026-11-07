#!/usr/bin/env python3
"""Open draft bump PRs for manually pinned upstream tools.

Phase A covers the two Tier 1 components whose bumps are fully
mechanical:

- go2rtc (AlexxIT/go2rtc): version, tag-commit revision and per-arch
  SHA256s in docker/go2rtc-prebuilt.sh.
- Spoolman (Donkie/Spoolman): the commit pin in the Dockerfile's
  SPOOLMAN_REF argument.

Phase B adds the Tier 2 component:

- Bambu Studio (bambulab/BambuStudio): the commit pin in the
  Dockerfile's BAMBU_REF argument. A commit pin has no version to
  compare, so a release counts as newer when its publish date is
  after the pinned commit's commit date (the upstream-watch rule).
  The bump also regenerates the HMS error table
  (print_handy/printers/bambulab/hms_data.json) with
  scripts/bambu-hms.py from the new commit's own resources/hms
  files, verified with the generator's --check mode before the
  plan is allowed to commit it; if the upstream layout has moved,
  the bump stops for a human instead of writing a hand-made table.

Phase C adds the Tier 3 component:

- OrcaSlicer (OrcaSlicer/OrcaSlicer): the single bundled build's
  pin in docker/orca-prebuilt.sh (version, tag-commit revision and
  per-arch SHA256s) plus the CI flatpak bundle pin in
  .github/workflows/ci.yml. One channel only (the preview channel
  and the beta pin were removed in #936), so the target is the
  newest release overall - newest of (latest final, latest
  prerelease) under prerelease-aware ordering. Finals wait the
  usual 3 days; prereleases are exempt. A bump also sweeps every
  recorded surface in the same commit and verifies the new
  bundle's vendor profiles against the identities recorded in the
  printer facts: Print Handy ships no vendor profiles (templates
  are generated on the operator's machine from the installed
  bundle), so there is no generated profile data to commit - the
  regeneration proof is the flatten + recorded-identity match the
  scripts/*-templates.py --check mode performs, run for both
  packagings (the packed preset caches from the shipped AppImage
  itself, and the JSON tree from the upstream source tree at the
  same commit - the AppImage ships no JSON tree). Any model whose
  profiles match no recorded identity stops the bump for a human
  sweep; the planner never writes identity data.

The script is run by .github/workflows/upstream-bump.yml with a
GitHub App installation token (the print-handy-upstream-bot app), so
the PRs it opens trigger CI like a human's. With no identity
configured it is deliberately inert: --check-identity reports
configured=false and write mode exits 0 without changing anything.
When both secrets are present, --check-identity also validates
their shape (the identifier must be the numeric App ID or a Client
ID in Iv1.../Iv23... form, and the private key must carry a PEM
PRIVATE KEY header and matching footer): a present-but-malformed
value reports configured=false, names the malformed secret without
ever printing its value, and exits 2, so a wrong-but-nonempty pair
cannot sail through to an opaque API failure. --diagnose-identity
goes further: it builds an app JWT from the configured pair, asks
GitHub which app the pair actually identifies (GET /app), and
whether that app is installed on this repository
(GET /repos/<repo>/installation), printing the named cause - wrong
key or ID, or the app not installed / not granted this repository -
instead of the bare HTTP 404 the token-mint step reports (#950).

A bump moves every surface that records the pin in the same commit
- for go2rtc the container contract test and three doc records,
for Spoolman the SETTINGS.md row and the DOCKER.md build sentence -
exactly like a hand-run bump; if a recorded surface does not hold
the value the bump expects, planning fails for a human instead of
guessing. Historical records that name old versions on purpose
(acceptance reports, migration notes) are never rewritten.

Step size (catduck's policy): a bump whose first or second version
component changes is a MAJOR step; a change confined to later
components is a MINOR step. The comparison uses normalized numeric
tuples, never string order (Bambu's 02.09.01.70 form defeats string
comparison). A minor step's PR keeps the standard body; a major
step's PR gains a "Major step - deep dive required before merge"
section naming the upstream range and the Print Handy surfaces a
human must re-examine against the upstream changes since the last
major version, because targeted regression tests around the touched
surface are not enough for a major step. The Spoolman plan also
moves the test-harness pins (the CI spoolman job's release zip and
scripts/run_spoolman_native.sh) with the shipped pin: the harness
is how the real-server tests run, and a harness left on the old
version would prove the old release while the image ships the new
one.

Components this automation deliberately does NOT bump (enumerated
2026-11-07 at catduck's direction, so the upstream check's coverage
is explicit rather than assumed):

- OpenSCAD and Tesseract OCR: shipped as Debian 13 (trixie) packages
  in the Dockerfile runtime stage, installed from the same distro in
  the CI offline legs. There is no repo-owned release pin for the
  planner to move: their version is the distro's, and moving it means
  moving the base image, a release decision a human makes. The
  forcing function is the parity test
  (tests/test_upstream_version_parity.py), which fails if the two
  install surfaces or their distro bases drift apart. When a distro
  move changes either tool's upstream version, the step-size rule
  above classifies that change like any other bump.
- The native bounds toolchain (Rust 1.99.1 via rustup, maturin
  1.16.0, and the crate's Cargo dependencies locked in
  native/bounds/Cargo.lock): the pins are repo-owned but the planner
  cannot check them the way it checks a GitHub release - there is no
  single upstream release artifact to fetch and verify - so they
  move by hand, and the same parity test fails if the Dockerfile,
  the CI jobs and this enumeration disagree on the values. Cargo
  dependencies move only through Cargo.lock (`maturin build
  --locked` everywhere the wheel is built), never by the planner
  rewriting Cargo.toml.

A bump PR is opened as a draft; .github/workflows/
upstream-bump-ready.yml flips it to ready once its CI suite is
green (see --mark-ready). Nothing here ever merges a PR.

Only the Python standard library is used. Run with --dry-run to see
the pins read, the upstream state and the intended actions without
changing anything (read-only; needs no identity).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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
BAMBU_REPO = "bambulab/BambuStudio"
BAMBU_FILE = "Dockerfile"
BAMBU_URL = "https://github.com/bambulab/BambuStudio.git"
HMS_FILE = "print_handy/printers/bambulab/hms_data.json"
ORCA_REPO = "OrcaSlicer/OrcaSlicer"
ORCA_URL = "https://github.com/OrcaSlicer/OrcaSlicer.git"
ORCA_FILE = "docker/orca-prebuilt.sh"
ORCA_CI_FILE = ".github/workflows/ci.yml"
ORCA_FLATPAK_SHA_TEST = "tests/test_ci_bambu_workflow.py"
ORCA_GUARD_TEST = "tests/test_docker_release.py"
ORCA_CONTRACT_TEST = "tests/test_docker_release_contract.py"
BAMBULAB_FACTS = "print_handy/printers/bambulab/facts.py"
BAMBULAB_P1S_TEST = "tests/printers/test_bambulab_p1s.py"
DOC_BAMBU_A1 = "docs/BAMBU-A1-DOCKER.md"
DOC_ENV_EXAMPLE = ".env.example"
DOC_README = "README.md"
DOC_UNRAID = "docker/unraid/print-handy.xml"
ORCA_VENDOR_DOCS = (
    "docs/ANYCUBIC.md",
    "docs/CREALITY.md",
    "docs/ELEGOO.md",
    "docs/PRUSA.md",
    "docs/QIDI.md",
)
# Printer family package -> the vendor name its OrcaSlicer profiles
# ship under (docker/orca-prebuilt.sh's bundle carries profiles for
# all six; the identity sweep checks every family's recorded models).
ORCA_VENDORS = {
    "bambulab": "BBL",
    "anycubic": "Anycubic",
    "creality": "Creality",
    "elegoo": "Elegoo",
    "prusa": "Prusa",
    "qidi": "Qidi",
}


# Components checked for parity but not auto-bumped (see the module
# docstring for the reasons). The parity tests in
# tests/test_upstream_version_parity.py read this enumeration, so a
# component dropped here without a new home fails the suite.
NON_BUMPED_COMPONENTS = {
    "openscad": {
        "surface": "Debian 13 package in the Dockerfile runtime stage and the CI offline legs",
        "pin": "distro version (unpinned apt install on both surfaces)",
    },
    "tesseract": {
        "surface": "Debian 13 package (tesseract-ocr, with its tesseract-ocr-eng language data) in the Dockerfile runtime stage and the CI offline legs",
        "pin": "distro version (unpinned apt install on both surfaces)",
    },
    "bounds-toolchain": {
        "surface": "Dockerfile bounds-build stage, CI native-bounds job and CI offline legs",
        "pin": "rustup toolchain 1.99.1, maturin==1.16.0, native/bounds/Cargo.lock",
    },
}


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


def numeric_tuple(text):
    """The normalized numeric tuple of a version's core.

    Leading zeros vanish (02.09.01.70 -> (2, 9, 1, 70)) and a
    prerelease suffix is not part of the core, so step size is
    judged on the release line the step moves along, never on
    string order.
    """
    core = str(text).strip().lstrip("vV").partition("-")[0]
    nums = []
    for part in core.split("."):
        match = re.match(r"\d+", part)
        nums.append(int(match.group()) if match else 0)
    return tuple(nums)


def classify_step(old_version, new_version):
    """'major' or 'minor' for a bump from one version to another.

    A change in the first or second numeric component is a major
    step; a change confined to later components is minor. Equal
    cores (a prerelease promotion like 2.5.0-rc -> 2.5.0, or a
    prerelease-to-prerelease move on one core) are minor: the
    release line itself did not move.
    """
    old, new = numeric_tuple(old_version), numeric_tuple(new_version)
    width = max(len(old), len(new), 3)
    old += (0,) * (width - len(old))
    new += (0,) * (width - len(new))
    for index, (before, after) in enumerate(zip(old, new)):
        if before != after:
            return "major" if index < 2 else "minor"
    return "minor"


# Print Handy surfaces a major step's deep dive must re-examine, per
# component: the places the upstream tool's behavior reaches into
# this repo, named in the major-step PR body's scaffold section.
STEP_SURFACE_HINTS = {
    "go2rtc": [
        "the asset names and per-arch digests recorded in docker/go2rtc-prebuilt.sh",
        "the go2rtc container contract test's pinned table",
        "the camera and timelapse consumers of the go2rtc fan-out",
    ],
    "spoolman": [
        "the Spoolman API surface Print Handy calls (spool, filament and field endpoints, the ph_slot extra field)",
        "the test-harness pins this bump moves with the shipped pin (the CI spoolman job and scripts/run_spoolman_native.sh)",
        "Spoolman's migrations against an existing operator database",
        "any new outbound behavior the upstream release adds (update checks, telemetry) against Print Handy's local-first posture",
    ],
    "bambu": [
        "the regenerated print_handy/printers/bambulab/hms_data.json (code counts, per-model override prefixes, any new device prefix and what model it names)",
        "the Bambu Lab printer facts and profiles derived from the Bambu Studio tree",
        "the CI Bambu fallback leg's recorded generation verdict (tests/test_ci_bambu_workflow.py)",
    ],
    "orca": [
        "the identity sweep results in the provenance below, for all six vendors",
        "the printer facts' recorded Orca identities",
        "the CI bundle pin and the flatpak slicer legs",
    ],
}


# --------------------------------------------------------------------------
# Identity (the inert-until-configured gate)
# --------------------------------------------------------------------------


EXPECTED_APP_SLUG = "print-handy-upstream-bot"
_CLIENT_ID_RE = re.compile(r"^Iv[0-9][A-Za-z0-9._-]{6,}$")
_PEM_HEADER_RE = re.compile(r"-----BEGIN (?:([A-Za-z0-9 ]+?) )?PRIVATE KEY-----")


def identity_configured(env):
    """True only when both upstream-bot secrets are present."""
    return bool(env.get("UPSTREAM_BOT_APP_ID")) and bool(
        env.get("UPSTREAM_BOT_PRIVATE_KEY")
    )


def valid_app_identifier(value):
    """Shape check for UPSTREAM_BOT_APP_ID (no value is ever logged).

    GitHub accepts either form as the app JWT issuer and the
    create-github-app-token client-id input accepts either through
    the same code path: the numeric App ID (digits only) or the
    Client ID (Iv1... / Iv23...). Anything else - a slug, a name, a
    pasted token - is malformed.
    """
    value = (value or "").strip()
    if not value:
        return False
    return value.isdigit() or bool(_CLIENT_ID_RE.match(value))


def valid_private_key(value):
    """Shape check for UPSTREAM_BOT_PRIVATE_KEY (shape only).

    Well-formed means a PEM block: a -----BEGIN <label> PRIVATE
    KEY----- header with the matching -----END footer. The key
    material itself is never inspected beyond that and never logged.
    """
    value = value or ""
    match = _PEM_HEADER_RE.search(value)
    if not match:
        return False
    label = match.group(1)
    footer = f"-----END {label} PRIVATE KEY-----" if label else "-----END PRIVATE KEY-----"
    return footer in value


def identity_shape_problems(env):
    """Precise, non-secret errors for present-but-malformed secrets."""
    problems = []
    if not valid_app_identifier(env.get("UPSTREAM_BOT_APP_ID")):
        problems.append(
            "UPSTREAM_BOT_APP_ID is malformed: expected the numeric App ID "
            "(digits only) or a Client ID in Iv1.../Iv23... form. "
            "The secret value itself is not printed."
        )
    if not valid_private_key(env.get("UPSTREAM_BOT_PRIVATE_KEY")):
        problems.append(
            "UPSTREAM_BOT_PRIVATE_KEY is malformed: expected a PEM private "
            "key containing a -----BEGIN ... PRIVATE KEY----- header and a "
            "matching -----END ... PRIVATE KEY----- footer. "
            "The key material is not printed."
        )
    return problems


def check_identity(env, out, err):
    """Print configured=true|false for $GITHUB_OUTPUT; notice on stderr.

    Absent secrets keep the inert behavior (configured=false, exit
    0). Present-but-malformed secrets also print configured=false
    but exit 2 with a precise error naming which value is malformed,
    so the workflow fails loudly instead of minting against a
    wrong-but-nonempty pair and reporting a bare 404 (#950).
    """
    if not identity_configured(env):
        print(INERT_NOTICE, file=err)
        print("configured=false", file=out)
        return 0
    problems = identity_shape_problems(env)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=err)
        print("configured=false", file=out)
        return 2
    print("configured=true", file=out)
    return 0


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _now_epoch(now):
    if now is None:
        return int(time.time())
    if isinstance(now, datetime):
        return int(now.timestamp())
    return int(now)


def _openssl_sign(signing_input, private_key):
    """Default JWT signer: RS256 via openssl, key in a 0600 temp file.

    The key file is deleted immediately after signing, and the key
    material never reaches a log or an argument list.
    """
    fd, path = tempfile.mkstemp(prefix="upstream-bot-key-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(private_key)
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", path],
            input=signing_input,
            capture_output=True,
        )
        if result.returncode != 0:
            raise BumpError(
                "openssl could not sign the app JWT with "
                "UPSTREAM_BOT_PRIVATE_KEY (the key is not usable as an "
                "RSA private key; its material is not printed)"
            )
        return result.stdout
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def build_app_jwt(identifier, private_key, signer=None, now=None):
    """An RS256 app JWT: iss=<identifier>, iat=now-60, exp=now+600."""
    epoch = _now_epoch(now)
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"iss": identifier, "iat": epoch - 60, "exp": epoch + 600}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode())
    ).encode("ascii")
    signature = (signer or _openssl_sign)(signing_input, private_key)
    return signing_input.decode("ascii") + "." + _b64url(signature)


def _http_status(exc):
    match = re.search(r"HTTP (\d{3})", str(exc))
    return int(match.group(1)) if match else None


def diagnose_identity(env, repo, out, err, opener=None, signer=None, now=None):
    """Name the exact identity failure instead of a bare 404 (#950).

    Builds an app JWT from the configured pair, then asks GitHub
    who the pair identifies (GET /app) and whether that app is
    installed on this repository (GET /repos/<repo>/installation).
    Returns 0 when the app is installed on the repo, 3 when the
    installation lookup 404s, 2 for missing/malformed secrets, and
    a nonzero code for authentication or other API failures. Secret
    values and key material are never printed.
    """
    if not identity_configured(env):
        print(INERT_NOTICE, file=err)
        print("diagnosis: the upstream bot identity is not configured.", file=out)
        return 2
    problems = identity_shape_problems(env)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=err)
            print(f"diagnosis: {problem}", file=out)
        return 2
    identifier = env["UPSTREAM_BOT_APP_ID"].strip()
    try:
        jwt_token = build_app_jwt(
            identifier, env["UPSTREAM_BOT_PRIVATE_KEY"], signer=signer, now=now
        )
    except BumpError as exc:
        print(f"diagnosis: {exc}", file=out)
        print(f"error: {exc}", file=err)
        return 4
    api = GitHubApi(token=jwt_token, opener=opener)
    try:
        app = api.request("GET", "/app") or {}
    except BumpError as exc:
        status = _http_status(exc)
        if status == 401:
            message = (
                "diagnosis: GitHub rejected the app JWT (HTTP 401): the "
                "private key does not belong to the identifier - "
                "UPSTREAM_BOT_PRIVATE_KEY is not the private key for "
                "UPSTREAM_BOT_APP_ID (wrong key or wrong ID). Regenerate "
                "the key on the app's settings page and store the pair "
                "from the same app."
            )
            print(message, file=out)
            print(message, file=err)
            return 4
        message = f"diagnosis: GET /app failed: HTTP {status}" if status else f"diagnosis: GET /app failed: {exc}"
        print(message, file=out)
        print(message, file=err)
        return 5
    slug = app.get("slug") or ""
    owner = (app.get("owner") or {}).get("login") or ""
    print(
        f"app: id={app.get('id')} slug={slug} name={app.get('name')} owner={owner}",
        file=out,
    )
    if slug != EXPECTED_APP_SLUG:
        print(
            f"diagnosis: the configured secrets identify the app '{slug}', "
            f"not '{EXPECTED_APP_SLUG}' - the secret pair belongs to a "
            "differently-named app.",
            file=out,
        )
    try:
        installation = api.request("GET", f"/repos/{repo}/installation") or {}
    except BumpError as exc:
        status = _http_status(exc)
        if status == 404:
            lines = [
                f"diagnosis: the app '{slug}' is not installed on {repo} "
                "(GET /repos/"
                f"{repo}/installation returned HTTP 404). Two candidate causes:",
                f"1. The app named {slug} is not installed on this repository: "
                f"install it, checking Settings -> Installations -> {slug} -> "
                "Repository access includes this repository.",
                "2. A different app is the installed one: if the app installed "
                "on this repository is not "
                f"{slug}, the secrets belong to the wrong app - "
                "UPSTREAM_BOT_APP_ID / UPSTREAM_BOT_PRIVATE_KEY must be the "
                f"credentials of the installed app ({slug} is the app the "
                "current secrets identify).",
            ]
            for line in lines:
                print(line, file=out)
            print(lines[0], file=err)
            return 3
        message = (
            f"diagnosis: GET /repos/{repo}/installation failed: HTTP {status}"
            if status
            else f"diagnosis: GET /repos/{repo}/installation failed: {exc}"
        )
        print(message, file=out)
        print(message, file=err)
        return 5
    print(
        f"installation: id={installation.get('id')} "
        f"repository_selection={installation.get('repository_selection')}",
        file=out,
    )
    print(f"diagnosis: '{slug}' is installed on {repo}; the identity is healthy.", file=out)
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
    prerelease: bool = False

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

    def list_releases(self, repo):
        """All published non-draft releases, prereleases included.

        The API returns releases newest-first, so the first page
        always holds the newest release by version; callers pick
        their target by version ordering, not by list position.
        """
        releases = self.request("GET", f"/repos/{repo}/releases?per_page=100")
        out = []
        for entry in releases or []:
            if entry.get("draft"):
                continue
            out.append(
                Release(
                    tag=entry["tag_name"],
                    version=entry["tag_name"].lstrip("vV"),
                    name=entry.get("name") or entry["tag_name"],
                    published_at=_parse_time(entry["published_at"]),
                    html_url=entry["html_url"],
                    assets=entry.get("assets") or [],
                    prerelease=bool(entry.get("prerelease")),
                )
            )
        return out

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

    def get_commit_date(self, repo, ref):
        """The committer date of a commit, as the watch script reads it."""
        data = self.request("GET", f"/repos/{repo}/commits/{ref}")
        return _parse_time(data["commit"]["committer"]["date"])

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


_BAMBU_RE = re.compile(r"^ARG BAMBU_REF=([0-9a-f]{40})$", flags=re.MULTILINE)


def parse_bambu(text):
    match = _BAMBU_RE.search(text)
    if not match:
        raise BumpError("Dockerfile is missing the ARG BAMBU_REF pin")
    return match.group(1)


def render_bambu(text, ref):
    new_text, count = _BAMBU_RE.subn(f"ARG BAMBU_REF={ref}", text)
    if count != 1:
        raise BumpError("Dockerfile has no BAMBU_REF line to update")
    return new_text


@dataclass
class OrcaPin:
    version: str
    revision: str
    sha256_amd64: str
    sha256_arm64: str
    asset_amd64: str
    asset_arm64: str


_ORCA_KEYS = ("version", "revision", "sha256_amd64", "sha256_arm64", "asset_amd64", "asset_arm64")
_ORCA_BLOCK_RE = re.compile(r"stable\)\n(.*?)\n[ \t]*;;", flags=re.S)


def _orca_block(text):
    """The single stable) case block of orca-prebuilt.sh.

    Print Handy ships one bundled OrcaSlicer (#936), so the pin
    lives in exactly one block; the watch script's parser reads it
    the same way. A missing block fails closed - the parser exists
    to notice pin-file reshapes, not to guess past them.
    """
    match = _ORCA_BLOCK_RE.search(text)
    if not match:
        raise BumpError("orca-prebuilt.sh has no stable) pin block")
    return match


def parse_orca(text):
    block = _orca_block(text).group(1)
    values = {}
    for key in _ORCA_KEYS:
        match = re.search(rf"^[ \t]*{key}=(\S+)[ \t]*$", block, flags=re.MULTILINE)
        if not match:
            raise BumpError(f"orca pin block is missing {key}=")
        values[key] = match.group(1)
    return OrcaPin(**values)


def render_orca(text, pin):
    match = _orca_block(text)
    block = match.group(1)
    for key in _ORCA_KEYS:
        value = getattr(pin, key)
        block, count = re.subn(
            rf"^([ \t]*{key}=).*$",
            lambda m, v=value: m.group(1) + v,
            block,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise BumpError(f"orca pin block has no {key}= line to update")
    return text[: match.start(1)] + block + text[match.end(1):]


_ORCA_CI_VERSION_RE = re.compile(r"^  ORCA_BUNDLE_VERSION: (\S+)$", flags=re.MULTILINE)
_ORCA_CI_SHA_RE = re.compile(r"^  ORCA_BUNDLE_SHA256: ([0-9a-f]{64})$", flags=re.MULTILINE)


def parse_orca_ci(text):
    """(version, flatpak sha256) of the CI bundle pin in ci.yml.

    CI runs the slicer legs against the same bundled build the
    image ships, installed from the release's x86_64 flatpak bundle
    (scripts/install-orca-flatpak.sh reads these env values); the
    bump moves this pin with the prebuilt pin, in the same commit.
    """
    version = _ORCA_CI_VERSION_RE.search(text)
    sha256 = _ORCA_CI_SHA_RE.search(text)
    if not version or not sha256:
        raise BumpError("ci.yml is missing the ORCA_BUNDLE_VERSION / ORCA_BUNDLE_SHA256 env pin")
    return version.group(1), sha256.group(1)


# --------------------------------------------------------------------------
# Bump planning
# --------------------------------------------------------------------------


GO2RTC_GUARD_TEST = "tests/test_go2rtc_container.py"
DOC_COMPONENTS = "docs/COMPONENTS.md"
DOC_DOCKER = "docs/DOCKER.md"
DOC_SETTINGS = "docs/SETTINGS.md"
DOC_BAMBU = "docs/BAMBU-CONNECTIVITY.md"


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
    old_version: str = ""  # the version being replaced, for step classification
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
    return Plan("go2rtc", "bump", version=release.version, old_version=pin.version,
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


SPOOLMAN_CI_FILE = ".github/workflows/ci.yml"
SPOOLMAN_NATIVE_SCRIPT = "scripts/run_spoolman_native.sh"
SPOOLMAN_ZIP_ASSET = "spoolman.zip"
_SPOOLMAN_ZIP_URL_RE = re.compile(
    r"releases/download/v(\d+\.\d+\.\d+)/spoolman\.zip"
)
_SPOOLMAN_ZIP_SHA_RE = re.compile(r"echo '([0-9a-f]{64})  '\"\$archive\"")


def _spoolman_harness_edits(root, api, release, old_version):
    """(edits, changes, provenance) moving the test-harness pins.

    The real-server tests run against a Spoolman the harness itself
    installs: the CI spoolman job downloads a release zip and checks
    its SHA256, and scripts/run_spoolman_native.sh does the same for
    local runs. Both are recorded surfaces of the shipped pin - if
    they stay on the old version, the suite proves the old release
    while the image ships the new one. A harness pin that does not
    record the version being replaced is drift, and planning stops
    for a human instead of guessing which surface is right.
    """
    zip_sha, zip_prov = _asset_sha256(api, release, SPOOLMAN_ZIP_ASSET)
    ci_text = (root / SPOOLMAN_CI_FILE).read_text()
    ci_match = _SPOOLMAN_ZIP_URL_RE.search(ci_text)
    if not ci_match:
        raise BumpError("ci.yml's spoolman job has no release-zip download to move")
    if ci_match.group(1) != old_version:
        raise BumpError(
            f"ci.yml's spoolman harness pins v{ci_match.group(1)}, not the recorded "
            f"pin version {old_version}; the harness drifted from the shipped pin"
        )
    ci_sha_match = _SPOOLMAN_ZIP_SHA_RE.search(ci_text)
    if not ci_sha_match:
        raise BumpError("ci.yml's spoolman job has no zip SHA256 check to move")
    ci_text = replace_exact(
        ci_text, f"download/v{old_version}/spoolman.zip",
        f"download/v{release.version}/spoolman.zip", "ci.yml spoolman harness version")
    ci_text = replace_exact(
        ci_text, f"echo '{ci_sha_match.group(1)}  '\"$archive\"",
        f"echo '{zip_sha}  '\"$archive\"", "ci.yml spoolman harness zip sha256")
    native = (root / SPOOLMAN_NATIVE_SCRIPT).read_text()
    native_match = _SPOOLMAN_ZIP_URL_RE.search(native)
    if not native_match:
        raise BumpError("scripts/run_spoolman_native.sh has no release-zip download to move")
    if native_match.group(1) != old_version:
        raise BumpError(
            f"scripts/run_spoolman_native.sh pins v{native_match.group(1)}, not the "
            f"recorded pin version {old_version}; the harness drifted from the shipped pin"
        )
    native_sha_match = _SPOOLMAN_ZIP_SHA_RE.search(native)
    if not native_sha_match:
        raise BumpError("scripts/run_spoolman_native.sh has no zip SHA256 check to move")
    native = replace_exact(
        native, f"Launch isolated Spoolman {old_version} natively",
        f"Launch isolated Spoolman {release.version} natively",
        "run_spoolman_native.sh header version")
    native = replace_exact(
        native, f"print-handy-spoolman-{old_version}",
        f"print-handy-spoolman-{release.version}",
        "run_spoolman_native.sh cache directory version")
    native = replace_exact(
        native, f"download/v{old_version}/spoolman.zip",
        f"download/v{release.version}/spoolman.zip", "run_spoolman_native.sh harness version")
    native = replace_exact(
        native, f"echo '{native_sha_match.group(1)}  '\"$archive\"",
        f"echo '{zip_sha}  '\"$archive\"", "run_spoolman_native.sh zip sha256")
    edits = [(SPOOLMAN_CI_FILE, ci_text), (SPOOLMAN_NATIVE_SCRIPT, native)]
    changes = [
        "the test-harness pins (CI spoolman job zip and scripts/run_spoolman_native.sh) "
        f"updated to v{release.version} with the release zip's SHA256, so the real-server "
        "tests run against the version the image ships"
    ]
    provenance = [f"spoolman.zip sha256 ({SPOOLMAN_ZIP_ASSET}): {zip_prov}"]
    return edits, changes, provenance


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
    harness_edits, harness_changes, harness_provenance = _spoolman_harness_edits(
        root, api, release, old_version)
    edits = [
        (SPOOLMAN_FILE, render_spoolman(text, commit)),
        (DOC_SETTINGS, settings),
        (DOC_DOCKER, docker_doc),
        *harness_edits,
    ]
    changes.append("the SETTINGS.md row and DOCKER.md build sentence updated to the same version and ref")
    changes.extend(harness_changes)
    provenance.extend(harness_provenance)
    return Plan("spoolman", "bump", version=release.version, old_version=old_version,
                files=[relpath for relpath, _ in edits],
                provenance=provenance, changes=changes, release=release, edits=edits)


_BAMBU_SETTINGS_RE = re.compile(
    r"\| `BAMBU_REF` \| `([0-9a-f]{40})` \| Pinned Bambu Studio (\S+) source commit \|"
)


def _bambu_recorded(root, pin_ref):
    """(recorded release text, HMS provenance commit) for the pin.

    The recorded surfaces must agree with the Dockerfile and with
    each other before anything moves: the SETTINGS.md row records
    the full ref and the release it names, COMPONENTS.md records
    the ref's prefix, and docs/BAMBU-CONNECTIVITY.md names the HMS
    table's provenance commit (which is its own surface - the
    table was generated at that commit, not necessarily at the
    build pin). Any disagreement is drift, and drift is a human's
    problem.
    """
    settings = (root / DOC_SETTINGS).read_text()
    match = _BAMBU_SETTINGS_RE.search(settings)
    if not match:
        raise BumpError("docs/SETTINGS.md has no BAMBU_REF row in the expected shape")
    if match.group(1) != pin_ref:
        raise BumpError("docs/SETTINGS.md records a different BAMBU_REF than the Dockerfile")
    recorded_version = match.group(2)
    components = (root / DOC_COMPONENTS).read_text()
    if f"commit `{pin_ref[:8]}…`" not in components:
        raise BumpError("docs/COMPONENTS.md does not record the Dockerfile's BAMBU_REF prefix")
    try:
        table = json.loads((root / HMS_FILE).read_text())
        hms_commit = table["_provenance"]["bambu_studio_commit"]
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise BumpError(f"cannot read the HMS table's provenance commit: {exc}")
    connectivity = (root / DOC_BAMBU).read_text()
    if connectivity.count(hms_commit) != 2:
        raise BumpError(
            "docs/BAMBU-CONNECTIVITY.md does not name the HMS provenance commit "
            "in the two places the bump expects"
        )
    return recorded_version, hms_commit


def _git(args, cwd):
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise BumpError(
            f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def fetch_bambu_source(commit, dest):
    """Fetch BambuStudio at an exact commit into dest, sparsely.

    Only resources/hms is materialized: the planner needs nothing
    else, and the full tree is gigabytes. A blob-less partial fetch
    (--filter=blob:none) plus a cone sparse-checkout downloads just
    that folder's files. The checked-out HEAD is verified against
    the pin, the same check the Dockerfile's bambu-build stage
    performs on its own full fetch.
    """
    dest.mkdir(parents=True, exist_ok=True)
    _git(["init", "."], dest)
    _git(["remote", "add", "origin", BAMBU_URL], dest)
    _git(["sparse-checkout", "init", "--cone"], dest)
    _git(["sparse-checkout", "set", "resources/hms"], dest)
    _git(["fetch", "--depth", "1", "--filter=blob:none", "origin", commit], dest)
    _git(["checkout", "--detach", "FETCH_HEAD"], dest)
    head = _git(["rev-parse", "HEAD"], dest)
    if head != commit:
        raise BumpError(f"fetched BambuStudio HEAD {head} is not the pin {commit}")
    return dest


def locate_hms_dir(tree):
    """The resources/hms folder of a fetched BambuStudio tree.

    The generator expects hms_en_*.json files there; if the layout
    has moved, the bump stops for a human rather than hunting
    through the tree for something similar.
    """
    candidate = Path(tree) / "resources" / "hms"
    if not candidate.is_dir() or not any(candidate.glob("hms_en_*.json")):
        raise BumpError(
            "the fetched Bambu Studio tree has no resources/hms/hms_en_*.json "
            "- the upstream layout moved; stopping for a human"
        )
    return candidate


def regenerate_hms(hms_dir, commit):
    """Regenerate hms_data.json at a commit; return its exact text.

    Runs the repo's own generator (scripts/bambu-hms.py) in a
    staged mirror of its layout - the generator derives its output
    path from its own location - then runs it again with --check
    in the stage, so the text the plan commits is proven to be a
    fresh generation at that commit before anything is written.
    """
    generator = Path(__file__).resolve().with_name("bambu-hms.py")
    if not generator.is_file():
        raise BumpError(f"the HMS generator is missing next to this script: {generator}")
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        staged = stage / "scripts" / "bambu-hms.py"
        staged.parent.mkdir(parents=True)
        shutil.copyfile(generator, staged)
        out = stage / "print_handy" / "printers" / "bambulab" / "hms_data.json"
        out.parent.mkdir(parents=True)
        for extra in ([], ["--check"]):
            result = subprocess.run(
                [sys.executable, str(staged), "--hms-dir", str(hms_dir),
                 "--commit", commit, *extra],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                raise BumpError(f"scripts/bambu-hms.py {' '.join(extra) or 'generate'} failed: {detail}")
        return out.read_text()


def plan_bambu(root, api, now, fetcher=None):
    text = (root / BAMBU_FILE).read_text()
    pin_ref = parse_bambu(text)
    release = api.get_latest_release(BAMBU_REPO)
    if release is None:
        raise BumpError("Bambu Studio has no published final release")
    commit = api.resolve_tag_commit(BAMBU_REPO, release.tag)
    if commit == pin_ref:
        return Plan("bambu", "current", version=release.version,
                    reason=f"pin already resolves to {release.tag}")
    # A commit pin carries no version: the release is newer when it
    # was published after the pinned commit was made (the rule the
    # upstream-watch script uses for the same pin).
    pin_date = api.get_commit_date(BAMBU_REPO, pin_ref)
    if release.published_at <= pin_date:
        return Plan("bambu", "not-newer", version=release.version,
                    reason=f"latest final release {release.tag} was published "
                           f"{release.published_at.date().isoformat()}, not after the pinned "
                           f"commit's date {pin_date.date().isoformat()}; the pin is not behind")
    ok, age = _age_gate(release, now)
    if not ok:
        return Plan("bambu", "too-young", version=release.version,
                    reason=f"release is {age:.1f} days old; the gate is {MIN_RELEASE_AGE_DAYS} days")
    recorded_version, hms_commit = _bambu_recorded(root, pin_ref)
    # Every text surface is edited (and drift-checked) before the
    # source fetch, so a bump a human must fix never pays for the
    # download first.
    settings = (root / DOC_SETTINGS).read_text()
    settings = replace_exact(settings, f"`{pin_ref}`", f"`{commit}`",
                             "docs/SETTINGS.md BAMBU_REF row")
    settings = replace_exact(settings, f"Pinned Bambu Studio {recorded_version} source commit",
                             f"Pinned Bambu Studio {release.version} source commit",
                             "docs/SETTINGS.md Bambu version")
    components = replace_exact(
        (root / DOC_COMPONENTS).read_text(),
        f"commit `{pin_ref[:8]}…`", f"commit `{commit[:8]}…`",
        "docs/COMPONENTS.md BambuStudio row",
    )
    connectivity = replace_exact(
        (root / DOC_BAMBU).read_text(), hms_commit, commit,
        "docs/BAMBU-CONNECTIVITY.md HMS provenance records", count=2,
    )
    fetch = fetcher or fetch_bambu_source
    with tempfile.TemporaryDirectory() as tmp:
        tree = fetch(commit, Path(tmp) / "BambuStudio")
        hms_text = regenerate_hms(locate_hms_dir(tree), commit)
    table = json.loads(hms_text)
    hms_codes = len(table.get("hms") or {})
    error_codes = len(table.get("error") or {})
    provenance = [
        f"Release: {release.html_url} (published {release.published_at.date().isoformat()})",
        f"BAMBU_REF: the full commit {release.tag} resolves to, {commit}",
        f"Comparison: the release was published after the pinned commit's date "
        f"{pin_date.date().isoformat()}, so the pin is behind (the commit-pin rule).",
        f"hms_data.json: regenerated by scripts/bambu-hms.py from resources/hms at the new "
        f"commit and verified with --check in the staging tree before commit "
        f"({hms_codes} HMS codes, {error_codes} print-error codes); its provenance commit "
        f"moves {hms_commit} -> {commit}.",
        "The Dockerfile build fetches and verifies this exact ref, so CI is the judge of the bump.",
    ]
    changes = [
        f"BAMBU_REF: {pin_ref} -> {commit}",
        f"hms_data.json regenerated at the new commit ({hms_codes} HMS codes, {error_codes} print-error codes)",
        "the SETTINGS.md row, COMPONENTS.md row and BAMBU-CONNECTIVITY.md provenance records updated to the same commit",
    ]
    edits = [
        (BAMBU_FILE, render_bambu(text, commit)),
        (DOC_SETTINGS, settings),
        (DOC_COMPONENTS, components),
        (DOC_BAMBU, connectivity),
        (HMS_FILE, hms_text),
    ]
    return Plan("bambu", "bump", version=release.version, old_version=recorded_version,
                files=[relpath for relpath, _ in edits],
                provenance=provenance, changes=changes, release=release, edits=edits)


def select_orca_target(releases):
    """The newest OrcaSlicer release overall.

    Single channel: the shipped build tracks prereleases (the pin
    is v2.5.0-rc), so the target is the newest of (latest final,
    latest prerelease) under prerelease-aware ordering - a final
    beats its own prereleases, an rc beats a beta, and a newer
    line's prerelease beats an older final. Publication time only
    breaks exact-version ties.
    """
    if not releases:
        raise BumpError("OrcaSlicer has no published releases")
    return max(releases, key=lambda r: (version_key(r.version), r.published_at))


def _orca_asset_name(old_name, old_version, new_version):
    """The new release's asset name, derived from the pin's own.

    Upstream names the AppImages after the version; deriving the
    name from the current pin (rather than baking in a pattern)
    means an upstream naming change stops the bump - the derived
    name is then absent from the release and the asset lookup
    refuses - instead of the planner guessing.
    """
    if old_name.count(old_version) != 1:
        raise BumpError(
            f"asset name {old_name!r} does not embed the pinned version "
            f"{old_version!r} exactly once; cannot derive the new asset name"
        )
    return old_name.replace(old_version, new_version)


def fetch_orca_bundle(api, release, asset_name, sha256, dest):
    """Download the amd64 AppImage, verify it, extract it.

    Returns the extracted resources folder. The download's SHA256
    is checked against the value the plan will commit before
    extraction - the same check docker/orca-prebuilt.sh performs
    in the image build.
    """
    asset = release.asset(asset_name)
    if asset is None:
        raise BumpError(f"release {release.tag} has no asset named {asset_name}")
    dest.mkdir(parents=True, exist_ok=True)
    image = dest / asset_name
    image.write_bytes(api.download(asset["browser_download_url"]))
    actual = hashlib.sha256(image.read_bytes()).hexdigest()
    if actual != sha256:
        raise BumpError(
            f"downloaded {asset_name} hashes to {actual}, not the recorded {sha256}"
        )
    image.chmod(0o755)
    result = subprocess.run(
        [str(image), "--appimage-extract"], cwd=dest, capture_output=True, text=True
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise BumpError(f"--appimage-extract of {asset_name} failed: {detail}")
    resources = dest / "squashfs-root" / "resources"
    if not (resources / "profiles").is_dir():
        raise BumpError(
            "the extracted AppImage has no resources/profiles folder - the "
            "upstream layout moved; stopping for a human"
        )
    return resources


def fetch_orca_source(commit, dest):
    """Fetch OrcaSlicer at an exact commit into dest, sparsely.

    Only resources/profiles is materialized - the identity sweep's
    JSON-tree variant reads the vendor indexes and profile files
    there, and the full tree is gigabytes. A blob-less partial
    fetch (--filter=blob:none) plus a cone sparse-checkout
    downloads just that folder, the same shape as
    fetch_bambu_source; the checked-out HEAD is verified against
    the pin.
    """
    dest.mkdir(parents=True, exist_ok=True)
    _git(["init", "."], dest)
    _git(["remote", "add", "origin", ORCA_URL], dest)
    _git(["sparse-checkout", "init", "--cone"], dest)
    _git(["sparse-checkout", "set", "resources/profiles"], dest)
    _git(["fetch", "--depth", "1", "--filter=blob:none", "origin", commit], dest)
    _git(["checkout", "--detach", "FETCH_HEAD"], dest)
    head = _git(["rev-parse", "HEAD"], dest)
    if head != commit:
        raise BumpError(f"fetched OrcaSlicer HEAD {head} is not the pin {commit}")
    return dest


def locate_orca_tree(tree):
    """The resources folder of a fetched OrcaSlicer source tree.

    The sweep's tree variant reads profiles/<Vendor>.json plus
    profiles/<Vendor>/ for each checked vendor; if the layout has
    moved, the bump stops for a human rather than hunting through
    the tree for something similar.
    """
    resources = Path(tree) / "resources"
    profiles = resources / "profiles"
    missing = [
        vendor for vendor in ORCA_VENDORS.values()
        if not (profiles / f"{vendor}.json").is_file() or not (profiles / vendor).is_dir()
    ]
    if missing:
        raise BumpError(
            "the fetched OrcaSlicer tree has no resources/profiles JSON tree for "
            f"{', '.join(missing)} - the upstream layout moved; stopping for a human"
        )
    return resources


def stage_packed_profiles(resources, dest):
    """A resources-shaped folder holding only the packed caches.

    The bundle ships each vendor's profiles twice: the JSON tree
    (profiles/<Vendor>/) and the packed preset cache
    (profiles/<Vendor>.opc). The profile pipeline prefers the tree
    when both are present, so the packed packaging is swept
    against a staged folder where only the caches exist - the two
    packagings flatten differently and carry separate recorded
    identities.
    """
    profiles = dest / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    for vendor in ORCA_VENDORS.values():
        cache = resources / "profiles" / f"{vendor}.opc"
        if not cache.is_file():
            raise BumpError(
                f"the bundle ships no packed preset cache for {vendor} "
                f"(profiles/{vendor}.opc) - the upstream layout moved; "
                "stopping for a human"
            )
        shutil.copyfile(cache, profiles / cache.name)
    return dest


_ORCA_SWEEP_DRIVER = '''\
"""Identity sweep driver, written to a temp file by bump_upstream.py.

For every identity-checked model of the six Orca vendors, flatten
the model's profiles from each resources variant with the repo's
own pipeline (print_handy.printers.bambulab.profiles - the same
flatten the scripts/*-templates.py --check mode verifies with)
and report which recorded identities the flattened hashes match.
For models that declare per-material filament profiles
(orca_filaments - Bambu Lab models), the matched identity must
also cover those profiles at their recorded hashes, the same
conjunction profiles.check_resources enforces. It mirrors the
hand-run sweep harness used for the 2.5.0-rc bump, extended with
that filament coverage.
"""
import importlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
variants = {"tree": Path(sys.argv[2]), "packed": Path(sys.argv[3])}
out_path = Path(sys.argv[4])
families = sys.argv[5].split(",") if len(sys.argv) > 5 and sys.argv[5] else None
sys.path.insert(0, str(root))
from print_handy.printers.bambulab import profiles as shared

VENDORS = {
    "bambulab": "BBL",
    "anycubic": "Anycubic",
    "creality": "Creality",
    "elegoo": "Elegoo",
    "prusa": "Prusa",
    "qidi": "Qidi",
}

results = {}
for pkg, vendor in VENDORS.items():
    if families and pkg not in families:
        continue
    facts = importlib.import_module(f"print_handy.printers.{pkg}.facts")
    models = getattr(facts, "MODELS", None) or {
        name.lower(): obj
        for name, obj in vars(facts).items()
        if isinstance(obj, dict) and "orca_identity" in obj
    }
    results[pkg] = {}
    for key, model in sorted(models.items()):
        names = model["orca_profiles"].value
        recorded = model["orca_identity"].value
        declared = model["orca_filaments"].value if "orca_filaments" in model else {}
        per_variant = {}
        for vname, resources in variants.items():
            try:
                upstream, version = shared.flatten(resources, names, vendor=vendor)
                hashes = {kind: shared.sha256(upstream[kind]) for kind in shared.KINDS}
                matched = []
                for entry in recorded:
                    if not (all(entry["sha256"].get(kind) == hashes[kind] for kind in shared.KINDS)
                            and entry["vendor_version"] == version):
                        continue
                    if declared:
                        # The identity must also cover the model's
                        # declared per-material filament profiles at
                        # their recorded hashes (production's
                        # filament_hashes: the identity's filaments
                        # map plus the default filament from the
                        # triple). flatten_filaments reads the BBL
                        # vendor, so a declared set on any other
                        # family is refused, never silently skipped.
                        if pkg != "bambulab":
                            raise ValueError(
                                f"{pkg}/{key} declares orca_filaments, which the "
                                "sweep can only verify for bambulab models"
                            )
                        expected = {**entry.get("filaments", {}),
                                    names["filament"]: entry["sha256"]["filament"]}
                        flat = shared.flatten_filaments(
                            resources, sorted(set(declared.values())))
                        if any(shared.sha256(flat[name]) != expected.get(name)
                               for name in set(declared.values())):
                            continue
                    matched.append(entry["label"])
            except Exception as exc:  # keep going: the report names every failure
                per_variant[vname] = {"error": f"{type(exc).__name__}: {exc}"}
                continue
            per_variant[vname] = {"version": version, "sha256": hashes, "matched": matched}
        results[pkg][key] = per_variant
out_path.write_text(json.dumps(results, indent=1) + "\\n")
'''


def sweep_orca_identities(root, tree_resources, packed_resources, families=None):
    """Run the identity sweep driver against a bundle's resources.

    A subprocess on purpose: the bump script stays stdlib-only and
    never imports print_handy itself; the driver imports the
    repo's profile pipeline from the checkout being planned,
    exactly like scripts/*-templates.py do. Returns the parsed
    results: {family: {model: {variant: {version, sha256, matched}
    or {error}}}}.
    """
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        driver = stage / "orca_identity_sweep.py"
        driver.write_text(_ORCA_SWEEP_DRIVER)
        out = stage / "results.json"
        cmd = [
            sys.executable, str(driver), str(root),
            str(tree_resources), str(packed_resources), str(out),
        ]
        if families:
            cmd.append(",".join(families))
        # A fresh bytecode-cache prefix inside the stage: the driver
        # must read the checkout's facts as they are on disk, never
        # a stale .pyc a previous run left beside them (the cache
        # key is whole-second mtime + size, so a same-size edit
        # inside one second would otherwise be silently missed).
        env = {**os.environ, "PYTHONPYCACHEPREFIX": str(stage / "pycache")}
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if result.returncode != 0 or not out.is_file():
            detail = (result.stderr or result.stdout).strip()
            raise BumpError(f"the Orca identity sweep driver failed: {detail}")
        return json.loads(out.read_text())


def verify_orca_identities(results):
    """Summary lines for a sweep whose every model matched.

    Raises BumpError naming every model that failed to flatten or
    matched no recorded identity in either packaging: those
    profiles changed (or the vendor format moved), and recording
    new identities is a human sweep's judgment, never the
    planner's.
    """
    problems = []
    lines = []
    total = 0
    for pkg in sorted(results):
        models = results[pkg]
        covered = 0
        versions = set()
        for key in sorted(models):
            total += 1
            ok = True
            for variant, per in sorted(models[key].items()):
                if "error" in per:
                    problems.append(f"{pkg}/{key} ({variant}): {per['error']}")
                    ok = False
                elif not per.get("matched"):
                    problems.append(
                        f"{pkg}/{key} ({variant}): flattened profiles at vendor "
                        f"version {per.get('version')} match no recorded identity"
                    )
                    ok = False
                else:
                    versions.add(per["version"])
            if ok:
                covered += 1
        lines.append(
            f"identity sweep: {pkg} {covered}/{len(models)} models match a recorded "
            f"identity in both packagings (vendor profiles {', '.join(sorted(versions))})"
        )
    if problems:
        raise BumpError(
            "the new bundle's profiles are not fully covered by the recorded "
            "identities; a human sweep must check the new bundle and record "
            "any new identities first: " + "; ".join(problems)
        )
    lines.append(
        f"identity sweep: all {total} identity-checked models across "
        f"{len(results)} families match a recorded identity in both packagings"
    )
    return lines


def _orca_surface_edits(root, pin, new_pin, ci_sha, flatpak_sha):
    """Every recorded Orca surface, edited and drift-checked.

    Planned before the bundle download: a surface that does not
    hold the value the bump expects stops the bump for a human
    instead of paying for the download first. Narrative records
    that tell the release line's story (the pin comment in
    orca-prebuilt.sh, docs/BAMBU-LAB.md, the vendor guides'
    identity-coverage notes, the Bambu model docstrings) are not
    pin records and are deliberately not rewritten - a mechanical
    version swap there could assert something false, so they stay
    a human's sentence pass on the bump PR.
    """
    edits = [(ORCA_FILE, render_orca((root / ORCA_FILE).read_text(), new_pin))]
    ci_text = (root / ORCA_CI_FILE).read_text()
    ci_text = replace_exact(
        ci_text, f"ORCA_BUNDLE_VERSION: {pin.version}",
        f"ORCA_BUNDLE_VERSION: {new_pin.version}", "ci.yml ORCA_BUNDLE_VERSION",
    )
    ci_text = replace_exact(
        ci_text, f"ORCA_BUNDLE_SHA256: {ci_sha}",
        f"ORCA_BUNDLE_SHA256: {flatpak_sha}", "ci.yml ORCA_BUNDLE_SHA256",
    )
    edits.append((ORCA_CI_FILE, ci_text))
    edits.append((ORCA_FLATPAK_SHA_TEST, replace_exact(
        (root / ORCA_FLATPAK_SHA_TEST).read_text(),
        f'ORCA_BUNDLE_SHA256 = "{ci_sha}"', f'ORCA_BUNDLE_SHA256 = "{flatpak_sha}"',
        "tests/test_ci_bambu_workflow.py ORCA_BUNDLE_SHA256 constant",
    )))
    guard = (root / ORCA_GUARD_TEST).read_text()
    for old, new, what in [
        (f"version={pin.version}", f"version={new_pin.version}", "pin line"),
        (f"revision={pin.revision}", f"revision={new_pin.revision}", "pin line"),
        (f"sha256_amd64={pin.sha256_amd64}", f"sha256_amd64={new_pin.sha256_amd64}", "pin line"),
        (f"asset_amd64={pin.asset_amd64}", f"asset_amd64={new_pin.asset_amd64}", "pin line"),
        (f"sha256_arm64={pin.sha256_arm64}", f"sha256_arm64={new_pin.sha256_arm64}", "pin line"),
        (f"asset_arm64={pin.asset_arm64}", f"asset_arm64={new_pin.asset_arm64}", "pin line"),
        (f'"{pin.version}"', f'"{new_pin.version}"', "expected table"),
        (f'"{pin.revision}"', f'"{new_pin.revision}"', "expected table"),
        (f'"{pin.asset_amd64}"', f'"{new_pin.asset_amd64}"', "expected table"),
        (f'"{pin.sha256_amd64}"', f'"{new_pin.sha256_amd64}"', "expected table"),
        (f'"{pin.asset_arm64}"', f'"{new_pin.asset_arm64}"', "expected table"),
        (f'"{pin.sha256_arm64}"', f'"{new_pin.sha256_arm64}"', "expected table"),
    ]:
        guard = replace_exact(guard, old, new, f"tests/test_docker_release.py {what}")
    edits.append((ORCA_GUARD_TEST, guard))
    contract = (root / ORCA_CONTRACT_TEST).read_text()
    for old, new in [
        (pin.asset_amd64, new_pin.asset_amd64),
        (pin.sha256_amd64, new_pin.sha256_amd64),
        (pin.revision, new_pin.revision),
        (pin.asset_arm64, new_pin.asset_arm64),
        (pin.sha256_arm64, new_pin.sha256_arm64),
    ]:
        contract = replace_exact(
            contract, old, new, "tests/test_docker_release_contract.py pin tuple"
        )
    edits.append((ORCA_CONTRACT_TEST, contract))
    # Bambu Lab's ORCA_RELEASE records the release whose BBL vendor
    # profiles were checked; it names the pin itself, so it moves
    # with the pin once the sweep below has checked the new
    # bundle's BBL profiles. Elegoo's and Prusa's ORCA_RELEASE
    # constants deliberately record a different, older release
    # (their checked profiles were derived there) and are not pin
    # records - they are never touched.
    old_tuple = f'("v{pin.version}", "{pin.revision}")'
    new_tuple = f'("v{new_pin.version}", "{new_pin.revision}")'
    edits.append((BAMBULAB_FACTS, replace_exact(
        (root / BAMBULAB_FACTS).read_text(), old_tuple, new_tuple,
        "print_handy/printers/bambulab/facts.py ORCA_RELEASE",
    )))
    edits.append((BAMBULAB_P1S_TEST, replace_exact(
        (root / BAMBULAB_P1S_TEST).read_text(), old_tuple, new_tuple,
        "tests/printers/test_bambulab_p1s.py ORCA_RELEASE assertion",
    )))
    edits.append((DOC_COMPONENTS, replace_exact(
        (root / DOC_COMPONENTS).read_text(),
        f"`{pin.version}`, revision `{pin.revision[:8]}…`",
        f"`{new_pin.version}`, revision `{new_pin.revision[:8]}…`",
        "docs/COMPONENTS.md OrcaSlicer row",
    )))
    edits.append((DOC_DOCKER, replace_exact(
        (root / DOC_DOCKER).read_text(),
        f"now v{pin.version})", f"now v{new_pin.version})",
        "docs/DOCKER.md shipped OrcaSlicer record",
    )))
    settings = (root / DOC_SETTINGS).read_text()
    settings = replace_exact(
        settings, f"single build (v{pin.version})", f"single build (v{new_pin.version})",
        "docs/SETTINGS.md PRINT_HANDY_ORCA_CHANNEL row",
    )
    settings = replace_exact(
        settings, f"release (v{pin.version}, SHA256 verified",
        f"release (v{new_pin.version}, SHA256 verified",
        "docs/SETTINGS.md WITH_ORCA row",
    )
    edits.append((DOC_SETTINGS, settings))
    edits.append((DOC_ENV_EXAMPLE, replace_exact(
        (root / DOC_ENV_EXAMPLE).read_text(),
        f"bundled OrcaSlicer (v{pin.version}):", f"bundled OrcaSlicer (v{new_pin.version}):",
        ".env.example bundled OrcaSlicer record",
    )))
    edits.append((DOC_README, replace_exact(
        (root / DOC_README).read_text(),
        f"one bundled build (v{pin.version})", f"one bundled build (v{new_pin.version})",
        "README.md bundled OrcaSlicer record",
    )))
    edits.append((DOC_UNRAID, replace_exact(
        (root / DOC_UNRAID).read_text(),
        f"bundled OrcaSlicer (v{pin.version}) and stable",
        f"bundled OrcaSlicer (v{new_pin.version}) and stable",
        "docker/unraid/print-handy.xml OrcaSlicer channel description",
    )))
    for relpath in ORCA_VENDOR_DOCS:
        edits.append((relpath, replace_exact(
            (root / relpath).read_text(),
            f"OrcaSlicer v{pin.version} installation",
            f"OrcaSlicer v{new_pin.version} installation",
            f"{relpath} generator instructions",
        )))
    a1_doc = (root / DOC_BAMBU_A1).read_text()
    a1_doc = replace_exact(
        a1_doc, f"bundled OrcaSlicer v{pin.version}", f"bundled OrcaSlicer v{new_pin.version}",
        "docs/BAMBU-A1-DOCKER.md bundled slicer record",
    )
    a1_doc = replace_exact(
        a1_doc, f"pinned OrcaSlicer v{pin.version} AppImage",
        f"pinned OrcaSlicer v{new_pin.version} AppImage",
        "docs/BAMBU-A1-DOCKER.md pinned AppImage record",
    )
    edits.append((DOC_BAMBU_A1, a1_doc))
    return edits


def plan_orca(root, api, now, fetcher=None, source_fetcher=None, sweeper=None):
    text = (root / ORCA_FILE).read_text()
    pin = parse_orca(text)
    target = select_orca_target(api.list_releases(ORCA_REPO))
    kind = "prerelease" if target.prerelease else "final"
    if not is_newer(target.version, pin.version):
        return Plan("orca", "current", version=pin.version,
                    reason=f"pin {pin.version} is at or ahead of the newest OrcaSlicer "
                           f"release {target.version} ({kind})")
    if not target.prerelease:
        ok, age = _age_gate(target, now)
        if not ok:
            return Plan("orca", "too-young", version=target.version,
                        reason=f"release is {age:.1f} days old; the gate is "
                               f"{MIN_RELEASE_AGE_DAYS} days (finals wait; prereleases are exempt)")
    revision = api.resolve_tag_commit(ORCA_REPO, target.tag)
    asset_amd64 = _orca_asset_name(pin.asset_amd64, pin.version, target.version)
    asset_arm64 = _orca_asset_name(pin.asset_arm64, pin.version, target.version)
    flatpak_name = f"OrcaSlicer-Linux-flatpak_V{target.version}_x86_64.flatpak"
    # Asset hashes come from the release payload's digest fields
    # (the download-and-hash fallback in _asset_sha256 is the
    # degraded path); every recorded surface is then edited and
    # drift-checked before the one large download - the bundle
    # itself - is fetched for the identity sweep.
    sha_amd64, prov_amd64 = _asset_sha256(api, target, asset_amd64)
    sha_arm64, prov_arm64 = _asset_sha256(api, target, asset_arm64)
    sha_flatpak, prov_flatpak = _asset_sha256(api, target, flatpak_name)
    ci_version, ci_sha = parse_orca_ci((root / ORCA_CI_FILE).read_text())
    if ci_version != pin.version:
        raise BumpError(
            f"ci.yml pins ORCA_BUNDLE_VERSION {ci_version}, not the prebuilt pin "
            f"{pin.version}; the two pins disagree - stopping for a human"
        )
    new_pin = OrcaPin(
        version=target.version,
        revision=revision,
        sha256_amd64=sha_amd64,
        sha256_arm64=sha_arm64,
        asset_amd64=asset_amd64,
        asset_arm64=asset_arm64,
    )
    edits = _orca_surface_edits(root, pin, new_pin, ci_sha, sha_flatpak)
    fetch = fetcher or fetch_orca_bundle
    fetch_source = source_fetcher or fetch_orca_source
    sweep = sweeper or sweep_orca_identities
    with tempfile.TemporaryDirectory() as tmp:
        resources = fetch(api, target, asset_amd64, sha_amd64, Path(tmp) / "bundle")
        packed = stage_packed_profiles(resources, Path(tmp) / "packed")
        # The shipped AppImage carries the profiles only as packed
        # preset caches (its profiles/<Vendor>/ folders hold cover
        # images, no JSON tree), so the tree packaging is swept
        # from the upstream source tree at the same commit - the
        # split the hand-run 2.5.0-rc sweep used (src tree versus
        # extracted bundle).
        source = fetch_source(revision, Path(tmp) / "OrcaSlicer")
        results = sweep(root, locate_orca_tree(source), packed)
    sweep_lines = verify_orca_identities(results)
    gate_note = (
        "prereleases are exempt from the 3-day release-age gate (single-channel policy)"
        if target.prerelease
        else "the 3-day release-age gate is satisfied"
    )
    provenance = [
        f"Release: {target.html_url} (published {target.published_at.date().isoformat()}, "
        f"{kind}; {gate_note})",
        f"revision {revision}: the full commit {target.tag} resolves to (annotated tags dereferenced)",
        f"sha256_amd64 ({asset_amd64}): {prov_amd64}",
        f"sha256_arm64 ({asset_arm64}): {prov_arm64}",
        f"ORCA_BUNDLE_SHA256 ({flatpak_name}): {prov_flatpak}",
        "Identity sweep inputs: the packed packaging from the shipped AppImage itself "
        "(downloaded and SHA256-verified above, then extracted); the JSON-tree packaging "
        "from the OrcaSlicer source tree at the same commit (blob-less sparse fetch of "
        "resources/profiles, checked-out HEAD verified against the revision) - the "
        "AppImage ships no JSON tree, only the packed caches.",
        *sweep_lines,
        "Identity handling: the sweep verifies the new bundle's profiles against the "
        "identities already recorded in the printer facts, using the same flatten + "
        "recorded-identity check the scripts/*-templates.py --check mode performs. "
        "Print Handy ships no vendor profiles (they are generated on the operator's "
        "machine from the installed bundle), so there is no generated profile data "
        "to commit; the planner never writes identity data.",
    ]
    changes = [
        f"version: {pin.version} -> {target.version} ({kind})",
        f"revision: {pin.revision} -> {revision}",
        f"sha256_amd64: {pin.sha256_amd64} -> {sha_amd64}",
        f"sha256_arm64: {pin.sha256_arm64} -> {sha_arm64}",
        f"asset_amd64: {pin.asset_amd64} -> {asset_amd64}",
        f"asset_arm64: {pin.asset_arm64} -> {asset_arm64}",
        f"ORCA_BUNDLE_SHA256 (ci.yml, the flatpak bundle the slicer legs run): {ci_sha} -> {sha_flatpak}",
        "guard tests (tests/test_docker_release.py, tests/test_docker_release_contract.py, "
        "tests/test_ci_bambu_workflow.py), the BBL ORCA_RELEASE provenance record and its "
        "assertion, and the COMPONENTS/DOCKER/SETTINGS/.env.example/README/Unraid-template/"
        "vendor-guide records updated to the same values",
        sweep_lines[-1],
        "narrative records that tell the release line's story (the pin comment in "
        "docker/orca-prebuilt.sh, docs/BAMBU-LAB.md, the vendor guides' identity-coverage "
        "notes, the Bambu model docstrings) are not pin records and are not rewritten; "
        "a human rewords them on the bump PR when the story changes",
    ]
    return Plan("orca", "bump", version=target.version, old_version=pin.version,
                files=[relpath for relpath, _ in edits],
                provenance=provenance, changes=changes, release=target, edits=edits)


PLANNERS = {
    "go2rtc": plan_go2rtc,
    "spoolman": plan_spoolman,
    "bambu": plan_bambu,
    "orca": plan_orca,
}


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
    step = classify_step(plan.old_version, plan.version) if plan.old_version else "minor"
    step_line = (
        f"Step size: {step} (`{plan.old_version}` -> `{plan.version}`, classified on "
        "normalized numeric tuples: a first- or second-component change is major, a "
        "later-component-only change is minor)."
        if plan.old_version else
        "Step size: not classified (the plan records no replaced version)."
    )
    major_section = ""
    if step == "major":
        hints = "\n".join(
            f"- {hint}" for hint in STEP_SURFACE_HINTS.get(component, [])
        ) or "- the surfaces this component's pin feeds (no recorded hints for this component)"
        major_section = f"""
## Major step — deep dive required before merge

This bump is a **major step** (`{plan.old_version}` -> `{plan.version}`): the first or second version component changed. Targeted regression tests around the touched surface are the minor-step standard and are NOT sufficient here. Before this PR is merged, a human (or a dedicated deep-dive pass) must:

- Read the upstream changelog / release notes for the full range `{plan.old_version}` -> `{plan.version}` - every major version in between, not just the target release.
- Report what changed upstream that Print Handy should change, improve, or look into as a result, and either fold the needed changes into this PR, file them as follow-ups, or record a deliberate no-action verdict per item.
- Re-examine at least these Print Handy surfaces for this component:
{hints}

Upstream release notes: {plan.release.html_url}
"""
    if plan.release is not None and getattr(plan.release, "prerelease", False):
        release_phrase = "upstream prerelease"
        latest_phrase = ("the newest upstream release (a prerelease; the single "
                         "bundled channel tracks prereleases)")
    else:
        release_phrase = "final upstream release"
        latest_phrase = "the latest final upstream release"
    return f"""## Summary and user impact

Automated bump of {component} to {plan.version}, opened by the upstream bump automation (scripts/bump_upstream.py) because a newer {release_phrase} exists and the repository pin is behind it. Merging stays a human decision; this PR never merges itself.

Upstream release: {plan.release.html_url} (published {plan.release.published_at.date().isoformat()}).

## Problem and cause

Not applicable - this is routine dependency maintenance, not a bug fix. The pin is behind {latest_phrase}; see the provenance below.

## Proposed changes

{step_line}

- {files} updated:
{changes}

No other files change. Confirmation, readiness, profile, clearance and artifact guards are untouched.
{major_section}
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

## Tests added and regression scoping

Tests added: none
Tests added detail: No new test is added: this PR changes only a pinned upstream version value, and no Print Handy behavior of its own changes to pin. The existing suite is the regression net for the bump.
Regression scoping: checked
Regression scoping reason: The change was scoped against the full suite: this PR stays draft until CI on its head runs the full suite and every check is SUCCESS, so the existing regression tests are the verification for the bump. Nothing was deliberately left out; a red suite is the verdict to close this PR.

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
- [ ] Tests added are named in the Tests added section, or the reason for adding none is specific and credible; added tests pin the changed behavior or regression, not only the happy path.
- [ ] Regression scoping is credible: the full suite and neighboring regression areas were considered, and any exclusion or coverage gap is named with its reason.
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
    parser.add_argument("--component", choices=["all", "go2rtc", "spoolman", "bambu", "orca"], default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repo", default=env.get("GITHUB_REPOSITORY") or DEFAULT_REPO)
    parser.add_argument("--check-identity", action="store_true")
    parser.add_argument("--diagnose-identity", action="store_true")
    parser.add_argument("--mark-ready", action="store_true")
    parser.add_argument("--branch", default="")
    args = parser.parse_args(argv)

    if args.check_identity:
        return check_identity(env, out, err)

    if args.diagnose_identity:
        return diagnose_identity(env, args.repo, out, err)

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
    components = ["go2rtc", "spoolman", "bambu", "orca"] if args.component == "all" else [args.component]
    return run_components(root, api, args.repo, components, args.dry_run, now,
                          log=lambda msg: print(msg, file=out))


if __name__ == "__main__":
    raise SystemExit(main())
