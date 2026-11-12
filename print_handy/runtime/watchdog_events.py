"""Durable camera-watchdog verdict events for Wrapped (#981, #455).

The bad-print watchdog's verdicts used to live only in process memory
(``FailureStreak`` and the watch loops), so no honest pasta count could
ever exist. This module is the one writer of the durable events file,
``<records vault>/Watchdog/events.jsonl``: one JSON object per line,
append-only, beside the print records and the maintenance JSONL.

Recording is **always-on** (catduck's decision on #981): there is no
setting and no toggle, because events from before a toggle is flipped
can never be recovered. It is also best-effort by construction: every
function here returns a bool instead of raising, a failed append is
logged once per failure, and a print or a watch loop must never notice
that recording failed.

Event rule — which verdicts write events: only the **alert verdicts**
(``spaghetti``, ``detached``, ``air``, ``uncertain`` — the verdicts
that already raise an operator alert in the watch loops). Healthy
verdicts (``ok``, ``safe``, ``empty_idle``) are not recorded: the watch
checks every few minutes on long prints, so recording healthy verdicts
would grow the file without bound while adding nothing a recap can
use; the file exists so Wrapped can count failures the camera actually
flagged. Idle plate checks never record either — they are not prints,
and their verdicts would invent pasta for prints that do not exist.
Only the print-watch loops call the recorder.

One event carries: ``timestamp`` (ISO UTC), ``printer`` and
``print_number`` / ``job_key`` where the watch knows them (either may
be null — the watch does not always have a print number), ``verdict``,
``streak`` (the watch's consecutive-suspicious count where it tracks
one, else null), and ``action`` — what the watch did with the verdict
(``"alert"``, ``"auto_pause"``, ``"suppressed"`` or ``"none"``).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# The alert verdicts, mirroring visual_watch.VISUAL_ALERT_VERDICTS.
EVENT_VERDICTS = frozenset({"spaghetti", "detached", "air", "uncertain"})


def events_path(vault=None) -> Path:
    """``<records vault>/Watchdog/events.jsonl`` (vault resolved live)."""
    if vault is None:
        from . import _config
        vault = _config.records_vault()
    return Path(vault) / "Watchdog" / "events.jsonl"


def should_record(verdict) -> bool:
    """The event rule above, as code: alert verdicts only."""
    return str(verdict or "").strip().lower() in EVENT_VERDICTS


def append_event(event, *, vault=None, path=None) -> bool:
    """Append one event dict as a JSON line. Never raises.

    Append-only: the file is opened in append mode and never rewritten,
    so a crash mid-write can lose at most the line being written.
    """
    try:
        target = Path(path) if path is not None else events_path(vault)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:
        logger.warning("watchdog event append failed: %s: %s",
                       type(exc).__name__, exc)
        return False


def record_verdict(*, verdict, printer=None, print_number=None, job_key=None,
                   streak=None, action="none", timestamp=None,
                   vault=None, path=None) -> bool:
    """Record one watch verdict if the event rule admits it.

    Returns True only when a line was actually appended. Never raises:
    the watch loop's verdict is the product, this file is a byproduct.
    """
    try:
        if not should_record(verdict):
            return False
        event = {
            "timestamp": timestamp or datetime.now(timezone.utc)
                         .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "printer": printer,
            "print_number": (print_number if isinstance(print_number, int)
                             and not isinstance(print_number, bool) else None),
            "job_key": str(job_key) if job_key else None,
            "verdict": str(verdict).strip().lower(),
            "streak": (streak if isinstance(streak, int)
                       and not isinstance(streak, bool) else None),
            "action": str(action or "none"),
        }
        return append_event(event, vault=vault, path=path)
    except Exception as exc:  # pragma: no cover - append_event already guards
        logger.warning("watchdog event record failed: %s: %s",
                       type(exc).__name__, exc)
        return False


def record_for_file(filename, *, verdict, streak=None, action="none",
                    vault=None, path=None) -> bool:
    """Record a verdict for a print the watch knows only by file name.

    Printer and print number come from the print's own record
    (:func:`print_records.latest_record_for_file`) when one exists —
    best-effort: no record yet means both stay null and the job key
    (the file name) still identifies the print.
    """
    if not should_record(verdict):
        return False  # healthy verdicts never even look the record up
    printer = print_number = None
    try:
        from . import print_records
        record = print_records.latest_record_for_file(str(filename or ""))
        if record:
            printer = record.get("printer")
            number = record.get("print_number")
            if isinstance(number, int) and not isinstance(number, bool):
                print_number = number
    except Exception:
        pass
    return record_verdict(verdict=verdict, printer=printer,
                          print_number=print_number, job_key=filename,
                          streak=streak, action=action, vault=vault, path=path)


__all__ = ["EVENT_VERDICTS", "append_event", "events_path", "record_for_file",
           "record_verdict", "should_record"]
