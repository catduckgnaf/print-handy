"""Web UI Data tab and Print Handy Wrapped pages (#956).

``GET /data`` and ``GET /wrapped`` are the pages; ``GET /api/data``
and ``GET /api/wrapped`` are the same payloads as JSON;
``POST /wrapped/video`` compiles the Wrapped video.

The whole surface is opt-in under ``[timelapse] wrapped_enabled``
(default false). While it is off, every route here answers 403 with
:data:`OFF_REASON` naming the setting, and the navigation carries no
Data/Wrapped links (the pages below thread the extended nav only when
the feature is on). The video step additionally needs
``[timelapse] wrapped_ai_video`` (default false); while that is off
the Wrapped page says so by name and the video route refuses by name.

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

from ..core import print_stats
from . import shell
from . import wrapped_video as video_module

OFF_REASON = ("the Data tab and Print Handy Wrapped are off: set "
              "[timelapse] wrapped_enabled = true to allow them. Nothing shown.")
VIDEO_OFF_REASON = ("the Wrapped video is off: set [timelapse] wrapped_ai_video = true "
                    "(with wrapped_enabled on) to allow it. Nothing was compiled.")
EXTRA_NAV = shell.NAV[:-1] + (("/data", "Data"), ("/wrapped", "Wrapped")) + shell.NAV[-1:]


def wrapped_enabled(data) -> bool:
    return bool(((data or {}).get("timelapse") or {}).get("wrapped_enabled", False))


def ai_video_enabled(data) -> bool:
    return bool(((data or {}).get("timelapse") or {}).get("wrapped_ai_video", False))


def nav_for(data):
    """The shell nav for a settings mapping: extended only when on."""
    return EXTRA_NAV if wrapped_enabled(data) else shell.NAV


def _vault(data, records_vault):
    if records_vault is not None:
        return Path(records_vault)
    return Path(((data or {}).get("paths") or {}).get("records_vault") or ".")


def _load(data, records_vault):
    return print_stats.load_records(_vault(data, records_vault))


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
    if not wrapped_enabled(data):
        return 403, {"ok": False, "reason": OFF_REASON}
    records, skipped = _load(data, records_vault)
    try:
        stats = print_stats.aggregate(records, year=year, tz=_display_tz(data))
    except (TypeError, ValueError):
        return 400, {"ok": False, "reason": "year must be a whole number. Nothing shown."}
    return 200, {"ok": True, **stats, "skipped_records": skipped}


def wrapped_view(data, year=None, *, records_vault=None):
    """(code, body) for GET /api/wrapped. The default year is the latest recorded."""
    if not wrapped_enabled(data):
        return 403, {"ok": False, "reason": OFF_REASON}
    records, skipped = _load(data, records_vault)
    tz = _display_tz(data)
    years = print_stats.aggregate(records, tz=tz)["years_available"]
    if year is None:
        year = years[-1] if years else _current_year(tz)
    try:
        payload = print_stats.build_wrapped(records, int(year), tz=tz)
    except (TypeError, ValueError):
        return 400, {"ok": False, "reason": "year must be a whole number. Nothing shown."}
    # #981: clip counts are a finder result, not a record fact, so they
    # live here at the Web UI layer (where the finder runs) under their
    # own name, not inside the record aggregate.
    try:
        clips_found = len(video_module.default_clip_finder(data, int(year)))
    except Exception:
        clips_found = 0
    return 200, {"ok": True, **payload, "skipped_records": skipped,
                 "timelapse_clips_found": clips_found,
                 "ai_video_enabled": ai_video_enabled(data)}


def _current_year(tz=None):
    from datetime import datetime, timezone
    return datetime.now(tz or timezone.utc).year


def make_video(data, year, *, records_vault=None, out_dir=None, **video_kwargs):
    """(code, body) for POST /wrapped/video: the on-demand compile."""
    if not wrapped_enabled(data):
        return 403, {"ok": False, "reason": OFF_REASON}
    if not ai_video_enabled(data):
        return 403, {"ok": False, "reason": VIDEO_OFF_REASON}
    records, _skipped = _load(data, records_vault)
    try:
        wrapped = print_stats.build_wrapped(records, int(year), tz=_display_tz(data))
    except (TypeError, ValueError):
        return 400, {"ok": False, "reason": "year must be a whole number. Nothing was compiled."}
    target_dir = Path(out_dir) if out_dir is not None else _vault(data, records_vault) / "wrapped"
    result = video_module.build_wrapped_video(
        int(year), wrapped, target_dir, data=data, **video_kwargs)
    if not result.get("ok"):
        return 502, {"ok": False, "reason": result.get("reason") or "the video compile failed",
                     "script_path": result.get("script_path"), "notes": result.get("notes") or []}
    return 200, {"ok": True, "year": int(year), "path": result["path"],
                 "script_path": result["script_path"], "notes": result.get("notes") or []}


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
    for title, key in (("Printer", "by_printer"), ("Printer profile", "by_printer_profile"),
                       ("Material", "by_material"), ("Color", "by_color"),
                       ("Vendor", "by_vendor"), ("Spool", "by_spool"), ("Nozzle", "by_nozzle")):
        parts.append(_breakdown_table(title, stats["breakdowns"][key]))
    monthly = "".join(f"<tr><td>{_esc(month)}</td><td>{count}</td></tr>"
                      for month, count in stats["monthly"].items())
    parts.append(f"<h2>Activity by month</h2><table><tr><th>month</th><th>prints</th></tr>"
                 f"{monthly}</table>" if monthly else "<h2>Activity by month</h2><p>(nothing recorded)</p>")
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


def render_wrapped_page(data, payload, *, form_token="", notice=None, error=None,
                        theme=shell.DEFAULT_THEME):
    """GET /wrapped as HTML, from a :func:`wrapped_view` success payload."""
    year = payload["year"]
    headline = payload["headline"]
    highlights = payload["highlights"]
    years = payload.get("years_available") or [year]
    options = "".join(
        f'<option value="{y}"{" selected" if y == year else ""}>{y}</option>' for y in years)
    parts = [f'<form method="get" action="/wrapped"><label>Year '
             f'<select name="year">{options}</select></label> '
             '<button type="submit">Show</button></form>']
    if notice:
        parts.append(f'<p class="notice">{_esc(notice)}</p>')
    if error:
        parts.append(f'<p class="error">{_esc(error)}</p>')
    parts.append(_cards([
        ("Prints", headline["total_prints"]),
        ("Hours printing", f"{headline['total_hours']:g}"),
        ("Filament used", f"{headline['total_filament_used_kg']:g} kg"),
        ("Success rate", _rate(headline["success_rate"])),
    ]))
    facts = []
    for label, key in (("Top color", "top_color"), ("Top material", "top_material"),
                       ("Top printer", "top_printer"), ("Top vendor", "top_vendor"),
                       ("Busiest month", "busiest_month")):
        if highlights.get(key):
            facts.append(f"<li>{label}: {_esc(highlights[key])}</li>")
    facts.append(f"<li>Risk-flagged prints: {highlights['risk_flagged']}</li>")
    facts.append(f"<li>Failed prints: {highlights['failed']}</li>")
    facts.append(f"<li>Worst failure run: {highlights['worst_failure_streak']}</li>")
    parts.append(f"<h2>Highlights</h2><ul>{''.join(facts)}</ul>")
    lines = "".join(f'<p class="card">{_esc(line)}</p>' for line in payload["script"])
    parts.append(f"<h2>Commentary</h2>{lines}")
    if payload.get("ai_video_enabled"):
        parts.append(
            "<h2>Make my Wrapped video</h2>"
            f'<form method="post" action="/wrapped/video">'
            f'<input type="hidden" name="_csrf" value="{_esc(form_token)}">'
            f'<input type="hidden" name="year" value="{year}">'
            '<button type="submit" data-busy="Compiling…">Make my Wrapped video</button>'
            "<p>Compiling runs on this machine, on demand, from stat cards, "
            "this commentary as on-screen text, and the year's timelapses "
            "where they are found. The commentary lines above, with their "
            "numbers, are first sent to the configured AI route to be "
            "rewritten; if no route is configured, the local lines above "
            "are used as-is and nothing leaves this machine. There is no "
            "spoken narration.</p></form>")
    else:
        parts.append("<h2>Wrapped video</h2><p>The video step is off: set "
                     "[timelapse] wrapped_ai_video = true to turn on the AI "
                     "commentary and the on-demand video compile.</p>")
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


__all__ = ["EXTRA_NAV", "OFF_REASON", "VIDEO_OFF_REASON", "ai_video_enabled",
           "data_view", "make_video", "nav_for", "render_data_page",
           "render_video_result", "render_wrapped_page", "wrapped_enabled",
           "wrapped_view"]
