"""Who may use the Web UI, decided in one place (#708 first-run setup).

The server asks an :class:`Access` whether a credential is required and
whether a provided one matches; it never holds the secret itself. The
rules, first match wins, keep every existing install exactly as it was:

1. ``[webui] require_auth = false`` — open, the explicit trusted-network
   opt-out (#704). A token in the environment is ignored, as before.
2. The access token in the environment variable ``[webui]
   access_token_env`` names — that token is required, as before.
3. The choice saved by the first-run setup page in the state folder
   (``ACCESS_FILE``): a hashed password, or an explicit "open".
4. A loopback bind — open (the form token still guards writes), as before.
5. Otherwise — ``setup``: the Web UI serves only the setup page until the
   operator sets a password or affirmatively leaves it open. Before #708
   this case refused to start at all, so no working install lands here.

The password is stored only as a salted PBKDF2-SHA256 hash; the file is
written atomically with mode 0600. Deleting it (or setting the
environment token) is the reset.
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
from dataclasses import dataclass, field
from pathlib import Path

from .. import settings

ACCESS_FILE = "webui_access.json"
MIN_PASSWORD_LENGTH = 8
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
            quick = hashlib.sha256(provided.encode()).digest()
            if any(hmac.compare_digest(quick, seen) for seen in self._verified):
                return True
            if hmac.compare_digest(_digest(provided, self._salt, self._iterations), self._hash):
                if len(self._verified) < 8:
                    self._verified.add(quick)
                return True
        return False


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
                          _iterations=int(saved["iterations"]))
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
    if token:
        return Access(TOKEN, _token=token)
    saved = _read_saved(access_path(data))
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


def save_password(data, password, confirm):
    """Store a new Web UI password (hashed). Raises AccessError when refused."""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AccessError(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    if password != confirm:
        raise AccessError("The two passwords do not match.")
    salt = secrets.token_bytes(16)
    _write(access_path(data), {
        "mode": PASSWORD, "iterations": _ITERATIONS,
        "salt": base64.b64encode(salt).decode(),
        "hash": base64.b64encode(_digest(password, salt, _ITERATIONS)).decode()})


def save_open(data, confirmed):
    """Record the operator's affirmative choice to run without a password."""
    if not confirmed:
        raise AccessError("Tick the box to confirm you want no password.")
    _write(access_path(data), {"mode": OPEN})
