"""#1476: the Web UI Timing page renders the one timing report."""
import json
from pathlib import Path

from print_handy.webui import routes, timing


def _job(archive, name, base=1000.0):
    folder = Path(archive) / name
    folder.mkdir(parents=True, exist_ok=True)
    rows = [
        {"stage": "message_received", "t": base},
        {"stage": "first_question_posted", "t": base + 2},
        {"stage": "slice_start", "t": base + 30},
        {"stage": "slice_done", "t": base + 42},
        {"stage": "intake_start_tapped", "t": base + 60},
        {"stage": "intake_start_returned", "t": base + 100},
    ]
    (folder / "TIMING.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_timing_route_is_declared_with_the_session_policy():
    route, _capture = routes.resolve("GET", "/timing")
    assert route is not None and route.policy == routes.SESSION
    assert route.handler == "_timing_route"


def test_timing_page_renders_the_report(tmp_path):
    _job(tmp_path, "job-a")
    page = timing.render_timing_page({"paths": {"archive": str(tmp_path)}})
    assert "Link to first question" in page
    assert "Tap to print started" in page
    assert "2.0s" in page and "40.0s" in page  # the two measured medians
    assert "Start responses lost: 0" in page


def test_timing_page_with_no_jobs_says_not_measured(tmp_path):
    page = timing.render_timing_page({"paths": {"archive": str(tmp_path)}})
    assert "No readable" in page and "TIMING.jsonl" in page
