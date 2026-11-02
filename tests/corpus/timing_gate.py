"""Fail when the fake Discord flow got slower than its recorded baseline.

    python3 -m tests.corpus.timing_gate RESULTS.json [RESULTS.json ...] [--baseline FILE]
                                        [--summary FILE] [--warn-only]
    python3 -m tests.corpus.timing_gate RESULTS.json ... --record   # print a new baseline to review by hand

RESULTS.json comes from `python3 -m tests.corpus.time_corpus --json`. Several files are repeats of
the same corpus; the gate compares their per-case medians, never one sample. Each baseline entry is
keyed "case/transport" and holds the seconds measured for the whole flow (wall_s), for the first
Discord question (first_prompt_s) and for the bed fit check (fit_check_s). A run fails the gate when
a metric exceeds

    baseline * (1 + tolerance) + slack_s

tolerance is the issue #734 "+10%" (0.10). slack_s is an absolute allowance for runner noise that a
percentage cannot absorb on tiny flows, where a 0.07 s mock flow can read 0.12 s on a busy runner.
tests/README.md (Slicing-speed gate) records the measurements behind both numbers and the release-time update step.

A justified slowdown is not hidden by editing numbers silently: raise the entry and give it a
"reason" (what got slower and why that is acceptable). An entry whose numbers rose without a reason
is refused. Missing cases, new cases and failed flows are reported too.

--summary appends a per-case markdown table (median, baseline, limit) to FILE, for example
$GITHUB_STEP_SUMMARY. --warn-only prints problems as GitHub warning annotations and exits 0: the
real-slicer leg uses it while its noise is being measured (start as a warning, promote later).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_BASELINE = Path(__file__).with_name("timing_baseline.json")
METRICS = ("wall_s", "first_prompt_s", "fit_check_s")
# Used when a baseline file does not set its own. See tests/README.md (Slicing-speed gate) for the measurements.
DEFAULT_TOLERANCE = 0.10
DEFAULT_SLACK_S = 0.5


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
            if metric == "fit_check_s" and len(values) != len(group):
                row[metric] = None  # one omitted repeat must not be hidden by the median
            elif values:
                row[metric] = statistics.median(values)
        out.append(row)
    return out


def limits(baseline):
    """(tolerance, slack_s) of a baseline file, with the module defaults for a file that omits them."""
    return float(baseline.get("tolerance", DEFAULT_TOLERANCE)), float(baseline.get("slack_s", DEFAULT_SLACK_S))


def check(rows, baseline):
    """Return a list of problems; empty means the gate passes."""
    tol, slack = limits(baseline)
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
        # A case listed as fit-check-required must report a measurement; omission is not a pass.
        if k in baseline.get("fit_check_required", []) and row.get("fit_check_s") is None:
            problems.append(f"{k}: fit_check_s was not measured but this case must run the bed fit check")
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


def record(rows, old=None, recorded_from=""):
    """A proposed baseline from a run. Entries that got slower keep raised_from and need a reason.

    recorded_from names the measurement (the CI run and commit whose artifact was recorded), so a
    reviewer can open the run the numbers came from. It is never carried over from the old file."""
    old = old or {}
    tol, slack = limits(old)
    out = {"tolerance": tol, "slack_s": slack, "cases": {}, "recorded_from": recorded_from}
    if "description" in old:
        out["description"] = old["description"]
    if "fit_check_budget_s" in old:
        out["fit_check_budget_s"] = old["fit_check_budget_s"]
    if "fit_check_required" in old:
        out["fit_check_required"] = old["fit_check_required"]
    for row in rows:
        if row.get("result") not in ("PASS", "XFAIL"):
            continue
        k = key(row)
        entry = {m: round(row[m], 2) for m in METRICS if row.get(m) is not None}
        prev = old.get("cases", {}).get(k)
        # A metric the old entry never had (a newly measured fit check) is not a raise.
        if prev and any(m in prev and m in entry and entry[m] > prev[m] for m in METRICS):
            entry["raised_from"] = {m: prev.get(m) for m in METRICS if m in prev}
            entry["reason"] = prev.get("reason", "")
        out["cases"][k] = entry
    return out


def summary(rows, baseline, problems, title="Timing gate"):
    """Markdown: one line per case/transport with the median, its baseline and its limit."""
    tol, slack = limits(baseline)
    cases = baseline.get("cases", {})
    lines = [f"### {title}", "",
             f"Limit = baseline x (1 + {tol:.0%}) + {slack:g} s, compared on the median of "
             f"{max((r.get('repeats', 1) for r in rows), default=0)} repeats. "
             f"{len(problems)} problem(s).", "",
             "| case/transport | result | metric | median s | baseline s | limit s | vs baseline |",
             "|---|---|---|---|---|---|---|"]
    for row in sorted(rows, key=key):
        entry = cases.get(key(row), {})
        for metric in METRICS:
            got, base = row.get(metric), entry.get(metric)
            if got is None:
                continue
            if base is None:
                lines.append(f"| {key(row)} | {row.get('result')} | {metric} | {got:.2f} | - | - | no baseline |")
                continue
            limit = base * (1 + tol) + slack
            change = f"{(got - base) / base:+.0%}" if base else "-"
            flag = " OVER" if got > limit else ""
            lines.append(f"| {key(row)} | {row.get('result')} | {metric} | {got:.2f} | {base:.2f} | {limit:.2f} | "
                         f"{change}{flag} |")
    if problems:
        lines += ["", "Problems:", ""] + [f"- {p}" for p in problems]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+", help="one or more time_corpus JSON files; medians are compared")
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--recorded-from", default="",
                    help="with --record: the CI run and commit the results came from (stored in the baseline)")
    ap.add_argument("--summary", help="append a per-case markdown table to this file (e.g. $GITHUB_STEP_SUMMARY)")
    ap.add_argument("--title", default="Timing gate", help="heading of the --summary table")
    ap.add_argument("--warn-only", action="store_true",
                    help="report problems as GitHub warning annotations and exit 0 (reporting-only leg)")
    args = ap.parse_args(argv)
    rows = median_rows([json.loads(Path(p).read_text())["rows"] for p in args.results])
    path = Path(args.baseline)
    baseline = json.loads(path.read_text()) if path.exists() else {}
    if args.record:
        print(json.dumps(record(rows, baseline, args.recorded_from), indent=1, sort_keys=True))
        return 0
    problems = check(rows, baseline)
    for p in problems:
        print(f"::warning title=Timing (reporting only)::{p}" if args.warn_only else f"TIMING GATE: {p}")
    print(f"timing gate: {len(rows)} runs, {len(problems)} problems" + (" (warn-only)" if args.warn_only else ""))
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(summary(rows, baseline, problems, args.title))
    return 0 if args.warn_only or not problems else 1


if __name__ == "__main__":
    sys.exit(main())
