"""Given/When/Then stories for Catduck using the public Discord model-intake flow."""

import pytest

from . import harness


def _run(case_name, tmp_path):
    case = harness.load_case(harness.CASES / case_name)
    run = harness.run_case(case, tmp_path / case_name)
    return case, run


def _picked(run, fragment):
    return [q["picked"] for q in run.adapter.questions if fragment.casefold() in q["question"].casefold()]


def test_catduck_drops_a_multicolor_file_then_slices_and_prints(tmp_path):
    """Given Catduck drops a four-color 3MF in Discord, when he assigns colors and approves,
    then each component keeps its requested slot and one verified G-code reaches the start path.
    """
    case, run = _run("four-colors-one-plate", tmp_path)
    harness.check_expectations(case, run)
    assert _picked(run, '("Part 1")') == ["White PLA"]
    assert _picked(run, '("Part 2")') == ["Black PLA"]
    assert _picked(run, '("Part 3")') == ["Pink PLA"]
    assert _picked(run, '("Part 4")') == ["Dark Green PLA"]
    assert len(run.gcodes) == 1 and len(run.starts) == 1
    assert run.receipt["status"] == "STARTED"


def test_catduck_accepts_a_smaller_fit_for_an_oversized_model(tmp_path):
    """Given Catduck drops a model wider than the bed, when he accepts the suggested shrink,
    then the receipt records a sub-unit scale and the smaller model is sliced before start.
    """
    case, run = _run("must-shrink", tmp_path)
    harness.check_expectations(case, run)
    assert 0.78 <= run.receipt["scale"] <= 0.84
    assert run.events.index(next(e for e in run.events if e[0] == "slice_start")) > next(
        i for i, e in enumerate(run.events) if e[0] == "prompt" and "does not fit" in e[1].casefold())
    assert len(run.starts) == 1


def test_catduck_chooses_print_quantity_for_a_single_plate(tmp_path):
    """Given Catduck needs two copies of a one-color model, when he selects two copies,
    then both the saved receipt and sliced job account for the quantity.
    """
    case, run = _run("single-part", tmp_path)
    # First matching rule wins in the harness, so replace the case's own copies
    # rule instead of appending: this story is the one choosing the quantity.
    rules = [{"question": "How many copies", "button": "Two copies"}] + [
        rule for rule in case.clicks if rule["question"] != "How many copies"]
    case.clicks = rules
    run = harness.run_case(case, tmp_path / "two-copies")
    assert run.receipt["status"] == "STARTED"
    assert int(run.receipt.get("copies", 1)) == 2
    assert int(run.job.get("sliced_copies", {}).get("copies", 1)) == 2
    assert len(run.starts) == 1


def test_catduck_stops_before_approving_slice_and_print(tmp_path):
    """Given Catduck has not yet approved the final print request, when that prompt is
    unanswered, then the Discord flow fails closed and nothing starts. Slicing is
    prepared before the approval prompt (the prompt shows its results), so a slice
    may have run — the gate this story pins is that no print ever starts without
    approval.
    """
    case = harness.load_case(harness.CASES / "single-part")
    case.clicks = [rule for rule in case.clicks if rule["question"] != "·"]
    run = harness.run_case(case, tmp_path / "stop-before-approval")
    assert run.receipt["status"] == "FAILED"
    assert not run.starts
    assert any("Nothing sliced" in sent or "Nothing" in sent for sent in run.adapter.sent)


@pytest.mark.parametrize("transport", ["direct", "standalone"])
def test_catduck_cancels_an_unanswered_color_prompt_without_start(transport, tmp_path):
    """Given Catduck drops an STL but never answers its color prompt, when either Discord-backed
    intake waits for his choice, then the scenario does not authorize a slice or printer start.
    """
    case = harness.load_case(harness.CASES / "stl-single-part")
    case.clicks = []
    try:
        run = harness.run_case(case, tmp_path / transport, transport=transport, auto=False)
    except ImportError as exc:
        if transport == "standalone" and "discord" in str(exc).casefold():
            pytest.skip("discord.py is not installed for the system Python")
        raise
    assert not run.starts
    assert not run.slice_starts
    assert run.receipt["status"] in {"FAILED", "CANCELLED"}
