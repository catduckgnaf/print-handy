"""Web UI HTTP server (#286, stages 2-3).

A stdlib-only HTTP server (``http.server``; no new dependency, no build
step) serving:

- ``GET /`` — a simple server-rendered status page (plain HTML, every
  interpolated value escaped, no JavaScript);
- ``GET /api/status`` — the status snapshot as JSON
  (:mod:`print_handy.webui.status`);
- ``GET /api/printers/<id>/status`` — one roster printer's live status
  as JSON (:func:`print_handy.webui.status.printer_status`, #664): the
  adapter's normalized status, read live; an unknown id is a 404 and an
  unreachable printer answers with an explicit unavailable status;
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

Unknown GET paths are 404. Writes happen only through the config page's
two POSTs (``POST /config/preview`` renders the diff and writes nothing;
``POST /config`` with the confirm field performs the validated write);
every other non-GET method or path is 405.

The server starts only when ``[webui] enabled`` is true and ``port`` is
above 0. Access control: the access token lives only in the environment
variable ``[webui] access_token_env`` names (default
``WEBUI_ACCESS_TOKEN``) and is sent as the ``X-WebUI-Token`` header,
compared with ``hmac.compare_digest``. When the bind host is not
loopback, a token is mandatory — an empty token refuses the start with a
clear error, because a LAN-reachable status page must not be open. On
loopback the token is optional, and honoured when set. The token value
itself is never rendered, logged, or included in any response.

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
turned into a CSRF path). Sessions live only in server memory with a
``SESSION_TTL_S`` lifetime — a restart signs everyone out — and
``POST /logout`` ends the caller's own session. ``/healthz`` stays
unauthenticated: the container healthcheck probes it. With no token
configured there is nothing to log in to: ``/login`` redirects to ``/``
and the loopback posture is otherwise unchanged.
"""
from __future__ import annotations

import hmac
import html
import ipaddress
import json
import os
import secrets
import sys
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .. import settings
from ..bot import health as bot_health
from . import config as config_api
from . import designs as designs_module
from . import layers as layers_module
from . import pages
from . import status as status_module

TOKEN_HEADER = "X-WebUI-Token"
LOOPBACK_NAMES = frozenset({"localhost"})
# Browser login sessions: the cookie carries only an opaque id; the
# id -> expiry map lives in server memory (see make_server), so nothing
# is persisted and a restart signs everyone out. No Secure flag: the
# documented posture is plain HTTP on a trusted LAN (Unraid WebUI link).
SESSION_COOKIE = "webui_session"
SESSION_TTL_S = 12 * 60 * 60  # 12 hours


class WebUIServerError(RuntimeError):
    """The Web UI server was refused: disabled, unconfigured, or unsafe."""


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
    """Refuse an unsafe or unconfigured start; return (host, port, token)."""
    webui = data["webui"]
    if not webui["enabled"]:
        raise WebUIServerError(
            "webui: not started: [webui] enabled is false (the Web UI is "
            "optional and off by default; set enabled = true and a port to run it)")
    port = int(webui["port"] or 0)
    if port <= 0:
        raise WebUIServerError(
            "webui: not started: [webui] port is 0 (unset); set a port above 0 to run it")
    host = webui["host"]
    token = access_token(data, environ)
    if webui.get("require_auth", True) and not is_loopback(host) and not token:
        raise WebUIServerError(
            f"webui: refusing to bind {host}: a non-loopback bind requires an "
            f"access token in ${webui['access_token_env']} (the environment variable "
            f"[webui] access_token_env names); set the token, bind 127.0.0.1, or "
            f"set [webui] require_auth = false on a trusted network")
    return host, port, token


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
    ok, reason = bot_health.evaluate(heartbeat, stale_s=data["bot"]["health_stale_s"])
    body = {"ok": ok, "reason": reason}
    if isinstance(heartbeat, dict):
        body.update({key: heartbeat[key] for key in status_module.HEARTBEAT_KEYS
                     if key in heartbeat})
    return (200 if ok else 503), body


def render_status_html(snap, form_token=""):
    """The / page: the snapshot as plain escaped HTML tables. No scripts."""
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
         + ("ok" if spoolman["ok"] else "down") + f" ({spoolman['detail']})"),
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
        roster_table = ("<h2>Printers (read-only)</h2><p>The roster is edited in the config "
                        "file or the setup wizard, not here (stage 2e decision, docs/WEBUI.md).</p>"
                        "<table><tr><th>id</th><th>name</th><th>model</th><th>connection</th>"
                        f"<th>channel</th><th>endpoint</th></tr>{body}</table>")
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
            '<button name="action" value="off" type="submit">Turn off</button>'
            "</form>")
    main_rows = "".join(
        f"<tr><th>{esc(label)}</th><td>{esc(str(value))}</td></tr>" for label, value in rows)
    return ("<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
            f"<title>Print Handy {esc(snap['version'])} — status</title></head><body>"
            "<h1>Print Handy status</h1>"
            f"<p>Overall: {'ok' if snap['ok'] else 'attention needed'} (read-only page; "
            "settings are changed on the <a href=\"/config\">configuration page</a>, "
            "which writes the YAML settings file; AI designs are listed on the "
            "<a href=\"/designs\">designs page</a>; sliced plates are on the "
            "<a href=\"/layers\">layers page</a>)</p>"
            f"<table>{main_rows}</table>"
            + roster_table
            + (f"<h2>Services</h2><table>{services}</table>" if services else "")
            + f"<h2>Doctor checks</h2><table>{doctor}</table>"
            + debug_section
            + "</body></html>\n")


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
        """
        allowed = self.server.allowed_hosts
        if allowed is None:
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
        if all(name in allowed for name in names):
            return False
        self._send(403, "Forbidden: this Web UI answers only to its own loopback address.",
                   "text/plain; charset=utf-8")
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

    # -- auth ---------------------------------------------------------------
    def _header_authorized(self):
        """The access-token header alone (the stage 2 rule)."""
        token = self.server.webui_token
        if not token:
            return True
        provided = self.headers.get(TOKEN_HEADER) or ""
        return hmac.compare_digest(provided, token)

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
        unauthenticated."""
        session_id = self._session_id()
        if not session_id:
            return False
        expiry = self.server.sessions.get(session_id)
        if expiry is None:
            return False
        if expiry <= time.time():
            del self.server.sessions[session_id]
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
        """GET auth: no token configured, the header, or a login session."""
        if not self.server.webui_token:
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
                self._json(401, {"ok": False, "reason": f"missing or wrong {TOKEN_HEADER}"})
                return
            if path == "/":
                snap = self.server.snapshot_func()
                self._send(200, render_status_html(snap, form_token=self.server.form_token),
                           "text/html; charset=utf-8")
            elif path == "/api/status":
                self._json(200, self.server.snapshot_func())
            elif path.startswith("/api/printers/"):
                self._printer_status_route(path[len("/api/printers/"):])
            elif path == "/config":
                self._send(200, self._config_html(self._field_values()),
                           "text/html; charset=utf-8")
            elif path == "/designs":
                rows = designs_module.catalog_rows(self.server.settings_data)
                self._send(200, designs_module.render_designs_page(rows),
                           "text/html; charset=utf-8")
            elif path.startswith("/designs/"):
                self._design_route(path[len("/designs/"):])
            elif path == "/layers" or path.startswith("/layers/"):
                archive = layers_module.archive_root(self.server.settings_data)
                rest = path[len("/layers/"):] if path.startswith("/layers/") else ""
                layer = (parse_qs(urlparse(self.path).query).get("layer") or ["0"])[0]
                code, content_type, body = layers_module.respond(archive, rest, layer)
                if content_type.startswith("text/html"):
                    self._send(code, body, content_type)
                else:
                    self._json(code, body)
            else:
                self._json(404, {"ok": False,
                                 "reason": "try /, /api/status, /api/printers/<id>/status, "
                                           "/healthz, /config, /designs, /layers or /login"})
        except Exception as exc:  # a broken snapshot is a 500, never a dropped connection
            self._json(500, {"ok": False, "reason": f"{type(exc).__name__}: {exc}"})

    # -- per-printer live status (#664) -------------------------------------
    def _printer_status_route(self, rest):
        """GET /api/printers/<id>/status: exactly one id segment plus the
        literal ``status`` tail; anything else under /api/printers/ is a
        404 naming the one route that exists there."""
        segments = rest.split("/")
        if len(segments) != 2 or not segments[0] or segments[1] != "status":
            self._json(404, {"ok": False,
                             "reason": "try /api/printers/<id>/status"})
            return
        code, payload = self.server.printer_status_func(unquote(segments[0]))
        self._json(code, payload)

    # -- designs catalog (read-only, #388 stage 3) --------------------------
    def _design_route(self, rest):
        """GET /designs/<id> (detail) and /designs/<id>/<kind> (a file).

        ``rest`` is the path after ``/designs/``: exactly one segment is
        a session id, exactly two is that session's ``scad`` / ``stl`` /
        ``preview`` file. Anything else — extra segments, ``..``, unknown
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
            session, scad_text, has_stl, has_preview = detail
            self._send(200, designs_module.render_design_detail(
                session, scad_text, has_stl=has_stl, has_preview=has_preview),
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

    # -- browser login ------------------------------------------------------
    def _login_page(self):
        if not self.server.webui_token:
            # No token configured: nothing to log in to (loopback posture).
            self._redirect("/")
            return
        self._send(200, pages.render_login_page(), "text/html; charset=utf-8")

    def _login_submit(self):
        form, error = self._read_form()
        if error:
            self._json(error[0], {"ok": False, "reason": error[1]})
            return
        if not self.server.webui_token:
            self._redirect("/")
            return
        provided = (form.get(pages.LOGIN_TOKEN_FIELD) or [""])[0]
        # The submitted token is compared, never logged or reflected.
        if not provided or not hmac.compare_digest(provided, self.server.webui_token):
            self._send(401, pages.render_login_page(
                error="That token was not accepted. Try again."),
                "text/html; charset=utf-8")
            return
        now = time.time()
        sessions = self.server.sessions
        for stale in [sid for sid, expiry in sessions.items() if expiry <= now]:
            del sessions[stale]
        session_id = secrets.token_urlsafe(32)
        sessions[session_id] = now + SESSION_TTL_S
        cookie = (f"{SESSION_COOKIE}={session_id}; HttpOnly; "
                  "SameSite=Strict; Path=/")
        self._redirect("/", headers=[("Set-Cookie", cookie)])

    def _logout(self):
        # Ends only the caller's own session, so no form token is needed:
        # the worst a cross-origin post can do is sign the operator out.
        session_id = self._session_id()
        if session_id:
            self.server.sessions.pop(session_id, None)
        cookie = (f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; "
                  "Path=/; Max-Age=0")
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
        if self.server.webui_token and self._header_authorized():
            return True
        provided = (form.get(pages.CSRF_FIELD) or [""])[0]
        return bool(provided) and hmac.compare_digest(provided, self.server.form_token)

    def _config_form(self):
        """(form, field_values) for an authorized config POST, else None
        (the refusal response has already been sent)."""
        form, error = self._read_form()
        if error:
            self._json(error[0], {"ok": False, "reason": error[1]})
            return None
        if not self._post_authorized(form):
            self._json(403, {"ok": False,
                             "reason": f"missing or wrong form token: POST /config "
                                       f"requires the {TOKEN_HEADER} header or the "
                                       f"form's own {pages.CSRF_FIELD} token"})
            return None
        return form, self._field_values()

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if self._host_refused(post=True):
                return
            if path == "/login":
                self._login_submit()
            elif path == "/logout":
                self._logout()
            elif path == "/config/preview":
                self._config_preview()
            elif path == "/config":
                self._config_save()
            elif path == "/debug":
                self._debug_post()
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
                                              submitted=submitted),
                       "text/html; charset=utf-8")
            return
        if not changes:
            self._send(200, self._config_html(
                field_values, submitted=submitted,
                notice="No changes: every submitted value already matches "
                       "the effective settings."), "text/html; charset=utf-8")
            return
        effective = {fv.field: fv.value for fv in field_values}
        entries = [(field, effective[field], value) for field, value in changes.items()]
        self._send(200, pages.render_preview_page(
            entries, submitted, form_token=self.server.form_token),
            "text/html; charset=utf-8")

    def _config_save(self):
        got = self._config_form()
        if got is None:
            return
        form, field_values = got
        if (form.get(pages.CONFIRM_FIELD) or [""])[0] != "yes":
            self._json(400, {"ok": False,
                             "reason": "saving needs the preview step: POST to "
                                       "/config/preview first, then confirm"})
            return
        # The confirm form names the fields the preview parsed; re-parse
        # exactly those, so hidden-input absence keeps its preview meaning.
        named = (form.get(pages.FIELDS_FIELD) or [""])[0]
        only = {part for part in named.split(",") if part} or None
        changes, submitted, errors = pages.parse_form(form, field_values, only=only)
        if errors:
            self._send(400, self._config_html(field_values, errors=errors,
                                              submitted=submitted),
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
                                              submitted=submitted),
                       "text/html; charset=utf-8")
            return
        self._send(200, pages.render_result_page(changed), "text/html; charset=utf-8")

    def _debug_post(self):
        """POST /debug (#478): turn debug log mode on or off.

        Same authorization as a config POST (access-token header or the
        page's form token). The mode is shared state in the state folder,
        so this one write reaches the bot and every listener process;
        nothing about printing changes.
        """
        form, error = self._read_form()
        if error:
            self._json(error[0], {"ok": False, "reason": error[1]})
            return
        if not self._post_authorized(form):
            self._json(403, {"ok": False,
                             "reason": f"missing or wrong form token: POST /debug "
                                       f"requires the {TOKEN_HEADER} header or the "
                                       f"form's own {pages.CSRF_FIELD} token"})
            return
        from .. import debug_mode
        action = (form.get("action") or [""])[0]
        data = self.server.settings_data
        if action == "on":
            raw = (form.get("minutes") or [""])[0].strip()
            try:
                minutes = int(raw) if raw else None
            except ValueError:
                self._json(400, {"ok": False, "reason": "minutes must be a whole number"})
                return
            debug_mode.start(data, minutes=minutes)
        elif action == "off":
            debug_mode.end(data)
        else:
            self._json(400, {"ok": False, "reason": "action must be on or off"})
            return
        self._redirect("/")

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
                printer_status_func=None):
    """A bound (not yet serving) ThreadingHTTPServer for the Web UI.

    ``host``/``port`` override the settings (tests bind port 0 for an
    ephemeral loopback port; the access-control rules then come from the
    *effective* bind host). Raises WebUIServerError when the start is
    refused, before any socket is bound.
    """
    environ = os.environ if environ is None else environ
    data = settings.load(environ=environ) if data is None else data
    cfg_host, cfg_port, token = check_start(data, environ)
    bind_host = cfg_host if host is None else host
    bind_port = cfg_port if port is None else int(port)
    if data["webui"].get("require_auth", True) and not is_loopback(bind_host) and not token:
        raise WebUIServerError(
            f"webui: refusing to bind {bind_host}: a non-loopback bind requires an "
            f"access token in ${data['webui']['access_token_env']}")
    server = ThreadingHTTPServer((bind_host, bind_port), _Handler)
    server.settings_data = data
    server.webui_token = token
    # DNS-rebinding guard (see _Handler._host_refused): only a tokenless
    # loopback bind relies on the form token alone, so only it checks Host.
    server.allowed_hosts = ({"localhost", "127.0.0.1", "::1", str(bind_host).lower()}
                            if is_loopback(bind_host) and not token else None)
    # The config page reads and writes through the same environment the
    # settings were loaded with (PRINT_HANDY_CONFIG resolution included).
    server.webui_environ = environ
    # Per-server form token for config POSTs (pages.CSRF_FIELD): generated
    # here, kept only in memory, never the access token.
    server.form_token = secrets.token_urlsafe(32)
    # Browser login sessions: {session id: expiry epoch}. Memory only —
    # a restart signs everyone out; nothing about a session is persisted.
    server.sessions = {}
    server.snapshot_func = snapshot_func or (
        lambda: status_module.snapshot(data, environ=environ))
    server.printer_status_func = printer_status_func or (
        lambda printer_id: status_module.printer_status(data, printer_id))
    return server


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
