"""Measure the real-Orca timing leg's runner noise from CI artifacts, before anyone makes it blocking.

    gh run download <run-id> -n slicer-timing-real -D artifacts/<run-id>      # for each green main run
    python3 -m tests.corpus.timing_variance artifacts/* --baseline tests/corpus/timing_baseline_real_slicer.json \
        --source "..." > tests/corpus/timing_variance_real_slicer.json

Each directory is one CI run's slicer-timing-real artifact (run-1.json .. run-3.json). Every run is reduced to
its per-case medians exactly as timing_gate does, then:

- per case/metric spread across runs (min, median, p95, max, coefficient of variation);
- the checked-in baseline gated against every run (how many unchanged-code runs it would fail);
- leave one out: each run recorded as the baseline in turn (the release step's --record) and every other run
  gated against it, for a grid of tolerance and slack_s. A threshold is safe to make blocking only when it has
  zero failing pairings, because each pairing is a release baseline meeting a later run of unchanged code.

The output is the reviewed record tests/corpus/test_timing_gate.py reads: the real leg may drop --warn-only and
continue-on-error only if this record shows zero failing pairings at the baseline's own tolerance and slack_s.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from .timing_gate import METRICS, check, key, median_rows, record

TOLERANCES = (0.10, 0.15, 0.20)
SLACKS = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)


def load_runs(dirs):
    """{run dir name: per-case median rows}, skipping a directory with fewer than 3 repeats or a failed flow."""
    runs = {}
    for d in map(Path, dirs):
        files = sorted(d.glob("run-*.json"))
        if len(files) < 3:
            continue
        rows = median_rows([json.loads(f.read_text())["rows"] for f in files])
        if all(r.get("result") in ("PASS", "XFAIL") for r in rows):
            runs[d.name] = rows
    return runs


def spread(runs):
    by = {}
    for rows in runs.values():
        for r in rows:
            for m in METRICS:
                if r.get(m) is not None:
                    by.setdefault(f"{key(r)} {m}", []).append(r[m])
    out = {}
    for name, v in sorted(by.items()):
        v = sorted(v)
        mean = statistics.mean(v)
        out[name] = {"n": len(v), "min": round(v[0], 3), "median": round(statistics.median(v), 3),
                     "p95": round(v[int(0.95 * (len(v) - 1))], 3), "max": round(v[-1], 3),
                     "cv": round(statistics.pstdev(v) / mean, 3) if mean else 0.0}
    return out


def leave_one_out(runs, tol, slack):
    """(failing pairings, total pairings, worst median minus its limit in seconds)."""
    fail = pairs = 0
    worst = float("-inf")
    for a, base_rows in runs.items():
        base = record(base_rows, {"tolerance": tol, "slack_s": slack})
        for b, rows in runs.items():
            if a == b:
                continue
            pairs += 1
            fail += bool(check(rows, base))
            for r in rows:
                entry = base["cases"].get(key(r), {})
                for m in METRICS:
                    if entry.get(m) is not None and r.get(m) is not None:
                        worst = max(worst, r[m] - (entry[m] * (1 + tol) + slack))
    return fail, pairs, round(worst, 2)


def measure(runs, baseline, source=""):
    tol, slack = float(baseline.get("tolerance", 0.10)), float(baseline.get("slack_s", 1.0))
    grid = []
    for t in TOLERANCES:
        for s in SLACKS:
            fail, pairs, worst = leave_one_out(runs, t, s)
            grid.append({"tolerance": t, "slack_s": s, "failing_pairings": fail, "pairings": pairs,
                         "worst_over_limit_s": worst})
    at_baseline = next((g for g in grid if g["tolerance"] == tol and g["slack_s"] == slack), None)
    if at_baseline is None:
        fail, pairs, worst = leave_one_out(runs, tol, slack)
        at_baseline = {"tolerance": tol, "slack_s": slack, "failing_pairings": fail, "pairings": pairs,
                       "worst_over_limit_s": worst}
    return {
        "_doc": "Runner noise of ci.yml slicer-timing-real, written by python3 -m tests.corpus.timing_variance "
                "from green main CI artifacts and reviewed by hand. test_timing_gate.py keeps the real-Orca leg "
                "reporting-only unless leave_one_out_at_baseline shows zero failing pairings.",
        "source": source,
        "runs": len(runs),
        "baseline_limits": {"tolerance": tol, "slack_s": slack},
        "checked_in_baseline_failing_runs": sum(bool(check(rows, baseline)) for rows in runs.values()),
        "leave_one_out_at_baseline": at_baseline,
        "leave_one_out_grid": grid,
        "spread": spread(runs),
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+", help="one directory per CI run, each holding run-1.json .. run-3.json")
    ap.add_argument("--baseline", default=str(Path(__file__).with_name("timing_baseline_real_slicer.json")))
    ap.add_argument("--source", default="", help="which runs were measured (run id range, branch, dates)")
    args = ap.parse_args(argv)
    runs = load_runs(args.dirs)
    if len(runs) < 2:
        print("need at least two usable runs", file=sys.stderr)
        return 2
    baseline = json.loads(Path(args.baseline).read_text())
    print(json.dumps(measure(runs, baseline, args.source), indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
