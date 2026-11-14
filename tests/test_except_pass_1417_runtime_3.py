import ast
from pathlib import Path

FILES = ['moonraker_event_listener.py', '_config.py', 'camera_watch.py', 'family_event_listener.py', 'nozzle_change.py', 'print_alert_buttons.py', 'print_stop.py', 'start_claims.py', 'timelapse_encode.py', 'visual_watch.py', 'watchdog_events.py']

def test_assigned_runtime_files_have_no_except_pass():
    root = Path(__file__).parents[1] / "print_handy" / "runtime"
    found = []
    for name in FILES:
        tree = ast.parse((root / name).read_text(), filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                found.append(f"{name}:{node.lineno}")
    assert not found, found


def test_clear_shutdown_dropped_restart_logs_info_not_warning(caplog):
    """The firmware restart drops the connection by design (duckcatgnaf #1433)."""
    import logging
    from unittest import mock

    from print_handy.runtime import print_alert_buttons as pab

    def caller(path, method="GET", timeout=10):
        raise ConnectionError("connection dropped")

    caplog.set_level(logging.DEBUG, logger=pab.__name__)
    with mock.patch.object(pab.time, "sleep", return_value=None):
        ok, _msg = pab.clear_shutdown(wait_s=5, poll_s=0, caller=caller, reader=lambda: {"klippy": "ready"})
    assert ok is True
    records = [r for r in caplog.records if r.name == pab.__name__]
    assert any(r.levelno == logging.INFO and "firmware_restart" in r.getMessage() for r in records)
    assert not any(r.levelno >= logging.WARNING for r in records)


def test_prune_frames_tolerates_a_frame_removed_by_someone_else(tmp_path, caplog, monkeypatch):
    """A frame already gone is the goal of pruning, not a warning (duckcatgnaf #1433)."""
    import logging
    import os
    from pathlib import Path as _P

    from print_handy.runtime import visual_watch

    old = tmp_path / "old.jpg"
    old.write_bytes(b"x")
    os.utime(old, (1, 1))
    real_unlink = _P.unlink

    def racing_unlink(self, missing_ok=False):
        real_unlink(self)  # another process wins the race first
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(_P, "unlink", racing_unlink)
    caplog.set_level(logging.DEBUG, logger=visual_watch.__name__)
    removed = visual_watch.prune_frames(tmp_path, keep=0, max_age_s=10, now=100)
    assert removed == [old]
    assert not old.exists()
    assert not any(r.levelno >= logging.WARNING for r in caplog.records if r.name == visual_watch.__name__)
