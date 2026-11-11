"""CONTRIBUTING's required-checks text matches branch protection (#1141).

Branch protection on ``main`` is an owner-only setting that CI cannot read
without an admin token, so this test pins the list the owner set (read from
``gh api repos/catduckgnaf/print-handy/branches/main/protection`` on
2026-11-10: ``strict`` false, five contexts) and checks three things:

1. every place in CONTRIBUTING.md that lists the required checks lists
   exactly that set, and any count it gives matches;
2. every required name is a check that ``ci.yml`` actually reports, so a job
   rename that would silently orphan a required check fails here;
3. the "not up to date" wording matches ``strict: false``.

When the owner changes branch protection, update ``REQUIRED`` and the text
in the same PR.
"""

import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRIBUTING = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

REQUIRED = {
    "offline-tests (3.12)",
    "offline-tests (3.14)",
    "spoolman",
    "Slicer test (stable)",
    "discord-timing",
}
NUMBERS = {"four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}


def paragraph_starting(prefix):
    for block in CONTRIBUTING.split("\n"):
        if block.startswith(prefix):
            return block
    raise AssertionError(f"CONTRIBUTING.md has no line starting {prefix!r}")


def backticked(text):
    return set(re.findall(r"`([^`]+)`", text))


def ci_check_names():
    """The check names ci.yml reports: job name (or key), matrix expanded."""
    workflow = yaml.load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"),
                         Loader=yaml.BaseLoader)
    names = set()
    for key, job in workflow["jobs"].items():
        name = job.get("name", key)
        matrix = job.get("strategy", {}).get("matrix", {})
        values = matrix.get("python-version") if isinstance(matrix, dict) else None
        if "${{ matrix.python-version }}" in name and isinstance(values, list):
            for value in values:
                names.add(name.replace("${{ matrix.python-version }}", value))
        else:
            names.add(name)
    return names


class RequiredChecksText(unittest.TestCase):
    def test_canonical_list_matches_branch_protection(self):
        line = paragraph_starting("Checks branch protection requires on `main`")
        self.assertEqual(backticked(line) - {"main"}, REQUIRED)
        self.assertEqual(backticked(line) & ci_check_names(), REQUIRED)

    def test_merge_bullet_lists_the_same_set(self):
        line = paragraph_starting("- **Merge:**")
        # Every backticked CI check name in the bullet, and only those.
        self.assertEqual(backticked(line) & ci_check_names(), REQUIRED)
        self.assertIn("discord-timing", line)

    def test_merging_paragraph_lists_all_or_none(self):
        # A partial list is how the text drifted from branch protection
        # (#1141: "four checks" while five were required).
        line = paragraph_starting("- Merging runs on the required checks")
        named = backticked(line) & REQUIRED
        self.assertIn(named, (set(), REQUIRED), named)
        self.assertNotRegex(line, r"offline tests on both Python versions, spoolman")

    def test_every_count_of_required_checks_matches(self):
        for match in re.finditer(r"\b(four|five|six|seven|eight) (?:required )?checks above\b",
                                 CONTRIBUTING):
            self.assertEqual(NUMBERS[match.group(1)], len(REQUIRED), match.group(0))

    def test_branches_need_not_be_up_to_date(self):
        # strict: false in branch protection.
        self.assertIn("do **not** require the branch to be up to date", CONTRIBUTING)

    def test_every_required_check_is_reported_by_ci(self):
        self.assertLessEqual(REQUIRED, ci_check_names())


if __name__ == "__main__":
    unittest.main()
