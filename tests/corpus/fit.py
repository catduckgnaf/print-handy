"""Corpus model sizes computed from the real merge planner instead of magic millimeters (#824).

"Roomy", "tight" and "no room" used to be hand-picked numbers (120 mm, 150 mm, 214 mm) that silently
stop meaning what they say when the part spacing, the bed edge margin or the tower math changes. Here a
size is found by asking print_handy.core.plate_merge.plan, the planner the Discord flow itself calls, whether
a trial model merges onto one bed:

  merge_limit   the largest footprint that still merges with a prime tower (two or more colors)
  alone_limit   the largest footprint that fits the bed with no tower at all (one color)

  roomy  2/3 of merge_limit
  tight  merge_limit minus a margin (default 5 mm): merges, with little room to spare
  none   alone_limit plus a margin (default 10 mm): nothing merges and the model overflows the bed

The planner reads its spacing and edge margins from the settings at import. The test suite pins the
"medium" layout preset in tests/conftest.py (10 mm part spacing, 8 mm edge); the generator sets the same
preset before the first print_handy import (tests/corpus/make_models.py), so a size computed by
`python -m tests.corpus.make_models` equals the one computed inside pytest.

This module does not import pytest.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from . import printer_overrides

FIXTURE_STATE = Path(__file__).resolve().parents[1] / "pipeline" / "fixtures" / "printer_hardware.json"
STANDARD_SPACING_MM = 10.0  # the "medium" layout preset the test suite pins (tests/conftest.py)
ROOMY_FRACTION = 2 / 3
TIGHT_MARGIN_MM = 5
NONE_MARGIN_MM = 10
FITS = ("roomy", "tight", "none")


def printer_state(printer=None):
    """The shared hardware record with a case's [printer] nozzle and bed applied."""
    return printer_overrides.apply_to_state(json.loads(FIXTURE_STATE.read_text()), printer)


def _require_standard_layout(plate_merge):
    if plate_merge.SPACING_MM != STANDARD_SPACING_MM:
        raise RuntimeError(
            f"corpus sizes are computed with {STANDARD_SPACING_MM:g} mm part spacing (the pinned "
            f"layout.spacing = medium preset), but the planner has {plate_merge.SPACING_MM:g} mm: "
            "unset PRINT_HANDY__LAYOUT__SPACING and PRINT_HANDY_CONFIG, or set the spacing to medium")


def merges(source, colors, printer=None):
    """Whether plate_merge.plan puts every object of `source` on one bed, the way the flow plans it.

    `colors` is how many distinct filament slots the parts use; one color needs no prime tower. Only
    plain rectangle packing counts: a fit that needs the polygon or turned fallbacks is a layout the
    corpus would not want to sit on the edge of.
    """
    from print_handy.core import model_job, plate_merge, source_colors
    _require_standard_layout(plate_merge)
    state = printer_state(printer)
    manifest = source_colors.inventory(source)
    plates = model_job.plate_manifest(source)["plates"]
    parts = manifest["parts"]
    slots = {part["key"]: index % colors + 1 for index, part in enumerate(parts)}
    capacity = max(int((printer or {}).get("slots", printer_overrides.MAX_SLOTS)), colors)
    with printer_overrides.patched(printer):
        verdict = plate_merge.plan(source, manifest, plates, slots, state, capacity=capacity)
    return verdict["mode"] == "merge" and verdict.get("packing") == "rectangle"


def largest_width(write, colors, printer=None, ceiling=400):
    """The largest whole millimeter width for which the trial model `write(path, width)` merges.

    Merging gets harder as the width grows, so a bisection finds the edge. Returns 0 when even a
    1 mm model does not merge (a bed too small for the tower).
    """
    with tempfile.TemporaryDirectory() as folder:
        trial = Path(folder) / "trial.3mf"

        def fits(width):
            write(trial, width)
            return merges(trial, colors, printer)

        low, high = 0, ceiling
        while high - low > 1:
            middle = (low + high) // 2
            low, high = (middle, high) if fits(middle) else (low, middle)
        return low


def fitted_width(write, fit, colors, printer=None, margin=None):
    """The width in mm (an int) that `fit` ("roomy", "tight" or "none") means for this model and printer."""
    if fit not in FITS:
        raise ValueError(f"fit must be one of {FITS}, got {fit!r}")
    if fit == "none":
        # The planner's bed limit with no tower: one color, so nothing reserves room for a tower.
        return largest_width(write, 1, printer) + (NONE_MARGIN_MM if margin is None else margin)
    limit = largest_width(write, colors, printer)
    if limit < 1:
        raise ValueError("no model fits this printer with a prime tower")
    if fit == "roomy":
        return max(1, int(limit * ROOMY_FRACTION))
    return max(1, limit - (TIGHT_MARGIN_MM if margin is None else margin))
