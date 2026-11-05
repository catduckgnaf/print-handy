"""Geometry screening reference checks and interlocking assembly tests (#911, #912).

Tests the 17 core 3MF fixtures using the deterministic reference detector,
and verifies that interlocking build items are preserved as one assembly.
"""
import json
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.corpus.geometry import detect_underfaces_reference as det
from print_handy.core import source_colors, color_geometry
from print_handy.core.threemf_canonical import CORE_NS

GEOMETRY_DIR = Path(__file__).resolve().parents[1] / "geometry"
MANIFEST_PATH = GEOMETRY_DIR / "fixture_manifest.json"


def _load_manifest_cases():
    if not MANIFEST_PATH.is_file():
        return []
    manifest = json.loads(MANIFEST_PATH.read_text())
    return manifest.get("cases", [])


CASES = _load_manifest_cases()


@pytest.mark.parametrize("case", CASES, ids=[c["fixture"] for c in CASES])
def test_geometry_fixture_low_gap_flags(case):
    """Reference detector accurately flags horizontal undersides below 1.0mm gap."""
    fixture_path = GEOMETRY_DIR / case["fixture"]
    assert fixture_path.is_file(), f"missing fixture {fixture_path}"
    res = det.diagnose(fixture_path, gap=1.0, area=500.0)
    flags = res["low_gap_flags"]
    assert flags == case["expect_low_gap_flags"], (
        f"{case['fixture']}: expected {case['expect_low_gap_flags']} low_gap_flags, got {flags}"
    )


def test_geometry_fixture_manifest_contract():
    """Ensure manifest cases record purpose and non-empty descriptions."""
    manifest = json.loads(MANIFEST_PATH.read_text())
    assert manifest["gap_mm"] == 1.0
    assert manifest["area_mm2"] == 500.0
    assert len(manifest["cases"]) == 17
    for c in manifest["cases"]:
        assert c["description"], f"{c['fixture']} missing description"


def test_todo_911_future_analyzer_expectations():
    """TODO #911: Validate cases that the reference analyzer flags for production Raycast/Span check."""
    complex_cases = [c for c in CASES if c.get("other_checks")]
    assert len(complex_cases) >= 10
    # Every case marked for future checks is recorded in the manifest
    unsupported_types = {
        check for c in complex_cases for check in c["other_checks"]
    }
    assert "PRODUCTION_SLOPED_TRIANGLE_RAYCAST" in unsupported_types
    assert "SPAN_RISK_INDEPENDENT_OF_AREA" in unsupported_types
    assert "INTERSECTION_COLLISION_GUARD" in unsupported_types


def _create_synthetic_interlocking_inlay(path):
    """Two build items without cutters that overlap/touch in XY and Z (#912)."""
    box_main = '<mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="40" y="0" z="0"/><vertex x="40" y="40" z="0"/><vertex x="0" y="40" z="0"/><vertex x="0" y="0" z="10"/><vertex x="40" y="0" z="10"/><vertex x="40" y="40" z="10"/><vertex x="0" y="40" z="10"/></vertices><triangles><triangle v1="0" v2="2" v3="1"/><triangle v1="0" v2="3" v3="2"/><triangle v1="4" v2="5" v3="6"/><triangle v1="4" v2="6" v3="7"/><triangle v1="0" v2="1" v3="5"/><triangle v1="0" v2="5" v3="4"/><triangle v1="1" v2="2" v3="6"/><triangle v1="1" v2="6" v3="5"/><triangle v1="2" v2="3" v3="7"/><triangle v1="2" v2="7" v3="6"/><triangle v1="3" v2="0" v3="4"/><triangle v1="3" v2="4" v3="7"/></triangles></mesh>'
    box_inlay = '<mesh><vertices><vertex x="15" y="15" z="5"/><vertex x="25" y="15" z="5"/><vertex x="25" y="25" z="5"/><vertex x="15" y="25" z="5"/><vertex x="15" y="15" z="10"/><vertex x="25" y="15" z="10"/><vertex x="25" y="25" z="10"/><vertex x="15" y="25" z="10"/></vertices><triangles><triangle v1="0" v2="2" v3="1"/><triangle v1="0" v2="3" v3="2"/><triangle v1="4" v2="5" v3="6"/><triangle v1="4" v2="6" v3="7"/><triangle v1="0" v2="1" v3="5"/><triangle v1="0" v2="5" v3="4"/><triangle v1="1" v2="2" v3="6"/><triangle v1="1" v2="6" v3="5"/><triangle v1="2" v2="3" v3="7"/><triangle v1="2" v2="7" v3="6"/><triangle v1="3" v2="0" v3="4"/><triangle v1="3" v2="4" v3="7"/></triangles></mesh>'
    settings_xml = (
        '<?xml version="1.0" encoding="UTF-8"?><config>'
        '<object id="1"><metadata key="name" value="BASE"/><metadata key="extruder" value="1"/><part id="1" subtype="normal_part"/></object>'
        '<object id="2"><metadata key="name" value="INLAY"/><metadata key="extruder" value="2"/><part id="2" subtype="normal_part"/></object>'
        '</config>'
    )
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<model unit="millimeter" xmlns="{CORE_NS}"><resources>'
        f'<object id="1" name="BASE" type="model">{box_main}</object>'
        f'<object id="2" name="INLAY" type="model">{box_inlay}</object>'
        '</resources><build>'
        '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 20 20 0" printable="1"/>'
        '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 20 20 0" printable="1"/>'
        '</build></model>'
    )
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr('3D/3dmodel.model', model)
        z.writestr('Metadata/model_settings.config', settings_xml)
    return path


def test_interlocking_inlay_assembly_grouped_into_one_orca_object(tmp_path):
    """PR #912 assertion: two interlocking build items must be grouped into one Orca assembly."""
    src = _create_synthetic_interlocking_inlay(tmp_path / "interlocking.3mf")
    manifest = source_colors.inventory(src)
    assert not manifest["cutters"], "interlocking fixture has no cutters"

    from tests.pipeline.test_plate_merge import _two_slot_decisions
    decisions = _two_slot_decisions(manifest)
    out_3mf = tmp_path / "out.3mf"
    color_geometry.export_color_geometry(src, out_3mf, decisions)
    with zipfile.ZipFile(out_3mf) as z:
        root = ET.fromstring(z.read("3D/3dmodel.model"))
        build = root.find(f"{{{CORE_NS}}}build")
        items = build.findall(f"{{{CORE_NS}}}item") if build is not None else []
        # Grouped into 1 build item / assembly
        assert len(items) == 1
