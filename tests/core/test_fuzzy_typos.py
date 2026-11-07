"""Fuzzy phase 2: real misspellings resolve, real words never get rewritten.

The pairs are the curated set mined from the operator's own chat history
(aggregate misspelling -> correction counts only). Each must resolve through
``fuzzy.canonical`` and score as a match; each real English word near the
vocabulary must stay exactly as typed.
"""
import pytest

from print_handy.bot import runtime
from print_handy.core import fuzzy
from print_handy.core.fuzzy_vocabulary import REAL_WORDS, TYPO_FORMS

CURATED_PAIRS = {
    "seperate": "separate", "separat": "separate", "maintance": "maintenance",
    "tempature": "temperature", "filement": "filament", "flament": "filament",
    "filame": "filament", "shrnik": "shrink", "supprots": "supports",
    "supporte": "support", "envirement": "environment", "restar": "restart",
    "stopp": "stop", "resum": "resume", "scal": "scale", "nozzl": "nozzle",
    "priht": "print", "printe": "print", "cance": "cancel",
}
VARIANTS = {"levelling": "leveling", "canceled": "cancel"}
CARD_TYPOS = {
    "nozzel": "nozzle", "filiment": "filament", "filamant": "filament",
    "calibarte": "calibrate", "callibrate": "calibrate", "purg": "purge",
    "extuder": "extruder", "retracion": "retraction", "brimm": "brim",
    "suport": "support", "spegetti": "spaghetti", "enviroment": "environment",
    "propbably": "probably", "alot": "a lot", "incase": "in case", "dept": "debt",
}
DO_NOT_MAP = ["stale", "later", "head", "point", "step", "shrine", "restriction",
              "warning", "pure", "spook", "micro", "calibre", "fitment", "stats",
              "states", "parse", "nest", "upstate", "fir"]
# Second mining pass over the operator's chat history (same aggregate-only
# rule: pairs, never message text). "iddint" maps to the apostrophe-less
# form the matcher canonicalizes to, and "seel" is kept deliberately: it is
# an archaic falconry word, not a living one, and the mined context is
# unambiguously "see". Real words found misspelled-looking in the same pass
# (nest, upstate, fir) were rejected, not mapped — see DO_NOT_MAP's rule.
OPERATOR_MINED_2 = {
    "ahow": "how", "catagories": "categories", "comouse": "compose",
    "conficts": "conflicts", "ddi": "did", "dobe": "done",
    "eleego": "elegoo", "iddint": "didnt", "incorperate": "incorporate",
    "intergration": "integration", "jewelery": "jewelry",
    "langauge": "language", "listerners": "listeners", "ocming": "coming",
    "recongized": "recognized", "rgar": "that", "sanatize": "sanitize",
    "seel": "see", "seperately": "separately", "spegattis": "spaghettis",
    "suppert": "support",
    "suprise": "surprise", "syntaxt": "syntax", "tempalates": "templates",
    "throughly": "thoroughly", "wel": "well", "whgatever": "whatever",
}
# Generic high-frequency non-word misspellings: domain typos the bounded
# edit-distance pass misses (filamet, nozel) plus classic English keyboard
# slips (teh, recieve, definately). Suggested in a branch commit whose file
# writes landed as placeholders; re-added here properly, pair by pair.
# Seven of these (infil, infiil, nozel, retracton, stringin, temperture,
# temprature) also exist in TOKEN_FORMS — the same deliberate duplication
# as cance/extuder/nozzl — because ``typo_fold`` consumers never see
# TOKEN_FORMS, so the TYPO entry is what folds them there.
GENERIC_TYPOS = {
    "adn": "and", "becasue": "because", "becuase": "because",
    "definately": "definitely", "definatly": "definitely",
    "extrudor": "extruder", "filamet": "filament", "filamnt": "filament",
    "infiil": "infill", "infil": "infill", "klipperr": "klipper",
    "listners": "listeners", "nozel": "nozzle", "occured": "occurred",
    "recieve": "receive", "reccomend": "recommend", "recomend": "recommend",
    "retraciton": "retraction", "retracton": "retraction",
    "seperatly": "separately", "stringin": "stringing", "teh": "the",
    "temperture": "temperature", "temprature": "temperature",
    "untill": "until", "warpping": "warping", "wich": "which",
}
ALL_PAIRS = {**CURATED_PAIRS, **VARIANTS, **CARD_TYPOS, **OPERATOR_MINED_2,
             **GENERIC_TYPOS}


@pytest.mark.parametrize("typo,word", sorted(ALL_PAIRS.items()))
def test_every_pair_is_in_the_table(typo, word):
    assert TYPO_FORMS[typo] == word


@pytest.mark.parametrize("typo,word", sorted(ALL_PAIRS.items()))
def test_every_pair_folds_to_its_word(typo, word):
    assert fuzzy.typo_fold(typo) == word
    assert fuzzy.canonical(typo) == fuzzy.canonical(word)


@pytest.mark.parametrize("typo,word", sorted(ALL_PAIRS.items()))
def test_every_pair_scores_as_a_match(typo, word):
    assert fuzzy.fuzzy_score(typo, word) == 99


@pytest.mark.parametrize("word", DO_NOT_MAP)
def test_real_words_are_never_rewritten(word):
    assert word not in TYPO_FORMS
    assert word in REAL_WORDS
    assert fuzzy.typo_fold(word) == word
    vocabulary = set(TYPO_FORMS.values()) | {"layer", "scale", "heat", "print", "stop",
                                             "states", "status", "shrink", "filament"}
    assert fuzzy.correct_token(word, vocabulary) == word
    assert fuzzy.correct_typos(word, vocabulary) == word


@pytest.mark.parametrize("real,near", [("stale", "scale"), ("later", "layer"),
                                       ("point", "print"), ("stats", "status"),
                                       ("fitment", "filament")])
def test_real_words_do_not_score_as_their_neighbour(real, near):
    assert fuzzy.fuzzy_score(real, near) < 99


@pytest.mark.parametrize("text,word", [
    ("shrnik it by 20%", "shrink"), ("can you add supprots", "supports"),
    ("seperate the bed and nozzle", "separate"), ("do some maintance", "maintenance"),
])
def test_phrases_fold_in_context(text, word):
    assert word in fuzzy.typo_fold(text).split()


def test_two_word_split_is_repaired():
    assert fuzzy.typo_fold("youa bout") == "you about"


# ------------------------------------------------- bounded edit distance

@pytest.mark.parametrize("a,b,distance", [
    ("shrnik", "shrink", 1), ("nozzel", "nozzle", 1), ("kitten", "sitting", 3),
    ("maintenence", "maintenance", 1), ("calibrtion", "calibration", 1),
])
def test_edit_distance_counts_transpositions_as_one(a, b, distance):
    assert fuzzy.edit_distance(a, b, 3) == distance


def test_edit_distance_stops_past_the_limit():
    assert fuzzy.edit_distance("abcdefgh", "zzzzzzzz", 2) == 3


def test_edit_distance_long_tokens_bounded():
    assert fuzzy.edit_distance("a" * 2000, "a" * 1999 + "b", 2) == 1
    assert fuzzy.windowed_fuzzy_score("a" * 2000, "a" * 1999 + "b") == 99


@pytest.mark.parametrize("token,limit", [("bed", 0), ("stop", 0), ("purge", 1),
                                         ("nozzle", 1), ("extrude", 1),
                                         ("filament", 2), ("calibration", 2),
                                         ("a" * 50, 0)])
def test_typo_limit_by_length(token, limit):
    assert fuzzy.typo_limit(token) == limit


@pytest.mark.parametrize("token,expected", [
    ("tempreture", "temperature"),   # 10 letters, 2 edits
    ("maintenence", "maintenance"),  # 11 letters, 1 edit
    ("nozlze", "nozzle"),            # 6 letters, 1 transposition
    ("calibrtion", "calibration"),
])
def test_correct_token_within_bounds(token, expected):
    vocabulary = {"temperature", "maintenance", "nozzle", "calibration", "filament"}
    assert fuzzy.correct_token(token, vocabulary) == expected


@pytest.mark.parametrize("token", [
    "nozz",      # 4 letters: no tolerance
    "mozzle",    # first letter differs
    "nzzole",    # first letter matches but 2 edits at 6 letters
    "tmprtr",    # too far
    "ab12cd",    # not a plain word
])
def test_correct_token_outside_bounds_is_untouched(token):
    assert fuzzy.correct_token(token, {"nozzle", "temperature"}) == token


def test_correct_token_ties_are_left_alone():
    # "spoal" is one edit from both "spool" and "spoil": no guess.
    assert fuzzy.correct_token("spoal", {"spool", "spoil"}) == "spoal"


def test_correct_typos_keeps_ids_urls_and_numbers():
    text = "check the nozzel @<123> https://x.example/a.3mf 0.4mm"
    assert fuzzy.correct_typos(text, {"nozzle"}) == "check the nozzle @<123> https://x.example/a.3mf 0.4mm"


def test_fuzzy_score_tolerates_typos_against_the_target():
    assert fuzzy.fuzzy_score("tempreture tower", "temperature tower") == 99
    assert fuzzy.fuzzy_score("calibrtion cube", "Calibration Cube") == 99


# --------------------------------------------------------- bot routing

@pytest.mark.parametrize("text,command", [
    ("seperate the bed and nozzle", "/print-bed-down"),
    ("do some maintance", "/print-maintenance"),
    ("do the maintance", "/print-maintenance"),
    ("change filiment", "/spool"),
    ("restar the printer", "/print-reboot"),
    ("resum the print", "/print-resume"),
    ("stopp the print", "/print-stop"),
    ("the nozzel is clogged", "/print-troubleshoot clog"),
    ("change the nozzel", "/nozzle change"),
    ("callibrate the bed", "/print-calibrate"),
])
def test_misspelled_tasks_get_the_right_suggestion(text, command):
    assert runtime.command_suggestion(text) == command


@pytest.mark.parametrize("text", ["later", "stale", "point", "step", "the head", "warning"])
def test_real_words_get_no_task_suggestion(text):
    assert runtime.command_suggestion(text) is None


def test_typo_routing_is_only_a_suggestion():
    # The suggestion is a string the user confirms; nothing executes here.
    assert isinstance(runtime.command_suggestion("stopp the print"), str)
