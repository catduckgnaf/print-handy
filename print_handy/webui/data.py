"""Web UI Data tab and Pasta Wrapped pages (#956).

``GET /data`` and ``GET /wrapped`` are the pages; ``GET /api/data``
and ``GET /api/wrapped`` are the same payloads as JSON;
``POST /wrapped/video`` compiles the Wrapped video and
``POST /wrapped/share`` exports the static share bundle
(:mod:`print_handy.webui.share_export`).

The surface is on by default (settings restructure stage 5), under
two switches: ``[timelapse] data_enabled`` gates the Data tab and
``[timelapse] wrapped_enabled`` gates Wrapped. Both default true;
while data_enabled is unset it follows wrapped_enabled (the loader
bakes the followed value in and stamps ``data_enabled_explicit``
false, and :func:`data_enabled` applies the same fallback to a
loaded or raw mapping alike). While a switch is off, its routes
answer 403 with the reason naming that switch, and the navigation
carries only the links whose switch is on. The video step
additionally needs ``[timelapse] wrapped_video`` (default false);
while that is off the Wrapped page says so by name and the video
route refuses by name.

Aggregation is read-only over the print JSON records
(:mod:`print_handy.core.print_stats`); the records vault comes from
the loaded settings the server already carries (``[paths]
records_vault``), never from new global state. The video trigger is a
page form POST (the Spools page pattern: a login session plus the
server form token, checked by the server), because it is a browser
action from the Wrapped page, not an API call — API POSTs here would
need the header-only rule and a browser could never use them.

Each handler returns ``(http_code, body)`` in the Web UI's
``{ok, reason}`` shape, or an HTML string for a page render.
"""
from __future__ import annotations

import html
import os
import threading
import time
from pathlib import Path

from ..core import print_stats, wrapped_joins
from . import share_export
from . import shell
from . import wrapped_video as video_module

DATA_OFF_REASON = ("the Data tab is off: set "
                   "[timelapse] data_enabled = true to allow it. Nothing shown.")
WRAPPED_OFF_REASON = ("Pasta Wrapped is off: set "
                      "[timelapse] wrapped_enabled = true to allow it. Nothing shown.")
VIDEO_OFF_REASON = ("the Wrapped video is off: set [timelapse] wrapped_video = true "
                    "(with wrapped_enabled on) to allow it. Nothing was compiled.")


def wrapped_enabled(data) -> bool:
    return bool(((data or {}).get("timelapse") or {}).get("wrapped_enabled", True))


def data_enabled(data) -> bool:
    """The Data tab switch: data_enabled when set, else wrapped_enabled.

    A loaded settings mapping carries ``data_enabled_explicit`` (the
    loader stamps it, the ``mode_explicit`` pattern): while it is
    false the baked ``data_enabled`` value is only the followed one,
    so the gate keeps following ``wrapped_enabled`` on that mapping
    instead of treating the baked value as a pin. A raw mapping
    without the flag keeps the plain fallback: an absent
    ``data_enabled`` follows, a present one pins.
    """
    timelapse = (data or {}).get("timelapse") or {}
    if timelapse.get("data_enabled_explicit") is False:
        return wrapped_enabled(data)
    value = timelapse.get("data_enabled")
    if value is None:
        return wrapped_enabled(data)
    return bool(value)


def ai_video_enabled(data) -> bool:
    return bool(((data or {}).get("timelapse") or {}).get("wrapped_video", False))


def nav_for(data):
    """The shell nav for a settings mapping: a Data and/or Wrapped link
    appears only while its own switch is on."""
    entries = list(shell.NAV[:-1])
    if data_enabled(data):
        entries.append(("/data", "Data"))
    if wrapped_enabled(data):
        entries.append(("/wrapped", "Wrapped"))
    entries.append(shell.NAV[-1])
    return tuple(entries)


def _vault(data, records_vault):
    if records_vault is not None:
        return Path(records_vault)
    return Path(((data or {}).get("paths") or {}).get("records_vault") or ".")


# ------------------------------------------------------------------ cache
#
# Every Data or Wrapped load used to read and parse every record, rebuild
# the receipt/JOB joins over all of them, aggregate (twice on Wrapped) and
# re-glob the clip folders. The results are now kept for a short time,
# keyed on what they were computed from: the records folder's fingerprint
# (record count, newest modification time, total size), the year, the
# display timezone and the files the joins read. A new or changed record
# changes the key at once; the time-to-live only bounds how stale the
# receipt, JOB.json, clip and Spoolman inputs (which have no cheap
# fingerprint) can look.

SUMMARY_TTL_S = 30.0
SPOOLMAN_TTL_S = 60.0
SPOOLMAN_TIMEOUT_S = 2.0
_CACHE_MAX = 64
_cache = {}
_cache_lock = threading.Lock()
_build_lock = threading.RLock()


def clear_cache():
    """Forget every cached view (tests, and a way to force a rebuild)."""
    with _cache_lock:
        _cache.clear()


def _cached(key, build, ttl=SUMMARY_TTL_S, serialize=True):
    """The value for ``key`` if fresh, else ``build()`` stored under it.

    Builds are serialized by default, so two page loads arriving together
    compute a summary once instead of twice.
    """
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
    lock = _build_lock if serialize else threading.Lock()
    with lock:
        with _cache_lock:
            hit = _cache.get(key)
            if hit is not None and time.monotonic() - hit[0] < ttl:
                return hit[1]
        value = build()
        with _cache_lock:
            if len(_cache) >= _CACHE_MAX:
                oldest = min(_cache, key=lambda k: _cache[k][0])
                _cache.pop(oldest, None)
            _cache[key] = (time.monotonic(), value)
        return value


def _stamp(path):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _records_fingerprint(vault):
    """(count, newest modification time, total bytes) of the record files."""
    count = newest = size = 0
    try:
        with os.scandir(print_stats.prints_dir(vault)) as entries:
            for entry in entries:
                if entry.name.startswith("print-") and entry.name.endswith(".json"):
                    stat = entry.stat()
                    count += 1
                    newest = max(newest, stat.st_mtime_ns)
                    size += stat.st_size
    except OSError:
        pass
    return (count, newest, size)


def _tz_key(tz):
    return getattr(tz, "key", None) or str(tz)


def _sources(data):
    """The joins' input folders: (archive dir, intake receipts dir)."""
    archive_dir = ((data or {}).get("paths") or {}).get("archive")
    receipts_dir = os.environ.get("PRINT_HANDY_INTAKE_RECEIPTS")
    if not receipts_dir:
        try:
            from .. import settings as _settings
            receipts_dir = str(_settings.state_dir(data) / "model-link-intakes")
        except Exception:
            receipts_dir = None
    return (str(archive_dir) if archive_dir else None, receipts_dir)


def _load(data, records_vault):
    """(records, skipped_names), cached on the records folder's fingerprint."""
    vault = _vault(data, records_vault)
    key = ("records", str(vault), _records_fingerprint(vault))
    return _cached(key, lambda: print_stats.load_records(vault))


def _load_events(data, records_vault):
    """(events_or_None, skipped_lines): the watchdog events file.

    None (not an empty list) when no events file exists, so the
    aggregate keeps the honest not_recorded pasta state (#981 v2).
    """
    vault = _vault(data, records_vault)

    def build():
        try:
            events, skipped, exists = print_stats.load_watchdog_events(vault)
        except Exception:
            return None, []
        return (events if exists else None), skipped

    key = ("events", str(vault), _stamp(print_stats.watchdog_events_path(vault)))
    return _cached(key, build)


def _load_enrichments(data, records, year=None, tz=None, records_vault=None):
    """The receipt/JOB joins, or None when they cannot be built.

    Fail-soft by design: the joins are read-only beside the records,
    and a broken archive or receipts folder must never take the Data
    tab down with it. Receipts live where model_job reads them
    (``PRINT_HANDY_INTAKE_RECEIPTS`` or ``<state>/model-link-intakes``).
    Per-record work (stop sidecars, archived G-code) is done only for the
    selected year's records, and the per-record color splits recovered
    from archived G-code persist beside the Wrapped outputs, so each is
    read once.
    """
    try:
        archive_dir, receipts_dir = _sources(data)
        scope = (print_stats.records_in_year(records, year, tz)
                 if year is not None else None)
        return wrapped_joins.build_enrichments(
            records, archive_dir=archive_dir, receipts_dir=receipts_dir, scope=scope,
            derived_cache=_vault(data, records_vault) / "wrapped" / "derived-cache.json")
    except Exception:
        return None


def _spoolman_lifetime(data):
    """The all-time Spoolman block (never per-year), fail-soft.

    One short, bounded attempt, remembered for a minute either way: an
    unreachable Spoolman costs the first page load two seconds at most,
    and never fails the page.
    """
    base = str(((data or {}).get("services") or {}).get("spoolman_url") or "").strip()

    def build():
        try:
            from ..core import spoolman_client
            spools = spoolman_client.list_spools(
                base_url=base or None, timeout=SPOOLMAN_TIMEOUT_S, allow_archived=True)
        except Exception:
            spools = None
        return wrapped_joins.spoolman_lifetime(spools)

    return _cached(("spoolman", base), build, ttl=SPOOLMAN_TTL_S, serialize=False)


def _maintenance(data, records_vault, year, tz):
    vault = _vault(data, records_vault)
    path = vault / "Maintenance" / "maintenance.jsonl"

    def build():
        try:
            return wrapped_joins.maintenance_block(vault, year=year, tz=tz)
        except Exception:
            return {"state": "absent", "sessions": 0, "by_kind": {}, "problems": 0,
                    "cold_pulls": 0, "skipped_lines": []}

    return _cached(("maintenance", str(vault), _stamp(path), year, _tz_key(tz)), build)


def _display_tz(data):
    """The configured display timezone for Wrapped bucketing (#981).

    The name comes from the settings mapping the handlers already
    carry (``discord.timezone``, the same setting
    ``hermes._config.tz()`` reads); when the mapping names none, the
    ``_config.tz()`` value itself is used. Any failure falls back to
    UTC, which is the pure functions' own default bucketing.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    name = str(((data or {}).get("discord") or {}).get("timezone") or "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    try:
        from ..hermes import _config
        return _config.tz()
    except Exception:
        from datetime import timezone
        return timezone.utc


def _summary(data, year, records_vault, kind):
    """One year's computed view, built once per fingerprint.

    ``kind`` is ``"stats"`` (the Data tab's aggregate; ``year`` may be
    None for all time) or ``"wrapped"`` (the Wrapped payload for a whole
    number ``year``). Returns a dict with the records, the skipped
    record names, the events and the timezone beside the result, so no
    caller loads anything a second time.
    """
    vault = _vault(data, records_vault)
    tz = _display_tz(data)
    year = int(year) if year is not None else None
    key = ("summary", kind, str(vault), _records_fingerprint(vault),
           _stamp(print_stats.watchdog_events_path(vault)), year, _tz_key(tz),
           _sources(data))

    def build():
        records, skipped = _load(data, records_vault)
        events, skipped_events = _load_events(data, records_vault)
        enrichments = _load_enrichments(data, records, year, tz, records_vault)
        result = {"records": records, "skipped": skipped, "events": events,
                  "skipped_events": skipped_events, "tz": tz}
        if kind == "wrapped":
            result["wrapped"] = print_stats.build_wrapped(
                records, year, tz=tz, events=events, enrichments=enrichments)
        else:
            result["stats"] = print_stats.aggregate(
                records, year=year, tz=tz, events=events, enrichments=enrichments)
        return result

    return _cached(key, build)


def _clips(data, year, records_vault, records, tz):
    """The year's timelapse clips joined to print numbers, remembered briefly."""
    vault = _vault(data, records_vault)
    archive_dir, _receipts = _sources(data)
    cache_dir = ((data or {}).get("paths") or {}).get("cache")
    archive_clips = ((data or {}).get("timelapse") or {}).get("archive_dir")
    key = ("clips", str(vault), _records_fingerprint(vault), int(year), _tz_key(tz),
           str(cache_dir), str(archive_clips))

    def build():
        try:
            return video_module.default_clip_finder_detailed(
                data, int(year), records=records, tz=tz)
        except Exception:
            return []

    return _cached(key, build)


def data_view(data, year=None, *, records_vault=None):
    """(code, body) for GET /api/data."""
    if not data_enabled(data):
        return 403, {"ok": False, "reason": DATA_OFF_REASON}
    try:
        year_value = int(year) if year is not None else None
    except (TypeError, ValueError):
        return 400, {"ok": False, "reason": "year must be a whole number. Nothing shown."}
    summary = _summary(data, year_value, records_vault, "stats")
    return 200, {"ok": True, **summary["stats"], "skipped_records": summary["skipped"],
                 "skipped_event_lines": summary["skipped_events"],
                 "maintenance": _maintenance(data, records_vault, year_value, summary["tz"]),
                 "spoolman_lifetime": _spoolman_lifetime(data)}


def wrapped_view(data, year=None, *, records_vault=None):
    """(code, body) for GET /api/wrapped. The default year is the latest recorded."""
    if not wrapped_enabled(data):
        return 403, {"ok": False, "reason": WRAPPED_OFF_REASON}
    records, skipped = _load(data, records_vault)
    tz = _display_tz(data)
    years = print_stats.years_available(records, tz)
    if year is None:
        year = years[-1] if years else _current_year(tz)
    try:
        year = int(year)
    except (TypeError, ValueError):
        return 400, {"ok": False, "reason": "year must be a whole number. Nothing shown."}
    summary = _summary(data, year, records_vault, "wrapped")
    payload = summary["wrapped"]
    # #981: clip counts are a finder result, not a record fact, so they
    # live here at the Web UI layer (where the finder runs) under their
    # own name, not inside the record aggregate. V2: the detailed
    # finder also joins clips to print numbers by delivery name.
    detailed = _clips(data, year, records_vault, records, tz)
    clips_by_print = [{"print_number": entry["print_number"],
                       "name": Path(entry["path"]).name}
                      for entry in detailed if entry.get("print_number")]
    first_clip_name = {}
    for clip in clips_by_print:
        first_clip_name.setdefault(clip["print_number"], clip["name"])
    longest = (payload.get("highlights") or {}).get("longest_print") or {}
    longest_clip = first_clip_name.get(longest.get("print_number"))
    # #981 sharing: the share panel lists past exports from the local
    # registry beside the wrapped outputs; a corrupt registry fails
    # soft into a named note here, never a broken page.
    share_entries, share_note = share_export.load_exports(
        _vault(data, records_vault) / "wrapped")
    return 200, {"ok": True, **payload, "skipped_records": summary["skipped"],
                 "share_exports": share_entries,
                 "share_preview": share_export.preview(payload),
                 "share_registry_note": share_note,
                 "skipped_event_lines": summary["skipped_events"],
                 "timelapse_clips_found": len(detailed),
                 "timelapse_clips_by_print": clips_by_print,
                 "longest_print_clip": longest_clip,
                 "maintenance": _maintenance(data, records_vault, year, tz),
                 "video_available": wrapped_video_path(data, year,
                                                       records_vault=records_vault) is not None,
                 "video_job": video_module.JOBS.status(year),
                 "ai_video_enabled": ai_video_enabled(data)}


def wrapped_video_path(data, year, *, records_vault=None, out_dir=None):
    """The compiled video file for ``year``, or None.

    The only file the video route may serve: exactly
    ``wrapped-<year>.mp4`` inside the wrapped outputs folder, for a
    whole-number year, and only while Wrapped is enabled. The name
    is constructed here from the integer year, never taken from a
    request, so path traversal has no input to work with.
    """
    if not wrapped_enabled(data):
        return None
    try:
        year = int(year)
    except (TypeError, ValueError):
        return None
    folder = Path(out_dir) if out_dir is not None else _vault(data, records_vault) / "wrapped"
    candidate = folder / f"wrapped-{year}.mp4"
    return candidate if candidate.is_file() else None


def parse_byte_range(header, size):
    """The (start, end) bytes a ``Range`` header asks of a ``size``-byte file.

    Inclusive offsets for a single satisfiable ``bytes=`` range; None when
    there is no usable header (serve the whole file); the string
    ``"unsatisfiable"`` when the range lies outside the file (answer 416).
    Only one range is honored: a multi-range request is served whole.
    """
    if not header or not str(header).strip().lower().startswith("bytes="):
        return None
    spec = str(header).strip()[6:].strip()
    if "," in spec or "-" not in spec:
        return None
    first, _, last = spec.partition("-")
    first, last = first.strip(), last.strip()
    try:
        if not first:  # a suffix: the last N bytes
            count = int(last)
            if count <= 0:
                return "unsatisfiable"
            return (max(0, size - count), size - 1) if size else "unsatisfiable"
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return "unsatisfiable"
    return (start, min(end, size - 1))


def _current_year(tz=None):
    from datetime import datetime, timezone
    return datetime.now(tz or timezone.utc).year


def make_video(data, year, *, records_vault=None, out_dir=None, export_silent=False,
               wait=False, jobs=None, **video_kwargs):
    """(code, body) for POST /wrapped/video: start the compile, or join it.

    The compile is one background job per year (see
    :class:`wrapped_video.VideoJobs`): this returns at once with 202 and
    the job's status, and a request for a year already being compiled
    joins that job (``"joined": true``) instead of starting another.
    ``wait`` blocks until the job ends and answers in the old
    synchronous shape (200 with the paths, or 502 with the reason).
    """
    if not wrapped_enabled(data):
        return 403, {"ok": False, "reason": WRAPPED_OFF_REASON}
    if not ai_video_enabled(data):
        return 403, {"ok": False, "reason": VIDEO_OFF_REASON}
    try:
        year = int(year)
    except (TypeError, ValueError):
        return 400, {"ok": False, "reason": "year must be a whole number. Nothing was compiled."}
    target_dir = Path(out_dir) if out_dir is not None else _vault(data, records_vault) / "wrapped"

    def work(progress):
        wrapped, records = _wrapped_for_video(data, year, records_vault)
        kwargs = dict(video_kwargs)
        kwargs.setdefault("records", records)
        kwargs.setdefault("tz", _display_tz(data))
        return video_module.build_wrapped_video(
            year, wrapped, target_dir, data=data, export_silent=export_silent,
            progress=progress, **kwargs)

    status, joined = (jobs or video_module.JOBS).start(year, work, wait=wait)
    if wait and status["state"] == "failed":
        return 502, {"ok": False, "reason": status["reason"] or "the video compile failed",
                     "script_path": status["script_path"], "notes": status["notes"]}
    if wait and status["state"] == "done":
        return 200, {"ok": True, "year": year, "path": status["path"],
                     "script_path": status["script_path"], "notes": status["notes"]}
    return 202, {"ok": True, "year": year, "joined": joined, "job": status}


def _wrapped_for_video(data, year, records_vault):
    summary = _summary(data, year, records_vault, "wrapped")
    return summary["wrapped"], summary["records"]


def video_status(data, year, *, records_vault=None, jobs=None):
    """(code, body) for GET /api/wrapped/video-status?year=: the page's poll."""
    if not wrapped_enabled(data):
        return 403, {"ok": False, "reason": WRAPPED_OFF_REASON}
    try:
        year = int(year)
    except (TypeError, ValueError):
        return 400, {"ok": False, "reason": "year must be a whole number. Nothing shown."}
    status = (jobs or video_module.JOBS).status(year)
    return 200, {"ok": True, **status,
                 "available": wrapped_video_path(
                     data, year, records_vault=records_vault) is not None}


def export_share(data, year, visibility="link-only", *, records_vault=None, out_dir=None,
                 destination_note="", passphrase=None, include_titles=True,
                 include_printers=True, include_video=False):
    """(code, body) for POST /wrapped/share: one explicit export.

    Behind the same ``wrapped_enabled`` gate as every other Wrapped
    surface. The bundle is written locally under the wrapped outputs
    (``<vault>/wrapped/exports/``); nothing is uploaded anywhere.
    Private exports refuse by name inside share_export until an
    established AES-GCM library is a dependency.
    """
    if not wrapped_enabled(data):
        return 403, {"ok": False, "reason": WRAPPED_OFF_REASON}
    try:
        wrapped = _summary(data, int(year), records_vault, "wrapped")["wrapped"]
    except (TypeError, ValueError):
        return 400, {"ok": False, "reason": "year must be a whole number. Nothing was exported."}
    target_dir = Path(out_dir) if out_dir is not None else _vault(data, records_vault) / "wrapped"
    result = share_export.export_bundle(
        wrapped, target_dir, visibility=str(visibility or "link-only"),
        destination_note=destination_note, passphrase=passphrase,
        include_titles=include_titles, include_printers=include_printers,
        include_video=include_video)
    if not result.get("ok"):
        return 400, {"ok": False, "reason": result.get("reason") or "the export failed"}
    return 200, {"ok": True, **result}


# ------------------------------------------------------------------ pages

def _esc(value):
    return html.escape(str(value), quote=True)


def _page(data, title, body, *, path, theme=shell.DEFAULT_THEME):
    return shell.page(title, body, path=path, theme=theme, nav=nav_for(data))


def _cards(pairs):
    items = "".join(
        f'<section class="card"><p class="eyebrow">{_esc(label)}</p>'
        f"<h2>{_esc(value)}</h2></section>" for label, value in pairs)
    return f'<div class="cards">{items}</div>'


def _breakdown_table(title, table):
    if not table:
        return f"<h2>{_esc(title)}</h2><p>(nothing recorded)</p>"
    rows = "".join(
        f"<tr><td>{_esc(label)}</td><td>{row['count']}</td>"
        f"<td>{row['filament_used_g']:g} g</td>"
        f"<td>{row['print_duration_seconds'] / 3600:.2f} h</td></tr>"
        for label, row in table.items())
    return (f"<h2>{_esc(title)}</h2><div class=\"table-scroll\"><table>"
            f"<tr><th>{_esc(title)}</th><th>prints</th><th>filament</th><th>time</th></tr>"
            f"{rows}</table></div>")


def _undated_text(count):
    return (f"{print_stats.plural(count, 'print')} without a date "
            f"{'was' if count == 1 else 'were'} left out of this year.")


def _rate(rate):
    return "unknown" if rate is None else f"{round(rate * 100):g}%"


def _bar_chart(title, items):
    """One server-rendered inline SVG horizontal bar chart.

    ``items`` is a list of (label, value) pairs read from the same
    aggregate payload the tables render — there is no second counting
    path. Every bar carries its label and value as SVG text, and the
    figure carries an aria label, so the chart stays readable as text
    without SVG (the tables on this page carry the same numbers).
    """
    items = [(str(label), float(value)) for label, value in items]
    peak = max((value for _label, value in items), default=0.0) or 1.0
    width, row_h, label_w, value_w = 560, 22, 150, 70
    height = max(len(items), 1) * row_h + 4
    summary = ", ".join(f"{label}: {value:g}" for label, value in items) or "nothing recorded"
    rows = []
    for index, (label, value) in enumerate(items):
        y = index * row_h + 4
        bar_w = (width - label_w - value_w) * (value / peak)
        rows.append(
            f'<text x="0" y="{y + 13}">{_esc(label)}</text>'
            f'<rect x="{label_w}" y="{y}" width="{bar_w:.1f}" height="16" fill="#7a9e7e"></rect>'
            f'<text x="{label_w + bar_w + 6:.1f}" y="{y + 13}">{value:g}</text>')
    return (f'<figure><figcaption>{_esc(title)}</figcaption>'
            f'<svg role="img" aria-label="{_esc(title)}: {_esc(summary)}" '
            f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
            f'{"".join(rows)}</svg></figure>')


def _selection_text(entry):
    """A per-print filament selection as one line of text."""
    selection = entry.get("filament_selection")
    if isinstance(selection, dict):
        bits = [f"slot {selection['slot']}"] if selection.get("slot") else []
        for key in ("color", "material"):
            if selection.get(key):
                bits.append(str(selection[key]))
        if selection.get("spoolman_id"):
            bits.append(f"spool {selection['spoolman_id']}")
        return ", ".join(bits) or "recorded"
    if isinstance(selection, list):
        return "; ".join(str(item) for item in selection)
    return "recorded"


def render_data_page(data, payload, *, theme=shell.DEFAULT_THEME):
    """GET /data as HTML, from a :func:`data_view` success payload."""
    stats = payload
    parts = [_cards([
        ("Prints", stats["total_prints"]),
        ("Hours printing", f"{stats['total_hours']:g}"),
        ("Filament used", f"{stats['total_filament_used_kg']:g} kg"),
        ("Success rate", _rate(stats["success_rate"])),
    ])]
    parts.append(
        f"<p>Completed {stats['completed']} · failed {stats['failed']} · "
        f"canceled {stats['canceled']} · in progress {stats['in_progress']}. "
        f"{_esc(stats.get('success_rate_note') or '')}</p>")
    if stats.get("year") is not None and stats.get("undated_prints"):
        parts.append(f'<p class="note">{_undated_text(stats["undated_prints"])}</p>')
    if stats.get("totals_caveat"):
        parts.append(
            f"<p>{_esc(stats['totals_caveat'])} Hours cover "
            f"{stats.get('duration_records', 0)} prints; filament covers "
            f"{stats.get('filament_records', 0)} prints.</p>")
    if stats.get("derived_filament_records"):
        parts.append(
            f"<p>DERIVED filament: {stats['derived_filament_g']:g} g recovered "
            f"from filament length for {stats['derived_filament_records']} "
            "prints that recorded millimeters but no grams. Derived grams "
            "are not included in the filament total above.</p>")
    if stats.get("longest_print"):
        longest = stats["longest_print"]
        parts.append(f"<p>Longest print: {_esc(longest.get('title') or 'untitled')} "
                     f"({(longest.get('seconds') or 0) / 3600:.2f} h). "
                     f"Average print: "
                     f"{(stats['average_print_seconds'] or 0) / 60:.0f} min.</p>")
    failures = stats["failures"]
    stop = ", ".join(f"{_esc(k)}: {v}" for k, v in failures["stop_reasons"].items()) or "none recorded"
    parts.append(f"<h2>Failures and risk flags</h2><p>Failed: {failures['failed']}. "
                 f"Risk-flagged prints: {failures['risk_flagged']}. "
                 f"Stop reasons: {stop}.</p>")
    outcomes = stats["outcomes"]
    if outcomes.get("pasta_state") == "recorded":
        pasta_text = (
            f"Pasta: {outcomes['pasta']} prints ({outcomes['pasta_events']} "
            "spaghetti events). Pasta counts from when watchdog event "
            f"recording began ({_esc(outcomes.get('pasta_recording_started_at') or 'unknown')}); "
            "events from before recording began were never recorded and "
            "cannot be counted. ")
        worst_spaghetti = stats.get("worst_spaghetti")
        if worst_spaghetti:
            worst_name = print_stats.display_title(
                worst_spaghetti.get("title"), worst_spaghetti.get("job_key")) or "one print"
            pasta_text += (
                f"Worst spaghetti: {_esc(worst_name)} — streak "
                f"{worst_spaghetti['streak']}, {worst_spaghetti['events']} events. ")
    else:
        pasta_text = (
            "Pasta: not recorded yet — the camera watchdog's detections are not "
            "persisted, so no pasta count exists until event recording begins. ")
    parts.append(
        "<h2>Outcomes</h2><p>"
        f"Failed: {outcomes['failed']}. "
        f"Stopped: {outcomes['stopped']}. "
        + pasta_text +
        "These three numbers are always shown separately, never merged. "
        f"{_esc(outcomes['stopped_note'])}</p>")
    if stats.get("per_color_state") == "recorded":
        color_rows = "".join(
            f"<tr><td>{_esc(color)}</td><td>{grams:g} g</td></tr>"
            for color, grams in stats["per_color_grams"].items())
        parts.append(
            "<h2>Filament per color (per-tool split)</h2>"
            "<p>Per-color grams from each print's per-tool filament split "
            "(recorded at slice time, or recovered from the archived job's "
            "G-code header). Shown only where that data exists.</p>"
            f"<div class=\"table-scroll\"><table><tr><th>color</th>"
            f"<th>filament</th></tr>{color_rows}</table></div>")
    enrich = stats.get("enrichments") or {}
    if enrich:
        almost = enrich.get("almost_made") or {}
        selection_rows = "".join(
            f"<tr><td>{number}</td><td>{_esc(_selection_text(entry))}</td></tr>"
            for number, entry in sorted((enrich.get("per_print") or {}).items())
            if entry.get("filament_selection"))
        parts.append(
            f"<h2>Intake joins</h2><p>Support waivers: "
            f"{enrich.get('support_waivers', 0)} (receipts that waived a "
            "support check and went on to print). Prints you almost made: "
            f"{almost.get('count', 0)}"
            + (f" — {_esc(', '.join(almost.get('titles') or []))}"
               if almost.get("titles") else "")
            + ". Receipts carry no creation date, so the almost-made list "
            "is not split by year.</p>"
            + (f"<div class=\"table-scroll\"><table><tr><th>print</th>"
               f"<th>filament selection</th></tr>{selection_rows}</table></div>"
               if selection_rows else ""))
    maintenance = payload.get("maintenance")
    if maintenance and maintenance.get("state") == "ok":
        kinds = ", ".join(f"{_esc(k)}: {v}" for k, v in
                          sorted((maintenance.get("by_kind") or {}).items()))
        parts.append(
            f"<h2>Maintenance</h2><p>Sessions: {maintenance['sessions']}"
            + (f" ({kinds})" if kinds else "")
            + f". Problems: {maintenance['problems']}. Cold pulls: "
            f"{maintenance['cold_pulls']}.</p>")
    lifetime = payload.get("spoolman_lifetime")
    if lifetime:
        if lifetime.get("state") == "ok":
            spool_rows = "".join(
                f"<tr><td>{_esc(row['label'])}</td><td>{row['used_g']:g} g</td>"
                f"<td>{'archived' if row['archived'] else 'active'}</td></tr>"
                for row in lifetime["spools"] if row.get("used_g") is not None)
            parts.append(
                "<h2>Spoolman lifetime (all-time)</h2><p>All-time per-spool "
                "usage (initial minus remaining, archived spools included) "
                "— not a per-year number.</p>"
                + (f"<div class=\"table-scroll\"><table><tr><th>spool</th>"
                   f"<th>used</th><th>state</th></tr>{spool_rows}</table></div>"
                   if spool_rows else "<p>(no spool has both weights recorded)</p>"))
        else:
            parts.append(
                "<h2>Spoolman lifetime (all-time)</h2><p>Spoolman lifetime: "
                "unavailable — Spoolman did not answer, so no lifetime "
                "numbers are shown.</p>")
    repeats = stats["repeats"]
    repeat_rows = "".join(
        f"<tr><td>{_esc(entry['title'])}</td><td>{entry['count']}</td>"
        f"<td>{entry['total_hours']:g} h</td><td>{entry['total_filament_g']:g} g</td></tr>"
        for entry in repeats["most_reprinted"])
    parts.append(
        f"<h2>Repeats</h2><p>Distinct prints: {repeats['distinct_prints']}. "
        f"Reprints (prints beyond the first of the same print): {repeats['reprint_count']}. "
        "A print counts as the same print only when both its title and its "
        "G-code file name match exactly.</p>"
        + (f"<div class=\"table-scroll\"><table><tr><th>print</th><th>times printed</th>"
           f"<th>time</th><th>filament</th></tr>{repeat_rows}</table></div>"
           if repeat_rows else "<p>(no print was repeated)</p>"))
    hour_rows = "".join(f"<tr><td>{_esc(hour)}:00</td><td>{count}</td></tr>"
                        for hour, count in stats["by_hour"].items())
    weekday = ", ".join(f"{_esc(day)}: {count}"
                        for day, count in stats["by_weekday"].items()) or "none recorded"
    parts.append(
        f"<h2>Time of prints</h2><p>By weekday: {weekday}. "
        "Hours and weekdays are in the configured display timezone.</p>"
        + (f"<div class=\"table-scroll\"><table><tr><th>hour</th><th>prints</th></tr>"
           f"{hour_rows}</table></div>" if hour_rows else "<p>(nothing recorded)</p>"))
    for title, key in (("Printer", "by_printer"), ("Printer profile", "by_printer_profile"),
                       ("Material", "by_material"), ("Color", "by_color"),
                       ("Vendor", "by_vendor"), ("Spool", "by_spool"), ("Nozzle", "by_nozzle")):
        parts.append(_breakdown_table(title, stats["breakdowns"][key]))
    monthly = "".join(f"<tr><td>{_esc(month)}</td><td>{count}</td></tr>"
                      for month, count in stats["monthly"].items())
    parts.append(f"<h2>Activity by month</h2><table><tr><th>month</th><th>prints</th></tr>"
                 f"{monthly}</table>" if monthly else "<h2>Activity by month</h2><p>(nothing recorded)</p>")
    charts = ["<h2>Graphs</h2>"]
    charts.append(_bar_chart("Prints per month", list(stats["monthly"].items())))
    charts.append(_bar_chart(
        "Filament per material (g)",
        [(label, round(row["filament_used_g"], 1))
         for label, row in stats["breakdowns"]["by_material"].items()]))
    outcome_items = [
        ("Completed", stats["completed"]), ("Failed", stats["failed"]),
        ("Stopped", outcomes["stopped"]), ("In progress", stats["in_progress"]),
        ("Risk-flagged", stats["risk_flagged"])]
    if outcomes.get("pasta_state") == "recorded":
        outcome_items.append(("Pasta", outcomes["pasta"]))
    charts.append(_bar_chart("Outcome split", outcome_items))
    if outcomes.get("pasta_state") == "recorded":
        charts.append(f"<p>Pasta: {outcomes['pasta']} prints with recorded "
                      "spaghetti events (counted from when watchdog event "
                      "recording began).</p>")
    else:
        charts.append("<p>Pasta: not recorded yet — no pasta segment is drawn, "
                      "because no pasta count exists until watchdog event "
                      "recording begins.</p>")
    charts.append(_bar_chart(
        "Top colors", [(label, row["count"]) for label, row in
                       list(stats["breakdowns"]["by_color"].items())[:5]]))
    charts.append(_bar_chart(
        "Top materials", [(label, row["count"]) for label, row in
                          list(stats["breakdowns"]["by_material"].items())[:5]]))
    charts.append(_bar_chart(
        "Hour of day", [(f"{hour:02d}:00", stats["by_hour"].get(f"{hour:02d}", 0))
                        for hour in range(24)]))
    parts.append("".join(charts))
    for title, rows, key, unit in (("Top prints by duration", stats["top_by_duration"],
                                    "seconds", "s"),
                                   ("Top prints by filament", stats["top_by_filament"],
                                    "filament_used_g", "g")):
        items = "".join(f"<li>{_esc(row.get('title') or 'untitled')}: {row[key]:g} {unit}</li>"
                        for row in rows)
        parts.append(f"<h2>{title}</h2><ul>{items}</ul>" if items
                     else f"<h2>{title}</h2><p>(nothing recorded)</p>")
    if payload.get("skipped_records"):
        named = ", ".join(_esc(name) for name in payload["skipped_records"])
        parts.append(f'<p class="note">Skipped records that could not be read: {named}.</p>')
    return _page(data, "data", "".join(parts), path="/data", theme=theme)


def _share_panel(year, form_token, exports, registry_note, preview=None,
                 video_available=False):
    """The share panel (#981 sharing phase): export form, deploy
    guides, and the local list of what has been shared and where."""
    preview = preview or {}
    titles = preview.get("titles") or []
    printers = preview.get("printers") or []
    parts = [
        "<h2>Share this Wrapped</h2>",
        "<p>Export this year as a self-contained static bundle — the "
        "scrolling Wrapped page and the summary card images, written "
        "locally under the wrapped outputs. Print Handy uploads nothing "
        "and asks for no tokens: hosting is your own free static-host "
        "account. Choose what goes in before you export.</p>",
        f'<form method="post" action="/wrapped/share">'
        f'<input type="hidden" name="_csrf" value="{_esc(form_token)}">'
        f'<input type="hidden" name="year" value="{year}">'
        '<input type="hidden" name="options" value="explicit">'
        '<fieldset><legend>What this export includes</legend>'
        "<p>Always: the year's numbers (prints, hours, kilograms, success "
        "rate, top color and material, busiest month), your printing "
        "personality and the commentary. Nothing else from your records, "
        "settings or files.</p>"
        '<p><label><input type="checkbox" name="include_titles" value="1" checked> '
        "Print titles"
        + (f" — would show: {_esc(', '.join(titles[:8]))}"
           + (f" and {len(titles) - 8} more" if len(titles) > 8 else "")
           if titles else " — none appear this year")
        + "</label></p>"
        '<p><label><input type="checkbox" name="include_printers" value="1" checked> '
        "Printer names"
        + (f" — would show: {_esc(', '.join(printers[:6]))}" if printers
           else " — none appear this year")
        + "</label></p>"
        '<p><label><input type="checkbox" name="include_video" value="1"'
        + ("" if video_available else " disabled")
        + "> The compiled video"
        + ("" if video_available else " (compile it first)")
        + "</label> <strong>The video contains camera footage</strong>: "
        "frames from your printer's camera, which can show the room, the "
        "desk and anyone in view. It is left out unless you tick this, and "
        "you should watch it first.</p>"
        "<p>Leaving titles or printer names out also drops every commentary "
        "line that mentions one.</p></fieldset>"
        '<label>Visibility <select name="visibility">'
        '<option value="link-only" selected>link-only (recommended)</option>'
        '<option value="public">public</option>'
        '<option value="private" disabled>private (not available yet)</option>'
        "</select></label> "
        '<label>Passphrase (private exports) '
        '<input type="password" name="passphrase" disabled></label> '
        '<label>Destination note '
        '<input type="text" name="destination" '
        'placeholder="e.g. my GitHub Pages repo"></label> '
        '<button type="submit">Export share bundle</button></form>',
        "<p><strong>Link-only</strong> puts "
        "the bundle under an unguessable address with a noindex tag — "
        "that is obscurity, not privacy: anyone with the link can open "
        "it. At most "
        f"{share_export.MAX_LINK_ONLY_EXPORTS} link-only exports per year are kept; "
        "older ones are removed. <strong>Public</strong> is public: the plain "
        "bundle, indexable by search engines, so leave out anything you "
        "would not put on a web page. <strong>Private</strong> exports are "
        f"not available yet: {_esc(share_export.PRIVATE_UNAVAILABLE_REASON)}</p>",
        "<h3>Put it on a free static host</h3>"
        "<p><strong>GitHub Pages:</strong> create a repository (or use "
        "an existing one), copy the bundle's files into it so "
        "<code>index.html</code> sits at the repository root (for "
        "link-only, keep the bundle's slug folder as a folder in the "
        "repo and share that folder's address), commit, then turn on "
        "Pages for that branch in the repository settings. "
        "<strong>Cloudflare Pages:</strong> create a Pages project, "
        "upload the bundle folder (or connect the GitHub repo above), "
        "and the same <code>index.html</code> works unchanged at the "
        "project root. Either way the files work exactly as exported — "
        "no build step, no server. Taking a share down is deleting "
        "those files at the host.</p>",
        "<h3>What you have shared</h3>",
    ]
    if registry_note:
        parts.append(f'<p class="note">{_esc(registry_note)}</p>')
    if exports:
        rows = "".join(
            f"<li>{_esc(entry.get('folder') or '')} — {entry.get('year', '')}, "
            f"{_esc(entry.get('visibility') or '')}"
            + (f", to {_esc(entry['destination'])}" if entry.get("destination") else "")
            + f", exported {_esc(entry.get('exported_at') or '')}, "
            f"video {'included' if entry.get('video_included') else 'not included'}"
            + ("" if entry.get("titles_included", True) else ", titles left out")
            + ("" if entry.get("printer_names_included", True) else ", printer names left out")
            + "</li>"
            for entry in exports)
        parts.append(f"<ul>{rows}</ul>")
    else:
        parts.append("<p>(nothing exported yet)</p>")
    return "".join(parts)


def _video_progress(year, job):
    """The compile's progress block, polled until the job ends."""
    steps = int(job.get("steps") or 0)
    step = int(job.get("step") or 0)
    label = ("Waiting to start…" if job.get("state") == "queued" or not steps
             else f"Step {step} of {steps}")
    bar = (f'<progress id="video-progress" max="{steps}" value="{step}"></progress>'
           if steps else '<progress id="video-progress"></progress>')
    return (
        '<div id="video-job" role="status">'
        f"<p>Compiling your {year} video in the background. "
        f'<span id="video-job-label">{_esc(label)}</span></p>{bar}</div>'
        "<script>(function(){var year=" + str(int(year)) + ";"
        "function poll(){fetch('/api/wrapped/video-status?year='+year,"
        "{credentials:'same-origin'}).then(function(r){return r.json();})"
        ".then(function(s){if(s.state==='done'||s.state==='failed'||s.state==='idle')"
        "{location.reload();return;}"
        "var bar=document.getElementById('video-progress'),"
        "label=document.getElementById('video-job-label');"
        "if(s.steps){bar.max=s.steps;bar.value=s.step;"
        "label.textContent='Step '+s.step+' of '+s.steps;}"
        "setTimeout(poll,2000);}).catch(function(){setTimeout(poll,5000);});}"
        "setTimeout(poll,2000);})();</script>")


def render_wrapped_page(data, payload, *, form_token="", notice=None, error=None,
                        theme=shell.DEFAULT_THEME):
    """GET /wrapped as HTML, from a :func:`wrapped_view` success payload."""
    year = payload["year"]
    headline = payload["headline"]
    highlights = payload["highlights"]
    years = payload.get("years_available") or [year]
    options = "".join(
        f'<option value="{y}"{" selected" if y == year else ""}>{y}</option>' for y in years)
    stats = payload.get("stats") or {}
    breakdowns = stats.get("breakdowns") or {}
    # Scroll-first (#981, catduck's verdict in comment 6702782262):
    # the Wrapped page is one continuous scrolling page — hero,
    # volume, colors/materials, time patterns (with charts), repeats,
    # personality, pasta/failure, and the finale share card last.
    # The tappable story is not the lead presentation and no story
    # player is built here; the video is a section of the page.
    parts = [
        f'<section class="wrapped-hero"><p class="eyebrow">Pasta Wrapped</p>'
        f"<h1>Your {year} in printing</h1>"
        f"<p>{print_stats.plural(headline['total_prints'], 'print')}. Scroll the year.</p></section>",
        f'<form method="get" action="/wrapped"><label>Year '
        f'<select name="year">{options}</select></label> '
        '<button type="submit">Show</button></form>']
    undated = headline.get("undated_prints") or 0
    if undated:
        parts.append(f'<p class="note">{_undated_text(undated)}</p>')
    if notice:
        parts.append(f'<p class="notice">{_esc(notice)}</p>')
    if error:
        parts.append(f'<p class="error">{_esc(error)}</p>')
    parts.append("<h2>By the numbers</h2>" + _cards([
        ("Prints", headline["total_prints"]),
        ("Hours printing", f"{headline['total_hours']:g}"),
        ("Filament used", f"{headline['total_filament_used_kg']:g} kg"),
        ("Success rate", _rate(headline["success_rate"])),
    ]))
    color_facts = []
    for label, key in (("Top color", "top_color"), ("Top material", "top_material"),
                       ("Busiest printer (by prints)", "top_printer"), ("Top vendor", "top_vendor")):
        if highlights.get(key):
            color_facts.append(f"<li>{label}: {_esc(highlights[key])}</li>")
    parts.append(f"<h2>Colors and materials</h2><ul>{''.join(color_facts)}</ul>"
                 + _bar_chart("Top colors", [(label, row["count"]) for label, row in
                                             list((breakdowns.get("by_color") or {}).items())[:5]])
                 + _bar_chart("Top materials", [(label, row["count"]) for label, row in
                                                list((breakdowns.get("by_material") or {}).items())[:5]]))
    time_facts = []
    if highlights.get("busiest_month"):
        time_facts.append(f"<li>Busiest month: {_esc(highlights['busiest_month'])} "
                          f"({print_stats.plural(highlights['busiest_month_prints'], 'print')})</li>")
    if highlights.get("peak_weekday"):
        time_facts.append(f"<li>Busiest weekday: {_esc(highlights['peak_weekday'])}</li>")
    if highlights.get("peak_hour") is not None:
        time_facts.append(f"<li>Busiest hour: {highlights['peak_hour']:02d}:00</li>")
    time_facts.append(f"<li>Late-night prints (midnight to 5 a.m. local): "
                      f"{highlights['night_prints']}</li>")
    by_hour = stats.get("by_hour") or {}
    parts.append(f"<h2>Time patterns</h2><ul>{''.join(time_facts)}</ul>"
                 + _bar_chart("Prints per month", list((stats.get("monthly") or {}).items()))
                 + _bar_chart("Hour of day", [(f"{hour:02d}:00", by_hour.get(f"{hour:02d}", 0))
                                              for hour in range(24)]))
    most_reprinted = highlights.get("most_reprinted")
    repeat_text = ("(no print was repeated this year)"
                   if not most_reprinted else
                   f"Most reprinted: {_esc(most_reprinted['title'])} — printed "
                   f"{most_reprinted['count']} times. "
                   f"Reprints this year: {highlights.get('reprint_count', 0)}.")
    parts.append(f"<h2>Repeats</h2><p>{repeat_text}</p>")
    archetype = payload.get("archetype") or highlights.get("archetype") or {}
    if archetype.get("name"):
        parts.append(
            f"<h2>Your printing personality</h2>"
            f"<p><strong>{_esc(archetype['name'])}</strong> — "
            f"{_esc(archetype.get('fact') or '')}</p>")
    if headline.get("pasta_state") == "recorded" or (
            (stats.get("outcomes") or {}).get("pasta_state") == "recorded"):
        outcomes = stats.get("outcomes") or {}
        pasta_section = (
            f"Pasta: {headline.get('pasta')} prints turned into pasta "
            f"({outcomes.get('pasta_events', 0)} spaghetti events), counted "
            "from when watchdog event recording began "
            f"({outcomes.get('pasta_recording_started_at') or 'unknown'}); "
            "events from before recording began were never recorded. ")
        worst_spaghetti = highlights.get("worst_spaghetti")
        if worst_spaghetti:
            worst_name = print_stats.display_title(
                worst_spaghetti.get("title"), worst_spaghetti.get("job_key")) or "one print"
            pasta_section += (
                f"Worst spaghetti: {_esc(worst_name)} — streak "
                f"{worst_spaghetti['streak']}. ")
    else:
        pasta_section = (
            "Pasta: not recorded yet — the camera watchdog's detections are not "
            "persisted, so no pasta count exists until event recording begins. ")
    worst_failure = highlights.get("worst_failure") or {}
    failure_detail = ""
    if worst_failure.get("stop_detail"):
        detail = worst_failure["stop_detail"]
        bits = [f"stop reason: {_esc(detail['stop_reason'])}"
                if detail.get("stop_reason") else "",
                f"captured {_esc(detail['first_captured_at'])}"
                if detail.get("first_captured_at") else "",
                _esc(detail.get("printer_state_message") or "")]
        failure_detail = (" Worst failure: "
                          + _esc(worst_failure.get("title") or "untitled")
                          + " (" + ", ".join(b for b in bits if b) + ").")
    parts.append(
        "<h2>Pasta, failures and stops</h2><p>"
        f"Failed prints: {highlights['failed']}. "
        f"Stopped prints: {headline.get('stopped', headline.get('canceled', 0))}. "
        + pasta_section
        + f"Risk-flagged prints: {highlights['risk_flagged']}. "
        f"Worst failure run: {highlights['worst_failure_streak']}."
        f"{failure_detail}</p>")
    if payload.get("longest_print_clip"):
        parts.append(f"<p>Your longest print has a timelapse clip by name: "
                     f"{_esc(payload['longest_print_clip'])}.</p>")
    almost = ((stats.get("enrichments") or {}).get("almost_made") or {})
    if almost.get("count"):
        parts.append(
            f"<h2>Prints you almost made</h2><p>{almost['count']} sliced "
            "intakes never started"
            + (f": {_esc(', '.join(almost.get('titles') or []))}"
               if almost.get("titles") else "")
            + ". From intake receipts on file; receipts carry no creation "
            "date, so this list is not split by year.</p>")
    per_color = stats.get("per_color_grams") or {}
    if per_color:
        color_bits = ", ".join(f"{_esc(color)}: {grams:g} g"
                               for color, grams in list(per_color.items())[:5])
        parts.append(f"<h2>Filament per color</h2><p>{color_bits}. "
                     "From each print's per-tool filament split, where "
                     "recorded.</p>")
    lines = "".join(f'<p class="card">{_esc(line)}</p>' for line in payload["script"])
    parts.append(f"<h2>Commentary</h2>{lines}")
    job = payload.get("video_job") or {}
    compiling = job.get("state") in ("queued", "running")
    if payload.get("ai_video_enabled"):
        parts.append("<h2>Make my Wrapped video</h2>")
        if compiling:
            parts.append(_video_progress(year, job))
        else:
            if job.get("state") == "failed":
                parts.append(
                    '<p class="error">The last video compile failed: '
                    f"{_esc(job.get('reason') or 'unknown reason')}</p>")
            parts.append(
                f'<form method="post" action="/wrapped/video">'
                f'<input type="hidden" name="_csrf" value="{_esc(form_token)}">'
                f'<input type="hidden" name="year" value="{year}">'
                '<label><input type="checkbox" name="export_silent" value="1"> '
                "Export silent (no music, no narration — add music in the "
                "app you post to)</label> "
                '<button type="submit">Make my Wrapped video</button></form>')
        parts.append(
            "<p>Compiling runs on this machine, in the background, on demand, "
            "from stat cards, this commentary as on-screen text, and the "
            "year's timelapses where they are found. You can leave this page "
            "while it works. The commentary is the local lines above, "
            "never rewritten by an AI model, and nothing leaves this "
            "machine. Narration: "
            "with [timelapse] wrapped_tts_engine set to piper, the slide "
            "lines are spoken by the local Piper binary and nothing is "
            "sent anywhere; with none (the default) there is no spoken "
            "narration.</p>")
    else:
        parts.append("<h2>Wrapped video</h2><p>The video step is off: set "
                     "[timelapse] wrapped_video = true to turn on the "
                     "on-demand video compile.</p>")
    if payload.get("video_available"):
        # The video is an embed inside the scrolling page (#981
        # comment 6702782262), served by GET /wrapped/video-file.
        parts.append(
            "<h2>Your Wrapped video</h2>"
            f'<video controls preload="metadata" '
            f'src="/wrapped/video-file?year={year}"></video>')
    parts.append(_share_panel(year, form_token, payload.get("share_exports") or [],
                              payload.get("share_registry_note"),
                              payload.get("share_preview"),
                              bool(payload.get("video_available"))))
    # Finale: the share card is the last section of the scroll.
    if archetype.get("name"):
        share_top = _esc(highlights.get("top_color") or "unknown")
        parts.append(
            '<section class="card share-card"><p class="eyebrow">'
            f"Pasta Wrapped {year}</p>"
            f"<h2>{_esc(archetype['name'])}</h2>"
            f"<p>{print_stats.plural(headline['total_prints'], 'print')} · "
            f"{print_stats.hours_text(headline['total_hours'])} · "
            f"{headline['total_filament_used_kg']:g} kg · "
            f"success rate {_rate(headline['success_rate'])} · "
            f"top color {share_top}</p></section>")
    if payload.get("skipped_records"):
        named = ", ".join(_esc(name) for name in payload["skipped_records"])
        parts.append(f'<p class="note">Skipped records that could not be read: {named}.</p>')
    return _page(data, "wrapped", "".join(parts), path="/wrapped", theme=theme)


def render_share_result(data, payload, *, theme=shell.DEFAULT_THEME):
    """The POST /wrapped/share result page: where the bundle landed."""
    manifest = payload.get("manifest") or {}
    notes = "".join(f"<li>{_esc(note)}</li>" for note in (manifest.get("notes") or []))
    files = "".join(f"<li>{_esc(entry.get('name') or '')} "
                    f"({_esc(entry.get('kind') or '')})</li>"
                    for entry in (manifest.get("files") or []))
    body = (f"<p>Your Wrapped {payload.get('year')} share bundle is ready: "
            f"<code>{_esc(payload.get('path') or '')}</code></p>"
            f"<p>Visibility: {_esc(payload.get('visibility') or '')}. "
            f"Video: {'included' if payload.get('video_included') else 'not included'}. "
            "Nothing was uploaded — hosting the bundle on your own static "
            "host is the next step (the Wrapped page's Share panel has "
            "the GitHub Pages and Cloudflare Pages steps).</p>"
            + (f"<h2>Files</h2><ul>{files}</ul>" if files else "")
            + (f"<h2>Notes</h2><ul>{notes}</ul>" if notes else "")
            + (f'<p class="note">{_esc(payload["registry_note"])}</p>'
               if payload.get("registry_note") else "")
            + '<p><a href="/wrapped">Back to Wrapped</a></p>')
    return _page(data, "wrapped share", body, path="/wrapped", theme=theme)


__all__ = ["DATA_OFF_REASON", "VIDEO_OFF_REASON",
           "WRAPPED_OFF_REASON", "ai_video_enabled", "data_enabled", "data_view",
           "export_share", "make_video", "nav_for", "parse_byte_range", "render_data_page",
           "render_share_result", "render_wrapped_page", "video_status",
           "wrapped_enabled", "wrapped_video_path", "wrapped_view"]
