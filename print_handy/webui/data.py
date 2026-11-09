"""Web UI Data tab and Print Handy Wrapped pages (#956).

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
additionally needs ``[timelapse] wrapped_ai_video`` (default false);
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
from pathlib import Path

from ..core import print_stats, wrapped_joins
from . import share_export
from . import shell
from . import wrapped_video as video_module

DATA_OFF_REASON = ("the Data tab is off: set "
                   "[timelapse] data_enabled = true to allow it. Nothing shown.")
WRAPPED_OFF_REASON = ("Print Handy Wrapped is off: set "
                      "[timelapse] wrapped_enabled = true to allow it. Nothing shown.")
# The pre-split combined reason, kept for any outside caller.
OFF_REASON = WRAPPED_OFF_REASON
VIDEO_OFF_REASON = ("the Wrapped video is off: set [timelapse] wrapped_ai_video = true "
                    "(with wrapped_enabled on) to allow it. Nothing was compiled.")
EXTRA_NAV = shell.NAV[:-1] + (("/data", "Data"), ("/wrapped", "Wrapped")) + shell.NAV[-1:]


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
    return bool(((data or {}).get("timelapse") or {}).get("wrapped_ai_video", False))


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


def _load(data, records_vault):
    return print_stats.load_records(_vault(data, records_vault))


def _load_events(data, records_vault):
    """(events_or_None, skipped_lines): the watchdog events file.

    None (not an empty list) when no events file exists, so the
    aggregate keeps the honest not_recorded pasta state (#981 v2).
    """
    try:
        events, skipped, exists = print_stats.load_watchdog_events(
            _vault(data, records_vault))
    except Exception:
        return None, []
    return (events if exists else None), skipped


def _load_enrichments(data, records):
    """The receipt/JOB joins, or None when they cannot be built.

    Fail-soft by design: the joins are read-only beside the records,
    and a broken archive or receipts folder must never take the Data
    tab down with it. Receipts live where model_job reads them
    (``PRINT_HANDY_INTAKE_RECEIPTS`` or ``<state>/model-link-intakes``).
    """
    try:
        import os
        from .. import settings as _settings
        archive_dir = ((data or {}).get("paths") or {}).get("archive")
        receipts_dir = os.environ.get("PRINT_HANDY_INTAKE_RECEIPTS") or str(
            _settings.state_dir(data) / "model-link-intakes")
        return wrapped_joins.build_enrichments(
            records, archive_dir=archive_dir, receipts_dir=receipts_dir)
    except Exception:
        return None


def _spoolman_lifetime(data):
    """The all-time Spoolman block (never per-year), fail-soft."""
    try:
        from ..core import spoolman_client
        base = str(((data or {}).get("services") or {}).get("spoolman_url") or "").strip()
        spools = spoolman_client.list_spools(
            base_url=base or None, allow_archived=True)
    except Exception:
        spools = None
    return wrapped_joins.spoolman_lifetime(spools)


def _maintenance(data, records_vault, year, tz):
    try:
        return wrapped_joins.maintenance_block(
            _vault(data, records_vault), year=year, tz=tz)
    except Exception:
        return {"state": "absent", "sessions": 0, "by_kind": {},
                "problems": 0, "cold_pulls": 0, "skipped_lines": []}


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


def data_view(data, year=None, *, records_vault=None):
    """(code, body) for GET /api/data."""
    if not data_enabled(data):
        return 403, {"ok": False, "reason": DATA_OFF_REASON}
    records, skipped = _load(data, records_vault)
    events, skipped_events = _load_events(data, records_vault)
    tz = _display_tz(data)
    try:
        stats = print_stats.aggregate(
            records, year=year, tz=tz, events=events,
            enrichments=_load_enrichments(data, records))
        year_value = int(year) if year is not None else None
    except (TypeError, ValueError):
        return 400, {"ok": False, "reason": "year must be a whole number. Nothing shown."}
    return 200, {"ok": True, **stats, "skipped_records": skipped,
                 "skipped_event_lines": skipped_events,
                 "maintenance": _maintenance(data, records_vault, year_value, tz),
                 "spoolman_lifetime": _spoolman_lifetime(data)}


def wrapped_view(data, year=None, *, records_vault=None):
    """(code, body) for GET /api/wrapped. The default year is the latest recorded."""
    if not wrapped_enabled(data):
        return 403, {"ok": False, "reason": WRAPPED_OFF_REASON}
    records, skipped = _load(data, records_vault)
    events, skipped_events = _load_events(data, records_vault)
    tz = _display_tz(data)
    years = print_stats.aggregate(records, tz=tz)["years_available"]
    if year is None:
        year = years[-1] if years else _current_year(tz)
    try:
        payload = print_stats.build_wrapped(
            records, int(year), tz=tz, events=events,
            enrichments=_load_enrichments(data, records))
    except (TypeError, ValueError):
        return 400, {"ok": False, "reason": "year must be a whole number. Nothing shown."}
    # #981: clip counts are a finder result, not a record fact, so they
    # live here at the Web UI layer (where the finder runs) under their
    # own name, not inside the record aggregate. V2: the detailed
    # finder also joins clips to print numbers by delivery name.
    try:
        detailed = video_module.default_clip_finder_detailed(
            data, int(year), records=records, tz=tz)
    except Exception:
        detailed = []
    clips_by_print = [{"print_number": entry["print_number"],
                       "name": Path(entry["path"]).name}
                      for entry in detailed if entry.get("print_number")]
    longest = (payload.get("highlights") or {}).get("longest_print") or {}
    longest_clip = next((c["name"] for c in clips_by_print
                         if c["print_number"] == longest.get("print_number")), None)
    # #981 sharing: the share panel lists past exports from the local
    # registry beside the wrapped outputs; a corrupt registry fails
    # soft into a named note here, never a broken page.
    share_entries, share_note = share_export.load_exports(
        _vault(data, records_vault) / "wrapped")
    return 200, {"ok": True, **payload, "skipped_records": skipped,
                 "share_exports": share_entries,
                 "share_registry_note": share_note,
                 "skipped_event_lines": skipped_events,
                 "timelapse_clips_found": len(detailed),
                 "timelapse_clips_by_print": clips_by_print,
                 "longest_print_clip": longest_clip,
                 "maintenance": _maintenance(data, records_vault, int(year), tz),
                 "video_available": wrapped_video_path(data, int(year),
                                                       records_vault=records_vault) is not None,
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


def _current_year(tz=None):
    from datetime import datetime, timezone
    return datetime.now(tz or timezone.utc).year


def make_video(data, year, *, records_vault=None, out_dir=None, export_silent=False,
               **video_kwargs):
    """(code, body) for POST /wrapped/video: the on-demand compile."""
    if not wrapped_enabled(data):
        return 403, {"ok": False, "reason": WRAPPED_OFF_REASON}
    if not ai_video_enabled(data):
        return 403, {"ok": False, "reason": VIDEO_OFF_REASON}
    records, _skipped = _load(data, records_vault)
    events, _skipped_events = _load_events(data, records_vault)
    try:
        wrapped = print_stats.build_wrapped(
            records, int(year), tz=_display_tz(data), events=events,
            enrichments=_load_enrichments(data, records))
    except (TypeError, ValueError):
        return 400, {"ok": False, "reason": "year must be a whole number. Nothing was compiled."}
    target_dir = Path(out_dir) if out_dir is not None else _vault(data, records_vault) / "wrapped"
    video_kwargs.setdefault("records", records)
    video_kwargs.setdefault("tz", _display_tz(data))
    result = video_module.build_wrapped_video(
        int(year), wrapped, target_dir, data=data, export_silent=export_silent,
        **video_kwargs)
    if not result.get("ok"):
        return 502, {"ok": False, "reason": result.get("reason") or "the video compile failed",
                     "script_path": result.get("script_path"), "notes": result.get("notes") or []}
    return 200, {"ok": True, "year": int(year), "path": result["path"],
                 "script_path": result["script_path"], "notes": result.get("notes") or []}


def export_share(data, year, visibility, *, records_vault=None, out_dir=None,
                 destination_note="", passphrase=None):
    """(code, body) for POST /wrapped/share: one explicit export.

    Behind the same ``wrapped_enabled`` gate as every other Wrapped
    surface. The bundle is written locally under the wrapped outputs
    (``<vault>/wrapped/exports/``); nothing is uploaded anywhere.
    Private exports refuse by name inside share_export until an
    established AES-GCM library is a dependency.
    """
    if not wrapped_enabled(data):
        return 403, {"ok": False, "reason": WRAPPED_OFF_REASON}
    records, _skipped = _load(data, records_vault)
    events, _skipped_events = _load_events(data, records_vault)
    try:
        wrapped = print_stats.build_wrapped(
            records, int(year), tz=_display_tz(data), events=events,
            enrichments=_load_enrichments(data, records))
    except (TypeError, ValueError):
        return 400, {"ok": False, "reason": "year must be a whole number. Nothing was exported."}
    target_dir = Path(out_dir) if out_dir is not None else _vault(data, records_vault) / "wrapped"
    result = share_export.export_bundle(
        wrapped, target_dir, visibility=str(visibility or "public"),
        destination_note=destination_note, passphrase=passphrase)
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
        f"canceled {stats['canceled']} · in progress {stats['in_progress']}.</p>")
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
            worst_name = (worst_spaghetti.get("title")
                          or worst_spaghetti.get("job_key") or "one print")
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


def _share_panel(year, form_token, exports, registry_note):
    """The share panel (#981 sharing phase): export form, deploy
    guides, and the local list of what has been shared and where."""
    parts = [
        "<h2>Share this Wrapped</h2>",
        "<p>Export this year as a self-contained static bundle — the "
        "scrolling Wrapped page, the summary card images, and the "
        "year's compiled video when one exists — written locally under "
        "the wrapped outputs. Print Handy uploads nothing and asks for "
        "no tokens: hosting is your own free static-host account.</p>",
        f'<form method="post" action="/wrapped/share">'
        f'<input type="hidden" name="_csrf" value="{_esc(form_token)}">'
        f'<input type="hidden" name="year" value="{year}">'
        '<label>Visibility <select name="visibility">'
        '<option value="public">public</option>'
        '<option value="link-only">link-only</option>'
        '<option value="private" disabled>private (not available yet)</option>'
        "</select></label> "
        '<label>Passphrase (private exports) '
        '<input type="password" name="passphrase" disabled></label> '
        '<label>Destination note '
        '<input type="text" name="destination" '
        'placeholder="e.g. my GitHub Pages repo"></label> '
        '<button type="submit">Export share bundle</button></form>',
        "<p><strong>Public</strong> is public: the plain bundle, "
        "indexable by search engines. <strong>Link-only</strong> puts "
        "the bundle under an unguessable address with a noindex tag — "
        "that is obscurity, not privacy: anyone with the link can open "
        "it. <strong>Private</strong> exports are not available yet: "
        f"{_esc(share_export.PRIVATE_UNAVAILABLE_REASON)}</p>",
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
            f"video {'included' if entry.get('video_included') else 'not included'}</li>"
            for entry in exports)
        parts.append(f"<ul>{rows}</ul>")
    else:
        parts.append("<p>(nothing exported yet)</p>")
    return "".join(parts)


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
        f'<section class="wrapped-hero"><p class="eyebrow">Print Handy Wrapped</p>'
        f"<h1>Your {year} in printing</h1>"
        f"<p>{headline['total_prints']} prints. Scroll the year.</p></section>",
        f'<form method="get" action="/wrapped"><label>Year '
        f'<select name="year">{options}</select></label> '
        '<button type="submit">Show</button></form>']
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
                       ("Top printer", "top_printer"), ("Top vendor", "top_vendor")):
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
                          f"({highlights['busiest_month_prints']} prints)</li>")
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
            worst_name = (worst_spaghetti.get("title")
                          or worst_spaghetti.get("job_key") or "one print")
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
    if payload.get("ai_video_enabled"):
        parts.append(
            "<h2>Make my Wrapped video</h2>"
            f'<form method="post" action="/wrapped/video">'
            f'<input type="hidden" name="_csrf" value="{_esc(form_token)}">'
            f'<input type="hidden" name="year" value="{year}">'
            '<label><input type="checkbox" name="export_silent" value="1"> '
            "Export silent (no music, no narration — add music in the "
            "app you post to)</label> "
            '<button type="submit" data-busy="Compiling…">Make my Wrapped video</button>'
            "<p>Compiling runs on this machine, on demand, from stat cards, "
            "this commentary as on-screen text, and the year's timelapses "
            "where they are found. The commentary is the local lines above, "
            "never rewritten by an AI model, and nothing leaves this "
            "machine. Narration: "
            "with [timelapse] wrapped_tts_engine set to piper, the slide "
            "lines are spoken by the local Piper binary and nothing is "
            "sent anywhere; with none (the default) there is no spoken "
            "narration.</p></form>")
    else:
        parts.append("<h2>Wrapped video</h2><p>The video step is off: set "
                     "[timelapse] wrapped_ai_video = true to turn on the "
                     "on-demand video compile.</p>")
    if payload.get("video_available"):
        # The video is an embed inside the scrolling page (#981
        # comment 6702782262), served by GET /wrapped/video-file.
        parts.append(
            "<h2>Your Wrapped video</h2>"
            f'<video controls preload="metadata" '
            f'src="/wrapped/video-file?year={year}"></video>')
    parts.append(_share_panel(year, form_token, payload.get("share_exports") or [],
                              payload.get("share_registry_note")))
    # Finale: the share card is the last section of the scroll.
    if archetype.get("name"):
        share_top = _esc(highlights.get("top_color") or "unknown")
        parts.append(
            '<section class="card share-card"><p class="eyebrow">'
            f"Print Handy Wrapped {year}</p>"
            f"<h2>{_esc(archetype['name'])}</h2>"
            f"<p>{headline['total_prints']} prints · "
            f"{headline['total_hours']:g} hours · "
            f"{headline['total_filament_used_kg']:g} kg · "
            f"success rate {_rate(headline['success_rate'])} · "
            f"top color {share_top}</p></section>")
    if payload.get("skipped_records"):
        named = ", ".join(_esc(name) for name in payload["skipped_records"])
        parts.append(f'<p class="note">Skipped records that could not be read: {named}.</p>')
    return _page(data, "wrapped", "".join(parts), path="/wrapped", theme=theme)


def render_video_result(data, payload, *, theme=shell.DEFAULT_THEME):
    """The POST /wrapped/video result page: where the video landed, and its notes."""
    notes = "".join(f"<li>{_esc(note)}</li>" for note in (payload.get("notes") or []))
    body = (f"<p>Your Wrapped video is ready: <code>{_esc(payload.get('path') or '')}</code></p>"
            f"<p>Script: <code>{_esc(payload.get('script_path') or '')}</code></p>"
            + (f"<h2>Notes</h2><ul>{notes}</ul>" if notes else "")
            + '<p><a href="/wrapped">Back to Wrapped</a></p>')
    return _page(data, "wrapped video", body, path="/wrapped", theme=theme)


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


__all__ = ["DATA_OFF_REASON", "EXTRA_NAV", "OFF_REASON", "VIDEO_OFF_REASON",
           "WRAPPED_OFF_REASON", "ai_video_enabled", "data_enabled", "data_view",
           "export_share", "make_video", "nav_for", "render_data_page",
           "render_share_result", "render_video_result", "render_wrapped_page",
           "wrapped_enabled", "wrapped_video_path", "wrapped_view"]
