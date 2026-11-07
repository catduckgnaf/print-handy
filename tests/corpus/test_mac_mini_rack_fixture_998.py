"""Guard for the issue #998 Mac mini rack reference CAD fixture.

The fixture directory (tests/corpus/design/mac_mini_rack_issue_998/) is a
reference corpus for the multi-part design feature, not a validated rack
mount. These tests run the fixture's own validator in the offline suite so
the committed binaries, their checksum manifest, the regenerated ZIP and
the deliberately pinned known defects cannot drift silently. They assert
facts about the files as committed; passing them says nothing about
mechanical safety, load capacity or print success.
"""
import hashlib
import importlib.util
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "design" / "mac_mini_rack_issue_998"
ORIGINAL_ZIP_SHA256 = "72da79a42d35f15e28d61091807903802f4694ee72a5ae6562e00924dbaea437"


def _validator():
    spec = importlib.util.spec_from_file_location("mac_mini_rack_validate_fixtures", FIXTURE_DIR / "validate_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_validator_passes_on_committed_files():
    validator = _validator()
    errors = validator.collect_errors(FIXTURE_DIR, log=lambda _line: None)
    assert errors == []


def test_validator_check_mode_writes_nothing():
    validator = _validator()
    watched = ["SHA256SUMS.txt", "WARNING_NOT_VALIDATED.txt"]
    before = {name: hashlib.sha256((FIXTURE_DIR / name).read_bytes()).hexdigest() for name in watched}
    validator.collect_errors(FIXTURE_DIR, log=lambda _line: None)
    after = {name: hashlib.sha256((FIXTURE_DIR / name).read_bytes()).hexdigest() for name in watched}
    assert after == before


def test_manifest_covers_exactly_the_twelve_binary_artifacts():
    lines = [line for line in (FIXTURE_DIR / "SHA256SUMS.txt").read_text(encoding="utf8").splitlines() if line.strip()]
    names = sorted(line.partition("  ")[2].strip() for line in lines)
    assert len(names) == 12
    assert sum(name.endswith(".stl") for name in names) == 5
    assert sum(name.endswith(".step") for name in names) == 5
    assert sum(name.endswith(".png") for name in names) == 2


def test_original_provenance_and_license_stay_recorded():
    readme = (FIXTURE_DIR / "FIXTURE_README.md").read_text(encoding="utf8")
    assert ORIGINAL_ZIP_SHA256 in readme
    assert "CC0-1.0" in readme
    assembly = (FIXTURE_DIR / "README_ASSEMBLY_AND_PRINTING.md").read_text(encoding="utf8")
    assert "CC0-1.0" in assembly


def test_known_defects_stay_documented_in_fixture_readme():
    readme = (FIXTURE_DIR / "FIXTURE_README.md").read_text(encoding="utf8")
    assert "88.5 mm" in readme
    assert "88.11 mm" in readme
    assert "+10 mm Z" in readme
    assert "214 mm" in readme
    assert "not a validated" in readme
