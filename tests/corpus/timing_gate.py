"""Fail when the fake Discord flow got slower than its recorded baseline.

    python3 -m tests.corpus.timing_gate RESULTS.json [--baseline tests/corpus/timing_baseline.json]
    python3 -m tests.corpus.timing_gate RESULTS.json --record   # print a new baseline to review by hand

RESULTS.json comes from `python3 -m tests.corpus.time_corpus --json`. Each baseline entry is keyed
"case/transport" and holds the seconds measured for the whole flow (wall_s) and for the first
Discord question (first_prompt_s). A run fails the gate when a metric exceeds

    baseline * (1 + tolerance) + slack_s

Tolerance absorbs runner noise on big models; slack_s absorbs it on tiny ones, where a 0.3 s flow
can double from a cold import. A justified slowdown is not hidden by editing numbers silently:
raise the entry and give it a "reason" (what got slower and why that is acceptable). An entry whose
numbers rose without a reason is refused. Missing cases, new cases and failed flows are reported too.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_BASELINE = Path(__file__).with_name("timing_baseline.json")
METRICS = ("wall_s", "first_prompt_s", "fit_check_s")


def key(row):
    return f"{row['case']}/{row['transport']}"


def median_rows(runs):
    """Merge repeated runs: per case/transport, the median of each metric; any failed repeat wins.

    One sample is not a measurement on a shared runner (a single 0.2 s flow was seen at 8.5 s while
    another job held the CPU), so the gate compares medians of at least three repeats."""
    import statistics
    merged = {}
    for rows in runs:
        for row in rows:
            merged.setdefault(key(row), []).append(row)
    out = []
    for k, group in merged.items():
        bad = next((r for r in group if r.get("result") in ("FAIL", "ERROR", "XPASS")), None)
        row = dict(bad or group[0], repeats=len(group))
        for metric in METRICS:
            values = [r[metric] for r in group if r.get(metric) is not None]
            if values:
                row[metric] = statistics.median(values)
        out.append(row)
    return out


def check(rows, baseline):
    """Return a list of problems; empty means the gate passes."""
    tol = float(baseline.get("tolerance", 0.25))
    slack = float(baseline.get("slack_s", 1.0))
    cases = baseline.get("cases", {})
    seen, problems = set(), []
    for row in rows:
        k = key(row)
        seen.add(k)
        if row.get("result") in ("FAIL", "ERROR", "XPASS"):
            problems.append(f"{k}: flow result {row.get('result')} ({row.get('error', '')})")
            continue
        # Separate absolute ceiling on the bed fit check, enforced even for a case with no baseline.
        budget = baseline.get("fit_check_budget_s")
        if budget is not None and row.get("fit_check_s") is not None and row["fit_check_s"] > float(budget):
            problems.append(f"{k}: fit_check_s {row['fit_check_s']:.2f}s > fit-check budget {float(budget):.2f}s")
        entry = cases.get(k)
        if entry is None:
            problems.append(f"{k}: no baseline; record one with --record and review it")
            continue
        for metric in METRICS:
            limit_base, got = entry.get(metric), row.get(metric)
            if limit_base is None or got is None:
                continue
            limit = limit_base * (1 + tol) + slack
            if got > limit:
                problems.append(f"{k}: {metric} {got:.2f}s > limit {limit:.2f}s "
                                f"(baseline {limit_base:.2f}s, +{tol:.0%} +{slack:.1f}s)")
        if entry.get("raised_from") and not entry.get("reason"):
            problems.append(f"{k}: baseline was raised without a reason")
    for k in sorted(set(cases) - seen):
        problems.append(f"{k}: in the baseline but was not run")
    return problems


def record(rows, old=None):
    """A proposed baseline from a run. Entries that got slower keep raised_from and need a reason."""
    old = old or {}
    out = {"tolerance": old.get("tolerance", 0.25), "slack_s": old.get("slack_s", 1.0), "cases": {}}
    if "fit_check_budget_s" in old:
        out["fit_check_budget_s"] = old["fit_check_budget_s"]
    for row in rows:
        if row.get("result") not in ("PASS", "XFAIL"):
            continue
        k = key(row)
        entry = {m: round(row[m], 2) for m in METRICS if row.get(m) is not None}
        prev = old.get("cases", {}).get(k)
        if prev and any(entry.get(m, 0) > prev.get(m, 0) for m in METRICS):
            entry["raised_from"] = {m: prev.get(m) for m in METRICS if m in prev}
            entry["reason"] = prev.get("reason", "")
        out["cases"][k] = entry
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+", help="one or more time_corpus JSON files; medians are compared")
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    ap.add_argument("--record", action="store_true")
    args = ap.parse_args(argv)
    rows = median_rows([json.loads(Path(p).read_text())["rows"] for p in args.results])
    path = Path(args.baseline)
    baseline = json.loads(path.read_text()) if path.exists() else {}
    if args.record:
        print(json.dumps(record(rows, baseline), indent=1, sort_keys=True))
        return 0
    problems = check(rows, baseline)
    for p in problems:
        print("TIMING GATE:", p)
    print(f"timing gate: {len(rows)} runs, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
