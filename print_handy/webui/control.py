"""Per-printer control for the Web UI API (#708).

The write companion to :mod:`print_handy.webui.status`: where
``printer_status()`` reads one roster printer's live status for
``GET /api/printers/<id>/status``, this module acts on one roster
printer for the control routes the server serves **only** while
``[webui] controls_enabled`` is true:

- ``POST /api/printers/<id>/pause``
- ``POST /api/printers/<id>/resume``
- ``POST /api/printers/<id>/cancel``
- ``POST /api/printers/<id>/target`` — set target temperatures

Guard design (settled on the issue, recorded here so the code and the
docs cannot drift apart): controls are **off by default** — while the
setting is false the server does not serve these routes at all. When
enabled, the caller must hold the same Web UI access token as the
read routes; there is deliberately no second token system and no
extra gate on cancel in this version. Chat control is unchanged and
stays confirm-first; the API is the surface an operator explicitly
opts into for trusted callers (the Home Assistant integration's
phase 3 is the consumer this was built for).

Actions route through the same transports chat uses — Moonraker's
``/printer/print/*`` endpoints for Moonraker connections, and each
family adapter's own ``control()`` operation for the rest — with the
chat paths' discipline (``print_handy.hermes.family_control`` and
``print_stop`` / ``print_control`` are the reference): re-read the
state first, refuse when the state does not allow the action, send at
most one command, then read back and report what the printer actually
shows. Nothing here retries, and a refusal or a failure is always an
explicit non-2xx answer in the Web UI's ``{ok, reason}`` shape — a
control action never silently succeeds.

Target temperatures follow the same per-family honesty: Moonraker
printers take ``SET_HEATER_TEMPERATURE`` through
``/printer/gcode/script`` (extruder and heater_bed, the heaters the
normalized status reports) and OctoPrint printers take its printer
API targets (tool0 and bed). The remaining families have no
temperature-setting transport Print Handy may use — Bambu Lab's LAN
protocol is never sent raw G-code by project rule, SDCP and PrusaLink
expose no heater command in the surfaces Print Handy has verified,
and stock Flashforge takes no temperatures from Print Handy — so a
target request to one of them is a named, tested refusal (422), not
a guess. The ``offline`` connection is refused for every action:
Print Handy never contacts those printers.
"""
from __future__ import annotations

import json
import math
import time
import urllib.request

from .. import healthcheck, printers, settings
from . import status as status_module

ACTIONS = ("pause", "resume", "cancel")

# The shared adapter control vocabulary (family_control's): chat spells
# the third action "cancel", the adapters spell it "stop".
_FAMILY_ACTIONS = {"pause": "pause", "resume": "resume", "cancel": "stop"}

# States each action may be sent from — the chat paths' rule (a pause
# only for a printing print, a resume only for a paused one, a cancel
# for either). Anything else is a 409 refusal and nothing is sent.
_STATE_GATES = {
    "pause": (frozenset({"printing"}), "pause", "printing"),
    "resume": (frozenset({"paused"}), "resume", "paused"),
    "cancel": (frozenset({"printing", "paused"}), "cancel", "printing or paused"),
}

_ACTIVE = frozenset({"printing", "paused"})

# Read-back: after a send, poll the live status until the state leaves
# the pre-action state or the attempts run out (the chat paths wait up
# to 20 s; an HTTP caller gets a shorter window and a truthful detail
# saying the command was sent but the state has not changed yet).
_READBACK_ATTEMPTS = 6
_READBACK_POLL_S = 0.5

_CONTROL_TIMEOUT_S = 10

# Sanity bounds for a target request, deliberately generous: the
# printer's own firmware limits sit below them and a firmware refusal
# surfaces as a failed send. 0 is valid — it turns a heater off.
_TARGET_MAX_C = {"hotend_c": 400.0, "bed_c": 200.0}


class UnsupportedControl(RuntimeError):
    """This printer's connection has no such control through Print Handy."""


def _connection_of(adapter):
    """An adapter's connection, asked the way status.py asks it."""
    return getattr(adapter, "connection", None) or getattr(adapter, "transport", None)


def _resolve(data, printer_id):
    """(entry, view, identity) for one roster printer, or the 404 tuple.

    The identity fields match the /api/status roster rows, exactly as
    printer_status() builds them. Returns ``(None, (code, body))`` for
    an unknown id.
    """
    try:
        entry = settings.by_id(printer_id, data)
    except settings.SettingsError:
        return None, (404, {"ok": False,
                            "reason": f"no printer with id {printer_id!r} is configured"})
    view = settings.view_for(printer_id, data)
    identity = {
        "id": entry["id"],
        "name": entry.get("name") or "",
        "model": entry.get("model") or "",
        "vendor": view["printer"].get("vendor") or "",
        "connection": healthcheck.listener_connection(view),
    }
    return (entry, view, identity), None


def _moonraker_post(view, path, body=None):
    """One POST to a Moonraker printer; raises on any failure."""
    base = (view.get("services", {}).get("moonraker_url") or "").rstrip("/")
    if not base:
        raise ValueError("no Moonraker URL is configured for this printer")
    payload = json.dumps(body).encode() if body is not None else b""
    request = urllib.request.Request(
        f"{base}{path}", data=payload, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=_CONTROL_TIMEOUT_S) as response:
        response.read()


def _send_action(adapter, view, action):
    """Send one pause / resume / cancel through the printer's own transport.

    Raises UnsupportedControl for a connection with no control path
    (offline, or anything unrecognized) and whatever the transport
    raises for a failed send. Exactly one command is sent.
    """
    connection = _connection_of(adapter)
    if connection == "moonraker":
        _moonraker_post(view, f"/printer/print/{action}")
        return
    if connection in status_module._FAMILY_STATUS_READS:
        adapter.control(_FAMILY_ACTIONS[action])
        return
    if connection == "offline":
        raise UnsupportedControl(
            "connection 'offline': Print Handy never contacts this printer")
    raise UnsupportedControl(f"no print control for connection {connection!r}")


def _send_target(adapter, view, hotend_c, bed_c):
    """Send target temperatures through the printer's own transport.

    Only Moonraker (SET_HEATER_TEMPERATURE) and adapters that define
    ``set_targets`` (OctoPrint) can do this today; every other
    connection raises UnsupportedControl — see the module docstring
    for the per-family reasons.
    """
    connection = _connection_of(adapter)
    if connection == "moonraker":
        lines = []
        if hotend_c is not None:
            lines.append(f"SET_HEATER_TEMPERATURE HEATER=extruder TARGET={hotend_c:g}")
        if bed_c is not None:
            lines.append(f"SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET={bed_c:g}")
        _moonraker_post(view, "/printer/gcode/script", {"script": "\n".join(lines)})
        return
    setter = getattr(adapter, "set_targets", None)
    if setter is not None:
        setter(hotend_c=hotend_c, bed_c=bed_c)
        return
    if connection == "offline":
        raise UnsupportedControl(
            "connection 'offline': Print Handy never contacts this printer")
    raise UnsupportedControl(
        f"setting temperatures is not supported for connection {connection!r} "
        "through Print Handy")


def _normalized(adapter, view, read):
    live = read(adapter, view)
    return {**status_module._STATUS_DEFAULTS, **(live or {})}


def _read_back(adapter, view, read, sleep, before_state, action):
    """Poll after a send for the state the printer settles into.

    Stops early once the state is no longer the pre-action state (for
    cancel: no longer active). A failed read-back read keeps the last
    good status — the send already happened and is reported as sent.
    """
    current = None
    for attempt in range(_READBACK_ATTEMPTS):
        if attempt:
            sleep(_READBACK_POLL_S)
        try:
            current = _normalized(adapter, view, read)
        except Exception:
            continue
        state = str(current.get("state") or "unknown")
        if action == "cancel":
            if state not in _ACTIVE:
                break
        elif state != before_state:
            break
    return current


def _action_detail(action, before_state, after):
    """Truthful one-line outcome, in the chat paths' wording discipline."""
    state = str((after or {}).get("state") or "unknown")
    if action == "pause":
        if state == "paused":
            return "Paused."
        return (f"Pause sent, but the printer still reports {state}. "
                "Check the printer screen. Not retrying.")
    if action == "resume":
        if state == "printing":
            return "Resumed."
        return (f"Resume sent, but the printer still reports {state}. "
                "Check the printer screen. Not retrying.")
    if state not in _ACTIVE:
        return f"Stopped. Printer now reports {state}."
    return (f"Cancel sent, but the printer still reports {state}. "
            "Check the printer screen. Not retrying.")


def printer_control(data, printer_id, action, *, read=None, send=None, sleep=time.sleep):
    """(http_code, body) for POST /api/printers/<id>/pause|resume|cancel (#708).

    ``read`` / ``send`` override the live read and the transport send
    for tests, with the same signatures status.printer_status() and
    this module's defaults use (read(adapter, view), send(adapter,
    view, action)).
    """
    if action not in ACTIONS:
        return 404, {"ok": False, "reason": f"no such control action: {action!r}"}
    resolved, error = _resolve(data, printer_id)
    if error is not None:
        return error
    entry, view, identity = resolved
    read = status_module.read_live_status if read is None else read
    send = _send_action if send is None else send
    try:
        adapter = printers.get(entry["model"], view)
        before = _normalized(adapter, view, read)
    except Exception as exc:  # unreachable / unconfigured: explicit failure
        return 502, {"ok": False,
                     "reason": f"could not read printer {printer_id!r} before "
                               f"{action}: {type(exc).__name__}: {exc}"}
    allowed, _verb, wanted = _STATE_GATES[action]
    state = str(before.get("state") or "unknown")
    if state not in allowed:
        return 409, {"ok": False,
                     "reason": f"nothing to {action}: printer {printer_id!r} is "
                               f"{state} (needs {wanted}); nothing sent"}
    try:
        send(adapter, view, action)
    except UnsupportedControl as exc:
        return 422, {"ok": False, "reason": str(exc)}
    except Exception as exc:  # the send itself failed: explicit, never silent
        return 502, {"ok": False,
                     "reason": f"{action} for printer {printer_id!r} failed: "
                               f"{type(exc).__name__}: {exc}"}
    after = _read_back(adapter, view, read, sleep, state, action)
    body = {"ok": True, **identity, "action": action,
            "detail": _action_detail(action, state, after)}
    if after is not None:
        body["status"] = after
    return 200, body


def _validate_target(name, value):
    """An error reason for one target value, or None when it is usable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"{name} must be a number of degrees Celsius"
    if not math.isfinite(value):
        return f"{name} must be a finite number of degrees Celsius"
    if not 0 <= value <= _TARGET_MAX_C[name]:
        return f"{name} must be between 0 and {_TARGET_MAX_C[name]:g} degrees Celsius"
    return None


def printer_target(data, printer_id, hotend_c, bed_c, *, read=None, send=None):
    """(http_code, body) for POST /api/printers/<id>/target (#708).

    ``hotend_c`` / ``bed_c`` are the request's values (None = not
    requested). At least one is required. ``send`` overrides the
    transport send for tests (send(adapter, view, hotend_c, bed_c)).
    """
    resolved, error = _resolve(data, printer_id)
    if error is not None:
        return error
    entry, view, identity = resolved
    if hotend_c is None and bed_c is None:
        return 400, {"ok": False,
                     "reason": "at least one of hotend_c and bed_c is required"}
    for name, value in (("hotend_c", hotend_c), ("bed_c", bed_c)):
        if value is not None:
            problem = _validate_target(name, value)
            if problem is not None:
                return 400, {"ok": False, "reason": problem}
    read = status_module.read_live_status if read is None else read
    send = _send_target if send is None else send
    try:
        adapter = printers.get(entry["model"], view)
    except Exception as exc:
        return 502, {"ok": False,
                     "reason": f"could not build the adapter for printer "
                               f"{printer_id!r}: {type(exc).__name__}: {exc}"}
    try:
        send(adapter, view, hotend_c, bed_c)
    except UnsupportedControl as exc:
        return 422, {"ok": False, "reason": str(exc)}
    except Exception as exc:
        return 502, {"ok": False,
                     "reason": f"setting temperatures on printer {printer_id!r} "
                               f"failed: {type(exc).__name__}: {exc}"}
    targets = {name: value for name, value
               in (("hotend_c", hotend_c), ("bed_c", bed_c)) if value is not None}
    body = {"ok": True, **identity, "action": "target", "targets": targets,
            "detail": "Targets sent: " + ", ".join(
                f"{name} {value:g} °C" for name, value in targets.items()) + "."}
    try:
        body["status"] = _normalized(adapter, view, read)
    except Exception:  # the send already happened; a failed read-back is noted, not fatal
        body["detail"] += " The read-back failed, so the current status is unverified."
    return 200, body
