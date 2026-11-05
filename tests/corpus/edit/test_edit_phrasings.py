"""Representative typed edit requests used by the separate corpus runner."""
import copy

import pytest

from print_handy.core import edit_plan, model_questions
from tests.corpus.edit.runner import resolve_plan, verify_probe


@pytest.mark.parametrize("text,kind", [
    ("add tree supports", "supports"),
    ("no supports", "supports"),
    ("scale to 50%", "scale_percent"),
    ("shrink this by 20%", "scale_percent"),
    ("shrnik it by 20%", "scale_percent"),
    ("make it half the size", "scale_percent"),
    ("rotate 90", "rotate"),
    ("mirror it", "mirror"),
    ("print 3 copies", "copies"),
    ("brim 5mm", "brim"),
    ("layer height 0.12", "layer_height"),
    ("infill 25%", "infill"),
])
def test_resolve_without_mutating_job(text, kind):
    job = {"audit": {"size_x": 40, "size_y": 20, "size_z": 10}, "scale": 1.0}
    original = copy.deepcopy(job)
    plan = resolve_plan(text, job, None)
    assert plan["kind"] == kind
    assert job == original


def test_ambiguous_size_offers_picks_not_an_action():
    kind, choices = edit_plan.resolve_request("make it bigger")
    assert kind == "pick"
    assert [choice["percent"] for choice in choices] == [110, 125, 150]


@pytest.mark.parametrize("text", ["does this need supports?", "does this have supports?"])
def test_support_questions_are_classified_as_questions(text):
    assert model_questions.classify(text) == "supports"


def test_probe_rejects_wrong_sliced_configuration():
    job = {"audit": {"size_x": 40, "size_y": 20, "size_z": 10}, "scale": 1.0}
    plan = resolve_plan("add tree supports", job, None)
    ok, reasons = verify_probe(plan, job, "; enable_support = 0\n")
    assert not ok
    assert reasons


def test_unrelated_chat_is_not_an_edit():
    assert edit_plan.resolve_request("hello there") is None
    assert not edit_plan.looks_like_edit("hello there")
