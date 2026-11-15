"""#1467: the broad handlers on the launch, purge and gate decision paths.

Each one used to catch ``Exception`` and return a default with no log, so a
typo-level bug in the try body was indistinguishable from "printer
unreachable". Now each either logs the cause at a level, or catches only the
transport and payload errors it means to catch — and a narrowed catch lets a
programming error raise instead of silently settling a launch decision,
which is why every narrowing below has its own propagation test.
"""
import logging
import urllib.error

import pytest

from print_handy.core import launch_state, printer_state, purge
from print_handy.launch import gates


def _warnings(caplog, logger):
    return [r for r in caplog.records if r.name == logger and r.levelno == logging.WARNING]


# --------------------------------------------------------------- printer_state

def test_read_purge_mode_logs_an_unreachable_printer(monkeypatch, caplog):
    monkeypatch.setattr(printer_state, "_get", lambda url: (_ for _ in ()).throw(OSError("down")))
    with caplog.at_level(logging.WARNING, logger="print_handy.core.printer_state"):
        assert printer_state.read_purge_mode({"moonraker_url": "http://printer.test:7125"}) is None
    assert _warnings(caplog, "print_handy.core.printer_state")
    assert "purge mode" in caplog.text and "OSError" in caplog.text


def test_read_purge_mode_logs_a_malformed_answer(monkeypatch, caplog):
    monkeypatch.setattr(printer_state, "_get", lambda url: {"unexpected": True})
    with caplog.at_level(logging.WARNING, logger="print_handy.core.printer_state"):
        assert printer_state.read_purge_mode({"moonraker_url": "http://printer.test:7125"}) is None
    assert _warnings(caplog, "print_handy.core.printer_state")


def test_read_purge_mode_lets_a_programming_error_raise(monkeypatch):
    # Narrowed catch: a bug in the request path is not "printer unreachable"
    # and must not feed the launch gate a silent None.
    monkeypatch.setattr(printer_state, "_get", lambda url: (_ for _ in ()).throw(RuntimeError("bug")))
    with pytest.raises(RuntimeError, match="bug"):
        printer_state.read_purge_mode({"moonraker_url": "http://printer.test:7125"})


# ---------------------------------------------------------------------- purge

def test_printer_id_for_logs_a_settings_failure(caplog):
    with caplog.at_level(logging.WARNING, logger="print_handy.core.purge"):
        assert purge.printer_id_for({}, {"no_roster_here": True}) is None
    assert _warnings(caplog, "print_handy.core.purge")
    assert "printer id" in caplog.text


def test_read_cache_missing_file_is_the_quiet_empty_case(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(purge, "_cache_path", lambda printer_id, data=None: tmp_path / "none.json")
    with caplog.at_level(logging.WARNING, logger="print_handy.core.purge"):
        assert purge.read_cache("ad5x") == {}
    assert _warnings(caplog, "print_handy.core.purge") == []


def test_read_cache_logs_a_corrupt_cache(tmp_path, monkeypatch, caplog):
    cache = tmp_path / "zmod_detection.json"
    cache.write_text("this is not json")
    monkeypatch.setattr(purge, "_cache_path", lambda printer_id, data=None: cache)
    with caplog.at_level(logging.WARNING, logger="print_handy.core.purge"):
        assert purge.read_cache("ad5x") == {}
    assert _warnings(caplog, "print_handy.core.purge")
    assert "cached purge detection" in caplog.text


def test_read_cache_lets_a_programming_error_raise(monkeypatch):
    def broken(printer_id, data=None):
        raise RuntimeError("bug")
    monkeypatch.setattr(purge, "_cache_path", broken)
    with pytest.raises(RuntimeError, match="bug"):
        purge.read_cache("ad5x")


def test_refresh_best_effort_logs_a_refresh_failure(monkeypatch, caplog):
    monkeypatch.setattr(purge, "refresh", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with caplog.at_level(logging.WARNING, logger="print_handy.core.purge"):
        assert purge.refresh_best_effort({}) == {}
    assert _warnings(caplog, "print_handy.core.purge")
    assert "refresh failed" in caplog.text


def test_status_row_logs_a_resolve_failure(monkeypatch, caplog):
    monkeypatch.setattr(purge, "resolve", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with caplog.at_level(logging.WARNING, logger="print_handy.core.purge"):
        assert purge.status_row("ad5x") is None
    assert _warnings(caplog, "print_handy.core.purge")
    assert "purge plan" in caplog.text


# --------------------------------------------------------------- launch_state

def _held_row():
    return {"remote": "Bag-Clip_plate_1_bf4e.gcode.3mf", "leased_at": "2026-10-30T19:21:32-0400"}


def test_reached_printer_logs_an_unreadable_history(caplog):
    def broken():
        raise OSError("moonraker down")
    with caplog.at_level(logging.WARNING, logger="print_handy.core.launch_state"):
        assert launch_state.reached_printer(_held_row(), "", broken) is False
    assert _warnings(caplog, "print_handy.core.launch_state")
    assert "job history" in caplog.text


def test_capture_baseline_logs_an_unreadable_history(caplog):
    def broken():
        raise OSError("moonraker down")
    with caplog.at_level(logging.WARNING, logger="print_handy.core.launch_state"):
        assert launch_state._capture_baseline(broken) == []
    assert _warnings(caplog, "print_handy.core.launch_state")
    assert "baseline" in caplog.text


def test_printer_clock_skew_logs_an_unreachable_printer(monkeypatch, caplog):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down")))
    with caplog.at_level(logging.WARNING, logger="print_handy.core.launch_state"):
        assert launch_state.printer_clock_skew("http://printer.test:7125") is None
    assert _warnings(caplog, "print_handy.core.launch_state")
    assert "printer clock" in caplog.text


def test_printer_clock_skew_lets_a_programming_error_raise(monkeypatch):
    # Narrowed catch: only transport and Date-header parse failures mean
    # "clock unknown"; a bug in the request path still raises.
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    with pytest.raises(RuntimeError, match="bug"):
        launch_state.printer_clock_skew("http://printer.test:7125")


# ---------------------------------------------------------------------- gates

def test_screen_z_offset_logs_an_unreachable_printer(monkeypatch, caplog):
    monkeypatch.setattr(gates, "_native", lambda: True)
    monkeypatch.setattr(gates, "_mr", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    with caplog.at_level(logging.WARNING, logger="print_handy.launch.gates"):
        assert gates.screen_z_offset({"moonraker_url": "http://printer.test:7125"}) is None
    assert _warnings(caplog, "print_handy.launch.gates")
    assert "screen offset" in caplog.text


def test_screen_z_offset_lets_a_programming_error_raise(monkeypatch):
    # Narrowed catch: a bug in the request path is not "printer unreachable"
    # and must not silently settle the mesh gate as unreadable.
    monkeypatch.setattr(gates, "_native", lambda: True)
    monkeypatch.setattr(gates, "_mr", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    with pytest.raises(RuntimeError, match="bug"):
        gates.screen_z_offset({"moonraker_url": "http://printer.test:7125"})
