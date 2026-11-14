"""Focused regression coverage for the #1417 recovery paths."""
from __future__ import annotations

import ast
from pathlib import Path


def test_targeted_except_pass_handlers_are_removed():
    paths = [
        Path("print_handy/jobs/audit.py"),
        Path("print_handy/jobs/prompts.py"),
        Path("print_handy/gcode/verify.py"),
    ]
    for path in paths:
        tree = ast.parse(path.read_text())
        assert not any(
            isinstance(node, ast.ExceptHandler)
            and len(node.body) == 1
            and isinstance(node.body[0], ast.Pass)
            for node in ast.walk(tree)
        ), path


def test_verify_marker_write_failure_is_safe_without_job(monkeypatch, tmp_path):
    from print_handy.gcode import verify as module

    monkeypatch.setattr(module, "_verify_state", lambda state, printer_id: {})
    monkeypatch.setattr(module, "_already_verified_artifact", lambda *args: False)
    monkeypatch.setattr(module, "_recorded_temperatures", lambda *args: None)
    monkeypatch.setattr(module, "verify_gcode", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(module, "_full_verified_marker", lambda *args: (_ for _ in ()).throw(OSError("read-only")))
    monkeypatch.setattr(module.GcodeArtifact, "of", lambda path: type("Artifact", (), {"sha256": "digest"})())
    result = module._verify_routed(tmp_path / "file.gcode", adapter=None, state={}, printer_id="p", job=None)
    assert result == {"ok": True}
