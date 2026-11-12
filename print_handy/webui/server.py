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
- ``GET /api/printers/<id>/screen/snapshot`` — one JPEG of that
  printer's own display (:mod:`print_handy.webui.screen`, #1303),
  discovered through Moonraker's registered ``screen`` webcam and
  never the printer's address; ``GET /printers/<id>/screen`` is its
  labeled, read-only page;
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
  ``GET /api/wrapped`` — the Data tab and Pasta Wrapped (#956,
  :mod:`print_handy.webui.data`), behind ``[timelapse]
  wrapped_enabled``; ``POST /wrapped/video`` compiles the Wrapped
  video from the Wrapped page's form under the same form-token rule,
  additionally behind ``[timelapse] wrapped_video``;
  ``POST /wrapped/share`` exports the Wrapped static share bundle
  (#981) from the Wrapped page's form under the same form-token rule
  and the same ``wrapped_enabled`` gate;
- ``GET /healthz`` — the bot heartbeat evaluation, with the same
  200/503 + JSON shape as the standalone bot's own health server;
- ``GET /config`` — the picker configuration page
  (:mod:`print_handy.webui.pages`), where settings are edited.
- ``GET /designs`` and ``GET /designs/<id>`` — the read-only catalog of
  AI design sessions (#388, stage 3;
  :mod:`print_handy.webui.designs`), with per-session file downloads
  under ``GET /designs/<id>/<scad|stl|preview>``.
- ``GET /projects`` and ``GET /api/projects`` — the read-only
  multi-part project readout (#1204; :mod:`print_handy.webui.projects`),
  read from each job's ``project_v1`` manifest and the print records.
- ``GET /layers`` and ``GET /layers/<job>/<file>`` — a read-only view of
  one sliced plate (#624; :mod:`print_handy.webui.layers`). No print,
  pause, or exclude control, and nothing here changes chat confirm.
- ``GET /walkthrough`` and ``POST /walkthrough`` — the first-run
  walkthrough (#813; :mod:`print_handy.webui.walkthrough`), offered to
  every install that has not finished it; its steps write through the
  config path above, and its Login step is where a password is set.
- ``GET /theme?set=dark|light&back=<path>`` — the header theme toggle
  (#813; :mod:`print_handy.webui.shell`): sets the ``ph-theme`` cookie
  and redirects back. A browser preference only, behind the same
  authorization as every other page.

Every HTML page renders through the one shell
(:mod:`print_handy.webui.shell`): one inline stylesheet, the header
chrome, and the dark-by-default theme.

Unknown GET paths are 404. Writes go only through the POST routes
``do_POST`` names: the config page (``POST /config/preview`` renders the
diff and writes nothing; ``POST /config`` with the confirm field
performs the validated write), the walkthrough's ``POST /walkthrough``
(the same validated path), ``POST /printers`` (one roster printer),
``POST /spools/<id>``, ``POST /debug`` (debug log mode), the web chat
(``POST /chat`` and ``POST /chat/choice``), uploads (``POST /upload``
and ``POST /u/<link>``), the Wrapped exports (``POST /wrapped/video``
and ``POST /wrapped/share``), sign-in and sign-out (``POST /login``,
``POST /logout``), and the header-only control API under
``POST /api/printers/<id>/...``. Every form POST passes the form-token
rule below (``_post_authorized``), with three exceptions: ``/login``
checks the credential itself, ``/logout`` ends only the caller's own
session, and ``/u/<link>`` is authorized by its upload link. The control
API takes the header only. A POST to any other path, and every
PUT, DELETE, PATCH or OPTIONS, is 405. A request that fails with an
unexpected error gets a 500 naming only a request id; the log line with
the traceback carries the same id, never the response.

The server starts only when ``[webui] enabled`` is true and ``port`` is
above 0. Access control is decided by :mod:`print_handy.webui.access`:
the access token in the environment variable ``[webui] access_token_env``
names (default ``WEBUI_ACCESS_TOKEN``), or the password chosen in the
walkthrough's Login step, is sent as the ``X-WebUI-Token`` header and
compared in constant time. ``[webui] require_auth`` false keeps its
meaning: no credential at all. A bind with neither a token nor a saved
choice serves openly and is *unclaimed* — the walkthrough is offered
from the first request, and a credential exists only once one is
deliberately set (the #708 setup gate is reversed). The token or
password is never rendered, logged, or included in any response.

Form POSTs carry ``pages.CSRF_FIELD``. While access is open the
per-server form token (generated at start, memory only, never the
access token) is that guard on its own, so a cross-origin page cannot
change settings. On a claimed install the form token is not a
credential: a POST needs the header, or a live session plus the
``_csrf`` value bound to that session (``session_form_token``).

Browser login: a browser cannot send the ``X-WebUI-Token`` header, so
``GET /login`` (reachable without auth — it is the entry point) renders
the sign-in form whenever a credential is required: a username and
password for a login saved in the walkthrough (a single password field
for one saved before usernames existed), or a single field for an
access token from the environment, which is also accepted with any
username as the way back in after a forgotten password. ``POST /login``
checks the pair (``Access.matches_login``) under the per-address
failed-attempt throttle and trades a correct one for a session cookie
(``SESSION_COOKIE``, HttpOnly, SameSite=Strict). A password saved at an
older PBKDF2 cost is re-hashed at the current one after a successful
sign-in, on the form or with the header, signing nobody out. An
unauthenticated browser navigation (a GET for a
non-API page with ``Accept: text/html``) redirects to ``/login``
instead of returning the JSON 401 (#440); API requests keep the JSON
401. A valid, unexpired session cookie authorises GETs
exactly like the header; it never authorises a form POST on its own
(those also need the session-bound form token, so login cannot be
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
import re
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
from . import chat as chat_module
from . import config as config_api
from . import controls as controls_module
from . import data as data_module
from . import designs as designs_module
from . import layers as layers_module
from . import pages
from . import printers_page
from . import projects as projects_module
from . import routes as routes_module
from . import screen as screen_module
from . import shell
from . import sources as sources_module
from . import spools as spools_module
from . import status as status_module
from . import upload as upload_module
from . import walkthrough as walkthrough_module

log = logging.getLogger(__name__)

TOKEN_HEADER = "X-WebUI-Token"
# Sent on every response. No Web UI route is meant to be framed (the
# Discord Activity is its own server, print_handy.live_activity), so
# framing is refused everywhere.
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", "frame-ancestors 'none'"),
)
# Per-connection socket timeout (seconds without progress on a read or
# write) and the most requests handled at once: a slow or stalled
# client cannot hold a thread forever, nor spawn threads without bound.
REQUEST_TIMEOUT_S = 30
MAX_HANDLER_THREADS = 64
INTERNAL_ERROR_REASON = "internal error; the Web UI log has the details"
# A refused upload is answered before its body is read. Closing a socket
# that still holds unread bytes makes TCP send a reset, which a browser
# can show instead of the answer, so after the answer is sent up to
# DRAIN_LIMIT_BYTES of the unread body are read and dropped (for at most
# DRAIN_TIMEOUT_S) before the connection closes. Nothing drained is
# parsed or kept. A larger body still ends in a reset (docs/WEBUI.md).
DRAIN_LIMIT_BYTES = 4 * 1024 * 1024
DRAIN_TIMEOUT_S = 3.0
LOOPBACK_NAMES = frozenset({"localhost"})
# Browser login sessions: the cookie carries only an opaque id; the
# id -> expiry map lives in server memory (see make_server), so nothing
# is persisted and a restart signs everyone out. The Secure flag is
# off by default for plain HTTP on a trusted LAN (Unraid WebUI link);
# set [webui] cookie_secure = true when exposed via an HTTPS reverse proxy.
SESSION_COOKIE = "webui_session"
# The web chat's per-browser cookie on an install with no login (#1145):
# a random id that scopes this browser's chat transcript. With a login,
# the login session scopes it instead and this cookie is not used.
CHAT_COOKIE = "webui_chat"
_CHAT_COOKIE_VALUE = re.compile(r"[A-Za-z0-9_-]{32,64}")
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


def _access_stamp(data):
    """A change marker for the saved login file, or None when there is none."""
    try:
        return access_module.access_path(data).stat().st_mtime_ns
    except Exception:
        return None


def session_form_token(server, session_id):
    """The ``_csrf`` value bound to one login session on ``server``: an
    HMAC-SHA256 of the session id under the server's in-memory secret
    (rotated with every access-rule change)."""
    secret = getattr(server, "csrf_secret", None)
    if secret is None:
        secret = server.csrf_secret = secrets.token_bytes(32)
    return hmac.new(secret, session_id.encode("utf-8"), hashlib.sha256).hexdigest()


_BROKEN_LOG_LOCK = threading.Lock()


def _log_broken_access(server):
    """Log a broken login file once per distinct problem (not per request)."""
    access = getattr(server, "access", None)
    if not getattr(access, "broken", False):
        return
    key = (access.problem, access._broken_path)
    with _BROKEN_LOG_LOCK:
        if getattr(server, "broken_access_logged", None) == key:
            return
        server.broken_access_logged = key
    log.error("webui: refusing all requests: %s (file: %s)", access.problem,
              access._broken_path)


def _remember_lock(server):
    """The server's remembered-session lock, or a no-op context when the
    server object predates it (the mutation sites stay safe either way)."""
    lock = getattr(server, "remembered_lock", None)
    return lock if lock is not None else contextlib.nullcontext()


def _claim_lock(server):
    """The server's walkthrough-write lock (serializes login claims), or a
    no-op context when the server object predates it."""
    lock = getattr(server, "claim_lock", None)
    return lock if lock is not None else contextlib.nullcontext()


def _chat_lock(server):
    """The server's chat-service build lock, or a no-op context when the
    server object predates it (same rule as _remember_lock)."""
    lock = getattr(server, "chat_service_lock", None)
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
# The answer for a no-login upload link that is unknown, expired or revoked.
INVALID_LINK_MESSAGE = "This upload link is not valid. Ask for a fresh one."


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


def allowed_hosts(bind_host, access):
    """The Host names an open loopback bind answers to, or None.

    The DNS-rebinding guard (_Handler._host_refused): only an open
    loopback bind relies on the form token alone, so only it checks Host
    against this list. Computed from the access in force on every
    request, never once at start: removing a claim on a loopback bind
    must turn the Host check back on at once."""
    if is_loopback(bind_host) and not access.required:
        return {"localhost", "127.0.0.1", "::1", str(bind_host).lower()}
    return None


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

    A bind with no token and no saved choice starts open and unclaimed
    (:mod:`print_handy.webui.access`): the full Web UI serves from the
    first request and the walkthrough is offered.
    """
    webui = data["webui"]
    if not webui["enabled"]:
        raise WebUIServerError(
            "webui: not started: [webui] enabled is false (the Web UI is "
            "on by default; this install turned it off — set enabled = true and a port to run it)")
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
                       credential_ignored=False):
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
    body = ((f'<p class="error">{esc(error)}</p>' if error else "")
            + overall
            + f'<table class="rows">{main_rows}</table>'
            + roster_table
            + (f"<h2>Services</h2><table>{services}</table>" if services else "")
            + f"<h2>Doctor checks</h2><table>{doctor}</table>"
            + failures_table
            + debug_section)
    return shell.page("status", body, path="/", theme=theme, banner=banner, nav=nav)


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with at most ``max_threads`` requests in flight.

    The accept loop waits for a free slot instead of starting a new
    thread per connection without bound; with the handler's socket
    timeout, a stalled client gives its slot back."""

    max_threads = MAX_HANDLER_THREADS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(self.max_threads)

    def process_request(self, request, client_address):
        self._slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def _request_id():
    """A short random id for one failed request: shown in the 500
    response and written in the log line with the traceback, so a
    reported error can be matched to its log entry. It names nothing
    about the request."""
    return secrets.token_hex(6)


def _drain(sock, rfile, unread):
    """Read and drop up to DRAIN_LIMIT_BYTES of ``unread`` body bytes,
    giving up after DRAIN_TIMEOUT_S or at end of stream."""
    budget = min(max(0, int(unread)), DRAIN_LIMIT_BYTES)
    deadline = time.monotonic() + DRAIN_TIMEOUT_S
    while budget > 0:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        sock.settimeout(left)
        chunk = rfile.read1(min(65536, budget))
        if not chunk:
            return
        budget -= len(chunk)


class _CountingReader:
    """Wraps the request stream to count the body bytes a parser read,
    so a refusal part way through knows how much is still unread."""

    def __init__(self, raw):
        self._raw = raw
        self.count = 0

    def read(self, size=-1):
        chunk = self._raw.read(size)
        self.count += len(chunk)
        return chunk


class _Handler(BaseHTTPRequestHandler):
    server_version = "PrintHandyWebUI/1"
    protocol_version = "HTTP/1.1"
    # StreamRequestHandler applies this to the connection's socket.
    timeout = REQUEST_TIMEOUT_S

    # -- plumbing ---------------------------------------------------------
    def log_message(self, *args):  # keep stdout clean; errors still surface as 500s
        pass

    def finish(self):
        # A refused body (_refuse_body) is drained after the answer is
        # written and before the socket closes, so the client reads the
        # answer instead of a reset.
        unread, self._unread_body = getattr(self, "_unread_body", 0), 0
        if unread:
            try:
                self.wfile.flush()
                _drain(self.connection, self.rfile, unread)
            except (OSError, ValueError):
                pass  # the client went away or stalled; close as before
        super().finish()

    def _content_length(self):
        """The request's Content-Length, or 0 when absent or not a number."""
        try:
            return max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            return 0

    def _refuse_body(self, unread):
        """Close the connection after this response without reading the
        body, draining up to DRAIN_LIMIT_BYTES of its ``unread`` bytes
        first (see finish). Draining is not parsing: nothing read is
        looked at, so auth-before-parse and the size cap still hold."""
        self.close_connection = True
        self._unread_body = max(0, int(unread or 0))

    def _internal_error(self, method, path, *, html_page=False):
        """The 500 answer: a generic reason and a request id, never the
        exception text (it can carry paths, URLs or settings values).
        The log line carries the same id beside the traceback."""
        rid = _request_id()
        log.exception("Web UI %s %s failed (request id %s)", method, path, rid)
        if html_page:
            self._html_refusal(500, "Something went wrong rendering this page. "
                                    f"Request id: {rid}.", "/")
        else:
            self._json(500, {"ok": False, "reason": INTERNAL_ERROR_REASON,
                             "request_id": rid})

    def _host_refused(self, *, post=False):
        """True (and a 403 sent) for a DNS-rebinding request.

        On a loopback bind without an access token the form token is the
        only guard on a write, and the page that carries it is served to
        anyone who can reach the port. A hostile site that rebinds its own
        hostname to 127.0.0.1 is "same origin" with this server, so it
        could read the token and post settings changes. Such a request
        still names the attacker's hostname in Host (and Origin), so only
        the loopback names and the bind address are accepted.

        A LAN bind with no password (an unclaimed install, or the "leave
        it open" choice) has the same exposure on any address. A rebinding site needs a public DNS name it
        controls, so there only names it cannot serve are accepted (an IP
        literal, a single-label name, a private-use suffix), and a write's
        Origin must be the same host, which also refuses cross-site posts.
        Re-read per request: finishing setup replaces server.access.
        """
        allowed = allowed_hosts(self.server.bind_host, self.server.access)
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
                        "address or a local host name. Set a password in the setup "
                        "walkthrough's Login step to reach it through another name.",
                   "text/plain; charset=utf-8")
        return True

    def _broken_refused(self, path):
        """True (and a 503 sent) while the saved login file is broken.

        Only /healthz still answers, so the container stays observable.
        The reason is logged once per distinct problem, with the file's
        full path; the response names only the file."""
        access = self.server.access
        if not getattr(access, "broken", False) or path == "/healthz":
            return False
        _log_broken_access(self.server)
        self._send(503, access.problem + "\n", "text/plain; charset=utf-8",
                   headers=[("Retry-After", "30")])
        return True

    def _send(self, code, body, content_type, headers=()):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        named = {name.lower() for name, _value in headers}
        if content_type.startswith("text/html") and "cache-control" not in named:
            # Pages embed a form token (and show settings): never let a
            # browser or proxy cache keep a copy.
            self.send_header("Cache-Control", "no-store")
        for name, value in SECURITY_HEADERS:
            if name.lower() not in named:
                self.send_header(name, value)
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
        address = self._client_ip()
        throttle = self.server.login_throttle
        if throttle.retry_after(address):
            return False
        if access.matches(provided):
            throttle.record_success(address)
            # A client that only ever sends the saved password as the
            # header (the Home Assistant integration) upgrades an old
            # hash the same way a form sign-in does (#1243).
            self._rehash_after_login(provided)
            return True
        throttle.record_failure(address)
        return False

    def _client_ip(self):
        """The address the login throttle counts this request under.

        The socket's peer, unless the peer is one of the configured
        ``[webui] trusted_proxies``: then the client the proxy reports
        in X-Forwarded-For (access.client_address). A forwarded-for
        header from anyone else is ignored."""
        peer = self.client_address[0] if self.client_address else ""
        trusted = getattr(self.server, "trusted_proxies", ())
        if not trusted:
            return peer
        return access_module.client_address(
            peer, self.headers.get_all("X-Forwarded-For") or (), trusted)

    def _deny_header(self):
        """401 for a missing or wrong header token; 429 with Retry-After
        while this address is locked out after repeated wrong tokens."""
        wait = self.server.login_throttle.retry_after(self._client_ip())
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
        return bool(self._valid_session_id())

    def _valid_session_id(self):
        """The caller's session id when it names a live session, else ""."""
        session_id = self._session_id()
        if not session_id:
            return ""
        now = time.time()
        expiry = self.server.sessions.get(session_id)
        if expiry is not None:
            if expiry <= now:
                self.server.sessions.pop(session_id, None)
                return ""
            return session_id
        remembered = getattr(self.server, "remembered_sessions", None) or {}
        digest = _session_digest(session_id)
        expiry = remembered.get(digest)
        if expiry is None:
            return ""
        if expiry <= now:
            with _remember_lock(self.server):
                if remembered.pop(digest, None) is not None:
                    _save_remembered_sessions(self.server)
            return ""
        return session_id

    def _session_form_token(self, session_id):
        """The form token bound to one session: an HMAC of its id under a
        per-process secret, so a token copied from one signed-in browser
        is worthless with another session or none."""
        return session_form_token(self.server, session_id)

    def _form_token(self):
        """The ``_csrf`` value to embed in a page served to this request.

        While access is open (unclaimed, or deliberately left open) it
        is the per-process form token, as before. On a claimed install a
        signed-in browser gets the token bound to its session; a header
        client (which posts with its header, not a form) gets the
        process token, which authorizes nothing there on its own."""
        if self.server.access.required:
            session_id = self._valid_session_id()
            if session_id:
                return self._session_form_token(session_id)
        return self.server.form_token

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
            form_token=self._form_token(), **kwargs)

    # -- routes and the policy middleware (#1319) --------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    def _dispatch(self, method):
        """The one way into a route handler: find the route in the table
        (routes.ROUTES), run the screens every request gets, enforce the
        route's declared policy (_policy), then call its handler.

        A failure anywhere is a 500 with a request id, never a dropped
        connection or the exception text (log_message is silenced; the
        log keeps the traceback under the id the response names). A
        method without its own routes gets the read-only refusal with no
        screens, as it always has."""
        path = urlparse(self.path).path
        route, capture = routes_module.resolve(method, path)
        if route is None or not route.prechecks:
            self._read_only()
            return
        try:
            self._sync_access()
            if self._broken_refused(path):
                return
            if route.host_check and self._host_refused(post=method == "POST"):
                return
            arg = unquote(capture) if (capture is not None and route.decode) else capture
            self._route, self._route_arg = route, arg
            with self._policy(route, arg) as grant:
                if grant is None:
                    return
                args = [] if capture is None else [arg]
                if route.pass_path:
                    args.append(path)
                if route.policy == routes_module.SESSION_CSRF:
                    args.append(grant.form)
                getattr(self, route.handler)(*args)
        except Exception:
            self._internal_error(method, path,
                                 html_page=method == "GET" and self._browser_navigation(path))

    class _Grant:
        """What the policy hands a handler: the parsed form, for a
        ``session+csrf`` route (None for a multipart upload)."""

        def __init__(self, form=None):
            self.form = form

    @contextlib.contextmanager
    def _policy(self, route, arg):
        """Enforce ``route.policy`` (routes.py says what each one means).

        Yields a _Grant when the request may proceed, or None after the
        refusal has been sent. A ``claim_lock`` route stays inside the
        claim lock until its handler returns."""
        policy = route.policy
        if policy == routes_module.PUBLIC:
            yield self._Grant()
        elif policy == routes_module.SESSION:
            if self._authorized():
                yield self._Grant()
                return
            if self._browser_navigation(urlparse(self.path).path):
                # A browser following the Unraid WebUI link (or any
                # bookmark) sends Accept: text/html and cannot send the
                # token header: send it to the login form instead of raw
                # JSON (#440). API clients keep the JSON 401.
                self._redirect("/login")
            else:
                self._deny_header()
            yield None
        elif policy == routes_module.HEADER:
            if self._header_authorized():
                yield self._Grant()
                return
            self._deny_header()
            yield None
        elif policy == routes_module.LINK_TOKEN:
            if self._link_valid(arg):
                yield self._Grant()
                return
            if self.command == "POST":
                self._refuse_body(self._content_length())
            self._send(404, upload_module.render_token_page(error=INVALID_LINK_MESSAGE),
                       "text/html; charset=utf-8")
            yield None
        elif policy == routes_module.SESSION_CSRF:
            back = self._route_back(route, arg)
            content_type = self.headers.get("Content-Type") or ""
            if route.multipart and content_type.startswith("multipart/form-data"):
                # Gate before the body is read: an upload body can run to
                # the shared download budget (~200 MiB), so an
                # unauthenticated client must not be able to make the
                # server receive and parse one. The gate is the read rule
                # (session first, so a browser request does not trip the
                # header throttle); the form token is checked once the
                # fields are parsed (_policy_recheck). A session alone
                # still cannot save a file.
                if self.server.access.required and not (
                        self._session_authorized() or self._header_authorized()):
                    self._refuse_body(self._content_length())
                    self._html_refusal(403, STALE_FORM_MESSAGE, back)
                    yield None
                    return
                yield self._Grant()
                return
            form, error = self._read_form()
            if error:
                self._html_refusal(error[0], error[1], back)
                yield None
                return
            lock = _claim_lock(self.server) if route.claim_lock else contextlib.nullcontext()
            with lock:
                if route.claim_lock:
                    # One claim at a time, authorized inside the lock: two
                    # browsers racing to claim an unclaimed install cannot
                    # both win. The first claim rotates the form token (and
                    # makes a session required), so the second is refused
                    # here instead of silently replacing the first one's
                    # password.
                    self._sync_access()
                if not self._post_authorized(form, process_token=route.process_token):
                    self._html_refusal(403, STALE_FORM_MESSAGE, back)
                    yield None
                    return
                yield self._Grant(form)
        else:  # an undeclared policy never answers (the table test catches it)
            raise ValueError(f"route {route.pattern!r} has unknown policy {policy!r}")

    @staticmethod
    def _route_back(route, arg):
        return route.back.format(arg=quote(arg or "", safe=""))

    def _policy_recheck(self, fields):
        """The second half of the policy for a body received in a stream
        (_receive_upload), run once its fields are parsed and before the
        handler sees them. True when the request may proceed; otherwise
        the refusal has been sent.

        A link token is checked again because reading a large upload can
        take a while, and a link revoked (or expired) in that window must
        not still deliver a file (save_upload_for_link makes the final
        check atomic with the write). A form upload's form token can only
        be checked now that its fields exist."""
        route, arg = self._route, self._route_arg
        if route.policy == routes_module.LINK_TOKEN:
            if self._link_valid(arg):
                return True
            self._send(404, upload_module.render_token_page(error=INVALID_LINK_MESSAGE),
                       "text/html; charset=utf-8")
            return False
        if route.policy == routes_module.SESSION_CSRF:
            if self._post_authorized(fields, process_token=route.process_token):
                return True
            self._html_refusal(403, STALE_FORM_MESSAGE, self._route_back(route, arg))
            return False
        return True

    # -- small routes -------------------------------------------------------
    def _healthz(self):
        code, body = healthz(self.server.settings_data)
        self._json(code, body)

    def _setup_redirect(self):
        self._redirect("/")

    def _status_page(self):
        if (self._browser_navigation("/")
                and walkthrough_module.offer_once(self.server.settings_data)):
            # First run (#813): a browser landing on the status page goes
            # to the walkthrough until it is done; the setup gate's
            # discipline, one step later. API callers and every other
            # route are unaffected.
            self._redirect("/walkthrough")
            return
        snap = self.server.snapshot_func()
        self._send(200, render_status_html(
            snap, form_token=self._form_token(), theme=self._theme(),
            walkthrough_pending=walkthrough_module.pending(self.server.settings_data),
            unclaimed=self.server.access.unclaimed,
            open_reason=getattr(self.server.access, "open_reason", ""),
            credential_ignored=getattr(self.server.access, "credential_ignored", False),
            loopback=is_loopback(self.server.bind_host),
            job=_overall_job(self.server.settings_data),
            nav=data_module.nav_for(self.server.settings_data)
            if (data_module.data_enabled(self.server.settings_data)
                or data_module.wrapped_enabled(self.server.settings_data))
            else None),
            "text/html; charset=utf-8")

    def _api_status(self):
        self._json(200, self.server.api_snapshot_func())

    def _readyz(self):
        # Readiness per component (Spoolman, slicer, each printer), behind
        # the same auth as /api/status; /healthz stays the
        # unauthenticated liveness probe.
        ready = self.server.readiness_func() or {}
        self._json(200 if ready.get("ready", False) else 503, ready)

    def _spools_index(self):
        entries = settings.printers(self.server.settings_data)
        if len(entries) == 1:
            # One printer: no picker step in the way (#875).
            self._redirect("/spools/" + quote(str(entries[0]["id"]), safe=""))
            return
        self._send(200, spools_module.render_spools_index(entries, theme=self._theme()),
                   "text/html; charset=utf-8")

    def _config_page(self):
        query = parse_qs(urlparse(self.path).query)
        section = (query.get("section") or [None])[0]
        reveal = (query.get("setup") or [None])[0]
        self._send(200, self._config_html(self._field_values(), theme=self._theme(),
                                          section=section, reveal=reveal),
                   "text/html; charset=utf-8")

    def _chat_state(self):
        self._json(200, chat_module.chat_state(
            self._chat_view(issue=False) if self._web_chat_enabled() else None))

    def _upload_page(self):
        self._send(200, self._upload_page_html(), "text/html; charset=utf-8")

    def _sources_page(self):
        self._send(200, sources_module.render_sources_page(
            theme=self._theme(), nav=data_module.nav_for(self.server.settings_data)),
            "text/html; charset=utf-8")

    def _designs_page(self):
        rows = designs_module.catalog_rows(self.server.settings_data)
        self._send(200, designs_module.render_designs_page(rows, theme=self._theme()),
                   "text/html; charset=utf-8")

    def _projects_route(self, path):
        # The read-only multi-part project readout (#1204 item 5).
        self._send(*projects_module.respond(
            self.server.settings_data, path, theme=self._theme(),
            nav=data_module.nav_for(self.server.settings_data)))

    def _layers_route(self, rest=""):
        archive = layers_module.archive_root(self.server.settings_data)
        layer = (parse_qs(urlparse(self.path).query).get("layer") or ["0"])[0]
        code, content_type, body = layers_module.respond(
            archive, rest, layer, theme=self._theme())
        if content_type.startswith("text/html"):
            self._send(code, body, content_type)
        else:
            self._json(code, body)

    def _not_found(self, path):
        if self._browser_navigation(path):
            # A browser navigation to a path that does not exist gets the
            # themed refusal, not a JSON dump (#840).
            self._html_refusal(404, "No page lives at this address.", "/")
            return
        self._json(404, {"ok": False,
                         "reason": "try /, /api/status, /api/printers/<id>/status, "
                                   "/api/printers/<id>/camera/snapshot, "
                                   "/api/printers/<id>/spools, "
                                   "/healthz, /readyz, /config, /designs, /layers, /spools, "
                                   "/walkthrough or /login"})

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

        Served under the Web UI's own access rules, like every other
        page: open on an install with no login set, behind the login
        when one is set. While a route's own switch is off ([timelapse]
        data_enabled for the Data routes, wrapped_enabled for the
        Wrapped routes), it answers the named 403 refusal from
        data.py, for pages and APIs alike.
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

    def _wrapped_video_file_route(self):
        """GET /wrapped/video-file?year=<year> (#981 step 4).

        Serves the compiled Wrapped video so the Wrapped page can
        embed it. Behind the same login as every other route here
        (do_GET has already authorized), plus the wrapped_enabled
        gate. Only the one file data.py constructs for a whole-number
        year inside the wrapped outputs can be served — the request
        never supplies a file name, so traversal has no input.
        """
        data = self.server.settings_data
        if not data_module.wrapped_enabled(data):
            self._json(403, {"ok": False, "reason": data_module.WRAPPED_OFF_REASON})
            return
        year, error = self._year_param()
        if error:
            self._json(error[0], error[1])
            return
        if year is None:
            self._json(400, {"ok": False,
                             "reason": "year must be a whole number. Nothing shown."})
            return
        target = data_module.wrapped_video_path(data, year)
        if target is None:
            self._json(404, {"ok": False,
                             "reason": "no compiled Wrapped video for that year. "
                                       "Compile it from the Wrapped page first."})
            return
        self._stream_file(target, "video/mp4")

    VIDEO_CHUNK = 256 * 1024

    def _stream_file(self, target, content_type):
        """Send a file in chunks, honoring one ``Range`` (206) so a browser
        can seek; the whole file is never held in memory."""
        try:
            handle = open(target, "rb")
        except OSError:
            self._json(404, {"ok": False, "reason": "the Wrapped video could not be read."})
            return
        with handle:
            size = os.fstat(handle.fileno()).st_size
            wanted = data_module.parse_byte_range(self.headers.get("Range"), size)
            if wanted == "unsatisfiable":
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = wanted if wanted else (0, size - 1)
            length = max(0, end - start + 1)
            self.send_response(206 if wanted else 200)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if wanted:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(self.VIDEO_CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _wrapped_video_status_route(self):
        """GET /api/wrapped/video-status?year=<year>: the compile's state
        (idle, queued, running with a step count, done, failed with a
        short reason), polled by the Wrapped page while a video compiles."""
        year, error = self._year_param()
        if error or year is None:
            self._json(*(error or (400, {"ok": False, "reason":
                                         "year must be a whole number. Nothing shown."})))
            return
        code, payload = data_module.video_status(self.server.settings_data, year)
        self._json(code, payload)

    def _wrapped_video_post(self, form):
        """POST /wrapped/video (#956): the Wrapped page's video form.

        Policy ``session+csrf`` with the process token (routes.py): a
        login session alone never authorizes it. The compile itself, its
        two toggles and every named refusal live in data.py /
        wrapped_video.py.
        """
        raw_year = (form.get("year") or [""])[0]
        try:
            year = int(raw_year)
        except (TypeError, ValueError):
            self._html_refusal(400, "year must be a whole number. Nothing was compiled.",
                               "/wrapped")
            return
        # The compile runs as a background job: this starts it (or joins the
        # one already running for the year) and goes back to the page, which
        # shows the progress and then the finished video.
        code, payload = data_module.make_video(
            self.server.settings_data, year,
            export_silent=bool((form.get("export_silent") or [""])[0]))
        if code not in (200, 202):
            self._html_refusal(code, payload.get("reason", "the video compile failed"),
                               "/wrapped")
            return
        self._redirect(f"/wrapped?year={year}")

    def _wrapped_share_post(self, form):
        """POST /wrapped/share (#981): the Wrapped page's share form.

        Authorized exactly like the video form (routes.py: the page's
        form token; a login session alone never authorizes it). The
        export itself, its wrapped_enabled gate and every named
        refusal live in data.py / share_export.py. The export is a
        local file write only — nothing is uploaded anywhere.
        """
        raw_year = (form.get("year") or [""])[0]
        try:
            year = int(raw_year)
        except (TypeError, ValueError):
            self._html_refusal(400, "year must be a whole number. Nothing was exported.",
                               "/wrapped")
            return
        # The share form posts what to include as checkboxes (an unticked
        # box is simply absent); a post without the options marker keeps
        # the safe defaults: titles and printer names in, footage out.
        explicit = (form.get("options") or [""])[0] == "explicit"

        def ticked(name, default):
            return bool((form.get(name) or [""])[0]) if explicit else default

        code, payload = data_module.export_share(
            self.server.settings_data, year,
            (form.get("visibility") or ["link-only"])[0],
            destination_note=(form.get("destination") or [""])[0],
            passphrase=(form.get("passphrase") or [None])[0],
            include_titles=ticked("include_titles", True),
            include_printers=ticked("include_printers", True),
            include_video=ticked("include_video", False))
        if code != 200:
            self._html_refusal(code, payload.get("reason", "the export failed"),
                               "/wrapped")
            return
        self._send(200, data_module.render_share_result(
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
        if len(segments) == 3 and segments[0] and segments[1:] == ["screen", "snapshot"]:
            code, body, content_type = self.server.screen_snapshot_func(unquote(segments[0]))
            if code != 200:
                self._json(code, body)
                return
            self._send(200, body, content_type, headers=[("Cache-Control", "no-store")])
            return
        self._json(404, {"ok": False,
                         "reason": "try /api/printers/<id>/status, "
                                   "/api/printers/<id>/spools, "
                                   "/api/printers/<id>/camera/snapshot or "
                                   "/api/printers/<id>/screen/snapshot"})

    def _printer_post_route(self, rest):
        """POST /api/printers/<id>/...: a spool action or a control verb,
        both behind the ``header`` policy (routes.py)."""
        segments = rest.split("/")
        if len(segments) >= 2 and segments[1] == "spools":
            self._printer_spools_route(rest)
        else:
            self._printer_control_route(rest)

    # -- per-printer controls (#708, control endpoints phase) ----------------
    def _printer_control_route(self, rest):
        """POST /api/printers/<id>/pause|resume|cancel|temperature|target.

        Also the ha-print-handy #1 verbs: emergency_stop,
        firmware_restart, server_restart, gcode and exclude.

        Authorization is the ``header`` policy (routes.py): the
        access-token header only (or no token configured); a
        login-session cookie never authorizes a control POST. The body must be JSON,
        so a cross-site HTML form cannot post here. The off-by-default
        switch, cancel's confirm field and the temperature limits live
        in controls.py; a 404 for an unknown printer keeps the status
        route's shape.
        """
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
            settings.printers(self._walkthrough_data()), form_token=self._form_token(),
            notice=notice or done or None, error=error, theme=self._theme()),
            "text/html; charset=utf-8")

    def _printers_post(self, form):
        """POST /printers: append one printer through the roster writer."""
        ok, message = printers_page.add(
            form, settings.printers(self._walkthrough_data()),
            environ=self.server.webui_environ)
        if ok:
            self._redirect("/printers?done=" + quote(message, safe=""))
        else:
            self._printers_page(error=message, code=400)

    def _screen_page(self, printer_id):
        """GET /printers/<id>/screen: the printer screen view (#1303).

        Availability is probed (discovery only, no frame is pulled);
        the page renders the labeled read-only view when a screen is
        published and the reason when it is not, so an unsupported or
        unavailable screen is an explanation, never a broken image.
        An unknown id is a 404 in the same shape.
        """
        if not printer_id or "/" in printer_id:
            self._json(404, {"ok": False,
                             "reason": "that printer id does not name a "
                                       "printer screen page"})
            return
        try:
            entry = settings.by_id(printer_id, self.server.settings_data)
        except settings.SettingsError:
            self._json(404, {"ok": False,
                             "reason": f"no printer with id {printer_id!r} "
                                       f"is configured"})
            return
        ok, reason = self.server.screen_probe_func(printer_id)
        self._send(200, screen_module.render_page(
            entry.get("name") or printer_id, printer_id,
            ok=ok, reason=reason, theme=self._theme()),
            "text/html; charset=utf-8")

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
            view, form_token=self._form_token(), theme=self._theme(),
            notice=done or None,
            purchase_ready=spools_module.purchase_available(),
            printers=settings.printers(self.server.settings_data)),
            "text/html; charset=utf-8")

    def _spools_page_post(self, printer_id, form):
        """POST /spools/<id>: one picker form from the Spools page.

        Same ``session+csrf`` policy as a config POST (routes.py; a
        login session alone never authorizes it). The form converts to the same action the JSON API takes,
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
                       view, form_token=self._form_token(), theme=self._theme(),
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

    def _sync_access(self):
        """Apply a login another process saved (chat setup, the CLI wizard).

        The bot and the Web UI are separate processes, so a login set from
        chat is only visible here as a change to the saved file. Old
        sessions and form tokens must not outlive the credential they
        were issued under."""
        server = self.server
        if not hasattr(server, "access_stamp"):
            return
        # A broken login file is re-read on every request: fixing its
        # ownership or mode changes no mtime, and the fix must apply
        # without a restart.
        broken = getattr(server.access, "broken", False)
        if not broken and _access_stamp(server.settings_data) == server.access_stamp:
            return
        with server.access_sync_lock:
            stamp = _access_stamp(server.settings_data)
            if stamp == server.access_stamp and not server.access.broken:
                return
            server.access_stamp = stamp
            new_access = access_module.resolve(
                server.settings_data, server.bind_host, server.webui_environ)
            if new_access.credential_fingerprint() == \
                    server.access.credential_fingerprint():
                # The stamp is only the file's mtime: a rewrite carrying
                # the credential this server already enforces (a byte-for-
                # byte re-save, or a login this process resolved from the
                # file itself) changed no rule, so nothing issued under it
                # is stale — sessions and the form token stay valid.
                server.access = new_access
                return
            server.access = new_access
            server.sessions.clear()
            self._rotate_form_token()
            self._revoke_remembered_sessions()
            self._revoke_upload_links()

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
        self.server.csrf_secret = secrets.token_bytes(32)

    def _revoke_remembered_sessions(self):
        """Revoke every remembered session and delete its store file.

        Called only where the login rule itself was just set, changed or
        removed (the walkthrough Login step, beside its
        _rotate_form_token call): a session minted under the old
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

    def _revoke_upload_links(self):
        """Revoke every no-login upload link: the login was set or changed.

        A link minted while the install was open (or under the old
        password) must not keep working once the owner sets a login.
        The link store is rebound to the new credential's fingerprint,
        so even a link the rewrite missed fails the fingerprint check.
        A failure is logged, never raised: the login change stands."""
        try:
            upload_module.revoke_all_links(
                self.server.settings_data, self.server.access.credential_fingerprint())
        except OSError as exc:
            log.warning("webui: could not revoke the upload links: %s", exc)

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
        has no walkthrough pending (finished or skipped). Every install
        owes it until then, however its access was resolved. An unclaimed
        install is the exception: the status page's "No login is set"
        banner links here, so a closed walkthrough reopens at Login
        instead of bouncing the banner's link back to the status page."""
        data = self._walkthrough_data()
        if not walkthrough_module.pending(data):
            if not self.server.access.unclaimed:
                self._redirect("/")
                return
            walkthrough_module.reopen_at_login(data)
        self._send(200, walkthrough_module.render_page(
            data, self._field_values(),
            form_token=self._form_token(), theme=self._theme(),
            environ=self.server.webui_environ,
            loopback=is_loopback(self.server.bind_host)), "text/html; charset=utf-8")

    def _walkthrough_post(self, form):
        """POST /walkthrough: one step's action, authorized exactly like
        a config POST. The policy (routes.py, ``claim_lock``) authorizes
        it inside the claim lock and holds the lock until this returns,
        so two browsers racing to claim an unclaimed install cannot both
        win."""
        if self._login_change_refused(form):
            return
        self._walkthrough_apply(form)

    def _login_change_refused(self, form):
        """True (the step re-rendered with the reason) when a claimed
        install's login would change without the current credential.

        Changing or removing the password needs the password (or the
        access token) in force now, typed into the Login step: a signed-
        in session that was left open, or a stolen page, is not enough to
        lock the owner out. Wrong entries count toward the login form's
        per-address backoff. An open install has no current credential,
        so nothing is asked there (the unclaimed design)."""
        access = self.server.access
        data = self._walkthrough_data()
        if not access.required or not walkthrough_module.login_change_requested(data, form):
            return False
        address = self._client_ip()
        throttle = self.server.login_throttle
        wait = throttle.retry_after(address)
        current = (form.get(walkthrough_module.CURRENT_PASSWORD_FIELD) or [""])[0]
        if not wait and current and access.matches(current):
            throttle.record_success(address)
            return False
        if wait:
            message = f"Too many failed attempts. Try again in {wait} seconds."
        elif current:
            throttle.record_failure(address)
            message = "The current password was not accepted, so the login was not changed."
        else:
            message = "Enter the current password to change or remove the login."
        self._send(403, walkthrough_module.render_page(
            data, self._field_values(), form_token=self._form_token(), theme=self._theme(),
            environ=self.server.webui_environ,
            submitted={"username": (form.get(pages.SETUP_USER_FIELD) or [""])[0]},
            general_error=message, step_index=walkthrough_module._LOGIN_INDEX,
            loopback=is_loopback(self.server.bind_host)), "text/html; charset=utf-8")
        return True

    def _walkthrough_apply(self, form):
        kind, payload = walkthrough_module.apply_post(
            self._walkthrough_data(), self.server.webui_environ, form,
            self._field_values(), form_token=self._form_token(),
            theme=self._theme(), loopback=is_loopback(self.server.bind_host))
        if kind == "access":
            # The Login step saved a new rule: apply it now, and keep the
            # person who chose it signed in on this browser. A password
            # saved under require_auth = false turned that setting back on
            # in the settings file; tightening applies at once (loosening
            # still waits for a restart), so the password is enforced now.
            webui = self.server.settings_data.get("webui")
            if isinstance(webui, dict) and not webui.get("require_auth", True) and \
                    self._walkthrough_data()["webui"].get("require_auth", True):
                webui["require_auth"] = True
            self.server.access = access_module.resolve(
                self.server.settings_data, self.server.bind_host, self.server.webui_environ)
            self.server.access_stamp = _access_stamp(self.server.settings_data)
            # Every session issued under the old rule ends — an intruder's
            # live session must not survive the owner changing the
            # password. The browser that made the change gets a fresh one.
            self.server.sessions.clear()
            self._rotate_form_token()
            self._revoke_remembered_sessions()
            self._revoke_upload_links()
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
        address = self._client_ip()
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
        self._rehash_after_login(provided)
        remember = (form.get(pages.LOGIN_REMEMBER_FIELD) or [""])[0].strip().lower() \
            in _REMEMBER_TRUTHY
        self._redirect("/", headers=[self._start_session(remember=remember)])

    def _rehash_after_login(self, password):
        """Upgrade a password saved at an older PBKDF2 cost, once.

        Runs after a successful sign-in, on the login form or with the
        ``X-WebUI-Token`` header (#1243): either way the password was
        just verified, and rehash_password writes only the saved
        password, never the environment token. Once the new Access is
        adopted ``needs_rehash`` is false, so later requests pay one
        attribute check. The re-hashed file is the same
        login, so nothing issued under it is revoked: the server adopts
        the new Access, and the remembered-session store and the upload
        links are rebound to its fingerprint (which names the stored
        hash). A failed write is logged and the sign-in stands; the
        next sign-in tries again."""
        server = self.server
        access = server.access
        if not access.needs_rehash:
            return
        lock = getattr(server, "access_sync_lock", None) or contextlib.nullcontext()
        with lock:
            if server.access is not access:
                return
            data = server.settings_data
            old_fingerprint = access.credential_fingerprint()
            try:
                if not access_module.rehash_password(data, access, password):
                    return
            except OSError as exc:
                log.warning("webui: could not re-hash the saved password: %s", exc)
                return
            server.access = access_module.resolve(
                data, server.bind_host, server.webui_environ)
            server.access_stamp = _access_stamp(data)
            new_fingerprint = server.access.credential_fingerprint()
            if getattr(server, "remembered_sessions", None):
                _save_remembered_sessions(server)
            try:
                upload_module.rebind_links(data, old_fingerprint, new_fingerprint)
            except OSError as exc:
                log.warning("webui: could not rebind the upload links: %s", exc)
            log.info("webui: the saved password was re-hashed at the current cost")

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

    def _post_authorized(self, form, process_token=False):
        """Whether a state-changing form POST may proceed.

        While access is open (unclaimed or deliberately left open) the
        per-process form token embedded in the page is the guard, exactly
        as before: a cross-origin page cannot read one.

        On a claimed install (a password or token is required) the form
        token is no longer a credential on its own: the request needs the
        access-token header, or a live login session AND the ``_csrf``
        value bound to that session (see _session_form_token). A session
        cookie alone never authorizes a POST, and a page token copied
        from one browser authorizes nothing for another.

        ``process_token``: a route whose page another module renders with
        the process form token (the Wrapped page's forms) still needs a
        live session on a claimed install, but accepts the process token
        beside it until that page embeds the session-bound one."""
        provided = (form.get(pages.CSRF_FIELD) or [""])[0]
        if not self.server.access.required:
            return bool(provided) and hmac.compare_digest(provided, self.server.form_token)
        if self._header_authorized():
            return True
        session_id = self._valid_session_id()
        if not (session_id and provided):
            return False
        if hmac.compare_digest(provided, self._session_form_token(session_id)):
            return True
        return process_token and hmac.compare_digest(provided, self.server.form_token)

    # -- web chat -------------------------------------------------------------

    def _chat_service(self):
        return chat_service_for(self.server)

    def _web_chat_enabled(self):
        """[webui] chat_enabled, the chat page's own switch (PR #1106).

        Read from the server's settings mapping on every request, so
        the page a request gets always matches the saved configuration.
        """
        from ..bot.web import chat_enabled
        return chat_enabled(self.server.settings_data)

    def _chat_page(self, notice=None, error=None):
        if not self._web_chat_enabled():
            # Off is a page, not an error: it says the state and the way
            # back on, and the chat service is never built behind it.
            self._send(200, chat_module.render_chat_disabled_page(
                theme=self._theme(),
                nav=data_module.nav_for(self.server.settings_data)),
                "text/html; charset=utf-8")
            return
        ft = self._form_token()
        view = self._chat_view()
        self._send(200, chat_module.render_chat_page(
            view, form_token=ft, notice=notice, error=error,
            theme=self._theme(),
            nav=data_module.nav_for(self.server.settings_data)),
            "text/html; charset=utf-8", headers=self._chat_cookie_headers())

    def _cookie_value(self, name):
        raw = self.headers.get("Cookie") or ""
        if not raw:
            return ""
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:  # a malformed Cookie header carries nothing
            return ""
        morsel = jar.get(name)
        return morsel.value if morsel is not None else ""

    def _chat_view(self, issue=True):
        """This browser's view of the web chat: its own transcript only.

        Web chat is one conversation per Web UI session (#1145), never a
        shared outbox. The scope is the login session when the browser
        has one; on an install with no login it is the browser's own
        chat cookie, issued here on first use (``issue``). A request
        with neither (and ``issue`` off, as for the state poll) gets a
        fresh scope that holds nothing."""
        from ..bot.web import scope_for
        secret = self._valid_session_id()
        if not secret and self.server.access.required and self.headers.get(TOKEN_HEADER):
            # A header client (no cookies) signed in with the access
            # token: one conversation for the token holder. The request
            # was already authorized by that header to get here.
            secret = "\x00access-token-client"
        if not secret:
            secret = self._cookie_value(CHAT_COOKIE)
            if not _CHAT_COOKIE_VALUE.fullmatch(secret or ""):
                secret = secrets.token_urlsafe(32)
                if issue:
                    self._new_chat_cookie = secret
        return self._chat_service().view(scope_for(secret))

    def _chat_cookie_headers(self):
        """The Set-Cookie header for a chat cookie issued on this request."""
        value = getattr(self, "_new_chat_cookie", None)
        if not value:
            return []
        flags = "; Secure" if self._cookie_secure() else ""
        return [("Set-Cookie", f"{CHAT_COOKIE}={value}; HttpOnly; SameSite=Strict; "
                               f"Path=/; Max-Age={REMEMBER_TTL_S}{flags}")]

    def _chat_post(self, form):
        if not self._web_chat_enabled():
            self._chat_page()  # the disabled page, with the explanation
            return
        text = (form.get("text") or [""])[0].strip()
        if not text:
            self._redirect("/chat")
            return
        if not self._chat_view().submit(text):
            self._chat_page(error="Too many messages in the last minute; "
                                  "give the bot a moment to catch up.")
            return
        self._redirect("/chat", headers=self._chat_cookie_headers())

    def _chat_choice_post(self, form):
        if not self._web_chat_enabled():
            self._chat_page()  # the disabled page, with the explanation
            return
        clarify_id = (form.get("clarify_id") or [""])[0]
        try:
            index = int((form.get("index") or ["-1"])[0])
        except ValueError:
            index = -1
        if clarify_id and index >= 0:
            # A press can fail two ways, and neither may look like
            # success: the answer wait can time out (which used to
            # surface as a raw 500 from the outer POST guard), and the
            # press can resolve nothing — the question already closed,
            # or the press was refused — which used to redirect in
            # silence. Both come back as the page with an explanation.
            try:
                label = self._chat_view().answer(clarify_id, index)
            except Exception:
                self._chat_page(error="That answer didn't reach the bot "
                                      "in time. The question is still "
                                      "open — try again.")
                return
            if label is None:
                self._chat_page(error="That answer wasn't accepted — "
                                      "the question may have closed, or "
                                      "the answer belongs to another step.")
                return
        self._redirect("/chat")

    # -- uploads ------------------------------------------------------------

    def _credential_fingerprint(self):
        """The fingerprint upload links are minted and checked under."""
        return self.server.access.credential_fingerprint()

    def _upload_page_html(self, notice=None, error=None):
        return upload_module.render_upload_page(
            self.server.settings_data, form_token=self._form_token(),
            notice=notice, error=error, theme=self._theme(),
            nav=data_module.nav_for(self.server.settings_data),
            fingerprint=self._credential_fingerprint())

    def _receive_upload(self, handle):
        """Receive one multipart upload body and hand it to ``handle``.

        The body is streamed to a spool file in the inbox, never read
        into memory whole (upload.parse_multipart_stream). Before a
        byte is read: at most MAX_CONCURRENT_UPLOADS uploads are
        received at once (503 with Retry-After otherwise), the
        Content-Length must fit the size cap (413), and the inbox
        quota must have room for it (507 with the plain reason).
        ``handle(fields, files)`` gets the parsed text fields and
        ``[(filename, spool path)]``; whatever spool file it does not
        move into the inbox is removed afterwards. Returns None, or
        ``(code, reason)`` when the body was refused. A refused body is
        not read; the connection closes after the answer, with a
        bounded drain of the unread rest first (_refuse_body), so a
        browser shows the answer rather than a reset."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True  # no length to drain by
            return 400, "bad Content-Length"
        content_type = self.headers.get("Content-Type") or ""
        if not content_type.startswith("multipart/form-data"):
            self._refuse_body(length)
            return 415, "uploads must be multipart/form-data"
        if length < 0 or length > (upload_module.MAX_UPLOAD_BYTES
                                   + upload_module.MULTIPART_OVERHEAD_BYTES):
            self._refuse_body(length)
            return 413, "upload too large"
        data = self.server.settings_data
        # The whole Content-Length counts toward the pre-read check: the
        # multipart framing is usually far smaller than its allowance, so
        # subtracting the allowance would let a file that cannot fit be
        # received before the save-time check refused it.
        upload_module.prune_stale_spool(data)
        full = upload_module.quota_refusal(data, max(0, length))
        if full is not None:
            self._refuse_body(length)
            return 507, full
        if not upload_module.UPLOAD_SLOTS.try_acquire():
            self._refuse_body(length)
            return 503, upload_module.BUSY_MESSAGE
        files = []
        body = _CountingReader(self.rfile)
        try:
            try:
                fields, files = upload_module.parse_multipart_stream(
                    body, length, content_type, upload_module.spool_dir(data))
            except upload_module.UploadError as exc:
                self._refuse_body(length - body.count)
                return exc.status, str(exc)
            except OSError:
                rid = _request_id()
                log.exception("webui: could not receive an upload (request id %s)", rid)
                self._refuse_body(length - body.count)
                return 500, ("the upload could not be received; nothing was saved "
                             f"(request id {rid})")
            fields = {name: [value] for name, value in fields.items()}
            if self._policy_recheck(fields):
                handle(fields, files)
            return None
        finally:
            upload_module.discard_spooled(files)
            upload_module.UPLOAD_SLOTS.release()

    def _upload_post(self, form):
        """POST /upload: a multipart file (``form`` is None; the policy
        gated it before the body and checks its form token once the
        fields are parsed) or a link and inbox action form."""
        if form is None:
            def handle(fields, files):
                if not files:
                    self._html_refusal(400, "no file was attached", "/upload")
                    return
                saved, reason = upload_module.save_upload(
                    self.server.settings_data, files[0][0], files[0][1])
                if saved is None:
                    self._html_refusal(507 if reason.startswith(upload_module.INBOX_FULL)
                                       else 400, reason, "/upload")
                    return
                self._send(200, self._upload_page_html(
                    notice=f"{saved.name} is in the inbox. Pick it up in chat "
                           "to slice and print it."), "text/html; charset=utf-8")

            refused = self._receive_upload(handle)
            if refused:
                headers = [("Retry-After", "30")] if refused[0] == 503 else []
                self._send(refused[0], shell.page(
                    "error", f'<p class="error">{html.escape(refused[1])}</p>'
                             '<p><a href="/upload">Back</a></p>',
                    path="/upload", theme=self._theme()),
                    "text/html; charset=utf-8", headers=headers)
            return
        action = (form.get("action") or [""])[0]
        notice = error_text = None
        data = self.server.settings_data
        if action == "create_link":
            token = upload_module.create_link(data, self._credential_fingerprint())
            days = upload_module.LINK_TTL_S // 86400
            notice = (f"Upload link created: /u/{token} — copy it now; "
                      "only a digest of it is kept, so it cannot be shown again. "
                      f"It works for {days} days, or until you revoke it or the "
                      "Web UI login changes.")
        elif action == "revoke_link":
            if upload_module.revoke_link(data, (form.get("digest") or [""])[0]):
                notice = "Upload link revoked."
            else:
                error_text = "that upload link is already gone"
        elif action == "delete_file":
            name = (form.get("name") or [""])[0]
            if upload_module.delete_inbox_file(data, name):
                notice = f"{name} was deleted from the inbox."
            else:
                error_text = "that file is no longer in the inbox"
        else:
            error_text = "unknown upload action"
        self._send(200, self._upload_page_html(notice=notice, error=error_text),
                   "text/html; charset=utf-8")

    def _link_valid(self, token):
        return upload_module.link_valid(self.server.settings_data, token,
                                        self._credential_fingerprint())

    def _token_upload_page(self, _token):
        # The link-token policy (routes.py) already checked the token.
        self._send(200, upload_module.render_token_page(),
                   "text/html; charset=utf-8")

    def _token_upload_submit(self, token):
        # The link-token policy (routes.py) checked the token before the
        # body and checks it again once the body is received; the
        # guarantee is in save_upload_for_link, which makes the final
        # validity decision and the write one atomic step against
        # revocation.
        invalid = INVALID_LINK_MESSAGE

        def handle(fields, files):
            if not files:
                self._send(400, upload_module.render_token_page(
                    error="No file was attached."), "text/html; charset=utf-8")
                return
            saved, reason = upload_module.save_upload_for_link(
                self.server.settings_data, token, files[0][0], files[0][1],
                self._credential_fingerprint())
            if saved is None and reason is None:
                # Revoked after the re-check above but before the write;
                # the atomic check-and-save refused the delivery.
                self._send(404, upload_module.render_token_page(error=invalid),
                           "text/html; charset=utf-8")
                return
            if saved is None:
                self._send(507 if reason.startswith(upload_module.INBOX_FULL) else 400,
                           upload_module.render_token_page(error=reason),
                           "text/html; charset=utf-8")
                return
            self._send(200, upload_module.render_token_page(
                notice=f"{saved.name} is in the inbox. It will be picked up "
                       "and confirmed there before anything prints."),
                "text/html; charset=utf-8")

        refused = self._receive_upload(handle)
        if refused:
            headers = [("Retry-After", "30")] if refused[0] == 503 else []
            self._send(refused[0], upload_module.render_token_page(error=refused[1]),
                       "text/html; charset=utf-8", headers=headers)

    def _config_preview(self, form):
        field_values = self._field_values()
        changes, submitted, errors = pages.parse_form(form, field_values)
        if not errors and changes:
            field_errors, general = pages.validate_change_set(
                changes, environ=self.server.webui_environ)
            errors.update(field_errors)
            if general:
                errors["_general"] = general
        if errors:
            self._send(400, self._config_html(
                field_values, errors=errors, submitted=submitted,
                theme=self._theme(),
                section=(form.get(pages.SECTION_FIELD) or [None])[0],
                reveal=(form.get(pages.REVEAL_FIELD) or [None])[0]),
                       "text/html; charset=utf-8")
            return
        if not changes:
            self._send(200, self._config_html(
                field_values, submitted=submitted, theme=self._theme(),
                section=(form.get(pages.SECTION_FIELD) or [None])[0],
                reveal=(form.get(pages.REVEAL_FIELD) or [None])[0],
                notice="No changes: every submitted value already matches "
                       "the effective settings."), "text/html; charset=utf-8")
            return
        effective = {fv.field: fv.value for fv in field_values}
        entries = [(field, effective[field], value) for field, value in changes.items()]
        self._send(200, pages.render_preview_page(
            entries, submitted, form_token=self._form_token(), theme=self._theme(),
            warnings=self._preview_warnings(changes),
            section=(form.get(pages.SECTION_FIELD) or [None])[0],
            reveal=(form.get(pages.REVEAL_FIELD) or [None])[0],
            rebinds=config_api.pending_rebinds(
                {field.id: value for field, value in changes.items()},
                environ=self.server.webui_environ)),
            "text/html; charset=utf-8")

    def _request_is_https(self):
        """Whether this request reached the Web UI over HTTPS. The server
        itself speaks plain HTTP; HTTPS arrives through a reverse proxy,
        which reports the original scheme as X-Forwarded-Proto."""
        forwarded = self.headers.get("X-Forwarded-Proto") or ""
        return forwarded.split(",")[0].strip().lower() == "https"

    def _preview_warnings(self, changes):
        """Plain-language warnings about a previewed change set. Turning
        the Secure session cookie on over plain HTTP strands browser
        sign-ins: browsers drop Secure cookies there, so the session
        cookie never comes back and there is no UI way back in — the
        preview is the place to say so before the save is confirmed."""
        warnings = []
        for field, value in changes.items():
            if field.id == "webui.cookie_secure" and value and not self._request_is_https():
                warnings.append(
                    "The Secure session cookie is being turned on, but this "
                    "request arrived over plain HTTP. Browsers drop Secure "
                    "cookies on plain HTTP, so browser sign-ins will not "
                    "stick once this takes effect. Turn it on only when the "
                    "Web UI is reached over HTTPS, for example behind an "
                    "HTTPS reverse proxy.")
        return warnings

    def _config_save(self, form):
        field_values = self._field_values()
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
            self._send(400, self._config_html(
                field_values, errors=errors, submitted=submitted,
                theme=self._theme(),
                section=(form.get(pages.SECTION_FIELD) or [None])[0],
                reveal=(form.get(pages.REVEAL_FIELD) or [None])[0]),
                       "text/html; charset=utf-8")
            return
        try:
            changed, warnings = config_api.write_changes_reporting(
                {field.id: value for field, value in changes.items()},
                environ=self.server.webui_environ,
                rebind=form.get(pages.REBIND_FIELD) or ())
        except config_api.WebUIConfigError as exc:
            field_errors, general = pages.attribute_error(
                str(exc), changes, environ=self.server.webui_environ)
            errors = dict(field_errors)
            if general:
                errors["_general"] = general
            self._send(400, self._config_html(
                field_values, errors=errors, submitted=submitted,
                theme=self._theme(),
                section=(form.get(pages.SECTION_FIELD) or [None])[0],
                reveal=(form.get(pages.REVEAL_FIELD) or [None])[0]),
                       "text/html; charset=utf-8")
            return
        self._send(200, pages.render_result_page(
            changed, warnings=warnings,
            theme=self._theme()), "text/html; charset=utf-8")

    def _debug_post(self, form):
        """POST /debug (#478): turn debug log mode on or off.

        Same ``session+csrf`` policy as a config POST (routes.py). The
        mode is shared state in the state folder, so this one write
        reaches the bot and every listener process; nothing about
        printing changes.
        """
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
            snap, form_token=self._form_token(), theme=self._theme(),
            walkthrough_pending=walkthrough_module.pending(self.server.settings_data),
            job=_overall_job(self.server.settings_data), error=message),
            "text/html; charset=utf-8")

    def _read_only(self, *_ignored):
        self._json(405, {"ok": False,
                         "reason": "the Web UI is read-only here (GET only; "
                                   "configuration posts go to /config)"})


def make_server(data=None, *, environ=None, host=None, port=None, snapshot_func=None,
                printer_status_func=None, printer_control_func=None,
                camera_snapshot_func=None, screen_snapshot_func=None,
                screen_probe_func=None, spools_view_func=None, spools_action_func=None,
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
    try:
        server = _BoundedThreadingHTTPServer((bind_host, bind_port), _Handler)
    except OSError as exc:
        # A bind failure (port already in use, address not available) is a
        # start refusal like the ones check_start raises: name it the same
        # way, so serve() prints it and exits 2 instead of dying with a raw
        # OSError traceback (#1042).
        raise WebUIServerError(
            f"webui: not started: could not bind {bind_host}:{bind_port} "
            f"({exc.strerror or exc}); another process may already be using "
            "that port — stop it or set a different [webui] port") from exc
    server.settings_data = data
    setattr(server, "cookie_secure", bool((data or {}).get("webui", {}).get("cookie_secure", False)))
    server.access = access_module.resolve(data, bind_host, environ)
    _log_broken_access(server)
    server.access_stamp = _access_stamp(data)
    server.access_sync_lock = threading.Lock()
    # Walkthrough POSTs (the Login step's claim above all) run one at a time.
    server.claim_lock = threading.Lock()
    server.bind_host = bind_host
    # Web chat's service, built lazily under this lock on the first
    # /chat request (see _Handler._chat_service).
    server.chat_service = None
    server.chat_service_lock = threading.Lock()
    # The config page reads and writes through the same environment the
    # settings were loaded with (PRINT_HANDY_CONFIG resolution included).
    server.webui_environ = environ
    # Per-server form token for config POSTs (pages.CSRF_FIELD): generated
    # here, kept only in memory, never the access token.
    server.form_token = secrets.token_urlsafe(32)
    # The key session-bound form tokens are derived from on a claimed
    # install (_Handler._session_form_token); memory only, like the token.
    server.csrf_secret = secrets.token_bytes(32)
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
    # Spool files left by a process that stopped mid-upload would count
    # toward the inbox quota forever (the upload page cannot list them).
    upload_module.prune_stale_spool(data)
    # The reverse proxies whose X-Forwarded-For names the client for the
    # throttle; none by default, so the header is never read (#1145).
    server.trusted_proxies = settings.trusted_proxy_networks(
        (data or {}).get("webui", {}).get("trusted_proxies", ""))
    server.snapshot_func = snapshot_func or (
        lambda: status_module.snapshot(data, environ=environ))
    # GET /api/status (polled by Home Assistant) reuses one snapshot for a
    # few seconds; the status page and every control route stay live.
    server.api_snapshot_func = status_module.SnapshotCache(server.snapshot_func).get
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
    server.screen_snapshot_func = screen_snapshot_func or (
        lambda printer_id: screen_module.snapshot(data, printer_id))
    server.screen_probe_func = screen_probe_func or (
        lambda printer_id: screen_module.probe(data, printer_id))
    server.spools_view_func = spools_view_func or (
        lambda printer_id: spools_module.spools_view(data, printer_id))
    server.spools_action_func = spools_action_func or (
        lambda printer_id, action, body: spools_module.spool_action(data, printer_id, action, body))
    return server


def chat_service_for(server):
    """The server's one web chat service, built on first use.

    Built lazily (the service spawns a BotCore and its own event-loop
    thread, so a server that never serves /chat must not pay for it), but
    the build is serialized: this server is threaded, and two concurrent
    first requests must not each build one — the loser's service would be
    orphaned when its assignment is overwritten, its replies rendered
    nowhere.
    """
    service = getattr(server, "chat_service", None)
    if service is not None:
        return service
    with _chat_lock(server):
        service = getattr(server, "chat_service", None)
        if service is None:
            from ..bot.web import WebChatService
            service = WebChatService(server.settings_data)
            server.chat_service = service
        return service


def start_web_alerts(server):
    """Start web chat with the server when it has alerts to deliver.

    Web chat delivers the bot heartbeat's alerts (bot/web.py
    HEARTBEAT_CHECKS: a ZMod update alert with its Update and Skip this
    version buttons), so waiting for the first /chat request would hold
    an alert back until someone happened to open the page. Started only
    when the page's switch is on, ``[firmware] update_alerts`` is on and
    the roster has a printer that gets alerts; otherwise the service stays
    lazy, as before. Returns the service, or None. Never raises: a failure
    is logged and the page builds the service on first use as before.
    """
    try:
        data = server.settings_data
        from ..bot import firmware_alert
        from ..bot.web import chat_enabled
        if not (chat_enabled(data) and firmware_alert.enabled(data)
                and firmware_alert.alert_printers(data)):
            return None
        return chat_service_for(server)
    except Exception:
        log.exception("could not start web chat alerts")
        return None


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
    start_web_alerts(server)
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
