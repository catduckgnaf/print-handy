"""Deterministic multi-part design checks (#998), against the reference
Mac mini rack fixture (#1000) and synthetic meshes.

The reference design's known defects must be FOUND from geometry, not
from a README: the 88.5 mm ears are under the 2U pitch but over the 2U
panel clearance, and the 214 mm tray's 3 mm bed margin is unproven.
"""
from pathlib import Path

import pytest

from print_handy.core import assembly_check as ac
from print_handy.core.geometry_layout import write_binary_stl

FIXTURE = Path(__file__).resolve().parents[1] / "corpus" / "design" / "mac_mini_rack_issue_998"
AD5X_BED = (220, 220, 220)


def _box(path, dx, dy, dz, drop_one=False):
    v = [(0, 0, 0), (dx, 0, 0), (dx, dy, 0), (0, dy, 0),
         (0, 0, dz), (dx, 0, dz), (dx, dy, dz), (0, dy, dz)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    if drop_one:
        faces = faces[:-1]
    write_binary_stl([[v[a], v[b], v[c]] for a, b, c in faces], path)
    return path


# ---------------------------------------------------------------- reference fixture

@pytest.fixture(scope="module")
def reference_findings():
    return ac.review(sorted(FIXTURE.glob("*.stl")), bed_mm=AD5X_BED, rack_units=2)


def _codes(findings, level):
    return sorted((f.code, f.part) for f in findings if f.level == level)


def test_reference_ears_fail_panel_clearance_not_pitch(reference_findings):
    fails = _codes(reference_findings, "fail")
    assert fails == [("rack_panel_clearance", "02_RIGHT_2U_ARM.stl"),
                     ("rack_panel_clearance", "03_LEFT_2U_ARM.stl")]
    ear = next(f for f in reference_findings if f.code == "rack_panel_clearance")
    assert ear.evidence["height_mm"] == pytest.approx(88.5, abs=0.05)
    assert ear.evidence["unit_pitch_mm"] == pytest.approx(88.9)
    assert ear.evidence["panel_clearance_mm"] == pytest.approx(88.106, abs=0.001)


def test_reference_tray_margin_is_a_warning_not_a_pass(reference_findings):
    assert _codes(reference_findings, "warn") == [("bed_margin_unproven", "01_CENTER_TRAY_214x214.stl")]
    tray = next(f for f in reference_findings if f.code == "bed_margin_unproven")
    assert tray.evidence["edge_margin_mm"] == pytest.approx(3.0, abs=0.05)


def test_reference_parts_are_measured_watertight_and_classified(reference_findings):
    assert not any(f.code == "mesh_topology" for f in reference_findings)
    shape = next(f for f in reference_findings if f.code == "parts")
    assert shape.evidence["structural"] == ["01_CENTER_TRAY_214x214.stl", "02_RIGHT_2U_ARM.stl",
                                            "03_LEFT_2U_ARM.stl"]
    assert shape.evidence["coupons"] == ["00_TEST_LOWER_M4_NUT.stl", "00_TEST_UPPER_M4_BUTTON_HEAD.stl"]
    assert any(f.code == "mirror_ok" for f in reference_findings)
    dims = {f.part: f.evidence["dimensions_mm"] for f in reference_findings if f.code == "measured"}
    assert dims["01_CENTER_TRAY_214x214.stl"][:2] == pytest.approx([214, 214], abs=0.05)
    assert sorted(dims["02_RIGHT_2U_ARM.stl"])[1:] == pytest.approx([172.3, 188], abs=0.1)


def test_summary_names_failures_and_what_was_not_checked(reference_findings):
    text = ac.summary_text(reference_findings)
    assert "2 failure(s), 1 warning(s)" in text
    assert "Not checked:" in text and "load" in text


# ---------------------------------------------------------------- rules

def test_panel_clearance_is_pitch_minus_the_eia_allowance():
    assert ac.panel_clearance_mm(1) == pytest.approx(43.656, abs=0.001)
    assert ac.panel_clearance_mm(2) == pytest.approx(88.106, abs=0.001)


@pytest.mark.parametrize("height,code", [(88.0, "rack_clearance_ok"), (88.5, "rack_panel_clearance"),
                                         (89.5, "rack_over_pitch")])
def test_rack_ear_heights(tmp_path, height, code):
    part = ac.describe_part(_box(tmp_path / "ear.stl", 20, 30, height))
    assert ac.check_rack_ear(part, 2)[0].code == code


def test_bed_fit_rotates_and_fails_when_too_big(tmp_path):
    long = ac.describe_part(_box(tmp_path / "long.stl", 200, 100, 10))
    assert ac.check_bed_fit(long, (110, 210, 200))[0].code == "bed_fit"  # fits after 90° about Z
    big = ac.describe_part(_box(tmp_path / "big.stl", 230, 100, 10))
    assert ac.check_bed_fit(big, AD5X_BED)[0].code == "exceeds_bed"
    tall = ac.describe_part(_box(tmp_path / "tall.stl", 10, 10, 230))
    assert ac.check_bed_fit(tall, AD5X_BED)[0].code == "too_tall"


def test_an_open_mesh_fails(tmp_path):
    part = ac.describe_part(_box(tmp_path / "open.stl", 10, 10, 10, drop_one=True))
    findings = ac.check_part(part)
    assert findings[0].level == "fail" and findings[0].code == "mesh_topology"
    assert "not watertight" in findings[0].message


def test_one_part_or_only_coupons_never_satisfy_a_multi_part_request(tmp_path):
    single = [ac.describe_part(_box(tmp_path / "TRAY.stl", 50, 50, 5))]
    assert ac.check_project_shape(single)[0].code == "too_few_parts"
    coupons = [ac.describe_part(_box(tmp_path / "00_TEST_A.stl", 20, 20, 5)),
               ac.describe_part(_box(tmp_path / "00_TEST_B.stl", 20, 20, 5)),
               ac.describe_part(_box(tmp_path / "TRAY.stl", 50, 50, 5))]
    assert ac.check_project_shape(coupons)[0].code == "too_few_parts"


def test_reference_geometry_is_never_counted_or_bed_checked(tmp_path):
    ref = _box(tmp_path / "REFERENCE_MAC_MINI_ENVELOPE_NOT_FOR_PRINTING.stl", 300, 300, 36)
    findings = ac.review([ref, _box(tmp_path / "A.stl", 50, 50, 5), _box(tmp_path / "B.stl", 50, 50, 5)],
                         bed_mm=AD5X_BED)
    assert not any(f.part == ref.name and f.code in ("exceeds_bed", "bed_fit") for f in findings)
    assert any(f.code == "reference_not_printable" for f in findings)
    assert next(f for f in findings if f.code == "parts").evidence["reference"] == [ref.name]


def test_mismatched_mirror_pair_fails(tmp_path):
    left = ac.describe_part(_box(tmp_path / "LEFT_ARM.stl", 100, 50, 80))
    right = ac.describe_part(_box(tmp_path / "RIGHT_ARM.stl", 100, 52, 80))
    assert ac.check_mirrored_pair(left, right)[0].code == "mirror_mismatch"
