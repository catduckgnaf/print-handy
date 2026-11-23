"""Historic print names remain resolvable after record cleanup (#1838)."""
import json

import pytest

from print_handy.jobs import store


@pytest.mark.parametrize("field", ["remote", "remote_name", "file"])
def test_resolve_archived_launch_name_from_older_field(tmp_path, field):
    directory = tmp_path / "example" / "sliced" / "main" / "build"
    directory.mkdir(parents=True)
    artifact = directory / "part.gcode"
    artifact.write_text("G1 X1\n")
    record = directory / "part.launch.json"
    record.write_text(json.dumps({field: "older-remote.gcode"}))
    assert store.resolve_print_target("older-remote.gcode", archive=tmp_path) == str(artifact)


def test_relative_legacy_remote_matches_reported_printer_directory(tmp_path):
    directory = tmp_path / "example" / "sliced" / "main" / "build"
    directory.mkdir(parents=True)
    artifact = directory / "part.gcode"
    artifact.write_text("G1 X1\n")
    (directory / "part.launch.json").write_text(json.dumps({"remote_name": "older-remote.gcode"}))
    assert store.resolve_print_target("3mf_print/older-remote.gcode", archive=tmp_path) == str(artifact)
