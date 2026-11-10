"""Caching and bounded reads for the Data tab and Wrapped.

Every page load used to parse all records, build the receipt/JOB joins over
all of them, read whole archived G-code files and aggregate twice. These
tests pin the cache key, the head/tail-only G-code read, the persisted
per-record values, the clip index and the bounded Spoolman call.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from print_handy.core import print_stats, wrapped_joins
from print_handy.webui import data as data_module
from print_handy.webui import wrapped_video


def _write(vault, number, started, **extra):
    prints = Path(vault) / "Prints"
    prints.mkdir(parents=True, exist_ok=True)
    record = {"print_number": number, "status": "completed", "title": f"P{number}",
              "started_at": started, "print_duration_seconds": 60, **extra}
    (prints / f"print-{number:04d}.json").write_text(json.dumps(record))


class SummaryCacheTests(unittest.TestCase):
    def setUp(self):
        data_module.clear_cache()
        self.addCleanup(data_module.clear_cache)

    def data(self, tmp):
        return {"timelapse": {"wrapped_enabled": True}, "paths": {"records_vault": str(tmp)}}

    def test_second_load_does_not_rebuild_and_a_new_record_does(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, 1, "2026-03-01T10:00:00Z")
            data = self.data(tmp)
            with mock.patch.object(print_stats, "build_wrapped",
                                   wraps=print_stats.build_wrapped) as built, \
                    mock.patch.object(print_stats, "load_records",
                                      wraps=print_stats.load_records) as loaded:
                data_module.wrapped_view(data, 2026)
                data_module.wrapped_view(data, 2026)
                self.assertEqual(built.call_count, 1)
                self.assertEqual(loaded.call_count, 1)
                _write(tmp, 2, "2026-03-02T10:00:00Z")
                code, body = data_module.wrapped_view(data, 2026)
                self.assertEqual(built.call_count, 2)
                self.assertEqual(body["headline"]["total_prints"], 2)

    def test_data_view_aggregates_once_and_wrapped_does_not_aggregate_for_years(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, 1, "2026-03-01T10:00:00Z")
            data = self.data(tmp)
            with mock.patch.object(print_stats, "aggregate",
                                   wraps=print_stats.aggregate) as aggregated:
                data_module.wrapped_view(data, 2026)
                self.assertEqual(aggregated.call_count, 1)  # inside build_wrapped only
                data_module.data_view(data, 2026)
                self.assertEqual(aggregated.call_count, 2)

    def test_ttl_bounds_staleness(self):
        calls = []
        key = ("ttl-test",)
        data_module._cached(key, lambda: calls.append(1) or 1, ttl=0.05)
        data_module._cached(key, lambda: calls.append(1) or 1, ttl=0.05)
        self.assertEqual(len(calls), 1)
        time.sleep(0.08)
        data_module._cached(key, lambda: calls.append(1) or 1, ttl=0.05)
        self.assertEqual(len(calls), 2)

    def test_enrichment_work_is_scoped_to_the_selected_year(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, 1, "2025-03-01T10:00:00Z")
            _write(tmp, 2, "2026-03-01T10:00:00Z")
            seen = {}

            def fake(records, **kwargs):
                seen["all"] = len(records)
                seen["scope"] = [r["print_number"] for r in kwargs["scope"]]
                return {}

            with mock.patch.object(wrapped_joins, "build_enrichments", side_effect=fake):
                data_module.wrapped_view(self.data(tmp), 2026)
            self.assertEqual(seen, {"all": 2, "scope": [2]})

    def test_unreachable_spoolman_is_bounded_cached_and_never_fails_the_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, 1, "2026-03-01T10:00:00Z")
            data = self.data(tmp)
            from print_handy.core import spoolman_client
            with mock.patch.object(spoolman_client, "list_spools",
                                   side_effect=OSError("down")) as listed:
                code, body = data_module.data_view(data, 2026)
                code2, body2 = data_module.data_view(data, 2026)
            self.assertEqual((code, code2), (200, 200))
            self.assertEqual(body["spoolman_lifetime"]["state"], "unavailable")
            self.assertEqual(listed.call_count, 1)
            self.assertEqual(listed.call_args.kwargs["timeout"], data_module.SPOOLMAN_TIMEOUT_S)


class GcodeReadTests(unittest.TestCase):
    HEADER = ("; filament_colour = #FF0000;#00FF00\n; filament used [g] = 1.50,2.50\n"
              "; filament_type = PLA;PLA\n")

    def test_only_head_and_tail_of_a_large_file_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.gcode"
            body = ("G1 X1 Y1 E1\n" * 80000)  # ~1 MB of toolpath
            path.write_text(self.HEADER + body + "; end\n")
            reads = []
            real_open = open

            def spy(file, mode="r", *args, **kwargs):
                handle = real_open(file, mode, *args, **kwargs)
                if str(file) == str(path):
                    original = handle.read
                    handle.read = lambda n=-1: (reads.append(n), original(n))[1]
                return handle

            with mock.patch("builtins.open", spy):
                colors = wrapped_joins._per_color_from_gcode(path)
            self.assertEqual(colors, {"#FF0000": 1.5, "#00FF00": 2.5})
            self.assertTrue(reads and all(n != -1 and 0 < n <= 65536 for n in reads), reads)

    def test_small_file_is_read_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.gcode"
            path.write_text(self.HEADER)
            self.assertEqual(wrapped_joins._per_color_from_gcode(path),
                             {"#FF0000": 1.5, "#00FF00": 2.5})


class DerivedCacheAndJoinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def job(self, name, number, grams):
        folder = self.root / "archive" / name
        folder.mkdir(parents=True)
        gcode = folder / "plate.gcode"
        gcode.write_text(f"; filament_colour = #AAAAAA\n; filament used [g] = {grams}\n")
        (folder / "JOB.json").write_text(json.dumps(
            {"print_number": number, "gcode": str(gcode)}))
        return gcode

    def record(self, number, key, local):
        return {"print_number": number, "record_id": key, "started_at": "2026-01-01T00:00:00Z",
                "slicer_settings": {"local_gcode": str(local)}}

    def test_a_shared_print_number_joins_on_the_record_not_the_number(self):
        first = self.job("a", 7, "10.0")
        second = self.job("b", 7, "99.0")
        records = [self.record(7, "r1", first), self.record(7, "r2", second)]
        result = wrapped_joins.build_enrichments(
            records, archive_dir=self.root / "archive")
        by_record = result["per_color_grams_by_record"]
        self.assertEqual(by_record["r1"], {"#AAAAAA": 10.0})
        self.assertEqual(by_record["r2"], {"#AAAAAA": 99.0})

    def test_a_bare_shared_number_with_no_path_joins_nothing(self):
        self.job("a", 7, "10.0")
        self.job("b", 7, "99.0")
        records = [{"print_number": 7, "record_id": "r1",
                    "started_at": "2026-01-01T00:00:00Z"}]
        result = wrapped_joins.build_enrichments(records, archive_dir=self.root / "archive")
        self.assertEqual(result["per_color_grams_by_record"], {})

    def test_derived_values_are_computed_once_until_the_file_changes(self):
        gcode = self.job("a", 7, "10.0")
        records = [self.record(7, "r1", gcode)]
        cache = self.root / "wrapped" / "derived-cache.json"
        with mock.patch.object(wrapped_joins, "_per_color_from_gcode",
                               wraps=wrapped_joins._per_color_from_gcode) as read:
            for _ in range(3):
                result = wrapped_joins.build_enrichments(
                    records, archive_dir=self.root / "archive", derived_cache=cache)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(result["per_color_grams_by_record"]["r1"], {"#AAAAAA": 10.0})
            gcode.write_text("; filament_colour = #AAAAAA\n; filament used [g] = 12.0\n"
                             "; padding to change the size\n")
            result = wrapped_joins.build_enrichments(
                records, archive_dir=self.root / "archive", derived_cache=cache)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(result["per_color_grams_by_record"]["r1"], {"#AAAAAA": 12.0})

    def test_scope_limits_the_per_record_reads(self):
        a = self.job("a", 1, "1.0")
        b = self.job("b", 2, "2.0")
        records = [self.record(1, "r1", a), self.record(2, "r2", b)]
        with mock.patch.object(wrapped_joins, "_per_color_from_gcode",
                               wraps=wrapped_joins._per_color_from_gcode) as read:
            wrapped_joins.build_enrichments(
                records, archive_dir=self.root / "archive", scope=[records[1]])
        self.assertEqual(read.call_count, 1)


class ClipIndexTests(unittest.TestCase):
    def test_index_resolves_like_the_scan(self):
        records = [{"print_number": 90, "print_number_was": 12,
                    "started_at": "2026-04-01T16:00:00Z"},
                   {"print_number": 12, "started_at": "2026-04-01T10:00:00Z"}]
        index = wrapped_video.claims_index(records)
        for name in ("20260401-1000-A-print-12.mp4", "20260401-1600-B-print-12.mp4",
                     "20260401-1600-B-print-5.mp4"):
            self.assertEqual(
                wrapped_video.resolve_clip_print_number(name, records, None, index),
                wrapped_video.resolve_clip_print_number(name, records, None))

    def test_finder_builds_the_index_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "arch"
            archive.mkdir()
            for n in range(5):
                (archive / f"20260301-1200-M-print-{n + 1}.mp4").write_bytes(b"x")
            data = {"timelapse": {"archive_dir": str(archive)}, "paths": {"cache": ""}}
            with mock.patch.object(wrapped_video, "claims_index",
                                   wraps=wrapped_video.claims_index) as built:
                found = wrapped_video.default_clip_finder_detailed(
                    data, 2026, records=[{"print_number": 1}])
            self.assertEqual(len(found), 5)
            self.assertEqual(built.call_count, 1)


if __name__ == "__main__":
    unittest.main()
