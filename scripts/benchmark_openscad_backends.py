#!/usr/bin/env python3
"""Offline OpenSCAD CGAL/Manifold comparison harness for #1069.

Produces measured time/RSS and artifact hashes, NOT a correctness or GPU
speedup verdict. Run on the actual target host with pinned tool binaries.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as data:
        for block in iter(lambda: data.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def build_command(time_binary: str, tool: str, backend: str | None,
                  source: Path, dest: Path, rss_path: Path) -> list[str]:
    command = [time_binary, "-f", "%M", "-o", str(rss_path), tool]
    if backend:
        command.append("--backend=" + backend)
    return [*command, "-o", str(dest), str(source)]


def run_once(time_binary: str, tool: str, backend: str | None, source: Path,
             output: Path, rss_path: Path, timeout: int) -> dict:
    command = build_command(time_binary, tool, backend, source, output, rss_path)
    start = time.perf_counter()
    try:
        process = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        returncode = process.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        returncode = None
        timed_out = True
    elapsed = time.perf_counter() - start
    try:
        peak_kib = int(rss_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        peak_kib = None
    exists = output.is_file() and output.stat().st_size > 0
    return {
        "exit_code": returncode, "timeout": timed_out, "wall_seconds": round(elapsed, 6),
        "peak_rss_kib": peak_kib, "output_bytes": output.stat().st_size if exists else None,
        "output_sha256": digest(output) if exists else None,
        "passed": returncode == 0 and exists and not timed_out,
    }


def summarize(runs: list[dict]) -> dict:
    valid = [r for r in runs if r["passed"]]
    return {
        "passed": len(valid) == len(runs) and len(runs) > 0,
        "successful_repeats": len(valid),
        "total_repeats": len(runs),
        "wall_median_s": round(statistics.median(r["wall_seconds"] for r in valid), 6) if valid else None,
        "rss_peak_kib": max((r["peak_rss_kib"] for r in valid if r["peak_rss_kib"] is not None), default=None),
        "runs": runs,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True,
                        help="The pinned OpenSCAD binary (2021.01, no backend argument)")
    parser.add_argument("--candidate", type=Path, required=True,
                        help="The Manifold-capable OpenSCAD executable")
    parser.add_argument("--scad", type=Path, action="append", required=True,
                        help="Input .scad, may repeat for multiple workloads")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--gnu-time", default="/usr/bin/time")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.repeats <= 10 or not 1 <= args.timeout <= 3600:
        parser.error("repeats must be 1..10 and timeout 1..3600 seconds")
    for p in [args.baseline, args.candidate, *args.scad]:
        if not p.is_file():
            parser.error(f"missing binary or case: {p}")
    if not Path(args.gnu_time).is_file():
        parser.error("GNU time is required to measure peak RSS on Linux")
    cases = {}
    passed = True
    for src in args.scad:
        measurements = {}
        for name, binary, backend in [
            ("pinned-2021", args.baseline, None),
            ("candidate-cgal", args.candidate, "cgal"),
            ("candidate-manifold", args.candidate, "manifold"),
        ]:
            runs = []
            with tempfile.TemporaryDirectory(prefix="ph-scad-bench-") as temp:
                for i in range(args.repeats):
                    output = Path(temp) / f"output-{i}.stl"
                    rss = Path(temp) / f"rss-{i}.txt"
                    runs.append(run_once(args.gnu_time, str(binary), backend, src, output, rss, args.timeout))
            summary = summarize(runs)
            passed &= summary["passed"]
            measurements[name] = summary
        cases[src.name] = measurements
    report = {
        "schema": "print-handy-openscad-benchmark-v1",
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "source": "#1069",
        "units": {"time": "seconds", "peak_rss": "KiB"},
        "repeats": args.repeats,
        "tool_identity": {
            "baseline_sha256": digest(args.baseline),
            "candidate_sha256": digest(args.candidate),
        },
        "cases": cases,
        "all_commands_succeeded": bool(passed),
        "correctness": "NOT VERIFIED. Compare solid volume, bounds, openings, warnings and hardwarnings separately.",
        "gpu_speedup": "NOT CLAIMED. OpenSCAD STL rendering is a CPU workload.",
    }
    if args.out.exists():
        parser.error("refusing to overwrite an existing benchmark report")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Recorded {len(cases)} case(s) at {args.out}. Correctness NOT verified.")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
