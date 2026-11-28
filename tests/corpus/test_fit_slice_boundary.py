"""Fit-intake and slice-volume boundary parity for the public fit corpus (#1947)."""
import math
import tomllib

import pytest
from pathlib import Path

from print_handy.core import orca_adapter
from print_handy.jobs import slice as slice_jobs


def test_face_change_case_keeps_fit_question_and_slice_volume_agree():
    """The face-change corpus model is asked, not silently rotated into a fit."""
    case = Path(__file__).parent / "cases" / "rotation-fit" / "case.toml"
    data = tomllib.loads(case.read_text())
    assert "face change" in data["description"]
    dims = tuple(data["model"]["part"][0]["size"])
    state = {"bed_mm": [220, 220], "max_print_height_mm": 220}
    assert not orca_adapter.fit_envelope_fits(dims, state["bed_mm"], state["max_print_height_mm"], margin_mm=0)
    assert not slice_jobs._volume_precheck(*dims, 1.0, state)
    assert any("does not fit the bed full size" in click["question"] for click in data["click"])


def test_diagonal_turn_is_full_size_fit_at_intake_and_slice_boundary():
    """A 231x74 footprint fails axis-aligned bounds but fits after in-plane turning."""
    dims = (231, 74, 85)
    state = {"bed_mm": [220, 220], "max_print_height_mm": 220}
    angle = math.radians(45)
    footprint_x, footprint_y = dims[:2]
    rotated_x = abs(footprint_x * math.cos(angle)) + abs(footprint_y * math.sin(angle))
    rotated_y = abs(footprint_x * math.sin(angle)) + abs(footprint_y * math.cos(angle))
    rotated = (rotated_x, rotated_y, dims[2])

    assert rotated_x == pytest.approx(305 / math.sqrt(2))
    assert rotated_y == pytest.approx(305 / math.sqrt(2))
    assert rotated_x < state["bed_mm"][0]
    assert rotated_y < state["bed_mm"][1]
    assert not orca_adapter.fit_envelope_fits(dims, state["bed_mm"], state["max_print_height_mm"], margin_mm=0)
    assert orca_adapter.fit_envelope_fits(rotated, state["bed_mm"], state["max_print_height_mm"], margin_mm=0)
    assert slice_jobs._volume_precheck(*rotated, 1.0, state)


def test_rotation_fit_runs_through_production_intake_and_slice_path(tmp_path):
    """The corpus's real Discord journey reaches slicing after the fit decision."""
    from tests.corpus import harness

    case = harness.load_case(Path(__file__).parent / "cases" / "rotation-fit")
    run = harness.run_case(case, tmp_path, real_slicer=False)
    harness.check_expectations(case, run)
    assert run.slice_starts, "production BackgroundSlice.start was never reached"
    assert any("does not fit the bed full size" in prompt["question"]
               for prompt in run.adapter.questions), run.adapter.questions
    assert run.receipt.get("scale", 1.0) == 1.0
