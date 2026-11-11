"""Stop the current print with one Moonraker cancel, or one Bambu LAN stop. No agent, no retries.

Used by the ``/print-stop`` Discord command in the print-feedback plugin and by the
listener's paused alert. Reads print_stats, sends at most one /printer/print/cancel,
then reports the state Moonraker actually shows afterwards, and the heater targets it
reads back (#1147): a heater left on after the stop is said plainly, never hidden
behind "Stopped". Nothing is sent to turn it off; the operator decides.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

from . import _config

BASE = _config.moonraker_url()
ACTIVE = ("printing", "paused")
STOP_LABEL = "Stop print now"
# After a stop, how long the heater targets may take to reach 0 (a cancel macro
# turns them off as it finishes) before a heater still set is reported (#1147).
HEATER_SETTLE_S = 10.0
KEEP_LABEL = "Keep printing"


def _base_for(printer_id: str | None) -> str:
    """The Moonraker base URL for a roster printer (#335); the configured one when printer_id is None."""
    if not printer_id:
        return BASE
    from print_handy import settings
    return str(settings.view_for(printer_id)["services"]["moonraker_url"])


def _call(path: str, method: str = "GET", timeout: float = 10, base: str | None = None) -> dict[str, Any]:
    req = urllib.request.Request(_config.require(BASE if base is None else base, "services.moonraker_url") + path, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def _send(path: str, method: str = "GET", timeout: float = 10, printer_id: str | None = None) -> dict[str, Any]:
    """_call against one printer's Moonraker; the no-printer call shape is unchanged."""
    if printer_id:
        return _call(path, method=method, timeout=timeout, base=_base_for(printer_id))
    return _call(path, method=method, timeout=timeout)


def _lan_adapter(printer_id: str | None = None):
    """A printer's adapter when it talks Bambu Lab LAN MQTT; None for Moonraker printers (#276).

    Without a printer_id this is the configured printer, exactly as before;
    with one it is that roster entry's adapter, bound to its own settings view.
    """
    from print_handy import printers
    if not printer_id:
        adapter = printers.current()
    else:
        from print_handy import settings
        adapter = printers.get(settings.by_id(printer_id)["model"], settings.view_for(printer_id))
    return adapter if getattr(adapter, "transport", None) == "bambulab_lan" else None


NO_CONTROL = "not connected"  # the status state of a printer Print Handy has no control connection to


def _family(printer_id: str | None = None):
    """(control, adapter) for a printer that is neither Moonraker nor Bambu LAN.

    OctoPrint, PrusaLink, Elegoo SDCP and stock Flashforge stop through their own adapter's
    control (family_control.FamilyControl, the path the alert buttons and the Web UI already use);
    Moonraker URLs mean nothing to them. (None, adapter) for a printer with no control connection
    at all (offline); (None, None) for Moonraker, which keeps the path below. The printer is the one
    the caller chose (printer_id), or the configured printer.
    """
    from . import family_control
    try:
        adapter = family_control.adapter_for(printer_id)
    except Exception:
        if printer_id:
            raise
        return None, None  # no adapter resolvable for the configured printer: the Moonraker path, as before
    connection = family_control.connection_of(adapter)
    if connection in (None, "moonraker", "bambulab_lan"):
        return None, None
    if connection in family_control.FAMILY_CONNECTIONS:
        return family_control.FamilyControl(adapter), adapter
    return None, adapter


def _unconnected_text(adapter) -> str:
    label = getattr(adapter, "label", None) or "This printer"
    return (f"{label} has no live connection in Print Handy, so it cannot be stopped from here. "
            "Stop it on the printer itself. Nothing sent.")


def status(printer_id: str | None = None) -> dict[str, Any]:
    """state, filename, layer, total_layer, progress (0..1), print_duration."""
    adapter = _lan_adapter() if printer_id is None else _lan_adapter(printer_id)
    if adapter is None:
        control, family_adapter = _family(printer_id)
        if control is not None:
            live = control.status()
            return {"state": str(live.get("state") or "unknown"), "filename": live.get("filename") or "",
                    "layer": live.get("layer"), "total_layer": live.get("total_layer"),
                    "progress": live.get("progress"), "print_duration": live.get("print_duration")}
        if family_adapter is not None:
            return {"state": NO_CONTROL, "filename": "", "layer": None, "total_layer": None,
                    "progress": None, "print_duration": None}
    if adapter is not None:
        if not adapter.lan_configured():
            raise RuntimeError("Bambu LAN status is not configured ([bambu] lan_host and lan_serial plus the access code)")
        live = adapter.normalize_status(adapter.client().snapshot())
        st = {"state": str(live.get("state") or "unknown"), "filename": live.get("filename") or "",
              "layer": live.get("layer"), "total_layer": live.get("total_layer"),
              "progress": live.get("progress"), "print_duration": None}
        # Camera parity (#294): a status read also tries the printer's camera.
        # Best-effort — the outcome rides along as a saved frame path or a plain
        # note, and a camera problem never fails the status read itself.
        try:
            from . import bambu_watch
            path, note = bambu_watch.status_camera(adapter)
            st["camera_image_path"] = str(path) if path else None
            st["camera_note"] = note
        except Exception:
            st["camera_image_path"] = None
            st["camera_note"] = ""
        return st
    data = _send("/printer/objects/query?print_stats&virtual_sdcard=progress", printer_id=printer_id)
    s = (data.get("result") or {}).get("status") or {}
    ps = s.get("print_stats") or {}
    info = ps.get("info") or {}
    return {"state": str(ps.get("state") or "unknown"), "filename": ps.get("filename") or "",
            "layer": info.get("current_layer"), "total_layer": info.get("total_layer"),
            "progress": (s.get("virtual_sdcard") or {}).get("progress"),
            "print_duration": ps.get("print_duration")}


def _target(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def moonraker_heater_targets(printer_id: str | None = None) -> dict[str, float | None]:
    """{"nozzle": C, "bed": C} heater targets from one Moonraker read; None where unreadable."""
    data = _send("/printer/objects/query?extruder=target&heater_bed=target", printer_id=printer_id)
    s = (data.get("result") or {}).get("status") or {} if isinstance(data, dict) else {}
    s = s if isinstance(s, dict) else {}
    return {"nozzle": _target((s.get("extruder") or {}).get("target") if isinstance(s.get("extruder"), dict) else None),
            "bed": _target((s.get("heater_bed") or {}).get("target") if isinstance(s.get("heater_bed"), dict) else None)}


def status_heater_targets(st: dict[str, Any]) -> dict[str, float | None]:
    """The heater targets a family's normalised status carries ({} when it carries none)."""
    out = {}
    for key, label in (("hotend_target_c", "nozzle"), ("bed_target_c", "bed")):
        if key in (st or {}):
            out[label] = _target(st.get(key))
    return out


def heaters_after_stop(read, *, wait_s: float = HEATER_SETTLE_S, poll_s: float = 1.0,
                       sleep=None, clock=None) -> str:
    """Read heater targets back after a stop; the sentence to add to the reply ("" when all are 0).

    ``read`` returns {heater: target or None}. Targets are re-read for up to
    ``wait_s`` while one is still set (a cancel macro turns heaters off as it
    finishes). A heater still set is named with its target; targets that
    cannot be read are said to be unknown. Never sends anything.
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + wait_s
    targets: dict[str, float | None] = {}
    while True:
        try:
            targets = dict(read() or {})
        except Exception:
            targets = {"nozzle": None, "bed": None}
        on = {name: t for name, t in targets.items() if t is not None and t > 0}
        if not on and all(t is not None for t in targets.values()):
            return ""
        if clock() >= deadline:
            break
        sleep(min(poll_s, max(0.0, deadline - clock())))
    if on:
        named = " and ".join(f"the {name} heater is still set to {t:g} C" for name, t in on.items())
        return (f" But {named}: check the printer and turn it off there if it should be off. "
                "Print Handy did not send anything to change it.")
    return (" The heater targets could not be read back, so Print Handy cannot say the heaters "
            "are off. Check the printer.")


def where_text(st: dict[str, Any]) -> str:
    bits = []
    if st.get("layer") and st.get("total_layer"):
        bits.append(f"layer {st['layer']}/{st['total_layer']}")
    if isinstance(st.get("progress"), (int, float)):
        bits.append(f"{st['progress'] * 100:.0f}% done")
    return ", ".join(bits)


def cancel(expected_filename: str, wait_s: float = 20, poll_s: float = 1.0,
           printer_id: str | None = None) -> tuple[bool, str]:
    """One cancel, only if the same file is still printing or paused. Returns (sent, truthful text).

    Moonraker cancels with one /printer/print/cancel and Bambu with one LAN stop; every other family
    stops through its own adapter control (FamilyControl.cancel, the same discipline), and a printer
    with no control connection says so instead of erroring."""
    if (_lan_adapter() if printer_id is None else _lan_adapter(printer_id)) is None:
        control, family_adapter = _family(printer_id)
        if control is not None:
            return control.cancel(expected_filename, wait_s=wait_s, poll_s=poll_s)
        if family_adapter is not None:
            return False, _unconnected_text(family_adapter)
    before = status(printer_id)
    if before["state"] not in ACTIVE:
        return False, f"Nothing to stop: printer is {before['state']}. Nothing sent."
    if expected_filename and before["filename"] != expected_filename:
        return False, "A different print is running now than the one you confirmed. Nothing sent."
    adapter = _lan_adapter() if printer_id is None else _lan_adapter(printer_id)
    if adapter is not None:
        adapter.control("stop")  # one LAN MQTT stop; the printer answers on the next snapshot
    else:
        try:
            from . import klipper_report
            klipper_report.mark_bot_cancel()
        except Exception:
            pass
        _send("/printer/print/cancel", method="POST", timeout=15, printer_id=printer_id)
    deadline = time.monotonic() + wait_s
    after = before
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        try:
            after = status(printer_id)
        except Exception:
            continue
        if after["state"] not in ACTIVE:
            break
    if after["state"] in ACTIVE:
        return True, (f"Cancel sent, but the printer still reports {after['state']} after {wait_s:.0f}s. "
                      "Check the printer screen. Not retrying.")
    # Read the heater targets back (#1147) rather than assume the stop turned them off:
    # Moonraker from extruder/heater_bed, Bambu from its LAN snapshot's nozzle and bed targets.
    if adapter is None:
        heaters = heaters_after_stop(lambda: moonraker_heater_targets(printer_id), poll_s=poll_s)
    else:
        heaters = heaters_after_stop(
            lambda: status_heater_targets(adapter.normalize_status(adapter.client().snapshot())),
            poll_s=poll_s)
    if adapter is not None and after["state"] == "error":
        # A Bambu printer reports a stopped job as FAILED until the operator clears it on the screen.
        return True, ("Stopped. The printer shows the cancelled job as an error until you acknowledge it "
                      f"on its screen.{heaters}")
    return True, f"Stopped. Printer now reports {after['state']}.{heaters}"


def pause(expected_filename: str, wait_s: float = 20, poll_s: float = 1.0,
          printer_id: str | None = None) -> tuple[bool, str]:
    """One Bambu LAN pause, only if the same file is still printing. Returns (sent, truthful text).

    The watchdog alert's Pause button lands here (#294 stage 3): the press is
    the operator's, the state is re-read first, exactly one control("pause")
    is sent, and the result is what the next snapshots actually report —
    the same discipline as cancel(). Bambu only: the Moonraker pause path
    lives in print_control.
    """
    adapter = _lan_adapter() if printer_id is None else _lan_adapter(printer_id)
    if adapter is None:
        return False, "Pause is not available for this printer here. Nothing sent."
    before = status(printer_id)
    if before["state"] != "printing":
        return False, f"Nothing to pause: printer is {before['state']}. Nothing sent."
    if expected_filename and before["filename"] != expected_filename:
        return False, "A different print is running now than the one in this alert. Nothing sent."
    adapter.control("pause")  # one LAN MQTT pause; the printer answers on the next snapshot
    deadline = time.monotonic() + wait_s
    after = before
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        try:
            after = status(printer_id)
        except Exception:
            continue
        if after["state"] != "printing":
            break
    if after["state"] == "paused":
        return True, "Paused. Check the printer screen for the current state."
    if after["state"] == "printing":
        return True, (f"Pause sent, but the printer still reports printing after {wait_s:.0f}s. "
                      "Check the printer screen. Not retrying.")
    return True, f"Pause sent. Printer now reports {after['state']}."
