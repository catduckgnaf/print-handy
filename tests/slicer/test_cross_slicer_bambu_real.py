"""Cross-origin real-Bambu-Studio tests (#1525): genuine foreign projects, Bambu Studio as destination.

Skipped unless PRINT_HANDY_REAL_SLICER=1 and PRINT_HANDY_REAL_BAMBU=1; CI's Slicer test (bambu) job runs
this file next to tests/slicer/test_bambu_real_a1.py, with the pinned Flathub Bambu Studio 2.8.2.61 as the
configured [bambu] command and the A1 templates generated from the pinned OrcaSlicer bundle's resources
(profiles.install, production identities only), exactly like that file. Inside the gate there are no
further skips: a missing slicer, resources folder or fixture fails.

tests/slicer/test_cross_slicer_real.py proves the genuine producer fixtures (PROVENANCE.json: Cura 5.8.1,
PrusaSlicer 2.8.1, OrcaSlicer 2.5.1 and Bambu Studio 02.08.02.61) with OrcaSlicer as the destination. This
file is the same proof with Bambu Studio as the destination: each fixture is sliced through the production
entry point (orca_adapter.slice_model) for a Bambu Lab A1 whose [printer] slicer is bambu_studio, the
route a foreign project takes when Bambu Studio is the primary slicer. Orca never runs on that route, so
the output can only be Bambu Studio's; the tests still assert it from the provenance (slicer, generator,
no fallback, the Bambu command) and from the G-code's own header block. The production rebuild owns the
destination settings, so the source's own values (Bambu's -1 sentinels included) never reach the slicer.
The Bambu Studio fixture is the same-family case; the other three are genuine cross-origin projects.
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from print_handy import printers, settings
from print_handy.core import author_settings, orca_adapter, slice_guard, source_descriptor, spoolman_client
from print_handy.printers.bambulab import profiles

from tests.printers.a1_flow import a1_state
from .test_orca_real import WORK
from .test_orca_real_bambu_a1 import installed_resources, toolpath_bounds

REAL = os.environ.get("PRINT_HANDY_REAL_SLICER") == "1"
REAL_BAMBU = os.environ.get("PRINT_HANDY_REAL_BAMBU") == "1"
pytestmark = pytest.mark.skipif(not (REAL and REAL_BAMBU),
                                reason="set PRINT_HANDY_REAL_SLICER=1 and PRINT_HANDY_REAL_BAMBU=1 "
                                       "to slice with the real Bambu Studio CLI")
ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "corpus" / "producer-fixtures"
# The Bambu Studio the CI leg deploys (Flathub commit pinned in ci.yml, slicer-test-bambu).
BAMBU_VERSION = "02.08.02.61"
CUBE_MM = 20.0

# fixture -> what the production readers must report for it (the producer proof the Orca file asserts too).
SOURCES = {
    "cura-genuine.3mf": {"kind": "cura", "user_class": "editable-project", "family": "cura",
                         "source_format": "cura_project"},
    "prusa-genuine.3mf": {"kind": "prusa", "user_class": "geometry-only", "family": None,
                          "source_format": "unknown"},
    "orca-genuine.3mf": {"kind": "orca", "user_class": "editable-project", "family": "orca",
                         "source_format": "orca_project"},
    "bambu-genuine.3mf": {"kind": "bambu", "user_class": "editable-project", "family": "bambu_studio",
                          "source_format": "orca_project"},
}


@pytest.fixture(scope="module")
def work():
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        yield Path(tmp)


@pytest.fixture(scope="module")
def a1(work):
    """A Bambu Lab A1 whose [printer] slicer is bambu_studio, templates generated from the installed OrcaSlicer."""
    config = settings.load(local=False, environ={
        "PRINT_HANDY__PRINTER__MODEL": "bambulab.a1",
        "PRINT_HANDY__PRINTER__SLICER": "bambu_studio",
        "PRINT_HANDY__PATHS__STATE_HOME": str(work / "hermes")})
    adapter = printers.get("bambulab.a1", config)
    known = profiles.install(adapter, installed_resources())
    print(f"\nA1 profiles: {known['label']} from {installed_resources()}")
    assert orca_adapter.slicer_choice(adapter) == "bambu_studio"
    return adapter


def _provenance_row(name):
    rows = {row["file"]: row for row in json.loads((FIXTURES / "PROVENANCE.json").read_text())["fixtures"]}
    return rows[name]


@pytest.mark.parametrize("name", list(SOURCES))
def test_genuine_source_slices_in_bambu_studio_through_production(work, a1, name):
    expected = SOURCES[name]
    fixture = FIXTURES / name
    assert fixture.is_file(), f"genuine producer fixture missing: {fixture}"
    row = _provenance_row(name)
    assert row["class"] == "genuine"
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == row["sha256"], "fixture differs from PROVENANCE.json"
    described = source_descriptor.describe(fixture)
    assert described["kind"] == expected["kind"] and described["user_class"] == expected["user_class"]
    assert author_settings.read_project(fixture).get("source_family") == expected["family"]

    folder = work / name.replace(".3mf", "")
    with mock.patch.object(spoolman_client, "get_spool", return_value=None):
        out, provenance, _configs = orca_adapter.slice_model(fixture, folder, a1_state(), adapter=a1)
    text = Path(out).read_text(errors="replace")
    lines = text.splitlines()
    print(f"\n{name}: slicer={provenance['slicer']} generator={provenance['generator']} "
          f"fallback={provenance['fallback']} timing={provenance['timing_s']}")

    # Bambu Studio produced it, as the primary slicer: no Orca run, no fallback.
    assert provenance["slicer"] == "Bambu Studio" and provenance["fallback"] is None
    assert provenance["slicer_command"][:len(orca_adapter.BAMBU_COMMAND)] == orca_adapter.BAMBU_COMMAND
    assert not (folder / "orca.log").exists() and (folder / "bambu.log").is_file()
    assert provenance["generator"] == slice_guard.generator_identity(text) == f"BambuStudio {BAMBU_VERSION}"
    assert lines[0] == "; HEADER_BLOCK_START" and lines[1] == f"; BambuStudio {BAMBU_VERSION}"
    assert any(line.startswith("; total layer number:") for line in lines[:12])
    conversion = provenance["conversion_provenance"]
    assert conversion["source_format"] == expected["source_format"]
    assert conversion["destination_slicer_version"] == BAMBU_VERSION

    # The destination machine and filament, not the source's.
    machine = json.loads((a1.templates_dir() / "machine.json").read_text())
    filament = json.loads((a1.templates_dir() / "filament.json").read_text())
    assert f"; printer_model = {machine['printer_model']}" in text
    assert "; filament_type = PLA" in text
    assert any(line.startswith("; filament_settings_id = ") and filament["name"] in line for line in lines)
    assert a1.gcode_problems(text) == []

    # The part is present: a 20 mm cube's extruding toolpath, inside the A1 bed.
    bounds = toolpath_bounds(text)
    print(f"{name}: extruding toolpath bounds (mm): {bounds}")
    assert 0 <= bounds["X"][0] and bounds["X"][1] <= 256 and 0 <= bounds["Y"][0] and bounds["Y"][1] <= 256
    assert bounds["X"][1] - bounds["X"][0] >= CUBE_MM - 0.5 and bounds["Y"][1] - bounds["Y"][0] >= CUBE_MM - 0.5
    assert 0 < bounds["Z"][0] and CUBE_MM - 0.5 <= bounds["Z"][1] <= CUBE_MM + 0.5
