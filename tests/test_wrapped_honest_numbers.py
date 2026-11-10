"""Wrapped and Data say only what the numbers support."""
import unittest

from print_handy.core import print_stats
from print_handy.webui import data as data_module
from print_handy.webui import wrapped_video


def rec(number, status="completed", started="2026-03-01T10:00:00Z", **extra):
    return {"print_number": number, "status": status, "title": f"T{number}",
            "started_at": started, "print_duration_seconds": 3600,
            "filament_used_g": 10.0, "gcode_file": f"f{number}.gcode", **extra}


class SuccessRateTests(unittest.TestCase):
    def test_a_stopped_print_is_not_a_failure(self):
        records = [rec(1), rec(2), rec(3, "canceled"), rec(4, "canceled"), rec(5, "failed")]
        stats = print_stats.aggregate(records, year=2026)
        self.assertEqual(stats["decided"], 3)
        self.assertEqual(stats["success_rate"], round(2 / 3, 4))
        self.assertEqual(stats["failure_rate"], round(1 / 3, 4))
        self.assertEqual(stats["outcomes"]["stopped"], 2)
        self.assertIn("stopped", stats["success_rate_note"])

    def test_only_stopped_prints_means_no_rate_not_zero(self):
        stats = print_stats.aggregate([rec(1, "canceled")], year=2026)
        self.assertIsNone(stats["success_rate"])

    def test_the_video_slide_says_what_the_rate_is_out_of(self):
        wrapped = print_stats.build_wrapped([rec(1), rec(2, "failed")], 2026)
        text = " ".join(s["line"] for s in wrapped_video.build_slides(wrapped))
        self.assertIn("finished or failed", text)


class FilamentCopyTests(unittest.TestCase):
    def test_filament_is_not_called_finished_objects_when_prints_failed(self):
        records = [rec(1, filament_used_g=100.0), rec(2, "failed", filament_used_g=50.0),
                   rec(3, "canceled", filament_used_g=50.0)]
        wrapped = print_stats.build_wrapped(records, 2026)
        script = " ".join(wrapped["script"])
        self.assertNotIn("into objects", script)
        self.assertIn("200 g of filament through the printer", script)
        self.assertIn("100 g of it went into prints that finished", script)
        self.assertEqual(wrapped["headline"]["completed_filament_used_g"], 100.0)
        slides = " ".join(s["line"] for s in wrapped_video.build_slides(wrapped))
        self.assertNotIn("finished objects", slides)
        self.assertIn("failed prints included", slides)

    def test_all_finished_prints_need_no_split(self):
        script = " ".join(print_stats.build_wrapped([rec(1)], 2026)["script"])
        self.assertNotIn("went into prints that finished", script)


class ReprintShareTests(unittest.TestCase):
    def test_reprint_machine_share_is_out_of_all_prints_and_never_over_100(self):
        # 6 reprints of one thing, all still printing: finished is 0.
        records = [rec(n, "printing", title="Same", gcode_file="same.gcode")
                   for n in range(1, 8)]
        stats = print_stats.aggregate(records, year=2026)
        self.assertEqual(stats["finished"], 0)
        archetype = print_stats.build_wrapped(records, 2026)["archetype"]
        self.assertEqual(archetype["name"], "Reprint Machine")
        self.assertEqual(archetype["fact"], "6 of 7 prints were reprints.")
        self.assertLessEqual(stats["repeats"]["reprint_share"], 1)


class PrinterCopyTests(unittest.TestCase):
    def test_the_busiest_printer_line_says_it_ranks_by_prints(self):
        records = [rec(1, printer="Short Jobs"), rec(2, printer="Short Jobs"),
                   rec(3, printer="Long Job", print_duration_seconds=99999)]
        script = " ".join(print_stats.build_wrapped(records, 2026)["script"])
        self.assertNotIn("hardest-working", script)
        self.assertIn("busiest printer, by number of prints, was Short Jobs", script)


class SingularTests(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(print_stats.plural(1, "print"), "1 print")
        self.assertEqual(print_stats.plural(2, "print"), "2 prints")
        self.assertEqual(print_stats.hours_text(1), "1 hour")
        self.assertEqual(print_stats.hours_text(1.04), "1 hour")
        self.assertEqual(print_stats.hours_text(9.14), "9.1 hours")
        self.assertEqual(print_stats.hours_text(0), "0 hours")

    def test_one_print_one_hour_in_script_slides_and_page(self):
        records = [rec(1, "failed", print_duration_seconds=3600)]
        wrapped = print_stats.build_wrapped(records, 2026)
        script = " ".join(wrapped["script"])
        self.assertIn("you made 1 print.", script)
        self.assertIn("spent 1 hour printing", script)
        self.assertNotIn("1 hours", script)
        slides = " ".join(s["hero"] + " " + s["line"] for s in wrapped_video.build_slides(wrapped))
        self.assertIn("1 print", slides)
        self.assertIn("1 hour printing", slides)
        self.assertNotIn("1 hours", slides)
        self.assertNotIn("1 prints", slides)
        page = data_module.render_wrapped_page(
            {}, {"ok": True, **wrapped, "ai_video_enabled": False}, form_token="t")
        self.assertIn("1 print. Scroll the year.", page)
        self.assertNotIn("1 prints", page)


class DatesAndStreakTests(unittest.TestCase):
    def test_failure_streak_orders_by_time_not_by_text(self):
        # "+02:00" text sorts after "Z" text, but 09:00+02:00 is 07:00Z, before 08:00Z.
        records = [
            rec(1, "failed", "2026-03-01T08:00:00Z"),
            rec(2, "completed", "2026-03-01T09:00:00+02:00"),
            rec(3, "failed", "2026-03-01T10:00:00Z"),
        ]
        # In time order: #2 (07:00Z, completed) resets, then #1 and #3 fail: a run of 2.
        self.assertEqual(print_stats._failure_streak(records), 2)
        # The raw-text order would put #2 last and report only 1.

    def test_undated_records_are_counted_and_named(self):
        records = [rec(1), rec(2, started=None), rec(3, started="not a date")]
        stats = print_stats.aggregate(records, year=2026)
        self.assertEqual(stats["total_prints"], 1)
        self.assertEqual(stats["undated_prints"], 2)
        wrapped = print_stats.build_wrapped(records, 2026)
        self.assertEqual(wrapped["headline"]["undated_prints"], 2)
        page = data_module.render_wrapped_page(
            {}, {"ok": True, **wrapped, "ai_video_enabled": False}, form_token="t")
        self.assertIn("2 prints without a date were left out of this year.", page)
        one = print_stats.build_wrapped([rec(1), rec(2, started=None)], 2026)
        page = data_module.render_wrapped_page(
            {}, {"ok": True, **one, "ai_video_enabled": False}, form_token="t")
        self.assertIn("1 print without a date was left out of this year.", page)

    def test_the_data_page_names_undated_prints_for_a_year_view(self):
        records = [rec(1), rec(2, started=None)]
        stats = print_stats.aggregate(records, year=2026)
        page = data_module.render_data_page({}, {**stats, "skipped_records": []})
        self.assertIn("1 print without a date was left out of this year.", page)
        everything = print_stats.aggregate(records)
        self.assertEqual(everything["total_prints"], 2)
        page = data_module.render_data_page({}, {**everything, "skipped_records": []})
        self.assertNotIn("without a date", page)


if __name__ == "__main__":
    unittest.main()
