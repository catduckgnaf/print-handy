"""Guarded, idle-only IFS slot loading for AD5X running ZMod Klipper.

Wraps documented ZMod macros for physical slot engagement without automatic
invocation in standard slicing or print flows.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from . import print_control as pc

logger = logging.getLogger(__name__)


def load_slot(
    slot: int | str,
    *,
    caller: Callable[..., dict[str, Any]] | None = None,
    reader: Callable[[], dict[str, Any]] | None = None,
    gcode_sender: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Guarded idle slot load using ZMod macros.

    Refuses unless printer is idle (standby, complete, cancelled, error) and
    Klipper is ready. Holds pc._CONTROL_LOCK.

    Refuses when the display mode is unknown: only an explicit integer
    ``display_off`` of 0 or 1 is known. A missing or unrecognized value
    fails closed before any macro is sent, matching how the rest of the
    readiness code treats unknown modes, because the two modes use
    different macro sequences and an assumed mode would move filament
    the wrong way.

    Under screenless mode (display_off=1), executes:
        IFS_F11
        IFS_F10
        SET_EXTRUDER_SLOT SLOT={slot}

    Under native screen mode (display_off=0), executes:
        REMOVE_PRUTOK_IFS
        INSERT_PRUTOK_IFS
        SET_EXTRUDER_SLOT SLOT={slot}
    """
    try:
        slot_int = int(slot)
        if slot_int not in (1, 2, 3, 4):
            return {
                "ok": False,
                "error": f"Refused: slot must be an integer from 1 through 4 (got {slot!r}). Nothing sent.",
            }
    except (ValueError, TypeError):
        return {
            "ok": False,
            "error": f"Refused: slot must be an integer from 1 through 4 (got {slot!r}). Nothing sent.",
        }

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
            data = call("/printer/objects/query?print_stats&webhooks&save_variables")
            st = (data.get("result") or {}).get("status") or {}

        job_state = str((st.get("print_stats") or {}).get("state") or "unknown").casefold()
        klippy = str((st.get("webhooks") or {}).get("state") or "unknown").casefold()
        variables = (st.get("save_variables") or {}).get("variables") or {}
        display_off = variables.get("display_off")

        if type(display_off) is not int or display_off not in (0, 1):
            return {
                "ok": False,
                "error": (
                    f"Refused: printer display mode is unknown "
                    f"(display_off={display_off!r}); slot loading needs an explicit "
                    f"native-screen (0) or screenless (1) mode. Nothing sent."
                ),
            }

        if job_state in {"printing", "paused"}:
            return {
                "ok": False,
                "error": f"Refused: cannot load slot while printer is {job_state}. Nothing sent.",
            }

        if job_state not in {"standby", "complete", "cancelled", "error"}:
            return {
                "ok": False,
                "error": f"Refused: printer state is {job_state}. Nothing sent.",
            }

        if klippy != "ready":
            return {
                "ok": False,
                "error": f"Refused: Klipper is {klippy}, not ready. Nothing sent.",
            }

        if display_off == 1:
            script = f"IFS_F11\nIFS_F10\nSET_EXTRUDER_SLOT SLOT={slot_int}"
        else:
            script = f"REMOVE_PRUTOK_IFS\nINSERT_PRUTOK_IFS\nSET_EXTRUDER_SLOT SLOT={slot_int}"

        if gcode_sender:
            gcode_sender(script)
        else:
            call("/printer/gcode/script", method="POST", data={"script": script}, timeout=30)

        return {
            "ok": True,
            "slot": slot_int,
            "display_off": display_off,
            "script": script,
            "message": f"Slot {slot_int} load sequence sent (display_off={display_off}).",
        }

    except Exception as exc:
        logger.exception("Error executing slot load")
        return {
            "ok": False,
            "error": "The slot load did not run. Check the printer state on its screen before trying again.",
        }
    finally:
        pc._CONTROL_LOCK.release()
