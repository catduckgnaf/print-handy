"""Issue-grade sanitization for log exports (#478).

``print_handy.logs.redact`` strips secrets from every log line as it is
written. That is the right bar for a private log file, but not for a file
an operator attaches to a *public* GitHub issue: internal IP addresses and
hostnames, chat-platform user/channel IDs, local file paths, email
addresses, MAC addresses and serial numbers are not secrets, yet they do
not belong in a public report either.

This module is the second, export-only pass, used by the diagnostics
bundle and the debug-mode export so there is one standard of "safe to
attach":

* :func:`logs.redact` runs first (secrets and credential shapes);
* internal IPs/hostnames in URLs and bare text are replaced, keeping the
  URL scheme and shape so the line stays diagnosable;
* platform user/channel IDs become **consistent pseudonyms** within one
  export (``user-1``, ``channel-1``, …) so lines still correlate;
* local paths, emails, MACs, serial-shaped values and URL query strings
  are replaced;
* the operator's own machine names (``[public_text] denylist``, #1020)
  become pseudonyms too, and a path inside the app's own data or install
  directories stays readable — only paths outside them are replaced;
* a **verification pass** then re-runs the whole pipeline over the result:
  anything the first pass missed is removed there and counted, so an
  export never silently ships a value this module claims to strip.

Every run returns a counts-only report (categories and counts, never the
removed values) via :meth:`Sanitizer.report`.
"""
from __future__ import annotations

import re
from collections import Counter

from . import logs

#: Report labels, in report order. Keys are the counter names used below.
LABELS = (
    ("secrets", "secrets/tokens"),
    ("ip_addresses", "IP addresses"),
    ("hostnames", "hostnames"),
    ("machine_names", "operator machine names"),
    ("user_ids", "user IDs"),
    ("channel_ids", "channel IDs"),
    ("paths", "file paths"),
    ("emails", "email addresses"),
    ("mac_addresses", "MAC addresses"),
    ("serial_numbers", "serial numbers"),
    ("url_query_strings", "URL query strings"),
)

#: Hosts that are public by design and stay readable in an export (the
#: service name is itself diagnostic; the operator's own hosts are not).
_PUBLIC_HOST_SUFFIXES = (
    "discord.com", "discordapp.com", "telegram.org", "github.com",
    "githubusercontent.com", "googleapis.com", "printables.com",
    "makerworld.com", "thingiverse.com", "myminifactory.com", "cults3d.com",
    "bambulab.com",
)

_URL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^\s'\"<>\]]+)")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b"
                  r"|\b(?:[0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"\b(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}\b")
_INTERNAL_HOST = re.compile(r"(?i)\b(?:[A-Za-z0-9-]+\.)+(?:local|lan|internal|home|localdomain)\b")
_SNOWFLAKE = re.compile(r"\b\d{17,20}\b")
_TELEGRAM_CHAT = re.compile(r"(?<!\d)-100\d{10,}\b")
_MATRIX_ROOM = re.compile(r"![A-Za-z0-9._-]+:[A-Za-z0-9.-]+")
_MATRIX_USER = re.compile(r"(?<![\w.])@[A-Za-z0-9._-]+:[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_USER_ID_CTX = re.compile(r"(?i)\b(user|author|member|sender|approver)([ _-]?id)?(\s*[:=]?\s*)(\d{5,})")
_CHANNEL_ID_CTX = re.compile(r"(?i)\b(channel|chat|room|thread)([ _-]?id)?(\s*[:=]?\s*)(\d{5,})")
_POSIX_PATH = re.compile(r"(?<![:/\w])(?:/(?:[\w.~-]+/)+[\w.~-]*|/(?:[\w.~-]+\.)+[\w~-]+)")
_HOME_PATH = re.compile(r"(?<![\w/])~/(?:[\w.~-]+/)*[\w.~-]*")
_WINDOWS_PATH = re.compile(r"(?i)(?<![\w])[A-Za-z]:\\(?:[^\s\\'\"]+\\)*[^\s\\'\"]*")
_SERIAL_CTX = re.compile(r"(?i)\b(serial(?:\s*number)?|sn|device[ _-]?id|access[ _-]?code)"
                         r"(\s*[:=]\s*)([A-Za-z0-9-]{4,})")
_SERIAL_DASHED = re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9]{4}(?:-[A-Z0-9]{4}){2,}\b")
_SERIAL_BARE = re.compile(r"\b\d{2}[A-Z][A-Z0-9]{11,}\b")
_PSEUDO_HOST = re.compile(r"(?i)^(?:host|ip)-\d+$")


def _is_ip(text):
    if _IPV4.fullmatch(text):
        return all(int(part) <= 255 for part in text.split("."))
    return ":" in text


class Sanitizer:
    """One export's sanitizer: pseudonym maps live here, so the same raw
    value always becomes the same pseudonym within the export."""

    def __init__(self, *, known_user_ids=(), known_channel_ids=(),
                 machine_names=(), keep_path_roots=()):
        self.counts: Counter = Counter()
        self.verification_counts: Counter = Counter()
        self._users: dict[str, str] = {}
        self._channels: dict[str, str] = {}
        self._hosts: dict[str, str] = {}
        self._ips: dict[str, str] = {}
        self._emails: dict[str, str] = {}
        self._machines: dict[str, str] = {}
        # Operator machine names (#1020): whole-word, case-insensitive,
        # the same matching shape as the public-text policy checker, so
        # the name is caught in prose and inside a kept app path alike.
        self._machine_patterns = [
            re.compile(r"(?i)(?<![A-Za-z0-9])" + re.escape(str(name).strip()) + r"(?![A-Za-z0-9])")
            for name in machine_names if str(name).strip()]
        # Paths under one of these roots are the app's own data/install
        # directories (#1020): they keep their layout but are relabeled
        # relative to the root's label (``app-data/state/...``), so the
        # absolute prefix — a home directory, a volume path — never
        # leaves the machine. Every other local path is replaced. Roots
        # are (root, label) pairs (a bare root gets the label
        # ``app-path``), longest root first so a nested root wins. No
        # roots (the default) keeps the stricter remove-every-path
        # behavior for callers that pass none.
        pairs = []
        for item in keep_path_roots or ():
            if isinstance(item, (tuple, list)) and len(item) == 2:
                root, label = str(item[0]).rstrip("/"), str(item[1]).strip() or "app-path"
            else:
                root, label = str(item).rstrip("/"), "app-path"
            if root:
                pairs.append((root, label))
        self._keep_roots = tuple(sorted(pairs, key=lambda pair: len(pair[0]), reverse=True))
        # Known IDs first (longest first so a channel id that contains a
        # user id still wins), so lines naming them correlate even when a
        # value never matches a generic pattern.
        for raw in sorted({str(v) for v in known_user_ids if str(v)}, key=len, reverse=True):
            self._users[raw] = f"user-{len(self._users) + 1}"
        for raw in sorted({str(v) for v in known_channel_ids if str(v)}, key=len, reverse=True):
            self._channels[raw] = f"channel-{len(self._channels) + 1}"

    # -- pseudonym helpers -------------------------------------------------
    def _pseudo(self, table, raw, prefix):
        if raw not in table:
            table[raw] = f"{prefix}-{len(table) + 1}"
        return table[raw]

    def _user(self, raw):
        return self._pseudo(self._users, raw, "user")

    def _channel(self, raw):
        return self._pseudo(self._channels, raw, "channel")

    def _host(self, raw):
        return self._pseudo(self._hosts, raw, "host")

    def _ip(self, raw):
        return self._pseudo(self._ips, raw, "ip")

    def _email(self, raw):
        return self._pseudo(self._emails, raw, "email")

    def _machine(self, raw):
        return self._pseudo(self._machines, raw.lower(), "machine")

    def _kept_path(self, raw):
        """(label, root) when ``raw`` is under an app root, else None."""
        for root, label in self._keep_roots:
            if raw == root or raw.startswith(root + "/"):
                return label, root
        return None

    def _path_repl(self, counts):
        def repl(match):
            raw = match.group(0)
            kept = self._kept_path(raw)
            counts.update(["paths"])
            if kept is not None:
                label, root = kept
                return label + raw[len(root):]
            return "<local path>"
        return repl

    # -- one pass ------------------------------------------------------------
    def _secrets(self, text, counts):
        """logs.redact, with the removals counted (pattern hits + env values).

        A pattern hit only counts when applying it changes the text, so
        an already-redacted ``token=***`` is not "removed" again by the
        verification pass.
        """
        found = 0
        import os
        for name, value in os.environ.items():
            if len(value) >= 8 and name.upper().endswith(logs._SECRET_ENV_SUFFIXES):
                found += text.count(value)
        for pattern, _replacement in logs._PATTERNS:
            found += sum(1 for m in pattern.finditer(text)
                         if pattern.sub(_replacement, m.group(0)) != m.group(0))
        out = logs.redact(text)
        if found:
            counts["secrets"] += found
        return out

    def _url(self, match, counts):
        scheme, rest = match.group(1), match.group(2)
        cut = len(rest)
        for char in "/?#":
            index = rest.find(char)
            if index != -1:
                cut = min(cut, index)
        authority, tail = rest[:cut], rest[cut:]
        if "@" in authority:  # userinfo (credentials) never survives
            counts["secrets"] += 1
            authority = "***@" + authority.rsplit("@", 1)[1]
        host, colon, port = authority.partition(":")
        bare = host.strip("[]")
        if bare and not _PSEUDO_HOST.match(bare) and not any(
                bare.lower() == suffix or bare.lower().endswith("." + suffix)
                for suffix in _PUBLIC_HOST_SUFFIXES):
            if _is_ip(bare):
                counts["ip_addresses"] += 1
                host = self._ip(bare)
            else:
                counts["hostnames"] += 1
                host = self._host(bare.lower())
            authority = host + (colon + port if colon else "")
        if "?" in tail:
            path, _, query = tail.partition("?")
            query, hashmark, fragment = query.partition("#")
            if query and query != "<redacted>":
                counts["url_query_strings"] += 1
                tail = path + "?<redacted>" + (hashmark + fragment if hashmark else "")
        return scheme + authority + tail

    def _pass(self, text, counts):
        out = self._secrets(text, counts)
        for raw, pseudo in sorted(self._users.items(), key=lambda kv: len(kv[0]), reverse=True):
            hits = out.count(raw)
            if hits:
                counts["user_ids"] += hits
                out = out.replace(raw, pseudo)
        for raw, pseudo in sorted(self._channels.items(), key=lambda kv: len(kv[0]), reverse=True):
            hits = out.count(raw)
            if hits:
                counts["channel_ids"] += hits
                out = out.replace(raw, pseudo)
        out = _URL.sub(lambda m: self._url(m, counts), out)
        out = _EMAIL.sub(lambda m: counts.update(["emails"]) or self._email(m.group(0)), out)
        out = _MATRIX_ROOM.sub(lambda m: counts.update(["channel_ids"]) or self._channel(m.group(0)), out)
        out = _MATRIX_USER.sub(lambda m: counts.update(["user_ids"]) or self._user(m.group(0)), out)
        out = _TELEGRAM_CHAT.sub(lambda m: counts.update(["channel_ids"]) or self._channel(m.group(0)), out)
        out = _USER_ID_CTX.sub(
            lambda m: counts.update(["user_ids"]) or
            f"{m.group(1)}{m.group(2) or ''}{m.group(3)}{self._user(m.group(4))}", out)
        out = _CHANNEL_ID_CTX.sub(
            lambda m: counts.update(["channel_ids"]) or
            f"{m.group(1)}{m.group(2) or ''}{m.group(3)}{self._channel(m.group(4))}", out)
        out = _SNOWFLAKE.sub(lambda m: counts.update(["user_ids"]) or self._user(m.group(0)), out)
        out = _MAC.sub(lambda m: counts.update(["mac_addresses"]) or "<mac-address>", out)
        out = _IPV4.sub(
            lambda m: (counts.update(["ip_addresses"]) or self._ip(m.group(0)))
            if _is_ip(m.group(0)) else m.group(0), out)
        out = _IPV6.sub(
            lambda m: (counts.update(["ip_addresses"]) or self._ip(m.group(0)))
            if ("::" in m.group(0) or re.search(r"[A-Fa-f]", m.group(0))) else m.group(0), out)
        out = _INTERNAL_HOST.sub(lambda m: counts.update(["hostnames"]) or self._host(m.group(0).lower()), out)
        for pattern in self._machine_patterns:
            out = pattern.sub(lambda m: counts.update(["machine_names"]) or self._machine(m.group(0)), out)
        out = _WINDOWS_PATH.sub(self._path_repl(counts), out)
        out = _HOME_PATH.sub(lambda m: counts.update(["paths"]) or "<local path>", out)
        out = _POSIX_PATH.sub(self._path_repl(counts), out)
        out = _SERIAL_CTX.sub(
            lambda m: counts.update(["serial_numbers"]) or f"{m.group(1)}{m.group(2)}<serial>", out)
        out = _SERIAL_DASHED.sub(lambda m: counts.update(["serial_numbers"]) or "<serial>", out)
        out = _SERIAL_BARE.sub(lambda m: counts.update(["serial_numbers"]) or "<serial>", out)
        return out

    # -- public API ----------------------------------------------------------
    def sanitize(self, text) -> str:
        """Sanitize ``text``: a first pass, then the verification pass.

        The verification pass re-runs the same pipeline over the first
        pass's output; anything it still removes is counted separately in
        :attr:`verification_counts`, so the report can prove the second
        look happened and what it caught.
        """
        out = self._pass(str(text or ""), self.counts)
        out = self._pass(out, self.verification_counts)
        return out

    def report(self) -> str:
        """The counts-only sanitization report (never the removed values)."""
        total = sum(self.counts.values())
        verified = sum(self.verification_counts.values())
        if not total and not verified:
            return "Sanitization report: nothing needed removing."
        parts = [f"{self.counts[key]} {label}" for key, label in LABELS if self.counts.get(key)]
        line = "Sanitization report: removed " + ", ".join(parts) + "."
        if verified:
            caught = [f"{self.verification_counts[key]} {label}"
                      for key, label in LABELS if self.verification_counts.get(key)]
            line += f" Verification pass removed {', '.join(caught)} more."
        else:
            line += " Verification pass found nothing further."
        return line


def sanitize_text(text, **kwargs) -> tuple[str, str]:
    """(sanitized text, report line) for a one-off export."""
    sanitizer = Sanitizer(**kwargs)
    return sanitizer.sanitize(text), sanitizer.report()


def find_residuals(text) -> dict[str, int]:
    """What a fresh sanitizer would still remove from ``text``.

    Used by tests (and safe to call on any finished export): an export
    produced by :meth:`Sanitizer.sanitize` must report no residuals.
    Pseudonyms registered by the producing export are not known here, so
    only pattern-detectable categories can appear.
    """
    probe = Sanitizer()
    probe._pass(str(text or ""), probe.counts)
    return {key: probe.counts[key] for key, _label in LABELS if probe.counts.get(key)}
