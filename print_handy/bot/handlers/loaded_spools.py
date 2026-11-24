"""The loaded spools chat handler.
"""
import logging
import asyncio, re

from .common import _confirm, _control_gate, _pkw, _printer_label, _routed_printer_id, _undecided_printer_ids

# Per-module logger (#1404): records name the handler module that
# made them, so [logging] levels rules can target one handler.
logger = logging.getLogger(__name__)


_LOADED_NL_RES = (
    re.compile(r"(?:please\s+)?(?:what|which)(?:'s|\s+is|\s+are)?\s+(?:the\s+)?(?:colou?rs?|filaments?|spools?)\s+(?:is\s+|are\s+)?loaded\s*[.!?]?", re.I),
    re.compile(r"(?:please\s+)?(?:what|which)(?:'s|\s+is)\s+loaded\s*[.!?]?", re.I),
    re.compile(r"(?:current|loaded)\s+(?:spools?|filaments?|colou?rs?)\s*[.!?]?", re.I),
    re.compile(r"(?:please\s+)?(?:show|list)(?:\s+me)?\s+(?:the\s+)?(?:loaded|current)\s+(?:spools?|filaments?|colou?rs?)\s*[.!?]?", re.I),
    re.compile(r"print[-\s]colou?rs?\s*[.!?]?", re.I),
    re.compile(r"(?:please\s+)?how\s+much\s+filament\s+is\s+left(?:\s+on\s+(?:the\s+)?spools?)?\s*[.!?]?", re.I),
)


def _loaded_spools_intent(text):
    """Deterministic plain-text asks for the loaded-filament view (#744), else None.

    Anchored full-message patterns in the style of _spool_nl_intent (#475):
    'what colors are loaded', 'current spools', 'show me the loaded
    filament'. Discussion about loaded filament ('the loaded filament is
    tangled') never matches, and this runs with no LLM configured, so the
    Telegram pure-bot path answers it too.
    """
    text = ' '.join(str(text or '').split()).strip()
    if not text or text.startswith('/'):
        return None
    return 'loaded' if any(pattern.fullmatch(text) for pattern in _LOADED_NL_RES) else None


# Fuzzy phrase targets for the loaded view (#744, fuzzy extension): the
# canonical asks a near miss is measured against ("how much filament is
# left" is the same question with no word "loaded" in it, so it is both
# an exact pattern above and a typo target here). Rivals are the neighbouring
# spool intents a loaded ask must beat clearly before it may even be
# suggested — a fuzzy tie with "change the filament" is not a loaded ask.
_LOADED_FUZZY_PHRASES = (
    "what colors are loaded",
    "what color is loaded",
    "what filament is loaded",
    "what filaments are loaded",
    "which spools are loaded",
    "which filament is loaded",
    "which filaments are loaded",
    "current spools",
    "loaded filament",
    "loaded spools",
    "loaded colors",
    "loaded colours",
    "what is loaded",
    "show the loaded spools",
    "print colors",
    "how much filament is left",
    "how much filament is left on the spools",
    "how much filament is loaded",
)
_LOADED_FUZZY_RIVALS = (
    "add new filament",
    "new spool",
    "change the filament",
    "swap the filament",
    "replace the filament",
    "load the filament",
    # "Load a spool" is a load request (#1524 tranche 2 routes it to the
    # guided /spool change); one letter from "loaded spools", it must
    # lose to these, not earn a "what is loaded" prompt.
    "load a spool",
    "load the spool",
    "load a roll",
)

_LOADED_SUGGEST_YES = 'Yes — show it'
_LOADED_SUGGEST_NO = 'No'
_LOADED_SUGGEST_TIMEOUT_S = 60  # the #468 stage-3 suggestion expiry


def _loaded_spools_fuzzy(text):
    """True when a plain message is a clear fuzzy near-miss of a loaded ask.

    Measured with the shared scorer (core.fuzzy) against the canonical
    phrases above, family against family: the shared decide() policy
    must call the loaded family the single direct answer — high
    confidence with a clear margin over the rival spool intents — or
    the message is not a loaded ask at all. Grey-zone and contested
    scores fall through to the usual lanes, exactly as #468 prescribes:
    fuzzy here may only ever earn a did-you-mean prompt, never an
    unasked answer. Deterministic, offline, no LLM involved.
    """
    text = ' '.join(str(text or '').split()).strip()
    if not text or text.startswith('/'):
        return False
    from print_handy.core import fuzzy
    best: dict[str, int] = {}
    for family, phrases in (("loaded", _LOADED_FUZZY_PHRASES), ("other", _LOADED_FUZZY_RIVALS)):
        for phrase in phrases:
            score = fuzzy.fuzzy_score(text, phrase)
            if score > best.get(family, 0):
                best[family] = score
    scored = [(score, family) for family, score in best.items() if score]
    if not scored:
        return False
    decision = fuzzy.decide(scored)
    return decision.kind == "direct" and decision.best == "loaded"


def slot_answer(parsed, source, gateway, *, cfg=None):
    """Resolve the channel before looking up a physical slot; never guess a printer."""
    from print_handy import settings
    from print_handy.runtime import spool_sync
    from .common import _channel_printer_id

    slot = parsed.slots["slot"]
    wanted = str(parsed.slots.get("printer_name") or "").strip().casefold()
    try:
        candidates = _undecided_printer_ids(source, gateway)
        pid = _channel_printer_id(source, gateway)
        entries = settings.printers(cfg)
        names = {str(entry["id"]): str(entry.get("name") or entry["id"]) for entry in entries}
        stamped = getattr(source, "printer_id", None)
        if (candidates and isinstance(stamped, str) and stamped in names
                and not getattr(source, "shared_printer_ids", None)
                and getattr(gateway, "shared_printer_ids_for", None) is None):
            # A source stamped with its printer has decided it, even when that
            # printer is the default one _routed_printer_id normalizes away;
            # only a truly shared channel is undecided.
            candidates, pid = None, stamped
        if candidates:
            allowed = [str(candidate) for candidate in candidates]
        elif pid:
            allowed = [pid]
        else:
            allowed = [str(settings.default_printer_id(cfg))]
        if wanted:
            matches = [p for p in allowed if wanted in (p.casefold(), names.get(p, p).casefold())]
            if len(matches) != 1:
                return f"Which printer for slot {slot}? Choose " + " or ".join(names.get(p, p) for p in allowed) + "."
            pid = matches[0]
        elif len(allowed) != 1:
            options = " or ".join(names.get(p, p) for p in allowed)
            return (f"Which printer for slot {slot}: {options}? "
                    f"Ask 'Which spool is in slot {slot} on <printer name>?'")
        else:
            pid = allowed[0]
        label = names.get(pid, pid)
        return f"{label} — " + spool_sync.loaded_slot_text(slot, printer_id=pid)
    except Exception:
        logger.exception("could not read printer slot")
        return f"Slot {slot}: I couldn't read the printer's spool record. Nothing was changed."


async def loaded_spools_dispatch(event, gateway, **kwargs):
    """/print-spools and its twins (#744): what filament is loaded, where,
    and how much is left — read-only, no confirmation, never the shelf.

    Trigger forms: `/print-spools`, `/spool loaded`, a bare `/print-colors`
    (no job named — the archived-job form `/print-colors <job>` keeps
    its meaning and is claimed earlier by color_selection_dispatch), and
    the anchored plain-text asks in _loaded_spools_intent. Runs before
    spool_dispatch so '/spool loaded' is not read as a spool change verb.

    A clear fuzzy near-miss of a plain ask (_loaded_spools_fuzzy) earns
    the #468 did-you-mean prompt instead: the view is shown only when
    the user presses Yes — a fuzzy match never answers by itself.
    """
    text = str(getattr(event, 'text', '')).strip()
    folded = ' '.join(text.split())
    from ..natural_language import slot_query
    precise = slot_query(text)
    if precise:
        source, adapter = _control_gate(event, gateway)
        if source is None:
            return None
        if adapter is None:
            return {'action': 'skip', 'reason': 'loaded slot UI delivery unavailable'}
        metadata = {'thread_id': source.thread_id} if source.thread_id else None
        message = await asyncio.to_thread(slot_answer, precise, source, gateway)
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
        return {'action': 'skip', 'reason': 'loaded slot lookup handled'}
    exact = (folded in ('/print-spools', '/print-colors', '/spool loaded')
             or folded.startswith('/print-spools ')
             or bool(_loaded_spools_intent(text)))
    if not exact and not _loaded_spools_fuzzy(text):
        return None
    source, adapter = _control_gate(event, gateway)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'loaded spools UI delivery unavailable'}
    from print_handy.runtime import spool_sync
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    if not exact:
        answer = await _confirm(adapter, source, metadata, 'spools-suggest',
                                'Did you mean "what colors are loaded"? I can show the filament '
                                'that is loaded, where it is, and how much is left.',
                                [_LOADED_SUGGEST_YES, _LOADED_SUGGEST_NO],
                                _LOADED_SUGGEST_TIMEOUT_S,
                                live_key=gateway._session_key_for_source(source))
        if not (isinstance(answer, str) and answer.strip().casefold()
                in (_LOADED_SUGGEST_YES.casefold(), 'yes', 'y')):
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content='Okay — nothing was shown.')
            return {'action': 'skip', 'reason': 'loaded spools suggestion declined'}
    pid = _routed_printer_id(source, gateway)
    candidates = _undecided_printer_ids(source, gateway) if not pid else None
    try:
        if candidates:
            # A shared channel (#536) decides no printer: like /spool list,
            # the read-only view names each printer's loaded filament.
            sections = []
            for candidate in candidates:
                body = await asyncio.to_thread(spool_sync.loaded_text, printer_id=candidate)
                sections.append(f'**{_printer_label(candidate)}**\n{body}')
            message = '\n\n'.join(sections)
        else:
            message = await asyncio.to_thread(spool_sync.loaded_text, **_pkw(pid))
    except Exception:
        logger.exception('/print-spools failed')
        message = ('I could not read the spool record just now, so I cannot say what is loaded. '
                   'Nothing was changed.')
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'loaded spools view handled'}


# --- handler interface (#1323) ---
from ._gates import free_text  # noqa: E402

match = free_text
handle = loaded_spools_dispatch
