"""Replay corpus beyond print-start: pause / cancel / complete / error.

Provenance — read this before trusting the fixtures: the recordings in
``tests/replay/recordings/`` are **synthetic-in-recorder-format**. No
scrubbed real recording of a pause, cancel, completion or error flow
exists in this repository (the recorded AD5X fixtures under
``tests/fixtures/printers/ad5x_zmod/`` cover idle/printing snapshots and
the start path only). The synthetic sequences were generated in the
start recorder's exported-fixture format (``{"recorded": ..., "frames":
[{"t_s", "result"}]}``, the shape ``start_recorder.export_fixture``
writes) from the ``print_stats`` event shapes the hermes suites drive
(``tests/runtime/test_family_event_listener.py``). They are labeled as
synthetic inside each file's ``recorded`` block as well.

Replay seam: each recording's frames are normalized to the status dicts
the polling family listener consumes, and driven through the real
``FamilyListener`` state machine. Its terminal poster renders through
the real shared ``format_event`` against an archive job carrying a
post-#1029 ``started_by`` stamp, so the assertions cover the terminal
post content and the starter ping decision (#1095: the rendered text
carries no mention; the classifier pings the starter at the send path),
not just classification.
"""
import json
from pathlib import Path

import pytest

from print_handy.runtime import discord_print_summary as summary
from print_handy.runtime import family_event_listener as fel

RECORDINGS = Path(__file__).parent / "recordings"
REMOTE = "Replay-Part-1_plate_1.gcode"
STARTER = "111222333444555666"
VERBS = {"complete": "Print complete", "cancelled": "Print cancelled",
         "error": "Print stopped with an error"}


def load_recording(name):
    doc = json.loads((RECORDINGS / name).read_text())
    # The fixture really is in the recorder's exported format, and its
    # provenance is labeled synthetic inside the file itself.
    assert set(doc) == {"recorded", "frames"}
    assert doc["recorded"]["provenance"] == "synthetic-in-recorder-format"
    times = []
    for frame in doc["frames"]:
        assert set(frame) == {"t_s", "result"}
        assert set(frame["result"]) == {"eventtime", "status"}
        status = frame["result"]["status"]
        assert {"print_stats", "virtual_sdcard", "idle_timeout",
                "extruder", "heater_bed"} <= set(status)
        times.append(frame["t_s"])
    assert times == sorted(times) and len(times) == len(set(times))
    return doc


def statuses(doc):
    out = []
    for frame in doc["frames"]:
        status = frame["result"]["status"]
        out.append({"state": status["print_stats"]["state"],
                    "filename": status["print_stats"]["filename"],
                    "progress": status["virtual_sdcard"]["progress"]})
    return out


@pytest.fixture
def archive(tmp_path, monkeypatch):
    folder = tmp_path / "archive" / "replay--abc"
    folder.mkdir(parents=True)
    gcode = folder / REMOTE
    gcode.write_text("; estimated printing time (normal mode) = 1h 0m\n"
                     "; total filament used [g] = 12\n")
    job = {"title": "Replay Part", "gcode": str(gcode),
           "started_by": {"platform": "discord", "user_id": STARTER}}
    (folder / "JOB.json").write_text(json.dumps(job))
    monkeypatch.setattr(summary, "ARCHIVE", tmp_path / "archive")
    monkeypatch.setattr(summary, "CONTEXT_PATH", tmp_path / "no-context.json")
    return tmp_path / "archive"


class Rig:
    """A FamilyListener whose posters record — and whose terminal poster
    renders the real shared post text for the terminal kind."""

    def __init__(self, tmp_path, archive, sequence):
        self.starts, self.pauses, self.terminals, self.retires = [], [], [], []
        self.rendered = []
        items = list(sequence)

        def next_status():
            return items.pop(0) if len(items) > 1 else items[0]

        def terminal(kind, filename):
            self.terminals.append((kind, filename))
            self.rendered.append(
                summary.format_event(kind, filename, archive=archive, emit=False))
            return True

        self.listener = fel.FamilyListener(
            "prusalink", next_status,
            claim_fn=lambda _f: False,
            start_poster=lambda kind, fn: self.starts.append((kind, fn)) or True,
            pause_poster=lambda fn, *a: self.pauses.append(fn) or True,
            terminal_poster=terminal,
            retirer=lambda fn, headline: self.retires.append((fn, headline)) or 1,
            beat=lambda **kw: None, sleep=lambda _s: None,
            clock=lambda: 0.0, state_path=tmp_path / "family.json")
        self.polls = len(sequence)

    def run(self):
        for _ in range(self.polls):
            self.listener.poll_once()
        return self


@pytest.mark.parametrize("recording, kind", [
    ("completion_pause_complete.json", "complete"),
    ("completion_cancelled.json", "cancelled"),
    ("completion_error.json", "error"),
])
def test_replayed_terminal_sequence_posts_once_with_starter_mention(
        tmp_path, archive, recording, kind, monkeypatch):
    doc = load_recording(recording)
    assert doc["recorded"]["outcome"] == kind
    rig = Rig(tmp_path, archive, statuses(doc)).run()
    assert rig.starts == [("started", REMOTE)]
    assert rig.terminals == [(kind, REMOTE)]
    (text,) = rig.rendered
    assert text.startswith(f"{VERBS[kind]}: Replay Part"), text
    # Post-#1095 shape: the rendered terminal text carries no mention;
    # the send path's classifier pings only the starter, once.
    assert "<@" not in text
    from print_handy.runtime import starter_pings
    monkeypatch.setattr(starter_pings, "STATE", tmp_path / "pings.json")
    assert starter_pings.ping_for(kind, job_key=REMOTE) == STARTER
    assert rig.retires and rig.retires[-1][0] == REMOTE


def test_replayed_pause_episode_alerts_once_per_episode(tmp_path, archive):
    doc = load_recording("completion_pause_complete.json")
    rig = Rig(tmp_path, archive, statuses(doc)).run()
    # The recording holds the paused state for two consecutive frames:
    # one pause episode, one alert — and the print still completes.
    assert rig.pauses == [REMOTE]
    assert rig.terminals == [("complete", REMOTE)]


def test_replaying_the_same_recording_twice_renders_identically(tmp_path, archive):
    doc = load_recording("completion_error.json")
    first = Rig(tmp_path / "a", archive, statuses(doc)).run()
    second = Rig(tmp_path / "b", archive, statuses(doc)).run()
    assert first.terminals == second.terminals == [("error", REMOTE)]
    assert first.rendered == second.rendered


def test_a_recording_without_a_start_posts_no_terminal(tmp_path, archive):
    """A print first seen mid-flight is a baseline (the family listener's
    contract): replaying only the tail of the completion recording —
    frames from the paused state on — must not invent a terminal post
    for a print this listener never saw start."""
    doc = load_recording("completion_pause_complete.json")
    tail = statuses(doc)[3:]  # paused onward: no standby -> printing edge
    rig = Rig(tmp_path, archive, tail).run()
    assert rig.starts == [] and rig.terminals == [] and rig.rendered == []
