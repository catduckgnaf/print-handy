#!/usr/bin/env python3
"""Nightly print log catch up. Moonraker job history is the source of truth.

Any finished Moonraker job in the lookback window with no matching print
record gets one written from history (start, end, duration, filament,
outcome). A record matches when gcode_file is the same and started_at is
within 10 minutes. Records written before the JSON format (legacy Markdown
notes) still count as matches, so an upgrade never backfills a duplicate.
No AI agent, no Discord posts. Prints one line per action; exit 1 on failure.
"""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import moonraker_discord_announcements as ann
from .print_records import PRINTS_DIR

# Moonraker history status -> Klipper print_stats state used by build_record_data.
STATUS = {"completed": "complete", "cancelled": "cancelled"}  # everything else is an error
FIELD = re.compile(r'^(gcode_file|started_at):\s*"?([^"\n]*)"?\s*$', re.M)


def _started_epoch(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00").replace("z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, OSError):
        return None


def existing_records():
    records = []
    for path in PRINTS_DIR.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            records.append((Path(str(record.get("gcode_file") or "")).name,
                            _started_epoch(record.get("started_at"))))
    for path in PRINTS_DIR.glob("*.md"):
        head = path.read_text(encoding="utf-8", errors="replace").split("\n---", 1)[0]
        fields = dict(FIELD.findall(head))
        records.append((Path(fields.get("gcode_file", "")).name,
                        _started_epoch(fields.get("started_at"))))
    return records


def has_record(records, filename, start):
    name = Path(filename).name
    return any(n == name and (s is None or abs(s - start) <= 600) for n, s in records)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=48)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    since = datetime.now(timezone.utc).timestamp() - args.hours * 3600
    try:
        result = ann.get_json(f"/server/history/list?limit=100&order=desc&since={since:.0f}")
    except Exception as exc:
        print(f"print log: Moonraker history unavailable: {exc}")
        return 1
    jobs = (result.get("result") or {}).get("jobs") or []
    records = existing_records()
    failed = 0
    for job in jobs:
        status, filename, start, end = job.get("status"), job.get("filename"), job.get("start_time"), job.get("end_time")
        if not filename or not start or status in (None, "in_progress") or has_record(records, filename, start):
            continue
        if args.dry_run:
            print(f"print log: would add {filename} ({status}) started {iso(start)}")
            continue
        stats = {"state": STATUS.get(status, "error"), "print_duration": job.get("print_duration"),
                 "filament_used": job.get("filament_used")}
        ctx = ann.load_context(filename)
        ctx.setdefault("started_at", iso(start))
        try:
            data = ann.build_record_data(filename, stats=stats, metadata={}, context=ctx,
                                         completed_at=iso(end) if end else None)
            data["moonraker_state"] = status
            path = ann.write_completed_record(data).get("record_path")
            records.append((Path(filename).name, start))
            print(f"print log: added missing record for {filename} ({status}) -> {path}")
        except Exception as exc:
            failed += 1
            print(f"print log: could not write record for {filename} ({status}): {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
