"""Pinned fit option x color mode journey inventory (#594), run through the real intake offline.

Every row drives the production intake on a generated CC0 model with the fake Discord adapter. A row
is only listed when the prompts it needs are really offered; UNSUPPORTED documents combinations the
product does not offer, so nobody has to invent them. Adding a row means a new, reviewed line here.
"""
from types import SimpleNamespace as S

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
    ("split-plan", GROUP, "rotated 90", "STARTED", "split"),  # per-part resized job: one fitted plate per slot
    ("many-parts", ONE, None, "STARTED", None),
    ("many-parts", GROUP, None, "STARTED", "merge"),
    ("many-parts", EACH, None, "STARTED", "merge"),
    ("four-colors-one-plate", GROUP, None, "STARTED", "merge"),
]
# Combinations the product does not offer in this harness, with the reason (documented, not run).
UNSUPPORTED = {
    ("split-plan", "every part individually"): "two parts only offer one color, choose colors, or cancel; the per-part button needs three or more parts",
    ("must-shrink", "color-mode prompt"): "single-part model: no color-mode prompt is asked",
    ("must-shrink", "split"): "a single part larger than the bed has no split option, only shrink",
    ("many-parts", "fit prompt"): "8 small parts fit one bed: no fit prompt is shown",
    ("duplo-rearrange", "private geometry"): "private Duplo is not in CI; generated four-part rearrange is exercised below, not excluded",
    ("split-plan", "shrink button"): "the offered buttons are split, rotated 96.3%, and cancel; shrink is not a separate button for this geometry",
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
    # The chosen fit option decides scale and plate count; nothing is left implicit.
    scale = result.receipt.get("scale") or 1.0
    if fit and "split" in fit:
        assert scale == 1.0 and len(result.gcodes) == 2, (scale, result.gcodes)
    elif fit and "rotated" in fit:
        assert 0.95 <= scale < 1.0 and len(result.gcodes) == 1, (scale, result.gcodes)
    elif fit:
        assert 0.78 <= scale <= 0.84 and len(result.gcodes) == 1, (scale, result.gcodes)
    else:
        assert scale == 1.0
    assert len(result.starts) == 1, "exactly one start per journey"
    for row in harness.gcode_checks(result):
        assert row["mapping"], "embedded tool to slot map"
        assert row["guard"].status != "blocked"


@pytest.mark.parametrize("case_name", ["must-shrink", "split-plan"])
def test_cancelling_the_fit_prompt_slices_nothing_and_starts_nothing(case_name, tmp_path):
    case = next(harness.load_case(f) for f in harness.case_dirs() if f.name == case_name)
    case.clicks = [{"question": "Inspected", "button": ONE}, {"question": "Choose", "button": "White"},
                   {"question": "does not fit", "button": "Cancel"}]
    result = harness.run_case(case, tmp_path)
    assert any("does not fit" in q["question"] for q in result.adapter.questions)
    assert result.receipt.get("status") != "STARTED", result.receipt
    assert not result.starts and not result.slice_starts and not result.gcodes
    assert not [e for e in result.events if e[0] == "slice"], "no slicer run after a refused fit"


@pytest.mark.parametrize("color", [
    pytest.param(ONE, marks=pytest.mark.xfail(strict=True, reason=(
        "one-color auto-rearrange rewrites the source but leaves color_decisions bound to the original "
        "SHA256; slice_model rejects it before slicing (#594 phase 2 product defect)"))), GROUP])
def test_generated_rearrange_journey(color, tmp_path):
    from . import make_models
    model = tmp_path / "rearrange.3mf"
    make_models.write_3mf(model,
        [(f"Part {i}", 1, make_models.box(30, 20, 5), (i * 100, 0)) for i in range(4)],
        [("Plate 1", list(range(4)))], [("#FFFFFF", "PLA")])
    case = S(name="generated-rearrange", model=model, clicks=clicks(color, None))
    result = harness.run_case(case, tmp_path / "run")
    assert result.receipt.get("fit_auto_rearranged") is True
    assert result.receipt.get("scale") == 1.0
    assert result.receipt.get("status") == "STARTED", result.receipt
    assert len(result.gcodes) == 1 and len(result.starts) == 1


def test_inventory_is_pinned():
    names = {f.name for f in harness.case_dirs()}
    assert {r[0] for r in SUPPORTED} <= names
    assert len(SUPPORTED) == len(set(SUPPORTED)), "duplicate journey row"
    assert all(reason for reason in UNSUPPORTED.values())
