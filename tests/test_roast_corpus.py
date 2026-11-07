"""Corpus gates for the local roast database (#1002, test layer 1).

Every corpus change must keep these green, the way the fuzzy vocabulary
tables are pinned: no duplicates or near-duplicates, known slots only,
every line fills cleanly and meets the contract with a fully populated
print, blocklist clean, and no bank quietly shrinks.
"""
import difflib
import json
import re
from importlib import resources

import pytest

from print_handy.core import roast

FULL = roast.build_context("Benchy", duration_s=14 * 3600 + 22 * 60, material="PLA",
                           color="Galaxy Black", grams=212, printer="AD5X", copies=4,
                           print_number=57, times=5, finish_hour=3,
                           last_title="Calibration Cube", prev_fails=2,
                           fail_streak=3, color_streak=4, top_color="Black")
# A long but legal title: every line must still fit with it, or be skipped at draw time.
LONG = roast.build_context("Articulated Crystal Dragon Large", duration_s=14 * 3600,
                           material="PETG", color="Silk Gold", grams=480, printer="AD5X",
                           copies=2, print_number=1234, times=12, finish_hour=4,
                           last_title="Voron Stealthburner Toolhead Beta", prev_fails=2,
                           fail_streak=3, color_streak=4, top_color="Gold")
MIN_BANK_SIZES = {
    "generic": 220, "one_liners": 120, "long_print": 55, "overnight": 45,
    "short_print": 45, "heavy_filament": 45, "repeat_print": 55,
    "failed": 80, "cancelled": 60, "spaghetti": 95, "dark_humor": 100
}


def _all_entries():
    corpus = roast.load_corpus()
    out = [(bank, text) for bank, rows in corpus["banks"].items() for _id, text in rows]
    out += [(f"title:{k}", text) for k, rows in corpus["title_reactions"].items() for _id, text in rows]
    out += [(f"color:{k}", text) for k, rows in corpus.get("color_roasts", {}).items() for _id, text in rows]
    out += [(f"reprint_failed:{k}", text) for k, rows in corpus.get("reprint_failed", {}).items() for _id, text in rows]
    out += [(f"history:{k}", text) for k, rows in corpus.get("history_callbacks", {}).items() for _id, text in rows]
    return out


def test_every_bank_file_is_valid_json_with_provenance():
    folder = resources.files("print_handy").joinpath("data", "roasts")
    names = sorted(p.name for p in folder.iterdir() if p.name.endswith(".json"))
    assert len(names) == 18
    for name in names:
        payload = json.loads(folder.joinpath(name).read_text(encoding="utf-8"))
        assert payload["_about"] and payload["_phase"] and payload["_license"], name
        assert payload.get("entries") or payload.get("categories"), name


@pytest.mark.parametrize("bank,minimum", sorted(MIN_BANK_SIZES.items()))
def test_banks_never_shrink_below_the_seed(bank, minimum):
    assert len(roast.load_corpus()["banks"][bank]) >= minimum


def test_title_reactions_and_fragments_keep_the_seed():
    corpus = roast.load_corpus()
    assert len(corpus["title_reactions"]) >= 108
    assert all(len(rows) >= 3 for rows in corpus["title_reactions"].values())
    assert sum(len(rows) for rows in corpus["title_reactions"].values()) >= 430
    assert len(corpus.get("color_roasts", {})) >= 20
    assert all(len(rows) >= 5 for rows in corpus["color_roasts"].values())
    assert sum(len(rows) for rows in corpus["color_roasts"].values()) >= 120
    assert len(corpus.get("reprint_failed", {}).get("completed", [])) >= 40
    assert len(corpus.get("reprint_failed", {}).get("failed", [])) >= 40
    assert len(corpus.get("history_callbacks", {}).get("previous_print", [])) >= 40
    assert len(corpus.get("history_callbacks", {}).get("fail_streak", [])) >= 30
    assert {k: len(v) for k, v in corpus["fragments"].items()} == {
        "comparisons": 62, "compliments": 62, "openers": 62, "verdicts": 62}
    assert all(len(v) >= 60 for v in corpus["fragments"].values())
    assert roast.dictionary_size() >= 520
    assert roast.corpus_size() >= 1500


def test_no_exact_duplicates_anywhere():
    seen = {}
    corpus = roast.load_corpus()
    texts = _all_entries() + [(f"fragment:{k}", t) for k, rows in corpus["fragments"].items() for t in rows]
    for bank, text in texts:
        key = re.sub(r"\s+", " ", text.strip().casefold())
        assert key not in seen, f"{text!r} in {bank} and {seen[key]}"
        seen[key] = bank


def test_no_near_duplicates_across_all_banks():
    entries = _all_entries()
    folded = [(bank, text, text.casefold()) for bank, text in entries]
    for i, (bank_a, text_a, a) in enumerate(folded):
        matcher = difflib.SequenceMatcher(None, a)
        for bank_b, text_b, b in folded[i + 1:]:
            matcher.set_seq1(b)
            if matcher.real_quick_ratio() <= 0.88 or matcher.quick_ratio() <= 0.88:
                continue
            assert matcher.ratio() <= 0.88, f"{text_a!r} ({bank_a}) ~ {text_b!r} ({bank_b})"


def test_known_slots_only():
    for bank, text in _all_entries():
        assert set(re.findall(r"\{(\w+)\}", text)) <= set(roast.SLOTS), (bank, text)


@pytest.mark.parametrize("ctx", [FULL, LONG], ids=["benchy", "long-title"])
def test_every_entry_fills_and_meets_the_contract(ctx):
    corpus = roast.load_corpus()
    failures = []
    for bank, text in _all_entries():
        line = roast.render(text, ctx)
        problem = "unrenderable" if line is None else roast.contract_problem(line, corpus["blocklist"])
        if problem:
            failures.append((bank, problem, text))
    if ctx is FULL:
        assert failures == []
    else:
        # A longer title may push a few lines over 20 words; those are skipped
        # at draw time, never trimmed. The corpus keeps them rare.
        assert len(failures) <= len(_all_entries()) // 10, failures[:10]


def test_blocklist_clean_and_no_quotes_or_hashtags_in_entries():
    corpus = roast.load_corpus()
    for bank, text in _all_entries():
        low = text.casefold()
        for word in corpus["blocklist"]:
            assert not re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", low), (bank, text, word)
        assert '"' not in text and not re.search(r"#\w", text), (bank, text)


def test_one_liners_need_no_facts():
    for _id, text in roast.load_corpus()["banks"]["one_liners"]:
        assert not re.findall(r"\{(\w+)\}", text), text


def test_status_banks_never_use_success_words():
    # Claims that the print went well; "short of success" style lines are fine.
    success = re.compile(r"\b(?:succeeded|nailed it|flawless|perfect print|turned out great|came out great)\b", re.I)
    corpus = roast.load_corpus()
    for bank in ("failed", "cancelled", "spaghetti"):
        for _id, text in corpus["banks"][bank]:
            assert not success.search(text), (bank, text)


def test_dictionary_structure_and_categories():
    corpus = roast.load_corpus()
    cats = corpus.get("dictionary", {}).get("categories", {})
    assert len(cats) == 8
    expected = {"models", "slicer", "defects", "materials", "colors", "hardware", "habits", "commands"}
    assert set(cats.keys()) == expected
    for name, terms in cats.items():
        assert len(terms) >= 20, f"category {name} has too few terms"
        for t in terms:
            assert isinstance(t.get("term"), str) and t["term"].strip()
            assert t.get("category") == name
            tag = t.get("roast_tag")
            if tag:
                assert tag.lower() in corpus["title_reactions"], f"invalid roast_tag {tag} for {t['term']}"


def test_color_roasts_cover_all_color_families():
    corpus = roast.load_corpus()
    color_keys = set(corpus.get("color_roasts", {}).keys())
    assert set(roast.COLOR_FAMILIES).issubset(color_keys)
    assert "streak" in color_keys


def test_spaghetti_contains_pasta_jokes():
    corpus = roast.load_corpus()
    pasta_terms = re.compile(r"\b(?:pasta|spaghetti|marinara|noodles|fettuccine|ramen|linguine|parmesan|sauce)\b", re.I)
    spaghetti_lines = [text for _id, text in corpus["banks"]["spaghetti"]]
    pasta_hits = [t for t in spaghetti_lines if pasta_terms.search(t)]
    assert len(pasta_hits) >= 15, f"expected at least 15 pasta-themed jokes, got {len(pasta_hits)}"
