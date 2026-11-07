"""Web UI HTTP server (#286, stages 2-3).

A stdlib-only HTTP server (``http.server``; no new dependency, no build
step) serving:

- ``GET /`` — a simple server-rendered status page (plain HTML, every
  interpolated value escaped; the only JavaScript anywhere is the
  shell's shared form-submit guard, #840);
- ``GET /api/status`` — the status snapshot as JSON
  (:mod:`print_handy.webui.status`);
- ``POST /api/printers/<id>/pause|resume|cancel|temperature`` — printer
  controls (#708, :mod:`print_handy.webui.controls`), off unless
  ``[webui] controls_enabled = true``; header auth only, JSON bodies;
- ``POST /api/printers/<id>/emergency_stop|firmware_restart|
  server_restart|gcode|exclude`` — the ha-print-handy #1 control
  routes (same module and the same guard): the Moonraker power
  endpoints behind ``{"confirm": true}``, raw G-code (Moonraker
  script, OctoPrint printer commands), and Klipper exclude-object
  through ``core.exclude_object``;
- ``GET /api/printers/<id>/status`` — one roster printer's live status
  as JSON (:func:`print_handy.webui.status.printer_status`, #664): the
  adapter's normalized status, read live; an unknown id is a 404 and an
  unreachable printer answers with an explicit unavailable status;
- ``GET /api/printers/<id>/camera/snapshot`` — one JPEG from that
  printer's camera (:mod:`print_handy.webui.camera`, #708), never the
  camera URL itself;
- ``GET /api/printers/<id>/spools`` and
  ``POST /api/printers/<id>/spools/{assign,swap,unassign,add,edit,sync,
  purchase/propose,purchase/create}`` — spool management (#789,
  :mod:`print_handy.webui.spools`): the loaded view and shelf as JSON,
  and the slot/spool changes through spool_sync, behind the same
  header-only POST auth and the #708 controls guard as the controls;
- ``GET /spools`` and ``GET /spools/<id>`` — the per-printer Spools
  page (#789): the same view as HTML, with picker forms that post back
  to ``POST /spools/<id>`` under the config posts' form-token rule;
- ``GET /data``, ``GET /wrapped``, ``GET /api/data`` and
  ``GET /api/wrapped`` — the Data tab and Print Handy Wrapped (#956,
  :mod:`print_handy.webui.data`), behind ``[timelapse]
  wrapped_enabled``; ``POST /wrapped/video`` compiles the Wrapped
  video from the Wrapped page's form under the same form-token rule,
  additionally behind ``[timelapse] wrapped_ai_video``;
- ``GET /healthz`` — the bot heartbeat evaluation, with the same
  200/503 + JSON shape as the standalone bot's own health server;
- ``GET /config`` — the picker configuration page
  (:mod:`print_handy.webui.pages`), the one write surface in the Web UI.
- ``GET /designs`` and ``GET /designs/<id>`` — the read-only catalog of
  AI design sessions (#388, stage 3;
  :mod:`print_handy.webui.designs`), with per-session file downloads
  under ``GET /designs/<id>/<scad|stl|preview>``.
- ``GET /layers`` and ``GET /layers/<job>/<file>`` — a read-only view of
  one sliced plate (#624; :mod:`print_handy.webui.layers`). No print,
  pause, or exclude control, and nothing here changes chat confirm.
- ``GET /walkthrough`` and ``POST /walkthrough`` — the first-run
  walkthrough (#813; :mod:`print_handy.webui.walkthrough`), offered
  only to installs that resolved access through the setup page and
  have not finished it; its steps write through the config path above.
- ``GET /theme?set=dark|light&back=<path>`` — the header theme toggle
  (#813; :mod:`print_handy.webui.shell`): sets the ``ph-theme`` cookie
  and redirects back. A browser preference only, behind the same
  authorization as every other page.

Every HTML page renders through the one shell
(:mod:`print_handy.webui.shell`): one inline stylesheet, the header
chrome, and the dark-by-default theme.

Unknown GET paths are 404. Writes happen only through the config page's
two POSTs (``POST /config/preview`` renders the diff and writes nothing;
``POST /config`` with the confirm field performs the validated write)
and the walkthrough's ``POST /walkthrough``, which writes through the
same validated path; every other non-GET method or path is 405.

The server starts only when ``[webui] enabled`` is true and ``port`` is
above 0. Access control is decided by :mod:`print_handy.webui.access`:
the access token in the environment variable ``[webui] access_token_env``
names (default ``WEBUI_ACCESS_TOKEN``), or the password chosen on the
first-run setup page (#708), is sent as the ``X-WebUI-Token`` header and
compared in constant time. ``[webui] require_auth`` false keeps its
meaning: no credential at all. A non-loopback bind with neither a token
nor a saved setup choice starts in setup mode and serves only
``/setup`` (and ``/healthz``) until the operator sets a password or
affirmatively leaves it open. The token or password is never rendered,
logged, or included in any response.

POSTs additionally accept the per-server form token the config pages
embed (``pages.CSRF_FIELD``) instead of the header — that is how a
browser form posts — and on loopback with no access token configured
the form token is still required, so a cross-origin page in the
operator's browser cannot change settings. The form token is generated
at server start, kept only in memory, and is not the access token.

Browser login: a browser cannot send the ``X-WebUI-Token`` header, so
``GET /login`` (reachable without auth — it is the entry point) renders
a token form when a token is configured, and ``POST /login`` trades a
correct token for a session cookie (``SESSION_COOKIE``, HttpOnly,
SameSite=Strict). An unauthenticated browser navigation (a GET for a
non-API page with ``Accept: text/html``) redirects to ``/login``
instead of returning the JSON 401 (#440); API requests keep the JSON
401. A valid, unexpired session cookie authorises GETs
exactly like the header; it never authorises a config POST on its own
(those still need the header or the form token, so login cannot be
turned into a CSRF path). Plain sessions live only in server memory
with a ``SESSION_TTL_S`` lifetime — a restart signs them out. Ticking
"Keep me signed in on this browser" on the login form instead mints a
remembered session with a ``REMEMBER_TTL_S`` (30-day) lifetime: the
cookie gains a matching ``Max-Age`` and the session id is persisted
only as its SHA-256 hash in ``webui_sessions.json`` in the state
folder (mode 0600, written atomically), so that browser stays signed
in across restarts. Changing the access credential revokes every
remembered session, and ``POST /logout`` ends the caller's own
session in both stores. ``/healthz`` stays
unauthenticated: the container healthcheck probes it. With no token
configured there is nothing to log in to: ``/login`` redirects to ``/``
and the loopback posture is otherwise unchanged.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import secrets
import sys
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .. import settings
from ..bot import health as bot_health
from . import access as access_module
from . import camera as camera_module
from . import config as config_api
from . import controls as controls_module
from . import data as data_module
from . import designs as designs_module
from . import layers as layers_module
from . import pages
from . import printers_page
from . import shell
from . import spools as spools_module
from . import status as status_module
from . import walkthrough as walkthrough_module

log = logging.getLogger(__name__)

TOKEN_HEADER = "X-WebUI-Token"
LOOPBACK_NAMES = frozenset({"localhost"})
# Browser login sessions: the cookie carries only an opaque id; the
# id -> expiry map lives in server memory (see make_server), so nothing
# is persisted and a restart signs everyone out. The Secure flag is
# off by default for plain HTTP on a trusted LAN (Unraid WebUI link);
# set [webui] cookie_secure = true when exposed via an HTTPS reverse proxy.
SESSION_COOKIE = "webui_session"
SESSION_TTL_S = 12 * 60 * 60  # 12 hours
# Remembered sessions ("Keep me signed in on this browser" on the login
# form): a 30-day session whose id is persisted only as its SHA-256 hash
# in SESSIONS_FILE in the state folder, so it survives a server restart.
# The raw id is never written to disk.
REMEMBER_TTL_S = 30 * 24 * 60 * 60  # 30 days
SESSIONS_FILE = "webui_sessions.json"
_REMEMBER_TRUTHY = frozenset({"on", "true", "1", "yes"})
# The store file's reserved key for the credential fingerprint (see
# _load_remembered_sessions). Session keys are SHA-256 hex digests, so
# this name can never collide with one.
_FINGERPRINT_KEY = "__credential_sha256__"


def sessions_path(data):
    return Path(settings.state_dir(data)) / SESSIONS_FILE


def _session_digest(session_id):
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _remember_lock(server):
    """The server's remembered-session lock, or a no-op context when the
    server object predates it (the mutation sites stay safe either way)."""
    lock = getattr(server, "remembered_lock", None)
    return lock if lock is not None else contextlib.nullcontext()


def _load_remembered_sessions(data, fingerprint):
    """{sha256(session id): expiry epoch} from SESSIONS_FILE.

    A missing or unreadable file reads as no remembered sessions, exactly
    like access._read_saved; expired entries are pruned on load. The file
    also carries the fingerprint of the credential its sessions were
    minted under: when that no longer matches ``fingerprint`` (the token
    was rotated in the environment, or the saved password changed) the
    whole store is discarded and deleted — a session minted under an old
    credential must not survive its rotation just because the rotation
    happened while the server was stopped."""
    path = sessions_path(data)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(saved, dict):
        return {}
    if saved.get(_FINGERPRINT_KEY) != fingerprint:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("webui: could not delete stale remembered sessions file: %s", exc)
        return {}
    now = time.time()
    loaded = {}
    for digest, expiry in saved.items():
        if digest == _FINGERPRINT_KEY:
            continue
        try:
            expiry = float(expiry)
        except (TypeError, ValueError):
            continue
        if isinstance(digest, str) and digest and expiry > now:
            loaded[digest] = expiry
    return loaded


def _save_remembered_sessions(server):
    """Persist server.remembered_sessions atomically with mode 0600.

    The snapshot and the write happen under the server's remembered
    lock, so a concurrent logout or revocation cannot be overwritten by
    a stale snapshot (callers that mutate the store hold the same lock
    across their mutation-through-save sequence). Returns True when the
    file was written. A failure is logged and reported, never raised: a
    read-only state mount must not break sign-in (the caller falls back
    to an ephemeral session)."""
    path = sessions_path(server.settings_data)
    with _remember_lock(server):
        try:
            payload = dict(getattr(server, "remembered_sessions", {}) or {})
            access = getattr(server, "access", None)
            if access is not None:
                payload[_FINGERPRINT_KEY] = access.credential_fingerprint()
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".webui_sessions.")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            log.warning("webui: could not save remembered sessions to %s: %s", path, exc)
            return False
    return True

# The form-token refusal for browser forms (#840): the token is
# regenerated on every process start, so a tab left open across a
# restart — the config flow tells the operator to restart — always
# lands here, and the wording names that case instead of the mechanism.
STALE_FORM_MESSAGE = ("This page is out of date (Print Handy restarted). "
                      "Reload it and try again.")


class WebUIServerError(RuntimeError):
    """The Web UI server was refused: disabled, unconfigured, or unsafe."""


# Suffixes public DNS never serves (mDNS, RFC 6762/8375 home and private-use names).
PRIVATE_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localhost")


def private_hostname(name):
    """True for a Host a DNS-rebinding site cannot own: an IP literal, a single-label
    name ("tower") or a private-use suffix ("unraid.local", "nas.home.arpa")."""
    name = str(name or "").rstrip(".")
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return "." not in name or name.endswith(PRIVATE_SUFFIXES)


def is_loopback(host):
    """True for loopback bind hosts (127.0.0.0/8, ::1, "localhost") only."""
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def access_token(data, environ=None):
    """The configured access token's *value* from the environment ("" if unset).

    Only the name is a setting (``[webui] access_token_env``); the value
    comes from the environment and is never a setting, like every other
    Print Handy secret. With ``[webui] require_auth`` false the token is
    deliberately ignored, so "" is returned even when it is set.
    """
    environ = os.environ if environ is None else environ
    if not data["webui"].get("require_auth", True):
        return ""
    return environ.get(data["webui"]["access_token_env"]) or ""


def check_start(data, environ=None):
    """Refuse a disabled or unconfigured start; return (host, port, token).

    A non-loopback bind with no token and no saved setup choice no longer
    refuses: it starts in first-run setup (:mod:`print_handy.webui.access`)
    and serves nothing but the setup page until a choice is made (#708).
    """
    webui = data["webui"]
    if not webui["enabled"]:
        raise WebUIServerError(
            "webui: not started: [webui] enabled is false (the Web UI is "
            "optional and off by default; set enabled = true and a port to run it)")
    port = int(webui["port"] or 0)
    if port <= 0:
        raise WebUIServerError(
            "webui: not started: [webui] port is 0 (unset); set a port above 0 to run it")
    return webui["host"], port, access_token(data, environ)


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
    ok = ok is not False  # not applicable (Hermes mode) is healthy, as in the snapshot
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
                       walkthrough_pending=False, job=None, error=None, nav=None):
    """The / page: the snapshot as escaped HTML in the shared shell (#813).

    The overall card on top is derived from what the snapshot already
    carries — its ok flag, the doctor rows' warn/fail counts, and the
    printer's name and connection — plus, when the caller fetched one
    (``job``, the default printer's normalized live status), the active
    job's file, progress and layers. ``walkthrough_pending`` adds the
    slim finish-setup banner under the header. ``error`` (a /debug
    validation refusal re-rendering this page, #840) shows above the
    card.
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
    body = ((f'<p class="error">{esc(error)}</p>' if error else "")
            + overall
            + f'<table class="rows">{main_rows}</table>'
            + roster_table
            + (f"<h2>Services</h2><table>{services}</table>" if services else "")
            + f"<h2>Doctor checks</h2><table>{doctor}</table>"
            + failures_table
            + debug_section)
    return shell.page("status", body, path="/", theme=theme, banner=banner, nav=nav)


class _Handler(BaseHTTPRequestHandler):
    server_version = "PrintHandyWebUI/1"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, *args):  # keep stdout clean; errors still surface as 500s
        pass

    def _host_refused(self, *, post=False):
        """True (and a 403 sent) for a DNS-rebinding request.

        On a loopback bind without an access token the form token is the
        only guard on a write, and the page that carries it is served to
        anyone who can reach the port. A hostile site that rebinds its own
        hostname to 127.0.0.1 is "same origin" with this server, so it
        could read the token and post settings changes. Such a request
        still names the attacker's hostname in Host (and Origin), so only
        the loopback names and the bind address are accepted.

        A LAN bind with no password (first-run setup, or the "leave it open"
        choice) has the same exposure on any address, and #708 makes it the
        container default. A rebinding site needs a public DNS name it
        controls, so there only names it cannot serve are accepted (an IP
        literal, a single-label name, a private-use suffix), and a write's
        Origin must be the same host, which also refuses cross-site posts.
        Re-read per request: finishing setup replaces server.access.
        """
        allowed = self.server.allowed_hosts
        open_lan = not is_loopback(self.server.bind_host) and not self.server.access.required
        if allowed is None and not open_lan:
            return False
        def hostname(value):
            value = str(value or "").strip().lower()
            if value.startswith("["):
                return value[1:].split("]", 1)[0]
            return value.rsplit(":", 1)[0] if value.count(":") == 1 else value
        names = [hostname(self.headers.get("Host"))]
        origin = self.headers.get("Origin")
        if post and origin and origin != "null":
            names.append(hostname(urlparse(origin).netloc))
        if allowed is not None:
            if all(name in allowed for name in names):
                return False
            self._send(403, "Forbidden: this Web UI answers only to its own loopback address.",
                       "text/plain; charset=utf-8")
            return True
        if private_hostname(names[0]) and all(name == names[0] for name in names[1:]):
            return False
        self._send(403, "Forbidden: with no Web UI password this page answers only by IP "
                        "address or a local host name. Set a password on the setup page to "
                        "reach it through another name.", "text/plain; charset=utf-8")
        return True

    def _send(self, code, body, content_type, headers=()):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload, indent=1, sort_keys=True, default=str),
                   "application/json")

    def _redirect(self, location, headers=()):
        self._send(303, "", "text/html; charset=utf-8",
                   headers=[("Location", location), *headers])

    def _html_refusal(self, code, message, back="/"):
        """A refusal as a themed page for a browser form or navigation
        (#840): the shell, the plain message, and a Back link, with the
        status code unchanged. ``/api/*`` routes never come through
        here — they keep the JSON ``{ok, reason}`` shape."""
        body = (f'<p class="error">{html.escape(message)}</p>'
                f'<p><a href="{html.escape(back, quote=True)}">Back</a></p>')
        self._send(code, shell.page("error", body, path=back, theme=self._theme()),
                   "text/html; charset=utf-8")

    # -- auth ---------------------------------------------------------------
    def _header_authorized(self):
        """The access token (or setup password) in the ``X-WebUI-Token``
        header (the stage 2 rule); nothing is required when access is open."""
        access = self.server.access
        if not access.required:
            return True
        provided = self.headers.get(TOKEN_HEADER) or ""
        if not provided:
            return False  # no header is not a guess; a browser session may still apply
        # Failed-guess backoff shares the login form's per-address counter.
        # A locked-out address is refused before any hashing, even with the
        # right token; _deny_header turns that into a 429.
        address = self.client_address[0]
        throttle = self.server.login_throttle
        if throttle.retry_after(address):
            return False
        if access.matches(provided):
            throttle.record_success(address)
            return True
        throttle.record_failure(address)
        return False

    def _deny_header(self):
        """401 for a missing or wrong header token; 429 with Retry-After
        while this address is locked out after repeated wrong tokens."""
        wait = self.server.login_throttle.retry_after(self.client_address[0])
        if wait:
            self._send(429, json.dumps({"ok": False, "reason": "too many failed attempts",
                                        "retry_after": wait}, indent=1, sort_keys=True),
                       "application/json", headers=[("Retry-After", str(wait))])
            return
        self._json(401, {"ok": False, "reason": f"missing or wrong {TOKEN_HEADER}"})

    def _theme(self):
        """This browser's theme from its cookie (#813); dark by default."""
        return shell.theme_from_cookies(self.headers.get("Cookie"))

    def _session_id(self):
        """The session id from the Cookie header, or "" (garbage = none)."""
        raw = self.headers.get("Cookie") or ""
        if not raw:
            return ""
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:  # a malformed Cookie header carries no session
            return ""
        morsel = jar.get(SESSION_COOKIE)
        return morsel.value if morsel is not None else ""

    def _session_authorized(self):
        """A valid, unexpired login-session cookie. Expired ids are
        pruned on sight; unknown or expired cookies are simply
        unauthenticated. Both stores count: the ephemeral in-memory
        map (raw id) and the remembered map (SHA-256 of the id)."""
        session_id = self._session_id()
        if not session_id:
            return False
        now = time.time()
        expiry = self.server.sessions.get(session_id)
        if expiry is not None:
            if expiry <= now:
                del self.server.sessions[session_id]
                return False
            return True
        remembered = getattr(self.server, "remembered_sessions", None) or {}
        digest = _session_digest(session_id)
        expiry = remembered.get(digest)
        if expiry is None:
            return False
        if expiry <= now:
            with _remember_lock(self.server):
                if remembered.pop(digest, None) is not None:
                    _save_remembered_sessions(self.server)
            return False
        return True

    def _browser_navigation(self, path):
        """True for a browser page navigation: a GET for a non-API page
        whose Accept header asks for HTML (#440). API paths always keep
        the JSON contract, whatever the Accept header says."""
        if path == "/api" or path.startswith("/api/"):
            return False
        accept = self.headers.get("Accept") or ""
        return "text/html" in accept.lower()

    def _authorized(self):
        """GET auth: access open, the header, or a login session."""
        if not self.server.access.required:
            return True
        return self._header_authorized() or self._session_authorized()

    def _field_values(self):
        return config_api.read_fields(environ=self.server.webui_environ)

    def _config_html(self, field_values, **kwargs):
        return pages.render_config_page(
            field_values, data=self.server.settings_data,
            form_token=self.server.form_token, **kwargs)

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path != "/healthz" and self._host_refused():
                return
            if path == "/healthz":
                # Unauthenticated by design: the container healthcheck
                # probes this route (it sends the header when it has the
                # token, but liveness must not depend on that).
                code, body = healthz(self.server.settings_data)
                self._json(code, body)
                return
            if self.server.access.setup_pending:
                # First-run setup (#708): nothing but the setup page until
                # the operator sets a password or chooses to leave it open.
                if path == "/setup":
                    self._send(200, pages.render_setup_page(self.server.setup_token,
                                                            theme=self._theme()),
                               "text/html; charset=utf-8")
                elif self._browser_navigation(path):
                    self._redirect("/setup")
                else:
                    self._setup_pending_json()
                return
            if path == "/setup":
                self._redirect("/")
                return
            if path == "/login":
                self._login_page()
                return
            if not self._authorized():
                if self._browser_navigation(path):
                    # A browser following the Unraid WebUI link (or any
                    # bookmark) sends Accept: text/html and cannot send
                    # the token header: send it to the login form
                    # instead of raw JSON (#440). API clients keep the
                    # JSON 401 below.
                    self._redirect("/login")
                    return
                self._deny_header()
                return
            if path == "/theme":
                self._theme_route()
            elif path == "/walkthrough":
                self._walkthrough_page()
            elif path == "/":
                if (self._browser_navigation(path)
                        and walkthrough_module.offer_once(self.server.settings_data)):
                    # First run (#813): a browser landing on the status
                    # page goes to the walkthrough until it is done —
                    # the setup gate's discipline, one step later. API
                    # callers and every other route are unaffected.
                    self._redirect("/walkthrough")
                    return
                snap = self.server.snapshot_func()
                self._send(200, render_status_html(
                    snap, form_token=self.server.form_token, theme=self._theme(),
                    walkthrough_pending=walkthrough_module.pending(self.server.settings_data),
                    job=_overall_job(self.server.settings_data),
                    nav=data_module.nav_for(self.server.settings_data)
                    if data_module.wrapped_enabled(self.server.settings_data) else None),
                    "text/html; charset=utf-8")
            elif path == "/api/status":
                self._json(200, self.server.snapshot_func())
            elif path in ("/data", "/wrapped", "/api/data", "/api/wrapped"):
                self._data_route(path)
            elif path == "/readyz":
                # Readiness per component (Spoolman, slicer, each printer),
                # behind the same auth as /api/status; /healthz stays the
                # unauthenticated liveness probe.
                ready = self.server.readiness_func() or {}
                self._json(200 if ready.get("ready", False) else 503, ready)
            elif path.startswith("/api/printers/"):
                self._printer_status_route(path[len("/api/printers/"):])
            elif path == "/spools":
                entries = settings.printers(self.server.settings_data)
                if len(entries) == 1:
                    # One printer: no picker step in the way (#875).
                    self._redirect("/spools/" + quote(str(entries[0]["id"]), safe=""))
                    return
                self._send(200, spools_module.render_spools_index(entries, theme=self._theme()),
                           "text/html; charset=utf-8")
            elif path == "/printers":
                self._printers_page()
            elif path.startswith("/spools/"):
                self._spools_page(unquote(path[len("/spools/"):]))
            elif path == "/config":
                self._send(200, self._config_html(self._field_values(), theme=self._theme()),
                           "text/html; charset=utf-8")
            elif path == "/designs":
                rows = designs_module.catalog_rows(self.server.settings_data)
                self._send(200, designs_module.render_designs_page(rows, theme=self._theme()),
                           "text/html; charset=utf-8")
            elif path.startswith("/designs/"):
                self._design_route(path[len("/designs/"):])
            elif path == "/layers" or path.startswith("/layers/"):
                archive = layers_module.archive_root(self.server.settings_data)
                rest = path[len("/layers/"):] if path.startswith("/layers/") else ""
                layer = (parse_qs(urlparse(self.path).query).get("layer") or ["0"])[0]
                code, content_type, body = layers_module.respond(
                    archive, rest, layer, theme=self._theme())
                if content_type.startswith("text/html"):
                    self._send(code, body, content_type)
                else:
                    self._json(code, body)
            elif self._browser_navigation(path):
                # A browser navigation to a path that does not exist
                # gets the themed refusal, not a JSON dump (#840).
                self._html_refusal(404, "No page lives at this address.", "/")
            else:
                self._json(404, {"ok": False,
                                 "reason": "try /, /api/status, /api/printers/<id>/status, "
                                           "/api/printers/<id>/camera/snapshot, "
                                           "/api/printers/<id>/spools, "
                                           "/healthz, /readyz, /config, /designs, /layers, /spools, "
                                           "/walkthrough or /login"})
        except Exception as exc:  # a broken snapshot is a 500, never a dropped connection
            if self._browser_navigation(path):
                # The page hides the exception text, so keep it in the log (log_message is silenced).
                log.exception("Web UI page %s failed", path)
                self._html_refusal(500, "Something went wrong rendering this page.", "/")
            else:
                self._json(500, {"ok": False, "reason": f"{type(exc).__name__}: {exc}"})

    # -- Data tab and Wrapped (#956) ---------------------------------------
    def _year_param(self):
        raw = (parse_qs(urlparse(self.path).query).get("year") or [None])[0]
        if raw is None or raw == "":
            return None, None
        try:
            return int(raw), None
        except ValueError:
            return None, (400, {"ok": False,
                                "reason": "year must be a whole number. Nothing shown."})

    def _data_route(self, path):
        """GET /data, /wrapped, /api/data and /api/wrapped (#956).

        Behind the same login as every other page. While
        [timelapse] wrapped_enabled is off, all four answer the named
        403 refusal from data.py, for pages and APIs alike.
        """
        year, error = self._year_param()
        if error:
            self._json(error[0], error[1])
            return
        data = self.server.settings_data
        if path == "/api/data":
            code, payload = data_module.data_view(data, year=year)
            self._json(code, payload)
            return
        if path == "/api/wrapped":
            code, payload = data_module.wrapped_view(data, year=year)
            self._json(code, payload)
            return
        if path == "/data":
            code, payload = data_module.data_view(data, year=year)
            if code != 200:
                self._json(code, payload)
                return
            self._send(200, data_module.render_data_page(data, payload, theme=self._theme()),
                       "text/html; charset=utf-8")
            return
        code, payload = data_module.wrapped_view(data, year=year)
        if code != 200:
            self._json(code, payload)
            return
        done = (parse_qs(urlparse(self.path).query).get("done") or [""])[0]
        self._send(200, data_module.render_wrapped_page(
            data, payload, form_token=self.server.form_token, theme=self._theme(),
            notice=done or None), "text/html; charset=utf-8")

    def _wrapped_video_post(self):
        """POST /wrapped/video (#956): the Wrapped page's video form.

        Authorized exactly like a Spools page form (the access-token
        header or the page's form token; a login session alone never
        authorizes it). The compile itself, its two toggles and every
        named refusal live in data.py / wrapped_video.py.
        """
        form, error = self._read_form()
        if error:
            self._html_refusal(error[0], error[1], "/wrapped")
            return
        if not self._post_authorized(form):
            self._html_refusal(403, STALE_FORM_MESSAGE, "/wrapped")
            return
        raw_year = (form.get("year") or [""])[0]
        try:
            year = int(raw_year)
        except (TypeError, ValueError):
            self._html_refusal(400, "year must be a whole number. Nothing was compiled.",
                               "/wrapped")
            return
        code, payload = data_module.make_video(self.server.settings_data, year)
        if code != 200:
            self._html_refusal(code, payload.get("reason", "the video compile failed"),
                               "/wrapped")
            return
        self._send(200, data_module.render_video_result(
            self.server.settings_data, payload, theme=self._theme()),
            "text/html; charset=utf-8")

    # -- per-printer live status (#664) -------------------------------------
    def _printer_status_route(self, rest):
        """GET /api/printers/<id>/status and /api/printers/<id>/camera/snapshot
        (#708): exactly one id segment plus a known tail; anything else
        under /api/printers/ is a 404 naming the routes that exist there."""
        segments = rest.split("/")
        if len(segments) == 2 and segments[0] and segments[1] == "status":
            code, payload = self.server.printer_status_func(unquote(segments[0]))
            self._json(code, payload)
            return
        if len(segments) == 2 and segments[0] and segments[1] == "spools":
            code, payload = self.server.spools_view_func(unquote(segments[0]))
            self._json(code, payload)
            return
        if len(segments) == 3 and segments[0] and segments[1:] == ["camera", "snapshot"]:
            code, body, content_type = self.server.camera_snapshot_func(unquote(segments[0]))
            if code != 200:
                self._json(code, body)
                return
            self._send(200, body, content_type, headers=[("Cache-Control", "no-store")])
            return
        self._json(404, {"ok": False,
                         "reason": "try /api/printers/<id>/status, "
                                   "/api/printers/<id>/spools or "
                                   "/api/printers/<id>/camera/snapshot"})

    # -- per-printer controls (#708, control endpoints phase) ----------------
    def _printer_control_route(self, rest):
        """POST /api/printers/<id>/pause|resume|cancel|temperature|target.

        Also the ha-print-handy #1 verbs: emergency_stop,
        firmware_restart, server_restart, gcode and exclude.

        Authorization is the access-token header only (or no token
        configured): a login-session cookie never authorizes a control
        POST, the same rule as the config POSTs. The body must be JSON,
        so a cross-site HTML form cannot post here. The off-by-default
        switch, cancel's confirm field and the temperature limits live
        in controls.py; a 404 for an unknown printer keeps the status
        route's shape.
        """
        if not self._header_authorized():
            self._deny_header()
            return
        segments = rest.split("/")
        verbs = (*controls_module.ACTIONS, "temperature", "target",
                 *controls_module.POWER_ACTIONS, "gcode", "exclude")
        if len(segments) != 2 or not segments[0] or segments[1] not in verbs:
            self._json(404, {"ok": False, "reason": "try /api/printers/<id>/pause, resume, "
                                                    "cancel, temperature, target, emergency_stop, "
                                                    "firmware_restart, server_restart, gcode "
                                                    "or exclude"})
            return
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > 4096:
            self._json(400, {"ok": False, "reason": "bad or oversized body. Nothing sent."})
            return
        raw = self.rfile.read(length) if length else b""
        if content_type != "application/json":
            self._json(415, {"ok": False, "reason": "control POSTs must be application/json. "
                                                    "Nothing sent."})
            return
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._json(400, {"ok": False, "reason": "body is not valid JSON. Nothing sent."})
            return
        printer_id, verb = unquote(segments[0]), segments[1]
        code, payload = self.server.printer_control_func(printer_id, verb, body)
        self._json(code, payload)

    # -- per-printer spools (#789) -------------------------------------------
    def _printer_spools_route(self, rest):
        """POST /api/printers/<id>/spools/<action> (and purchase/<step>).

        Authorization and body rules mirror the control route exactly:
        the access-token header only (a login-session cookie never
        authorizes a spool POST), a JSON body only, so a cross-site
        HTML form cannot post here. The controls_enabled guard, the
        named-printer rule and every refusal live in spools.py.
        """
        if not self._header_authorized():
            self._deny_header()
            return
        segments = rest.split("/")
        actions = (*spools_module.ACTIONS, "purchase/propose", "purchase/create")
        action = None
        if len(segments) == 3 and segments[0] and segments[1] == "spools" \
                and segments[2] in spools_module.ACTIONS:
            action = segments[2]
        elif len(segments) == 4 and segments[0] and segments[1] == "spools" \
                and segments[2] == "purchase" and segments[3] in ("propose", "create"):
            action = f"purchase/{segments[3]}"
        if action is None:
            self._json(404, {"ok": False, "reason": "try /api/printers/<id>/spools/"
                                                    + ", ".join(actions)})
            return
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > 65536:
            self._json(400, {"ok": False, "reason": "bad or oversized body. Nothing changed."})
            return
        raw = self.rfile.read(length) if length else b""
        if content_type != "application/json":
            self._json(415, {"ok": False, "reason": "spool POSTs must be application/json. "
                                                    "Nothing changed."})
            return
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._json(400, {"ok": False, "reason": "body is not valid JSON. Nothing changed."})
            return
        code, payload = self.server.spools_action_func(unquote(segments[0]), action, body)
        self._json(code, payload)

    def _spools_error_page(self, printer_id, code, reason):
        self._send(code, spools_module.render_spools_error(
            printer_id, code, reason, theme=self._theme()),
            "text/html; charset=utf-8")

    def _printers_page(self, notice=None, error=None, code=200):
        """GET /printers: the roster with actions and Add a printer (#875)."""
        done = (parse_qs(urlparse(self.path).query).get("done") or [""])[0]
        self._send(code, printers_page.render(
            settings.printers(self._walkthrough_data()), form_token=self.server.form_token,
            notice=notice or done or None, error=error, theme=self._theme()),
            "text/html; charset=utf-8")

    def _printers_post(self):
        """POST /printers: append one printer through the roster writer."""
        form, error = self._read_form()
        if error:
            self._html_refusal(error[0], error[1], "/printers")
            return
        if not self._post_authorized(form):
            self._html_refusal(403, STALE_FORM_MESSAGE, "/printers")
            return
        ok, message = printers_page.add(
            form, settings.printers(self._walkthrough_data()),
            environ=self.server.webui_environ)
        if ok:
            self._redirect("/printers?done=" + quote(message, safe=""))
        else:
            self._printers_page(error=message, code=400)

    def _spools_page(self, printer_id):
        """GET /spools/<id>: the per-printer Spools page (#789).

        A failed view (Spoolman down, unknown id) renders the themed
        error page at the same status code (#840); the JSON shape is
        kept for ``/api/printers/<id>/spools`` only. A ``?done=`` from
        a form post's redirect (#840's Post/Redirect/Get) reads back
        as the page notice.
        """
        if not printer_id or "/" in printer_id:
            self._spools_error_page(printer_id, 404,
                                    "That printer id does not name a spools page.")
            return
        code, view = self.server.spools_view_func(printer_id)
        if code != 200:
            self._spools_error_page(printer_id, code, view.get("reason", ""))
            return
        done = (parse_qs(urlparse(self.path).query).get("done") or [""])[0]
        self._send(200, spools_module.render_spools_page(
            view, form_token=self.server.form_token, theme=self._theme(),
            notice=done or None,
            purchase_ready=spools_module.purchase_available(),
            printers=settings.printers(self.server.settings_data)),
            "text/html; charset=utf-8")

    def _spools_page_post(self, printer_id):
        """POST /spools/<id>: one picker form from the Spools page.

        Same authorization as a config POST (the access-token header or
        the page's form token; a login session alone never authorizes
        it). The form converts to the same action the JSON API takes,
        so the page and the API can never diverge. Success answers
        with Post/Redirect/Get (#840): a 303 to the page with the
        action's detail in ``?done=``, so a reload never repeats the
        action. Refusals keep the direct render, so the error and the
        submitted values stay on screen, and ``purchase/propose`` keeps
        its direct render too — its proposed lines belong on this page
        and resubmitting it creates nothing.
        """
        back = f"/spools/{quote(printer_id, safe='')}"
        if not printer_id or "/" in printer_id:
            self._spools_error_page(printer_id, 404,
                                    "That printer id does not name a spools page.")
            return
        form, error = self._read_form()
        if error:
            self._html_refusal(error[0], error[1], back)
            return
        if not self._post_authorized(form):
            self._html_refusal(403, STALE_FORM_MESSAGE, back)
            return
        action_name = (form.get("spool_action") or [""])[0]
        action = None
        proposed = None
        try:
            action, body = spools_module.form_body(action_name, form)
        except spools_module.SpoolsError as refusal:
            code, payload = refusal.code, {"ok": False, "reason": refusal.reason}
        else:
            code, payload = self.server.spools_action_func(printer_id, action, body)
            if code == 200 and action == "purchase/propose":
                proposed = payload.get("lines")
        if code == 200 and action != "purchase/propose":
            self._redirect(f"{back}?done=" + quote(payload.get("detail", ""), safe=""))
            return
        view_code, view = self.server.spools_view_func(printer_id)
        if view_code != 200:
            self._spools_error_page(printer_id, view_code, view.get("reason", ""))
            return
        self._send(code if code != 200 else 200,
                   spools_module.render_spools_page(
                       view, form_token=self.server.form_token, theme=self._theme(),
                       notice=payload.get("detail") if code == 200 else None,
                       error=payload.get("reason") if code != 200 else None,
                       proposed=proposed,
                       purchase_ready=spools_module.purchase_available()),
                   "text/html; charset=utf-8")

    # -- designs catalog (read-only, #388 stage 3) --------------------------
    def _design_route(self, rest):
        """GET /designs/<id> (detail) and /designs/<id>/<kind> (a file).

        ``rest`` is the path after ``/designs/``: exactly one segment is
        a session id, exactly two is that session's ``scad`` / ``stl`` /
        ``preview`` / ``base`` file. Anything else — extra segments, ``..``, unknown
        ids or kinds — is a 404; the file itself is resolved by
        :func:`print_handy.core.design.session_file` from the session
        record, so nothing outside the session's own folder can be named.
        """
        segments = rest.split("/")
        data = self.server.settings_data
        if len(segments) == 1 and segments[0]:
            detail = designs_module.load_detail(data, segments[0])
            if detail is None:
                self._json(404, {"ok": False, "reason": "no such design session"})
                return
            session, scad_text, has_stl, has_preview, has_base = detail
            self._send(200, designs_module.render_design_detail(
                session, scad_text, has_stl=has_stl, has_preview=has_preview,
                has_base=has_base, theme=self._theme()),
                "text/html; charset=utf-8")
            return
        if len(segments) == 2 and segments[0] and segments[1] in designs_module.FILE_KINDS:
            found = designs_module.file_for(data, segments[0], segments[1])
            if found is None:
                self._json(404, {"ok": False, "reason": "no such design file"})
                return
            path, content_type, inline = found
            disposition = "inline" if inline else "attachment"
            self._send(200, path.read_bytes(), content_type,
                       headers=[("Content-Disposition",
                                 f'{disposition}; filename="{path.name}"')])
            return
        self._json(404, {"ok": False, "reason": "no such design session"})

    # -- theme toggle (#813) --------------------------------------------------
    def _theme_route(self):
        """GET /theme?set=dark|light&back=<path>: remember the theme in
        the ph-theme cookie and go back. Behind the same authorization
        as every other page (and unreachable pre-setup, like them). The
        ``back`` target must be a local path — anything else lands on
        the status page, so the toggle is never an open redirect."""
        query = parse_qs(urlparse(self.path).query)
        theme = (query.get("set") or [""])[0]
        if theme not in shell.THEMES:
            self._json(400, {"ok": False, "reason": "set must be dark or light"})
            return
        back = (query.get("back") or ["/"])[0]
        if not back.startswith("/") or back.startswith("//") \
                or any(ord(char) < 0x20 or ord(char) == 0x7F for char in back):
            # Not a local path, or it carries control characters: a decoded
            # CR/LF would otherwise land in the Location header verbatim
            # and inject response headers of the caller's choosing.
            back = "/"
        self._redirect(back, headers=[("Set-Cookie", shell.theme_cookie_header(theme, secure=self._cookie_secure()))])

    def _rotate_form_token(self):
        """Replace the config form token after an access-rule change.

        Pages served under the old rule handed this token to anyone who
        could reach the Web UI then, and _post_authorized accepts it on
        its own — if it survived the change, a retained copy would keep
        authorizing protected writes under the new rule, including
        replacing or removing the login that was just set. The browser
        that made the change is signed in with a session, and every
        page it loads next carries the new token."""
        self.server.form_token = secrets.token_urlsafe(32)

    def _revoke_remembered_sessions(self):
        """Revoke every remembered session and delete its store file.

        Called only where the login rule itself was just set, changed or
        removed (the setup submit and the walkthrough Login step, beside
        their _rotate_form_token calls): a session minted under the old
        credential must not outlive it. Ephemeral sessions are untouched
        — they die with the process, as before."""
        with _remember_lock(self.server):
            remembered = getattr(self.server, "remembered_sessions", None)
            if remembered is not None:
                remembered.clear()
            try:
                sessions_path(self.server.settings_data).unlink(missing_ok=True)
            except OSError as exc:
                log.warning("webui: could not delete remembered sessions file: %s", exc)
                _save_remembered_sessions(self.server)

    # -- first-run walkthrough (#813) -----------------------------------------
    def _walkthrough_data(self):
        """The current effective settings for a walkthrough render or
        POST: reloaded rather than the server's startup snapshot, so a
        step that just saved a new printer model renders the new model's
        fields when the operator comes Back to it — field visibility is
        model-driven, and the startup data still names the old model."""
        return settings.load(environ=self.server.webui_environ)

    def _walkthrough_page(self):
        """GET /walkthrough: the current step, or / when this install
        has no walkthrough pending (finished, skipped, or never owed
        one — token, loopback and require_auth=false installs)."""
        data = self._walkthrough_data()
        if not walkthrough_module.pending(data):
            self._redirect("/")
            return
        self._send(200, walkthrough_module.render_page(
            data, self._field_values(),
            form_token=self.server.form_token, theme=self._theme(),
            environ=self.server.webui_environ), "text/html; charset=utf-8")

    def _walkthrough_post(self):
        """POST /walkthrough: one step's action, authorized exactly like
        a config POST (access header or the page's form token)."""
        form, error = self._read_form()
        if error:
            self._html_refusal(error[0], error[1], "/walkthrough")
            return
        if not self._post_authorized(form):
            self._html_refusal(403, STALE_FORM_MESSAGE, "/walkthrough")
            return
        kind, payload = walkthrough_module.apply_post(
            self._walkthrough_data(), self.server.webui_environ, form,
            self._field_values(), form_token=self.server.form_token,
            theme=self._theme())
        if kind == "access":
            # The Login step saved a new rule: apply it now, and keep the
            # person who chose it signed in on this browser.
            self.server.access = access_module.resolve(
                self.server.settings_data, self.server.bind_host, self.server.webui_environ)
            self._rotate_form_token()
            self._revoke_remembered_sessions()
            self._redirect(payload, headers=[self._start_session()]
                           if self.server.access.required else [])
        elif kind == "redirect":
            self._redirect(payload)
        else:
            self._send(200, payload, "text/html; charset=utf-8")

    # -- browser login ------------------------------------------------------
    def _login_page(self):
        if not self.server.access.required:
            # Access is open: nothing to log in to (loopback posture).
            self._redirect("/")
            return
        self._send(200, pages.render_login_page(password=self._password_mode(),
                                                username=self._username_mode(),
                                                theme=self._theme()),
                   "text/html; charset=utf-8")

    def _password_mode(self):
        return self.server.access.mode == access_module.PASSWORD

    def _username_mode(self):
        return self._password_mode() and bool(self.server.access.username)

    def _cookie_secure(self):
        secure = getattr(self.server, "cookie_secure", None)
        if secure is not None:
            return bool(secure)
        data = getattr(self.server, "settings_data", None) or {}
        return bool(data.get("webui", {}).get("cookie_secure", False))

    def _start_session(self, remember=False):
        """A new login-session cookie header for this browser.

        With ``remember`` the session lasts REMEMBER_TTL_S, the cookie
        gains a matching Max-Age, and only the id's SHA-256 hash is
        persisted, so the session survives a server restart. When that
        write fails (a read-only state mount, say) the login still
        succeeds with a plain ephemeral session, as before."""
        now = time.time()
        sessions = self.server.sessions
        for stale in [sid for sid, expiry in sessions.items() if expiry <= now]:
            del sessions[stale]
        session_id = secrets.token_urlsafe(32)
        flags = "; Secure" if self._cookie_secure() else ""
        if remember:
            remembered = getattr(self.server, "remembered_sessions", None)
            if remembered is None:
                remembered = self.server.remembered_sessions = {}
            # Mutation through persistence under the shared lock, so a
            # concurrent logout or revocation is never overwritten by
            # this mint's snapshot (and this mint is never lost to one).
            with _remember_lock(self.server):
                for stale in [d for d, expiry in remembered.items() if expiry <= now]:
                    del remembered[stale]
                remembered[_session_digest(session_id)] = now + REMEMBER_TTL_S
                if _save_remembered_sessions(self.server):
                    return ("Set-Cookie", f"{SESSION_COOKIE}={session_id}; HttpOnly; "
                            f"SameSite=Strict; Path=/; Max-Age={REMEMBER_TTL_S}{flags}")
                # Fail open to the current behavior: ephemeral, no Max-Age.
                del remembered[_session_digest(session_id)]
        sessions[session_id] = now + SESSION_TTL_S
        return ("Set-Cookie", f"{SESSION_COOKIE}={session_id}; HttpOnly; SameSite=Strict; Path=/{flags}")

    def _login_submit(self):
        form, error = self._read_form()
        if error:
            self._json(error[0], {"ok": False, "reason": error[1]})
            return
        if not self.server.access.required:
            self._redirect("/")
            return
        provided = (form.get(pages.LOGIN_TOKEN_FIELD) or [""])[0]
        username = (form.get(pages.LOGIN_USER_FIELD) or [""])[0]
        # Failed-login backoff: a locked-out address is refused before any
        # hashing, so a flood costs the server nothing and guessing stalls.
        address = self.client_address[0]
        throttle = self.server.login_throttle
        wait = throttle.retry_after(address)
        if wait:
            self._send(429, pages.render_login_page(
                error=f"Too many failed attempts. Try again in {wait} seconds.",
                password=self._password_mode(), username=self._username_mode(),
                theme=self._theme()),
                "text/html; charset=utf-8", headers=[("Retry-After", str(wait))])
            return
        # The submitted credentials are compared, never logged or reflected.
        if not self.server.access.matches_login(username, provided):
            throttle.record_failure(address)
            what = ("username or password" if self._username_mode()
                    else "password" if self._password_mode() else "token")
            self._send(401, pages.render_login_page(
                error=f"That {what} was not accepted. Try again.",
                password=self._password_mode(), username=self._username_mode(),
                theme=self._theme()),
                "text/html; charset=utf-8")
            return
        throttle.record_success(address)
        remember = (form.get(pages.LOGIN_REMEMBER_FIELD) or [""])[0].strip().lower() \
            in _REMEMBER_TRUTHY
        self._redirect("/", headers=[self._start_session(remember=remember)])

    # -- first-run setup (#708) ---------------------------------------------
    def _setup_pending_json(self):
        self._json(503, {"ok": False,
                         "reason": "first-run setup is not finished: open /setup in a "
                                   "browser to set a Web UI password or leave it open"})

    def _setup_submit(self):
        """POST /setup: save a password or the affirmative open choice.

        Needs the setup page's own token, so a cross-origin page cannot
        answer setup for the operator. That token is separate from the
        config form token and authorizes nothing else: the setup page is
        served to anyone before setup, so its token must never open a
        config or debug write once a password protects the Web UI. Only the first completed setup counts;
        later posts (setup already done) just go to the status page.
        """
        form, error = self._read_form()
        if error:
            self._html_refusal(error[0], error[1], "/setup")
            return
        provided = (form.get(pages.CSRF_FIELD) or [""])[0]
        if not (provided and hmac.compare_digest(provided, self.server.setup_token)):
            self._html_refusal(403, STALE_FORM_MESSAGE, "/setup")
            return
        data = self.server.settings_data
        with self.server.setup_lock:
            if not self.server.access.setup_pending:
                self._redirect("/")
                return
            choice = (form.get(pages.SETUP_CHOICE_FIELD) or [""])[0]
            try:
                if choice == "password":
                    access_module.save_password(
                        data, (form.get(pages.SETUP_PASSWORD_FIELD) or [""])[0],
                        (form.get(pages.SETUP_CONFIRM_FIELD) or [""])[0],
                        username=(form.get(pages.SETUP_USER_FIELD) or [""])[0])
                elif choice == "open":
                    access_module.save_open(data, (form.get(pages.SETUP_OPEN_FIELD) or [""])[0] == "yes")
                else:
                    raise access_module.AccessError("Choose one of the two options.")
            except access_module.AccessError as exc:
                self._send(400, pages.render_setup_page(self.server.setup_token,
                                                        error=str(exc), theme=self._theme()),
                           "text/html; charset=utf-8")
                return
            self.server.access = access_module.resolve(data, self.server.bind_host,
                                                       self.server.webui_environ)
            self._rotate_form_token()
            self._revoke_remembered_sessions()
        # The person who just chose the password is signed in on this browser.
        self._redirect("/", headers=[self._start_session()] if self.server.access.required else [])

    def _logout(self):
        # Ends only the caller's own session, so no form token is needed:
        # the worst a cross-origin post can do is sign the operator out.
        session_id = self._session_id()
        if session_id:
            self.server.sessions.pop(session_id, None)
            remembered = getattr(self.server, "remembered_sessions", None) or {}
            with _remember_lock(self.server):
                if remembered.pop(_session_digest(session_id), None) is not None:
                    _save_remembered_sessions(self.server)
        flags = "; Secure" if self._cookie_secure() else ""
        cookie = (f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; "
                  f"Path=/; Max-Age=0{flags}")
        self._redirect("/login", headers=[("Set-Cookie", cookie)])

    # -- config writes (stage 3) -------------------------------------------
    def _read_form(self):
        """(form, None) or (None, (code, reason)) for a urlencoded POST body."""
        content_type = self.headers.get("Content-Type") or ""
        if content_type and not content_type.startswith("application/x-www-form-urlencoded"):
            return None, (415, "POST bodies must be application/x-www-form-urlencoded")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, (400, "bad Content-Length")
        if length < 0 or length > 65536:
            return None, (413, "form body too large")
        raw = self.rfile.read(length) if length else b""
        return parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True), None

    def _post_authorized(self, form):
        """A config POST needs the access-token header (stage 2's rule) or
        the form token embedded in the page. With no access token set the
        header authorizes nothing, so the form token is always required
        then — a cross-origin page cannot mint one. A login session
        cookie deliberately does NOT authorise a POST on its own: the
        form token is the CSRF guard, and a cookie-authenticated user
        reaches it through GET /config like any browser."""
        if self.server.access.required and self._header_authorized():
            return True
        provided = (form.get(pages.CSRF_FIELD) or [""])[0]
        return bool(provided) and hmac.compare_digest(provided, self.server.form_token)

    def _config_form(self):
        """(form, field_values) for an authorized config POST, else None
        (the refusal response has already been sent)."""
        form, error = self._read_form()
        if error:
            self._html_refusal(error[0], error[1], "/config")
            return None
        if not self._post_authorized(form):
            self._html_refusal(403, STALE_FORM_MESSAGE, "/config")
            return None
        return form, self._field_values()

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if self._host_refused(post=True):
                return
            if path == "/setup":
                self._setup_submit()
                return
            if self.server.access.setup_pending:
                self._setup_pending_json()
                return
            if path == "/login":
                self._login_submit()
            elif path == "/logout":
                self._logout()
            elif path == "/walkthrough":
                self._walkthrough_post()
            elif path == "/wrapped/video":
                self._wrapped_video_post()
            elif path == "/config/preview":
                self._config_preview()
            elif path == "/config":
                self._config_save()
            elif path == "/debug":
                self._debug_post()
            elif path == "/printers":
                self._printers_post()
            elif path.startswith("/spools/"):
                self._spools_page_post(unquote(path[len("/spools/"):]))
            elif path.startswith("/api/printers/"):
                rest = path[len("/api/printers/"):]
                segments = rest.split("/")
                if len(segments) >= 2 and segments[1] == "spools":
                    self._printer_spools_route(rest)
                else:
                    self._printer_control_route(rest)
            else:
                self._read_only()
        except Exception as exc:  # a broken write is a 500, never a dropped connection
            self._json(500, {"ok": False, "reason": f"{type(exc).__name__}: {exc}"})

    def _config_preview(self):
        got = self._config_form()
        if got is None:
            return
        form, field_values = got
        changes, submitted, errors = pages.parse_form(form, field_values)
        if not errors and changes:
            field_errors, general = pages.validate_change_set(
                changes, environ=self.server.webui_environ)
            errors.update(field_errors)
            if general:
                errors["_general"] = general
        if errors:
            self._send(400, self._config_html(field_values, errors=errors,
                                              submitted=submitted, theme=self._theme()),
                       "text/html; charset=utf-8")
            return
        if not changes:
            self._send(200, self._config_html(
                field_values, submitted=submitted, theme=self._theme(),
                notice="No changes: every submitted value already matches "
                       "the effective settings."), "text/html; charset=utf-8")
            return
        effective = {fv.field: fv.value for fv in field_values}
        entries = [(field, effective[field], value) for field, value in changes.items()]
        self._send(200, pages.render_preview_page(
            entries, submitted, form_token=self.server.form_token, theme=self._theme()),
            "text/html; charset=utf-8")

    def _config_save(self):
        got = self._config_form()
        if got is None:
            return
        form, field_values = got
        if (form.get(pages.CONFIRM_FIELD) or [""])[0] != "yes":
            self._html_refusal(400, "saving needs the preview step: POST to "
                                    "/config/preview first, then confirm", "/config")
            return
        # The confirm form names the fields the preview parsed; re-parse
        # exactly those, so hidden-input absence keeps its preview meaning.
        named = (form.get(pages.FIELDS_FIELD) or [""])[0]
        only = {part for part in named.split(",") if part} or None
        changes, submitted, errors = pages.parse_form(form, field_values, only=only)
        if errors:
            self._send(400, self._config_html(field_values, errors=errors,
                                              submitted=submitted, theme=self._theme()),
                       "text/html; charset=utf-8")
            return
        try:
            changed = config_api.write_changes(
                {field.id: value for field, value in changes.items()},
                environ=self.server.webui_environ)
        except config_api.WebUIConfigError as exc:
            field_errors, general = pages.attribute_error(
                str(exc), changes, environ=self.server.webui_environ)
            errors = dict(field_errors)
            if general:
                errors["_general"] = general
            self._send(400, self._config_html(field_values, errors=errors,
                                              submitted=submitted, theme=self._theme()),
                       "text/html; charset=utf-8")
            return
        self._send(200, pages.render_result_page(changed, theme=self._theme()),
                   "text/html; charset=utf-8")

    def _debug_post(self):
        """POST /debug (#478): turn debug log mode on or off.

        Same authorization as a config POST (access-token header or the
        page's form token). The mode is shared state in the state folder,
        so this one write reaches the bot and every listener process;
        nothing about printing changes.
        """
        form, error = self._read_form()
        if error:
            self._html_refusal(error[0], error[1], "/")
            return
        if not self._post_authorized(form):
            self._html_refusal(403, STALE_FORM_MESSAGE, "/")
            return
        from .. import debug_mode
        action = (form.get("action") or [""])[0]
        data = self.server.settings_data
        if action == "on":
            raw = (form.get("minutes") or [""])[0].strip()
            try:
                minutes = int(raw) if raw else None
            except ValueError:
                self._debug_error("minutes must be a whole number")
                return
            debug_mode.start(data, minutes=minutes)
        elif action == "off":
            debug_mode.end(data)
        else:
            self._debug_error("action must be on or off")
            return
        self._redirect("/")

    def _debug_error(self, message):
        """A /debug validation refusal (#840): the status page itself
        re-rendered at 400 with the error on it, so the debug form and
        its current state stay on screen instead of a JSON dump."""
        snap = self.server.snapshot_func()
        self._send(400, render_status_html(
            snap, form_token=self.server.form_token, theme=self._theme(),
            walkthrough_pending=walkthrough_module.pending(self.server.settings_data),
            job=_overall_job(self.server.settings_data), error=message),
            "text/html; charset=utf-8")

    def _read_only(self):
        self._json(405, {"ok": False,
                         "reason": "the Web UI is read-only here (GET only; "
                                   "configuration posts go to /config)"})

    do_PUT = _read_only
    do_DELETE = _read_only
    do_PATCH = _read_only
    do_HEAD = _read_only
    do_OPTIONS = _read_only


def make_server(data=None, *, environ=None, host=None, port=None, snapshot_func=None,
                printer_status_func=None, printer_control_func=None,
                camera_snapshot_func=None, spools_view_func=None, spools_action_func=None,
                readiness_func=None):
    """A bound (not yet serving) ThreadingHTTPServer for the Web UI.

    ``host``/``port`` override the settings (tests bind port 0 for an
    ephemeral loopback port; the access-control rules then come from the
    *effective* bind host). Raises WebUIServerError when the start is
    refused, before any socket is bound.
    """
    environ = os.environ if environ is None else environ
    data = settings.load(environ=environ) if data is None else data
    cfg_host, cfg_port, _token = check_start(data, environ)
    bind_host = cfg_host if host is None else host
    bind_port = cfg_port if port is None else int(port)
    server = ThreadingHTTPServer((bind_host, bind_port), _Handler)
    server.settings_data = data
    setattr(server, "cookie_secure", bool((data or {}).get("webui", {}).get("cookie_secure", False)))
    server.access = access_module.resolve(data, bind_host, environ)
    server.bind_host = bind_host
    server.setup_lock = threading.Lock()
    # DNS-rebinding guard (see _Handler._host_refused): only an open
    # loopback bind relies on the form token alone, so only it checks Host.
    server.allowed_hosts = ({"localhost", "127.0.0.1", "::1", str(bind_host).lower()}
                            if is_loopback(bind_host) and not server.access.required else None)
    # The config page reads and writes through the same environment the
    # settings were loaded with (PRINT_HANDY_CONFIG resolution included).
    server.webui_environ = environ
    # Per-server form token for config POSTs (pages.CSRF_FIELD): generated
    # here, kept only in memory, never the access token.
    server.form_token = secrets.token_urlsafe(32)
    # First-run setup's own CSRF token (#708): shown to unauthenticated
    # visitors on /setup, so it is never the config form token above.
    server.setup_token = secrets.token_urlsafe(32)
    # Browser login sessions: {session id: expiry epoch}. Memory only —
    # a restart signs these out; nothing about them is persisted.
    server.sessions = {}
    # Remembered sessions: {sha256(session id): expiry epoch}, loaded
    # from webui_sessions.json in the state folder (expired entries
    # pruned on load, and the whole store discarded when the credential
    # fingerprint it was minted under no longer matches) and re-saved
    # on every change, so a browser that ticked "Keep me signed in"
    # survives a restart. The raw id is never written to disk.
    # Mutations and persistence are serialized with remembered_lock.
    server.remembered_lock = threading.RLock()
    server.remembered_sessions = _load_remembered_sessions(
        data, server.access.credential_fingerprint())
    server.login_throttle = access_module.LoginThrottle()
    server.snapshot_func = snapshot_func or (
        lambda: status_module.snapshot(data, environ=environ))
    # /readyz builds its components on their own (no doctor run); a
    # caller-supplied snapshot_func still drives it when no readiness_func
    # is given, so embedders and tests keep one source.
    server.readiness_func = readiness_func or (
        (lambda: (snapshot_func() or {}).get("components") or {}) if snapshot_func
        else (lambda: status_module.readiness(data, environ=environ)))
    server.printer_status_func = printer_status_func or (
        lambda printer_id: status_module.printer_status(data, printer_id))
    server.printer_control_func = printer_control_func or (
        lambda printer_id, verb, body: _printer_control(data, printer_id, verb, body))
    server.camera_snapshot_func = camera_snapshot_func or (
        lambda printer_id: camera_module.snapshot(data, printer_id))
    server.spools_view_func = spools_view_func or (
        lambda printer_id: spools_module.spools_view(data, printer_id))
    server.spools_action_func = spools_action_func or (
        lambda printer_id, action, body: spools_module.spool_action(data, printer_id, action, body))
    return server


def _printer_control(data, printer_id, verb, body):
    if verb == "temperature":
        return controls_module.set_temperature(data, printer_id, body)
    if verb == "target":
        return controls_module.set_targets(data, printer_id, body)
    if verb in controls_module.POWER_ACTIONS:
        return controls_module.power_action(data, printer_id, verb, body)
    if verb == "gcode":
        return controls_module.send_gcode(data, printer_id, body)
    if verb == "exclude":
        return controls_module.exclude_object(data, printer_id, body)
    return controls_module.control(data, printer_id, verb, body)


def serve(data=None, *, environ=None):
    """Run the server until interrupted; returns a process exit code."""
    try:
        server = make_server(data, environ=environ)
    except WebUIServerError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    host, port = server.server_address[:2]
    print(f"webui: server on http://{host}:{port}/ (status page; "
          f"configuration at /config, Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv=None):
    """``python3 -m print_handy.webui``: load settings and serve (stage 2)."""
    try:
        data = settings.load()
    except settings.SettingsError as exc:
        print(f"webui: configuration unresolved: {exc}", file=sys.stderr)
        return 2
    return serve(data)


if __name__ == "__main__":
    sys.exit(main())
