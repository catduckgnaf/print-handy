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
    assert "hardware" in text  # the U1 gap stays named
    assert "not branch-protection-required" in text or "not a required" in text
    # The Prusa rich-fidelity remainder stays named: the genuine fixture
    # is geometry-only and the rich roles are proven on a spec-synthetic
    # fixture, labeled as such.
    assert "spec-synthetic" in text
    # Genuine Cura, Orca and Bambu producer fixtures exist now, so no gap
    # may still claim one of those is missing. (The Anycubic gap's "no
    # genuine fixture is redistributed" is about that vendor's files and
    # is not one of these claims.)
    assert "not yet produced" not in text
    for stale in ("no genuine cura", "no genuine prusa",
                  "no genuine orca", "no genuine bambu"):
        assert stale not in text


def test_genuine_producer_cells_name_fixtures_the_provenance_manifest_calls_genuine():
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    provenance = {row["file"]: row["class"] for row in json.loads(
        (root / "tests" / "corpus" / "producer-fixtures" / "PROVENANCE.json")
        .read_text())["fixtures"]}
    for cell in sc.cells():
        if cell["evidence"] != "genuine-producer":
            continue
        name = Path(cell["fixture"]).name
        assert provenance.get(name) == "genuine", cell["id"]


def test_no_cell_claims_genuine_producer_without_a_gap_note():
    for cell in sc.cells():
        if cell["evidence"] == "genuine-producer":
            assert cell["fixture"], cell["id"]


def test_anycubic_claims_detection_only():
    cell = sc.cell_for("anycubic")
    assert cell is not None
    assert "detection only" in cell["feature"].lower()
