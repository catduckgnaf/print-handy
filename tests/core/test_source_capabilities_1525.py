"""Capability manifest contract for issue #1525 (tranche A).

The manifest is the machine-readable source capability record: every cell
names its owner, fixture and executed evidence level, and every known gap
appears in the gap table. These tests keep the manifest honest in shape;
they do not upgrade any evidence level.
"""
from print_handy.core import source_capabilities as sc


def test_manifest_is_well_formed():
    assert sc.validate_manifest() == []


def test_every_source_kind_has_a_cell_or_is_unknown_only():
    covered = {cell["source"] for cell in sc.cells()}
    for kind in ("bambu", "orca", "prusa", "cura", "generic-core", "gcode-only"):
        assert kind in covered, kind


def test_gap_table_names_the_honest_gaps():
    text = " ".join(row["gap"] or "" for row in sc.gap_table()).lower()
    assert "cura" in text and "genuine" in text
    assert "hardware" in text  # the U1 gap stays named
    assert "not branch-protection-required" in text or "not a required" in text


def test_no_cell_claims_genuine_producer_without_a_gap_note():
    for cell in sc.cells():
        if cell["evidence"] == "genuine-producer":
            assert cell["fixture"], cell["id"]


def test_anycubic_claims_detection_only():
    cell = sc.cell_for("anycubic")
    assert cell is not None
    assert "detection only" in cell["feature"].lower()
