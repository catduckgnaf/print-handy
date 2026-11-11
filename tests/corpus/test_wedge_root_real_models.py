"""Real-model cases for the support-clearance wedge-root rule (#1151, #1195).

The rule's regressions in tests/pipeline/test_support_clearance_phase2.py are
synthetic meshes. These cases pin the rule on real published models from the
corpus-v1 release, which the model-corpus workflow downloads and verifies
(the files are not in this repository). Each model has a real bottom
chamfer rooted at the plate:

* the 10-to-19 inch rack adapter (the cad_split preset's source) has a
  straight chamfer strip narrower than 2 mm, excused on its bounding width;
* the towel hook, the planter and the QR stand have chamfers that run round
  the whole base, so their bounding width is the base's width. They are
  excused only through the across-slope width on a region proved to be a
  thin strip (#1137), and without that branch each would be flagged.

Measured on 2026-11-11 over all 171 corpus-v1 models: classifying with the
thin-strip proof disabled (the loosened pre-#1137 rule) changes no model's
wedge roots or risk findings, so no real model in the corpus reproduces the
wide-faceted-underside defect; the synthetic sawtooth stays its regression.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from print_handy.core import support_clearance

ROOT = os.environ.get("PRINT_HANDY_PRIVATE_CORPUS")
pytestmark = pytest.mark.skipif(not ROOT, reason="corpus-v1 not downloaded here; the model-corpus "
                                                 "workflow sets PRINT_HANDY_PRIVATE_CORPUS")

RACK = ("002-10to19inadapter--4e1a3ff6af8d6a6c5454e709d57e0f714aee777ab1be3878acb1e1a3a31da727"
        "/10to19inadapter.stl")
HOOK = ("013-auto-locking-hanger-gravity-towel-hook-ribbed--"
        "2c1b9f92640f4b6ab17cdc26083e6b7bcd7f5e6216a603015e5ba12a1e53826f/Auto-locking Hanger (Tape).3mf")
PLANTER = ("014-barista-buddy-cute-planter--3c3689213ed3065e08c17ca79a09e37762824402e91aaf900bf33af70c357d97"
           "/60mm_SizeAdjustable.3mf")
QR = ("039-customizable-qr-code-with-stand--9332d1d7b1334c862d559d7d5db2b08dbd173288114c95dbb3f22b6a76d2f39b"
      "/qr plate (1).3mf")

# model: (wedge-root (area mm2, bounding width mm) pairs, risk findings now,
#         risk findings with the across-slope width refused)
CASES = {
    RACK: ([(65.8, 1.5)], 0, 0),
    HOOK: ([(282.0, 28.6)], 10, 11),
    PLANTER: ([(22.2, 2.4), (191.0, 55.7)], 0, 2),
    QR: ([(163.0, 38.0)], 0, 1),
}


def _summary(path):
    report = support_clearance.analyze(str(path))
    roots = sorted((r["area_mm2"], r["width_mm"]) for r in report["regions"]
                   if r.get("kind") == "sloped_wedge_root")
    risks = sum(1 for f in report["findings"] if f.get("severity") == "risk")
    return roots, risks, report


@pytest.mark.parametrize("model", list(CASES), ids=["rack-adapter", "towel-hook", "planter", "qr-stand"])
def test_real_bottom_chamfers_are_wedge_roots_and_the_gate_still_fires(model, monkeypatch):
    path = Path(ROOT) / model
    assert path.is_file(), f"corpus-v1 model missing: {model}"
    roots, risks, report = _summary(path)
    expected_roots, expected_risks, strict_risks = CASES[model]
    assert roots == sorted(expected_roots)
    assert risks == expected_risks
    for region in report["regions"]:
        if region.get("kind") == "sloped_wedge_root":
            # Every excused region is a sloped face rooted at the plate in the
            # self-supporting band, never a floating underside.
            assert region["sloped"] and region["over"] == "plate"
            assert region["clearance_mm"]["min"] < 0.2
            assert support_clearance.WEDGE_COS_MIN <= region["slope_nz"] <= support_clearance.WEDGE_COS_MAX
    # The cases exercise the branch they claim: refusing the across-slope
    # width turns every wide chamfer into a flagged low-clearance finding,
    # and the narrow rack strip stays excused on its bounding width.
    monkeypatch.setattr(support_clearance, "_thin_strip", lambda *args, **kwargs: False)
    strict_roots, strict, _ = _summary(path)
    assert strict == strict_risks
    assert strict_roots == sorted(r for r in expected_roots if r[1] < support_clearance.WEDGE_WIDTH_MM)
