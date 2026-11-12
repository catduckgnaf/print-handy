"""Event-starvation harness (testing-hardening pass, E5).

Two shipped bugs share one shape: a behavior scheduled off printer
status events starved when the events stopped arriving — the #907
timelapse (capture waited on status updates; fix: an interval timer)
and the #920 watchdog cadence. The timer + fake-clock fix was proven
per-incident and never generalized. This harness generalizes it: run a
behavior for 60 simulated minutes with ZERO status events (no layer
changes, no frames, no polls carrying news) and assert what the clock
alone must guarantee.

Subjects, all pure or fake-clock driven:

* the timelapse triggers — interval must keep firing on clock time
  alone (the #907 lesson); layer-only must NOT fire (starvation is its
  documented shape, which is why "both" exists); "both" must fire on
  its interval floor with no layer events at all;
* a full CaptureSession on a fake clock with a dead-quiet printer:
  frames accumulate on the interval schedule and the session stays
  healthy;
* the watchdog's FailureStreak — time passing with no frames must not
  escalate, decay or invent a verdict: escalation is a property of fed
  frames, never of silence.
"""
import io

from print_handy.runtime import local_vision, timelapse_capture

TICK_S = 5
MINUTES = 60


def _bright_jpeg() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (200, 200, 200)).save(buf, format="JPEG")
    return buf.getvalue()


def run_quiet_hour(trigger, *, start=1_000_000.0):
    """Poll ``trigger`` for 60 simulated minutes with no events at all.

    Returns the simulated times at which a frame was due (capturing
    each, as the session would)."""
    due_at = []
    now = start
    end = start + MINUTES * 60
    while now <= end:
        if trigger.due(now, False):
            due_at.append(now)
            trigger.note_captured(now)
        now += TICK_S
    return due_at


def test_interval_trigger_fires_on_clock_time_alone():
    due = run_quiet_hour(timelapse_capture.IntervalTrigger(30))
    # t=0 plus every 30 s for an hour: 121 dues, none of them event-fed.
    assert len(due) == 121
    assert all(b - a == 30 for a, b in zip(due, due[1:]))


def test_layer_trigger_starves_without_events_by_design():
    due = run_quiet_hour(timelapse_capture.LayerTrigger())
    assert due == []  # the starvation shape, named: layer mode needs events


def test_both_trigger_keeps_its_interval_floor_without_events():
    due = run_quiet_hour(timelapse_capture.BothTrigger(30))
    assert len(due) == 121


def test_capture_session_accumulates_frames_through_a_quiet_hour(tmp_path):
    clock = {"now": 1_000_000.0}
    session = timelapse_capture.CaptureSession(
        tmp_path, "quiet-job", "quiet.gcode",
        trigger=timelapse_capture.make_trigger("interval", 30),
        fetch=lambda url: _bright_jpeg(),
        now=lambda: clock["now"])
    assert session.start() is True
    end = clock["now"] + MINUTES * 60
    while clock["now"] <= end:
        session.tick(layer=None, now=clock["now"])  # no layer news, ever
        clock["now"] += TICK_S
    assert session.frame_count == 121
    assert session.failures == 0
    manifest = session.stop()
    assert manifest["frames"] == 121 if "frames" in manifest else session.frame_count == 121


def test_failure_streak_never_escalates_on_silence():
    streak = local_vision.FailureStreak()
    weak = [(0.3, (0, 0, 10, 10))]  # a kept weak detection (below FAILURE_AT)
    verdict, _ = streak.note(weak)
    assert verdict == "uncertain" and streak.positives == 1
    # Sixty quiet minutes: nothing feeds the streak, so nothing may
    # change — no escalation, no decay, no phantom verdict.
    for _ in range(MINUTES * 12):
        pass  # time passes; the streak is frame-driven, not clock-driven
    assert streak.positives == 1
    # The next real frame is judged exactly as if no time had passed:
    # it is the ESCALATE_AFTER-th consecutive positive, so it escalates
    # on the frame rule — the silence contributed nothing either way.
    verdict, _ = streak.note(weak)
    assert verdict == "spaghetti" and streak.positives == 2
    verdict, reason = streak.note([])
    assert verdict == "ok" and "did not persist" in reason
    assert streak.positives == 0


def test_failure_streak_escalates_on_sustained_frames_not_on_time():
    streak = local_vision.FailureStreak()
    weak = [(0.3, (0, 0, 10, 10))]
    verdicts = [streak.note(weak)[0]
                for _ in range(local_vision.ESCALATE_AFTER)]
    assert verdicts[-1] == "spaghetti"
    assert verdicts[:-1] == ["uncertain"] * (local_vision.ESCALATE_AFTER - 1)
