"""GcodeArtifact: one verification reads the G-code once and every check sees the same bytes (#1323)."""
from pathlib import Path
from unittest import mock

import pytest

from print_handy.gcode import verify
from print_handy.gcode.artifact import GcodeArtifact, _text_like_read_text, file_sha256

BODY = b"; MD5:00000000000000000000000000000000\r\nG90\r\nG1 X1 Y2 Z3 ; \xff caf\xc3\xa9\n"


@pytest.fixture
def gcode(tmp_path):
    path = tmp_path / "plate.gcode"
    path.write_bytes(BODY)
    return path


def test_answers_match_the_old_per_question_reads(gcode):
    artifact = GcodeArtifact.of(gcode)
    assert artifact.path == Path(gcode)
    assert artifact.raw == gcode.read_bytes()
    assert artifact.text == gcode.read_bytes().decode(errors="ignore")
    assert artifact.read_text(errors="ignore") == gcode.read_text(errors="ignore")
    assert artifact.sha256 == file_sha256(gcode)
    assert _text_like_read_text(BODY, "ignore") == gcode.read_text(errors="ignore")


def test_outside_read_once_each_artifact_reads_the_file_as_it_is_now(gcode):
    first = GcodeArtifact.of(gcode)
    assert first.raw == BODY
    gcode.write_bytes(b"G90\n")
    assert GcodeArtifact.of(gcode) is not first
    assert GcodeArtifact.of(str(gcode)).raw == b"G90\n"


def test_inside_read_once_one_path_is_one_read(gcode):
    reads = []
    real = Path.read_bytes

    def counting(self):
        reads.append(self)
        return real(self)

    @GcodeArtifact.read_once
    def check(path):
        a = GcodeArtifact.of(path)
        a.raw
        gcode.write_bytes(b"replaced mid-check\n")
        b = GcodeArtifact.of(str(path))
        return a is b, b.sha256, verify.zmod_md5_valid(path)

    with mock.patch.object(Path, "read_bytes", counting):
        same, digest, _ = check(gcode)
    import hashlib
    assert same and len(reads) == 1
    assert digest == hashlib.sha256(BODY).hexdigest()


def test_nested_read_once_calls_share_the_outer_artifacts(gcode):
    seen = []

    @GcodeArtifact.read_once
    def inner(path):
        seen.append(GcodeArtifact.of(path))

    @GcodeArtifact.read_once
    def outer(path):
        seen.append(GcodeArtifact.of(path))
        inner(path)

    outer(gcode)
    assert seen[0] is seen[1]
    outer(gcode)
    assert seen[2] is not seen[0]


def test_a_failed_read_is_raised_and_retried(tmp_path):
    missing = tmp_path / "later.gcode"

    @GcodeArtifact.read_once
    def check():
        artifact = GcodeArtifact.of(missing)
        with pytest.raises(FileNotFoundError):
            artifact.raw
        missing.write_bytes(b"G90\n")
        return artifact.raw

    assert check() == b"G90\n"


def test_routed_verification_binds_the_marker_to_the_verified_bytes(gcode, monkeypatch):
    """The marker digest is of the bytes verify_gcode checked, not of a later re-read."""
    import hashlib
    job = {"gcode_verified": True}

    def fake_verify(path, **kwargs):
        GcodeArtifact.of(path).raw  # what the real verification reads first
        gcode.write_bytes(b"swapped after the check\n")
        return True

    monkeypatch.setattr(verify, "verify_gcode", fake_verify)
    monkeypatch.setattr(verify, "_verify_state", lambda state, printer_id: {"nozzle": {"diameter_mm": 0.4}})
    assert verify._verify_routed(gcode, None, None, None, job=job) is True
    assert job["gcode_full_verified_sha256"].split("+")[0] == hashlib.sha256(BODY).hexdigest()
