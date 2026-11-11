#!/usr/bin/env python3
"""Fail on any GitHub Actions ``uses:`` reference that is not pinned (#1141).

Every third-party action in ``.github/workflows`` and ``.github/actions``
must be pinned to a full 40-character commit SHA with a trailing version
comment, for example::

    uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1

A tag such as ``@v7`` can be moved by whoever controls the action's
repository, and some of these actions hold secrets (the GitHub App key in
``create-github-app-token``, the registry token in ``docker/login-action``).
A ``docker://`` reference must carry an ``@sha256:`` digest. Local actions
(``./path``) are exempt. Renovate keeps the pins and comments current.

Usage: ``python3 scripts/check_action_pins.py [ROOT]``; exits 1 and lists
each offending ``file:line`` when anything is unpinned.
"""

import pathlib
import re
import sys

USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<value>.*)$")
ACTION_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@(?P<ref>[^\s#]+)")
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"docker://[^\s#]+@sha256:[0-9a-f]{64}")
VERSION_COMMENT_RE = re.compile(r"#\s*v?[0-9]+(?:\.[0-9]+)*\S*")


def workflow_files(root):
    github = pathlib.Path(root) / ".github"
    files = []
    for folder in (github / "workflows", github / "actions"):
        if folder.is_dir():
            files.extend(p for p in folder.rglob("*") if p.suffix in (".yml", ".yaml"))
    return sorted(files)


def line_problem(value):
    """The reason a ``uses:`` value is not acceptably pinned, or None."""
    value = value.strip()
    reference, _, comment = value.partition("#")
    reference = reference.strip().strip("'\"")
    comment = "#" + comment if comment else ""
    if not reference:
        return "empty uses: value"
    if reference.startswith("./"):
        return None
    if reference.startswith("docker://"):
        if DIGEST_RE.fullmatch(reference):
            return None
        return f"{reference} is not pinned by @sha256 digest"
    match = ACTION_RE.fullmatch(reference)
    if not match:
        return f"{reference} is not an owner/repo@ref reference"
    if not SHA_RE.fullmatch(match.group("ref")):
        return f"{reference} is pinned to {match.group('ref')!r}, not a 40-character commit SHA"
    if not VERSION_COMMENT_RE.fullmatch(comment.strip()):
        return f"{reference} has no trailing version comment (for example '# v7.0.1')"
    return None


def problems(root):
    found = []
    for path in workflow_files(root):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            match = USES_RE.match(line)
            if not match:
                continue
            reason = line_problem(match.group("value"))
            if reason:
                rel = path.relative_to(root).as_posix()
                found.append(f"{rel}:{number}: {reason}")
    return found


def main(argv):
    root = pathlib.Path(argv[1]) if len(argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
    found = problems(root)
    if found:
        print("Unpinned GitHub Actions references (pin each to a full commit SHA "
              "with a '# vX.Y.Z' comment):")
        for line in found:
            print(f"  {line}")
        return 1
    print(f"Every uses: reference in {len(workflow_files(root))} workflow files is SHA-pinned.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
