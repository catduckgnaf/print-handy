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


def history_facts(current_title: str, current_gcode: str = "", current_color: str = "",
                  exclude_record_id: str = "", prints_dir: Path | None = None,
                  status: str = "", exclude_number: int | None = None) -> dict[str, Any]:
    """Extract user print-history facts for local roasts (#1002 Phase 2).

    Reads records from prints_dir (via print_stats.load_records_from_dir)
    sorted chronologically by started_at. Fail-soft returning {} on any error.
    """
    try:
        from ..core import print_stats
        if prints_dir is None:
            try:
                from . import print_records
                prints_dir = getattr(print_records, "PRINTS_DIR", None)
            except Exception:
                prints_dir = None
        if prints_dir is None or not Path(prints_dir).is_dir():
            return {}
        records, _ = print_stats.load_records_from_dir(prints_dir)
        if not records:
            return {}

        def _sort_key(r):
            return str(r.get("started_at") or r.get("completed_at") or "")

        records.sort(key=_sort_key)

        # Filter out current and subsequent prints
        filtered = []
        for r in records:
            p_num = r.get("print_number")
            if exclude_number is not None and isinstance(p_num, int):
                if p_num >= exclude_number:
                    continue
            r_id = str(r.get("record_id") or "")
            r_key = str(print_stats.record_key(r)) if hasattr(print_stats, "record_key") else ""
            if exclude_record_id and (r_id == exclude_record_id or r_key == exclude_record_id):
                continue
            filtered.append(r)
        records = filtered

        out: dict[str, Any] = {}
        curr_title_fold = " ".join(str(current_title or "").split()).casefold()
        curr_gcode_stem = " ".join(
            str(current_gcode or "").replace("\\", "/").rsplit("/", 1)[-1].split()
        ).casefold()

        # 1. last_title: title of the most recent prior record (whose title != current_title)
        for r in reversed(records):
            t = str(r.get("title") or "").strip()
            if t and " ".join(t.split()).casefold() != curr_title_fold:
                out["last_title"] = t
                break

        # 2. prev_fails: number of prior records with the same print identity
        # whose status was in ("failed", "cancelled", "canceled")
        prev_fails = 0
        for r in records:
            r_status = str(r.get("status") or "").strip().lower()
            if r_status in ("failed", "cancelled", "canceled"):
                r_title = " ".join(str(r.get("title") or "").split()).casefold()
                r_gcode = " ".join(
                    str(r.get("gcode_file") or "").replace("\\", "/").rsplit("/", 1)[-1].split()
                ).casefold()
                matches = False
                if curr_title_fold and r_title == curr_title_fold:
                    matches = True
                elif curr_gcode_stem and r_gcode == curr_gcode_stem:
                    matches = True
                if matches:
                    prev_fails += 1
        if prev_fails >= 1:
            out["prev_fails"] = prev_fails

        # 3. fail_streak: count of consecutive trailing failed/cancelled records
        # immediately preceding (and including, if current status is failed/cancelled) this job
        fail_streak = 1 if status.lower() in ("failed", "cancelled", "canceled") else 0
        for r in reversed(records):
            r_status = str(r.get("status") or "").strip().lower()
            if r_status in ("failed", "cancelled", "canceled"):
                fail_streak += 1
            else:
                break
        if fail_streak >= 2:
            out["fail_streak"] = fail_streak

        # 4. color_streak: count of consecutive trailing records (including current)
        # with the same normalized color name (when >= 3)
        curr_color_clean = str(current_color or "").strip().casefold()
        if curr_color_clean:
            color_streak = 1
            for r in reversed(records):
                r_color = str(r.get("filament_color") or r.get("color") or "").strip().casefold()
                if r_color and r_color == curr_color_clean:
                    color_streak += 1
                else:
                    break
            if color_streak >= 3:
                out["color_streak"] = color_streak

        # 5. top_color: most frequent filament_color across records if it appears >= 3 times
        from collections import Counter
        color_counts = Counter()
        for r in records:
            c = str(r.get("filament_color") or r.get("color") or "").strip()
            if c:
                color_counts[c] += 1
        if str(current_color or "").strip():
            color_counts[str(current_color).strip()] += 1
        if color_counts:
            most_common_color, count = color_counts.most_common(1)[0]
            if count >= 3:
                out["top_color"] = most_common_color

        return out
    except Exception:
        return {}


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

    # History facts (#1002 Phase 2)
    record_id = str(record.get("record_id") or "")
    rec_title = str(row.get("title") or record.get("title") or "")
    rec_gcode = str(record.get("gcode_file") or (job or {}).get("filename") or "")
    rec_color = str(facts.get("color") or "")
    hf = history_facts(rec_title, current_gcode=rec_gcode, current_color=rec_color,
                       exclude_record_id=record_id, status=status,
                       exclude_number=int(row["index"]))
    facts.update(hf)

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


__all__ = ["RoastLookupError", "facts_for", "history_facts", "resolve", "roast_reply", "roast_request"]
