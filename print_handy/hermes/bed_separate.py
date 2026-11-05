"""Guided bed separation after a stop: open the nozzle-to-bed gap (``/print-bed-down``).

After an emergency stop or a failed first layer the nozzle can be resting on,
or buried in, the print. This module opens that gap by a small configured
distance (``[maintenance] bed_separate_mm``, default 20 mm) so the bed can be
cleaned, and degrades to instruction-only guidance whenever a safe live move
cannot be established.

Safety posture (mirrors :mod:`print_handy.hermes.nozzle_change`):

* Motion is a physical printer action. It is sent only after an explicit,
  fresh operator confirmation (5-minute window, captured after the button
  press), under the shared interprocess control lock (P1-33), and only after
  a fresh state read taken inside that lock.
* The live path is Moonraker/Klipper only, and only when Klipper itself
  reports that Z is homed (``toolhead.homed_axes`` contains ``z``) and
  reports both the current Z position and the Z axis maximum. The move is one
  absolute ``G1`` to ``current Z + distance``, and it is refused unless that
  target stays inside the reported Z maximum.
* Nothing here forces a position: no ``SET_KINEMATIC_POSITION``, no
  ``FORCE_MOVE``, no homing (``G28``) and no heat. Connection availability, a
  printer's kinematics label or an assumed Z of 0 never stand in for
  Klipper's own homed-axis report. An unhomed Z (the normal state right
  after an emergency-stop restart), any other connection, or any failed read
  gets manual instructions and nothing is sent.

Nothing in this module runs on import, and every printer-touching function
takes injectable sender/reader callables so tests never contact a printer.
"""

from __future__ import annotations

import time
from typing import Any, Callable

DEFAULT_SEPARATE_MM = 20.0
MIN_SEPARATE_MM = 1.0
MAX_SEPARATE_MM = 50.0
CONFIRM_WINDOW_S = 300       # a motion confirmation is fresh for 5 minutes
MOVE_FEED_MM_MIN = 300       # 5 mm/s: slow enough to watch and stop
MAX_MARGIN_MM = 1.0          # stay this far inside the reported Z maximum

_IDLE_STATES = {"standby", "complete", "cancelled", "error"}
_QUERY = "/printer/objects/query?print_stats&toolhead=homed_axes,position,axis_maximum&webhooks"


def separate_mm(cfg: dict[str, Any] | None = None) -> float:
    """The configured separation distance, bounded to a sane range.

    A missing or unreadable setting falls back to the default; a value outside
    [MIN_SEPARATE_MM, MAX_SEPARATE_MM] is clamped, never trusted as typed.
    """
    try:
        if cfg is None:
            from . import _config
            cfg = _config.cfg()
        value = float((cfg.get("maintenance") or {}).get("bed_separate_mm", DEFAULT_SEPARATE_MM))
    except Exception:
        value = DEFAULT_SEPARATE_MM
    if value != value:  # NaN
        value = DEFAULT_SEPARATE_MM
    return min(MAX_SEPARATE_MM, max(MIN_SEPARATE_MM, value))


def supports_live_move(connection: str | None) -> bool:
    """Only a Moonraker/Klipper connection can even be considered for a live move."""
    return str(connection or "").casefold() == "moonraker"


def manual_instructions(distance_mm: float | None = None, reason: str = "") -> str:
    """The instruction-only fallback. Never sends anything."""
    distance = separate_mm() if distance_mm is None else float(distance_mm)
    lead = f"{reason} " if reason else ""
    return (f"{lead}Nothing was sent to the printer. To open the gap by hand: on the printer's own screen "
            f"(Move / Axis / Jog), move Z by +{distance:g} mm so the nozzle and bed move apart. On a "
            "printer whose bed drops, the bed moves down; on one whose head rises, the head moves up. "
            "If the screen refuses Z moves until the printer homes, home it from the screen only after "
            "you have checked that nothing is under the nozzle, or lower the bed by hand with the "
            "power off if your printer allows that. Keep fingers clear of the nozzle and bed; either "
            "may still be hot.")


def _lock_for(printer_id: str | None = None):
    """The shared interprocess control lock (P1-33), the same one every control action uses."""
    from . import print_control as pc
    return pc._lock_for(printer_id)


def _fresh_confirmation(confirmed_at: float) -> bool:
    return not (time.time() - float(confirmed_at) > CONFIRM_WINDOW_S
                or float(confirmed_at) > time.time() + 2)


def _state_view(reader: Callable[[], dict[str, Any]] | None, printer_id: str | None) -> dict[str, Any]:
    if reader is not None:
        return reader() or {}
    from . import print_control as pc
    data = pc._send(_QUERY, printer_id=printer_id)
    return (data.get("result") or {}).get("status") or {}


def _send_gcode(script: str, printer_id: str | None, sender: Callable[[str], Any] | None) -> None:
    if sender is not None:
        sender(script)
        return
    from . import print_control as pc
    pc._send("/printer/gcode/script", method="POST", data={"script": script},
             timeout=60, printer_id=printer_id)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value == value else None


def plan_move(status: dict[str, Any], distance_mm: float) -> tuple[float | None, str]:
    """Decide from one state read whether a live move is safe.

    Returns ``(target_z, "")`` when it is, or ``(None, reason)`` when the
    caller must fall back to instructions. Pure: reads only ``status``.
    """
    webhooks = str((status.get("webhooks") or {}).get("state") or "unknown").casefold()
    if webhooks != "ready":
        return None, f"The printer's controller is {webhooks}, not ready."
    job = str((status.get("print_stats") or {}).get("state") or "unknown").casefold()
    if job in {"printing", "paused"}:
        return None, f"The printer is {job}; a job still owns the motion system."
    if job not in _IDLE_STATES:
        return None, f"The printer's job state is {job}."
    toolhead = status.get("toolhead") or {}
    homed = str(toolhead.get("homed_axes") or "").casefold()
    if "z" not in homed:
        return None, ("Klipper reports Z is not homed, so its Z position is unknown and a live move "
                      "cannot be bounded.")
    position = toolhead.get("position")
    maximum = toolhead.get("axis_maximum")
    current = _number(position[2]) if isinstance(position, (list, tuple)) and len(position) > 2 else None
    z_max = _number(maximum[2]) if isinstance(maximum, (list, tuple)) and len(maximum) > 2 else None
    if current is None or z_max is None:
        return None, "Klipper did not report the Z position and Z maximum."
    target = round(current + float(distance_mm), 3)
    if target > z_max - MAX_MARGIN_MM:
        return None, (f"Z is at {current:g} mm and the Z maximum is {z_max:g} mm, so +{distance_mm:g} mm "
                      "would leave the allowed travel.")
    return target, ""


def separate(*, confirmed_at: float, distance_mm: float | None = None,
             connection: str | None = None,
             gcode_sender: Callable[[str], Any] | None = None,
             reader: Callable[[], dict[str, Any]] | None = None,
             printer_id: str | None = None) -> tuple[bool, str]:
    """Open the nozzle-to-bed gap once, after a fresh confirmation.

    Returns ``(True, message)`` only when the move was sent and the readback
    agrees. Every refusal returns ``(False, message)``; a refusal before the
    move sends nothing and carries the manual instructions.
    """
    distance = separate_mm() if distance_mm is None else float(distance_mm)
    if not MIN_SEPARATE_MM <= distance <= MAX_SEPARATE_MM:
        return False, (f"Refused: {distance:g} mm is outside {MIN_SEPARATE_MM:g}-{MAX_SEPARATE_MM:g} mm. "
                       "Nothing sent.")
    if connection is None:
        try:
            from . import print_control as pc
            connection = pc._adapter_for(printer_id).connection
        except Exception:
            connection = None
    if not supports_live_move(connection):
        return False, manual_instructions(distance, "This printer is not on a Moonraker/Klipper connection.")
    if not _fresh_confirmation(confirmed_at):
        return False, "Refused: confirmation expired. Nothing sent."
    lock = _lock_for(printer_id)
    if not lock.acquire(blocking=False):
        return False, "Refused: another printer control action is in flight. Nothing sent."
    try:
        try:
            status = _state_view(reader, printer_id)
        except Exception as exc:
            return False, manual_instructions(
                distance, f"The printer state could not be read ({type(exc).__name__}).")
        target, reason = plan_move(status, distance)
        if target is None:
            return False, manual_instructions(distance, reason)
        try:
            _send_gcode(f"G90\nG1 Z{target:g} F{MOVE_FEED_MM_MIN}", printer_id, gcode_sender)
        except Exception as exc:
            return False, (f"The move command failed ({type(exc).__name__}). Check the printer screen "
                           "before doing anything else; do not retry blindly.")
        try:
            after = _state_view(reader, printer_id)
            position = (after.get("toolhead") or {}).get("position")
            now_z = _number(position[2]) if isinstance(position, (list, tuple)) and len(position) > 2 else None
        except Exception:
            now_z = None
        if now_z is None or abs(now_z - target) > 0.05:
            return False, (f"Sent one Z move to {target:g} mm, but the readback is {now_z!r}. "
                           "Check the printer before touching the bed.")
        return True, (f"Opened the gap by {distance:g} mm (Z is now {now_z:g} mm). No homing, no heat. "
                      "The nozzle and bed may still be hot; clean the bed with tools, not fingers.")
    finally:
        lock.release()
