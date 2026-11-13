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


# Moonraker answers a print resume or cancel only once the printer's macro has finished, and
# on an AD5X that can take longer than the 15 s request timeout (2026-11-08: the reply timed
# out, the printer went paused -> printing 10 s later). A reply that times out after the
# request went out is not a failure: the printer may be acting on it, so the state read-back
# decides, over this longer window.
NO_REPLY_WAIT_S = 90.0


def post_print_command(send, path: str, **kw) -> bool:
    """POST one print command through ``send``. True when it answered; False when the request
    went out but the reply timed out. Errors before the request was sent still raise.

    urllib wraps a failure to connect or send in URLError; only the read of the reply raises a
    bare TimeoutError, so that is the one case read as "sent, waiting for the printer".
    """
    try:
        send(path, method="POST", timeout=15, **kw)
    except TimeoutError:
        return False
    return True


def read_back(read, done, wait_s: float, poll_s: float = 1.0):
    """Re-read until ``done(reading)`` or ``wait_s`` passes. The last good reading; None when none.

    The read-back after one print command: a read that raises is skipped, never retried as a send.
    """
    deadline = time.monotonic() + wait_s
    last = None
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        try:
            last = read()
        except Exception:
            continue
        if done(last):
            break
    return last

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


# A print start still on its way to the printer (#B 2026-11-05: Stop 24 s after "Print started"
# said "Nothing to stop: printer is standby", and the print began 40 s later). A start claim
# counts for this long; a held launch lease counts while launch_state calls it in flight.
START_IN_FLIGHT_S = 600.0
# After "stop it when it starts", how long to wait for the printer to report the print.
PENDING_START_WAIT_S = 600.0
STOP_START_LABEL = "Stop it when it starts"
LET_START_LABEL = "Let it print"


def start_in_flight(printer_id: str | None = None, st: dict[str, Any] | None = None,
                    now: float | None = None, max_age_s: float | None = START_IN_FLIGHT_S) -> dict[str, Any] | None:
    """The print start under way on this printer while it is not yet printing, or None.

    {"file": the uploaded file name ("" when not known yet)}. Evidence is the printer's
    launch lease (checks, upload, start sent), or a recent start claim while the printer
    still reads standby. Read only; never raises. ``max_age_s=None`` accepts any unexpired claim.
    """
    if st is not None and st.get("state") in ACTIVE:
        return None
    try:
        from ..core import launch_state
        row = launch_state.in_flight(printer_id, now=now)
    except Exception:
        row = None
    if row is not None:
        return {"file": str(row.get("remote") or "")}
    if st is not None and st.get("state") != "standby":
        return None  # a finished, cancelled or failed job is not a start on its way
    try:
        from . import start_claims
        now = time.time() if now is None else now
        claims = []
        for name in start_claims.claimed_start_filenames(now):
            owner = start_claims.claim_printer(name)
            if printer_id and owner and owner != printer_id:
                continue  # another printer's start (#1380)
            ts = start_claims.start_claim_time(name, now)
            if ts is not None and (max_age_s is None or now - ts <= max_age_s):
                claims.append((ts, name))
    except Exception:
        return None
    return {"file": str(max(claims)[1])} if claims else None


def start_label(filename: str) -> str:
    """The plain print name for a start file, as the start card shows it; "a print" when unknown."""
    if not filename:
        return "a print"
    try:
        from . import discord_print_summary as summary
        return summary.format_event("started", filename, emit=False).splitlines()[0].split(": ", 1)[-1]
    except Exception:
        from pathlib import PurePosixPath
        return PurePosixPath(filename).name


def nothing_to_stop_text(st: dict[str, Any], start: dict[str, Any] | None = None) -> str:
    """The reply when the printer is not printing; names a start in flight instead of 'nothing'."""
    if start is not None:
        return (f"Nothing is printing yet, but {start_label(start.get('file') or '')} is starting "
                f"(the printer reads {st.get('state')}). To stop it, type /print-stop and choose "
                f"{STOP_START_LABEL}. Nothing sent.")
    return f"Nothing to stop: printer is {st.get('state')}. Nothing sent."


def stop_pending_start(start_file: str, printer_id: str | None = None,
                       wait_s: float = PENDING_START_WAIT_S, poll_s: float = 2.0) -> tuple[bool, str]:
    """Wait for a start in flight to become a print, then stop it with one cancel. (sent, text).

    Nothing is sent before the printer reports the print. A different file printing is
    refused; a start that goes away (no lease, no claim) or never prints says so.
    """
    from ..core.launch_state import same_remote
    kw = {"printer_id": printer_id} if printer_id else {}
    name = start_label(start_file)
    deadline = time.monotonic() + wait_s
    st: dict[str, Any] | None = None
    while True:
        try:
            st = status(**kw)
        except Exception:
            st = None
        if st is not None and st.get("state") in ACTIVE:
            running = str(st.get("filename") or "")
            if start_file and running != start_file and not same_remote(running, start_file):
                return False, "A different print is running now than the start you asked to stop. Nothing sent."
            sent, text = cancel(running, **kw)
            return sent, (f"{name} started, so it was stopped right away. {text}" if sent else text)
        if st is not None and start_in_flight(printer_id, st, max_age_s=None) is None:
            return False, f"{name} did not start, so there was nothing to stop. Nothing sent."
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_s)
    state = (st or {}).get("state") or "unknown"
    return False, (f"{name} has not started printing after {wait_s / 60:.0f} min (the printer reads "
                   f"{state}). Nothing sent. If it starts, stop it on the printer screen or type /print-stop again.")

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
        return False, nothing_to_stop_text(before, start_in_flight(printer_id, before))
    if expected_filename and before["filename"] != expected_filename:
        return False, "A different print is running now than the one you confirmed. Nothing sent."
    adapter = _lan_adapter() if printer_id is None else _lan_adapter(printer_id)
    replied = True
    if adapter is not None:
        adapter.control("stop")  # one LAN MQTT stop; the printer answers on the next snapshot
    else:
        try:
            from . import klipper_report
            klipper_report.mark_bot_cancel(printer_id=printer_id)
        except Exception:
            pass
        # Moonraker holds the reply until the cancel macro finishes; a reply that times out
        # after the request went out means "sent, waiting for the printer", not failure.
        replied = post_print_command(_send, "/printer/print/cancel", printer_id=printer_id)
        if not replied:
            wait_s = max(wait_s, NO_REPLY_WAIT_S)
    after = read_back(lambda: status(printer_id), lambda st: st["state"] not in ACTIVE,
                      wait_s, poll_s) or before
    if after["state"] in ACTIVE:
        if not replied:
            return True, (f"Cancel sent. The printer has not answered and still reports {after['state']} "
                          f"after {wait_s:.0f}s. Check the printer screen. Not retrying.")
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
