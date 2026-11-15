"""Read TIMING.jsonl back: the waits a person actually feels (#1476).

Every job already writes ``TIMING.jsonl`` with named stages (see
:func:`print_handy.jobs.store._timing`); this module is the reader the
issue asked for. It scans the newest job folders in the archive,
computes median and p90 for the user-facing spans below, counts
``start_response_lost``, and renders the same report for the doctor,
the diagnostics bundle, a Web UI page and the release tooling.

Span definitions (stage names are the ones in the files; the mapping
is deliberately honest about what each pair of stages brackets):

``link_to_first_question``
    ``message_received`` -> ``first_question_posted``. The front of the
    flow: the intake took the message, then the first question went
    up. Both stages are new in #1476, so older jobs have no sample.
``last_answer_to_ready``
    ``intake_slice_requested`` -> ``intake_slice_returned``. The slice
    approval is the intake's last answer; the second stamp lands when
    the slice call returns and the job stands sliced and ready. When
    the speculative early slice already finished, this measures the
    remaining settle wait, which is what the person actually waited.
``slice``
    ``slice_start`` -> ``slice_done``; for a re-sliced job the LAST
    ``slice_start`` and the first ``slice_done`` after it (the attempt
    that produced the job's G-code).
``slice_verify``
    ``verify_begin`` -> ``verify_done`` (the post-slice verify inside
    the single-plate slice path; other slice paths emit no sample).
``launch_preflight``
    ``print_now_start`` -> ``preflight_verify_done``.
``tap_to_start_accepted``
    ``intake_start_tapped`` -> ``start_submitted``: the operator tapped
    Start, then the printer accepted the start command.
``tap_to_print_started``
    ``intake_start_tapped`` -> ``intake_start_returned``: print_now
    returned with the start outcome known (confirmed, pending or
    refused), which is when the intake posts the result. This is the
    "Print started" wait a person sees, fixed waits included.

Every other span pairs the FIRST start stamp with the first end stamp
after it. A job contributes at most one sample per span. Malformed
lines, partial files and unreadable folders are skipped, never fatal:
this is reporting over whatever an install actually recorded.

Budgets live in :data:`DEFAULT_BUDGETS` (seconds, median and p90 per
span) and :func:`check_budgets` is the check the hardware smoke and
proof tooling call. Movement between releases: :func:`build_report`
accepts a previous summary (the dict :func:`summary_dict` produces,
which the release checklist stores from a proof run) and flags a span
whose median moved by more than :data:`MOVEMENT_RELATIVE` (20%) and
more than :data:`MOVEMENT_ABSOLUTE_S` (0.5 s) at once, so neither a
0.1 s doubling nor a 100 s drift of a few percent raises a flag alone.

CLI: ``python3 -m print_handy.jobs.timing_report [--archive PATH]
[--jobs N] [--previous SUMMARY.json] [--summary-out PATH]
[--check-budgets]``. With ``--check-budgets`` the exit code is 1 when
a budget is exceeded and 2 when there is no timing data to check.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

SUMMARY_VERSION = 1
DEFAULT_JOBS = 100
MOVEMENT_RELATIVE = 0.20
MOVEMENT_ABSOLUTE_S = 0.5

# key -> (label, start stage, end stage, pairing)
SPANS = {
    "link_to_first_question": ("Link to first question", "message_received",
                               "first_question_posted", "first"),
    "last_answer_to_ready": ("Last answer to ready to slice", "intake_slice_requested",
                             "intake_slice_returned", "first"),
    "slice": ("Slice", "slice_start", "slice_done", "last_start"),
    "slice_verify": ("Post-slice verify", "verify_begin", "verify_done", "first"),
    "launch_preflight": ("Launch preflight", "print_now_start",
                         "preflight_verify_done", "first"),
    "tap_to_start_accepted": ("Tap to start accepted", "intake_start_tapped",
                              "start_submitted", "first"),
    "tap_to_print_started": ("Tap to print started", "intake_start_tapped",
                             "intake_start_returned", "first"),
}

# Seconds: (median budget, p90 budget) per span. Set from the real
# install numbers in #1476 (slice 11.7 s median / 63 s p90, verify
# 4.2 / 14, preflight 3.0 / 9.4, tap to print started 40.7 median) with
# headroom, so a budget trip means a real regression, not noise. The
# front-of-flow span has no production history yet; its budget is a
# plain statement of what "responsive" means there, to be tightened
# once proof runs record it.
DEFAULT_BUDGETS = {
    "link_to_first_question": (5.0, 15.0),
    "last_answer_to_ready": (20.0, 75.0),
    "slice": (20.0, 75.0),
    "slice_verify": (8.0, 20.0),
    "launch_preflight": (6.0, 15.0),
    "tap_to_start_accepted": (30.0, 60.0),
    "tap_to_print_started": (60.0, 120.0),
}


def read_timing_file(path):
    """One job's TIMING.jsonl: ({stage: sorted [t, ...]}, lost_count).

    Returns None when the file cannot be read or holds no valid line.
    A valid line is a JSON object with a string ``stage`` and a numeric
    ``t``; anything else is skipped, never fatal."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    stages = {}
    lost = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        stage, t = row.get("stage"), row.get("t")
        if not isinstance(stage, str) or not isinstance(t, (int, float)):
            continue
        stages.setdefault(stage, []).append(float(t))
        if stage == "start_response_lost":
            lost += 1
    if not stages:
        return None
    for times in stages.values():
        times.sort()
    return stages, lost


def _span_seconds(stages, start_stage, end_stage, pairing):
    """One job's seconds for one span, or None when it has no sample."""
    starts = stages.get(start_stage)
    ends = stages.get(end_stage)
    if not starts or not ends:
        return None
    start = starts[-1] if pairing == "last_start" else starts[0]
    for end in ends:
        if end >= start:
            return round(end - start, 3)
    return None


def job_spans(stages):
    """{span key: seconds} for one job's stage map (see read_timing_file)."""
    out = {}
    for key, (_label, start, end, pairing) in SPANS.items():
        seconds = _span_seconds(stages, start, end, pairing)
        if seconds is not None:
            out[key] = seconds
    return out


def _median(values):
    return round(statistics.median(values), 3) if values else None


def _p90(values):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)], 3)


def movement(previous_median, current_median):
    """The movement record for one span, or None when it did not move
    past the threshold (or either side has no median)."""
    if previous_median is None or current_median is None:
        return None
    delta = round(current_median - previous_median, 3)
    if previous_median > 0:
        delta_pct = round(delta / previous_median, 4)
    else:
        delta_pct = 0.0 if delta == 0 else math.inf
    if abs(delta) > MOVEMENT_ABSOLUTE_S and abs(delta_pct) > MOVEMENT_RELATIVE:
        return {"previous_median": previous_median, "delta_s": delta,
                "delta_pct": delta_pct,
                "direction": "slower" if delta > 0 else "faster"}
    return None


def build_report(archive, limit=DEFAULT_JOBS, previous=None, budgets=None):
    """The timing report over the newest ``limit`` jobs in ``archive``.

    ``previous`` is a stored summary dict (see summary_dict) to flag
    median movement against; ``budgets`` overrides DEFAULT_BUDGETS.
    Never raises on a missing or unreadable archive: the report simply
    has no samples."""
    budgets = DEFAULT_BUDGETS if budgets is None else budgets
    samples = {key: [] for key in SPANS}
    lost_occurrences = 0
    lost_jobs = 0
    jobs_seen = 0
    jobs_used = 0
    try:
        files = list(Path(archive).glob("*/TIMING.jsonl"))
    except OSError:
        files = []
    dated = []
    for path in files:
        try:
            dated.append((path.stat().st_mtime, path))
        except OSError:
            continue
    dated.sort(key=lambda item: item[0], reverse=True)
    for _mtime, path in dated[:limit]:
        jobs_seen += 1
        parsed = read_timing_file(path)
        if parsed is None:
            continue
        stages, lost = parsed
        jobs_used += 1
        if lost:
            lost_occurrences += lost
            lost_jobs += 1
        for key, seconds in job_spans(stages).items():
            samples[key].append(seconds)
    previous_spans = (previous or {}).get("spans") or {}
    spans = {}
    for key, (label, start, end, _pairing) in SPANS.items():
        values = samples[key]
        median, p90 = _median(values), _p90(values)
        budget_median, budget_p90 = budgets.get(key, (None, None))
        over = []
        if median is not None and budget_median is not None and median > budget_median:
            over.append("median")
        if p90 is not None and budget_p90 is not None and p90 > budget_p90:
            over.append("p90")
        spans[key] = {
            "label": label, "start_stage": start, "end_stage": end,
            "n": len(values), "median": median, "p90": p90,
            "budget_median": budget_median, "budget_p90": budget_p90,
            "over_budget": over,
            "moved": movement((previous_spans.get(key) or {}).get("median"), median),
        }
    return {
        "jobs_requested": limit, "jobs_seen": jobs_seen, "jobs_used": jobs_used,
        "jobs_skipped": jobs_seen - jobs_used,
        "spans": spans,
        "start_response_lost": {"occurrences": lost_occurrences, "jobs": lost_jobs},
    }


def summary_dict(report):
    """The stored-summary form of a report: what a proof run records
    and a later report (or the release checklist) compares against.
    Self-describing (labels and budget flags included) so a consumer
    that cannot import this module, like the standalone release
    checklist builder, can still render and flag it faithfully."""
    return {
        "version": SUMMARY_VERSION,
        "jobs_seen": report["jobs_seen"], "jobs_used": report["jobs_used"],
        "spans": {key: {"label": span["label"], "median": span["median"],
                        "p90": span["p90"], "n": span["n"],
                        "over_budget": list(span["over_budget"])}
                  for key, span in report["spans"].items()},
        "start_response_lost": dict(report["start_response_lost"]),
    }


def load_summary(path):
    """A stored summary dict from a JSON file, or None when the file is
    missing, unreadable or not a summary. Never raises."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("spans"), dict):
        return None
    return data


def check_budgets(report, budgets=None):
    """Budget violations in a report, as plain strings.

    The check the hardware smoke and proof tooling call: an empty list
    means every measured span is inside its budget. ``budgets``
    overrides DEFAULT_BUDGETS (pass a report built with the same
    budgets; the report's own over_budget flags are what is read)."""
    violations = []
    for key, span in report["spans"].items():
        for which in span["over_budget"]:
            value = span["median"] if which == "median" else span["p90"]
            budget = span["budget_median"] if which == "median" else span["budget_p90"]
            violations.append(
                f"{span['label']}: {which} {value:.1f}s over budget {budget:.1f}s "
                f"({span['start_stage']} -> {span['end_stage']}, {span['n']} jobs)")
    return violations


def _seconds(value):
    return "not measured" if value is None else f"{value:.1f}s"


def render_text(report):
    """The report as plain text: the doctor, bundle and CLI rendering."""
    if not report["jobs_used"]:
        return (f"Timing: no readable TIMING.jsonl in the last "
                f"{report['jobs_requested']} jobs, so no span is measured yet.")
    lines = [f"Timing over the last {report['jobs_used']} jobs "
             f"({report['jobs_skipped']} skipped as unreadable or empty):"]
    for span in report["spans"].values():
        row = (f"  {span['label']}: median {_seconds(span['median'])}, "
               f"p90 {_seconds(span['p90'])} ({span['n']} jobs; "
               f"{span['start_stage']} -> {span['end_stage']})")
        if span["over_budget"]:
            row += f"  OVER BUDGET ({', '.join(span['over_budget'])})"
        if span["moved"]:
            moved = span["moved"]
            row += (f"  MOVED {moved['direction']} by {abs(moved['delta_s']):.1f}s "
                    f"({abs(moved['delta_pct']) * 100:.0f}%) since the previous summary")
        lines.append(row)
    lost = report["start_response_lost"]
    lines.append(f"  Start responses lost: {lost['occurrences']} "
                 f"in {lost['jobs']} job(s).")
    return "\n".join(lines)


def _default_archive():
    from .. import settings
    return settings.load()["paths"]["archive"]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Report the real user-facing waits from the archive's TIMING.jsonl files.")
    parser.add_argument("--archive", help="job archive folder (default: the configured archive)")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                        help=f"how many of the newest jobs to read (default {DEFAULT_JOBS})")
    parser.add_argument("--previous", help="a stored summary JSON to flag median movement against")
    parser.add_argument("--summary-out", help="write this run's summary JSON to this path")
    parser.add_argument("--check-budgets", action="store_true",
                        help="exit 1 when a span is over budget, 2 when nothing is measured")
    args = parser.parse_args(argv)
    archive = args.archive or _default_archive()
    previous = load_summary(args.previous) if args.previous else None
    report = build_report(archive, limit=args.jobs, previous=previous)
    print(render_text(report))
    if args.summary_out:
        Path(args.summary_out).write_text(
            json.dumps(summary_dict(report), indent=2) + "\n", encoding="utf-8")
    if args.check_budgets:
        if not report["jobs_used"]:
            print("check-budgets: no timing data to check", file=sys.stderr)
            return 2
        violations = check_budgets(report)
        for violation in violations:
            print(f"OVER BUDGET: {violation}", file=sys.stderr)
        return 1 if violations else 0
    return 0


__all__ = [
    "DEFAULT_BUDGETS", "DEFAULT_JOBS", "MOVEMENT_ABSOLUTE_S", "MOVEMENT_RELATIVE",
    "SPANS", "SUMMARY_VERSION", "build_report", "check_budgets", "job_spans",
    "load_summary", "main", "movement", "read_timing_file", "render_text",
    "summary_dict",
]


if __name__ == "__main__":
    raise SystemExit(main())
