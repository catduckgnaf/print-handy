"""Session state and browser login handlers for the Web UI (#1603 item 3).

Moved out of ``webui/server.py``: ``SessionStore`` (#1588) lives here
with the login, logout and session-cookie handlers next to it, exactly
as issue #1603 item 3 asks. ``webui/server.py`` re-exports the names
tests and embedders import from there, and ``_Handler`` keeps thin
delegates with the same names the route table (``webui/routes.py``)
dispatches to, so URLs, cookie headers, status codes and session
semantics are unchanged.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path

from .. import settings
from . import access as access_module
from . import pages
from . import upload as upload_module

log = logging.getLogger(__name__)

SESSION_COOKIE = "webui_session"
SESSION_TTL_S = 12 * 60 * 60  # 12 hours
REMEMBER_TTL_S = 30 * 24 * 60 * 60  # 30 days
SESSIONS_FILE = "webui_sessions.json"
_FINGERPRINT_KEY = "__credential_sha256__"
_REMEMBER_TRUTHY = frozenset({"on", "true", "1", "yes"})

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
    store = getattr(server, "session_store", None)
    if store is not None:
        return store.form_token_for(session_id)
    secret = getattr(server, "csrf_secret", None)
    if secret is None:
        secret = server.csrf_secret = secrets.token_bytes(32)
    return hmac.new(secret, session_id.encode("utf-8"), hashlib.sha256).hexdigest()

class SessionStore:
    """The Web UI's session state, in one place with its own lock (#1588).

    Holds the ephemeral login sessions (raw id -> expiry, memory only),
    the remembered sessions (SHA-256 of the id -> expiry; persistence to
    ``webui_sessions.json`` stays with ``_save_remembered_sessions``,
    which snapshots this store), the per-process form token and the
    secret session-bound form tokens are derived from. Before #1588
    these were bare attributes on the server object, created and mutated
    at a dozen sites with ``getattr`` fallbacks; now ``make_server``
    builds one store and the server's old attribute names delegate to
    it, so the state has one owner and one lock (the same shape as
    ``access.LoginThrottle``).
    """

    def __init__(self, remembered_sessions=None):
        self.lock = threading.RLock()
        self.sessions = {}
        self.remembered_sessions = dict(remembered_sessions or {})
        self.form_token = secrets.token_urlsafe(32)
        self.csrf_secret = secrets.token_bytes(32)
        self._remembered_pruned = False

    # -- tokens -----------------------------------------------------------
    def rotate_tokens(self):
        """A new form token and CSRF secret (an access-rule change)."""
        with self.lock:
            self.form_token = secrets.token_urlsafe(32)
            self.csrf_secret = secrets.token_bytes(32)
            return self.form_token

    def form_token_for(self, session_id):
        """The ``_csrf`` value bound to one session (see
        :func:`session_form_token`)."""
        with self.lock:
            secret = self.csrf_secret
            if secret is None:
                secret = self.csrf_secret = secrets.token_bytes(32)
        return hmac.new(secret, session_id.encode("utf-8"),
                        hashlib.sha256).hexdigest()

    # -- ephemeral sessions -------------------------------------------------
    def prune(self, *, now=None):
        """Drop expired ephemeral sessions; returns how many went."""
        now = time.time() if now is None else now
        with self.lock:
            stale = [sid for sid, expiry in list(self.sessions.items())
                     if expiry <= now]
            for sid in stale:
                self.sessions.pop(sid, None)
            return len(stale)

    def mint(self, session_id=None, *, ttl_s=SESSION_TTL_S, now=None):
        """Mint (or record) an ephemeral session; returns its id.

        Expired sessions are pruned first, so two logins at once never
        trip over a stale entry."""
        now = time.time() if now is None else now
        with self.lock:
            self.prune(now=now)
            session_id = session_id or secrets.token_urlsafe(32)
            self.sessions[session_id] = now + ttl_s
            return session_id

    def clear(self):
        """End every ephemeral session (the credential changed)."""
        with self.lock:
            self.sessions.clear()

    # -- remembered sessions --------------------------------------------------
    def remember(self, session_id, *, ttl_s=REMEMBER_TTL_S, now=None):
        """Record a remembered session by digest; returns the digest.

        In-memory only: the caller persists the snapshot and rolls this
        back with :meth:`forget_remembered` when the write fails."""
        now = time.time() if now is None else now
        digest = _session_digest(session_id)
        with self.lock:
            for stale in [d for d, expiry in list(self.remembered_sessions.items())
                          if expiry <= now]:
                del self.remembered_sessions[stale]
            self.remembered_sessions[digest] = now + ttl_s
            return digest

    def forget_remembered(self, session_id):
        """Drop one remembered session; True when one was present."""
        with self.lock:
            return self.remembered_sessions.pop(
                _session_digest(session_id), None) is not None

    def revoke_all_remembered(self):
        with self.lock:
            self.remembered_sessions.clear()

    def snapshot_remembered(self):
        with self.lock:
            return dict(self.remembered_sessions)

    # -- validation and revocation --------------------------------------------
    def validate(self, session_id, *, now=None):
        """``session_id`` when it names a live session, else "".

        Both stores count. An expired id is pruned on sight; when the
        pruned entry was a remembered one, :meth:`take_remembered_pruned`
        reports it once so the caller can persist the removal."""
        now = time.time() if now is None else now
        if not session_id:
            return ""
        with self.lock:
            expiry = self.sessions.get(session_id)
            if expiry is not None:
                if expiry <= now:
                    self.sessions.pop(session_id, None)
                    return ""
                return session_id
            digest = _session_digest(session_id)
            expiry = self.remembered_sessions.get(digest)
            if expiry is None:
                return ""
            if expiry <= now:
                del self.remembered_sessions[digest]
                self._remembered_pruned = True
                return ""
            return session_id

    def take_remembered_pruned(self):
        """True (once) when :meth:`validate` pruned a remembered entry."""
        with self.lock:
            pruned, self._remembered_pruned = self._remembered_pruned, False
            return pruned

    def revoke(self, session_id):
        """End one session in both stores; True when a remembered entry
        went (the caller then persists the removal)."""
        if not session_id:
            return False
        with self.lock:
            self.sessions.pop(session_id, None)
            return self.remembered_sessions.pop(
                _session_digest(session_id), None) is not None

def _session_store(server):
    """The server's :class:`SessionStore`, or None for a server object
    that predates it (the mutation sites stay safe either way)."""
    return getattr(server, "session_store", None)

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
            store = _session_store(server)
            payload = store.snapshot_remembered() if store is not None else \
                dict(getattr(server, "remembered_sessions", {}) or {})
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


# -- browser login handlers (moved from _Handler) ---------------------

def session_id(handler):
    """The session id from the Cookie header, or "" (garbage = none)."""
    raw = handler.headers.get("Cookie") or ""
    if not raw:
        return ""
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:  # a malformed Cookie header carries no session
        return ""
    morsel = jar.get(SESSION_COOKIE)
    return morsel.value if morsel is not None else ""

def session_authorized(handler):
    """A valid, unexpired login-session cookie. Expired ids are
    pruned on sight; unknown or expired cookies are simply
    unauthenticated. Both stores count: the ephemeral in-memory
    map (raw id) and the remembered map (SHA-256 of the id)."""
    return bool(handler._valid_session_id())

def valid_session_id(handler):
    """The caller's session id when it names a live session, else ""."""
    session_id = handler._session_id()
    if not session_id:
        return ""
    store = _session_store(handler.server)
    if store is not None:
        valid = store.validate(session_id)
        if store.take_remembered_pruned():
            # An expired remembered entry was pruned in memory;
            # persist the removal, as the bare-dict code did.
            with _remember_lock(handler.server):
                _save_remembered_sessions(handler.server)
        return valid
    now = time.time()
    expiry = handler.server.sessions.get(session_id)
    if expiry is not None:
        if expiry <= now:
            handler.server.sessions.pop(session_id, None)
            return ""
        return session_id
    remembered = getattr(handler.server, "remembered_sessions", None) or {}
    digest = _session_digest(session_id)
    expiry = remembered.get(digest)
    if expiry is None:
        return ""
    if expiry <= now:
        with _remember_lock(handler.server):
            if remembered.pop(digest, None) is not None:
                _save_remembered_sessions(handler.server)
        return ""
    return session_id

def bound_form_token(handler, session_id):
    """The form token bound to one session: an HMAC of its id under a
    per-process secret, so a token copied from one signed-in browser
    is worthless with another session or none."""
    return session_form_token(handler.server, session_id)

def current_form_token(handler):
    """The ``_csrf`` value to embed in a page served to this request.

    While access is open (unclaimed, or deliberately left open) it
    is the per-process form token, as before. On a claimed install a
    signed-in browser gets the token bound to its session; a header
    client (which posts with its header, not a form) gets the
    process token, which authorizes nothing there on its own."""
    if handler.server.access.required:
        session_id = handler._valid_session_id()
        if session_id:
            return handler._session_form_token(session_id)
    return handler.server.form_token

def login_page(handler):
    if not handler.server.access.required:
        # Access is open: nothing to log in to (loopback posture).
        handler._redirect("/")
        return
    handler._send(200, pages.render_login_page(password=handler._password_mode(),
                                            username=handler._username_mode(),
                                            theme=handler._theme()),
               "text/html; charset=utf-8")

def password_mode(handler):
    return handler.server.access.mode == access_module.PASSWORD

def username_mode(handler):
    return handler._password_mode() and bool(handler.server.access.username)

def cookie_secure(handler):
    secure = getattr(handler.server, "cookie_secure", None)
    if secure is not None:
        return bool(secure)
    data = getattr(handler.server, "settings_data", None) or {}
    return bool(data.get("webui", {}).get("cookie_secure", False))

def start_session(handler, remember=False):
    """A new login-session cookie header for this browser.

    With ``remember`` the session lasts REMEMBER_TTL_S, the cookie
    gains a matching Max-Age, and only the id's SHA-256 hash is
    persisted, so the session survives a server restart. When that
    write fails (a read-only state mount, say) the login still
    succeeds with a plain ephemeral session, as before."""
    store = _session_store(handler.server)
    if store is not None:
        session_id = secrets.token_urlsafe(32)
        flags = "; Secure" if handler._cookie_secure() else ""
        if remember:
            # Mutation through persistence under the store's lock,
            # so a concurrent logout or revocation is never
            # overwritten by this mint's snapshot (and this mint is
            # never lost to one).
            with _remember_lock(handler.server):
                store.remember(session_id)
                if _save_remembered_sessions(handler.server):
                    return ("Set-Cookie",
                            f"{SESSION_COOKIE}={session_id}; HttpOnly; "
                            f"SameSite=Strict; Path=/; Max-Age={REMEMBER_TTL_S}{flags}")
                # Fail open to the current behavior: ephemeral, no Max-Age.
                store.forget_remembered(session_id)
        store.mint(session_id)
        return ("Set-Cookie", f"{SESSION_COOKIE}={session_id}; HttpOnly; "
                f"SameSite=Strict; Path=/{flags}")
    now = time.time()
    sessions = handler.server.sessions
    # list() snapshots the map and pop() tolerates a concurrent prune:
    # two logins at once must not turn into a 500.
    for stale in [sid for sid, expiry in list(sessions.items()) if expiry <= now]:
        sessions.pop(stale, None)
    session_id = secrets.token_urlsafe(32)
    flags = "; Secure" if handler._cookie_secure() else ""
    if remember:
        remembered = getattr(handler.server, "remembered_sessions", None)
        if remembered is None:
            remembered = handler.server.remembered_sessions = {}
        # Mutation through persistence under the shared lock, so a
        # concurrent logout or revocation is never overwritten by
        # this mint's snapshot (and this mint is never lost to one).
        with _remember_lock(handler.server):
            for stale in [d for d, expiry in remembered.items() if expiry <= now]:
                del remembered[stale]
            remembered[_session_digest(session_id)] = now + REMEMBER_TTL_S
            if _save_remembered_sessions(handler.server):
                return ("Set-Cookie", f"{SESSION_COOKIE}={session_id}; HttpOnly; "
                        f"SameSite=Strict; Path=/; Max-Age={REMEMBER_TTL_S}{flags}")
            # Fail open to the current behavior: ephemeral, no Max-Age.
            del remembered[_session_digest(session_id)]
    sessions[session_id] = now + SESSION_TTL_S
    return ("Set-Cookie", f"{SESSION_COOKIE}={session_id}; HttpOnly; SameSite=Strict; Path=/{flags}")

def login_submit(handler):
    form, error = handler._read_form()
    if error:
        handler._json(error[0], {"ok": False, "reason": error[1]})
        return
    if not handler.server.access.required:
        handler._redirect("/")
        return
    provided = (form.get(pages.LOGIN_TOKEN_FIELD) or [""])[0]
    username = (form.get(pages.LOGIN_USER_FIELD) or [""])[0]
    # Failed-login backoff: a locked-out address is refused before any
    # hashing, so a flood costs the server nothing and guessing stalls.
    address = handler._client_ip()
    throttle = handler.server.login_throttle
    wait = throttle.retry_after(address)
    if wait:
        handler._send(429, pages.render_login_page(
            error=f"Too many failed attempts. Try again in {wait} seconds.",
            password=handler._password_mode(), username=handler._username_mode(),
            theme=handler._theme()),
            "text/html; charset=utf-8", headers=[("Retry-After", str(wait))])
        return
    # The submitted credentials are compared, never logged or reflected.
    if not handler.server.access.matches_login(username, provided):
        throttle.record_failure(address)
        what = ("username or password" if handler._username_mode()
                else "password" if handler._password_mode() else "token")
        handler._send(401, pages.render_login_page(
            error=f"That {what} was not accepted. Try again.",
            password=handler._password_mode(), username=handler._username_mode(),
            theme=handler._theme()),
            "text/html; charset=utf-8")
        return
    throttle.record_success(address)
    handler._rehash_after_login(provided)
    remember = (form.get(pages.LOGIN_REMEMBER_FIELD) or [""])[0].strip().lower() \
        in _REMEMBER_TRUTHY
    handler._redirect("/", headers=[handler._start_session(remember=remember)])

def rehash_after_login(handler, password):
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
    server = handler.server
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

def logout(handler):
    # Ends only the caller's own session, so no form token is needed:
    # the worst a cross-origin post can do is sign the operator out.
    session_id = handler._session_id()
    if session_id:
        store = _session_store(handler.server)
        if store is not None:
            if store.revoke(session_id):
                with _remember_lock(handler.server):
                    _save_remembered_sessions(handler.server)
        else:
            handler.server.sessions.pop(session_id, None)
            remembered = getattr(handler.server, "remembered_sessions", None) or {}
            with _remember_lock(handler.server):
                if remembered.pop(_session_digest(session_id), None) is not None:
                    _save_remembered_sessions(handler.server)
    flags = "; Secure" if handler._cookie_secure() else ""
    cookie = (f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; "
              f"Path=/; Max-Age=0{flags}")
    handler._redirect("/login", headers=[("Set-Cookie", cookie)])

def rotate_form_token(handler):
    """Replace the config form token after an access-rule change.

    Pages served under the old rule handed this token to anyone who
    could reach the Web UI then, and _post_authorized accepts it on
    its own — if it survived the change, a retained copy would keep
    authorizing protected writes under the new rule, including
    replacing or removing the login that was just set. The browser
    that made the change is signed in with a session, and every
    page it loads next carries the new token."""
    store = _session_store(handler.server)
    if store is not None:
        store.rotate_tokens()
        return
    handler.server.form_token = secrets.token_urlsafe(32)
    handler.server.csrf_secret = secrets.token_bytes(32)

def revoke_remembered_sessions(handler):
    """Revoke every remembered session and delete its store file.

    Called only where the login rule itself was just set, changed or
    removed (the walkthrough Login step, beside its
    _rotate_form_token call): a session minted under the old
    credential must not outlive it. Ephemeral sessions are untouched
    — they die with the process, as before."""
    with _remember_lock(handler.server):
        store = _session_store(handler.server)
        if store is not None:
            store.revoke_all_remembered()
        else:
            remembered = getattr(handler.server, "remembered_sessions", None)
            if remembered is not None:
                remembered.clear()
        try:
            sessions_path(handler.server.settings_data).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("webui: could not delete remembered sessions file: %s", exc)
            _save_remembered_sessions(handler.server)

def revoke_upload_links(handler):
    """Revoke every no-login upload link: the login was set or changed.

    A link minted while the install was open (or under the old
    password) must not keep working once the owner sets a login.
    The link store is rebound to the new credential's fingerprint,
    so even a link the rewrite missed fails the fingerprint check.
    A failure is logged, never raised: the login change stands."""
    try:
        upload_module.revoke_all_links(
            handler.server.settings_data, handler.server.access.credential_fingerprint())
    except OSError as exc:
        log.warning("webui: could not revoke the upload links: %s", exc)

