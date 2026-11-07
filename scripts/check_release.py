#!/usr/bin/env python3
"""Release check: fail closed on the mistakes past releases actually made.

Run before a version is tagged (see docs/RELEASE-CHECKLIST.md). It checks
the surfaces that have shipped stale before:

- version consistency: ``print_handy/__init__.py``, the README's
  "Current release" line and the ``docs/INTERNAL_RELEASE_NOTES.md``
  section for the target version must all name the target version
  (the docker-publish surfaces gate checks a looser form of this at
  publish time; this check runs it before tagging, where fixes are
  still cheap);
- notes coverage, ancestry-based (the 2.1.7/#511 lesson, repeated in
  #680 and again at the 2.2.6 freeze): every squash-merged PR number in
  ``git log <from-ref>..HEAD`` must be mentioned in BOTH the draft
  GitHub release notes and the internal notes section for the version.
  The PR list comes from the tag range's ancestry, never from dates;
- US spelling over the whole draft release notes (the CI spelling job
  only scans added Markdown lines in a PR diff, so release notes —
  which are not a PR diff — were never scanned; audit row A10).

The template/SETTINGS parity and Docker contract surfaces are covered
by the existing test suites (tests/test_variables_contract.py,
tests/test_docker_release.py, tests/test_docker_release_contract.py);
the release-check workflow runs those explicitly instead of duplicating
them here. Pages drift is checked by scripts/check_release_surfaces.py
in the docker-publish surfaces gate, reusing the docs repo's own
check-sync definition; this script does not reimplement it.

Usage:
  check_release.py --version 2.2.6 --from-ref 2.2.5 \
      --notes path/to/draft-release-notes.md \
      [--internal-notes docs/INTERNAL_RELEASE_NOTES.md] \
      [--range-file range.txt]

``--range-file`` supplies the PR numbers one per line (or in git-log
text) instead of running git, which is how the tests drive the coverage
logic without a repository history.

Exit code: 0 when every check passes, 1 when any check fails, 2 on
malformed input.
"""

import argparse
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
SQUASH_PR_RE = re.compile(r"\(#([0-9]+)\)\s*$")
MENTION_RE_TEMPLATE = r"#{}\b"


def _load_spelling():
    spec = importlib.util.spec_from_file_location(
        "check_us_spelling", Path(__file__).with_name("check_us_spelling.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pr_numbers_from_subjects(subjects):
    """Squash-merged PR numbers from commit subjects, sorted, de-duplicated.

    Only a trailing ``(#NNN)`` counts — the same convention the
    post-release surfaces workflow uses. A trailing number that turns
    out to be an issue reference rather than a PR is still returned:
    mentioning it in the notes is harmless, and dropping numbers
    silently is how the 2.2.1 workflow broke (audit row A4).
    """
    numbers = set()
    for subject in subjects:
        match = SQUASH_PR_RE.search(subject.strip())
        if match:
            numbers.add(int(match.group(1)))
    return sorted(numbers)


def pr_numbers_in_range(from_ref, root, to_ref="HEAD"):
    proc = subprocess.run(
        ["git", "log", "--format=%s", f"{from_ref}..{to_ref}"],
        cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ValueError(
            f"git log {from_ref}..{to_ref} failed: {proc.stderr.strip()}")
    return pr_numbers_from_subjects(proc.stdout.splitlines())


def missing_mentions(numbers, text):
    return [n for n in numbers
            if not re.search(MENTION_RE_TEMPLATE.format(n), text)]


def internal_section(text, version):
    """The ``# Version X.Y.Z`` section of the internal notes, or None."""
    match = re.search(
        rf"^# Version {re.escape(version)}\s*$", text, re.MULTILINE)
    if match is None:
        return None
    rest = text[match.end():]
    next_heading = re.search(r"^# Version ", rest, re.MULTILINE)
    return rest[:next_heading.start()] if next_heading else rest


def version_problems(root, version):
    problems = []
    init_text = (root / "print_handy" / "__init__.py").read_text()
    match = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
    if match is None:
        problems.append("print_handy/__init__.py: no __version__ found")
    elif match.group(1) != version:
        problems.append(
            f"print_handy/__init__.py: __version__ is {match.group(1)}, "
            f"not the release version ({version})")
    readme = (root / "README.md").read_text()
    current = re.search(r"^Current release: \*\*([^*]+)\*\*", readme,
                        re.MULTILINE)
    if current is None:
        problems.append("README.md: no 'Current release: **X.Y.Z**' line")
    elif current.group(1) != version:
        problems.append(
            f"README.md: Current release is {current.group(1)}, "
            f"not the release version ({version})")
    return problems


def run_checks(root, version, from_ref, notes_path, internal_path,
               range_file=None, to_ref="HEAD"):
    problems = []
    problems += version_problems(root, version)

    internal_text = Path(internal_path).read_text()
    section = internal_section(internal_text, version)
    if section is None:
        problems.append(
            f"{internal_path}: no '# Version {version}' section")
        section = ""

    notes_text = Path(notes_path).read_text()

    if range_file is not None:
        numbers = pr_numbers_from_subjects(
            Path(range_file).read_text().splitlines())
        # A range file may also carry bare numbers, one per line.
        if not numbers:
            numbers = sorted({int(line) for line in
                              Path(range_file).read_text().splitlines()
                              if line.strip().isdigit()})
    else:
        numbers = pr_numbers_in_range(from_ref, root, to_ref)
    if not numbers:
        problems.append(
            f"no merged PRs found in range {from_ref}..{to_ref}; "
            "refusing to pass a release with an empty ancestry range")
    for label, text in (("draft release notes", notes_text),
                        (f"internal notes section {version}", section)):
        missing = missing_mentions(numbers, text)
        if missing:
            problems.append(
                f"{label}: merged PRs missing from the notes: "
                + ", ".join(f"#{n}" for n in missing))

    spelling = _load_spelling()
    for source, number, word, replacement in spelling.text_violations(
            notes_text, str(notes_path)):
        problems.append(
            f"{source}:{number}: British spelling {word!r}, "
            f"use {replacement!r}")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--from-ref", default="",
                        help="previous release tag (default: none; "
                             "required unless --range-file is given)")
    parser.add_argument("--notes", required=True,
                        help="draft GitHub release notes (Markdown)")
    parser.add_argument("--internal-notes",
                        default=str(ROOT / "docs" / "INTERNAL_RELEASE_NOTES.md"))
    parser.add_argument("--range-file", default=None)
    parser.add_argument("--to-ref", default="HEAD",
                        help="range end (default HEAD; the version tag "
                             "itself for post-release checks)")
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args(argv)

    if not VERSION_RE.fullmatch(args.version):
        print(f"--version must use MAJOR.MINOR.PATCH form, "
              f"got {args.version!r}")
        return 2
    if args.range_file is None and not args.from_ref:
        print("--from-ref is required unless --range-file is given")
        return 2
    try:
        problems = run_checks(Path(args.root), args.version, args.from_ref,
                              args.notes, args.internal_notes,
                              args.range_file, args.to_ref)
    except (OSError, ValueError) as exc:
        print(f"release check could not run: {exc}")
        return 2
    if problems:
        print(f"## Release check — {args.version}: FAIL")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print(f"## Release check — {args.version}: PASS")
    print("Version surfaces agree, every merged PR in the range is "
          "mentioned in the draft release notes and the internal notes, "
          "and the draft notes pass the US-spelling scan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
