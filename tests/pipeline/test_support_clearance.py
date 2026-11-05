"""Tests for intake support clearance and modeled support analysis (#911)."""

import json
from pathlib import Path
import pytest

from print_handy.core import support_clearance

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "support_clearance"
MANIFEST_PATH = FIXTURES_DIR / "fixture_manifest.json"
V12_FIXTURE = Path(__file__).parent.parent / "fixtures" / "modeled_supports_shelf.3mf"
V14_FIXTURE = Path("/home/dysprosium/.hermes/profiles/homelab/workspace/print-handy-fixtures/911-handoff/models/Pixel_Patina_V14_Two_Color_Assembly.3mf")


@pytest.fixture(scope="module")
def manifest():
    return json.loads(MANIFEST_PATH.read_text())


def test_fixture_manifest_all_cases(manifest):
    """Every case in fixture_manifest.json is analyzed and matches expectations."""
    cases = {c["fixture"]: c for c in manifest["cases"]}

    for filename, case in cases.items():
        fixture_path = FIXTURES_DIR / filename
        assert fixture_path.is_file(), f"Missing fixture file {filename}"

        rep = support_clearance.analyze(str(fixture_path))
        codes = support_clearance.codes(rep)

        other = case.get("other_checks", [])

        # 1. Check low_gap_flags against expect_low_gap_flags
        # Note: manifest expect_low_gap_flags=0 for 13_sloped_low_clearance reflects the horizontal-only
        # reference script limitation; production raycast must catch it (low_gap_flags=1).
        if "PRODUCTION_SLOPED_TRIANGLE_RAYCAST" in other:
            assert rep["low_gap_flags"] == 1, (
                f"{filename}: production raycast expected to detect sloped low gap, got {rep['low_gap_flags']}"
            )
        else:
            expected_flags = case["expect_low_gap_flags"]
            assert rep["low_gap_flags"] == expected_flags, (
                f"{filename}: expected low_gap_flags={expected_flags}, got {rep['low_gap_flags']}"
            )

        # 2. Check other_checks specified in manifest
        if "MODELED_SUPPORT_COVERAGE_TOO_LOW" in other:
            assert support_clearance.CODE_MODELED in codes
            finding = next(f for f in rep["findings"] if f["code"] == support_clearance.CODE_MODELED)
            assert finding["reason"] == "low_coverage"
            assert finding["support_coverage"] < 0.10

        if "INTERSECTION_COLLISION_GUARD" in other:
            assert support_clearance.CODE_MODELED in codes
            finding = next(f for f in rep["findings"] if f["code"] == support_clearance.CODE_MODELED)
            assert finding["reason"] == "intersects_part"

        if "BELOW_BED_GEOMETRY" in other:
            assert support_clearance.CODE_BELOW_BED in codes
            finding = next(f for f in rep["findings"] if f["code"] == support_clearance.CODE_BELOW_BED)
            assert finding["min_z_mm"] < 0
            assert finding["volume_below_mm3"] > 0

        if "SPAN_RISK_INDEPENDENT_OF_AREA" in other:
            assert support_clearance.CODE_CLEARANCE in codes
            span_findings = [f for f in rep["findings"] if f["code"] == support_clearance.CODE_CLEARANCE and f["trigger"] == "span"]
            assert len(span_findings) >= 1
            assert span_findings[0]["span_mm"] >= 15.0

        if "BUILD_TRANSFORM_MUST_APPLY" in other:
            assert support_clearance.CODE_CLEARANCE in codes
            finding = next(f for f in rep["findings"] if f["code"] == support_clearance.CODE_CLEARANCE)
            assert pytest.approx(finding["clearance_mm"]["min"], abs=0.05) == 0.40

        if "PRESERVE_CONNECTED_COMPONENTS" in other:
            # Disjoint islands must be analyzed as separate regions, not merged into one >500 mm2 area
            assert rep["low_gap_flags"] == 0
            low_regions = [r for r in rep["regions"] if r.get("kind") == "low_clearance"]
            assert len(low_regions) == 2
            for r in low_regions:
                assert r["area_mm2"] == pytest.approx(400.0, rel=0.05)

        if "PRODUCTION_SLOPED_TRIANGLE_RAYCAST" in other:
            # Raycast detects sloped faces under 1.0mm gap
            assert support_clearance.CODE_CLEARANCE in codes
            sloped_f = [f for f in rep["findings"] if f["code"] == support_clearance.CODE_CLEARANCE and f["sloped"]]
            assert len(sloped_f) >= 1


def test_pixel_patina_v12_golden_regression():
    """V12 shelf has 9493 mm2 flat underside at Z0.4 and ineffective modeled supports."""
    assert V12_FIXTURE.is_file()
    rep = support_clearance.analyze(str(V12_FIXTURE))
    assert rep["risky"] is True
    assert rep["low_gap_flags"] == 1

    codes = support_clearance.codes(rep)
    assert support_clearance.CODE_CLEARANCE in codes
    assert support_clearance.CODE_MODELED in codes

    # Check the 0.4mm plate gap finding
    clearance_findings = [f for f in rep["findings"] if f["code"] == support_clearance.CODE_CLEARANCE]
    assert len(clearance_findings) == 1
    f_clear = clearance_findings[0]
    assert f_clear["trigger"] == "area"
    assert f_clear["over"] == "plate"
    assert pytest.approx(f_clear["area_mm2"], rel=0.02) == 9493.0
    assert pytest.approx(f_clear["clearance_mm"]["min"], abs=0.05) == 0.40
    assert pytest.approx(f_clear["z_mm"]["min"], abs=0.05) == 0.40

    # Check modeled support coverage findings
    modeled_findings = [f for f in rep["findings"] if f["code"] == support_clearance.CODE_MODELED]
    assert len(modeled_findings) == 2
    for mf in modeled_findings:
        assert mf["reason"] == "low_coverage"
        assert mf["support_coverage"] < 0.05  # ~1.7% and ~1.8%

    # Check describe output formatting
    lines = support_clearance.describe(rep)
    assert any("0.4 mm gap to the build plate across 9493 mm2" in l for l in lines)
    assert any("modeled supports touch only" in l for l in lines)


def test_pixel_patina_v14_components():
    """V14 component-based 3MF parses transforms and produces no false positive risks."""
    if not V14_FIXTURE.is_file():
        pytest.skip("V14 fixture not on disk")
    rep = support_clearance.analyze(str(V14_FIXTURE))
    assert rep["risky"] is False
    assert rep["low_gap_flags"] == 0
    assert rep["findings"] == []


def test_safe_analyze_error_handling(tmp_path):
    """safe_analyze returns None on missing or corrupt files without raising."""
    assert support_clearance.safe_analyze(None) is None
    assert support_clearance.safe_analyze("") is None
    assert support_clearance.safe_analyze(str(tmp_path / "nonexistent.3mf")) is None

    bad = tmp_path / "corrupt.3mf"
    bad.write_text("not a zip file")
    assert support_clearance.safe_analyze(str(bad)) is None
