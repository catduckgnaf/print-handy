"""Print-record statistics for the Data tab and Print Handy Wrapped (#956).

Pure aggregation over the durable print JSON records
(``<records vault>/Prints/print-*.json``, #392). The loader is the only
I/O: it reads a directory, skips malformed or half-written files and
names them in ``skipped_records``. Everything else takes plain record
dicts, so tests inject fixtures with no filesystem at all.

Missing data stays missing. A record without a color, nozzle or
filament weight counts toward the totals it can prove and lands in the
explicit ``"unknown"`` bucket for the breakdowns it cannot. Nothing is
estimated or invented. ``watch_risk`` is a non-empty string naming why
a print counted as risky (set in ``core.model_job`` from the camera
watch's ``risky_job`` result, e.g. ``"slicer finding"``); a record is
counted as watchdog-flagged when ``watch_risk`` is a truthy value (a
non-empty string in practice, ``True`` defensively).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

UNKNOWN = "unknown"


def prints_dir(records_vault):
    return Path(records_vault) / "Prints"


def load_records(records_vault):
    """(records, skipped_records) from ``<records_vault>/Prints/print-*.json``.

    A missing directory is empty stats, not an error. A file that is
    not valid JSON, or whose JSON is not an object, is skipped and its
    file name is returned in ``skipped_records``.
    """
    return load_records_from_dir(prints_dir(records_vault))


def load_records_from_dir(directory):
    directory = Path(directory)
    if not directory.is_dir():
        return [], []
    records, skipped = [], []
    for path in sorted(directory.glob("print-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            skipped.append(path.name)
            continue
        if not isinstance(payload, dict):
            skipped.append(path.name)
            continue
        records.append(payload)
    return records, skipped


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value:  # NaN
        return None
    return float(value)


def _label(value):
    if value is None:
        return UNKNOWN
    text = str(value).strip()
    return text or UNKNOWN


def _spool_label(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return UNKNOWN
    return str(value)


def _nozzle_label(record):
    settings = record.get("slicer_settings")
    if isinstance(settings, str):
        try:
            parsed = json.loads(settings)
        except ValueError:
            parsed = None
        settings = parsed if isinstance(parsed, dict) else None
    if not isinstance(settings, dict):
        return UNKNOWN
    value = settings.get("nozzle_diameter")
    if value is None:
        value = settings.get("nozzle_diameter_mm")
    number = _num(value)
    if number is not None:
        return f"{number:g}"
    if isinstance(value, str) and value.strip():
        return value.strip()
    return UNKNOWN


def _parse_started(value):
    """A datetime for an ISO ``started_at`` string, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _status(record):
    return str(record.get("status") or UNKNOWN).strip().lower() or UNKNOWN


def _is_finished(record):
    return _status(record) in ("completed", "failed", "canceled", "cancelled")


def _empty_breakdown():
    return {"count": 0, "filament_used_g": 0.0, "print_duration_seconds": 0.0}


def _add_breakdown(table, label, record):
    row = table.setdefault(label, _empty_breakdown())
    row["count"] += 1
    grams = _num(record.get("filament_used_g"))
    if grams is not None:
        row["filament_used_g"] += grams
    seconds = _num(record.get("print_duration_seconds"))
    if seconds is not None:
        row["print_duration_seconds"] += seconds


def _top(rows, key, limit=5):
    scored = []
    for record in rows:
        value = _num(record.get(key))
        if value is None:
            continue
        scored.append((value, record))
    scored.sort(key=lambda item: item[0], reverse=True)
    out = []
    for value, record in scored[:limit]:
        entry = {"title": record.get("title"), "print_number": record.get("print_number"),
                 key: value}
        if key == "print_duration_seconds":
            entry["seconds"] = value
        out.append(entry)
    return out


def aggregate(records, year=None):
    """Aggregate record dicts into a JSON-able stats dict.

    ``year`` (an int) keeps only records whose ``started_at`` falls in
    that year; records with an unparseable ``started_at`` are excluded
    from a year filter and from monthly activity, but always count in
    the unfiltered totals. ``years_available`` always names every year
    present in the input, filtered or not.
    """
    records = [r for r in (records or []) if isinstance(r, dict)]
    years = sorted({parsed.year for r in records
                    if (parsed := _parse_started(r.get("started_at"))) is not None})
    if year is not None:
        year = int(year)
        rows = [r for r in records
                if (parsed := _parse_started(r.get("started_at"))) is not None
                and parsed.year == year]
    else:
        rows = list(records)

    counts_by_status: dict[str, int] = {}
    for record in rows:
        status = _status(record)
        counts_by_status[status] = counts_by_status.get(status, 0) + 1
    completed = counts_by_status.get("completed", 0)
    failed = counts_by_status.get("failed", 0)
    canceled = counts_by_status.get("canceled", 0) + counts_by_status.get("cancelled", 0)
    in_progress = counts_by_status.get("printing", 0)
    finished = completed + failed + canceled
    success_rate = round(completed / finished, 4) if finished else None

    durations = [v for r in rows if (v := _num(r.get("print_duration_seconds"))) is not None]
    total_seconds = float(sum(durations))
    longest = None
    if durations:
        best = max(rows, key=lambda r: _num(r.get("print_duration_seconds"))
                   if _num(r.get("print_duration_seconds")) is not None else -1)
        longest = {"title": best.get("title"), "print_number": best.get("print_number"),
                   "seconds": _num(best.get("print_duration_seconds"))}
    grams_values = [v for r in rows if (v := _num(r.get("filament_used_g"))) is not None]
    total_grams = float(sum(grams_values))
    estimated_values = [v for r in rows if (v := _num(r.get("estimated_filament_g"))) is not None]

    breakdowns = {}
    dimensions = {
        "by_printer": lambda r: _label(r.get("printer")),
        "by_printer_profile": lambda r: _label(r.get("printer_profile")),
        "by_material": lambda r: _label(r.get("filament_material")),
        "by_color": lambda r: _label(r.get("filament_color")),
        "by_vendor": lambda r: _label(r.get("filament_vendor")),
        "by_spool": lambda r: _spool_label(r.get("spoolman_spool_id")),
        "by_nozzle": _nozzle_label,
    }
    for name, pick in dimensions.items():
        table: dict[str, dict] = {}
        for record in rows:
            _add_breakdown(table, pick(record), record)
        breakdowns[name] = dict(sorted(table.items(), key=lambda kv: (-kv[1]["count"], kv[0])))

    stop_reasons: dict[str, int] = {}
    for record in rows:
        reason = record.get("stop_reason")
        if isinstance(reason, str) and reason.strip():
            stop_reasons[reason.strip()] = stop_reasons.get(reason.strip(), 0) + 1
    flagged = [r for r in rows if r.get("watch_risk")]
    monthly: dict[str, int] = {}
    night_prints = 0
    for record in rows:
        parsed = _parse_started(record.get("started_at"))
        if parsed is None:
            continue
        key = parsed.strftime("%Y-%m")
        monthly[key] = monthly.get(key, 0) + 1
        if parsed.hour < 5:  # started_at is UTC ISO; 00:00-04:59 counts as late night
            night_prints += 1

    return {
        "year": year,
        "total_prints": len(rows),
        "counts_by_status": counts_by_status,
        "completed": completed,
        "failed": failed,
        "canceled": canceled,
        "in_progress": in_progress,
        "finished": finished,
        "success_rate": success_rate,
        "failure_rate": round(failed / finished, 4) if finished else None,
        "total_print_duration_seconds": total_seconds,
        "total_hours": round(total_seconds / 3600, 2),
        "average_print_seconds": round(total_seconds / len(durations), 1) if durations else None,
        "longest_print": longest,
        "total_filament_used_g": total_grams,
        "total_filament_used_kg": round(total_grams / 1000, 3),
        "total_estimated_filament_g": float(sum(estimated_values)),
        "estimated_filament_records": len(estimated_values),
        "breakdowns": breakdowns,
        "failures": {
            "failed": failed,
            "stop_reasons": dict(sorted(stop_reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
            "watchdog_flagged": len(flagged),
        },
        "watchdog_flagged": len(flagged),
        "monthly": dict(sorted(monthly.items())),
        "night_prints": night_prints,
        "top_by_duration": _top(rows, "print_duration_seconds"),
        "top_by_filament": _top(rows, "filament_used_g"),
        "timelapse_known": sum(1 for r in rows if r.get("timelapse")),
        "years_available": years,
    }


def _top_label(table):
    """(label, count) of the biggest non-unknown bucket, else (None, 0)."""
    best = (None, 0)
    for label, row in (table or {}).items():
        if label == UNKNOWN:
            continue
        if row["count"] > best[1]:
            best = (label, row["count"])
    return best


def _failure_streak(records):
    """The longest run of failed/canceled prints, in started_at order."""
    ordered = sorted((r for r in records if _parse_started(r.get("started_at")) is not None),
                     key=lambda r: str(r.get("started_at")))
    best = current = 0
    for record in ordered:
        if _status(record) in ("failed", "canceled", "cancelled"):
            current += 1
            best = max(best, current)
        elif _status(record) == "completed":
            current = 0
    return best


def build_wrapped(records, year):
    """The Wrapped payload for one year: headline stats, highlights, script.

    Every script line is data-driven: a line is included only when the
    number behind it is true in this same payload.
    """
    records = [r for r in (records or []) if isinstance(r, dict)]
    year = int(year)
    stats = aggregate(records, year=year)
    rows = [r for r in records
            if (parsed := _parse_started(r.get("started_at"))) is not None and parsed.year == year]
    breakdowns = stats["breakdowns"]
    top_color, top_color_count = _top_label(breakdowns["by_color"])
    top_material, _ = _top_label(breakdowns["by_material"])
    top_printer, _ = _top_label(breakdowns["by_printer"])
    top_vendor, _ = _top_label(breakdowns["by_vendor"])
    top_spool, top_spool_count = _top_label(breakdowns["by_spool"])
    busiest_month = None
    if stats["monthly"]:
        busiest_month, busiest_count = max(stats["monthly"].items(), key=lambda kv: kv[1])
    else:
        busiest_count = 0
    biggest = stats["top_by_filament"][0] if stats["top_by_filament"] else None
    highlights = {
        "top_color": top_color, "top_color_prints": top_color_count,
        "top_material": top_material, "top_printer": top_printer,
        "top_vendor": top_vendor, "top_spool": top_spool,
        "busiest_month": busiest_month, "busiest_month_prints": busiest_count,
        "longest_print": stats["longest_print"], "biggest_by_filament": biggest,
        "watchdog_flagged": stats["watchdog_flagged"], "failed": stats["failed"],
        "worst_failure_streak": _failure_streak(rows),
        "night_prints": stats["night_prints"],
    }
    script = _script(year, stats, highlights)
    return {"year": year, "stats": stats, "headline": {
                "total_prints": stats["total_prints"], "completed": stats["completed"],
                "failed": stats["failed"], "canceled": stats["canceled"],
                "success_rate": stats["success_rate"], "total_hours": stats["total_hours"],
                "total_filament_used_g": stats["total_filament_used_g"],
                "total_filament_used_kg": stats["total_filament_used_kg"]},
            "highlights": highlights, "script": script,
            "years_available": stats["years_available"]}


def _script(year, stats, highlights):
    lines = []
    total = stats["total_prints"]
    if not total:
        return [f"Print Handy Wrapped {year}: no prints were recorded this year. "
                "The printer rested. Respect."]
    lines.append(f"Print Handy Wrapped {year}: you made {total} print"
                 f"{'s' if total != 1 else ''}. The printer did the work, "
                 "but you get the recap.")
    hours = stats["total_hours"]
    if hours >= 1:
        lines.append(f"You spent {hours:g} hours printing this year. "
                     "That is a part-time job with no paycheck, only parts.")
    grams = stats["total_filament_used_g"]
    if grams > 0:
        lines.append(f"You turned {grams:g} g of filament into objects "
                     f"({stats['total_filament_used_kg']:g} kg). Some of it even stayed on the bed.")
    top_color = highlights["top_color"]
    if top_color and total and highlights["top_color_prints"] / total > 0.5:
        lines.append(f"Your top color was {top_color}, in "
                     f"{highlights['top_color_prints']} of {total} prints. "
                     "Variety was available. You declined.")
    elif top_color:
        lines.append(f"Your top color was {top_color}. A bold range, by Wrapped standards.")
    if highlights["top_material"]:
        lines.append(f"Your top material was {highlights['top_material']}. It knows the way to the bed by now.")
    if highlights["top_printer"]:
        lines.append(f"Your hardest-working printer was {highlights['top_printer']}. It has earned a nozzle check.")
    failed = stats["failed"]
    if failed > 0:
        lines.append(f"{failed} print{'s' if failed != 1 else ''} failed this year. "
                     "The spaghetti was not always dinner.")
    elif stats["finished"]:
        lines.append("Zero failed prints this year. Wrapped does not believe it either, "
                     "but the records say so.")
    if highlights["worst_failure_streak"] >= 3:
        lines.append(f"Your worst run was {highlights['worst_failure_streak']} failed or "
                     "canceled prints in a row. Character building, technically.")
    if stats["watchdog_flagged"] > 0:
        lines.append(f"The camera watchdog flagged {stats['watchdog_flagged']} print"
                     f"{'s' if stats['watchdog_flagged'] != 1 else ''}. "
                     "It saw things. It always sees things.")
    if highlights["night_prints"] > 0:
        lines.append(f"{highlights['night_prints']} print"
                     f"{'s' if highlights['night_prints'] != 1 else ''} started between "
                     "midnight and 5 a.m. UTC. Sleep was also an option.")
    longest = stats["longest_print"]
    if longest and longest.get("seconds"):
        title = longest.get("title") or "your longest print"
        lines.append(f"Your longest print, {title}, ran "
                     f"{longest['seconds'] / 3600:.1f} hours. Commitment, or a very long nap.")
    if highlights["busiest_month"]:
        lines.append(f"Your busiest month was {highlights['busiest_month']} with "
                     f"{highlights['busiest_month_prints']} prints. The printer remembers.")
    if highlights["top_spool"] and highlights.get("top_spool"):
        spool_count = 0
        row = stats["breakdowns"]["by_spool"].get(highlights["top_spool"]) or {}
        spool_count = row.get("count", 0)
        if spool_count and total and spool_count / total > 0.5:
            lines.append(f"Spool {highlights['top_spool']} carried {spool_count} of {total} "
                         "prints. One spool did the heavy lifting.")
    lines.append(f"That was {year}. Same printer, new year: print something weird.")
    return lines


__all__ = ["UNKNOWN", "aggregate", "build_wrapped", "load_records",
           "load_records_from_dir", "prints_dir"]
