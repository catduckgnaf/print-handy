#!/usr/bin/env python3
"""Coverage floors for the guard modules only; stdlib only.

Reads a Cobertura ``coverage.xml`` (the format the offline CI legs
already produce and upload to Codecov) and checks the line rate of a
small floor table against it. This is deliberately NOT a global
coverage percentage: only the modules that guard a print start or word
a refusal get a floor, so a dip in a safety path fails loudly without
turning coverage into a number the whole suite chases.

The guard modules, and why each earns a floor:

* ``print_handy/core/slice_guard.py`` -- the pre-slice guard
  (``pre_slice``): profile, nozzle, protected-settings and bounds
  checks that can block a slice before any file is produced.
* ``print_handy/core/launch_state.py`` -- the print-start launch
  guard: one submission lease per printer and consumed bed-clear
  approvals, in one transaction. A regression here can double-start
  or start without an approval.
* ``print_handy/core/slice_refusal.py`` -- deterministic slice
  refusals: the plain-language reason and the one safe fix a refusal
  may offer.
* ``print_handy/core/slice_failure.py`` -- the failure side of the
  same seam: how a failed slice is classified and worded.
* ``print_handy/diagnostics.py`` -- the friendly-error path:
  ``plain_post`` and the error-code wording every refusal post in
  chat goes through.

Floors are set below what the current suite achieves. Measuring note
(2026-11-07): a focused run of the guard tests alone
(``tests/core/test_slice_guard.py``, ``tests/pipeline/test_launch_state.py``,
``tests/core/test_slice_failure.py``, ``tests/test_friendly_errors.py``,
``tests/test_diagnostics.py``; 163 passed) measured line rates of
0.736 / 0.707 / 0.333 / 0.893 / 0.720 for the five modules above, in
table order. The full offline suite exercises these modules from many
more tests, so its rates are at or above the focused ones; each floor
below sits a few points under the focused measurement, never above
it. Before this check gates anything, confirm the table once against
a full-suite ``coverage.xml`` from CI and raise any floor that has
honest headroom.

Usage: ``python3 scripts/coverage_floors.py coverage.xml`` -- exit 1
and one line per problem (below floor, or module missing from the
report) when any floor is missed; exit 0 when every floor holds.
"""
import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Module path (as Cobertura records it, relative to the repo root) ->
# minimum line rate. Data, not code: raising a floor is a one-line
# edit here, reviewed like any other change.
FLOORS = {
    "print_handy/core/slice_guard.py": 0.65,
    "print_handy/core/launch_state.py": 0.62,
    "print_handy/core/slice_refusal.py": 0.28,
    "print_handy/core/slice_failure.py": 0.80,
    "print_handy/diagnostics.py": 0.62,
}


def rates_from_xml(root):
    """Map Cobertura class filename -> line rate (float)."""
    rates = {}
    for element in root.iter("class"):
        name = element.get("filename")
        rate = element.get("line-rate")
        if name is not None and rate is not None:
            rates[name.replace("\\", "/")] = float(rate)
    return rates


def load_rates(path):
    return rates_from_xml(ET.parse(path).getroot())


def check_floors(rates, floors=None):
    """One problem string per missed floor; empty when every floor holds."""
    problems = []
    for module, floor in sorted((floors or FLOORS).items()):
        if module not in rates:
            problems.append(f"{module}: missing from the coverage report "
                            f"(floor {floor:.0%} cannot be verified)")
        elif rates[module] < floor:
            problems.append(f"{module}: line coverage {rates[module]:.1%} "
                            f"is below the floor {floor:.0%}")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("coverage_xml", type=Path,
                        help="a Cobertura coverage.xml from the offline suite")
    args = parser.parse_args(argv)
    rates = load_rates(args.coverage_xml)
    problems = check_floors(rates)
    for problem in problems:
        print(problem)
    print(f"Coverage floors: {len(problems)} problem(s); "
          f"{len(FLOORS)} guard modules checked.")
    return bool(problems)


if __name__ == "__main__":
    raise SystemExit(main())
