"""Wrapped v2 joins (#981): receipts, JOB.json, Spoolman, maintenance.

Everything here is a fail-soft read beside the print records. A missing
or corrupt source is skipped and named; it never changes a record
number and never raises into a page.

The receipt/JOB join key is ``print_number``, the post-#975 shape:
``print_now`` reconciles the owning intake receipt on a confirmed
start and records ``print_number`` / ``print_id`` on it
(``core/model_job._record_intake_receipt_started``), JOB.json can carry
its own ``print_number``, and a receipt's ``job_json`` path reaches
the JOB.json that holds the job's title and ``filament_selection``.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import gcode_3mf
from .print_stats import record_key

# Receipt statuses that mean the intake was sliced but the print never
# started (prepared_intake's lifecycle): these are the "prints you
# almost made". A receipt whose ``started`` flag is explicitly False
# counts the same way regardless of status spelling.
NEVER_STARTED_STATUSES = frozenset(
    {"SLICED", "SLICED_NOT_STARTED", "START_REFUSED"})


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value:  # NaN
        return None
    return float(value)


def _print_number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) \
        and value > 0 else None


def _read_json(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def load_receipts(receipts_dir):
    """(receipts, skipped_names) from an intake receipts directory."""
    directory = Path(receipts_dir) if receipts_dir else None
    if directory is None or not directory.is_dir():
        return [], []
    receipts, skipped = [], []
    for path in sorted(directory.glob("*.json")):
        payload = _read_json(path)
        if payload is None:
            skipped.append(path.name)
            continue
        receipts.append(payload)
    return receipts, skipped


def load_jobs(archive_dir):
    """(jobs, skipped_names): JOB.json dicts under ``<archive>/*/JOB.json``.

    Each job is returned with its path under ``_job_path`` (a join-only
    key, named with the underscore the codebase uses for path metadata).
    """
    directory = Path(archive_dir) if archive_dir else None
    if directory is None or not directory.is_dir():
        return [], []
    jobs, skipped = [], []
    for path in sorted(directory.glob("*/JOB.json")):
        payload = _read_json(path)
        if payload is None:
            skipped.append(str(path))
            continue
        jobs.append({**payload, "_job_path": str(path)})
    return jobs, skipped


def _job_gcode_paths(job):
    paths = [job.get("gcode")]
    paths += [row.get("gcode") for row in job.get("derived_plates") or []
              if isinstance(row, dict)]
    return [str(p) for p in paths if p]


def _native_color_rows(record):
    """The record's native per-tool split, or None when it has none."""
    settings = record.get("slicer_settings")
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except ValueError:
            settings = None
    if not isinstance(settings, dict):
        return None
    grams = settings.get("filament_used_g_list")
    if not isinstance(grams, list) or not grams:
        return None
    return grams


def _metadata_text(path):
    """The part of a G-code file its filament metadata lives in.

    :func:`gcode_3mf.plate_metadata` searches only the first and last
    ``_METADATA_SLICE`` characters (the slicer's header block and footer
    config, never the toolpath), so only those two windows are read: a
    50 MB archived file costs two small reads instead of a whole-file
    decode per record per page load. A file small enough to fit both
    windows is read whole.
    """
    window = gcode_3mf._METADATA_SLICE - 1
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        if size <= 2 * window:
            return handle.read().decode("utf-8", errors="replace")
        head = handle.read(window)
        handle.seek(size - window)
        tail = handle.read(window)
    return head.decode("utf-8", errors="replace") + "\n" + tail.decode("utf-8", errors="replace")


def _per_color_from_gcode(path):
    """{color: grams} from one archived G-code's header, or {}."""
    try:
        text = _metadata_text(path)
    except OSError:
        return {}
    try:
        meta = gcode_3mf.plate_metadata(text)
    except Exception:
        return {}
    out = {}
    for row in meta.get("filaments") or []:
        grams = _num(row.get("used_g"))
        if grams is None:
            continue
        color = str(row.get("color") or "unknown")
        out[color] = out.get(color, 0.0) + grams
    return out


def _stop_detail(record):
    """The stop-capture sidecar facts a record points at, or None.

    Only fields the sidecar actually carries (stop_capture.capture's
    shape): its stop reason, when it was first captured, and the newest
    event's trigger and printer state message. A missing or unreadable
    sidecar is None — step-1 behavior, nothing invented.
    """
    path = record.get("stop_capture")
    if not isinstance(path, str) or not path.strip():
        return None
    payload = _read_json(path)
    if payload is None:
        return None
    detail = {}
    if isinstance(payload.get("stop_reason"), str) and payload["stop_reason"].strip():
        detail["stop_reason"] = payload["stop_reason"].strip()
    if payload.get("first_captured_at"):
        detail["first_captured_at"] = str(payload["first_captured_at"])
    events = [e for e in payload.get("events") or [] if isinstance(e, dict)]
    if events:
        newest = events[-1]
        if newest.get("trigger"):
            detail["trigger"] = str(newest["trigger"])
        info = newest.get("printer_info") or {}
        if isinstance(info, dict) and info.get("state_message"):
            detail["printer_state_message"] = str(info["state_message"])[:300]
    return detail or None


def _norm(path):
    return os.path.normcase(os.path.normpath(str(path)))


def _record_job(record, jobs_by_path, jobs_by_number):
    """The archived job one record belongs to, or None.

    The record's own slicer-settings ``local_gcode`` names a file inside
    its job folder, so that is the join key: it is exact. A bare print
    number joins only when exactly one job carries it, because numbers
    are not unique in history from before per-launch numbering (a
    reprint reused the original's number): with two jobs on one number
    there is no way to tell which one this print was, and a wrong join
    would hand it another print's colors.
    """
    settings = record.get("slicer_settings")
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except ValueError:
            settings = None
    local = settings.get("local_gcode") if isinstance(settings, dict) else None
    if local:
        for parent in Path(str(local)).parents:
            job = jobs_by_path.get(_norm(parent / "JOB.json"))
            if job is not None:
                return job
    candidates = jobs_by_number.get(_print_number(record.get("print_number")))
    return candidates[0] if candidates and len(candidates) == 1 else None


def _gcode_signature(job):
    """[path, mtime_ns, size] of each G-code a job names: what a cached read depends on."""
    signature = []
    for gcode_path in _job_gcode_paths(job):
        try:
            stat = os.stat(gcode_path)
            signature.append([gcode_path, stat.st_mtime_ns, stat.st_size])
        except OSError:
            signature.append([gcode_path, None, None])
    return signature


def _load_derived(path):
    payload = _read_json(path) if path else None
    return payload if isinstance(payload, dict) else {}


def _save_derived(path, payload):
    """Best-effort atomic write of the derived-values cache; never raises."""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".{target.name}.{os.getpid()}.partial")
        partial.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(partial, target)
    except OSError:
        pass


def build_enrichments(records, *, archive_dir=None, receipts_dir=None, scope=None,
                      derived_cache=None):
    """The receipt/JOB enrichments for a set of print records.

    Returns a JSON-able dict (see the module docstring for the join
    key). ``records`` is the caller's full record list (a receipt is
    "never started" only when no record anywhere carries its number).
    ``scope`` is the records the per-record work is done for (stop
    sidecars and archived-G-code color splits), normally the selected
    year's: a Wrapped page for one year must not read every other year's
    files. The aggregate still filters ``per_print``,
    ``per_color_grams_by_print`` and ``support_waiver_print_numbers`` by
    what is in scope.

    ``derived_cache`` is a JSON file path. A record's per-color split
    recovered from an archived G-code is stored there against the
    G-code files' modification times and sizes, so each is computed once
    and recomputed only when the file changes.
    """
    records = [r for r in (records or []) if isinstance(r, dict)]
    scope_records = records if scope is None else [r for r in scope if isinstance(r, dict)]
    receipts, skipped_receipts = load_receipts(receipts_dir)
    jobs, skipped_jobs = load_jobs(archive_dir)
    jobs_by_path = {job["_job_path"]: job for job in jobs}
    jobs_by_number: dict[int, list] = {}
    for job in jobs:
        number = _print_number(job.get("print_number"))
        if number is not None:
            jobs_by_number.setdefault(number, []).append(job)
    jobs_by_norm_path = {_norm(job["_job_path"]): job for job in jobs}

    # Receipt -> job, and receipt -> print number (its own reconciled
    # number first, then its JOB.json's).
    def receipt_job(receipt):
        path = receipt.get("job_json")
        if isinstance(path, str) and path in jobs_by_path:
            return jobs_by_path[path]
        if isinstance(path, str):
            payload = _read_json(path)
            if payload is not None:
                return {**payload, "_job_path": path}
        return None

    def receipt_number(receipt):
        number = _print_number(receipt.get("print_number"))
        if number is None:
            number = _print_number(receipt.get("print_id"))
        if number is None:
            job = receipt_job(receipt)
            if job is not None:
                number = _print_number(job.get("print_number"))
        return number

    per_print: dict[int, dict] = {}
    waivers: list[int] = []
    almost_titles: list[str] = []
    almost_count = 0
    record_numbers = {_print_number(r.get("print_number")) for r in records}
    record_numbers.discard(None)

    for job in jobs:
        number = _print_number(job.get("print_number"))
        if number is None or len(jobs_by_number.get(number, ())) != 1:
            continue  # a number two jobs share says nothing about either
        entry = per_print.setdefault(number, {})
        if job.get("title"):
            entry["job_title"] = str(job["title"])
        if job.get("url"):
            entry["source_url"] = str(job["url"])
        selection = job.get("filament_selection")
        if isinstance(selection, dict) and selection:
            entry["filament_selection"] = selection
        elif isinstance(selection, list) and selection:
            entry["filament_selection"] = selection

    for receipt in receipts:
        job = receipt_job(receipt)
        number = receipt_number(receipt)
        if job is not None and number is not None:
            entry = per_print.setdefault(number, {})
            if job.get("title") and "job_title" not in entry:
                entry["job_title"] = str(job["title"])
            if job.get("url") and "source_url" not in entry:
                entry["source_url"] = str(job["url"])
            selection = job.get("filament_selection")
            if selection and "filament_selection" not in entry:
                entry["filament_selection"] = selection
        if isinstance(receipt.get("support_override"), dict) and number is not None:
            if number not in waivers:
                waivers.append(number)
        started = receipt.get("started")
        status = str(receipt.get("status") or "").strip().upper()
        never_started = started is False or (
            started is not True and status in NEVER_STARTED_STATUSES
            and number not in record_numbers)
        if never_started:
            almost_count += 1
            title = None
            if job is not None and job.get("title"):
                title = str(job["title"])
            if title and title not in almost_titles:
                almost_titles.append(title)

    # Per-color grams for archived jobs, and the stop sidecar detail —
    # both keyed by the record's own print number.
    per_color_by_print: dict[int, dict] = {}
    # The same joins keyed by each record's own identity: print_number is
    # not unique in pre-#988 history (a reprint reused the original's
    # number), so the by-number maps above merge two prints. Readers use
    # these first; the by-number maps stay for compatibility.
    per_color_by_record: dict[str, dict] = {}
    stop_by_record: dict[str, dict] = {}
    jobs_for_number = {number: list(rows) for number, rows in jobs_by_number.items()}
    for receipt in receipts:
        job = receipt_job(receipt)
        number = receipt_number(receipt)
        if job is not None and number is not None and number not in jobs_for_number:
            jobs_for_number[number] = [job]
    derived = _load_derived(derived_cache)
    derived_changed = False
    for record in scope_records:
        number = _print_number(record.get("print_number"))
        if number is None:
            continue
        detail = _stop_detail(record)
        if detail is not None:
            per_print.setdefault(number, {})["stop_detail"] = detail
            stop_by_record[record_key(record)] = detail
        if _native_color_rows(record) is not None:
            continue  # the record carries it natively; never double-count
        job = _record_job(record, jobs_by_norm_path, jobs_for_number)
        if job is None:
            continue
        key = record_key(record)
        signature = _gcode_signature(job)
        cached = derived.get(key)
        if isinstance(cached, dict) and cached.get("signature") == signature \
                and isinstance(cached.get("colors"), dict):
            merged = cached["colors"]
        else:
            merged = {}
            for gcode_path in _job_gcode_paths(job):
                for color, grams in _per_color_from_gcode(gcode_path).items():
                    merged[color] = merged.get(color, 0.0) + grams
            derived[key] = {"signature": signature, "colors": merged}
            derived_changed = True
        if merged:
            per_color_by_print[number] = merged
            per_color_by_record[key] = merged
    if derived_cache and derived_changed:
        _save_derived(derived_cache, derived)

    return {
        "per_print": per_print,
        "per_color_grams_by_print": per_color_by_print,
        "per_color_grams_by_record": per_color_by_record,
        "stop_detail_by_record": stop_by_record,
        "support_waiver_print_numbers": sorted(waivers),
        "almost_made": {
            "count": almost_count,
            "titles": almost_titles,
            # Receipts carry no creation date (the post-#975 shape has
            # lifecycle timestamps only for prints that started), so
            # this list is honestly not split by year.
            "scope_note": ("From intake receipts on file; receipts carry no "
                           "creation date, so this list is not split by year."),
        },
        "skipped_receipts": skipped_receipts,
        "skipped_jobs": skipped_jobs,
    }


def spoolman_lifetime(spools):
    """The all-time Spoolman block from a ``list_spools`` result.

    ``spools`` is the client's list (archived included), or None when
    Spoolman was unreachable/unconfigured — that is a named
    ``unavailable`` state with no numbers, never fake zeros. Lifetime
    used per spool is ``initial_weight - remaining_weight``; a spool
    missing either weight keeps its row with ``used_g`` null.
    """
    if not isinstance(spools, list):
        return {"state": "unavailable", "scope": "all_time", "spools": [],
                "total_used_g": None,
                "note": ("Spoolman lifetime: unavailable — Spoolman did not "
                         "answer, so no lifetime numbers are shown. This "
                         "block is all-time when it is shown, never per-year.")}
    rows = []
    total = 0.0
    for spool in spools:
        if not isinstance(spool, dict):
            continue
        filament = spool.get("filament") if isinstance(spool.get("filament"), dict) else {}
        vendor = filament.get("vendor") if isinstance(filament.get("vendor"), dict) else {}
        label_parts = [spool.get("vendor") or vendor.get("name"),
                       spool.get("material") or filament.get("material")
                       or filament.get("name"),
                       spool.get("color") or filament.get("color")]
        label = " ".join(str(p) for p in label_parts if p) or "unknown spool"
        initial = _num(spool.get("initial_weight"))
        remaining = _num(spool.get("remaining_weight"))
        used = None
        if initial is not None and remaining is not None:
            used = round(initial - remaining, 1)
            total += used
        rows.append({"id": spool.get("id"), "label": label,
                     "initial_weight": initial, "remaining_weight": remaining,
                     "used_g": used, "archived": bool(spool.get("archived"))})
    return {"state": "ok", "scope": "all_time", "spools": rows,
            "total_used_g": round(total, 1),
            "note": ("All-time Spoolman lifetime (initial minus remaining per "
                     "spool, archived spools included) — not a per-year number.")}


def _parse_instant(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00")
                                        .replace("z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def maintenance_block(records_vault, year=None, tz=None):
    """The maintenance year block from ``<vault>/Maintenance/maintenance.jsonl``.

    Only the fields that file carries (maintenance.append_run's shape):
    session ``kind``, ``steps``, ``problems`` and ``cold_pull``, stamped
    ``at``. Bucketing follows the records' rule: an entry with an
    unparseable ``at`` counts in the unfiltered block and is excluded
    from a year filter. ``tz`` is a tzinfo (the display timezone) or
    None for UTC. Malformed lines are skipped and named.
    """
    from datetime import tzinfo as _tzinfo
    from .print_stats import _resolve_tz
    resolved = tz if isinstance(tz, _tzinfo) else _resolve_tz(tz)
    path = Path(records_vault) / "Maintenance" / "maintenance.jsonl" \
        if records_vault else None
    block = {"state": "absent", "sessions": 0, "by_kind": {}, "problems": 0,
             "cold_pulls": 0, "skipped_lines": []}
    if path is None or not path.is_file():
        return block
    block["state"] = "ok"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return block
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            block["skipped_lines"].append(f"Maintenance/maintenance.jsonl line {index}")
            continue
        if not isinstance(entry, dict):
            block["skipped_lines"].append(f"Maintenance/maintenance.jsonl line {index}")
            continue
        if year is not None:
            parsed = _parse_instant(entry.get("at"))
            if parsed is None or parsed.astimezone(resolved).year != int(year):
                continue
        block["sessions"] += 1
        kind = str(entry.get("kind") or "unknown")
        block["by_kind"][kind] = block["by_kind"].get(kind, 0) + 1
        problems = entry.get("problems")
        if isinstance(problems, list):
            block["problems"] += len([p for p in problems if p])
        steps = entry.get("steps")
        if isinstance(steps, list):
            block["problems"] += len([s for s in steps if s == "problem"])
        if entry.get("cold_pull"):
            block["cold_pulls"] += 1
    return block


__all__ = ["build_enrichments", "load_jobs", "load_receipts",
           "maintenance_block", "spoolman_lifetime"]
