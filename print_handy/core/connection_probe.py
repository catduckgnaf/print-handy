"""Per-domain connection probes for candidate printer settings (#537).

One question, answered before anything is saved: *does a printer answer at
the address just typed, with the credentials the operator says are stored?*
Guided printer onboarding runs a probe against the candidate roster fields
(not against any configured printer), reports what answered, and loops back
to the field that failed. There was no ``test_connection`` utility anywhere
before this module: doctor probes the *configured* Moonraker/Spoolman URLs,
``printer_state check`` needs a pre-existing hardware record and treats an
unconfigured Bambu/Prusa/SDCP connection as a pass, and the setup wizard
makes no network call at all.

Each domain's probe is that domain's lightest known proof call:

- moonraker: ``GET {moonraker_url}/printer/info`` (the doctor proof).
- bambulab_lan: ``BambuLanClient.snapshot()`` — a full MQTT snapshot, up to
  the client's timeout; MQTT rc=5 means the access code was rejected, which
  is reported as its own category so the flow can loop back to the secret
  step rather than the host step.
- prusalink: ``GET /api/v1/info`` through PrusaLinkClient.
- elegoo_sdcp: a WebSocket Cmd 0 status round trip; the MainboardID is
  discovered by the client as part of it (SDCP has no authentication).
- octoprint: ``GET {octoprint_url}/api/version`` with the API-key header.
- flashforge_stock: ``POST :8898/detail`` — the one call that validates the
  host and both credentials (serial + check code) at once.

A probe is read-only for every domain: a GET, a status request, or the
stock ``/detail`` read. It never starts, moves, heats, or configures
anything. Results are a :class:`ProbeResult`: ``ok``, a user-facing
``summary`` of what answered (facts the protocol itself returned — host
name, serial, firmware — never a secret), a user-facing ``error`` on
failure, and a ``category`` the caller maps to the field to revisit:
``unreachable`` (host/URL fields), ``auth`` (the stored secret), ``missing_secret``
(no secret stored yet), ``protocol`` (something answered, but not as the
domain speaks it — host/URL fields), or ``config`` (the field values are
not well-formed).

Secret *values* are passed in by the caller, resolved from the environment
or the profile ``.env`` under the name the roster entry will use; this
module never reads the environment itself and never puts a secret in a
result, a summary, or an exception it raises.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field as _dc_field

CATEGORIES = ("unreachable", "auth", "missing_secret", "protocol", "config")


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    summary: str = ""
    error: str = ""
    category: str = ""
    detail: dict = _dc_field(default_factory=dict)


def _fail(category, error):
    return ProbeResult(ok=False, error=error, category=category)


def _http_get_json(url, *, headers=None, timeout_s, opener):
    """One GET expecting JSON. Returns (status, payload); HTTPError propagates
    with its status, transport failures propagate as OSError/URLError."""
    req = urllib.request.Request(url, headers=headers or {})
    with opener(req, timeout=timeout_s) as resp:
        body = resp.read()
    return getattr(resp, "status", 200), json.loads(body.decode("utf-8", "replace"))


def _moonraker_geometry(url, headers, timeout_s, opener):
    """Best-effort nozzle, bed, and max Z. A failed read never fails the probe."""
    from .printer_geometry import from_configfile
    try:
        _status, payload = _http_get_json(
            url + "/printer/objects/query?configfile",
            headers=headers, timeout_s=timeout_s, opener=opener)
    except Exception:
        return None, "the printer config did not include nozzle, bed, or Z"
    found = from_configfile(payload)
    if found.get("error"):
        return None, found["error"]
    return found, ""


def _probe_moonraker(fields, secrets, timeout_s, opener):
    url = str(fields.get("moonraker_url") or "").strip().rstrip("/")
    if not url:
        return _fail("config", "no Moonraker URL was given")
    headers = {}
    if secrets.get("moonraker_api_key"):
        headers["X-Api-Key"] = secrets["moonraker_api_key"]
    try:
        status, payload = _http_get_json(url + "/printer/info", headers=headers,
                                         timeout_s=timeout_s, opener=opener)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _fail("auth", f"Moonraker at {url} refused the request (HTTP {exc.code}); "
                                 "if it expects an API key, store it as MOONRAKER_API_KEY and retry")
        return _fail("protocol", f"Moonraker at {url} answered HTTP {exc.code} on /printer/info")
    except (urllib.error.URLError, OSError, TimeoutError):
        return _fail("unreachable", f"nothing answered at {url} — check the printer is on and "
                                    "the URL (host and port, usually 7125) is right")
    except ValueError:
        return _fail("protocol", f"the answer from {url}/printer/info was not Moonraker JSON")
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return _fail("protocol", f"the answer from {url}/printer/info carried no printer info")
    detail = {k: result[k] for k in ("hostname", "software_version") if result.get(k)}
    geometry, geometry_error = _moonraker_geometry(url, headers, timeout_s, opener)
    if geometry:
        detail["geometry"] = geometry
    if geometry_error:
        detail["geometry_error"] = geometry_error
    bits = [str(detail[k]) for k in ("hostname", "software_version") if k in detail]
    summary = f"Moonraker at {url} answered /printer/info" + (f" — {', '.join(bits)}" if bits else "")
    return ProbeResult(ok=True, summary=summary, detail=detail)


def _probe_bambu(fields, secrets, timeout_s, client_factory):
    from ..printers.bambulab.client import BambuLanClient, BambuLanError
    host = str(fields.get("lan_host") or "").strip()
    serial = str(fields.get("lan_serial") or "").strip()
    if not secrets.get("access_code"):
        return _fail("missing_secret", "no LAN access code is stored yet — the printer cannot "
                                       "be tested until the code is in its environment variable")
    try:
        client = (client_factory or BambuLanClient)(
            host, serial, secrets["access_code"], timeout_s=max(timeout_s, 15.0))
    except BambuLanError as exc:
        return _fail("config", str(exc))
    try:
        snapshot = client.snapshot()
    except BambuLanError as exc:
        text = str(exc)
        if "access code" in text or "access denied" in text:
            return _fail("auth", text)
        return _fail("unreachable", text)
    detail = {"serial": serial}
    info = snapshot.get("info") if isinstance(snapshot, dict) else None
    if isinstance(info, dict):
        for module in info.get("module") or []:
            if isinstance(module, dict) and module.get("name") == "ota" and module.get("sw_ver"):
                detail["firmware"] = str(module["sw_ver"])
    bits = [f"serial {serial}"] + ([f"firmware {detail['firmware']}"] if "firmware" in detail else [])
    return ProbeResult(ok=True,
                       summary=f"the Bambu Lab printer at {host} answered a full status "
                               f"snapshot ({', '.join(bits)})",
                       detail=detail)


def _probe_prusa(fields, secrets, timeout_s, client_factory):
    from ..printers.prusa.client import PrusaLinkClient, PrusaLinkError
    host = str(fields.get("link_host") or "").strip()
    if not secrets.get("api_key"):
        return _fail("missing_secret", "no PrusaLink API key is stored yet — the printer cannot "
                                       "be tested until the key is in its environment variable")
    try:
        client = (client_factory or PrusaLinkClient)(host, secrets["api_key"])
    except PrusaLinkError as exc:
        return _fail("config", str(exc))
    try:
        info = client.info()
    except PrusaLinkError as exc:
        text = str(exc)
        if " 401" in text or " 403" in text:
            return _fail("auth", f"PrusaLink at {host} refused the API key (it answered "
                                 "401/403); re-check the key stored in the environment variable")
        if "malformed JSON" in text:
            return _fail("protocol", f"the answer from PrusaLink at {host} on /api/v1/info "
                                     "was not PrusaLink JSON — check the host really is "
                                     "the printer's PrusaLink address")
        return _fail("unreachable", f"PrusaLink at {host} did not answer /api/v1/info — check "
                                    "the printer is on and the host is right")
    if not isinstance(info, dict):
        return _fail("protocol", f"the answer from PrusaLink at {host} was not the expected info")
    printer = info.get("printer") if isinstance(info.get("printer"), dict) else {}
    detail = {}
    for key, source in (("hostname", info.get("hostname") or printer.get("hostname")),
                        ("name", printer.get("name")),
                        ("serial", printer.get("serial")),
                        ("firmware", info.get("firmware"))):
        if source:
            detail[key] = str(source)
    bits = [str(detail[k]) for k in ("name", "hostname", "serial", "firmware") if k in detail]
    summary = f"PrusaLink at {host} answered /api/v1/info" + (f" — {', '.join(bits)}" if bits else "")
    return ProbeResult(ok=True, summary=summary, detail=detail)


def _probe_sdcp(fields, secrets, timeout_s, client_factory):
    from ..printers.elegoo_sdcp.client import SdcpClient, SdcpError
    host = str(fields.get("host") or "").strip()
    try:
        client = (client_factory or SdcpClient)(host, request_timeout=timeout_s)
    except SdcpError as exc:
        return _fail("config", str(exc))
    try:
        status = client.status()
    except SdcpError as exc:
        return _fail("unreachable", f"the SDCP printer at {host} did not answer a status "
                                    f"request: {exc}")
    finally:
        try:
            client.close()
        except Exception:
            pass
    detail = {}
    if getattr(client, "mainboard_id", ""):
        detail["mainboard_id"] = str(client.mainboard_id)
    if isinstance(status, dict) and status.get("MachineName"):
        detail["machine"] = str(status["MachineName"])
    bits = [str(detail[k]) for k in ("machine", "mainboard_id") if k in detail]
    summary = f"the Elegoo SDCP printer at {host} answered a status request" + \
        (f" — {', '.join(bits)}" if bits else "")
    return ProbeResult(ok=True, summary=summary, detail=detail)


def _probe_octoprint(fields, secrets, timeout_s, opener):
    url = str(fields.get("octoprint_url") or "").strip().rstrip("/")
    if not url:
        return _fail("config", "no OctoPrint URL was given")
    if not secrets.get("api_key"):
        return _fail("missing_secret", "no OctoPrint API key is stored yet — the printer cannot "
                                       "be tested until OCTOPRINT_API_KEY is set")
    try:
        status, payload = _http_get_json(url + "/api/version",
                                         headers={"X-Api-Key": secrets["api_key"]},
                                         timeout_s=timeout_s, opener=opener)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _fail("auth", f"OctoPrint at {url} refused the API key (HTTP {exc.code}); "
                                 "re-check the key stored as OCTOPRINT_API_KEY")
        return _fail("protocol", f"OctoPrint at {url} answered HTTP {exc.code} on /api/version")
    except (urllib.error.URLError, OSError, TimeoutError):
        return _fail("unreachable", f"nothing answered at {url} — check OctoPrint is running "
                                    "and the URL is right")
    except ValueError:
        return _fail("protocol", f"the answer from {url}/api/version was not OctoPrint JSON")
    if not isinstance(payload, dict) or "server" not in payload:
        return _fail("protocol", f"the answer from {url}/api/version was not OctoPrint's")
    detail = {k: str(payload[k]) for k in ("server", "api", "text") if payload.get(k)}
    bits = [f"OctoPrint {detail['server']}"] if "server" in detail else []
    return ProbeResult(ok=True,
                       summary=f"OctoPrint at {url} answered /api/version" +
                               (f" — {', '.join(bits)}" if bits else ""),
                       detail=detail)


def _probe_stock(fields, secrets, timeout_s, post):
    from ..printers.flashforge_stock.client import BUSY_CODE, DETAIL_PATH, _endpoint
    host = str(fields.get("host") or "").strip()
    serial = str(secrets.get("serial") or "").strip()
    code = str(secrets.get("check_code") or "").strip()
    if not serial or not code:
        return _fail("missing_secret", "the stock Flashforge credentials are not both stored "
                                       "yet (AD5X_SERIAL and AD5X_CHECK_CODE) — the printer "
                                       "cannot be tested until they are")
    try:
        endpoint = _endpoint(host, DETAIL_PATH)
    except Exception:
        return _fail("config", "the stock Flashforge host must be a plain IP or hostname")
    if post is None:
        from ..printers.flashforge_stock.client import _urlopen_post
        post = _urlopen_post
    try:
        reply = post(endpoint, {"serialNumber": serial, "checkCode": code}, "application/json")
    except Exception:
        # The original exception can carry the request body (and with it the
        # check code); it is never chained or quoted.
        return _fail("unreachable", f"nothing answered at {host}:8898 — check the printer is "
                                    "on and the host is right")
    if not isinstance(reply, dict):
        return _fail("protocol", f"the answer from {host}:8898 was not the stock API's")
    if reply.get("code") == BUSY_CODE:
        return _fail("protocol", "the printer answered but reports itself busy; retry when it "
                                 "is idle")
    if reply.get("code") != 0:
        return _fail("auth", "the printer refused the stored serial number / check code pair; "
                             "re-check AD5X_SERIAL and AD5X_CHECK_CODE")
    detail_obj = reply.get("detail")
    if not isinstance(detail_obj, dict):
        return _fail("protocol", f"the answer from {host}:8898 carried no printer detail")
    detail = {}
    for key, source in (("name", detail_obj.get("name")),
                        ("state", detail_obj.get("state") or detail_obj.get("status")),
                        ("firmware", detail_obj.get("firmwareVersion") or detail_obj.get("version"))):
        if source:
            detail[key] = str(source)
    bits = [str(detail[k]) for k in ("name", "state", "firmware") if k in detail]
    summary = f"the stock Flashforge printer at {host} answered /detail" + \
        (f" — {', '.join(bits)}" if bits else "")
    return ProbeResult(ok=True, summary=summary, detail=detail)


_PROBES = {
    "moonraker": lambda f, s, t, kw: _probe_moonraker(f, s, t, kw.get("opener") or urllib.request.urlopen),
    "bambulab_lan": lambda f, s, t, kw: _probe_bambu(f, s, t, kw.get("bambu_factory")),
    "prusalink": lambda f, s, t, kw: _probe_prusa(f, s, t, kw.get("prusa_factory")),
    "elegoo_sdcp": lambda f, s, t, kw: _probe_sdcp(f, s, t, kw.get("sdcp_factory")),
    "octoprint": lambda f, s, t, kw: _probe_octoprint(f, s, t, kw.get("opener") or urllib.request.urlopen),
    "flashforge_stock": lambda f, s, t, kw: _probe_stock(f, s, t, kw.get("stock_post")),
}


def probe(connection, fields, secrets=None, *, timeout_s=10.0, **factories):
    """Test whether a printer answers for the candidate ``fields``.

    ``connection`` is the vendor connection string (``printers``
    ``VendorProfile.connection``); ``fields`` carries the candidate roster
    entry's connection values; ``secrets`` carries the resolved secret
    values under the role names ``moonraker_api_key``, ``access_code``,
    ``api_key``, ``serial`` and ``check_code``. Client factories and the
    HTTP opener are injectable for tests (``opener``, ``bambu_factory``,
    ``prusa_factory``, ``sdcp_factory``, ``stock_post``). Never raises for a
    printer-side problem — those are results, not exceptions.
    """
    handler = _PROBES.get(str(connection or ""))
    if handler is None:
        return _fail("config", f"no connection probe exists for {connection!r}")
    return handler(dict(fields or {}), dict(secrets or {}), float(timeout_s), factories)
