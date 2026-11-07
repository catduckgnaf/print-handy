"""Roasts on request, in plain language (#1002): "roast my last print".

The chat side of the local roast engine (core/roast.py). Someone types
"roast my last print", "roast print 84", "roast the benchie" or "rost it" and
gets one line about that print, drawn from the local corpus. No LLM at
all, and nothing on the printer is touched: this is read-only.

Understanding the request reuses the shared natural-language layer instead
of a private one:

* the words go through ``fuzzy.correct_typos`` against this request's own
  vocabulary first, the same second pass the plain-language task intents use
  ("rost my lst print" -> "roast my last print");
* which print is meant resolves exactly the way ``print 84`` and
  ``/print-again <name>`` resolve: per-launch numbers, exact or substring
  title match (``print_control.match_printed``), and for a near miss the
  same "Did you mean" pick-list (``fuzzy_printed``). A fuzzy hit is never
  roasted on a guess.

The print's own record decides the status bank: a failed or cancelled
print gets a gentle line about that, never success language.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core import fuzzy

_VOCABULARY = frozenset({"roast", "print", "last", "latest", "recent", "previous", "again"})
_ROAST_RE = re.compile(
    r"^(?:please\s+|pls\s+)?(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|go\s+ahead\s+and\s+|now\s+)?"
    r"roast\s+(?P<q>.*?)\s*[?.!]*$", re.I)
_LAST_WORDS = {"", "it", "that", "this", "me", "us", "one", "last", "latest", "recent", "previous",
               "last print", "latest print", "recent print", "previous print", "last one",
               "that print", "this print", "the print", "my print", "it again", "that one",
               "this one", "the last one", "my last one"}
_LEADERS = re.compile(r"^(?:my|our|the|that|this|your)\s+", re.I)
_TRAILERS = re.compile(r"\s+(?:print|one|please|again|for\s+me)$", re.I)
_NUMBER_RE = re.compile(r"^(?:print\s+)?(?:number\s+|no\.?\s*)?#?(\d+)$", re.I)
STATUS_MAP = {"completed": "completed", "complete": "completed", "success": "completed",
              "failed": "failed", "error": "failed", "cancelled": "cancelled",
              "canceled": "cancelled"}


def roast_request(text: Any) -> str | None:
    """The print query of a roast request ("" = the last print), or None.

    Typo tolerant through the shared fuzzy layer. Only messages that ask
    for a roast count: "roast" must lead the request, so chat that merely
    mentions roasting is left alone.
    """
    clean = " ".join(str(text or "").split()).strip()
    if not clean or clean.startswith("/") or len(clean) > 120:
        return None
    match = _ROAST_RE.match(clean)
    if match is None:
        corrected = fuzzy.correct_typos(clean, _VOCABULARY)
        match = _ROAST_RE.match(corrected)
        if match is None:
            return None
    query = " ".join(match.group("q").split()).strip(" ?.!,")

    def _bare(text):
        for _ in range(3):
            text = _TRAILERS.sub("", _LEADERS.sub("", text)).strip()
        return text

    # "the lasst one" -> last print: the request words are typo-corrected;
    # a model name keeps the person's own spelling for the title search.
    corrected = _bare(fuzzy.correct_typos(query, _VOCABULARY)) if query else ""
    if corrected.casefold() in _LAST_WORDS:
        return ""
    number = _NUMBER_RE.match(corrected)
    if number:
        return number.group(1)
    query = _bare(query)
    if re.search(r"https?://", query, re.I):
        return None  # a roast of a link is not a printed job
    return query


class RoastLookupError(LookupError):
    """No single printed job matches; the message says what to type."""


def resolve(query: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The printed_jobs row a roast query means (newest first rows)."""
    from . import print_control as pc

    if not rows:
        raise RoastLookupError("Nothing has been printed yet, so there is nothing to roast.")
    if not query:
        return rows[0]
    if query.isdigit():
        hit = next((r for r in rows if r["index"] == int(query)), None)
        if hit is None:
            raise RoastLookupError(f"There is no print #{int(query)}. Type /print-again list to see them.")
        return hit
    hits = pc.match_printed(query, rows)
    if hits:
        titles = {pc._fold(r["title"]) for r in hits}
        if len(titles) == 1:
            return hits[0]  # one model, maybe printed several times: its newest print
        options = "\n".join(f"{r['index']}. {r['title']}" for r in hits[: pc.AGAIN_LIST_LIMIT])
        raise RoastLookupError(f"\"{query}\" matches several prints:\n{options}\n"
                               "Pick one with: roast print <number>")
    near = pc.fuzzy_printed(query, rows)
    if near:
        options = "\n".join(f"{r['index']}. {r['title']}" for r in near[: pc.AGAIN_LIST_LIMIT])
        raise RoastLookupError(f"Nothing printed is called \"{query}\". Did you mean one of these?\n"
                               f"{options}\nPick one with: roast print <number>")
    raise RoastLookupError(f"Nothing printed is called \"{query}\". Type /print-again list to see them.")


def _record_for(number: int) -> dict[str, Any]:
    from . import print_records

    path = Path(print_records.PRINTS_DIR) / f"print-{int(number):04d}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def facts_for(row: dict[str, Any], tz=None) -> tuple[dict[str, Any], str]:
    """(facts, status) for one printed_jobs row, from its record and job."""
    from . import discord_print_summary as summary
    from . import print_control as pc

    facts: dict[str, Any] = {"print_number": int(row["index"])}
    record = _record_for(row["index"])
    status = STATUS_MAP.get(str(record.get("status") or "").casefold(), "completed")
    try:
        job = json.loads(Path(row["job_json"]).read_text(encoding="utf-8"))
        plate = next(iter(summary.plates_of(job)), {})
    except (OSError, ValueError, TypeError):
        job, plate = {}, {}
    duration = record.get("print_duration_seconds")
    if not isinstance(duration, (int, float)) or duration <= 0:
        duration = plate.get("seconds")
    if isinstance(duration, (int, float)) and duration > 0:
        facts["duration_s"] = float(duration)
    for key, record_key in (("material", "filament_material"), ("color", "filament_color")):
        value = record.get(record_key) or plate.get(key)
        if value:
            facts[key] = str(value)
    grams = record.get("filament_used_g")
    if not isinstance(grams, (int, float)) or grams <= 0:
        grams = plate.get("filament_g")
    if isinstance(grams, (int, float)) and grams > 0:
        facts["grams"] = float(grams)
    printer = summary.announcement_printer_name(job) if job else None
    if printer:
        facts["printer"] = str(printer)
    try:
        launches = pc._load_number_registry()["launches"]
        times = sum(1 for e in launches if e.get("job") == str(row["job_json"]))
        if times >= 2:
            facts["times"] = times
    except Exception:
        pass
    finished = record.get("completed_at")
    try:
        if finished:
            when = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
            facts["finish_hour"] = (when.astimezone(tz) if tz else when).hour
    except ValueError:
        pass
    return facts, status


def roast_reply(text: Any, *, rows=None, state_path=None, rng=None, tz=None) -> str | None:
    """The reply to a roast request, or None when ``text`` is not one."""
    from ..core import roast as engine
    from . import print_control as pc

    query = roast_request(text)
    if query is None:
        return None
    try:
        row = resolve(query, rows if rows is not None else pc.printed_jobs())
    except RoastLookupError as exc:
        return str(exc)
    facts, status = facts_for(row, tz=tz)
    ctx = engine.build_context(row["title"], **facts)
    line = engine.roast(ctx, status=status, state_path=state_path, rng=rng)
    if not line:
        return f"I could not find a roast that fits print #{row['index']} right now."
    return f"Print #{row['index']}, {engine.scrub_title(row['title']) or 'that print'}: {line}"


__all__ = ["RoastLookupError", "facts_for", "resolve", "roast_reply", "roast_request"]
