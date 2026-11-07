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
a print counted as risky (set in ``core.model_job`` from
``visual_watch.risky_job``, e.g. ``"slicer finding"``); that is a
**pre-print** slice-risk label decided before the print starts, not a
camera detection during the print (#981). A record counts as
risk-flagged when ``watch_risk`` is a truthy value (a non-empty string
in practice, ``True`` defensively). The aggregate names that count
``risk_flagged``; ``watchdog_flagged`` remains as a deprecated alias
with the same value for existing payload consumers.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def _resolve_tz(tz):
    """A tzinfo for ``tz``: None means UTC, a string is an IANA name,
    a tzinfo passes through. An unknown name raises ValueError."""
    if tz is None:
        return timezone.utc
    if isinstance(tz, tzinfo):
        return tz
    if isinstance(tz, str):
        try:
            return ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone name: {tz!r}") from exc
    raise TypeError("tz must be None, an IANA timezone name, or a tzinfo")


def _tz_name(resolved):
    return getattr(resolved, "key", None) or str(resolved)


def _local(parsed, resolved):
    """``parsed`` (an aware instant) expressed in the bucket timezone."""
    return parsed.astimezone(resolved)


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


def _print_identity(record):
    """The conservative repeat identity of one record, or None.

    Two records are the same print only when BOTH the normalized title
    and the normalized G-code file name match exactly. Normalization
    is deliberately shallow: surrounding whitespace is stripped, runs
    of whitespace collapse, case folds, and the G-code value is reduced
    to its file name (paths differ between the slicer, the archive and
    the printer for the same job). Nothing fuzzier is attempted: a
    same-title print from a different file, and a renamed print from
    the same file, count as different prints, and a record missing
    either field is never grouped with anything — undercounting
    repeats is honest, merging two different prints into one is not.
    """
    title = record.get("title")
    gcode = record.get("gcode_file")
    if not isinstance(title, str) or not isinstance(gcode, str):
        return None
    title_key = " ".join(title.split()).casefold()
    gcode_key = " ".join(gcode.replace("\\", "/").rsplit("/", 1)[-1].split()).casefold()
    if not title_key or not gcode_key:
        return None
    return title_key, gcode_key


def _repeats(rows):
    """The repeats breakdown for ``rows`` (see :func:`_print_identity`)."""
    groups: dict[tuple[str, str], dict] = {}
    ungrouped = 0
    for record in rows:
        identity = _print_identity(record)
        if identity is None:
            ungrouped += 1
            continue
        group = groups.setdefault(identity, {
            "title": str(record.get("title")).strip(),
            "gcode_file": identity[1], "count": 0,
            "total_seconds": 0.0, "total_filament_g": 0.0})
        group["count"] += 1
        seconds = _num(record.get("print_duration_seconds"))
        if seconds is not None:
            group["total_seconds"] += seconds
        grams = _num(record.get("filament_used_g"))
        if grams is not None:
            group["total_filament_g"] += grams
    repeated = [g for g in groups.values() if g["count"] >= 2]
    repeated.sort(key=lambda g: (-g["count"], g["title"].casefold()))
    reprint_count = sum(g["count"] - 1 for g in repeated)
    most = [{"title": g["title"], "gcode_file": g["gcode_file"], "count": g["count"],
             "total_hours": round(g["total_seconds"] / 3600, 2),
             "total_filament_g": round(g["total_filament_g"], 1)}
            for g in repeated[:5]]
    return {
        "distinct_prints": len(groups) + ungrouped,
        "repeated_prints": len(repeated),
        "reprint_count": reprint_count,
        "reprint_share": round(reprint_count / len(rows), 4) if rows else None,
        "most_reprinted": most,
    }


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


def aggregate(records, year=None, tz=None):
    """Aggregate record dicts into a JSON-able stats dict.

    ``year`` (an int) keeps only records whose ``started_at`` falls in
    that year; records with an unparseable ``started_at`` are excluded
    from a year filter and from monthly activity, but always count in
    the unfiltered totals. ``years_available`` always names every year
    present in the input, filtered or not.

    ``tz`` buckets the year filter, the monthly keys,
    ``years_available`` and the late-night count: None (the default)
    preserves the historical UTC bucketing for direct callers, a
    string is an IANA timezone name, and a tzinfo is used as given.
    The Web UI passes the configured display timezone
    (``hermes._config.tz()``) so the recap matches the operator's local
    year (#981). ``started_at`` instants themselves stay UTC in the
    records and in every payload field that echoes them.
    """
    resolved = _resolve_tz(tz)
    records = [r for r in (records or []) if isinstance(r, dict)]
    years = sorted({_local(parsed, resolved).year for r in records
                    if (parsed := _parse_started(r.get("started_at"))) is not None})
    if year is not None:
        year = int(year)
        rows = [r for r in records
                if (parsed := _parse_started(r.get("started_at"))) is not None
                and _local(parsed, resolved).year == year]
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
    by_hour: dict[str, int] = {}
    by_weekday: dict[str, int] = {}
    night_prints = 0
    for record in rows:
        parsed = _parse_started(record.get("started_at"))
        if parsed is None:
            continue
        local = _local(parsed, resolved)
        key = local.strftime("%Y-%m")
        monthly[key] = monthly.get(key, 0) + 1
        hour_key = local.strftime("%H")
        by_hour[hour_key] = by_hour.get(hour_key, 0) + 1
        weekday_key = local.strftime("%A")
        by_weekday[weekday_key] = by_weekday.get(weekday_key, 0) + 1
        if local.hour < 5:  # 00:00-04:59 in the bucket timezone counts as late night
            night_prints += 1
    weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday",
                     "Friday", "Saturday", "Sunday"]
    by_weekday = {day: by_weekday[day] for day in weekday_order if day in by_weekday}

    return {
        "year": year,
        "timezone": _tz_name(resolved),
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
            "risk_flagged": len(flagged),
            # Deprecated alias of risk_flagged (the #956 name); same value.
            "watchdog_flagged": len(flagged),
        },
        "risk_flagged": len(flagged),
        # Deprecated alias of risk_flagged (the #956 name); same value.
        "watchdog_flagged": len(flagged),
        # Pasta, failed and stopped are three separate headline numbers
        # (#981), never merged into one "problems" count. Pasta means
        # watchdog spaghetti events; those events are not recorded yet
        # (the v2 events file does not exist), so pasta is honestly
        # None in a named not-recorded state — the risk-flag count
        # above is a different thing and is never presented as pasta.
        "outcomes": {
            "completed": completed,
            "failed": failed,
            "stopped": canceled,
            "in_progress": in_progress,
            "pasta": None,
            "pasta_state": "not_recorded",
            "pasta_note": ("Pasta (spaghetti) events are not recorded yet: "
                           "the camera watchdog's detections are not persisted, "
                           "so no pasta count exists until event recording begins."),
            "stopped_note": ("Stopped counts records with status canceled/cancelled. "
                             "The records do not say whether a person or the printer "
                             "stopped the print, so no finer split is shown."),
        },
        "repeats": _repeats(rows),
        "monthly": dict(sorted(monthly.items())),
        "by_hour": dict(sorted(by_hour.items())),
        "by_weekday": by_weekday,
        "night_prints": night_prints,
        "top_by_duration": _top(rows, "print_duration_seconds"),
        "top_by_filament": _top(rows, "filament_used_g"),
        # No "timelapse_known" (#981): it keyed on a `timelapse` record
        # field no record ever carries, so it was structurally always 0.
        # Clip counts are a finder result, not a record fact; the Web UI
        # layer reports them separately as timelapse_clips_found.
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


def _archetype(stats, highlights, rows):
    """The year's one printing personality (#981 design spec).

    Exactly one archetype is always returned — a failed-heavy year
    earns a fun one, never a shaming one, and the neutral fallback
    guarantees every year gets one. Rules are checked in this
    documented precedence order (most specific signal first), and
    every rule is data-gated on the number named in its fact:

    1. Spaghetti Magnet — the most pasta (spaghetti) events, and at
       least 3. GATED OFF in this step: pasta events are not recorded
       yet (``outcomes.pasta`` is None), so this rule can never fire
       here, and it never reads risk flags as pasta.
    2. The Perfectionist — success rate at least 95% on at least 20
       finished prints.
    3. Reprint Machine — reprints are at least 25% of finished prints.
    4. Marathoner — the longest print is at least 3 times the median
       print duration and at least 2 hours.
    5. Night Owl — at least 30% of timed starts fall in the local
       night hours (00:00-04:59 in the bucket timezone).
    6. One-Color Loyalist — the top color covers over 50% of prints.
    7. The Regular — the fallback, when no signal above is strong.
    """
    outcomes = stats["outcomes"]
    pasta = outcomes.get("pasta")
    if isinstance(pasta, (int, float)) and pasta >= 3:
        return {"name": "Spaghetti Magnet",
                "fact": f"{int(pasta)} pasta events were recorded this year."}
    finished = stats["finished"]
    if (finished >= 20 and stats["success_rate"] is not None
            and stats["success_rate"] >= 0.95):
        return {"name": "The Perfectionist",
                "fact": f"{round(stats['success_rate'] * 100):g}% success across "
                        f"{finished} finished prints."}
    reprints = stats["repeats"]["reprint_count"]
    if finished and reprints and reprints / finished >= 0.25:
        return {"name": "Reprint Machine",
                "fact": f"{reprints} of {finished} finished prints were reprints."}
    durations = sorted(v for r in rows
                       if (v := _num(r.get("print_duration_seconds"))) is not None)
    longest = stats["longest_print"]
    if durations and longest and longest.get("seconds"):
        median = durations[len(durations) // 2] if len(durations) % 2 else \
            (durations[len(durations) // 2 - 1] + durations[len(durations) // 2]) / 2
        if median and longest["seconds"] >= 3 * median and longest["seconds"] >= 7200:
            return {"name": "Marathoner",
                    "fact": f"Your longest print ran {longest['seconds'] / 3600:.1f} hours, "
                            f"at least three times your median print."}
    timed = sum(stats["by_hour"].values())
    if timed and stats["night_prints"] / timed >= 0.30:
        return {"name": "Night Owl",
                "fact": f"{stats['night_prints']} of {timed} prints started between "
                        "midnight and 5 a.m. local time."}
    total = stats["total_prints"]
    if highlights["top_color"] and total and highlights["top_color_prints"] / total > 0.5:
        return {"name": "One-Color Loyalist",
                "fact": f"{highlights['top_color']} covered "
                        f"{highlights['top_color_prints']} of {total} prints."}
    return {"name": "The Regular",
            "fact": f"A steady year: {total} prints, no single habit took over."}


def build_wrapped(records, year, tz=None):
    """The Wrapped payload for one year: headline stats, highlights, script.

    Every script line is data-driven: a line is included only when the
    number behind it is true in this same payload. ``tz`` has the same
    meaning as in :func:`aggregate` (None preserves UTC bucketing).
    """
    resolved = _resolve_tz(tz)
    records = [r for r in (records or []) if isinstance(r, dict)]
    year = int(year)
    stats = aggregate(records, year=year, tz=resolved)
    rows = [r for r in records
            if (parsed := _parse_started(r.get("started_at"))) is not None
            and _local(parsed, resolved).year == year]
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
    most_reprinted = stats["repeats"]["most_reprinted"][0] if stats["repeats"]["most_reprinted"] else None
    peak_hour = None
    if stats["by_hour"]:
        peak_hour = int(max(stats["by_hour"].items(), key=lambda kv: kv[1])[0])
    peak_weekday = None
    if stats["by_weekday"]:
        peak_weekday = max(stats["by_weekday"].items(), key=lambda kv: kv[1])[0]
    highlights = {
        "top_color": top_color, "top_color_prints": top_color_count,
        "top_material": top_material, "top_printer": top_printer,
        "top_vendor": top_vendor, "top_spool": top_spool,
        "busiest_month": busiest_month, "busiest_month_prints": busiest_count,
        "longest_print": stats["longest_print"], "biggest_by_filament": biggest,
        "risk_flagged": stats["risk_flagged"],
        # Deprecated alias of risk_flagged (the #956 name); same value.
        "watchdog_flagged": stats["watchdog_flagged"], "failed": stats["failed"],
        "worst_failure_streak": _failure_streak(rows),
        "night_prints": stats["night_prints"],
        "most_reprinted": most_reprinted,
        "reprint_count": stats["repeats"]["reprint_count"],
        "peak_hour": peak_hour, "peak_weekday": peak_weekday,
    }
    highlights["archetype"] = _archetype(stats, highlights, rows)
    script = _script(year, stats, highlights, tz=resolved)
    return {"year": year, "stats": stats, "headline": {
                "total_prints": stats["total_prints"], "completed": stats["completed"],
                "failed": stats["failed"], "canceled": stats["canceled"],
                # Pasta/failed/stopped stay separate (#981): stopped is
                # the canceled count under its plain name, pasta is the
                # honest not-recorded state (None) until watchdog event
                # recording exists.
                "stopped": stats["outcomes"]["stopped"],
                "pasta": None, "pasta_state": stats["outcomes"]["pasta_state"],
                "success_rate": stats["success_rate"], "total_hours": stats["total_hours"],
                "total_filament_used_g": stats["total_filament_used_g"],
                "total_filament_used_kg": stats["total_filament_used_kg"]},
            "highlights": highlights, "script": script,
            "archetype": highlights["archetype"],
            "years_available": stats["years_available"]}


def _script(year, stats, highlights, tz=None):
    resolved = _resolve_tz(tz)
    local_tz = resolved != timezone.utc
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
    most_reprinted = highlights.get("most_reprinted")
    if most_reprinted and most_reprinted["count"] >= 3:
        lines.append(f"You printed {most_reprinted['title']} {most_reprinted['count']} times. "
                     "If it works, print it again. And again.")
    if highlights.get("peak_hour") is not None and highlights.get("peak_weekday"):
        lines.append(f"Your busiest hour was {highlights['peak_hour']:02d}:00 and your "
                     f"busiest weekday was {highlights['peak_weekday']}. The printer "
                     "keeps your hours now.")
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
    if stats["risk_flagged"] > 0:
        lines.append(f"{stats['risk_flagged']} print"
                     f"{'s' if stats['risk_flagged'] != 1 else ''} started with a risk flag. "
                     "The slicer findings and support checks raised a hand before "
                     "those prints ever started.")
    if highlights["night_prints"] > 0:
        when = "between midnight and 5 a.m. your time" if local_tz else \
            "between midnight and 5 a.m. UTC"
        lines.append(f"{highlights['night_prints']} print"
                     f"{'s' if highlights['night_prints'] != 1 else ''} started "
                     f"{when}. Sleep was also an option.")
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
    archetype = highlights.get("archetype")
    if archetype:
        lines.append(f"Your printing personality: {archetype['name']}. {archetype['fact']}")
    lines.append(f"That was {year}. Same printer, new year: print something weird.")
    return lines


__all__ = ["UNKNOWN", "aggregate", "build_wrapped", "load_records",
           "load_records_from_dir", "prints_dir"]
