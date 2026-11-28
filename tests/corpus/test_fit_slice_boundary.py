"""Fit-intake and slice-volume boundary parity for the public fit corpus (#1947)."""
import tomllib
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
    rotated = (208.5, 208.5, 85)
    assert not orca_adapter.fit_envelope_fits(dims, state["bed_mm"], state["max_print_height_mm"], margin_mm=0)
    assert orca_adapter.fit_envelope_fits(rotated, state["bed_mm"], state["max_print_height_mm"], margin_mm=0)
    assert slice_jobs._volume_precheck(*rotated, 1.0, state)
