"""Large STL sources skip per-triangle Python loops where the answer is unchanged (#1790).

A 30 MB STL (about 600k faces) loads as CompactTriangles. Bounds and volume
on that type fell back to a per-triangle Python loop (about 4 s a pass, run
eight times while combining two parts), and the multi-file unit question ran
the full topology check (about 48 s) only to read the part's size.
"""

import math
import random
import struct
from pathlib import Path

import pytest

from print_handy.core import geometry_layout, mesh_fast, model_parts
from print_handy.core.compact_geometry import CompactTriangles
from print_handy.jobs import prompts


def _random_compact(count, seed=1790, scale=80.0):
    rng = random.Random(seed)
    return CompactTriangles(
        [tuple(rng.uniform(-scale, scale) for _ in range(3)) for _ in range(3)]
        for _ in range(count))


def _loop_bounds_and_volume(triangles):
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    volume = 0.0
    for (ax, ay, az), (bx, by, bz), (cx, cy, cz) in triangles:
        for point in ((ax, ay, az), (bx, by, bz), (cx, cy, cz)):
            for axis in range(3):
                lo[axis] = min(lo[axis], point[axis])
                hi[axis] = max(hi[axis], point[axis])
        volume += (ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)) / 6.0
    return lo, hi, volume


def test_large_compact_bounds_and_volume_match_the_loop():
    tris = _random_compact(mesh_fast.COMPACT_FAST_MIN + 1000)
    assert mesh_fast.compact_points(tris) is not None
    lo, hi, volume = mesh_fast.bounds_and_volume(tris)
    want_lo, want_hi, want_volume = _loop_bounds_and_volume(list(tris))
    assert (lo, hi) == (want_lo, want_hi)  # bounds are pure selection: exact
    assert volume == pytest.approx(want_volume, rel=1e-9, abs=1e-6)
    assert mesh_fast.bounds(tris) == (want_lo, want_hi)
    assert model_parts._bounds(tris) == (want_lo, want_hi)


def test_small_meshes_keep_the_scalar_path():
    tris = _random_compact(100)
    assert mesh_fast.compact_points(tris) is None
    assert mesh_fast.bounds_and_volume(tris) == mesh_fast.bounds_and_volume(list(tris))


def test_a_non_finite_coordinate_keeps_the_historical_path():
    tris = _random_compact(mesh_fast.COMPACT_FAST_MIN)
    tris.coordinates[4] = float("nan")
    assert mesh_fast.compact_points(tris) is None
    assert mesh_fast.bounds(tris) == mesh_fast._scalar_bounds(tris)
    lo, hi = model_parts._bounds(tris)
    assert all(math.isfinite(value) for value in lo + hi)  # the loop skips NaN, as before


def _write_stl(path, triangles):
    with open(path, "wb") as handle:
        handle.write(b"unit question".ljust(80, b" "))
        handle.write(struct.pack("<I", len(triangles)))
        for tri in triangles:
            handle.write(struct.pack("<12fH", 0.0, 0.0, 1.0, *(v for p in tri for v in p), 0))
    return Path(path)


def _box(width, depth, height):
    p = [(0, 0, 0), (width, 0, 0), (width, depth, 0), (0, depth, 0),
         (0, 0, height), (width, 0, height), (width, depth, height), (0, depth, height)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return [[p[a], p[b], p[c]] for a, b, c in faces]


def test_the_unit_question_reads_the_size_without_the_topology_check(tmp_path, monkeypatch):
    small = _write_stl(tmp_path / "lid.stl", _box(3.0, 2.0, 1.0))
    smaller = _write_stl(tmp_path / "latch.stl", _box(1.0, 1.0, 0.5))

    def no_topology(*_args, **_kwargs):
        raise AssertionError("the unit question must not run the topology check")
    monkeypatch.setattr(geometry_layout, "describe_triangles", no_topology)
    monkeypatch.setattr(geometry_layout, "load_parts", no_topology)

    packet = prompts.parts_unit_question({}, [small, smaller])
    assert packet is not None
    assert tuple(round(v, 3) for v in packet["original_mm"]) == (3.0, 2.0, 1.0)  # the largest part


def test_a_millimeter_sized_set_asks_nothing(tmp_path):
    big = _write_stl(tmp_path / "tongs.stl", _box(44.0, 114.0, 23.0))
    assert max(44.0, 114.0, 23.0) >= prompts.UNIT_QUESTION_MAX_MM
    assert prompts.parts_unit_question({}, [big]) is None
