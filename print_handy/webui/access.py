"""Who may use the Web UI, decided in one place (#708 first-run setup).

The server asks an :class:`Access` whether a credential is required and
whether a provided one matches; it never holds the secret itself. The
rules, first match wins, keep every existing install exactly as it was:

1. ``[webui] require_auth = false`` — open, the explicit trusted-network
   opt-out (#704). A token in the environment is ignored, as before.
2. The access token in the environment variable ``[webui]
   access_token_env`` names — that token is required, as before.
3. The choice saved by the setup page or the walkthrough's Login step in
   the state folder (``ACCESS_FILE``): a username and hashed password, or
   an explicit "open". A saved login is checked before rule 2 for people
   signing in (#875); the environment token is still accepted alongside
   it, so header clients keep working and the token remains a way back
   in after a forgotten password. A saved "open" does not override a token.
4. A loopback bind — open (the form token still guards writes), as before.
5. Otherwise — ``setup``: the Web UI serves only the setup page until the
   operator sets a password or affirmatively leaves it open. Before #708
   this case refused to start at all, so no working install lands here.

The password is stored only as a salted PBKDF2-SHA256 hash; the file is
written atomically with mode 0600. Deleting it is the reset; the
environment token also signs in when the password is forgotten.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import settings

ACCESS_FILE = "webui_access.json"
MIN_PASSWORD_LENGTH = 8
MAX_USERNAME_LENGTH = 64
_ITERATIONS = 200_000
_LOOPBACK_NAMES = frozenset({"localhost"})

OPEN, TOKEN, PASSWORD, SETUP = "open", "token", "password", "setup"


class AccessError(ValueError):
    """A setup choice was refused (too short, mismatched, or not confirmed)."""


def _is_loopback(host):
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _digest(secret, salt, iterations):
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iterations)


@dataclass
class Access:
    """The resolved access rule. ``mode`` is one of OPEN, TOKEN, PASSWORD, SETUP."""

    mode: str
    _token: str = field(default="", repr=False)
    _salt: bytes = field(default=b"", repr=False)
    _hash: bytes = field(default=b"", repr=False)
    _iterations: int = _ITERATIONS
    # The login name chosen on the setup page. Empty for a password saved
    # before usernames existed: then any name is accepted with the password.
    username: str = ""
    # Passwords already verified this process, kept as SHA-256 digests so a
    # client polling with the header does not pay PBKDF2 on every request.
    _verified: set = field(default_factory=set, repr=False)

    @property
    def required(self):
        """True when a request must carry a matching credential."""
        return self.mode in (TOKEN, PASSWORD)

    @property
    def setup_pending(self):
        return self.mode == SETUP

    def matches(self, provided):
        """Constant-time check of a provided token or password."""
        if not provided:
            return False
        if self.mode == TOKEN:
            return hmac.compare_digest(provided.encode(), self._token.encode())
        if self.mode == PASSWORD:
            if self._token and hmac.compare_digest(provided.encode(), self._token.encode()):
                return True
            quick = hashlib.sha256(provided.encode()).digest()
            if any(hmac.compare_digest(quick, seen) for seen in self._verified):
                return True
            if hmac.compare_digest(_digest(provided, self._salt, self._iterations), self._hash):
                if len(self._verified) < 8:
                    self._verified.add(quick)
                return True
        return False


    def matches_login(self, username, provided):
        """A username and password pair (the login form).

        TOKEN mode has no username, so any name is accepted with the token.
        The name is compared in constant time and never logged. The
        environment token, when one is set, is accepted with any name: it
        is the documented way back in after a forgotten password."""
        if self._token and provided and hmac.compare_digest(
                provided.encode(), self._token.encode()):
            return True
        if self.mode == PASSWORD and self.username:
            if not hmac.compare_digest((username or "").strip().casefold().encode(),
                                       self.username.casefold().encode()):
                # Still pay for the hash so a wrong name is not faster.
                self.matches(provided)
                return False
        return self.matches(provided)


class LoginThrottle:
    """Bounded backoff for failed logins, kept in memory per client address.

    After ``max_failures`` failures inside ``window_s`` the address is locked
    out for ``lockout_s``; during the lockout even a correct credential is
    refused, so guessing cannot continue at full speed. One address never
    locks another out (behind a reverse proxy every client shares the proxy's
    address, so they share a lockout). The table is capped so spraying from
    many addresses cannot grow memory without bound.
    """

    def __init__(self, max_failures=5, window_s=300, lockout_s=60, max_entries=1024,
                 clock=time.monotonic):
        self.max_failures = max_failures
        self.window_s = window_s
        self.lockout_s = lockout_s
        self.max_entries = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = {}   # address -> [failure times]
        self._locked = {}     # address -> lockout end

    def retry_after(self, address):
        """Seconds left on this address's lockout, or 0 when it may try."""
        with self._lock:
            end = self._locked.get(address, 0)
            now = self._clock()
            if end > now:
                return int(end - now) + 1
            self._locked.pop(address, None)
            return 0

    def record_failure(self, address):
        with self._lock:
            now = self._clock()
            recent = [t for t in self._failures.get(address, []) if now - t < self.window_s]
            recent.append(now)
            if len(recent) >= self.max_failures:
                self._locked[address] = now + self.lockout_s
                recent = []
            self._failures[address] = recent
            if len(self._failures) > self.max_entries:
                for key in [k for k, v in self._failures.items()
                            if not v or now - v[-1] >= self.window_s]:
                    del self._failures[key]
                for key in [k for k, end in self._locked.items() if end <= now]:
                    del self._locked[key]
                while len(self._failures) > self.max_entries:
                    self._failures.pop(next(iter(self._failures)))

    def record_success(self, address):
        with self._lock:
            self._failures.pop(address, None)


def access_path(data):
    return Path(settings.state_dir(data)) / ACCESS_FILE


def _read_saved(path):
    """The saved choice as an Access, or None when there is none (or it is unreadable)."""
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(saved, dict):
        return None
    if saved.get("mode") == OPEN:
        return Access(OPEN)
    if saved.get("mode") == PASSWORD:
        try:
            return Access(PASSWORD, _salt=base64.b64decode(saved["salt"]),
                          _hash=base64.b64decode(saved["hash"]),
                          _iterations=int(saved["iterations"]),
                          username=str(saved.get("username") or "").strip())
        except (KeyError, TypeError, ValueError):
            return None
    return None


def resolve(data, host, environ=None):
    """The Access for this configuration, environment and bind host."""
    environ = os.environ if environ is None else environ
    webui = data["webui"]
    if not webui.get("require_auth", True):
        return Access(OPEN)
    token = environ.get(webui["access_token_env"]) or ""
    saved = _read_saved(access_path(data))
    if saved is not None and saved.mode == PASSWORD:
        # A login chosen in the Web UI wins over the environment token for
        # people (#875), and the token keeps working as a credential, so
        # Home Assistant and other header clients are unaffected.
        saved._token = token
        return saved
    if token:
        return Access(TOKEN, _token=token)
    if saved is not None:
        return saved
    if _is_loopback(host):
        return Access(OPEN)
    return Access(SETUP)


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".webui_access.")
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


def clean_username(username):
    """The login name as stored, or AccessError when it cannot be used."""
    username = (username or "").strip()
    if not username:
        raise AccessError("Choose a username.")
    if len(username) > MAX_USERNAME_LENGTH or ":" in username or any(
            ch.isspace() or not ch.isprintable() for ch in username):
        raise AccessError(f"Use a username of up to {MAX_USERNAME_LENGTH} characters "
                          "with no spaces or colons.")
    return username


def save_password(data, password, confirm, username=None):
    """Store a new Web UI login (password hashed). Raises AccessError when refused.

    ``username`` is required for a new login; None keeps the old
    password-only form (any name accepted), which older callers use."""
    if username is not None:
        username = clean_username(username)
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AccessError(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    if password != confirm:
        raise AccessError("The two passwords do not match.")
    salt = secrets.token_bytes(16)
    payload = {"mode": PASSWORD}
    if username:
        payload["username"] = username
    _write(access_path(data), {
        **payload, "iterations": _ITERATIONS,
        "salt": base64.b64encode(salt).decode(),
        "hash": base64.b64encode(_digest(password, salt, _ITERATIONS)).decode()})


def save_open(data, confirmed):
    """Record the operator's affirmative choice to run without a password."""
    if not confirmed:
        raise AccessError("Tick the box to confirm you want no password.")
    _write(access_path(data), {"mode": OPEN})
