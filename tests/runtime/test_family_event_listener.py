"""family_event_listener + the Bambu sink (#460): external-print parity.

Offline throughout: scripted normalised statuses drive the polling
listener; synthetic contract events drive the Bambu sink. The contract
under test is the Moonraker listener's: a print started outside Print
Handy gets exactly one start post, one pause alert per pause episode and
one terminal post; a claimed print gets none of them (its monitor owns
it) but its pause alerts are still retired when it ends; a print first
seen mid-flight is a baseline and gets nothing.
"""
import logging

from print_handy import __version__
from print_handy.runtime import family_control as fc
from print_handy.runtime import family_event_listener as fel

REMOTE = "benchy.gcode"


def st(state, filename=REMOTE):
    return {"state": state, "filename": filename, "progress": 0.5}


def scripted(values):
    items = list(values)

    def next_value():
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, Exception):
            raise item
        return item

    return next_value


class Posts:
    def __init__(self):
        self.starts = []
        self.pauses = []
        self.terminals = []
        self.retires = []
        self.beats = []

    def start(self, kind, filename):
        self.starts.append((kind, filename))
        return True

    def terminal(self, kind, filename):
        self.terminals.append((kind, filename))
        return True

    def pause(self, filename, connection, printer_id, status):
        self.pauses.append((filename, connection))
        return True

    def retire(self, filename, headline):
        self.retires.append((filename, headline))
        return 1

    def beat(self, **details):
        self.beats.append(details)


def listener(tmp_path, statuses, claimed=False, name="fam.json", clock=None):
    posts = Posts()
    rig = fel.FamilyListener(
        "prusalink", scripted(statuses),
        claim_fn=lambda _f: claimed,
        start_poster=posts.start, pause_poster=posts.pause,
        terminal_poster=posts.terminal, retirer=posts.retire,
        beat=posts.beat, sleep=lambda _s: None,
        clock=clock or (lambda: 0.0),
        state_path=tmp_path / name)
    return rig, posts


def drive(rig, polls):
    for _ in range(polls):
        rig.poll_once()


# -- the polling listener ---------------------------------------------------------

def test_run_forever_startup_line_names_the_version(tmp_path, caplog):
    rig, _posts = listener(tmp_path, [st("printing")])
    rig._sleep = lambda _s: rig.stop()
    with caplog.at_level(logging.INFO, logger="print_handy.runtime.family_event_listener"):
        assert rig.run_forever() == 0
    assert f"family listener starting (Print Handy {__version__})" in caplog.text


def test_external_print_gets_one_start_one_pause_alert_one_terminal(tmp_path):
    rig, posts = listener(tmp_path, [
        st("standby", ""), st("printing"), st("printing"), st("paused"),
        st("paused"), st("printing"), st("complete"), st("standby", "")])
    drive(rig, 8)
    assert posts.starts == [("started", REMOTE)]
    assert posts.pauses == [(REMOTE, "prusalink")]
    assert posts.terminals == [("complete", REMOTE)]
    assert posts.retires == [(REMOTE, "Print complete.")]


def test_claimed_print_is_the_monitors_not_the_listeners(tmp_path):
    rig, posts = listener(tmp_path, [
        st("standby", ""), st("printing"), st("paused"), st("complete")],
        claimed=True)
    drive(rig, 4)
    assert posts.starts == [] and posts.pauses == [] and posts.terminals == []
    # Retiring is the one thing the listener still does for a claimed
    # print: it cleans up the monitor's pause alert if the monitor died.
    assert posts.retires == [(REMOTE, "Print complete.")]


def test_print_first_seen_mid_flight_is_a_silent_baseline(tmp_path):
    rig, posts = listener(tmp_path, [st("printing"), st("paused"), st("complete")])
    drive(rig, 3)
    assert posts.starts == [] and posts.pauses == [] and posts.terminals == []
    assert posts.retires == [(REMOTE, "Print complete.")]


def test_standby_after_active_posts_cancelled(tmp_path):
    rig, posts = listener(tmp_path, [
        st("standby", ""), st("printing"), st("standby", "")])
    drive(rig, 3)
    assert posts.terminals == [("cancelled", REMOTE)]
    assert posts.retires == [(REMOTE, "Print ended at the printer.")]


def test_standby_at_full_progress_is_a_completed_print(tmp_path):
    # OctoPrint has no "complete" state: a finished job reads as standby at 100%.
    done = {"state": "standby", "filename": REMOTE, "progress": 1.0}
    rig, posts = listener(tmp_path, [st("standby", ""), st("printing"), done])
    drive(rig, 3)
    assert posts.terminals == [("complete", REMOTE)]
    assert posts.retires == [(REMOTE, "Print complete.")]


def test_octoprint_finished_job_through_the_real_adapter_posts_complete(tmp_path):
    from print_handy.printers.octoprint.adapter import OctoPrintAdapter

    adapter = OctoPrintAdapter.__new__(OctoPrintAdapter)

    def raw(flags, completion):
        return {"printer": {"state": {"flags": flags}, "temperature": {}},
                "job": {"job": {"file": {"path": REMOTE}}, "progress": {"completion": completion}}}

    idle = {"ready": True, "operational": True}
    statuses = [adapter.normalize_status(raw(idle, None)),
                adapter.normalize_status(raw({"printing": True, "operational": True}, 42.0)),
                adapter.normalize_status(raw(idle, 100.0))]
    rig, posts = listener(tmp_path, statuses)
    drive(rig, 3)
    assert posts.terminals == [("complete", REMOTE)]

    cancelled = [statuses[0], statuses[1], adapter.normalize_status(raw(idle, 42.0))]
    rig, posts = listener(tmp_path, cancelled, name="fam2.json")
    drive(rig, 3)
    assert posts.terminals == [("cancelled", REMOTE)]


def test_restart_restores_the_classification_without_a_second_start(tmp_path):
    rig_a, posts_a = listener(tmp_path, [st("standby", ""), st("printing")])
    drive(rig_a, 2)
    assert posts_a.starts == [("started", REMOTE)]

    # A new listener process over the same state file, mid-print.
    rig_b, posts_b = listener(tmp_path, [st("printing"), st("paused"), st("complete")])
    drive(rig_b, 3)
    assert posts_b.starts == []          # no duplicate start post
    assert posts_b.terminals == []       # restored prints stay baselined
    assert posts_b.retires == [(REMOTE, "Print complete.")]


def test_unknown_gap_before_the_terminal_still_ends_the_print(tmp_path):
    rig, posts = listener(tmp_path, [
        st("standby", ""), st("printing"), st("unknown"), st("complete")])
    drive(rig, 4)
    assert posts.terminals == [("complete", REMOTE)]
    assert posts.retires == [(REMOTE, "Print complete.")]


def test_read_failures_cost_polls_not_the_listener(tmp_path):
    ticks = [0.0]

    def clock():
        ticks[0] += 31.0  # past the 30 s heartbeat interval, per poll
        return ticks[0]

    rig, posts = listener(tmp_path, [
        RuntimeError("printer unreachable"), RuntimeError("still down"),
        st("standby", ""), st("printing")], clock=clock)
    drive(rig, 4)
    assert posts.starts == [("started", REMOTE)]
    assert posts.beats[0]["printer_connected"] is False
    assert posts.beats[-1]["printer_connected"] is True


def test_replacement_print_is_a_new_witnessed_start(tmp_path):
    rig, posts = listener(tmp_path, [
        st("standby", ""), st("printing", "a.gcode"), st("printing", "b.gcode"),
        st("complete", "b.gcode")])
    drive(rig, 4)
    assert posts.starts == [("started", "a.gcode"), ("started", "b.gcode")]
    # The replaced print ended unobserved: its terminal post is the
    # cancelled kind, the Moonraker "gone job" shape.
    assert posts.terminals == [("cancelled", "a.gcode"), ("complete", "b.gcode")]
    # a.gcode's close-out retires under the standby headline; b.gcode's
    # terminal retires under the completion headline.
    assert posts.retires == [("a.gcode", "Print ended at the printer."),
                             ("b.gcode", "Print complete.")]


# -- the Bambu sink -----------------------------------------------------------------

def sink(claimed=False):
    posts = Posts()
    rig = fc.BambuEventSink(
        claim_fn=lambda _f: claimed,
        start_poster=posts.start, pause_poster=posts.pause,
        terminal_poster=posts.terminal, retirer=posts.retire)
    return rig, posts


def event(state, transition=None, filename=REMOTE):
    return {"status": st(state, filename), "transition": transition}


def test_bambu_sink_posts_the_external_lifecycle():
    rig, posts = sink()
    rig("status", event("standby"))
    rig("status", event("printing", "started"))
    rig("status", event("printing"))
    rig("status", event("paused", "paused"))
    rig("completion", event("complete"))
    assert posts.starts == [("started", REMOTE)]
    assert posts.pauses == [(REMOTE, "bambulab_lan")]
    assert posts.terminals == [("complete", REMOTE)]
    assert posts.retires == [(REMOTE, "Print complete.")]


def test_bambu_sink_stays_silent_for_a_claimed_print_but_retires():
    rig, posts = sink(claimed=True)
    rig("status", event("standby"))
    rig("status", event("printing", "started"))
    rig("status", event("paused", "paused"))
    rig("completion", event("complete"))
    assert posts.starts == [] and posts.pauses == [] and posts.terminals == []
    assert posts.retires == [(REMOTE, "Print complete.")]


def test_bambu_sink_never_classifies_a_print_first_seen_mid_flight():
    rig, posts = sink()
    rig("status", event("paused"))            # first observation: no start seen
    rig("status", event("printing", "resumed"))
    rig("completion", event("complete"))
    assert posts.starts == [] and posts.pauses == [] and posts.terminals == []
    assert posts.retires == [(REMOTE, "Print complete.")]


def test_bambu_sink_error_event_posts_the_error_terminal():
    rig, posts = sink()
    rig("status", event("standby"))
    rig("status", event("printing", "started"))
    rig("error", event("error"))
    assert posts.terminals == [("error", REMOTE)]
    assert posts.retires == [(REMOTE, "Print stopped with an error.")]


def test_the_default_retirer_is_scoped_to_the_owning_printer(tmp_path, monkeypatch):
    """P2-176: with no retirer injected, the family listener retires
    through retire_pause_alerts carrying its own printer_id."""
    seen = []
    monkeypatch.setattr(
        fel.family_control, "retire_pause_alerts",
        lambda fn, headline, printer_id=None: seen.append((fn, headline, printer_id)) or 1)
    posts = Posts()
    rig = fel.FamilyListener(
        "prusalink", scripted([st("standby"), st("printing"), st("complete")]),
        printer_id="printer-a",
        claim_fn=lambda _f: False,
        start_poster=posts.start, pause_poster=posts.pause,
        terminal_poster=posts.terminal,
        beat=posts.beat, sleep=lambda _s: None, clock=lambda: 0.0,
        state_path=tmp_path / "fam.json")
    drive(rig, 3)
    assert seen == [(REMOTE, "Print complete.", "printer-a")]
