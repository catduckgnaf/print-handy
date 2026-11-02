"""Unit pins for the shared #468 fuzzy scorer (print_handy/core/fuzzy.py).

The scorer is deliberately narrow: whole-string difflib similarity with a
floor at 65 and a cap at 99. These pins hold the bands the #468 Stage 0
report set (65–84 suggestion material, 85–99 high-confidence, 100 reserved
for exact matches owned by callers) and the false-friend behaviour measured
against the real registries while the scorer was designed.
"""
import pytest

from print_handy.core import fuzzy


@pytest.mark.parametrize("query,target,band", [
    ("adheison", "adhesion", (85, 99)),      # transposed letters, the issue's example
    ("nozzel", "nozzle", (65, 99)),          # letter swap
    ("ofset", "offset", (85, 99)),           # dropped letter
    ("prnt stop", "print stop", (85, 99)),   # command near-miss (stage 3 reuses this)
    ("spol", "spool", (85, 99)),
    ("stringng", "ringing", (65, 84)),       # the known false friend: suggestion band only
])
def test_typo_pairs_land_in_their_measured_bands(query, target, band):
    score = fuzzy.fuzzy_score(query, target)
    assert band[0] <= score <= band[1], (query, target, score)


@pytest.mark.parametrize("query,target", [
    ("pizza", "prusa mk4s"),
    ("weather", "heat bed"),
    ("banana", "bambu lab a1"),
    ("fan", "first layer"),     # token-pair averaging scored this 0.67; whole-string must not
    ("", "nozzle"),
    ("nozzle", ""),
    ("   ", "nozzle"),
])
def test_unrelated_pairs_score_zero(query, target):
    assert fuzzy.fuzzy_score(query, target) == 0


def test_fuzzy_never_reaches_exact_100():
    # Identical strings are the caller's exact tier; through the fuzzy lens
    # they cap at 99 so a fuzzy contribution can never impersonate exact.
    assert fuzzy.fuzzy_score("adhesion", "adhesion") == 99
    # Singular/plural are canonical variants (hardening pass), so this pair
    # now folds together and caps at 99 like an identical pair — before the
    # canonical fold it scored 92 on the raw ratio.
    assert fuzzy.fuzzy_score("nozzle", "nozzles") == 99


def test_normalization_is_applied_to_both_sides():
    assert fuzzy.fuzzy_score("Nozzel!", "NOZZLE") == fuzzy.fuzzy_score("nozzel", "nozzle")
    assert fuzzy.fuzzy_score("  bed   adheison ", "bed adhesion") >= fuzzy.FUZZY_FLOOR


def test_coverage_guard_rejects_one_shared_word_false_friends():
    # One long shared word must not carry a multi-word query whose other
    # words match nothing in the target (measured: this pair's raw ratio
    # clears 65 on "calibration" alone).
    assert fuzzy.fuzzy_score("warp drive calibration", "probe calibrate") == 0
    # ...while a query whose words each have a partner — typo'd or not —
    # keeps its whole-string score.
    assert fuzzy.fuzzy_score("bed adheison", "adhesion") >= fuzzy.FUZZY_FLOOR
    assert fuzzy.fuzzy_score("nozzel clog fix", "nozzle clog") >= fuzzy.FUZZY_FLOOR


def test_windowed_score_finds_a_partial_title():
    # Long print titles (stage 2): the query names part of the title, and a
    # typo in it. Whole-string scores 52 here; the "desk stand" window is 90.
    assert fuzzy.windowed_fuzzy_score("desk stadn", "G4 Instant Desk Stand") == 90
    # A missing space is a window-size difference, not a different query.
    assert fuzzy.windowed_fuzzy_score("deskstand", "G4 Instant Desk Stand") >= 85
    # Windows do not rescue unrelated text.
    assert fuzzy.windowed_fuzzy_score("benchy", "G4 Instant Desk Stand") == 0
    assert fuzzy.windowed_fuzzy_score("warp drive calibration", "Probe Calibrate Mount") == 0


def test_best_fuzzy_score_takes_the_best_target():
    assert fuzzy.best_fuzzy_score("adheison", ["zzz", "adhesion failure", "adhesion"]) >= 85
    assert fuzzy.best_fuzzy_score("pizza", ["zzz", "adhesion"]) == 0
    assert fuzzy.best_fuzzy_score("adheison", []) == 0


def test_nondecimal_unicode_digits_do_not_crash_normalization():
    # P2-264: '❶' (U+2776) and friends report isdigit() == True but int()
    # refuses them — they are not decimal digits. Scoring or normalizing a
    # query/title containing one must return a result, never raise, so one
    # archived title like "Desk stand ❶" cannot abort every fuzzy lookup
    # that scans it.
    for char in ("❶", "➀", "፩"):
        assert isinstance(fuzzy.fuzzy_score(char, "nozzle"), int)
        assert isinstance(fuzzy.windowed_fuzzy_score("desk stand", f"Desk stand {char}"), int)
    # The character itself survives normalization (it is content, not noise).
    assert "❶" in fuzzy.canonical("Desk stand ❶")
    # Real decimal digits — ASCII or not — still fold to their value.
    assert fuzzy.canonical("mk 3") == fuzzy.canonical("mk ٣")
