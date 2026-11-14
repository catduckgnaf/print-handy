"""The container supervisor's per-printer listener record (#1020).

``docker/supervise.sh`` restarts a printer's listener when it exits and
watches its heartbeat while it runs, but its restart counts lived only in
shell variables: nothing else in the container could see them, so the
diagnostics bundle could not report them and the bot could not tell a
listener that will be restarted again from one whose restart budget is
exhausted and will stay down.

The supervisor now records each restart (and the budget exhaustion) here,
one JSON file per printer under its state folder, through this module's
CLI::

    python3 -m print_handy.supervisor_state --printer <id> --restarts N \
        --budget B --window S [--status N] [--exhausted]

Readers (:func:`read`, :func:`read_all`) never raise: a missing or broken
record is simply absent, exactly like a missing heartbeat.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import settings

STATE_NAME = "supervisor_listener.json"


def path_for(printer_id, data=None) -> Path:
    """One printer's supervisor record: <state>/printers/<id>/supervisor_listener.json."""
    return settings.printer_state_dir(printer_id, data) / STATE_NAME


def write(printer_id, *, restarts, budget, window_s, exhausted=False,
          last_exit_status=None, now=None, data=None) -> Path:
    """Record the supervisor's restart state for one printer, atomically."""
    record = {
        "printer_id": printer_id,
        "restarts": int(restarts),
        "budget": int(budget),
        "window_s": int(window_s),
        "exhausted": bool(exhausted),
        "updated_at": time.time() if now is None else float(now),
    }
    if last_exit_status is not None:
        record["last_exit_status"] = int(last_exit_status)
    target = path_for(printer_id, data)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=1, sort_keys=True))
    os.replace(tmp, target)
    return target


def read(printer_id, data=None):
    """One printer's supervisor record, or None when there is none."""
    try:
        record = json.loads(path_for(printer_id, data).read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def read_all(data=None):
    """{printer_id: record} over the configured roster; gaps are absent."""
    data = settings.current() if data is None else data
    out = {}
    for entry in settings.printers(data):
        record = read(entry["id"], data)
        if record is not None:
            out[entry["id"]] = record
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Record the supervisor's listener restart state for one printer.")
    parser.add_argument("--printer", required=True, help="roster id of the printer")
    parser.add_argument("--restarts", required=True, type=int,
                        help="restarts used in the current window")
    parser.add_argument("--budget", required=True, type=int, help="the restart budget")
    parser.add_argument("--window", required=True, type=int, help="the budget window, seconds")
    parser.add_argument("--status", type=int, default=None,
                        help="the listener's last exit status, when it exited")
    parser.add_argument("--exhausted", action="store_true",
                        help="the restart budget is exhausted; the listener stays down")
    args = parser.parse_args(argv)
    try:
        target = write(args.printer, restarts=args.restarts, budget=args.budget,
                       window_s=args.window, exhausted=args.exhausted,
                       last_exit_status=args.status)
    except Exception as exc:  # the supervisor treats this as best-effort
        print(f"supervisor state not recorded for {args.printer}: {exc} "
              "[PH_SUPERVISOR_TRANSIENT]", file=sys.stderr)
        return 1
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
