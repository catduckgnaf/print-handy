"""Debug log mode (#478): a temporary, operator-controlled DEBUG capture.

    python3 -m print_handy.debug_mode [on [minutes] | off | status]

#390 gave Print Handy a one-shot diagnostics bundle and static
``[logging] levels`` config. This module adds the *mode* on top:

* ``on`` writes a small state file (``debug-state.json``) into the state
  folder with a session id and an expiry (default 30 minutes). It never
  edits config and never restarts anything.
* Every Print Handy process observes that shared state: the bot and the
  CLI apply it immediately, and each process's logging setup
  (``logs.configure``) plus a cheap :func:`poll` from the listener loops
  pick changes up — so the separate listener processes are covered by the
  same mode, which a config edit could never do cleanly.
* While on, this process's own loggers (``print_handy.*``) run at DEBUG —
  noisy library loggers stay at INFO, the ``logs.configure`` precedent —
  and records are also written to a dedicated, size-bounded capture file
  (``debug-capture.log``, rotating) in the state folder, delimited by
  ``DEBUG_SESSION_START`` / ``DEBUG_SESSION_END`` markers carrying the
  session id. The capture file exists whether or not a ``[logging] file``
  is configured, so a Docker/stderr install still produces capturable logs.
* ``off`` (or the expiry — auto-off) ends the session; the export for the
  session window is sanitized for a public GitHub issue by
  ``print_handy.sanitize_logs`` and delivered by the callers (in chat by
  the bot, as a file by the CLI). Nothing is uploaded anywhere.

Logging only: starting, stopping or exporting never touches a print path.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path

from . import logs, sanitize_logs, settings

STATE_NAME = "debug-state.json"
CAPTURE_NAME = "debug-capture.log"
EXPORT_DIR_NAME = "debug-exports"
DEFAULT_MINUTES = 30
MAX_MINUTES = 24 * 60
CAPTURE_MAX_BYTES = 5 * 1024 * 1024
CAPTURE_BACKUPS = 2
MARKER = "PH_DEBUG_CONFIGURATION"

_CAPTURE_MARK = "_print_handy_debug_capture"
_NOISY = ("discord", "websockets", "asyncio")
# Per-process sync cache: the state file's (mtime, size) last applied, and
# the session id this process has written its START marker for.
_sync_cache: dict = {"stamp": None, "started": set(), "active": None}


# --------------------------------------------------------------------- state

def state_path(data=None) -> Path:
    return settings.state_dir(data) / STATE_NAME


def capture_path(data=None) -> Path:
    return settings.state_dir(data) / CAPTURE_NAME


def _read_state(data=None) -> dict | None:
    try:
        raw = json.loads(state_path(data).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and raw.get("session") else None


def _write_state(state, data=None) -> Path:
    target = state_path(data)
    target.parent.mkdir(parents=True, exist_ok=True)
    from .setup_wizard import _atomic_write
    _atomic_write(target, json.dumps(state, indent=1), 0o600)
    return target


def _clear_state(data=None) -> None:
    try:
        state_path(data).unlink()
    except OSError:
        pass


def _remaining(state, now) -> float:
    return float(state.get("expires_at") or 0) - now


def status(data=None, *, now=None) -> dict | None:
    """The live session dict, or None. An expired session is ended (and its
    END marker written) as a side effect — that is the auto-off."""
    now = time.time() if now is None else now
    state = _read_state(data)
    if state is None:
        return None
    if _remaining(state, now) <= 0:
        end(data, now=now)
        return None
    return state


def status_text(data=None, *, now=None) -> str:
    state = status(data, now=now)
    if state is None:
        return "Debug mode is off. `/print-handy debug on` starts it (30 minutes by default)."
    left = int(_remaining(state, time.time() if now is None else now) // 60)
    return (f"Debug mode is ON — session `{state['session']}`, off in about "
            f"{max(left, 0)} minute(s) [{MARKER}]. Logs are being captured for a "
            "sanitized export; printing is unaffected.")


def check_row(data=None, *, now=None) -> str | None:
    """The doctor / bot-check row: a visible line while on, None while off."""
    state = status(data, now=now)
    if state is None:
        return None
    left = int(_remaining(state, time.time() if now is None else now) // 60)
    return (f"ON — session {state['session']}, off in about {max(left, 0)} minute(s); "
            f"turn it off with /print-handy debug off when the problem is reproduced [{MARKER}]")


# ----------------------------------------------------------------- lifecycle

def _new_session(now) -> str:
    stamp = datetime.fromtimestamp(now).astimezone().strftime("%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def start(data=None, *, minutes=None, now=None) -> dict:
    """Start (or cleanly restart/extend) a debug session. Returns the state."""
    now = time.time() if now is None else now
    minutes = DEFAULT_MINUTES if minutes is None else max(1, min(int(minutes), MAX_MINUTES))
    previous = _read_state(data)
    if previous and _remaining(previous, now) > 0:
        _end_capture_marker(previous, data)
    state = {"session": _new_session(now), "started_at": now,
             "expires_at": now + minutes * 60, "minutes": minutes}
    _write_state(state, data)
    apply(data, now=now)
    return state


def end(data=None, *, now=None) -> dict | None:
    """End the session (manual off or auto-off). Returns the ended state."""
    state = _read_state(data)
    if state is None:
        return None
    _end_capture_marker(state, data)
    _clear_state(data)
    _detach_capture()
    _sync_cache["active"] = None
    logging.getLogger("print_handy").setLevel(logging.NOTSET)
    return state


# ------------------------------------------------------- capture and levels

def _capture_handler(data):
    target = capture_path(data)
    target.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        target, maxBytes=CAPTURE_MAX_BYTES, backupCount=CAPTURE_BACKUPS, encoding="utf-8")
    handler.setFormatter(logs.TextFormatter())
    setattr(handler, _CAPTURE_MARK, True)
    return handler


def _find_capture(root):
    return next((h for h in root.handlers if getattr(h, _CAPTURE_MARK, False)), None)


def _detach_capture() -> None:
    root = logging.getLogger()
    handler = _find_capture(root)
    if handler is not None:
        root.removeHandler(handler)
        handler.close()


def _write_marker(state, word, data=None) -> None:
    """Append one session marker line to the capture file (never raises)."""
    try:
        target = capture_path(data)
        target.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} INFO print_handy.debug_mode: {word} session={state['session']}\n")
    except OSError:
        pass


def _end_capture_marker(state, data=None) -> None:
    if _sync_cache["started"] and state["session"] in _sync_cache["started"]:
        _write_marker(state, "DEBUG_SESSION_END", data)
        _sync_cache["started"].discard(state["session"])
    else:
        # Another process may own the live capture; the marker is written
        # directly so the session window is closed in the file regardless.
        _write_marker(state, "DEBUG_SESSION_END", data)


def apply(data=None, *, now=None) -> bool:
    """Apply the shared state to *this* process's logging. Returns on/off.

    Called by ``logs.configure`` (every process entrypoint) and by the
    start/stop surfaces; cheap enough to re-call. While on: ``print_handy``
    loggers at DEBUG, noisy libraries held at INFO, and the capture handler
    attached (with this process's START marker written once per session).
    While off: the capture handler and the DEBUG override are removed.
    """
    now = time.time() if now is None else now
    state = _read_state(data)
    root = logging.getLogger()
    if state is not None and _remaining(state, now) <= 0:
        end(data, now=now)
        return False
    if state is None:
        if _find_capture(root) is not None or _sync_cache["active"] is not None:
            _detach_capture()
            logging.getLogger("print_handy").setLevel(logging.NOTSET)
            _sync_cache["active"] = None
        return False
    if _find_capture(root) is None:
        try:
            root.addHandler(_capture_handler(data))
        except OSError:
            pass  # capture is best-effort; the level change still applies
    if state["session"] not in _sync_cache["started"]:
        _write_marker(state, "DEBUG_SESSION_START", data)
        _sync_cache["started"].add(state["session"])
    logging.getLogger("print_handy").setLevel(logging.DEBUG)
    for noisy in _NOISY:
        logging.getLogger(noisy).setLevel(logging.INFO)
    _sync_cache["active"] = state["session"]
    return True


def poll(data=None, *, now=None) -> bool:
    """Cheap shared-state check for long-running processes (the listeners).

    Stats the state file and only re-applies when it changed, so a loop can
    call this every tick: a listener started before ``debug on`` picks the
    mode up, and auto-off lands without any process restarting.
    """
    try:
        stat = state_path(data).stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None
    if stamp == _sync_cache["stamp"] and stamp is not None:
        # No file change, but the expiry still has to land on time.
        state = _read_state(data)
        if state is not None and _remaining(state, time.time() if now is None else now) <= 0:
            return apply(data, now=now)
        return _sync_cache["active"] is not None
    _sync_cache["stamp"] = stamp
    return apply(data, now=now)


# -------------------------------------------------------------------- export

def _capture_lines(data=None) -> list[str]:
    """Every capture line on disk, oldest first (#1404).

    The capture rotates (``RotatingFileHandler``, ``CAPTURE_BACKUPS``
    backups), so the main file alone can miss a session that rotated:
    the backups are read in chronological order — ``.2`` (oldest), then
    ``.1``, then the main file — and concatenated. Missing files are
    simply absent from the result.
    """
    base = capture_path(data)
    lines: list[str] = []
    for candidate in (Path(f"{base}.2"), Path(f"{base}.1"), base):
        try:
            lines.extend(candidate.read_text(errors="replace").splitlines())
        except OSError:
            continue
    return lines


def session_window(data=None, *, session=None) -> str | None:
    """The capture lines between one session's markers, or None.

    With ``session`` None the most recent completed-or-live session in the
    capture file is used. Rotation backups are searched too (oldest
    first, see :func:`_capture_lines`): a window that rotated into a
    backup is still found whole rather than reported as missing.
    """
    lines = _capture_lines(data)
    if not lines:
        return None
    starts = [i for i, line in enumerate(lines)
              if "DEBUG_SESSION_START" in line and (session is None or f"session={session}" in line)]
    if not starts:
        return None
    sid = session
    if sid is None:
        marker = lines[starts[-1]].rsplit("session=", 1)[-1].strip()
        sid = marker.split()[0] if marker else None
        starts = [i for i in starts if sid is None or f"session={sid}" in lines[i]]
    # First START for the session: every covered process writes its own
    # START marker, and the window begins at the earliest one.
    begin = starts[0]
    end_at = len(lines)
    for i in range(begin + 1, len(lines)):
        if "DEBUG_SESSION_END" in lines[i] and (sid is None or f"session={sid}" in lines[i]):
            end_at = i + 1
            break
    return "\n".join(lines[begin:end_at])


def latest_session(data=None, *, unexported_only: bool = False) -> str | None:
    """The session id of the most recent START marker in the capture file.

    ``unexported_only`` skips a session an `off` already exported (it carries
    a DEBUG_SESSION_EXPORTED marker), so a finished session is not recovered
    again; an auto-expired one has no such marker and still is (2.2.5 review).
    Rotation backups are searched too (#1404), in chronological order.
    """
    lines = _capture_lines(data)
    if not lines:
        return None
    for line in reversed(lines):
        if "DEBUG_SESSION_START" in line and "session=" in line:
            marker = line.rsplit("session=", 1)[-1].strip()
            if marker:
                sid = marker.split()[0]
                if unexported_only and any("DEBUG_SESSION_EXPORTED" in other and f"session={sid}" in other
                                           for other in lines):
                    return None
                return sid
    return None


def export_name(state, *, now=None) -> str:
    now = time.time() if now is None else now
    day = datetime.fromtimestamp(now).astimezone().strftime("%Y-%m-%d")
    session = (state or {}).get("session") or "nosession"
    return f"print-handy-debug-{day}-{session}.md"


def build_export(data=None, *, state=None, now=None) -> tuple[str, str, str]:
    """(file name, sanitized Markdown, report line) for one debug session.

    The session window is preferred; with no window the bundle's existing
    bounded tail is the fallback, named as such in the export. The whole
    document passes through the export sanitizer, so the config summary,
    doctor output and log lines share the bundle's one standard.
    """
    from . import diagnostics_bundle
    now = time.time() if now is None else now
    state = state if state is not None else (_read_state(data) or {})
    window = session_window(data, session=state.get("session") or None)
    if window is not None:
        log_section, log_title = window, f"Debug session log (session {state.get('session')})"
    else:
        log_section = diagnostics_bundle.log_tail(data)
        log_title = "Recent log tail (no debug session capture found)"
    parts = [
        "# Print Handy debug log export",
        "",
        f"- Session: {state.get('session') or '(none)'}",
        f"- Exported: {datetime.fromtimestamp(now).astimezone().isoformat(timespec='seconds')}",
        "",
        "## Sanitization",
        "",
        "{REPORT}",
        "",
        "## Configuration summary (sanitized)",
        "",
        diagnostics_bundle.config_summary(data),
        "",
        "## Doctor",
        "",
        "```text",
        diagnostics_bundle.doctor_output(),
        "```",
        "",
        f"## {log_title}",
        "",
        "```text",
        log_section,
        "```",
        "",
    ]
    # Every platform's IDs, list-safe: the same registry the diagnostics
    # bundle uses. Building this from the Discord section alone (and by
    # string-splitting it) leaked Telegram/Matrix/approval IDs and mangled
    # a list-valued allowed_user_ids into bracketed fragments.
    sanitizer = diagnostics_bundle._sanitizer(data)
    clean = sanitizer.sanitize("\n".join(parts))
    report = sanitizer.report()
    return export_name(state, now=now), clean.replace("{REPORT}", report), report


def write_export(data=None, *, state=None, now=None) -> tuple[Path, str]:
    """Write the sanitized export under ``<state>/debug-exports``; (path, report)."""
    data = settings.current() if data is None else data
    name, text, report = build_export(data, state=state, now=now)
    target = settings.state_dir(data) / EXPORT_DIR_NAME / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target, report


def off_and_export(data=None, *, now=None) -> tuple[Path, str, dict]:
    """End the session and build its export: (path, report, ended state).

    Also works just after an auto-off: the state file is already gone, so
    the session is recovered from the capture file's latest START marker
    and its window is exported.
    """
    state = _read_state(data) or {}
    if not state.get("session"):
        sid = latest_session(data, unexported_only=True)
        if sid:
            state = {"session": sid}
    target, report = write_export(data, state=state, now=now)
    ended = end(data, now=now) or state
    if state.get("session"):
        _write_marker(state, "DEBUG_SESSION_EXPORTED", data)
    return target, report, ended


# ----------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Print Handy debug log mode: on [minutes] | off | status. "
                    "Logging only; nothing is uploaded anywhere.")
    parser.add_argument("action", choices=("on", "off", "status"))
    parser.add_argument("minutes", nargs="?", type=int,
                        help=f"how long `on` lasts (default {DEFAULT_MINUTES}, max {MAX_MINUTES})")
    args = parser.parse_args(argv)
    data = settings.current()
    if args.action == "on":
        state = start(data, minutes=args.minutes)
        print(f"Debug mode ON — session {state['session']}, off in {state['minutes']} minute(s). "
              f"Capture: {capture_path(data)}. Stop it with: python3 -m print_handy.debug_mode off")
        return 0
    if args.action == "off":
        if _read_state(data) is None and not latest_session(data, unexported_only=True):
            # No live state and no capture to recover: genuinely off. (A
            # session that auto-expired has no state file left — a status
            # check or listener poll ended it — but its capture remains,
            # and off_and_export recovers it below.)
            print("Debug mode is already off.")
            return 0
        target, report, ended = off_and_export(data)
        print(f"Debug mode OFF (session {ended.get('session')}). Sanitized export: {target}\n{report}\n"
              "Review it, then attach it to your bug report — nothing was uploaded anywhere.")
        return 0
    print(status_text(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
