"""Charge a finished multi-slot print's filament to each slot's spool.

A multi-slot launch clears Moonraker's active spool (print_now's
``_clear_active_spool``) because Moonraker can only charge ONE spool, and
one spool cannot carry a multi-color job's usage. Nothing charged the
spools afterwards, so every multi-color print left Spoolman untouched.

This module apportions the usage once the print is over. The inputs are
what the launch recorded in the print context (print_now.record_start):

* ``tool_to_physical_slot``: the tool -> IFS slot map the file printed with;
* ``slicer_settings.mapped_materials``: each tool's slot record AT LAUNCH,
  so ``spoolman_id`` there is the spool bound to that slot when the print
  started. A spool swapped into the slot later is never charged for it;
* ``slicer_settings.filament_used_g_list``: the G-code's per-tool grams
  (``; filament used [g] = a, b``), indexed by tool number;
* ``planned_filament_mm``: the G-code's total planned filament length.

A completed print charges the slicer's per-tool grams. A cancelled or
stopped print charges them scaled by the filament actually used over the
planned total (``print_stats.filament_used`` vs ``planned_filament_mm``).

Each print is charged at most once: a ledger in the state directory keys
every charge by the print's identity (job key plus print number, or its
start time) and records each slot's charge before and after the Spoolman
call, so a listener restart or a duplicate end event never charges twice.
A slot whose charge failed because Spoolman did not answer stays pending
and is retried when Spoolman is reachable again (``retry_pending``).

A single-slot print is charged by Moonraker's own Spoolman tracker while it
runs, and this module only checks that charge when the print ends (#1374).
Moonraker's ``set_active_spool`` resets its extrusion high-water mark to the
last toolhead E position it saw, which is 0 after a Moonraker restart until
the extruder next moves. The first extrusion after such a spool change then
charges every millimetre Klipper has extruded since it last started to the
new spool (a 0.6 g print charged 987 g). The launch records the spool's used
weight just before the start (``launch_baseline``); at the end
``guard_single_slot`` compares Spoolman's growth with what the print
actually extruded and, when Moonraker charged far more, writes the used
weight back to baseline plus the print's own grams. Same ledger, so a
duplicate end event never corrects twice.

Spoolman is bookkeeping: nothing here raises into the print lifecycle.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..core import spoolman_client
from . import _config

logger = logging.getLogger(__name__)

LEDGER_ENV = "PRINT_HANDY_MULTISLOT_USAGE_STATE"
LEDGER_NAME = "multislot_usage_charges.json"
LEDGER_KEEP = 200

SUSPENDED_REASON = "filament usage is paused for an open spool question"

_lock = threading.Lock()


def ledger_path() -> Path:
    return _config.state_file(LEDGER_ENV, LEDGER_NAME, per_instance=True)


def planned_filament_mm(gcode_text: str) -> float | None:
    """The G-code's planned filament length in mm, from its own header.

    Orca writes the per-tool list ``; filament used [mm] = a, b``; its sum
    is the planned total. None when the header does not name it.
    """
    match = re.search(r"^;\s*filament used \[mm\]\s*=\s*([0-9.,;\s]+)$", gcode_text or "", re.M)
    if not match:
        match = re.search(r"^;\s*total filament (?:used|length) \[mm\]\s*[=:]\s*([0-9.]+)", gcode_text or "", re.M)
    if not match:
        return None
    total = 0.0
    for item in re.split(r"[;,]", match.group(1)):
        item = item.strip()
        if not item:
            continue
        try:
            total += float(item)
        except ValueError:
            return None
    return round(total, 2) if total > 0 else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _spool_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip()) or None
    return None


def charge_key(context: dict[str, Any] | None) -> str | None:
    """This print's identity in the ledger, or None when the context has none."""
    context = context or {}
    job_key = context.get("job_key")
    run = context.get("print_number") or context.get("started_at")
    if not job_key or not run:
        return None
    return f"{job_key}#{run}"


def slot_plan(context: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """Per-tool (slot, launch-bound spool, planned grams), or None for a non-multi-slot job.

    Only a job whose tools map to two or more distinct slots qualifies: a
    one-slot job keeps Moonraker's active spool and is charged there.
    """
    context = context or {}
    mapping = context.get("tool_to_physical_slot")
    if not isinstance(mapping, dict) or not mapping:
        return None
    slots = {value for value in mapping.values() if isinstance(value, int) and not isinstance(value, bool)}
    if len(slots) < 2:
        return None
    settings = context.get("slicer_settings") or {}
    grams = settings.get("filament_used_g_list") or []
    materials = settings.get("mapped_materials") or {}
    rows = []
    for tool in sorted(mapping, key=lambda t: int(t) if str(t).isdigit() else 99):
        slot = mapping[tool]
        record = materials.get(str(tool)) if isinstance(materials, dict) else None
        index = int(tool) if str(tool).isdigit() else None
        planned = _number(grams[index]) if index is not None and index < len(grams) else None
        rows.append({"tool": str(tool), "slot": slot,
                     "spool_id": _spool_id((record or {}).get("spoolman_id")),
                     "planned_g": planned})
    return rows


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, data: dict[str, Any]) -> None:
    if len(data) > LEDGER_KEEP:
        newest = sorted(data.items(), key=lambda kv: float((kv[1] or {}).get("recorded_at") or 0))
        data = dict(newest[-LEDGER_KEEP:])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _ratio(outcome: str, actual_mm: Any, planned_mm: Any) -> float | None:
    """The share of the planned grams this print used; None when unknown."""
    if outcome == "complete":
        return 1.0
    actual, planned = _number(actual_mm), _number(planned_mm)
    if actual is None or planned is None or planned <= 0 or actual < 0:
        return None
    return min(1.0, actual / planned)


def _new_entry(key: str, context: dict[str, Any], plan: list[dict[str, Any]], outcome: str,
               ratio: float | None, suspended: bool, actual_mm: Any, planned_mm: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "job_key": context.get("job_key"), "outcome": outcome, "recorded_at": time.time(),
        "actual_mm": _number(actual_mm), "planned_mm": _number(planned_mm),
        "ratio": None if ratio is None else round(ratio, 4), "slots": []}
    if suspended:
        entry.update(status="skipped", reason=SUSPENDED_REASON)
    elif ratio is None:
        entry.update(status="skipped", reason="actual filament use unknown for a stopped print")
    for row in plan:
        slot = dict(row)
        if suspended or ratio is None:
            slot["status"] = "skipped"
        elif slot["spool_id"] is None:
            slot.update(status="skipped", reason="no spool bound to the slot at launch")
        elif slot["planned_g"] is None:
            slot.update(status="skipped", reason="the G-code names no grams for this tool")
        else:
            slot["grams"] = round(slot["planned_g"] * ratio, 2)
            slot["status"] = "planned" if slot["grams"] > 0 else "skipped"
        entry["slots"].append(slot)
    return entry


def _charge_slots(path: Path, ledger: dict[str, Any], key: str, request: Callable[..., Any],
                  base_url: str | None) -> None:
    request = request or spoolman_client.request
    entry = ledger[key]
    for slot in entry["slots"]:
        if slot.get("status") not in ("planned", "pending"):
            continue
        # Mark the slot before the call: a crash mid-call leaves "charging",
        # which is never retried, so one charge can never land twice.
        slot["status"] = "charging"
        _save(path, ledger)
        try:
            request("PUT", f"/api/v1/spool/{int(slot['spool_id'])}/use",
                    {"use_weight": slot["grams"]}, base_url=base_url, timeout=8)
        except spoolman_client.SpoolmanUnavailable as exc:
            # Spoolman answered with a client error (no such spool, archived):
            # retrying cannot help. Anything else is an outage: retry later.
            final = exc.status is not None and 400 <= int(exc.status) < 500
            slot.update(status="failed" if final else "pending", error=str(exc)[:200])
        except Exception as exc:  # never raise into the print lifecycle
            slot.update(status="pending", error=f"{exc!r}"[:200])
        else:
            slot.update(status="charged", charged_at=time.time())
            slot.pop("error", None)
        _save(path, ledger)
    if entry.get("reason"):
        return  # skipped as a whole when it was recorded
    states = {slot.get("status") for slot in entry["slots"]}
    if "pending" in states:
        entry["status"] = "pending"
    elif "charged" in states:
        entry["status"] = "charged" if states <= {"charged", "skipped"} else "partial"
    else:
        entry["status"] = "failed" if "failed" in states else "skipped"
    _save(path, ledger)


def charge_finished_print(context: dict[str, Any] | None, outcome: str, *, actual_mm: Any = None,
                          planned_mm: Any = None, suspended: bool = False,
                          request: Callable[..., Any] | None = None,
                          base_url: str | None = None, path: Path | None = None) -> dict[str, Any] | None:
    """Charge each mapped slot's launch-bound spool for a finished multi-slot print.

    ``outcome`` is "complete" for a finished print; anything else is a
    stopped print, charged in proportion to ``actual_mm`` over the planned
    total (``planned_mm``, else the context's ``planned_filament_mm``).
    ``suspended`` (an open spool question pausing usage) charges nothing.
    Returns the ledger entry, or None for a job this does not apply to.
    Idempotent per print: a second call returns the recorded entry and
    only retries slots still pending on a Spoolman outage.
    """
    context = context or {}
    plan = slot_plan(context)
    key = charge_key(context)
    if plan is None or key is None:
        return None
    path = path or ledger_path()
    with _lock:
        ledger = _load(path)
        if key not in ledger:
            planned = planned_mm if _number(planned_mm) else context.get("planned_filament_mm")
            ratio = _ratio(outcome, actual_mm, planned)
            ledger[key] = _new_entry(key, context, plan, outcome, ratio, suspended, actual_mm, planned)
            _save(path, ledger)
        _charge_slots(path, ledger, key, request, base_url)
        return json.loads(json.dumps(ledger[key]))


def usage_suspended() -> bool:
    """Whether an open spool question pauses filament usage on this printer.

    Reads the listener's runout question record (the same file and the
    same rule as the listener's load_runout_pause: a runout or swap
    question with usage_suspended set, which never expires).
    """
    path = _config.state_file("RUNOUT_PAUSE_STATE", "runout_pause.json", per_instance=True)
    record = _load(path)
    return record.get("cause") in ("runout", "swap") and bool(record.get("usage_suspended"))


def _iso_seconds(value: Any) -> float | None:
    from datetime import datetime, timezone
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


# The launch records its start a little before Moonraker's job starts
# (upload, then heating); an hour of slack covers that without reaching a
# reprint of the same file, which starts after this entry ended.
_CONTEXT_START_SLACK_S = 3600


# Moonraker legitimately charges a little the print's own filament_used does
# not count (an IFS load, a manual purge): a charge is a runaway only past
# the print's grams plus the larger of these.
GUARD_SLACK_G = 15.0
GUARD_SLACK_RATIO = 0.25
# A baseline older than this before the print's start belongs to another launch.
_BASELINE_MAX_AGE_S = 6 * 3600


def launch_baseline(spool_id: Any, *, request: Callable[..., Any] | None = None,
                    base_url: str | None = None) -> dict[str, Any] | None:
    """The spool's used weight just before a single-slot print starts, or None.

    Read after the launch points Moonraker at the spool and before the
    start, so nothing this print extrudes is in it. None when Spoolman does
    not answer: the end check is then skipped, never guessed.
    """
    sid = _spool_id(spool_id)
    if sid is None:
        return None
    request = request or spoolman_client.request
    try:
        spool = request("GET", f"/api/v1/spool/{sid}", base_url=base_url, timeout=8)
    except Exception:
        return None
    used = _number((spool or {}).get("used_weight")) if isinstance(spool, dict) else None
    if used is None:
        return None
    return {"spool_id": sid, "used_weight": round(used, 4), "recorded_at": time.time()}


def grams_from_mm(mm: Any, context: dict[str, Any] | None) -> float | None:
    """Filament length in mm as grams, from the print's own density and diameter."""
    length = _number(mm)
    if length is None or length < 0:
        return None
    context = context or {}
    density = _number(context.get("filament_density")) or 1.24
    diameter = _number(context.get("filament_diameter_mm")) or 1.75
    return length * 3.141592653589793 * (diameter / 2) ** 2 * density / 1000.0


def _baseline_for(context: dict[str, Any]) -> dict[str, Any] | None:
    baseline = context.get("spool_usage_baseline")
    if not isinstance(baseline, dict) or _spool_id(baseline.get("spool_id")) is None:
        return None
    if _number(baseline.get("used_weight")) is None:
        return None
    started = _iso_seconds(context.get("started_at"))
    recorded = _number(baseline.get("recorded_at"))
    if started is not None and recorded is not None and not (
            started - _BASELINE_MAX_AGE_S <= recorded <= started + _BASELINE_MAX_AGE_S):
        return None
    return baseline


def _expected_grams(context: dict[str, Any], outcome: str, actual_mm: Any) -> float | None:
    grams = grams_from_mm(actual_mm, context) if _number(actual_mm) else None
    if grams is not None or outcome != "complete":
        return grams
    listed = [_number(g) for g in (context.get("slicer_settings") or {}).get("filament_used_g_list") or []]
    if listed and all(g is not None for g in listed):
        return float(sum(listed))
    return _number(context.get("estimated_filament_g"))


def guard_single_slot(context: dict[str, Any] | None, outcome: str, *, actual_mm: Any = None,
                      suspended: bool = False, request: Callable[..., Any] | None = None,
                      base_url: str | None = None, path: Path | None = None) -> dict[str, Any] | None:
    """Undo a runaway Moonraker charge on a finished single-slot print (#1374).

    ``actual_mm`` is the filament the print extruded (print_stats or the
    history entry's ``filament_used``); a completed print without it falls
    back to the sliced grams. When the spool's used weight grew by more
    than those grams plus the slack, the used weight is written back to the
    launch baseline plus the print's grams. Returns the ledger entry, or
    None for a print this does not apply to (multi-slot, no baseline).
    Idempotent per print.
    """
    context = context or {}
    key = charge_key(context)
    baseline = _baseline_for(context)
    if key is None or baseline is None or slot_plan(context) is not None:
        return None
    path = path or ledger_path()
    request = request or spoolman_client.request
    sid = int(baseline["spool_id"])
    before = float(baseline["used_weight"])
    with _lock:
        ledger = _load(path)
        if key in ledger:
            return json.loads(json.dumps(ledger[key]))
        entry: dict[str, Any] = {"kind": "single_slot", "job_key": context.get("job_key"),
                                 "outcome": outcome, "recorded_at": time.time(), "spool_id": sid,
                                 "baseline_used_g": round(before, 2)}
        expected = _expected_grams(context, outcome, actual_mm)
        if suspended:
            entry.update(status="skipped", reason=SUSPENDED_REASON)
        elif expected is None:
            entry.update(status="skipped", reason="actual filament use unknown")
        else:
            entry["expected_g"] = round(expected, 2)
            try:
                spool = request("GET", f"/api/v1/spool/{sid}", base_url=base_url, timeout=8)
                now = _number((spool or {}).get("used_weight"))
            except Exception as exc:
                spool, now = None, None
                entry["error"] = str(exc)[:200]
            if now is None:
                entry.update(status="skipped", reason="Spoolman did not answer at the end of the print")
            else:
                observed = now - before
                entry["observed_g"] = round(observed, 2)
                final = now
                if observed > expected + max(GUARD_SLACK_G, expected * GUARD_SLACK_RATIO):
                    target = round(before + expected, 2)
                    try:
                        request("PATCH", f"/api/v1/spool/{sid}", {"used_weight": target},
                                base_url=base_url, timeout=8)
                    except Exception as exc:
                        entry.update(status="overcharged", error=str(exc)[:200])
                    else:
                        entry.update(status="corrected", corrected_used_g=target)
                        final = target
                else:
                    entry["status"] = "ok"
                initial = _number((spool or {}).get("initial_weight"))
                if initial and final > initial:
                    entry["over_initial"] = True
        ledger[key] = entry
        _save(path, ledger)
        return json.loads(json.dumps(entry))


def charge_history_job(job: dict[str, Any] | None, context: dict[str, Any] | None, *,
                       suspended: bool | None = None, request: Callable[..., Any] | None = None,
                       base_url: str | None = None, path: Path | None = None) -> dict[str, Any] | None:
    """Charge a multi-slot print that finished while the listener was down.

    ``job`` is the Moonraker history entry the announcer's missed-finish
    check found; ``context`` is the print context for its file. The
    context is used only when it is this print's: its start falls between
    an hour before the entry's start and the entry's end, so a later
    reprint of the same file is never charged for it. A completed entry
    charges the sliced grams; a cancelled one is scaled by the entry's
    ``filament_used``. Same ledger as the live end, so whichever comes
    first charges and the other is a no-op.
    """
    job, context = job or {}, context or {}
    status = job.get("status")
    if status not in ("completed", "cancelled"):
        return None
    started = _iso_seconds(context.get("started_at"))
    job_start, job_end = _number(job.get("start_time")), _number(job.get("end_time"))
    if started is None or job_start is None or job_end is None:
        return None
    if not (job_start - _CONTEXT_START_SLACK_S <= started <= job_end):
        return None
    if slot_plan(context) is None:
        return guard_single_slot(
            context, "complete" if status == "completed" else "stopped",
            actual_mm=job.get("filament_used"),
            suspended=usage_suspended() if suspended is None else suspended,
            request=request, base_url=base_url, path=path)
    planned = ((job.get("metadata") or {}).get("filament_total")
               if not context.get("planned_filament_mm") else None)
    return charge_finished_print(
        context, "complete" if status == "completed" else "stopped",
        actual_mm=job.get("filament_used"), planned_mm=planned,
        suspended=usage_suspended() if suspended is None else suspended,
        request=request, base_url=base_url, path=path)


def retry_pending(*, request: Callable[..., Any] | None = None,
                  base_url: str | None = None, path: Path | None = None) -> list[str]:
    """Retry every slot a Spoolman outage left pending. Returns the retried keys."""
    path = path or ledger_path()
    retried = []
    with _lock:
        ledger = _load(path)
        for key, entry in list(ledger.items()):
            if isinstance(entry, dict) and any(
                    (slot or {}).get("status") == "pending" for slot in entry.get("slots") or []):
                _charge_slots(path, ledger, key, request, base_url)
                retried.append(key)
    return retried


def entry_for(context: dict[str, Any] | None, path: Path | None = None) -> dict[str, Any] | None:
    key = charge_key(context)
    if key is None:
        return None
    entry = _load(path or ledger_path()).get(key)
    return entry if isinstance(entry, dict) else None


def _grams_text(value: Any) -> str:
    return f"{float(value):.2f}".rstrip("0").rstrip(".") + " g"


def _single_slot_line(entry: dict[str, Any]) -> str:
    """The finish-post line for a single-slot check: silent unless something is wrong."""
    sid = entry.get("spool_id")
    status = entry.get("status")
    if status == "corrected":
        line = (f"Spool usage: Moonraker charged spool #{sid} {_grams_text(entry['observed_g'])} "
                f"for a print that used {_grams_text(entry['expected_g'])}; corrected to "
                f"{_grams_text(entry['expected_g'])}.")
    elif status == "overcharged":
        line = (f"Spool usage: Moonraker charged spool #{sid} {_grams_text(entry['observed_g'])} "
                f"for a print that used {_grams_text(entry['expected_g'])}, and the correction "
                f"failed ({entry.get('error') or 'Spoolman refused'}); fix the spool's weight.")
    else:
        line = ""
    if entry.get("over_initial"):
        warn = (f"Spool #{sid} now shows more filament used than it started with; "
                "correct its weight if it is not empty.")
        line = f"{line} {warn}".strip() if line else f"Spool usage: {warn}"
    return line


def summary_line(entry: dict[str, Any] | None) -> str:
    """One finish-post line naming what each slot's spool was charged, or ''."""
    if not entry:
        return ""
    if entry.get("kind") == "single_slot":
        return _single_slot_line(entry)
    if entry.get("reason") == SUSPENDED_REASON:
        return f"Spool usage: not charged, {SUSPENDED_REASON}."
    if entry.get("status") == "skipped" and entry.get("reason"):
        return f"Spool usage: not charged, {entry['reason']}."
    parts = []
    for slot in entry.get("slots") or []:
        status = slot.get("status")
        where = f"slot {slot.get('slot')}"
        if status == "charged":
            parts.append(f"{where} spool #{slot['spool_id']} {_grams_text(slot['grams'])}")
        elif status == "pending":
            parts.append(f"{where} spool #{slot['spool_id']} {_grams_text(slot['grams'])} "
                         "(not yet charged: Spoolman did not answer, retried later)")
        elif status == "charging":
            parts.append(f"{where} spool #{slot['spool_id']} {_grams_text(slot['grams'])} "
                         "(charge unconfirmed: check the spool in Spoolman)")
        elif status == "failed":
            parts.append(f"{where} spool #{slot['spool_id']} not charged ({slot.get('error') or 'Spoolman refused'})")
    if not parts:
        return ""
    return "Spool usage: " + "; ".join(parts)
