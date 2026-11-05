"""Phrasing table for chat edits (#903 rework): relative sizing, typos, and questions.

Every phrase from the #908 review table is pinned here. Deterministic and offline.
"""

import pytest

from print_handy.core import edit_plan as ep
from print_handy.core import fuzzy
from print_handy.core import model_questions as mq


def _percent(text):
    kind, plan = ep.resolve_request(text)
    assert kind == "plan", (text, kind, plan)
    assert plan["kind"] == "scale_percent", (text, plan)
    return plan["percent"]


@pytest.mark.parametrize("text,percent", [
    ("make it 80%", 80),
    ("scale to 80%", 80),
    ("shrink it to 80%", 80),
    ("shrink it by 20%", 80),
    ("shrnik it by 20%", 80),
    ("make it 20% smaller", 80),
    ("20% bigger please", 120),
    ("enlarge it by 50%", 150),
    ("scale it down by 10%", 90),
    ("make it half the size", 50),
    ("double the size", 200),
    ("make it twice as big", 200),
    ("make it dubble", 200),
])
def test_relative_and_absolute_sizing(text, percent):
    assert _percent(text) == percent


@pytest.mark.parametrize("text,picks", [
    ("shrink this item", [90, 75, 50]),
    ("shrink this", [90, 75, 50]),
    ("make it smaller", [90, 75, 50]),
    ("smaler", [90, 75, 50]),
    ("make it bigger", [110, 125, 150]),
])
def test_direction_without_amount_is_a_pick_list_not_an_action(text, picks):
    kind, plans = ep.resolve_request(text)
    assert kind == "pick"
    assert [p["percent"] for p in plans] == picks
    assert all(p["kind"] == "scale_percent" for p in plans)


def test_relative_sizing_composes_against_the_current_scale():
    job = {"scale": 0.8, "dims_mm": [100, 50, 20]}
    _kind, plan = ep.resolve_request("shrink it by 50%")
    ep.apply_plan(job, plan)
    assert job["scale"] == pytest.approx(0.4)


@pytest.mark.parametrize("text,kind", [
    ("infill 20%", "infill"),
    ("print 2 copies", "copies"),
    ("rotate 90", "rotate"),
    ("rotate it up", "rotate"),
    ("add supports", "supports"),
    ("make it 50 mm", "scale_to_mm"),
    ("lay it flat", "lay_flat"),
])
def test_other_kinds_are_not_captured_by_relative_sizing(text, kind):
    result = ep.resolve_request(text)
    assert result and result[0] == "plan" and result[1]["kind"] == kind


@pytest.mark.parametrize("text", ["hello there", "help me design something", "shrink by 150%"])
def test_non_edits_and_impossible_sizes_resolve_to_nothing(text):
    assert ep.resolve_request(text) is None


@pytest.mark.parametrize("text", [
    "does this need supports",
    "does it need supports?",
    "are there supports",
    "does this have supports?",
])
def test_supports_questions_are_questions_not_edits(text):
    assert mq.classify(text) == "supports"


@pytest.mark.parametrize("text,kind", [
    ("does this need supports", "supports"),
    ("do I need supports", "supports"),
    ("do i need supports?", "supports"),
    ("will it fit", "fit"),
    ("will it fit?", "fit"),
    ("how big is it", "size"),
    ("how big is it?", "size"),
])
def test_real_questions_stay_questions(text, kind):
    assert mq.classify(text) == kind


@pytest.mark.parametrize("text", [
    "can you add supports",
    "can you add supports?",
    "could you shrink it",
    "could you shrink it?",
    "please add a brim",
    "please add a brim?",
    "can you make it smaller",
    "can u add supports",
    "would you please scale it to 80%?",
    "ok can you turn off supports",
])
def test_polite_requests_are_commands_not_questions(text):
    assert mq.classify(text) is None
    assert not mq.is_question(text)


@pytest.mark.parametrize("text", ["can you add supports", "could you shrink it", "can you make it smaller"])
def test_polite_requests_reach_the_edit_vocabulary(text):
    assert ep.looks_like_edit(text) or ep.resolve_request(text) is not None


@pytest.mark.parametrize("query,word", [
    ("shrnik", "shrink"), ("shirnk", "shrink"), ("smaler", "smaller"),
    ("biger", "bigger"), ("dubble", "double"), ("shrinking", "shrink"),
])
def test_token_match_tolerates_typos(query, word):
    assert fuzzy.token_match(f"please {query} it", [word])[1] == word


@pytest.mark.parametrize("query,word", [("sink", "shrink"), ("layer", "larger"), ("it", "up"), ("item", "it")])
def test_token_match_rejects_near_misses(query, word):
    assert fuzzy.token_match(query, [word]) == (0, None)
