import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy.core.link_intake import requested_authored_plate
from tests.pipeline.test_multi_tool_intake import creator, flow_env, run_flow


@pytest.mark.parametrize("message, expected", [
    ("https://www.printables.com/model/1-ghost print plate 2", ["2"]),
    ("https://www.printables.com/model/1-ghost print plate 3", []),
    ("https://www.printables.com/model/1-ghost print plate 1 or 2", []),
])
def test_selector_resolves_only_existing_unambiguous_authored_plate(message, expected):
    manifest = {"plate_usage": {"1": {"part_keys": ["a"]}, "2": {"part_keys": ["b"]}}}
    assert requested_authored_plate(message, manifest) == expected


def test_bare_model_link_preserves_unselected_flow():
    manifest = {"plate_usage": {"1": {}, "2": {}}}
    assert requested_authored_plate("https://www.printables.com/model/1-ghost", manifest) is None


def test_direct_link_plate_two_reaches_color_and_slice_scope(flow_env):
    source = Path(__file__).resolve().parents[1] / "corpus/cases/plates-multicolor/model.3mf"
    adapter, result, receipt = run_flow(flow_env, source.read_bytes(), ["Use these and pick the rest", "Black", "Red", "Slice and print"],
                                        text="https://www.printables.com/model/1-ghost print plate 2")
    assert receipt["status"] == "STARTED", receipt
    job = json.loads(Path(receipt["job_json"]).read_text())
    assert job["selected_plates"] == ["2"]
    assert flow_env.slice_scopes == [["2"]]
    assert flow_env.sliced
    assert {choice["slot"] for choice in job["color_decisions"]["choices"].values()} == {2, 3}
    assert any("Black" in q["question"] or "Red" in q["question"] for q in adapter.questions)

