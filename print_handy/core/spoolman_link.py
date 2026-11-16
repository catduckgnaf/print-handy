"""Detect a printer's own Spoolman link and resolve who counts filament (#1536).

Print Handy can run against its bundled Spoolman, an outside Spoolman on
another host, or the very Spoolman instance a printer's Moonraker already
points at. When Moonraker is linked to a Spoolman it charges filament usage
live as a print runs; if Print Handy also charges the same print at the end,
the spool is counted twice. This module is the one place that answers two
questions for every Moonraker printer:

- is this printer's Moonraker linked to a Spoolman, and which instance?
  (:func:`detect_spoolman_link`, fed by an injectable transport so it is
  fully testable offline), and
- who is the one accounting authority for the printer, Print Handy or the
  printer itself? (:func:`resolve_accounting_authority`).

The parsers here are pure; the only network shape is the caller's
``moonraker_call(method, path) -> payload`` callable.
"""
from __future__ import annotations

import logging
import threading
import time

from urllib.parse import urlsplit

# The stored values for [spool] accounting and a roster entry's
# spoolman_accounting: "" and "auto" both mean "decide from the detected
# Moonraker link", the other two name the authority outright.
ACCOUNTING_AUTO = "auto"
ACCOUNTING_PRINT_HANDY = "print_handy"
ACCOUNTING_PRINTER = "printer"
ACCOUNTING_VALUES = ("", ACCOUNTING_AUTO, ACCOUNTING_PRINT_HANDY, ACCOUNTING_PRINTER)


logger = logging.getLogger(__name__)
_identity_cache = {}
_identity_cache_lock = threading.Lock()
_IDENTITY_CACHE_SECONDS = 300

def _spoolman_fingerprint(url, fetch):
    """Stable instance evidence: API info and the lowest-id spool identity."""
    info = fetch(url, "/api/v1/info")
    spools = fetch(url, "/api/v1/spool")
    if not isinstance(info, dict) or not isinstance(spools, list) or not spools:
        raise ValueError("incomplete Spoolman identity response")
    spool = min(spools, key=lambda item: int(item["id"]))
    registered = spool.get("registered") or spool.get("registered_at")
    if not registered:
        raise ValueError("spool registration timestamp missing")
    return (info, int(spool["id"]), str(registered))

def _same_instance_by_probe(a, b, fetch):
    key = tuple(sorted((normalize_spoolman_url(a), normalize_spoolman_url(b))))
    now = time.monotonic()
    with _identity_cache_lock:
        cached = _identity_cache.get(key)
        if cached and now - cached[0] < _IDENTITY_CACHE_SECONDS:
            return cached[1]
    try:
        result = _spoolman_fingerprint(a, fetch) == _spoolman_fingerprint(b, fetch)
    except Exception as exc:
        logger.info("could not confirm the two Spoolman URLs are one instance: %s", exc)
        result = None
    with _identity_cache_lock:
        _identity_cache[key] = (now, result)
    return result

def _production_identity_fetch(url, path):
    from . import spoolman_client
    return spoolman_client.request("GET", path, base_url=url, timeout=2)


def normalize_spoolman_url(url) -> str:
    """Canonicalize a Spoolman base URL for comparison.

    Lowercases scheme and host, drops the scheme's default port and any
    trailing slashes, and assumes http when no scheme is spelled out, so
    ``HTTP://NAS.local:7912/`` and ``http://nas.local:7912`` compare equal.
    An empty or hostless value normalizes to "".
    """
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    scheme = (parts.scheme or "http").lower()
    port = parts.port
    if (scheme, port) in (("http", 80), ("https", 443)):
        port = None
    netloc = host if port is None else f"{host}:{port}"
    path = (parts.path or "").rstrip("/")
    return f"{scheme}://{netloc}{path}"


def same_spoolman_instance(a, b) -> bool:
    """True when two URLs name the same Spoolman instance (both non-empty)."""
    left, right = normalize_spoolman_url(a), normalize_spoolman_url(b)
    return bool(left) and left == right


def _config_map(payload) -> dict:
    """The Moonraker config mapping out of a /server/config payload, any shape."""
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("config"), dict):
        return result["config"]
    if isinstance(payload.get("config"), dict):
        return payload["config"]
    return payload


def parse_moonraker_spoolman_config(payload) -> str:
    """The [spoolman] server URL out of a Moonraker GET /server/config payload.

    Moonraker reports its loaded config under result.config; the spoolman
    component's section key is "spoolman" and its URL option is "server".
    Returns "" when the printer has no spoolman section (not linked).
    """
    section = _config_map(payload).get("spoolman")
    if not isinstance(section, dict):
        return ""
    return str(section.get("server") or "").strip()


def parse_active_spool_payload(payload):
    """The active spool id out of a /server/spoolman/spool_id payload, or None."""
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if isinstance(result, dict):
        value = result.get("spool_id")
    elif result is not None:
        value = result
    else:
        value = payload.get("spool_id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def detect_spoolman_link(moonraker_call) -> dict:
    """Ask a Moonraker whether it is linked to a Spoolman, and which one.

    ``moonraker_call(method, path)`` returns the decoded JSON payload (or
    raises). Two signals, either is enough: the loaded config names a
    [spoolman] server, or the spool_id endpoint answers at all (Moonraker
    only serves it when the spoolman component is loaded). Fail-soft: a
    printer that cannot be asked reads as unlinked, with the failure kept
    in "error" when neither signal came back.
    """
    detection = {"linked": False, "server_url": "", "active_spool_id": None, "error": ""}
    errors = []
    try:
        server = parse_moonraker_spoolman_config(moonraker_call("GET", "/server/config"))
        if server:
            detection["server_url"] = server
    except Exception as exc:  # noqa: BLE001 - detection is best-effort by design
        errors.append(str(exc))
    answered = False
    try:
        detection["active_spool_id"] = parse_active_spool_payload(
            moonraker_call("GET", "/server/spoolman/spool_id"))
        answered = True
    except Exception as exc:  # noqa: BLE001 - same: an unlinked Moonraker 404s here
        errors.append(str(exc))
    detection["linked"] = bool(detection["server_url"]) or answered
    if errors and not detection["linked"]:
        detection["error"] = "; ".join(errors)
    return detection


def resolve_accounting_authority(configured, detection=None, configured_url="", *,
                                 firmware_assigns_spools=False, identity_probe=None) -> dict:
    """Resolve the one accounting authority for a printer.

    An explicit configured value ("print_handy" or "printer") wins. "" and
    "auto" hand counting to the printer only when both are true: its
    Moonraker is linked to a Spoolman, and its firmware picks the active
    spool itself at each tool change (``firmware_assigns_spools``, the
    OpenCreator AFC case). A linked Moonraker alone is not enough: on
    every other printer Print Handy points Moonraker at the job's spool
    for a one-spool print and clears it and charges each slot itself for a
    multi-slot print, so handing those to the printer would count nothing. The returned dict carries the authority, where the decision came
    from ("source": "setting" or "auto"), the detection facts, whether the
    Moonraker server and the configured Spoolman URL are the same instance
    (None when either is unknown), and human-readable warnings for the
    combinations that would double-count or count nothing.
    """
    detection = detection or {}
    configured_norm = str(configured or "").strip().lower()
    linked = bool(detection.get("linked"))
    moonraker_server = str(detection.get("server_url") or "").strip()
    both_known = bool(moonraker_server) and bool(str(configured_url or "").strip())
    same_instance = (same_spoolman_instance(moonraker_server, configured_url) if both_known else None)
    if both_known and same_instance is False and identity_probe is not None:
        if identity_probe is _production_identity_fetch:
            same_instance = _same_instance_by_probe(moonraker_server, configured_url, identity_probe)
        else:
            try:
                same_instance = (_spoolman_fingerprint(moonraker_server, identity_probe) ==
                                 _spoolman_fingerprint(configured_url, identity_probe))
            except Exception as exc:
                # Unverified, never assumed: the warning below says so.
                logger.info("could not confirm the two Spoolman URLs are one instance: %s", exc)
                same_instance = None
    warnings = []
    if configured_norm == ACCOUNTING_PRINT_HANDY:
        authority, source = ACCOUNTING_PRINT_HANDY, "setting"
    elif configured_norm == ACCOUNTING_PRINTER and linked:
        authority, source = ACCOUNTING_PRINTER, "setting"
    elif configured_norm == ACCOUNTING_PRINTER:
        # A missing link cannot act as an accounting authority. Fall back to
        # Print Handy rather than silently losing every filament charge.
        authority, source = ACCOUNTING_PRINT_HANDY, "fallback"
        warnings.append(
            "Spool accounting is set to the printer, but no Moonraker Spoolman link "
            "was detected for it; Print Handy will count filament usage instead.")
    else:
        authority = (ACCOUNTING_PRINTER if linked and firmware_assigns_spools
                     else ACCOUNTING_PRINT_HANDY)
        source = "auto"
    if linked and both_known and same_instance is not True:
        unverified = same_instance is None
        if authority == ACCOUNTING_PRINT_HANDY:
            warnings.append(
                "This printer's Moonraker charges Spoolman at "
                f"{normalize_spoolman_url(moonraker_server)} while Print Handy charges "
                f"{normalize_spoolman_url(configured_url)}: " +
                ("could not confirm they are the same Spoolman, so the print may be counted twice. "
                 "Point both at one Spoolman instance, or set spool accounting to the printer."
                 if unverified else
                 "the same print can be counted twice, once by each side. Point both at one "
                 "Spoolman instance, or set spool accounting to the printer."))
        else:
            warnings.append(
                "This printer's Moonraker counts usage in Spoolman at "
                f"{normalize_spoolman_url(moonraker_server)} while Print Handy reads "
                f"{normalize_spoolman_url(configured_url)}: " +
                ("could not confirm they are the same Spoolman, so loaded spools and remaining weights "
                 "may not line up." if unverified else
                 "two Spoolman instances are in play, so loaded spools and remaining weights "
                 "will not line up."))
    return {
        "authority": authority,
        "source": source,
        "configured": configured_norm or ACCOUNTING_AUTO,
        "moonraker_linked": linked,
        "moonraker_server": moonraker_server,
        "same_instance": same_instance,
        "warnings": warnings,
    }


def _firmware_assigns_spools(printer_id, adapter=None) -> bool:
    """Whether this printer's firmware picks the active spool itself (adapter fact)."""
    if adapter is None:
        from print_handy import printers, settings
        adapter = (printers.get(settings.by_id(printer_id)["model"], settings.view_for(printer_id))
                   if printer_id else printers.current())
    return firmware_assigns_spools(adapter)


def firmware_assigns_spools(adapter) -> bool:
    """True only when the adapter declares the fact as a real ``True``.

    A missing attribute, None or any other truthy object (a test double, a
    misspelled override) keeps Print Handy counting: handing a print to the
    printer on a guess would count it zero times.
    """
    return getattr(adapter, "firmware_assigns_spools", False) is True


def accounting_authority_for(printer_id=None, *, moonraker_call=None, data=None, adapter=None) -> dict:
    """Resolve the accounting authority for one printer from the settings.

    The printer's roster entry wins over the global [spool] accounting and
    services.spoolman_url (settings.view_for already projects both). With
    no moonraker_call, no network happens and "auto" resolves as if no
    link were detected; pass a call to fold in a live detection.
    """
    from print_handy import settings

    if data is None:
        data = settings.current()
    view = settings.view_for(printer_id, data) if printer_id else data
    configured = (view.get("spool") or {}).get("accounting") or ""
    configured_url = (view.get("services") or {}).get("spoolman_url") or ""
    detection = (detect_spoolman_link(moonraker_call) if moonraker_call is not None
                 else {"linked": False, "server_url": "", "active_spool_id": None, "error": ""})
    try:
        assigns = _firmware_assigns_spools(printer_id, adapter)
    except Exception as exc:  # noqa: BLE001 - an unknown model keeps today's behavior
        logger.warning("could not read the printer's spool handling, Print Handy keeps counting: %s", exc)
        assigns = False
    return resolve_accounting_authority(configured, detection, configured_url,
                                        firmware_assigns_spools=assigns, identity_probe=_production_identity_fetch)
