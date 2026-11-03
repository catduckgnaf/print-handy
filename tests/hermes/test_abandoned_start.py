"""#836: a claimed start that heats, then drops back to standby with cold heaters and never prints.

The start path now keeps such a start's lease uncertain, and the listener owns the follow-up:
after the printer has read standby/error with both heater targets at 0 for
``ABANDONED_START_COLD_S``, it edits the one start card once and releases the claim.
These tests drive the watch at its smallest seam, plus the card edit, the claim
release and the listener poll wiring.
"""
import asyncio
import json
import time

from print_handy.hermes import moonraker_event_listener as mel

FILE = "Replay-Part_plate_1_" + "ab" * 32 + ".gcode"


def cold(watch, filename=FILE, state="standby", now=1000.0):
    return watch.observe(filename, state, 0.0, 0.0, now=now)


def test_cold_standby_must_be_sustained_for_the_named_threshold():
    watch = mel.AbandonedStartWatch()
    assert mel.ABANDONED_START_COLD_S == 60.0
    assert cold(watch, now=1000.0) is False
    assert cold(watch, now=1000.0 + 59.9) is False
    assert cold(watch, now=1000.0 + 60.0) is True
    # Exactly once: later cold reads do not fire again for the same claimed start.
    assert cold(watch, now=1000.0 + 3600.0) is False


def test_error_state_with_cold_heaters_counts_as_abandoned():
    watch = mel.AbandonedStartWatch()
    assert watch.observe(FILE, "error", 0.0, 0.0, now=0.0) is False
    assert watch.observe(FILE, "error", 0.0, 0.0, now=61.0) is True


def test_any_heater_target_restarts_the_cold_window():
    watch = mel.AbandonedStartWatch()
    assert cold(watch, now=0.0) is False
    assert watch.observe(FILE, "standby", 200.0, 0.0, now=50.0) is False  # heating again: not abandoned
    assert cold(watch, now=60.0) is False   # the cold window restarted at the heating read
    assert cold(watch, now=120.0) is True


def test_printing_or_paused_never_reads_as_abandoned():
    watch = mel.AbandonedStartWatch()
    assert cold(watch, now=0.0) is False
    assert watch.observe(FILE, "printing", 0.0, 0.0, now=30.0) is False
    assert watch.observe(FILE, "paused", 0.0, 0.0, now=90.0) is False
    assert cold(watch, now=120.0) is False  # a fresh cold window starts only now
    assert cold(watch, now=180.0) is True


def test_an_unreadable_target_is_not_evidence_of_anything():
    watch = mel.AbandonedStartWatch()
    assert cold(watch, now=0.0) is False
    assert watch.observe(FILE, "standby", None, 0.0, now=3600.0) is False
    assert cold(watch, now=3600.0) is False
    assert cold(watch, now=3660.0) is True


def test_a_new_claimed_file_gets_its_own_window():
    watch = mel.AbandonedStartWatch()
    assert cold(watch, now=0.0) is False
    other = "Other_plate_1_" + "cd" * 32 + ".gcode"
    assert cold(watch, filename=other, now=3600.0) is False
    assert cold(watch, filename=other, now=3660.0) is True


def _claimed_card(monkeypatch, tmp_path, message_id="777"):
    """A recorded start message for FILE, with the Discord edit captured."""
    edits = []
    from print_handy.hermes import print_alert_buttons as pab
    from print_handy.hermes import print_next
    monkeypatch.setattr(print_next, "take_start_message_target",
                        lambda filename, now=None: (message_id, "chan-1") if message_id else (None, None))
    monkeypatch.setattr(pab, "edit_message",
                        lambda token, channel, mid, text: edits.append((mid, channel, text)) or True)
    monkeypatch.setattr(mel, "_bot_token", lambda: "token")
    monkeypatch.setattr(mel, "_objects_line", lambda filename: "")
    monkeypatch.setattr(mel.print_summary, "format_event", lambda event, filename: f"Print started: {filename}")
    monkeypatch.setattr(mel, "log", lambda line: None)
    return edits


def test_announce_start_abandoned_edits_the_start_card_once_with_the_audit_wording(monkeypatch, tmp_path):
    edits = _claimed_card(monkeypatch, tmp_path)
    assert mel.announce_start_abandoned(FILE) is True
    assert len(edits) == 1 and edits[0][0] == "777" and edits[0][1] == "chan-1"
    assert edits[0][2].endswith(
        "The printer stopped preparing this job without printing. Check the IFS slot / HelixScreen.")


def test_announce_start_abandoned_without_a_recorded_card_posts_nothing(monkeypatch, tmp_path):
    edits = _claimed_card(monkeypatch, tmp_path, message_id=None)
    assert mel.announce_start_abandoned(FILE) is False
    assert edits == []  # no second post: the intake's card is the only start card there is


def test_release_start_claim_removes_only_the_matching_claim(tmp_path, monkeypatch):
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps({FILE: 1000.0, "other.gcode": 1000.0}))
    monkeypatch.setattr(mel, "START_CLAIMS", claims)
    mel.release_start_claim("3mf_print/" + FILE[:-len(".gcode")] + ".gcode")  # the printer's reported path
    assert json.loads(claims.read_text()) == {"other.gcode": 1000.0}
    mel.release_start_claim("never-claimed.gcode")  # no-op, no error
    assert json.loads(claims.read_text()) == {"other.gcode": 1000.0}


def test_poll_announces_and_releases_once_the_cold_window_is_met(monkeypatch):
    listener = mel.Listener()
    listener.abandoned_watch.observe(FILE, "standby", 0.0, 0.0, now=time.time() - 120)
    announced, released = [], []
    monkeypatch.setattr(mel, "claimed_start_filenames", lambda now=None: [FILE])
    monkeypatch.setattr(mel.print_control, "_call", lambda path: {"result": {"status": {
        "print_stats": {"state": "standby", "filename": ""},
        "extruder": {"target": 0.0}, "heater_bed": {"target": 0.0}}}})
    monkeypatch.setattr(mel, "announce_start_abandoned", lambda filename: announced.append(filename) or True)
    monkeypatch.setattr(mel, "release_start_claim", lambda filename: released.append(filename))
    monkeypatch.setattr(mel, "log", lambda line: None)
    asyncio.run(listener.poll_abandoned_starts())
    assert announced == [FILE] and released == [FILE]
    asyncio.run(listener.poll_abandoned_starts())  # the claim is gone: nothing fires twice
    assert announced == [FILE] and released == [FILE]


def test_poll_stays_quiet_while_the_claimed_start_is_heating(monkeypatch):
    listener = mel.Listener()
    announced = []
    monkeypatch.setattr(mel, "claimed_start_filenames", lambda now=None: [FILE])
    monkeypatch.setattr(mel.print_control, "_call", lambda path: {"result": {"status": {
        "print_stats": {"state": "standby", "filename": ""},
        "extruder": {"target": 220.0}, "heater_bed": {"target": 60.0}}}})
    monkeypatch.setattr(mel, "announce_start_abandoned", lambda filename: announced.append(filename) or True)
    monkeypatch.setattr(mel, "log", lambda line: None)
    asyncio.run(listener.poll_abandoned_starts())
    assert announced == []
