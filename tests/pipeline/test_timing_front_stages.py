"""#1476: a real intake records the front-of-flow stages in TIMING.jsonl.

Runs the shared prepared-intake harness end to end (link in, questions,
download, inspection, slice) and reads the job's TIMING.jsonl back:
message_received, download_done, inspection_done and
first_question_posted must all be there, in time order, ahead of the
slice stages that already existed.
"""
import json
from pathlib import Path

from .test_color_intake import state  # noqa: F401
from .test_prepared_intake import Adapter, env, run_flow  # noqa: F401


def test_link_intake_records_the_front_stages(env):
    adapter, result, receipt = run_flow(env, [1, 0, 1, 1])  # slice only, then Done
    assert receipt["status"] == "SLICED_NOT_STARTED"
    timing_path = Path(receipt["job_json"]).parent / "TIMING.jsonl"
    rows = [json.loads(line) for line in timing_path.read_text().splitlines()]
    stages = {row["stage"]: row["t"] for row in rows}
    for stage in ("message_received", "download_done", "inspection_done",
                  "first_question_posted", "intake_slice_requested",
                  "intake_slice_returned"):
        assert stage in stages, f"missing stage {stage}: {sorted(stages)}"
    # message_received opens the flow; download precedes inspection,
    # which precedes the slice approval. (In this single-profile flow
    # the first question happens to be asked after inspection; the
    # invariant is only that it follows the message.)
    assert (stages["message_received"] <= stages["first_question_posted"]
            and stages["message_received"] <= stages["download_done"]
            <= stages["inspection_done"] <= stages["intake_slice_requested"]
            <= stages["intake_slice_returned"])
    assert adapter.questions  # the flow really asked questions
