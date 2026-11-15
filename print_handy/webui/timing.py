"""The Web UI Timing page (#1476): the real waits, read back from TIMING.jsonl.

One rendering of the one report builder in
:mod:`print_handy.jobs.timing_report`, beside the doctor's and the
diagnostics bundle's text rendering: median and p90 per user-facing
span over the newest jobs in the archive, the budget each span is
held to, and how often a start response was lost. Read-only; the page
records nothing and changes nothing.
"""
from __future__ import annotations

from ..jobs import timing_report
from . import layers as layers_module
from . import shell

_esc = shell._esc


def _seconds(value):
    return "not measured" if value is None else f"{value:.1f}s"


def render_timing_page(data, *, theme=shell.DEFAULT_THEME, nav=None):
    """The Timing page document for a settings mapping."""
    report = timing_report.build_report(layers_module.archive_root(data))
    parts = [
        "<p>The waits a person actually feels, read back from the "
        "<code>TIMING.jsonl</code> file every job writes: median and 90th "
        "percentile per span over the newest "
        f"{report['jobs_requested']} jobs in the archive. The same report "
        "appears in the doctor and in the diagnostics bundle.</p>"
    ]
    if not report["jobs_used"]:
        parts.append(
            "<p>No readable <code>TIMING.jsonl</code> yet, so no span is "
            "measured. Spans appear here as jobs run.</p>")
    else:
        parts.append(
            f"<p>Measured over {report['jobs_used']} jobs "
            f"({report['jobs_skipped']} skipped as unreadable or empty).</p>")
        rows = []
        for span in report["spans"].values():
            notes = []
            if span["over_budget"]:
                notes.append("over budget (" + ", ".join(span["over_budget"]) + ")")
            if span["moved"]:
                moved = span["moved"]
                notes.append(
                    f"moved {moved['direction']} by {abs(moved['delta_s']):.1f}s "
                    f"({abs(moved['delta_pct']) * 100:.0f}%) since the previous summary")
            rows.append(
                "<tr>"
                f"<td>{_esc(span['label'])}<br><span class=\"muted\">"
                f"{_esc(span['start_stage'])} -&gt; {_esc(span['end_stage'])}</span></td>"
                f"<td>{_seconds(span['median'])}</td>"
                f"<td>{_seconds(span['p90'])}</td>"
                f"<td>{span['n']}</td>"
                f"<td>median {_seconds(span['budget_median'])}, "
                f"p90 {_seconds(span['budget_p90'])}</td>"
                f"<td>{_esc('; '.join(notes))}</td>"
                "</tr>")
        parts.append(
            "<table><tr><th>Span</th><th>Median</th><th>p90</th><th>Jobs</th>"
            "<th>Budget</th><th>Notes</th></tr>" + "".join(rows) + "</table>")
        lost = report["start_response_lost"]
        parts.append(
            f"<p>Start responses lost: {lost['occurrences']} in "
            f"{lost['jobs']} job(s). A lost start response means the printer "
            "was asked to start and its answer never arrived; the start is "
            "then confirmed from the printer's own state, never assumed.</p>")
    return shell.page("Timing", "".join(parts), path="/timing", theme=theme, nav=nav)


__all__ = ["render_timing_page"]
