#!/usr/bin/env python3
"""Durable Print Handy print records.

Every print gets one self-contained JSON record in the records folder
(``<records vault>/Prints/print-NNNN.json``, named by its permanent print
number when one was assigned at launch). The watcher supplies confirmed
Moonraker data and, when available, a JSON context sidecar through
PRINT_HANDY_CONTEXT_PATH. The sidecar is the only source for user
confirmation, source selection, author-directed supports, and Discord session
identity. Missing values stay absent from the record; nothing is invented.
No slicing, support generation, printer control, or gateway operation happens
here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import _config

VAULT_ROOT = _config.records_vault()
PRINTS_DIR = VAULT_ROOT / "Prints"
PLANS_DIR = VAULT_ROOT / "Plans"
DEFAULT_CHAT_ID = _config.discord_chat_id()
CLEANUP_DELAY_SECONDS = 5 * 60
NATIVE_PRINT_OPTIONS = (
    "Bed clear: upload and start print",
    "Not clear: do not upload or start",
    "Keep sliced only",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_utc(value: Any) -> str | None:
    """Normalize an epoch timestamp, datetime, or ISO string to an ISO-8601 UTC Z string.

    Returns None for empty, invalid, or boolean values. Naive datetimes/strings
    are assumed to be UTC.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            dt = value.replace(tzinfo=timezone.utc)
        else:
            dt = value.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    s = str(value).strip()
    if not s:
        return None
    try:
        num = float(s)
        if num > 0:
            return datetime.fromtimestamp(num, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OSError):
        return None


TERMINAL_STATUS = {"complete": ("completed", True), "cancelled": ("cancelled", False), "error": ("failed", False), "printing": ("printing", None)}


def _hm(seconds: Any) -> str | None:
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return None
    return f"{total // 3600}h {total % 3600 // 60}m"


def _grams(mm: Any, context: Mapping[str, Any]) -> float | None:
    try:
        d, rho = float(context["filament_diameter_mm"]), float(context["filament_density"])
        return round(float(mm) * 3.141592653589793 * (d / 2) ** 2 * rho / 1000, 1)
    except (KeyError, TypeError, ValueError):
        return None


def _safe_slug(value: str, fallback: str = "print") -> str:
    value = re.sub(r"[^A-Za-z0-9._]+", "-", str(value or "").strip()).strip(".-")
    return value[:100] or fallback


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _iso_from_epoch(value: Any) -> str | None:
    """ISO-Z string for a Moonraker epoch timestamp, or None."""
    return normalize_utc(value)


def load_context(job_key: str, path: Path | None = None) -> dict[str, Any]:
    """Load the optional Print Handy confirmation context without guessing.

    Supported sidecar shapes are either one job object, or
    ``{"jobs": {"gcode/path": {...}}}``. A nonmatching or malformed file is
    treated as absent so a bad sidecar cannot stop completion announcements.
    """
    configured = path or _config.state_file("PRINT_HANDY_CONTEXT_PATH", "print_handy_context.json")
    try:
        payload = json.loads(configured.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    jobs = payload.get("jobs")
    if isinstance(jobs, dict) and isinstance(jobs.get(job_key), dict):
        return dict(jobs[job_key])
    if payload.get("job_key") in (None, job_key):
        return payload
    return {}


def latest_completed_record(job_key: str) -> dict[str, Any]:
    """The newest finished record for a job key, or {} when none is written.

    Completion facts (duration above all) live in the completed record,
    not the start context; readers that need them after the fact — the
    vertical short's overlay (P2-112) — look here. In-progress records
    never answer: their duration is not the print's yet.
    """
    best: dict[str, Any] = {}
    best_at = ""
    try:
        paths = list(PRINTS_DIR.glob("*.json"))
    except OSError:
        return {}
    for path in paths:
        record = _read_json_record(path)
        if not record or record.get("job_key") != job_key:
            continue
        if record.get("status") in (None, "printing"):
            continue
        at = str(record.get("completed_at") or "")
        if not best or at >= best_at:
            best, best_at = record, at
    return best


def latest_record_for_file(filename: str) -> dict[str, Any]:
    """The newest record (finished or in progress) for a gcode file name, or {} (#906).

    Backends without a Moonraker job key (Bambu, Elegoo) name their delivered timelapse from
    this. Matches the record's gcode_file or job_key by exact value or base name.
    """
    if not filename:
        return {}
    exact_wanted = str(filename)
    base_wanted = Path(exact_wanted).name
    best: dict[str, Any] = {}
    best_key = (-1, "")
    try:
        paths = list(PRINTS_DIR.glob("*.json"))
    except OSError:
        return {}
    for path in paths:
        record = _read_json_record(path)
        if not record:
            continue
        names = {str(record.get("gcode_file") or ""), str(record.get("job_key") or "")}
        names.discard("")
        # Tier 1: the exact value. Tier 0: base name only, used when no exact match exists.
        if exact_wanted in names:
            tier = 1
        elif base_wanted in {Path(n).name for n in names}:
            tier = 0
        else:
            continue
        at = str(record.get("started_at") or record.get("completed_at") or "")
        if (tier, at) >= best_key:
            best, best_key = record, (tier, at)
    return best


def _valid_print_number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _registry_print_number(context: Mapping[str, Any]) -> int | None:
    """The permanent number the launch registry already assigned this job, or None.

    Read-only: numbers are minted at launch (print_control.assign_print_numbers);
    a record never assigns one. The job is located through the slicer settings'
    local G-code path, whose parent chain holds the job's JOB.json.
    """
    settings = context.get("slicer_settings")
    local = settings.get("local_gcode") if isinstance(settings, Mapping) else None
    if not local:
        return None
    job_path = next((parent / "JOB.json" for parent in Path(str(local)).parents
                     if (parent / "JOB.json").is_file()), None)
    if job_path is None:
        return None
    try:
        from . import print_control
        numbers = json.loads(print_control.PRINT_NUMBERS.read_text(encoding="utf-8")).get("numbers") or {}
    except (OSError, ValueError, AttributeError, ImportError):
        return None
    if not isinstance(numbers, dict):
        return None
    return _valid_print_number(numbers.get(str(job_path.resolve())))


def _spool_fields(spool: Any) -> dict[str, Any]:
    if not isinstance(spool, dict):
        return {}
    filament = spool.get("filament") if isinstance(spool.get("filament"), dict) else {}
    vendor = filament.get("vendor") if isinstance(filament.get("vendor"), dict) else {}
    return {
        "spoolman_spool_id": _first(spool, "id", "spool_id"),
        "filament_vendor": _first(spool, "vendor", "vendor_name") or _first(vendor, "name"),
        "filament_material": _first(spool, "material", "material_name") or _first(filament, "material", "name"),
        "filament_color": _first(spool, "color", "color_name") or _first(filament, "color"),
    }


def _resolved_title(filename):
    """Model title for a hashed Moonraker filename, so records never use the hash as a name."""
    try:
        from . import discord_print_summary
        job, _plate = discord_print_summary.find_job(filename)
        return discord_print_summary.safe_label(job["title"]) if job and job.get("title") else None
    except Exception:
        return None


def build_record_data(
    filename: str,
    stats: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    active_spool: Mapping[str, Any] | None = None,
    completed_at: str | None = None,
) -> dict[str, Any]:
    """Build a record from confirmed data only."""
    stats = stats or {}
    metadata = metadata or {}
    context = dict(context or {})
    spool = active_spool or context.get("active_spool") or {}
    # P2-155: a launch-time accounting failure rides the context from the job
    # record (record_start copies JOB.json's marker). It marks the record
    # pending even when Spoolman answers at completion — that answer names
    # the spool Moonraker kept charging, not the spool the job launched for.
    launch_pending = context.get("spoolman_usage_pending")
    launch_pending = launch_pending if isinstance(launch_pending, dict) and launch_pending else None
    usage_pending = bool(spool.get("spoolman_unavailable")) or launch_pending is not None
    intended_id = launch_pending.get("spool_id") if launch_pending else None
    if isinstance(intended_id, int) and not isinstance(intended_id, bool) and intended_id > 0:
        recorded = context.get("active_spool")
        if isinstance(recorded, dict) and recorded:
            spool = recorded  # attribute the print to its intended spool
    spool_fields = _spool_fields(spool)
    slicer_settings = _first(metadata, "slicer_settings", "slicer_settings_json") or context.get("slicer_settings")
    if not isinstance(slicer_settings, (dict, list, str, int, float, bool)):
        slicer_settings = None
    tree_supports = context.get("author_directed_tree_supports")
    if tree_supports is None:
        tree_supports = context.get("tree_supports")
    selected_option = context.get("selected_option")
    if selected_option not in NATIVE_PRINT_OPTIONS:
        selected_option = None
    native = str(_first(stats, "state") or "complete").lower()
    status, success = TERMINAL_STATUS.get(native, (native, None))
    used_mm = _first(stats, "filament_used") or _first(metadata, "filament_used")
    # P2-150: the record's identity (record_id, filename) hangs on
    # started_at. When the context does not carry one, Moonraker's
    # history start time — an epoch riding stats — names the same
    # physical print on every call; the per-call completion clock must
    # not mint a second record for a retried completion.
    raw_started = context.get("started_at") or _first(stats, "start_time", "print_start_time")
    started_at = normalize_utc(raw_started) or (str(raw_started) if raw_started else None)
    norm_completed = normalize_utc(completed_at) if completed_at else None
    from ..core import projects
    number = _valid_print_number(context.get("print_number")) or _registry_print_number(context)
    project_id = context.get("project_id")
    suffix = context.get("project_plate_suffix")
    return {
        "print_number": number,
        "project_id": project_id,
        "project_number": number if project_id else None,
        "project_run_id": context.get("project_run_id") if project_id else None,
        "project_plate_id": context.get("project_plate_id") if project_id else None,
        "project_plate_suffix": suffix if project_id else None,
        "project_plate_ref": projects.project_ref(number, suffix) if project_id else None,
        "project_source_kind": context.get("project_source_kind") if project_id else None,
        "project_component_ids": context.get("project_component_ids") if project_id else None,
        "project_part_keys": context.get("project_part_keys") if project_id else None,
        "status": status,
        "success": success,
        "title": context.get("title") or _resolved_title(filename),
        "started_at": started_at,
        "completed_at": None if status == "printing" else (norm_completed or completed_at or utc_now()),
        "hardware": context.get("hardware"),
        "estimated_time": context.get("estimated_time"),
        "estimated_filament_g": context.get("estimated_filament_g"),
        "filament_used_g": _grams(used_mm, context),
        "record_path": context.get("record_path"),
        "source_url": context.get("source_url"),
        "source_platform": context.get("source_platform"),
        "selected_option": selected_option,
        "selection_confirmation": context.get("selection_confirmation") if selected_option else None,
        "printer": context.get("printer") or context.get("printer") or "Klipper 3D Printer",
        "printer_profile": _first(context, "printer_profile") or _first(metadata, "printer_profile", "printer_model"),
        "gcode_file": filename,
        "filament_vendor": spool_fields.get("filament_vendor"),
        "filament_material": spool_fields.get("filament_material"),
        "filament_color": spool_fields.get("filament_color"),
        "spoolman_spool_id": spool_fields.get("spoolman_spool_id"),
        # #387: Spoolman did not answer at completion, so this job's usage
        # could not be confirmed against Spoolman. Recorded as pending for
        # later reconciliation; the completion itself never waits on it.
        # P2-155: a launch-time accounting failure is pending the same way,
        # even when the completion-time lookup succeeds.
        "spoolman_usage_pending": True if usage_pending else None,
        "filament_used_mm": used_mm,
        "print_duration_seconds": _first(stats, "print_duration", "total_duration") or _first(metadata, "print_duration"),
        "slicer_settings": slicer_settings,
        "author_directed_tree_supports": tree_supports,
        "physical_outcome": context.get("physical_outcome"),
        "follow_up": context.get("follow_up"),
        "multipart_plan_id": context.get("multipart_plan_id"),
        "part_id": context.get("part_id") or filename,
        "required_parts": context.get("required_parts"),
        "discord_session_id": context.get("discord_session_id"),
        "discord_chat_id": str(context.get("discord_chat_id") or DEFAULT_CHAT_ID),
        "moonraker_state": native,
        "job_key": context.get("job_key"),
        # #913: the camera watch schedule chosen at start (normal or
        # early_layer) and why a print counted as risky.
        "watch_mode": context.get("watch_mode"),
        "watch_risk": context.get("watch_risk"),
        **_stop_fields(filename, started_at, printer=context.get("printer_id") or context.get("printer")),
    }


def _stop_fields(filename: str, started_at: Any = None, printer: str | None = None) -> dict[str, Any]:
    """stop_reason and stop_capture (the sidecar path) captured when this print stopped.

    A capture older than this print's start belongs to an earlier print of the same file.
    """
    try:
        from . import stop_capture
        captured = stop_capture.load(filename, printer_id=printer)
    except Exception:
        return {}
    if not captured:
        return {}
    if started_at and str(captured.get("first_captured_at") or "") < str(started_at):
        return {}
    return {"stop_reason": captured.get("stop_reason") or None,
            "stop_capture": str(stop_capture.sidecar_path(filename, printer_id=printer))}


def _newest_record_path_for_file(filename: str, printer: str | None = None) -> Path | None:
    """Path of the newest record for a gcode file (same matching as latest_record_for_file)."""
    exact, base = str(filename or ""), Path(str(filename or "")).name
    best, best_key = None, (-1, "")
    try:
        paths = list(PRINTS_DIR.glob("*.json"))
    except OSError:
        return None
    for path in paths:
        record = _read_json_record(path)
        if not record:
            continue
        if printer:
            rec_printer = str(record.get("printer") or "")
            rec_id = str(record.get("printer_id") or "")
            if str(printer) not in (rec_printer, rec_id):
                continue
        names = {str(record.get("gcode_file") or ""), str(record.get("job_key") or "")} - {""}
        tier = 1 if exact in names else 0 if base in {Path(n).name for n in names} else -1
        if tier < 0:
            continue
        at = str(record.get("started_at") or record.get("completed_at") or "")
        if (tier, at) >= best_key:
            best, best_key = path, (tier, at)
    return best


def attach_stop_reason(filename: str, reason: str, capture_path: str, captured_at: str = "",
                       printer_id: str | None = None) -> str:
    """Add the captured stop reason to the newest record for `filename`; return its path or ''.

    An existing reason is kept: the first stop is the one that explains the print. A record
    that completed before the stop is an earlier print and is left alone; the stopped print's
    own record, written later, picks the reason up from the sidecar.
    """
    path = _newest_record_path_for_file(filename, printer=printer_id)
    record = _read_json_record(path) if path else None
    if record is None or path is None:
        return ""
    done = str(record.get("completed_at") or "")
    if captured_at and done and done < captured_at:
        return ""
    changed = False
    if reason and not record.get("stop_reason"):
        record["stop_reason"] = reason
        changed = True
    if capture_path and not record.get("stop_capture"):
        record["stop_capture"] = capture_path
        changed = True
    if changed:
        _atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return str(path)


# The fields a print record carries, in file order: the lookup schema. One
# self-contained JSON object per print answers what print N was, when it ran,
# on which printer, how it ended, what filament it used and how long it took,
# with jq or grep alone — no index or external app required.
_RECORD_FIELDS = (
    "print_number", "title", "status", "success", "started_at", "completed_at",
    "printer", "printer_profile", "gcode_file", "job_key", "source_platform",
    "source_url", "selected_option", "selection_confirmation", "hardware",
    "estimated_time", "estimated_filament_g", "filament_vendor",
    "filament_material", "filament_color", "spoolman_spool_id",
    "spoolman_usage_pending",
    "filament_used_mm", "filament_used_g", "print_duration_seconds",
    "slicer_settings", "author_directed_tree_supports", "physical_outcome",
    "follow_up", "multipart_plan_id", "part_id",
    "project_id", "project_number", "project_run_id",
    "project_plate_id", "project_plate_suffix", "project_plate_ref",
    "project_source_kind", "project_component_ids", "project_part_keys",
    "print_attempt_id", "moonraker_state",
    "discord_chat_id", "watch_mode", "watch_risk", "stop_reason", "stop_capture",
)


def _record_id(data: Mapping[str, Any]) -> str:
    """Stable identity for one physical print: job + file + start instant."""
    key = f"{data.get('job_key') or ''}:{data.get('gcode_file') or ''}:{data.get('started_at') or data.get('completed_at') or ''}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def _record_json(data: Mapping[str, Any], cleanup_status: str = "pending") -> dict[str, Any]:
    record = {key: data.get(key) for key in _RECORD_FIELDS}
    record["record_id"] = _record_id(data)
    if data.get("project_id"):
        # One physical attempt is a distinct receipt, even for a reprinted
        # 82A. The human-facing plate alias remains 82A across attempts.
        record["print_attempt_id"] = record["record_id"]
    record["cleanup_status"] = cleanup_status
    # Absent data stays absent (never invented). print_number is the one
    # anchored key: always present, null when no number was ever assigned.
    return {key: value for key, value in record.items() if value is not None or key == "print_number"}


def _record_path_for(data: Mapping[str, Any], digest: str) -> Path:
    number = _valid_print_number(data.get("print_number"))
    if number is not None:
        suffix = data.get("project_plate_suffix") if data.get("project_id") else None
        if isinstance(suffix, str) and re.fullmatch(r"[A-Z]+", suffix):
            return PRINTS_DIR / f"print-{number:04d}-{suffix}.json"
        return PRINTS_DIR / f"print-{number:04d}.json"
    stamp = str(data.get("started_at") or data.get("completed_at") or utc_now())
    slug = _safe_slug(Path(str(data.get("gcode_file") or "print")).stem)
    return PRINTS_DIR / f"{stamp.replace(':', '').replace('-', '')[:15]}-{slug}-{digest}.json"


def _read_json_record(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _avoid_collision(path: Path, digest: str) -> Path:
    """A name already held by a different print (batch copies of one job share
    its number) gains that print's digest suffix; the same print keeps its name."""
    if not path.exists():
        return path
    existing = _read_json_record(path)
    if existing is not None and existing.get("record_id") == digest:
        return path
    return path.with_name(f"{path.stem}-{digest}.json")


def _write_json_record(path: Path, data: Mapping[str, Any], cleanup_status: str = "pending") -> None:
    _atomic_write(path, json.dumps(_record_json(data, cleanup_status), ensure_ascii=False, indent=2) + "\n")


def _plan_parts(required_parts: Any) -> list[dict[str, Any]]:
    if not isinstance(required_parts, list):
        return []
    result = []
    for item in required_parts:
        if isinstance(item, str) and item.strip():
            result.append({"id": item, "label": item, "status": "planned", "quantity": 1, "completed_quantity": 0})
        elif isinstance(item, dict):
            part_id = _first(item, "id", "part_id", "filename", "name")
            if part_id:
                quantity = item.get("quantity", item.get("required_quantity", 1))
                if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                    quantity = 1
                completed_quantity = item.get("completed_quantity", 0)
                if isinstance(completed_quantity, bool) or not isinstance(completed_quantity, int):
                    completed_quantity = 0
                completed_quantity = min(max(completed_quantity, 0), quantity)
                part = {"id": str(part_id), "label": item.get("label") or str(part_id),
                        "status": item.get("status") or "planned", "quantity": quantity,
                        "completed_quantity": completed_quantity}
                for key in ("plate_id", "plate", "run_id", "run", "optional", "required",
                            "alternative_group", "selected", "estimated_part_seconds",
                            "plate_estimate_seconds", "estimated_filament_g", "gcode_file", "job_json", "gcode_path"):
                    if key in item:
                        part[key] = item[key]
                if part["status"] == "completed":
                    part["completed_quantity"] = quantity
                result.append(part)
    return result


def remaining_plate_choices(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Remaining required parts the plan can start exactly (recorded JOB.json and G-code path), in plan order."""
    from .print_next import has_exact_start_target, remaining_parts

    return [part for part, _done in remaining_parts(plan) if has_exact_start_target(part)]


def format_multipart_summary(plan: Mapping[str, Any], completed_part_id: str | None = None,
                             picker: bool = False) -> str | None:
    """Render required-part progress by physical plate and run, without guessing estimates.

    ``picker=True`` means the caller posts an interactive plate picker, so no command text is shown.
    """
    raw_parts = plan.get("parts") if isinstance(plan, Mapping) else None
    parts = _plan_parts(raw_parts)
    if not parts:
        return None

    def placement(part: Mapping[str, Any]) -> str:
        run = _first(part, "run", "run_id")
        plate = _first(part, "plate", "plate_id")
        labels = []
        if run is not None:
            labels.append(f"Run {run}")
        if plate is not None:
            labels.append(f"Plate {plate}")
        return ", ".join(labels) if labels else "Run not recorded, plate not recorded"

    required = []
    variants = []
    for part in parts:
        alt_group = part.get("alternative_group")
        chosen = part.get("selected") is True
        optional = part.get("optional") is True or part.get("required") is False
        if optional or (alt_group and not chosen):
            variants.append(part)
        else:
            required.append(part)

    completed_lines = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for part in required:
        quantity = part["quantity"]
        completed = part["completed_quantity"]
        if part.get("status") == "completed":
            completed = quantity
        completed = min(max(completed, 0), quantity)
        if completed:
            completed_lines.append(f"{part['label']} x{completed}")
        if completed < quantity:
            groups.setdefault(placement(part), []).append({**part, "completed_quantity": completed})

    lines = ["**Print continuation:**"]
    if completed_lines:
        lines.append("- Completed required parts: " + "; ".join(completed_lines))
    else:
        lines.append("- Completed required parts: none recorded")

    remaining = [(group, rows) for group, rows in groups.items()]
    if not remaining:
        lines.append("- Remaining required parts: none. Job complete.")
    else:
        next_part = next((part for _, rows in remaining for part in rows), None)
        current_names = set()
        if completed_part_id:
            current_path = Path(str(completed_part_id))
            current_names = {str(completed_part_id), current_path.name, current_path.stem}
            current_names |= {name.replace("_", " ") for name in tuple(current_names)}
        current_part = next((part for part in required
                             if str(part.get("id")) in current_names
                             or str(part.get("label")) in current_names
                             or Path(str(part.get("gcode_file") or "")).name in current_names
                             or Path(str(part.get("last_gcode_file") or "")).name in current_names), None)
        current_plate = placement(current_part) if current_part else None
        if current_part or completed_part_id:
            finished_labels = [part["label"] for part in required
                               if str(part.get("id")) in current_names
                               or str(part.get("label")) in current_names
                               or Path(str(part.get("gcode_file") or "")).name in current_names
                               or Path(str(part.get("last_gcode_file") or "")).name in current_names]
            if finished_labels:
                lines.append("- Current print finished: " + ", ".join(finished_labels))
            if remaining:
                lines.append(f"- Current plate/run finished: {current_plate or 'plate/run not recorded'}")
                lines.append("- Remove the finished pieces and clear the bed before the next plate.")
        if next_part:
            lines.append(f"- Next required part: {next_part['label']} ({placement(next_part)})")
        lines.append("- All remaining required parts:")
        known_total = 0
        unknown_groups = []
        for group, rows in remaining:
            lines.append(f"  - {group}:")
            per_part_estimates = [part.get("estimated_part_seconds") for part in rows]
            if all(isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
                   for value in per_part_estimates):
                estimate = sum(value * (part["quantity"] - part["completed_quantity"])
                               for part, value in zip(rows, per_part_estimates))
                estimate_label = f"part estimates ~{_hm(estimate)}"
                known_total += estimate
            else:
                shared = [float(value) for part in rows
                          if isinstance((value := part.get("plate_estimate_seconds")), (int, float))
                          and not isinstance(value, bool) and value >= 0]
                if len(shared) == len(rows) and shared:
                    estimate = max(shared)
                    estimate_label = f"shared plate estimate ~{_hm(estimate)}; these pieces share the plate"
                    known_total += estimate
                elif len(shared) == 1:
                    estimate = shared[0]
                    estimate_label = f"shared plate estimate ~{_hm(estimate)}; these pieces share the plate"
                    known_total += estimate
                elif shared:
                    estimate_label = "shared plate estimate inconsistent; duration unknown"
                    unknown_groups.append(group)
                else:
                    estimate_label = "duration unknown"
                    unknown_groups.append(group)
            lines.append(f"    - {estimate_label}")
            for part in rows:
                remaining_count = part["quantity"] - part["completed_quantity"]
                part_estimate = part.get("estimated_part_seconds")
                if part_estimate is None:
                    estimate_detail = ""
                elif isinstance(part_estimate, (int, float)) and not isinstance(part_estimate, bool) and part_estimate >= 0:
                    estimate_detail = f"; per-part estimate ~{_hm(part_estimate)}"
                else:
                    estimate_detail = "; per-part estimate unknown"
                lines.append(f"    - {part['label']}: {remaining_count} remaining{estimate_detail}")
        if unknown_groups:
            lines.append("- Combined remaining duration incomplete; unknown estimate for " + "; ".join(unknown_groups))
        else:
            lines.append(f"- Combined remaining duration: ~{_hm(known_total)}")
        # Remaining filament next to the remaining duration (#438), but only
        # when copies are in play: a one-of-each plan keeps today's wording.
        # Per-part grams are per copy, so they scale by the copies still to go,
        # exactly like the per-part duration above.
        remaining_rows = [part for _group, rows in remaining for part in rows]
        if any(part["quantity"] > 1 for part in remaining_rows):
            known_g, any_known, all_known = 0.0, False, True
            for part in remaining_rows:
                est = part.get("estimated_filament_g")
                if isinstance(est, (int, float)) and not isinstance(est, bool) and est >= 0:
                    known_g += float(est) * (part["quantity"] - part["completed_quantity"])
                    any_known = True
                else:
                    all_known = False
            if all_known:
                lines.append(f"- Combined remaining filament: ~{known_g:g} g")
            elif any_known:
                lines.append(f"- Combined remaining filament: at least ~{known_g:g} g (unknown for some remaining parts)")
            else:
                lines.append("- Combined remaining filament: unknown")
        next_artifact = str(next_part.get("gcode_file") or "") if next_part else ""
        exact_target = bool(next_part and next_part.get("job_json") and next_part.get("gcode_path"))
        plan_id = plan.get("plan_id") if isinstance(plan, Mapping) else None
        if exact_target and plan_id and picker:
            lines.append("- Start option: choose the next plate from the picker below, then tap Confirm bed is clear & print. "
                         "One tap starts that exact plate once. No automatic advance.")
        elif exact_target and plan_id:
            lines.append(f"- Start option: type `/print-next {plan_id}` for a Start next plate · bed is clear button. "
                         "One tap starts this exact plate once. No automatic advance.")
        elif next_artifact and Path(next_artifact).name == next_artifact:
            lines.append("- Start option: Start next plate · bed is clear; require a fresh confirmation for this exact artifact. No automatic advance.")

    if variants:
        lines.append("- Optional or alternative variants (not required): " + "; ".join(
            f"{part['label']} ({'optional' if part.get('optional') is True or part.get('required') is False else 'alternative set ' + str(part.get('alternative_group'))})"
            for part in variants))
    return "\n".join(lines)


def _bind_same_job_plates(parts: list[dict[str, Any]], data: Mapping[str, Any]) -> None:
    """Record exact start targets for parts that are plates of the job just printed.

    Only the JOB.json that owns the completed G-code is consulted, and a part is
    bound only when its basename matches exactly one plate in that job.
    """
    settings = data.get("slicer_settings")
    local = settings.get("local_gcode") if isinstance(settings, Mapping) else None
    if not local:
        return
    job_path = next((parent / "JOB.json" for parent in Path(str(local)).parents
                     if (parent / "JOB.json").is_file()), None)
    if job_path is None:
        return
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    plates = [str(row["gcode"]) for row in job.get("derived_plates") or []
              if isinstance(row, dict) and row.get("gcode")]
    if job.get("gcode") and str(job["gcode"]) not in plates:
        plates.append(str(job["gcode"]))
    for part in parts:
        name = str(part.get("gcode_file") or "")
        if part.get("job_json") or not name or Path(name).name != name:
            continue
        matches = [plate for plate in plates if Path(plate).name == name]
        if len(matches) == 1:
            part["job_json"] = str(job_path)
            part["gcode_path"] = matches[0]


def update_multipart_plan(data: Mapping[str, Any]) -> tuple[str | None, bool]:
    plan_id = data.get("multipart_plan_id")
    required = _plan_parts(data.get("required_parts"))
    if not plan_id and not required:
        return None, True
    if not plan_id:
        plan_id = _safe_slug(data.get("gcode_file"), "print-plan")
    plan_id = _safe_slug(str(plan_id), "print-plan")
    json_path = PLANS_DIR / f"{plan_id}.json"
    try:
        existing = json.loads(json_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        existing = {}
    parts = existing.get("parts") if isinstance(existing, dict) else None
    if not isinstance(parts, list) or required:
        by_id = {str(part.get("id")): part for part in (parts or []) if isinstance(part, dict) and part.get("id")}
        for part in required:
            existing_part = by_id.setdefault(str(part["id"]), part)
            # Exact start targets may arrive after the plan was first written.
            for key in ("job_json", "gcode_path", "gcode_file"):
                if part.get(key) and not existing_part.get(key):
                    existing_part[key] = part[key]
        parts = list(by_id.values())
    current_id = str(data.get("part_id") or data.get("gcode_file"))
    current_artifact = Path(str(data.get("gcode_file") or "")).stem
    completed_units = data.get("completed_quantity", data.get("part_quantity", 1))
    if isinstance(completed_units, bool) or not isinstance(completed_units, int) or completed_units < 1:
        completed_units = 1
    matched = False
    for part in parts:
        matches_id = str(part.get("id")) == current_id or str(part.get("label")) == current_id
        artifact_names = {str(part.get(key) or "") for key in ("gcode_file", "last_gcode_file")}
        artifact_names |= {Path(name).name for name in tuple(artifact_names) if name}
        artifact_stems = {Path(name).stem for name in artifact_names if name}
        matches_artifact = bool(current_artifact) and (str(data.get("gcode_file")) in artifact_names or current_artifact in artifact_stems)
        if matches_id or matches_artifact:
            quantity = part.get("quantity", 1)
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                quantity = 1
            already_completed = part.get("completed_quantity", 0)
            if isinstance(already_completed, bool) or not isinstance(already_completed, int):
                already_completed = 0
            part["quantity"] = quantity
            part["completed_quantity"] = min(quantity, max(0, already_completed) + completed_units)
            part["status"] = "completed" if part["completed_quantity"] >= quantity else "planned"
            part["completed_at"] = data.get("completed_at")
            part["last_gcode_file"] = data.get("gcode_file")
            matched = True
    if not matched and parts:
        artifact_matches = [part for part in parts
                            if current_artifact and Path(str(part.get("id") or "")).stem == current_artifact]
        if len(artifact_matches) == 1:
            part = artifact_matches[0]
            quantity = part.get("quantity", 1)
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                quantity = 1
            already_completed = part.get("completed_quantity", 0)
            if isinstance(already_completed, bool) or not isinstance(already_completed, int):
                already_completed = 0
            part["quantity"] = quantity
            part["completed_quantity"] = min(quantity, max(0, already_completed) + completed_units)
            part["status"] = "completed" if part["completed_quantity"] >= quantity else "planned"
            part["completed_at"] = data.get("completed_at")
            part["last_gcode_file"] = data.get("gcode_file")
            matched = True
    if not matched and parts and len(parts) == 1:
        quantity = parts[0].get("quantity", 1)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            quantity = 1
        already_completed = parts[0].get("completed_quantity", 0)
        if isinstance(already_completed, bool) or not isinstance(already_completed, int):
            already_completed = 0
        parts[0]["quantity"] = quantity
        parts[0]["completed_quantity"] = min(quantity, max(0, already_completed) + completed_units)
        parts[0]["status"] = "completed" if parts[0]["completed_quantity"] >= quantity else "planned"
        parts[0]["completed_at"] = data.get("completed_at")
        parts[0]["last_gcode_file"] = data.get("gcode_file")
    _bind_same_job_plates(parts, data)
    from ..core import projects
    number = _valid_print_number(data.get("project_number"))
    for idx, part in enumerate(parts):
        suffix = part.setdefault('project_plate_suffix', projects.plate_suffix(idx))
        display = projects.project_ref(number, suffix)
        if display:
            part['display_ref'] = display
    payload = {
        "plan_id": plan_id,
        "title": existing.get("title") if isinstance(existing, dict) else None,
        "project_id": data.get("project_id") or (existing.get("project_id") if isinstance(existing, dict) else None),
        "project_number": number or (existing.get("project_number") if isinstance(existing, dict) else None),
        "project_run_id": data.get("project_run_id") or (existing.get("project_run_id") if isinstance(existing, dict) else None),
        "source_kind": data.get("project_source_kind") or (existing.get("source_kind") if isinstance(existing, dict) else None),
        "source_url": data.get("source_url"),
        "source_platform": data.get("source_platform"),
        "updated_at": data.get("completed_at"),
        "parts": parts,
    }
    _atomic_write(json_path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return plan_id, bool(parts) and all(part.get("status") == "completed" for part in parts)


def plan_is_complete(plan_id: str) -> bool:
    path = PLANS_DIR / f"{_safe_slug(plan_id)}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    parts = payload.get("parts") if isinstance(payload, dict) else None
    return bool(parts) and all(part.get("status") == "completed" for part in parts)


def write_completed_record(data: Mapping[str, Any]) -> dict[str, Any]:
    """Write one idempotent JSON record and its multipart plan, then return cleanup facts."""
    plan_id, eligible = update_multipart_plan(data)
    digest = _record_id(data)
    target = _record_path_for(data, digest)
    existing = _read_json_record(target) if target.exists() else None
    if existing is not None and existing.get("record_id") != digest:
        target = _avoid_collision(target, digest)
        existing = _read_json_record(target) if target.exists() else None
    # A start record (status printing) is finalized in place or renamed to the
    # numbered name; a final record for this same print is immutable.
    final_exists = existing is not None and existing.get("record_id") == digest and existing.get("status") != "printing"
    if not final_exists:
        _write_json_record(target, data)
        started = Path(str(data.get("record_path") or ""))
        if data.get("record_path") and started.parent == PRINTS_DIR and started != target:
            start_record = _read_json_record(started)
            if start_record is not None and start_record.get("record_id") == digest:
                try:
                    started.unlink()
                except OSError:
                    pass
    return {
        "record_path": str(target),
        "plan_id": plan_id,
        "cleanup_eligible": eligible,
        "discord_session_id": data.get("discord_session_id"),
        "discord_chat_id": data.get("discord_chat_id") or DEFAULT_CHAT_ID,
    }


def write_start_record(context: Mapping[str, Any]) -> str:
    """Create the in-progress JSON record for a just-started print and return its path."""
    data = build_record_data(str(context["job_key"]), stats={"state": "printing"}, context=context)
    digest = _record_id(data)
    path = _avoid_collision(_record_path_for(data, digest), digest)
    # No multipart plan update here: a start must never mark a part complete.
    _write_json_record(path, data)
    return str(path)


def update_cleanup_record(record_path: str, status: str, session_id: str | None = None, detail: str = "") -> None:
    """Record the session-cleanup outcome on the print's JSON record.

    A path that is not a JSON record (a legacy Markdown note from before
    this format) is left entirely untouched: historical notes are history.
    """
    path = Path(record_path)
    record = _read_json_record(path)
    if record is None:
        return
    record["cleanup_status"] = status
    if status == "archived":
        record["cleanup_at"] = utc_now()
    if session_id:
        record["cleanup_session"] = session_id
    if detail:
        record["cleanup_detail"] = detail
    _atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")


def native_option_labels() -> tuple[str, str, str]:
    """Canonical Discord picker labels; callers must render these as buttons."""
    return NATIVE_PRINT_OPTIONS
