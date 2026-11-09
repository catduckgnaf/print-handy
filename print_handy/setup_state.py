"""One shared setup-completion record for every setup surface.

Before this module each surface kept its own idea of "setup is done":
the Web UI walkthrough had ``webui_walkthrough.json``, the chat guided
setup had a session with ``status = "complete"`` in ``guided-setup.json``,
and the CLI wizard had nothing at all. Finishing setup in one surface
therefore did not stop the others from offering or re-asking the whole
walkthrough (audit B5).

The shared record is one small JSON file in the state folder,
``setup_completion.json``::

    {"done": true, "surface": "chat", "when": "2026-11-09T12:00:00+00:00"}

``surface`` names the surface that finished setup (``webui``, ``chat``
or ``wizard``) so another surface can say "set up via chat on <date>"
instead of presenting a fresh walkthrough. ``when`` is an ISO-8601 UTC
timestamp, or ``None`` for a record migrated from an older state file
that carried no timestamp.

Read-both migration: an existing ``webui_walkthrough.json`` with
``done`` true counts as completion on first read, so no install loses
its done state when this record is introduced. Writes go through the
setup wizard's atomic writer at mode 0600, the same helper the
walkthrough state file already uses.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import settings

COMPLETION_FILE = "setup_completion.json"
WALKTHROUGH_FILE = "webui_walkthrough.json"
# The path constructions below spell these names out as literals on
# purpose: tests/test_state_files.py registers state files in
# state_files.KNOWN by scanning the source for literal names, and a name
# reached only through a constant is invisible to it (and to the doctor's
# stray detection). Keep the literals at the construction sites.

SURFACES = ("webui", "chat", "wizard")


def _state_dir(data):
    """The state folder for a settings mapping, tolerating string homes.

    ``settings.state_dir`` expects the loaded settings shape (where the
    home is a ``Path``); the wizard works with the plain string form, so
    resolve that shape here instead of failing on it.
    """
    if isinstance(data, (str, Path)):
        return Path(data).expanduser()
    paths = data.get("paths") if isinstance(data, dict) else None
    if isinstance(paths, dict):
        home = paths.get("hermes_home") or paths.get("data_home")
        if isinstance(home, str) and home.strip():
            return Path(home).expanduser() / "state"
        if isinstance(home, Path):
            return home / "state"
    return Path(settings.state_dir(data))


def completion_path(data):
    return _state_dir(data) / "setup_completion.json"


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def load_completion_in_state_dir(state_dir):
    """The completion record found in ``state_dir``, or ``{}``.

    The shared record wins when present; otherwise an existing
    walkthrough state file with ``done`` true migrates in as a
    ``webui`` completion with no timestamp.
    """
    state_dir = Path(state_dir)
    record = _read_json(state_dir / "setup_completion.json")
    if record.get("done"):
        loaded = {"done": True,
                  "surface": str(record.get("surface") or "webui"),
                  "when": record.get("when")}
        if record.get("skipped"):
            loaded["skipped"] = True
        return loaded
    legacy = _read_json(state_dir / "webui_walkthrough.json")
    if legacy.get("done"):
        migrated = {"done": True, "surface": "webui", "when": None,
                    "migrated": True}
        if legacy.get("skipped"):
            migrated["skipped"] = True
        return migrated
    return {}


def load_completion(data):
    """The completion record for a settings mapping, or ``{}``."""
    try:
        return load_completion_in_state_dir(_state_dir(data))
    except Exception:  # settings too broken to locate the state folder
        return {}


def is_complete(data):
    return bool(load_completion(data).get("done"))


def is_complete_in_state_dir(state_dir):
    return bool(load_completion_in_state_dir(state_dir).get("done"))


def record_completion_in_state_dir(state_dir, surface, when=None, skipped=False):
    """Write the shared record into ``state_dir`` and return it."""
    if surface not in SURFACES:
        raise ValueError(f"unknown setup surface {surface!r}; "
                         f"use one of {', '.join(SURFACES)}")
    record = {"done": True, "surface": surface,
              "when": when or datetime.now(timezone.utc).isoformat()}
    if skipped:
        record["skipped"] = True
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    from .setup_wizard import _atomic_write
    _atomic_write(state_dir / "setup_completion.json",
                  json.dumps(record, indent=1) + "\n", 0o600)
    return record


def record_completion(data, surface, when=None, skipped=False):
    """Write the shared record for a settings mapping and return it."""
    return record_completion_in_state_dir(_state_dir(data), surface, when=when,
                                          skipped=skipped)


def clear_completion(data):
    """Forget a recorded completion, so setup can deliberately run again."""
    try:
        completion_path(data).unlink(missing_ok=True)
    except Exception:  # settings too broken to locate the state folder
        pass


def describe(record):
    """A short human line for a completion record, or ``""``."""
    if not record or not record.get("done"):
        return ""
    surface = str(record.get("surface") or "webui")
    if record.get("skipped"):
        return f"Setup was skipped via {surface}."
    when = record.get("when")
    if when:
        return f"Setup was already completed via {surface} on {when}."
    return f"Setup was already completed via {surface}."


__all__ = [
    "COMPLETION_FILE", "SURFACES", "WALKTHROUGH_FILE", "clear_completion", "completion_path",
    "describe", "is_complete", "is_complete_in_state_dir", "load_completion",
    "load_completion_in_state_dir", "record_completion",
    "record_completion_in_state_dir",
]
