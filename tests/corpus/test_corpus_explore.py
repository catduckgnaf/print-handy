"""Every button, not just the first one: the corpus explorer (#1373).

The flow corpus answers each question with its case's first matching [[click]] rule, so a
case exercises one path through the intake and is judged by its receipt. A live hard intake
showed what that misses: a menu offering a choice before its numbers existed, the same short
spool asked about twice, "without supports" printing with supports. harness.explore re-runs a
case once per alternative button at every question it asked, and holds every path to
harness.path_invariants (a cancel starts nothing, a support choice is what the G-code says,
no question offers "with supports" while it reads "slicing...", and so on).

CI explores a fixed set of committed cases with a bounded number of runs each. Set
PRINT_HANDY_CORPUS_EXPLORE=all to walk every committed case without a bound (slow), or to a
comma-separated list of case names.
"""
import os

import pytest

from . import harness, spool_fixture

# Committed cases whose questions cover the decision points: supports, fit (shrink, rotate,
# split), color modes, many parts, multiple plates, a foreign printer's project.
CI_CASES = ("needs-supports", "stl-needs-supports", "must-shrink", "rotation-fit", "split-plan",
            "many-parts", "four-colors-one-plate", "plates-multicolor", "foreign-bambu-profile",
            "witch-ghost-cults3d")
CI_MAX_RUNS = 10


def _selected():
    wanted = os.environ.get("PRINT_HANDY_CORPUS_EXPLORE", "").strip()
    dirs = {d.name: d for d in harness.case_dirs() if not harness.is_private_case(d)}
    if wanted == "all":
        return sorted(dirs), None
    if wanted:
        return [n for n in wanted.split(",") if n in dirs], None
    return [n for n in CI_CASES if n in dirs], CI_MAX_RUNS


NAMES, MAX_RUNS = _selected()


def _case(name):
    return harness.load_case(next(d for d in harness.case_dirs() if d.name == name))


def _report(failures):
    return "\n\n".join(f"PATH {label}\n  " + "\n  ".join(problems) for label, problems in failures)


@pytest.mark.parametrize("name", NAMES)
def test_every_offered_button_keeps_the_product_rules(name, tmp_path):
    case = _case(name)
    if getattr(case, "entry", None):
        pytest.skip("typed and print-next entry cases have no drop journey to explore")
    failures, runs = harness.explore(case, tmp_path, max_runs=MAX_RUNS)
    assert runs >= 1
    assert not failures, _report(failures)


SHORT_SPOOLS = tuple((slot, brand, material, color, 5) for slot, brand, material, color, _ in spool_fixture.SPOOLS)


@pytest.mark.parametrize("name", ["four-colors-one-plate", "single-part"])
def test_a_short_spool_is_asked_about_once_on_every_path(name, tmp_path):
    """Every spool holds 5 g: the shortage shows on the Ready question and the start never asks again."""
    case = _case(name)
    failures, runs = harness.explore(case, tmp_path, max_runs=6, spools=SHORT_SPOOLS)
    assert runs >= 1
    assert not failures, _report(failures)
