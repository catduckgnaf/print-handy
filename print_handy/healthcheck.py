"""Listener process liveness, adapted from zaheria985's Docker contribution.

Source: 3bb0984250c20eb336d11ba1da06633950b45182 (MIT).
Separate files and host/process identity prevent another service's heartbeat
from satisfying this check. Printer connectivity is reported, not required:
restarting a healthy listener cannot fix an offline printer. No network probe.
"""
from __future__ import annotations

import json
import math
import os
import socket
import sys
import time
from pathlib import Path

from . import listeners, printers, settings

HEARTBEAT_S = 30

# The listener registry (print_handy.listeners, #299) is the one place that
# maps a vendor connection to its listener domain. Implemented domains
# (moonraker, bambulab_lan) run a listener: the container supervisor must
# start it and the container healthcheck must require its heartbeat. Every
# other domain is planned or none, so no listener runs for it.
LISTENER_CONNECTIONS = listeners.implemented_connections()

# Heartbeat identity of the first implemented listener domain, kept as the
# default for beat() and as the fallback expectation when the configured
# instance's domain cannot be resolved here. Each listener beats under its
# own domain's registered runtime (see expected_runtime).
RUNTIME = listeners.DOMAINS["moonraker"].runtime


def listener_connection(data=None, printer_id=None):
    """Vendor connection of a printer's model, e.g. 'moonraker'.

    Without `printer_id` this is the configured (default) printer, exactly
    as before #336; with one, it is that roster entry's model connection.
    """
    data = settings.current() if data is None else data
    if printer_id is not None:
        model = settings.by_id(printer_id, data)["model"]
    else:
        model = data["printer"]["model"]
    known = printers.MODELS.get(model)
    if known is None:
        raise settings.SettingsError(f"unknown printer model {model!r}")
    return known.vendor.connection


def listener_domain(data=None, printer_id=None):
    """Listener domain of a printer instance (print_handy.listeners).

    None when the connection has no registered domain; settings problems
    propagate as SettingsError, the gate's unresolved case (#256).
    """
    return listeners.domain_for_connection(listener_connection(data, printer_id))


def listener_required(data=None, printer_id=None):
    """True when the instance's listener domain is implemented."""
    domain = listener_domain(data, printer_id)
    return bool(domain and domain.implemented)


def expected_runtime(data=None, printer_id=None):
    """The heartbeat runtime identity the instance's listener beats under.

    The registry names one runtime per implemented domain (moonraker,
    bambulab_lan, ...); check() must expect the instance's domain's, not a
    single hard-coded one (#299 Stage B). Falls back to RUNTIME when the
    domain cannot be resolved in this process.
    """
    try:
        domain = listener_domain(data, printer_id)
    except Exception:
        domain = None
    return domain.runtime if domain is not None and domain.runtime else RUNTIME


def resolve_printer_id(data=None, printer_id=None):
    """The printer a heartbeat operation concerns (#336).

    An explicit `printer_id` wins; otherwise the listener instance this
    process serves (print_handy.listeners' instance context, set by the
    registry launcher); otherwise the roster's default printer. None when
    no roster can be resolved from `data` at all (unresolved configuration
    surfaces elsewhere; heartbeat paths then keep their legacy shape).
    """
    if printer_id is not None:
        return printer_id
    instance = listeners.instance_printer_id()
    if instance is not None:
        return instance
    try:
        return settings.default_printer_id(data)
    except Exception:
        return None


def heartbeat_path(data=None, printer_id=None):
    """One printer's heartbeat: <state>/printers/<id>/listener_health.json.

    The per-printer layout of Stage 2b (#334). When no printer can be
    resolved (unresolved configuration), the legacy root path is returned
    so the failure surfaces as a missing heartbeat, as before.
    """
    resolved = resolve_printer_id(data, printer_id)
    if resolved is None:
        return settings.state_dir(data) / "listener_health.json"
    return settings.printer_state_dir(resolved, data) / "listener_health.json"


def process_identity(pid):
    # Linux start ticks disambiguate PID reuse, including container PID 1.
    text = Path(f"/proc/{int(pid)}/stat").read_text()
    return text.rsplit(")", 1)[1].split()[19]


def beat(runtime=RUNTIME, data=None, printer_id=None, **details):
    resolved = resolve_printer_id(data, printer_id)
    path = heartbeat_path(data, resolved)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {**details, "runtime": runtime, "pid": os.getpid(),
              "host": socket.gethostname(), "time": time.time(),
              "process_start": process_identity(os.getpid())}
    if resolved is not None:
        record["printer_id"] = resolved
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, sort_keys=True))
    os.replace(tmp, path)
    return record


def check(data=None, now=None, printer_id=None):
    resolved = resolve_printer_id(data, printer_id)
    try:
        record = json.loads(heartbeat_path(data, resolved).read_text())
        age = (time.time() if now is None else now) - float(record["time"])
        if not math.isfinite(age) or age < -5 or age > 90:
            return False, "listener heartbeat stale or invalid"
        if record["runtime"] != expected_runtime(data, resolved) or record["host"] != socket.gethostname():
            return False, "heartbeat belongs to another service or container"
        if resolved is not None and record.get("printer_id") not in (None, resolved):
            return False, "heartbeat belongs to another printer"
        if int(record["pid"]) <= 0 or process_identity(record["pid"]) != record["process_start"]:
            return False, "listener process is gone or replaced"
        connected = record.get("printer_connected", record.get("moonraker_connected", False))
        return True, "listener event loop alive; printer_connected=" + str(connected)
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError, settings.SettingsError):
        return False, "listener heartbeat missing, unreadable or invalid"


def printer_reachability(data=None, printer_id=None, now=None):
    """(ok, reason) for whether a printer's listener reaches its printer.

    Read from the listener's own heartbeat only: no network probe and
    nothing sent to the printer. Kept apart from check() on purpose: a
    live listener with an offline printer is a healthy process (restarting
    it cannot fix the printer) but not a reachable printer, and the two
    must not mask each other. ok is None when reachability is unknown —
    no listener exists for the connection, or the listener itself is not
    healthy (then check()'s reason says why).
    """
    resolved = resolve_printer_id(data, printer_id)
    domain = listener_domain(data, resolved)
    if domain is None or not domain.implemented:
        return None, (f"unknown: no listener for connection "
                      f"{listener_connection(data, resolved)!r} reports it")
    alive, reason = check(data, now=now, printer_id=resolved)
    if not alive:
        return None, f"unknown: {reason}"
    try:
        record = json.loads(heartbeat_path(data, resolved).read_text())
    except (OSError, ValueError):
        return None, "unknown: listener heartbeat unreadable"
    connected = record.get("printer_connected", record.get("moonraker_connected"))
    if connected is None:
        return None, "unknown: the listener heartbeat does not report the printer connection"
    if connected:
        return True, "listener connected to the printer"
    return False, "listener running, printer not connected"


def listener_statuses(data=None):
    """Per-printer listener health over the roster (#336).

    {printer_id: {"required": bool, "ok": bool | None, "reason": str}} —
    a heartbeat is required exactly when that printer's domain is
    implemented; a printer whose domain has no listener carries ok=None
    and adds no health requirement. This is the aggregation the bot
    status, the Web UI and diagnostics share.
    """
    data = settings.current() if data is None else data
    statuses = {}
    for entry in settings.printers(data):
        printer_id = entry["id"]
        domain = listener_domain(data, printer_id)
        if domain is None or not domain.implemented:
            detail = "no listener" if domain is None or domain.availability == listeners.NONE \
                else "no listener yet"
            statuses[printer_id] = {
                "required": False, "ok": None,
                "reason": f"not required for connection "
                          f"{listener_connection(data, printer_id)!r} ({detail})"}
            continue
        ok, reason = check(data, printer_id=printer_id)
        statuses[printer_id] = {"required": True, "ok": ok, "reason": reason}
    return statuses


def main(argv=None):
    printer_id = None
    args = list(sys.argv[1:] if argv is None else argv)
    if "--printer" in args:
        index = args.index("--printer")
        if index + 1 >= len(args):
            print("FAIL  --printer requires a printer id")
            return 1
        printer_id = args[index + 1]
    ok, reason = check(printer_id=printer_id)
    print(f"{'ok' if ok else 'FAIL'}  {reason}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
