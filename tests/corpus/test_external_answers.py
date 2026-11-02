"""Narrow default-answer coverage without running the external model corpus."""
import pytest

from tests.corpus import run_external_corpus
from tests.corpus.harness import FakeAdapter, UnansweredPrompt


def default_adapter():
    # The runner is importable since its work moved under main(); the
    # generic case's click rules are its GENERIC_CLICKS constant.
    return FakeAdapter(run_external_corpus.GENERIC_CLICKS)


@pytest.mark.parametrize("question,choices,expected", [
    ("Inspected 2 parts: Stand, Base. Choose colors for these parts or one color for everything?",
     ["Choose colors for these parts", "One color for everything", "Cancel"], 1),
    ("Inspected 1 part: Body. Keep distinct source regions or explicitly use one color for everything? "
     "Keeping painted regions prints each painted area in the color you pick for it.",
     ["Keep distinct source regions", "One color for everything", "Cancel"], 1),
    ("stand (223 × 223 × 124 mm) does not fit the bed full size.",
     ["100% + supports", "Shrink to 95%", "Cancel"], 1),
])
def test_generic_answers(question, choices, expected):
    assert default_adapter().pick(question, choices) == expected


@pytest.mark.parametrize("question,choices", [
    ("Unexpected approval question", ["Continue", "Cancel"]),
    ("stand does not fit the bed full size.", ["100% + supports", "Cancel"]),
])
def test_unrecognized_or_unavailable_answers_still_fail(question, choices):
    with pytest.raises(UnansweredPrompt):
        default_adapter().pick(question, choices)
