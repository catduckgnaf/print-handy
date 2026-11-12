"""Web UI printer control endpoints (#708, control endpoints phase).

``POST /api/printers/<id>/pause|resume|cancel`` and
``POST /api/printers/<id>/temperature`` send one command to one roster
printer through the same paths chat uses:

- Moonraker printers: Moonraker's ``/printer/print/pause|resume|cancel``
  (``print_control`` / ``print_stop``) and ``M104`` / ``M140`` through
  ``/printer/gcode/script`` (the ``nozzle_change`` heat path);
- every other family: the adapter's own ``control()`` operation
  (``family_control.FamilyControl``), with cancel spelled ``stop``.

The ha-print-handy #1 routes add, under the same guard:

- ``POST /api/printers/<id>/emergency_stop``, ``/firmware_restart`` and
  ``/server_restart`` — Moonraker's ``/printer/emergency_stop``,
  ``/printer/firmware_restart`` and ``/server/restart`` (the endpoints
  the chat panic ladder in ``hermes/panic.py`` uses). Each needs
  ``{"confirm": true}``. Emergency stop has NO state gate and no status
  read at all: it is the panic button, and a hung controller is when it
  is needed, so it sends at once and falls back to ``M112`` through the
  script endpoint when the emergency-stop call itself fails.
  No other family exposes a verified stop or restart capability in its
  adapter or client code, so every other family refuses with a named
  501 — no behavior is invented for them. This deliberately reverses
  the #708 presumption that emergency stop stays chat-side only, at
  catduck's direction for ha-print-handy #1.
- ``POST /api/printers/<id>/gcode`` — raw G-code from the JSON body
  ``{"script": "..."}`` (also a #708 scope reversal, same direction).
  Moonraker sends it through ``/printer/gcode/script``; OctoPrint
  sends one command per non-empty line through ``/api/printer/command``
  (the endpoint its client already uses for temperature commands);
  every other family refuses with a named 501. The script is refused
  (400) when it is missing, not a string, empty, or longer than
  ``GCODE_MAX_CHARS`` characters. A script's own temperature commands
  (``M104``/``M109``/``M140``/``M190``, Marlin's ``M303`` autotune and
  Klipper's ``SET_HEATER_TEMPERATURE`` / ``PID_CALIBRATE``)
  are held to the same fitted-nozzle/model ceiling as the temperature
  routes and refused by name above it, and the script is state-gated
  like the module's other mutating routes: a printer reporting a state
  that cannot accept commands (unknown, error, shutdown, disconnected,
  offline, unavailable) refuses 409 and receives nothing.
- ``POST /api/printers/<id>/exclude`` — exclude one named object from
  the running print, Moonraker only (exclude-object is a Klipper
  feature; other families refuse 501). The body is
  ``{"object": "<name>", "confirm": true}`` and the decision is built
  by ``core.exclude_object.exclude_plan`` from the live status's
  objects minus its excluded objects: a confirmed exclude sends the
  plan's ``EXCLUDE_OBJECT NAME=<target>`` script, the last remaining
  object answers 409 and sends nothing (cancel the print instead), an
  unknown or already-excluded name refuses 400, and a missing confirm
  answers 400 naming the ``{"confirm": true}`` requirement.

Guard (the issue requires one): controls are OFF by default. Only
``[webui] controls_enabled = true`` turns them on; while off every
control route answers 403 with the reason and sends nothing. ``cancel``
additionally needs the JSON body ``{"confirm": true}``. A target
temperature is checked against the printer's own limits (its
``max_hotend_c`` / ``max_bed_c`` facts, the fitted nozzle type's safe
maximum) and refused above them, never clamped — on the temperature
routes and inside raw G-code scripts alike. A family with no
existing temperature command path refuses cleanly (501). Every route
re-reads the printer first: an unreachable printer is an explicit 502
failure and a printer in the wrong state is a 409; both send nothing.
A Moonraker pause, resume or cancel whose reply times out after the
request went out is read back (print_stop.read_back) for up to 90 s:
reaching the action's state is success, otherwise a 504 naming the state.

Each function returns ``(http_code, body)`` in the Web UI's ``{ok,
reason}`` shape. ``read`` and ``send`` are injectable for tests, which
use fake adapters only.
"""
from __future__ import annotations

import json
import re
import shlex
import urllib.error
import urllib.request

from .. import printer_control, printers, settings
from ..core import exclude_object as _exclude_object
from ..core import heater_ceiling as _heater_ceiling
from . import status as status_module

ACTIONS = ("pause", "resume", "cancel")
# ha-print-handy #1: the power routes and their Moonraker endpoints
# (the chat panic ladder's, in hermes/panic.py and print_alert_buttons).
POWER_ACTIONS = {"emergency_stop": "/printer/emergency_stop",
                 "firmware_restart": "/printer/firmware_restart",
                 "server_restart": "/server/restart"}
# The send-G-code size cap: a script is a snippet (a macro call, a few
# moves), not a print file. 2048 characters also keeps the JSON body
# under the control route's 4096-byte cap in server.py.
GCODE_MAX_CHARS = 2048
HEATERS = ("nozzle", "bed")
# The ceiling for a heater no model fact describes (a chamber on a model
# with no max_chamber_c, or a Klipper heater_generic named in a raw
# script) is core.heater_ceiling's; re-exported for callers of this module.
UNKNOWN_HEATER_CEILING_C = _heater_ceiling.UNKNOWN_HEATER_CEILING_C
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
# What a Moonraker control send returns when the request went out but the reply
# timed out (print_stop.post_print_command): Moonraker holds that reply until the
# printer's PAUSE/RESUME/CANCEL macro finishes, so the state read-back decides.
NO_REPLY = object()
_READBACK_POLL_S = 2.0

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
    """Send one command.

    Shapes: ``("control", action)``, ``("temperature", heater, target)``,
    ``("power", action)`` for the ha-print-handy #1 power routes, and
    ``("gcode", script)`` for raw G-code (which the exclude route also
    uses for its EXCLUDE_OBJECT script, on Moonraker only).
    """
    connection = _connection(adapter)
    if command[0] == "control":
        action = command[1]
        if connection == "moonraker":
            from ..runtime import print_stop
            replied = print_stop.post_print_command(
                lambda path, **_kw: _moonraker_post(view, path), f"/printer/print/{action}")
            return None if replied else NO_REPLY
        if connection == "offline" or not callable(getattr(adapter, "control", None)):
            raise ControlError(501, f"print controls are not supported for "
                                    f"{connection or 'this'} printers. Nothing sent.")
        return adapter.control(_FAMILY_ACTION[action])
    if command[0] == "power":
        action = command[1]
        if connection == "moonraker":
            return _moonraker_post(view, POWER_ACTIONS[action])
        raise ControlError(501, f"{action} is not supported for "
                                f"{connection or 'this'} printers: only Moonraker printers "
                                "expose it in Print Handy. Nothing sent.")
    if command[0] == "gcode":
        script = command[1]
        if connection == "moonraker":
            return _moonraker_post(view, "/printer/gcode/script", {"script": script})
        if connection == "octoprint":
            sender = getattr(adapter, "send_gcode", None)
            if not callable(sender):
                raise ControlError(501, "send-G-code is not supported for OctoPrint "
                                        "printers in this build. Nothing sent.")
            lines = [line.strip() for line in str(script).splitlines() if line.strip()]
            return sender(lines)
        raise ControlError(501, f"send-G-code is not supported for "
                                f"{connection or 'this'} printers. Nothing sent.")
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


def _hardware_record(printer_id):
    """The addressed printer's hardware record (``/nozzle change``,
    ``printer_state set``), or None when there is none to read."""
    if printer_id is None:
        return None
    try:
        from ..core import printer_state
        record = printer_state.load(printer_id)
    except Exception:  # no readable hardware record: configuration is all there is
        return None
    return record if isinstance(record, dict) else None


def temperature_limit(adapter, view, heater, printer_id=None):
    """The highest target this printer accepts for ``heater`` (°C).

    The temperature routes and the raw G-code scan both ask here, and
    this asks ``core.heater_ceiling.ceiling`` (#1241), the one ceiling
    print-now's G-code check, the panic purge, the OctoPrint preheat and
    macro arguments use: the fitted nozzle is ``printer_id``'s hardware
    record when it names one, else the configured type, and a ``stock``
    nozzle resolves per model exactly as it does there. So every surface
    refuses the same values for the same printer.

    The firmware's own ``max_temp`` remains the real limit; this is the
    ceiling the Web UI refuses to send above.
    """
    return printer_control.for_printer(printer_id, adapter=adapter, view=view).ceiling(
        heater, state=_hardware_record(printer_id))


def control(data, printer_id, action, body=None, *, adapter=None, read=None, send=None):
    """(code, body) for POST /api/printers/<id>/pause|resume|cancel."""
    read = status_module.read_live_status if read is None else read
    send = default_send if send is None else send
    try:
        if action not in ACTIONS:
            raise ControlError(404, "try /api/printers/<id>/pause, resume, cancel, "
                                    "temperature, target, emergency_stop, firmware_restart, "
                                    "server_restart, gcode or exclude")
        entry, view = _resolve(data, printer_id)
        if action == "cancel" and not (isinstance(body, dict) and body.get("confirm") is True):
            raise ControlError(400, 'cancel needs the JSON body {"confirm": true}. Nothing sent.')
        adapter = adapter or printers.get(entry["model"], view)
        state = _live_state(adapter, view, read)
        if state not in _ALLOWED_FROM[action]:
            raise ControlError(409, f"cannot {action}: printer is {state}. Nothing sent.")
        detail = f"{action} sent; check the printer's status for the result."
        service = printer_control.for_printer(printer_id, adapter=adapter, view=view)
        try:
            if service.send(("control", action), sender=send) is NO_REPLY:
                detail = _read_back_after_no_reply(adapter, view, read, action)
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


def _read_back_after_no_reply(adapter, view, read, action):
    """The detail for a control whose reply timed out after it was sent: the printer's own answer.

    Re-reads for up to print_stop.NO_REPLY_WAIT_S; success when the printer reaches the
    action's state, a 504 naming the state it still reports otherwise. Never resent.
    """
    from ..runtime import print_stop
    now = print_stop.read_back(lambda: _live_state(adapter, view, read), _REACHED[action],
                               print_stop.NO_REPLY_WAIT_S, _READBACK_POLL_S)
    if now is not None and _REACHED[action](now):
        return f"{action} sent; the printer was slow to answer and now reports {now}."
    raise ControlError(504, f"{action} sent, but the printer has not answered and still reports "
                            f"{now or 'unknown'} after {print_stop.NO_REPLY_WAIT_S:.0f}s. "
                            "Check the printer. Not retried.")


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
        limit = temperature_limit(adapter, view, heater, printer_id)
        if not 0 <= target <= limit:
            raise ControlError(400, f"{heater} target {target:g}°C is outside 0..{limit:g}°C "
                                    "for this printer. Nothing sent.")
        _live_state(adapter, view, read)
        service = printer_control.for_printer(printer_id, adapter=adapter, view=view)
        try:
            service.send(("temperature", heater, float(target)), sender=send)
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
            limits[key] = temperature_limit(adapter, view, heater, printer_id)
            if not 0 <= target <= limits[key]:
                raise ControlError(400, f"{key} {target:g}°C is outside 0..{limits[key]:g}°C "
                                        "for this printer. Nothing sent.")
        _live_state(adapter, view, read)
        service = printer_control.for_printer(printer_id, adapter=adapter, view=view)
        for key, heater, target in wanted:
            try:
                service.send(("temperature", heater, target), sender=send)
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


# -- ha-print-handy #1: power, send-G-code and exclude-object routes -------


def _require_confirm(body, label):
    if not (isinstance(body, dict) and body.get("confirm") is True):
        raise ControlError(400, f'{label} needs the JSON body {{"confirm": true}}. Nothing sent.')


def _live_status(adapter, view, read):
    """The full live status dict; an unreadable printer is an explicit 502."""
    try:
        live = read(adapter, view) or {}
    except ControlError:
        raise
    except Exception as exc:  # unreachable is an explicit failure, never a guess
        raise ControlError(502, f"printer unreachable ({type(exc).__name__}: {exc}). "
                                "Nothing sent.") from None
    return live if isinstance(live, dict) else {}


def _dispatch(send, service, command, label):
    """Send one command through the control service; a failure to reach the printer is a 502."""
    try:
        service.send(command, sender=send)
    except ControlError:
        raise
    except Exception as exc:
        raise ControlError(502, f"{label} failed to reach the printer "
                                f"({type(exc).__name__}: {exc})") from None


def _emergency_stop(entry, service, send):
    """Send the emergency stop without waiting on any status read.

    A hung or unreachable controller is exactly when the stop is
    needed, so nothing is read first: the stop goes out at once. When
    the primary endpoint call fails (the transport, not a family that
    has no stop at all), the firmware's own G-code emergency command
    ``M112`` is tried through the script endpoint. The answer says
    truthfully what was sent: 200 only when one of the two was accepted,
    otherwise a 502 naming both failures and telling the operator to cut
    power."""
    try:
        service.send(("power", "emergency_stop"), sender=send)
    except ControlError:
        raise  # 501: this family has no stop path; nothing was sent
    except Exception as primary:
        first = f"{type(primary).__name__}: {primary}"
        try:
            service.send(("gcode", "M112"), sender=send)
        except Exception as fallback:
            second = (fallback.reason if isinstance(fallback, ControlError)
                      else f"{type(fallback).__name__}: {fallback}")
            raise ControlError(502, f"emergency stop could not be delivered: the emergency-stop "
                                    f"endpoint failed ({first}) and the M112 fallback failed "
                                    f"({second}). The printer may still be running: cut its "
                                    "power.") from None
        return 200, {"ok": True, "id": entry["id"], "action": "emergency_stop",
                     "from_state": "unknown", "sent": "M112",
                     "detail": f"the emergency-stop endpoint failed ({first}), so M112 was "
                               "sent through the G-code endpoint instead; check the "
                               "printer's status for the result."}
    return 200, {"ok": True, "id": entry["id"], "action": "emergency_stop",
                 "from_state": "unknown", "sent": "emergency_stop",
                 "detail": "emergency_stop sent without waiting for a status read; "
                           "check the printer's status for the result."}


def power_action(data, printer_id, action, body=None, *, adapter=None, read=None, send=None):
    """(code, body) for POST /api/printers/<id>/emergency_stop|firmware_restart|server_restart.

    All three need ``{"confirm": true}``. There is deliberately no state
    gate. Emergency stop is not even read first (see _emergency_stop):
    it sends at once, with an ``M112`` fallback. Firmware restart reads
    the printer first, so an unreachable printer is an explicit 502
    rather than a blind send — and ``server_restart``, whose read is best-effort: Moonraker
    rejects the status query while Klippy is disconnected, and server
    restart is the recovery control for exactly that failure, so the
    restart POST itself decides reachability there.
    Moonraker only; every other family refuses 501 from ``default_send``.
    """
    read = status_module.read_live_status if read is None else read
    send = default_send if send is None else send
    try:
        if action not in POWER_ACTIONS:
            raise ControlError(404, "try /api/printers/<id>/emergency_stop, "
                                    "firmware_restart or server_restart")
        entry, view = _resolve(data, printer_id)
        _require_confirm(body, action)
        adapter = adapter or printers.get(entry["model"], view)
        service = printer_control.for_printer(printer_id, adapter=adapter, view=view)
        if action == "emergency_stop":
            return _emergency_stop(entry, service, send)
        if action == "server_restart":
            # Best-effort read only: Moonraker rejects the objects query
            # while Klippy is disconnected, but /server/restart does not
            # depend on Klippy — it is the recovery control for exactly
            # that failure. The restart POST itself decides whether
            # Moonraker is reachable (a failed send is a 502 below).
            try:
                state = str(_live_status(adapter, view, read).get("state") or "unknown")
            except ControlError:
                state = "unknown"
        else:
            state = str(_live_status(adapter, view, read).get("state") or "unknown")
        _dispatch(send, service, ("power", action), action)
    except ControlError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}
    return 200, {"ok": True, "id": entry["id"], "action": action, "from_state": state,
                 "detail": f"{action} sent; check the printer's status for the result."}


# The traditional temperature words a raw script can carry: M-code ->
# (heater, the parameters that carry a temperature). M104/M109 set the
# nozzle (S; R on the wait variant; B is Marlin's AUTOTEMP maximum),
# M140/M190 the bed, M141/M191 the chamber, and M303 (Marlin PID
# autotune) heats to its S target: the nozzle by default, the bed when
# E is -1 and the chamber when E is -2.
_HEATER_WORDS = {104: ("nozzle", "SRB"), 109: ("nozzle", "SRB"),
                 140: ("bed", "SR"), 190: ("bed", "SR"),
                 141: ("chamber", "SR"), 191: ("chamber", "SR"),
                 303: ("nozzle", "S")}
# A number as the loosest real parser could read it: an optional sign,
# digits with an optional fraction (or a leading-dot fraction), and an
# optional exponent. The scan must read the whole token — stopping
# early, at an exponent or a leading dot, would check a different
# number than the one a parser accepting those forms acts on: ``S5e2``
# is 500 °C to such a parser, not 5 °C.
_HEATER_NUMBER = r"[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?"
# One letter and its value with any spaces or tabs between them, on an
# uppercased line: Klipper splits a traditional line into letter/value
# pairs and strips each value, and Marlin skips spaces, so ``S 300``,
# ``S<tab>300`` and ``M 104`` are what the firmware runs.
_PAIR_RE = re.compile(r"([A-Z])[ \t]*(" + _HEATER_NUMBER + r")")
_COMMAND_RE = re.compile(r"^([A-Z])[ \t]*([-+]?[0-9]+(?:\.[0-9]*)?)")
_LINE_NUMBER_RE = re.compile(r"^[Nn][ \t]*[0-9]+[ \t]*")
# Klipper's extended-command shape: a word of two or more characters
# that is not a letter followed by a number.
_EXTENDED_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]+)(?:[ \t]+|$)(.*)$")
_PLAIN_WORD_RE = re.compile(r"^[A-Za-z][0-9]+$")
_HEATER_COMMANDS = ("SET_HEATER_TEMPERATURE", "PID_CALIBRATE")
_MACRO_PARAM_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*(\"[^\"]*\"|'[^']*'|\S+)")
# Parameter-name parts that mark a temperature on a macro the scan does
# not know (PRINT_START EXTRUDER=250 BED=110, a TEMP= on a filament
# macro): BED and CHAMBER name those heaters, EXTRUDER* and these the
# hotend. Only a numeric value counts (EXTRUDER=extruder1 is a name).
_MACRO_NOZZLE_PARTS = frozenset({"HOTEND", "NOZZLE", "TEMP", "TEMPERATURE", "TOOL"})
# States in which a printer cannot accept commands at all, so a raw
# script is refused 409 like the state-gated control actions instead of
# being sent blind. Any positively known state (idle, printing, paused,
# operational, complete, ...) passes: mid-print scripts are this
# route's legitimate use, so the gate refuses only the states that
# name a printer in no position to act.
_GCODE_REFUSED_STATES = frozenset(
    {"unknown", "error", "shutdown", "disconnected", "offline", "unavailable"})


def _number(text):
    """A finite float from a parameter value (quotes allowed), or None."""
    try:
        value = float(str(text).strip().strip("\"'"))
    except ValueError:
        return None
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def _named_heater(name):
    """(kind, label) for a Klipper heater name: ``extruder*`` is the
    nozzle, ``heater_bed`` the bed, a name containing ``chamber`` the
    chamber, and any other named heater an ``other`` heater held to the
    conservative ceiling (see temperature_limit)."""
    name = name.strip().lower()
    if name.startswith("extruder"):
        return "nozzle", "nozzle"
    if name == "heater_bed":
        return "bed", "bed"
    if "chamber" in name:
        return "chamber", "chamber"
    return "other", f"heater {name}"


def _macro_param_heater(name):
    """The heater a macro parameter name points at, or None."""
    parts = name.upper().split("_")
    if "BED" in parts:
        return "bed"
    if "CHAMBER" in parts:
        return "chamber"
    if any(part.startswith("EXTRUDER") for part in parts) or \
            _MACRO_NOZZLE_PARTS.intersection(parts):
        return "nozzle"
    return None


def _extended_params(word, rest, *, strict):
    """The KEY=VALUE parameters of one extended line, last value winning.

    Klipper tokenizes extended commands with ``shlex`` and builds a
    parameter dict, so this does the same: quoted values parse as
    Klipper parses them, and a repeated key's last value is the one that
    wins. ``strict`` (the heater commands) refuses a line that cannot be
    parsed that way — unbalanced quotes, a token without ``=`` — with a
    400 (fail closed); for any other command a parse failure falls back
    to a plain KEY=VALUE scan, so an odd line never hides a value."""
    try:
        tokens = shlex.split(rest)
    except ValueError:
        tokens = None
    params = {}
    if tokens is not None:
        for token in tokens:
            key, sep, value = token.partition("=")
            if not sep or not key.strip() or not value.strip():
                tokens = None
                break
            params[key.strip().upper()] = value.strip()
    if tokens is None:
        if strict:
            raise ControlError(400, f"{word} line could not be parsed for its heater "
                                    "and target. Nothing sent.")
        params = {key.upper(): value for key, value in _MACRO_PARAM_RE.findall(rest)}
    return params


def _extended_heater_target(word, rest):
    """(word, kind, label, target_c) for a SET_HEATER_TEMPERATURE or
    PID_CALIBRATE line. A line that cannot be parsed, a missing HEATER
    or TARGET, or a non-numeric TARGET refuses with a 400 instead of
    being sent unchecked. Every named heater is checked: a heater this
    module has no model fact for gets the conservative ceiling."""
    params = _extended_params(word, rest, strict=True)
    if "HEATER" not in params or "TARGET" not in params:
        raise ControlError(400, f"{word} line could not be parsed for its heater "
                                "and target. Nothing sent.")
    target = _number(params["TARGET"])
    if target is None:
        raise ControlError(400, f"{word} target {params['TARGET']!r} is not a number "
                                "of °C. Nothing sent.")
    kind, label = _named_heater(params["HEATER"])
    return word, kind, label, target


def _traditional_targets(line):
    """[(word, kind, label, target_c, above_only)] for one uppercased
    traditional line.

    The command is matched by its number however it is spelled
    (``M104``, ``M 104``, ``M0104``). Every value of every temperature
    parameter is checked, not only the one a firmware keeps (Klipper and
    Marlin keep the last), so a repeated parameter cannot hide an
    over-limit value. The whole line is scanned, past a ``*`` too:
    Klipper reads ``*`` as one more parameter letter and keeps parsing
    after it, so ``M104 *S400`` sets 400 °C."""
    command = _COMMAND_RE.match(line)
    if not command or command.group(1) != "M":
        return []
    try:
        code = int(float(command.group(2)))
    except ValueError:
        return []
    if code not in _HEATER_WORDS:
        return []
    kind, letters = _HEATER_WORDS[code]
    word = f"M{code}"
    pairs = _PAIR_RE.findall(line[command.end():])
    if code == 303:
        selectors = [_number(value) for letter, value in pairs if letter == "E"]
        selector = selectors[-1] if selectors else None
        kind = "bed" if selector == -1 else "chamber" if selector == -2 else "nozzle"
    found = []
    for letter, value in pairs:
        target = _number(value) if letter in letters else None
        if target is not None:
            found.append((word, kind, kind, target, False))
    return found


def _script_heater_targets(script):
    """[(word, kind, label, target_c, above_only)]: every temperature a
    raw script sets.

    Line by line, the way firmware reads it: a ``;`` comment is stripped
    first (a heater word inside a comment sets nothing) and an optional
    line number is dropped. Then:

    - a traditional command is uppercased (both firmwares are
      case-insensitive) and read as letter/value pairs with any spaces
      or tabs between letter and value (_traditional_targets): M104/M109
      (S, R, B), M140/M190 (S, R), the chamber's M141/M191 (S, R), and
      M303's S target on the heater its E selects;
    - ``SET_HEATER_TEMPERATURE`` and ``PID_CALIBRATE`` are checked
      against the named heater's own limit; a malformed line raises
      ``ControlError`` (400);
    - any other extended command (a macro such as ``PRINT_START``) has
      each numeric parameter whose name marks a temperature (``BED``,
      ``EXTRUDER``, ``TEMP``, ``CHAMBER`` and the like, see
      _macro_param_heater) held to that heater's ceiling. These are
      ``above_only``: a negative macro value is an offset, not a target.

    Klipper ends an extended command's arguments at ``#``, ``*`` or
    ``;``, and so does this scan. Targets are read whole — sign, fraction
    and exponent. This is a pre-send screen, not the safety limit: the
    firmware's own ``max_temp`` stays the real ceiling, and a macro can
    still heat by means no scan of its call can see.
    """
    found = []
    for raw_line in str(script).splitlines():
        line = _LINE_NUMBER_RE.sub("", raw_line.split(";", 1)[0].strip()).strip()
        if not line:
            continue
        if _COMMAND_RE.match(line.upper()):
            found.extend(_traditional_targets(line.upper()))
        # A first word that is more than a letter and a number is also
        # read as an extended command: Klipper runs a macro named like
        # S3D_START even though its line starts with a letter and digit.
        extended = _EXTENDED_RE.match(line)
        if not extended or _PLAIN_WORD_RE.match(extended.group(1)):
            continue
        word = extended.group(1).upper()
        rest = re.split(r"[#*]", extended.group(2), maxsplit=1)[0]
        if word in _HEATER_COMMANDS:
            found.append((*_extended_heater_target(word, rest), False))
            continue
        for name, value in _extended_params(word, rest, strict=False).items():
            kind = _macro_param_heater(name)
            target = _number(value) if kind else None
            if target is not None:
                found.append((f"{word} {name}", kind, kind, target, True))
    return found


def send_gcode(data, printer_id, body=None, *, adapter=None, read=None, send=None):
    """(code, body) for POST /api/printers/<id>/gcode.

    Body: ``{"script": "<G-code text>"}``. A missing, non-string, empty
    or oversized script (over ``GCODE_MAX_CHARS`` characters) refuses
    400 and sends nothing. Temperature commands inside the script are
    held to the same ceiling as the temperature routes (see
    ``_script_heater_targets``): a target outside ``0..<limit>`` for
    this printer refuses 400 naming the word, the heater and the
    limit, and nothing is sent — the whole script, never a trimmed
    version of it. The printer's state gates the send like the other
    mutating routes: a state in ``_GCODE_REFUSED_STATES`` refuses 409.
    There is deliberately no ``{"confirm": true}`` requirement: the
    temperature routes perform the same physical action without one,
    so a script-only confirm would add friction without protection —
    the ceiling is the guard. Moonraker receives the script verbatim
    through ``/printer/gcode/script``; OctoPrint receives one command
    per non-empty line through ``/api/printer/command``; every other
    family refuses 501 from ``default_send``.
    """
    read = status_module.read_live_status if read is None else read
    send = default_send if send is None else send
    try:
        entry, view = _resolve(data, printer_id)
        script = body.get("script") if isinstance(body, dict) else None
        if not isinstance(script, str):
            raise ControlError(400, "script must be a string of G-code. Nothing sent.")
        if not script.strip():
            raise ControlError(400, "script is empty. Nothing sent.")
        if len(script) > GCODE_MAX_CHARS:
            raise ControlError(400, f"script is {len(script)} characters; the limit is "
                                    f"{GCODE_MAX_CHARS}. Nothing sent.")
        adapter = adapter or printers.get(entry["model"], view)
        limits = {}
        for word, kind, label, target, above_only in _script_heater_targets(script):
            if kind not in limits:
                limits[kind] = temperature_limit(adapter, view, kind, printer_id)
            limit = limits[kind]
            if target > limit or (target < 0 and not above_only):
                raise ControlError(400, f"{word} sets the {label} to {target:g}°C, "
                                        f"outside 0..{limit:g}°C for this printer. "
                                        "Nothing sent.")
        live = _live_status(adapter, view, read)
        state = str(live.get("state") or "unknown")
        if state.strip().lower() in _GCODE_REFUSED_STATES:
            raise ControlError(409, f"cannot send G-code: printer is {state}. "
                                    "Nothing sent.")
        _dispatch(send, printer_control.for_printer(printer_id, adapter=adapter, view=view),
                  ("gcode", script), "send-G-code")
    except ControlError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}
    return 200, {"ok": True, "id": entry["id"], "action": "gcode", "from_state": state,
                 "detail": "G-code sent; check the printer's status for the result."}


def exclude_object(data, printer_id, body=None, *, adapter=None, read=None, send=None):
    """(code, body) for POST /api/printers/<id>/exclude.

    Body: ``{"object": "<name>", "confirm": true}``. Moonraker only —
    exclude-object is a Klipper feature, so any other family refuses
    501 before the printer is read. The plan comes from
    ``core.exclude_object.exclude_plan`` over the live status's objects
    minus its excluded objects: ``exclude`` sends the plan's
    EXCLUDE_OBJECT script through the Moonraker G-code path,
    ``cancel_print`` (the last remaining object) answers 409 and sends
    nothing, ``refuse`` (unknown or already-excluded name) answers 400
    and sends nothing, and without ``confirm: true`` the plan's
    ``confirm`` action surfaces as a 400 naming the requirement.
    """
    read = status_module.read_live_status if read is None else read
    send = default_send if send is None else send
    try:
        entry, view = _resolve(data, printer_id)
        body = body if isinstance(body, dict) else {}
        target = body.get("object")
        if not isinstance(target, str) or not target.strip():
            raise ControlError(400, "object must name one object on the plate. Nothing sent.")
        target = target.strip()
        adapter = adapter or printers.get(entry["model"], view)
        connection = _connection(adapter)
        if connection != "moonraker":
            raise ControlError(501, "exclude-object is a Klipper (Moonraker) feature and is "
                                    f"not supported for {connection or 'this'} printers. "
                                    "Nothing sent.")
        live = _live_status(adapter, view, read)
        objects = [name for name in (live.get("objects") or []) if isinstance(name, str)]
        excluded = {name for name in (live.get("excluded_objects") or [])
                    if isinstance(name, str)}
        remaining = [name for name in objects if name not in excluded]
        plan = _exclude_object.exclude_plan(objects, target,
                                            confirmed=body.get("confirm") is True,
                                            remaining=remaining)
        action = plan["action"]
        if action == "confirm":
            raise ControlError(400, 'excluding an object needs the JSON body '
                                    '{"object": "<name>", "confirm": true}. '
                                    f'{plan["reason"]} Nothing sent.')
        if action == "cancel_print":
            raise ControlError(409, f'{plan["reason"]} Nothing sent.')
        if action != "exclude":
            raise ControlError(400, f'{plan["reason"]}. Nothing sent.')
        script = plan["body"]["params"]["script"]
        _dispatch(send, printer_control.for_printer(printer_id, adapter=adapter, view=view),
                  ("gcode", script), "exclude-object")
    except ControlError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}
    return 200, {"ok": True, "id": entry["id"], "action": "exclude", "object": target,
                 "detail": plan["reason"]}
