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
- the printer's own display as a still from the existing screen
  snapshot route (#1622), in its own labeled panel, only for a printer
  that publishes a screen: availability comes from the screen module's
  discovery through its cache, so a dashboard build adds no uncached
  webcam-list read. The panel is never a camera: the bed camera keeps
  its own source and the #1306 separation is unchanged;
- the emergency stop, offered only while ``[webui] controls_enabled``
  is on. The button posts to ``POST /dashboard/estop/<id>``, a
  ``session+csrf`` route (a browser cannot send the API's
  ``X-WebUI-Token`` header, so the API route refused every press on a
  claimed install), which calls the same control function as
  ``POST /api/printers/<id>/emergency_stop`` with ``{"confirm": true}``:
  the page asks first with a confirm dialog, and the per-family
  refusals come back verbatim. No new control path is added,
  and there is no Light button: the Web UI has no light control (the
  light is switched in chat and by the auto light-off automation), so
  the dashboard does not invent one.

Temperature history is the one fact nothing records today. The Web UI
keeps a small bounded in-process sample history instead: one sample per
printer per dashboard build, throttled and capped, held only in memory.
It starts empty when the Web UI starts and says so on the page; nothing
is persisted and nothing is backfilled or faked. Each sample carries the
heater targets too, so the graph draws current against target per
heater (#1515), with the scale and time span written on it.

The nozzle and bed panels (#1515) read the printer's hardware record
(the one ``/nozzle change`` and ``printer_state set`` write) and the
resolved settings: the fitted nozzle size and material, its recorded Z
offset and whether it is verified, the nozzle material's temperature
offset, and the per-material nozzle and bed defaults that offset
composes onto. Nothing here writes: the swap button opens the existing
guided ``/nozzle change`` flow in the Web UI chat, which keeps its own
confirmations.

Every section fails soft: a printer that cannot be read renders an
unavailable card, a Spoolman that does not answer renders a note in
place of the filament panel, and a records or clips problem renders no
list. The dashboard never breaks the Status page it rides on.
"""
from __future__ import annotations

import html
import logging
import re
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .. import go2rtc, settings
from ..core import print_stats
from . import controls as controls_module
from . import data as data_module
from . import screen as screen_module
from . import spools as spools_module
from . import status as status_module
from . import swatch as swatch_module
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

    def sample(self, printer_id, hotend_c, bed_c, hotend_target_c=None, bed_target_c=None):
        """Record one sample when due; a sample with no reading at all is skipped.

        Targets are kept alongside the readings (None when unknown); a
        target alone, with no reading, is not a sample.
        """
        hotend, bed = _number(hotend_c), _number(bed_c)
        if hotend is None and bed is None:
            return
        now = self._clock()
        series = self._series.setdefault(
            printer_id, deque(maxlen=self.MAX_SAMPLES))
        if series and now - series[-1][0] < self.MIN_INTERVAL_S:
            return
        series.append((now, hotend, bed, _number(hotend_target_c), _number(bed_target_c)))

    def samples(self, printer_id):
        """[(epoch, hotend_c, bed_c, hotend_target_c, bed_target_c)] oldest
        first; any value may be None."""
        return list(self._series.get(printer_id) or ())


def _number(value):
    """A real number, or None (bools are not temperatures)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


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
    names the clip rather than embedding a player nothing can serve.
    (Wrapped is sealed most of the year, so it is no place to send
    someone looking for yesterday's clip.) The answer is cached per set of roots
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


def _redact_detail(value):
    """Scrub embedded URLs, not just messages consisting of one URL."""
    import re
    from ..live_stream import redact_url
    return re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>\"']+",
                  lambda match: redact_url(match.group()), str(value))


# A transport failure arrives as the exception's own text ("URLError:
# <urlopen error [Errno 111] Connection refused>"). That is log detail,
# not something to show a person: the card says it in plain words, and
# a named reason keeps its words without the exception class in front.
_TRANSPORT_NOISE = re.compile(
    r"<urlopen|\[Errno|Traceback|timed out|HTTP Error \d+|Connection (?:refused|reset)"
    r"|Name or service not known|No route to host", re.IGNORECASE)
_EXCEPTION_PREFIX = re.compile(r"^\s*[A-Za-z_][\w.]*(?:Error|Exception|Timeout)\s*:\s*")
UNREACHABLE_TEXT = ("Print Handy could not reach this printer. Check that it is "
                    "turned on and connected to the network, then reload this "
                    "page.")


def _plain_detail(value):
    """``value`` for the card, in plain words: no exception class or socket text."""
    text = str(value or "").strip()
    if _TRANSPORT_NOISE.search(text):
        return UNREACHABLE_TEXT
    if _EXCEPTION_PREFIX.match(text):
        return _EXCEPTION_PREFIX.sub("", text, count=1).strip() or UNREACHABLE_TEXT
    return text


def _nozzle_label(kind):
    return str(kind or "").replace("_", " ").strip() or "unknown"


def hardware_facts(view, record):
    """The nozzle and bed panel facts for one printer, from its resolved
    settings ``view`` and hardware ``record`` (None when unreadable).

    Every fact names where it came from; a missing record falls back to
    the configured nozzle and says so. Never raises.
    """
    from ..core import nozzle as nozzle_module
    printer = (view or {}).get("printer") or {}
    record = record if isinstance(record, dict) else None
    fitted = (record or {}).get("nozzle") or {}
    facts = {"source": "hardware record" if fitted else "settings",
             "diameter_mm": _number(fitted.get("diameter_mm"))
             or _number(printer.get("nozzle_diameter_mm")),
             "type": None, "type_note": "", "installed": fitted.get("installed") or "",
             "z_offset_mm": _number((record or {}).get("z_offset_mm")),
             "z_offset_verified": bool((record or {}).get("z_offset_verified")),
             "temp_offset_c": None, "pending": [], "mesh_stale": False, "materials": []}
    try:
        raw = nozzle_module.recorded_nozzle_type(fitted.get("type"), printer.get("nozzle_type"))
        facts["type"] = nozzle_module.normalize_nozzle_type(raw)
        defaults = nozzle_module._nozzle_type_defaults()[facts["type"]]
        override = ((view or {}).get("nozzle_temp_offsets") or {}).get(facts["type"])
        facts["temp_offset_c"] = (_number(override) if _number(override) is not None
                                  else defaults["temp_offset_c"])
        facts["max_c"] = defaults["max_temperature_c"]
    except Exception as exc:  # an unknown type is shown as such, never guessed
        log.debug("dashboard nozzle type unresolved: %s", exc)
        facts["type_note"] = "type not recognized"
    pending = (record or {}).get("calibration_pending") or []
    facts["pending"] = [str(item) for item in pending if isinstance(item, str)]
    facts["mesh_stale"] = bool((record or {}).get("bed_mesh_stale"))
    offset = facts["temp_offset_c"] or 0.0
    for family, temps in sorted(((view or {}).get("material_temps") or {}).items()):
        if not isinstance(temps, dict):
            continue
        nozzle_c, bed_c = _number(temps.get("nozzle")), _number(temps.get("bed"))
        if nozzle_c is None or bed_c is None:
            continue
        facts["materials"].append({"family": str(family), "nozzle_c": nozzle_c + offset,
                                   "base_nozzle_c": nozzle_c, "bed_c": bed_c})
    return facts


def _screen_available(data, printer_id):
    """True when the printer publishes a screen (#1622).

    The screen module's probe, run through its cached discovery so a
    dashboard build reuses one webcam-list answer per printer inside
    the discovery TTL instead of adding an uncached read per page
    load. Discovery only: no still is fetched. Never raises: any
    failure reads as no screen, so the card renders without a panel.
    """
    try:
        available, _reason = screen_module.probe(
            data, printer_id, discover=screen_module._cached_discover)
    except Exception as exc:  # a failed probe is no panel, never a broken card
        log.debug("dashboard screen probe failed for %s: %s", printer_id, exc)
        return False
    return bool(available)


def build_dashboard(data, *, status_for=None, spools_for=None, history=None,
                    hardware_for=None, screen_for=None):
    """The dashboard's data: one card per roster printer, plus the
    install-wide recent prints and latest timelapse.

    ``status_for`` / ``spools_for`` are the server's per-printer
    functions (``printer_id -> (code, body)``); the defaults call
    ``status.printer_status`` and ``spools.spools_view`` directly.
    ``history`` is the temperature history to sample into and read;
    the module's shared one by default. ``hardware_for`` reads one
    printer's hardware record (``printer_id -> dict | None``); the
    default reads the record ``/nozzle change`` writes. ``screen_for``
    answers whether one printer publishes a screen (``printer_id ->
    bool``); the default probes through the screen module's cached
    discovery. Never raises.
    """
    history = HISTORY if history is None else history
    if status_for is None:
        status_for = lambda printer_id: status_module.printer_status(data, printer_id)
    if spools_for is None:
        spools_for = lambda printer_id: spools_module.spools_view(data, printer_id)
    if screen_for is None:
        screen_for = lambda printer_id: _screen_available(data, printer_id)
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
    if hardware_for is None:
        hardware_for = controls_module._hardware_record
    controls_on = controls_module.controls_enabled(data)
    for entry in entries:
        printer_id = entry["id"]
        card = {"id": printer_id, "name": entry.get("name") or printer_id,
                "model": entry.get("model") or "", "default": printer_id == default_id,
                "live": {}, "state": "unavailable", "detail": "",
                "camera": False, "screen": False, "controls_enabled": controls_on,
                "spools": None, "spools_note": "", "history": [], "hardware": None}
        view = None
        try:
            view = settings.view_for(printer_id, data)
            card["camera"] = _camera_capable(entry, view)
        except Exception as exc:  # identity facts only; the card still renders
            log.debug("dashboard view failed for %s: %s", printer_id, exc)
        try:
            card["screen"] = bool(screen_for(printer_id))
        except Exception as exc:  # a failed screen check is no panel
            log.debug("dashboard screen check failed for %s: %s", printer_id, exc)
        try:
            card["hardware"] = hardware_facts(view, hardware_for(printer_id))
        except Exception as exc:  # no panel facts is a note, never a broken card
            log.warning("dashboard hardware read failed for %s: %s", printer_id, exc)
        try:
            _code, body = status_for(printer_id)
            live = (body or {}).get("status") or {}
            card["live"] = live
            card["state"] = str(live.get("state") or "unavailable")
            raw_detail = _redact_detail(live.get("detail") or "")
            if raw_detail and raw_detail != _plain_detail(raw_detail):
                log.info("dashboard: printer %s unavailable: %s", printer_id, raw_detail)
            card["detail"] = _plain_detail(raw_detail)
            card["name"] = (body or {}).get("name") or card["name"]
            card["model"] = (body or {}).get("model") or card["model"]
            history.sample(printer_id, live.get("hotend_c"), live.get("bed_c"),
                           live.get("hotend_target_c"), live.get("bed_target_c"))
        except Exception as exc:  # a dead read is an unavailable card
            log.warning("dashboard status read failed for %s: %s", printer_id,
                        _redact_detail(f"{type(exc).__name__}: {exc}"))
            card["detail"] = UNREACHABLE_TEXT
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
            log.warning("dashboard spool read failed for %s: %s: %s", printer_id,
                        type(exc).__name__, _redact_detail(exc))
            card["spools_note"] = "Spoolman did not answer."
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


def _span_text(seconds):
    minutes = max(1, round(seconds / 60))
    return f"{minutes} min" if minutes < 90 else f"{minutes / 60:.1f} h"


def _heater_plot(samples, index, kind, label):
    """One heater's current and target lines, on that heater's own scale.

    Nozzle and bed run 150 degrees apart, so a shared axis flattens the
    bed into a line along the bottom (#1515 review). Each heater gets
    its own short plot with its scale written beside it.
    """
    width, height = 600, 64
    values = [row[i] for row in samples for i in (index, index + 2)
              if i < len(row) and row[i] is not None and (i == index or row[i] > 0)]
    if not values:
        return ""
    low, high = min(values), max(values)
    span = max(10.0, high - low)
    low = max(0.0, low - (span - (high - low)) / 2 - 2)
    high = low + span + 4
    t0, t1 = samples[0][0], max(samples[-1][0], samples[0][0] + 1)

    def points(i, skip_off):
        coords = []
        for row in samples:
            value = row[i] if i < len(row) else None
            if value is None or (skip_off and value <= 0):
                continue  # an off heater has no target line
            x = (row[0] - t0) / (t1 - t0) * width
            y = height - (value - low) / (high - low) * height
            coords.append(f"{x:.1f},{y:.1f}")
        return " ".join(coords)

    lines = []
    target = points(index + 2, True)
    if target:
        lines.append(f'<polyline class="{kind} target" points="{target}"></polyline>')
    current = points(index, False)
    if current:
        lines.append(f'<polyline class="{kind}" points="{current}"></polyline>')
    return ('<div class="temp-row">'
            f'<span class="temp-name"><span class="key {kind}"></span>{_esc(label)}</span>'
            '<div class="temp-plot">'
            f'<span class="temp-axis top">{_temp(high)}</span>'
            f'<span class="temp-axis bottom">{_temp(low)}</span>'
            f'<svg class="temp-chart" viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
            f'role="img" aria-label="{_esc(label)} temperature history">'
            + "".join(lines) + "</svg></div></div>")


def _temp_chart(card):
    """Current against target per heater, or the collecting note.

    Solid lines are readings, dashed lines are targets (a target of 0
    is a heater that is off and draws no target line). The time span
    and each heater's scale are written on the graph.
    """
    samples = card["history"]
    live = card.get("live") or {}

    def pair(label, current, target):
        text = f"{label} {_temp(current)}"
        if _number(target):
            text += f" → {_temp(target)}"
        return text

    legend = ('<p class="muted temp-legend">'
              f'<span class="key nozzle"></span>{_esc(pair("Nozzle", live.get("hotend_c"), live.get("hotend_target_c")))} · '
              f'<span class="key bed"></span>{_esc(pair("Bed", live.get("bed_c"), live.get("bed_target_c")))}</p>')
    if len(samples) < 2:
        return ('<div class="dash-panel"><h3>Temperature</h3>' + legend
                + '<p class="muted">Temperature history collects while the '
                "Web UI runs and starts empty each time it restarts; "
                "check back in a few minutes.</p></div>")
    span = samples[-1][0] - samples[0][0]
    plots = _heater_plot(samples, 1, "nozzle", "Nozzle") + _heater_plot(samples, 2, "bed", "Bed")
    return ('<div class="dash-panel"><h3>Temperature</h3>' + legend + plots
            + f'<p class="muted">Last {_span_text(span)}, since the Web UI started. '
              'Solid: current; dashed: target.</p></div>')


def _disc(spool, small=False):
    # Spoolman's color_hex has no '#'; swatch() writes the valid CSS form.
    color = (spool or {}).get("color_hex")
    kind = "disc small" if small else "disc"
    return (swatch_module.swatch(color, size="small" if small else "large")
            or f'<span class="{kind}" aria-hidden="true"></span>')


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


_PENDING_LABELS = {"z_offset": "Z offset", "bed_mesh": "bed mesh",
                   "pressure_advance_flow": "pressure advance and flow"}


def _hardware_panels(card):
    """The nozzle and bed panels, or a note when the facts are unreadable."""
    facts = card.get("hardware")
    if not facts:
        return ('<div class="hw-panels"><div class="hw-panel"><h3>Nozzle and bed</h3>'
                '<p class="muted">The hardware record could not be read.</p></div></div>')
    size = facts["diameter_mm"]
    kind = (_nozzle_label(facts["type"]) if facts["type"]
            else facts["type_note"] or "unknown")
    nozzle = ['<div class="hw-panel"><h3>Nozzle</h3>',
              f'<p class="job-line">{_esc(f"{size:g} mm" if size else "size unknown")} · '
              f'{_esc(kind)}</p>']
    meta = []
    if facts["installed"]:
        meta.append(f'fitted {facts["installed"]}')
    if facts["source"] == "settings":
        meta.append("from settings: no nozzle change recorded yet")
    if meta:
        nozzle.append(f'<p class="muted">{_esc(" · ".join(meta))}</p>')
    z = facts["z_offset_mm"]
    if z is None:
        z_text = "Z offset: not recorded"
    else:
        z_text = f"Z offset {z:+.3f} mm" + ("" if facts["z_offset_verified"] else " (not verified)")
    nozzle.append(f'<p>{_esc(z_text)}</p>')
    if facts["temp_offset_c"] is not None:
        nozzle.append(f'<p class="muted">Nozzle temperature offset '
                      f'{facts["temp_offset_c"]:+g}°C for {_esc(kind)}, added to every material below.</p>')
    if facts["pending"]:
        names = ", ".join(_PENDING_LABELS.get(item, item.replace("_", " "))
                          for item in facts["pending"])
        nozzle.append(f'<p class="badge warn">Calibration due: {_esc(names)}</p>')
    nozzle.append('<p class="panel-action"><a class="button" href="/chat?text=%2Fnozzle%20change">'
                  'Swap nozzle</a></p><p class="muted">Opens the guided nozzle change in chat.</p></div>')
    bed = ['<div class="hw-panel"><h3>Bed</h3>']
    live = card.get("live") or {}
    bed_now = _temp(live.get("bed_c"))
    bed_target = live.get("bed_target_c")
    bed.append(f'<p class="job-line">{_esc(bed_now)}'
               + (f' <span class="muted">target {_esc(_temp(bed_target))}</span>'
                  if _number(bed_target) else ' <span class="muted">heater off</span>') + '</p>')
    bed.append(f'<p>{"Bed mesh: stale since the last nozzle change" if facts["mesh_stale"] else "Bed mesh: no change recorded since it was probed"}</p>')
    if facts["materials"]:
        rows = "".join(
            f'<tr><td>{_esc(row["family"])}</td><td>{_temp(row["nozzle_c"])}</td>'
            f'<td>{_temp(row["bed_c"])}</td></tr>' for row in facts["materials"])
        bed.append('<details class="hw-more"><summary>Material temperature defaults</summary>'
                   '<table class="hw-defaults"><thead><tr><th>Material</th><th>Nozzle</th>'
                   f'<th>Bed</th></tr></thead><tbody>{rows}</tbody></table>'
                   '<p class="muted">Used when a spool or model sets no temperature '
                   '([material_temps]); the nozzle column includes the offset above.</p></details>')
    bed.append("</div>")
    return f'<div class="hw-panels">{"".join(nozzle)}{"".join(bed)}</div>'


def _screen_panel(card):
    """The printer screen panel (#1622): the printer's own display as a
    still from the existing screen snapshot route, labeled as the
    screen and linked to the dedicated screen page. It is never the
    bed camera (#1306): the camera keeps its own block above."""
    printer_id = quote(str(card["id"]), safe="")
    return (
        '<div class="dash-panel dash-screen"><h3>Printer screen</h3>'
        f'<img data-dash-screen src="/api/printers/{printer_id}/screen/snapshot" '
        f'alt="Printer screen of {_esc(card["name"])}">'
        '<p class="muted dash-screen-note"></p>'
        '<p class="muted">This is the printer\'s own display, not the bed '
        f'camera. <a href="/printers/{printer_id}/screen">Open the full '
        'printer screen view</a>.</p></div>')


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
    side = [f'<div class="dash-job">{_ring(card)}<div><p class="job-line">{_esc(headline)}</p>']
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
    side.append("</div></div>")
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
            f'data-name="{_esc(card["name"])}">Emergency stop</button>'
            '<p class="muted estop-note"></p></div>')
    # Status first, the camera beside it (#1515: the Home Assistant
    # dashboard's order), then heat, hardware and filament.
    top.insert(0, f'<div class="dash-side">{"".join(side)}</div>')
    parts.append(f'<div class="dash-top">{"".join(top)}</div>')
    if card.get("screen"):
        parts.append(_screen_panel(card))
    parts.append(f'<div class="dash-panel">{_filament(card)}</div>')
    parts.append(_temp_chart(card))
    parts.append(_hardware_panels(card))
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
            f'<p class="muted">{_esc(_age(clip["mtime"], now))} · {size_mb:.1f} MB</p>'
            '</section>')


# The dashboard's one script: refresh each camera and printer screen
# still in place (the screen page's backoff pattern) and wire the
# E-stop buttons to the existing control route. The E-stop asks first
# (a plain confirm dialog), then posts the route's own
# {"confirm": true} body; the route's answer, refusal included, is
# shown verbatim under the button.
# Without JavaScript the stills show their first frame and the E-stop
# button does nothing: no control is ever sent by the page itself.
_SCRIPT = (
    "<script>"
    "document.querySelectorAll('img[data-dash-cam],img[data-dash-screen]').forEach(function(img){"
    "var note=img.parentElement.querySelector('.dash-cam-note,.dash-screen-note');"
    "var base=img.src.split('?')[0];var failures=0;"
    "function next(){setTimeout(tick,Math.min(2000*Math.pow(2,failures),60000));}"
    "function tick(){if(document.hidden){next();return;}img.src=base+'?t='+Date.now();}"
    "img.onload=function(){failures=0;if(note)note.textContent='';next();};"
    "img.onerror=function(){failures+=1;"
    "if(note)note.textContent=img.hasAttribute('data-dash-screen')?"
    "'The printer screen did not answer; retrying less often.'"
    ":'The camera did not answer; retrying less often.';next();};"
    "});"
    "document.querySelectorAll('button[data-estop]').forEach(function(b){"
    "b.addEventListener('click',function(){"
    "var note=b.parentElement.querySelector('.estop-note');"
    "if(!confirm('Emergency stop '+b.getAttribute('data-name')+'? "
    "This halts the printer immediately.'))return;"
    "b.disabled=true;"
    "var t=document.getElementById('dash-csrf');"
    "fetch('/dashboard/estop/'+encodeURIComponent(b.getAttribute('data-estop')),"
    "{method:'POST',credentials:'same-origin',"
    "headers:{'Content-Type':'application/x-www-form-urlencoded'},"
    "body:'_csrf='+encodeURIComponent(t?t.value:'')})"
    ".then(function(r){return r.json().catch(function(){return {ok:false,reason:"
    "r.status===403?'this page is out of date or you are signed out; reload it and press again'"
    ":'HTTP '+r.status};});})"
    ".then(function(j){if(note)note.textContent=j.ok?(j.detail||'Emergency stop sent.')"
    ":('Not sent: '+(j.reason||'refused'));b.disabled=false;})"
    ".catch(function(){if(note)note.textContent="
    "'Could not reach the Web UI server; this page sent nothing.';b.disabled=false;});"
    "});});"
    "</script>")


def render_dashboard(dash, snap, *, now=None, form_token=""):
    """The dashboard sections as HTML, for the Status page body.

    ``form_token`` is the request's ``_csrf`` value; the E-stop button
    posts it to ``/dashboard/estop/<id>`` (a session+csrf route), since a
    browser cannot send the API's ``X-WebUI-Token`` header."""
    now = time.time() if now is None else now
    parts = [_health_row(dash, snap),
             f'<input type="hidden" id="dash-csrf" value="{_esc(form_token)}">']
    parts.extend(_card(card) for card in dash["cards"])
    parts.append(_timelapse(dash, now))
    parts.append(_recent(dash, now))
    parts.append(_SCRIPT)
    return "".join(parts)


__all__ = ["HISTORY", "TempHistory", "build_dashboard", "hardware_facts", "latest_timelapse",
           "recent_prints", "render_dashboard"]
