"""The one Moonraker transport (#1466).

Every HTTP request Print Handy sends to a Moonraker goes through here, so
the base URL handling, the ``X-Api-Key`` header for ``MOONRAKER_API_KEY``,
timeouts and error classification live in exactly one place. Before this
module, sixteen modules each built their own ``urllib`` request and most
forgot the key, so on a key-protected Moonraker stop, pause, launch state
and the purge gate all failed — usually silently, folded into "the printer
could not say".

Classification: a 401 or 403 from Moonraker is a :class:`MoonrakerAuthError`,
whose text names ``MOONRAKER_API_KEY``. It is never folded into a generic
"unreadable" result by this module; callers that swallow errors decide for
themselves, and the surfaces that must name it (the doctor, chat's stop and
resume paths, the purge launch gate) catch it explicitly.

Installs without a key behave exactly as before: no header is added and the
same requests go out in the same shape.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

__all__ = [
    "MoonrakerAuthError",
    "api_key",
    "build_request",
    "is_moonraker_url",
    "moonraker_json",
    "open_url",
    "url_bytes",
    "url_json",
    "url_text",
]

AUTH_MESSAGE = (
    "Moonraker rejected the request as unauthorized (HTTP {code}): this "
    "Moonraker is key-protected and the API key Print Handy sent was missing "
    "or wrong. Set MOONRAKER_API_KEY to the printer's Moonraker API key and try again."
)


class MoonrakerAuthError(urllib.error.HTTPError):
    """A 401/403 from Moonraker: an authentication failure, not "unreadable".

    Subclasses ``HTTPError`` so callers that already handle HTTP errors keep
    working, and carries the status on both ``code`` and ``status``.
    """

    def __init__(self, url: str, code: int):
        self.auth_message = AUTH_MESSAGE.format(code=code)
        super().__init__(url, code, self.auth_message, hdrs=None, fp=None)

    def __str__(self) -> str:  # HTTPError.__str__ would hide the remedy
        return self.auth_message


def _profile_key() -> str:
    """The key from the profile environment (.env files), or "".

    The environment itself is checked by :func:`api_key` first; this is the
    same second source ``runtime._config.profile_env_value`` gives the
    modules that already sent the key (guided setup, the listener).
    """
    try:
        from .runtime import _config

        return _config.profile_env_value("MOONRAKER_API_KEY")
    except Exception:
        return ""


def api_key(explicit: str | None = None) -> str:
    """The configured Moonraker API key, or "" when none is configured.

    ``explicit`` wins when given (including "", which forces no key);
    otherwise the ``MOONRAKER_API_KEY`` environment variable, then the
    profile .env files.
    """
    if explicit is not None:
        return str(explicit).strip()
    value = os.environ.get("MOONRAKER_API_KEY")
    if value:
        return value.strip()
    return _profile_key().strip()


def _configured_bases() -> tuple[str, ...]:
    """Every Moonraker base URL the settings know (default + roster)."""
    bases: list[str] = []
    try:
        from . import settings

        data = settings.current()
        url = (data.get("services") or {}).get("moonraker_url")
        if url:
            bases.append(str(url).rstrip("/"))
        for entry in settings.printers(data):
            url = entry.get("moonraker_url") if isinstance(entry, dict) else None
            if url:
                bases.append(str(url).rstrip("/"))
    except Exception:
        return ()
    return tuple(dict.fromkeys(bases))


def is_moonraker_url(url: str) -> bool:
    """Whether ``url`` points at a configured Moonraker.

    Lets a generic transport (``jobs.common.request_json``, which also
    talks to outside model sites) add the key only where it belongs: the
    key is never sent to a non-Moonraker host.
    """
    text = str(url or "")
    return any(text == base or text.startswith(base + "/") for base in _configured_bases())


def build_request(url: str, *, method: str | None = None, data: Any = None,
                  headers: dict[str, str] | None = None, moonraker: bool = False,
                  key: str | None = None) -> urllib.request.Request:
    """A request for ``url`` with the Moonraker key header when it belongs.

    ``data`` may be bytes (sent as-is), a dict/list (JSON-encoded, with a
    JSON content type unless the caller set one), or None. ``moonraker``
    marks the URL as a Moonraker URL; without it the key is added only when
    the URL matches a configured Moonraker base.
    """
    body: bytes | None
    merged = dict(headers or {})
    if isinstance(data, (bytes, bytearray)):
        body = bytes(data)
    elif data is not None:
        body = json.dumps(data).encode("utf-8")
        if not any(name.lower() == "content-type" for name in merged):
            merged["Content-Type"] = "application/json"
    else:
        body = None
    request = urllib.request.Request(str(url), method=method, data=body, headers=merged)
    resolved = api_key(key)
    if resolved and (moonraker or is_moonraker_url(str(url))):
        if request.get_header("X-api-key") is None:
            request.add_header("X-Api-Key", resolved)
    return request


def open_url(url_or_request: Any, *, timeout: float = 10, method: str | None = None,
             data: Any = None, headers: dict[str, str] | None = None,
             moonraker: bool = False, key: str | None = None):
    """Open one request and return the response (a context manager).

    A string URL (or a caller-built ``Request``) gains the key header the
    same way :func:`build_request` adds it. A 401/403 answer raises
    :class:`MoonrakerAuthError` when the request is a Moonraker request
    (marked, or aimed at a configured Moonraker base); other HTTP errors
    propagate unchanged.
    """
    if isinstance(url_or_request, urllib.request.Request):
        request = url_or_request
        target = request.full_url
        marked = moonraker or is_moonraker_url(target)
        resolved = api_key(key)
        if resolved and marked and request.get_header("X-api-key") is None:
            request.add_header("X-Api-Key", resolved)
    else:
        target = str(url_or_request)
        marked = moonraker or is_moonraker_url(target)
        request = build_request(target, method=method, data=data, headers=headers,
                                moonraker=moonraker, key=key)
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if marked and exc.code in (401, 403):
            raise MoonrakerAuthError(target, exc.code) from exc
        raise


def _read_json(raw: bytes) -> Any:
    return json.loads(raw or b"{}")


def moonraker_json(base: str, path: str, *, method: str = "GET", data: Any = None,
                   timeout: float = 10, headers: dict[str, str] | None = None,
                   key: str | None = None) -> Any:
    """One JSON request to ``base + path`` on a Moonraker; the parsed reply."""
    url = str(base or "").rstrip("/") + path
    with open_url(url, timeout=timeout, method=method, data=data, headers=headers,
                  moonraker=True, key=key) as response:
        return _read_json(response.read())


def url_json(url: str, *, method: str | None = None, data: Any = None,
             timeout: float = 10, headers: dict[str, str] | None = None,
             moonraker: bool = False, key: str | None = None) -> Any:
    """One JSON request to a full URL; the parsed reply."""
    with open_url(url, timeout=timeout, method=method, data=data, headers=headers,
                  moonraker=moonraker, key=key) as response:
        return _read_json(response.read())


def url_bytes(url: str, *, timeout: float = 10, headers: dict[str, str] | None = None,
              moonraker: bool = False, key: str | None = None) -> bytes:
    """One GET to a full URL; the raw reply bytes."""
    with open_url(url, timeout=timeout, headers=headers, moonraker=moonraker, key=key) as response:
        return response.read()


def url_text(url: str, *, timeout: float = 10, headers: dict[str, str] | None = None,
             moonraker: bool = False, key: str | None = None) -> str:
    """One GET to a full URL; the reply decoded as text."""
    return url_bytes(url, timeout=timeout, headers=headers, moonraker=moonraker, key=key).decode("utf-8", "replace")
