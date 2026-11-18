"""The print statistics chat handler (#1647).

/print-stats: the print history's totals, per user and per printer.
Read-only and informational: it never blocks, delays or refuses a
print. With no selection it answers the channel / instance totals
exactly as the records aggregate today, unattributed prints included.
"me", an @mention, a user name, or "printer <name>" narrows the same
aggregation to that starter or printer — the filtering is
print_stats.filter_records, the one implementation the Web UI Data
tab and Pasta Wrapped page also use, so the numbers cannot drift.
"wrapped" answers the selection's Pasta Wrapped recap instead, under
the same December 1 reveal rule as every Wrapped surface.
"""
import logging
import asyncio
import re

from .common import _control_preamble

logger = logging.getLogger(__name__)

_MENTION = re.compile(r"<@!?(\d+)>")
_MATRIX_ID = re.compile(r"@[A-Za-z0-9.\-]+:[A-Za-z0-9.\-]+")
_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")
_WRAPPED = re.compile(r"\bwrapped\b", re.I)
_ME = re.compile(r"\b(?:me|my|mine)\b", re.I)
_FILLER = {"for", "the", "a", "an", "stats", "statistics", "printing", "print",
           "show", "please", "pasta", "of", "on"}


def _records(data):
    from print_handy.core import print_stats
    vault = ((data.get("paths") or {}).get("records_vault")) or "."
    try:
        records, _skipped = print_stats.load_records(vault)
    except Exception as exc:
        logger.warning("stats: could not read the print records: %s", exc)
        return []
    return records


def _tz():
    try:
        from print_handy.runtime import _config
        return _config.tz()
    except Exception as exc:
        logger.warning("stats: could not read the display timezone: %s", exc)
        return None


def _events(data, rows, *, scoped):
    """The watchdog events for a Wrapped answer: every recorded event
    for the unfiltered recap, only the selection's prints' events for
    a filtered one. None keeps the honest not-recorded pasta state."""
    from print_handy.core import print_stats
    vault = ((data.get("paths") or {}).get("records_vault")) or "."
    try:
        loaded, _skipped, exists = print_stats.load_watchdog_events(vault)
    except Exception as exc:
        logger.warning("stats: could not read the watchdog events: %s", exc)
        return None
    if not exists:
        return None
    return print_stats.filter_events_for_records(loaded, rows) if scoped else loaded


def _known_printers(data, records):
    """Printer names a query can name: the printers the records carry,
    plus the configured roster's names (a roster printer with no
    prints yet still resolves, and answers zero honestly)."""
    from print_handy.core import print_stats
    names = [row["name"] for row in print_stats.printer_options(records)]
    seen = {name.lower() for name in names}
    for entry in data.get("printers") or []:
        if isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("id") or "").strip()
            if name and name.lower() not in seen:
                names.append(name)
                seen.add(name.lower())
    return names


def _resolve_printer(candidate, printers):
    """The canonical printer name for ``candidate``, or None."""
    wanted = " ".join(str(candidate or "").split()).strip().lower()
    if not wanted:
        return None
    for name in printers:
        if name.lower() == wanted:
            return name
    return None


def _resolve_starter_by_name(candidate, options, platform):
    """(option, ambiguous) for a typed user name: an exact display-name
    or user-id match among the recorded starters. Two starters sharing
    one display name resolve to the asker's own platform when that
    settles it; otherwise the match is ambiguous and nothing is
    guessed."""
    wanted = " ".join(str(candidate or "").split()).strip().lower()
    if not wanted:
        return None, False
    matches = [row for row in options
               if row["label"].lower() == wanted or row["user_id"].lower() == wanted
               or row["key"].lower() == wanted]
    if len(matches) <= 1:
        return (matches[0] if matches else None), False
    own = [row for row in matches if row["platform"] == platform]
    if len(own) == 1:
        return own[0], False
    return None, True


def parse_selection(text, records, data, *, platform, requester_id, requester_name=None):
    """Parse a /print-stats query into a selection.

    Returns {"wrapped", "year", "starter_key", "user_label",
    "printer", "error"}: the filters to hand print_stats, the label
    parts for the answer, or one error sentence naming what could not
    be resolved (an unknown name is an answer, never a guess).
    """
    from print_handy.core import print_stats
    out = {"wrapped": False, "year": None, "starter_key": None,
           "user_label": None, "printer": None, "error": None}
    work = " ".join(str(text or "").split()).strip()
    if work.startswith("/"):
        parts = work.split(None, 1)
        work = parts[1] if len(parts) > 1 else ""
    if _WRAPPED.search(work):
        out["wrapped"] = True
        work = _WRAPPED.sub(" ", work)
    year = _YEAR.search(work)
    if year:
        out["year"] = int(year.group(1))
        work = work[:year.start()] + " " + work[year.end():]
    work = " ".join(work.split()).strip(" ,")

    printers = _known_printers(data, records)
    options = print_stats.starter_options(records)

    mention = _MENTION.search(work)
    matrix = _MATRIX_ID.search(work) if not mention else None
    if mention:
        user_id = mention.group(1)
        out["starter_key"] = f"{platform}:{user_id}"
        known = next((row for row in options if row["key"] == out["starter_key"]), None)
        out["user_label"] = known["label"] if known else f"user {user_id}"
        work = (work[:mention.start()] + " " + work[mention.end():]).strip()
    elif matrix:
        user_id = matrix.group(0)
        out["starter_key"] = f"matrix:{user_id}"
        known = next((row for row in options if row["key"] == out["starter_key"]), None)
        out["user_label"] = known["label"] if known else user_id
        work = (work[:matrix.start()] + " " + work[matrix.end():]).strip()

    printer_phrase = re.search(r"\bprinter\s+(.+)$", work, re.I)
    on_phrase = None if printer_phrase else re.search(r"\bon\s+(?:the\s+)?(.+)$", work, re.I)
    phrase = printer_phrase or on_phrase
    if phrase:
        candidate = phrase.group(1)
        resolved = _resolve_printer(candidate, printers)
        if resolved is None:
            known = ", ".join(printers) if printers else "none recorded yet"
            out["error"] = (f"No printer named \"{candidate.strip()}\" in the print "
                            f"history. Printers with recorded prints: {known}.")
            return out
        out["printer"] = resolved
        work = work[:phrase.start()].strip()

    if out["starter_key"] is None and _ME.search(work):
        out["starter_key"] = f"{platform}:{requester_id}"
        out["user_label"] = requester_name or "you"
        work = _ME.sub(" ", work)

    rest = " ".join(word for word in work.split()
                    if word.lower() not in _FILLER).strip(" ,")
    if rest and out["starter_key"] is None:
        option, ambiguous = _resolve_starter_by_name(rest, options, platform)
        if option is not None:
            out["starter_key"] = option["key"]
            out["user_label"] = option["label"]
            rest = ""
        elif ambiguous:
            out["error"] = (f"More than one recorded starter is named \"{rest}\"; "
                            "@mention the one you mean.")
            return out
    if rest and out["printer"] is None:
        resolved = _resolve_printer(rest, printers)
        if resolved is not None:
            out["printer"] = resolved
            rest = ""
    if rest and out["starter_key"] is None:
        starters = ", ".join(row["label"] for row in options) or "none yet"
        printer_names = ", ".join(printers) or "none yet"
        out["error"] = (f"I could not tell who or which printer \"{rest}\" means. "
                        f"Recorded starters: {starters}. Printers: {printer_names}. "
                        "Try /print-stats me, /print-stats @user, or "
                        "/print-stats printer <name>.")
    return out


def _stats_lines(stats, label, year):
    from print_handy.core import print_stats
    total = stats["total_prints"]
    scope = f" in {year}" if year else ""
    if not total:
        return [f"No prints recorded for {label}{scope} yet."]
    lines = [f"Print statistics — {label}{scope}:"]
    rate = (f", {round(stats['success_rate'] * 100):g}% success"
            if stats["success_rate"] is not None else "")
    lines.append(f"- {print_stats.plural(total, 'print')}: {stats['completed']} completed, "
                 f"{stats['failed']} failed, {stats['canceled']} stopped{rate}.")
    lines.append(f"- {print_stats.hours_text(stats['total_hours'])} of printing, "
                 f"{stats['total_filament_used_kg']:g} kg of filament.")
    breakdowns = stats["breakdowns"]
    top_material = next((name for name, row in breakdowns["by_material"].items()
                         if name != print_stats.UNKNOWN), None)
    top_color = next((name for name, row in breakdowns["by_color"].items()
                      if name != print_stats.UNKNOWN), None)
    tops = []
    if top_material:
        tops.append(f"top material {top_material} "
                    f"({breakdowns['by_material'][top_material]['count']})")
    if top_color:
        tops.append(f"top color {top_color} "
                    f"({breakdowns['by_color'][top_color]['count']})")
    if tops:
        lines.append("- " + "; ".join(tops) + ".")
    return lines


def stats_answer(text, data=None, *, platform="discord", requester_id=None,
                 requester_name=None):
    """The statistics answer text for one ask. Pure apart from
    settings/records reads, like cost_answer."""
    from print_handy import settings as _settings
    from print_handy.core import print_stats

    data = data if data is not None else _settings.current()
    records = _records(data)
    if not records:
        return "No prints recorded yet, so there are no statistics to show."
    selection = parse_selection(text, records, data, platform=platform,
                                requester_id=requester_id,
                                requester_name=requester_name)
    if selection["error"]:
        return selection["error"]
    filtered = print_stats.filter_records(
        records, starter=selection["starter_key"], printer=selection["printer"])
    descriptor = print_stats.selection_descriptor(
        user_label=selection["user_label"], printer=selection["printer"])
    label = descriptor["label"] if descriptor else "all prints"
    tz = _tz()
    if selection["wrapped"]:
        return _wrapped_answer(filtered, data, selection, descriptor, tz)
    stats = print_stats.aggregate(filtered, year=selection["year"], tz=tz)
    lines = _stats_lines(stats, label, selection["year"])
    if descriptor is None:
        by_starter = stats["breakdowns"]["by_starter"]
        if by_starter:
            parts = [f"{name} {row['count']}" for name, row in
                     list(by_starter.items())[:6]]
            lines.append("- By starter: " + ", ".join(parts) + ".")
    return "\n".join(lines)


def _wrapped_answer(filtered, data, selection, descriptor, tz):
    from print_handy.core import print_stats
    label = descriptor["label"] if descriptor else "all prints"
    years = print_stats.years_available(filtered, tz)
    year = selection["year"] or (years[-1] if years else None)
    if year is None:
        return f"No prints recorded for {label} yet, so there is no Pasta Wrapped to show."
    try:
        from print_handy.webui import data as _webui_data
        revealed = _webui_data.wrapped_revealed(data, year)
    except Exception as exc:
        logger.warning("stats: could not read the Wrapped reveal state, "
                       "answering as revealed: %s", exc)
        revealed = True
    if not revealed:
        return (f"Pasta Wrapped {year} is sealed until December 1, like every "
                "Wrapped surface. The year's statistics are open now: "
                "/print-stats answers those.")
    events = _events(data, filtered, scoped=descriptor is not None)
    payload = print_stats.build_wrapped(filtered, year, tz=tz, events=events,
                                        selection=descriptor)
    lines = list(payload["script"])
    text = "\n".join(lines)
    if len(text) > 1900:
        kept = []
        size = 0
        for line in lines:
            if size + len(line) + 1 > 1800:
                break
            kept.append(line)
            size += len(line) + 1
        kept.append("The full recap is on the Web UI Pasta Wrapped page.")
        text = "\n".join(kept)
    return text


async def stats_dispatch(event, gateway, **kwargs):
    """/print-stats: the history's totals for one selection (#1647)."""
    source, adapter = _control_preamble(event, gateway, '/print-stats', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'stats UI delivery unavailable'}
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    text = str(getattr(event, 'text', '')).strip()
    from ..runtime import platform_name_of
    try:
        message = await asyncio.to_thread(
            stats_answer, text, platform=platform_name_of(source),
            requester_id=str(source.user_id),
            requester_name=getattr(source, 'display_name', None))
    except Exception:
        logger.exception('/print-stats failed')
        message = 'Could not read the print statistics just now. Nothing was changed.'
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'print stats handled'}


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/print-stats', True)
handle = stats_dispatch
handle.dispatch_gate = match
