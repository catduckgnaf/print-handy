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
- ``POST /api/upload`` — the header-token upload (#1548): multipart,
  one file into the same inbox and under the same intake rules as
  ``POST /upload``, answering in the API JSON shape for clients that
  cannot use session cookies, CSRF form tokens or minted links;
- ``POST /api/printers/<id>/nozzle`` — record a nozzle change
  (#1548, same controls module and guard): the record step of the
  guided ``/nozzle change`` flow for a nozzle already fitted; it
  writes only the hardware record and sends nothing to the printer;
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
  labeled page (read-only while controls are off; tappable while
  they are on, through ``POST /printers/<id>/screen/touch``, #1626);
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
- ``GET /designs`` and ``GET /designs/<id>`` — the design workshop over
  the catalog of AI design sessions (#388, stage 3; #1449;
  :mod:`print_handy.webui.designs`), with per-session file downloads
  under ``GET /designs/<id>/<scad|stl|preview>`` (``preview?turn=N``
  for one turn's). The workshop is a conversation (#1636): ``POST
  /designs`` (its message box) and ``POST /designs/choice`` (a
  question's button) submit through this browser's design-thread view
  of web chat, so a design started here runs the chat design dispatch
  itself, not a page-side pipeline; ``GET /designs/state`` is its poll,
  ``POST /designs/new`` starts a fresh thread and ``POST
  /designs/<id>/continue`` reopens one of this browser's own.
- ``GET /walkthrough`` and ``POST /walkthrough`` — the first-run
  walkthrough (#813; :mod:`print_handy.webui.walkthrough`), offered to
  every install that has not finished it; its steps write through the
  config path above, and its Login step is where a password is set.
- ``GET /theme?set=dark|light&back=<path>`` — the header theme toggle
  (#813; :mod:`print_handy.webui.shell`): sets the ``ph-theme`` cookie
  and redirects back. A browser preference only, behind the same
  authorization as every other page.
- ``GET /manifest.webmanifest``, ``GET /sw.js``, ``GET /offline``
  and ``GET /static/<file>`` — the PWA install plumbing (#450
  Phase 1; :mod:`print_handy.webui.pwa`), declared ``public`` in the
  route table like the login form: a browser fetches them before any
  sign-in, and they carry no state or printer data. The static route
  is the favicon change's icon route (#1456): its allowlist carries
  the branding icons and the PWA maskable icon alike.

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
import hmac
import html
import ipaddress
import json
import shutil
import logging
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from .. import __version__, logs, settings
from ..bot import health as bot_health
from . import access as access_module
from . import body as body_module
from . import camera as camera_module
from . import controls as controls_module
from . import pages
from . import routes as routes_module
from . import screen as screen_module
from . import shell
from . import spools as spools_module
from . import status as status_module
from . import upload as upload_module
from . import walkthrough as walkthrough_module
from . import sessions as sessions_module
from . import status_handlers as status_handlers_module
from . import data_handlers as data_handlers_module
from . import diagnostics as diagnostics_module
from . import printer_handlers as printer_handlers_module
from . import design_handlers as design_handlers_module
from . import firmware_handlers as firmware_handlers_module
from . import notifications as notifications_module
from . import config_handlers as config_handlers_module
from . import chat_handlers as chat_handlers_module
from . import upload_handlers as upload_handlers_module
from . import walkthrough_handlers as walkthrough_handlers_module
from . import cost_handlers as cost_handlers_module
from .sessions import (REMEMBER_TTL_S, SESSIONS_FILE, SESSION_COOKIE,
                       SESSION_TTL_S, SessionStore, _access_stamp,
                       _load_remembered_sessions, _remember_lock,
                       _save_remembered_sessions, _session_digest,
                       _session_store, session_form_token, sessions_path)
from .sessions import _FINGERPRINT_KEY
from .chat_handlers import CHAT_COOKIE, _CHAT_COOKIE_VALUE
from .status_handlers import ICON_FILES, STATIC_DIR, healthz, render_status_html, _overall_job

log = logging.getLogger(__name__)

# Re-export surface (#1603 item 3): names that lived in this module
# before the handler split and are imported (or patched) as
# ``server.<name>`` by tests and embedders. The implementations now
# live in webui/sessions.py, webui/status_handlers.py and
# webui/chat_handlers.py; listing them here keeps them importable
# from webui.server exactly as before.
__all__ = [
    "CHAT_COOKIE", "ICON_FILES", "REMEMBER_TTL_S", "SESSIONS_FILE",
    "SESSION_COOKIE", "SESSION_TTL_S", "STATIC_DIR", "SessionStore",
    "_CHAT_COOKIE_VALUE", "_FINGERPRINT_KEY", "_overall_job",
    "_remember_lock", "_save_remembered_sessions", "_session_digest",
    "_session_store", "bot_health",
    "healthz", "render_status_html", "session_form_token", "sessions_path",
    "walkthrough_module",
]

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
# The web chat's per-browser cookie on an install with no login (#1145):
# a random id that scopes this browser's chat transcript. With a login,
# the login session scopes it instead and this cookie is not used.
# Remembered sessions ("Keep me signed in on this browser" on the login
# form): a 30-day session whose id is persisted only as its SHA-256 hash
# in SESSIONS_FILE in the state folder, so it survives a server restart.
# The raw id is never written to disk.


_FORWARDED_WARN_LOCK = threading.Lock()


def _warn_untrusted_forwarded_for(server):
    """Log (once per server) that X-Forwarded-For arrived while
    ``[webui] trusted_proxies`` is empty (#1588 item 1).

    The header is ignored in that configuration, so every client behind
    the proxy shares the proxy's address for login throttling: one
    client's failed logins can lock out the others, the Home Assistant
    integration included. The warning names the setting that fixes it;
    it fires on the first forwarded request only, never per request."""
    with _FORWARDED_WARN_LOCK:
        if getattr(server, "_forwarded_for_warned", False):
            return
        try:
            server._forwarded_for_warned = True
        except (AttributeError, TypeError):
            return
    log.warning(
        "webui: a request carried X-Forwarded-For but [webui] trusted_proxies "
        "is empty, so the header is ignored and every client behind the proxy "
        "shares one address for login throttling: one client's failed logins "
        "can lock out the others. List the proxy in [webui] trusted_proxies "
        "if a reverse proxy fronts the Web UI. [PH_WEBUI_UNTRUSTED_PROXY]")


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


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with at most ``max_threads`` requests in flight.

    The accept loop waits for a free slot instead of starting a new
    thread per connection without bound; with the handler's socket
    timeout, a stalled client gives its slot back."""

    max_threads = MAX_HANDLER_THREADS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(self.max_threads)

    # Session state lives in one SessionStore (#1588 item 4); the names
    # the state used to be stored under directly on the server object
    # delegate to it, so existing readers (and tests that seed a session
    # by writing ``server.sessions[id]``) keep working on the same data.
    @property
    def session_store(self):
        store = self.__dict__.get("_session_store")
        if store is None:
            store = SessionStore()
            self.__dict__["_session_store"] = store
        return store

    @session_store.setter
    def session_store(self, store):
        self.__dict__["_session_store"] = store

    @property
    def sessions(self):
        return self.session_store.sessions

    @sessions.setter
    def sessions(self, value):
        store = self.session_store
        with store.lock:
            store.sessions.clear()
            store.sessions.update(value or {})

    @property
    def remembered_sessions(self):
        return self.session_store.remembered_sessions

    @remembered_sessions.setter
    def remembered_sessions(self, value):
        store = self.session_store
        with store.lock:
            store.remembered_sessions.clear()
            store.remembered_sessions.update(value or {})

    @property
    def remembered_lock(self):
        return self.session_store.lock

    @remembered_lock.setter
    def remembered_lock(self, lock):
        self.session_store.lock = lock

    @property
    def form_token(self):
        return self.session_store.form_token

    @form_token.setter
    def form_token(self, value):
        self.session_store.form_token = value

    @property
    def csrf_secret(self):
        return self.session_store.csrf_secret

    @csrf_secret.setter
    def csrf_secret(self, value):
        self.session_store.csrf_secret = value

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
    def log_message(self, *args):  # stdlib access logs are intentionally disabled
        log.debug("Web UI HTTP access log suppressed [PH_WEBUI_ACCESS]")

    def finish(self):
        # A refused body (_refuse_body) is drained after the answer is
        # written and before the socket closes, so the client reads the
        # answer instead of a reset.
        unread, self._unread_body = getattr(self, "_unread_body", 0), 0
        if unread:
            try:
                self.wfile.flush()
                _drain(self.connection, self.rfile, unread)
            except (OSError, ValueError) as exc:
                log.debug("client disconnected while draining refused request body [PH_WEBUI_DRAIN]: %s", exc)
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

    def _refuse_unread_body(self):
        """Refuse a body without reading it: the connection closes after
        the answer, so the unread bytes are never parsed as the next
        request on a keep-alive connection (request desync)."""
        try:
            unread = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            unread = 0
        self._refuse_body(unread)

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
        if post and origin:
            # "null" is an opaque origin (a sandboxed frame, a data: page):
            # never this Web UI's own page, which sends its real origin
            # (Referrer-Policy is same-origin). It counts as a mismatch.
            names.append("null" if origin == "null" else hostname(urlparse(origin).netloc))
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
        if code == 200 and content_type.startswith("text/html"):
            data = self._inject_toasts(data)
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

    def _send_file(self, path, content_type, headers=()):
        """Send a file as the response body in chunks (review of #1464).

        A sliced plate can be well over 100 MB; reading it whole per request
        (and for a HEAD request, only to discard it) is memory the box does
        not need to spend. The length comes from stat, the body is copied
        in fixed-size pieces."""
        try:
            size = path.stat().st_size
            handle = open(path, "rb")
        except OSError:
            self._json(404, {"ok": False, "reason": "that file could not be read"})
            return
        with handle:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            named = {name.lower() for name, _value in headers}
            for name, value in SECURITY_HEADERS:
                if name.lower() not in named:
                    self.send_header(name, value)
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                shutil.copyfileobj(handle, self.wfile, 1024 * 1024)

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
        return sessions_module.session_id(self)

    def _session_authorized(self):
        return sessions_module.session_authorized(self)

    def _note_forwarded_for(self):
        """Item 1 of #1588: warn once when a forwarded-for header arrives
        but no proxy is trusted (see _warn_untrusted_forwarded_for)."""
        if getattr(self.server, "trusted_proxies", ()):
            return
        if self.headers.get_all("X-Forwarded-For"):
            _warn_untrusted_forwarded_for(self.server)

    def _valid_session_id(self):
        return sessions_module.valid_session_id(self)

    def _session_form_token(self, session_id):
        return sessions_module.bound_form_token(self, session_id)

    def _form_token(self):
        return sessions_module.current_form_token(self)

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
        return config_handlers_module.field_values(self)

    def _config_html(self, field_values, **kwargs):
        return config_handlers_module.config_html(self, field_values, **kwargs)

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
        connection or the exception text (the log keeps the traceback
        under the id the response names). A
        method without its own routes gets the read-only refusal with no
        screens, as it always has."""
        self._note_forwarded_for()
        try:
            # One nav for the whole Web UI, from the live settings (shell.REQUEST_NAV).
            from . import data as _data
            shell.REQUEST_NAV.set(_data.nav_for(self.server.settings_data))
        except Exception:
            log.debug("request nav unavailable; the shell uses its default tabs", exc_info=True)
        transfer_codings = [coding.strip().lower()
                            for header in self.headers.get_all(
                                "Transfer-Encoding", [])
                            for coding in header.split(",")]
        if transfer_codings and transfer_codings != ["identity"]:
            # No transfer coding is decoded anywhere in the Web UI
            # (#1588 item 2): a chunked or otherwise coded body would be
            # read as length 0 and its bytes left on the keep-alive
            # connection to be parsed as the next request. Refuse the
            # unsupported codings and close the connection. The identity
            # coding applies no coding at all, so a request carrying it
            # beside a valid Content-Length is framed exactly by that
            # length and is served normally (duckcatgnaf review,
            # PR #1594).
            self.close_connection = True
            self._json(411, {"ok": False,
                             "reason": "chunked or otherwise "
                                       "transfer-encoded request bodies "
                                       "are not supported: resend the body "
                                       "unencoded with a Content-Length"})
            return
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
                if not self._post_authorized(form):
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
            if self._post_authorized(fields):
                return True
            self._html_refusal(403, STALE_FORM_MESSAGE, self._route_back(route, arg))
            return False
        return True

    # -- small routes -------------------------------------------------------
    def _healthz(self):
        return status_handlers_module.healthz_route(self)

    def _favicon_route(self):
        return status_handlers_module.favicon_route(self)

    def _static_route(self, name):
        return status_handlers_module.static_route(self, name)

    def _icon_file(self, name):
        return status_handlers_module.icon_file(self, name)

    def _setup_redirect(self):
        return status_handlers_module.setup_redirect(self)

    # -- PWA routes (#450 Phase 1, declared public in routes.py) ----------
    def _pwa_manifest(self):
        return status_handlers_module.pwa_manifest(self)

    def _pwa_service_worker(self):
        return status_handlers_module.pwa_service_worker(self)

    def _pwa_offline(self):
        return status_handlers_module.pwa_offline(self)

    def _status_page(self):
        return status_handlers_module.status_page(self)

    def _api_status(self):
        return status_handlers_module.api_status(self)

    def _api_printers(self):
        return status_handlers_module.api_printers(self)

    def _readyz(self):
        return status_handlers_module.readyz_route(self)

    def _spools_index(self):
        return printer_handlers_module.spools_index(self)

    def _config_page(self):
        return config_handlers_module.config_page(self)

    def _printer_settings_page(self):
        return config_handlers_module.printer_settings_page(self)

    def _printer_settings_save(self, form):
        return config_handlers_module.printer_settings_save(self, form)

    def _chat_state(self):
        if self._chat_design_mode():
            return self._designs_state()
        return chat_handlers_module.chat_state(self)

    def _upload_page(self):
        return upload_handlers_module.upload_page(self)

    def _sources_page(self):
        return design_handlers_module.sources_page(self)

    def _designs_page(self, notice=None, error=None):
        return design_handlers_module.designs_page(self, notice, error)

    def _designs_post(self, form, route="/designs"):
        return design_handlers_module.designs_post(self, form, route=route)

    def _designs_upload_post(self, form, route="/designs"):
        return design_handlers_module.designs_upload_post(self, form, route=route)

    def _designs_choice_post(self, form, route="/designs"):
        return design_handlers_module.designs_choice_post(self, form, route=route)

    def _designs_state(self):
        return design_handlers_module.designs_state(self)

    def _designs_new_post(self, form):
        return design_handlers_module.designs_new_post(self, form)

    def _designs_continue_post(self, rest, form):
        return design_handlers_module.designs_continue_post(self, rest, form)

    def _design_view(self, issue=True):
        return design_handlers_module.design_view(self, issue)

    def _firmware_route(self):
        return design_handlers_module.firmware_route(self)

    def _cost_page(self):
        return cost_handlers_module.cost_page(self)

    def _cost_post(self, form):
        return cost_handlers_module.cost_post(self, form)

    def _firmware_update_route(self):
        return firmware_handlers_module.update_confirm_route(self)

    def _firmware_update_post(self, form):
        return firmware_handlers_module.update_apply_route(self, form)

    def _firmware_update_status_route(self):
        return firmware_handlers_module.update_status_route(self)

    def _firmware_channel_route(self):
        return firmware_handlers_module.channel_route(self)

    def _firmware_channel_post(self, form):
        return firmware_handlers_module.channel_apply_route(self, form)

    def _firmware_channel_status_route(self):
        return firmware_handlers_module.channel_status_route(self)

    def _notifications_dismiss_post(self, form):
        return firmware_handlers_module.dismiss_route(self, form)

    def _inject_toasts(self, data):
        """Insert the current toast stack after a shell page's header (#1610).

        Only full shell pages carry the site header; anything else (the
        login page, downloads, fragments) passes through untouched. The
        notification store reads state files only — no printer traffic —
        and ``toasts_for_page`` never raises.
        """
        if b'<header class="site">' not in data:
            return data
        settings_data = getattr(self.server, "settings_data", None)
        if not settings_data:
            return data
        try:
            form_token = self._form_token()
        except Exception:  # noqa: BLE001 - no session token: the page goes out without toasts
            log.debug("no form token for the toast Dismiss form", exc_info=True)
            return data
        toasts = notifications_module.toasts_for_page(
            settings_data, back=urlparse(self.path).path or "/", form_token=form_token)
        if not toasts:
            return data
        marker = b"</header>"
        at = data.find(marker)
        if at < 0:
            return data
        return data[:at + len(marker)] + toasts.encode("utf-8") + data[at + len(marker):]

    def _settings_report_route(self, rest=""):
        return design_handlers_module.settings_report_route(self, rest)

    def _conversion_report_route(self, rest=""):
        return design_handlers_module.conversion_report_route(self, rest)

    def _editable_export_route(self, rest=""):
        return design_handlers_module.editable_export_route(self, rest)

    def _sliced_route(self, rest=""):
        return design_handlers_module.sliced_route(self, rest)

    def _slice_result_route(self, rest=""):
        return design_handlers_module.slice_result_route(self, rest)

    def _u1_feasibility_route(self, rest=""):
        return design_handlers_module.u1_feasibility_route(self, rest)

    def _external_intake_route(self, notice=None, error=None):
        return design_handlers_module.external_intake_route(self, notice, error)

    def _external_intake_confirm_post(self, form):
        return design_handlers_module.external_intake_confirm_post(self, form)

    def _not_found(self, path):
        return status_handlers_module.not_found(self, path)

    # -- Data tab and Wrapped (#956) ---------------------------------------
    def _year_param(self):
        return data_handlers_module.year_param(self)

    def _data_route(self, path):
        return data_handlers_module.data_route(self, path)

    def _diagnostics_download_route(self):
        return diagnostics_module.diagnostics_route(self)

    def _wrapped_video_file_route(self):
        return data_handlers_module.wrapped_video_file_route(self)

    VIDEO_CHUNK = 256 * 1024

    def _stream_file(self, target, content_type):
        return data_handlers_module.stream_file(self, target, content_type)

    def _wrapped_video_status_route(self):
        return data_handlers_module.wrapped_video_status_route(self)

    def _wrapped_video_post(self, form):
        return data_handlers_module.wrapped_video_post(self, form)

    def _wrapped_reel_file_route(self):
        return data_handlers_module.wrapped_video_file_route(self, kind="reel")

    def _wrapped_reel_status_route(self):
        return data_handlers_module.wrapped_video_status_route(self, kind="reel")

    def _wrapped_reel_post(self, form):
        return data_handlers_module.wrapped_video_post(self, form, kind="reel")

    def _wrapped_share_post(self, form):
        return data_handlers_module.wrapped_share_post(self, form)

    # -- per-printer live status (#664) -------------------------------------
    def _printer_status_route(self, rest):
        return printer_handlers_module.printer_status_route(self, rest)

    def _printer_post_route(self, rest):
        return printer_handlers_module.printer_post_route(self, rest)

    # -- per-printer controls (#708, control endpoints phase) ----------------
    def _printer_control_route(self, rest):
        return printer_handlers_module.printer_control_route(self, rest)

    def _dashboard_estop_post(self, printer_id, form):
        return printer_handlers_module.dashboard_estop_post(self, printer_id, form)

    # -- per-printer spools (#789) -------------------------------------------
    def _printer_spools_route(self, rest):
        return printer_handlers_module.printer_spools_route(self, rest)

    def _spools_error_page(self, printer_id, code, reason):
        return printer_handlers_module.spools_error_page(self, printer_id, code, reason)

    def _printers_page(self, notice=None, error=None, code=200):
        return printer_handlers_module.printers_page(self, notice, error, code)

    def _printers_post(self, form):
        return printer_handlers_module.printers_post(self, form)

    def _screen_page(self, printer_id):
        return printer_handlers_module.screen_page(self, printer_id)

    def _screen_touch_post(self, printer_id, form):
        return printer_handlers_module.screen_touch_post(self, printer_id, form)

    def _screen_pair_post(self, printer_id, form):
        return printer_handlers_module.screen_pair_post(self, printer_id, form)

    def _spools_page(self, printer_id):
        return printer_handlers_module.spools_page(self, printer_id)

    def _spools_page_post(self, printer_id, form):
        return printer_handlers_module.spools_page_post(self, printer_id, form)

    # -- designs catalog and files (#388 stage 3) ----------------------------
    def _design_route(self, rest):
        return design_handlers_module.design_route(self, rest)

    # -- theme toggle (#813) --------------------------------------------------
    def _theme_route(self):
        return status_handlers_module.theme_route(self)

    def _sync_access(self):
        return walkthrough_handlers_module.sync_access(self)

    def _rotate_form_token(self):
        return sessions_module.rotate_form_token(self)

    def _revoke_remembered_sessions(self):
        return sessions_module.revoke_remembered_sessions(self)

    def _revoke_upload_links(self):
        return sessions_module.revoke_upload_links(self)

    # -- first-run walkthrough (#813) -----------------------------------------
    def _walkthrough_data(self):
        return walkthrough_handlers_module.walkthrough_data(self)

    def _walkthrough_page(self):
        return walkthrough_handlers_module.walkthrough_page(self)

    def _walkthrough_post(self, form):
        return walkthrough_handlers_module.walkthrough_post(self, form)

    def _login_change_refused(self, form):
        return walkthrough_handlers_module.login_change_refused(self, form)

    def _walkthrough_apply(self, form):
        return walkthrough_handlers_module.walkthrough_apply(self, form)

    # -- browser login ------------------------------------------------------
    def _login_page(self):
        return sessions_module.login_page(self)

    def _password_mode(self):
        return sessions_module.password_mode(self)

    def _username_mode(self):
        return sessions_module.username_mode(self)

    def _cookie_secure(self):
        return sessions_module.cookie_secure(self)

    def _start_session(self, remember=False):
        return sessions_module.start_session(self, remember)

    def _login_submit(self):
        return sessions_module.login_submit(self)

    def _rehash_after_login(self, password):
        return sessions_module.rehash_after_login(self, password)

    def _logout(self):
        return sessions_module.logout(self)

    # -- config writes (stage 3) -------------------------------------------
    def _read_form(self):
        """(form, None) or (None, (code, reason)) for a urlencoded POST body.

        The reading itself lives in webui/body.py (#1590 item 8)."""
        return body_module.read_form(self)

    def _post_authorized(self, form):
        """Whether a state-changing form POST may proceed.

        While access is open (unclaimed or deliberately left open) the
        per-process form token embedded in the page is the guard, exactly
        as before: a cross-origin page cannot read one.

        On a claimed install (a password or token is required) the form
        token is no longer a credential on its own: the request needs the
        access-token header, or a live login session AND the ``_csrf``
        value bound to that session (see _session_form_token). A session
        cookie alone never authorizes a POST, and a page token copied
        from one browser authorizes nothing for another. There is one
        token kind per posture: every page, the Wrapped page included,
        embeds ``_form_token()`` (#1588 item 3 removed the second,
        process-token acceptance the Wrapped routes used to carry)."""
        provided = (form.get(pages.CSRF_FIELD) or [""])[0]
        if not self.server.access.required:
            return bool(provided) and hmac.compare_digest(provided, self.server.form_token)
        if self._header_authorized():
            return True
        session_id = self._valid_session_id()
        if not (session_id and provided):
            return False
        return hmac.compare_digest(provided, self._session_form_token(session_id))

    # -- web chat -------------------------------------------------------------

    def _chat_service(self):
        return chat_service_for(self.server)

    def _web_chat_enabled(self):
        return chat_handlers_module.web_chat_enabled(self)

    def _chat_page(self, notice=None, error=None):
        return chat_handlers_module.chat_page(self, notice, error)

    def _cookie_value(self, name):
        return chat_handlers_module.cookie_value(self, name)

    def _chat_view(self, issue=True):
        return chat_handlers_module.chat_view(self, issue)

    def _chat_cookie_headers(self):
        return chat_handlers_module.chat_cookie_headers(self)

    def _chat_post(self, form):
        return chat_handlers_module.chat_post(self, form)

    def _chat_design_mode(self):
        return parse_qs(urlparse(self.path).query).get("view") == ["design"]

    def _chat_upload_post(self, form):
        if self._chat_design_mode():
            return self._designs_upload_post(form, route="/chat?view=design")
        return chat_handlers_module.chat_upload_post(self, form)

    def _chat_design_new_post(self, form):
        return design_handlers_module.designs_new_post(self, form, route="/chat?view=design")

    def _chat_choice_post(self, form):
        if self._chat_design_mode():
            return self._designs_choice_post(form, route="/chat?view=design")
        return chat_handlers_module.chat_choice_post(self, form)

    # -- workflow (#1450) ---------------------------------------------------

    def _workflow_page(self, notice=None, error=None):
        return chat_handlers_module.workflow_page(self, notice, error)

    def _workflow_state(self):
        return chat_handlers_module.workflow_state(self)

    def _workflow_busy(self, view):
        return chat_handlers_module.workflow_busy(self, view)

    def _workflow_link_post(self, form):
        return chat_handlers_module.workflow_link_post(self, form)

    def _workflow_choice_post(self, form):
        return chat_handlers_module.workflow_choice_post(self, form)

    def _workflow_start_post(self, form):
        return chat_handlers_module.workflow_start_post(self, form)

    def _workflow_upload_post(self, form):
        return chat_handlers_module.workflow_upload_post(self, form)

    def _workflow_download_route(self, rest):
        return chat_handlers_module.workflow_download_route(self, rest)

    # -- uploads ------------------------------------------------------------

    def _credential_fingerprint(self):
        """The fingerprint upload links are minted and checked under."""
        return self.server.access.credential_fingerprint()

    def _upload_page_html(self, notice=None, error=None):
        return upload_handlers_module.upload_page_html(self, notice, error)

    def _receive_upload(self, handle):
        return upload_handlers_module.receive_upload(self, handle)

    def _upload_post(self, form):
        return upload_handlers_module.upload_post(self, form)

    def _api_upload_post(self):
        return upload_handlers_module.api_upload_post(self)

    def _link_valid(self, token):
        return upload_module.link_valid(self.server.settings_data, token,
                                        self._credential_fingerprint())

    def _token_upload_page(self, _token):
        return upload_handlers_module.token_upload_page(self, _token)

    def _token_upload_submit(self, token):
        return upload_handlers_module.token_upload_submit(self, token)

    def _config_preview(self, form):
        return config_handlers_module.config_preview(self, form)

    def _request_is_https(self):
        return config_handlers_module.request_is_https(self)

    def _preview_warnings(self, changes):
        return config_handlers_module.preview_warnings(self, changes)

    def _config_save(self, form):
        return config_handlers_module.config_save(self, form)

    def _config_field_save(self, form):
        return config_handlers_module.config_field_save(self, form)

    def _debug_post(self, form):
        return config_handlers_module.debug_post(self, form)

    def _debug_error(self, message):
        return config_handlers_module.debug_error(self, message)

    def _read_only(self, *_ignored):
        self._json(405, {"ok": False,
                         "reason": "the Web UI is read-only here (GET only; "
                                   "configuration posts go to /config)"})


def make_server(data=None, *, environ=None, host=None, port=None, snapshot_func=None,
                printer_status_func=None, printer_control_func=None,
                camera_snapshot_func=None, screen_snapshot_func=None,
                screen_probe_func=None, screen_touch_func=None,
                screen_pair_func=None,
                spools_view_func=None, spools_action_func=None,
                readiness_func=None, roster_func=None):
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
    # All session state lives in one SessionStore (#1588 item 4): the
    # per-server form token for config POSTs (pages.CSRF_FIELD, generated
    # at start, kept only in memory, never the access token), the secret
    # session-bound form tokens are derived from on a claimed install
    # (_Handler._session_form_token; memory only, like the token), the
    # ephemeral browser login sessions ({session id: expiry epoch};
    # memory only — a restart signs these out) and the remembered
    # sessions ({sha256(session id): expiry epoch}, loaded from
    # webui_sessions.json in the state folder — expired entries pruned
    # on load, and the whole store discarded when the credential
    # fingerprint it was minted under no longer matches — and re-saved
    # on every change, so a browser that ticked "Keep me signed in"
    # survives a restart; the raw id is never written to disk).
    # Mutations and persistence are serialized with the store's lock
    # (server.remembered_lock delegates to it).
    server.session_store = SessionStore(_load_remembered_sessions(
        data, server.access.credential_fingerprint()))
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
    server.roster_func = roster_func or (lambda: status_module.roster(data))
    server.printer_status_func = printer_status_func or (
        lambda printer_id: status_module.printer_status(data, printer_id))
    server.printer_control_func = printer_control_func or (
        lambda printer_id, verb, body: _printer_control(data, printer_id, verb, body))
    server.camera_snapshot_func = camera_snapshot_func or (
        lambda printer_id: camera_module.snapshot(data, printer_id))
    server.screen_snapshot_func = screen_snapshot_func or (
        lambda printer_id: screen_module.snapshot(data, printer_id))
    server.screen_probe_func = screen_probe_func or (
        lambda printer_id: screen_module.probe_source(data, printer_id))
    server.screen_touch_func = screen_touch_func or (
        lambda printer_id, x, y: screen_module.touch(data, printer_id, x, y))
    server.screen_pair_func = screen_pair_func or (
        lambda printer_id, code: screen_module.pair(data, printer_id, code))
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
    version buttons) and the listener's print posts (bot/web_feed.py:
    started, paused, complete, cancelled, error, with an alert's buttons),
    so waiting for the first /chat request would hold them back until
    someone happened to open the page, and the feed only carries posts
    from the moment web chat starts. Every printer gets print posts, so
    it starts whenever the page's switch is on; with it off the service
    stays lazy, as before. Returns the service, or None. Never raises: a
    failure is logged and the page builds the service on first use.
    """
    try:
        data = server.settings_data
        from ..bot.web import chat_enabled
        if not chat_enabled(data):
            return None
        return chat_service_for(server)
    except Exception:
        log.exception("could not start web chat alerts")
        return None


def _printer_control(data, printer_id, verb, body):
    if verb == "nozzle":
        return controls_module.record_nozzle(data, printer_id, body)
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
    # The Web UI is a Print Handy process too (#1404): join the shared
    # logging (and crash capture, role "webui") before serving, so its
    # failures land in the same log and crash record as every other
    # process instead of only on this process's stderr.
    logs.configure((data or {}).get("logging") if isinstance(data, dict) else None,
                   role="webui")
    log.info("webui starting (Print Handy %s)", __version__)
    try:
        server = make_server(data, environ=environ)
    except WebUIServerError as exc:
        print(f"{str(exc)} [PH_WEBUI_CONFIGURATION]", file=sys.stderr)
        return 2
    start_web_alerts(server)
    host, port = server.server_address[:2]
    print(f"webui: server on http://{host}:{port}/ (status page; "
          f"configuration at /config, Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Web UI shutdown requested [PH_WEBUI_SHUTDOWN]")
    finally:
        server.server_close()
    return 0


def main(argv=None):
    """``python3 -m print_handy.webui``: load settings and serve (stage 2)."""
    try:
        data = settings.load()
    except settings.SettingsError as exc:
        print(f"webui: configuration unresolved: {exc} [PH_WEBUI_CONFIGURATION]", file=sys.stderr)
        return 2
    return serve(data)


if __name__ == "__main__":
    sys.exit(main())
