#!/usr/bin/env python3
"""CI scope: does this pull request need the full suite, or is it only a release bump?

A release-bump PR changes the version string and the release notes and nothing
else. The 2.3.6 bump PR (#1733) edited five such files and still ran the full
suite four times (every push and a merge from main restarted it), about 40
minutes of slicer and offline shards that cannot be affected by a version
string. The full suite still runs on the push to main, and the docker-publish
guard refuses to publish a tag whose exact commit has no green CI run, so
nothing ships on the lightweight PR run alone.

Prints ``full=true`` or ``full=false`` for $GITHUB_OUTPUT and the reason on
stderr. Fails safe: anything other than a pull request whose every changed file
is on the allowlist (and whose ``print_handy/__init__.py`` change touches only
the ``__version__`` line) is ``full=true``, and so is any error. stdlib only.

    BASE_SHA=... HEAD_SHA=... EVENT=pull_request python3 scripts/ci_scope.py
"""
import os
import re
import subprocess
import sys

# Files a version bump edits. docs/RELEASE-X.Y.Z.md is the per-release page.
BUMP_FILES = (
    re.compile(r"README\.md"),
    re.compile(r"docs/history/INTERNAL_RELEASE_NOTES\.md"),
    re.compile(r"\.github/release-notes-draft\.md"),
    re.compile(r"docs/RELEASE-[0-9]+\.[0-9]+\.[0-9]+\.md"),
)
INIT = "print_handy/__init__.py"
VERSION_LINE = re.compile(r'__version__\s*=\s*"[0-9]+\.[0-9]+\.[0-9]+"')


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def init_change_is_version_only(diff):
    """True when every added or removed line of the unified diff is the ``__version__`` line."""
    changed = [line[1:] for line in diff.splitlines()
               if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    return bool(changed) and all(VERSION_LINE.fullmatch(line.strip()) for line in changed)


def scope(event, base, head, run=git):
    """(full, reason) for an event and the PR's base and head commits."""
    if event != "pull_request":
        return True, f"{event or 'unknown'} event: full suite"
    if not base or not head:
        return True, "no base/head commit: full suite"
    files = [f for f in run("diff", "--name-only", f"{base}...{head}").splitlines() if f]
    if not files:
        return True, "no changed files found: full suite"
    other = [f for f in files if f != INIT and not any(p.fullmatch(f) for p in BUMP_FILES)]
    if other:
        return True, f"{len(files)} changed file(s), {len(other)} outside a release bump (e.g. {other[0]}): full suite"
    if INIT in files and not init_change_is_version_only(run("diff", "-U0", f"{base}...{head}", "--", INIT)):
        return True, f"{INIT} changes more than the __version__ line: full suite"
    return False, f"release bump only ({', '.join(files)}): lightweight suite; main runs the full suite"


def main():
    try:
        full, reason = scope(os.environ.get("EVENT", ""), os.environ.get("BASE_SHA", ""),
                             os.environ.get("HEAD_SHA", ""))
    except Exception as exc:  # noqa: BLE001 - any failure means the full suite, never a skip
        full, reason = True, f"scope check failed ({type(exc).__name__}: {exc}): full suite"
    print(f"full={'true' if full else 'false'}")
    print(f"CI scope: {reason}", file=sys.stderr)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"CI scope: {reason}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
