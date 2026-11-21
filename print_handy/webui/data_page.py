"""The everyday sections of the Data tab: how printing is going, in plain words.

:func:`render_sections` turns one ``/api/data`` payload into the page a
maker reads first: the headline numbers, the month-by-month trend, recent
prints with their outcome, each printer's record, what the filament and
money went into, and the failure causes worth acting on. Every number is
formatted for a person (hours and minutes, grams or kilograms, whole
percentages), and every section has an empty state that says what will
appear once there is something to show. The full statistics stay on the
page below these sections, folded away (``data.render_data_page``).

Styles are scoped to ``.data-page`` and ride in the page head, so the
shared stylesheet is untouched.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone
from urllib.parse import urlencode

from ..core import print_stats

UNKNOWN = print_stats.UNKNOWN

CSS = """
.data-page .dp-years { display: flex; flex-wrap: wrap; gap: 0.375rem; margin: 0 0 1rem; }
.data-page .dp-years a {
  padding: 0.3rem 0.75rem; border: 1px solid var(--color-line); border-radius: 999px;
  color: var(--color-ink); text-decoration: none; font-size: 0.875rem;
}
.data-page .dp-years a[aria-current="page"] { background: var(--color-ink); color: var(--color-bg); }
.data-page .dp-stats {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(9.5rem, 1fr));
  gap: 0.75rem; margin: 1rem 0;
}
.data-page .dp-stat {
  background: var(--color-surface); border: 1px solid var(--color-line);
  border-radius: var(--radius-card); padding: 0.75rem 1rem; min-width: 0;
}
.data-page .dp-stat .v { font-size: 1.5rem; font-weight: 600; line-height: 1.2; }
.data-page .dp-stat .s { color: var(--color-muted); font-size: 0.8125rem; margin-top: 0.25rem; }
.data-page .dp-lede { font-size: 1.0625rem; }
.data-page .dp-list { list-style: none; margin: 0.75rem 0; padding: 0; }
.data-page .dp-list > li {
  display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between;
  gap: 0.25rem 0.75rem; padding: 0.625rem 0; border-bottom: 1px solid var(--color-line);
}
.data-page .dp-list > li:last-child { border-bottom: none; }
.data-page .dp-list .t { font-weight: 500; overflow-wrap: anywhere; min-width: 0; }
.data-page .dp-list .m { color: var(--color-muted); font-size: 0.875rem; }
.data-page .dp-list .badge { margin-left: 0; }
.data-page .dp-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr)); gap: 0.75rem;
}
.data-page .dp-grid .card { margin: 0; }
.data-page .dp-bar {
  height: 0.5rem; border-radius: 999px; background: var(--color-line);
  overflow: hidden; margin: 0.375rem 0 0;
}
.data-page .dp-bar > i { display: block; height: 100%; background: var(--color-ok); }
.data-page .dp-trend { display: grid; gap: 0.5rem; margin: 0.75rem 0; }
.data-page .dp-trend .row {
  display: grid; grid-template-columns: 4.5rem 1fr auto; align-items: center; gap: 0.75rem;
  font-size: 0.875rem;
}
.data-page .dp-trend .dp-bar { margin: 0; }
.data-page .dp-empty { text-align: left; }
.data-page details.dp-more { margin: 2rem 0 0; }
.data-page details.dp-more > summary { cursor: pointer; font-weight: 600; padding: 0.5rem 0; }
.data-page svg { max-width: 100%; height: auto; }
.data-page svg text { fill: var(--color-ink); font-size: 12px; }
"""

_OUTCOME_LABELS = {
    "finished": ("Finished", "ok"),
    "failed": ("Failed", "bad"),
    "stopped": ("Stopped", "warn"),
    "printing": ("Printing", ""),
    # A print the reconciliation proved had ended, with no outcome
    # recorded: it is not still printing, and no verdict is invented.
    "unknown": ("Not recorded", "warn"),
}


def _esc(value):
    return html.escape(str(value), quote=True)


# ------------------------------------------------------------- formatting

def duration(seconds):
    """Seconds as a person says them: ``45 min``, ``3 h 20 min``, ``212 h``."""
    if seconds is None:
        return "not recorded"
    minutes = int(round(float(seconds) / 60))
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours >= 100 or not rest:
        return f"{hours} h"
    return f"{hours} h {rest} min"


def weight(grams):
    """Grams as a person says them: ``820 g``, ``4.9 kg``."""
    if grams is None:
        return "not recorded"
    value = float(grams)
    if value >= 1000:
        return f"{value / 1000:.1f} kg" if value < 100000 else f"{value / 1000:.0f} kg"
    return f"{value:.0f} g" if value >= 10 else f"{value:.1f} g".replace(".0 g", " g")


def percent(rate):
    return "no result yet" if rate is None else f"{round(rate * 100):g}%"


def money(amount, currency):
    if amount is None:
        return ""
    code = (currency or "").upper()
    if code == "USD":
        return f"${float(amount):,.2f}"
    return f"{code} {float(amount):,.2f}".strip()


def _zone(payload):
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:  # pragma: no cover - Python 3.9+ always has zoneinfo
        return timezone.utc
    name = payload.get("timezone")
    if not name or name == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def day(value, zone):
    """An ISO instant as ``Nov 14, 2026`` in the display timezone, or ``""``."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(zone)
    return f"{local:%b} {local.day}, {local.year}"


def month_name(key):
    """``2026-11`` as ``Nov 2026``."""
    try:
        return datetime.strptime(key, "%Y-%m").strftime("%b %Y")
    except ValueError:
        return key


def _name(label, fallback="Not recorded"):
    return fallback if label in (None, "", UNKNOWN) else str(label)


# --------------------------------------------------------------- sections

def year_switcher(payload):
    """Links to all time and each recorded year, the current choice marked."""
    years = payload.get("years_available") or []
    if not years:
        return ""
    current = payload.get("year")
    links = ['<a href="/data"' + (' aria-current="page"' if current is None else "")
             + ">All time</a>"]
    for year in reversed(years):
        mark = ' aria-current="page"' if current == year else ""
        links.append(f'<a href="/data?{urlencode({"year": year})}"{mark}>{_esc(year)}</a>')
    return f'<nav class="dp-years" aria-label="Choose a year">{"".join(links)}</nav>'


def empty_state(payload):
    """The page for a period with no prints: what will show up, and where to start."""
    if payload.get("year") is not None:
        lead = f"No prints were recorded in {_esc(payload['year'])}."
    else:
        lead = "No prints recorded yet."
    return ('<section class="card dp-empty" id="data-empty">'
            f"<h2>{lead}</h2>"
            "<p>Print Handy keeps a record of every print it watches. Once a print "
            "finishes, this page shows how your prints are going, how much filament, "
            "time and money they used, and what went wrong when one failed.</p>"
            '<p>Check your printers are connected on the <a href="/printers">Printers</a> '
            'page, or price a file now on the <a href="/cost">Print COST</a> page.</p></section>')


def overview(payload):
    """How am I doing: the headline numbers in a row of tiles."""
    costs = payload.get("costs") or {}
    total = payload.get("total_prints", 0)
    completed = payload.get("completed", 0)
    failed = payload.get("failed", 0)
    decided = completed + failed
    rate_sub = (f"{completed} finished out of {decided} that finished or failed. "
                "Excludes stopped, printing, and unrecorded outcomes."
                if decided else "No finished or failed prints yet.")
    recorded = payload.get("filament_records", 0)
    missing = total - recorded
    filament_sub = (f"{recorded} of {print_stats.plural(total, 'print')} had weight recorded"
                    + (f" ({missing} missing)" if missing else ""))
    tiles = [
        ("Prints", str(total), ""),
        ("Success rate", percent(payload.get("success_rate")), rate_sub),
        ("Printing time", duration(payload.get("total_print_duration_seconds")),
         f"about {duration(payload.get('average_print_seconds'))} a print"
         if payload.get("average_print_seconds") else "no print time recorded"),
        ("Filament used", weight(payload.get("total_filament_used_g") if recorded else None),
         filament_sub),
    ]
    if costs.get("has_priced"):
        tiles.append(("Spent", money(costs.get("out_of_pocket_total"), costs.get("currency_code")),
                      f"out of pocket, {print_stats.plural(costs.get('priced_count', 0), 'priced print')}"))
    breakdown, note = _outcome_line(payload)
    body = "".join(
        f'<div class="dp-stat"><p class="eyebrow">{_esc(label)}</p>'
        f'<div class="v">{_esc(value)}</div>'
        f'<div class="s">{breakdown if label == "Prints" else _esc(sub)}</div></div>'
        for label, value, sub in tiles)
    return f'<section id="data-overview"><div class="dp-stats">{body}</div>{note}</section>'


def _outcome_line(payload):
    completed = payload.get("completed", 0)
    failed = payload.get("failed", 0)
    stopped = payload.get("canceled", 0)
    printing = payload.get("in_progress", 0)
    unrecorded = payload.get("total_prints", 0) - completed - failed - stopped - printing
    bits = [f"{completed} finished", f"{failed} failed", f"{stopped} stopped"]
    if printing:
        bits.append(f"{printing} printing now")
    note = ""
    if unrecorded > 0:
        bits.append(f'<a href="#data-outcome-note">{_esc(unrecorded)} outcome not recorded</a>')
        note = ('<p class="note" id="data-outcome-note">Some print records have no final outcome. '
                'They count toward total prints but not toward the success rate.</p>')
    return ", ".join(bits), note


def highlights(payload):
    """The #1719 highlights: pasta, where prints came from, most popular files.

    Every number here reads a field the records already carry: pasta is
    the watchdog count (with its not-recorded state shown honestly),
    sources are the records' own recorded platform or source address,
    and popular files are the prints made more than once. A print with
    no source recorded is counted as exactly that, never guessed.
    """
    outcomes = payload.get("outcomes") or {}
    parts = []
    if outcomes.get("pasta_state") == "recorded" and outcomes.get("pasta") is not None:
        pasta = outcomes["pasta"]
        line = (f"You made pasta {print_stats.plural(pasta, 'time')}: the camera "
                f"caught {print_stats.plural(pasta, 'print')} turning into spaghetti")
        if outcomes.get("pasta_events"):
            line += f" ({print_stats.plural(outcomes['pasta_events'], 'spaghetti event')} in all)"
        parts.append(f'<p class="dp-lede">{_esc(line)}.</p>')
    else:
        parts.append('<p class="dp-lede">Pasta is not counted yet: the camera '
                     "watchdog's spaghetti events are not recorded, so there is "
                     "no pasta count to show.</p>")
    if payload.get("stale_printing_note"):
        parts.append(f'<p class="note">{_esc(payload["stale_printing_note"])}</p>')
    sources = (payload.get("breakdowns") or {}).get("by_source") or {}
    if sources:
        total = sum(row.get("count", 0) for row in sources.values()) or 1
        items = []
        for label, row in list(sources.items())[:6]:
            count = row.get("count", 0)
            share = round(count * 100 / total)
            items.append(
                f'<li><span class="t">{_esc(_name(label, "Source not recorded"))}</span>'
                f'<span class="m">{_esc(print_stats.plural(count, "print"))} · {share}%</span>'
                f'<span class="dp-bar" style="flex-basis:100%" aria-hidden="true">'
                f'<i style="width:{share}%"></i></span></li>')
        parts.append('<h3>Where your prints came from</h3>'
                     f'<ul class="dp-list">{"".join(items)}</ul>')
    popular = [entry for entry in
               ((payload.get("repeats") or {}).get("most_reprinted") or [])
               if entry.get("count", 0) > 1][:5]
    if popular:
        items = "".join(
            f'<li><span class="t">{_esc(entry.get("title") or "Untitled print")}</span>'
            f'<span class="m">{_esc(entry.get("count"))} times</span></li>'
            for entry in popular)
        parts.append('<h3>Most popular files</h3>'
                     f'<ul class="dp-list">{items}</ul>')
    return ('<section id="data-highlights"><h2>Highlights</h2>'
            + "".join(parts) + "</section>")


def trend(payload, months=6):
    """The success rate month by month, newest last, as bars."""
    monthly = payload.get("monthly_outcomes") or {}
    keys = list(monthly)[-months:]
    if not keys:
        return ""
    rows = []
    for key in keys:
        entry = monthly[key]
        rate = entry.get("success_rate")
        width = 0 if rate is None else round(rate * 100)
        rows.append(
            f'<div class="row"><span>{_esc(month_name(key))}</span>'
            f'<div class="dp-bar" aria-hidden="true"><i style="width:{width}%"></i></div>'
            f'<span class="muted">{_esc(percent(rate))} of '
            f'{_esc(print_stats.plural(entry.get("prints", 0), "print"))}</span></div>')
    summary = _trend_sentence(monthly, keys)
    return ('<section id="data-trend"><h2>Month by month</h2>'
            + (f'<p class="dp-lede">{_esc(summary)}</p>' if summary else "")
            + f'<div class="dp-trend">{"".join(rows)}</div></section>')


def _trend_sentence(monthly, keys):
    """Whether the latest month did better or worse than the months before it."""
    rated = [k for k in keys if monthly[k].get("success_rate") is not None]
    if len(rated) < 2:
        return ""
    latest = rated[-1]
    earlier = rated[:-1]
    finished = sum(monthly[k]["finished"] for k in earlier)
    decided = finished + sum(monthly[k]["failed"] for k in earlier)
    if not decided:
        return ""
    before = finished / decided
    now = monthly[latest]["success_rate"]
    diff = round((now - before) * 100)
    name = month_name(latest)
    if diff >= 5:
        return f"{name} went better than the months before it ({percent(now)} against {percent(before)})."
    if diff <= -5:
        return f"{name} had more failures than the months before it ({percent(now)} against {percent(before)})."
    return f"{name} was about as reliable as the months before it ({percent(now)})."


def recent(payload):
    """The newest prints with their outcome."""
    rows = payload.get("recent_prints") or []
    if not rows:
        return ""
    zone = _zone(payload)
    items = []
    for row in rows:
        label, tone = _OUTCOME_LABELS.get(row.get("outcome"), ("Unknown", ""))
        meta = [day(row.get("started_at"), zone), _name(row.get("printer"), "")]
        if row.get("seconds") is not None:
            meta.append(duration(row["seconds"]))
        if row.get("filament_used_g") is not None:
            meta.append(weight(row["filament_used_g"])
                        + (f" {row['material']}" if row.get("material") else ""))
        if row.get("cost") is not None:
            meta.append(money(row["cost"], row.get("currency_code")))
        why = ""
        if row.get("outcome") == "failed" and row.get("stop_reason"):
            why = f'<span class="m">Why: {_esc(print_stats.failure_cause(row["stop_reason"])[0])}</span>'
        items.append(
            f'<li><span class="t">{_esc(row.get("title") or "Untitled print")}</span>'
            f'<span class="badge {tone}">{_esc(label)}</span>'
            f'<span class="m" style="flex-basis:100%">{_esc(" · ".join(b for b in meta if b))}</span>'
            f"{why}</li>")
    return ('<section id="data-recent"><h2>Recent prints</h2>'
            f'<ul class="dp-list">{"".join(items)}</ul></section>')


def failures(payload):
    """What went wrong, grouped by cause, with what to check."""
    causes = payload.get("failure_causes") or []
    outcomes = payload.get("outcomes") or {}
    extra = []
    if outcomes.get("pasta_state") == "recorded" and outcomes.get("pasta"):
        extra.append(f"The camera caught {print_stats.plural(outcomes['pasta'], 'print')} "
                     "turning into spaghetti.")
    if payload.get("risk_flagged"):
        extra.append(f"{print_stats.plural(payload['risk_flagged'], 'print')} started with a "
                     "warning from the slicer or support checks.")
    if not causes:
        text = ("No failed prints in this period. Nice work."
                if payload.get("total_prints") else "")
        if not text and not extra:
            return ""
        return ('<section id="data-failures"><h2>What went wrong</h2>'
                + (f"<p>{_esc(text)}</p>" if text else "")
                + "".join(f"<p>{_esc(line)}</p>" for line in extra) + "</section>")
    items = []
    for entry in causes:
        tip = f'<span class="m" style="flex-basis:100%">{_esc(entry["tip"])}</span>' if entry.get("tip") else ""
        items.append(f'<li><span class="t">{_esc(entry["cause"])}</span>'
                     f'<span class="m">{_esc(print_stats.plural(entry["count"], "print"))}</span>'
                     f"{tip}</li>")
    failed = payload.get("failed", 0)
    lede = (f"{print_stats.plural(failed, 'print')} failed. "
            "The most common causes first, with what to check.")
    return ('<section id="data-failures"><h2>What went wrong</h2>'
            f"<p>{_esc(lede)}</p>"
            f'<ul class="dp-list">{"".join(items)}</ul>'
            + "".join(f'<p class="note">{_esc(line)}</p>' for line in extra) + "</section>")


def printers(payload):
    """Each printer's share and record."""
    rows = payload.get("printers") or []
    if not rows:
        return ""
    zone = _zone(payload)
    total = sum(row.get("prints", 0) for row in rows) or 1
    cards = []
    for row in rows:
        rate = row.get("success_rate")
        width = 0 if rate is None else round(rate * 100)
        last = day(row.get("last_started_at"), zone)
        cards.append(
            '<section class="card">'
            f'<h3>{_esc(_name(row.get("printer"), "Printer not recorded"))}</h3>'
            f'<p>{_esc(print_stats.plural(row.get("prints", 0), "print"))} '
            f'<span class="muted">({round(row.get("prints", 0) * 100 / total)}% of all)</span></p>'
            f'<p>{_esc(percent(rate))} success'
            f'<span class="muted">, {row.get("failed", 0)} failed, {row.get("stopped", 0)} stopped</span></p>'
            f'<div class="dp-bar" aria-hidden="true"><i style="width:{width}%"></i></div>'
            f'<p class="muted">{_esc(duration(row.get("seconds")))} printing · '
            f'{_esc(weight(row.get("filament_used_g")))} filament'
            + (f" · last print {_esc(last)}" if last else "") + "</p></section>")
    return ('<section id="data-printers"><h2>Your printers</h2>'
            f'<div class="dp-grid">{"".join(cards)}</div>'
            '<p class="note">Printer settings and status are on the '
            '<a href="/printers">Printers</a> page.</p></section>')


def materials(payload):
    """What the filament and money went into: by material, top colors, spending."""
    breakdowns = payload.get("breakdowns") or {}
    by_material = breakdowns.get("by_material") or {}
    costs = payload.get("costs") or {}
    cost_by_material = costs.get("by_material") or {}
    currency = costs.get("currency_code")
    if not by_material and not costs.get("has_priced"):
        return ""
    items = []
    for label, row in list(by_material.items())[:6]:
        spent = cost_by_material.get(label)
        meta = [print_stats.plural(row.get("count", 0), "print"),
                weight(row.get("filament_used_g") or 0)]
        if spent:
            meta.append(money(spent, currency))
        items.append(f'<li><span class="t">{_esc(_name(label, "Material not recorded"))}</span>'
                     f'<span class="m">{_esc(" · ".join(meta))}</span></li>')
    colors = [label for label in (breakdowns.get("by_color") or {}) if label != UNKNOWN][:5]
    color_line = (f"<p>Favorite colors: {_esc(', '.join(colors))}.</p>" if colors else "")
    money_part = ""
    if costs.get("has_priced"):
        bits = [f"You spent {money(costs.get('out_of_pocket_total'), currency)} on filament "
                f"and power for {print_stats.plural(costs.get('priced_count', 0), 'priced print')}"]
        if costs.get("failed_count"):
            bits.append(f"{money(costs.get('failure_total'), currency)} of it went into "
                        f"{print_stats.plural(costs['failed_count'], 'print')} that failed or "
                        "were stopped")
        priciest = costs.get("most_expensive") or {}
        sentence = "; ".join(bits) + "."
        if priciest:
            sentence += (f" Most expensive: {print_stats.display_title(priciest.get('title')) or 'one print'}"
                         f" at {money(priciest.get('out_of_pocket'), currency)}.")
        money_part = f"<p>{_esc(sentence)}</p>"
    else:
        money_part = ("<p>No print in this period has a recorded cost yet. Costs are "
                      "recorded when a print finishes.</p>")
    return ('<section id="data-materials"><h2>Filament and cost</h2>'
            f'<ul class="dp-list">{"".join(items)}</ul>{color_line}{money_part}'
            '<p class="note">Price a file on the <a href="/cost">Print COST</a> page; see what is '
            'left on each spool on the <a href="/spools">Spools</a> page.</p></section>')


def favorites(payload):
    """The prints made most often."""
    repeats = (payload.get("repeats") or {}).get("most_reprinted") or []
    repeats = [entry for entry in repeats if entry.get("count", 0) > 1][:5]
    if not repeats:
        return ""
    items = "".join(
        f'<li><span class="t">{_esc(entry.get("title") or "Untitled print")}</span>'
        f'<span class="m">{_esc(entry.get("count"))} times</span></li>' for entry in repeats)
    return ('<section id="data-favorites"><h2>Printed most often</h2>'
            f'<ul class="dp-list">{items}</ul></section>')


def render_sections(payload):
    """The everyday part of the Data tab as HTML, from an ``/api/data`` payload."""
    parts = [year_switcher(payload)]
    if not payload.get("total_prints"):
        parts.append(empty_state(payload))
        return "".join(parts)
    parts.extend([overview(payload), highlights(payload), trend(payload), recent(payload),
                  failures(payload), printers(payload), materials(payload),
                  favorites(payload)])
    return "".join(part for part in parts if part)


__all__ = ["CSS", "day", "duration", "money", "month_name", "percent", "render_sections",
           "weight"]
