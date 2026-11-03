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


@pytest.mark.parametrize("question,choices,expected", [
    ("This STL measures 2.3 x 2.55 x 4.02 mm as written, and STL files do not record units. "
     "Is that size in millimeters, or in inches?",
     ["Millimeters · print at 2.3 x 2.55 x 4.02 mm", "Inches · print at 58.42 x 64.77 x 102.108 mm", "Cancel"], 0),
    ("Which plate prints first?", ["Plate 1: shelf · white PLA · ~1h 0m", "Plate 2: shelf", "Cancel"], 0),
    ("2-Tier Shelf does not fit the Flashforge AD5X bed full size as one plate, even rearranged.",
     ["100% · split 2 plates", "Shrink to 94%", "Cancel"], 1),
    ("Dragon Egg does not fit the Flashforge AD5X bed full size as one plate, even rearranged.",
     ["100% · split 6 plates", "Cancel"], 0),
    ("Spool Tower (244.76 × 201.82 × 94 mm) does not fit the bed full size as the author laid it out.",
     ["100% + supports", "90% · turned 88°", "Cancel"], 1),
    ("spiral-vase (179.83 × 300 × 179.83 mm) does not fit the bed full size as the author laid it out.",
     ["83.3% · rotated 90° on X", "Cancel"], 0),
])
def test_answers_for_the_questions_added_since_2026_11_02(question, choices, expected):
    assert default_adapter().pick(question, choices) == expected


def test_complex_case_extras_follow_the_case_rules(tmp_path):
    case_dir = tmp_path / "spiral-vase"
    case_dir.mkdir()
    (case_dir / "case.toml").write_text('[[click]]\nquestion = "Choose the color"\nbutton = "White"\n')
    model = case_dir / "model.stl"
    model.write_bytes(b"solid x\nendsolid x\n")
    case = run_external_corpus.build_case(model, tmp_path)
    assert case.clicks[0]["button"] == "White"
    assert case.clicks[-1] == {"question": "does not fit", "button": "rotated"}
