"""The spelling corpus drives the search surfaces, not just the spelling fold.

Anchor: "the spelling test" is ``tests/core/test_fuzzy_typos.py`` — the
curated misspelling corpus (``ALL_PAIRS`` plus the phase-3 mined pairs)
that pins how each typo folds to its word through ``fuzzy.typo_fold`` /
``fuzzy.canonical``. (It is not ``tests/test_us_spelling.py``: that one
lints PR prose for British spellings, a CI style check with no search
behavior behind it.) Folding is only the first step of what a typo'd
message actually meets in chat, so this sibling suite drives the same
corpus through the two search behaviors built on top of the fold:

- **Fuzzy search** — ``runtime.command_suggestion`` /
  ``runtime.command_suggestion_candidates``: near-miss commands and
  natural-language task phrases resolve to one canonical suggestion, a
  contested near miss resolves to *no* suggestion plus a pick-list, and
  a suggestion is only ever a string the user confirms (the
  did-you-mean prompt); nothing here dispatches.
- **Conversation search** — conversational questions route through the
  task-intent table to ``/print-lookup`` with the person's own words
  carried over *corrected*, and the knowledge registry search
  (``knowledge`` lookup) tops at the same entry for a typo'd query as
  for the word itself across its topic families.

Every case is derived from the corpus tables at collection time, so a
typo pair added to ``test_fuzzy_typos`` gains search coverage here
automatically. The cases are chosen so a broken matcher changes the
outcome, verified by mutation while writing this suite: drop the
``TYPO_FORMS`` entries for "alot", "teh" or "spegatti" and the carried
lookup query keeps the typo (and "the print is spegatti" stops
resolving at all); remove the {q} typo correction in the task-intent
matcher and the carry cases fail the same way; break the contested
policy in ``fuzzy.decide`` and the pick-list cases gain a coin-flip
suggestion.
"""
import pytest

from print_handy import knowledge
from print_handy.bot import runtime
from tests.core.test_fuzzy_typos import (
    ALL_PAIRS,
    DO_NOT_MAP,
    PHASE3_COLLISION_SHIELDS,
    PHASE3_MINED_TYPOS,
)

cs = runtime.command_suggestion
candidates = runtime.command_suggestion_candidates

CORPUS = {**ALL_PAIRS, **PHASE3_MINED_TYPOS}

# The carried lookup query is corrected against the *intent* vocabulary
# (runtime._match_task_intent), whose canonical forms are the words the
# task patterns themselves use. Two corpus corrections are therefore
# routed in their intent-canonical form rather than the table's literal
# value: the plural "spaghettis" routes as "spaghetti" (the form every
# spaghetti pattern matches) and the adverb "separately" as "separate"
# (the bed-down patterns' verb). Everything else routes as the table's
# word, verified pair by pair below.
ROUTED_FORM = {"spaghettis": "spaghetti", "separately": "separate"}


def routed(word):
    return ROUTED_FORM.get(word, word)


# ------------------------------------------- conversation search: the
# lookup carry. A conversational question keeps the person's words as
# the lookup argument, with each typo replaced by its correction.

@pytest.mark.parametrize("typo,word", sorted(CORPUS.items()))
def test_how_to_questions_carry_the_corrected_word(typo, word):
    expected = f"/print-lookup fix {routed(word)}"
    assert cs(f"how to fix {typo}") == expected
    assert cs(f"how to fix {word}") == expected


@pytest.mark.parametrize("typo,word", sorted(CORPUS.items()))
def test_what_does_it_mean_carries_the_corrected_word(typo, word):
    expected = f"/print-lookup {routed(word)}"
    assert cs(f"what does {typo} mean") == expected
    assert cs(f"what does {word} mean") == expected


@pytest.mark.parametrize("word", DO_NOT_MAP)
def test_real_words_are_carried_into_lookup_unchanged(word):
    # A real word near a vocabulary word is a legitimate lookup term in
    # its own right: the carry must not "correct" it into its neighbour.
    assert cs(f"how to fix {word}") == f"/print-lookup fix {word}"


# ------------------------------------------- fuzzy search: conversational
# task phrases whose operative word is a corpus correction. Cases are
# derived from the corpus by corrected word, so every typo of (say)
# "filament" in the table is exercised in the phrase, not a hand-picked
# few.

TASK_TEMPLATES = {
    "filament": [("change the {}", "/spool"),
                 ("change {}", "/spool")],
    "nozzle": [("change the {}", "/nozzle change"),
               ("the {} is clogged", "/print-troubleshoot clog"),
               ("clean the {}", "/print-bed-down")],
    "hotend": [("the {} is clogged", "/print-troubleshoot clog"),
               ("{} temperature", "/print-status")],
    "cancel": [("{} the print", "/print-stop")],
    "pause": [("{} the print", "/print-pause")],
    "resume": [("{} the print", "/print-resume")],
    "stop": [("{} the print", "/print-stop")],
    "maintenance": [("do some {}", "/print-maintenance"),
                    ("do the {}", "/print-maintenance")],
    "separate": [("{} the bed and nozzle", "/print-bed-down")],
    "spaghetti": [("the print is {}", "/print-troubleshoot first-layer"),
                  ("my print turned into {}", "/print-troubleshoot first-layer")],
    "stringing": [("too much {}", "/print-lookup stringing")],
    "calibrate": [("{} the bed", "/print-calibrate")],
    "temperature": [("what {} is the nozzle", "/print-status")],
}


def _task_cases():
    cases = []
    for typo, word in sorted(CORPUS.items()):
        for template, expected in TASK_TEMPLATES.get(word, ()):
            cases.append((typo, word, template, expected))
    return cases


@pytest.mark.parametrize("typo,word,template,expected", _task_cases())
def test_corpus_typos_route_their_task_phrase(typo, word, template, expected):
    assert cs(template.format(typo)) == expected
    # The typo must cost nothing: the correctly spelled phrase routes
    # to exactly the same suggestion.
    assert cs(template.format(word)) == expected


# ------------------------------------------- fuzzy search: command-shaped
# near misses built from corpus typos of command words.

SLASH_TEMPLATES = {
    "maintenance": "/print-maintenance",
    "resume": "/print-resume",
    "stop": "/print-stop",
}


def _slash_cases():
    cases = []
    for typo, word in sorted(CORPUS.items()):
        if word in SLASH_TEMPLATES:
            cases.append((f"/print-{typo}", SLASH_TEMPLATES[word]))
    return cases


@pytest.mark.parametrize("text,expected", _slash_cases())
def test_corpus_typos_of_command_words_suggest_the_command(text, expected):
    assert cs(text) == expected
    # A single clear suggestion means no pick-list is offered.
    assert candidates(text) == []


@pytest.mark.parametrize("text,expected", [
    ("/spol list", "/spool list"),          # whole-name typo, argument carried
    ("/print-spols", "/print-spools"),      # typo inside the longer name
    ("/print-bed-dwn", "/print-bed-down"),  # dropped vowel in the tail word
    ("/nozzel", "/nozzle"),                 # corpus typo as the whole command
    # "pause" typo: no /print-pause command exists, and since
    # /print-plate-calibrate landed (#455) its windowed stem score wins
    # this outright, so the case pins the single suggestion instead of
    # the pick-list it used to get.
    ("/print-pauze", "/print-plate-calibrate"),
])
def test_more_command_shaped_near_misses(text, expected):
    assert cs(text) == expected
    assert candidates(text) == []


# A contested near miss gets no coin-flip suggestion: the honest answer
# is the pick-list. Each input below is a real corpus typo (or one edit
# from a command) whose scores sit within the clear margin of several
# commands.
@pytest.mark.parametrize("text", [
    "/prnt",            # print-* stem, several whole commands contend
    "/print-staus",     # "status" typo; stop/source/resume contend
    "/print-restar",    # "restart" typo; resume/reboot contend
    "/print-calibrte",  # "calibrate" typo; calibration/live contend
])
def test_contested_command_typos_get_a_pick_list_not_a_guess(text):
    assert cs(text) is None
    assert len(candidates(text)) >= 2


# ------------------------------------------- false-positive guards

@pytest.mark.parametrize("word", sorted(set(DO_NOT_MAP) | {w for w, _ in PHASE3_COLLISION_SHIELDS}))
@pytest.mark.parametrize("template", ["{}", "the {}", "print {}"])
def test_real_words_get_no_suggestion(word, template):
    # Real English words near the vocabulary, alone or in light phrasing,
    # are conversation, not near misses: no suggestion, no pick-list.
    text = template.format(word)
    assert cs(text) is None
    assert candidates(text) == []


@pytest.mark.parametrize("text", [
    "", "   ", "?", "???", "!", "...", "----", "12345", "a",
    "\U0001F389", "\U0001F389\U0001F389\U0001F389",
])
def test_empty_punctuation_and_emoji_only_get_no_suggestion(text):
    assert cs(text) is None
    assert candidates(text) == []


@pytest.mark.parametrize("text", [
    "ask ai why is my print stringin",
    "ask ai stopp the print",
    "ai: cancle the print",
])
def test_ask_ai_with_typos_is_not_a_deterministic_suggestion(text):
    # An explicit AI invocation belongs to the fallback lane that strips
    # and re-checks it; the deterministic suggestion on the raw text —
    # typos and all — stays None, exactly as for correct spelling.
    assert cs(text) is None


# ------------------------------------------- conversation search: the
# knowledge registry itself. For the registry's topic families, a typo'd
# query tops at the same entry as the word, in the suggestion/high band
# — never at the exact-only 100. (Bare ambiguous words are deliberately
# not pinned here: lookup returns ranked candidates over the whole
# registry, firmware entries included, so a bare word's top is ranking
# detail, not a contract — test_knowledge.py pins that surface, e.g.
# "nozzel" keeps the clog flow among its hits at <= 84. What is a
# contract is the topic-family behavior below, and that a typo never
# invents a hit for a word that has none.)

@pytest.fixture(scope="module")
def registry():
    return knowledge.load_registry()


def _top(registry, query):
    hits = registry.lookup(query)
    assert hits, query
    hit = hits[0]
    return hit.kind, getattr(hit.item, "id", getattr(hit.item, "key", None)), hit.score


# (corrected word, phrase template, expected top item id) — cases are
# derived from the corpus by word, so every typo of "adhesion" in the
# table is searched bare and in the "bed ..." phrasing, and every typo
# of "nozzle" in the "... clog" phrasing a person would actually use.
LOOKUP_FAMILIES = [
    ("adhesion", "{}", "first-layer"),
    ("adhesion", "bed {}", "first-layer"),
    ("nozzle", "{} clog", "clog"),
    ("help", "{}", "print-help"),
    ("heat", "{}", "print-heat"),
    ("prime", "{}", "prime-line"),
]


def _lookup_cases():
    cases = []
    for typo, word in sorted(CORPUS.items()):
        for family_word, template, expected_id in LOOKUP_FAMILIES:
            if word == family_word:
                cases.append((typo, word, template, expected_id))
    return cases


@pytest.mark.parametrize("typo,word,template,expected_id", _lookup_cases())
def test_typo_lookup_tops_where_the_word_tops(registry, typo, word, template, expected_id):
    typo_top = _top(registry, template.format(typo))
    word_top = _top(registry, template.format(word))
    assert typo_top[:2] == word_top[:2] == (word_top[0], expected_id)
    assert typo_top[2] < 100  # a fuzzy hit is never graded exact


# Corpus corrections with no registry topic: the word itself finds
# nothing, and the typo must find nothing either — a misspelling can
# lose a match, never invent one.
@pytest.mark.parametrize("typo,word", [(t, w) for t, w in sorted(CORPUS.items())
                                       if w in ("warping", "retraction")])
def test_typo_lookup_invents_nothing_for_topicless_words(registry, typo, word):
    assert registry.lookup(word) == []
    assert registry.lookup(typo) == []
