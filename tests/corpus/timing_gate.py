"""Fail when the fake Discord flow got slower than its recorded baseline.

    python3 -m tests.corpus.timing_gate RESULTS.json [RESULTS.json ...] [--baseline FILE]
                                        [--summary FILE] [--warn-only]
    python3 -m tests.corpus.timing_gate RESULTS.json ... --record   # print a new baseline to review by hand

RESULTS.json comes from `python3 -m tests.corpus.time_corpus --json`. Several files are repeats of
the same corpus; the gate compares their per-case medians, never one sample. Each baseline entry is
keyed "case/transport" and holds the seconds measured for the whole flow (wall_s), for the first
Discord question (first_prompt_s) and for the bed fit check (fit_check_s). A run fails the gate when
a metric exceeds

    baseline * (1 + tolerance) + slack(baseline)

tolerance is the issue #734 "+10%" (0.10). slack is an allowance for runner noise that a percentage cannot
absorb on tiny flows, where a 0.07 s mock flow can read 0.12 s on a busy runner. It is relative with a small
floor, max(SLACK_FLOOR_S, SLACK_REL * baseline), so a long flow gets 10% slack and a short flow is not given
a flat half second. The floor (0.35 s) is the smallest value that passed unchanged code: eight green main runs
of the mock leg, gated against the checked-in baseline, needed up to 0.33 s of slack (one run read
assembly-2c-tight at 0.75 s against a 0.38 s baseline with no relevant code change), and 0.30 s already failed
two of 312 case/metric pairings. A floor below that makes the blocking leg flaky; lower it only after measuring.
A flow shorter than the floor therefore cannot be gated tighter than runner noise by this check alone: the
cumulative limit and the reason rule below are what keep a series of small raises honest.
`--absolute-slack` restores the old behavior, a flat baseline["slack_s"], for the reporting-only real-Orca leg
(its noise is measured in seconds, see timing_variance.py).
tests/README.md (Slicing-speed gate) records the measurements behind the numbers and the release-time update step.

A justified slowdown is not hidden by editing numbers silently: raise the entry and give it a
"reason" (what got slower and why that is acceptable). An entry whose numbers rose without a reason
is refused, and so is one whose reason is the previous raise's reason copied over: --record leaves the
reason empty and stores the old one as "previous_reason", so each raise needs fresh words. Missing cases,
new cases and failed flows are reported too.

Cumulative drift: every entry keeps "original", the first values ever recorded for the case (carried
forward by --record; seeded from "raised_from", the earliest value the file still has, for entries
recorded before this field existed). A median over

    original * (1 + CUMULATIVE_TOLERANCE) + slack(original)

fails the gate even when each individual raise was small and explained (+50%). Past the lower
CUMULATIVE_WARN_TOLERANCE (+25%) the gate only reports a "timing drift" warning and a summary note, so the
trend is visible before it blocks. Every summary and every --record proposal also prints the cumulative
drift of every raised entry, however small, so a raise never hides its own total (#1142). The blocking limit is +50% with real headroom: no checked-in entry stands past the +25% warning
once the slack floor is applied, and the largest wall drift on a case over a second is
parts-512-four-color at +10% against its first recording (4.74 s, recorded 2026-11-03; 5.35 s at the
2.2.5 re-record as the fit probes moved ahead of the first prompt, since eased to 5.20 s). An earlier
file carried a "raised_from" of 3.77 s for that case that no recorded baseline in its history supports,
which printed as +38% drift; the #1142 review corrected the entry to its first recording and stored
"original" explicitly on every entry, so the seed the drift is measured from cannot be silently
replaced by a later re-record again.
Raising the baseline past the blocking limit means the slowdown is bigger than the process allows: fix it,
or change the limit in this file in a reviewed change that says why.

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
DEFAULT_SLACK_S = 0.5  # legacy flat slack, used only with --absolute-slack
SLACK_FLOOR_S = 0.35
SLACK_REL = 0.10
CUMULATIVE_TOLERANCE = 0.50
CUMULATIVE_WARN_TOLERANCE = 0.25
MIN_REASON_CHARS = 20


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


def slack_for(base, baseline, absolute=False):
    """Noise allowance for a metric whose recorded value is ``base``."""
    if absolute:
        return limits(baseline)[1]
    floor = float(baseline.get("slack_floor_s", SLACK_FLOOR_S))
    return max(floor, float(baseline.get("slack_rel", SLACK_REL)) * base)


def original_of(entry):
    """First values on record for a case: ``original``, else the oldest value the file still has."""
    return entry.get("original") or entry.get("raised_from") or {}


def raise_problem(k, entry):
    """Why a raised entry is refused (no reason, or the previous raise's reason copied), else None."""
    if not entry.get("raised_from"):
        return None
    reason = (entry.get("reason") or "").strip()
    if len(reason) < MIN_REASON_CHARS:
        return f"{k}: baseline was raised without a reason"
    if reason == (entry.get("previous_reason") or "").strip():
        return f"{k}: baseline was raised with the previous raise's reason copied over; say what changed this time"
    return None


def drift_notes(rows, baseline):
    """Cases past the warning tolerance of their first recording but not past the blocking one."""
    notes = []
    cases = baseline.get("cases", {})
    for row in rows:
        entry = cases.get(key(row))
        if entry is None or row.get("result") in ("FAIL", "ERROR", "XPASS"):
            continue
        for metric in METRICS:
            first, got = original_of(entry).get(metric), row.get(metric)
            if first is None or got is None:
                continue
            warn = first * (1 + CUMULATIVE_WARN_TOLERANCE) + slack_for(first, baseline)
            hard = first * (1 + CUMULATIVE_TOLERANCE) + slack_for(first, baseline)
            if warn < got <= hard:
                notes.append(f"{key(row)}: {metric} {got:.2f}s is {(got - first) / first:+.0%} over the first "
                             f"recorded {first:.2f}s (warns past {warn:.2f}s, fails past {hard:.2f}s)")
    return notes


def drift_lines(baseline):
    """Every case/metric whose recorded baseline sits above its first recording, with the drift.

    Printed in every gate summary and on every --record proposal (stderr there, so the proposal
    on stdout stays pure JSON for the CI redirect), not only past the warning tolerance: a raise
    is exactly where the cumulative drift has to be visible (#1142)."""
    lines = []
    for k, entry in sorted(baseline.get("cases", {}).items()):
        first_all = original_of(entry)
        for metric in METRICS:
            base, first = entry.get(metric), first_all.get(metric)
            if base is None or first is None or not first or base <= first:
                continue
            lines.append(f"{k}: {metric} baseline {base:.2f}s vs first recorded {first:.2f}s "
                         f"({(base - first) / first:+.0%} cumulative, blocking past "
                         f"+{CUMULATIVE_TOLERANCE:.0%})")
    return lines


def check(rows, baseline, absolute_slack=False):
    """Return a list of problems; empty means the gate passes."""
    tol = limits(baseline)[0]
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
            slack = slack_for(limit_base, baseline, absolute_slack)
            limit = limit_base * (1 + tol) + slack
            if got > limit:
                problems.append(f"{k}: {metric} {got:.2f}s > limit {limit:.2f}s "
                                f"(baseline {limit_base:.2f}s, +{tol:.0%} +{slack:.2f}s)")
            first = original_of(entry).get(metric)
            if first is not None and not absolute_slack:
                cumulative = first * (1 + CUMULATIVE_TOLERANCE) + slack_for(first, baseline)
                if got > cumulative:
                    problems.append(f"{k}: {metric} {got:.2f}s > cumulative limit {cumulative:.2f}s "
                                    f"(first recorded {first:.2f}s, +{CUMULATIVE_TOLERANCE:.0%} over all raises)")
        raised = raise_problem(k, entry)
        if raised:
            problems.append(raised)
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
            # A raise needs its own reason: the old one explained the old raise. It is kept only so the
            # gate can tell that the new reason was not just copied over it.
            entry["reason"] = ""
            entry["previous_reason"] = prev.get("reason", "")
        # The cumulative baseline: first values ever recorded, never overwritten by a later recording.
        first = dict(prev.get("original") or {}) if prev else {}
        seed = (prev.get("raised_from") or {}) if prev else {}
        for m in METRICS:
            if m in entry and m not in first:
                first[m] = seed.get(m, prev.get(m, entry[m])) if prev else entry[m]
        entry["original"] = {m: first[m] for m in METRICS if m in first}
        out["cases"][k] = entry
    return out


def summary(rows, baseline, problems, title="Timing gate", absolute_slack=False, notes=()):
    """Markdown: one line per case/transport with the median, its baseline and its limit."""
    tol = limits(baseline)[0]
    cases = baseline.get("cases", {})
    slack_text = (f"{limits(baseline)[1]:g} s" if absolute_slack
                  else f"max({baseline.get('slack_floor_s', SLACK_FLOOR_S):g} s, "
                       f"{baseline.get('slack_rel', SLACK_REL):.0%} of the baseline)")
    lines = [f"### {title}", "",
             f"Limit = baseline x (1 + {tol:.0%}) + {slack_text}, compared on the median of "
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
            limit = base * (1 + tol) + slack_for(base, baseline, absolute_slack)
            change = f"{(got - base) / base:+.0%}" if base else "-"
            flag = " OVER" if got > limit else ""
            lines.append(f"| {key(row)} | {row.get('result')} | {metric} | {got:.2f} | {base:.2f} | {limit:.2f} | "
                         f"{change}{flag} |")
    if notes:
        lines += ["", "Timing drift (warning, over the first recorded value):", ""] + [f"- {n}" for n in notes]
    drift = drift_lines(baseline)
    if drift:
        lines += ["", "Cumulative drift from the first recording (every raised entry):", ""] + \
                 [f"- {d}" for d in drift]
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
    ap.add_argument("--absolute-slack", action="store_true",
                    help="use the baseline's flat slack_s instead of the relative slack with a floor "
                         "(the reporting-only real-Orca leg, whose noise is measured in seconds)")
    ap.add_argument("--warn-only", action="store_true",
                    help="report problems as GitHub warning annotations and exit 0 (reporting-only leg)")
    args = ap.parse_args(argv)
    rows = median_rows([json.loads(Path(p).read_text())["rows"] for p in args.results])
    path = Path(args.baseline)
    baseline = json.loads(path.read_text()) if path.exists() else {}
    if args.record:
        proposed = record(rows, baseline, args.recorded_from)
        print(json.dumps(proposed, indent=1, sort_keys=True))
        # The reviewer of a re-record sees the cumulative drift of every raised entry with the
        # proposal, on stderr so stdout stays the pure JSON the CI step redirects to a file.
        for line in drift_lines(proposed):
            print(f"cumulative drift: {line}", file=sys.stderr)
        return 0
    problems = check(rows, baseline, args.absolute_slack)
    notes = [] if args.absolute_slack else drift_notes(rows, baseline)
    for n in notes:
        print(f"::warning title=Timing drift::{n}")
    for p in problems:
        print(f"::warning title=Timing (reporting only)::{p}" if args.warn_only else f"TIMING GATE: {p}")
    print(f"timing gate: {len(rows)} runs, {len(problems)} problems" + (" (warn-only)" if args.warn_only else ""))
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(summary(rows, baseline, problems, args.title, args.absolute_slack, notes))
    return 0 if args.warn_only or not problems else 1


if __name__ == "__main__":
    sys.exit(main())
