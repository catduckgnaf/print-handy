"""The one Spoolman HTTP client. Every Spoolman call goes through here (#387).

Spoolman is filament bookkeeping. It must never be a dependency for slicing,
starting, pausing, resuming or finishing a print (#235 made the hardware
check warnings-only; #387 closes the paths that escaped it and makes the
rule structural). This module is the only place in Print Handy that opens
an HTTP connection to a Spoolman base URL:

- `request(...)` performs one call with a short timeout and raises
  `SpoolmanUnavailable` on ANY transport or HTTP error, after logging a
  WARNING. It is for callers that must distinguish "Spoolman answered" from
  "Spoolman did not answer" (operator commands, the slot-mapping workflow).
- `call(...)` and the resource helpers (`get_spool`, `list_spools`) are the
  best-effort form used by the print lifecycle: they return a default
  (None) when Spoolman is unavailable, so the caller continues on the
  recorded state and, where usage accounting is affected, records the usage
  as pending in the job record for later reconciliation.
- `health(...)` is the reachability probe for diagnostics; it never raises.

A contract test (tests/pipeline/test_spoolman_wrapper_contract.py) fails CI
if a Spoolman API path is ever combined with a direct HTTP transport
outside this module.

The base URL resolves through `printer_state.spoolman_url()` (settings
`services.spoolman_url` first, the hardware record's cached URL only as a
fallback, #235) unless the caller passes `base_url` — the Hermes-side
callers pass the base from their own config, which is the same setting.
Moonraker's `/server/spoolman/spool_id` endpoint is a Moonraker call, not
a Spoolman call, and is not routed through here.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

TIMEOUT = 5.0
HEALTH_PATH = "/api/v1/health"
UNAVAILABLE_NOTICE = ("Spoolman unavailable; spool tracking skipped for this job and the print "
                      "continues. Check that the Spoolman service is running and that "
                      "services.spoolman_url points at it; the WARNING log line for this failure "
                      "carries the PH_SPOOLMAN_* reference marker.")

_notice_lock = threading.Lock()
_notice_shown = False


class SpoolmanUnavailable(RuntimeError):
    """Spoolman did not answer: unreachable, timed out, or an HTTP error.

    The message is the underlying transport/HTTP error's message, so
    existing warning text built from it stays accurate (#235/#240).
    ``status`` carries the HTTP status when Spoolman answered with one
    (None for a timeout, a refused connection, or "not configured"), so
    callers can tell a 404 — no such record — from an outage (#839).
    ``unconfirmed`` is True when the request went out but no reply came
    back (a read timeout, a dropped connection): a write may have landed,
    so a caller must not blindly send it again.
    """

    def __init__(self, message: str = "", *, status: int | None = None, unconfirmed: bool = False):
        super().__init__(message)
        self.status = status
        self.unconfirmed = unconfirmed


def _resolve_base(base_url: str | None) -> str:
    if base_url is not None:
        return str(base_url).rstrip("/")
    from . import printer_state  # lazy: printer_state imports this module
    return printer_state.spoolman_url()


def request(method: str, path: str, body: Any = None, *,
            base_url: str | None = None, timeout: float | None = None) -> Any:
    """One Spoolman HTTP call. Returns the parsed JSON body on success.

    Raises SpoolmanUnavailable on any transport or HTTP error (a WARNING
    with the detail is logged first). Raises it too when no Spoolman URL
    is configured: "not configured" is an unavailability outcome, never a
    reason to fall back to some other transport.
    """
    base = _resolve_base(base_url)
    if not base:
        logger.warning("Spoolman %s %s skipped: services.spoolman_url is not configured",
                       method, path)
        raise SpoolmanUnavailable("Spoolman is not configured (services.spoolman_url)")
    sent = False
    try:
        # Construction is inside the protected block on purpose (P2-154): a
        # configured URL with a malformed authority (http://[::1) passes the
        # prefix-only settings validation, and urllib raises ValueError while
        # *building* the request — that is an unavailability outcome, not a
        # caller error, or a bad bookkeeping endpoint would abort slicing.
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json",
                                              "Accept": "application/json"})
        sent = True
        with urllib.request.urlopen(req, timeout=TIMEOUT if timeout is None else timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
        return json.loads(raw)
    except Exception as exc:
        # #390: the warning carries the taxonomy marker so a support
        # conversation can start from one greppable string.
        from .. import diagnostics
        logger.warning("Spoolman %s %s failed: %s [%s]", method, path, exc,
                       diagnostics.marker("spoolman", diagnostics.classify(exc)))
        # urllib wraps every failure to connect or send in URLError (an
        # HTTPError is an answer); anything else after urlopen was called
        # (a read timeout, a reset while waiting) left the request unanswered.
        raise SpoolmanUnavailable(str(exc) or type(exc).__name__,
                                  status=getattr(exc, 'code', None),
                                  unconfirmed=sent and not isinstance(exc, urllib.error.URLError)) from exc


def call(method: str, path: str, body: Any = None, *,
         base_url: str | None = None, timeout: float | None = None,
         default: Any = None) -> Any:
    """Best-effort `request`: returns `default` when Spoolman is unavailable."""
    try:
        return request(method, path, body, base_url=base_url, timeout=timeout)
    except SpoolmanUnavailable:
        return default


def get_spool(spool_id: int, *, base_url: str | None = None,
              timeout: float | None = None) -> dict[str, Any] | None:
    """One spool record, or None when Spoolman is unavailable or has no such record shape."""
    result = call("GET", f"/api/v1/spool/{int(spool_id)}", base_url=base_url, timeout=timeout)
    return result if isinstance(result, dict) else None


def list_spools(*, base_url: str | None = None, timeout: float | None = None,
                allow_archived: bool = False) -> list[Any] | None:
    """Spool records (archived ones only with allow_archived), or None when Spoolman is unavailable."""
    path = "/api/v1/spool?allow_archived=true" if allow_archived else "/api/v1/spool"
    result = call("GET", path, base_url=base_url, timeout=timeout)
    return result if isinstance(result, list) else None


def health(*, base_url: str | None = None, timeout: float | None = None) -> tuple[bool, str]:
    """(ok, detail) for one Spoolman base URL's /api/v1/health. Never raises.

    The detail strings match the long-standing diagnostics format:
    "HTTP <status>" for an answer, "<ErrorType>: <message>" for a failure.
    """
    base = _resolve_base(base_url)
    if not base:
        return False, "Spoolman is not configured (services.spoolman_url)"
    try:
        with urllib.request.urlopen(base + HEALTH_PATH,
                                    timeout=TIMEOUT if timeout is None else timeout) as response:
            return response.status == 200, f"HTTP {response.status}"
    except Exception as exc:  # a down service is data, not a caller error
        return False, f"{type(exc).__name__}: {exc}"


def note_unavailable_once(printer=print) -> bool:
    """Emit UNAVAILABLE_NOTICE exactly once per process; True when this call emitted it.

    Slicing and print start can each attempt several Spoolman calls; the
    user gets the one line from #387, not one line per attempt. The detail
    of every failure is in the WARNING log.
    """
    global _notice_shown
    with _notice_lock:
        if _notice_shown:
            return False
        _notice_shown = True
    printer(UNAVAILABLE_NOTICE, flush=True)
    return True


def reset_notice() -> None:
    """Forget that the notice was shown (tests; a fresh job in a long-lived process)."""
    global _notice_shown
    with _notice_lock:
        _notice_shown = False
