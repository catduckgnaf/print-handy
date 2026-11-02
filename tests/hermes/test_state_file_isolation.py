"""P1-82: sibling listener instances must not share operational state files.

One listener process runs per roster printer (#336), but the Moonraker
listener's state — lifecycle notification claims, the announcer's current
job, runout state, IFS edges, known Spoolman spools — resolved to single
shared files under the state root. Two Moonraker listeners could
therefore suppress each other's notifications (claims are keyed by
printer-local job id) or overwrite each other's announcer state. The
family polling listeners share the same defect through their single
``family_listener.json``.

Listener-owned state now resolves per instance: a process serving a
non-default roster printer gets ``<state>/printers/<id>/<name>`` (#334's
layout); the default printer keeps the legacy path (its state was always
the singleton's), a process serving no instance is unchanged, and an env
override always wins. Cross-process channels stay shared by design —
notably the start claims, which print-now writes and the listeners read.
"""
import copy
import importlib.util
import sys
from pathlib import Path

import pytest

from print_handy import listeners
from print_handy.hermes import _config

FIRST = "first-printer"
SECOND = "second-printer"


@pytest.fixture
def two_printer_state(tmp_path):
    """A full test config whose state root is a temp dir, with a two-printer roster."""
    # tests/conftest.py installs the test settings as the _config override
    # for the whole session: restore that object, never None (clearing it
    # makes _config.cfg() fall back to settings.current() for every later
    # test in the process).
    previous = _config.cfg()
    data = copy.deepcopy(previous)
    data["paths"]["hermes_home"] = tmp_path
    data["printers"] = [{"id": FIRST}, {"id": SECOND}]
    _config.set_override(data)
    yield tmp_path / "state"
    _config.set_override(previous)
    listeners._set_instance_printer_id(None)


def _load_copy(module_name, alias):
    """Execute a fresh copy of a module (its import-time state paths re-resolve)."""
    spec = importlib.util.spec_from_file_location(
        alias, importlib.util.find_spec(module_name).origin)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(alias, None)
    return module


# -- the resolution mechanism -------------------------------------------------


def test_state_file_per_instance_resolution(two_printer_state, monkeypatch):
    state = two_printer_state
    for var in ("PRINT_HANDY_NOTIFICATION_STATE",):
        monkeypatch.delenv(var, raising=False)
    # Not per-instance: never namespaced, whatever the process serves.
    listeners._set_instance_printer_id(SECOND)
    assert _config.state_file(None, "x.json") == state / "x.json"
    # Per-instance with no instance identity (the bot): the legacy path.
    listeners._set_instance_printer_id(None)
    assert _config.state_file(None, "x.json", per_instance=True) == state / "x.json"
    # Per-instance for the default printer: the legacy path — its state
    # was always the singleton's, so nothing is orphaned at upgrade.
    listeners._set_instance_printer_id(FIRST)
    assert _config.state_file(None, "x.json", per_instance=True) == state / "x.json"
    # Per-instance for a sibling printer: its own folder.
    listeners._set_instance_printer_id(SECOND)
    assert _config.state_file(None, "x.json", per_instance=True) == \
        state / "printers" / SECOND / "x.json"


def test_state_file_env_override_wins_over_namespacing(two_printer_state, monkeypatch):
    custom = two_printer_state / "elsewhere.json"
    monkeypatch.setenv("PRINT_HANDY_TEST_STATE", str(custom))
    listeners._set_instance_printer_id(SECOND)
    assert _config.state_file("PRINT_HANDY_TEST_STATE", "x.json",
                              per_instance=True) == custom


# -- the Moonraker listener's own state ----------------------------------------


def test_sibling_moonraker_listeners_do_not_suppress_each_other(two_printer_state,
                                                                monkeypatch):
    monkeypatch.delenv("PRINT_HANDY_NOTIFICATION_STATE", raising=False)
    state = two_printer_state
    listeners._set_instance_printer_id(SECOND)
    second = _load_copy("print_handy.hermes.moonraker_event_listener",
                        "print_handy.hermes._mel_second_copy")
    assert second.NOTIFICATION_STATE == \
        state / "printers" / SECOND / "lifecycle_notifications.json"
    assert second._claim_notification("job:7", "started") is True
    assert second._claim_notification("job:7", "started") is False  # own claims dedupe

    listeners._set_instance_printer_id(FIRST)
    first = _load_copy("print_handy.hermes.moonraker_event_listener",
                       "print_handy.hermes._mel_first_copy")
    assert first.NOTIFICATION_STATE == state / "lifecycle_notifications.json"
    # The sibling claimed the same printer-local job id and phase; this
    # listener's identical claim must still go through (pre-fix it was
    # suppressed by the shared file).
    assert first._claim_notification("job:7", "started") is True


def test_announcer_state_is_namespaced_per_instance(two_printer_state, monkeypatch):
    monkeypatch.delenv("MOONRAKER_ANNOUNCEMENT_STATE", raising=False)
    state = two_printer_state
    listeners._set_instance_printer_id(SECOND)
    announcer = _load_copy("print_handy.hermes.moonraker_discord_announcements",
                           "print_handy.hermes._announcer_second_copy")
    assert announcer.STATE_PATH == \
        state / "printers" / SECOND / "moonraker_discord_announcements.json"
    announcer.save_state({"job_key": "benchy.gcode"})
    assert announcer.load_state()["job_key"] == "benchy.gcode"
    # Nothing leaked into the shared file the default printer's announcer owns.
    assert not (state / "moonraker_discord_announcements.json").exists()


# -- the family polling listeners ----------------------------------------------


def test_family_listener_default_state_path_is_namespaced(two_printer_state,
                                                          monkeypatch):
    monkeypatch.delenv("PRINT_HANDY_FAMILY_LISTENER_STATE", raising=False)
    from print_handy.hermes import family_event_listener as fel

    state = two_printer_state

    def make(printer_id):
        return fel.FamilyListener(
            "prusalink", lambda: {"state": "idle", "filename": ""},
            runtime="prusalink_event_listener", printer_id=printer_id,
            beat=lambda **_details: None)

    listeners._set_instance_printer_id(SECOND)
    assert make(SECOND)._state_path == \
        state / "printers" / SECOND / "family_listener.json"
    listeners._set_instance_printer_id(FIRST)
    assert make(FIRST)._state_path == state / "family_listener.json"
    listeners._set_instance_printer_id(None)
    assert make(None)._state_path == state / "family_listener.json"
