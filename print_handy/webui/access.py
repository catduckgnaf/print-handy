"""Who may use the Web UI, decided in one place.

The server asks an :class:`Access` whether a credential is required and
whether a provided one matches; it never holds the secret itself. The
rules, first match wins:

1. ``[webui] require_auth = false`` — open, the explicit trusted-network
   opt-out (#704). A token in the environment is ignored, as before.
2. A login saved by the walkthrough's Login step in the state folder
   (``ACCESS_FILE``): a username and hashed password. A saved login is
   checked before rule 3 for people signing in (#875); the environment
   token is still accepted alongside it, so header clients keep working
   and the token remains a way back in after a forgotten password.
   A saved "open" choice is honored after rule 3: it does not override
   a token.
3. The access token in the environment variable ``[webui]
   access_token_env`` names — that token is required, as before.
4. Otherwise — open and *unclaimed*: no credential exists yet, so the
   Web UI serves everything and surfaces the unclaimed state (the
   walkthrough is offered, with its Login step as the place to set a
   password). A credential comes into existence only by a deliberate
   act: the Docker variable (rule 3), the walkthrough's Login step, or
   a chat platform writing through the same writers.

This reverses the first-run setup gate from #708 (and #440 before it):
an unclaimed install is no longer blocked behind a setup page that
serves nothing else. The ``SETUP`` mode name is kept only so older
imports do not break; :func:`resolve` never returns it.

The password is stored only as a salted PBKDF2-SHA256 hash; the file is
written atomically with mode 0600. Deleting it is the reset; the
environment token also signs in when the password is forgotten.

A file that exists but cannot be used (unreadable, not JSON, missing
fields) resolves to ``BROKEN``, never to "unclaimed": the server then
refuses every request but ``/healthz`` with a 503 naming the file
until the owner fixes or deletes it. Only a missing file is unclaimed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
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

OPEN, TOKEN, PASSWORD, SETUP = "open", "token", "password", "setup"
# A saved login file that exists but cannot be used. Nothing matches it
# and the server refuses every request (except /healthz) until the file
# is fixed or deleted: a claimed install never silently reopens.
BROKEN = "broken"
# Why an OPEN Access is open (Access.open_reason), shown on the status
# page's banner: nothing set yet, the require_auth opt-out, or the
# owner's saved "leave it open" choice.
OPEN_UNCLAIMED, OPEN_AUTH_OFF, OPEN_SAVED = "unclaimed", "require_auth_off", "saved_open"


class AccessError(ValueError):
    """A setup choice was refused (too short, mismatched, or not confirmed)."""


def _digest(secret, salt, iterations):
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iterations)


@dataclass
class Access:
    """The resolved access rule. ``mode`` is one of OPEN, TOKEN, PASSWORD.

    ``unclaimed`` is true when the mode is OPEN only because nothing has
    been set yet — no saved choice, no environment token, and
    ``require_auth`` not turned off. It is a state the UI surfaces (a
    "no login set" note pointing at the walkthrough's Login step), never
    a serving block."""

    mode: str
    unclaimed: bool = False
    # OPEN only: OPEN_UNCLAIMED, OPEN_AUTH_OFF or OPEN_SAVED.
    open_reason: str = ""
    # OPEN_AUTH_OFF only: a saved password or an environment token exists
    # but is being ignored because require_auth is off.
    credential_ignored: bool = False
    # BROKEN only: the plain explanation the server answers with, and the
    # full path of the damaged file for the log.
    problem: str = ""
    _broken_path: str = field(default="", repr=False)
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
        """True when a request must carry a matching credential.

        BROKEN counts: nothing matches it, so anything that only asks
        whether a credential is required stays closed."""
        return self.mode in (TOKEN, PASSWORD, BROKEN)

    @property
    def broken(self):
        """True when the saved login file exists but cannot be used."""
        return self.mode == BROKEN

    def credential_fingerprint(self):
        """A hash naming this exact credential configuration.

        Persisted Web UI state that was minted under a credential (the
        remembered-session store) carries this fingerprint and is
        discarded when it no longer matches, so rotating the token in
        the environment or changing the saved password invalidates that
        state at the next start instead of letting it ride along. The
        secret itself is never stored — only its hash, salt and hash
        already being hashes in password mode."""
        material = "\x00".join((self.mode, self._token, self.username,
                                str(self._iterations),
                                self._salt.hex(), self._hash.hex(), self.problem))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @property
    def setup_pending(self):
        """Kept for older callers: nothing is ever setup-pending any more.

        The gate reversal removed the setup serving block; an install
        with no credential is OPEN and ``unclaimed`` instead."""
        return False

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
            if len(self._failures) > self.max_entries or len(self._locked) > self.max_entries:
                for key in [k for k, v in self._failures.items()
                            if not v or now - v[-1] >= self.window_s]:
                    del self._failures[key]
                for key in [k for k, end in self._locked.items() if end <= now]:
                    del self._locked[key]
                while len(self._failures) > self.max_entries:
                    self._failures.pop(next(iter(self._failures)))
                while len(self._locked) > self.max_entries:
                    self._locked.pop(next(iter(self._locked)))

    def record_success(self, address):
        with self._lock:
            self._failures.pop(address, None)
            self._locked.pop(address, None)


def access_path(data):
    return Path(settings.state_dir(data)) / ACCESS_FILE


def _broken(path, cause):
    return Access(BROKEN, problem=(
        f"The Web UI login file {path.name} in the state folder exists but {cause}. "
        "The Web UI refuses every request until it is fixed, instead of reopening "
        "without the login that was set. Fix the file (for example its owner or "
        "permissions, so the user Print Handy runs as can read it), or delete it to "
        "clear the login: the Web UI is then open until a new one is set."),
        _broken_path=str(path))


def _read_saved(path):
    """The saved choice as an Access, or None when there is no saved file.

    A file that exists but cannot be used — unreadable (often a container
    user change or a restore with other ownership), not JSON, or missing
    the fields its mode needs — is a BROKEN Access, never None: the owner
    chose a login, and reading the damage as "nothing saved" would reopen
    the Web UI to anyone without a word."""
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except PermissionError:
        return _broken(path, "cannot be read (permission denied; the file probably "
                             "belongs to another user, as after a container user change "
                             "or a restore)")
    except (OSError, ValueError) as exc:
        return _broken(path, f"cannot be read ({type(exc).__name__})")
    try:
        saved = json.loads(text)
    except ValueError:
        return _broken(path, "is not valid JSON (it may have been cut short or edited by hand)")
    if not isinstance(saved, dict):
        return _broken(path, "does not hold a saved login")
    if saved.get("mode") == OPEN:
        return Access(OPEN, open_reason=OPEN_SAVED)
    if saved.get("mode") == PASSWORD:
        try:
            return Access(PASSWORD, _salt=base64.b64decode(saved["salt"], validate=True),
                          _hash=base64.b64decode(saved["hash"], validate=True),
                          _iterations=int(saved["iterations"]),
                          username=str(saved.get("username") or "").strip())
        except (KeyError, TypeError, ValueError):
            return _broken(path, "is missing the fields a saved password needs")
    return _broken(path, "names no login mode this version knows")


def resolve(data, host, environ=None):
    """The Access for this configuration, environment and bind host."""
    environ = os.environ if environ is None else environ
    webui = data["webui"]
    token = environ.get(webui["access_token_env"]) or ""
    saved = _read_saved(access_path(data))
    if not webui.get("require_auth", True):
        # The explicit opt-out wins, but the Access says whether it is
        # hiding a login the owner set, so the status page can say so.
        ignored = bool(token) or (saved is not None and saved.mode in (PASSWORD, BROKEN))
        return Access(OPEN, open_reason=OPEN_AUTH_OFF, credential_ignored=ignored)
    if saved is not None and saved.mode == BROKEN:
        # Fail closed, even beside an environment token: the file is the
        # owner's choice and nobody can tell what it held.
        return saved
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
    # Nothing set: open on every bind, loopback or not, with the claim
    # state exposed so the UI can offer the walkthrough and its Login
    # step instead of gating on a setup page (the #708 reversal).
    return Access(OPEN, unclaimed=True, open_reason=OPEN_UNCLAIMED)


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


PASSWORD_HINT = ("Eight characters is the minimum; 12 or more, such as three or four unrelated words, is better. "
                 "Avoid your username, repeated characters and common passwords.")
_COMMON_PASSWORDS = frozenset({
    "password", "password1", "password123", "passw0rd", "12345678", "123456789",
    "1234567890", "qwertyui", "qwerty123", "iloveyou", "letmein1", "welcome1",
    "admin123", "administrator", "changeme", "printhandy", "print-handy",
    "abc12345", "11111111", "00000000"})


def weak_password_reason(password, username=None):
    """Why a password is too guessable for a network login, or None."""
    lowered = (password or "").casefold()
    if lowered in _COMMON_PASSWORDS:
        return "That password is on the list of commonly guessed passwords. " + PASSWORD_HINT
    if username and lowered == username.casefold():
        return "The password must not be the same as the username. " + PASSWORD_HINT
    if len(set(lowered)) < 4:
        return "That password repeats too few distinct characters. " + PASSWORD_HINT
    return None


def check_password(password, confirm, username=None):
    """The cleaned username for a login save_password would accept.

    Raises AccessError exactly as save_password does, without writing,
    so a caller can check the choice before it changes anything else."""
    if username is not None:
        username = clean_username(username)
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AccessError(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    weak = weak_password_reason(password, username)
    if weak:
        raise AccessError(weak)
    if password != confirm:
        raise AccessError("The two passwords do not match.")
    return username


def save_password(data, password, confirm, username=None):
    """Store a new Web UI login (password hashed). Raises AccessError when refused.

    ``username`` is required for a new login; None keeps the old
    password-only form (any name accepted), which older callers use."""
    username = check_password(password, confirm, username)
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
