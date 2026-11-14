"""The Discord Activity core for live camera viewing (issue #311, stage 3).

The Stage 0 audit settled the shape: Discord's official bot API cannot
send video, the custom-sender lineage is selfbot-only, and catduck chose
the Embedded App (Activity) fallback — a web page in a sandboxed iframe
inside the voice channel, served from the *same* Discord application as
the bot (no second bot). This module is the offline-buildable core of
that page's server side. It is stdlib-only and performs no I/O of its
own except through injected transports, so every behaviour is testable
offline:

- :func:`authenticate_code` — the OAuth allowlist check. The page gets
  a one-time code from the Discord client; the *server* exchanges it
  for an access token and reads ``GET /users/@me`` itself (Discord's
  networking guide is explicit: do not trust identity data coming from
  the client), then allows only the configured Discord user IDs. Deny
  is the default on every failure. Codes and access tokens are secrets
  in transit: they are never stored on the result and never appear in
  errors or logs.
- Session markers — a short-lived HMAC-signed token
  (:func:`make_session_token` / :func:`verify_session_token`) the
  server sets as a cookie after a successful exchange. The signing
  secret is never a setting: it lives in the environment variable
  :data:`SESSION_SECRET_ENV` names, the same indirection as every
  other Print Handy secret; the Discord client secret likewise lives
  in :data:`CLIENT_SECRET_ENV`.
- :data:`BOOTSTRAP_PAGE` — the one unauthenticated page (the fix for
  the 2026-10-30 status check's blocker 1: the Activity could never
  sign anyone in, because the only page sat behind the session it was
  supposed to create). Served by ``GET /`` when no valid session
  cookie arrives, it loads the vendored Embedded App SDK from this
  same origin (:data:`SDK_ROUTE`; the SDK ships in the package because
  the CSP allows no external hosts), runs the SDK ``authorize()`` for
  a one-time code, posts the code to ``POST /api/token``, and reloads
  ``/`` — the session cookie the exchange sets then brings back
  :data:`ACTIVITY_PAGE`. The bootstrap carries no stream name and no
  feed: a leaked bootstrap URL teaches an attacker nothing, and only
  the server-side allowlist exchange can mint a session.
- :data:`ACTIVITY_PAGE` — the single self-contained page. It speaks
  go2rtc's MSE-over-WebSocket protocol on ``/api/ws`` (on MediaSource
  open it sends ``{"type": "mse", "value": [<codecs>]}``, the server
  answers with the mime type, then binary fMP4 frames are appended to
  a SourceBuffer), building the WebSocket URL from a same-origin
  relative path plus a stream-name parameter, and shows a plain
  fallback message when the feed cannot start. Everything the page
  loads is itself — strict CSP, no external hosts. How the page
  becomes reachable from Discord clients (Developer Portal URL
  mappings, a tunnel, the ``discordsays.com`` proxy) is deployment
  wiring for stage 4, not this module's concern; the audit found
  WebRTC is not supported through that proxy, which is why MSE is
  the in-Activity transport.
- :func:`make_server` — a stdlib ``http.server`` factory.
  ``GET /health`` answers openly (it reveals nothing: no stream name,
  no configuration). ``POST /api/token`` runs the code exchange and
  sets the session cookie. ``GET /`` serves the page only with a
  valid session cookie, and the sign-in bootstrap (above) without
  one. The default bind is
  loopback; this module never publishes anything to the internet.
- The feed itself — ``GET /api/ws`` (stage 4). go2rtc stays
  loopback-only and is never exposed raw: this server is the one
  narrow, authenticated proxy the Stage 0 audit specified. A valid
  session cookie is required (a leaked Activity URL without one
  serves nothing), only the one published stream name is proxied,
  and the WebSocket upgrade is relayed byte-for-byte to go2rtc on
  loopback after the handshake. The feed is also gated on the shared
  live-session record: a connection is relayed only while a viewing
  session is live, and an open relay is closed when that session
  ends, so /print-live stop, empty-timeout expiry and print end all
  end the frames, not just the bookkeeping; and the browser's Origin
  is rewritten to the loopback upstream's own origin on the hop to
  go2rtc, whose origin check would otherwise refuse the public
  tunnel origin (its api.origin is unset). That keeps exactly one
  public origin (this server, behind the tunnel) and one Developer
  Portal URL mapping, on every deployment shape.

Stage 4 — the supervised server. ``python3 -m print_handy.live_activity``
is the production entrypoint docker/supervise.sh starts when
:mod:`print_handy.live_gate` says the feature is enabled and
configured: it binds the configured loopback port (the page reaches
Discord clients through the named Cloudflare Tunnel and the Developer
Portal URL mapping, never a bare public bind), builds the production
OAuth transport from the environment-held client secret, and takes
the allowlist from the same Discord user IDs the bot already trusts
(``[discord] approval_user_id`` plus ``allowed_user_ids``). Two
callbacks keep the shared live-session record
(:mod:`print_handy.live_stream`) honest: an authenticated page load
notes a viewer, and the page's ``pagehide`` beacon to
``POST /api/leave`` notes the leave, so the leave-when-empty timeout
has real presence to measure. A background tick in the entrypoint —
not in the handler — ends a session that has stayed empty for the
configured timeout. The page's stream name is
:data:`print_handy.live_stream.GO2RTC_STREAM_NAME`, the name
``print_handy.go2rtc`` renders, so the page and the container's one
camera fan-out cannot drift apart.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import select
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping, Protocol

from .live_stream import GO2RTC_STREAM_NAME, feed_stream_name

log = logging.getLogger(__name__)

__all__ = [
    "ACTIVITY_PAGE",
    "BOOTSTRAP_PAGE",
    "CLIENT_SECRET_ENV",
    "DEFAULT_STREAM_NAME",
    "FEED_UPSTREAM",
    "SESSION_COOKIE",
    "SESSION_SECRET_ENV",
    "SESSION_TTL_S",
    "ActivityAuthError",
    "AuthResult",
    "OAuthTransport",
    "SDK_ROUTE",
    "SDK_VERSION",
    "UrllibOAuthTransport",
    "authenticate_code",
    "bootstrap_page_for",
    "client_secret",
    "make_server",
    "make_session_token",
    "sdk_bundle",
    "session_secret",
    "verify_session_token",
]

TOKEN_URL = "https://discord.com/api/oauth2/token"
USER_URL = "https://discord.com/api/users/@me"
CLIENT_SECRET_ENV = "DISCORD_CLIENT_SECRET"
SESSION_SECRET_ENV = "LIVE_STREAM_SESSION_SECRET"
SESSION_COOKIE = "pb_live_session"
SESSION_TTL_S = 3600
#: Where go2rtc's API (and its /api/ws feed endpoint) listens: loopback
#: only, inside the same deployment (print_handy.go2rtc's generated
#: config). The Activity server proxies the feed from here; the address
#: is never served to the page or published anywhere.
FEED_UPSTREAM = ("127.0.0.1", 1984)
# The stream the page requests is the one go2rtc stream the container
# publishes (#311 stage 4): print_handy.go2rtc renders its config under
# live_stream.GO2RTC_STREAM_NAME, and this alias keeps the page, its
# tests and the go2rtc config reading from that one definition.
DEFAULT_STREAM_NAME = GO2RTC_STREAM_NAME
_MAX_TOKEN_BODY = 4096

_PAGE_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
             "connect-src 'self' ws: wss:; media-src 'self' blob:; img-src 'self' data:; "
             "base-uri 'none'; form-action 'none'")

#: The pinned Discord Embedded App SDK this server vendors and serves
#: itself (docs/COMPONENTS.md). The Activity pages' CSP allows no
#: external hosts, so the SDK can never be fetched from a CDN at
#: runtime: the single-file bundle ships in the package beside this
#: module and is served from SDK_ROUTE on this same origin. A bump
#: replaces the bundle file whole at a new pin; nothing patches it.
SDK_VERSION = "2.5.0"
SDK_BUNDLE_FILE = "discord_embedded_app_sdk.js"
SDK_ROUTE = "/sdk.js"
# The bootstrap page's CSP: like the feed page's, plus 'self' as a
# script source so the vendored SDK module may load from SDK_ROUTE.
# Nothing external is allowed here either.
_BOOTSTRAP_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; "
                  "style-src 'unsafe-inline'; connect-src 'self'; "
                  "img-src 'self' data:; base-uri 'none'; form-action 'none'")

_sdk_bundle_cache = None


def sdk_bundle() -> bytes:
    """The vendored Embedded App SDK bundle, read once and cached.

    Raises OSError when the bundle is missing (a broken package); the
    handler turns that into a plain 500, because sign-in — never the
    feed an existing session is watching — is what the bundle serves.
    """
    global _sdk_bundle_cache
    if _sdk_bundle_cache is None:
        _sdk_bundle_cache = Path(__file__).with_name(SDK_BUNDLE_FILE).read_bytes()
    return _sdk_bundle_cache


def bootstrap_page_for(client_id) -> str:
    """BOOTSTRAP_PAGE with the deployment's Discord client ID filled in.

    The client ID is a plain identifier (it is in every invite URL),
    never a secret; it is substituted as a JSON string literal, the
    same replace-don't-format pattern the feed page's stream name
    uses, so an unexpected value cannot break out of the script.
    """
    return BOOTSTRAP_PAGE.replace("__CLIENT_ID_JSON__", json.dumps(str(client_id or "")))


class ActivityAuthError(RuntimeError):
    """The Activity sign-in was refused. Messages never carry the code or any token."""


@dataclass(frozen=True)
class AuthResult:
    """An allowlisted Discord identity. Deliberately holds no token."""

    user_id: str
    username: str = ""


class OAuthTransport(Protocol):
    """The two Discord calls the auth check needs, injected for tests."""

    def exchange_code(self, code: str) -> str:
        """Trade the one-time OAuth code for an access token, or raise."""
        ...

    def fetch_user(self, access_token: str) -> Mapping:
        """GET /users/@me with the token, as a mapping, or raise."""
        ...


class UrllibOAuthTransport:
    """The production transport: stdlib urllib against discord.com.

    The client secret comes from the environment (see
    :func:`client_secret`), never from a settings file. Response bodies
    are never included in errors — a Discord error body is not needed to
    refuse a sign-in, and the discipline keeps codes and tokens out of
    logs by construction.
    """

    def __init__(self, client_id, client_secret_value, *, opener=None, timeout=10):
        self.client_id = str(client_id)
        self._client_secret = str(client_secret_value)
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def exchange_code(self, code):
        body = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self._client_secret,
            "grant_type": "authorization_code",
            "code": code,
        }).encode()
        request = urllib.request.Request(
            TOKEN_URL, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except Exception:
            raise ActivityAuthError("the Discord sign-in exchange failed") from None
        token = str((payload or {}).get("access_token") or "")
        if not token:
            raise ActivityAuthError("the Discord sign-in exchange failed")
        return token

    def fetch_user(self, access_token):
        request = urllib.request.Request(
            USER_URL, headers={"Authorization": f"Bearer {access_token}"})
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except Exception:
            raise ActivityAuthError("the Discord profile could not be read") from None
        if not isinstance(payload, Mapping):
            raise ActivityAuthError("the Discord profile could not be read")
        return payload


def client_secret(environ=None, env_name=CLIENT_SECRET_ENV):
    """The Discord application's client secret *value* from the environment ("" if unset)."""
    environ = os.environ if environ is None else environ
    return environ.get(env_name) or ""


def authenticate_code(code, *, transport, allowlist):
    """Allow only an allowlisted Discord user, checked server-side.

    `transport` is an :class:`OAuthTransport`; `allowlist` is an
    iterable of Discord user IDs (the ``[discord] approval_user_id``
    and ``allowed_user_ids`` the deployment chooses). Every failure —
    no code, an empty allowlist, a refused exchange, an unreadable
    profile, an ID that is not listed — raises
    :class:`ActivityAuthError` with a message that contains neither
    the code nor the access token.
    """
    code = str(code or "").strip()
    allowed = {str(user).strip() for user in (allowlist or ()) if str(user).strip()}
    if not code:
        raise ActivityAuthError("no sign-in code was provided")
    if not allowed:
        raise ActivityAuthError("no Discord user is on the live view allowlist")
    try:
        token = transport.exchange_code(code)
    except ActivityAuthError:
        raise
    except Exception:
        raise ActivityAuthError("the Discord sign-in exchange failed") from None
    if not token:
        raise ActivityAuthError("the Discord sign-in exchange failed")
    try:
        user = transport.fetch_user(token)
    except ActivityAuthError:
        raise
    except Exception:
        raise ActivityAuthError("the Discord profile could not be read") from None
    user_id = str((user or {}).get("id") or "").strip()
    if not user_id:
        raise ActivityAuthError("the Discord profile could not be read")
    if user_id not in allowed:
        raise ActivityAuthError("this Discord account is not on the live view allowlist")
    return AuthResult(user_id=user_id, username=str(user.get("username") or ""))


# --------------------------------------------------------------------- session markers

def session_secret(environ=None, env_name=SESSION_SECRET_ENV):
    """The session signing secret as bytes, from the environment (b"" if unset)."""
    environ = os.environ if environ is None else environ
    return (environ.get(env_name) or "").encode()


def _sign(payload, secret):
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def make_session_token(user_id, *, secret, now, ttl_s=SESSION_TTL_S):
    """A signed ``<user_id>.<expiry>.<hmac>`` marker for one allowlisted user."""
    user_id = str(user_id or "").strip()
    if not user_id or "." in user_id:
        raise ActivityAuthError("a session marker needs a plain Discord user ID")
    if not secret:
        raise ActivityAuthError("the live session secret is not configured")
    expiry = int(now) + int(ttl_s)
    payload = f"{user_id}.{expiry}"
    return f"{payload}.{_sign(payload, secret)}"


def verify_session_token(token, *, secret, now):
    """The user ID a session marker proves, or None for anything invalid or expired."""
    if not token or not secret:
        return None
    parts = str(token).split(".")
    if len(parts) != 3:
        return None
    user_id, expiry, signature = parts
    if not user_id or not hmac.compare_digest(signature, _sign(f"{user_id}.{expiry}", secret)):
        return None
    try:
        expiry_s = int(expiry)
    except ValueError:
        return None
    if float(now) >= expiry_s:
        return None
    return user_id


# --------------------------------------------------------------------- the page

ACTIVITY_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Print Handy live camera</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 1rem; background: #1e1f22; color: #f2f3f5; }
  video { width: 100%; max-height: 80vh; background: #000; }
  #status { padding: 0.75rem; border: 1px solid #4e5058; border-radius: 6px; }
</style>
</head>
<body>
<h1>Printer camera</h1>
<video id="feed" autoplay muted playsinline></video>
<p id="status" hidden></p>
<script>
(function () {
  "use strict";
  var status = document.getElementById("status");
  var video = document.getElementById("feed");
  function unavailable(message) {
    status.textContent = message;
    status.hidden = false;
  }
  // Leaving the Activity notes the leave server-side (the session's
  // viewer count is what the empty-timeout enforcement measures).
  window.addEventListener("pagehide", function () {
    if (navigator.sendBeacon) { navigator.sendBeacon("/api/leave"); }
  });
  var params = new URLSearchParams(location.search);
  var stream = (params.get("src") || "camera").replace(/[^A-Za-z0-9_-]/g, "");
  if (!stream) {
    unavailable("No camera stream was named for this page.");
    return;
  }
  if (!("MediaSource" in window) || !("WebSocket" in window)) {
    unavailable("This Discord client cannot play the live feed (MediaSource is unavailable).");
    return;
  }
  // go2rtc MSE over WebSocket, on the same origin this page was served
  // from: ws(s)://<host>/api/ws?src=<stream>. Once both the socket and
  // the MediaSource are open the client offers its codecs as
  // {"type": "mse", "value": [<codecs>]}; go2rtc answers with the mime
  // type to build the SourceBuffer with, then sends binary fMP4
  // frames. Any failure lands on the plain fallback message below;
  // the watchdog snapshots in chat keep working.
  var scheme = location.protocol === "https:" ? "wss:" : "ws:";
  var url = scheme + "//" + location.host + "/api/ws?src=" + encodeURIComponent(stream);
  var codecs = ['video/mp4; codecs="avc1.64001f,mp4a.40.2"',
                'video/mp4; codecs="avc1.42e01f,mp4a.40.2"'];
  var ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";
  var mediaSource = new MediaSource();
  video.src = URL.createObjectURL(mediaSource);
  var buffer = null;
  var queue = [];
  function append(chunk) {
    if (!buffer) { return; }
    if (buffer.updating || queue.length) { queue.push(chunk); }
    else { buffer.appendBuffer(chunk); }
  }
  // The handshake goes out only once BOTH the WebSocket and the
  // MediaSource are open (P2-122): sourceopen can fire while the
  // socket is still CONNECTING, and ws.send in that state throws
  // InvalidStateError — with the send attempted only there, the feed
  // would never start even after the socket connected.
  var wsOpen = false;
  var msOpen = false;
  var handshakeSent = false;
  function sendHandshake() {
    if (handshakeSent || !wsOpen || !msOpen) { return; }
    handshakeSent = true;
    ws.send(JSON.stringify({type: "mse", value: codecs}));
  }
  ws.addEventListener("open", function () {
    wsOpen = true;
    sendHandshake();
  });
  mediaSource.addEventListener("sourceopen", function () {
    msOpen = true;
    sendHandshake();
  });
  ws.onmessage = function (event) {
    if (typeof event.data === "string") {
      var message;
      try { message = JSON.parse(event.data); } catch (e) { return; }
      if (message.type === "mse" && !buffer) {
        try {
          buffer = mediaSource.addSourceBuffer(message.value);
          buffer.addEventListener("updateend", function () {
            if (queue.length && !buffer.updating) { buffer.appendBuffer(queue.shift()); }
          });
        } catch (e) {
          unavailable("The live feed's format is not playable in this Discord client.");
          ws.close();
        }
      } else if (message.type === "error") {
        unavailable("The camera feed is unavailable right now. Nothing is wrong with the print; try again in a moment.");
      }
      return;
    }
    append(event.data);
  };
  ws.onerror = function () {
    if (!buffer) {
      unavailable("The camera feed could not be reached. Nothing is wrong with the print; try again in a moment.");
    }
  };
  ws.onclose = function () {
    if (!buffer) {
      unavailable("The camera feed is unavailable right now. Nothing is wrong with the print; try again in a moment.");
    }
  };
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------- the sign-in bootstrap page

# The one page served without a session (see the module docstring).
# It deliberately contains no feed and no stream name, and the only
# value the server fills in is the Discord client ID — a plain
# identifier. The flow is the SDK's own: ready(), authorize() for a
# one-time code with the identify scope (all the server's /users/@me
# check needs), the code posted to /api/token, then a reload of / so
# the session cookie set by the exchange serves the feed page. The
# access token never comes back to this page: the server exchanges
# the code itself and keeps the token server-side, exactly like the
# rest of this module's auth design.
BOOTSTRAP_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Print Handy live view</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 1rem; background: #1e1f22; color: #f2f3f5; }
  #status { padding: 0.75rem; border: 1px solid #4e5058; border-radius: 6px; }
</style>
</head>
<body>
<h1>Printer live view</h1>
<p id="status">Signing you in with Discord…</p>
<script type="module">
import { DiscordSDK } from "/sdk.js";

var status = document.getElementById("status");
function fail(message) { status.textContent = message; }
var CLIENT_ID = __CLIENT_ID_JSON__;

async function main() {
  if (!CLIENT_ID) {
    fail("This live view is not fully configured: the server has no Discord " +
         "client ID. The operator needs to set DISCORD_CLIENT_ID and restart.");
    return;
  }
  var sdk;
  try {
    sdk = new DiscordSDK(CLIENT_ID);
    await sdk.ready();
  } catch (e) {
    fail("Open this page from the Print Handy activity inside the Discord " +
         "voice channel — sign-in only works there.");
    return;
  }
  var code;
  try {
    var auth = await sdk.commands.authorize({
      client_id: CLIENT_ID,
      response_type: "code",
      state: "ph-live-signin",
      prompt: "none",
      scope: ["identify"]
    });
    code = auth.code;
  } catch (e) {
    fail("Discord did not hand over a sign-in code, so the live view cannot " +
         "start. Close the activity and launch it again.");
    return;
  }
  // The code is single-use and goes only to this server's own exchange
  // endpoint — never into this page's text, a log, or anywhere else.
  var res;
  try {
    res = await fetch("/api/token", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({code: code})
    });
  } catch (e) {
    fail("The sign-in answer could not reach the Print Handy server. " +
         "Check the tunnel is up, then launch the activity again.");
    return;
  }
  if (!res.ok) {
    fail("Print Handy refused this sign-in. Only accounts on the bot's " +
         "allowlist can watch the live view.");
    return;
  }
  window.location.replace("/" + window.location.search);
}

main();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------- the HTTP surface

#: How often an idle relay direction re-checks its end conditions, and
#: how long BOTH directions may stay silent before the relay is ended
#: as idle (P2-142). Module-level so tests can shrink them.
_PUMP_POLL_S = 1
_PUMP_IDLE_CUTOFF_S = 60
_PUMP_CHECK_S = 1  # should_close re-reads the session file: at most once a second while chunks flow


def _pump(client, upstream, should_close=None):
    """Relay bytes between the client and go2rtc until either side closes.

    One direction runs on a helper thread, the other on the caller's;
    whichever finishes first shuts both sockets down so the other
    direction cannot linger. Frame contents are never inspected —
    this is a byte pipe behind the session check, exactly as narrow as
    the audit specified.

    When `should_close` is given it is consulted on every pass (and at
    least once a second while idle): as soon as it reports True both
    sockets are shut down, so a relay cannot outlive the condition
    that allowed it — for the feed proxy, the live viewing session
    (P1-89). A `should_close` that raises counts as closed: the pump
    fails shut.

    The idle cutoff measures inactivity across BOTH directions
    (P2-142): during normal MSE playback the page sends its offer once
    and then only receives, so one direction staying silent is the
    healthy state, not a dead relay — the old per-direction cutoff cut
    exactly those streams after 60 seconds and go2rtc sends no pings
    to prevent it. The relay now ends when either side actually
    closes, when `should_close` fires, or when both directions have
    been quiet for the whole cutoff.
    """

    checked = {"at": None}

    def ended(force=False):
        if should_close is None:
            return False
        now = time.monotonic()
        if not force and checked["at"] is not None and now - checked["at"] < _PUMP_CHECK_S:
            return False  # a busy relay re-reads the session at most once a second (2.2.5 review)
        checked["at"] = now
        try:
            return bool(should_close())
        except Exception:
            return True

    activity = {"last": time.monotonic()}

    def forward(source, target):
        try:
            while not ended():
                readable, _, _ = select.select([source], [], [], _PUMP_POLL_S)
                if not readable:
                    if ended(force=True) or time.monotonic() - activity["last"] >= _PUMP_IDLE_CUTOFF_S:
                        break
                    continue
                chunk = source.recv(65536)
                if not chunk:
                    break
                activity["last"] = time.monotonic()
                target.sendall(chunk)
        except OSError:
            pass
        finally:
            for sock in (client, upstream):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    thread = threading.Thread(target=forward, args=(upstream, client), daemon=True)
    thread.start()
    forward(client, upstream)
    thread.join(timeout=5)


def make_handler(*, transport, allowlist, secret, page=ACTIVITY_PAGE, clock=time.time,
                 on_viewer=None, on_leave=None, feed_upstream=FEED_UPSTREAM,
                 stream_name=DEFAULT_STREAM_NAME, session_active=None,
                 client_id="", bootstrap_page=BOOTSTRAP_PAGE):
    """A BaseHTTPRequestHandler class bound to the given auth pieces.

    Routes: ``GET /health`` (open; reveals nothing), ``GET /sdk.js``
    (the vendored Embedded App SDK bundle; open — it is public library
    code and carries no configuration), ``POST /api/token``
    (the OAuth exchange; sets the session cookie on success, 403 on any
    refusal), ``GET /`` (the feed page, with a valid session cookie
    only; without one, the sign-in bootstrap page, which can do
    nothing but run the authorize -> /api/token flow), ``POST
    /api/leave`` (the page's leave beacon; a
    valid session cookie is required), ``GET /api/ws`` (the feed: a
    valid session cookie and the one published stream name required,
    then the WebSocket upgrade is proxied to go2rtc on loopback).
    Everything else is 404, and other methods are 405. Request logging
    is silenced: request lines can carry codes and cookies, and neither
    belongs in a log.

    ``client_id`` is the Discord application ID the bootstrap page
    hands the SDK's authorize call; the supervised entrypoint passes
    the deployment's DISCORD_CLIENT_ID (live_gate). It is a plain
    identifier, not a secret. Without it the bootstrap still serves
    and its own flow reports the misconfiguration.

    ``on_viewer(user_id)`` / ``on_leave(user_id)`` are the stage 4
    presence callbacks: a successful exchange and every authenticated
    page serve note a viewer; the leave beacon notes the leave. A
    callback that raises never breaks serving — presence is
    bookkeeping, the feed is the feature.

    ``stream_name`` is the one go2rtc stream this server proxies and
    the page asks for by default: the raw camera stream, or the
    transcoded live stream when a [live_stream] transcode value is set
    (print_handy.live_stream.feed_stream_name, wired in 2.2.2). The
    served page's default stream is rewritten to it, so page and proxy
    can never disagree about the name.

    ``session_active`` is the live viewing session's state as a
    zero-argument callable (P1-89): when given, the feed proxy refuses
    a new connection while it reports False and closes an open relay
    as soon as it flips to False, so /print-live stop, empty-timeout
    expiry and print-end shutdown end the frames, not just the JSON
    bookkeeping. A check that raises reads as not live — the gate
    fails closed. When omitted, no session gate applies (the offline
    core's tests exercise the relay without a session record; the
    supervised entrypoint always wires it).
    """
    served_page = page.replace('|| "camera"', f'|| "{stream_name}"')
    served_bootstrap = bootstrap_page.replace("__CLIENT_ID_JSON__",
                                              json.dumps(str(client_id or "")))

    def _session_live():
        if session_active is None:
            return True
        try:
            return bool(session_active())
        except Exception:
            return False

    def _notify(callback, user_id):
        if callback is None or not user_id:
            return
        try:
            callback(str(user_id))
        except Exception:
            pass

    class ActivityHandler(BaseHTTPRequestHandler):
        server_version = "PrintHandyLive/1"
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            pass

        # ---------------------------------------------------------- plumbing

        def _send(self, status, body, content_type, extra=()):
            payload = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def _json(self, status, payload, extra=()):
            self._send(status, json.dumps(payload), "application/json", extra)

        def _session_user(self):
            header = self.headers.get("Cookie") or ""
            for part in header.split(";"):
                name, _, value = part.strip().partition("=")
                if name == SESSION_COOKIE and value:
                    return verify_session_token(value, secret=secret, now=clock())
            return None

        # ---------------------------------------------------------- the feed proxy

        def _proxy_feed(self):
            """Relay one feed WebSocket to go2rtc on loopback (stage 4).

            Auth first (a valid session cookie), then the stream name:
            only the one published stream is proxied, under its one
            name. Then the viewing-session lifecycle (P1-89): while the
            shared live session is not live, the feed is refused even
            with a valid cookie, and an open relay is pumped with a
            close check on that same state so the session ending ends
            the frames. The client's upgrade request is rebuilt for the
            upstream (its Host, no Cookie — go2rtc needs neither our
            session nor our credentials), the upstream's answer goes
            back verbatim, and on a 101 the two sockets are pumped in
            both directions until either side closes. Any failure
            before the handshake completes is a plain HTTP refusal;
            nothing about the upstream is revealed beyond "unavailable".
            """
            if self._session_user() is None:
                self._json(403, {"ok": False, "error": "not authorised"})
                return
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if (query.get("src") or [""])[0] != stream_name:
                self._json(404, {"ok": False, "error": "not found"})
                return
            if not _session_live():
                self._json(403, {"ok": False, "error": "the live view is not running"})
                return
            try:
                upstream = socket.create_connection(feed_upstream, timeout=10)
            except OSError:
                self._json(502, {"ok": False, "error": "the camera feed is unavailable"})
                return
            try:
                lines = [f"GET /api/ws?src={stream_name} HTTP/1.1",
                         f"Host: {feed_upstream[0]}:{feed_upstream[1]}",
                         "Upgrade: websocket", "Connection: Upgrade"]
                for name in ("Sec-WebSocket-Key", "Sec-WebSocket-Version",
                             "Sec-WebSocket-Protocol", "Sec-WebSocket-Extensions",
                             "User-Agent"):
                    value = self.headers.get(name)
                    if value:
                        lines.append(f"{name}: {value}")
                if self.headers.get("Origin"):
                    # P1-88: the browser's Origin is the public tunnel
                    # origin, but Host here is the loopback upstream —
                    # and go2rtc, whose generated config leaves
                    # api.origin unset, refuses an Origin whose host
                    # differs from Host, so forwarding the browser
                    # Origin verbatim earns an upstream 403. On this
                    # authenticated loopback hop the Origin is
                    # rewritten to the upstream's own origin, which
                    # always matches the Host go2rtc sees.
                    lines.append(
                        f"Origin: http://{feed_upstream[0]}:{feed_upstream[1]}")
                upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
                head = b""
                while b"\r\n\r\n" not in head and len(head) <= 65536:
                    chunk = upstream.recv(4096)
                    if not chunk:
                        break
                    head += chunk
                if not head:
                    self._json(502, {"ok": False, "error": "the camera feed is unavailable"})
                    return
                header_block, _, rest = head.partition(b"\r\n\r\n")
                self.connection.sendall(header_block + b"\r\n\r\n")
                status = int(header_block.split(b" ", 2)[1])
                if status != 101:
                    return
                if rest:
                    self.connection.sendall(rest)
                self.close_connection = True
                _pump(self.connection, upstream,
                      should_close=None if session_active is None
                      else (lambda: not _session_live()))
            except (OSError, ValueError, IndexError):
                return
            finally:
                try:
                    upstream.close()
                except OSError:
                    pass

        # ---------------------------------------------------------- routes

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/ws":
                self._proxy_feed()
                return
            if path == "/health":
                self._json(200, {"ok": True, "service": "print-handy-live"})
                return
            if path == SDK_ROUTE:
                try:
                    bundle = sdk_bundle()
                except OSError:
                    self._json(500, {"ok": False,
                                     "error": "the sign-in bundle is unavailable"})
                    return
                self._send(200, bundle, "text/javascript; charset=utf-8")
                return
            if path == "/":
                user_id = self._session_user()
                if user_id is None:
                    self._send(200, served_bootstrap, "text/html; charset=utf-8",
                               extra=(("Content-Security-Policy", _BOOTSTRAP_CSP),))
                    return
                _notify(on_viewer, user_id)
                self._send(200, served_page, "text/html; charset=utf-8",
                           extra=(("Content-Security-Policy", _PAGE_CSP),))
                return
            self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/leave":
                user_id = self._session_user()
                if user_id is None:
                    self._json(403, {"ok": False, "error": "not authorised"})
                    return
                _notify(on_leave, user_id)
                self._json(200, {"ok": True})
                return
            if path != "/api/token":
                self._json(404, {"ok": False, "error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if 0 < length <= _MAX_TOKEN_BODY else b""
            code = ""
            if raw:
                try:
                    code = str(json.loads(raw).get("code") or "")
                except (ValueError, AttributeError):
                    form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
                    code = (form.get("code") or [""])[0]
            try:
                result = authenticate_code(code, transport=transport, allowlist=allowlist)
                marker = make_session_token(result.user_id, secret=secret, now=clock())
            except ActivityAuthError as exc:
                self._json(403, {"ok": False, "error": str(exc)})
                return
            cookie = (f"{SESSION_COOKIE}={marker}; Path=/; HttpOnly; Secure; "
                      "SameSite=None; Partitioned")
            _notify(on_viewer, result.user_id)
            self._json(200, {"ok": True, "user_id": result.user_id},
                       extra=(("Set-Cookie", cookie),))

        def _method_not_allowed(self):
            self._json(405, {"ok": False, "error": "method not allowed"})

        do_PUT = _method_not_allowed
        do_DELETE = _method_not_allowed
        do_PATCH = _method_not_allowed

    return ActivityHandler


def make_server(host="127.0.0.1", port=0, **handler_kwargs):
    """A ThreadingHTTPServer for the Activity core; loopback unless told otherwise.

    The caller runs ``serve_forever`` (a later stage supervises it).
    Binding anything beyond loopback is deployment wiring (stage 4)
    and goes through the Discord proxy/tunnel design, never a bare
    public bind of this server.
    """
    return ThreadingHTTPServer((host, port), make_handler(**handler_kwargs))


# --------------------------------------------------------------------- stage 4: the supervised server

class SessionPresence:
    """Viewer presence over the shared live-session record (#311 stage 4).

    The Activity server is the one process that knows when an
    authenticated viewer is looking at the page, so it owns noting
    viewers on the shared record (:mod:`print_handy.live_stream`):
    a viewer is counted from their first authenticated page load until
    their leave beacon (or the session's end). Counts are of distinct
    Discord user IDs this server has seen for the live session.
    :meth:`tick` is the periodic enforcement the entrypoint runs: it
    re-reads the record (another process may have started or stopped
    the session), refreshes the viewer count, and ends a session that
    has stayed empty past ``empty_timeout_s``. Every method fails
    soft: presence bookkeeping must never break the feed, a command or
    an announcement, so a broken record reads as an idle session and a
    failed write is dropped.
    """

    def __init__(self, path, section):
        self._path = path
        self._section = section
        self._viewers = set()
        self._lock = threading.Lock()

    def _apply(self, now):
        from . import live_stream

        # The read-modify-write runs as one cross-process transaction
        # (P2-143): the bot's /print-live and the listener's hooks own
        # the same record, and an unlocked presence write could land
        # its stale live snapshot over their stop.
        def update(session):
            if not session.is_live:
                with self._lock:
                    self._viewers.clear()
                return False
            with self._lock:
                viewers = len(self._viewers)
            session.note_viewers(viewers, now=now)
            live_stream.expire_empty(session, self._section, now=now)
            return True

        return live_stream.transact_session_file(self._path, update)

    def is_live(self):
        """Whether the shared record currently says the session is live.

        A fresh read on every call — another process owns starting and
        stopping the session — and a missing or broken record reads as
        not live (print_handy.live_stream.read_session_file fails soft
        to an idle session), so the feed proxy's lifecycle gate (P1-89)
        fails closed, never open.
        """
        from . import live_stream

        return live_stream.read_session_file(self._path).is_live

    def note_viewer(self, user_id, *, now=None):
        with self._lock:
            self._viewers.add(str(user_id))
        self._apply(time.time() if now is None else now)

    def note_leave(self, user_id, *, now=None):
        with self._lock:
            self._viewers.discard(str(user_id))
        self._apply(time.time() if now is None else now)

    def tick(self, *, now=None):
        """Periodic empty-timeout enforcement; returns the session it saw."""
        return self._apply(time.time() if now is None else now)


def allowlist_from_settings(data):
    """The Activity allowlist: the Discord users the bot already trusts.

    ``[discord] approval_user_id`` (always allowed, like everywhere
    else in Print Handy) plus ``allowed_user_ids``, parsed with the
    settings loader's own parser so a malformed entry refuses the
    start loudly instead of silently allowing nobody — or everybody.
    """
    from . import settings

    discord = data.get("discord", {})
    allowed = settings.user_ids(discord.get("allowed_user_ids"))
    for approval in reversed(settings.approver_ids(discord)):
        if approval not in allowed:
            allowed.insert(0, approval)
    return tuple(allowed)


def main(argv=None) -> int:
    """The supervised Activity server entrypoint (docker/supervise.sh).

    Exit codes mirror the gate (print_handy.live_gate): 0 after a
    clean serve, 3 when the feature is not enabled and configured
    (nothing to serve), 2 when the configuration cannot be resolved.
    """
    from . import __version__, live_gate, logs, settings
    from .live_stream import session_file_path

    try:
        data = settings.load()
        config = live_gate.live_config(data)
        allowlist = allowlist_from_settings(data)
    except settings.SettingsError as exc:
        print(f"live activity: configuration unresolved: {exc}", file=sys.stderr)
        return 2
    if not config.ready:
        if not config.enabled:
            print("live activity: [live_stream] enabled is false; not serving")
        else:
            print("live activity: enabled but not configured; not serving: "
                  + "; ".join(config.missing))
        return 3
    if not allowlist:
        print("live activity: no Discord user is on the allowlist "
              "([discord] approval_user_id / allowed_user_ids); not serving",
              file=sys.stderr)
        return 2
    # The gates pass and this process will serve: join the shared logging
    # (and crash capture, role "live-activity") like every other Print
    # Handy process (#1404). The refusal paths above keep their plain
    # prints and exit codes, before any of this runs.
    logs.configure(data.get("logging") if isinstance(data, dict) else None,
                   role="live-activity")
    presence = SessionPresence(session_file_path(data), data["live_stream"])
    stop = threading.Event()

    def _tick():
        while not stop.wait(15):
            try:
                presence.tick()
            except Exception:
                pass  # enforcement is bookkeeping; the feed keeps serving

    stream_name = feed_stream_name(data["live_stream"])
    server = make_server(
        "127.0.0.1", config.port,
        transport=UrllibOAuthTransport(config.client_id, client_secret()),
        allowlist=allowlist, secret=session_secret(),
        on_viewer=presence.note_viewer, on_leave=presence.note_leave,
        session_active=presence.is_live,
        stream_name=stream_name,
        client_id=config.client_id)
    log.info(f"live activity: serving on 127.0.0.1:{config.port} "
             f"for {config.activity_origin} (stream {stream_name}) "
             f"(Print Handy {__version__})")
    ticker = threading.Thread(target=_tick, daemon=True)
    ticker.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
