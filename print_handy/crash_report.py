"""Crash capture (#1404): one redacted JSON record per process role.

Every long-lived Print Handy process names its role when it configures
logging (``logs.configure(role=...)``: bot, listener, heavy-worker,
monitor-worker, webui, live-activity). :func:`install` then points the
interpreter's exception hooks at :func:`record`, so an *unhandled*
exception — the crash that otherwise survives only as a traceback in a
log nobody may have configured — also lands as a structured record at
``<state>/crashes/<role>.json`` (one file per role, newest crash wins).

A record carries the time, the role, the Print Handy version, the pid,
the exception type, the redacted message, a bounded redacted traceback,
and the log context bound at crash time. Secrets are redacted with
:func:`print_handy.logs.redact` before anything is written, and the
diagnostics bundle sanitizes its Crashes section a second time on the
way out (the bundle's export pass).

There is no telemetry: nothing is uploaded anywhere. A record leaves
the machine only when the operator attaches a diagnostics bundle that
includes it. Everything here is fail-soft by design — recording a crash
must never raise, mask the original failure, or take down the process
that is already crashing, so every public function swallows its own
errors. ``KeyboardInterrupt`` and ``SystemExit`` are not crashes: the
hooks chain to the previous hook without recording them.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import __version__, logs, settings

CRASH_DIR_NAME = "crashes"
TRACEBACK_MAX_CHARS = 6000
_SAFE_ROLE = re.compile(r"[^a-z0-9-_]+")
_installed_roles: set[str] = set()


def _role_name(role) -> str:
    """A role as a safe file stem: lowercase, ``[a-z0-9-_]``, else "process"."""
    name = _SAFE_ROLE.sub("-", str(role or "").strip().lower()).strip("-")
    return name or "process"


def crash_dir(data=None) -> Path:
    return settings.state_dir(data) / CRASH_DIR_NAME


def _is_crash(exc_type) -> bool:
    """Unexpected exceptions only: interrupts and exits are not crashes."""
    return (isinstance(exc_type, type)
            and issubclass(exc_type, Exception)
            and not issubclass(exc_type, (KeyboardInterrupt, SystemExit)))


def record(exc_type, exc, tb, *, role, data=None, now=None) -> Path | None:
    """Write one crash record for ``role``; returns its path, or None.

    Never raises: a crash handler that raises would hide the crash it
    was called for. The write is atomic (temp file + ``os.replace``),
    so a reader never sees a half-written record.
    """
    try:
        when = time.time() if now is None else float(now)
        formatted = ""
        if exc_type is not None:
            try:
                formatted = "".join(traceback.format_exception(exc_type, exc, tb))
            except Exception:
                formatted = ""
        try:
            # Redacted like the message and traceback: a secret bound into
            # the log context (by key name or by value) must not reach disk.
            context = logs._redact_value(
                {str(key): value for key, value in logs.bound_context().items()})
        except Exception:
            context = {}
        payload = {
            "time": when,
            "recorded_at": datetime.fromtimestamp(when).astimezone().isoformat(timespec="seconds"),
            "role": _role_name(role),
            "version": __version__,
            "pid": os.getpid(),
            "exception": getattr(exc_type, "__name__", None) or str(exc_type),
            "reference": "PH_CRASH_PROGRAMMING",
            "message": logs.redact(str(exc)) if exc is not None else "",
            "traceback": logs.redact(formatted)[-TRACEBACK_MAX_CHARS:] if formatted else "",
            "context": context,
        }
        directory = crash_dir(data)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{payload['role']}.json"
        # One temp file per writer (pid + thread), so two processes or
        # threads of one role crashing at once cannot clobber each other's
        # temp file before os.replace; the last replace wins whole.
        tmp = directory / f".{payload['role']}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            tmp.write_text(json.dumps(payload, indent=1, sort_keys=True, default=str),
                           encoding="utf-8")
            os.replace(tmp, target)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        return target
    except Exception:
        return None


def install(role, data=None) -> bool:
    """Point the exception hooks at :func:`record` for ``role``.

    Idempotent per role: a second call for the same role changes
    nothing (re-installing would chain the hook to itself). The hooks
    record, then chain to whichever hook was installed before them, so
    the interpreter's default traceback printing (or a host program's
    hook) still runs. Returns True when the hooks are in place for the
    role. Never raises.
    """
    try:
        name = _role_name(role)
        if name in _installed_roles:
            return True
        _installed_roles.add(name)
        previous_sys = sys.excepthook
        previous_thread = threading.excepthook

        def _sys_hook(exc_type, exc, tb):
            if _is_crash(exc_type):
                record(exc_type, exc, tb, role=name, data=data)
            try:
                previous_sys(exc_type, exc, tb)
            except Exception:
                pass

        def _thread_hook(args):
            exc_type = getattr(args, "exc_type", None)
            if _is_crash(exc_type):
                record(exc_type, getattr(args, "exc_value", None),
                       getattr(args, "exc_traceback", None), role=name, data=data)
            try:
                previous_thread(args)
            except Exception:
                pass

        sys.excepthook = _sys_hook
        threading.excepthook = _thread_hook
        return True
    except Exception:
        return False


def read_records(data=None) -> list[dict]:
    """Every recorded crash, oldest first. Unreadable files are skipped.

    Never raises: a broken record is a gap in the bundle, not a reason
    to produce no bundle at all.
    """
    try:
        directory = crash_dir(data)
        files = sorted(directory.glob("*.json"))
    except Exception:
        return []
    records = []
    for item in files:
        try:
            raw = json.loads(item.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(raw, dict):
            records.append(raw)
    records.sort(key=lambda rec: rec.get("time")
                 if isinstance(rec.get("time"), (int, float)) else 0)
    return records


def section(data=None) -> str:
    """The bundle's Crashes section body: one block per recorded crash."""
    records = read_records(data)
    if not records:
        return "(no crashes recorded)"
    parts = []
    for rec in records:
        when = rec.get("recorded_at") or rec.get("time") or "(time not recorded)"
        parts.append(f"### {rec.get('role') or 'process'} — {when}")
        parts.append(f"Exception: {rec.get('exception') or 'unknown'}: "
                     f"{rec.get('message') or ''}".rstrip())
        trace = str(rec.get("traceback") or "").strip()
        if trace:
            parts.append(f"```text\n{trace}\n```")
        else:
            parts.append("(no traceback recorded)")
        parts.append("")
    return "\n".join(parts).strip()


__all__ = ["CRASH_DIR_NAME", "crash_dir", "install", "read_records", "record", "section"]
