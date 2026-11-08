"""Tests for intake support clearance and modeled support analysis (#911)."""

import json
import zipfile
from pathlib import Path
import pytest

from print_handy.core import support_clearance

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "support_clearance"
MANIFEST_PATH = FIXTURES_DIR / "fixture_manifest.json"
V12_FIXTURE = Path(__file__).parent.parent / "fixtures" / "modeled_supports_shelf.3mf"


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


def _box_object_model(z0):
    """One 30x30x10 box mesh as a component object file, underside at z0."""
    # Corner indices 0-3 are the bottom face, 4-7 the top face.
    verts = [(0, 0, z0), (30, 0, z0), (30, 30, z0), (0, 30, z0),
             (0, 0, z0 + 10), (30, 0, z0 + 10), (30, 30, z0 + 10), (0, 30, z0 + 10)]
    tris = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
            (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    v = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in verts)
    t = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    return (f'<model unit="millimeter"><resources><object id="1"><mesh><vertices>{v}</vertices>'
            f'<triangles>{t}</triangles></mesh></object></resources>'
            f'<build><item objectid="1"/></build></model>')


def test_component_based_3mf_parses_transforms_no_false_positives(tmp_path):
    """Component-based 3MF parses transforms and produces no false positive risks.

    Replaces the V14 test, which pointed at a Pixel_Patina_V14 file under a
    hardcoded home-directory path on one operator machine and so skipped
    everywhere else (2026-11-07 test-suite cleanup; see
    tests/TEST-SUITE-AUDIT-2026-11-07.md). This synthetic assembly states the
    same conditions: two components, one authored 0.4 mm above the plate in
    its object file and pulled onto the plate by its component transform —
    if transforms were dropped, its 900 mm2 underside at 0.4 mm would flag.
    """
    path = tmp_path / "components.3mf"
    main = """<model xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" unit="millimeter"><resources>
<object id="1" name="Left"><components><component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>
<object id="2" name="Right"><components><component p:path="/3D/Objects/object_2.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 40 0 -0.4"/></components></object>
</resources><build><item objectid="1"/><item objectid="2"/></build></model>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("3D/Objects/object_1.model", _box_object_model(0))
        archive.writestr("3D/Objects/object_2.model", _box_object_model(0.4))
        archive.writestr("3D/3dmodel.model", main)
        archive.writestr("3D/_rels/3dmodel.model.rels",
                         '<Relationships>'
                         '<Relationship Target="/3D/Objects/object_1.model" Id="rel-1"/>'
                         '<Relationship Target="/3D/Objects/object_2.model" Id="rel-2"/>'
                         '</Relationships>')
    rep = support_clearance.analyze(str(path))
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


def test_extend_underside_applies_build_transform(tmp_path):
    """A build transform lifting the mesh 0.4 mm must not defeat the repair:
    findings are in printer coordinates, so the fix maps them back to local
    coordinates instead of matching local Z against the gap (Codex P2)."""
    import shutil
    src = FIXTURES_DIR / "10_build_transform_z040.3mf"
    assert src.is_file()
    work = tmp_path / "model.3mf"
    shutil.copy(src, work)
    rep = support_clearance.analyze(str(work))
    finding = next(f for f in rep["findings"] if f["code"] == support_clearance.CODE_CLEARANCE)
    res = support_clearance.extend_underside(str(work), gap_mm=finding["clearance_mm"]["min"])
    assert res["modified_vertices"] > 0
    rep2 = support_clearance.analyze(str(work))
    assert support_clearance.CODE_CLEARANCE not in support_clearance.codes(rep2)
