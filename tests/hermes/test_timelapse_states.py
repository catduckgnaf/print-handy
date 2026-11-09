"""Completion timelapse states (#540): each delivery path reports what
actually happened, and only a bounded wait that really elapsed may be
described as a render timeout.

The incident: capture disabled and the SSH archive destination unset, yet
the listener logged "timelapse not rendered ... within the wait" — a
timeout that never happened, because the archive lookup returns before
searching or waiting when the destination is unset. These tests pin the
distinct states end to end at the listener: archive off (lookup skipped),
render absent after a real wait, archive/delivery failure, the dark
capture outcome, and the in-container capture states — with the ordinary
text completion post intact in every one (#126).
"""
import pytest

from print_handy.hermes import moonraker_discord_announcements as announcer
from print_handy.hermes import moonraker_event_listener as mel

COMPLETE = "Print complete: Stand"


@pytest.fixture
def delivery(monkeypatch):
    posted, logs = [], []
    monkeypatch.setattr(mel, "_post_completion",
                        lambda msg, orig, job_key=None: posted.append(msg) or True)
    monkeypatch.setattr(mel, "_vertical_short_attachment", lambda video, key: None)
    monkeypatch.setattr(mel, "log", lambda line: logs.append(line))
    monkeypatch.setattr(mel, "_captured_timelapse", lambda key: None)
    monkeypatch.setattr(mel, "_dark_capture", lambda key: None)
    return posted, logs


def _archive_outcome(monkeypatch, outcome):
    monkeypatch.setattr(announcer, "archive_completed_timelapse", lambda key: outcome)


def test_archive_off_is_never_called_a_render_timeout(delivery, monkeypatch):
    posted, logs = delivery
    _archive_outcome(monkeypatch, announcer.ArchiveOutcome(announcer.ARCHIVE_OFF))
    mel._timelapse_post("stand.gcode", COMPLETE)
    # The configured-off behaviour is unchanged in the channel: bare text.
    assert posted == [COMPLETE]
    # The log names both actual states instead of a wait that never ran.
    assert any("lookup skipped" in line for line in logs)
    assert any("capture is disabled" in line for line in logs)
    assert not any("within the wait" in line for line in logs)
    assert not any("within the wait" in message for message in posted)


def test_capture_enabled_without_a_video_is_not_called_disabled(delivery, monkeypatch):
    posted, logs = delivery
    monkeypatch.setattr(mel.go2rtc, "capture_enabled", lambda data=None: True)
    _archive_outcome(monkeypatch, announcer.ArchiveOutcome(announcer.ARCHIVE_OFF))
    mel._timelapse_post("stand.gcode", COMPLETE)
    assert posted == [COMPLETE]
    assert any("produced no video" in line for line in logs)
    assert not any("capture is disabled" in line for line in logs)


def test_not_rendered_after_a_real_wait_keeps_the_timeout_wording(delivery, monkeypatch):
    posted, logs = delivery
    _archive_outcome(monkeypatch, announcer.ArchiveOutcome(announcer.ARCHIVE_NOT_RENDERED))
    mel._timelapse_post("stand.gcode", COMPLETE)
    assert posted == [f"{COMPLETE}\nTimelapse not attached: the printer's rendered video "
                      "didn't arrive within the wait (see the listener log)."]
    assert any("timelapse not rendered for 'stand.gcode' within the wait" in line
               for line in logs)


def test_archive_failure_is_reported_as_a_delivery_failure(delivery, monkeypatch):
    posted, logs = delivery
    _archive_outcome(monkeypatch, announcer.ArchiveOutcome(
        announcer.ARCHIVE_FAILED, detail="ssh down"))
    mel._timelapse_post("stand.gcode", COMPLETE)
    # The text completion post is intact; the note names the actual state.
    assert len(posted) == 1
    assert posted[0].startswith(COMPLETE)
    assert "archiving it failed" in posted[0]
    assert "didn't arrive" not in posted[0]
    assert any("archive failed" in line and "ssh down" in line for line in logs)
    assert not any("within the wait" in line for line in logs)
    assert not any("within the wait" in message for message in posted)


def test_dark_capture_still_names_its_own_outcome(delivery, monkeypatch):
    posted, logs = delivery
    monkeypatch.setattr(mel, "_dark_capture", lambda key: {"dark_frames": 2})
    _archive_outcome(monkeypatch, announcer.ArchiveOutcome(announcer.ARCHIVE_OFF))
    mel._timelapse_post("stand.gcode", COMPLETE)
    assert len(posted) == 1
    assert posted[0].startswith(COMPLETE)
    assert "completely dark" in posted[0]
    assert not any("within the wait" in message for message in posted)


def test_plan_clip_archive_off_logs_the_skip_not_a_timeout(delivery, monkeypatch):
    posted, logs = delivery
    monkeypatch.setattr(mel.plan_timelapse, "prune", lambda: [])
    _archive_outcome(monkeypatch, announcer.ArchiveOutcome(announcer.ARCHIVE_OFF))
    mel._plan_timelapse_post("plate.gcode", "Plate 1 done", ("trio", "more", 0))
    assert posted == ["Plate 1 done"]
    assert any("lookup skipped" in line for line in logs)
    assert not any("within the wait" in line for line in logs)


def test_plan_clip_not_rendered_keeps_the_timeout_wording(delivery, monkeypatch):
    posted, logs = delivery
    monkeypatch.setattr(mel.plan_timelapse, "prune", lambda: [])
    _archive_outcome(monkeypatch, announcer.ArchiveOutcome(announcer.ARCHIVE_NOT_RENDERED))
    mel._plan_timelapse_post("plate.gcode", "Plate 1 done", ("trio", "more", 0))
    assert posted == ["Plate 1 done"]
    assert any("timelapse not rendered for 'plate.gcode' within the wait" in line
               for line in logs)


def test_plan_clip_archive_failure_is_logged_as_a_failure(delivery, monkeypatch):
    posted, logs = delivery
    monkeypatch.setattr(mel.plan_timelapse, "prune", lambda: [])
    _archive_outcome(monkeypatch, announcer.ArchiveOutcome(
        announcer.ARCHIVE_FAILED, detail="disk full"))
    mel._plan_timelapse_post("plate.gcode", "Plate 1 done", ("trio", "more", 0))
    assert posted == ["Plate 1 done"]
    assert any("archive failed" in line and "disk full" in line for line in logs)
    assert not any("within the wait" in line for line in logs)
