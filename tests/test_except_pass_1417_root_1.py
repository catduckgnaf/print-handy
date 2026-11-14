"""#1417 package-root modules: no silent except-pass, and each changed path behaves."""
import ast
import io
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "print_handy"
FILES = ["crash_report.py", "credential_binding.py", "debug_mode.py", "diagnostics.py",
         "firmware_update.py", "healthcheck.py", "logs.py", "setup_state.py", "setup_wizard.py"]


@pytest.mark.parametrize("name", FILES)
def test_no_silent_except_pass(name):
    tree = ast.parse((ROOT / name).read_text())
    silent = [n.lineno for n in ast.walk(tree)
              if isinstance(n, ast.ExceptHandler) and len(n.body) == 1 and isinstance(n.body[0], ast.Pass)]
    assert silent == [], f"{name}: silent except-pass at lines {silent}"


def test_undeclared_category_falls_through_without_raising():
    from print_handy import diagnostics

    class Odd(Exception):
        diagnostic_category = "not-a-category"

    # The bad declaration is ignored; the generic rules still classify it.
    assert diagnostics.classify(Odd("x")) == diagnostics.classify(Exception("x"))

    class Known(Exception):
        diagnostic_category = diagnostics.Category.CONFIGURATION.value

    assert diagnostics.classify(Known("x")) is diagnostics.Category.CONFIGURATION


@pytest.mark.parametrize("value, ok", [("2.5", True), ("0", False), ("-1", False), ("abc", False),
                                       ("nan", False), ("inf", True)])
def test_setup_positive_validator(value, ok):
    from print_handy import setup_wizard
    assert (setup_wizard._positive(value) is None) is ok


def test_log_prune_reports_an_undeletable_file_on_stderr(tmp_path, monkeypatch, capsys):
    from print_handy import logs
    target = tmp_path / "print-handy.log"
    old = tmp_path / "print-handy-2020-01-01.log"
    old.write_text("x")

    def refuse(self, missing_ok=False):
        raise PermissionError("read-only")

    monkeypatch.setattr(type(old), "unlink", refuse)
    assert logs.prune(target, 7, today="2026-11-14") == []
    assert "cannot remove old log print-handy-2020-01-01.log" in capsys.readouterr().err


def test_log_prune_counts_a_file_another_process_already_removed(tmp_path, monkeypatch):
    from print_handy import logs
    target = tmp_path / "print-handy.log"
    old = tmp_path / "print-handy-2020-01-01.log"
    old.write_text("x")
    monkeypatch.setattr(logs, "dated_files", lambda path: [("2020-01-01", old)])
    old.unlink()  # gone before prune reaches it
    assert logs.prune(target, 7, today="2026-11-14") == [old]


def test_debug_clear_state_is_quiet_when_already_cleared(tmp_path, monkeypatch, caplog):
    from print_handy import debug_mode
    monkeypatch.setattr(debug_mode, "state_path", lambda data=None: tmp_path / "absent.json")
    with caplog.at_level(logging.WARNING, logger=debug_mode.logger.name):
        debug_mode._clear_state()
    assert caplog.records == []


def test_crash_hook_reports_a_failing_previous_hook_on_stderr(monkeypatch, capsys):
    from print_handy import crash_report

    def broken(*args):
        raise RuntimeError("host hook broke")

    monkeypatch.setattr(sys, "excepthook", broken)
    monkeypatch.setattr(crash_report, "_installed_roles", set())
    monkeypatch.setattr(crash_report, "record", lambda *a, **kw: None)
    assert crash_report.install("test-role") is True
    sys.excepthook(ValueError, ValueError("boom"), None)  # must not raise
    assert "the previous sys.excepthook failed: RuntimeError: host hook broke" in capsys.readouterr().err


def test_signal_note_never_raises_when_stderr_is_closed(monkeypatch):
    from print_handy import healthcheck

    def closed(fd, data):
        raise OSError(9, "Bad file descriptor")

    monkeypatch.setattr(healthcheck.os, "write", closed)
    healthcheck._signal_note("x", RuntimeError("y"))  # must not raise


def test_firmware_refresh_failure_falls_back_to_the_plain_read(monkeypatch, caplog):
    from print_handy import firmware_update as fw
    calls = []

    def transport(method, path, *a, **kw):
        calls.append(method)
        if method == "POST":
            raise ConnectionError("refresh refused")
        raise fw.FirmwareError("plain read reached")

    with caplog.at_level(logging.INFO, logger=fw.logger.name), pytest.raises(fw.FirmwareError, match="plain read reached"):
        fw.fetch_status(transport, refresh=True)
    assert calls[:2] == ["POST", "GET"]
    assert any("refresh request failed" in r.getMessage() for r in caplog.records)
