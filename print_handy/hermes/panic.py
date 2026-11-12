"""Instant, no-LLM printer panic controls for FlashForge AD5X running ZMod Klipper.

Provides:
- force-restart [PRINTER_ID]: Unconditional panic stop and restart ladder for one printer
  (the default printer when no id is given). Order: POST /printer/emergency_stop (M112) -> POST /printer/firmware_restart ->
  poll klippy ready (90s max) -> fallback to ZMod REBOOT macro + safe_idle check.
- purge: Guarded extrusion of filament already loaded at the toolhead.
  Refuses while printing/paused, requires ready klippy and head sensor filament_detected,
  clamps mm (<=150) and raises temp to at least 180, refuses a temp above the printer's nozzle
  ceiling (core.heater_ceiling, the one ceiling every path uses), extrudes in 10 mm chunks, always
  resets heater to 0.
- status: Read-only JSON of state, klippy, temperatures and head sensor.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from . import print_control as pc
from .slot_load import load_slot


def _known_printer(printer_id):
    """The roster id to act on, or None for the default printer. An unknown id raises ValueError."""
    if not printer_id:
        return None
    from print_handy import settings
    try:
        known = settings.by_id(str(printer_id))["id"]
    except settings.SettingsError as exc:
        raise ValueError(str(exc)) from None
    # The default printer keeps the exact path the ladder always used.
    return None if known == settings.default_printer_id() else known


def force_restart(
    *,
    printer_id: str | None = None,
    caller=None,
    status_reader=None,
    moonraker_reader=None,
    native_reader=None,
    wait_s: float = 90.0,
    poll_s: float = 2.0,
    reboot_grace_s: float = 30.0,
    reboot_wait_s: float = 120.0,
) -> dict[str, Any]:
    """Unconditional emergency stop and restart ladder.

    Does not refuse while printing: this is the panic button. ``printer_id``
    names the roster printer it acts on; None is the default printer, the
    only one this ladder reached before (#1147).
    Ladder:
    1. POST /printer/emergency_stop
    2. POST /printer/firmware_restart
    3. Poll klippy ready up to wait_s
    4. If klippy not ready: send ZMod REBOOT macro once and wait for safe_idle.
    """
    printer_id = _known_printer(printer_id)
    if caller is not None:
        call = caller
    elif printer_id:
        def call(path, **kw):
            return pc._send(path, printer_id=printer_id, **kw)
    else:
        call = pc._call
    read_st = status_reader or ((lambda: pc.status(printer_id)) if printer_id else pc.status)
    steps: list[str] = []

    # Step 1: Emergency stop
    steps.append("emergency_stop")
    try:
        call("/printer/emergency_stop", method="POST", timeout=15)
    except Exception:
        pass

    # Step 2: Firmware restart
    steps.append("firmware_restart")
    try:
        call("/printer/firmware_restart", method="POST", timeout=15)
    except Exception:
        pass

    # Step 3: Poll until klippy is ready
    deadline = time.monotonic() + wait_s
    klippy_ready = False
    last_st: dict[str, Any] = {}
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        try:
            last_st = read_st()
        except Exception:
            continue
        if str(last_st.get("klippy", "")).casefold() == "ready":
            klippy_ready = True
            break

    if klippy_ready:
        steps.append("klippy_ready")
        return {"ok": True, "steps": steps, "final_state": last_st}

    # Step 4: Fallback to ZMod REBOOT macro once
    steps.append("reboot_macro")
    try:
        call("/printer/gcode/script", method="POST", data={"script": "REBOOT"}, timeout=15)
    except Exception:
        pass

    from print_handy.core.clear_job import safe_idle

    def default_mr():
        if printer_id:
            # The same shape clear_job.moonraker() returns, read from this printer's Moonraker.
            s = ((pc._send("/printer/objects/query?webhooks&print_stats&extruder&heater_bed",
                           printer_id=printer_id).get("result") or {}).get("status") or {})
            return {"state": (s.get("webhooks") or {}).get("state"), "print": s.get("print_stats") or {},
                    "extruder": s.get("extruder") or {}, "bed": s.get("heater_bed") or {}}
        from print_handy.core.clear_job import moonraker
        return moonraker()

    def default_native():
        from print_handy.core import printer_state
        from print_handy.core.model_job import read_stock_detail
        return read_stock_detail(printer_state.load(printer_id) if printer_id else printer_state.load())

    mr_read = moonraker_reader or default_mr
    nat_read = native_reader or default_native

    if reboot_grace_s > 0:
        time.sleep(reboot_grace_s)

    reboot_deadline = time.monotonic() + reboot_wait_s
    idle_ok = False
    while time.monotonic() < reboot_deadline:
        try:
            m, n = mr_read(), nat_read()
            if isinstance(m, dict) and isinstance(n, dict) and safe_idle(m, n):
                idle_ok = True
                break
        except Exception:
            pass
        time.sleep(poll_s)

    if idle_ok:
        steps.append("safe_idle")
        try:
            final_st = read_st()
        except Exception:
            final_st = {"state": "standby", "klippy": "ready"}
        return {"ok": True, "steps": steps, "final_state": final_st}

    try:
        final_st = read_st()
    except Exception:
        final_st = last_st or {"state": "unknown", "klippy": "unknown"}
    return {"ok": False, "steps": steps, "final_state": final_st}


def purge(
    *,
    slot: int | None = None,
    mm: int = 60,
    temp: int = 210,
    caller=None,
    reader=None,
    gcode_sender=None,
    adapter=None,
    state=None,
) -> dict[str, Any]:
    """Guarded purge of filament already loaded at the toolhead.

    Refuses unless print_stats.state is in {standby, complete, cancelled, error}
    and klippy is ready, and filament is detected at the head switch sensor.
    Holds pc._CONTROL_LOCK.
    Clamps mm to <= 150 and raises temp to at least 180 C. A temp above the
    printer's nozzle ceiling (core.heater_ceiling: the fitted nozzle type and the
    model's max_hotend_c) is refused before anything is sent, never clamped (#1147).
    Extrudes in 10 mm chunks and always sets extruder heater to 0 in finally.
    """
    if slot is not None:
        return {
            "ok": False,
            "error": f"Refused: --slot is unsupported in purge. Purge operates only on filament already loaded at the head. To load slot {slot} while idle, use ph-load-slot {slot}.",
        }

    clamped_mm = max(1, min(150, int(mm)))
    clamped_temp = max(180, int(temp))
    problem = _nozzle_ceiling_refusal(clamped_temp, adapter, state)
    if problem:
        return {"ok": False, "error": f"Refused: {problem}. Nothing sent."}

    if not pc._CONTROL_LOCK.acquire(blocking=False):
        return {
            "ok": False,
            "error": "Refused: another printer control action is in flight. Nothing sent.",
        }

    call = caller or pc._call

    try:
        if reader:
            st = reader()
        else:
            data = call("/printer/objects/query?print_stats&webhooks&filament_switch_sensor%20head_switch_sensor&extruder")
            st = (data.get("result") or {}).get("status") or {}

        job_state = str((st.get("print_stats") or {}).get("state") or "unknown").casefold()
        klippy = str((st.get("webhooks") or {}).get("state") or "unknown").casefold()
        sensor = (st.get("filament_switch_sensor head_switch_sensor") or {}).get("filament_detected")

        if klippy != "ready":
            return {"ok": False, "error": f"Refused: Klipper is {klippy}, not ready. Nothing sent."}

        if job_state in {"printing", "paused"}:
            return {"ok": False, "error": f"Refused: printer is {job_state}. Nothing sent."}

        if job_state not in {"standby", "complete", "cancelled", "error"}:
            return {"ok": False, "error": f"Refused: printer state is {job_state}. Nothing sent."}

        if sensor is not True:
            return {
                "ok": False,
                "error": "Refused: no filament at the head sensor. Load filament on the screen first. Nothing sent.",
            }

        def default_gcode_sender(script: str) -> None:
            call("/printer/gcode/script", method="POST", data={"script": script}, timeout=300)  # heat-up from cold exceeds 60 s

        send = gcode_sender or default_gcode_sender
        gcode_error: str | None = None

        try:
            send(f"M104 S{clamped_temp}")
            send(f"TEMPERATURE_WAIT SENSOR=extruder MINIMUM={clamped_temp - 5}")
            send("M83")
            remaining = clamped_mm
            while remaining > 0:
                chunk = min(10, remaining)
                send(f"G1 E{chunk} F150")
                remaining -= chunk
        except Exception as exc:
            gcode_error = str(exc)
        finally:
            try:
                send("M104 S0")
            except Exception as exc:
                if not gcode_error:
                    gcode_error = f"Failed to reset heater: {exc}"

        if reader:
            after = reader()
        else:
            after = ((call("/printer/objects/query?extruder").get("result") or {}).get("status") or {})

        extruder = after.get("extruder") or {}
        target = extruder.get("target")

        if gcode_error:
            return {"ok": False, "error": gcode_error, "extruder_target": target}

        if target != 0:
            return {
                "ok": False,
                "error": f"Purge command finished, but heater target readback is {target!r}, not 0. Check the printer; do not retry.",
                "extruder_target": target,
            }

        return {
            "ok": True,
            "mm": clamped_mm,
            "temp": clamped_temp,
            "extruder_target": target,
            "message": f"Purged {clamped_mm} mm at {clamped_temp}°C; heater target is 0.",
        }
    finally:
        pc._CONTROL_LOCK.release()


def _nozzle_ceiling_refusal(temp, adapter=None, state=None):
    """Why ``temp`` is above the configured printer's nozzle ceiling, or None."""
    from print_handy import printer_control
    if adapter is None:
        from print_handy import printers
        adapter = printers.current()
    control = printer_control.for_printer(adapter=adapter)
    # No state given: the configured printer's hardware record (None when unreadable).
    return control.refusal("nozzle", temp, state=control.hardware_record() if state is None else state)


def get_status(*, caller=None) -> dict[str, Any]:
    """Read-only JSON of state, klippy, temperatures, and head sensor."""
    call = caller or pc._call
    try:
        data = call("/printer/objects/query?print_stats&webhooks&extruder&heater_bed&filament_switch_sensor%20head_switch_sensor")
        st = (data.get("result") or {}).get("status") or {}
    except Exception as exc:
        try:
            info = (call("/server/info").get("result") or {})
            klippy = str(info.get("klippy_state") or "unknown").casefold()
        except Exception:
            klippy = "unknown"
        return {
            "ok": False,
            "state": "unknown",
            "klippy": klippy,
            "temperatures": {},
            "head_sensor": {},
            "error": str(exc),
        }

    ps = st.get("print_stats") or {}
    wh = st.get("webhooks") or {}
    ext = st.get("extruder") or {}
    bed = st.get("heater_bed") or {}
    head_switch = st.get("filament_switch_sensor head_switch_sensor") or {}

    return {
        "ok": True,
        "state": str(ps.get("state") or "unknown").casefold(),
        "klippy": str(wh.get("state") or "unknown").casefold(),
        "temperatures": {
            "extruder": {
                "temperature": ext.get("temperature"),
                "target": ext.get("target"),
            },
            "heater_bed": {
                "temperature": bed.get("temperature"),
                "target": bed.get("target"),
            },
        },
        "head_sensor": {
            "filament_detected": head_switch.get("filament_detected"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="No-LLM printer panic controls")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # force-restart
    p_restart = subparsers.add_parser("force-restart", help="Unconditional emergency stop and restart ladder")
    p_restart.add_argument("printer_id", nargs="?", default=None,
                           help="roster printer id to restart (default: the default printer)")
    p_restart.add_argument("--printer", dest="printer_flag", default=None, help="same as PRINTER_ID")

    # purge
    p_purge = subparsers.add_parser("purge", help="Guarded extrusion of filament at the toolhead")
    p_purge.add_argument("--slot", type=int, default=None, help="IFS slot number (unsupported in native screen mode)")
    p_purge.add_argument("--mm", type=int, default=60, help="Millimeters of filament to extrude (max 150, default 60)")
    p_purge.add_argument("--temp", type=int, default=210,
                         help="Extruder temperature in C (at least 180, at most the printer's nozzle ceiling; default 210)")

    # status
    subparsers.add_parser("status", help="Read-only JSON of state, klippy, temperatures, and head sensor")

    args = parser.parse_args(argv)
    from print_handy import printer_control

    if args.command == "force-restart":
        if args.printer_id and args.printer_flag and args.printer_id != args.printer_flag:
            print(json.dumps({"ok": False, "error": "Refused: two different printer ids given. Nothing sent."}))
            return 2
        try:
            res = printer_control.for_printer(args.printer_id or args.printer_flag).emergency_stop()
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": f"Refused: {exc}. Nothing sent."}))
            return 2
        print(json.dumps(res))
        return 0 if res.get("ok") else 1
    elif args.command == "purge":
        res = printer_control.for_printer().purge(slot=args.slot, mm=args.mm, temp=args.temp)
        print(json.dumps(res))
        return 0 if res.get("ok") else 1
    elif args.command == "status":
        res = get_status()
        print(json.dumps(res))
        return 0 if res.get("ok") else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
