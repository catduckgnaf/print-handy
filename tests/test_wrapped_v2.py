"""#981 Wrapped v2: watchdog events, timelapse name join, receipt/JOB
join, per-color grams, Spoolman lifetime, maintenance block, worst
failure sidecar, derived grams, headline caveat.

Fakes and fixtures only: temporary vaults, fake Spoolman spool lists, a
fake ffmpeg runner. No network, no real Spoolman, no printer.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from print_handy.core import print_stats, wrapped_joins
from print_handy.hermes import watchdog_events
from print_handy.webui import wrapped_video


def _rec(number, **kw):
    record = {"print_number": number, "status": "completed", "title": f"Print {number}",
              "gcode_file": f"print-{number}.gcode", "started_at": "2026-03-01T12:00:00Z",
              "print_duration_seconds": 3600, "filament_used_g": 10.0}
    record.update(kw)
    return record


EVENTS = [
    {"timestamp": "2026-03-01T12:30:00Z", "printer": "AD5X", "print_number": 1,
     "job_key": "a.gcode", "verdict": "spaghetti", "streak": 2, "action": "alert"},
    {"timestamp": "2026-03-01T12:45:00Z", "printer": "AD5X", "print_number": 1,
     "job_key": "a.gcode", "verdict": "spaghetti", "streak": 3, "action": "alert"},
    {"timestamp": "2026-04-02T09:00:00Z", "printer": "AD5X", "print_number": 2,
     "job_key": "b.gcode", "verdict": "spaghetti", "streak": 1, "action": "alert"},
    {"timestamp": "2026-05-03T09:00:00Z", "printer": "AD5X", "print_number": 3,
     "job_key": "c.gcode", "verdict": "spaghetti", "streak": 1, "action": "alert"},
    {"timestamp": "2026-05-04T09:00:00Z", "printer": "AD5X", "print_number": 3,
     "job_key": "c.gcode", "verdict": "detached", "streak": 1, "action": "alert"},
    {"timestamp": "not-a-date", "printer": "AD5X", "print_number": 9,
     "job_key": "z.gcode", "verdict": "spaghetti", "streak": 5, "action": "alert"},
    {"timestamp": "2025-12-31T09:00:00Z", "printer": "AD5X", "print_number": 8,
     "job_key": "y.gcode", "verdict": "spaghetti", "streak": 4, "action": "alert"},
]


class WatchdogEventWriteTests(unittest.TestCase):
    def test_flagged_verdict_writes_event_with_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            ok = watchdog_events.record_verdict(
                verdict="spaghetti", printer="AD5X", print_number=7,
                job_key="widget.gcode", streak=3, action="alert",
                timestamp="2026-03-01T12:30:00Z", vault=vault)
            self.assertTrue(ok)
            lines = (vault / "Watchdog" / "events.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 1)
            event = json.loads(lines[0])
            self.assertEqual(event, {
                "timestamp": "2026-03-01T12:30:00Z", "printer": "AD5X",
                "print_number": 7, "job_key": "widget.gcode",
                "verdict": "spaghetti", "streak": 3, "action": "alert"})

    def test_healthy_verdicts_are_not_recorded(self):
        # The documented event rule: alert verdicts only. An "ok" verdict
        # every few minutes would grow the file for no recap value.
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            for verdict in ("ok", "safe", "empty_idle"):
                self.assertFalse(watchdog_events.record_verdict(
                    verdict=verdict, printer="AD5X", vault=vault))
            self.assertFalse((vault / "Watchdog" / "events.jsonl").exists())

    def test_append_failure_returns_false_and_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocker"
            blocker.write_text("a file, not a directory")
            ok = watchdog_events.record_verdict(
                verdict="spaghetti", printer="AD5X", vault=blocker / "vault")
            self.assertFalse(ok)

    def test_append_only_second_event_keeps_first_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            watchdog_events.record_verdict(verdict="spaghetti", vault=vault,
                                            timestamp="2026-01-01T00:00:00Z")
            watchdog_events.record_verdict(verdict="detached", vault=vault,
                                            timestamp="2026-01-02T00:00:00Z")
            lines = (vault / "Watchdog" / "events.jsonl").read_text().splitlines()
            self.assertEqual([json.loads(line)["verdict"] for line in lines],
                             ["spaghetti", "detached"])


class BambuWatchEventTests(unittest.TestCase):
    """The watch path still returns its verdict / posts its alert when
    event recording fails outright."""

    def _monitor(self, recorder):
        from print_handy.hermes import bambu_watch, visual_watch
        alerts = []
        monitor = bambu_watch.BambuPrintMonitor(
            "widget.gcode", status_fn=lambda: {"state": "printing"},
            camera_fn=lambda: b"not-a-jpeg",
            post=lambda *a, **k: None,
            watch=visual_watch.VisualPrintWatch(),
            verdict=lambda image, prompt: ("spaghetti", "messy"),
            alert=lambda *a, **k: alerts.append(a),
            changed=lambda a, b: True,
            event_recorder=recorder)
        return monitor, alerts

    def test_visual_check_records_event_and_alerts(self):
        seen = []
        monitor, alerts = self._monitor(lambda **kw: seen.append(kw) or True)
        monitor.visual_check("periodic", 3)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["verdict"], "spaghetti")
        self.assertEqual(seen[0].get("filename") or seen[0].get("job_key"),
                         "widget.gcode")
        self.assertEqual(seen[0]["action"], "alert")

    def test_visual_check_survives_a_raising_recorder(self):
        def boom(**kw):
            raise RuntimeError("disk gone")
        monitor, alerts = self._monitor(boom)
        monitor.visual_check("periodic", 3)  # must not raise
        self.assertEqual(len(alerts), 1)


class EventConsumeTests(unittest.TestCase):
    def test_loader_skips_and_names_malformed_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            folder = vault / "Watchdog"
            folder.mkdir(parents=True)
            (folder / "events.jsonl").write_text(
                json.dumps(EVENTS[0]) + "\n{broken\n" + json.dumps(EVENTS[1]) + "\n")
            events, skipped, exists = print_stats.load_watchdog_events(vault)
        self.assertTrue(exists)
        self.assertEqual(len(events), 2)
        self.assertEqual(skipped, ["Watchdog/events.jsonl line 2"])

    def test_missing_file_is_the_not_recorded_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            events, skipped, exists = print_stats.load_watchdog_events(Path(tmp))
        self.assertEqual((events, skipped, exists), ([], [], False))
        stats = print_stats.aggregate([_rec(1)], year=2026)
        self.assertIsNone(stats["outcomes"]["pasta"])
        self.assertEqual(stats["outcomes"]["pasta_state"], "not_recorded")

    def test_empty_existing_file_is_recorded_zero(self):
        stats = print_stats.aggregate([_rec(1)], year=2026, events=[])
        self.assertEqual(stats["outcomes"]["pasta"], 0)
        self.assertEqual(stats["outcomes"]["pasta_state"], "recorded")

    def test_pasta_counts_distinct_prints_and_worst_spaghetti(self):
        records = [_rec(1), _rec(2), _rec(3)]
        stats = print_stats.aggregate(records, year=2026, events=EVENTS)
        outcomes = stats["outcomes"]
        # Prints 1, 2 and 3 had spaghetti in 2026; print 9's timestamp is
        # unparseable (excluded from the year, like records) and print 8
        # is a 2025 event.
        self.assertEqual(outcomes["pasta"], 3)
        self.assertEqual(outcomes["pasta_events"], 4)
        self.assertEqual(outcomes["pasta_state"], "recorded")
        self.assertEqual(outcomes["pasta_all_time_prints"], 5)
        self.assertEqual(outcomes["pasta_recording_started_at"], "2025-12-31T09:00:00Z")
        self.assertIn("recording began", outcomes["pasta_note"])
        worst = stats["worst_spaghetti"]
        self.assertEqual(worst["print_number"], 1)
        self.assertEqual(worst["streak"], 3)
        self.assertEqual(worst["events"], 2)
        self.assertEqual(worst["title"], "Print 1")
        # Unfiltered: the unparseable-timestamp event counts, and its
        # streak of 5 makes print 9 the all-time worst.
        all_stats = print_stats.aggregate(records, events=EVENTS)
        self.assertEqual(all_stats["outcomes"]["pasta"], 5)
        self.assertEqual(all_stats["worst_spaghetti"]["print_number"], 9)

    def test_spaghetti_magnet_fires_on_events_never_on_risk_flags(self):
        records = [_rec(i, filament_color=("Black", "Red", "Blue")[i % 3],
                        status="failed" if i % 2 else "completed")
                   for i in range(1, 7)]
        wrapped = print_stats.build_wrapped(records, 2026, events=EVENTS)
        self.assertEqual(wrapped["headline"]["pasta"], 3)
        self.assertEqual(wrapped["archetype"]["name"], "Spaghetti Magnet")
        self.assertIn("pasta", "\n".join(wrapped["script"]).lower())
        flagged = [_rec(i, watch_risk="slicer finding", status="failed",
                        filament_color=("Black", "Red", "Blue")[i % 3])
                   for i in range(1, 7)]
        wrapped2 = print_stats.build_wrapped(flagged, 2026)
        self.assertNotEqual(wrapped2["archetype"]["name"], "Spaghetti Magnet")
        self.assertIsNone(wrapped2["headline"]["pasta"])


GCODE_TWO_COLOR = ("; filament_type = PLA;PLA\n"
                   "; filament_colour = #FF0000;#00FF00\n"
                   "; filament used [g] = 10.5, 2.5\n"
                   "G1 X1\n")


def _join_fixtures(tmp):
    """(records, archive, receipts) for the (d)/(e) joins."""
    root = Path(tmp)
    archive = root / "archive"
    job_dir = archive / "job-one"
    job_dir.mkdir(parents=True)
    gcode = job_dir / "widget.gcode"
    gcode.write_text(GCODE_TWO_COLOR)
    job = {"print_number": 10, "title": "Widget", "url": "https://example.invalid/widget",
           "gcode": str(gcode),
           "filament_selection": {"slot": 2, "color": "Red", "material": "PLA",
                                  "spoolman_id": 42}}
    (job_dir / "JOB.json").write_text(json.dumps(job))
    job2_dir = archive / "job-two"
    job2_dir.mkdir(parents=True)
    (job2_dir / "JOB.json").write_text(json.dumps({"title": "Almost One"}))
    receipts = root / "receipts"
    receipts.mkdir()
    (receipts / "a.json").write_text(json.dumps({
        "intake_id": "a" * 64, "status": "STARTED", "started": True,
        "print_number": 10, "job_json": str(job_dir / "JOB.json"),
        "support_override": {"via": "print_anyway", "support_coverage": True}}))
    (receipts / "b.json").write_text(json.dumps({
        "intake_id": "b" * 64, "status": "SLICED_NOT_STARTED", "started": False,
        "job_json": str(job2_dir / "JOB.json")}))
    (receipts / "broken.json").write_text("{nope")
    records = [_rec(10, started_at="2026-03-01T12:00:00Z")]
    return records, archive, receipts


class JoinTests(unittest.TestCase):
    def test_receipt_and_job_join_on_print_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            records, archive, receipts = _join_fixtures(tmp)
            enrich = wrapped_joins.build_enrichments(
                records, archive_dir=archive, receipts_dir=receipts)
        per_print = enrich["per_print"][10]
        self.assertEqual(per_print["filament_selection"],
                         {"slot": 2, "color": "Red", "material": "PLA",
                          "spoolman_id": 42})
        self.assertEqual(per_print["job_title"], "Widget")
        self.assertEqual(enrich["support_waiver_print_numbers"], [10])
        self.assertEqual(enrich["almost_made"]["count"], 1)
        self.assertEqual(enrich["almost_made"]["titles"], ["Almost One"])
        self.assertEqual(enrich["skipped_receipts"], ["broken.json"])
        stats = print_stats.aggregate(records, year=2026, enrichments=enrich)
        self.assertEqual(stats["enrichments"]["support_waivers"], 1)
        self.assertEqual(stats["enrichments"]["almost_made"]["titles"], ["Almost One"])
        self.assertEqual(
            stats["enrichments"]["per_print"][10]["filament_selection"]["slot"], 2)

    def test_corrupt_receipts_change_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            records, archive, receipts = _join_fixtures(tmp)
            enrich = wrapped_joins.build_enrichments(
                records, archive_dir=archive, receipts_dir=receipts)
            plain = print_stats.aggregate(records, year=2026)
            joined = print_stats.aggregate(records, year=2026, enrichments=enrich)
        for key in ("total_prints", "total_filament_used_g", "total_hours", "completed"):
            self.assertEqual(joined[key], plain[key])

    def test_per_color_grams_from_archived_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            records, archive, receipts = _join_fixtures(tmp)
            enrich = wrapped_joins.build_enrichments(
                records, archive_dir=archive, receipts_dir=receipts)
            stats = print_stats.aggregate(records, year=2026, enrichments=enrich)
        self.assertEqual(stats["per_color_grams"], {"#FF0000": 10.5, "#00FF00": 2.5})
        self.assertEqual(stats["per_color_state"], "recorded")

    def test_per_color_grams_from_native_record_list(self):
        record = _rec(11, slicer_settings={
            "nozzle_diameter": 0.4,
            "filament_used_g_list": [4.0, 6.0],
            "filament_color_list": ["Black", "White"]})
        stats = print_stats.aggregate([record], year=2026)
        self.assertEqual(stats["per_color_grams"], {"Black": 4.0, "White": 6.0})

    def test_native_list_is_not_double_counted_from_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            records, archive, receipts = _join_fixtures(tmp)
            records[0]["slicer_settings"] = {
                "filament_used_g_list": [1.0], "filament_color_list": ["Black"]}
            enrich = wrapped_joins.build_enrichments(
                records, archive_dir=archive, receipts_dir=receipts)
            stats = print_stats.aggregate(records, year=2026, enrichments=enrich)
        self.assertEqual(stats["per_color_grams"], {"Black": 1.0})


class RecordStartNativeListTests(unittest.TestCase):
    def test_record_start_stashes_per_tool_list_in_slicer_settings(self):
        from unittest import mock as _mock
        from print_handy.core import model_job
        from print_handy.hermes import print_records
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gcode = root / "job.gcode"
            gcode.write_text(GCODE_TWO_COLOR)
            context_path = root / "print_handy_context.json"
            state = {"printer": "Klipper",
                     "spool": {"diameter_mm": 1.75, "density": 1.24,
                               "spoolman_id": 3, "material": "PLA", "name": "Red"}}
            with _mock.patch.object(print_records, "PRINTS_DIR", root / "Prints"), \
                 _mock.patch.object(model_job, "_job_for_gcode",
                                    return_value={"title": "Widget"}), \
                 _mock.patch.object(model_job.printer_state, "load", return_value=state), \
                 _mock.patch.object(model_job.printer_state, "summary",
                                    return_value="test printer"), \
                 _mock.patch.object(model_job.printers, "current",
                                    return_value=_mock.Mock(multi_slot=False,
                                                            native_or_none=None)), \
                 _mock.patch.object(model_job, "CONTEXT", context_path):
                record_path = model_job.record_start(str(gcode),
                                                     started_at="2026-11-02T00:00:00Z")
            record = json.loads(Path(record_path).read_text())
        settings = record["slicer_settings"]
        self.assertEqual(settings["filament_used_g_list"], [10.5, 2.5])
        self.assertEqual(settings["filament_color_list"], ["#FF0000", "#00FF00"])


class SpoolmanLifetimeTests(unittest.TestCase):
    SPOOLS = [
        {"id": 1, "initial_weight": 1000, "remaining_weight": 250, "archived": False,
         "filament": {"material": "PLA", "color": "Black", "vendor": {"name": "Sunlu"}}},
        {"id": 2, "initial_weight": 500, "remaining_weight": 500, "archived": True,
         "filament": {"material": "PETG", "color": "Red"}},
        {"id": 3, "remaining_weight": 10},  # no initial: cannot derive, named unknown
    ]

    def test_lifetime_block_is_all_time_and_per_spool(self):
        block = wrapped_joins.spoolman_lifetime(self.SPOOLS)
        self.assertEqual(block["state"], "ok")
        self.assertEqual(block["scope"], "all_time")
        by_id = {row["id"]: row for row in block["spools"]}
        self.assertEqual(by_id[1]["used_g"], 750.0)
        self.assertEqual(by_id[1]["label"], "Sunlu PLA Black")
        self.assertTrue(by_id[2]["archived"])
        self.assertIsNone(by_id[3]["used_g"])
        self.assertEqual(block["total_used_g"], 750.0)
        self.assertIn("all-time", block["note"].lower())

    def test_unavailable_spoolman_is_a_named_note_not_zeros(self):
        block = wrapped_joins.spoolman_lifetime(None)
        self.assertEqual(block["state"], "unavailable")
        self.assertEqual(block["spools"], [])
        self.assertIsNone(block["total_used_g"])
        self.assertIn("unavailable", block["note"])


class MaintenanceTests(unittest.TestCase):
    def test_year_block_from_jsonl_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            folder = vault / "Maintenance"
            folder.mkdir(parents=True)
            rows = [
                {"kind": "full", "steps": ["done", "problem", None, "done"],
                 "problems": ["nozzle clog"], "cold_pull": "clean",
                 "at": "2026-02-01T10:00:00Z"},
                {"kind": "quick", "steps": ["done", "done"], "problems": [],
                 "cold_pull": None, "at": "2026-03-01T10:00:00Z"},
                {"kind": "full", "steps": ["done"] * 4, "problems": [],
                 "cold_pull": "dirty", "at": "2025-03-01T10:00:00Z"},
            ]
            (folder / "maintenance.jsonl").write_text(
                "\n".join(json.dumps(r) for r in rows) + "\n{broken\n")
            block = wrapped_joins.maintenance_block(vault, year=2026)
        self.assertEqual(block["sessions"], 2)
        self.assertEqual(block["by_kind"], {"full": 1, "quick": 1})
        self.assertEqual(block["problems"], 2)  # one listed + one problem step
        self.assertEqual(block["cold_pulls"], 1)
        self.assertEqual(block["skipped_lines"], ["Maintenance/maintenance.jsonl line 4"])


class WorstFailureTests(unittest.TestCase):
    def test_sidecar_enriches_worst_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = Path(tmp) / "STOP.json"
            sidecar.write_text(json.dumps({
                "stop_reason": "Timer too close", "first_captured_at": "2026-03-01T13:00:00Z",
                "events": [{"trigger": "error", "printer_info": {"state_message": "MCU shutdown"}}]}))
            failed = _rec(5, status="failed", print_duration_seconds=7200,
                          stop_capture=str(sidecar), stop_reason="Timer too close")
            small = _rec(6, status="failed", print_duration_seconds=60)
            enrich = wrapped_joins.build_enrichments(
                [failed, small], archive_dir=Path(tmp) / "none",
                receipts_dir=Path(tmp) / "none")
            wrapped = print_stats.build_wrapped([failed, small], 2026, enrichments=enrich)
        worst = wrapped["highlights"]["worst_failure"]
        self.assertEqual(worst["print_number"], 5)
        self.assertEqual(worst["stop_detail"]["stop_reason"], "Timer too close")
        self.assertEqual(worst["stop_detail"]["trigger"], "error")
        self.assertEqual(worst["stop_detail"]["printer_state_message"], "MCU shutdown")

    def test_absent_sidecar_keeps_step1_shape(self):
        failed = _rec(5, status="failed", print_duration_seconds=7200)
        wrapped = print_stats.build_wrapped([failed], 2026)
        worst = wrapped["highlights"]["worst_failure"]
        self.assertEqual(worst["print_number"], 5)
        self.assertNotIn("stop_detail", worst)


class DerivedGramsTests(unittest.TestCase):
    def test_derived_grams_labeled_and_out_of_headline(self):
        # 10000 mm at 1.75 mm / 1.24 g/cm3 = 29.8 g (the _grams formula).
        record = _rec(1, filament_used_g=None, filament_used_mm=10000,
                      filament_material="PLA",
                      filament_diameter_mm=1.75, filament_density=1.24)
        record.pop("filament_used_g")
        stats = print_stats.aggregate([record], year=2026)
        self.assertEqual(stats["total_filament_used_g"], 0.0)
        self.assertAlmostEqual(stats["derived_filament_g"], 29.8, places=1)
        self.assertEqual(stats["derived_filament_records"], 1)
        self.assertIn("DERIVED", stats["derived_note"])

    def test_derived_fallback_is_documented_in_payload(self):
        record = _rec(1, filament_used_mm=10000, filament_material="PLA")
        record.pop("filament_used_g")
        stats = print_stats.aggregate([record], year=2026)
        self.assertGreater(stats["derived_filament_g"], 0)
        self.assertIn("1.75", stats["derived_note"])


class CaveatTests(unittest.TestCase):
    def test_totals_caveat_and_record_counts(self):
        stats = print_stats.aggregate([_rec(1), _rec(2, filament_used_g=None)], year=2026)
        self.assertIn("of prints with that data recorded", stats["totals_caveat"])
        self.assertEqual(stats["filament_records"], 1)
        self.assertEqual(stats["duration_records"], 2)


class FakeRunner:
    def __init__(self):
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append([str(p) for p in command])
        target = Path(command[-1])
        if target.suffix == ".mp4":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake")
        return SimpleNamespace(returncode=0)


class ClipJoinTests(unittest.TestCase):
    def test_parse_delivery_name(self):
        self.assertEqual(
            wrapped_video.parse_clip_print_number("20260301-1200-Widget-print-12.mp4"), 12)
        self.assertIsNone(wrapped_video.parse_clip_print_number("random-clip.mp4"))
        self.assertIsNone(wrapped_video.parse_clip_print_number("print-3.mp4.bak"))

    def test_detailed_finder_joins_across_locations_archive_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "archive"; archive.mkdir()
            cache = root / "cache"; (cache / "moonraker-announcements").mkdir(parents=True)
            named = archive / "20260301-1200-Widget-print-12.mp4"
            named.write_bytes(b"x")
            other = cache / "moonraker-announcements" / "20260401-1200-Gear-print-13.mp4"
            other.write_bytes(b"x")
            data = {"timelapse": {"archive_dir": str(archive)},
                    "paths": {"cache": str(cache)}}
            detailed = wrapped_video.default_clip_finder_detailed(data, 2026)
        self.assertEqual([d["print_number"] for d in detailed], [12, 13])
        self.assertEqual(Path(detailed[0]["path"]), named)

    def test_selection_reserves_failure_slots_and_labels_them(self):
        wrapped = {"year": 2026, "headline": {}, "script": [],
                   "highlights": {"failed_print_numbers": [9], "pasta_print_numbers": []}}
        clips = [{"path": f"/clips/ok-{i}.mp4", "print_number": i} for i in range(1, 8)]
        clips.append({"path": "/clips/bad-9.mp4", "print_number": 9})
        selected = wrapped_video.select_clips(clips, wrapped)
        numbers = [c["print_number"] for c in selected]
        self.assertLessEqual(len(selected), wrapped_video.MAX_CLIPS)
        self.assertIn(9, numbers)

    def test_failure_clip_gets_label_card_immediately_before_it(self):
        wrapped = {"year": 2026, "headline": {"total_prints": 2}, "script": [],
                   "highlights": {"failed_print_numbers": [9], "pasta_print_numbers": [9]}}
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "20260301-1200-Ok-print-1.mp4"; good.write_bytes(b"x")
            bad = Path(tmp) / "20260302-1200-Bad-print-9.mp4"; bad.write_bytes(b"x")
            result = wrapped_video.build_wrapped_video(
                2026, wrapped, Path(tmp) / "out", ffmpeg="ffmpeg", runner=runner,
                find_clips=lambda year: [{"path": str(good), "print_number": 1},
                                         {"path": str(bad), "print_number": 9}],
                ai_call=lambda lines: lines)
            self.assertTrue(result["ok"])
        # Segment build order: find the clip segment built from the bad
        # clip; the segment built immediately before it is its label card.
        clip_index = next(i for i, cmd in enumerate(runner.commands)
                          if str(bad) in " ".join(cmd) and "clip-" in cmd[-1])
        before = " ".join(runner.commands[clip_index - 1])
        self.assertIn("card-", runner.commands[clip_index - 1][-1])
        self.assertIn("turned into pasta", before)

    def test_no_failure_clips_behaves_as_before(self):
        wrapped = {"year": 2026, "headline": {"total_prints": 1}, "script": [],
                   "highlights": {"failed_print_numbers": [], "pasta_print_numbers": []}}
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "20260301-1200-Ok-print-1.mp4"; good.write_bytes(b"x")
            result = wrapped_video.build_wrapped_video(
                2026, wrapped, Path(tmp) / "out", ffmpeg="ffmpeg", runner=runner,
                find_clips=lambda year: [{"path": str(good), "print_number": 1}],
                ai_call=lambda lines: lines)
            self.assertTrue(result["ok"])
        flat = " ".join(" ".join(cmd) for cmd in runner.commands)
        self.assertNotIn("turned into pasta", flat)
        self.assertNotIn("This one failed", flat)


class PageRenderTests(unittest.TestCase):
    def test_data_page_shows_recorded_pasta_caveat_and_blocks(self):
        from print_handy.webui import data as data_module
        records = [_rec(1), _rec(2), _rec(3)]
        stats = print_stats.aggregate(records, year=2026, events=EVENTS)
        payload = {"ok": True, **stats,
                   "maintenance": {"state": "ok", "sessions": 2,
                                   "by_kind": {"full": 1, "quick": 1},
                                   "problems": 1, "cold_pulls": 1,
                                   "skipped_lines": []},
                   "spoolman_lifetime": wrapped_joins.spoolman_lifetime(
                       SpoolmanLifetimeTests.SPOOLS)}
        page = data_module.render_data_page(
            {"timelapse": {"wrapped_enabled": True}}, payload)
        self.assertIn("Pasta: 3 prints", page)
        self.assertIn("recording began", page)
        self.assertIn("of prints with that data recorded", page)
        self.assertIn("Spoolman lifetime (all-time)", page)
        self.assertIn("Maintenance", page)
        self.assertIn("Sessions: 2", page)
        self.assertNotIn("Pasta: not recorded yet", page)

    def test_wrapped_page_shows_recorded_pasta_and_worst(self):
        from print_handy.webui import data as data_module
        records = [_rec(1), _rec(2), _rec(3)]
        wrapped = print_stats.build_wrapped(records, 2026, events=EVENTS)
        page = data_module.render_wrapped_page(
            {"timelapse": {"wrapped_enabled": True}},
            {"ok": True, **wrapped, "ai_video_enabled": False})
        self.assertIn("Pasta: 3 prints turned into pasta", page)
        self.assertIn("Worst spaghetti", page)
        self.assertNotIn("Pasta: not recorded yet", page)

    def test_data_view_end_to_end_with_events_file(self):
        from print_handy.webui import data as data_module
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            prints = vault / "Prints"; prints.mkdir(parents=True)
            (prints / "print-0001.json").write_text(json.dumps(_rec(1)))
            folder = vault / "Watchdog"; folder.mkdir()
            (folder / "events.jsonl").write_text(
                "\n".join(json.dumps(e) for e in EVENTS[:2]) + "\n")
            data = {"timelapse": {"wrapped_enabled": True},
                    "paths": {"records_vault": str(vault), "archive": str(Path(tmp) / "no-archive")},
                    "services": {}}
            code, payload = data_module.data_view(data, year=2026)
        self.assertEqual(code, 200)
        self.assertEqual(payload["outcomes"]["pasta"], 1)
        self.assertEqual(payload["outcomes"]["pasta_state"], "recorded")
        self.assertEqual(payload["spoolman_lifetime"]["state"], "unavailable")




class DuplicatePrintNumberTests(unittest.TestCase):
    """Pre-#988 history: a reprint was recorded under the original's
    print number. Per-print joins must count and describe each PRINT."""

    def test_reprint_sharing_a_number_counts_its_own_per_color_grams(self):
        with tempfile.TemporaryDirectory() as tmp:
            records, archive, receipts = _join_fixtures(tmp)
            reprint = _rec(10, started_at="2026-04-01T12:00:00Z", record_id="bbbb2222")
            records = [dict(records[0], record_id="aaaa1111"), reprint]
            enrich = wrapped_joins.build_enrichments(
                records, archive_dir=archive, receipts_dir=receipts)
            stats = print_stats.aggregate(records, year=2026, enrichments=enrich)
        # Two prints of the two-color job: twice the single print's grams.
        self.assertEqual(stats["per_color_grams"], {"#FF0000": 21.0, "#00FF00": 5.0})

    def test_worst_failure_gets_its_own_stop_detail_not_the_other_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            long_stop = Path(tmp) / "STOP-long.json"
            long_stop.write_text(json.dumps({"stop_reason": "Timer too close"}))
            short_stop = Path(tmp) / "STOP-short.json"
            short_stop.write_text(json.dumps({"stop_reason": "Filament runout"}))
            original = _rec(5, status="failed", print_duration_seconds=7200,
                            started_at="2026-03-01T12:00:00Z", record_id="aaaa1111",
                            stop_capture=str(long_stop), stop_reason="Timer too close")
            reprint = _rec(5, status="failed", print_duration_seconds=60,
                           started_at="2026-03-02T12:00:00Z", record_id="bbbb2222",
                           stop_capture=str(short_stop), stop_reason="Filament runout")
            enrich = wrapped_joins.build_enrichments(
                [original, reprint], archive_dir=Path(tmp) / "none",
                receipts_dir=Path(tmp) / "none")
            wrapped = print_stats.build_wrapped([original, reprint], 2026, enrichments=enrich)
        worst = wrapped["highlights"]["worst_failure"]
        self.assertEqual(worst["seconds"], 7200)
        self.assertEqual(worst["stop_detail"]["stop_reason"], "Timer too close")


if __name__ == "__main__":
    unittest.main()
