"""Local print roasts: a curated corpus and a small deterministic engine (#1002).

Roasts are no-LLM at all: no model call, no model fallback, no endpoint.
One short line per print is drawn from the JSON banks in
``print_handy/data/roasts/`` and filled from facts about *this* print
(title, duration, material, color, grams, printer, finish hour, how many
times this job has printed). The corpus grows the way the fuzzy vocabulary
did, in reviewed phases with validation gates (tests/test_roast_corpus.py);
the engine stays small.

Selection, in order:

1. **Status first.** A failed, cancelled or spaghetti print draws only from
   its status bank; success language never reaches a failed print. The
   status banks are wired to the failure, cancel and confirmed-spaghetti
   posts as well as to chat requests (#1017 batch 2).
2. **Title reaction** (a known keyword in the title, completed prints only),
   then **context** banks (overnight, long, heavy, repeat, short — each only
   when the fact it talks about is actually known), then the **generic**
   templates and one-liners.
3. Inside a bank: a persisted **shuffle bag** (no entry again until the bag
   empties) plus a **history window** of recent entries across all banks. An
   entry whose **family** was drawn inside the family window is passed over
   while any other fresh entry exists, so a line and its variants do not
   cluster. An exhausted bank falls through to the next. When everything
   eligible is in the history window, the least recently used eligible entry
   is taken — bounded work, never a loop.
4. **Contract** after filling: one sentence, at most 20 words, no quotes,
   no hashtags, bounded length. A line that fails is skipped, never trimmed.
5. **One roast per print.** With a ``key`` the chosen line is stored, so a
   retried or replayed delivery returns the same line without advancing the
   bag twice.
6. **Framing.** A drawn line may be framed with corpus fragments: an
   opener or a comparison on a generic line, a verdict after a status
   line. The framed line must pass the framed contract (bounded words and
   length, at most two sentences, blocklist clean) or the plain line
   ships. A framed line keeps its base entry's id and family — framing
   is presentation, not a new joke. Compliment fragments are not
   assembled: no template carries a grammatical frame for them, and an
   insert that does not parse is worse than none.

An operator can add house roasts without forking (#1002 Phase 4): the
JSON file ``vision.roast_custom_bank`` names is validated through the
same gates as the shipped banks (schema, known slots, filled contract,
blocklist, no duplicates or near-duplicates) and its surviving entries
join the generic pool. A file or entry that fails is refused by name in
the log and skipped — never a crash, never a blocked post.

Any failure (missing corpus, broken state file, nothing eligible) returns
"" and the post goes out unchanged, exactly the best-effort contract the
LLM roast had. Stdlib only; ``rng`` is injectable so tests pin draws.
"""
from __future__ import annotations

import difflib
import functools
import hashlib
import json
import logging
import os
import random
import re
import tempfile
import time
from importlib import resources
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

CORPUS_PACKAGE = "print_handy"
CORPUS_DIR = ("data", "roasts")
SLOTS = ("title", "duration", "material", "color", "grams", "printer",
         "copies", "print_number", "times", "hour", "last_title",
         "prev_fails", "fail_streak", "color_streak", "top_color")
STATUSES = ("completed", "failed", "cancelled", "spaghetti")
STATUS_BANKS = {"failed": "failed", "cancelled": "cancelled", "spaghetti": "spaghetti"}
# The operator's own bank (#1002 Phase 4): not a shipped file — the
# entries come from the local JSON file vision.roast_custom_bank names,
# are validated by load_custom_bank, and join the generic pool.
CUSTOM_BANK = "custom"
NEAR_DUPLICATE_RATIO = 0.88
# Context banks in precedence order, each with the fact it is about.
CONTEXT_BANKS = ("overnight", "long_print", "heavy_filament", "repeat_print", "short_print")
TEMPLATE_BANKS = ("generic", "dark_humor") + CONTEXT_BANKS + tuple(STATUS_BANKS.values())

MAX_WORDS = 20
MAX_CHARS = 200
FRAMED_MAX_WORDS = 36
FRAMED_MAX_CHARS = 260
FRAMED_CHANCE = 0.5
TITLE_MAX_CHARS = 40
TITLE_MAX_WORDS = 6
LAST_TITLE_MAX_CHARS = 28
LAST_TITLE_MAX_WORDS = 4
HISTORY_WINDOW = 200
FAMILY_WINDOW = 30
CHOSEN_KEEP = 200
LONG_PRINT_S = 6 * 3600
SHORT_PRINT_S = 30 * 60
OVERNIGHT_HOURS = range(0, 6)
OVERNIGHT_MIN_S = 2 * 3600
HEAVY_GRAMS = 150

COLOR_FAMILIES = (
    "rainbow", "silk", "neon", "glow", "clear", "gold", "silver", "copper",
    "beige", "brown", "pink", "purple", "orange", "yellow", "green", "blue",
    "red", "gray", "white", "black"
)
COLOR_SYNONYMS = {
    "grey": "gray", "charcoal": "gray", "slate": "gray", "ash": "gray",
    "tan": "beige", "khaki": "beige", "skin": "beige", "flesh": "beige",
    "ivory": "beige", "cream": "beige", "bone": "beige",
    "coffee": "brown", "chocolate": "brown", "wood": "brown",
    "terracotta": "brown", "bronze": "brown",
    "translucent": "clear", "transparent": "clear", "natural": "clear",
    "magenta": "purple", "violet": "purple", "lavender": "purple",
    "crimson": "red", "burgundy": "red", "scarlet": "red", "maroon": "red",
    "cyan": "blue", "teal": "blue", "turquoise": "blue", "navy": "blue",
    "cobalt": "blue", "sky": "blue",
    "lime": "green", "olive": "green", "mint": "green", "forest": "green",
    "emerald": "green",
}

_SLOT_RE = re.compile(r"\{(\w+)\}")
_HASHTAG_RE = re.compile(r"#(?!\d)\w")
_SENTENCE_BREAK_RE = re.compile(r"[.!?]\s+\S")

_COMMON_NON_KEYWORDS = frozenset({
    "widget", "widgets", "thing", "things", "object", "objects", "part", "parts",
    "model", "models", "print", "prints", "file", "files", "item", "items",
    "piece", "pieces", "test", "tests", "sample", "samples"
})



class CorpusError(RuntimeError):
    """The roast corpus is missing or malformed."""


# ---------------------------------------------------------------- corpus

def entry_id(bank: str, text: str) -> str:
    """The Phase 1 id of one entry: its bank and a hash of its text.

    Kept for two compat jobs: entries still written as bare strings get
    this id, and a persisted Phase 1 state file's ids are translated to
    the stored stable ids through the corpus's alias map (an unchanged
    line's hash id still names it). New entries carry their id in the
    bank file itself, so editing a line no longer changes its id.
    """
    return f"{bank}:{hashlib.sha1(text.encode('utf-8')).hexdigest()[:10]}"


def parse_entry(bank: str, raw: Any) -> tuple[str, str, str]:
    """One bank entry as (id, family, text).

    The stored shape is ``{"id": ..., "family": ..., "text": ...}``; the
    id and family are stable across edits of the text. A bare string is
    the Phase 1 shape and keeps its content-hash id, as its own family.
    """
    if isinstance(raw, str):
        if not raw.strip():
            raise CorpusError(f"roast bank {bank}.json has an empty entry")
        legacy = entry_id(bank, raw)
        return legacy, legacy, raw
    if isinstance(raw, dict):
        eid, family, text = raw.get("id"), raw.get("family"), raw.get("text")
        if not (isinstance(eid, str) and eid.strip()):
            raise CorpusError(f"roast bank {bank}.json entry has no stable id: {raw!r}")
        if not (isinstance(text, str) and text.strip()):
            raise CorpusError(f"roast entry {eid} has no text")
        if not (isinstance(family, str) and family.strip()):
            family = eid
        return eid, family, text
    raise CorpusError(f"roast bank {bank}.json entry is neither text nor an entry object: {raw!r}")


def _read_bank(name: str) -> Any:
    ref = resources.files(CORPUS_PACKAGE).joinpath(*CORPUS_DIR, f"{name}.json")
    payload = json.loads(ref.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "entries" not in payload:
        raise CorpusError(f"roast bank {name}.json has no 'entries'")
    return payload["entries"]


def _read_bank_optional(name: str) -> Any:
    try:
        ref = resources.files(CORPUS_PACKAGE).joinpath(*CORPUS_DIR, f"{name}.json")
        payload = json.loads(ref.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "entries" in payload:
            return payload["entries"]
        if isinstance(payload, dict):
            return payload
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().casefold())


def _near_duplicate(text: str, others: list[str]) -> bool:
    """The corpus gate's near-duplicate rule (difflib ratio over 0.88),
    with the same quick-ratio prefilter the gate test uses."""
    folded = text.casefold()
    for other in others:
        matcher = difflib.SequenceMatcher(None, folded, other.casefold())
        if matcher.real_quick_ratio() <= NEAR_DUPLICATE_RATIO \
                or matcher.quick_ratio() <= NEAR_DUPLICATE_RATIO:
            continue
        if matcher.ratio() > NEAR_DUPLICATE_RATIO:
            return True
    return False


def load_custom_bank(path: str | Path, shipped_ids=(), shipped_texts=(),
                     blocklist=()) -> tuple[list[tuple[str, str, str]], list[str]]:
    """An operator's custom bank file as ([(id, family, text)], [problems]).

    The file is a JSON object with an ``entries`` list (a bare list is
    accepted too), each entry in the shipped banks' shape — a
    ``{"id", "family", "text"}`` object or a bare string. Every entry
    passes the same gates the shipped corpus passes in CI: the entry
    schema, known slots only, the filled contract against a fully
    populated sample print (word cap included), the blocklist, and no
    exact or near duplicates against the shipped corpus or the entries
    accepted before it. A refused entry is named in the problems and
    skipped; the valid entries still load. A missing, unreadable or
    malformed file returns no entries and one named problem — the
    caller degrades to the shipped corpus alone, never a crash.
    """
    source = str(path)
    try:
        raw_text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        return [], [f"custom roast bank {source}: could not be read "
                    f"({exc.strerror or exc}); no custom roasts loaded"]
    try:
        payload = json.loads(raw_text)
    except ValueError as exc:
        return [], [f"custom roast bank {source}: not valid JSON ({exc}); "
                    f"no custom roasts loaded"]
    if isinstance(payload, dict):
        rows = payload.get("entries")
        if not isinstance(rows, list):
            return [], [f"custom roast bank {source}: no 'entries' list; "
                        f"no custom roasts loaded"]
    elif isinstance(payload, list):
        rows = payload
    else:
        return [], [f"custom roast bank {source}: expected a JSON object "
                    f"with an 'entries' list; no custom roasts loaded"]
    shipped_norm = {_norm_text(text) for text in shipped_texts}
    sample = _sample_context()
    entries: list[tuple[str, str, str]] = []
    problems: list[str] = []
    seen_ids: set[str] = set()
    seen_texts: dict[str, str] = {}
    for raw in rows:
        try:
            eid, family, text = parse_entry(CUSTOM_BANK, raw)
        except CorpusError as exc:
            problems.append(f"custom roast bank {source}: {exc}; entry skipped")
            continue
        if eid in seen_ids:
            problems.append(f"custom roast entry {eid}: id is used twice in "
                            f"the custom bank; entry skipped")
            continue
        if eid in shipped_ids:
            problems.append(f"custom roast entry {eid}: id is already used by "
                            f"the shipped corpus; entry skipped")
            continue
        slots = set(_SLOT_RE.findall(text))
        if not slots <= set(SLOTS):
            problems.append(f"custom roast entry {eid}: unknown slot(s) "
                            f"{sorted(slots - set(SLOTS))}; entry skipped")
            continue
        norm = _norm_text(text)
        if norm in seen_texts:
            problems.append(f"custom roast entry {eid}: duplicate of custom "
                            f"entry {seen_texts[norm]}; entry skipped")
            continue
        if norm in shipped_norm:
            problems.append(f"custom roast entry {eid}: duplicate of a "
                            f"shipped roast line; entry skipped")
            continue
        others = list(shipped_texts) + [t for _i, _f, t in entries]
        if _near_duplicate(text, others):
            problems.append(f"custom roast entry {eid}: near-duplicate of an "
                            f"existing roast line; entry skipped")
            continue
        low = text.casefold()
        blocked = [word for word in blocklist
                   if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", low)]
        if blocked:
            problems.append(f"custom roast entry {eid}: blocklisted word "
                            f"{blocked[0]!r}; entry skipped")
            continue
        line = render(text, sample)
        if line is None:
            problems.append(f"custom roast entry {eid}: does not fill from a "
                            f"fully populated print; entry skipped")
            continue
        problem = contract_problem(line, blocklist)
        if problem:
            problems.append(f"custom roast entry {eid}: {problem}; entry skipped")
            continue
        seen_ids.add(eid)
        seen_texts[norm] = eid
        entries.append((eid, family, text))
    return entries, problems


@functools.lru_cache(maxsize=None)
def load_corpus(custom_path: str | None = None) -> dict[str, Any]:
    """The whole corpus: {"banks": {name: [(id, text)]}, "title_reactions":
    {keyword: [(id, text)]}, "fragments": {...}, "blocklist": [...],
    "color_roasts": {...}, "reprint_failed": {...}, "history_callbacks": {...},
    "dictionary": {...}, "dictionary_lookup": {...}, "families": {id: family},
    "aliases": {phase-1 hash id: stable id}, "version": digest}."""
    banks: dict[str, list[tuple[str, str]]] = {}
    families: dict[str, str] = {}
    aliases: dict[str, str] = {}

    def collect(bank: str, rows: Any) -> list[tuple[str, str]]:
        if not isinstance(rows, list):
            raise CorpusError(f"roast bank {bank}.json must be a list of entries")
        out = []
        for raw in rows:
            eid, family, text = parse_entry(bank, raw)
            if eid in families:
                raise CorpusError(f"roast entry id {eid} is used twice")
            families[eid] = family
            aliases[entry_id(bank, text)] = eid
            out.append((eid, text))
        return out

    for name in TEMPLATE_BANKS + ("one_liners",):
        if name == "dark_humor":
            # Optional bank (#1002 Phase 2): an install without it still loads.
            opt = _read_bank_optional(name)
            banks[name] = collect(name, opt) if isinstance(opt, list) else []
            continue
        banks[name] = collect(name, _read_bank(name))
    reactions_raw = _read_bank("title_reactions")
    if not isinstance(reactions_raw, dict):
        raise CorpusError("title_reactions.json entries must map keyword -> list")
    reactions = {str(k).lower(): collect(f"title:{k}", v) for k, v in reactions_raw.items()}
    fragments = _read_bank("fragments")
    blocklist = [str(w).lower() for w in _read_bank("blocklist")]

    def keyed(name: str, prefix: str) -> dict[str, list[tuple[str, str]]]:
        raw = _read_bank_optional(name)
        if not isinstance(raw, dict):
            return {}
        return {str(k).lower(): collect(f"{prefix}:{k}", rows)
                for k, rows in raw.items() if isinstance(rows, list)}

    color_roasts = keyed("color_roasts", "color")
    reprint_failed = keyed("reprint_failed", "reprint_failed")
    history_callbacks = keyed("history_callbacks", "history")

    # The operator's custom bank (#1002 Phase 4): validated through the
    # same gates as the shipped banks (see load_custom_bank) and merged
    # additively. Its problems are named in the log and in the returned
    # corpus; they never stop the shipped corpus from loading.
    custom_problems: list[str] = []
    banks[CUSTOM_BANK] = []
    if custom_path:
        shipped_texts = [text for rows in banks.values() for _i, text in rows]
        shipped_texts += [text for rows in reactions.values() for _i, text in rows]
        for keyed_rows in (color_roasts, reprint_failed, history_callbacks):
            shipped_texts += [text for rows in keyed_rows.values() for _i, text in rows]
        custom_entries, custom_problems = load_custom_bank(
            custom_path, shipped_ids=set(families), shipped_texts=shipped_texts,
            blocklist=blocklist)
        for eid, family, text in custom_entries:
            families[eid] = family
            aliases[entry_id(CUSTOM_BANK, text)] = eid
            banks[CUSTOM_BANK].append((eid, text))
        for problem in custom_problems:
            logger.warning("%s", problem)

    dictionary_raw = _read_bank_optional("dictionary")
    dictionary: dict[str, Any] = dictionary_raw if isinstance(dictionary_raw, dict) else {}

    dict_lookup: dict[str, str] = {}
    categories = dictionary.get("categories") if isinstance(dictionary.get("categories"), dict) else {}
    for cat_terms in categories.values():
        if not isinstance(cat_terms, list):
            continue
        for entry in cat_terms:
            if not isinstance(entry, dict):
                continue
            tag = entry.get("roast_tag")
            term = entry.get("term")
            target = None
            if isinstance(tag, str) and tag.lower() in reactions:
                target = tag.lower()
            elif isinstance(term, str) and term.lower() in reactions:
                target = term.lower()
            if target:
                keys = [term] + list(entry.get("aliases") or []) + list(entry.get("typos") or [])
                for k in keys:
                    if isinstance(k, str) and k.strip():
                        dict_lookup[k.strip().lower()] = target

    digest = hashlib.sha1(json.dumps([banks, reactions, fragments, color_roasts,
                                      reprint_failed, history_callbacks, dictionary, families],
                                     sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return {
        "banks": banks, "title_reactions": reactions, "fragments": fragments,
        "blocklist": blocklist, "color_roasts": color_roasts,
        "reprint_failed": reprint_failed, "history_callbacks": history_callbacks,
        "dictionary": dictionary, "dictionary_lookup": dict_lookup,
        "families": families, "aliases": aliases, "version": digest,
        "custom_problems": custom_problems,
    }


def corpus_size(custom_path: str | None = None) -> int:
    """Entries a roast can actually be drawn from (templates, one-liners, reactions, color, reprint, history)."""
    corpus = load_corpus(custom_path)
    return (sum(len(v) for v in corpus["banks"].values())
            + sum(len(v) for v in corpus["title_reactions"].values())
            + sum(len(v) for v in corpus.get("color_roasts", {}).values())
            + sum(len(v) for v in corpus.get("reprint_failed", {}).values())
            + sum(len(v) for v in corpus.get("history_callbacks", {}).values()))


def dictionary_size() -> int:
    """Total number of terms defined in dictionary.json."""
    corpus = load_corpus()
    cats = corpus.get("dictionary", {}).get("categories", {})
    if not isinstance(cats, dict):
        return 0
    return sum(len(v) for v in cats.values() if isinstance(v, list))


# ---------------------------------------------------------------- context

_MARKUP_RE = re.compile(r"[*_~`|>\[\]()<{}\\#]")
_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_MENTION_RE = re.compile(r"(?i)@(?:everyone|here)\b|<[@#!&][^>]*>|@\w+")
_EXT_RE = re.compile(r"(?i)(?:\.(?:gcode|bgcode|3mf|stl|step|stp|obj))+$")


def scrub_title(title: Any) -> str:
    """A title safe to put inside a sentence on every chat platform, or "".

    Strips URLs, mentions (including @everyone/@here), chat markup, control
    characters, file extensions and runs of whitespace, then bounds it to a
    few words at a word boundary. Never invents a title.
    """
    text = str(title or "")
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    text = _URL_RE.sub(" ", text)
    text = _MENTION_RE.sub(" ", text)
    text = _EXT_RE.sub("", text.strip())
    text = _MARKUP_RE.sub(" ", text).replace('"', " ")
    text = re.sub(r"[_]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -.,;:!?'")
    words = text.split()
    if len(words) > TITLE_MAX_WORDS:
        words = words[:TITLE_MAX_WORDS]
    out = ""
    for word in words:
        candidate = f"{out} {word}".strip()
        if len(candidate) > TITLE_MAX_CHARS:
            break
        out = candidate
    return out


def scrub_last_title(title: Any) -> str:
    """A prior print's title safe to put alongside the current title in a template, or "".

    Bounded to 4 words and 28 characters so {last_title} and {title} templates always
    fit inside the 20-word roast contract.
    """
    clean = scrub_title(title)
    if not clean:
        return ""
    words = clean.split()
    if len(words) > LAST_TITLE_MAX_WORDS:
        words = words[:LAST_TITLE_MAX_WORDS]
    out = ""
    for word in words:
        candidate = f"{out} {word}".strip()
        if len(candidate) > LAST_TITLE_MAX_CHARS:
            break
        out = candidate
    return out


def color_family_keys(color_str: str) -> list[str]:
    """Color families matched from raw/cleaned color name."""
    if not color_str:
        return []
    tokens = re.findall(r"[a-z0-9]+", color_str.lower())
    matched = []
    seen = set()
    for tok in tokens:
        family = tok if tok in COLOR_FAMILIES else COLOR_SYNONYMS.get(tok)
        if family and family not in seen:
            seen.add(family)
            matched.append(family)
    return matched


def _clean_value(value: Any, limit: int = 24) -> str:
    text = scrub_title(value)
    return text if 0 < len(text) <= limit else ""


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{max(minutes, 1)}m"


def hour_label(hour: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    shown = hour % 12 or 12
    return f"{shown} {suffix}"


def build_context(title: Any, *, duration_s: Any = None, material: Any = None, color: Any = None,
                  grams: Any = None, printer: Any = None, copies: Any = None,
                  print_number: Any = None, times: Any = None,
                  finish_hour: Any = None,
                  last_title: Any = None, prev_fails: Any = None,
                  fail_streak: Any = None, color_streak: Any = None,
                  top_color: Any = None) -> dict[str, Any]:
    """The roast context: slot strings plus the raw facts the banks gate on.

    A fact that is unknown, zero or unusable is simply absent, so no
    template that needs it is eligible — nothing is invented.
    """
    ctx: dict[str, Any] = {"_facts": {}}
    facts = ctx["_facts"]
    clean = scrub_title(title)
    if clean:
        ctx["title"] = clean
    if isinstance(duration_s, (int, float)) and not isinstance(duration_s, bool) and duration_s >= 60:
        ctx["duration"] = human_duration(duration_s)
        facts["duration_s"] = float(duration_s)
    for slot, value in (("material", material), ("color", color), ("printer", printer)):
        cleaned = _clean_value(value)
        if cleaned:
            ctx[slot] = cleaned
    if isinstance(grams, (int, float)) and not isinstance(grams, bool) and grams >= 1:
        ctx["grams"] = str(int(round(grams)))
        facts["grams"] = float(grams)
    if isinstance(copies, int) and not isinstance(copies, bool) and copies >= 2:
        ctx["copies"] = str(copies)
    if isinstance(print_number, int) and not isinstance(print_number, bool) and print_number > 0:
        ctx["print_number"] = f"#{print_number}"
    if isinstance(times, int) and not isinstance(times, bool) and times >= 2:
        ctx["times"] = str(times)
        facts["times"] = times
    if isinstance(finish_hour, int) and not isinstance(finish_hour, bool) and 0 <= finish_hour <= 23:
        ctx["hour"] = hour_label(finish_hour)
        facts["hour"] = finish_hour

    # History & color slots
    clean_last = scrub_last_title(last_title)
    if clean_last and clean_last.casefold() != (ctx.get("title") or "").casefold():
        ctx["last_title"] = clean_last
        facts["last_title"] = clean_last
    if isinstance(prev_fails, int) and not isinstance(prev_fails, bool) and prev_fails >= 1:
        ctx["prev_fails"] = str(prev_fails)
        facts["prev_fails"] = prev_fails
    if isinstance(fail_streak, int) and not isinstance(fail_streak, bool) and fail_streak >= 2:
        ctx["fail_streak"] = str(fail_streak)
        facts["fail_streak"] = fail_streak
    if isinstance(color_streak, int) and not isinstance(color_streak, bool) and color_streak >= 3:
        ctx["color_streak"] = str(color_streak)
        facts["color_streak"] = color_streak
    cleaned_top = _clean_value(top_color)
    if cleaned_top:
        ctx["top_color"] = cleaned_top
        facts["top_color"] = cleaned_top

    return ctx


def _sample_context() -> dict[str, Any]:
    """A fully populated print, for filling templates at validation time.

    The corpus gates fill every shipped entry against a context like
    this so a template is only ever refused for its own contract, never
    for a fact the sample lacked; custom-bank validation uses the same
    sample for the same reason.
    """
    return build_context("Benchy", duration_s=14 * 3600 + 22 * 60, material="PLA",
                         color="Galaxy Black", grams=212, printer="AD5X", copies=4,
                         print_number=57, times=5, finish_hour=3,
                         last_title="Calibration Cube", prev_fails=2,
                         fail_streak=3, color_streak=4, top_color="Black")


def _context_banks(ctx: Mapping[str, Any]) -> list[str]:
    facts = ctx.get("_facts") or {}
    duration = facts.get("duration_s")
    out = []
    if facts.get("hour") in OVERNIGHT_HOURS and (duration or 0) >= OVERNIGHT_MIN_S:
        out.append("overnight")
    if duration is not None and duration >= LONG_PRINT_S:
        out.append("long_print")
    if (facts.get("grams") or 0) >= HEAVY_GRAMS:
        out.append("heavy_filament")
    if (facts.get("times") or 0) >= 2:
        out.append("repeat_print")
    if duration is not None and duration < SHORT_PRINT_S:
        out.append("short_print")
    return out


def _title_keywords(ctx: Mapping[str, Any], corpus: Mapping[str, Any]) -> list[str]:
    """Title-reaction keywords this title names, best first."""
    from . import fuzzy

    raw = str(ctx.get("title") or "")
    if not raw:
        return []
    folded = fuzzy.typo_fold(raw)
    tokens = [w for w in folded.split() if w not in _COMMON_NON_KEYWORDS]
    fuzzy_target = " ".join(tokens)
    singles = {fuzzy.norm(k) for k in corpus["title_reactions"] if " " not in k}
    corrected = fuzzy.correct_typos(fuzzy_target, singles) if fuzzy_target else ""
    hits = []
    for keyword in corpus["title_reactions"]:
        key = fuzzy.norm(keyword)
        if key in folded or (corrected and key in corrected):
            pattern = rf"(?<![a-z0-9]){re.escape(key)}(?:s|es)?(?![a-z0-9])"
            if re.search(pattern, folded) or (corrected and re.search(pattern, corrected)):
                hits.append(keyword)
        elif fuzzy_target and " " not in key and len(key) >= 5 and fuzzy.token_match(fuzzy_target, [key])[1] == key:
            hits.append(keyword)
    dict_lookup = corpus.get("dictionary_lookup") or {}
    for term_key, target in dict_lookup.items():
        key = fuzzy.norm(term_key)
        if key in folded or (corrected and key in corrected):
            pattern = rf"(?<![a-z0-9]){re.escape(key)}(?:s|es)?(?![a-z0-9])"
            if re.search(pattern, folded) or (corrected and re.search(pattern, corrected)):
                hits.append(target)
    return sorted(set(hits), key=lambda k: (-len(k), k))



def candidate_pools(ctx: Mapping[str, Any], status: str = "completed",
                    corpus: Mapping[str, Any] | None = None) -> list[tuple[str, list[tuple[str, str]]]]:
    """Ordered (pool name, entries) to draw from for this print."""
    corpus = corpus or load_corpus()
    if status not in STATUSES:
        return []
    facts = ctx.get("_facts") or {}
    prev_fails = facts.get("prev_fails") or 0
    fail_streak = facts.get("fail_streak") or 0

    if status != "completed":
        out: list[tuple[str, list[tuple[str, str]]]] = []
        if prev_fails >= 1 and corpus.get("reprint_failed", {}).get("failed"):
            out.append(("reprint_failed:failed", corpus["reprint_failed"]["failed"]))
        if fail_streak >= 2 and corpus.get("history_callbacks", {}).get("fail_streak"):
            out.append(("history:fail_streak", corpus["history_callbacks"]["fail_streak"]))
        bank = STATUS_BANKS[status]
        out.append((bank, corpus["banks"][bank]))
        return out

    pools: list[tuple[str, list[tuple[str, str]]]] = []
    if prev_fails >= 1 and corpus.get("reprint_failed", {}).get("completed"):
        pools.append(("reprint_failed:completed", corpus["reprint_failed"]["completed"]))

    for k in _title_keywords(ctx, corpus):
        pools.append((f"title:{k}", corpus["title_reactions"][k]))

    for name in _context_banks(ctx):
        pools.append((name, corpus["banks"][name]))

    if "last_title" in ctx and corpus.get("history_callbacks", {}).get("previous_print"):
        pools.append(("history:previous_print", corpus["history_callbacks"]["previous_print"]))

    color_streak = facts.get("color_streak") or 0
    if color_streak >= 3 and corpus.get("color_roasts", {}).get("streak"):
        pools.append(("color:streak", corpus["color_roasts"]["streak"]))

    raw_color = str(ctx.get("color") or "")
    for ckey in color_family_keys(raw_color):
        if corpus.get("color_roasts", {}).get(ckey):
            pools.append((f"color:{ckey}", corpus["color_roasts"][ckey]))

    generic_pool = (corpus["banks"]["generic"]
                    + corpus["banks"].get("dark_humor", [])
                    + corpus["banks"].get(CUSTOM_BANK, [])
                    + corpus["banks"]["one_liners"])
    pools.append(("generic", generic_pool))
    return pools


# ---------------------------------------------------------------- rendering

def render(text: str, ctx: Mapping[str, Any]) -> str | None:
    """Fill a template from ``ctx``; None when it needs a slot ctx lacks."""
    needed = set(_SLOT_RE.findall(text))
    if not needed <= {k for k in ctx if not k.startswith("_")}:
        return None
    try:
        return text.format(**{k: v for k, v in ctx.items() if not k.startswith("_")})
    except (KeyError, IndexError, ValueError):
        return None


def contract_problem(line: str, blocklist: list[str] | tuple[str, ...] = ()) -> str | None:
    """Why a filled line breaks the roast contract, or None when it holds."""
    if not line or not line.strip():
        return "empty"
    if len(line) > MAX_CHARS:
        return "too long"
    if len(line.split()) > MAX_WORDS:
        return "over 20 words"
    if '"' in line or "“" in line or "”" in line:
        return "quotes"
    if _HASHTAG_RE.search(line):
        return "hashtag"
    if "\n" in line or "\r" in line:
        return "multi-line"
    if _SENTENCE_BREAK_RE.search(line.strip()):
        return "more than one sentence"
    low = line.lower()
    for word in blocklist:
        if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", low):
            return f"blocklisted word {word!r}"
    return None


# ---------------------------------------------------------------- state

def _load_state(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path | None, state: Mapping[str, Any]) -> None:
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".roast-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _normalized(state: Mapping[str, Any], corpus: Mapping[str, Any]) -> dict[str, Any]:
    """State in the current corpus's id space.

    Ids a Phase 1 state file carries are content hashes; the corpus's
    alias map names the same (unchanged) lines by their stable ids, so
    history, bags and stored choices survive the migration — and any
    later corpus edit, since ids no longer derive from the text. Ids
    with no alias (a line since removed or reworded past recognition)
    stay as they are: they match nothing eligible and age out of the
    windows on their own.
    """
    aliases = corpus.get("aliases") or {}

    def translate(eid: Any) -> Any:
        return aliases.get(eid, eid) if isinstance(eid, str) else eid

    bags = state.get("bags") if isinstance(state.get("bags"), dict) else {}
    bags = {pool: [translate(e) for e in bag if isinstance(e, str)]
            for pool, bag in bags.items() if isinstance(bag, list)}
    raw_history = state.get("history")
    history = [translate(h) for h in (raw_history if isinstance(raw_history, list) else [])
               if isinstance(h, str)][-HISTORY_WINDOW:]
    chosen = {}
    for key, value in (state.get("chosen") if isinstance(state.get("chosen"), dict) else {}).items():
        if isinstance(value, dict):
            value = {**value, "id": translate(value.get("id"))}
        chosen[key] = value
    return {"version": 1, "corpus": corpus["version"], "bags": bags,
            "history": history, "chosen": chosen}


# ---------------------------------------------------------------- selection

def _draw(pool_name: str, entries: list[tuple[str, str]], ctx: Mapping[str, Any],
          state: dict[str, Any], rng: random.Random, blocklist: list[str],
          families: Mapping[str, str] | None = None,
          ignore_history: bool = False) -> tuple[str, str] | None:
    recent = set(state["history"])
    eligible: dict[str, str] = {}
    for eid, text in entries:
        line = render(text, ctx)
        if line is not None and contract_problem(line, blocklist) is None:
            eligible[eid] = line
    if not eligible:
        return None
    if ignore_history:
        # Least recently used first: bounded, deterministic relaxation.
        order = {eid: i for i, eid in enumerate(state["history"])}
        eid = min(eligible, key=lambda e: (order.get(e, -1), e))
        return eid, eligible[eid]
    fresh = [eid for eid in eligible if eid not in recent]
    if not fresh:
        return None
    if families:
        # Family window: a line whose family was drawn recently (the line
        # itself is already excluded by the history window; this catches
        # its variants and its framed forms) is passed over while any
        # other fresh entry exists.
        recent_families = {families.get(h) for h in state["history"][-FAMILY_WINDOW:]}
        preferred = [eid for eid in fresh if families.get(eid) not in recent_families]
        if preferred:
            fresh = preferred
    bag = [eid for eid in state["bags"].get(pool_name) or [] if eid in fresh]
    if not bag:
        bag = sorted(fresh)
        rng.shuffle(bag)
    eid = bag.pop()
    state["bags"][pool_name] = bag
    return eid, eligible[eid]


# ---------------------------------------------------------------- framing

_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")


def framed_problem(line: str, blocklist: list[str] | tuple[str, ...] = ()) -> str | None:
    """Why a framed line breaks the framed contract, or None when it holds.

    Framing buys a second sentence and a higher word bound than the flat
    contract; quotes, hashtags, line breaks and the blocklist still bar
    a line outright.
    """
    if not line or not line.strip():
        return "empty"
    if len(line) > FRAMED_MAX_CHARS:
        return "too long"
    if len(line.split()) > FRAMED_MAX_WORDS:
        return "over framed word bound"
    if '"' in line or "“" in line or "”" in line:
        return "quotes"
    if _HASHTAG_RE.search(line):
        return "hashtag"
    if "\n" in line or "\r" in line:
        return "multi-line"
    if len(_SENTENCE_END_RE.findall(line)) > 2:
        return "more than two sentences"
    low = line.lower()
    for word in blocklist:
        if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", low):
            return f"blocklisted word {word!r}"
    return None


def framing_options(line: str, pool_name: str, ctx: Mapping[str, Any],
                    corpus: Mapping[str, Any]) -> list[str]:
    """Every contract-passing framed form of one drawn line.

    Generic-pool lines take an opener (only the clause-introducing
    openers, the ones ending in a comma or colon — an opener that runs
    into its clause, like "the tale of", would not parse against an
    arbitrary template) or a comparison hung off the end of the line.
    Status-pool lines take a verdict as a second sentence. Other pools
    are not framed: their lines already carry the print's specific
    facts. Fragments that need an unknown slot are skipped, never
    filled with an invented fact.
    """
    fragments = corpus.get("fragments") or {}
    blocklist = corpus.get("blocklist") or []
    out: list[str] = []

    def offer(candidate: str) -> None:
        if framed_problem(candidate, blocklist) is None and candidate not in out:
            out.append(candidate)

    if pool_name == "generic":
        for opener in fragments.get("openers") or []:
            rendered = render(opener, ctx)
            if rendered is None or not rendered.rstrip().endswith((",", ":")):
                continue
            base = line
            if rendered.rstrip().endswith(",") and base[:1].isupper() and base[1:2].islower():
                base = base[:1].lower() + base[1:]
            offer(f"{rendered} {base}")
        for comparison in fragments.get("comparisons") or []:
            rendered = render(comparison, ctx)
            if rendered is None:
                continue
            offer(f"{line.rstrip().rstrip('.!?')}, {rendered}.")
    elif pool_name in STATUS_BANKS.values():
        for verdict in fragments.get("verdicts") or []:
            rendered = render(verdict, ctx)
            if rendered is not None:
                offer(f"{line} {rendered}")
    return out


def roast(ctx: Mapping[str, Any], *, status: str = "completed", key: str | None = None,
          state_path: Path | str | None = None, rng: random.Random | None = None,
          custom_path: str | None = None) -> str:
    """One roast line for this print, or "" (best-effort, never raises).

    ``custom_path`` names the operator's custom bank file (#1002
    Phase 4); its validated entries join the generic pool for completed
    prints. A bank that cannot be used changes nothing else.
    """
    try:
        return _roast(ctx, status=status, key=key,
                      state_path=Path(state_path) if state_path else None, rng=rng,
                      custom_path=custom_path)
    except Exception:
        return ""


def _roast(ctx, *, status, key, state_path, rng, custom_path=None):
    corpus = load_corpus(custom_path)
    state = _normalized(_load_state(state_path), corpus)
    if key:
        previous = state["chosen"].get(key)
        if isinstance(previous, dict) and isinstance(previous.get("line"), str):
            return previous["line"]
    rng = rng or random.Random()
    pools = candidate_pools(ctx, status, corpus)
    picked = None
    picked_pool = None
    for name, entries in pools:
        picked = _draw(name, entries, ctx, state, rng, corpus["blocklist"],
                       families=corpus["families"])
        if picked:
            picked_pool = name
            break
    if picked is None and pools:
        # Everything eligible is in the history window: take the least
        # recently used line from the broadest pool rather than nothing.
        name, entries = pools[-1]
        picked = _draw(name, entries, ctx, state, rng, corpus["blocklist"],
                       families=corpus["families"], ignore_history=True)
        picked_pool = name
    if picked is None:
        return ""
    eid, line = picked
    if picked_pool and rng.random() < FRAMED_CHANCE:
        options = framing_options(line, picked_pool, ctx, corpus)
        if options:
            line = rng.choice(options)
    state["history"] = [h for h in state["history"] if h != eid][-(HISTORY_WINDOW - 1):] + [eid]
    if key:
        state["chosen"][key] = {"line": line, "id": eid, "at": int(time.time())}
        if len(state["chosen"]) > CHOSEN_KEEP:
            oldest = sorted(state["chosen"].items(), key=lambda kv: kv[1].get("at", 0))
            for old_key, _ in oldest[: len(state["chosen"]) - CHOSEN_KEEP]:
                state["chosen"].pop(old_key, None)
    try:
        _save_state(state_path, state)
    except OSError:
        pass  # the line still goes out; only repeat-avoidance memory is lost
    return line


__all__ = ["CUSTOM_BANK", "SLOTS", "STATUSES", "CorpusError", "build_context", "candidate_pools",
           "color_family_keys", "contract_problem", "corpus_size", "dictionary_size",
           "entry_id", "framed_problem", "framing_options", "load_corpus", "load_custom_bank",
           "parse_entry", "render", "roast", "scrub_last_title", "scrub_title"]
