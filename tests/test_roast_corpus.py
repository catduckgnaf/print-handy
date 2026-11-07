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
                           print_number=57, times=5, finish_hour=3)
# A long but legal title: every line must still fit with it, or be skipped at draw time.
LONG = roast.build_context("Articulated Crystal Dragon Large", duration_s=14 * 3600,
                           material="PETG", color="Silk Gold", grams=480, printer="AD5X",
                           copies=2, print_number=1234, times=12, finish_hour=4)
MIN_BANK_SIZES = {"generic": 120, "one_liners": 60, "long_print": 24, "overnight": 20,
                  "short_print": 20, "heavy_filament": 16, "repeat_print": 16,
                  "failed": 30, "cancelled": 20, "spaghetti": 20}


def _all_entries():
    corpus = roast.load_corpus()
    out = [(bank, text) for bank, rows in corpus["banks"].items() for _id, text in rows]
    out += [(f"title:{k}", text) for k, rows in corpus["title_reactions"].items() for _id, text in rows]
    return out


def test_every_bank_file_is_valid_json_with_provenance():
    folder = resources.files("print_handy").joinpath("data", "roasts")
    names = sorted(p.name for p in folder.iterdir() if p.name.endswith(".json"))
    assert len(names) == 13
    for name in names:
        payload = json.loads(folder.joinpath(name).read_text(encoding="utf-8"))
        assert payload["_about"] and payload["_phase"] and payload["_license"], name
        assert payload["entries"], name


@pytest.mark.parametrize("bank,minimum", sorted(MIN_BANK_SIZES.items()))
def test_banks_never_shrink_below_the_seed(bank, minimum):
    assert len(roast.load_corpus()["banks"][bank]) >= minimum


def test_title_reactions_and_fragments_keep_the_seed():
    corpus = roast.load_corpus()
    assert len(corpus["title_reactions"]) >= 16
    assert all(len(rows) >= 3 for rows in corpus["title_reactions"].values())
    assert {k: len(v) for k, v in corpus["fragments"].items()} == {
        "comparisons": 40, "compliments": 40, "openers": 40, "verdicts": 40}
    assert roast.corpus_size() >= 394


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
