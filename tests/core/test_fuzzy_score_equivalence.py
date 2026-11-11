"""The #1153 fuzzy speedups give exactly the scores the removed code gave.

``fuzzy.canonical`` and ``fuzzy._phonetic`` are memoized, and the ratio
tests check ``SequenceMatcher``'s two documented upper bounds before the
full ratio. None of that may move a score: this module keeps a frozen copy
of the removed scoring path and compares it with the live one over every
fuzzy target the chat lanes use against typed, misspelled, unrelated and
long messages.
"""
from __future__ import annotations

import importlib.util
import itertools
import sys
from difflib import SequenceMatcher
from pathlib import Path

import pytest

from print_handy.core import fuzzy
from print_handy.core.fuzzy_vocabulary import TYPO_FORMS

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- frozen copy of the removed path

def _old_covered(query_tokens, target_tokens):
    query_tokens = query_tokens[:fuzzy.MAX_COVERAGE_QUERY_TOKENS]
    target_tokens = target_tokens[:fuzzy.MAX_COVERAGE_TARGET_TOKENS]
    if not query_tokens or not target_tokens:
        return False
    covered = sum(1 for word in query_tokens
                  if max(SequenceMatcher(None, word, other).ratio()
                         for other in target_tokens) * 100 >= fuzzy.FUZZY_FLOOR)
    return covered * 2 >= len(query_tokens)


def _old_score_folded(q, t):
    if not q or not t:
        return 0
    score = round(SequenceMatcher(None, q, t).ratio() * 100)
    if score < fuzzy.FUZZY_FLOOR or not _old_covered(q.split(), t.split()):
        return 0
    return min(score, 99)


def old_fuzzy_score(query, target):
    # fuzzy._canonical is the unchanged fold; only its memo wrapper is new.
    q, t = fuzzy._canonical(query), fuzzy._canonical(target)
    if q and t:
        q = " ".join(fuzzy.correct_tokens(q.split(), t.split()))
    score = _old_score_folded(q, t)
    if score or not q or not t:
        return score
    phonetic = fuzzy._phonetic.__wrapped__
    pq = " ".join(phonetic(token) for token in q.split())
    pt = " ".join(phonetic(token) for token in t.split())
    if pq == q and pt == t:
        return 0
    return _old_score_folded(pq, pt)


# ---------------------------------------------------------------- inputs

def _plugin():
    name = "print_feedback_fuzzy_equivalence"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "plugins" / "print-feedback" / "__init__.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _targets():
    plugin = _plugin()
    targets = list(plugin._LOADED_FUZZY_PHRASES) + list(plugin._LOADED_FUZZY_RIVALS)
    targets += ["Bambu Lab P1S", "Flashforge AD5X", "Prusa MK4S", "Elegoo Centauri Carbon",
                "G4 Instant Desk Stand", "first layer", "bed temperature", "retraction",
                "twenty one", "version 3", "PLA Basic", "PETG HF", "Jade White"]
    return targets


QUERIES = [
    "", " ", "what colors are loaded", "whta colors are loded", "wat colour is loded",
    "whcih spools r loaded", "curent spools", "how much filamnet is left", "loaded filamint",
    "show me the loded spools", "print colours", "change the filamnet", "swap filament plz",
    "can you print this for me tonight please", "hello there", "make it 20% bigger",
    "the loaded filament is tangled again and I need help", "what is the bed temperature right now?",
    "retrakshun", "enuf", "firstlayer", "twentyone", "v3", "p1s", "ad5x", "desk stadn",
    "jade wite", "petg", "pla", "🙂 printing", "ÉLÉGOO centauri", "日本語のテキスト",
    "x" * 600, "loaded " * 120,
]


def _queries():
    # Every curated misspelling and its word, so the typo table's own pairs are covered.
    rows = list(QUERIES)
    for typo, word in itertools.islice(sorted(TYPO_FORMS.items()), 0, None, 17):
        rows.extend((typo, word))
    return rows


def test_fuzzy_score_matches_the_removed_path_on_every_pair():
    fuzzy._canonical_memo.cache_clear()
    fuzzy._phonetic.cache_clear()
    targets, queries = _targets(), _queries()
    assert len(queries) > 250
    mismatches = []
    nonzero = 0
    # Twice: the second pass reads every fold from the memo.
    for _ in range(2):
        for query, target in itertools.product(queries, targets):
            new, old = fuzzy.fuzzy_score(query, target), old_fuzzy_score(query, target)
            nonzero += bool(new)
            if new != old:
                mismatches.append((query, target, new, old))
    assert not mismatches, mismatches[:10]
    assert nonzero > 100, "the inputs must exercise real matches, not only zeros"


def test_quick_ratio_bounds_never_hide_a_ratio_at_the_floor():
    words = sorted({w for q in _queries() + _targets() for w in fuzzy._canonical(q).split()})[:400]
    for a, b in itertools.product(words[:80], words):
        exact = SequenceMatcher(None, a, b).ratio() * 100 >= fuzzy.FUZZY_FLOOR
        assert fuzzy._ratio_at_least(a, b, fuzzy.FUZZY_FLOOR) == exact, (a, b)


def test_canonical_memo_is_bounded_and_returns_the_fold():
    fuzzy._canonical_memo.cache_clear()
    for text in ("What Colours are LOADED", "twenty one layers", None, 42, "x" * 600):
        assert fuzzy.canonical(text) == fuzzy._canonical(text)
    info = fuzzy._canonical_memo.cache_info()
    # Non-str and over-long inputs are folded uncached.
    assert info.currsize == 2 and info.maxsize == fuzzy._CANONICAL_CACHE_MAX


@pytest.mark.parametrize("text, expected", [
    ("whta colors are loded", True),
    ("curent spools", True),
    ("change the filamnet", False),
    ("can you print this for me tonight please", False),
])
def test_loaded_spools_near_miss_answers_are_unchanged(text, expected):
    assert _plugin()._loaded_spools_fuzzy(text) is expected
