"""A print cancelled while the listener was down, still the printer's job on reconnect, is posted.

2.3.2 review (finding 17): the job key did not change, so the missed-finish check
never ran, and the listener's cancel post needs a live transition it never saw.
The announcer now posts the cancel from history once, and only when it ended
before this process started; a live cancel stays the listener's.
"""
import time

from print_handy.runtime import moonraker_discord_announcements as watcher
from print_handy.runtime import print_records as records

FILE = "lantern.gcode"


def _job(state, native):
    return {"job": {"file": {"path": FILE, "display": FILE, "name": FILE}}, "progress": {"completion": 41.0},
            "state": state, "stats": {"state": native}}


def _setup(tmp_path, monkeypatch, responses, history, started_at):
    monkeypatch.setattr(records, "PRINTS_DIR", tmp_path / "Prints")
    monkeypatch.setattr(watcher, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setenv("PRINT_HANDY_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setattr(watcher, "get_job", lambda: next(responses))
    monkeypatch.setattr(watcher, "get_json",
                        lambda path, timeout=10: history if path.startswith("/server/history") else {})
    monkeypatch.setattr(watcher, "get_active_spool", lambda: {})
    monkeypatch.setattr(watcher, "get_file_metadata", lambda filename: {})
    monkeypatch.setattr(watcher.print_summary, "format_event",
                        lambda kind, filename, **_kw: f"{kind}: {filename}")
    monkeypatch.setattr(watcher, "_PROCESS_STARTED_AT", started_at, raising=False)


def _history(ended):
    return {"result": {"jobs": [{"job_id": "00A1", "filename": FILE, "status": "cancelled",
                                 "start_time": ended - 1800, "end_time": ended, "print_duration": 1700}]}}


def test_a_cancel_missed_while_down_is_posted_once_on_reconnect(tmp_path, monkeypatch, capsys):
    now = time.time()
    ended = now - 15 * 60
    responses = iter([_job("Printing", "printing")] + [_job("Cancelled", "cancelled")] * 3)
    _setup(tmp_path, monkeypatch, responses, _history(ended), started_at=now - 3600)
    watcher.main()                                     # the earlier process saw it printing
    capsys.readouterr()
    monkeypatch.setattr(watcher, "_PROCESS_STARTED_AT", now - 60, raising=False)   # restarted after the cancel
    watcher.main()
    out = capsys.readouterr().out
    assert "cancelled: lantern.gcode" in out
    assert f"Cancelled at {time.strftime('%H:%M', time.localtime(ended))} while the bot was offline." in out
    assert watcher.pending_completion_message(watcher.load_state()) == out.strip()
    watcher.clear_pending_completion_message(watcher.load_state())
    for _ in range(2):
        watcher.main()
        assert capsys.readouterr().out == ""


def test_a_cancel_seen_live_is_left_to_the_listener(tmp_path, monkeypatch, capsys):
    now = time.time()
    responses = iter([_job("Printing", "printing"), _job("Cancelled", "cancelled")])
    _setup(tmp_path, monkeypatch, responses, _history(now - 2), started_at=now - 3600)
    watcher.main()
    watcher.main()
    assert "offline" not in capsys.readouterr().out
    assert not watcher.pending_completion_message(watcher.load_state())
