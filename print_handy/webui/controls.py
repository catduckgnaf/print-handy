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
  ``{"confirm": true}``. Emergency stop has NO state gate: it is the
  panic button, so it sends from any state the printer can be read in.
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

from .. import printers, settings
from ..core import exclude_object as _exclude_object
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
            return _moonraker_post(view, f"/printer/print/{action}")
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


def _fact(adapter, name):
    fact = getattr(adapter, "fact", None)
    if not callable(fact):
        return None
    try:
        value = fact(name)
    except Exception:  # a model without that fact has no such limit
        return None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _fitted_nozzle_type(view, printer_id):
    """The nozzle type whose temperature limit applies to this printer.

    The fitted nozzle lives in the addressed printer's hardware record
    (written by ``/nozzle change`` and ``printer_state set``): a recorded
    type wins over the configured ``[printer] nozzle_type``, which names
    the nozzle the printer shipped with, not the one fitted now. No
    record, a record naming no type, or a recorded type that cannot be
    recognized keeps the configured behavior. A recorded ``"stock"`` is
    an unconfirmed factory nozzle and resolves exactly like a configured
    one (the caller maps it to the softest option).
    """
    configured = (view.get("printer") or {}).get("nozzle_type") or "brass"
    if printer_id is None:
        return configured
    try:
        from ..core import printer_state
        record = printer_state.load(printer_id)
    except Exception:  # no readable hardware record: configuration is all there is
        return configured
    recorded = (record.get("nozzle") or {}).get("type") if isinstance(record, dict) else None
    if isinstance(recorded, str) and recorded.strip() and recorded.strip().lower() != "stock":
        from ..core import nozzle
        try:
            nozzle.normalize_nozzle_type(recorded)
        except nozzle.NozzleError:
            return configured
        return recorded
    return configured


def temperature_limit(adapter, view, heater, printer_id=None):
    """The highest target this printer accepts for ``heater`` (°C).

    The nozzle limit follows the FITTED nozzle: ``printer_id``'s hardware
    record when it names a type, else the configured nozzle type.
    """
    if heater == "nozzle":
        from ..core import nozzle
        nozzle_type = _fitted_nozzle_type(view, printer_id)
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
        limit = temperature_limit(adapter, view, heater, printer_id)
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
            limits[key] = temperature_limit(adapter, view, heater, printer_id)
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


def _dispatch(send, adapter, view, command, label):
    try:
        send(adapter, view, command)
    except ControlError:
        raise
    except Exception as exc:
        raise ControlError(502, f"{label} failed to reach the printer "
                                f"({type(exc).__name__}: {exc})") from None


def power_action(data, printer_id, action, body=None, *, adapter=None, read=None, send=None):
    """(code, body) for POST /api/printers/<id>/emergency_stop|firmware_restart|server_restart.

    All three need ``{"confirm": true}``. There is deliberately no state
    gate — least of all on emergency stop, which must send from whatever
    state the printer can be read in (printing included, exactly like
    the chat panic ladder). The printer is still read first, so an
    unreachable printer is an explicit 502 rather than a blind send —
    except ``server_restart``, whose read is best-effort: Moonraker
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
        _dispatch(send, adapter, view, ("power", action), action)
    except ControlError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}
    return 200, {"ok": True, "id": entry["id"], "action": action, "from_state": state,
                 "detail": f"{action} sent; check the printer's status for the result."}


# The temperature-setting words a raw script can carry, and the heater
# each sets: the words this module itself sends for /temperature
# (M104/M140, in default_send above), their wait variants (M109/M190 —
# the same four the family G-code guards scan for), Marlin's M303
# autotune, and Klipper's SET_HEATER_TEMPERATURE, the form the chat
# nozzle-change path uses, plus its PID_CALIBRATE, which heats the
# named heater to its target in the same KEY=VALUE shape.
_HEATER_WORDS = {"M104": "nozzle", "M109": "nozzle", "M140": "bed", "M190": "bed"}
_HEATER_WORD_RE = re.compile(r"^(M104|M109|M140|M190|M303)(?![0-9])", re.IGNORECASE)
_HEATER_PARAM_RE = re.compile(r"([SR])([-+]?[0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_M303_EXT_RE = re.compile(r"E([-+]?[0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_EXTENDED_HEATER_RE = re.compile(
    r"^(SET_HEATER_TEMPERATURE|PID_CALIBRATE)(?![A-Za-z0-9_])", re.IGNORECASE)
# States in which a printer cannot accept commands at all, so a raw
# script is refused 409 like the state-gated control actions instead of
# being sent blind. Any positively known state (idle, printing, paused,
# operational, complete, ...) passes: mid-print scripts are this
# route's legitimate use, so the gate refuses only the states that
# name a printer in no position to act.
_GCODE_REFUSED_STATES = frozenset(
    {"unknown", "error", "shutdown", "disconnected", "offline", "unavailable"})


def _extended_heater_target(word, rest):
    """(word, heater, target_c) or None for one Klipper extended line.

    Klipper tokenizes extended commands with ``shlex`` and builds a
    parameter dict, so this does the same: quoted values parse as
    Klipper parses them, and a repeated key's last value is the one
    that wins. A line that cannot be parsed that way — unbalanced
    quotes, a token without ``=``, a missing HEATER or TARGET, or a
    non-numeric TARGET — is refused with a 400 (fail closed) instead
    of being sent unchecked. A named heater this module has no limit
    for returns None, exactly like a command this scan does not know.
    """
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
    if tokens is None or "HEATER" not in params or "TARGET" not in params:
        raise ControlError(400, f"{word} line could not be parsed for its heater "
                                "and target. Nothing sent.")
    try:
        target = float(params["TARGET"])
    except ValueError:
        target = None
    if target is None or target != target or target in (float("inf"), float("-inf")):
        raise ControlError(400, f"{word} target {params['TARGET']!r} is not a number "
                                "of °C. Nothing sent.")
    name = params["HEATER"].lower()
    heater = ("nozzle" if name.startswith("extruder")
              else "bed" if name == "heater_bed" else None)
    if heater is None:
        return None
    return word, heater, target


def _script_heater_targets(script):
    """[(word, heater, target_c)] — every temperature a raw script sets.

    Line by line, the way the script will run: a ``;`` comment is
    stripped first (a heater word inside a comment sets nothing), an
    optional line number and checksum are dropped, and the command word
    must start the line. ``M104``/``M109`` set the nozzle (the ``S``
    parameter, or ``R`` on the wait variant), ``M140``/``M190`` the
    bed. ``M303`` (Marlin PID autotune) heats to its ``S`` target:
    the nozzle by default, the bed when its ``E`` parameter is -1.
    ``SET_HEATER_TEMPERATURE`` and ``PID_CALIBRATE`` name their
    heater: ``extruder*`` is the nozzle and ``heater_bed`` the bed;
    any other named heater has no limit in this module and is left
    alone, exactly like a command this scan does not know. A malformed
    extended line raises ``ControlError`` (400) rather than passing
    unchecked.
    """
    found = []
    for raw_line in str(script).splitlines():
        line = raw_line.split(";", 1)[0].strip()
        if not line:
            continue
        line = re.sub(r"^[Nn][0-9]+\s*", "", line).split("*", 1)[0].strip()
        word_match = _HEATER_WORD_RE.match(line)
        if word_match:
            word = word_match.group(1).upper()
            rest = line[word_match.end():]
            if word == "M303":
                exts = _M303_EXT_RE.findall(rest)
                heater = "bed" if exts and float(exts[-1]) == -1 else "nozzle"
                for param, value in _HEATER_PARAM_RE.findall(rest):
                    if param.upper() == "S":
                        found.append((word, heater, float(value)))
            else:
                for _param, value in _HEATER_PARAM_RE.findall(rest):
                    found.append((word, _HEATER_WORDS[word], float(value)))
            continue
        extended_match = _EXTENDED_HEATER_RE.match(line)
        if extended_match:
            parsed = _extended_heater_target(extended_match.group(1).upper(),
                                             line[extended_match.end():])
            if parsed is not None:
                found.append(parsed)
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
        for word, heater, target in _script_heater_targets(script):
            if heater not in limits:
                limits[heater] = temperature_limit(adapter, view, heater, printer_id)
            limit = limits[heater]
            if not 0 <= target <= limit:
                raise ControlError(400, f"{word} sets the {heater} to {target:g}°C, "
                                        f"outside 0..{limit:g}°C for this printer. "
                                        "Nothing sent.")
        live = _live_status(adapter, view, read)
        state = str(live.get("state") or "unknown")
        if state.strip().lower() in _GCODE_REFUSED_STATES:
            raise ControlError(409, f"cannot send G-code: printer is {state}. "
                                    "Nothing sent.")
        _dispatch(send, adapter, view, ("gcode", script), "send-G-code")
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
        _dispatch(send, adapter, view, ("gcode", script), "exclude-object")
    except ControlError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}
    return 200, {"ok": True, "id": entry["id"], "action": "exclude", "object": target,
                 "detail": plan["reason"]}
