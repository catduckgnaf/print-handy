"""#956: print stats, Wrapped, the Data/Wrapped Web UI surface, and the video builder.

Fixture records only, written as JSON files in a temporary Prints
folder; the video builder runs with a fake ffmpeg runner. No network,
no real ffmpeg, no printer.
"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from print_handy import settings
from print_handy.core import print_stats
from print_handy.webui import catalog, data as data_module, server, shell
from print_handy.webui import wrapped_video

TOKEN = "wrapped-" + "test-token"

RECORDS = [
    {"print_number": 1, "status": "completed", "success": True, "title": "Bracket",
     "started_at": "2025-03-01T02:30:00Z", "completed_at": "2025-03-01T04:30:00Z",
     "printer": "AD5X", "printer_profile": "Profile A", "gcode_file": "bracket.gcode",
     "filament_vendor": "Sunlu", "filament_material": "PLA", "filament_color": "Black",
     "spoolman_spool_id": 3, "filament_used_g": 100.0, "filament_used_mm": 30000,
     "estimated_filament_g": 90.0, "estimated_time": 7000,
     "print_duration_seconds": 7200, "slicer_settings": {"nozzle_diameter": 0.4},
     "watch_mode": "normal", "source_platform": "discord"},
    {"print_number": 2, "status": "completed", "success": True, "title": "Clip",
     "started_at": "2025-03-15T12:00:00Z", "completed_at": "2025-03-15T13:00:00Z",
     "printer": "AD5X", "printer_profile": "Profile A", "gcode_file": "clip.gcode",
     "filament_vendor": "Sunlu", "filament_material": "PLA", "filament_color": "Black",
     "spoolman_spool_id": 3, "filament_used_g": 50.0, "estimated_filament_g": 55.0,
     "print_duration_seconds": 3600, "slicer_settings": {"nozzle_diameter": 0.4}},
    {"print_number": 3, "status": "failed", "success": False, "title": "Vase",
     "started_at": "2025-07-04T18:00:00Z", "completed_at": "2025-07-04T18:30:00Z",
     "printer": "AD5X", "printer_profile": "Profile A", "gcode_file": "vase.gcode",
     "filament_vendor": "eSun", "filament_material": "PETG", "filament_color": "Red",
     "spoolman_spool_id": 7, "filament_used_g": 20.0,
     "print_duration_seconds": 1800, "slicer_settings": '{"nozzle_diameter": 0.6}',
     "watch_mode": "early_layer", "watch_risk": "slicer finding",
     "stop_reason": "spaghetti detected"},
    {"print_number": 4, "status": "cancelled", "success": False, "title": "Gear",
     "started_at": "2025-07-05T18:00:00Z", "completed_at": "2025-07-05T18:10:00Z",
     "printer": "AD5X", "gcode_file": "gear.gcode",
     "filament_used_g": 10.0, "print_duration_seconds": 600, "slicer_settings": None},
    {"print_number": 5, "status": "printing", "success": None, "title": "Running",
     "started_at": "2025-07-06T18:00:00Z",
     "printer": "AD5X", "gcode_file": "running.gcode",
     "filament_material": "PLA", "filament_color": "Black",
     "filament_used_g": 5.0, "print_duration_seconds": 999},
    {"print_number": 6, "status": "completed", "success": True, "title": "Old Big",
     "started_at": "2024-01-10T12:00:00Z", "completed_at": "2024-01-10T22:00:00Z",
     "printer": "AD5X", "gcode_file": "old.gcode",
     "filament_vendor": "Sunlu", "filament_material": "ABS", "filament_color": "White",
     "spoolman_spool_id": 9, "filament_used_g": 500.0,
     "print_duration_seconds": 36000, "slicer_settings": {"nozzle_diameter": 0.4}},
]

FAKE_SNAPSHOT = {
    "version": "0.test", "host": "testhost", "ok": True,
    "printer": {"model": "flashforge.ad5x", "name": "Flashforge AD5X",
                "vendor": "flashforge", "connection": "moonraker"},
    "bot": {"platform": "discord", "mode": "standalone", "ok": True,
            "reason": "connected", "heartbeat": {"connected": True}},
    "listener": {"required": True, "ok": True, "reason": "alive"},
    "spoolman": {"bundled": True, "url": "http://127.0.0.1:8000", "ok": True,
                 "detail": "HTTP 200"},
    "services": {"spoolman_url": "http://127.0.0.1:8000"},
    "doctor": [{"label": "settings", "status": "ok", "detail": "fine"}],
}


def write_vault(tmp):
    vault = Path(tmp) / "vault"
    prints = vault / "Prints"
    prints.mkdir(parents=True)
    for index, record in enumerate(RECORDS, start=1):
        (prints / f"print-{index:04d}.json").write_text(json.dumps(record))
    (prints / "print-9999.json").write_text("{half-written")
    return vault


def load_settings(tmp, extra="", environ=None):
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n"
                     f"  records_vault: {tmp}/vault\n{extra}")
    return settings.load(local=local, environ={} if environ is None else environ)


class AggregateTests(unittest.TestCase):
    def test_totals_rates_and_unknown_buckets(self):
        stats = print_stats.aggregate(RECORDS, year=2025)
        self.assertEqual(stats["total_prints"], 5)
        self.assertEqual(stats["completed"], 2)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["canceled"], 1)
        self.assertEqual(stats["in_progress"], 1)
        self.assertEqual(stats["success_rate"], 0.5)  # printing excluded
        self.assertEqual(stats["total_print_duration_seconds"], 14199.0)
        self.assertEqual(stats["total_hours"], 3.94)
        self.assertEqual(stats["longest_print"]["title"], "Bracket")
        self.assertEqual(stats["longest_print"]["seconds"], 7200.0)
        self.assertEqual(stats["total_filament_used_g"], 185.0)
        self.assertEqual(stats["total_filament_used_kg"], 0.185)
        self.assertEqual(stats["total_estimated_filament_g"], 145.0)
        by_color = stats["breakdowns"]["by_color"]
        self.assertEqual(by_color["Black"]["count"], 3)
        self.assertEqual(by_color["unknown"]["count"], 1)
        self.assertEqual(stats["breakdowns"]["by_material"]["unknown"]["count"], 1)
        self.assertEqual(stats["breakdowns"]["by_vendor"]["unknown"]["count"], 2)
        self.assertEqual(stats["breakdowns"]["by_spool"]["unknown"]["count"], 2)
        by_nozzle = stats["breakdowns"]["by_nozzle"]
        self.assertEqual(by_nozzle["0.4"]["count"], 2)
        self.assertEqual(by_nozzle["0.6"]["count"], 1)  # string slicer_settings parsed
        self.assertEqual(by_nozzle["unknown"]["count"], 2)
        self.assertEqual(stats["breakdowns"]["by_printer"]["AD5X"]["count"], 5)
        self.assertEqual(stats["failures"]["stop_reasons"], {"spaghetti detected": 1})
        # watch_risk is a non-empty string naming the risk; that truthy shape is the flag.
        # #981: the honest primary name is risk_flagged (a pre-print slice-risk
        # label, not a camera detection); watchdog_flagged stays as a compat alias.
        self.assertEqual(stats["risk_flagged"], 1)
        self.assertEqual(stats["watchdog_flagged"], 1)
        self.assertEqual(stats["failures"]["risk_flagged"], 1)
        self.assertEqual(stats["failures"]["watchdog_flagged"], 1)
        # #981: timelapse_known keyed on a record field no record ever carries
        # (structurally always 0); it is removed, not repurposed here.
        self.assertNotIn("timelapse_known", stats)
        self.assertEqual(stats["monthly"], {"2025-03": 2, "2025-07": 3})
        self.assertEqual(stats["top_by_duration"][0]["title"], "Bracket")
        self.assertEqual(stats["top_by_filament"][0]["filament_used_g"], 100.0)
        self.assertEqual(stats["years_available"], [2024, 2025])

    def test_loader_skips_and_names_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = write_vault(tmp)
            records, skipped = print_stats.load_records(vault)
        self.assertEqual(len(records), 6)
        self.assertEqual(skipped, ["print-9999.json"])

    def test_missing_dir_is_empty(self):
        records, skipped = print_stats.load_records("/no/such/vault")
        self.assertEqual((records, skipped), ([], []))
        self.assertIsNone(print_stats.aggregate([])["success_rate"])


class WrappedTests(unittest.TestCase):
    def test_highlights_and_data_driven_script(self):
        wrapped = print_stats.build_wrapped(RECORDS, 2025)
        self.assertEqual(wrapped["headline"]["total_prints"], 5)
        highlights = wrapped["highlights"]
        self.assertEqual(highlights["top_color"], "Black")
        self.assertEqual(highlights["top_material"], "PLA")
        self.assertEqual(highlights["top_printer"], "AD5X")
        self.assertEqual(highlights["busiest_month"], "2025-07")
        self.assertEqual(highlights["longest_print"]["title"], "Bracket")
        self.assertEqual(highlights["biggest_by_filament"]["title"], "Bracket")
        self.assertEqual(highlights["risk_flagged"], 1)
        self.assertEqual(highlights["watchdog_flagged"], 1)
        self.assertEqual(highlights["worst_failure_streak"], 2)
        script = "\n".join(wrapped["script"])
        self.assertIn("top color was Black", script)       # 3 of 5 prints: over 50%
        self.assertIn("spaghetti was not always dinner", script)  # failures > 0
        self.assertIn("midnight and 5 a.m.", script)       # print 1 started 02:30 UTC
        self.assertNotIn("worst run was", script)          # streak is 2, under the 3 gate
        self.assertNotIn("Zero failed prints", script)

    def test_script_uses_honest_risk_flag_copy(self):
        """#981: watch_risk is a pre-print slice-risk label, not a camera
        detection. The script must say so and must never claim the camera
        saw anything."""
        wrapped = print_stats.build_wrapped(RECORDS, 2025)
        script = "\n".join(wrapped["script"])
        self.assertIn("started with a risk flag", script)
        self.assertNotIn("camera watchdog flagged", script)
        self.assertNotIn("It saw things", script)
        self.assertNotIn("watchdog", script.lower())

    def test_zero_failure_year_has_no_failure_roast(self):
        wrapped = print_stats.build_wrapped(RECORDS, 2024)
        script = "\n".join(wrapped["script"])
        self.assertIn("Zero failed prints", script)
        self.assertNotIn("spaghetti was not always dinner", script)
        self.assertNotIn("midnight and 5 a.m.", script)  # noon start only


class TimezoneTests(unittest.TestCase):
    """#981: year/month/night bucketing in a caller-supplied local timezone.

    Without a timezone argument the historical UTC behavior is preserved.
    """

    NY = "America/New_York"

    @staticmethod
    def _record(started_at, number=1):
        return {"print_number": number, "status": "completed", "title": "T",
                "started_at": started_at, "print_duration_seconds": 60,
                "filament_used_g": 1.0}

    def test_new_years_eve_record_counts_in_local_year(self):
        # 2026-01-01T02:00:00Z is 2025-12-31 21:00 in America/New_York.
        records = [self._record("2026-01-01T02:00:00Z")]
        stats = print_stats.aggregate(records, year=2025, tz=self.NY)
        self.assertEqual(stats["total_prints"], 1)
        self.assertEqual(stats["monthly"], {"2025-12": 1})
        self.assertEqual(stats["years_available"], [2025])
        self.assertEqual(print_stats.aggregate(records, year=2026, tz=self.NY)["total_prints"], 0)
        # The same call without a timezone keeps the UTC answer.
        utc = print_stats.aggregate(records, year=2026)
        self.assertEqual(utc["total_prints"], 1)
        self.assertEqual(utc["monthly"], {"2026-01": 1})
        self.assertEqual(utc["years_available"], [2026])

    def test_tzinfo_object_is_accepted(self):
        from zoneinfo import ZoneInfo
        records = [self._record("2026-01-01T02:00:00Z")]
        stats = print_stats.aggregate(records, year=2025, tz=ZoneInfo(self.NY))
        self.assertEqual(stats["total_prints"], 1)

    def test_night_bucket_uses_local_hours(self):
        records = [
            self._record("2026-06-01T08:30:00Z", 1),  # 04:30 EDT: night
            self._record("2026-06-01T05:30:00Z", 2),  # 01:30 EDT: night
            self._record("2026-06-01T10:00:00Z", 3),  # 06:00 EDT: not night
        ]
        stats = print_stats.aggregate(records, year=2026, tz=self.NY)
        self.assertEqual(stats["night_prints"], 2)
        # In UTC none of these starts falls in 00:00-04:59.
        self.assertEqual(print_stats.aggregate(records, year=2026)["night_prints"], 0)

    def test_wrapped_buckets_and_script_follow_local_timezone(self):
        records = [self._record("2026-01-01T02:00:00Z"),
                   self._record("2026-06-01T08:30:00Z", 2)]
        wrapped = print_stats.build_wrapped(records, 2025, tz=self.NY)
        self.assertEqual(wrapped["headline"]["total_prints"], 1)
        self.assertEqual(wrapped["years_available"], [2025, 2026])
        wrapped26 = print_stats.build_wrapped(records, 2026, tz=self.NY)
        script = "\n".join(wrapped26["script"])
        self.assertIn("between midnight and 5 a.m. your time", script)
        self.assertNotIn("UTC", script)


def _rec(number, title, gcode, started="2025-03-01T12:00:00Z", status="completed",
         seconds=3600, grams=10.0):
    record = {"print_number": number, "status": status, "title": title,
              "started_at": started, "print_duration_seconds": seconds,
              "filament_used_g": grams}
    if gcode is not None:
        record["gcode_file"] = gcode
    return record


class RepeatsTests(unittest.TestCase):
    """#981 core data: repeat prints. Identity rule (conservative, no
    fuzzy matching): a print is the same print only when BOTH the
    normalized title AND the normalized G-code file name match exactly.
    Records missing either field are never grouped."""

    RECORDS = [
        _rec(1, "Bracket", "bracket.gcode"),
        _rec(2, "Bracket", "bracket.gcode"),
        _rec(3, " bracket ", "Bracket.GCODE"),          # same after normalization
        _rec(4, "Bracket", "bracket-v2.gcode"),          # same title, different file
        _rec(5, "Renamed Bracket", "bracket.gcode"),     # same file, different title
        _rec(6, "Orphan", None),                         # no file: never grouped
        _rec(7, "Orphan", None),
    ]

    def test_repeat_grouping_and_identity_edges(self):
        stats = print_stats.aggregate(self.RECORDS, year=2025)
        repeats = stats["repeats"]
        # Groups: bracket x3, v2 x1, renamed x1, orphan x1, orphan x1.
        self.assertEqual(repeats["distinct_prints"], 5)
        self.assertEqual(repeats["reprint_count"], 2)
        self.assertEqual(repeats["reprint_share"], round(2 / 7, 4))
        top = repeats["most_reprinted"][0]
        self.assertEqual(top["title"], "Bracket")
        self.assertEqual(top["count"], 3)
        self.assertEqual(top["total_hours"], 3.0)
        self.assertEqual(top["total_filament_g"], 30.0)
        # Same title/different file and same file/different title are
        # separate prints, so neither appears as a repeat.
        titles = [entry["title"] for entry in repeats["most_reprinted"]]
        self.assertEqual(titles, ["Bracket"])

    def test_wrapped_repeat_roast_is_data_gated_at_three(self):
        wrapped = print_stats.build_wrapped(self.RECORDS, 2025)
        self.assertEqual(wrapped["highlights"]["most_reprinted"]["count"], 3)
        self.assertIn("printed Bracket 3 times", "\n".join(wrapped["script"]))
        two = [_rec(1, "Clip", "clip.gcode"), _rec(2, "Clip", "clip.gcode")]
        wrapped_two = print_stats.build_wrapped(two, 2025)
        self.assertEqual(wrapped_two["highlights"]["most_reprinted"]["count"], 2)
        self.assertNotIn("printed Clip 2 times", "\n".join(wrapped_two["script"]))


class TimeOfPrintsTests(unittest.TestCase):
    def test_hour_and_weekday_buckets_use_local_timezone(self):
        # 2026-06-01 is a Monday; 08:30Z is 04:30 EDT in America/New_York.
        records = [_rec(1, "T", "t.gcode", started="2026-06-01T08:30:00Z")]
        stats = print_stats.aggregate(records, year=2026, tz="America/New_York")
        self.assertEqual(stats["by_hour"], {"04": 1})
        self.assertEqual(stats["by_weekday"], {"Monday": 1})
        utc = print_stats.aggregate(records, year=2026)
        self.assertEqual(utc["by_hour"], {"08": 1})
        self.assertEqual(utc["by_weekday"], {"Monday": 1})

    def test_wrapped_peak_line_is_data_gated(self):
        records = [_rec(1, "T", "t.gcode", started="2026-06-01T08:30:00Z")]
        wrapped = print_stats.build_wrapped(records, 2026, tz="America/New_York")
        self.assertEqual(wrapped["highlights"]["peak_hour"], 4)
        self.assertEqual(wrapped["highlights"]["peak_weekday"], "Monday")
        self.assertIn("busiest hour", "\n".join(wrapped["script"]))


class OutcomesTests(unittest.TestCase):
    """#981: pasta, failed and stopped are three separate numbers,
    never merged. Pasta events are not recorded yet in this step, so
    the payload says so instead of inventing a number."""

    def test_three_numbers_stay_separate(self):
        stats = print_stats.aggregate(RECORDS, year=2025)
        outcomes = stats["outcomes"]
        self.assertEqual(outcomes["failed"], 1)
        self.assertEqual(outcomes["stopped"], 1)  # the cancelled record
        self.assertIsNone(outcomes["pasta"])
        self.assertEqual(outcomes["pasta_state"], "not_recorded")
        self.assertIn("not recorded yet", outcomes["pasta_note"])
        # The records cannot say who stopped a print; the payload says so.
        self.assertIn("do not say", outcomes["stopped_note"])
        wrapped = print_stats.build_wrapped(RECORDS, 2025)
        self.assertEqual(wrapped["headline"]["failed"], 1)
        self.assertEqual(wrapped["headline"]["stopped"], 1)
        self.assertIsNone(wrapped["headline"]["pasta"])


def _arch_rec(number, *, title=None, started="2025-03-01T12:00:00Z",
              status="completed", seconds=3600, color="Black", risk=None):
    record = {"print_number": number, "status": status,
              "title": title or f"Print {number}",
              "gcode_file": f"print-{number}.gcode",
              "started_at": started, "print_duration_seconds": seconds,
              "filament_used_g": 10.0, "filament_color": color}
    if risk:
        record["watch_risk"] = risk
    return record


class ArchetypeTests(unittest.TestCase):
    """#981 design spec: exactly one deterministic printing-personality
    archetype per year, every rule data-gated, never shaming, with a
    neutral fallback so every year earns one."""

    NAMES = {"Night Owl", "One-Color Loyalist", "Marathoner", "Reprint Machine",
             "Spaghetti Magnet", "The Perfectionist", "The Regular"}

    def archetype(self, records, year=2025):
        wrapped = print_stats.build_wrapped(records, year)
        arch = wrapped["archetype"]
        self.assertEqual(arch, wrapped["highlights"]["archetype"])
        self.assertIn(arch["name"], self.NAMES)
        self.assertTrue(arch["fact"])
        return arch

    def test_night_owl(self):
        records = [_arch_rec(i, started="2025-03-01T02:00:00Z",
                             color=("Black", "Red", "Blue")[i % 3],
                             status="failed" if i % 3 == 0 else "completed")
                   for i in range(1, 5)]
        records += [_arch_rec(i, color=("Black", "Red", "Blue")[i % 3])
                    for i in range(5, 11)]
        self.assertEqual(self.archetype(records)["name"], "Night Owl")

    def test_one_color_loyalist(self):
        records = [_arch_rec(i, color="Black" if i <= 4 else "Red",
                             status="failed" if i % 2 else "completed")
                   for i in range(1, 7)]
        self.assertEqual(self.archetype(records)["name"], "One-Color Loyalist")

    def test_marathoner(self):
        records = [_arch_rec(i, color=("Black", "Red", "Blue")[i % 3])
                   for i in range(1, 5)]
        records.append(_arch_rec(5, seconds=36000, color="Red"))
        self.assertEqual(self.archetype(records)["name"], "Marathoner")

    def test_reprint_machine(self):
        records = [_arch_rec(i, title="Bracket", color=("Black", "Red", "Blue")[i % 3],
                             status="failed" if i == 1 else "completed")
                   for i in range(1, 6)]
        for record in records:
            record["gcode_file"] = "bracket.gcode"
        records += [_arch_rec(i, color=("Black", "Red", "Blue")[i % 3])
                    for i in range(6, 9)]
        self.assertEqual(self.archetype(records)["name"], "Reprint Machine")

    def test_perfectionist(self):
        records = [_arch_rec(i, color=("Black", "Red", "Blue")[i % 3],
                             status="failed" if i == 20 else "completed")
                   for i in range(1, 21)]
        self.assertEqual(self.archetype(records)["name"], "The Perfectionist")

    def test_fallback_the_regular(self):
        records = [_arch_rec(i, color="Black" if i <= 2 else "Red",
                             status="failed" if i % 2 else "completed")
                   for i in range(1, 5)]
        self.assertEqual(self.archetype(records)["name"], "The Regular")

    def test_precedence_reprint_machine_beats_loyalist(self):
        # Fires both Reprint Machine and One-Color Loyalist; the
        # documented precedence puts Reprint Machine first.
        records = [_arch_rec(i, title="Bracket") for i in range(1, 6)]
        for record in records:
            record["gcode_file"] = "bracket.gcode"
        records += [_arch_rec(i) for i in range(6, 9)]
        self.assertEqual(self.archetype(records)["name"], "Reprint Machine")

    def test_spaghetti_magnet_never_fires_from_risk_flags(self):
        # Every record risk-flagged and failed, but pasta events are
        # not recorded in this step, so Spaghetti Magnet must not fire.
        records = [_arch_rec(i, status="failed", risk="slicer finding",
                             color=("Black", "Red", "Blue")[i % 3])
                   for i in range(1, 7)]
        arch = self.archetype(records)
        self.assertNotEqual(arch["name"], "Spaghetti Magnet")

    def test_wrapped_page_is_scroll_first_in_section_order(self):
        """#981 comment 6702782262: the scrolling page is the primary
        Wrapped format. Sections run hero, volume, colors/materials,
        time patterns, repeats, personality, pasta/failure, and the
        finale share card last. No story player is the lead view."""
        wrapped = print_stats.build_wrapped(RECORDS, 2025)
        data = {"timelapse": {"wrapped_enabled": True, "wrapped_ai_video": False}}
        page = data_module.render_wrapped_page(
            data, {"ok": True, **wrapped, "ai_video_enabled": False})
        markers = ["wrapped-hero", "By the numbers", "Colors and materials",
                   "Time patterns", "Repeats", "Your printing personality",
                   "Pasta, failures and stops", "share-card"]
        positions = [page.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("story-player", page)

    def test_share_card_and_archetype_on_wrapped_page(self):
        records = [_arch_rec(i) for i in range(1, 5)]
        wrapped = print_stats.build_wrapped(records, 2025)
        data = {"timelapse": {"wrapped_enabled": True, "wrapped_ai_video": False}}
        page = data_module.render_wrapped_page(
            data, {"ok": True, **wrapped, "ai_video_enabled": False})
        self.assertIn("Your printing personality", page)
        self.assertIn(wrapped["archetype"]["name"], page)
        self.assertIn("share-card", page)


class ChartTests(unittest.TestCase):
    """Data tab graphs read from the same aggregate() numbers as the
    tables: representative values must appear in the chart markup."""

    def test_charts_carry_the_table_values(self):
        payload = print_stats.aggregate(RECORDS, year=2025)
        data = {"timelapse": {"wrapped_enabled": True}}
        page = data_module.render_data_page(data, {"ok": True, **payload})
        self.assertIn("<svg", page)
        self.assertIn("Prints per month", page)
        self.assertIn("2025-07", page)               # busiest fixture month, 3 prints
        self.assertIn("Top colors", page)
        self.assertIn("Black", page)                 # top color, 3 prints
        self.assertIn("Top materials", page)
        self.assertIn("Hour of day", page)
        self.assertIn("Pasta: not recorded yet", page)


class SettingsTests(unittest.TestCase):
    def test_defaults_validation_catalog_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp)
            self.assertIs(data["timelapse"]["wrapped_enabled"], False)
            self.assertIs(data["timelapse"]["wrapped_ai_video"], False)
            env_data = load_settings(
                tmp, environ={"PRINT_HANDY__TIMELAPSE__WRAPPED_ENABLED": "true",
                              "PRINT_TIMELAPSE_WRAPPED_AI_VIDEO": "true"})
            self.assertIs(env_data["timelapse"]["wrapped_enabled"], True)
            self.assertIs(env_data["timelapse"]["wrapped_ai_video"], True)
            bad = Path(tmp) / "bad.yaml"
            bad.write_text("timelapse:\n  wrapped_enabled: sometimes\n")
            with self.assertRaises(settings.SettingsError):
                settings.load(local=bad, environ={})
        for field_id in ("timelapse.wrapped_enabled", "timelapse.wrapped_ai_video"):
            field = catalog.field_by_id(field_id)
            self.assertIsNotNone(field)
            self.assertEqual(field.kind, "toggle")
            self.assertEqual(field.group, "timelapse")


class FakeRunner:
    def __init__(self):
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append([str(part) for part in command])
        target = Path(command[-1])
        if target.suffix == ".mp4":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake-video")
        return SimpleNamespace(returncode=0)


class VideoTests(unittest.TestCase):
    def test_fake_ffmpeg_build_names_missing_pieces(self):
        wrapped = print_stats.build_wrapped(RECORDS, 2025)
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            result = wrapped_video.build_wrapped_video(
                2025, wrapped, Path(tmp) / "wrapped", ffmpeg="ffmpeg",
                runner=runner, find_clips=lambda year: [])
            self.assertTrue(result["ok"])
            self.assertTrue(Path(result["path"]).is_file())
            script = json.loads(Path(result["script_path"]).read_text())
            self.assertEqual(script["year"], 2025)
            self.assertTrue(script["script"])
            self.assertIn("timelapses: none found", result["notes"])
            flat = " ".join(" ".join(cmd) for cmd in runner.commands)
            self.assertIn("drawtext", flat)
            self.assertIn("concat", flat)

    def test_segments_are_normalized_to_one_frame_rate_and_timebase(self):
        """#981 pacing: card and clip segments must share the card frame
        rate and one video timebase, or the stream-copy concat inflates
        clip segments when their source frame rates differ."""
        wrapped = print_stats.build_wrapped(RECORDS, 2025)
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"fake")
            result = wrapped_video.build_wrapped_video(
                2025, wrapped, Path(tmp) / "wrapped", ffmpeg="ffmpeg",
                runner=runner, find_clips=lambda year: [clip])
            self.assertTrue(result["ok"])
        segment_commands = [cmd for cmd in runner.commands
                            if ("card-" in cmd[-1] or "clip-" in cmd[-1])]
        self.assertTrue(segment_commands)
        for command in segment_commands:
            flat = " ".join(command)
            self.assertIn("fps=", flat)
            self.assertIn("-video_track_timescale", flat)

    def test_clip_notes_say_clips_from_this_year_never_highlights(self):
        """#981: clips are 'clips from this year', never a best-hits reel."""
        wrapped = print_stats.build_wrapped(RECORDS, 2025)
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"fake")
            result = wrapped_video.build_wrapped_video(
                2025, wrapped, Path(tmp) / "wrapped", ffmpeg="ffmpeg",
                runner=FakeRunner(), find_clips=lambda year: [clip])
            self.assertTrue(result["ok"])
        notes = " ".join(result["notes"]).lower()
        self.assertIn("clips from this year", notes)
        self.assertNotIn("highlight", notes)
        self.assertNotIn("best", notes)

    def test_cards_are_data_led_repeats_outcomes_colors_materials(self):
        """#981: the card sequence carries the core data cards (repeats,
        failed/stopped, top color/material), not only hours/filament."""
        import re

        wrapped = {"year": 2025,
                   "headline": {"total_prints": 7, "failed": 2, "stopped": 1},
                   "highlights": {"top_color": "Black", "top_material": "PLA",
                                  "most_reprinted": {"title": "Bracket", "count": 4}},
                   "script": []}
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            result = wrapped_video.build_wrapped_video(
                2025, wrapped, Path(tmp) / "wrapped", ffmpeg="ffmpeg",
                runner=runner, find_clips=lambda year: [])
            self.assertTrue(result["ok"])
        texts = []
        for command in runner.commands:
            for part in command:
                texts.extend(re.findall(r"drawtext=text='((?:[^'\\]|\\.)*)'", part))
        joined = " ".join(texts)
        self.assertIn("Bracket printed 4 times", joined)
        self.assertIn("2 failed prints", joined)
        self.assertIn("1 stopped print", joined)
        self.assertIn("Top color Black", joined)
        self.assertIn("Top material PLA", joined)

    def test_the_commentary_is_the_local_script_and_no_ai_route_is_called(self):
        """Wrapped uses no AI model: even with an AI route configured, the
        video keeps the local script and never calls the route."""
        from unittest import mock
        from print_handy import ai_routes
        wrapped = print_stats.build_wrapped(RECORDS, 2025)
        data = {"ai": {"chat": {"chat_url": "https://ai.example/v1", "model": "m"}}}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ai_routes, "call", side_effect=AssertionError("no AI call")):
            result = wrapped_video.build_wrapped_video(
                2025, wrapped, Path(tmp) / "wrapped", ffmpeg="ffmpeg",
                runner=FakeRunner(), find_clips=lambda year: [], data=data)
            self.assertTrue(result["ok"])
            script = json.loads(Path(result["script_path"]).read_text())
            self.assertEqual(script["script"], wrapped["script"])
            self.assertFalse(any("ai_commentary" in n for n in result["notes"]))
        self.assertFalse(hasattr(wrapped_video, "_ai_enrich"))

    def test_no_ffmpeg_is_a_named_refusal_with_sidecar(self):
        wrapped = print_stats.build_wrapped(RECORDS, 2025)
        with tempfile.TemporaryDirectory() as tmp:
            result = wrapped_video.build_wrapped_video(
                2025, wrapped, Path(tmp) / "wrapped",
                ffmpeg="wrapped-no-such-ffmpeg", runner=FakeRunner(),
                find_clips=lambda year: [], data=None)
            self.assertFalse(result["ok"])
            self.assertIn("ffmpeg", result["reason"])
            self.assertTrue(Path(result["script_path"]).is_file())
            self.assertTrue(any("ffmpeg: unavailable" in n for n in result["notes"]))

    def test_drawtext_escaping(self):
        self.assertEqual(wrapped_video.escape_drawtext("a:b'c%"), "a\\:b\\'c\\%")

    def test_card_text_is_wrapped_to_fit_the_frame(self):
        """Long commentary lines wrap: no drawtext text may be wider than
        the 1280px frame at fontsize 40, and no words are lost."""
        import re

        long_line = ("You started 123 prints in 2025 and the longest single "
                     "print ran for nine hours and forty-two minutes "
                     "without pausing")
        wrapped = {"year": 2025, "headline": {}, "highlights": {},
                   "script": [long_line]}
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            result = wrapped_video.build_wrapped_video(
                2025, wrapped, Path(tmp) / "wrapped", ffmpeg="ffmpeg",
                runner=runner, find_clips=lambda year: [],
                data={})
            self.assertTrue(result["ok"])
        texts = []
        for command in runner.commands:
            for part in command:
                texts.extend(re.findall(r"drawtext=text='((?:[^'\\]|\\.)*)'", part))
        self.assertTrue(texts)
        self.assertTrue(all(len(text) <= 48 for text in texts), texts)
        joined = " ".join(texts).replace("\\", "")
        for word in ("123 prints", "nine hours", "without pausing"):
            self.assertIn(word, joined)


class RouteTests(unittest.TestCase):
    def start(self, tmp, extra="", environ=None):
        environ = {"WEBUI_ACCESS_TOKEN": TOKEN} if environ is None else environ
        data = load_settings(tmp, "webui:\n  enabled: true\n  port: 8765\n" + extra,
                             environ=environ)
        httpd = server.make_server(data, environ=environ, port=0,
                                   snapshot_func=lambda: FAKE_SNAPSHOT)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        return httpd

    def request(self, port, method, path, body=b"", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request(method, path, body=body,
                         headers=headers or {server.TOKEN_HEADER: TOKEN})
            response = conn.getresponse()
            return response.status, response.read().decode()
        finally:
            conn.close()

    def test_disabled_refusals_and_no_nav(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_vault(tmp)
            httpd = self.start(tmp)
            port = httpd.server_address[1]
            for path in ("/data", "/wrapped", "/api/data", "/api/wrapped"):
                code, raw = self.request(port, "GET", path)
                self.assertEqual(code, 403, path)
                self.assertIn("wrapped_enabled", raw)
            code, raw = self.request(port, "GET", "/")
            self.assertEqual(code, 200)
            self.assertNotIn('href="/data"', raw)
            self.assertNotIn('href="/wrapped"', raw)
        self.assertNotIn("/data", [target for target, _label in shell.NAV])
        code, payload = data_module.data_view({"timelapse": {}}, records_vault="/nope")
        self.assertEqual(code, 403)

    def test_enabled_pages_api_and_nav(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_vault(tmp)
            httpd = self.start(tmp, "timelapse:\n  wrapped_enabled: true\n"
                                    "  wrapped_ai_video: true\n")
            port = httpd.server_address[1]
            code, raw = self.request(port, "GET", "/api/data?year=2025")
            self.assertEqual(code, 200)
            payload = json.loads(raw)
            self.assertEqual(payload["total_prints"], 5)
            self.assertEqual(payload["skipped_records"], ["print-9999.json"])
            code, raw = self.request(port, "GET", "/data")
            self.assertEqual(code, 200)
            self.assertIn("Black", raw)
            # #981: the Data page uses the honest pre-print label.
            self.assertIn("Risk-flagged prints", raw)
            self.assertNotIn("Watchdog-flagged", raw)
            self.assertIn('href="/data"', raw)
            code, raw = self.request(port, "GET", "/api/wrapped?year=2025")
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(raw)["script"])
            code, raw = self.request(port, "GET", "/wrapped?year=2025")
            self.assertEqual(code, 200)
            self.assertIn("Make my Wrapped video", raw)
            self.assertIn("Risk-flagged prints", raw)
            self.assertNotIn("Watchdog-flagged", raw)
            self.assertNotIn("It saw things", raw)
            code, raw = self.request(port, "GET", "/")
            self.assertEqual(code, 200)
            self.assertIn('href="/wrapped"', raw)

    def test_wrapped_page_names_off_video_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_vault(tmp)
            httpd = self.start(tmp, "timelapse:\n  wrapped_enabled: true\n")
            port = httpd.server_address[1]
            code, raw = self.request(port, "GET", "/wrapped?year=2025")
            self.assertEqual(code, 200)
            self.assertIn("wrapped_ai_video", raw)
            self.assertNotIn("Make my Wrapped video", raw)

    def test_wrapped_page_says_the_commentary_stays_local(self):
        """Wrapped uses no AI model: the video form says the commentary is
        the local lines and nothing leaves this machine."""
        with tempfile.TemporaryDirectory() as tmp:
            write_vault(tmp)
            httpd = self.start(tmp, "timelapse:\n  wrapped_enabled: true\n"
                                    "  wrapped_ai_video: true\n")
            port = httpd.server_address[1]
            code, raw = self.request(port, "GET", "/wrapped?year=2025")
            self.assertEqual(code, 200)
            self.assertNotIn("configured AI route", raw)
            self.assertIn("never rewritten by an AI model", raw)

    def test_video_post_requires_form_token_and_ai_toggle(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_vault(tmp)
            httpd = self.start(tmp, "timelapse:\n  wrapped_enabled: true\n")
            port = httpd.server_address[1]
            body = "_csrf=" + httpd.form_token + "&year=2025"
            code, raw = self.request(
                port, "POST", "/wrapped/video", body=body.encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(code, 403)
            self.assertIn("wrapped_ai_video", raw)


if __name__ == "__main__":
    unittest.main()
