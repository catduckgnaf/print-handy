"""A print that ends while the listener is down still gets one finish post.

Seen live: a print ended while the listener was restarting (state None ->
printing, then None -> standby with no completion post), and an overnight
listener hang lost the finish posts of every print in between.
"""
import time

_REAL_TIME = time.time

from print_handy.runtime import moonraker_discord_announcements as watcher
from print_handy.runtime import print_records as records


def _job(path, state, completion):
    file_info = {"path": path, "display": path, "name": path} if path else {"name": None}
    return {"job": {"file": file_info}, "progress": {"completion": completion},
            "state": state, "stats": {"state": state.lower()}}


def _setup(tmp_path, monkeypatch, responses, history):
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


def test_finish_while_the_listener_was_down_is_posted_once(tmp_path, monkeypatch, capsys):
    ended = time.time() - 20 * 60
    responses = iter([_job("part.gcode", "Printing", 40.0)] + [_job("", "Operational", None)] * 3)
    monkeypatch.setattr(time, "time", lambda real=time.time: real() - 3600)  # first seen an hour ago
    history = {"result": {"jobs": [{"job_id": "0001A", "filename": "part.gcode", "status": "completed",
                                    "start_time": ended - 3600, "end_time": ended,
                                    "print_duration": 3500}]}}
    _setup(tmp_path, monkeypatch, responses, history)

    watcher.main()
    capsys.readouterr()
    monkeypatch.setattr(time, "time", _REAL_TIME)
    watcher.main()  # the listener comes back: the printer is idle, the job is gone
    out = capsys.readouterr().out
    assert "complete: part.gcode" in out
    assert f"Finished at {time.strftime('%H:%M', time.localtime(ended))} while the bot was offline." in out
    # Retryable until delivered, then never again.
    assert watcher.pending_completion_message(watcher.load_state()) == out.strip()
    watcher.clear_pending_completion_message(watcher.load_state())
    for _ in range(2):
        watcher.main()
        assert capsys.readouterr().out == ""


def test_every_print_lost_to_a_listener_hang_is_posted(tmp_path, monkeypatch, capsys):
    now = time.time()
    responses = iter([_job("tray.gcode", "Operational", None)])
    history = {"result": {"jobs": [
        {"job_id": "3", "filename": "tray.gcode", "status": "cancelled", "end_time": now - 600},
        {"job_id": "2", "filename": "upper.gcode", "status": "completed", "end_time": now - 7200},
        {"job_id": "1", "filename": "panel.gcode", "status": "completed", "end_time": now - 20000},
        {"job_id": "0", "filename": "old.gcode", "status": "completed", "end_time": now - 90000},
    ]}}
    _setup(tmp_path, monkeypatch, responses, history)
    state = {"job_key": "panel.gcode", "printing": True, "print_started_at": now - 30000}
    watcher.save_state(state)
    watcher.main()
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert [line for line in lines if ": " in line and "offline" not in line] == [
        "complete: panel.gcode", "complete: upper.gcode", "cancelled: tray.gcode"]
    assert "old.gcode" not in out


def test_a_live_stop_is_left_to_the_listener(tmp_path, monkeypatch, capsys):
    """A stop seen live runs the announcer within seconds: no offline post."""
    now = time.time()
    responses = iter([_job("stand.gcode", "Printing", 40.0), _job("", "Operational", None)])
    history = {"result": {"jobs": [{"job_id": "9", "filename": "stand.gcode", "status": "cancelled",
                                    "end_time": now - 2}]}}
    _setup(tmp_path, monkeypatch, responses, history)
    monkeypatch.setattr(watcher, "_PROCESS_STARTED_AT", now - 3600, raising=False)
    watcher.main()
    watcher.main()
    assert "offline" not in capsys.readouterr().out
    assert not watcher.pending_completion_message(watcher.load_state())
