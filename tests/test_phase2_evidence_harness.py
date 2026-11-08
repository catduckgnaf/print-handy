"""Offline contracts for phase-two evidence and OpenSCAD benchmark tooling.

No actual GPU, printer, firmware, account, OpenSCAD or source file is required.
"""
import json
import subprocess
from pathlib import Path

import pytest

from scripts import phase2_evidence as evidence
from scripts import benchmark_openscad_backends as benchmark


def test_issue_inventory_is_explicit_and_closed_1071_is_not_claimed():
    assert set(evidence.CASES) == {1067, 1068, 1069, 1070, 1072}
    assert "paused-command-layer" in evidence.CASES[1070]
    assert "release-candidate" in evidence.CASES[1072]


@pytest.mark.parametrize("bad", [
    {"issue": 1071, "case": "already-merged"},
    {"issue": 1070, "case": "unlisted-test"},
])
def test_unknown_cases_cannot_be_recorded(bad):
    with pytest.raises(ValueError, match="unknown issue/case"):
        evidence.make_record(**bad, status="blocked",
                             context="Real AD5X install", note="Cannot run on this host")


def test_observed_requires_actual_attachment_and_preserves_hash(tmp_path):
    f = tmp_path / "sanitized-run.txt"
    f.write_bytes(b"UNVERIFIED mock evidence content; test only\n")
    r = evidence.make_record(issue=1070, case="runout-diagnostics", status="observed",
                             context="AD5X firmware reproduction", note="Operator attached sanitized log",
                             artifact=f)
    assert r["attachment"]["sha256"] == evidence.sha256(f)
    assert evidence.verify_record(r, tmp_path) == []
    f.write_bytes(b"changed")
    assert any("changed after capture" in e for e in evidence.verify_record(r, tmp_path))


def test_observed_without_attachment_is_refused():
    with pytest.raises(ValueError, match="require an actual evidence"):
        evidence.make_record(issue=1067, case="camera-detector", status="observed",
                             context="On-device camera test", note="Measured")


def test_deferred_requires_reason_and_does_not_imply_evidence():
    with pytest.raises(ValueError, match="explanatory"):
        evidence.make_record(issue=1068, case="renumber-applied", status="deferred",
                             context="Owner live server", note="no")
    r = evidence.make_record(issue=1068, case="renumber-applied", status="deferred",
                             context="Owner live server", note="Awaiting operator approval")
    assert r["attachment"] is None
    assert evidence.verify_record(r, Path(".")) == []


@pytest.mark.parametrize("name", ["../private.txt", "/etc/passwd", "sub/path", "..", "C:\\secret"])
def test_verifier_rejects_unsafe_attachment_names(name, tmp_path):
    r = evidence.make_record(issue=1067, case="nvenc-encode", status="blocked",
                             context="One NVIDIA GPU test", note="Need a real card to run")
    r["status"] = "observed"
    r["attachment"] = {"name": name, "sha256": "0" * 64, "bytes": 1}
    assert any("safe basename" in e for e in evidence.verify_record(r, tmp_path))


def test_verifier_refuses_forged_issue_and_missing_artifact(tmp_path):
    r = evidence.make_record(issue=1067, case="nvenc-encode", status="blocked",
                             context="One NVIDIA GPU test", note="Need a real card to run")
    r["issue"] = 1071
    assert "unknown issue/case" in evidence.verify_record(r, tmp_path)
    r["issue"] = 1067
    r["status"] = "observed"
    assert "observed entry has no evidence file" in evidence.verify_record(r, tmp_path)


def test_record_cli_requires_non_overwrite(tmp_path):
    args = ["record", "--issue", "1070", "--case", "paused-command-layer",
            "--status", "blocked", "--context", "AD5X printer with HelixScreen",
            "--note", "Awaiting live firmware and UI logs",
            "--out", str(tmp_path / "proof.json")]
    assert evidence.main(args) == 0
    r = json.loads((tmp_path / "proof.json").read_text())
    assert evidence.verify_record(r, tmp_path) == []
    with pytest.raises(SystemExit) as error:
        evidence.main(args)
    assert error.value.code == 2


def test_command_does_not_claim_gpu_acceleration_or_use_unsafe_shell(tmp_path):
    src = tmp_path / "a.scad"
    out = tmp_path / "a.stl"
    rss = tmp_path / "rss.txt"
    command = benchmark.build_command("/usr/bin/time", "/opt/open scad", "manifold", src, out, rss)
    assert command[:4] == ["/usr/bin/time", "-f", "%M", "-o"]
    assert "--backend=manifold" in command
    assert command[-2:] == [str(out), str(src)]
    assert not any("shell=" in word for word in command)
    baseline = benchmark.build_command("/usr/bin/time", "/usr/bin/openscad", None, src, out, rss)
    assert not any(word.startswith("--backend") for word in baseline)


def test_benchmark_summary_reports_failures_without_inventing_measurement():
    failed = {"passed": False, "wall_seconds": 5.0, "peak_rss_kib": None}
    assert benchmark.summarize([failed])["wall_median_s"] is None
    values = [
        {"passed": True, "wall_seconds": 1.0, "peak_rss_kib": 1024},
        {"passed": True, "wall_seconds": 3.0, "peak_rss_kib": 2048},
        {"passed": True, "wall_seconds": 2.0, "peak_rss_kib": 512},
    ]
    result = benchmark.summarize(values)
    assert result["wall_median_s"] == 2.0
    assert result["rss_peak_kib"] == 2048
    assert result["passed"] is True
    assert benchmark.summarize(values + [failed])["passed"] is False


def test_benchmark_records_nonexistent_output_as_failure(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, b"", b"")
    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)
    result = benchmark.run_once("/usr/bin/time", "openscad", None,
                                tmp_path / "fixture.scad",
                                tmp_path / "missing.stl", tmp_path / "missing-rss.txt", 5)
    assert result["passed"] is False
    assert result["output_sha256"] is None
