"""#1282: the completion roast's {duration} is how long the print ran, not the extrusion-only time.

The 2.2.9 report: an AD5X (ZMod) job was printing from 14:39:07 to 14:47:21 (8m 14s, slicer
estimate 4m 37s) and the roast follow-up said "<name> concluded in 5m; the completion
notification arrived faster than expected." The 5m was Klipper's print_stats.print_duration,
which leaves out everything before the first extrusion; the wording claimed a comparison
with an expectation the roast never had. Offline: no printer, no Discord.
"""
import re

from print_handy.core import roast
from print_handy.hermes import moonraker_discord_announcements as announcer

# print_stats at completion on the reported job: open 8m 14s, extruding for about 5 minutes.
ISSUE_STATS = {"state": "complete", "filename": "3mf_print/Tiny_plate_1.gcode",
               "total_duration": 494.2, "print_duration": 301.7}
PLATE = {"seconds": 277, "material": "PLA", "color": "Black", "filament_g": 1.4}


def test_issue_1282_the_roast_duration_is_the_printing_span():
    facts = announcer.roast_facts({}, PLATE, ISSUE_STATS)
    assert facts["duration_s"] == 494.2
    assert roast.human_duration(facts["duration_s"]) == "8m"  # 2.2.9 said "5m"


def test_print_duration_then_the_estimate_are_the_fallbacks():
    assert announcer.roast_facts({}, PLATE, {"print_duration": 301.7})["duration_s"] == 301.7
    assert announcer.roast_facts({}, PLATE, {"total_duration": 0, "print_duration": None})["duration_s"] == 277
    assert "duration_s" not in announcer.roast_facts({}, {}, {})


def test_elapsed_print_seconds_ignores_unusable_values():
    assert announcer.elapsed_print_seconds({"total_duration": True, "print_duration": 12}) == 12.0
    assert announcer.elapsed_print_seconds({"total_duration": "494"}) is None
    assert announcer.elapsed_print_seconds(None) is None


def test_issue_1282_no_short_print_line_claims_it_beat_an_expectation():
    """The roast knows the duration, not how it compared with the estimate, so no line may
    say a print (or its notification) came faster or slower than expected."""
    claim = re.compile(r"\bthan expected\b", re.I)
    corpus = roast.load_corpus()
    offenders = [text for bank, rows in corpus["banks"].items() for _id, text in rows if claim.search(text)]
    assert offenders == []
    ctx = roast.build_context("Tiny", duration_s=494.2)
    for _id, text in corpus["banks"]["short_print"]:
        line = roast.render(text, ctx)
        assert line is None or "notification" not in line, line
