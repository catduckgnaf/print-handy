"""Representative typed edit requests used by the separate corpus runner."""
import copy
import pytest

from print_handy.core import edit_plan, model_questions
from tests.corpus.edit.runner import generate_mock_gcode, resolve_plan, verify_probe


@pytest.mark.parametrize("text,kind", [
    ("add tree supports", "supports"),
    ("add normal supports", "supports"),
    ("no supports", "supports"),
    ("remove supports", "supports"),
    ("add tree supprots", "supports"),
    ("no supprots", "supports"),
    ("scale to 50%", "scale_percent"),
    ("scale to 80%", "scale_percent"),
    ("shrink this by 20%", "scale_percent"),
    ("shrnik it by 20%", "scale_percent"),
    ("shrnk this by 20%", "scale_percent"),
    ("make it half the size", "scale_percent"),
    ("scale to 200%", "scale_percent"),
    ("rotate 90", "rotate"),
    ("rotat 90", "rotate"),
    ("rotate 90 on z", "rotate"),
    ("mirror it", "mirror"),
    ("print 3 copies", "copies"),
    ("2 copies", "copies"),
    ("1 copy", "copies"),
    ("brim 5mm", "brim"),
    ("brim 8", "brim"),
    ("no brim", "brim"),
    ("layer height 0.12", "layer_height"),
    ("0.16 layer height", "layer_height"),
    ("infill 25%", "infill"),
    ("20% infill", "infill"),
])
def test_resolve_without_mutating_job(text, kind):
    job = {"audit": {"size_x": 40.0, "size_y": 20.0, "size_z": 10.0}, "scale": 1.0}
    original = copy.deepcopy(job)
    plan = resolve_plan(text, job, None)
    assert plan["kind"] == kind
    assert job == original


@pytest.mark.parametrize("text", ["lay it flat", "lay flatt"])
def test_lay_flat_resolves_plan(text):
    resolved = edit_plan.resolve_request(text)
    assert resolved is not None and resolved[0] == "plan"
    assert resolved[1]["kind"] == "lay_flat"


@pytest.mark.parametrize("text,expected_percents", [
    ("make it bigger", [110, 125, 150]),
    ("make it 2x bigger", [110, 125, 150]),
    ("make it smaller", [90, 75, 50]),
    ("shrink it", [90, 75, 50]),
])
def test_ambiguous_size_offers_picks_not_an_action(text, expected_percents):
    kind, choices = edit_plan.resolve_request(text)
    assert kind == "pick"
    assert choices is not None
    assert [choice["percent"] for choice in choices] == expected_percents


@pytest.mark.parametrize("text", [
    "does this need supports?",
    "does this have supports?",
    "are supports needed?"
])
def test_support_questions_are_classified_as_questions(text):
    assert model_questions.classify(text) == "supports"


def test_probe_verifies_correct_mock_gcode():
    job = {"audit": {"size_x": 40.0, "size_y": 20.0, "size_z": 10.0}, "scale": 1.0}
    plan = resolve_plan("add tree supports", job, None)
    gcode = generate_mock_gcode(dims=(40, 20, 10), supports=True, support_type="tree")
    ok, lines = verify_probe(plan, job, gcode)
    assert ok, lines


def test_probe_rejects_wrong_sliced_configuration():
    job = {"audit": {"size_x": 40.0, "size_y": 20.0, "size_z": 10.0}, "scale": 1.0}
    plan = resolve_plan("add tree supports", job, None)
    ok, reasons = verify_probe(plan, job, "; enable_support = 0\n")
    assert not ok
    assert reasons


@pytest.mark.parametrize("text", [
    "hello there",
    "print this",
    "looks good",
    "thank you"
])
def test_unrelated_chat_is_not_an_edit(text):
    assert edit_plan.resolve_request(text) is None
    assert not edit_plan.looks_like_edit(text)


@pytest.mark.parametrize("text,err", [
    ("infill 200%", "infill"),
    ("brim 50mm", "brim"),
])
def test_out_of_bounds_edits_raise_plan_error(text, err):
    with pytest.raises(edit_plan.PlanError, match=err):
        edit_plan.resolve_request(text)
