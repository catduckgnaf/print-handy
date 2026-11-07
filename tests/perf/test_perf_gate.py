"""Offline performance gate for the 3MF intake fast paths.

Design (numbers measured on a 4 core host, see the PR description):
  * Fixtures are generated grid meshes, so there is no private or unlicensed model data.
  * Timings are ratios against a reference run in the same process, so machine speed cancels.
    A flat seconds ceiling would be a coin flip between a fast laptop and a loaded CI runner, so
    the absolute ceiling in baselines.json is reporting only (a PerfCeilingWarning), never a
    failure: PR #1014 failed on it (0.31s > 0.25s) with its ratio well inside the gate (#1017).
  * Each gate first proves the work really happened (triangle count, coordinate checksum, fast path
    taken). A gate that only timed an empty function would pass, so the work checks run first.
  * The gate must fail on injected delay; test_gate_rejects_injected_delay proves that.
Baselines live in tests/perf/baselines.json and are reviewed by hand. Drift is reported, never
refreshed automatically.
"""
import json
import statistics
import time
import warnings
import zipfile
from pathlib import Path

import pytest

from print_handy.core import mesh_fast, model_job, orca_adapter

NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
GRID = 100  # 2 * 99 * 99 = 19602 triangles
BASELINES = json.loads((Path(__file__).with_name("baselines.json")).read_text())
RUNS = 5


def _grid_model(n):
    vertices = "".join(
        f'<vertex x="{i * 0.5}" y="{j * 0.5}" z="{(i * j) % 7 * 0.25}"/>' for j in range(n) for i in range(n))
    triangles = "".join(
        f'<triangle v1="{j * n + i}" v2="{j * n + i + 1}" v3="{(j + 1) * n + i}"/>'
        f'<triangle v1="{j * n + i + 1}" v2="{(j + 1) * n + i + 1}" v3="{(j + 1) * n + i}"/>'
        for j in range(n - 1) for i in range(n - 1))
    return (f'<model xmlns="{NS}"><resources><object id="1"><mesh><vertices>{vertices}</vertices>'
            f'<triangles>{triangles}</triangles></mesh></object></resources>'
            f'<build><item objectid="1"/></build></model>').encode()


@pytest.fixture(scope="module")
def grid_3mf(tmp_path_factory):
    path = tmp_path_factory.mktemp("perf") / "grid.3mf"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("3D/3dmodel.model", _grid_model(GRID))
    return path


def _checksum(triangles):
    return round(sum(p[0] + 2 * p[1] + 3 * p[2] for t in triangles for p in t), 3)


def _expected_checksum():
    n = GRID
    pts = {(i, j): (i * 0.5, j * 0.5, (i * j) % 7 * 0.25) for j in range(n) for i in range(n)}
    total = 0.0
    for j in range(n - 1):
        for i in range(n - 1):
            for tri in (((i, j), (i + 1, j), (i, j + 1)), ((i + 1, j), (i + 1, j + 1), (i, j + 1))):
                for key in tri:
                    x, y, z = pts[key]
                    total += x + 2 * y + 3 * z
    return round(total, 3)


def _time(fn):
    start = time.perf_counter()
    result = fn()
    return time.perf_counter() - start, result


def paired_ratio(candidate, reference, runs=RUNS):
    """Interleaved min over runs, so drift hits both sides; returns (ratio, candidate_s, reference_s)."""
    cand, ref = [], []
    for _ in range(runs):
        cand.append(_time(candidate)[0])
        ref.append(_time(reference)[0])
    return min(cand) / min(ref), min(cand), min(ref)


class PerfCeilingWarning(UserWarning):
    """A candidate ran past its absolute seconds ceiling. Reported, never failed: wall-clock
    seconds measure the runner as much as the code; the paired ratio is the gate."""


def check_gate(name, ratio, candidate_s):
    """The blocking problems: only the paired ratio, which cancels machine speed."""
    limits = BASELINES[name]
    problems = []
    if ratio > limits["max_ratio"]:
        problems.append(f"ratio {ratio:.2f} > reviewed max {limits['max_ratio']} (baseline {limits['baseline_ratio']})")
    report_ceiling(name, candidate_s)
    return problems


def report_ceiling(name, candidate_s):
    """Warn (never fail) when the candidate's wall-clock time passes the absolute ceiling."""
    ceiling = BASELINES[name]["ceiling_s"]
    if candidate_s > ceiling:
        message = f"{name}: {candidate_s:.2f}s > absolute ceiling {ceiling}s (reporting only; the ratio is the gate)"
        warnings.warn(message, PerfCeilingWarning, stacklevel=2)
        return message
    return None


def _parse(path, use_fast):
    def run():
        mesh_fast._TABLE_CACHE.clear()
        original = mesh_fast.archive_tables
        if not use_fast:
            mesh_fast.archive_tables = lambda archive, name: (archive.read(name), None)
        try:
            return orca_adapter._parse_3mf_triangles(path)
        finally:
            mesh_fast.archive_tables = original
    return run


def test_parse_does_the_required_work(grid_3mf, monkeypatch):
    calls = []
    original = mesh_fast.archive_tables

    def spy(archive, name):
        result = original(archive, name)
        calls.append(result[1] is not None)
        return result

    monkeypatch.setattr(mesh_fast, "archive_tables", spy)
    mesh_fast._TABLE_CACHE.clear()
    triangles = orca_adapter._parse_3mf_triangles(grid_3mf)
    assert len(triangles) == 2 * (GRID - 1) ** 2
    assert _checksum(triangles) == _expected_checksum()
    assert calls == [True], "fast tables must be built exactly once and really used"


def test_parse_perf_gate(grid_3mf):
    ratio, fast_s, _ = paired_ratio(_parse(grid_3mf, True), _parse(grid_3mf, False))
    assert not check_gate("parse_3mf", ratio, fast_s)


def _bounds_old(tris):
    lo = [min(p[a] for t in tris for p in t) for a in range(3)]
    hi = [max(p[a] for t in tris for p in t) for a in range(3)]
    vol = 0.0
    for (ax, ay, az), (bx, by, bz), (cx, cy, cz) in tris:
        vol += (ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)) / 6.0
    return lo, hi, vol


@pytest.fixture(scope="module")
def grid_triangles(grid_3mf):
    return orca_adapter._parse_3mf_triangles(grid_3mf)


def test_bounds_does_the_required_work(grid_triangles):
    lo, hi, vol = mesh_fast.bounds_and_volume(grid_triangles)
    old_lo, old_hi, old_vol = _bounds_old(grid_triangles)
    assert [list(lo), list(hi), vol] == [old_lo, old_hi, old_vol]
    assert hi[0] - lo[0] == pytest.approx((GRID - 1) * 0.5)
    assert hi[2] - lo[2] > 0


def test_bounds_perf_gate(grid_triangles):
    ratio, fast_s, _ = paired_ratio(
        lambda: mesh_fast.bounds_and_volume(grid_triangles), lambda: _bounds_old(grid_triangles))
    assert not check_gate("bounds_and_volume", ratio, fast_s)


@pytest.mark.parametrize("name", ["parse_3mf", "bounds_and_volume"])
def test_gate_rejects_injected_delay(name, grid_3mf, grid_triangles):
    """A real slowdown must trip the gate: wrap the candidate with a sleep equal to the reference time."""
    if name == "parse_3mf":
        candidate, reference = _parse(grid_3mf, True), _parse(grid_3mf, False)
    else:
        candidate = lambda: mesh_fast.bounds_and_volume(grid_triangles)
        reference = lambda: _bounds_old(grid_triangles)
    ref_s = min(_time(reference)[0] for _ in range(3))

    # Twice the reference time: the nominal ratio lands near 3, so the reference drifting slower by
    # up to ~2x between this measurement and the paired runs below still cannot hide the delay.
    delay_s = 2 * ref_s

    def slowed():
        time.sleep(delay_s)
        return candidate()

    ratio, slow_s, _ = paired_ratio(slowed, reference, runs=3)
    assert check_gate(name, ratio, slow_s), f"injected {delay_s:.3f}s delay was not detected (ratio {ratio:.2f})"


def test_gate_rejects_missing_computation(grid_triangles):
    """A candidate that skips the work is fast; the work assertions, not the timer, must catch it."""
    lo, hi, vol = mesh_fast.bounds_and_volume(grid_triangles[:1])
    assert [list(lo), list(hi), vol] != list(_bounds_old(grid_triangles))


def test_absolute_ceiling_reports_but_never_fails():
    """#1017: a slow runner past the seconds ceiling with a good ratio is a warning, not a failure."""
    limits = BASELINES["bounds_and_volume"]
    slow_s = limits["ceiling_s"] * 1.25
    with pytest.warns(PerfCeilingWarning, match="reporting only"):
        problems = check_gate("bounds_and_volume", limits["baseline_ratio"], slow_s)
    assert problems == []
    # A bad ratio still fails, whatever the wall clock says.
    assert check_gate("bounds_and_volume", limits["max_ratio"] + 0.1, 0.001)
