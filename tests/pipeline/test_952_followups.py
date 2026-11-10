"""#952 follow-ups: bounds-cache validation/pruning, Bambu packaging parity,
slicer elapsed timing, and the TIMING stage split.

Each test pins a defect or gap the #952 deep dive confirmed at 256957d:
- cached_motion_bounds trusted any structurally valid JSON entry (string
  "bounds" came back as tuples of characters) and the on-disk cache had no
  eviction of any kind.
- delivery.bambu_package deflated at the default zip level 6 while
  gcode_3mf.package (the same layout for ZMod) uses level 1 (#929).
- _run_slicer logged its elapsed time but recorded it nowhere a TIMING
  consumer can read, so orca_begin -> orca_finalize_done could not be split
  into slicer time vs Print Handy Python time.
"""
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from print_handy.core import delivery, model_job, orca_adapter

SMALL = b"G90\nG1 X10 Y10 Z0.2 E1\nG1 X20 Y20 E2\n"


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    root = tmp_path / "bounds-cache"
    monkeypatch.setattr(model_job, "BOUNDS_CACHE", root)
    return root


def _entry(cache_dir, raw, value, name=None):
    """Write a cache entry where the current parser looks for it, stamped with
    the current parser version unless the value says otherwise."""
    digest = hashlib.sha256(raw).hexdigest()
    cache_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(value, dict):
        value = {"parser": model_job.BOUNDS_PARSER_VERSION, **value}
    name = name or f"{digest}.p{model_job.BOUNDS_PARSER_VERSION}.json"
    (cache_dir / name.format(digest=digest)).write_text(json.dumps(value))
    return digest


# S5: an S4-bypass file whose old (API 2) verdict was "X stays at 10". The
# current parser sees the G91 and reports the real reach.
BYPASS = b"G90\nG1 X10 Y1 Z1\nG91;rel\nG1 X100\nG1 X100\nG1 X100\n"
OLD_VERDICT = {"x": [10, 100], "y": [1, 1], "z": [1, 1]}


def test_bounds_cache_ignores_a_pre_version_entry(cache_dir):
    # Written by a parser before the cache carried a version: <sha>.json.
    _entry(cache_dir, BYPASS, dict(OLD_VERDICT), name="{digest}.json")
    assert model_job.cached_motion_bounds(BYPASS)["x"] == (10.0, 310.0)


def test_bounds_cache_ignores_an_entry_from_another_parser_version(cache_dir):
    # Right file name, but the entry says an older parser wrote it.
    _entry(cache_dir, BYPASS, {**OLD_VERDICT, "parser": model_job.BOUNDS_PARSER_VERSION - 1})
    assert model_job.cached_motion_bounds(BYPASS)["x"] == (10.0, 310.0)


def test_bounds_cache_hit_needs_the_current_version_and_records_it(cache_dir):
    first = model_job.cached_motion_bounds(SMALL)
    entries = list(cache_dir.glob("*.json"))
    assert len(entries) == 1
    stored = json.loads(entries[0].read_text())
    assert stored["parser"] == model_job.BOUNDS_PARSER_VERSION
    assert f".p{model_job.BOUNDS_PARSER_VERSION}." in entries[0].name
    # A current-version entry is a hit: its (edited) value comes back.
    _entry(cache_dir, SMALL, {"x": [1, 2], "y": [1, 2], "z": [1, 2]})
    assert model_job.cached_motion_bounds(SMALL) == {"x": (1.0, 2.0), "y": (1.0, 2.0), "z": (1.0, 2.0)}
    assert first != model_job.cached_motion_bounds(SMALL)


def test_bounds_cache_recomputes_a_corrupt_string_entry(cache_dir):
    _entry(cache_dir, SMALL, {"x": "ab", "y": [0, 20], "z": [0, 0.2]})
    bounds = model_job.cached_motion_bounds(SMALL)
    assert bounds == {"x": (10.0, 20.0), "y": (10.0, 20.0), "z": (0.2, 0.2)}


def test_bounds_cache_recomputes_an_inverted_entry(cache_dir):
    _entry(cache_dir, SMALL, {"x": [20, 10], "y": [10, 20], "z": [0.2, 0.2]})
    bounds = model_job.cached_motion_bounds(SMALL)
    assert bounds["x"] == (10.0, 20.0)


def test_bounds_cache_recomputes_a_non_finite_entry(cache_dir):
    _entry(cache_dir, SMALL, {"x": [0, 20], "y": [10, 20], "z": [0.2, float("nan")]})
    bounds = model_job.cached_motion_bounds(SMALL)
    assert bounds["z"] == (0.2, 0.2)


def test_bounds_cache_prunes_to_the_cap(cache_dir, monkeypatch):
    monkeypatch.setattr(model_job, "BOUNDS_CACHE_MAX_ENTRIES", 3)
    import os
    import time
    cache_dir.mkdir(parents=True)
    for i in range(5):
        entry = cache_dir / f"{i:064d}.json"
        entry.write_text("{}")
        os.utime(entry, (1000 + i, 1000 + i))
    model_job.cached_motion_bounds(SMALL)  # a miss writes one more entry, then prunes
    remaining = sorted(p.name for p in cache_dir.glob("*.json"))
    assert len(remaining) == 3
    assert f"{0:064d}.json" not in remaining  # the oldest go first


def test_bambu_package_uses_level_1_like_the_zmod_package(tmp_path, monkeypatch):
    seen = {}
    real_zipfile = zipfile.ZipFile

    class SpyZip(real_zipfile):
        def writestr(self, zinfo_or_arcname, data, compress_type=None, compresslevel=None):
            info = zinfo_or_arcname
            if isinstance(info, zipfile.ZipInfo) and info.filename.endswith(".gcode"):
                seen["level"] = info._compresslevel
            return super().writestr(zinfo_or_arcname, data, compress_type, compresslevel)

    monkeypatch.setattr(delivery.zipfile, "ZipFile", SpyZip)
    gcode = tmp_path / "plate_1.gcode"
    gcode.write_bytes(SMALL * 100)
    out = delivery.bambu_package(gcode, tmp_path / "plate_1.gcode.3mf")
    assert seen["level"] == 1
    with real_zipfile(out) as archive:  # still a readable package, byte-identical inner G-code
        assert archive.read("Metadata/plate_1.gcode") == SMALL * 100


def test_run_slicer_records_elapsed_on_the_result_and_sink(tmp_path, monkeypatch):
    def fake_spawn(command, *, timeout, cwd):
        return subprocess.CompletedProcess(command, 0, "out", "")

    monkeypatch.setattr(orca_adapter, "_spawn_slicer", fake_spawn)
    sink = []
    result = orca_adapter._run_slicer(["orca-slicer"], "OrcaSlicer", tmp_path, timing_sink=sink)
    assert result.elapsed_s >= 0
    assert sink == [result.elapsed_s]
