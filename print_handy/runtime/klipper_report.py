"""Full Klipper report for a cancelled print, posted to #3d-printer.

Read only: Moonraker object queries, the console history (gcode_store), and the tail of klippy.log.
Nothing here sends a command to the printer.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import _config

BASE = _config.moonraker_url()
BOT_CANCEL = _config.state_file("PRINT_BOT_CANCEL_MARKER", "last_bot_cancel.json")
BOT_WINDOW_S = 120
QUERY = ("/printer/objects/query?webhooks&print_stats&virtual_sdcard=progress&extruder=temperature,target"
         "&heater_bed=temperature,target&toolhead=position,homed_axes&gcode_move=gcode_position")
NOISE = re.compile(r"^(ok|B:|T:|// Klipper state: Ready|echo:)", re.I)
# UI prompt traffic, not diagnostics (2026-11-05): ZMod/Klipper action prompts,
# RESPOND TYPE=command relays and the color-prompt macros a cancel replays.
PROMPT_NOISE = re.compile(
    r"action:prompt_|RESPOND\s+TYPE=command|\bMSG=\"?action:|\b(?:RUN_ZCOLOR|CHANGE_ZCOLOR|ZCOLOR\w*)\b"
    r"|^Reset colors\b|^echo\b|^M117\b", re.I)
# Lines that are really a warning or an error. Startup chatter (ADC_MAX, BUS_PINS_*, heater
# checks, remote method registration) never matches, so it is never shown as a warning.
ALARM = re.compile(r"^!!|\b(?:error|warning|exception|traceback)\b|shutdown|must home|timer too close|"
                   r"lost communication|nozzle hit|part detachment|thermal runaway|not heating at expected rate|"
                   r"adc out of range|heater \w+ not heating|move out of range|runout|"
                   r"\bfailed\b|\babort", re.I)
# A Klipper restart starts a new log session; what follows is the new boot's config dump.
RESTART = re.compile(r"^(?:Start printer at|Starting Klippy|=+ Config file =+|Restarting printer|"
                     r"Klippy logging started|Args: \[)", re.I)
STOP_MARK = re.compile(r"Transition to shutdown state|^!!|Exiting SD card print|cancel_print|CANCEL_PRINT", re.I)


def is_stop_mark(line: str) -> bool:
    """True if line records a genuine runtime stop event, excluding config declarations."""
    if line.startswith((" ", "\t")):
        return False
    s = line.strip()
    if not s or STARTUP.match(s) or s.startswith(("[", "#", ";")):
        return False
    if re.match(r"^[a-zA-Z0-9_]+\s*[:=]", s):
        return False
    return bool(STOP_MARK.search(s))
# The boot-time config dump and registration chatter: never a warning, whatever words it holds.
STARTUP = re.compile(r"^(?:MCU '[^']*' config:|Loaded MCU|Configured MCU|Starting heater checks|"
                     r"webhooks: registering|\[[^\]]+\]$|(?-i:[a-z_0-9]+ = )|#|Stats |Receive:|Sent |"
                     r"BUS_PINS_|Extruder max_extrude_ratio|mcu_awake)", re.I)
NO_WARNINGS = "No printer warnings recorded before the stop."


def _url(path: str) -> str:
    return _config.require(BASE, "services.moonraker_url") + path


def _get(path: str, timeout: float = 10) -> Any:
    with urllib.request.urlopen(_url(path), timeout=timeout) as resp:
        raw = resp.read()
    return (json.loads(raw) if path.startswith(("/printer", "/server/gcode", "/server/history"))
            else raw.decode("utf-8", "replace"))


def _job_for(filename: str, job: dict[str, Any] | None) -> dict[str, Any] | None:
    """The finished history job for this print, or None.

    Moonraker writes the history record from print_stats as they were before
    the cancel macro reset them, so it keeps the real durations and filament
    after ZMod's cancel has zeroed the live print_stats and parked the head.
    A job still in progress, or one for a different file, is not this print.
    """
    if not isinstance(job, dict) or job.get("status") in (None, "in_progress"):
        return None
    name = str(job.get("filename") or "")
    want = str(filename or "")
    if want and name and name.rsplit("/", 1)[-1] != want.rsplit("/", 1)[-1]:
        return None
    return job


def mark_bot_cancel(now: float | None = None, printer_id: str | None = None) -> None:
    """Called by print_stop.cancel so the report can say who cancelled.

    The marker names the printer it was sent to (#1373 review): one install can
    run several printers, and a screen cancel on printer B within the window
    must not read as the bot's cancel of printer A.
    """
    try:
        BOT_CANCEL.parent.mkdir(parents=True, exist_ok=True)
        BOT_CANCEL.write_text(json.dumps({"at": time.time() if now is None else now,
                                          "printer_id": printer_id}))
    except OSError:
        pass


def cancelled_by_bot(now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        marker = json.loads(BOT_CANCEL.read_text())
        if not now - float(marker.get("at", 0)) < BOT_WINDOW_S:
            return False
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    from .. import instance
    mine, theirs = instance.printer_id(), marker.get("printer_id")
    # A marker for another printer is not this listener's cancel; an unnamed one
    # (a single-printer install, or an older marker) still counts.
    return not (mine and theirs and str(mine) != str(theirs))


def _hm(seconds: Any) -> str:
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return "unknown"
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s"


def _temp(obj: dict[str, Any]) -> str:
    t, tgt = obj.get("temperature"), obj.get("target")
    if t is None:
        return "unknown"
    return f"{t:.0f}°C" + (f" (target {tgt:.0f}°C)" if tgt else " (off)")


def prompt_noise(line: str) -> bool:
    """UI prompt traffic or echo chatter that says nothing about why a print stopped."""
    return bool(PROMPT_NOISE.search(line))


def console_lines(store: list[dict[str, Any]], since: float, limit: int = 8) -> list[str]:
    """Distinct, meaningful console lines since `since` (epoch), newest last.

    Drops the ok/temperature echo and UI prompt traffic (action:prompt_*, RESPOND
    TYPE=command relays, the ZMod color-prompt macros); keeps errors and real responses.
    """
    out: list[str] = []
    for entry in store:
        if float(entry.get("time") or 0) < since:
            continue
        for line in str(entry.get("message") or "").splitlines():
            line = line.lstrip("/! ").strip()
            if not line or NOISE.match(line) or prompt_noise(line) or line in out:
                continue
            out.append(line)
    return out[-limit:]


def log_sessions(text: str) -> list[list[str]]:
    """The klippy.log tail split at each Klipper restart, oldest first."""
    sessions: list[list[str]] = [[]]
    for line in text.splitlines()[1:]:  # first line of a ranged read is usually partial
        line = line.rstrip()
        if RESTART.match(line.strip()) and sessions[-1]:
            sessions.append([])
        sessions[-1].append(line)
    return [s for s in sessions if s]


def stop_session(text: str) -> list[str]:
    """Lines of the log session the stop happened in, cut at the stop line.

    A printer restarted after the stop has a newer session holding only its startup
    config dump: the newest session with a stop marker wins, else the newest session.
    """
    sessions = log_sessions(text)
    if not sessions:
        return []
    chosen = next((s for s in reversed(sessions) if any(is_stop_mark(x) for x in s)), None)
    if chosen is None:
        # No stop in the tail. A tail that is one fresh boot (the printer restarted and the log
        # rolled over) has nothing from before the stop.
        if RESTART.match(sessions[-1][0].strip()):
            return []
        chosen = sessions[-1]
    last = max((i for i, x in enumerate(chosen) if is_stop_mark(x)), default=len(chosen) - 1)
    # Klipper prints its shutdown dump right after the transition line; keep a few lines of it.
    return chosen[: last + 4]


def log_alarms(text: str, limit: int = 6) -> list[str]:
    """Real warning or error lines from before the stop, deduplicated, newest last."""
    seen: list[str] = []
    for line in stop_session(text):
        line = line.strip()
        if line and ALARM.search(line) and not prompt_noise(line) and not STARTUP.match(line):
            line = line[:220]
            if line in seen:
                seen.remove(line)
            seen.append(line)
    return seen[-limit:]


def build(filename: str, status: dict[str, Any], store: list[dict[str, Any]], log_tail: str,
          *, by_bot: bool, now: float | None = None, job: dict[str, Any] | None = None,
          stop_reason: str = "") -> str:
    from . import print_alert_buttons as pab
    from .. import printers
    now = time.time() if now is None else now
    live_ps = status.get("print_stats") or {}
    job = _job_for(filename, job)
    # After a macro reset (state "standby") the live counters read zero and the
    # toolhead sits at the park position, so neither describes the cancelled print.
    reset = live_ps.get("state") == "standby"
    ps = dict(live_ps)
    if job:
        for key in ("print_duration", "total_duration", "filament_used"):
            if job.get(key) is not None:
                ps[key] = job[key]
    wh = status.get("webhooks") or {}
    info = ps.get("info") or {}
    who = "by the print bot (/print-stop or a button)" if by_bot else "from the printer screen or another client"
    lines = [f"**Print cancelled {who}.** {printers.current().display_name} report:"]
    if stop_reason:
        lines.append("**Stop reason:** " + stop_reason)
    where = []
    if info.get("current_layer") and info.get("total_layer"):
        where.append(f"layer {info['current_layer']}/{info['total_layer']}")
    prog = (status.get("virtual_sdcard") or {}).get("progress")
    if isinstance(prog, (int, float)) and prog:
        where.append(f"{prog * 100:.0f}% of the file")
    planned = ((job or {}).get("metadata") or {}).get("filament_total")
    if not where and isinstance(planned, (int, float)) and planned > 0 and ps.get("filament_used"):
        where.append(f"about {min(float(ps['filament_used']) / planned, 1) * 100:.0f}% of the planned filament")
    pos = (status.get("gcode_move") or {}).get("gcode_position") or (status.get("toolhead") or {}).get("position")
    if pos and len(pos) >= 3 and not reset:
        where.append(f"nozzle at Z={pos[2]:.2f} mm")
    lines.append("Stopped at: " + (", ".join(where) or "position unknown"))
    lines.append(f"Printed for {_hm(ps.get('print_duration'))} (job open {_hm(ps.get('total_duration'))}), "
                 f"{(ps.get('filament_used') or 0) / 1000:.2f} m filament used")
    lines.append(f"Nozzle {_temp(status.get('extruder') or {})}, bed {_temp(status.get('heater_bed') or {})}")
    klippy = wh.get("state") or "unknown"
    lines.append(f"{printers.current().firmware_name}: {klippy}" + (f", Moonraker job message: {ps['message']}" if ps.get("message") else ""))
    if klippy in ("shutdown", "error"):
        lines.append("**Why it stopped:** " + pab.explain(wh.get("state_message") or stop_reason or ""))
        lines.append(f"{printers.current().firmware_name} is still in an emergency stop. It needs one firmware restart (/print-reboot) "
                     "before it will accept anything else.")
    since = now - max(float(ps.get("total_duration") or 0), 600) - 60
    console = console_lines(store, since)
    if console:
        lines.append("Last console messages:\n```\n" + "\n".join(console) + "\n```")
    alarms = log_alarms(log_tail)
    if alarms:
        lines.append("Printer warnings before the stop (klippy.log):\n```\n" + "\n".join(alarms) + "\n```")
    elif not console and klippy == "ready" and not stop_reason:
        lines.append("No errors in the console or klippy.log. Looks like a plain manual cancel.")
    else:
        lines.append(NO_WARNINGS)
    text = "\n".join(lines)
    return text if len(text) <= 1990 else text[:1950].rsplit("\n", 1)[0] + "\n(report trimmed)\n```"


def report(filename: str, stop_reason: str = "") -> str:
    """Collect everything live and build the text. Each source fails independently."""
    status: dict[str, Any] = {}
    store: list[dict[str, Any]] = []
    tail = ""
    try:
        status = (_get(QUERY).get("result") or {}).get("status") or {}
    except Exception:
        pass
    job: dict[str, Any] | None = None
    try:
        jobs = (_get("/server/history/list?limit=1&order=desc").get("result") or {}).get("jobs") or []
        job = jobs[0] if jobs else None
    except Exception:
        pass
    try:
        store = (_get("/server/gcode_store?count=100").get("result") or {}).get("gcode_store") or []
    except Exception:
        pass
    try:
        req = urllib.request.Request(_url("/server/files/klippy.log"), headers={"Range": "bytes=-20000"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            tail = resp.read().decode("utf-8", "replace")
    except Exception:
        pass
    text = build(filename, status, store, tail, by_bot=cancelled_by_bot(), job=job, stop_reason=stop_reason)
    if not status:
        text += "\n(Couldn't read live printer state; report is partial.)"
    return text
