"""Run every corpus case through the fake Discord flow and report how long each one takes.

    python3 -m tests.corpus.time_corpus [--transport hermes|standalone|both] [--case NAME ...]
                                        [--json out.json] [--real-slicer]

For each case and transport it records pass/fail against case.toml, total wall time, and the
phase split from the run's timed event log: time to the first Discord question, time to the
slicer start, and time from slicer start to the end of the flow. Nothing reaches Discord,
Moonraker or a printer; the mock tier fakes the Orca CLI exactly as the corpus tests do.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

from . import harness

# Same pin as tests/conftest.py: the corpus fixtures carry literal temperatures from before the shipped
# #513 steel offsets took effect.
for _steel in ("HARDENED_STEEL", "STAINLESS_STEEL"):
    os.environ.setdefault(f"PRINT_HANDY__NOZZLE_TEMP_OFFSETS__{_steel}", "0")


def _phases(run):
    events = run.events
    times = getattr(events, "times", None)
    if not times:
        return {}
    t0 = events.t0

    def first(kind):
        return next((t - t0 for t, e in zip(times, events) if e[0] == kind), None)

    fit_s, opened = 0.0, None
    for t, e in zip(times, events):
        if e[0] == "fit_check_start":
            opened = t
        elif e[0] == "fit_check_end" and opened is not None:
            fit_s, opened = fit_s + (t - opened), None
    return {
        "fit_check_s": fit_s if any(e[0] == "fit_check_end" for e in events) else None,
        "first_prompt_s": first("prompt"),
        "audit_start_s": first("audit_start"),
        "slice_start_s": first("slice_start"),
        "slice_s": first("slice"),
        "prompts": sum(1 for e in events if e[0] == "prompt"),
        "end_s": times[-1] - t0 if times else None,
    }


def _known_failure(case):
    return case.data.get("known_failure")


def run_one(case, transport, real_slicer, root):
    work = Path(tempfile.mkdtemp(prefix=f"{case.name}-{transport}-", dir=root))
    row = {"case": case.name, "transport": transport, "kind": case.model.suffix.lower(),
           "model_bytes": case.model.stat().st_size}
    start = time.monotonic()
    try:
        run = harness.run_case(case, work, real_slicer=real_slicer, transport=transport)
        row["wall_s"] = time.monotonic() - start
        row.update(_phases(run))
        try:
            harness.check_expectations(case, run)
            row["result"] = "XPASS" if _known_failure(case) else "PASS"
        except AssertionError as exc:
            row["result"] = "XFAIL" if _known_failure(case) else "FAIL"
            row["error"] = str(exc).splitlines()[0][:300] if str(exc) else "AssertionError"
    except Exception as exc:  # the flow itself raised
        row["wall_s"] = time.monotonic() - start
        row["result"] = "XFAIL" if _known_failure(case) else "ERROR"
        row["error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:300] if str(exc) else ''}"
        row["trace"] = traceback.format_exc()[-1500:]
    return row


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", choices=("hermes", "standalone", "both"), default="both")
    ap.add_argument("--case", action="append", default=[])
    ap.add_argument("--json")
    ap.add_argument("--real-slicer", action="store_true")
    args = ap.parse_args(argv)

    harness.install_clarify_stand_in()
    # Same isolation as tests/corpus/conftest.py: never write live printer state, use home slicer commands.
    from print_handy import settings
    from print_handy.core import orca_adapter, printer_state
    printer_state.save = lambda state: None
    home = settings.load(local=False, environ={})
    orca_adapter.ORCA_COMMAND = list(home["orca"]["command"])
    orca_adapter.BAMBU_COMMAND = list(home["bambu"]["command"])

    folders = harness.case_dirs()
    cases = [harness.load_case(f) for f in folders]
    if args.case:
        cases = [c for c in cases if c.name in set(args.case)]
    transports = ("hermes", "standalone") if args.transport == "both" else (args.transport,)
    root = Path(tempfile.mkdtemp(prefix="ph-corpus-timing-"))
    rows = []
    for case in cases:
        for transport in transports:
            row = run_one(case, transport, args.real_slicer, root)
            rows.append(row)
            fmt = lambda v: "   -  " if v is None else f"{v:6.2f}"  # noqa: E731
            print(f"{row['result']:5} {row['wall_s']:7.2f}s  prompt {fmt(row.get('first_prompt_s'))}  "
                  f"slice_start {fmt(row.get('slice_start_s'))}  q={row.get('prompts', '-'):>2}  "
                  f"{transport:10} {case.name}" + (f"  !! {row['error']}" if row.get("error") else ""),
                  flush=True)
    total = sum(r["wall_s"] for r in rows)
    bad = [r for r in rows if r["result"] in ("FAIL", "ERROR", "XPASS")]
    print(f"\n{len(rows)} runs, {len(cases)} cases, total {total:.1f}s, "
          f"slowest {max(rows, key=lambda r: r['wall_s'])['case'] if rows else '-'}, failures {len(bad)}")
    if args.json:
        Path(args.json).write_text(json.dumps({"python": sys.version.split()[0], "real_slicer": args.real_slicer,
                                               "cpu_count": os.cpu_count(), "rows": rows}, indent=1))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
