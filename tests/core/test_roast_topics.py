"""Regression tests for topic retrieval and externally sourced packs."""
import json
import random

import pytest

from print_handy.core import roast
from scripts.import_roast_pack import import_pack


SAMPLE = {
    "id": "external-topic-fixture",
    "family": "external-topic-fixture",
    "text": "Euclid would submit that diagram as evidence of a hostile workplace.",
    "keywords": ["right triangle", "trigonometry"],
    "source": "test fixture",
    "license": "original test fixture",
    "reviewed": True,
    "category": "geometry",
}


def test_tagged_pack_matches_suffix_beyond_display_title(tmp_path):
    pack = tmp_path / "pack.json"
    pack.write_text(json.dumps({"entries": [SAMPLE]}))
    corpus = roast.load_corpus(str(pack))
    ctx = roast.build_context("Extra Long Decorative Educational Model - RightTriangle.stl")
    assert "triangle" not in ctx["title"].lower()
    pools = dict(roast.candidate_pools(ctx, corpus=corpus))
    assert pools["title:external"] == [(SAMPLE["id"], SAMPLE["text"])]
    assert SAMPLE["id"] not in {eid for eid, _ in pools["generic"]}


def test_topic_match_respects_boundaries_and_status(tmp_path):
    pack = tmp_path / "pack.json"
    pack.write_text(json.dumps({"entries": [SAMPLE]}))
    corpus = roast.load_corpus(str(pack))
    for name in ("trigonometryish.stl", "unrelated.stl"):
        pools = dict(roast.candidate_pools(roast.build_context(name), corpus=corpus))
        assert "title:external" not in pools
    ctx = roast.build_context("right_triangle.stl")
    for status in ("failed", "cancelled", "spaghetti"):
        assert "title:external" not in dict(roast.candidate_pools(ctx, status, corpus))


def test_specific_tag_wins_over_single_word(tmp_path):
    pack = tmp_path / "pack.json"
    broad = {**SAMPLE, "id": "broad", "family": "broad",
             "text": "The triangle took one look and filed for a different angle.",
             "keywords": ["triangle"]}
    pack.write_text(json.dumps({"entries": [SAMPLE, broad]}))
    corpus = roast.load_corpus(str(pack))
    pools = dict(roast.candidate_pools(roast.build_context("right_triangle.stl"), corpus=corpus))
    assert pools["title:external"] == [(SAMPLE["id"], SAMPLE["text"])]


def test_title_gets_a_turn_despite_known_facts(tmp_path):
    ctx = roast.build_context("Remote Holder", material="PLA", color="Black")
    state = tmp_path / "state.json"
    rng = random.Random(10)
    for i in range(8):
        assert roast.roast(ctx, key=str(i), state_path=state, rng=rng)
    history = json.loads(state.read_text())["pool_history"]
    assert "title:holder" in history[:4]
    assert "generic" not in history


def test_import_requires_review_and_preserves_provenance(tmp_path):
    source, output = tmp_path / "source.json", tmp_path / "pack.json"
    source.write_text(json.dumps([SAMPLE, SAMPLE, {**SAMPLE, "reviewed": False}]))
    report = import_pack([source], output, minimum=1)
    assert report["accepted"] == 1
    assert report["preflight_rejected"]["not reviewed"] == 1
    row = json.loads(output.read_text())["entries"][0]
    assert row["source"] == SAMPLE["source"] and row["license"] == SAMPLE["license"]
    assert row["keywords"] == SAMPLE["keywords"]
    previous = output.read_text()
    with pytest.raises(ValueError, match="need 1000"):
        import_pack([source], output)
    assert output.read_text() == previous
