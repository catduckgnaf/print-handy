"""Capture why a print stopped, at the moment it stops (2026-11-05).

On a firmware shutdown, an error state or a cancel the listener calls capture(). It reads,
read only, Moonraker's printer.info state_message, the webhooks state, the print_stats message
and a bounded tail of the printer log, cut at the stop so a printer that already restarted
does not hand back its new boot. The result is written atomically as JSON:

  <state>/stops/<file slug>.json   the per-file sidecar; print_records reads it into the record
  <archive job folder>/STOP.json   next to JOB.json, when the job's archive folder is known

and the newest print record for the file gains stop_reason and stop_capture, so the reason
survives both a printer restart (its log rolls over) and a container recreate. Nothing here
sends a command to the printer.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import _config

LOG_TAIL_BYTES = 60000
LOG_TAIL_LINES = 80
MAX_EVENTS = 5
LOG_PATHS = ("/server/files/klippy.log", "/server/files/logs/klippy.log", "/server/files/logs/printer.log")
# Status text that means "running normally", never a stop reason.
NOT_A_REASON = re.compile(r"^(?:printer is ready|klipper state: ready|ready|startup|printer is not ready|"
                          r"klippy host not connected|.*\bstarting\b)", re.I)
LOG_REASON = re.compile(r"Transition to shutdown state:\s*(.+)|^!!\s*(.+)", re.I)


def _utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _current_printer_id() -> str | None:
    try:
        from print_handy import instance
        return instance.printer_id()
    except Exception:
        return None


def stops_dir(printer_id: str | None = None) -> Path:
    if printer_id is None:
        printer_id = _current_printer_id()
    if printer_id:
        try:
            from .. import settings
            data = _config.cfg()
            if printer_id != settings.default_printer_id(data):
                return settings.printer_state_dir(printer_id, data) / "stops"
        except Exception:
            pass
        return _config.state_dir() / "stops"
    return _config.state_file(None, "stops", per_instance=True)


def _slug(filename: str) -> str:
    stem = Path(str(filename or "print")).name
    return (re.sub(r"[^A-Za-z0-9._]+", "-", stem).strip(".-") or "print")[:120]


def sidecar_path(filename: str, printer_id: str | None = None) -> Path:
    return stops_dir(printer_id) / f"{_slug(filename)}.json"


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _default_fetch(path: str, range_bytes: int | None = None) -> Any:
    from . import klipper_report
    if range_bytes is None:
        return klipper_report._get(path)
    req = urllib.request.Request(klipper_report._url(path), headers={"Range": f"bytes=-{range_bytes}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", "replace")


def _reason_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return "" if not text or NOT_A_REASON.match(text) else text[:400]


def reason_from_log(lines: list[str]) -> str:
    """The last shutdown transition or !! line in the stop session, else ''."""
    found = ""
    for line in lines:
        match = LOG_REASON.search(line.strip())
        if match:
            found = (match.group(1) or match.group(2) or "").strip()
    return found[:400]


def collect(fetch: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Read the stop facts from Moonraker. Each source fails independently."""
    from . import klipper_report
    fetch = fetch or _default_fetch
    out: dict[str, Any] = {"sources": {}}
    try:
        info = (fetch("/printer/info") or {}).get("result") or {}
        out["printer_info"] = {"state": info.get("state"), "state_message": info.get("state_message")}
        out["sources"]["printer_info"] = "ok"
    except Exception as exc:
        out["sources"]["printer_info"] = f"failed: {type(exc).__name__}"
    try:
        status = ((fetch("/printer/objects/query?webhooks&print_stats") or {}).get("result") or {}).get("status") or {}
        wh = status.get("webhooks") or {}
        ps = status.get("print_stats") or {}
        out["webhooks"] = {"state": wh.get("state"), "state_message": wh.get("state_message")}
        out["print_stats"] = {"state": ps.get("state"), "message": ps.get("message"), "filename": ps.get("filename")}
        out["sources"]["objects"] = "ok"
    except Exception as exc:
        out["sources"]["objects"] = f"failed: {type(exc).__name__}"
    try:
        jobs = ((fetch("/server/history/list?limit=1&order=desc") or {}).get("result") or {}).get("jobs") or []
        if jobs and isinstance(jobs[0], dict):
            out["job"] = {k: jobs[0].get(k) for k in ("filename", "status", "start_time", "end_time")}
        out["sources"]["history"] = "ok"
    except Exception as exc:
        out["sources"]["history"] = f"failed: {type(exc).__name__}"
    for path in LOG_PATHS:
        try:
            text = fetch(path, LOG_TAIL_BYTES)
        except Exception as exc:
            out["sources"][path] = f"failed: {type(exc).__name__}"
            continue
        if isinstance(text, str) and text.strip():
            session = klipper_report.stop_session(text)
            out["log_source"] = path
            out["log_tail"] = [line[:300] for line in session[-LOG_TAIL_LINES:]]
            out["log_restarted_after_stop"] = len(klipper_report.log_sessions(text)) > 1 and bool(session)
            out["sources"][path] = "ok"
            break
    return out


def pick_reason(facts: dict[str, Any]) -> str:
    """The best stated reason, in order of authority. Never guessed."""
    wh = facts.get("webhooks") or {}
    info = facts.get("printer_info") or {}
    candidates = []
    if wh.get("state") in ("shutdown", "error"):
        candidates.append(wh.get("state_message"))
    if info.get("state") in ("shutdown", "error"):
        candidates.append(info.get("state_message"))
    candidates.append(reason_from_log(facts.get("log_tail") or []))
    candidates.append((facts.get("print_stats") or {}).get("message"))
    for value in candidates:
        text = _reason_text(value)
        if text:
            return text
    return ""


def load(filename: str, printer_id: str | None = None) -> dict[str, Any]:
    try:
        data = json.loads(sidecar_path(filename, printer_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _archive_folder(filename: str) -> Path | None:
    try:
        from . import discord_print_summary
        job, _ = discord_print_summary.find_job(filename)
    except Exception:
        return None
    path = (job or {}).get("_job_path") if isinstance(job, dict) else None
    return Path(path).parent if path else None


def capture(filename: str, trigger: str, *, fetch: Callable[..., Any] | None = None,
            now: float | None = None, log: Callable[[str], Any] | None = None,
            printer_id: str | None = None) -> dict[str, Any]:
    """Capture, store and return the stop facts for `filename`. Never raises."""
    now = time.time() if now is None else now
    if printer_id is None:
        printer_id = _current_printer_id()
    try:
        facts = collect(fetch)
    except Exception as exc:  # pragma: no cover - collect already isolates each source
        facts = {"sources": {"collect": f"failed: {type(exc).__name__}"}}
    reason = pick_reason(facts)
    event = {"trigger": trigger, "captured_at": _utc(now), "reason": reason, **facts}
    record = load(filename, printer_id)
    started = (facts.get("job") or {}).get("start_time")
    if record and isinstance(started, (int, float)) and _utc(started) > str(record.get("first_captured_at") or ""):
        record = {}  # the stored stop belongs to an earlier print of the same file
    events = [e for e in record.get("events") or [] if isinstance(e, dict)]
    events = (events + [event])[-MAX_EVENTS:]
    # The first stated reason wins: a cancel after a firmware stop reads a restarted printer.
    stop_reason = record.get("stop_reason") or reason
    payload = {
        "_doc": "Why this print stopped, captured by Print Handy when it stopped. Written by stop_capture.py.",
        "filename": filename,
        "printer_id": printer_id,
        "stop_reason": stop_reason,
        "first_captured_at": record.get("first_captured_at") or event["captured_at"],
        "events": events,
    }
    stored: list[str] = []
    try:
        path = sidecar_path(filename, printer_id)
        _atomic_write_json(path, payload)
        stored.append(str(path))
    except OSError as exc:
        _log(log, f"stop capture: sidecar write failed: {exc!r}")
    folder = _archive_folder(filename)
    if folder is not None:
        try:
            _atomic_write_json(folder / "STOP.json", payload)
            stored.append(str(folder / "STOP.json"))
        except OSError as exc:
            _log(log, f"stop capture: archive write failed: {exc!r}")
    try:
        from . import print_records
        record_path = print_records.attach_stop_reason(filename, stop_reason, stored[0] if stored else "",
                                                       payload["first_captured_at"],
                                                       printer_id=printer_id)
        if record_path:
            stored.append(record_path)
    except Exception as exc:
        _log(log, f"stop capture: record update failed: {exc!r}")
    _log(log, f"stop reason ({trigger}) for {Path(str(filename)).name}: "
              f"{stop_reason or 'none stated by the printer'}")
    payload["stored"] = stored
    return payload


def _log(log: Callable[[str], Any] | None, line: str) -> None:
    if log is not None:
        try:
            log(line)
            return
        except Exception:
            pass
    import logging
    logging.getLogger("print_handy.stop_capture").info("%s", line)
