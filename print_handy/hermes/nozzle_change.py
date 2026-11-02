"""Guided physical nozzle change (#513).

Backs ``/nozzle change`` in the print-feedback plugin. The procedure mirrors
the cold pull's safety posture in :mod:`print_handy.hermes.print_control`:

* Heating is a physical printer action. It happens only after an explicit,
  fresh operator confirmation (5-minute window, captured *after* the button
  press), only while Klipper reports an idle job state, and under the same
  shared interprocess control lock as every other control action (P1-33).
* The supported live-heat path is a Moonraker/Klipper printer, through the
  existing Moonraker client in ``print_control._send`` (``M104`` via
  ``/printer/gcode/script``) — no new raw transport is invented. Printers on
  other connections (Bambu LAN, PrusaLink, SDCP, stock FlashForge, OctoPrint)
  degrade gracefully: the flow gives a manual-heat instruction and waits for
  the operator's step confirmation instead of sending anything.
* Every heating step turns the heater back off on failure paths where that
  is possible, and the cooldown step refuses to declare completion until a
  live readback says the extruder is below the safe temperature — a timeout
  is reported as "still hot, check the printer", never as success.
* Completing the change records the new nozzle in ``printer_hardware.json``
  through :func:`record_nozzle_change`, which applies exactly the
  calibration invalidation ``printer_state set --nozzle`` applies (previous
  calibration is historical only, pressure advance marked stale) and leaves
  an explicit ``calibration_pending`` list for the post-change prompts.

Nothing in this module runs on import, and every printer-touching function
takes injectable sender/reader callables so tests never contact a printer.
"""

from __future__ import annotations

import datetime
import re
import time
from typing import Any, Callable

# Diameters/materials the guided flow offers (issue #513 §1). Validation
# itself goes through nozzle.validate_nozzle_setting against the printer's
# supported sets when they are known.
GUIDED_DIAMETERS_MM: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8)
GUIDED_TYPES: tuple[str, ...] = (
    "brass",
    "hardened_steel",
    "stainless_steel",
    "tungsten_carbide",
    "diamond",
)

LOOSEN_TEMP_C = 250          # hot-loosen / hot-torque temperature (issue §1)
SAFE_TEMP_C = 50             # cooldown completes only below this readback
CONFIRM_WINDOW_S = 300       # a heating confirmation is fresh for 5 minutes
HEAT_WAIT_TIMEOUT_S = 300    # give the hotend time to reach loosening temp
COOLDOWN_TIMEOUT_S = 900     # passive cooldown can take a while; never rushed
TORQUE_HINT = "1.5–2.5 Nm (use your hotend maker's spec)"

REMOVED_LABEL = "Old Nozzle Removed & New Nozzle Threaded"
TORQUED_LABEL = "Torqued hot — done"
COOLED_LABEL = "Cooled down — record the new nozzle"

# The step machine the plugin walks. ``heats`` marks the one step that may
# send a heater command — and only after its own explicit confirmation.
GUIDE_STEPS: tuple[dict[str, Any], ...] = (
    {"key": "select", "heats": False,
     "title": "Select the replacement nozzle diameter and material."},
    {"key": "confirm_heat", "heats": False,
     "title": "Operator confirms, in person at the printer, that the hotend "
              "may be heated for the change."},
    {"key": "heat_loosen", "heats": True,
     "title": f"Heat the hotend to {LOOSEN_TEMP_C} °C to melt residual plastic "
              "before loosening the old nozzle."},
    {"key": "removed_threaded", "heats": False,
     "title": f"Operator removes the old nozzle hot and threads the new one; "
              f"step confirmation: [{REMOVED_LABEL}]."},
    {"key": "hot_torque", "heats": False,
     "title": f"Hot-torque the new nozzle at temperature ({TORQUE_HINT}) "
              "while the heater stays at the loosening temperature."},
    {"key": "cooldown", "heats": False,
     "title": f"Heater off; wait for a live readback below {SAFE_TEMP_C} °C."},
    {"key": "record", "heats": False,
     "title": "Record the new nozzle in printer_hardware.json and invalidate "
              "the previous calibration."},
    {"key": "calibration", "heats": False,
     "title": "Offer the post-change calibrations: Z-offset (--sync-z), bed "
              "mesh leveling, pressure advance / flow check."},
)

_IDLE_STATES = {"standby", "complete", "cancelled", "error"}


def _lock_for(printer_id: str | None = None):
    """The shared interprocess control lock (P1-33): the same lock
    print_control, the standalone control CLIs and print_now contend for —
    not a private one, so a nozzle change can never overlap a purge, a
    slot load or a print start issued from another process."""
    from . import print_control as pc
    return pc._lock_for(printer_id)


def supports_live_heat(connection: str | None) -> bool:
    """Only the Moonraker/Klipper connection can be commanded to heat here."""
    return str(connection or "").casefold() == "moonraker"


def manual_heat_instructions(temp_c: int = LOOSEN_TEMP_C) -> str:
    """The graceful degradation for printers Print Handy cannot heat."""
    return (f"This printer is not on a Moonraker/Klipper connection, so I can't heat it from here. "
            f"On the printer's own screen, heat the hotend to {temp_c} °C, remove the old nozzle hot, "
            f"thread the new one, and torque it hot ({TORQUE_HINT}). Then turn the heater off and let "
            f"it cool below {SAFE_TEMP_C} °C before confirming here — the state record and calibration "
            "prompts still apply.")


_EXTRUDER_NAME = re.compile(r"^extruder\d*$")


def _extruder_sort_key(name: str) -> int:
    return int(name[8:]) if name[8:].isdigit() else 0


def _extruder_names(printer_id: str | None, *, offline: bool = False) -> list[str]:
    """Every extruder heater object the printer has, primary first.

    Prefers the printer adapter's ``moonraker_objects()`` (the Creator 5 / 5 Pro
    adapters list ``extruder..extruder{n-1}``). With no usable adapter it asks
    Moonraker's ``/printer/objects/list`` and keeps ``extruder`` plus
    ``extruder<digits>``, and finally falls back to plain ``extruder``.
    ``offline`` skips the network lookup (injected readers/senders in tests).
    """
    try:
        from . import print_control as pc
        names = [n for n in pc._adapter_for(printer_id).moonraker_objects()
                 if isinstance(n, str) and _EXTRUDER_NAME.match(n)]
    except Exception:
        names = []
    if not names and not offline:
        try:
            from . import print_control as pc
            data = pc._send("/printer/objects/list", printer_id=printer_id)
            listed = (data.get("result") or {}).get("objects") or []
            names = [n for n in listed if isinstance(n, str) and _EXTRUDER_NAME.match(n)]
        except Exception:
            names = []
    return sorted(dict.fromkeys(names), key=_extruder_sort_key) or ["extruder"]


def _state_view(reader: Callable[[], dict[str, Any]] | None,
                printer_id: str | None,
                extruders: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    if reader is not None:
        return reader() or {}
    from . import print_control as pc
    names = list(extruders) if extruders else _extruder_names(printer_id)
    query = "&".join(["print_stats", *names, "webhooks"])
    data = pc._send(f"/printer/objects/query?{query}", printer_id=printer_id)
    return ((data.get("result") or {}).get("status") or {})


def _heaters_off(printer_id: str | None, sender: Callable[[str], Any] | None,
                 extruders: list[str] | tuple[str, ...] | None = None) -> None:
    """Turn off every known extruder heater; ``M104 S0`` covers the primary.

    Every heater is attempted even if one command fails; the first error is
    raised afterwards so callers still report a failed heater-off.
    """
    names = list(extruders) if extruders else _extruder_names(
        printer_id, offline=sender is not None)
    errors: list[Exception] = []
    for script in ["M104 S0", *[f"SET_HEATER_TEMPERATURE HEATER={n} TARGET=0"
                                for n in names if n != "extruder"]]:
        try:
            _send_gcode(script, printer_id, sender)
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise errors[0]


def _send_gcode(script: str, printer_id: str | None,
                sender: Callable[[str], Any] | None) -> None:
    if sender is not None:
        sender(script)
        return
    from . import print_control as pc
    pc._send("/printer/gcode/script", method="POST", data={"script": script},
             timeout=30, printer_id=printer_id)


def _job_state(status: dict[str, Any]) -> str:
    return str((status.get("print_stats") or {}).get("state") or "unknown").casefold()


def _extruders(status: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return all reported extruder objects in status in stable order.

    Detects 'extruder', 'extruder1', 'extruder2', etc., matching Moonraker/Klipper
    and multi-tool conventions.
    """
    if not isinstance(status, dict):
        return []
    extruders = []
    if isinstance(status.get("extruder"), dict):
        extruders.append(("extruder", status["extruder"]))
    extras = []
    for k, v in status.items():
        if k.startswith("extruder") and k[8:].isdigit() and isinstance(v, dict):
            extras.append((int(k[8:]), k, v))
    for _, k, v in sorted(extras):
        extruders.append((k, v))
    return extruders


def _extruder(status: dict[str, Any]) -> dict[str, Any]:
    return status.get("extruder") or {}


def _fresh_confirmation(confirmed_at: float) -> bool:
    return not (time.time() - float(confirmed_at) > CONFIRM_WINDOW_S
                or float(confirmed_at) > time.time() + 2)


def heat_for_loosen(*, confirmed_at: float, temp_c: int = LOOSEN_TEMP_C,
                    gcode_sender: Callable[[str], Any] | None = None,
                    reader: Callable[[], dict[str, Any]] | None = None,
                    printer_id: str | None = None,
                    wait_timeout_s: float = HEAT_WAIT_TIMEOUT_S,
                    poll_s: float = 2.0) -> tuple[bool, str]:
    """Heat to the loosening temperature, once, after a fresh confirmation.

    Fail-closed like ``print_control.coldpull``: expired confirmation, a busy
    or non-idle printer, or a missing temperature readback all refuse with
    nothing (further) sent. On a failed heat-up the heater is turned off
    before returning, so a refused flow never leaves the hotend heating
    unattended.
    """
    if temp_c != LOOSEN_TEMP_C:
        return False, f"Refused: {temp_c} °C is not the guided loosening temperature. Nothing sent."
    if not _fresh_confirmation(confirmed_at):
        return False, "Refused: confirmation expired. Nothing sent."
    lock = _lock_for(printer_id)
    if not lock.acquire(blocking=False):
        return False, "Refused: another printer control action is in flight. Nothing sent."
    try:
        names = _extruder_names(printer_id, offline=reader is not None or gcode_sender is not None)
        try:
            st = _state_view(reader, printer_id, names)
        except Exception as exc:
            return False, f"Refused: printer state could not be read ({type(exc).__name__}). Nothing sent."
        job_state = _job_state(st)
        if job_state in {"printing", "paused"}:
            return False, f"Refused: printer is {job_state}. Nothing sent."
        if job_state not in _IDLE_STATES:
            return False, f"Refused: printer state is {job_state}. Nothing sent."
        try:
            _send_gcode(f"M104 S{int(temp_c)}", printer_id, gcode_sender)
        except Exception as exc:
            return False, f"Heating command failed ({type(exc).__name__}). Check the printer screen; do not retry blindly."
        deadline = time.monotonic() + wait_timeout_s
        last = None
        while True:
            try:
                st = _state_view(reader, printer_id, names)
            except Exception:
                st = {}
            ext = _extruder(st)
            last = ext.get("temperature")
            if isinstance(last, (int, float)) and float(last) >= temp_c - 10:
                if float(ext.get("target") or 0) != float(temp_c):
                    return False, (f"Hotend reached {float(last):.0f} °C but its target readback is "
                                   f"{ext.get('target')!r}, not {temp_c}. Check the printer; do not proceed.")
                return True, (f"Hotend is at {float(last):.0f} °C (target {temp_c} °C). "
                              "Remove the old nozzle now, thread the new one, then confirm.")
            if time.monotonic() >= deadline:
                break
            time.sleep(poll_s)
        # Never leave a heater running unattended behind a failed step.
        try:
            _heaters_off(printer_id, gcode_sender, names)
        except Exception:
            pass
        return False, (f"Hotend did not reach {temp_c} °C within {wait_timeout_s:.0f}s "
                       f"(last readback {last!r}). Heater turned off. Check the printer screen.")
    finally:
        lock.release()


def heater_off(*, gcode_sender: Callable[[str], Any] | None = None,
               printer_id: str | None = None) -> tuple[bool, str]:
    """Best-effort heater off for a cancelled flow. No waiting, no recording."""
    try:
        _heaters_off(printer_id, gcode_sender)
    except Exception as exc:
        return False, f"Heater-off command failed ({type(exc).__name__}). Turn the heater off on the printer screen NOW."
    return True, "Heater turned off. Nothing was recorded."


def cooldown(*, gcode_sender: Callable[[str], Any] | None = None,
             reader: Callable[[], dict[str, Any]] | None = None,
             printer_id: str | None = None,
             timeout_s: float = COOLDOWN_TIMEOUT_S,
             poll_s: float = 2.0) -> tuple[bool, str]:
    """Heater off, then wait for a live readback below SAFE_TEMP_C.

    A timeout is a failure ("still hot"), never a silent success: the state
    record is only written after this returns True.
    """
    lock = _lock_for(printer_id)
    if not lock.acquire(blocking=False):
        return False, "Refused: another printer control action is in flight. Nothing sent."
    try:
        names = _extruder_names(printer_id, offline=reader is not None or gcode_sender is not None)
        try:
            _heaters_off(printer_id, gcode_sender, names)
        except Exception as exc:
            return False, f"Heater-off command failed ({type(exc).__name__}). Turn the heater off on the printer screen."
        deadline = time.monotonic() + timeout_s
        last = None
        while True:
            try:
                st = _state_view(reader, printer_id, names)
            except Exception:
                st = {}
            all_ext = _extruders(st)
            if not all_ext:
                # Fallback if no extruder dicts are indexed as dicts
                ext = _extruder(st)
                all_ext = [("extruder", ext)]

            # Check if all reported nozzles are cool and targets are 0
            hot_nozzles = []
            nonzero_targets = []
            for name, ext in all_ext:
                t = ext.get("temperature")
                tgt = float(ext.get("target") or 0)
                if tgt != 0.0:
                    nonzero_targets.append((name, ext.get("target")))
                if not isinstance(t, (int, float)) or float(t) >= SAFE_TEMP_C:
                    hot_nozzles.append((name, t))

            primary_temp = all_ext[0][1].get("temperature")
            last = primary_temp if len(all_ext) == 1 else {name: ext.get("temperature") for name, ext in all_ext}

            if not hot_nozzles:
                if nonzero_targets:
                    targets_desc = ", ".join(f"{name}={tgt!r}" for name, tgt in nonzero_targets)
                    return False, (f"All reported nozzles are below {SAFE_TEMP_C} °C but heater target(s) are "
                                   f"non-zero ({targets_desc}). Check the printer; not recording the change yet.")
                temps_desc = f"{float(primary_temp or 0):.0f} °C" if len(all_ext) == 1 else ", ".join(
                    f"{name}: {float(ext.get('temperature') or 0):.0f} °C" for name, ext in all_ext
                )
                label = "Hotend is" if len(all_ext) == 1 else "All reported nozzles are"
                return True, f"{label} at {temps_desc} with the heater off. Safe to record the new nozzle."

            if time.monotonic() >= deadline:
                return False, (f"Nozzle(s) still hot after {timeout_s:.0f}s (needs < {SAFE_TEMP_C} °C, current: {last!r}). "
                               "Nothing was recorded. Check the printer and re-run /nozzle change to finish.")
            time.sleep(poll_s)
    finally:
        lock.release()


def record_nozzle_change(state: dict[str, Any], diameter_mm: float,
                         nozzle_type: str, *, today: str | None = None,
                         supported_sizes=None, supported_types=None) -> dict[str, Any]:
    """Record a completed change in a printer_hardware state dict (in place).

    Applies the same invalidation as ``printer_state set --nozzle``: the
    recorded nozzle is replaced, the user Z offset is cleared and marked
    unverified, the calibration authority records that the previous
    calibration is historical only, and a tuned pressure advance is marked
    stale. Adds the explicit ``calibration_pending`` list the post-change
    prompts work from, plus a ``nozzle_change`` audit record. Raises
    ``NozzleError`` for an unsupported diameter/type — nothing is mutated
    before validation passes.
    """
    from ..core.nozzle import validate_nozzle_setting

    dia, norm_type = validate_nozzle_setting(
        diameter_mm, nozzle_type,
        supported_sizes=supported_sizes, supported_types=supported_types)
    installed = today or datetime.date.today().isoformat()
    previous = dict(state.get("nozzle") or {})
    state["nozzle"] = {"diameter_mm": dia, "type": norm_type, "installed": installed}
    state["z_offset_verified"] = False
    state["z_offset_mm"] = None
    state["calibration_authority"] = {
        "mode": None, "operator_confirmed": False,
        "reason": "Nozzle changed; previous calibration is historical only"}
    state["z_offset_note"] = ("Nozzle changed; calibration needs a measured confirmation "
                              "for the active mode. --sync-z only records the probe trigger.")
    if state.get("pressure_advance"):
        state["pressure_advance"]["stale"] = True
    state["calibration_pending"] = ["z_offset", "bed_mesh", "pressure_advance_flow"]
    state["bed_mesh_stale"] = True
    state["nozzle_change"] = {
        "recorded": installed,
        "previous": {"diameter_mm": previous.get("diameter_mm"), "type": previous.get("type")},
        "new": {"diameter_mm": dia, "type": norm_type},
        "procedure": "guided /nozzle change (#513)",
    }
    return state


def calibration_prompts(state: dict[str, Any]) -> list[dict[str, Any]]:
    """The post-change calibration offers for the recorded nozzle (#513 §3).

    Each offer names what it does and its command path; offers that would
    move or heat the printer (bed mesh) stay offers — running one goes
    through its own confirmation in :func:`run_bed_mesh`.
    """
    nozzle = state.get("nozzle") or {}
    dia = nozzle.get("diameter_mm", 0.4)
    prompts = [
        {"key": "z_offset", "label": "Z-offset calibration",
         "detail": "A nozzle change moves the tip, so the old offset is historical only. "
                   "Measure at the printer, then record the live probe store with "
                   "`printer_state set --sync-z` (that records the probe trigger only; "
                   "a measured user offset is recorded separately with `--z-offset N`).",
         "command": "--sync-z", "moves_printer": False},
        {"key": "bed_mesh", "label": "Run Bed Mesh Leveling",
         "detail": "The saved mesh predates this nozzle, so its stale-mesh warning stays "
                   "until a fresh mesh is probed. Running it homes and probes the bed.",
         "command": "BED_MESH_CALIBRATE", "moves_printer": True},
    ]
    pa_detail = ("Pressure advance and flow were tuned for the previous nozzle; retune both "
                 "before trusting extrusion.")
    if dia != 0.4:
        pa_detail = (f"A {dia:g} mm nozzle changes extrusion volume versus the 0.4 mm baseline: "
                     "pressure advance and flow calibration are strongly recommended, not optional.")
    prompts.append({"key": "pressure_advance_flow", "label": "Pressure Advance / Flow check",
                    "detail": pa_detail, "command": None, "moves_printer": False})
    return prompts


def calibration_text(state: dict[str, Any]) -> str:
    """The post-change prompt block shown when a change is recorded."""
    nozzle = state.get("nozzle") or {}
    lines = [f"Recorded: {nozzle.get('diameter_mm')} mm {nozzle.get('type')} nozzle. "
             "Previous calibration is historical only. Before the next print:"]
    for prompt in calibration_prompts(state):
        lines.append(f"- **{prompt['label']}** — {prompt['detail']}")
    lines.append("Bed mesh leveling is offered as the next prompt in this flow; the other "
                 "checks can be done later — prints keep warning until the mesh and offset "
                 "are refreshed.")
    return "\n".join(lines)


def run_bed_mesh(*, confirmed_at: float,
                 gcode_sender: Callable[[str], Any] | None = None,
                 reader: Callable[[], dict[str, Any]] | None = None,
                 printer_id: str | None = None) -> tuple[bool, str]:
    """Send BED_MESH_CALIBRATE once, after a fresh confirmation, while idle."""
    if not _fresh_confirmation(confirmed_at):
        return False, "Refused: confirmation expired. Nothing sent."
    lock = _lock_for(printer_id)
    if not lock.acquire(blocking=False):
        return False, "Refused: another printer control action is in flight. Nothing sent."
    try:
        try:
            st = _state_view(reader, printer_id, ("extruder", "toolhead"))
        except Exception as exc:
            return False, f"Refused: printer state could not be read ({type(exc).__name__}). Nothing sent."
        job_state = _job_state(st)
        if job_state in {"printing", "paused"}:
            return False, f"Refused: printer is {job_state}. Nothing sent."
        if job_state not in _IDLE_STATES:
            return False, f"Refused: printer state is {job_state}. Nothing sent."
        # Klipper rejects BED_MESH_CALIBRATE on unhomed axes ("Must home axis
        # first") and nothing earlier in this flow homes the printer, so home
        # first whenever homing is not proven for all three axes. An
        # unreadable homed_axes counts as unhomed; G28 is idempotent.
        homed = str((st.get("toolhead") or {}).get("homed_axes") or "")
        if not all(axis in homed for axis in "xyz"):
            try:
                _send_gcode("G28", printer_id, gcode_sender)
            except Exception as exc:
                return False, f"Homing failed ({type(exc).__name__}). Check the printer screen. Nothing else sent."
        try:
            _send_gcode("BED_MESH_CALIBRATE", printer_id, gcode_sender)
        except Exception as exc:
            return False, f"Bed mesh command failed ({type(exc).__name__}). Check the printer screen."
        return True, "Bed mesh leveling sent once. It homes and probes; wait for it to finish on the printer screen."
    finally:
        lock.release()


def finish_change(state: dict[str, Any], diameter_mm: float, nozzle_type: str,
                  *, printer_id: str | None = None,
                  klipper_sync: Callable[[str, float], bool] | None = None,
                  **kwargs) -> str:
    """Record the change in printer_hardware.json and return the prompt text.

    Uses printer_state's own load/save so the per-printer file layout (#334)
    is honoured. ``state`` is accepted for callers/tests that already loaded
    it; when it is empty the record is loaded from disk instead.

    On a Moonraker printer the recorded diameter is also reconciled with
    Klipper's own ``extruder.nozzle_diameter`` (the ``printer_state set
    --nozzle`` sync): readiness refuses the next print while the two
    disagree, so presenting the change as complete without the sync would
    leave the printer blocked. A failed sync is reported in the returned
    text, never hidden.
    """
    from ..core import printer_state

    st = state if state else printer_state.load(printer_id)
    record_nozzle_change(st, diameter_mm, nozzle_type, **kwargs)
    printer_state.save(st, printer_id)
    text = calibration_text(st)
    note = _klipper_nozzle_sync_note(st, printer_id=printer_id, klipper_sync=klipper_sync)
    return f"{text}\n{note}" if note else text


def _klipper_nozzle_sync_note(st: dict[str, Any], *, printer_id: str | None,
                              klipper_sync: Callable[[str, float], bool] | None) -> str:
    """Sync the recorded diameter into Klipper's printer.cfg, Moonraker only.

    Returns the operator-facing note ("" when there is nothing to report:
    a non-Moonraker connection, no Moonraker URL on the record, or a
    printer.cfg that already matches).
    """
    from ..core import printer_state

    try:
        from . import print_control as pc
        connection = pc._adapter_for(printer_id).connection
    except Exception:
        return ""
    if str(connection or "").casefold() != "moonraker":
        return ""
    url = st.get("moonraker_url")
    if not url:
        return ""
    dia = float((st.get("nozzle") or {}).get("diameter_mm") or 0)
    sync = klipper_sync or printer_state.sync_klipper_nozzle
    try:
        changed = sync(str(url), dia)
    except Exception as exc:
        return (f"Recorded, but Klipper's nozzle_diameter could not be updated ({type(exc).__name__}), "
                "and printer readiness refuses prints while it disagrees with the record. "
                f"Run `printer_state set --nozzle {dia:g}` to sync it, or set nozzle_diameter "
                "in printer.cfg to match.")
    if changed:
        return (f"Klipper's printer.cfg was updated to {dia:g} mm and Klipper restarted, "
                "so its nozzle_diameter matches the record.")
    return ""
