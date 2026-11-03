"""Per case printer variation for the corpus (#824): `[printer]` in case.toml over the shared printer_hardware.json.

Every corpus case used to run on the one printer in tests/pipeline/fixtures/printer_hardware.json (0.4 mm
nozzle, 220 mm bed, four slots). A case may now say

    [printer]
    nozzle = 0.25     # nozzle diameter in mm (the printer must support it)
    tower = 35        # prime tower width in mm, the configured [prime_tower] size_mm
    bed = 180         # square bed edge in mm
    slots = 2         # filament slots with a spool bound (the slot map the state holds)

and the harness (tests/corpus/harness.py run_case) and the fit sizing (tests/corpus/fit.py) both apply it
through this module, so a case is generated for, and run on, the same printer.

`tower` is the configured width, not the nozzle's: the planner uses max(configured width, the nozzle's
tuned width) (color_geometry.effective_tower_mm), and the shipped width is 45 mm, so a smaller nozzle
shrinks the tower only when the configured width is lowered with it.

This module does not import pytest.
"""
from __future__ import annotations

import contextlib
import copy
from unittest import mock

KEYS = ("nozzle", "tower", "bed", "slots")
MAX_SLOTS = 4  # spool_fixture.SPOOLS builds four spools


def validate(table):
    """The override table as numbers, or ValueError naming the first bad key."""
    table = dict(table or {})
    unknown = sorted(set(table) - set(KEYS))
    if unknown:
        raise ValueError(f"unknown [printer] key(s) {unknown}; use {list(KEYS)}")
    out = {}
    for key, value in table.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"[printer] {key} must be a positive number, got {value!r}")
        out[key] = int(value) if key == "slots" else float(value)
    if "slots" in out and not 1 <= out["slots"] <= MAX_SLOTS:
        raise ValueError(f"[printer] slots must be 1 to {MAX_SLOTS}, got {out['slots']}")
    return out


def apply_to_state(state, table):
    """A copy of a printer_hardware.json record with the nozzle and bed overrides applied.

    The slot count is not applied here: the slot map is built from spools (spool_fixture.build_state),
    so the harness binds fewer spools instead of editing the map. The printer adapter's own position
    count is the model's and stays as it is.
    """
    table = validate(table)
    state = copy.deepcopy(state)
    if "nozzle" in table:
        state.setdefault("nozzle", {})["diameter_mm"] = table["nozzle"]
    if "bed" in table:
        bed = table["bed"]
        state["bed_mm"] = [bed, bed]
        limits = state.setdefault("axis_limits_mm", {})
        for axis in ("x", "y"):
            limits[axis] = [0, bed + 3]  # the fixture's axes run 3 mm past the usable bed
    return state


@contextlib.contextmanager
def patched(table):
    """The process wide constants the planner reads at import, set for the overrides, restored after."""
    table = validate(table)
    with contextlib.ExitStack() as stack:
        if table:
            from print_handy.core import color_geometry, model_job, plate_merge
            patch = lambda *args: stack.enter_context(mock.patch.object(*args))  # noqa: E731
            if "tower" in table:
                patch(color_geometry, "TOWER_MM", table["tower"])
                patch(plate_merge, "TOWER_MM", table["tower"])
            if "bed" in table:
                bed = int(table["bed"])
                patch(color_geometry, "BED_MM", bed)
                patch(model_job, "BED_MM", bed)
                patch(model_job, "BED_LIMIT_MM", bed)
        yield
