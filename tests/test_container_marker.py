"""Issue #971: the `container` pytest marker.

Tests that need the shipped container image are marked `container` and
auto-skip outside the image, so a local `pytest` run on a plain checkout
shows no environment-only failures. Detection is PRINT_HANDY_IN_CONTAINER=1,
set by the Dockerfile in the image; the CI offline-tests job sets it
explicitly so the marked tests keep running there.

These tests pin the whole wiring: the marker registration, the Dockerfile
variable, the CI opt-in, and the skip/run behavior itself (via a subprocess
pytest run over a throwaway marked probe test).
"""
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "test_zz_container_probe_971.py"

PROBE_SOURCE = '''import pytest


@pytest.mark.container
def test_container_probe():
    pass
'''


def test_container_marker_is_registered_in_pyproject():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    markers = config["tool"]["pytest"]["ini_options"]["markers"]
    assert any(str(m).startswith("container:") for m in markers), markers


def test_dockerfile_sets_the_detection_variable_in_the_runtime_stage():
    dockerfile = (ROOT / "Dockerfile").read_text()
    runtime = dockerfile.split(" AS runtime", 1)[1]
    assert "PRINT_HANDY_IN_CONTAINER=1" in runtime


def test_ci_offline_tests_job_opts_in_so_marked_tests_keep_running():
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    steps = ci["jobs"]["offline-tests"]["steps"]
    pytest_steps = [s for s in steps if "pytest" in str(s.get("run", ""))]
    assert pytest_steps, "offline-tests job no longer runs pytest"
    assert any((s.get("env") or {}).get("PRINT_HANDY_IN_CONTAINER") == "1"
               for s in pytest_steps), pytest_steps


def _run_probe(extra_env):
    env = {k: v for k, v in os.environ.items()
           if k not in ("PRINT_HANDY_IN_CONTAINER", "PRINT_HANDY_TEST_SHARD")}
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", str(PROBE)],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=100)


def test_marked_tests_skip_outside_the_container_and_run_inside_it():
    PROBE.write_text(PROBE_SOURCE)
    try:
        outside = _run_probe({})
        assert outside.returncode == 0, outside.stdout + outside.stderr
        assert "1 skipped" in outside.stdout, outside.stdout
        assert "needs the shipped container image" in outside.stdout, outside.stdout

        inside = _run_probe({"PRINT_HANDY_IN_CONTAINER": "1"})
        assert inside.returncode == 0, inside.stdout + inside.stderr
        assert "1 passed" in inside.stdout, inside.stdout
    finally:
        PROBE.unlink(missing_ok=True)
