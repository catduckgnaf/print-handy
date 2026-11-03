"""Mapped AD5X launches replayed over HTTP against the recorded ZMod printer (#616, #690, #691, #693).

Real code end to end: print-now, the idle guard, slot checks, the launch lease, the SET_ZCOLOR
POST, `_wait_printing`, `_printer_phase` and the readback all run unmodified against a fake
Moonraker that serves the recorded responses. Only the upload subprocess is replaced.
"""
import json
import re
import socket
import time
import urllib.error

import pytest

from print_handy.core import launch_state, model_job

from .helpers import intake_outcome, make_job, reported_name, run_print_now

HOLD_S = 35  # printer seconds a screenless SET_ZCOLOR POST stayed open while the printer heated (#616)


def leases():
    return launch_state.pending(model_job._job_printer_id(None))


def printer_starts(after_s, hold_s=0, heat_after_s=None):
    """SET_ZCOLOR behaviour: the printer heats, then reports the uploaded file as printing after `after_s`.
    The POST itself is held open for `hold_s` printer seconds."""
    def hook(fake, script):
        if not script.startswith("SET_ZCOLOR"):
            return 0
        remote = re.search(r"FILENAME=(\S+)", script).group(1)
        fake.after(heat_after_s if heat_after_s is not None else after_s / 2, fake.heat)
        fake.after(after_s, lambda: fake.start_printing(remote))
        return hold_s
    return hook


def printer_ignores_start(hold_s):
    """SET_ZCOLOR is accepted but the POST hangs and the printer shows nothing (the start is not yet visible)."""
    return lambda fake, script: hold_s if script.startswith("SET_ZCOLOR") else 0


def is_timeout(exc):
    return isinstance(exc, (TimeoutError, socket.timeout)) or (
        isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (TimeoutError, socket.timeout)))


# --- #616: the SET_ZCOLOR POST blocks while the printer heats ---------------------------------------

def test_a_blocked_set_zcolor_post_is_a_start_not_a_refusal(ad5x, tmp_path, client_errors, capsys):
    ad5x.script_hook = printer_starts(after_s=12, hold_s=HOLD_S, heat_after_s=3)
    job, _ = make_job(tmp_path)

    ok, tail = run_print_now(job)

    # The POST really timed out on a socket: the client gave up while the fake still held it open.
    timeouts = [exc for url, exc in client_errors if url.endswith("/printer/gcode/script") and is_timeout(exc)]
    assert len(timeouts) == 1, client_errors
    status, message = intake_outcome(ok, tail)
    assert status in ("STARTED", "START_PENDING"), tail
    assert message == "" and "Didn't start" not in tail
    # Exactly one upload and one start went out; nothing was resubmitted after the timeout.
    assert len(ad5x.uploads) == 1
    assert len(ad5x.zcolor()) == 1 and ad5x.zcolor()[0].endswith("T0=2")
    assert "MAPPED START READBACK" in tail
    # The printer now reports the uploaded file.
    assert ad5x.status["print_stats"]["state"] == "printing"
    assert ad5x.status["print_stats"]["filename"] == reported_name(ad5x.uploads[0])


def test_the_lease_after_a_lost_response_settles_once_the_printer_reports_the_file(ad5x, tmp_path, client_errors, capsys):
    ad5x.script_hook = printer_starts(after_s=12, hold_s=HOLD_S, heat_after_s=3)
    job, _ = make_job(tmp_path)
    ok, tail = run_print_now(job)
    assert ok and intake_outcome(ok, tail)[0] in ("STARTED", "START_PENDING")
    held = leases()
    assert [row["phase"] for row in held] == ["uncertain"]  # the duplicate guard stays until the file is seen

    # The next launch attempt reads the same printer: its current file settles the lease, and it still
    # does not start the first job again.
    st = model_job.printer_state.load()
    _state, observed, _busy = model_job._printer_phase(st)
    capsys.readouterr()
    lease = launch_state.acquire(model_job._job_printer_id(None), job_json="next.json", remote="next.gcode",
                                 observed_file=observed,
                                 started_files=lambda: model_job._moonraker_started_files(st))
    assert "LAUNCH RECONCILED" in capsys.readouterr().out
    assert [row["remote"] for row in leases()] == ["next.gcode"]
    lease.release()
    assert len(ad5x.zcolor()) == 1 and len(ad5x.uploads) == 1


def test_a_start_the_printer_never_shows_stays_pending_and_is_not_retried(ad5x, tmp_path, client_errors):
    ad5x.script_hook = printer_ignores_start(HOLD_S)
    job, _ = make_job(tmp_path)

    ok, tail = run_print_now(job)

    assert any(is_timeout(exc) for _url, exc in client_errors)
    status, message = intake_outcome(ok, tail)
    assert status == "START_PENDING" and message == ""
    assert [row["phase"] for row in leases()] == ["uncertain"]
    # A second attempt is refused with the held-lease wording and never posts another start.
    job2, _ = make_job(tmp_path, "second", title="Second Part")
    ok2, tail2 = run_print_now(job2)
    status2, message2 = intake_outcome(ok2, tail2)
    assert status2 == "START_REFUSED" and message2.startswith("Didn't start: an earlier start")
    assert len(ad5x.zcolor()) == 1 and len(ad5x.uploads) == 1


# --- #836: a start the printer accepts, heats for, then abandons is not "started" ---------------------

def printer_heats_then_gives_up(prep_s=10):
    """SET_ZCOLOR accepted at once; native prep heats for prep_s printer seconds, then the targets drop
    back to 0 and print_stats stays standby with no file (the printer abandoned the start)."""
    def cool(fake):
        with fake.lock:
            fake.status["extruder"]["target"] = 0.0
            fake.status["heater_bed"]["target"] = 0.0
            fake.status["idle_timeout"]["state"] = "Ready"
    def hook(fake, script):
        if not script.startswith("SET_ZCOLOR"):
            return 0
        fake.heat()                                   # targets set at once: what _preparation_evidence sees
        fake.after(prep_s, lambda: cool(fake))
        return 0                                      # the POST returns at once with 200
    return hook


def test_a_start_that_heats_then_returns_to_standby_is_not_reported_started(ad5x, tmp_path):
    # Preparation outlasts print-now's first heater read (2 real s), as a real IFS load does.
    ad5x.script_hook = printer_heats_then_gives_up(prep_s=60)
    job, _ = make_job(tmp_path)

    ok, tail = run_print_now(job, wait_s=6)           # real seconds; covers 120 printer s at SCALE 1/20

    status, _message = intake_outcome(ok, tail)
    assert ad5x.status["print_stats"]["state"] == "standby"           # sanity: the printer never printed
    assert ad5x.status["extruder"]["target"] == 0.0
    assert status == "START_REFUSED", tail                             # not "started": it never printed
    assert "went back to standby without printing" in tail
    assert leases() == []          # #848 review: the printer is free for the next start, no CLI needed
    assert len(ad5x.zcolor()) == 1 and len(ad5x.uploads) == 1          # never resubmitted


def test_a_start_still_preparing_when_the_wait_ends_keeps_its_lease_uncertain(ad5x, tmp_path):
    """#836: preparation evidence alone is provisional. While the printer is still heating at the end
    of the wait, the start reads "preparing", but the lease stays uncertain (not released) and the
    start claim stays held, so the listener can still report it if the printer never prints."""
    def hook(fake, script):
        if not script.startswith("SET_ZCOLOR"):
            return 0
        fake.heat()  # heats and keeps heating: preparation outlasts the wait window
        return 0
    ad5x.script_hook = hook
    job, _ = make_job(tmp_path)

    ok, tail = run_print_now(job, wait_s=3)

    status, _message = intake_outcome(ok, tail)
    assert status == "STARTED", tail  # provisional: the intake's card says started, warming up
    phase = json.loads([ln for ln in tail.splitlines() if "MAPPED START READBACK " in ln][-1]
                       .split("MAPPED START READBACK ", 1)[1])["phase"]
    assert phase == "preparing", tail
    assert [row["phase"] for row in leases()] == ["uncertain"]  # today: [] (released on prep evidence)
    assert model_job._read_claims() != {}  # the claimed start card is still this start's to edit
    assert len(ad5x.zcolor()) == 1 and len(ad5x.uploads) == 1


def test_a_preheat_during_the_upload_refuses_before_the_start_is_sent(ad5x, tmp_path, monkeypatch):
    """#836: the idle guard proves the heaters were off before the upload, but the upload can take
    minutes. A HelixScreen preheat landing in that window must not be credited to this job: the
    targets are re-read immediately before the SET_ZCOLOR POST and the start is refused, unsent."""
    real_upload = model_job.subprocess.run

    def upload_then_preheat(cmd, *args, **kwargs):
        result = real_upload(cmd, *args, **kwargs)
        if len(cmd) >= 4 and cmd[1] == "upload":
            ad5x.heat(nozzle=210, bed=0)  # a preheat tapped on HelixScreen while the file uploads
        return result
    monkeypatch.setattr(model_job.subprocess, "run", upload_then_preheat)
    ad5x.script_hook = lambda fake, script: 0
    job, _ = make_job(tmp_path)

    ok, tail = run_print_now(job)

    status, message = intake_outcome(ok, tail)
    assert status == "START_REFUSED", tail
    assert message.startswith("Didn't start"), tail
    assert ad5x.zcolor() == [] and len(ad5x.uploads) == 1  # the start was never sent
    assert leases() == [] and model_job._read_claims() == {}


def test_a_bang_bang_receipt_with_http_200_is_a_refusal(ad5x, tmp_path):
    """#836: Klipper answers a failed macro with HTTP 200 and a `!!` line in the console history.
    That is a refusal naming the `!!` text, not a pending start."""
    def hook(fake, script):
        if not script.startswith("SET_ZCOLOR"):
            return 0
        fake.gcode_store.append({"message": '!! Unknown command:"SET_ZCOLOR"', "time": time.time()})
        return 0  # Moonraker still answers 200
    ad5x.script_hook = hook
    job, gcode = make_job(tmp_path)

    ok, tail = run_print_now(job)

    status, message = intake_outcome(ok, tail)
    assert status == "START_REFUSED", tail
    assert '!! Unknown command:"SET_ZCOLOR"' in message, tail
    checkpoint = json.loads(gcode.with_suffix(".launch.json").read_text())
    assert checkpoint["phase"] == "rejected"
    assert leases() == []  # released: the printer refused, nothing is in flight
    assert len(ad5x.zcolor()) == 1 and len(ad5x.uploads) == 1  # exactly one POST, never resubmitted


def test_a_bang_line_logged_before_the_start_is_not_its_refusal(ad5x, tmp_path):
    """#848 Codex P1: a `!!` line from an unrelated console command, logged a moment before the
    SET_ZCOLOR POST, is in the store before the watermark is captured — it must not refuse a
    start the printer accepts and prints. (The old 5 s timestamp window blamed it.)"""
    ad5x.gcode_store.append({"message": "!! Must home axis first", "time": time.time()})
    ad5x.script_hook = printer_starts(after_s=2)
    job, _ = make_job(tmp_path)

    ok, tail = run_print_now(job)

    status, _message = intake_outcome(ok, tail)
    assert status == "STARTED", tail
    assert len(ad5x.zcolor()) == 1 and len(ad5x.uploads) == 1


# --- #690: the AD5X labels a Silk PLA slot SILK ------------------------------------------------------

def test_slot_two_labeled_silk_launches_with_silk_pla_in_spoolman(ad5x, tmp_path):
    slot = next(s for s in ad5x.status["zmod_color"]["slots"] if s["ID"] == "2")
    assert slot["Material"] == "SILK"  # as recorded from the printer
    assert model_job.printer_state.load()["filament_slots"]["2"]["material"] == "Silk PLA"
    ad5x.script_hook = printer_starts(after_s=2)
    job, _ = make_job(tmp_path)

    ok, tail = run_print_now(job)

    assert ok, tail
    assert intake_outcome(ok, tail)[0] == "STARTED"
    assert ad5x.zcolor() and ad5x.zcolor()[0].endswith("T0=2")
    assert "is labeled" not in tail


def test_a_slot_label_of_another_material_is_refused_and_named(ad5x, tmp_path):
    ad5x.set_slot(2, Material="PETG")
    job, _ = make_job(tmp_path)

    ok, tail = run_print_now(job)

    assert not ok
    status, message = intake_outcome(ok, tail)
    assert status == "START_REFUSED"
    assert message.startswith("Didn't start: printer slot 2 is labeled PETG but Spoolman has Silk PLA")
    assert ad5x.uploads == [] and ad5x.zcolor() == [] and leases() == []


# --- #691: ZMod reports X.gcode.3mf as 3mf_print/X.gcode, and a finished job reports no file ----------

@pytest.fixture
def delivers_3mf(ad5x, monkeypatch):
    from print_handy import settings
    monkeypatch.setitem(settings.current()["printer"], "zmod_delivery_format", "3mf")
    return ad5x


def test_an_uncertain_lease_from_a_finished_3mf_job_settles_from_history(delivers_3mf, tmp_path, capsys):
    fake = delivers_3mf
    fake.script_hook = printer_ignores_start(HOLD_S)  # the first start's response is lost and nothing is visible
    first, _ = make_job(tmp_path, "first", title="First Part")
    ok, tail = run_print_now(first)
    assert intake_outcome(ok, tail)[0] == "START_PENDING"
    (row,) = leases()
    assert row["phase"] == "uncertain" and row["remote"].endswith(".gcode.3mf")

    # The printer ran it and finished: it is idle, reports no file, and history holds 3mf_print/X.gcode.
    fake.add_finished_job(row["remote"])
    assert fake.status["print_stats"]["filename"] == ""
    assert fake.history[0]["filename"] == "3mf_print/" + row["remote"][:-len(".3mf")]
    fake.script_hook = printer_starts(after_s=2)

    second, _ = make_job(tmp_path, "second", title="Second Part")
    ok2, tail2 = run_print_now(second)

    assert ok2, tail2
    assert "LAUNCH RECONCILED" in tail2 and row["remote"] in tail2
    assert intake_outcome(ok2, tail2)[0] == "STARTED"
    # The first job was not resubmitted: one SET_ZCOLOR each, for two different files.
    names = [re.search(r"FILENAME=(\S+)", s).group(1) for s in fake.zcolor()]
    assert len(names) == 2 and names[0] == row["remote"] and names[1] != row["remote"]


def test_history_of_an_earlier_run_or_another_file_does_not_settle_the_lease(delivers_3mf, tmp_path):
    fake = delivers_3mf
    fake.script_hook = printer_ignores_start(HOLD_S)
    first, _ = make_job(tmp_path, "first", title="First Part")
    run_print_now(first)
    (row,) = leases()

    fake.add_finished_job(row["remote"], started=1.0)  # a reprint of the same file, long before this lease
    fake.add_finished_job("Some-Other-Part.gcode.3mf")  # another file, after it
    second, _ = make_job(tmp_path, "second", title="Second Part")
    ok, tail = run_print_now(second)

    assert not ok and "LAUNCH RECONCILED" not in tail
    status, message = intake_outcome(ok, tail)
    assert status == "START_REFUSED" and "an earlier start" in message
    assert len(fake.zcolor()) == 1


# --- #693: a failure before the start request is a refusal, not a start ------------------------------

def test_an_unconfirmed_slot_occupancy_reads_didnt_start(ad5x, tmp_path):
    import json
    record = json.loads(ad5x.record_path.read_text())
    record["filament_slots"]["2"]["physical_loading_verified"] = False
    ad5x.record_path.write_text(json.dumps(record))
    ad5x.set_slot(2, hasFilament=False)  # the feeder reports no filament, and the operator has not vouched for it
    job, _ = make_job(tmp_path)

    ok, tail = run_print_now(job)

    assert not ok
    assert tail.strip().splitlines()[-1] == "REFUSED: T0 slot 2 occupancy is unknown or unconfirmed. Nothing started."
    status, message = intake_outcome(ok, tail)
    assert status == "START_REFUSED" and message.startswith("Didn't start")
    assert "Do not send it again" not in message
    assert ad5x.uploads == [] and ad5x.zcolor() == [] and leases() == []


def test_a_failed_upload_reads_didnt_start_and_releases_the_lease(ad5x, tmp_path):
    ad5x.upload_fails = True
    job, _ = make_job(tmp_path)

    ok, tail = run_print_now(job)

    assert not ok
    status, message = intake_outcome(ok, tail)
    assert status == "START_REFUSED" and message.startswith("Didn't start")
    assert ad5x.zcolor() == [] and leases() == []
    assert not launch_state.submitted()


def test_a_bang_line_logged_while_a_held_open_start_prints_is_not_its_refusal(ad5x, tmp_path):
    """A held-open SET_ZCOLOR POST spans the printer's preparation; a `!!` from anything logged
    meanwhile must not refuse (and release the lease of) a start the printer is already printing."""
    def hook(fake, script):
        if not script.startswith("SET_ZCOLOR"):
            return 0
        remote = re.search(r"FILENAME=(\S+)", script).group(1)
        fake.after(1, fake.heat)
        fake.after(2, lambda: fake.start_printing(remote))
        fake.after(3, lambda: fake.gcode_store.append({"message": "!! some unrelated macro warning",
                                                       "time": time.time()}))
        return 5
    ad5x.script_hook = hook
    job, _ = make_job(tmp_path)

    ok, tail = run_print_now(job)

    status, _message = intake_outcome(ok, tail)
    assert status == "STARTED", tail
    assert leases() == []
    assert len(ad5x.zcolor()) == 1
