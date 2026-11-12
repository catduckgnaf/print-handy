"""Preventive maintenance checklist and print-hour due calculations."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import _config

PROFILE_STATE = _config.state_file("PRINTER_MAINTENANCE_STATE", "maintenance.json")


def _state_path(path: Path | str | None, printer_id: str | None) -> Path:
    """The maintenance record for one printer (#334, design §2.4).

    An explicit path wins, then the operator's env override (PROFILE_STATE is
    already env-resolved), then <state>/printers/<id>/maintenance.json. With
    no printer named, PROFILE_STATE exactly as before.
    """
    if path is not None:
        return Path(path)
    if printer_id is None or os.environ.get("PRINTER_MAINTENANCE_STATE"):
        return Path(PROFILE_STATE)
    return _config.state_dir() / "printers" / printer_id / "maintenance.json"


def _read_path(path: Path | str | None, printer_id: str | None) -> Path:
    """Where a load reads: the per-printer file, or the legacy file it adopts.

    Adoption is read-only and the default printer's alone (its record was the
    singleton's); writes always go to the per-printer location and the legacy
    file is left in place for one release.
    """
    target = _state_path(path, printer_id)
    if printer_id is None or path is not None or target.exists():
        return target
    try:
        default_id = _config.cfg()["printers"][0]["id"]
    except Exception:
        default_id = None
    if printer_id == default_id and Path(PROFILE_STATE).is_file():
        return Path(PROFILE_STATE)
    return target


VAULT_LOG = (Path(os.environ["PRINTER_MAINTENANCE_LOG"]) if os.environ.get("PRINTER_MAINTENANCE_LOG")
             else _config.records_vault() / "Maintenance" / "maintenance.jsonl")
QUICK_HOURS = float(_config.cfg()["maintenance"]["quick_hours"])
FULL_HOURS = float(_config.cfg()["maintenance"]["full_hours"])
FULL_DAYS = float(_config.cfg()["maintenance"]["full_days"])


def load(path: Path | str | None = None, *, printer_id: str | None = None) -> dict[str, Any]:
    try:
        data = json.loads(_read_path(path, printer_id).read_text())
        return data if isinstance(data, dict) else {"runs": []}
    except (OSError, ValueError):
        return {"runs": []}


def save(data: dict[str, Any], path: Path | str | None = None, *, printer_id: str | None = None) -> None:
    target = _state_path(path, printer_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, target)


def append_run(record: dict[str, Any], *, state_path: Path | str | None = None,
               vault_path: Path | str = VAULT_LOG, printer_id: str | None = None) -> dict[str, Any]:
    data = load(state_path, printer_id=printer_id)
    entry = {**record, "at": record.get("at") or datetime.now(timezone.utc).isoformat()}
    data["runs"].append(entry)
    save(data, state_path, printer_id=printer_id)
    vault = Path(vault_path)
    vault.parent.mkdir(parents=True, exist_ok=True)
    with vault.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def begin_session(kind: str, state: dict[str, Any], *, path: Path | str | None = None,
                  printer_id: str | None = None) -> dict[str, Any]:
    if kind not in {"full", "quick"}:
        raise ValueError("maintenance kind must be full or quick")
    session = {"kind": kind, "steps": [None] * (4 if kind == "full" else 2), "problems": [],
               "cold_pull": None, "created_at": datetime.now(timezone.utc).isoformat()}
    state["pending"] = session
    save(state, path, printer_id=printer_id)
    return session


def checklist_step(session: dict[str, Any], index: int, action: str) -> dict[str, Any]:
    if index not in range(4) or action not in {"done", "skip", "problem"}:
        raise ValueError("invalid maintenance checklist update")
    session.setdefault("steps", [None] * 4)[index] = action
    return session


def save_session(session: dict[str, Any], state: dict[str, Any], *, path: Path | str | None = None,
                 printer_id: str | None = None) -> None:
    state["pending"] = session
    save(state, path, printer_id=printer_id)


def due(hours_now: float, runs: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    latest = {kind: next((r for r in reversed(runs) if r.get("kind") == kind), None)
              for kind in ("full", "quick")}
    since = {kind: max(0.0, float(hours_now) - float(row.get("hours_total", hours_now))) if row else float(hours_now)
             for kind, row in latest.items()}
    full_row = latest["full"]
    age_days = (now - datetime.fromisoformat(full_row["at"]).astimezone(timezone.utc)).total_seconds() / 86400 if full_row and full_row.get("at") else None
    return {"quick_due": since["quick"] >= QUICK_HOURS,
            "full_due": since["full"] >= FULL_HOURS or age_days is None or age_days >= FULL_DAYS,
            "quick_hours_since": since["quick"], "full_hours_since": since["full"], "full_days_since": age_days}


def reminder_text(state: dict[str, Any], hours_now: float, *, idle: bool) -> str | None:
    if not idle:
        return None
    status = due(hours_now, state.get("runs", []))
    if status["full_due"]:
        return f"Maintenance due: full clean ({status['full_hours_since']:.1f} print hours since last full clean)."
    if status["quick_due"]:
        return f"Maintenance due: quick check ({status['quick_hours_since']:.1f} print hours since last quick check)."
    return None


def result_guidance(result: str, prior_dirty: int = 0) -> dict[str, Any]:
    """Policy for the operator-reported cold-pull tip result."""
    if result not in {"clean", "dirty", "snapped", "didnt_release"}:
        raise ValueError("invalid cold-pull tip result")
    dirty = prior_dirty + 1 if result == "dirty" else 0
    return {"dirty_streak": dirty, "offer_repeat": result == "dirty" and dirty < 2,
            "suggest_nozzle_swap": result == "dirty" and dirty >= 2,
            "offer_cold_95": result in {"snapped", "didnt_release"}}


def status_text(hours_now: float | None, runs: list[dict[str, Any]], *, now: datetime | None = None) -> str:
    """Format the last full clean. None hours means live totals are unavailable.

    Do not substitute the stored baseline for the current total: that makes the
    elapsed print-hour delta look like 0.0.
    """
    days = None
    hours_text = "print hours unknown"
    if hours_now is not None:
        status = due(hours_now, runs, now=now)
        days = status["full_days_since"]
        hours_text = f"{status['full_hours_since']:.1f} h"
    else:
        now = now or datetime.now(timezone.utc)
        full = next((row for row in reversed(runs) if row.get("kind") == "full"), None)
        if full and full.get("at"):
            days = (now - datetime.fromisoformat(full["at"]).astimezone(timezone.utc)).total_seconds() / 86400
    day_text = "unknown days" if days is None else f"{days:.0f} days"
    return f"Maintenance: last full clean {day_text} / {hours_text} ago."
