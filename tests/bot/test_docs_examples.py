"""Docs examples are the same phrases the tests run (#1524 tranche 8).

Every quoted example in the README's plain-words section and in the
generated capability help must be a gold-corpus phrase, so the docs can
never advertise a phrasing the layer does not actually handle.
"""
import json
import re
from pathlib import Path

from print_handy.bot import natural_language as nl

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "tests" / "corpus" / "natural_language"


def _corpus_texts():
    texts = set()
    for path in CORPUS.glob("*.json"):
        for case in json.loads(path.read_text())["cases"]:
            texts.add(" ".join(str(case["text"]).split()).strip().casefold())
    return texts


def test_readme_plain_word_examples_are_corpus_phrases():
    readme = (ROOT / "README.md").read_text()
    section = readme.split("## Talk to it in plain words", 1)[1].split("\n## ", 1)[0]
    quoted = re.findall(r'"([^"]+)"', section)
    corpus = _corpus_texts()
    assert quoted, "the README plain-words section quotes no examples"
    for phrase in quoted:
        assert " ".join(phrase.split()).casefold() in corpus, \
            f"README example {phrase!r} is not a corpus phrase"


def test_generated_help_examples_are_corpus_phrases():
    corpus = _corpus_texts()
    for cap in nl.CAPABILITIES:
        for example in cap.examples:
            assert " ".join(example.split()).casefold() in corpus, \
                f"{cap.id} example {example!r} is not a corpus phrase"


def test_readme_names_the_three_routes():
    readme = (ROOT / "README.md").read_text()
    section = readme.split("## Talk to it in plain words", 1)[1].split("\n## ", 1)[0]
    assert "Normal chat is local" in section
    assert "@mention is the conversational AI" in section
    assert "Designing is its own AI path" in section
