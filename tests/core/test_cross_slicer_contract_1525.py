"""Cross-slicer contract tests (#1525 completion): the offline tier.

Pure contract tests over the producer fixtures and the production
readers — no slicer binary, so they belong in the offline tests. The
real-binary smoke lives in tests/slicer/test_cross_slicer_real.py,
which the required Slicer test (stable) job now runs as its own named
step (pinned by tests/test_ci_cross_slicer_required.py).

Fixture honesty is part of the contract: PROVENANCE.json labels each
fixture genuine or spec-synthetic, the recorded SHA-256 must match the
bytes on disk, and a fixture labeled genuine must carry its producer
proof inside the archive itself.
"""
import hashlib
import json
import zipfile
from pathlib import Path

from print_handy.core import (author_settings, remap_integrity,
                              source_colors, source_descriptor)

FIXTURES = Path(__file__).resolve().parents[1] / "corpus" / "producer-fixtures"


def _provenance():
    return {row["file"]: row
            for row in json.loads((FIXTURES / "PROVENANCE.json").read_text())["fixtures"]}


def test_provenance_manifest_matches_the_bytes_on_disk():
    rows = _provenance()
    assert set(rows) == {"cura-spec-synthetic.3mf", "cura-genuine.3mf",
                         "prusa-genuine.3mf", "prusa-rich-spec-synthetic.3mf",
                         "orca-genuine.3mf", "bambu-genuine.3mf"}
    for name, row in rows.items():
        digest = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
        assert digest == row["sha256"], name
    assert rows["cura-genuine.3mf"]["class"] == "genuine"
    assert rows["prusa-genuine.3mf"]["class"] == "genuine"
    assert rows["orca-genuine.3mf"]["class"] == "genuine"
    assert rows["bambu-genuine.3mf"]["class"] == "genuine"
    assert rows["cura-spec-synthetic.3mf"]["class"] == "spec-synthetic"
    assert rows["prusa-rich-spec-synthetic.3mf"]["class"] == "spec-synthetic"


def test_genuine_cura_fixture_proves_its_producer_inside_the_archive():
    fixture = FIXTURES / "cura-genuine.3mf"
    with zipfile.ZipFile(fixture) as archive:
        names = archive.namelist()
        version = archive.read("Cura/version.ini").decode("utf-8", "replace")
    assert "cura_version = 5.8.1" in version
    assert any(name.startswith("Cura/") and name.endswith(".global.cfg")
               for name in names)
    described = source_descriptor.describe(fixture)
    assert described["kind"] == "cura"
    assert described["format"] == "cura_project"
    assert described["application"] == "UltiMaker Cura"
    assert described["has_geometry"] is True
    assert described["problems"] == []
    # The production author-settings path resolves the real stacks: the
    # translated subset carries the one setting the designer changed, and
    # none of the vendor quality/variant defaults (layer height 0.2, 15%
    # density, tree support, 7 mm brim) the stacks also hold.
    record = author_settings.read_project(fixture)
    assert record["format"] == "cura_project"
    assert record["author"] == {"sparse_infill_pattern": "gyroid"}
    assert author_settings.read(fixture) == {"sparse_infill_pattern": "gyroid"}


def test_genuine_prusa_fixture_is_geometry_only_and_says_so():
    fixture = FIXTURES / "prusa-genuine.3mf"
    with zipfile.ZipFile(fixture) as archive:
        names = archive.namelist()
    # The real CLI export carries no settings snapshot member; the
    # contract is that Print Handy reads it as geometry, not that a
    # snapshot appears.
    assert "Metadata/Slic3r_PE.config" not in names
    described = source_descriptor.describe(fixture)
    assert described["kind"] == "prusa"
    assert described["has_geometry"] is True
    inventory = source_colors.inventory(fixture)
    assert inventory["filaments"] == []
    report = remap_integrity.validate(fixture)
    assert report["ok"] is True
    assert report["roles"]["printable_parts"] == ["build:0"]
    assert report["roles"]["modifiers"] == []


def test_prusa_rich_fixture_fidelity_through_the_production_entry_points():
    """Modifier/negative/support/paint retention on the rich Prusa layout.

    The fixture is spec-synthetic (PROVENANCE.json says so, and the
    genuine Prusa CLI export above is geometry-only), so the fidelity
    proof runs through the same production entry points a real Prusa
    project takes: descriptor, inventory and remap_integrity.
    """
    fixture = FIXTURES / "prusa-rich-spec-synthetic.3mf"
    described = source_descriptor.describe(fixture)
    assert described["kind"] == "prusa"
    assert described["format"] == "prusaslicer_project"
    assert described["user_class"] == "editable-project"
    inventory = source_colors.inventory(fixture)
    assert [row["source_extruder"] for row in inventory["filaments"]] == [1, 2, 3, 4]
    assert inventory["painted"] is True
    # Filament 4 is named support-only by the project's own settings
    # and no part prints with it.
    assert inventory["support_filaments"]["base"] == 4
    report = remap_integrity.validate(fixture)
    assert report["ok"] is True
    assert report["problems"] == []
    roles = report["roles"]
    assert roles["printable_parts"] == ["build:0"]
    assert roles["modifiers"] == ["build:0/volume1"]
    assert roles["negative_parts"] == ["build:0/volume2"]
    assert roles["support_blockers"] == ["build:0/volume3"]
    assert roles["support_enforcers"] == ["build:0/volume4"]
    assert any("painted color identity" in warning
               for warning in report["warnings"])


def test_genuine_orca_fixture_proves_its_producer_inside_the_archive():
    fixture = FIXTURES / "orca-genuine.3mf"
    with zipfile.ZipFile(fixture) as archive:
        model = archive.read("3D/3dmodel.model").decode("utf-8", "replace")
    # The producer proof is OrcaSlicer's own metadata element: the
    # Application metadata of a genuine Orca save names its Bambu base,
    # so an Application-only reader mislabels this file as Bambu.
    assert '<metadata name="OrcaSlicer">2.5.1</metadata>' in model
    assert "BambuStudio-02.08.01.55" in model
    described = source_descriptor.describe(fixture)
    assert described["kind"] == "orca"
    assert described["format"] == "orca_project"
    assert described["application"] == "OrcaSlicer-2.5.1"
    assert described["source_version"] == "2.5.1"
    assert described["user_class"] == "editable-project"
    # The mesh lives in a 3D/Objects component file, not the root model.
    assert described["has_geometry"] is True
    assert described["has_thumbnail"] is True
    assert described["problems"] == []
    record = author_settings.read_project(fixture)
    assert record["format"] == "orca_project"
    assert record["source_slicer"] == "OrcaSlicer"
    assert record["source_family"] == "orca"
    assert record["source_version"] == "2.5.1"
    inventory = source_colors.inventory(fixture)
    assert [(row["source_extruder"], row["material"])
            for row in inventory["filaments"]] == [(1, "PLA")]
    assert remap_integrity.validate(fixture)["ok"] is True


def test_genuine_bambu_fixture_proves_its_producer_inside_the_archive():
    fixture = FIXTURES / "bambu-genuine.3mf"
    with zipfile.ZipFile(fixture) as archive:
        model = archive.read("3D/3dmodel.model").decode("utf-8", "replace")
    assert '<metadata name="Application">BambuStudio-02.08.02.61</metadata>' in model
    assert '<metadata name="OrcaSlicer">' not in model
    described = source_descriptor.describe(fixture)
    assert described["kind"] == "bambu"
    assert described["format"] == "orca_project"
    assert described["application"] == "BambuStudio-02.08.02.61"
    assert described["source_version"] == "02.08.02.61"
    assert described["user_class"] == "editable-project"
    assert described["has_geometry"] is True
    assert described["problems"] == []
    record = author_settings.read_project(fixture)
    assert record["format"] == "orca_project"
    assert record["source_slicer"] == "BambuStudio"
    assert record["source_family"] == "bambu_studio"
    assert record["source_version"] == "02.08.02.61"
    inventory = source_colors.inventory(fixture)
    assert [(row["source_extruder"], row["material"])
            for row in inventory["filaments"]] == [(1, "PLA")]
    assert remap_integrity.validate(fixture)["ok"] is True


def test_bambu_orca_destination_descriptor_contract(tmp_path):
    """The Bambu/Orca destination shape parses through the same readers.

    The Bambu destination smoke itself needs the Bambu Studio Flatpak
    and stays in its own CI job; what the offline tier pins is the
    descriptor/author contract every destination conversion starts
    from, so a destination-side shape change fails here first.
    """
    from tests.core.test_plate_usage import write_source
    source = write_source(tmp_path / "dest.3mf",
                          [("a", 1, None), ("b", 2, None)], [[0, 1]])
    described = source_descriptor.describe(source)
    assert described["kind"] == "orca"
    assert described["format"] == "orca_project"
    assert described["user_class"] == "editable-project"
    assert author_settings.read_project(source)["format"] == "orca_project"
    inventory = source_colors.inventory(source)
    assert {row["source_extruder"] for row in inventory["filaments"]} >= {1, 2}
