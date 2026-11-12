"""Print-lifecycle failure injection (P1-1): one harness, scripted failures.

Each scenario runs a real lifecycle seam from the repo's own code — the
``print_now`` start guard, the announcer's completion pass, the listener's
pending-completion delivery, the announcer state file a restart reloads —
with one dependency scripted to fail at the worst moment, and asserts the
user-visible outcome is NAMED: a REFUSED line, a pending marker in the
record, a retained pending post. Never a silent drop, never a job left
believed-running, never an exception type the flow does not name.

The seams are the deepest the existing fakes honestly support: Moonraker
is scripted at model_job's ``_mr`` transport (the pattern the pipeline
suite uses), Spoolman at the real socket seam (``urllib.request.urlopen``,
the #235/#387 pattern), and Discord at ``bot.rest``'s opener and at the
listener's one REST post path. The docker fake Moonraker
(``tests/docker/fake_moonraker.py``) only ever reports an idle printer and
never accepts a print, so it cannot stage these moments; where a scenario
could not be staged end to end, the test says so at its seam.

Known gap, deliberately NOT asserted here (candidate bonus find): with
Moonraker fully offline, ``print_now``'s idle guard itself raises the raw
``urllib.error.URLError`` out of ``_printer_phase`` — nothing is uploaded
or started, but the failure is an unnamed transport traceback, not the
REFUSED outcome the busy guard produces. Scenario 1 therefore pins the
two start-guard refusals that ARE named today (busy printer, and the
Moonraker active-spool read that refuses a launch when Moonraker does
not answer).
"""
import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import pytest

from print_handy.bot import rest
from print_handy.core import model_job, spoolman_client
from print_handy.runtime import moonraker_discord_announcements as watcher
from print_handy.runtime import moonraker_event_listener as listener
from print_handy.runtime import print_records as records

DEAD = "http://127.0.0.1:9"  # nothing ever connects; the socket seam is faked below
REFUSED = urllib.error.URLError(OSError(111, "Connection refused"))


@pytest.fixture(autouse=True)
def _fresh_notice():
    spoolman_client.reset_notice()
    yield
    spoolman_client.reset_notice()


def urlopen_raising(exc):
    def fake(req, *args, **kwargs):
        raise exc
    return fake


def _gcode(tmp_path):
    path = tmp_path / "x.gcode"
    path.write_text("; estimated printing time (normal mode) = 1h 2m 0s\n"
                    "; total filament used [g] = 12.3\n"
                    "START_PRINT EXTRUDER_TEMP=220 BED_TEMP=60 Z_OFFSET=-0.25\n")
    return path


def _moonraker_status(state, filename):
    return {"result": {"status": {
        "save_variables": {"variables": {"display_off": 1}},
        "print_stats": {"state": state, "filename": filename},
        "idle_timeout": {"state": "Printing" if state == "printing" else "Ready"},
        "virtual_sdcard": {"is_active": state == "printing", "file_path": filename},
        "webhooks": {"state": "ready"}}}}


# ------------------------------------------- scenario 1: start guard failures

def test_start_guard_refuses_a_busy_printer_and_uploads_nothing(tmp_path, monkeypatch):
    """The printer started something else between slicing and the start
    guard: print_now must end in the named REFUSED outcome, with no upload
    attempted and no verification spent on a print that will not start."""
    uploads = []
    verified = []
    monkeypatch.setattr(model_job.printer_state, "load",
                        lambda printer_id=None: {"moonraker_url": "http://printer.test:7125"})
    monkeypatch.setattr(model_job, "_mr",
                        lambda st, path, **kw: _moonraker_status("printing", "other.gcode"))
    monkeypatch.setattr(model_job, "verify_gcode", lambda *a, **kw: verified.append(a))
    monkeypatch.setattr(model_job.subprocess, "run",
                        lambda *a, **kw: uploads.append(a) or mock.Mock(returncode=0, stdout="{}", stderr=""))
    with pytest.raises(SystemExit) as caught:
        model_job.print_now(str(_gcode(tmp_path)))
    assert str(caught.value) == \
        "REFUSED: printer is busy (printing, other.gcode). Nothing uploaded or started."
    assert uploads == [], "an upload was attempted after the guard refused"
    assert verified == [], "verification ran even though the guard refused the start"


def test_start_guard_refuses_when_moonraker_does_not_answer(tmp_path, monkeypatch):
    """Moonraker offline at the launch's active-spool read: Moonraker is a
    launch dependency (#387), so the launch is refused with the named
    outcome and no spool record is written for a print that never started."""
    st = {"moonraker_url": "http://printer.test:7125",
          "filament_slots": {"2": {"spoolman_id": 11, "material": "PLA", "name": "Pink PLA"}},
          "spool": {"spoolman_id": 10}}
    saved = []
    monkeypatch.setattr(model_job.printer_state, "save", lambda fresh: saved.append(fresh))
    with mock.patch.object(model_job, "_mr", side_effect=REFUSED):
        with pytest.raises(RuntimeError) as caught:
            model_job._sync_active_spool(st, 2)
    assert "Moonraker active spool read failed" in str(caught.value)
    assert "nothing uploaded or started" in str(caught.value)
    assert saved == [], "a spool record was written for a refused launch"


# ------------------------------ scenario 2: Spoolman down on the completion path

def _stage_announcer(tmp_path, monkeypatch, responses, spoolman_base=None):
    monkeypatch.setattr(records, "PRINTS_DIR", tmp_path / "Prints")
    monkeypatch.setattr(records, "PLANS_DIR", tmp_path / "Plans")
    monkeypatch.setattr(watcher, "STATE_PATH", tmp_path / "announcement.json")
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps({"jobs": {}}))
    monkeypatch.setenv("PRINT_HANDY_CONTEXT_PATH", str(context_path))
    monkeypatch.setattr(watcher, "get_job", lambda: next(responses))
    monkeypatch.setattr(watcher, "_queued_jobs", lambda: [])
    monkeypatch.setattr(watcher, "get_file_metadata", lambda filename: {})
    if spoolman_base is not None:
        monkeypatch.setattr(watcher, "SPOOLMAN_BASE", spoolman_base)


def _printing_then_complete(filename="benchy.gcode"):
    row = {"path": filename, "display": filename}
    return iter((
        {"job": {"file": row}, "progress": {"completion": 25}, "state": "Printing",
         "stats": {"state": "printing"}},
        {"job": {"file": row}, "progress": {"completion": 100}, "state": "Completed",
         "stats": {"state": "complete", "print_duration": 30, "filament_used": 1200}},
    ))


def test_completion_with_spoolman_down_writes_the_record_marked_pending(tmp_path, monkeypatch, capsys):
    """Spoolman dies before the completion/usage path: the completion is
    NOT lost — the announcer still emits it, files it as the pending post,
    and writes the completion record with the usage marked pending for
    later reconciliation (#387), naming the spool Moonraker reported."""
    _stage_announcer(tmp_path, monkeypatch, _printing_then_complete(), spoolman_base=DEAD)
    monkeypatch.setattr(watcher, "get_json",
                        lambda path: {"result": {"spool_id": 7}} if "spoolman" in path
                        else {"result": {"jobs": []}})
    monkeypatch.setattr(urllib.request, "urlopen", urlopen_raising(REFUSED))
    assert watcher.main() == 0
    capsys.readouterr()
    assert watcher.main() == 0
    out = capsys.readouterr().out
    assert "Print complete" in out, "the completion post itself was lost with Spoolman"
    state = json.loads((tmp_path / "announcement.json").read_text())
    assert state["record_written"] is True
    pending = watcher.pending_completion_message(state)
    assert pending and "Print complete" in pending
    record_files = list((tmp_path / "Prints").glob("*.json"))
    assert len(record_files) == 1
    record = json.loads(record_files[0].read_text())
    assert record["status"] == "completed"
    assert record["spoolman_spool_id"] == 7
    assert record["spoolman_usage_pending"] is True, \
        "usage that could not be reported was silently dropped instead of marked pending"


# --------------------------- scenario 3: Discord REST failure on the terminal post

class _Response:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _http_error(status, message):
    return urllib.error.HTTPError(rest.API + "/channels/777/messages", status, message, {},
                                  io.BytesIO(b'{"message": "scripted failure"}'))


@pytest.mark.parametrize("status,message", [(429, "Too Many Requests"),
                                            (500, "Internal Server Error"),
                                            (503, "Service Unavailable")])
def test_rest_send_names_a_transient_discord_failure(status, message):
    """A 429/5xx on the terminal post comes back as a named (False, detail)
    result — the caller can retain and retry the post — never an exception
    escaping the send, and never a fake success."""
    def opener(req, timeout=None):
        raise _http_error(status, message)
    ok, detail = rest.send("token", "777", "Print complete: Benchy", opener=opener)
    assert ok is False
    assert str(status) in detail and "scripted failure" in detail


def test_rest_send_delivers_once_discord_recovers():
    attempts = []

    def opener(req, timeout=None):
        attempts.append(req)
        if len(attempts) == 1:
            raise _http_error(429, "Too Many Requests")
        return _Response(b'{"id": "424242"}')

    first = rest.send("token", "777", "Print complete: Benchy", opener=opener)
    second = rest.send("token", "777", "Print complete: Benchy", opener=opener)
    assert first[0] is False and "429" in first[1]
    assert second == (True, "424242")
    assert len(attempts) == 2
    assert "/channels/777/messages" in attempts[0].full_url
    assert json.loads(attempts[0].data)["content"] == "Print complete: Benchy"


def test_failed_terminal_post_stays_pending_and_is_retried_next_pass(tmp_path, monkeypatch):
    """The listener's give-up is a retained queue entry, not silence: while
    Discord answers 429 the pending completion stays in the persisted
    state untouched; the next pass delivers the same text exactly once
    and acknowledges it."""
    announcer = listener.announcer
    monkeypatch.setattr(announcer, "STATE_PATH", tmp_path / "announcement.json")
    monkeypatch.setattr(announcer, "main", lambda: None)
    monkeypatch.setattr(listener, "log", lambda text: None)
    monkeypatch.setattr(listener, "_bot_token", lambda: "token")
    announcer.save_state({"job_key": "benchy.gcode",
                          "pending_completion_message": "Print complete: Benchy"})
    results = iter([(False, "HTTP Error 429: Too Many Requests"), (True, "999")])
    sent = []
    monkeypatch.setattr(rest, "send",
                        lambda token, chat, text, **kw: sent.append(text) or next(results))
    listener.run_announcer("failure-injection")
    state = announcer.load_state()
    assert announcer.pending_completion_message(state) == "Print complete: Benchy", \
        "a failed terminal post was dropped from the pending state"
    assert not state.get("announced_completion")
    listener.run_announcer("retry")
    state = announcer.load_state()
    assert not announcer.pending_completion_message(state)
    assert state["announced_completion"] is True
    assert sent == ["Print complete: Benchy", "Print complete: Benchy"]


# --------------------- scenario 4: listener restart reconciles the running job

def test_restart_reconciles_a_print_that_completed_while_the_listener_was_down(
        tmp_path, monkeypatch, capsys):
    """The state file says a print was running when the listener stopped;
    the first pass after the restart reads the printer's reported state
    (complete) and lands exactly there: completion emitted, record
    written, completion filed as the pending post — the job is neither
    dropped nor left believed-running."""
    _stage_announcer(tmp_path, monkeypatch, iter((
        {"job": {"file": {"path": "benchy.gcode", "display": "benchy.gcode"}},
         "progress": {"completion": 100}, "state": "Completed",
         "stats": {"state": "complete", "print_duration": 30, "filament_used": 1200}},)))
    monkeypatch.setattr(watcher, "get_active_spool", lambda: {})
    # What the pre-restart listener persisted: this file, mid-print.
    watcher.save_state({"job_key": "benchy.gcode", "printing": True, "completion": 42.0,
                        "announced_completion": False, "record_written": False,
                        "print_started_at": 111.0})
    assert watcher.main() == 0
    out = capsys.readouterr().out
    assert "Print complete" in out
    state = json.loads((tmp_path / "announcement.json").read_text())
    assert state["printing"] is False, "the reconciled job is still believed running"
    assert state["record_written"] is True
    assert "Print complete" in watcher.pending_completion_message(state)
    record = json.loads(next((tmp_path / "Prints").glob("*.json")).read_text())
    assert record["status"] == "completed"


def test_restart_with_moonraker_down_keeps_the_running_job(tmp_path, monkeypatch, capsys):
    """A restart that cannot reach Moonraker at all names the failure on
    stderr and changes nothing: the persisted running job survives for
    the next pass instead of being finalized or dropped on no evidence."""
    _stage_announcer(tmp_path, monkeypatch, iter(()))
    monkeypatch.setattr(watcher, "get_job",
                        mock.Mock(side_effect=OSError("Moonraker unreachable")))
    watcher.save_state({"job_key": "benchy.gcode", "printing": True, "completion": 42.0,
                        "announced_completion": False, "record_written": False,
                        "print_started_at": 111.0})
    before = (tmp_path / "announcement.json").read_bytes()
    assert watcher.main() == 0
    assert "Moonraker announcement check failed" in capsys.readouterr().err
    assert (tmp_path / "announcement.json").read_bytes() == before
    assert json.loads(before)["printing"] is True
    assert not (tmp_path / "Prints").exists() or not list((tmp_path / "Prints").glob("*.json"))
