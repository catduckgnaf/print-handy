"""#1150 item 3: the interval timer must not skip beats under scheduler delay.

Experiment (fake clock, fake camera, no real time passes): a scheduler
polls the capture session every second -- sub-interval, like the
Moonraker listener's status-driven capture attempts -- except for one
stall, where the scheduler is delayed and no poll lands for 15 seconds.
On the pre-fix trigger, which re-anchored every deadline to the actual
capture time, the beats inside the stall collapsed into the single late
capture and the whole schedule drifted: 10 frames in the window instead
of the expected 13, at 28/33/38/... instead of on the 5 second grid.

The fixed trigger schedules against absolute deadlines: a delayed poll
makes a frame late, not lost -- missed deadlines stay due and are
caught up one per poll, and the schedule realigns to the original grid.
A gap far longer than a scheduler delay (a paused print, a suspended
process) is not caught up in a burst: the first frame after it
re-anchors the schedule.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from print_handy.hermes import timelapse_capture  # noqa: E402

JPEG = b"\xff\xd8" + b"fake-jpeg-payload" + b"\xff\xd9"
START = 1_000_000.0
INTERVAL = 5.0
WINDOW = 60
EXPECTED_GRID = list(range(0, WINDOW + 1, int(INTERVAL)))


def poll_times(stall_after=None, stall_to=None, window=WINDOW):
    """One poll per second, except the stall: after the poll at
    ``stall_after`` the next poll lands at ``stall_to``."""
    times = []
    t = 0
    while t <= window:
        times.append(t)
        t = stall_to if stall_after is not None and t == stall_after else t + 1
    return times


def run_session(times):
    tmp = tempfile.TemporaryDirectory()
    clock = {"now": START}
    session = timelapse_capture.CaptureSession(
        Path(tmp.name), "job:1", "long.gcode",
        trigger=timelapse_capture.make_trigger("interval", INTERVAL),
        url="http://cam/s", fetch=lambda url: JPEG, now=lambda: clock["now"])
    assert session.start()
    taken = []
    for t in times:
        clock["now"] = START + t
        if session.tick(now=clock["now"]):
            taken.append(t)
    session.stop()
    tmp.cleanup()
    return taken


def run_trigger(times):
    trigger = timelapse_capture.IntervalTrigger(INTERVAL)
    due_at = []
    for t in times:
        if trigger.due(START + t, False):
            due_at.append(t)
            trigger.note_captured(START + t)
    return due_at


class DelayedSchedulerTests(unittest.TestCase):
    def test_no_stall_captures_on_the_grid(self):
        # Control: an undelayed scheduler yields exactly the grid count.
        self.assertEqual(run_session(poll_times()), EXPECTED_GRID)

    def test_delayed_scheduler_still_yields_the_expected_count(self):
        # Polls every second, but after t=12 the scheduler stalls and
        # the next poll lands at t=28: the 15, 20 and 25 second beats
        # must be caught up, not skipped, and the schedule must be back
        # on the original grid afterwards (35, 40, ... not 33, 38, ...).
        times = poll_times(stall_after=12, stall_to=28)
        taken = run_session(times)
        self.assertEqual(len(taken), len(EXPECTED_GRID))
        self.assertEqual(taken, [0, 5, 10, 28, 29, 30, 31, 35, 40, 45, 50, 55, 60])

    def test_trigger_deadlines_survive_a_stall(self):
        times = poll_times(stall_after=12, stall_to=28)
        self.assertEqual(run_trigger(times),
                         [0, 5, 10, 28, 29, 30, 31, 35, 40, 45, 50, 55, 60])

    def test_a_long_gap_reanchors_instead_of_bursting(self):
        # A ten-minute gap (a pause, a suspended process) is not
        # scheduler jitter: the first poll after it captures one frame,
        # and the next frames fall one interval apart from there -- no
        # burst of stale-deadline catch-up frames at poll cadence.
        times = poll_times(stall_after=12, stall_to=600, window=615)
        taken = run_session(times)
        self.assertEqual([t for t in taken if t >= 600], [600, 605, 610, 615])

    def test_a_layer_capture_does_not_consume_an_interval_deadline(self):
        # "both" mode: a layer frame taken before the interval deadline
        # must not push the interval floor out; the floor still fires
        # on its original deadline.
        trigger = timelapse_capture.BothTrigger(INTERVAL)
        self.assertTrue(trigger.due(START, True))
        trigger.note_captured(START)  # first frame: layer + interval due together
        trigger.note_captured(START)  # a second layer frame at the same instant
        self.assertFalse(trigger.due(START + 4, False))
        self.assertTrue(trigger.due(START + 5, False))


if __name__ == "__main__":
    unittest.main()
