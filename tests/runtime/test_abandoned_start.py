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

from print_handy.runtime import moonraker_event_listener as mel

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
    # The start took: the idle, cold printer after it is a finished print, never an abandoned
    # start (#1282), however long it stays that way.
    assert cold(watch, now=120.0) is False
    assert cold(watch, now=180.0) is False
    assert cold(watch, now=3600.0) is False


def test_a_reclaim_of_the_same_file_starts_a_fresh_window_after_a_print_took():
    watch = mel.AbandonedStartWatch()
    assert watch.observe(FILE, "printing", 220.0, 60.0, now=0.0, claimed_at=-10.0) is False
    assert watch.observe(FILE, "standby", 0.0, 0.0, now=100.0, claimed_at=-10.0) is False
    assert watch.observe(FILE, "standby", 0.0, 0.0, now=200.0, claimed_at=-10.0) is False
    # The same file claimed again (a reprint): its own start can be abandoned.
    assert watch.observe(FILE, "standby", 0.0, 0.0, now=300.0, claimed_at=290.0) is False
    assert watch.observe(FILE, "standby", 0.0, 0.0, now=360.0, claimed_at=290.0) is True


ZMOD_UPLOAD = "Tiny_plate_1_" + "ef" * 32 + ".gcode.3mf"


def test_issue_1282_a_zmod_print_that_completed_is_never_reported_as_abandoned(monkeypatch, tmp_path):
    """#1282 replay: print-now claims the uploaded X.gcode.3mf; the AD5X (ZMod) runs it as
    3mf_print/X.gcode, completes, then resets to standby with an empty filename and cold
    heaters. The old watch dropped the claim's window on the printing read and opened a fresh
    one on the first cold read after the print, so 60 s later the poll logged "claimed start
    never became a print", tried to edit the start card and released the launch lease."""
    claims = tmp_path / "claims.json"
    claimed_at = time.time() - 600
    claims.write_text(json.dumps({ZMOD_UPLOAD: claimed_at}))
    monkeypatch.setattr(mel, "START_CLAIMS", claims)
    listener = mel.Listener()
    reads = []
    monkeypatch.setattr(mel.print_control, "_call", lambda path: reads[-1])
    announced, released = [], []
    monkeypatch.setattr(mel, "announce_start_abandoned", lambda filename: announced.append(filename) or True)
    monkeypatch.setattr(mel, "release_start_claim", lambda filename: released.append(filename))
    monkeypatch.setattr(listener, "_release_abandoned_lease", lambda filename: released.append(filename))
    monkeypatch.setattr(mel, "log", lambda line: None); monkeypatch.setattr(mel, "warn", lambda line: None)

    def read(state, filename, extruder=0.0, bed=0.0):
        return {"result": {"status": {"print_stats": {"state": state, "filename": filename},
                                      "extruder": {"target": extruder}, "heater_bed": {"target": bed}}}}

    clock = [time.time()]
    monkeypatch.setattr(mel.time, "time", lambda: clock[0])
    for state, filename, extruder, bed in (
            ("printing", "3mf_print/" + ZMOD_UPLOAD[:-len(".3mf")], 220.0, 60.0),
            ("complete", "3mf_print/" + ZMOD_UPLOAD[:-len(".3mf")], 0.0, 0.0),
            ("standby", "", 0.0, 0.0), ("standby", "", 0.0, 0.0), ("standby", "", 0.0, 0.0)):
        reads.append(read(state, filename, extruder, bed))
        asyncio.run(listener.poll_abandoned_starts())
        clock[0] += 45.0
    assert announced == [] and released == []


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
    from print_handy.runtime import print_alert_buttons as pab
    from print_handy.runtime import print_next
    monkeypatch.setattr(print_next, "take_start_message_target",
                        lambda filename, now=None: (message_id, "chan-1") if message_id else (None, None))
    monkeypatch.setattr(pab, "edit_message",
                        lambda token, channel, mid, text: edits.append((mid, channel, text)) or True)
    monkeypatch.setattr(mel, "_bot_token", lambda: "token")
    monkeypatch.setattr(mel, "_objects_line", lambda filename: "")
    monkeypatch.setattr(mel.print_summary, "format_event", lambda event, filename: f"Print started: {filename}")
    monkeypatch.setattr(mel, "log", lambda line: None); monkeypatch.setattr(mel, "warn", lambda line: None)
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
    monkeypatch.setattr(mel, "log", lambda line: None); monkeypatch.setattr(mel, "warn", lambda line: None)
    asyncio.run(listener.poll_abandoned_starts())
    assert announced == [FILE] and released == [FILE]
    asyncio.run(listener.poll_abandoned_starts())  # the claim is gone: nothing fires twice
    assert announced == [FILE] and released == [FILE]


OTHER = "Other_plate_1_" + "cd" * 32 + ".gcode"


def test_two_claims_each_keep_their_own_cold_window():
    """Codex P2 on #848: the poll feeds every outstanding claim the same empty-filename cold
    read, alternating filenames. A shared watch resets its window on every filename change, so
    neither claim ever reached 60 s; per-claim watches each fire at their own 60 s."""
    watch = mel.AbandonedStartWatch()
    for filename in (FILE, OTHER):
        assert cold(watch, filename=filename, now=1000.0) is False
    for filename in (FILE, OTHER):
        assert cold(watch, filename=filename, now=1030.0) is False
    assert cold(watch, filename=FILE, now=1060.0) is True    # FILE's own window, met
    assert cold(watch, filename=OTHER, now=1060.0) is True   # OTHER's own window, met
    assert cold(watch, filename=FILE, now=2000.0) is False   # still exactly once each
    assert cold(watch, filename=OTHER, now=2000.0) is False


def test_a_completed_claim_does_not_starve_the_next_claims_window():
    """Two claims: FILE's start completes (a printing read), OTHER's is abandoned. The poll
    keeps feeding both claims the idle cold reads, and OTHER must still be reported after its
    own sustained 60 s — the completed claim's interleaved reads must not reset it."""
    watch = mel.AbandonedStartWatch()
    assert watch.observe(FILE, "printing", 220.0, 60.0, now=0.0) is False  # FILE's start took
    assert cold(watch, filename=OTHER, now=10.0) is False                  # OTHER goes cold
    assert cold(watch, filename=FILE, now=20.0) is False                   # printer idle again
    assert cold(watch, filename=OTHER, now=40.0) is False
    assert cold(watch, filename=FILE, now=50.0) is False
    assert cold(watch, filename=OTHER, now=70.0) is True                   # OTHER's own 60 s


def test_a_retired_claim_gets_a_fresh_window_when_the_file_is_claimed_again():
    watch = mel.AbandonedStartWatch()
    assert cold(watch, now=0.0) is False
    assert cold(watch, now=60.0) is True
    watch.retire(FILE)  # the poll released the claim after firing
    assert cold(watch, now=120.0) is False  # a reprint of the same file starts a new window
    assert cold(watch, now=180.0) is True


def test_poll_reports_two_outstanding_claims_from_the_same_cold_reads(monkeypatch):
    """Poll level: two unexpired claims, the printer reports an empty filename, standby, cold.
    Both watches were seeded cold 120 s ago; one poll must announce and release both, not just
    whichever claim the shared watch happened to be tracking."""
    listener = mel.Listener()
    for filename in (FILE, OTHER):
        listener.abandoned_watch.observe(filename, "standby", 0.0, 0.0, now=time.time() - 120)
    announced, released = [], []
    monkeypatch.setattr(mel, "claimed_start_filenames", lambda now=None: [FILE, OTHER])
    monkeypatch.setattr(mel.print_control, "_call", lambda path: {"result": {"status": {
        "print_stats": {"state": "standby", "filename": ""},
        "extruder": {"target": 0.0}, "heater_bed": {"target": 0.0}}}})
    monkeypatch.setattr(mel, "announce_start_abandoned", lambda filename: announced.append(filename) or True)
    monkeypatch.setattr(mel, "release_start_claim", lambda filename: released.append(filename))
    monkeypatch.setattr(mel, "log", lambda line: None); monkeypatch.setattr(mel, "warn", lambda line: None)
    asyncio.run(listener.poll_abandoned_starts())
    assert announced == [FILE, OTHER] and released == [FILE, OTHER]


def test_poll_stays_quiet_while_the_claimed_start_is_heating(monkeypatch):
    listener = mel.Listener()
    announced = []
    monkeypatch.setattr(mel, "claimed_start_filenames", lambda now=None: [FILE])
    monkeypatch.setattr(mel.print_control, "_call", lambda path: {"result": {"status": {
        "print_stats": {"state": "standby", "filename": ""},
        "extruder": {"target": 220.0}, "heater_bed": {"target": 60.0}}}})
    monkeypatch.setattr(mel, "announce_start_abandoned", lambda filename: announced.append(filename) or True)
    monkeypatch.setattr(mel, "log", lambda line: None); monkeypatch.setattr(mel, "warn", lambda line: None)
    asyncio.run(listener.poll_abandoned_starts())
    assert announced == []


def test_an_abandoned_start_releases_its_launch_lease_for_the_next_print(monkeypatch):
    # #848 review: the start path keeps a preparing start's lease uncertain. When the printer
    # gives up on it, nothing else ever settled that lease, so every later print was refused
    # until someone ran reconcile-launch, which a chat user cannot.
    from print_handy.core import launch_state
    key = "flashforge-ad5x"
    lease = launch_state.acquire(key, job_json="/archive/a/JOB.json", gcode_sha256="a" * 64, remote=FILE)
    lease.submitting()
    lease.uncertain("preparing, never printed")
    listener = mel.Listener()
    listener.abandoned_watch.observe(FILE, "standby", 0.0, 0.0, now=time.time() - 120)
    monkeypatch.setattr(mel, "claimed_start_filenames", lambda now=None: [FILE])
    monkeypatch.setattr(mel.print_control, "_call", lambda path: {"result": {"status": {
        "print_stats": {"state": "standby", "filename": ""},
        "extruder": {"target": 0.0}, "heater_bed": {"target": 0.0}}}})
    monkeypatch.setattr(mel, "announce_start_abandoned", lambda filename: True)
    monkeypatch.setattr(mel, "release_start_claim", lambda filename: None)
    monkeypatch.setattr(mel, "log", lambda line: None); monkeypatch.setattr(mel, "warn", lambda line: None)
    from print_handy import listeners
    monkeypatch.setattr(listeners, "instance_printer_id", lambda: key)
    asyncio.run(listener.poll_abandoned_starts())
    assert launch_state.pending(key) == []
    launch_state.acquire(key, job_json="/archive/b/JOB.json", gcode_sha256="b" * 64,
                         remote="Next.gcode").release()


def test_release_abandoned_touches_only_that_files_sent_lease():
    from print_handy.core import launch_state
    key = "flashforge-ad5x"
    lease = launch_state.acquire(key, job_json="/archive/a/JOB.json", gcode_sha256="a" * 64, remote=FILE)
    assert launch_state.release_abandoned(key, FILE) is None  # leased: nothing was sent yet
    lease.submitting()
    assert launch_state.release_abandoned(key, FILE) is None  # its live owner is still deciding
    lease.uncertain("preparing")
    assert launch_state.release_abandoned(key, OTHER) is None
    assert launch_state.release_abandoned(key, "") is None
    assert launch_state.pending(key)[0]["phase"] == "uncertain"
    released = launch_state.release_abandoned(key, "3mf_print/" + FILE)
    assert released is None or released["reconciled"] == "not_started"
    if released is None:  # FILE is plain gcode: the printer reports it bare or under gcodes/
        released = launch_state.release_abandoned(key, "gcodes/" + FILE)
    assert released["reconciled"] == "not_started" and launch_state.pending(key) == []
