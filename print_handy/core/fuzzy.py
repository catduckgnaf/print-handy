"""Typo-tolerant similarity scoring (#468): deterministic, stdlib-only, offline.

One scorer shared by every fuzzy surface — knowledge lookup (stage 1),
print-history matching (stage 2) and did-you-mean command suggestions
(stage 3) — so the bands mean the same thing everywhere:

- ``score >= HIGH_CONFIDENCE`` (85): a high-confidence near-match.
- ``FUZZY_FLOOR <= score < HIGH_CONFIDENCE`` (65–84): suggestion material —
  fit for a "Did you mean …?" or a pick-list, never for acting on.
- ``score < FUZZY_FLOOR``: not a match; the function returns 0.

The score is the ``difflib.SequenceMatcher`` ratio of the whole normalized
strings, measured on the real registries in the #468 Stage 0 report
(~0.08 ms per comparison, the whole set in <6 ms, no dependency). Token-pair
averaging was measured too and rejected: it crowns false friends
("weather" → heater entries at 0.77, "fan" → anything containing "and" at
0.67) that the whole-string ratio keeps below the floor.

One guard rides on top of the ratio: token coverage. A multi-word query
whose words mostly match nothing in the target is a false friend even when
one long shared word inflates the ratio — "warp drive calibration" scored
0.65+ against "probe calibrate" on the strength of "calibration" alone.
So at least half of the query's words must each have a plausible partner
(ratio >= 0.65) among the target's words, or the score is 0. A typo'd word
still counts as covered when its partner is the word it mistyped
("adheison" → "adhesion", 0.88), so real multi-word typos pass.

A fuzzy score never reaches 100: exact is a separate tier owned by each
caller, deterministic exact matches always beat fuzzy ones, and a fuzzy
match by itself never runs anything — callers present it for a pick or a
confirmation that names the resolved target.

Hardening + vocabulary pass (post-2.2.1 audit, toward 2.2.2):

- ``norm`` is Unicode-honest: NFKD decomposition with combining marks
  stripped folds diacritics consistently ("café"/"naïve"/"résumé" all
  fold to their ASCII forms) and letters/digits of any script survive,
  so a CJK-titled print can be matched by name instead of folding to
  nothing. ``norm`` is the one fold: the knowledge registry, the flow
  matcher and print-history matching all route through it.
- Scoring canonicalizes both sides (``canonical``): number words and
  digits are the same number ("twenty one" == "21", "2nd" == "second"
  == "2"), domain abbreviations, variables and compound forms are
  equated ("cfg" == "configuration", "var" == "variable", "param" == "parameter",
  "dim" == "dimension", "accel" == "acceleration", "temp" == "temperature",
  "hot end" == "hotend", "heat bed" == "heatbed", "lead screw" == "leadscrew",
  "power supply" == "psu", units such as millimetre/cm/meter == "mm"/"cm"/"m",
  and Commonwealth/British spelling equivalents such as "colour" == "color",
  "grey" == "gray", "aluminium" == "aluminum", "fibre" == "fiber"),
  the software, sites and brands of the workflow equate spaced and
  joined ("orca slicer" == "orcaslicer", "bambu lab" == "bambulab",
  "creality cloud" == "crealitycloud", "mmf" == "myminifactory"), as do
  slicing and error terms ("tree support" == "treesupport",
  "thermal runaway" == "thermalrunaway", "heat creep" == "heatcreep"),
  and simple plurals/verb forms fold together ("nozzles" == "nozzle",
  "leveling" == "level", "jammed" == "jam"). This lives only in the
  fuzzy scorer: exact tiers and the exact print-number path are
  untouched, and fuzzy hits still only ever produce suggestions.
- When the plain pass scores 0, a phonetic rescue pass scores a lightly
  sound-folded form of both sides ("enuf" → "enough",
  "retrakshun" → "retraction", "adhezhun" → "adhesion", "wat" → "what",
  "lite" → "light"). It can only lift pairs the plain pass already rejected,
  so no existing score moves.
- ``windowed_fuzzy_score`` caps its window pass (see the constants):
  keyword-stuffed titles and very long queries fall back to the
  whole-string score instead of burning seconds of CPU per candidate.
- ``decide`` is the shared confidence/conflict policy for did-you-mean:
  a fuzzy result may be treated as the single clear answer only at high
  confidence with a clear margin over the runner-up; anything else that
  scores is suggestion material for a pick-list.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

FUZZY_FLOOR = 65
HIGH_CONFIDENCE = 85

# Confidence/conflict policy (decide): a top hit is "direct" — clear
# enough to be the single offered answer — only at or above
# CLEAR_CONFIDENCE with a margin of at least CLEAR_MARGIN points over
# the runner-up. CLEAR_CONFIDENCE is the existing high-confidence band
# edge. The margin comes from measuring the real command set: across
# 459 dropped-letter/transposition typos of every command name and
# alias, the intended command's margin over the runner-up was >= 17 in
# all but three cases (minimum 9), so 10 sits just under the observed
# separation and flags only genuinely close calls as conflicts.
CLEAR_CONFIDENCE = HIGH_CONFIDENCE
CLEAR_MARGIN = 10

# Window-pass caps (F2): the window pass is quadratic-ish in token
# counts (every window start × every window size × a coverage guard over
# both token sets). Measured on this scorer: a 30-token query against a
# 120-token keyword-stuffed title took ~2.1 s for ONE candidate, and
# /print-again runs the windowed scorer over the whole archive on every
# miss. Beyond these caps the whole-string score stands — a stuffed
# title gets the score the whole string deserves.
MAX_WINDOW_TARGET_TOKENS = 32
MAX_WINDOW_QUERY_TOKENS = 16
# The coverage guard gets the same treatment: it judges the first
# MAX_COVERAGE_* tokens only, which bounds its pairwise-ratio cost.
MAX_COVERAGE_QUERY_TOKENS = 16
MAX_COVERAGE_TARGET_TOKENS = 32


def norm(text):
    """The one text fold every matcher in the repo uses.

    Casefolds, decomposes (NFKD) and strips combining marks so accented
    letters fold to their base letters, then keeps letters and digits of
    any script (CJK included) and turns everything else into spaces.
    """
    folded = unicodedata.normalize("NFKD", str(text or "").casefold())
    kept = []
    for ch in folded:
        if unicodedata.combining(ch):
            continue  # diacritic mark: drop it, keep the base letter
        kept.append(ch if (ch.isalnum() or ch.isspace()) else " ")
    return " ".join("".join(kept).split())


# ---------------------------------------------------------------- numbers

_UNIT_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TEN_WORDS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_ORDINAL_UNIT_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17,
    "eighteenth": 18, "nineteenth": 19,
}
_ORDINAL_TEN_WORDS = {
    "twentieth": 20, "thirtieth": 30, "fortieth": 40, "fiftieth": 50,
    "sixtieth": 60, "seventieth": 70, "eightieth": 80, "ninetieth": 90,
}
_NUMBER_WORDS = {**_UNIT_WORDS, **_TEN_WORDS, **_ORDINAL_UNIT_WORDS,
                 **_ORDINAL_TEN_WORDS, "hundred": 100, "thousand": 1000,
                 "hundredth": 100, "thousandth": 1000}
_ORDINAL_DIGITS = re.compile(r"^(\d+)(st|nd|rd|th)$")
_VERSION_DIGITS = re.compile(r"^v(\d+)$")


def _parse_number_phrase(tokens, start):
    """(value, next index) for the number phrase at ``start``, or None.

    Handles cardinals and ordinals alike — both canonicalize to the same
    digits ("twenty first" and "twenty one" both become 21) — with
    hundred/thousand multipliers and a skipped "and" ("one hundred and
    five"). Digit tokens are not phrases; the caller handles them.
    """
    total, current, seen = 0, 0, False
    i = start
    while i < len(tokens):
        token = tokens[i]
        if token == "and" and seen and i + 1 < len(tokens) \
                and tokens[i + 1] in _NUMBER_WORDS:
            i += 1
            continue
        if token in ("hundred", "hundredth"):
            current = (current or 1) * 100
        elif token in ("thousand", "thousandth"):
            total += (current or 1) * 1000
            current = 0
        elif token in _NUMBER_WORDS:
            current += _NUMBER_WORDS[token]
        else:
            break
        seen = True
        i += 1
    if not seen:
        return None
    return total + current, i


# ------------------------------------------------------- canonical forms

# Compound phrases and single-token equivalents are defined in fuzzy_vocabulary.py
# (1,000+ entries covering 3D printer brands, lines, slicing variables, materials,
# units, UK/US spelling folds, and common mobile autocorrect/fat-finger typos).
from .fuzzy_vocabulary import (  # noqa: F401
    PHRASE_FORMS,
    TOKEN_FORMS,
    PHRASE_FORMS as _PHRASE_FORMS,
    REAL_WORDS,
    TOKEN_FORMS as _TOKEN_FORMS,
    TYPO_FORMS,
    TYPO_PHRASES,
)


# ------------------------------------------------------------ misspellings

def typo_tokens(tokens):
    """Apply the curated misspelling tables to already-``norm``ed tokens.

    ``TYPO_PHRASES`` fixes a space typed in the wrong place ("youa bout"),
    then ``TYPO_FORMS`` maps each known misspelling to its word (a value
    may be two words: "alot" is "a lot"). Unknown tokens pass through.
    """
    out = []
    i = 0
    while i < len(tokens):
        pair = tuple(tokens[i:i + 2])
        if pair in TYPO_PHRASES:
            out.extend(TYPO_PHRASES[pair])
            i += 2
            continue
        out.extend(TYPO_FORMS.get(tokens[i], tokens[i]).split())
        i += 1
    return out


def typo_fold(text):
    """``norm`` plus the curated misspelling table, as one string ("shrnik it" -> "shrink it")."""
    return " ".join(typo_tokens(norm(text).split()))


def edit_distance(a, b, limit):
    """Damerau-Levenshtein (optimal string alignment) distance, or ``limit + 1`` past ``limit``.

    A swap of two neighbouring letters is one edit, so "shrnik" is one
    edit from "shrink". Rows stop early once every cell exceeds the limit.
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2 = None
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if (prev2 is not None and i > 1 and j > 1
                    and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]):
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def typo_limit(token):
    """Edits tolerated for one token: none up to 4 letters, 1 for 5 to 7, 2 for 8 or more."""
    if len(token) <= 4:
        return 0
    return 1 if len(token) <= 7 else 2


def _correctable(token):
    """Only plain words are corrected: numbers, units, ids, URLs and mentions pass through."""
    return (token.isalpha() and token.isascii() and token not in REAL_WORDS
            and typo_limit(token) > 0)


def correct_token(token, vocabulary):
    """The one vocabulary word ``token`` is a bounded misspelling of, else ``token``.

    The first letter must match, the distance must be within
    ``typo_limit``, and the nearest word must be unique: two vocabulary
    words at the same distance are a genuine ambiguity, so the token is
    left alone and the caller's normal scoring (and pick list) decides.
    """
    if token in vocabulary or not _correctable(token):
        return token
    limit = typo_limit(token)
    best, best_distance, tied = None, limit + 1, False
    for word in vocabulary:
        if not word or word[0] != token[0] or not word.isalpha():
            continue
        distance = edit_distance(token, word, limit)
        if distance < best_distance:
            best, best_distance, tied = word, distance, False
        elif distance == best_distance and word != best:
            tied = True
    if best is None or tied:
        return token
    return best


def correct_tokens(tokens, vocabulary):
    """``correct_token`` over a token list (vocabulary is any container of words)."""
    vocabulary = set(vocabulary)
    return [correct_token(token, vocabulary) for token in tokens]


def correct_typos(text, vocabulary):
    """Text with misspelled words fixed against ``vocabulary``, for keyword routers.

    Folds with ``norm`` and the curated table first, then corrects each
    remaining word with ``correct_token``. Apostrophes are kept inside
    words ("won't") so routers that match them still can.
    """
    words = []
    for raw in str(text or "").split():
        core = raw.casefold().strip(".,!?;:\"()[]")
        if not core or not re.fullmatch(r"[a-z']+", core):
            words.append(raw)
            continue
        folded = typo_tokens([core]) if "'" not in core else [core]
        words.extend(correct_tokens(folded, vocabulary) if "'" not in core else folded)
    return " ".join(words)



def _undouble(stem):
    """Drop one of a doubled final consonant ("stopp" → "stop", "jamm" → "jam")."""
    if len(stem) >= 2 and stem[-1] == stem[-2] and stem[-1] not in "aeiou":
        return stem[:-1]
    return stem


def _stem(token):
    """Fold simple plurals and -ing/-ed verb forms to the base word.

    Applied to both sides of every comparison, so a fold only has to be
    consistent, not a perfect stemmer. Guards keep short and
    look-alike words intact ("thing", "spring", "glass", "axis",
    "status" all keep their form).
    """
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("es") \
            and token[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") \
            and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    if len(token) > 5 and token.endswith("ing") and len(token[:-3]) >= 4:
        return _undouble(token[:-3])
    if len(token) > 4 and token.endswith("ed"):
        return _undouble(token[:-2])
    return token


def canonical(text):
    """``norm`` plus the scoring canonicalization, as a single string.

    Number phrases and digit tokens become plain digits, known compound
    phrases join, abbreviation tokens map to their long form, and the
    remaining tokens are stemmed. Used only inside fuzzy scoring —
    callers' exact tiers never see this form.
    """
    tokens = typo_tokens(norm(text).split())
    # A number word joined to the word it counts ("firstlayer",
    # "twentyone") splits back apart so the phrase parser below can
    # read it; both sides fold identically, so this only equates the
    # joined and spaced spellings of the same words.
    split_tokens = []
    for token in tokens:
        prefix = max((word for word in _NUMBER_WORDS
                      if len(token) > len(word) + 1 and token.startswith(word)
                      and token not in _NUMBER_WORDS),
                     key=len, default=None)
        if prefix:
            split_tokens.extend((prefix, token[len(prefix):]))
        else:
            split_tokens.append(token)
    tokens = split_tokens
    # Compound phrases first: none of them contain number words.
    phrases = []
    i = 0
    while i < len(tokens):
        pair = tuple(tokens[i:i + 2])
        if pair in _PHRASE_FORMS:
            phrases.extend(_PHRASE_FORMS[pair])
            i += 2
        else:
            phrases.append(tokens[i])
            i += 1
    out = []
    i = 0
    while i < len(phrases):
        token = phrases[i]
        digits = _ORDINAL_DIGITS.match(token) or _VERSION_DIGITS.match(token)
        if digits:
            out.append(str(int(digits.group(1))))
            i += 1
            continue
        if token.isdecimal():
            # isdecimal, not isdigit: isdigit() also accepts digit characters
            # int() refuses (e.g. '❶'), and one such token in a query or an
            # archived title used to abort the whole lookup (P2-264).
            out.append(str(int(token)))
            i += 1
            continue
        phrase = _parse_number_phrase(phrases, i)
        if phrase is not None:
            value, i = phrase
            out.append(str(value))
            continue
        out.append(_stem(_TOKEN_FORMS.get(token, token)))
        i += 1
    # "version 3" and "v3" are the same form: a bare "version" directly
    # before a number carries no meaning of its own.
    out = [token for j, token in enumerate(out)
           if not (token == "version" and j + 1 < len(out) and out[j + 1].isdigit())]
    return " ".join(out)


# ------------------------------------------------------------- phonetics

def _phonetic(token):
    """A light sound fold for the rescue pass: spelling-by-ear forms.

    Only consulted when the plain pass scored 0, so it never moves an
    existing score; it exists for respellings difflib reads as unrelated
    ("enuf" for "enough", "retrakshun" for "retraction").
    """
    token = token.replace("ph", "f")
    token = token.replace("tion", "shun").replace("sion", "zhun")
    token = token.replace("ough", "uf")
    token = re.sub(r"c(?=[aou])", "k", token)
    token = token.replace("zh", "sh")
    token = re.sub(r"^wh", "w", token)
    token = re.sub(r"^wr", "r", token)
    token = token.replace("ck", "k")
    token = token.replace("lite", "light")
    token = token.replace("brite", "bright")
    return token


# ---------------------------------------------------------------- scoring

def _covered(query_tokens, target_tokens) -> bool:
    """True when at least half the query's words have a plausible partner word in the target."""
    query_tokens = query_tokens[:MAX_COVERAGE_QUERY_TOKENS]
    target_tokens = target_tokens[:MAX_COVERAGE_TARGET_TOKENS]
    if not query_tokens or not target_tokens:
        return False
    covered = sum(1 for word in query_tokens
                  if max(SequenceMatcher(None, word, other).ratio()
                         for other in target_tokens) * 100 >= FUZZY_FLOOR)
    return covered * 2 >= len(query_tokens)


def _score_folded(q, t) -> int:
    """The whole-string ratio + coverage guard over already-folded strings."""
    if not q or not t:
        return 0
    score = round(SequenceMatcher(None, q, t).ratio() * 100)
    if score < FUZZY_FLOOR or not _covered(q.split(), t.split()):
        return 0
    return min(score, 99)


def fuzzy_score(query, target) -> int:
    """Similarity of one query to one target name/alias, 0 or 65–99.

    Both sides are canonicalized first (numbers, variants and simple
    word forms equated — see the module docstring), so callers may pass
    raw or folded text. Returns 0 when either side is empty, the ratio
    falls below ``FUZZY_FLOOR`` — below the floor is "not a match", not
    a low score — or the token-coverage guard rejects the pair as a
    false friend. When the plain pass returns 0, a phonetic rescue pass
    gets one chance to lift a spelling-by-ear pair; it never lowers or
    alters a score the plain pass produced.
    """
    q, t = canonical(query), canonical(target)
    if q and t:
        # Bounded typo tolerance against this target's own words: a
        # misspelled query word becomes the target word it is clearly a
        # misspelling of, never a different valid word (see correct_token).
        q = " ".join(correct_tokens(q.split(), t.split()))
    score = _score_folded(q, t)
    if score or not q or not t:
        return score
    pq = " ".join(_phonetic(token) for token in q.split())
    pt = " ".join(_phonetic(token) for token in t.split())
    if pq == q and pt == t:
        return 0
    return _score_folded(pq, pt)


def best_fuzzy_score(query, targets) -> int:
    """The best ``fuzzy_score`` of a query over a candidate's names/aliases/ids."""
    return max((fuzzy_score(query, target) for target in targets), default=0)


def windowed_fuzzy_score(query, target) -> int:
    """Best ``fuzzy_score`` of a query against a target or a word window of it.

    For long candidate names where the query names only part of the name —
    print titles are the case in point: "desk stadn" scores 52 against the
    whole of "G4 Instant Desk Stand" but 90 against its "desk stand"
    window. Windows run one word shorter and one word longer than the query
    too, so a missing or extra space ("deskstand") still finds its window.
    The token-coverage guard applies inside every window: a window must
    still hold partners for at least half the query's words.

    The window pass is capped (``MAX_WINDOW_*_TOKENS``): with a longer
    target or query the whole-string score stands, which bounds the cost
    per candidate no matter how keyword-stuffed the title is.
    """
    q, t = canonical(query), canonical(target)
    if not q or not t:
        return 0
    best = fuzzy_score(q, t)
    qtokens, ttokens = q.split(), t.split()
    if len(ttokens) > 1 and len(ttokens) <= MAX_WINDOW_TARGET_TOKENS \
            and len(qtokens) <= MAX_WINDOW_QUERY_TOKENS:
        for size in {len(qtokens) - 1, len(qtokens), len(qtokens) + 1}:
            if size < 1 or size >= len(ttokens):
                continue
            for start in range(len(ttokens) - size + 1):
                best = max(best, fuzzy_score(q, " ".join(ttokens[start:start + size])))
    return best


TOKEN_MATCH_FLOOR = 82


def token_match(query, words, floor=TOKEN_MATCH_FLOOR):
    """The best (score, word) of any single query token against a word list.

    Argument-free core matching for short command verbs: "shrnik it by
    20%" has to find "shrink" without the number, the pronoun or the
    filler dragging the whole-string ratio under the floor. Each query
    token is scored on its own against each word (canonical forms, so
    "shrinking" meets "shrink"). Words of three letters or fewer match
    exactly only, because one typo in a short word is another word.
    The floor sits above the general one so a near miss such as
    "sink" for "shrink" (80) is not a match. Returns (0, None) when
    nothing reaches the floor; ties keep the earlier word.
    """
    best = (0, None)
    tokens = canonical(query).split()
    for word in words:
        target = canonical(word)
        for token in tokens:
            if len(target) <= 3 or len(token) <= 3:
                score = 99 if token == target else 0
            else:
                score = fuzzy_score(token, target)
            if score >= floor and score > best[0]:
                best = (score, word)
    return best


# ----------------------------------------------------------------- policy

@dataclass(frozen=True)
class Decision:
    """The shared did-you-mean verdict for one set of fuzzy scores.

    ``kind`` is "direct" (one clear, high-confidence answer), "suggest"
    (scores exist but are grey-zone or contested — present a did-you-mean
    or a pick-list, never act on the best guess), or "none" (nothing at
    or above the floor). ``best`` is the top payload (None for "none");
    ``contenders`` is every payload within ``CLEAR_MARGIN`` of the top
    score, in ranked order — the pick-list a "suggest" surface shows.
    """

    kind: str
    best: object = None
    contenders: tuple = ()


def decide(scored) -> Decision:
    """Apply the confidence/conflict policy to ``(score, payload)`` pairs.

    Scores are ``fuzzy_score`` integers; anything below ``FUZZY_FLOOR``
    (including the scorer's 0) is not a candidate. Direct requires the
    top score at or above ``CLEAR_CONFIDENCE`` *and* a margin of at
    least ``CLEAR_MARGIN`` over the runner-up — a tie is the extreme
    conflict and is never direct. Ranking is by score descending and
    stable, so equal scores keep the caller's order: deterministic, no
    coin flips. This governs fuzzy-band results only; exact matches are
    the callers' own tier and never pass through here.
    """
    ranked = sorted(((score, payload) for score, payload in scored if score >= FUZZY_FLOOR),
                    key=lambda entry: -entry[0])
    if not ranked:
        return Decision("none")
    top_score, top = ranked[0]
    contenders = tuple(payload for score, payload in ranked
                       if top_score - score < CLEAR_MARGIN)
    if top_score >= CLEAR_CONFIDENCE and len(contenders) == 1:
        return Decision("direct", top, (top,))
    return Decision("suggest", top, contenders)
