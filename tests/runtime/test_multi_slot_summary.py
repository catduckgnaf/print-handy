"""A print that uses several slots names every one of them in its posts.

A merged two-color plate (letters in slot 2, everything else in slot 3, tools
0 -> slot 2 and 1 -> slot 3) announced only "Slot 3: black PLA+": the summary
kept just the first part's saved choice. The start post, the listener's own
start and finish posts and the slice summary all read the same plate row, so
they all list both slots, each with its share of the grams when the slicer
wrote the per-tool usage line. One-slot prints keep their single line.
"""
import hashlib
import json
from pathlib import Path

from print_handy.runtime import discord_print_summary as summary
from .test_print_messages import _drive_listener, _fix_finish_text_on_windows  # noqa: F401  (fixture)


def _archive(tmp_path, monkeypatch, *, per_tool=True, slots=(3, 2, 3)):
    folder = tmp_path / "archive" / "plaque--abc"
    out = folder / "sliced" / "orca" / "run1"
    out.mkdir(parents=True)
    usage = "; filament used [g] = 6.80, 28.15\n" if per_tool else ""
    gcode = out / "plate_1.gcode"
    gcode.write_text("; estimated printing time (normal mode) = 1h 8m 20s\n"
                     "; total filament used [g] = 34.95\n" + usage + "G1 X1\n")
    names = ("01_PLAQUE_BLACK", "02_LETTERS_AND_TRIM_BRONZE", "03_FOOT_LEFT_BLACK")
    looks = {2: ("bronze", "PLA+"), 3: ("black", "PLA+")}
    data = {"title": "giveaway one plate multicolor v2", "gcode": str(gcode),
            "tool_mapping": {"0": 2, "1": 3}, "plate_merge": {"mode": "merge", "slots": sorted(set(slots))},
            "color_manifest": {"parts": [{"key": f"build:{n}", "name": name} for n, name in enumerate(names)]},
            # Saved in part order, so the first choice is a slot 3 part.
            "color_decisions": {"mode": "per_part", "schema": "part_slots_v1", "choices": {
                f"part:build:{n}": {"slot": slot, "color": looks[slot][0], "material": looks[slot][1]}
                for n, slot in enumerate(slots)}}}
    (folder / "JOB.json").write_text(json.dumps(data))
    monkeypatch.setattr(summary, "ARCHIVE", tmp_path / "archive")
    monkeypatch.setattr(summary, "CONTEXT_PATH", tmp_path / "no-context.json")
    digest = hashlib.sha256(Path(gcode).read_bytes()).hexdigest()
    return f"plate_1_{digest}.gcode"


def _slot_lines(text):
    return [line for line in text.split("\n") if line.startswith("Slot ")]


def test_start_post_lists_every_slot_with_its_grams(tmp_path, monkeypatch):
    name = _archive(tmp_path, monkeypatch)
    job, plate = summary.find_job(name)
    text = summary.format_started(job, plate)
    assert _slot_lines(text) == ["Slot 2: bronze PLA+ (~6.8 g)", "Slot 3: black PLA+ (~28.15 g)"]
    assert "Filament: 34.95 g" in text


def test_slots_are_listed_without_grams_when_the_slicer_gave_no_per_tool_usage(tmp_path, monkeypatch):
    name = _archive(tmp_path, monkeypatch, per_tool=False)
    job, plate = summary.find_job(name)
    assert _slot_lines(summary.format_started(job, plate)) == ["Slot 2: bronze PLA+", "Slot 3: black PLA+"]


def test_listener_start_and_finish_posts_list_every_slot(tmp_path, monkeypatch):
    name = _archive(tmp_path, monkeypatch)
    posted = _drive_listener(monkeypatch, name, ["standby", "printing"])
    assert len(posted) == 1
    assert _slot_lines(posted[0]) == ["Slot 2: bronze PLA+ (~6.8 g)", "Slot 3: black PLA+ (~28.15 g)"]
    done = summary.format_event("complete", name, emit=False)
    assert _slot_lines(done) == ["Slot 2: bronze PLA+ (~6.8 g)", "Slot 3: black PLA+ (~28.15 g)"]


def test_slice_summary_names_every_slot(tmp_path, monkeypatch):
    _archive(tmp_path, monkeypatch)
    job = json.loads(next((tmp_path / "archive").glob("*/JOB.json")).read_text())
    line = next(l for l in summary.format_slice_summary(job).split("\n") if l.startswith("Plate"))
    assert "bronze PLA+, slot 2; black PLA+, slot 3" in line


def test_one_slot_print_keeps_its_single_line(tmp_path, monkeypatch):
    name = _archive(tmp_path, monkeypatch, slots=(3, 3, 3))
    job, plate = summary.find_job(name)
    assert "filaments" not in plate
    assert _slot_lines(summary.format_started(job, plate)) == ["Slot 3: black PLA+"]
