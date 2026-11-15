"""The Status page dashboard (#1445): the Home Assistant sample, in the Web UI.

The sample dashboard catduck runs in Home Assistant puts the whole
printer on one screen: the camera, a printer card with a progress ring
and temperatures, the loaded and shelf filament, a temperature history
graph, a health row, recent prints and the latest timelapse. This module
builds that screen for the Web UI Status page from the pieces that
already own each fact, and nothing else:

- live printer state through ``status.printer_status`` (the normalized
  status the API and the Home Assistant integration already serve);
- loaded slots and the shelf through ``spools.spools_view`` (the
  Spoolman-backed view behind ``GET /api/printers/<id>/spools``);
- recent prints from the print records (``core.print_stats``), the
  same records the Data and Wrapped pages aggregate;
- the latest timelapse from the clip roots the Wrapped finder searches;
- the camera still from the existing snapshot route, embedded as an
  image and refreshed in place (the screen page's pattern);
- the emergency stop through the existing control route
  (``POST /api/printers/<id>/emergency_stop``), offered only while
  ``[webui] controls_enabled`` is on, and still carrying that route's
  own gates: the access policy, the ``{"confirm": true}`` body (the
  page asks first, with a confirm dialog) and the per-family refusals,
  whose reason the page shows verbatim. No new control path is added,
  and there is no Light button: the Web UI has no light control (the
  light is switched in chat and by the auto light-off automation), so
  the dashboard does not invent one.

Temperature history is the one fact nothing records today. The Web UI
keeps a small bounded in-process sample history instead: one sample per
printer per dashboard build, throttled and capped, held only in memory.
It starts empty when the Web UI starts and says so on the page; nothing
is persisted and nothing is backfilled or faked.

Every section fails soft: a printer that cannot be read renders an
unavailable card, a Spoolman that does not answer renders a note in
place of the filament panel, and a records or clips problem renders no
list. The dashboard never breaks the Status page it rides on.
"""
from __future__ import annotations

import html
import logging
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .. import go2rtc, settings
from ..core import print_stats
from . import controls as controls_module
from . import data as data_module
from . import spools as spools_module
from . import status as status_module
from . import wrapped_video

log = logging.getLogger(__name__)

# Connections whose adapters take their own camera stills (the camera
# snapshot route's rule), so a camera block is worth rendering even with
# no configured snapshot URL.
_ADAPTER_CAMERA_CONNECTIONS = ("bambulab_lan", "prusalink")

_FINISHED = ("completed", "failed", "canceled", "cancelled")
_RECENT_LIMIT = 8
_SHELF_LIMIT = 12


# ------------------------------------------------------------- temp history

class TempHistory:
    """Bounded in-process temperature samples, per printer.

    One sample per printer at most every ``MIN_INTERVAL_S`` seconds,
    at most ``MAX_SAMPLES`` kept (about 96 minutes at the floor). Held
    in memory only: a Web UI restart starts the graph over, which the
    page states plainly.
    """

    MAX_SAMPLES = 288
    MIN_INTERVAL_S = 20.0

    def __init__(self, clock=None):
        self._clock = clock or time.time
        self._series = {}

    def sample(self, printer_id, hotend_c, bed_c):
        """Record one sample when due; a sample with no reading at all is skipped."""
        hotend = hotend_c if isinstance(hotend_c, (int, float)) and not isinstance(hotend_c, bool) else None
        bed = bed_c if isinstance(bed_c, (int, float)) and not isinstance(bed_c, bool) else None
        if hotend is None and bed is None:
            return
        now = self._clock()
        series = self._series.setdefault(
            printer_id, deque(maxlen=self.MAX_SAMPLES))
        if series and now - series[-1][0] < self.MIN_INTERVAL_S:
            return
        series.append((now, hotend, bed))

    def samples(self, printer_id):
        """[(epoch, hotend_c|None, bed_c|None)] oldest first."""
        return list(self._series.get(printer_id) or ())


HISTORY = TempHistory()


# ------------------------------------------------------------------- facts

def _epoch(value):
    """Epoch seconds for a record timestamp (ISO text), or None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def recent_prints(data, limit=_RECENT_LIMIT, *, records_vault=None):
    """The newest finished print records, newest first.

    Rows carry only what a record itself states: title (through
    ``print_stats.display_title``, so a path never reaches the page),
    status, duration, filament grams and material, and the finish (or
    start) time as epoch seconds. A records problem is an empty list.
    """
    try:
        records, _skipped = print_stats.load_records(data_module._vault(data, records_vault))
    except Exception:  # a records problem is no list, never a broken page
        log.warning("dashboard could not load print records", exc_info=True)
        return []
    rows = []
    for record in records:
        status = str(record.get("status") or "").strip().lower()
        if status not in _FINISHED:
            continue
        when = _epoch(record.get("completed_at")) or _epoch(record.get("started_at"))
        duration = record.get("print_duration_seconds")
        grams = record.get("filament_used_g")
        rows.append({
            "title": print_stats.display_title(record.get("title")) or "untitled",
            "status": status,
            "when": when,
            "duration_s": float(duration) if isinstance(duration, (int, float))
            and not isinstance(duration, bool) else None,
            "filament_g": float(grams) if isinstance(grams, (int, float))
            and not isinstance(grams, bool) else None,
            "material": str(record.get("filament_material") or "").strip(),
        })
    rows.sort(key=lambda row: row["when"] or 0, reverse=True)
    return rows[:limit]


# The clip walk stats every clip under every root, so the Status page
# reuses one answer for this long instead of walking on every load.
_TIMELAPSE_TTL_S = 60.0
_timelapse_cache = {}


def latest_timelapse(data, *, now=None):
    """The newest timelapse clip in the finder's roots, or None.

    Name, size and modification time only: the Web UI serves the
    compiled Wrapped video, not individual clips, so the dashboard
    names the clip and links to the Wrapped page rather than embedding
    a player nothing can serve. The answer is cached per set of roots
    for ``_TIMELAPSE_TTL_S`` seconds.
    """
    try:
        roots = wrapped_video._clip_roots(data)
    except Exception:  # a roots problem is no clip, never a broken page
        log.warning("dashboard could not resolve the timelapse roots", exc_info=True)
        return None
    now = time.monotonic() if now is None else now
    key = tuple(str(root) for root in roots)
    cached = _timelapse_cache.get(key)
    if cached is not None and now - cached[0] < _TIMELAPSE_TTL_S:
        return cached[1]
    newest = None
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.mp4"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if newest is None or stat.st_mtime > newest["mtime"]:
                newest = {"name": path.name, "size": stat.st_size, "mtime": stat.st_mtime}
    _timelapse_cache.clear()  # one roots set per install; never grows
    _timelapse_cache[key] = (now, newest)
    return newest


def _camera_capable(entry, view):
    """True when the camera snapshot route can answer for this printer:
    a configured snapshot URL, or an adapter that takes its own stills,
    and camera images not switched off for the printer."""
    try:
        if go2rtc.camera_source(view) == "none":
            return False
    except Exception as exc:  # an unreadable pin never hides a camera
        log.debug("camera capability check failed: %s", exc)
    if (view.get("services", {}).get("camera_snapshot_url") or "").strip():
        return True
    from .. import healthcheck
    return healthcheck.listener_connection(view) in _ADAPTER_CAMERA_CONNECTIONS


def build_dashboard(data, *, status_for=None, spools_for=None, history=None):
    """The dashboard's data: one card per roster printer, plus the
    install-wide recent prints and latest timelapse.

    ``status_for`` / ``spools_for`` are the server's per-printer
    functions (``printer_id -> (code, body)``); the defaults call
    ``status.printer_status`` and ``spools.spools_view`` directly.
    ``history`` is the temperature history to sample into and read;
    the module's shared one by default. Never raises.
    """
    history = HISTORY if history is None else history
    if status_for is None:
        status_for = lambda printer_id: status_module.printer_status(data, printer_id)
    if spools_for is None:
        spools_for = lambda printer_id: spools_module.spools_view(data, printer_id)
    cards = []
    try:
        entries = settings.printers(data)
    except Exception:  # a broken roster is no cards, never a broken page
        log.warning("dashboard could not read the printer roster", exc_info=True)
        entries = []
    try:
        default_id = settings.default_printer_id(data)
    except Exception:
        log.warning("dashboard could not read the default printer", exc_info=True)
        default_id = None
    controls_on = controls_module.controls_enabled(data)
    for entry in entries:
        printer_id = entry["id"]
        card = {"id": printer_id, "name": entry.get("name") or printer_id,
                "model": entry.get("model") or "", "default": printer_id == default_id,
                "live": {}, "state": "unavailable", "detail": "",
                "camera": False, "controls_enabled": controls_on,
                "spools": None, "spools_note": "", "history": []}
        try:
            view = settings.view_for(printer_id, data)
            card["camera"] = _camera_capable(entry, view)
        except Exception as exc:  # identity facts only; the card still renders
            log.debug("dashboard view failed for %s: %s", printer_id, exc)
        try:
            _code, body = status_for(printer_id)
            live = (body or {}).get("status") or {}
            card["live"] = live
            card["state"] = str(live.get("state") or "unavailable")
            card["detail"] = str(live.get("detail") or "")
            card["name"] = (body or {}).get("name") or card["name"]
            card["model"] = (body or {}).get("model") or card["model"]
            history.sample(printer_id, live.get("hotend_c"), live.get("bed_c"))
        except Exception as exc:  # a dead read is an unavailable card
            log.warning("dashboard status read failed for %s: %s", printer_id, exc)
            card["detail"] = f"{type(exc).__name__}: {exc}"
        card["history"] = history.samples(printer_id)
        try:
            code, sbody = spools_for(printer_id)
            if code == 200 and (sbody or {}).get("ok"):
                card["spools"] = {"slots": sbody.get("slots") or [],
                                  "shelf": sbody.get("shelf") or []}
            else:
                card["spools_note"] = str((sbody or {}).get("reason")
                                          or "Spoolman did not answer.")
        except Exception as exc:  # a Spoolman problem is a note, not a failure
            card["spools_note"] = f"Spoolman did not answer ({type(exc).__name__})."
        cards.append(card)
    return {"cards": cards, "recent": recent_prints(data),
            "timelapse": latest_timelapse(data)}


# ------------------------------------------------------------------ render

def _esc(value):
    return html.escape(str(value), quote=True)


def _age(when, now):
    """A short age for an epoch time: "19h ago", "2d ago", "just now"."""
    if when is None:
        return ""
    seconds = max(0.0, now - when)
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)}m ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)}h ago"
    return f"{round(seconds / 86400)}d ago"


def _duration(seconds):
    """A print duration the way the sample shows it: "4h 18m", "20m"."""
    if seconds is None:
        return ""
    total = round(seconds)
    hours, remainder = divmod(total, 3600)
    if hours:
        return f"{hours}h {remainder // 60}m"
    return f"{max(1, total // 60)}m"


def _temp(value):
    """A temperature as the sample shows it: a whole degree, or a dash."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{round(value)}°C"
    return "—"


_STATE_BADGES = {"printing": "ok", "paused": "warn", "error": "bad",
                 "unavailable": "bad"}


def _headline(card):
    """(headline, subline) for one card's state, the sample's wording."""
    state, live = card["state"], card["live"]
    if state in ("printing",):
        return "Printing", str(live.get("filename") or "")
    if state == "paused":
        return "Paused", str(live.get("filename") or "")
    if state in ("idle", "standby", "ready", "operational"):
        return "Ready for the next print", "Load a spool, pick a file, go."
    if state == "complete":
        return "Print complete", str(live.get("filename") or "")
    if state == "error":
        return "Print error", str(live.get("filename") or card["detail"] or "")
    if state == "unavailable":
        return "Printer unavailable", card["detail"]
    return state.capitalize(), str(live.get("filename") or "")


def _ring(card):
    """The progress ring: an SVG circle pair with the percent in the middle."""
    live = card["live"]
    progress = live.get("progress")
    percent = None
    if isinstance(progress, (int, float)) and not isinstance(progress, bool):
        percent = min(100, max(0, round(progress * 100)))
    shown = percent if percent is not None else 0
    circumference = 326.7  # 2 * pi * 52, the radius below
    offset = circumference * (1 - shown / 100)
    state_word = {"idle": "Idle", "standby": "Idle", "ready": "Ready",
                  "operational": "Ready"}.get(card["state"], card["state"].capitalize())
    return (
        '<div class="ring-wrap"><svg viewBox="0 0 120 120" role="img" '
        f'aria-label="{_esc(state_word)}, {shown}%">'
        '<circle cx="60" cy="60" r="52" fill="none" stroke="var(--color-line)" '
        'stroke-width="10"></circle>'
        f'<circle cx="60" cy="60" r="52" fill="none" stroke="var(--color-accent)" '
        f'stroke-width="10" stroke-linecap="round" '
        f'stroke-dasharray="{circumference}" stroke-dashoffset="{offset:.1f}" '
        'transform="rotate(-90 60 60)"></circle></svg>'
        f'<div class="ring-center"><span class="pct">{shown}%</span>'
        f'<span class="muted">{_esc(state_word)}</span></div></div>')


def _temp_chart(card):
    """The temperature history as an inline SVG, or the collecting note."""
    samples = card["history"]
    live = card["live"]
    legend = (f'<p class="muted">Nozzle {_temp(live.get("hotend_c"))} · '
              f'Bed {_temp(live.get("bed_c"))}</p>')
    if len(samples) < 2:
        return (legend + '<p class="muted">Temperature history collects while the '
                "Web UI runs and starts empty each time it restarts; "
                "check back in a few minutes.</p>")
    width, height = 600, 120
    times = [row[0] for row in samples]
    values = [value for row in samples for value in row[1:] if value is not None]
    low, high = min(values), max(values)
    span = max(10.0, high - low)
    low -= (span - (high - low)) / 2
    high = low + span
    t0, t1 = times[0], max(times[-1], times[0] + 1)

    def points(index):
        coords = []
        for row in samples:
            if row[index] is None:
                continue
            x = (row[0] - t0) / (t1 - t0) * width
            y = height - (row[index] - low) / (high - low) * height
            coords.append(f"{x:.1f},{y:.1f}")
        return " ".join(coords)

    return (
        legend
        + f'<svg class="temp-chart" viewBox="0 0 {width} {height}" '
          'preserveAspectRatio="none" role="img" aria-label="Temperature history">'
        f'<polyline class="nozzle" points="{points(1)}"></polyline>'
        f'<polyline class="bed" points="{points(2)}"></polyline></svg>'
        + '<p class="muted">Temperature history since the Web UI started '
          '(orange: nozzle, yellow: bed).</p>')


def _disc(spool, small=False):
    color = (spool or {}).get("color_hex") or ""
    style = f' style="background: {_esc(color)}"' if color else ""
    kind = "disc small" if small else "disc"
    return f'<span class="{kind}"{style} aria-hidden="true"></span>'


def _grams(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{round(value)} g"
    return ""


def _filament(card):
    """The filament panel: loaded slots as discs, then the shelf by material."""
    printer_id = quote(str(card["id"]), safe="")
    if card["spools"] is None:
        return ('<h3>Filament</h3><p class="muted">Filament unavailable: '
                f'{_esc(card["spools_note"])} See the '
                f'<a href="/spools/{printer_id}">Spools page</a>.</p>')
    slots, shelf = card["spools"]["slots"], card["spools"]["shelf"]
    loaded = []
    for row in slots:
        spool = row.get("spool")
        if not spool:
            loaded.append(f'<div class="slot"><span class="disc empty" aria-hidden="true">'
                          f'</span><div class="muted">Slot {int(row.get("slot") or 0)}</div>'
                          '<div class="muted">Empty</div></div>')
            continue
        percent = spool.get("remaining_percent")
        meta = " · ".join(bit for bit in (
            _grams(spool.get("remaining_weight")),
            f"{round(percent)}%" if isinstance(percent, (int, float)) else "") if bit)
        slot_number = int(row.get("slot") or 0)
        card_link = (card["controls_enabled"] and
                     f'<a class="slot slot-link" href="/spools/{printer_id}#slot-{slot_number}" '
                     f'aria-label="Change spool in slot {slot_number}">')
        if card_link:
            loaded.append(
                card_link + f'{_disc(spool)}'
                f'<div>{_esc(spool.get("name") or "")}</div>'
                f'<div class="muted">{_esc(spool.get("material") or "")}</div>'
                f'<div class="muted">{_esc(meta)}</div>'
                '<span class="slot-action">Change spool →</span></a>')
        else:
            loaded.append(
                f'<div class="slot">{_disc(spool)}'
                f'<div>{_esc(spool.get("name") or "")}</div>'
                f'<div class="muted">{_esc(spool.get("material") or "")}</div>'
                f'<div class="muted">{_esc(meta)}</div></div>')
    parts = ['<h3>Filament</h3>', f'<div class="slots">{"".join(loaded)}</div>']
    if shelf:
        groups = {}
        for spool in shelf:
            groups.setdefault(spool.get("material") or "Other", []).append(spool)
        parts.append('<p class="eyebrow">Shelf</p>')
        shown = 0
        for material, rows in groups.items():
            if shown >= _SHELF_LIMIT:
                break  # no bare material headers once the row limit is reached
            parts.append(f'<p class="muted">{_esc(material)} · {len(rows)}</p>')
            for spool in rows:
                if shown >= _SHELF_LIMIT:
                    break
                shown += 1
                parts.append(
                    f'<div class="shelf-row">{_disc(spool, small=True)}'
                    f'<span>{_esc(spool.get("name") or "")}</span>'
                    f'<span class="muted">{_esc(_grams(spool.get("remaining_weight")))}</span></div>')
        if len(shelf) > shown:
            parts.append(f'<p class="muted">and {len(shelf) - shown} more on the '
                         f'<a href="/spools/{printer_id}">Spools page</a>.</p>')
    return "".join(parts)


def _card(card):
    live = card["live"]
    printer_id = quote(str(card["id"]), safe="")
    badge = _STATE_BADGES.get(card["state"], "")
    headline, subline = _headline(card)
    parts = ['<section class="card dash-card">']
    parts.append(
        f'<div class="overall-head"><h2>{_esc(card["name"])}</h2>'
        f'<span class="badge {badge}">{_esc(card["state"])}</span></div>')
    top = []
    if card["camera"]:
        top.append(
            '<div class="dash-cam">'
            f'<img data-dash-cam src="/api/printers/{printer_id}/camera/snapshot" '
            f'alt="Camera view of {_esc(card["name"])}">'
            '<p class="muted dash-cam-note"></p></div>')
    side = [_ring(card), f'<p class="job-line">{_esc(headline)}</p>']
    if subline:
        side.append(f'<p class="muted">{_esc(subline)}</p>')
    meta = []
    layer, total = live.get("layer"), live.get("total_layers")
    if layer is not None and total is not None:
        meta.append(f"Layer {layer} of {total}")
    if live.get("remaining_min") is not None:
        meta.append(f"about {live['remaining_min']} min left")
    if meta:
        side.append(f'<p class="muted">{" · ".join(_esc(bit) for bit in meta)}</p>')
    side.append(
        '<div class="temps">'
        f'<div class="temp-box"><div class="muted">Nozzle</div>'
        f'<div class="v">{_temp(live.get("hotend_c"))}</div>'
        + (f'<div class="muted">target {_temp(live.get("hotend_target_c"))}</div>'
           if live.get("hotend_target_c") else "")
        + '</div>'
        f'<div class="temp-box"><div class="muted">Bed</div>'
        f'<div class="v">{_temp(live.get("bed_c"))}</div>'
        + (f'<div class="muted">target {_temp(live.get("bed_target_c"))}</div>'
           if live.get("bed_target_c") else "")
        + '</div></div>')
    if card["controls_enabled"]:
        side.append(
            '<div class="dash-controls">'
            f'<button type="button" class="danger" data-estop="{printer_id}" '
            f'data-name="{_esc(card["name"])}">E-stop</button>'
            '<p class="muted estop-note"></p></div>')
    top.append(f'<div class="dash-side">{"".join(side)}</div>')
    parts.append(f'<div class="dash-top">{"".join(top)}</div>')
    parts.append(_temp_chart(card))
    parts.append(_filament(card))
    parts.append("</section>")
    return "".join(parts)


def _health_row(dash, snap):
    components = snap.get("components") or {}
    ready = components.get("ready", snap.get("ok"))
    fails = sum(1 for row in snap.get("doctor") or [] if row.get("status") == "fail")
    states = [card["state"] for card in dash["cards"]]
    if "error" in states:
        print_error = "Error"
    elif states and all(state == "unavailable" for state in states):
        print_error = "Unknown"
    else:
        print_error = "OK"
    stats = [("Ready", "On" if ready else "Not ready"),
             ("Doctor failures", str(fails)),
             ("Print error", print_error),
             ("Version", str(snap.get("version") or ""))]
    return ('<div class="dash-health">' + "".join(
        f'<div class="stat"><div class="muted">{_esc(label)}</div>'
        f'<div class="v">{_esc(value)}</div></div>' for label, value in stats)
        + "</div>")


def _recent(dash, now):
    if not dash["recent"]:
        return ""
    rows = []
    marks = {"completed": ("✓", "ok"), "failed": ("✕", "bad"),
             "canceled": ("■", ""), "cancelled": ("■", "")}
    for row in dash["recent"]:
        mark, kind = marks.get(row["status"], ("·", ""))
        right = " · ".join(bit for bit in (_duration(row["duration_s"]),
                                           _grams(row["filament_g"])) if bit)
        meta = " · ".join(bit for bit in (row["material"], _age(row["when"], now)) if bit)
        rows.append(
            '<div class="recent-row"><div>'
            f'<span class="badge {kind}">{mark}</span> {_esc(row["title"])}'
            f'<div class="muted">{_esc(meta)}</div></div>'
            f'<div class="muted">{_esc(right)}</div></div>')
    return ('<section class="card"><h2>Recent prints</h2>'
            + "".join(rows) + "</section>")


def _timelapse(dash, now):
    clip = dash["timelapse"]
    if not clip:
        return ""
    size_mb = clip["size"] / 1_000_000
    return ('<section class="card"><h2>Latest timelapse</h2>'
            f'<p class="job-line">{_esc(clip["name"])}</p>'
            f'<p class="muted">{_esc(_age(clip["mtime"], now))} · {size_mb:.1f} MB · '
            'watch it on the <a href="/wrapped">Wrapped page</a></p></section>')


# The dashboard's one script: refresh each camera still in place (the
# screen page's backoff pattern) and wire the E-stop buttons to the
# existing control route. The E-stop asks first (a plain confirm
# dialog), then posts the route's own {"confirm": true} body; the
# route's answer, refusal included, is shown verbatim under the button.
# Without JavaScript the camera shows its first still and the E-stop
# button does nothing: no control is ever sent by the page itself.
_SCRIPT = (
    "<script>"
    "document.querySelectorAll('img[data-dash-cam]').forEach(function(img){"
    "var note=img.parentElement.querySelector('.dash-cam-note');"
    "var base=img.src.split('?')[0];var failures=0;"
    "function next(){setTimeout(tick,Math.min(2000*Math.pow(2,failures),60000));}"
    "function tick(){if(document.hidden){next();return;}img.src=base+'?t='+Date.now();}"
    "img.onload=function(){failures=0;if(note)note.textContent='';next();};"
    "img.onerror=function(){failures+=1;"
    "if(note)note.textContent='The camera did not answer; retrying less often.';next();};"
    "});"
    "document.querySelectorAll('button[data-estop]').forEach(function(b){"
    "b.addEventListener('click',function(){"
    "var note=b.parentElement.querySelector('.estop-note');"
    "if(!confirm('Emergency stop '+b.getAttribute('data-name')+'? "
    "This halts the printer immediately.'))return;"
    "b.disabled=true;"
    "fetch('/api/printers/'+b.getAttribute('data-estop')+'/emergency_stop',"
    "{method:'POST',headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({confirm:true})})"
    ".then(function(r){return r.json();})"
    ".then(function(j){if(note)note.textContent=j.ok?(j.detail||'Emergency stop sent.')"
    ":('Not sent: '+(j.reason||'refused'));b.disabled=false;})"
    ".catch(function(){if(note)note.textContent="
    "'Could not reach the Web UI server; this page sent nothing.';b.disabled=false;});"
    "});});"
    "</script>")


def render_dashboard(dash, snap, *, now=None):
    """The dashboard sections as HTML, for the Status page body."""
    now = time.time() if now is None else now
    parts = [_health_row(dash, snap)]
    parts.extend(_card(card) for card in dash["cards"])
    parts.append(_timelapse(dash, now))
    parts.append(_recent(dash, now))
    parts.append(_SCRIPT)
    return "".join(parts)


__all__ = ["HISTORY", "TempHistory", "build_dashboard", "latest_timelapse",
           "recent_prints", "render_dashboard"]
