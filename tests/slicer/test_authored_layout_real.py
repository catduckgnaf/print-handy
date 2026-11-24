"""The merge planner keeps an authored layout that already meets every constraint (#1862).

The planner used to re-pack every object of a one-plate source, so parts the
author had seated with room to spare received per-object translations; on a
captured incident those translations alone took the Orca slice from success to
a G-code conflict (-101). When the authored relative placement already clears
spacing, edge, exclusion and tower-lane checks, the plan is now one common
translation. These tests drive the production planner and the real Orca CLI
with synthetic boxes generated here (CC0, no third-party geometry):

* a layout that meets every constraint keeps its relative offsets, and slices;
* a layout that does not (parts closer than the spacing, a span wider than
  the bed, several authored plates, a requested rearrangement) is still
  repacked as before, and slices.

Skipped unless PRINT_HANDY_REAL_SLICER=1, like every slicer-tier test.
"""
import json
import os
import tempfile
import zipfile
from pathlib import Path

import pytest

from print_handy.core import color_geometry, orca_adapter, plate_merge, source_colors
from print_handy.gcode import verify as gcode_verify
from print_handy.jobs import slice as jobs_slice
from tests.corpus import make_models as mm
from tests.slicer import test_spacing_minimums_real as sp

REAL = os.environ.get("PRINT_HANDY_REAL_SLICER") == "1"
real_only = pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CLI")
WORK = sp.WORK
GAP = plate_merge.SPACING_MM


def _write(path, rows, plates=None):
    """rows: [(w, d, h, (x, y), extruder)]; one plate unless plates are given (indices into rows)."""
    shapes = [(f"Part {i + 1}", ext, mm.box(w, d, h), at) for i, (w, d, h, at, ext) in enumerate(rows)]
    mm.write_3mf(path, shapes, plates or [("Plate 1", list(range(len(rows))))],
                 [("#FFFFFF", "PLA"), ("#FF00FF", "PLA")])
    return path


def _plan(src, state, **kwargs):
    manifest = source_colors.inventory(src)
    decisions = sp._decisions(state, manifest, second_slot_object=3)
    view = jobs_slice._per_part_view(manifest, decisions)
    slots = jobs_slice._part_slots(view, manifest)
    verdict = plate_merge.plan(src, manifest, jobs_slice._plates_for(src, manifest), slots, state, bed=220, **kwargs)
    return manifest, decisions, verdict


def _authored_boxes(src, manifest):
    with zipfile.ZipFile(src) as archive:
        return {str(p["object_id"]): color_geometry._world_bounds(archive, {str(p["object_id"])})[0]
                for p in manifest["parts"]}


def _slice(src, out, state, decisions, verdict):
    gcode, provenance, _cfg = orca_adapter.slice_model(
        src, out, state, None, color_decisions=decisions, item_shifts=verdict["shifts"])
    gcode_verify.verify_gcode_offline(gcode, state)
    extents = sp._extents(gcode)
    assert extents is not None and 0 <= extents[0] and extents[1] <= 220 and 0 <= extents[2] and extents[3] <= 220
    return provenance


def _placed(provenance, manifest):
    by_object = {}
    for row in provenance["geometry"]["parts"]:
        object_id = next(str(p["object_id"]) for p in manifest["parts"] if p["key"] == row["part_key"])
        by_object[object_id] = [tuple(axis) for axis in row["bounds_mm"][:2]]
    return by_object


@pytest.fixture
def work():
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as directory:
        yield Path(directory)


# Two colors, one plate, parts well apart: nothing needs to move relative to anything else.
SPREAD = [(40, 40, 20, (40, 40), 1), (30, 30, 10, (40 + 40 + GAP + 5, 40), 2), (20, 20, 8, (40, 40 + 40 + GAP + 5), 2)]


@real_only
def test_authored_layout_is_kept_and_slices_in_the_real_slicer(work):
    state = sp._state()
    src = _write(work / "src.3mf", SPREAD)
    manifest, decisions, verdict = _plan(src, state)
    assert verdict["mode"] == "merge" and verdict["packing"] == "authored"
    # One rigid translation: every object moves by the same vector.
    moves = {tuple(round(v, 6) for v in shift[:2]) for shift in verdict["shifts"].values()}
    assert len(moves) == 1
    provenance = _slice(src, work / "out", state, decisions, verdict)
    before, after = _authored_boxes(src, manifest), _placed(provenance, manifest)
    ids = sorted(before)
    for a in ids:
        for b in ids:
            for axis in (0, 1):
                assert after[b][axis][0] - after[a][axis][0] == pytest.approx(
                    before[b][axis][0] - before[a][axis][0], abs=1e-3)


@real_only
def test_parts_closer_than_the_spacing_are_still_repacked_and_slice(work):
    state = sp._state()
    rows = [SPREAD[0], (30, 30, 10, (40 + 40 + 2, 40), 2), SPREAD[2]]  # 2 mm apart: below the merge spacing
    src = _write(work / "src.3mf", rows)
    manifest, decisions, verdict = _plan(src, state)
    assert verdict["mode"] == "merge" and verdict["packing"] != "authored"
    provenance = _slice(src, work / "out", state, decisions, verdict)
    placed = _placed(provenance, manifest)
    for a in placed:
        for b in placed:
            if a < b:
                gap = max(placed[b][0][0] - placed[a][0][1], placed[a][0][0] - placed[b][0][1],
                          placed[b][1][0] - placed[a][1][1], placed[a][1][0] - placed[b][1][1])
                assert gap >= GAP - 1e-3


@real_only
def test_a_layout_wider_than_the_bed_is_still_repacked_and_slices(work):
    state = sp._state()
    rows = [(40, 40, 20, (5, 5), 1), (30, 30, 10, (205, 150), 2), (20, 20, 8, (100, 100), 2)]  # span > 220 mm
    src = _write(work / "src.3mf", rows)
    manifest, decisions, verdict = _plan(src, state)
    assert verdict["mode"] == "merge" and verdict["packing"] != "authored"
    _slice(src, work / "out", state, decisions, verdict)


@real_only
def test_several_authored_plates_are_still_merged_by_the_packer_and_slice(work):
    state = sp._state()
    src = _write(work / "src.3mf", SPREAD, plates=[("Plate 1", [0, 1]), ("Plate 2", [2])])
    manifest, decisions, verdict = _plan(src, state)
    assert verdict["mode"] == "merge" and verdict["packing"] != "authored"
    _slice(src, work / "out", state, decisions, verdict)


# Offline guards: the same planner without the slicer.

def test_authored_layout_is_refused_when_a_declared_exclusion_touches_a_part(tmp_path, monkeypatch):
    from print_handy.core import split_fit
    src = _write(tmp_path / "src.3mf", SPREAD)
    manifest = source_colors.inventory(src)
    boxes = _authored_boxes(src, manifest)
    args = (boxes, [{"id": 1}], 220, GAP, plate_merge.EDGE_MARGIN_MM, 0.0, False, {}, None)
    kept = plate_merge._authored_layout(*args)
    assert kept is not None
    monkeypatch.setattr(split_fit, "_declared_exclusions", lambda state=None: [(0.0, 0.0, 220.0, 220.0)])
    assert plate_merge._authored_layout(*args) is None


def test_authored_layout_is_refused_when_a_rearrangement_was_requested(tmp_path):
    src = _write(tmp_path / "src.3mf", SPREAD)
    boxes = _authored_boxes(src, source_colors.inventory(src))
    job = {"full_size_rearrange_plan": {"rearrange_plate": True}}
    assert plate_merge._authored_layout(boxes, [{"id": 1}], 220, GAP, plate_merge.EDGE_MARGIN_MM, 0.0, False, {}, job) is None


def test_authored_layout_keeps_the_bed_edge_clearance_and_the_prime_tower_lane(tmp_path, monkeypatch):
    src = _write(tmp_path / "src.3mf", SPREAD)
    boxes = _authored_boxes(src, source_colors.inventory(src))
    # Edge clearance so large that the 85 mm layout cannot fit the usable area.
    assert plate_merge._authored_layout(boxes, [{"id": 1}], 220, GAP, 100.0, 0.0, False, {}, None) is None

    def no_lane(*args, **kwargs):
        raise RuntimeError("no prime tower lane")
    monkeypatch.setattr(color_geometry, "prime_tower_xy", no_lane)
    assert plate_merge._authored_layout(boxes, [{"id": 1}], 220, GAP, plate_merge.EDGE_MARGIN_MM, 45.0, True, {}, None) is None
