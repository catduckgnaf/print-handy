"""Pinned fit option x color mode journey inventory (#594), run through the real intake offline.

Every row drives the production intake on a generated CC0 model with the fake Discord adapter. A row
is only listed when the prompts it needs are really offered; UNSUPPORTED documents combinations the
product does not offer, so nobody has to invent them. Adding a row means a new, reviewed line here.
"""
import tempfile
from pathlib import Path

import pytest

from . import harness

ONE = "One color for everything"
GROUP = "Choose colors"
EACH = "Choose every part individually"

# (case, color button, fit button or None, expected status, expected plate_mode or None)
SUPPORTED = [
    ("must-shrink", None, "Shrink to", "STARTED", None),
    ("split-plan", ONE, "split 2 plates", "STARTED", "split"),
    ("split-plan", GROUP, "split 2 plates", "STARTED", "split"),
    ("split-plan", ONE, "rotated 90", "STARTED", None),
    ("many-parts", ONE, None, "STARTED", None),
    ("many-parts", GROUP, None, "STARTED", "merge"),
    ("many-parts", EACH, None, "STARTED", "merge"),
    ("four-colors-one-plate", GROUP, None, "STARTED", "merge"),
]
# Combinations the product does not offer in this harness, with the reason (documented, not run).
UNSUPPORTED = {
    ("must-shrink", "color-mode prompt"): "single-part model: no color-mode prompt is asked",
    ("must-shrink", "split"): "a single part larger than the bed has no split option, only shrink",
    ("many-parts", "fit prompt"): "8 small parts fit one bed: no fit prompt is shown",
    ("duplo-rearrange", "fake slicer"): "per-part rearrange needs the private Duplo model and the real slicer geometry",
}


def clicks(color, fit):
    rules = []
    if color:
        rules.append({"question": "Inspected", "button": color})
    rules.append({"question": "Choose", "button": "White"})
    if fit:
        rules.append({"question": "does not fit", "button": fit})
    rules += [{"question": "How many copies", "button": "One copy"},
              {"question": "Which plate", "button": "Plate 1"},
              {"question": "·", "button": "Slice and print"}]
    return rules


def ids(row):
    return "-".join(str(part).replace(" ", "_") for part in row[:3])


@pytest.mark.parametrize("row", SUPPORTED, ids=[ids(r) for r in SUPPORTED])
def test_journey(row, tmp_path):
    case_name, color, fit, status, plate_mode = row
    case = next(harness.load_case(f) for f in harness.case_dirs() if f.name == case_name)
    case.clicks = clicks(color, fit)
    result = harness.run_case(case, tmp_path)
    asked = [q["question"] for q in result.adapter.questions]
    assert result.receipt.get("status") == status, asked
    if color:
        assert any("Inspected" in q for q in asked)
    if fit:
        assert any("does not fit" in q for q in asked)
        picked = [q["picked"] for q in result.adapter.questions if "does not fit" in q["question"]]
        assert fit.casefold() in picked[0].casefold()
    assert result.gcodes, "a started journey leaves sliced G-code"
    mode = (result.job.get("plate_merge") or {}).get("mode") or result.job.get("plate_mode")
    assert mode == plate_mode, (case_name, color, fit, mode)


def test_inventory_is_pinned():
    names = {f.name for f in harness.case_dirs()}
    assert {r[0] for r in SUPPORTED} <= names
    assert len(SUPPORTED) == len(set(SUPPORTED)), "duplicate journey row"
    assert all(reason for reason in UNSUPPORTED.values())
