"""#1446: the Wrapped page as a presentation.

The page is a scrolling story of full-screen sections, one headline
stat per screen, with the tools (year picker, video, share) together
in a finale section and the share card last. Same payload, same
words, same gate as before the rebuild; these tests pin the new
arrangement through the rendering entry point.
"""
import re
import unittest

from print_handy.core import print_stats
from print_handy.webui import data as data_module, shell

RECORDS = [
    {"print_number": 1, "status": "completed", "success": True, "title": "Bracket",
     "started_at": "2025-03-01T02:30:00Z", "completed_at": "2025-03-01T04:30:00Z",
     "printer": "AD5X", "gcode_file": "bracket.gcode",
     "filament_material": "PLA", "filament_color": "Black",
     "filament_used_g": 100.0, "print_duration_seconds": 7200},
    {"print_number": 2, "status": "completed", "success": True, "title": "Clip",
     "started_at": "2025-07-15T12:00:00Z", "completed_at": "2025-07-15T13:00:00Z",
     "printer": "AD5X", "gcode_file": "clip.gcode",
     "filament_material": "PLA", "filament_color": "Black",
     "filament_used_g": 50.0, "print_duration_seconds": 3600},
    {"print_number": 3, "status": "failed", "success": False, "title": "Vase",
     "started_at": "2025-07-04T18:00:00Z", "completed_at": "2025-07-04T18:30:00Z",
     "printer": "AD5X", "gcode_file": "vase.gcode",
     "filament_material": "PETG", "filament_color": "Red",
     "filament_used_g": 20.0, "print_duration_seconds": 1800},
    {"print_number": 4, "status": "cancelled", "success": False, "title": "Gear",
     "started_at": "2025-07-05T18:00:00Z", "completed_at": "2025-07-05T18:10:00Z",
     "printer": "AD5X", "gcode_file": "gear.gcode",
     "filament_used_g": 10.0, "print_duration_seconds": 600},
]
# 2025 totals for the fixture: 4 prints (2 completed, 1 failed,
# 1 cancelled), 3.67 hours, 0.18 kg, success rate 67 percent,
# longest and biggest print both "Bracket" (2 hours, 100 g),
# busiest month 2025-07 (3 prints), top material PLA, top color Black.

DATA = {"timelapse": {"wrapped_enabled": True, "wrapped_video": False}}


def _page(records=RECORDS, **extra):
    wrapped = print_stats.build_wrapped(records, 2025)
    payload = {"ok": True, **wrapped, "ai_video_enabled": False}
    payload.update(extra)
    return data_module.render_wrapped_page(DATA, payload, form_token="tok")


def _screens(page):
    """The inner markup of each full-screen section, in order."""
    return re.findall(
        r'<section class="[^"]*wrapped-screen[^"]*">(.*?)</section>',
        page, re.S)


def _screen_with(page, needle):
    return [screen for screen in _screens(page) if needle in screen]


class WrappedPresentationTests(unittest.TestCase):
    def test_each_headline_stat_gets_its_own_screen(self):
        page = _page()
        prints = _screen_with(page, '<p class="wrapped-big">4</p>')
        hours = _screen_with(page, '<p class="wrapped-big">3.67</p>')
        filament = _screen_with(page, '<p class="wrapped-big">0.18 kg</p>')
        success = _screen_with(page, '<p class="wrapped-big">67%</p>')
        self.assertEqual(len(prints), 1)
        self.assertEqual(len(hours), 1)
        self.assertEqual(len(filament), 1)
        self.assertEqual(len(success), 1)
        # Four distinct screens, not one grid: no screen holds two of them.
        for screen in _screens(page):
            bigs = screen.count('class="wrapped-big"')
            self.assertLessEqual(bigs, 1, screen[:200])

    def test_longest_and_biggest_print_screens(self):
        page = _page()
        longest = _screen_with(page, "Longest print")
        self.assertEqual(len(longest), 1)
        self.assertIn("2 hours", longest[0])
        self.assertIn("Bracket", longest[0])
        biggest = _screen_with(page, "Biggest print by filament")
        self.assertEqual(len(biggest), 1)
        self.assertIn("100 g", biggest[0])
        self.assertIn("Bracket", biggest[0])

    def test_top_material_and_busiest_month_are_big(self):
        page = _page()
        self.assertIn('<p class="wrapped-big wrapped-big-text">PLA</p>', page)
        self.assertIn('<p class="wrapped-big wrapped-big-text">2025-07</p>', page)
        self.assertIn("Top color: Black", page)

    def test_tools_live_in_a_finale_after_the_story(self):
        page = _page(ai_video_enabled=True, video_available=True)
        finale_at = page.index('class="wrapped-finale"')
        self.assertGreater(finale_at, page.index("Commentary"))
        finale = page[finale_at:]
        self.assertIn('<select name="year">', finale)
        self.assertIn("Make my Wrapped video", finale)
        self.assertIn("<video", finale)
        self.assertIn('action="/wrapped/share"', finale)
        # The share card still lands last.
        self.assertGreater(page.index("card share-card"), finale_at)

    def test_story_order_and_no_story_player(self):
        page = _page()
        markers = ["wrapped-hero", "By the numbers", "Colors and materials",
                   "Time patterns", "Repeats", "Your printing personality",
                   "Pasta, failures and stops", "card share-card"]
        positions = [page.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("story-player", page)
        self.assertIn('<div class="wrapped">', page)

    def test_presentation_css_is_scoped_and_snapping(self):
        css = shell.CSS
        self.assertIn("html:has(.wrapped)", css)
        self.assertIn("scroll-snap-type: y proximity", css)
        self.assertIn("scroll-snap-align: start", css)
        self.assertIn("100svh", css)
        self.assertIn("prefers-reduced-motion", css)
        # Charts cannot force horizontal scroll at phone widths.
        self.assertIn(".wrapped svg", css)
        self.assertIn("max-width: 100%", css)
        page = _page()
        self.assertIn("scroll-snap-type: y proximity", page)

    def test_empty_year_still_renders_the_story(self):
        page = _page(records=[])
        self.assertIn("0 prints. Scroll the year.", page)
        self.assertIn('<p class="wrapped-big">0</p>', page)
        self.assertNotIn("Longest print", page)
        self.assertNotIn("Biggest print by filament", page)
        self.assertIn('<select name="year">', page)

    def test_gate_is_unchanged_when_wrapped_is_off(self):
        code, payload = data_module.wrapped_view(
            {"timelapse": {"wrapped_enabled": False}})
        self.assertEqual(code, 403)
        self.assertEqual(payload["reason"], data_module.WRAPPED_OFF_REASON)


if __name__ == "__main__":
    unittest.main()
