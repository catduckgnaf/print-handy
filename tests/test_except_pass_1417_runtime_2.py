import ast
from pathlib import Path

FILES = ['discord_print_summary.py', 'panic.py', 'moonraker_discord_announcements.py', 'print_control.py', 'print_records.py', 'roast_chat.py', 'starter_pings.py', 'stop_capture.py']

def test_assigned_runtime_files_have_no_except_pass():
    root = Path(__file__).parents[1] / "print_handy" / "runtime"
    found = []
    for name in FILES:
        tree = ast.parse((root / name).read_text(), filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                found.append(f"{name}:{node.lineno}")
    assert not found, found


def test_force_restart_dropped_connections_log_info_not_warning(caplog):
    """firmware_restart and REBOOT drop the connection by design (duckcatgnaf #1432)."""
    import logging
    from unittest import mock

    from print_handy.runtime import panic

    def caller(url, method="GET", data=None, **kw):
        if url in ("/printer/firmware_restart", "/printer/gcode/script"):
            raise ConnectionError("connection dropped")
        return {}

    caplog.set_level(logging.DEBUG, logger=panic.__name__)
    with mock.patch("time.sleep", return_value=None), \
         mock.patch("print_handy.core.clear_job.safe_idle", return_value=True):
        res = panic.force_restart(
            caller=caller,
            status_reader=lambda: {"klippy": "shutdown", "state": "error"},
            moonraker_reader=lambda: {},
            native_reader=lambda: {},
            wait_s=0.05, poll_s=0.01, reboot_grace_s=0, reboot_wait_s=5.0,
        )
    assert res["ok"] is True
    assert "reboot_macro" in res["steps"]
    records = [r for r in caplog.records if r.name == panic.__name__]
    messages = [r.getMessage() for r in records if r.levelno == logging.INFO]
    assert any("firmware_restart" in m for m in messages)
    assert any("REBOOT" in m for m in messages)
    assert not any(r.levelno >= logging.WARNING and "ended without a reply" in r.getMessage() for r in records)
