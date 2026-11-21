"""Status, health and small public route handlers for the Web UI (#1603 item 3).

Moved out of ``webui/server.py``: the status page rendering, the
health/readiness JSON routes, the favicon/static icon route, the PWA
plumbing routes, the setup redirect, the theme toggle and the themed
404. ``_Handler`` in ``server.py`` keeps thin delegates with the names
the route table dispatches to, and ``server.py`` re-exports
``render_status_html``, ``healthz``, ``ICON_FILES`` and ``STATIC_DIR``
for existing importers. Behavior, headers and page output are unchanged.
"""
from __future__ import annotations

import html
import logging
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import settings
from ..bot import health as bot_health
from . import access as access_module
from . import dashboard as dashboard_module
from . import data as data_module
from . import pwa as pwa_module
from . import shell
from . import status as status_module
from . import walkthrough as walkthrough_module
from . import pages

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
ICON_FILES = {
    "favicon.ico": "image/x-icon",
    "icon-16.png": "image/png",
    "icon-32.png": "image/png",
    "icon-64.png": "image/png",
    "icon-180.png": "image/png",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "icon-512-maskable.png": "image/png",
}


def healthz(data=None, heartbeat=status_module.READ_HEARTBEAT):
    """(http_code, body_dict) for /healthz: the bot-health evaluation.

    Mirrors print_handy.bot.health.serve: 200 when evaluate() passes,
    503 otherwise, with the reason and the heartbeat record. The record
    is whitelisted to the same safe keys the snapshot shows.
    """
    data = settings.load() if data is None else data
    if heartbeat is status_module.READ_HEARTBEAT:
        try:
            heartbeat = bot_health.read()
        except Exception:  # an unreadable heartbeat file reads as no heartbeat
            heartbeat = None
    ok, reason = status_module.bot_verdict(data, heartbeat)
    ok = ok is not False  # an unevaluated (None) verdict is not a failure, as in the snapshot
    body = {"ok": ok, "reason": reason}
    if isinstance(heartbeat, dict):
        body.update({key: heartbeat[key] for key in status_module.HEARTBEAT_KEYS
                     if key in heartbeat})
    return (200 if ok else 503), body

def _overall_job(data, *, read=None):
    """The default printer's live job for the status page's overall card
    (#813, second pass): the normalized live status ``printer_status``
    already serves the API and the Home Assistant integration, fetched
    for the one default printer.

    Returns the status dict when the printer is printing or paused, or
    has a job file at all; None otherwise — idle, unavailable, offline,
    unconfigured. Never raises and never retries: the read carries its
    own timeout, and any failure simply leaves the card without a job
    block. ``snapshot()`` is untouched: this is a page-only extra, the
    /api/status JSON does not gain a live read.
    """
    try:
        printer_id = settings.default_printer_id(data)
        _code, body = status_module.printer_status(data, printer_id, read=read)
        live = body.get("status") or {}
    except Exception:  # a broken roster or read path is "no job block"
        return None
    if live.get("state") in ("printing", "paused") or live.get("filename"):
        return live
    return None

def _job_block(job):
    """The overall card's job lines, from a normalized live status dict:
    the file name, a progress bar with its percent, and a muted line of
    the parts that exist (state, layers, remaining time)."""
    esc = html.escape
    parts = []
    if job.get("filename"):
        parts.append(f'<p class="job-line">{esc(str(job["filename"]))}</p>')
    progress = job.get("progress")
    if isinstance(progress, (int, float)) and not isinstance(progress, bool):
        percent = min(100, max(0, round(progress * 100)))
        parts.append(f'<div class="progress"><i style="width: {percent}%"></i></div>')
    meta = []
    state = str(job.get("state") or "")
    if state:
        meta.append(state.capitalize())
    if isinstance(progress, (int, float)) and not isinstance(progress, bool):
        meta.append(f"{round(progress * 100):.0f}% done")
    layer, total = job.get("layer"), job.get("total_layers")
    if layer is not None and total is not None:
        meta.append(f"Layer {layer} of {total}")
    remaining = job.get("remaining_min")
    if remaining is not None:
        meta.append(f"about {remaining} min left")
    if meta:
        parts.append(f'<p class="muted">{" · ".join(esc(bit) for bit in meta)}</p>')
    return "".join(parts)

def render_status_html(snap, form_token="", *, theme=shell.DEFAULT_THEME,
                       walkthrough_pending=False, job=None, error=None, nav=None,
                       unclaimed=False, loopback=False, open_reason="",
                       credential_ignored=False, dashboard=None):
    """The / page: the snapshot as escaped HTML in the shared shell (#813).

    The overall card on top is derived from what the snapshot already
    carries — its ok flag, the doctor rows' warn/fail counts, and the
    printer's name and connection — plus, when the caller fetched one
    (``job``, the default printer's normalized live status), the active
    job's file, progress and layers. ``walkthrough_pending`` adds the
    slim finish-setup banner under the header. ``error`` (a /debug
    validation refusal re-rendering this page, #840) shows above the
    card. ``dashboard`` (#1445, a dashboard.build_dashboard result)
    renders the Home Assistant sample's screen — health row, printer
    cards with camera, progress ring, temperatures and history,
    filament, latest timelapse, recent prints — between the overall
    card and the detail tables; without one the page is exactly the
    pre-dashboard readout.
    """
    esc = html.escape
    bot, listener, spoolman = snap["bot"], snap["listener"], snap["spoolman"]
    heartbeat = bot["heartbeat"] or {}
    rows = [
        ("Version", snap["version"]),
        ("Host", snap["host"]),
        ("Printer", f"{snap['printer']['name']} ({snap['printer']['model']})"),
        ("Connection", snap["printer"]["connection"]),
        ("Bot", f"{bot['platform']} / {bot['mode']} — {bot['reason']}"),
        ("Bot connected", str(heartbeat.get("connected", "unknown"))),
        ("Listener", "not required" if not listener["required"]
         else ("ok" if listener["ok"] else "FAIL") + f" — {listener['reason']}"),
        ("Spoolman", ("bundled" if spoolman["bundled"] else "external")
         + f" {spoolman['url'] or '(not configured)'} — "
         + ("not configured" if spoolman["ok"] is None
            else "ok" if spoolman["ok"] else "down") + f" ({spoolman['detail']})"),
    ]
    services = "".join(
        f"<tr><th>{esc(key)}</th><td>{esc(str(value))}</td></tr>"
        for key, value in snap["services"].items())
    roster = snap.get("printers") or []
    roster_table = ""
    if len(roster) > 1:  # the read-only roster appears only when there is a roster (#337)
        body = "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                esc(row["id"]) + (" (default)" if row["default"] else ""),
                esc(row["name"]), esc(row["model"]), esc(row["connection"]),
                esc(row["chat_id"] or "main channel"),
                esc(row.get("moonraker_url") or row.get("octoprint_url")
                    or row.get("lan_host") or row.get("link_host") or row.get("host") or ""))
            for row in roster)
        roster_table = ("<h2>Printers</h2><p>Add a printer or open its spools on the "
                        '<a href="/printers">Printers page</a>.</p>'
                        '<div class="table-scroll"><table><tr><th>id</th><th>name</th>'
                        "<th>model</th><th>connection</th>"
                        f"<th>channel</th><th>endpoint</th></tr>{body}</table></div>")
    doctor = "".join(
        f"<tr><td>{esc(row['label'])}</td><td>{esc(row['status'])}</td>"
        f"<td>{esc(row['detail'])}</td></tr>" for row in snap["doctor"])
    # Debug log mode control (#478): the same form-token guard as the
    # config POSTs. Without a form token (programmatic renders) the
    # doctor table's debug mode row above is the whole surface.
    debug_section = ""
    if form_token:
        debug_row = next((row for row in snap["doctor"] if row["label"] == "debug mode"), None)
        state = esc(debug_row["detail"]) if debug_row else "off"
        debug_section = (
            "<h2>Debug log mode</h2>"
            f"<p>State: {state}. Logging only — printing is unaffected. "
            "Turning it off ends the session; the sanitized export is "
            "delivered by the bot (`/print-handy debug off`) or the CLI "
            "(`python3 -m print_handy.debug_mode off`).</p>"
            '<form method="post" action="/debug">'
            f'<input type="hidden" name="{pages.CSRF_FIELD}" value="{esc(form_token)}">'
            '<label>Minutes <input name="minutes" size="4" placeholder="30"></label> '
            '<button name="action" value="on" type="submit">Turn on</button> '
            '<button name="action" value="off" type="submit" '
            'data-confirm="Turn off debug mode? This ends the debug session.">Turn off</button>'
            "</form>")
    failures = snap.get("slice_failures") or []
    failures_table = ""
    if failures:
        failures_table = (
            "<h2>Recent model failures</h2><p>The raw slicer logs of a failed model are kept in these "
            "folders on the host; a chat failure post names the stage and cause, and these hold the "
            "detail to attach to a bug report.</p><table><tr><th>folder</th><th>modified</th></tr>"
            + "".join(f"<tr><td>{esc(row['path'])}</td><td>"
                      f"{esc(time.strftime('%Y-%m-%d %H:%M', time.localtime(row['modified'])))}</td></tr>"
                      for row in failures)
            + "</table>")
    main_rows = "".join(
        f"<tr><th>{esc(label)}</th><td>{esc(str(value))}</td></tr>" for label, value in rows)
    warns = sum(1 for row in snap["doctor"] if row["status"] == "warn")
    fails = sum(1 for row in snap["doctor"] if row["status"] == "fail")
    pills = []
    if fails:
        pills.append(f'<span class="badge bad">{fails} failing</span>')
    if warns:
        pills.append(f'<span class="badge warn">{warns} warning{"s" if warns != 1 else ""}</span>')
    if not pills:
        pills.append('<span class="badge ok">all checks ok</span>')
    headline = "All good" if snap["ok"] else "Attention needed"
    overall = ('<section class="card"><div class="overall-head">'
               f"<h2>{headline}</h2><span>{' '.join(pills)}</span></div>"
               f'<p class="muted">{esc(snap["printer"]["name"])} · '
               f'{esc(snap["printer"]["connection"])}</p>'
               f'{_job_block(job) if job else ""}</section>')
    banner = ""
    if walkthrough_pending:
        banner = ('<div class="banner"><span>Finish setting up Print Handy — '
                  'the first-run walkthrough is still open.</span>'
                  '<a class="button accent" href="/walkthrough">Finish setup</a></div>')
    if unclaimed:
        open_reason = access_module.OPEN_UNCLAIMED
    if open_reason == access_module.OPEN_UNCLAIMED:
        reach = ('It listens only on this machine, so only someone using this '
                 'machine can reach it. Set a login in the walkthrough if you '
                 'want one.' if loopback else
                 'anyone who can reach it can use it. Set a login in the '
                 'walkthrough, or leave it open on a network you trust.')
        banner += (f'<div class="banner"><span>No login is set for this Web UI — {reach}</span>'
                   '<a class="button accent" href="/walkthrough">Open the walkthrough</a></div>')
    elif open_reason:
        # Open for a reason other than "nothing set yet": say which, so
        # an open Web UI is never mistaken for a protected one.
        reach = ("only someone using this machine can reach it" if loopback
                 else "anyone who can reach it can use it")
        if open_reason == access_module.OPEN_AUTH_OFF:
            why = ("This Web UI is open: login is turned off by "
                   "<code>[webui] require_auth = false</code>, so " + reach + ".")
            if credential_ignored:
                why += (" A saved login or access token exists but is ignored until "
                        "that setting is back on.")
        else:
            why = ("This Web UI is open: a login was removed in the walkthrough "
                   "(the leave-it-open choice), so " + reach + ".")
        banner += (f'<div class="banner"><span>{why}</span>'
                   '<a class="button" href="/config?section=webui">Web UI settings</a></div>')
    # A retired AI endpoint that could not migrate (#1759): the Doctor row
    # above (label "AI endpoint", doctor.AI_ENDPOINT_LABEL) says which keys,
    # and this banner is the way to the fix — Settings → AI.
    if any(row.get("label") == "AI endpoint" and row.get("status") == "warn"
           for row in snap["doctor"]):
        banner += ('<div class="banner"><span>AI is not configured: your old AI settings '
                   'were retired and could not be moved to the one endpoint automatically.'
                   '</span><a class="button" href="/config?section=ai">Open AI settings</a></div>')
    body = ((f'<p class="error">{esc(error)}</p>' if error else "")
            + overall
            + (dashboard_module.render_dashboard(dashboard, snap, form_token=form_token) if dashboard else "")
            + roster_table
            + failures_table
            # The connection readout and doctor table are system detail,
            # folded at the bottom so the dashboard reads as status (#1515).
            # A failing or warning check opens the fold.
            + f'<details class="card sys-details"{" open" if (fails or warns or not dashboard) else ""}>'
            + "<summary>System details</summary>"
            + f'<table class="rows">{main_rows}</table>'
            + (f"<h2>Services</h2><table>{services}</table>" if services else "")
            + f"<h2>Doctor checks</h2><table>{doctor}</table>"
            + "</details>"
            + debug_section)
    return shell.page("status", body, path="/", theme=theme, banner=banner, nav=nav)

def healthz_route(handler):
    code, body = healthz(handler.server.settings_data)
    handler._json(code, body)

def favicon_route(handler):
    handler._icon_file("favicon.ico")

def static_route(handler, name):
    handler._icon_file(name)

def icon_file(handler, name):
    """One branding icon (#1456), from the ICON_FILES allowlist.

    Public by design (see routes.py): the bytes are the project's
    own logo, nothing about the install. Icons change only with a
    release, so a browser may keep one for a day."""
    content_type = ICON_FILES.get(name)
    if content_type is None:
        handler._json(404, {"ok": False,
                         "reason": "no icon by that name. "
                                   "try /favicon.ico or /static/icon-32.png"})
        return
    try:
        data = (STATIC_DIR / name).read_bytes()
    except OSError:
        handler._json(404, {"ok": False,
                         "reason": "that icon could not be read."})
        return
    handler._send(200, data, content_type,
               headers=[("Cache-Control", "public, max-age=86400")])

def setup_redirect(handler):
    handler._redirect("/")

def pwa_manifest(handler):
    handler._send(200, pwa_module.manifest_json(),
               "application/manifest+json; charset=utf-8",
               headers=[("Cache-Control", "public, max-age=86400")])

def pwa_service_worker(handler):
    # no-cache: a stale worker script would keep an old caching
    # policy alive; Service-Worker-Allowed grants the root scope
    # even though the script itself lives at the root already.
    handler._send(200, pwa_module.SW_SCRIPT,
               "application/javascript; charset=utf-8",
               headers=[("Service-Worker-Allowed", "/"),
                        ("Cache-Control", "no-cache")])

def pwa_offline(handler):
    handler._send(200, pwa_module.render_offline_page(),
               "text/html; charset=utf-8",
               headers=[("Cache-Control", "public, max-age=86400")])

def status_page(handler):
    if (handler._browser_navigation("/")
            and walkthrough_module.offer_once(handler.server.settings_data)):
        # First run (#813): a browser landing on the status page goes
        # to the walkthrough until it is done; the setup gate's
        # discipline, one step later. API callers and every other
        # route are unaffected.
        handler._redirect("/walkthrough")
        return
    snap = handler.server.snapshot_func()
    handler._send(200, render_status_html(
        snap, form_token=handler._form_token(), theme=handler._theme(),
        walkthrough_pending=walkthrough_module.pending(handler.server.settings_data),
        unclaimed=handler.server.access.unclaimed,
        open_reason=getattr(handler.server.access, "open_reason", ""),
        credential_ignored=getattr(handler.server.access, "credential_ignored", False),
        loopback=_is_loopback(handler.server.bind_host),
        job=_overall_job(handler.server.settings_data),
        dashboard=dashboard_module.build_dashboard(
            handler.server.settings_data,
            status_for=handler.server.printer_status_func,
            spools_for=handler.server.spools_view_func),
        nav=data_module.nav_for(handler.server.settings_data)
        if (data_module.data_enabled(handler.server.settings_data)
            or data_module.wrapped_enabled(handler.server.settings_data))
        else None),
        "text/html; charset=utf-8")

def api_status(handler):
    handler._json(200, handler.server.api_snapshot_func())

def api_printers(handler):
    # The cheap roster (#1400): no doctor run, no probe, no printer read.
    handler._json(200, handler.server.roster_func())

def readyz_route(handler):
    # Readiness per component (Spoolman, slicer, each printer), behind
    # the same auth as /api/status; /healthz stays the
    # unauthenticated liveness probe.
    ready = handler.server.readiness_func() or {}
    handler._json(200 if ready.get("ready", False) else 503, ready)

def not_found(handler, path):
    if handler._browser_navigation(path):
        # A browser navigation to a path that does not exist gets the
        # themed refusal, not a JSON dump (#840).
        handler._html_refusal(404, "No page lives at this address.", "/")
        return
    handler._json(404, {"ok": False,
                     "reason": "try /, /api/status, /api/printers/<id>/status, "
                               "/api/printers/<id>/camera/snapshot, "
                               "/api/printers/<id>/spools, "
                               "/healthz, /readyz, /config, /designs, /spools, "
                               "/walkthrough or /login"})

def theme_route(handler):
    """GET /theme?set=dark|light&back=<path>: a compatibility redirect only.

    The Web UI is dark only (#1750), so this changes nothing about how any
    page looks and sets no cookie. It stays behind the same authorization
    as every other page so older bookmarks and cached links keep working.
    The ``back`` target must be a local path; anything else lands on the
    status page, so the route is never an open redirect."""
    query = parse_qs(urlparse(handler.path).query)
    theme = (query.get("set") or [""])[0]
    if theme not in shell.THEMES:
        handler._json(400, {"ok": False, "reason": "set must be dark or light"})
        return
    back = (query.get("back") or ["/"])[0]
    if not back.startswith("/") or back.startswith("//") \
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in back):
        # Not a local path, or it carries control characters: a decoded
        # CR/LF would otherwise land in the Location header verbatim
        # and inject response headers of the caller's choosing.
        back = "/"
    handler._redirect(back)


def _is_loopback(host):
    from .server import is_loopback
    return is_loopback(host)
