"""Web UI printer control endpoints (#708, control endpoints phase).

``POST /api/printers/<id>/pause|resume|cancel`` and
``POST /api/printers/<id>/temperature`` send one command to one roster
printer through the same paths chat uses:

- Moonraker printers: Moonraker's ``/printer/print/pause|resume|cancel``
  (``print_control`` / ``print_stop``) and ``M104`` / ``M140`` through
  ``/printer/gcode/script`` (the ``nozzle_change`` heat path);
- every other family: the adapter's own ``control()`` operation
  (``family_control.FamilyControl``), with cancel spelled ``stop``.

Guard (the issue requires one): controls are OFF by default. Only
``[webui] controls_enabled = true`` turns them on; while off every
control route answers 403 with the reason and sends nothing. ``cancel``
additionally needs the JSON body ``{"confirm": true}``. A target
temperature is checked against the printer's own limits (its
``max_hotend_c`` / ``max_bed_c`` facts, the fitted nozzle type's safe
maximum) and refused above them, never clamped. A family with no
existing temperature command path refuses cleanly (501). Every route
re-reads the printer first: an unreachable printer is an explicit 502
failure and a printer in the wrong state is a 409; both send nothing.

Each function returns ``(http_code, body)`` in the Web UI's ``{ok,
reason}`` shape. ``read`` and ``send`` are injectable for tests, which
use fake adapters only.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .. import printers, settings
from . import status as status_module

ACTIONS = ("pause", "resume", "cancel")
HEATERS = ("nozzle", "bed")
# The family control vocabulary (FamilyControl): cancel is "stop".
_FAMILY_ACTION = {"pause": "pause", "resume": "resume", "cancel": "stop"}
# Which states each action may start from; anything else sends nothing.
_ALLOWED_FROM = {"pause": ("printing",), "resume": ("paused",),
                 "cancel": ("printing", "paused")}
# The state each action leaves the printer in. When Moonraker rejects a
# command (HTTP 4xx) the printer is re-read: already there is success
# (someone did it from the screen first), anything else is a 409.
_REACHED = {"pause": lambda s: s == "paused", "resume": lambda s: s == "printing",
            "cancel": lambda s: s not in ("printing", "paused", "unknown")}
_SEND_TIMEOUT_S = 15

OFF_REASON = ("printer controls are off: set [webui] controls_enabled = true "
              "to allow them. Nothing sent.")


class ControlError(Exception):
    """A refusal with its HTTP code; nothing was sent to the printer."""

    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def controls_enabled(data) -> bool:
    return bool((data.get("webui") or {}).get("controls_enabled", False))


def _connection(adapter):
    return getattr(adapter, "connection", None) or getattr(adapter, "transport", None)


def _moonraker_post(view, path, payload=None):
    """One POST to this printer's Moonraker (print_control._call's shape)."""
    base = (view.get("services", {}).get("moonraker_url") or "").rstrip("/")
    if not base:
        raise ValueError("no Moonraker URL is configured for this printer")
    body = json.dumps(payload).encode() if payload is not None else b""
    request = urllib.request.Request(base + path, method="POST", data=body,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=_SEND_TIMEOUT_S) as response:
        return json.loads(response.read() or b"{}")


def default_send(adapter, view, command):
    """Send one command: ("control", action) or ("temperature", heater, target)."""
    connection = _connection(adapter)
    if command[0] == "control":
        action = command[1]
        if connection == "moonraker":
            return _moonraker_post(view, f"/printer/print/{action}")
        if connection == "offline" or not callable(getattr(adapter, "control", None)):
            raise ControlError(501, f"print controls are not supported for "
                                    f"{connection or 'this'} printers. Nothing sent.")
        return adapter.control(_FAMILY_ACTION[action])
    _, heater, target = command
    if connection == "moonraker":
        word = "M104" if heater == "nozzle" else "M140"
        return _moonraker_post(view, "/printer/gcode/script",
                               {"script": f"{word} S{target:g}"})
    setter = getattr(adapter, "set_target_temperature", None)
    if not callable(setter):
        raise ControlError(501, f"setting temperatures is not supported for "
                                f"{connection or 'this'} printers. Nothing sent.")
    return setter(heater, target)


def _resolve(data, printer_id):
    if not controls_enabled(data):
        raise ControlError(403, OFF_REASON)
    try:
        entry = settings.by_id(printer_id, data)
    except settings.SettingsError:
        raise ControlError(404, f"no printer with id {printer_id!r} is configured") from None
    view = settings.view_for(printer_id, data)
    return entry, view


def _live_state(adapter, view, read):
    try:
        live = read(adapter, view) or {}
    except ControlError:
        raise
    except Exception as exc:  # unreachable is an explicit failure, never a guess
        raise ControlError(502, f"printer unreachable ({type(exc).__name__}: {exc}). "
                                "Nothing sent.") from None
    return str(live.get("state") or "unknown")


def _fact(adapter, name):
    fact = getattr(adapter, "fact", None)
    if not callable(fact):
        return None
    try:
        value = fact(name)
    except Exception:  # a model without that fact has no such limit
        return None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def temperature_limit(adapter, view, heater):
    """The highest target this printer accepts for ``heater`` (°C)."""
    if heater == "nozzle":
        from ..core import nozzle
        nozzle_type = (view.get("printer") or {}).get("nozzle_type") or "brass"
        if str(nozzle_type).strip().lower() == "stock":
            nozzle_type = "brass"  # the unconfirmed factory nozzle: the softest option
        limits = [nozzle._nozzle_type_defaults()[nozzle.normalize_nozzle_type(nozzle_type)]
                  ["max_temperature_c"]]
        hotend = _fact(adapter, "max_hotend_c")
        if hotend is not None:
            limits.append(hotend)
        return min(limits)
    bed = _fact(adapter, "max_bed_c")
    if bed is not None:
        return bed
    # No model fact: the hottest bed any configured material asks for.
    temps = {**settings.DEFAULT_MATERIAL_TEMPS, **(view.get("material_temps") or {})}
    return float(max(row["bed"] for row in temps.values()
                     if isinstance(row, dict) and isinstance(row.get("bed"), (int, float))))


def control(data, printer_id, action, body=None, *, adapter=None, read=None, send=None):
    """(code, body) for POST /api/printers/<id>/pause|resume|cancel."""
    read = status_module.read_live_status if read is None else read
    send = default_send if send is None else send
    try:
        if action not in ACTIONS:
            raise ControlError(404, "try /api/printers/<id>/pause, resume, cancel, "
                                    "temperature or target")
        entry, view = _resolve(data, printer_id)
        if action == "cancel" and not (isinstance(body, dict) and body.get("confirm") is True):
            raise ControlError(400, 'cancel needs the JSON body {"confirm": true}. Nothing sent.')
        adapter = adapter or printers.get(entry["model"], view)
        state = _live_state(adapter, view, read)
        if state not in _ALLOWED_FROM[action]:
            raise ControlError(409, f"cannot {action}: printer is {state}. Nothing sent.")
        detail = f"{action} sent; check the printer's status for the result."
        try:
            send(adapter, view, ("control", action))
        except ControlError:
            raise
        except urllib.error.HTTPError as exc:
            if not 400 <= exc.code < 500:
                raise ControlError(502, f"the printer answered {action} with HTTP {exc.code}") from None
            # The printer was reached and refused. A refusal can be a race
            # (the state changed between the read above and the send), so
            # re-read and only call it success if the printer is already
            # where the action would have put it. Never resent.
            try:
                now = _live_state(adapter, view, read)
            except ControlError:
                now = "unknown"
            if not _REACHED[action](now):
                raise ControlError(409, f"the printer refused {action} (HTTP {exc.code}); "
                                        f"it now reports {now}. Not retried.") from None
            detail = (f"the printer refused {action} (HTTP {exc.code}) but already "
                      f"reports {now}, so nothing more was sent.")
        except Exception as exc:
            raise ControlError(502, f"{action} failed to reach the printer "
                                    f"({type(exc).__name__}: {exc})") from None
    except ControlError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}
    return 200, {"ok": True, "id": entry["id"], "action": action, "from_state": state,
                 "detail": detail}


def set_temperature(data, printer_id, body=None, *, adapter=None, read=None, send=None):
    """(code, body) for POST /api/printers/<id>/temperature.

    Body: ``{"heater": "nozzle" | "bed", "target": <°C>}``; 0 turns the
    heater off.
    """
    read = status_module.read_live_status if read is None else read
    send = default_send if send is None else send
    try:
        entry, view = _resolve(data, printer_id)
        body = body if isinstance(body, dict) else {}
        heater, target = body.get("heater"), body.get("target")
        if heater not in HEATERS:
            raise ControlError(400, 'heater must be "nozzle" or "bed". Nothing sent.')
        if isinstance(target, bool) or not isinstance(target, (int, float)) or target != target:
            raise ControlError(400, "target must be a number of °C. Nothing sent.")
        adapter = adapter or printers.get(entry["model"], view)
        limit = temperature_limit(adapter, view, heater)
        if not 0 <= target <= limit:
            raise ControlError(400, f"{heater} target {target:g}°C is outside 0..{limit:g}°C "
                                    "for this printer. Nothing sent.")
        _live_state(adapter, view, read)
        try:
            send(adapter, view, ("temperature", heater, float(target)))
        except ControlError:
            raise
        except Exception as exc:
            raise ControlError(502, f"temperature change failed to reach the printer "
                                    f"({type(exc).__name__}: {exc})") from None
    except ControlError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}
    return 200, {"ok": True, "id": entry["id"], "heater": heater, "target": float(target),
                 "limit": limit, "detail": "target sent; check the printer's status for the result."}


# The Home Assistant integration (ha-print-handy 0.3.0) body: both heaters
# at once, null for "leave this heater alone".
TARGET_KEYS = (("hotend_c", "nozzle"), ("bed_c", "bed"))


def set_targets(data, printer_id, body=None, *, adapter=None, read=None, send=None):
    """(code, body) for POST /api/printers/<id>/target.

    Body: ``{"hotend_c": <°C> | null, "bed_c": <°C> | null}``, the shape
    ha-print-handy sends. ``null`` (or a missing key) leaves that heater
    alone; at least one must be a number. Same guard and limits as
    ``/temperature``, and every value is checked before anything is sent,
    so one bad value sends nothing at all.
    """
    read = status_module.read_live_status if read is None else read
    send = default_send if send is None else send
    sent = []
    try:
        entry, view = _resolve(data, printer_id)
        body = body if isinstance(body, dict) else {}
        wanted = []
        for key, heater in TARGET_KEYS:
            target = body.get(key)
            if target is None:
                continue
            if isinstance(target, bool) or not isinstance(target, (int, float)) or target != target:
                raise ControlError(400, f"{key} must be a number of °C or null. Nothing sent.")
            wanted.append((key, heater, float(target)))
        if not wanted:
            raise ControlError(400, 'give hotend_c and/or bed_c as a number of °C. '
                                    "Nothing sent.")
        adapter = adapter or printers.get(entry["model"], view)
        limits = {}
        for key, heater, target in wanted:
            limits[key] = temperature_limit(adapter, view, heater)
            if not 0 <= target <= limits[key]:
                raise ControlError(400, f"{key} {target:g}°C is outside 0..{limits[key]:g}°C "
                                        "for this printer. Nothing sent.")
        _live_state(adapter, view, read)
        for key, heater, target in wanted:
            try:
                send(adapter, view, ("temperature", heater, target))
            except ControlError as refusal:
                if sent:
                    raise ControlError(refusal.code, f"{refusal.reason} (already sent: "
                                                     f"{', '.join(sent)})") from None
                raise
            except Exception as exc:
                done = f"; already sent: {', '.join(sent)}" if sent else ""
                raise ControlError(502, f"{key} change failed to reach the printer "
                                        f"({type(exc).__name__}: {exc}){done}") from None
            sent.append(f"{key}={target:g}")
    except ControlError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}
    return 200, {"ok": True, "id": entry["id"],
                 "targets": {key: target for key, _, target in wanted},
                 "limits": limits, "detail": "targets sent; check the printer's status for the result."}
