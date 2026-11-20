"""Print-record statistics for the Data tab and Pasta Wrapped (#956).

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

Pasta (spaghetti) is a different source entirely: the watchdog events
file (``<records vault>/Watchdog/events.jsonl``, written always-on by
``runtime.watchdog_events``). Callers load it with
:func:`load_watchdog_events` and pass the events to :func:`aggregate`;
when no events file exists the aggregate keeps the honest
``not_recorded`` pasta state, and risk flags are never read as pasta.

Two #1719 reconciliations live in :func:`aggregate`. Outcomes are
resolved by :func:`_resolved_outcomes`: a record left at status
"printing" whose print provably ended (a terminal native state, a
recorded end time, or a later print on the same printer) counts by its
recorded outcome — or as unrecorded — never as still printing. And the
``by_source`` breakdown names where each print came from from the
record's own ``source_platform`` / ``source_url`` (or the intake join's
URL); a print with neither recorded lands in the explicit unknown
bucket, because prints started outside intake carry no source.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def record_key(record):
    """One print's identity for per-print joins: its record_id.

    print_number is NOT unique in history: before per-launch numbering
    (#979/#988) a reprint was recorded under the original's number, so
    joins keyed by number merged two different prints. record_id names
    one physical print (job + file + start instant). Records without one
    fall back to number plus start time.
    """
    rid = record.get("record_id") if isinstance(record, dict) else None
    if isinstance(rid, str) and rid:
        return rid
    return f"#{record.get('print_number')}@{record.get('started_at') or record.get('completed_at') or ''}"

UNKNOWN = "unknown"


_TITLE_MAX = 80
# A drive or root prefix, two or more separators, or a separator before a
# model/G-code file name: "A/B bracket" is a title, "/data/x/part.gcode" is a path.
_PATH_LIKE_RE = re.compile(
    r"^(?:[A-Za-z]:[\\/]|[\\/]|\.{1,2}[\\/])|[\\/].*[\\/]|[\\/][^\\/]*\.(?:gcode|3mf|stl|bgcode)$",
    re.IGNORECASE)


def display_title(title, job_key=None):
    """A title safe to show or share, or None when there is nothing to show.

    A record's title falls back to its job key, and a job key can be a
    file path. A value with a path separator is reduced to the file's base
    name without its extension, control characters are dropped and runs of
    whitespace collapse, so a path never reaches a page, a commentary line
    or a share bundle.
    """
    for candidate in (title, job_key):
        if not isinstance(candidate, str):
            continue
        text = "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in candidate)
        text = " ".join(text.split())
        if _PATH_LIKE_RE.search(text):
            text = text.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            text = text.rsplit(".", 1)[0] if "." in text.strip(".") else text
            text = " ".join(text.split())
        if text:
            return text[:_TITLE_MAX]
    return None


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


def watchdog_events_path(records_vault):
    return Path(records_vault) / "Watchdog" / "events.jsonl"


def load_watchdog_events(records_vault):
    """(events, skipped_lines, exists) from the watchdog events file.

    The same fail-soft pattern as the record loader: a missing file is
    ``([], [], False)`` — the caller keeps the ``not_recorded`` pasta
    state — and a malformed line is skipped and named
    (``Watchdog/events.jsonl line N``), never fatal.
    """
    path = watchdog_events_path(records_vault)
    if not path.is_file():
        return [], [], False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], [], True
    events, skipped = [], []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            skipped.append(f"Watchdog/events.jsonl line {index}")
            continue
        if not isinstance(payload, dict):
            skipped.append(f"Watchdog/events.jsonl line {index}")
            continue
        events.append(payload)
    return events, skipped, True


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


# The breakdown label for prints whose starter is not known (#1647):
# pre-feature records, records from start paths that never knew a
# starter, and records whose starter could not be captured. They count
# in every total and are named here, never silently dropped and never
# attributed to someone after the fact.
UNATTRIBUTED = "unattributed"


def record_starter(record):
    """The starter a print record carries, normalized (#1647).

    A record's ``started_by`` is the stamp the start path recorded at
    intake: ``{"platform", "user_id", "display_name"}``, with the
    display name present only when the platform supplied one. Returns
    ``{"platform", "user_id"}`` plus ``"display_name"`` when known, or
    None when the record names no starter — an absent, empty or
    malformed stamp is unattributed, never guessed.
    """
    if not isinstance(record, dict):
        return None
    raw = record.get("started_by")
    if not isinstance(raw, dict):
        return None
    user_id = raw.get("user_id")
    if user_id is None or str(user_id).strip() == "":
        return None
    starter = {"platform": str(raw.get("platform") or UNKNOWN),
               "user_id": str(user_id)}
    name = raw.get("display_name")
    if isinstance(name, str) and name.strip():
        starter["display_name"] = name.strip()
    return starter


def starter_key(record):
    """``"<platform>:<user_id>"`` for a record's starter, else None.

    The key is the identity a user filter matches on: the same person
    on two platforms is two starters, because the records cannot
    prove they are one person.
    """
    starter = record_starter(record)
    if starter is None:
        return None
    return f"{starter['platform']}:{starter['user_id']}"


def starter_label(record):
    """The human label for a record's starter: the display name
    recorded at start time, else the user id, else ``unattributed``."""
    starter = record_starter(record)
    if starter is None:
        return UNATTRIBUTED
    return starter.get("display_name") or starter["user_id"]


def _starter_labels(rows):
    """Starter key -> breakdown label for these rows (#1647).

    The label is the display name recorded at start time, else the
    user id. When two different starters share one display name, the
    platform is appended so the breakdown never merges two people
    into one row.
    """
    base = {}
    for record in rows:
        starter = record_starter(record)
        if starter is None:
            continue
        key = f"{starter['platform']}:{starter['user_id']}"
        base.setdefault(key, (starter.get("display_name") or starter["user_id"],
                              starter["platform"]))
    counts = {}
    for label, _platform in base.values():
        counts[label] = counts.get(label, 0) + 1
    return {key: (label if counts[label] == 1 else f"{label} ({platform})")
            for key, (label, platform) in base.items()}


def starter_options(records):
    """The distinct starters in ``records`` as filter options:
    ``[{"key", "label", "platform", "user_id", "prints"}]`` sorted by
    print count, then label. Unattributed prints are not an option
    here; :func:`filter_records` takes the ``UNATTRIBUTED`` starter
    value for them."""
    counts = {}
    labels = {}
    for record in records or []:
        starter = record_starter(record)
        if starter is None:
            continue
        key = f"{starter['platform']}:{starter['user_id']}"
        counts[key] = counts.get(key, 0) + 1
        labels.setdefault(key, starter)
    options = [{"key": key,
                "label": labels[key].get("display_name") or labels[key]["user_id"],
                "platform": labels[key]["platform"],
                "user_id": labels[key]["user_id"],
                "prints": counts[key]} for key in counts]
    options.sort(key=lambda row: (-row["prints"], row["label"].lower()))
    return options


def printer_options(records):
    """The distinct printers in ``records`` as filter options:
    ``[{"name", "prints"}]`` sorted by print count, then name. The
    name is the printer the records themselves carry, so an option
    always matches the records it counts."""
    counts = {}
    for record in records or []:
        name = _record_printer(record)
        if name:
            counts[name] = counts.get(name, 0) + 1
    options = [{"name": name, "prints": counts[name]} for name in counts]
    options.sort(key=lambda row: (-row["prints"], row["name"].lower()))
    return options


def _record_printer(record):
    """The printer a record names: its ``printer`` display name, or
    its ``printer_id`` when that is all the record carries."""
    if not isinstance(record, dict):
        return ""
    return str(record.get("printer") or record.get("printer_id") or "").strip()


def _printer_matches(record, wanted):
    """Whether ``record`` ran on the printer ``wanted`` names: the
    record's printer name or printer id, compared case-insensitively
    so a filter typed in chat matches the recorded name."""
    wanted = str(wanted or "").strip().lower()
    if not wanted:
        return False
    return wanted in {str(record.get("printer") or "").strip().lower(),
                      str(record.get("printer_id") or "").strip().lower()}


def filter_records(records, *, starter=None, printer=None):
    """The records matching a user / printer selection (#1647).

    ``starter`` is a starter key (``"discord:123"``), a starter dict
    as :func:`record_starter` returns, or :data:`UNATTRIBUTED` for
    the prints with no recorded starter. ``printer`` is a printer
    name or id. Both together select one person's prints on one
    printer. This is the one filtering implementation: the chat
    answers and the Web UI Data / Wrapped views all call it, so the
    numbers cannot drift between surfaces.
    """
    rows = list(records or [])
    if starter is not None:
        if starter == UNATTRIBUTED:
            rows = [row for row in rows if starter_key(row) is None]
        else:
            if isinstance(starter, dict):
                key = (f"{starter.get('platform') or UNKNOWN}:{starter.get('user_id')}"
                       if starter.get("user_id") not in (None, "") else None)
            else:
                key = str(starter)
            rows = [row for row in rows if starter_key(row) == key]
    if printer is not None:
        rows = [row for row in rows if _printer_matches(row, printer)]
    return rows


def filter_events_for_records(events, records):
    """The watchdog events that belong to ``records`` (#1647).

    Pasta events are keyed by print, not by starter or printer, so a
    filtered view keeps only the events whose print is one of the
    filtered records — matched on print number or job key, the same
    identity the pasta summary joins events to records with.
    """
    numbers = set()
    keys = set()
    for record in records or []:
        number = record.get("print_number")
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            numbers.add(number)
        job_key = record.get("job_key")
        if isinstance(job_key, str) and job_key.strip():
            keys.add(job_key.strip())
    kept = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        number = event.get("print_number")
        if isinstance(number, int) and not isinstance(number, bool) and number in numbers:
            kept.append(event)
            continue
        job_key = event.get("job_key")
        if isinstance(job_key, str) and job_key.strip() in keys:
            kept.append(event)
    return kept


def selection_descriptor(user_label=None, printer=None):
    """The label a filtered view carries (#1647): ``{"user",
    "printer", "label"}`` naming whose prints and which printer a
    selection covers, or None when nothing is filtered. The label is
    what the Wrapped recap, its share card and the Web UI pages print
    so a filtered number is never mistaken for the whole history."""
    user_label = str(user_label or "").strip() or None
    printer = str(printer or "").strip() or None
    if user_label is None and printer is None:
        return None
    if user_label and printer:
        label = f"{user_label} on {printer}"
    elif user_label:
        label = user_label
    else:
        label = printer
    return {"user": user_label, "printer": printer, "label": label}


def _settings_dict(record):
    """The record's slicer_settings as a dict, or None (it may be a
    JSON string, a dict, or absent, depending on the recording path)."""
    settings = record.get("slicer_settings")
    if isinstance(settings, str):
        try:
            parsed = json.loads(settings)
        except ValueError:
            parsed = None
        settings = parsed if isinstance(parsed, dict) else None
    return settings if isinstance(settings, dict) else None


def _nozzle_label(record):
    settings = _settings_dict(record)
    if settings is None:
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


def records_in_year(records, year, tz=None):
    """The records whose ``started_at`` falls in ``year`` in the bucket timezone."""
    resolved = _resolve_tz(tz)
    year = int(year)
    return [r for r in records or []
            if isinstance(r, dict)
            and (parsed := _parse_started(r.get("started_at"))) is not None
            and _local(parsed, resolved).year == year]


def years_available(records, tz=None):
    """Every year with a dated record, ascending. One cheap pass: no aggregate."""
    resolved = _resolve_tz(tz)
    return sorted({_local(parsed, resolved).year for r in records or []
                   if isinstance(r, dict)
                   and (parsed := _parse_started(r.get("started_at"))) is not None})


def plural(count, word):
    """``1 print`` / ``2 prints``: the count with the word agreeing."""
    return f"{count} {word}" + ("" if count == 1 else "s")


def hours_text(hours):
    """Hours as the copy says them: ``1 hour``, ``9.1 hours``."""
    value = float(hours)
    if value == int(value):
        return plural(int(value), "hour")
    shown = f"{value:.1f}"
    return "1 hour" if shown == "1.0" else f"{shown} hours"


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
            "title": display_title(record.get("title")) or "untitled",
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


# Density defaults (g/cm3) for the derived-grams fallback, by material
# family. Used only when a record carries neither its own density nor
# a recorded spool density — see _derived_grams.
_DENSITY_DEFAULTS = {"PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.07,
                     "TPU": 1.21, "NYLON": 1.14, "PA": 1.14, "PC": 1.20}
_DERIVED_DIAMETER_MM = 1.75
_DERIVED_DENSITY_DEFAULT = 1.24


def _derived_grams(record):
    """(grams, used_fallback) recovered from ``filament_used_mm``, or None.

    Only for records with no recorded ``filament_used_g``. The record's
    own filament diameter/density win when it carries them (top level
    or inside ``slicer_settings``); otherwise the documented fallback
    applies: 1.75 mm diameter and the material family's density
    (1.24 g/cm3 when the material is unknown). The result is always
    labeled DERIVED by the caller and never enters the headline total:
    a recovered estimate must not silently change what the recorded
    total means.
    """
    if _num(record.get("filament_used_g")) is not None:
        return None
    mm = _num(record.get("filament_used_mm"))
    if mm is None or mm <= 0:
        return None
    settings = _settings_dict(record) or {}
    diameter = _num(record.get("filament_diameter_mm"))
    if diameter is None:
        diameter = _num(settings.get("filament_diameter_mm"))
    density = _num(record.get("filament_density"))
    if density is None:
        density = _num(settings.get("filament_density"))
    fallback = False
    if diameter is None:
        diameter = _DERIVED_DIAMETER_MM
        fallback = True
    if density is None:
        material = str(record.get("filament_material") or "").strip().upper()
        density = next((v for k, v in _DENSITY_DEFAULTS.items()
                        if material.startswith(k)), _DERIVED_DENSITY_DEFAULT)
        fallback = True
    import math
    grams = mm * math.pi * (diameter / 2) ** 2 * density / 1000
    return grams, fallback


def _event_key(event, index):
    """The print identity of one watchdog event: its print number,
    else its job key, else the event itself (never merged)."""
    number = event.get("print_number")
    if isinstance(number, int) and not isinstance(number, bool) and number > 0:
        return ("number", number)
    job_key = event.get("job_key")
    if isinstance(job_key, str) and job_key.strip():
        return ("job", job_key.strip())
    return ("event", index)


def _pasta_summary(events, year, resolved, rows):
    """Pasta facts from the watchdog events list (see aggregate()).

    Pasta counts **distinct prints** with at least one ``spaghetti``
    verdict event in the period; the raw event count rides along as
    ``pasta_events``. Events with unparseable timestamps count in the
    unfiltered totals and are excluded from a year filter, exactly
    like records. ``worst_spaghetti`` is the print with the highest
    recorded streak (an event with no streak counts as a streak of 1,
    the one check that saw it); ties break on most spaghetti events,
    then earliest first event.
    """
    parsed = [(index, event, _parse_started(event.get("timestamp")))
              for index, event in enumerate(events)]
    recording_started = None
    for _index, event, when in parsed:
        if when is not None and (recording_started is None or when < recording_started[0]):
            recording_started = (when, event.get("timestamp"))

    def in_period(when):
        if year is None:
            return True
        return when is not None and _local(when, resolved).year == year

    spaghetti = [(index, event, when) for index, event, when in parsed
                 if str(event.get("verdict") or "").strip().lower() == "spaghetti"]
    period = [(index, event, when) for index, event, when in spaghetti
              if in_period(when)]
    all_keys = {_event_key(event, index) for index, event, _when in spaghetti}
    period_keys = {_event_key(event, index) for index, event, _when in period}
    groups: dict = {}
    for index, event, when in period:
        key = _event_key(event, index)
        group = groups.setdefault(key, {"events": 0, "streak": 0, "first": None,
                                        "event": event})
        group["events"] += 1
        streak = _num(event.get("streak"))
        group["streak"] = max(group["streak"], int(streak) if streak else 1)
        stamp = str(event.get("timestamp") or "")
        if group["first"] is None or (stamp and stamp < group["first"]):
            group["first"] = stamp or group["first"]
    worst = None
    if groups:
        key, group = sorted(
            groups.items(),
            key=lambda kv: (-kv[1]["streak"], -kv[1]["events"],
                            kv[1]["first"] or "9999"))[0]
        event = group["event"]
        number = event.get("print_number")
        number = number if isinstance(number, int) and not isinstance(number, bool) else None
        title = None
        if number is not None:
            match = next((r for r in rows if r.get("print_number") == number), None)
            title = match.get("title") if match else None
        title = display_title(title, event.get("job_key"))
        worst = {"print_number": number, "job_key": display_title(event.get("job_key")),
                 "printer": event.get("printer"), "title": title,
                 "date": group["first"], "streak": group["streak"],
                 "events": group["events"]}
    started_text = str(recording_started[1]) if recording_started else None
    return {
        "pasta": len(period_keys),
        "pasta_events": len(period),
        "pasta_all_time_prints": len(all_keys),
        "pasta_print_numbers": sorted(k[1] for k in period_keys if k[0] == "number"),
        "pasta_recording_started_at": started_text,
        "worst_spaghetti": worst,
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
        entry = {"title": display_title(record.get("title")),
                 "print_number": record.get("print_number"), key: value}
        if key == "print_duration_seconds":
            entry["seconds"] = value
        out.append(entry)
    return out


_OUTCOME_OF_STATUS = {"completed": "finished", "failed": "failed", "canceled": "stopped",
                      "cancelled": "stopped", "printing": "printing"}
# The native printer state a record also carries (``moonraker_state``,
# written by print_records.build_record_data from the same stats the
# status came from): a second, independent statement of how the print
# ended, used only when the status itself still says "printing".
_OUTCOME_OF_NATIVE_STATE = {"complete": "finished", "completed": "finished",
                            "cancelled": "stopped", "canceled": "stopped",
                            "error": "failed", "failed": "failed"}
_NO_REASON = "No reason recorded"

# Where a print came from (#1719): the record's ``source_platform`` is
# the intake job's site (print_records copies it from the job), where
# "upload" is a file the user uploaded and "direct" is a direct file
# link the user shared. The display names are the sites' own names.
_SOURCE_LABELS = {"upload": "Uploaded files", "direct": "Direct links",
                  "makerworld": "MakerWorld", "printables": "Printables",
                  "thingiverse": "Thingiverse", "myminifactory": "MyMiniFactory",
                  "cults3d": "Cults3D", "crealitycloud": "Creality Cloud",
                  "thangs": "Thangs", "nexprint": "Nexprint",
                  "makeronline": "MakerOnline"}
_SOURCE_HOSTS = {"makerworld.com": "MakerWorld", "printables.com": "Printables",
                 "thingiverse.com": "Thingiverse", "myminifactory.com": "MyMiniFactory",
                 "cults3d.com": "Cults3D", "crealitycloud.com": "Creality Cloud",
                 "thangs.com": "Thangs", "nexprint.com": "Nexprint",
                 "makeronline.com": "MakerOnline"}


def _source_label(record, enriched_url=None):
    """Where one print came from, from what its record actually carries.

    The recorded platform wins. With no platform, a recorded source URL
    (the record's own, or the intake join's) names the site by its host.
    With neither, the print lands in the UNKNOWN bucket: prints started
    outside intake (on the printer itself, or before source recording)
    carry no source, and none is invented for them.
    """
    platform = str(record.get("source_platform") or "").strip().lower()
    if platform:
        return _SOURCE_LABELS.get(
            platform, platform.replace("_", " ").replace("-", " ").title())
    url = record.get("source_url") or enriched_url
    if isinstance(url, str) and url.strip():
        from urllib.parse import urlsplit
        host = (urlsplit(url.strip()).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host:
            return _SOURCE_HOSTS.get(host, host)
    return UNKNOWN

# Plain-language failure causes for the Data tab. Each rule is (cause,
# tip, lowercase fragments that name it in a printer's stop reason). The
# first rule that matches wins; a reason no rule names keeps its own
# text, so nothing is hidden or guessed.
_FAILURE_CAUSES = (
    ("Ran out of filament", "Check the spool has enough left before a long print.",
     ("runout", "run out", "ran out", "out of filament", "filament sensor")),
    ("Nozzle clog or jam", "Clean or replace the nozzle and check the extruder.",
     ("clog", "jam", "under-extru", "underextru", "grind")),
    ("Heater problem", "Check the heater and temperature sensor wiring.",
     ("thermal runaway", "heater", "thermistor", "temperature", "adc out of range")),
    ("Layer shift", "Check belt tension and that nothing blocks the toolhead.",
     ("layer shift", "skipped step", "stall")),
    ("Came loose from the bed", "Clean the bed and check the first layer.",
     ("adhesion", "detach", "warp", "came loose", "knocked")),
    ("Turned into spaghetti", "Check bed adhesion and the supports on this model.",
     ("spaghetti", "pasta")),
    ("Printer lost contact with its controller", "Check the USB cable and the printer's power supply.",
     ("mcu", "timer too close", "lost communication", "serial", "usb")),
    ("Emergency stop", "Find out what triggered the stop before printing again.",
     ("emergency", "e-stop", "estop", "shutdown")),
)


def _record_title(record):
    return (display_title(record.get("title"), record.get("job_key") or record.get("gcode_file"))
            or "Untitled print")


def _outcome(record):
    return _OUTCOME_OF_STATUS.get(_status(record), "unknown")


def _resolved_outcomes(rows):
    """id(record) -> outcome for a whole history, reconciling stale
    "printing" records (#1719).

    A start record is written when a print starts and finalized when
    its completion is recorded; when that finalization never happens
    (the completion was missed), the record says "printing" forever and
    the Data tab showed every such print as still printing. A record
    like that is only still printing when nothing contradicts it:

    - a terminal native state (``moonraker_state``) or a recorded
      ``completed_at`` on the record itself proves the print ended, and
      the recorded success flag names the verdict when there is one;
    - one printer prints one job at a time, so a later-started record
      on the same printer proves this print had ended — its outcome
      was never recorded, so it resolves to "unknown", never to an
      invented verdict.

    A "printing" record with neither contradiction (the newest record
    on its printer, no end evidence) stays "printing": it may really
    be the print running now.
    """
    latest_started: dict[str, object] = {}
    for record in rows:
        parsed = _parse_started(record.get("started_at"))
        if parsed is None:
            continue
        name = _label(record.get("printer"))
        if name not in latest_started or parsed > latest_started[name]:
            latest_started[name] = parsed
    resolved = {}
    for record in rows:
        outcome = _outcome(record)
        if outcome != "printing":
            resolved[id(record)] = outcome
            continue
        native = str(record.get("moonraker_state") or "").strip().lower()
        if native in _OUTCOME_OF_NATIVE_STATE:
            resolved[id(record)] = _OUTCOME_OF_NATIVE_STATE[native]
            continue
        if record.get("completed_at"):
            success = record.get("success")
            resolved[id(record)] = ("finished" if success is True else
                                    "failed" if success is False else "unknown")
            continue
        parsed = _parse_started(record.get("started_at"))
        latest = latest_started.get(_label(record.get("printer")))
        if parsed is not None and latest is not None and latest > parsed:
            resolved[id(record)] = "unknown"
            continue
        resolved[id(record)] = "printing"
    return resolved


def failure_cause(reason):
    """(cause, tip) in plain words for one recorded stop reason.

    A reason no rule recognizes is returned as its own text with no
    tip, never rewritten into a cause it may not be.
    """
    text = " ".join(str(reason or "").split())
    lowered = text.lower()
    for cause, tip, fragments in _FAILURE_CAUSES:
        if any(fragment in lowered for fragment in fragments):
            return cause, tip
    return (text[:120] or _NO_REASON), ""


def _failure_causes(rows):
    """Failed prints grouped by plain-language cause, biggest first; unexplained last."""
    groups: dict[str, dict] = {}
    for record in rows:
        if _status(record) != "failed":
            continue
        reason = record.get("stop_reason")
        reason = " ".join(reason.split()) if isinstance(reason, str) else ""
        cause, tip = failure_cause(reason) if reason else (_NO_REASON, "")
        entry = groups.setdefault(cause, {"cause": cause, "count": 0, "tip": tip,
                                          "reasons": [], "recorded": bool(reason)})
        entry["count"] += 1
        if reason and reason not in entry["reasons"]:
            entry["reasons"].append(reason)
    return sorted(groups.values(), key=lambda e: (not e["recorded"], -e["count"], e["cause"]))


def _record_cost(record):
    """(amount, currency) a print's frozen cost records as spent, else (None, None)."""
    cost = record.get("cost")
    if not isinstance(cost, dict):
        return None, None
    value = _num(cost.get("out_of_pocket"))
    if value is None:
        value = _num(cost.get("cost_per_build"))
    if value is None:
        return None, None
    return value, str(cost.get("currency_code") or "USD")


def _recent_prints(rows, limit=10, outcomes=None):
    """The newest dated prints first, with what a person reads in a history list."""
    dated = [(parsed, r) for r in rows if (parsed := _parse_started(r.get("started_at"))) is not None]
    dated.sort(key=lambda item: item[0], reverse=True)
    out = []
    for _parsed, record in dated[:limit]:
        cost_value, currency = _record_cost(record)
        reason = record.get("stop_reason")
        material = _label(record.get("filament_material"))
        out.append({
            "title": _record_title(record), "print_number": record.get("print_number"),
            "printer": _label(record.get("printer")), "status": _status(record),
            "outcome": (outcomes or {}).get(id(record), _outcome(record)),
            "started_at": record.get("started_at"),
            "seconds": _num(record.get("print_duration_seconds")),
            "filament_used_g": _num(record.get("filament_used_g")),
            "material": None if material == UNKNOWN else material,
            "cost": cost_value, "currency_code": currency,
            "stop_reason": " ".join(reason.split()) if isinstance(reason, str) and reason.strip()
            else None})
    return out


def _printer_summaries(rows, outcomes=None):
    """One summary per printer: how much it printed and how it went, busiest first."""
    table: dict[str, dict] = {}
    for record in rows:
        name = _label(record.get("printer"))
        entry = table.setdefault(name, {"printer": name, "prints": 0, "finished": 0, "failed": 0,
                                        "stopped": 0, "printing": 0, "seconds": 0.0,
                                        "filament_used_g": 0.0, "last_started_at": None})
        entry["prints"] += 1
        outcome = (outcomes or {}).get(id(record), _outcome(record))
        if outcome in ("finished", "failed", "stopped", "printing"):
            entry[outcome] += 1
        seconds = _num(record.get("print_duration_seconds"))
        if seconds is not None:
            entry["seconds"] += seconds
        grams = _num(record.get("filament_used_g"))
        if grams is not None:
            entry["filament_used_g"] += grams
        parsed = _parse_started(record.get("started_at"))
        last = _parse_started(entry["last_started_at"])
        if parsed is not None and (last is None or parsed > last):
            entry["last_started_at"] = record.get("started_at")
    out = []
    for entry in table.values():
        decided = entry["finished"] + entry["failed"]
        entry["success_rate"] = round(entry["finished"] / decided, 4) if decided else None
        entry["seconds"] = round(entry["seconds"], 1)
        entry["filament_used_g"] = round(entry["filament_used_g"], 1)
        out.append(entry)
    return sorted(out, key=lambda e: (-e["prints"], e["printer"]))


def _monthly_outcomes(rows, resolved, outcomes=None):
    """Per month (display timezone): prints, finished, failed, stopped and the success rate."""
    months: dict[str, dict] = {}
    for record in rows:
        parsed = _parse_started(record.get("started_at"))
        if parsed is None:
            continue
        key = _local(parsed, resolved).strftime("%Y-%m")
        entry = months.setdefault(key, {"prints": 0, "finished": 0, "failed": 0, "stopped": 0})
        entry["prints"] += 1
        outcome = (outcomes or {}).get(id(record), _outcome(record))
        if outcome in ("finished", "failed", "stopped"):
            entry[outcome] += 1
    for entry in months.values():
        decided = entry["finished"] + entry["failed"]
        entry["success_rate"] = round(entry["finished"] / decided, 4) if decided else None
    return dict(sorted(months.items()))


def aggregate(records, year=None, tz=None, events=None, enrichments=None):
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
    (``runtime._config.tz()``) so the recap matches the operator's local
    year (#981). ``started_at`` instants themselves stay UTC in the
    records and in every payload field that echoes them.

    ``events`` is the watchdog events list from
    :func:`load_watchdog_events` (all events in the file, unfiltered;
    the year rules are applied here, mirroring records). ``None``
    means no events file exists and pasta stays in the ``not_recorded``
    state; a list (even empty) means recording exists and pasta is a
    real count for the period. ``enrichments`` is the optional
    :mod:`print_handy.core.wrapped_joins` result, filtered here to the
    print numbers in scope.
    """
    resolved = _resolve_tz(tz)
    records = [r for r in (records or []) if isinstance(r, dict)]
    years = years_available(records, resolved)
    if year is not None:
        year = int(year)
        rows = records_in_year(records, year, resolved)
    else:
        rows = list(records)

    counts_by_status: dict[str, int] = {}
    for record in rows:
        status = _status(record)
        counts_by_status[status] = counts_by_status.get(status, 0) + 1
    # The headline outcome counts read the reconciled outcomes (#1719),
    # not the raw statuses: a record left at "printing" whose print
    # provably ended counts by its recorded outcome (or as unrecorded),
    # so the page never claims old prints are still printing.
    # counts_by_status above stays the raw record count, named as such.
    resolved_outcomes = _resolved_outcomes(rows)
    resolved_counts: dict[str, int] = {}
    for record in rows:
        outcome = resolved_outcomes[id(record)]
        resolved_counts[outcome] = resolved_counts.get(outcome, 0) + 1
    completed = resolved_counts.get("finished", 0)
    failed = resolved_counts.get("failed", 0)
    canceled = resolved_counts.get("stopped", 0)
    in_progress = resolved_counts.get("printing", 0)
    stale_printing = sum(
        1 for record in rows
        if _status(record) == "printing" and resolved_outcomes[id(record)] != "printing")
    finished = completed + failed + canceled
    # The success rate is out of prints that ran to a verdict: finished or
    # failed. A print the user stopped (canceled) is not a failure of the
    # printer or the model, so it stays out of both sides and is counted
    # on its own as "stopped".
    decided = completed + failed
    success_rate = round(completed / decided, 4) if decided else None
    undated = len([r for r in records if _parse_started(r.get("started_at")) is None])
    completed_grams = float(sum(
        v for r in rows if _status(r) == "completed"
        and (v := _num(r.get("filament_used_g"))) is not None))

    durations = [v for r in rows if (v := _num(r.get("print_duration_seconds"))) is not None]
    total_seconds = float(sum(durations))
    longest = None
    if durations:
        best = max(rows, key=lambda r: _num(r.get("print_duration_seconds"))
                   if _num(r.get("print_duration_seconds")) is not None else -1)
        longest = {"title": display_title(best.get("title")),
                   "print_number": best.get("print_number"),
                   "seconds": _num(best.get("print_duration_seconds"))}
    grams_values = [v for r in rows if (v := _num(r.get("filament_used_g"))) is not None]
    total_grams = float(sum(grams_values))
    estimated_values = [v for r in rows if (v := _num(r.get("estimated_filament_g"))) is not None]

    breakdowns = {}
    starter_labels = _starter_labels(rows)
    # The intake join's source URLs by print number (#1719): a record
    # that carries no source of its own can still be named by the job
    # it came from. A number two jobs share names neither (the join's
    # own rule), so those records stay in the unrecorded bucket.
    enriched_urls = {}
    if isinstance(enrichments, dict):
        for number, entry in (enrichments.get("per_print") or {}).items():
            if isinstance(entry, dict) and entry.get("source_url"):
                enriched_urls[number] = entry["source_url"]
    dimensions = {
        "by_starter": lambda r: starter_labels.get(starter_key(r), UNATTRIBUTED),
        "by_source": lambda r: _source_label(r, enriched_urls.get(r.get("print_number"))),
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

    # Derived grams (#981): recovered from filament_used_mm where no
    # grams were recorded, labeled DERIVED, and kept OUT of the
    # headline total above — the headline stays recorded-only.
    derived_total = 0.0
    derived_records = 0
    derived_fallback = False
    for record in rows:
        derived = _derived_grams(record)
        if derived is not None:
            grams, used_fallback = derived
            derived_total += grams
            derived_records += 1
            derived_fallback = derived_fallback or used_fallback

    # Per-color grams (#981): the record's native per-tool list first
    # (slicer_settings.filament_used_g_list, aligned with
    # filament_color_list), then the archived-job join's per-print
    # split for prints without a native list. A print is counted once.
    per_color: dict[str, float] = {}
    for record in rows:
        settings = _settings_dict(record) or {}
        grams_list = settings.get("filament_used_g_list")
        if not isinstance(grams_list, list) or not grams_list:
            continue
        colors = settings.get("filament_color_list")
        numeric = [g for g in grams_list if _num(g) is not None]
        for index, grams in enumerate(grams_list):
            value = _num(grams)
            if value is None:
                continue
            if isinstance(colors, list) and index < len(colors) and colors[index]:
                color = str(colors[index])
            elif len(numeric) == 1 and record.get("filament_color"):
                color = str(record["filament_color"])
            else:
                color = UNKNOWN
            per_color[color] = per_color.get(color, 0.0) + value
    scope_numbers = {r.get("print_number") for r in rows
                     if isinstance(r.get("print_number"), int)
                     and not isinstance(r.get("print_number"), bool)}
    enrich_out = {"per_print": {}, "support_waivers": 0,
                  "almost_made": {"count": 0, "titles": [], "scope_note": ""}}
    if isinstance(enrichments, dict):
        by_record = enrichments.get("per_color_grams_by_record")
        if isinstance(by_record, dict):
            # One entry per PRINT in scope: a number shared by an original
            # and its pre-#988 reprint counts both prints' grams.
            joined = [by_record.get(record_key(r)) for r in rows]
        else:
            joined = [grams for number, grams in
                      (enrichments.get("per_color_grams_by_print") or {}).items()
                      if number in scope_numbers]
        for grams_by_color in joined:
            if not isinstance(grams_by_color, dict):
                continue
            for color, grams in grams_by_color.items():
                value = _num(grams)
                if value is not None:
                    per_color[str(color)] = per_color.get(str(color), 0.0) + value
        scope_keys = {record_key(r) for r in rows}
        enrich_out["stop_detail_by_record"] = {
            key: detail for key, detail in
            (enrichments.get("stop_detail_by_record") or {}).items() if key in scope_keys}
        enrich_out["per_print"] = {
            number: entry for number, entry in
            (enrichments.get("per_print") or {}).items() if number in scope_numbers}
        enrich_out["support_waivers"] = len(
            [n for n in enrichments.get("support_waiver_print_numbers") or []
             if n in scope_numbers])
        if isinstance(enrichments.get("almost_made"), dict):
            enrich_out["almost_made"] = enrichments["almost_made"]
    per_color = {color: round(grams, 1) for color, grams in
                 sorted(per_color.items(), key=lambda kv: (-kv[1], kv[0]))}

    # Pasta (#981 v2): from the watchdog events file when the caller
    # loaded one; the not_recorded state when none exists.
    if events is None:
        pasta_block = {
            "pasta": None, "pasta_state": "not_recorded",
            "pasta_note": ("Pasta (spaghetti) events are not recorded yet: "
                           "the camera watchdog's detections are not persisted, "
                           "so no pasta count exists until event recording begins."),
            "pasta_events": None, "pasta_all_time_prints": None,
            "pasta_print_numbers": [], "pasta_recording_started_at": None,
            "worst_spaghetti": None}
    else:
        summary = _pasta_summary(
            [e for e in events if isinstance(e, dict)], year, resolved, rows)
        started = summary["pasta_recording_started_at"]
        pasta_block = {
            "pasta": summary["pasta"], "pasta_state": "recorded",
            "pasta_note": ("Pasta counts prints the camera watchdog flagged "
                           "as spaghetti in its recorded events, from when "
                           "event recording began"
                           + (f" ({started})" if started else "")
                           + "; events from before recording began were "
                             "never recorded and cannot be counted."),
            "pasta_events": summary["pasta_events"],
            "pasta_all_time_prints": summary["pasta_all_time_prints"],
            "pasta_print_numbers": summary["pasta_print_numbers"],
            "pasta_recording_started_at": started,
            "worst_spaghetti": summary["worst_spaghetti"]}

    return {
        "year": year,
        "timezone": _tz_name(resolved),
        "total_prints": len(rows),
        "counts_by_status": counts_by_status,
        # Prints with no recorded starter (#1647): counted in every
        # total above and named, never attributed after the fact.
        "unattributed_prints": sum(1 for r in rows if starter_key(r) is None),
        "completed": completed,
        "failed": failed,
        "canceled": canceled,
        "in_progress": in_progress,
        # Records still marked "printing" that the reconciliation above
        # proved had ended (#1719), with the note the page shows for them.
        "stale_printing_records": stale_printing,
        "stale_printing_note": (
            f"{stale_printing} older records were left marked printing after "
            "their prints ended; they are counted by the outcome their record "
            "proves, or as not recorded where the record proves only that the "
            "print ended." if stale_printing else ""),
        "finished": finished,
        "decided": decided,
        "success_rate": success_rate,
        "success_rate_note": ("Finished prints out of prints that finished or "
                              "failed; prints you stopped are not counted "
                              "either way."),
        "failure_rate": round(failed / decided, 4) if decided else None,
        # Records with no usable start time cannot be placed in a year or
        # month: a year view leaves them out and says how many.
        "undated_prints": undated,
        "completed_filament_used_g": completed_grams,
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
        # watchdog spaghetti events from the events file (v2); with no
        # events file it stays honestly None in the not-recorded state.
        # The risk-flag count above is a different thing and is never
        # presented as pasta.
        "outcomes": {
            "completed": completed,
            "failed": failed,
            "stopped": canceled,
            "in_progress": in_progress,
            "pasta": pasta_block["pasta"],
            "pasta_state": pasta_block["pasta_state"],
            "pasta_note": pasta_block["pasta_note"],
            "pasta_events": pasta_block["pasta_events"],
            "pasta_all_time_prints": pasta_block["pasta_all_time_prints"],
            "pasta_recording_started_at": pasta_block["pasta_recording_started_at"],
            "stopped_note": ("Stopped counts records with status canceled/cancelled. "
                             "The records do not say whether a person or the printer "
                             "stopped the print, so no finer split is shown."),
        },
        "worst_spaghetti": pasta_block["worst_spaghetti"],
        "pasta_print_numbers": pasta_block["pasta_print_numbers"],
        # #981 §5: headline totals are floor values on mixed fleets —
        # they sum only the prints that recorded the data, and the
        # payload says so with the counts behind each total.
        "totals_caveat": ("Hours and filament totals are of prints with that "
                          "data recorded."),
        "duration_records": len(durations),
        "filament_records": len(grams_values),
        "derived_filament_g": round(derived_total, 1),
        "derived_filament_records": derived_records,
        "derived_note": ("DERIVED: grams recovered from filament_used_mm for "
                         "prints that recorded millimeters but no grams, "
                         + ("using the documented fallback of 1.75 mm "
                            "diameter and per-material density defaults "
                            "where the record carries no diameter/density. "
                            if derived_fallback else
                            "using each record's own filament diameter and "
                            "density. ")
                         + "Derived grams are not included in the headline "
                           "filament total."),
        "per_color_grams": per_color,
        "per_color_state": "recorded" if per_color else "none",
        "enrichments": enrich_out,
        "repeats": _repeats(rows),
        "monthly": dict(sorted(monthly.items())),
        "by_hour": dict(sorted(by_hour.items())),
        "by_weekday": by_weekday,
        "night_prints": night_prints,
        "top_by_duration": _top(rows, "print_duration_seconds"),
        "top_by_filament": _top(rows, "filament_used_g"),
        # Cost aggregates (#1625): frozen per-job cost records only.
        # A period with no priced jobs says so plainly in the block's
        # note; nothing is estimated here and currencies never mix.
        "costs": _cost_aggregates(rows),
        # A separately labeled estimate for older unpriced prints; never
        # part of "costs" (#1632 review).
        "cost_backfill": _cost_backfill(rows),
        # No "timelapse_known" (#981): it keyed on a `timelapse` record
        # field no record ever carries, so it was structurally always 0.
        # Clip counts are a finder result, not a record fact; the Web UI
        # layer reports them separately as timelapse_clips_found.
        "years_available": years,
        # The Data tab's everyday views: newest prints first, each
        # printer's share and record, the month-by-month success trend and
        # the failure causes in plain words.
        "recent_prints": _recent_prints(rows, outcomes=resolved_outcomes),
        "printers": _printer_summaries(rows, outcomes=resolved_outcomes),
        "monthly_outcomes": _monthly_outcomes(rows, resolved, outcomes=resolved_outcomes),
        "failure_causes": _failure_causes(rows),
    }


def _cost_aggregates(rows):
    """Frozen-cost aggregates for the Data tab and Pasta Wrapped (#1625)."""
    from . import cost as _cost
    return _cost.aggregate_costs(rows)


def _cost_backfill(rows):
    """The labeled estimate for unpriced records; empty when settings cannot be read."""
    from . import cost as _cost
    try:
        from .. import settings as _settings
        data = _settings.current()
    except Exception:  # noqa: BLE001 - no settings means no estimate, never a failed page
        logging.getLogger(__name__).warning("cost backfill estimate skipped", exc_info=True)
        return {}
    return _cost.backfill_estimate(rows, data)


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
    # Sorted on the parsed instant: the raw strings differ in offset and
    # precision ("...Z" against "+02:00") and do not order by time.
    ordered = sorted((r for r in records if _parse_started(r.get("started_at")) is not None),
                     key=lambda r: _parse_started(r.get("started_at")))
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

    1. Spaghetti Magnet — at least 3 distinct prints with recorded
       pasta (spaghetti) events. It reads only ``outcomes.pasta``
       (the watchdog events count), so it fires only when events were
       loaded, and it never reads risk flags as pasta.
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
    decided = stats["decided"]
    if (decided >= 20 and stats["success_rate"] is not None
            and stats["success_rate"] >= 0.95):
        return {"name": "The Perfectionist",
                "fact": f"{round(stats['success_rate'] * 100):g}% success across "
                        f"{decided} finished or failed prints."}
    reprints = stats["repeats"]["reprint_count"]
    all_prints = stats["total_prints"]
    # Out of every print in the year, so the share can never pass 100%.
    if all_prints and reprints and reprints / all_prints >= 0.25:
        return {"name": "Reprint Machine",
                "fact": f"{reprints} of {plural(all_prints, 'print')} were reprints."}
    durations = sorted(v for r in rows
                       if (v := _num(r.get("print_duration_seconds"))) is not None)
    longest = stats["longest_print"]
    if durations and longest and longest.get("seconds"):
        median = durations[len(durations) // 2] if len(durations) % 2 else \
            (durations[len(durations) // 2 - 1] + durations[len(durations) // 2]) / 2
        if median and longest["seconds"] >= 3 * median and longest["seconds"] >= 7200:
            return {"name": "Marathoner",
                    "fact": f"Your longest print ran {hours_text(longest['seconds'] / 3600)}, "
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
                        f"{highlights['top_color_prints']} of {plural(total, 'print')}."}
    return {"name": "The Regular",
            "fact": f"A steady year: {plural(total, 'print')}, no single habit took over."}


def build_wrapped(records, year, tz=None, events=None, enrichments=None,
                  selection=None):
    """The Wrapped payload for one year: headline stats, highlights, script.

    Every script line is data-driven: a line is included only when the
    number behind it is true in this same payload. ``tz`` has the same
    meaning as in :func:`aggregate` (None preserves UTC bucketing).
    ``events`` and ``enrichments`` pass through to :func:`aggregate`.

    ``selection`` (#1647) is the descriptor from
    :func:`selection_descriptor` when the caller filtered the records
    to one starter and/or one printer first (with
    :func:`filter_records`): it rides in the payload as ``selection``
    and names the recap's coverage in the script's first line, so a
    per-user or per-printer recap is never mistaken for the whole
    history. Unfiltered callers pass nothing and get today's payload
    unchanged.
    """
    if isinstance(selection, dict) and selection.get("label"):
        selection = {"user": selection.get("user"),
                     "printer": selection.get("printer"),
                     "label": str(selection["label"])}
    else:
        selection = None
    resolved = _resolve_tz(tz)
    records = [r for r in (records or []) if isinstance(r, dict)]
    year = int(year)
    stats = aggregate(records, year=year, tz=resolved, events=events,
                      enrichments=enrichments)
    rows = records_in_year(records, year, resolved)
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
    # Worst failure (#981 v2): the failed print that burned the most
    # time. Its stop_capture sidecar detail rides along only when the
    # enrichments join read one — absent sidecar, nothing is invented.
    worst_failure = None
    failed_rows = [r for r in rows if _status(r) == "failed"]
    if failed_rows:
        best_failed = max(
            failed_rows,
            key=lambda r: (_num(r.get("print_duration_seconds")) or 0,
                           str(r.get("started_at") or "")))
        worst_failure = {"title": display_title(best_failed.get("title")),
                         "print_number": best_failed.get("print_number"),
                         "seconds": _num(best_failed.get("print_duration_seconds"))}
        # The failed print's OWN stop detail (by record): a number shared
        # with another print in pre-#988 history must not lend it the
        # other run's stop reason.
        by_record = stats["enrichments"].get("stop_detail_by_record")
        if isinstance(by_record, dict) and by_record:
            detail = by_record.get(record_key(best_failed))
        else:
            detail = (stats["enrichments"]["per_print"].get(
                best_failed.get("print_number")) or {}).get("stop_detail")
        if isinstance(detail, dict):
            worst_failure["stop_detail"] = detail
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
        "worst_failure": worst_failure,
        "pasta": stats["outcomes"]["pasta"],
        "worst_spaghetti": stats["worst_spaghetti"],
        "failed_print_numbers": sorted(
            r["print_number"] for r in failed_rows
            if isinstance(r.get("print_number"), int)
            and not isinstance(r.get("print_number"), bool)),
        "pasta_print_numbers": stats["pasta_print_numbers"],
        "night_prints": stats["night_prints"],
        "most_reprinted": most_reprinted,
        "reprint_count": stats["repeats"]["reprint_count"],
        "peak_hour": peak_hour, "peak_weekday": peak_weekday,
    }
    highlights["archetype"] = _archetype(stats, highlights, rows)
    script = _script(year, stats, highlights, tz=resolved, selection=selection)
    return {"year": year, "selection": selection, "stats": stats, "headline": {
                "total_prints": stats["total_prints"], "completed": stats["completed"],
                "failed": stats["failed"], "canceled": stats["canceled"],
                # Pasta/failed/stopped stay separate (#981): stopped is
                # the canceled count under its plain name; pasta is the
                # real event count when watchdog events were loaded, or
                # the honest not-recorded state (None) when none exist.
                "stopped": stats["outcomes"]["stopped"],
                "pasta": stats["outcomes"]["pasta"],
                "pasta_state": stats["outcomes"]["pasta_state"],
                "success_rate": stats["success_rate"], "total_hours": stats["total_hours"],
                "total_filament_used_g": stats["total_filament_used_g"],
                "total_filament_used_kg": stats["total_filament_used_kg"],
                "completed_filament_used_g": stats["completed_filament_used_g"],
                "decided": stats["decided"],
                "undated_prints": stats["undated_prints"]},
            "highlights": highlights, "script": script,
            "archetype": highlights["archetype"],
            "years_available": stats["years_available"]}


def _script(year, stats, highlights, tz=None, selection=None):
    resolved = _resolve_tz(tz)
    local_tz = resolved != timezone.utc
    lines = []
    total = stats["total_prints"]
    if not total:
        if selection:
            return [f"Pasta Wrapped {year}: no prints were recorded this year "
                    f"for {selection['label']}. The printer rested. Respect."]
        return [f"Pasta Wrapped {year}: no prints were recorded this year. "
                "The printer rested. Respect."]
    if selection:
        label = selection["label"]
        subject = label[:1].upper() + label[1:]
        lines.append(f"Pasta Wrapped {year} — {subject} made "
                     f"{plural(total, 'print')}. This recap covers only "
                     f"{label}.")
    else:
        lines.append(f"Pasta Wrapped {year}: you made {plural(total, 'print')}. "
                     "The printer did the work, but you get the recap.")
    hours = stats["total_hours"]
    if hours >= 1:
        lines.append(f"You spent {hours_text(hours)} printing this year. "
                     "That is a part-time job with no paycheck, only parts.")
    grams = stats["total_filament_used_g"]
    if grams > 0:
        done_grams = stats.get("completed_filament_used_g") or 0
        kept = (f" {done_grams:g} g of it went into prints that finished."
                if 0 < done_grams < grams else "")
        lines.append(f"You ran {grams:g} g of filament through the printer "
                     f"({stats['total_filament_used_kg']:g} kg), failed and stopped "
                     f"prints included.{kept} Some of it even stayed on the bed.")
    # Recorded costs (#1625): frozen per-job figures only, no estimate
    # and no model involved. A year with no priced jobs gets no line.
    costs = stats.get("costs") or {}
    if costs.get("has_priced"):
        cur = costs["currency_code"]
        coverage = (f" ({costs['priced_count']} of {costs['considered_count']} prints "
                    "had a recorded cost)" if costs.get("unpriced_count") else "")
        lines.append(f"You spent {cur} {costs['out_of_pocket_total']:.2f} on filament, "
                     f"power and consumables across "
                     f"{plural(costs['priced_count'], 'priced print')}{coverage}. "
                     "That is the frozen cost on each record, not a fresh estimate.")
        priciest = costs.get("most_expensive")
        if priciest:
            lines.append(f"Your most expensive print was {priciest['title']} at "
                         f"{cur} {priciest['out_of_pocket']:.2f}.")
        if costs.get("failed_count"):
            lines.append(f"Your failures cost you {cur} {costs['failure_total']:.2f}.")
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
        lines.append(f"Your busiest printer, by number of prints, was {highlights['top_printer']}. "
                     "It has earned a nozzle check.")
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
        lines.append(f"{plural(failed, 'print')} failed this year. "
                     "The spaghetti was not always dinner.")
    elif stats["finished"]:
        lines.append("Zero failed prints this year. Wrapped does not believe it either, "
                     "but the records say so.")
    pasta = (stats.get("outcomes") or {}).get("pasta")
    if isinstance(pasta, (int, float)) and pasta > 0:
        lines.append(f"{plural(int(pasta), 'print')} turned into pasta "
                     "this year, caught by the camera watchdog and written down "
                     "this time. The count covers events recorded since "
                     "watchdog event recording began.")
        worst_spaghetti = highlights.get("worst_spaghetti")
        if worst_spaghetti and worst_spaghetti.get("streak", 0) >= 2:
            name = display_title(worst_spaghetti.get("title"),
                                 worst_spaghetti.get("job_key")) or "one print"
            lines.append(f"The worst of it: {name} hit a spaghetti streak of "
                         f"{worst_spaghetti['streak']} before anyone stepped in.")
    if highlights["worst_failure_streak"] >= 3:
        lines.append(f"Your worst run was {highlights['worst_failure_streak']} failed or "
                     "canceled prints in a row. Character building, technically.")
    if stats["risk_flagged"] > 0:
        lines.append(f"{plural(stats['risk_flagged'], 'print')} started with a risk flag. "
                     "The slicer findings and support checks raised a hand before "
                     "those prints ever started.")
    if highlights["night_prints"] > 0:
        when = "between midnight and 5 a.m. your time" if local_tz else \
            "between midnight and 5 a.m. UTC"
        lines.append(f"{plural(highlights['night_prints'], 'print')} started "
                     f"{when}. Sleep was also an option.")
    longest = stats["longest_print"]
    if longest and longest.get("seconds"):
        title = longest.get("title") or "your longest print"
        lines.append(f"Your longest print, {title}, ran "
                     f"{hours_text(longest['seconds'] / 3600)}. Commitment, or a very long nap.")
    if highlights["busiest_month"]:
        lines.append(f"Your busiest month was {highlights['busiest_month']} with "
                     f"{plural(highlights['busiest_month_prints'], 'print')}. The printer remembers.")
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


__all__ = ["UNKNOWN", "UNATTRIBUTED", "aggregate", "display_title", "filter_events_for_records",
           "filter_records", "hours_text", "plural", "printer_options", "build_wrapped", "load_records",
           "load_records_from_dir", "load_watchdog_events", "prints_dir",
           "record_key", "record_starter", "records_in_year", "selection_descriptor",
           "starter_key", "starter_label", "starter_options",
           "watchdog_events_path", "years_available"]
