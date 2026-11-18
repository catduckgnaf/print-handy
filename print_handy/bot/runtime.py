"""The standalone bot's message flow, independent of any chat client so tests can drive it.

A message from a user the platform's access policy allows in the print channel, on whichever platform
``[bot] platform`` names, becomes the same event the Hermes
gateway hands the plugin: ``source`` (who and where), ``text``, ``media_urls`` (dropped model files,
saved locally) and ``message_id``. The router runs the plugin's handlers on it. What no handler takes
answers an open button question of the same person, or else gets the optional LLM or the short
unmatched reply (#467: never the full command list as an error response).
A user a list refuses is never dropped silently (#386): the refusal is logged at INFO with the reason
and answered in channel once per user per hour.

The core talks to the chat service only through the platform interface in
``print_handy.bot.platform``; the Discord entry point passes exactly the
values it always used, so Discord sessions, sources and replies are unchanged.
"""
from __future__ import annotations

import asyncio
import contextvars
import collections
import contextlib
import inspect
import logging
import os
import re
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from .. import settings
from ..core import fuzzy, prompts
from ..runtime import _config
from . import agent, commands, llm, mention, platform, provenance, rest, routing
from .router import Router

logger = logging.getLogger(__name__)

PROFILE = "3dprinting"  # the plugin's handlers only answer events of the print profile
# A refused user hears why at most once per period (#386): often enough that
# a household member who was never told about a list learns it exists, never
# often enough to spam the channel while they keep posting.
REFUSAL_NOTICE_PERIOD_S = 3600
WORKING_SIGNAL_TIMEOUT_S = 10  # a typing indicator that has not landed by then is pointless
APPROVAL_ONLY_NOTICE = "Only an approval user can approve a print. Nothing was changed."
REFUSAL_NOTICE = ("You are not on this bot's allowed list, so I can't act on that. Nothing was changed. "
                  "If you should have access, ask the person who runs Print Handy to add you.")
# .scad rides along for design edit sessions (#388 Stage 2): prepared intake
# still only accepts .3mf/.stl (its own MODEL_FILE_EXTS), so a dropped .scad
# is saved here and ignored by every non-design flow.
MODEL_FILE_EXTS = {".3mf", ".stl", ".scad"}
# A .zip of model sources (#1204 item 3): prepared intake opens it safely and
# takes only its printable STL/3MF parts; every other handler ignores it.
ARCHIVE_EXTS = {".zip"}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
# The intake copies a dropped file into the job archive, so the cached upload is only needed briefly.
UPLOAD_KEEP_S = 7 * 24 * 3600
HELP_WORDS = {"/print-help", "/help", "help"}


def _poll_moonraker_state(entry):
    """(print_stats state, filename) from a direct Moonraker poll, or None.

    #1020 B4: used only while the printer's listener is down, so the bot
    can still report a pause, error or stop the printer itself knows
    about. Any failure (no URL, unreachable, odd payload) reads as None —
    no notice, no exception.
    """
    base = str((entry or {}).get("moonraker_url") or "").rstrip("/")
    if not base:
        return None
    try:
        from .. import moonraker
        payload = moonraker.moonraker_json(base, "/printer/objects/query?print_stats", timeout=5)
        stats = ((payload.get("result") or {}).get("status") or {}).get("print_stats") or {}
        state = stats.get("state")
        if not isinstance(state, str):
            return None
        return state, str(stats.get("filename") or "")
    except Exception:
        return None


def enable_warm_worker():
    """Keep one warm heavy intake worker on this bot's loop (#1332). Never raises.

    Called by each platform client once its loop is up, and only when
    run_bot started it: test loops never get a spare. A failure just leaves
    every step starting cold, as before.
    """
    try:
        from ..intake import worker as intake_worker
        intake_worker.enable_warm_worker()
    except Exception:
        logger.warning("warm heavy worker not enabled; intake steps start cold", exc_info=True)


def unmatched_text(bot_name="Print Handy"):
    """The short plain reply for a message nothing took (#467).

    The unmatched fallback used to post the whole command list as an
    error response — a wall that said nothing about what went wrong with
    what the person typed. This says it did not understand, names the
    most common things people mean here, and offers the full list on
    request (``/print-help``, or typing ``help``) instead of leading with
    it. Did-you-mean suggestions for near-miss commands are #468 stage 3
    (``command_suggestion`` below): when one fires it replaces this reply;
    this wording and the no-dump rule are #467's either way.
    """
    return (f"I didn't understand that, so nothing happened.\n"
            f"{bot_name} prints from this channel: post a MakerWorld, Printables, Thingiverse, MyMiniFactory, "
            "Creality Cloud, Thangs or Nexprint link, "
            "a direct link to a .3mf or .stl file (a Dropbox share link works too), "
            "or drop one .3mf or .stl file, and I will prepare it — nothing starts without a button press.\n"
            "For everything I can do, type `/print-help`.")


# #468 stage 3: the did-you-mean prompt's shape. The 60-second expiry is the
# Stage 0 report's; the labels are the whole contract — a press (or a typed
# "yes") is the only thing that can turn a suggestion into a dispatch.
SUGGESTION_TIMEOUT_S = 60
# How often an event-loop prompt wait looks for its answer. A press resolves
# the shared registry from wherever it arrives (a button, a typed reply, a
# session cancel), so the wait checks it on the loop instead of parking a
# worker thread on the registry's blocking wait.
PROMPT_POLL_S = 0.1


async def await_prompt(registry, clarify_id, timeout, poll=None):
    """The answer to an open prompt, or None on timeout — without a worker thread.

    The same contract as ``registry.wait_for_response`` (the prompt is closed
    either way, so a late press finds nothing), but the wait itself is an
    ``asyncio.sleep`` loop on the running event loop, so any number of open
    prompts cost no threads. Only the timeout edge goes to a thread: a prompt
    whose answer is reserved mid-commit (Matrix posts its close-out note
    first) gets the registry's bounded grace there, exactly as before.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, float(timeout))
    step = PROMPT_POLL_S if poll is None else poll
    while registry.is_open(clarify_id):
        remaining = deadline - loop.time()
        if remaining <= 0:
            return await asyncio.to_thread(registry.wait_for_response, clarify_id, 0)
        await asyncio.sleep(min(step, remaining))
    return registry.wait_for_response(clarify_id, 0)


# Commands a "yes" runs at once: they have no confirmation of their own, so
# the suggestion prompt must not promise one (#1524). Derived from the
# registry's at_once flag, never a hand-kept list.
_RUNS_AT_ONCE = commands.at_once_names()


def _runs_at_once(command):
    return (str(command or "").split() or [""])[0] in _RUNS_AT_ONCE


def _suggestion_question(suggestion, candidates=()):
    """The "Did you mean" text, honest about commands that run at once."""
    if suggestion is not None:
        head = f"Did you mean `{suggestion}`? Nothing runs unless you say yes"
        if _runs_at_once(suggestion):
            return f"{head}. A yes pauses the print at once."
        return f"{head} — and the command still asks before anything prints."
    head = "Did you mean one of these? Nothing runs unless you pick one"
    at_once = [c for c in candidates if _runs_at_once(c)]
    if at_once:
        return (f"{head}. `{at_once[0]}` pauses the print at once; "
                "the others still ask before anything prints.")
    return f"{head} — and the command still asks before anything prints."


def _running_note(command):
    if _runs_at_once(command):
        return f"Running {command} now."
    return f"Running {command} — watch for its own confirmation."

class SessionTurns:
    """Per-session ordering on the event loop, with a bounded lock store.

    ``turn(key)`` is an async context: turns for one key run one at a time
    in arrival order (asyncio locks are FIFO), different keys run
    concurrently, and a waiting turn holds no thread. The store keeps at
    most ``max_sessions`` idle locks and drops the least recently used
    first; a lock that is held or has turns waiting on it is never dropped,
    so a key can never end up with two locks and two concurrent turns.
    """

    def __init__(self, max_sessions=256):
        self.max_sessions = int(max_sessions)
        self._locks = collections.OrderedDict()  # key -> [asyncio.Lock, users]

    def __len__(self):
        return len(self._locks)

    @contextlib.asynccontextmanager
    async def turn(self, key):
        key = str(key or "")
        entry = self._locks.pop(key, None) or [asyncio.Lock(), 0]
        self._locks[key] = entry  # most recently used last
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            self._evict()

    def _evict(self):
        while len(self._locks) > self.max_sessions:
            idle = next((k for k, (_lock, users) in self._locks.items() if users == 0), None)
            if idle is None:
                return  # every lock is in use: nothing may be dropped
            del self._locks[idle]
SUGGEST_YES_LABEL = "Yes — run it"
SUGGEST_NO_LABEL = "No"
SUGGEST_NONE_LABEL = "None of these"
MAX_SUGGESTION_CANDIDATES = 4


def _command_scores(text):
    """(scored, rest, wholes) for a command-shaped near miss, else (None, "", {}).

    ``scored`` is the (score, command) list the shared policy judges —
    each command scored by its best name/alias window. ``rest`` is the
    message's argument tail, carried over untouched. ``wholes`` maps a
    scored command's name to its best whole-name (unwindowed) score,
    for the whole-name tie-break in ``_whole_name_winner``.
    """
    parts = str(text or "").strip().split(None, 1)
    if not parts or not parts[0].startswith("/"):
        return None, "", {}
    word = parts[0].lstrip("/").casefold()
    if not word or commands.by_name(word):
        return None, "", {}
    scored = []
    wholes = {}
    for cmd in commands.COMMANDS:
        score = max(fuzzy.windowed_fuzzy_score(word, name) for name in (cmd.name, *cmd.aliases))
        if score:
            scored.append((score, cmd))
            wholes[cmd.name] = max(fuzzy.fuzzy_score(word, name) for name in (cmd.name, *cmd.aliases))
    return scored, (parts[1] if len(parts) > 1 else ""), wholes


def _whole_name_winner(decision, wholes):
    """The one contender the query matches as a whole name, else None.

    A window-only match can tie a whole-name match on windowed score
    alone: "/spol" scores the same against "spool" (the whole name) as
    against the "spools" window of "print-spools" (the command #744
    added). The whole-name match is the honest single answer — the
    window-only contender named only part of its name — so a tie
    between exactly one whole-name contender and window-only ones
    resolves to the whole name instead of degrading to a pick-list.
    Genuinely ambiguous tops (no whole-name contender, or several)
    keep the pick-list.
    """
    if len(decision.contenders) < 2:
        return None
    whole = [cmd for cmd in decision.contenders if wholes.get(cmd.name, 0) >= fuzzy.FUZZY_FLOOR]
    return whole[0] if len(whole) == 1 else None


# Extra verbs handlers accept in the subcommand slot, per command: the
# spool handler's own aliases for its typed forms (handlers/spool.py).
_SUBCOMMAND_HANDLER_ALIASES = {
    "spool": frozenset({"ls", "set", "load", "add", "grams"}),
}


def subcommand_suggestion(text):
    """The corrected typed form when a subcommand word is a near miss (#1496).

    Fuzzy stays parallel with the command table: #1475 gave five commands
    real subcommands, and a typo in that second word ("/spool chnage")
    is the same kind of near miss as a typo in the command name — except
    the handlers answer an unknown subcommand word themselves (a usage
    line, a menu, a report), so the fallback never sees it. This is the
    one suggestion ``process`` offers before dispatch; a decline or an
    expiry falls through to the handler's own answer, unchanged.

    Only a clear correction is offered: the shared policy
    (``fuzzy.decide``) must name a single contender, or the word must be
    a unique nearest neighbour within a small edit distance sharing its
    first letter (the edit path also carries the pairs the scorer's
    canonical forms hide, like "snyc" for "sync", whose canonical form
    is "synchronize"). The vocabulary is the command's typed subcommand
    words — subcommands whose typed word is empty (the bare form) have
    no word to mistype. Exact words, numbers, handler aliases that sit
    far from every subcommand word ("/spool ls", "/spool set") and
    commands without subcommands are all left alone, and the remaining
    arguments carry over untouched.
    """
    parts = str(text or "").strip().split()
    if len(parts) < 2 or not parts[0].startswith("/"):
        return None
    command = commands.by_name(parts[0].lstrip("/").casefold())
    if command is None or not command.subcommands:
        return None
    word = parts[1].casefold().strip("?.!")
    words = [sub.word for sub in command.subcommands if sub.word]
    if not word.isalpha() or word in words:
        return None
    # Words a handler already accepts in this slot are valid forms, not
    # near misses — /spool's own aliases (spool_dispatch) must never be
    # "corrected" into a prompt ("ls" scores 67 against "list").
    if word in _SUBCOMMAND_HANDLER_ALIASES.get(command.name, ()):
        return None
    winner = None
    decision = fuzzy.decide([(fuzzy.fuzzy_score(word, w), w) for w in words])
    if decision.kind != "none" and len(decision.contenders) == 1:
        winner = decision.best
    else:
        limit = 1 if len(word) <= 5 else 2
        distances = [(fuzzy.edit_distance(word, w, limit), w) for w in words
                     if w[0] == word[0]]
        nearest = [w for distance, w in distances if distance <= limit]
        if nearest:
            best = min(distance for distance, w in distances if distance <= limit)
            winners = [w for distance, w in distances if distance == best]
            if len(winners) == 1:
                winner = winners[0]
    if winner is None:
        return None
    rest = " ".join(parts[2:])
    return f"/{command.name} {winner}" + (f" {rest}" if rest else "")


# Plain-language task suggestions (#792): common task intents when people phrase jobs in words.
# Maps regex patterns to canonical command suggestions. A suggestion may carry
# the matched words into the command via "{q}" (the pattern's named group) —
# /print-lookup and /print-design take the person's own phrasing as their
# argument. Table order is priority: specific tasks first, the generic
# question/error carry-overs last, so a phrase with a real task home never
# degrades into a lookup.
_LEAD = r"(?:please\s+)?(?:i\s+(?:need|want)\s+to\s+|(?:can|could|would)\s+you\s+(?:please\s+)?|how\s+(?:do|can)\s+i\s+)?"
_END = r"\s*[?.!]?\s*$"
# A firmware component named in words: every spelling in
# firmware_update.COMPONENT_ALIASES ("z-mod", "helix screen", "guppy",
# "klippy", "moon"...), plus the Moonraker web clients. Captured as "c"
# and canonicalized by firmware_update.canonical_component.
_FW_COMPONENT = (r"(?P<c>z[\s-]?mod|helix(?:[\s-]?screen)?|guppy(?:[\s-]?screen)?"
                 r"|klipper|klippy|moonraker|fluidd|mainsail)")
_FW_COMPONENT_VOCABULARY = ("helixscreen", "klipper", "moonraker")
_TASK_INTENT_SUGGESTIONS = (
    # Spools: change / add / what is loaded
    (re.compile(rf"^{_LEAD}(?:change|swap|replace)\s+(?:a\s+|the\s+)?(?:filament|spool|roll)(?:\s+(?:roll|spool))?(?:\s+in\s+slot\s+[1-4])?{_END}", re.I), "/spool"),
    # The subcommand forms (#1496): fuzzy stays parallel with the command
    # table #1475 formalized, so the specific forms route to their typed
    # subcommand text, not only to the bare guided command. These sit
    # above the generic add pattern, which keeps the bare "/spool".
    (re.compile(rf"^{_LEAD}(?:add|create)\s+a\s+new\s+(?:spool|roll){_END}", re.I), "/spool new"),
    (re.compile(rf"^new\s+(?:spool|roll){_END}", re.I), "/spool new"),
    (re.compile(rf"^{_LEAD}(?:add|create|new)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:filament|spool|roll){_END}", re.I), "/spool"),
    (re.compile(rf"^{_LEAD}(?:list|show)\s+(?:me\s+)?(?:the\s+)?spools{_END}", re.I), "/spool list"),
    # Load is its own intent (#1524 tranche 2): putting an existing spool
    # into a slot is the guided change flow, never guided creation.
    (re.compile(rf"^{_LEAD}load\s+(?:a\s+|the\s+)?(?:filament|spool|roll)(?:\s+#?\d+)?(?:\s+into\s+slot\s+[1-4])?{_END}", re.I), "/spool change"),
    (re.compile(rf"^{_LEAD}load\s+spool\s+#?\d+\s+into\s+slot\s+[1-4]{_END}", re.I), "/spool change"),
    # "How much filament" is an inventory question (#1524 tranche 2): the
    # list is the deterministic answer surface, not creation or loading.
    (re.compile(rf"^(?:please\s+)?how\s+much\s+filament\s+do\s+i\s+have{_END}", re.I), "/spool list"),
    (re.compile(rf"^{_LEAD}(?:sync|synchronize)\s+(?:the\s+)?(?:spools?|filament)(?:\s+with\s+spoolman)?{_END}", re.I), "/spool sync"),
    (re.compile(rf"^{_LEAD}sync\s+(?:with\s+)?spoolman{_END}", re.I), "/spool sync"),
    # Spool rename, shelve, archive (#1515): the Web UI's slot actions in chat.
    (re.compile(rf"^{_LEAD}(?:rename|name)\s+(?:a\s+|the\s+|my\s+)?(?:spool|roll){_END}", re.I), "/spool rename"),
    (re.compile(rf"^{_LEAD}(?:put|move)\s+(?:a\s+|the\s+|my\s+)?(?:spool|roll)\s+back\s+on\s+(?:the\s+)?shelf{_END}", re.I), "/spool shelve"),
    (re.compile(rf"^{_LEAD}(?:shelve|unload)\s+(?:a\s+|the\s+|my\s+)?(?:spool|roll){_END}", re.I), "/spool shelve"),
    (re.compile(rf"^{_LEAD}archive\s+(?:a\s+|the\s+|my\s+|an\s+old\s+)?(?:spool|roll){_END}", re.I), "/spool archive"),
    (re.compile(rf"^{_LEAD}(?:unarchive|restore)\s+(?:a\s+|the\s+|my\s+)?(?:spool|roll){_END}", re.I), "/spool unarchive"),
    # Spool weight wrong (#1374): correcting the grams left is /spool weight.
    # No "correct" here: as an intent word it would pull the typo layer's
    # "corected" / "corectly" to "correct" for every other command.
    (re.compile(rf"^{_LEAD}(?:fix|set|update|change)\s+(?:the\s+|a\s+|my\s+)?(?:spool|filament|roll)(?:'s)?\s+(?:weight|grams(?:\s+left)?|remaining(?:\s+weight)?){_END}", re.I), "/spool weight"),
    (re.compile(rf"^(?:the\s+|my\s+)?(?:spool|filament|roll)\s+(?:weight|grams|remaining\s+weight)\s+(?:is\s+)?(?:wrong|off|incorrect){_END}", re.I), "/spool weight"),
    (re.compile(rf"^(?:please\s+)?(?:what|which)\s+(?:filament|spool|color|colour)s?\s+(?:is|are)\s+loaded{_END}", re.I), "/print-spools"),
    (re.compile(rf"^(?:please\s+)?what(?:'s|\s+is)\s+in\s+the\s+(?:ams|ifs){_END}", re.I), "/print-spools"),
    (re.compile(rf"^(?:please\s+)?(?:which\s+spools\s+are\s+loaded|show\s+loaded\s+spools|check\s+filament\s+slots|how\s+much\s+filament\s+is\s+left){_END}", re.I), "/print-spools"),
    # Print control
    # "my print", "this job" (#1524 audit): the checklist's own "Pause my
    # print" and the corpus "Stop this job" fell through to "I didn't
    # understand". Each still only offers the did-you-mean prompt; the
    # command then asks or acts as it always has.
    (re.compile(rf"^{_LEAD}(?:stop|cancel|abort)\s+(?:the\s+|my\s+|this\s+)?(?:current\s+)?(?:print|job|print\s+job){_END}", re.I), "/print-stop"),
    (re.compile(rf"^{_LEAD}pause\s+(?:the\s+|my\s+|this\s+)?(?:current\s+)?(?:print|job|print\s+job){_END}", re.I), "/print-pause"),
    (re.compile(rf"^{_LEAD}(?:resume|continue|unpause)\s+(?:the\s+|my\s+|this\s+)?(?:current\s+)?(?:print|job|print\s+job){_END}", re.I), "/print-resume"),
    (re.compile(rf"^{_LEAD}(?:restart|reboot)\s+(?:the\s+)?printer{_END}", re.I), "/print-reboot"),
    (re.compile(rf"^{_LEAD}(?:do|run)\s+(?:a\s+)?cold\s*pull{_END}", re.I), "/print-coldpull"),
    # Prepare to clean: separate the bed and nozzle (the stop recovery ask).
    # Each form only offers the did-you-mean prompt; /print-bed-down then
    # asks its own confirmation before anything moves.
    (re.compile(rf"^{_LEAD}(?:reset|lower|drop)\s+(?:the\s+)?(?:printer'?s?\s+)?bed(?:\s+down)?{_END}", re.I), "/print-bed-down"),
    (re.compile(rf"^{_LEAD}(?:move|bring|put)\s+(?:the\s+)?bed\s+down{_END}", re.I), "/print-bed-down"),
    (re.compile(rf"^(?:the\s+)?bed\s+(?:down|reset){_END}", re.I), "/print-bed-down"),
    (re.compile(rf"^{_LEAD}separate\s+(?:the\s+)?(?:nozzle|bed)(?:\s+(?:and|from)\s+(?:the\s+)?(?:nozzle|bed))?{_END}", re.I), "/print-bed-down"),
    (re.compile(rf"^{_LEAD}get\s+(?:the\s+)?nozzle\s+off\s+(?:the\s+)?(?:part|print|bed){_END}", re.I), "/print-bed-down"),
    (re.compile(rf"^{_LEAD}(?:prepare|prep|get\s+ready)\s+(?:the\s+printer\s+)?(?:to|for)\s+clean(?:ing)?{_END}", re.I), "/print-bed-down"),
    (re.compile(rf"^{_LEAD}clean\s+the\s+nozzle{_END}", re.I), "/print-bed-down"),
    (re.compile(rf"^{_LEAD}(?:change|swap|replace)\s+(?:the\s+)?nozzle{_END}", re.I), "/nozzle change"),
    (re.compile(rf"^(?:please\s+)?nozzle\s+change{_END}", re.I), "/nozzle change"),
    # Status, progress and failure reports
    (re.compile(rf"^(?:please\s+)?(?:what\s+is\s+(?:the\s+)?)?(?:printer|print)\s+status{_END}", re.I), "/print-status"),
    (re.compile(rf"^{_LEAD}(?:show|check)\s+(?:me\s+)?(?:the\s+)?(?:printer|print)\s+status{_END}", re.I), "/print-status"),
    (re.compile(rf"^(?:please\s+)?how\s+is\s+the\s+print\s+doing{_END}", re.I), "/print-status"),
    (re.compile(rf"^(?:please\s+)?(?:is\s+the\s+print\s+(?:still\s+)?running|print\s+progress){_END}", re.I), "/print-status"),
    (re.compile(rf"^(?:is\s+the\s+)?(?:print|printer)\s+(?:done|finished|complete|completed){_END}", re.I), "/print-status"),
    (re.compile(rf"^how\s+long\s+(?:left|to\s+go|until\s+(?:it\s+is|it's|its)\s+done){_END}", re.I), "/print-status"),
    (re.compile(rf"^when\s+will\s+(?:it|the\s+print|this)\s+(?:finish|be\s+done|be\s+finished){_END}", re.I), "/print-status"),
    (re.compile(rf"^(?:the\s+|my\s+)?print\s+(?:just\s+)?failed{_END}", re.I), "/print-status"),
    (re.compile(rf"^(?:please\s+)?(?:what(?:'s|\s+is)\s+the\s+eta|how\s+much\s+time\s+is\s+left|time\s+remaining|percent\s+complete|what\s+layer\s+is\s+it\s+on){_END}", re.I), "/print-status"),
    (re.compile(rf"^(?:please\s+)?(?:what\s+temp(?:erature)?\s+is\s+the\s+(?:bed|nozzle)|(?:hotend|bed)\s+temperature|how\s+hot\s+is\s+the\s+(?:nozzle|bed)|is\s+the\s+bed\s+heated|is\s+the\s+nozzle\s+hot){_END}", re.I), "/print-status"),
    (re.compile(rf"^{_LEAD}(?:show\s+(?:me\s+)?(?:the\s+)?camera|check\s+(?:the\s+)?camera|take\s+a\s+snapshot|camera\s+view|webcam\s+snapshot){_END}", re.I), "/print-status"),
    # Slice result card (#1525): the finished slice's own numbers.
    (re.compile(rf"^{_LEAD}(?:show|see)\s+(?:me\s+)?(?:the\s+)?slicing\s+result{_END}", re.I), "/print-slice-result"),
    (re.compile(rf"^(?:please\s+)?how\s+much\s+filament\s+will\s+(?:it|this|the\s+print)\s+use{_END}", re.I), "/print-slice-result"),
    # Reprint and calibration
    (re.compile(rf"^{_LEAD}(?:reprint|print\s+(?:it\s+)?again|retry\s+(?:the|that|last)\s+print|try\s+that\s+print\s+again|run\s+that\s+print\s+again|print\s+another\s+(?:one|copy)|print\s+one\s+more){_END}", re.I), "/print-again"),
    (re.compile(rf"^{_LEAD}(?:(?:print|start)\s+(?:the\s+)?)?next\s+plate{_END}", re.I), "/print-next"),
    (re.compile(rf"^{_LEAD}(?:run\s+)?(?:bed\s+leveling|bed\s+level|auto\s+bed\s+leveling|calibration){_END}", re.I), "/print-calibration"),
    (re.compile(rf"^{_LEAD}(?:calibrate|level)\s+(?:the\s+)?bed{_END}", re.I), "/print-calibration"),
    # Calibration subcommands (#1496): each /print-calibration form #1475
    # registered gets its own plain-language route to its typed form.
    (re.compile(rf"^(?:please\s+)?(?:bed\s+mesh|mesh\s+calibration){_END}", re.I), "/print-calibration mesh"),
    (re.compile(rf"^{_LEAD}(?:run|calibrate|do|make)\s+(?:a\s+|the\s+)?bed\s+mesh{_END}", re.I), "/print-calibration mesh"),
    (re.compile(rf"^(?:please\s+)?(?:bed\s+screws|bed\s+screw\s+calibration|screw\s+calibration|screws?\s+tilt){_END}", re.I), "/print-calibration screws"),
    (re.compile(rf"^{_LEAD}(?:calibrate|adjust|level)\s+(?:the\s+)?bed\s+screws{_END}", re.I), "/print-calibration screws"),
    (re.compile(rf"^(?:please\s+)?pid\s+(?:tuning|calibration|tune|calibrate){_END}", re.I), "/print-calibration pid"),
    (re.compile(rf"^{_LEAD}(?:tune|calibrate|run)\s+(?:the\s+)?pid{_END}", re.I), "/print-calibration pid"),
    (re.compile(rf"^(?:please\s+)?input\s+shaper(?:\s+calibration)?{_END}", re.I), "/print-calibration shaper"),
    (re.compile(rf"^{_LEAD}(?:run|calibrate|do)\s+(?:the\s+)?input\s+shaper{_END}", re.I), "/print-calibration shaper"),
    (re.compile(rf"^(?:please\s+)?belt\s+(?:tension|calibration){_END}", re.I), "/print-calibration belts"),
    (re.compile(rf"^{_LEAD}(?:calibrate|check|adjust)\s+(?:the\s+)?belts?{_END}", re.I), "/print-calibration belts"),
    (re.compile(rf"^(?:please\s+)?z\s*offset(?:\s+calibration)?{_END}", re.I), "/print-calibration zoffset"),
    (re.compile(rf"^{_LEAD}(?:calibrate|set|adjust)\s+(?:the\s+)?z\s*offset{_END}", re.I), "/print-calibration zoffset"),
    (re.compile(rf"^(?:please\s+)?(?:calibration\s+records|past\s+calibrations|previous\s+calibrations){_END}", re.I), "/print-calibration records"),
    (re.compile(rf"^{_LEAD}(?:show|list|see)\s+(?:me\s+)?(?:the\s+|my\s+)?calibration\s+records{_END}", re.I), "/print-calibration records"),
    # Maintenance and firmware
    (re.compile(rf"^{_LEAD}(?:do|run)\s+(?:the\s+|some\s+)?maintenance{_END}", re.I), "/print-maintenance"),
    (re.compile(rf"^(?:what\s+)?maintenance\s+(?:is\s+)?(?:due|needed){_END}", re.I), "/print-maintenance"),
    (re.compile(rf"^(?:please\s+)?quick\s+maintenance{_END}", re.I), "/print-maintenance quick"),
    (re.compile(rf"^{_LEAD}(?:do|run)\s+(?:a\s+)?quick\s+maintenance{_END}", re.I), "/print-maintenance quick"),
    (re.compile(rf"^{_LEAD}(?:update|upgrade|check)\s+(?:the\s+)?firmware{_END}", re.I), "/print-firmware"),
    (re.compile(rf"^(?:is\s+there\s+)?(?:a\s+)?firmware\s+update{_END}", re.I), "/print-firmware"),
    # Firmware subcommand forms (#1496): the bare form is the versions
    # report (its subcommand's typed word is empty), and the guide form
    # takes the flavor the handler asks for when it is missing.
    (re.compile(rf"^(?:please\s+)?firmware\s+versions?{_END}", re.I), "/print-firmware"),
    (re.compile(rf"^{_LEAD}(?:show|list|check)\s+(?:me\s+)?(?:the\s+)?firmware\s+versions?{_END}", re.I), "/print-firmware"),
    (re.compile(rf"^(?:please\s+)?what\s+firmware\s+version\s+is\s+(?:installed|running){_END}", re.I), "/print-firmware"),
    (re.compile(rf"^(?:please\s+)?what\s+version\s+is\s+the\s+firmware{_END}", re.I), "/print-firmware"),
    (re.compile(rf"^(?:please\s+)?firmware\s+(?:update\s+)?guide{_END}", re.I), "/print-firmware guide"),
    (re.compile(rf"^{_LEAD}(?:show|open|give)\s+(?:me\s+)?(?:the\s+)?firmware\s+(?:update\s+)?guide{_END}", re.I), "/print-firmware guide"),
    # Update checks phrased as "check for ... updates", including the ones
    # named by component (zmod, helixscreen, ...) — the plain check above
    # only matched the literal word "firmware" in two narrow shapes, so
    # "can you check for zmod updates?" dead-ended (catduck, 2026-11-11).
    # The component set also carries the fluidd/mainsail names and the
    # bare "check <name>" form from #1290; every check form lands on the
    # bare command, whose own form is the read-only report — a check never
    # routes to the update form, and the confirm discipline stays the
    # command's own.
    (re.compile(rf"^{_LEAD}(?:check|look)\s+for\s+(?:a\s+)?firmware\s+updates?{_END}", re.I), "/print-firmware"),
    (re.compile(rf"^{_LEAD}(?:check|look)\s+for\s+(?:an?\s+|any\s+)?(?:new\s+)?{_FW_COMPONENT}\s+updates?{_END}", re.I), "/print-firmware"),
    (re.compile(rf"^{_LEAD}(?:check|look)\s+for\s+(?:an?\s+)?updates?{_END}", re.I), "/print-firmware"),
    (re.compile(rf"^(?:is\s+there\s+)?(?:an?\s+)?(?:new\s+)?{_FW_COMPONENT}\s+updates?{_END}", re.I), "/print-firmware"),
    (re.compile(rf"^(?:is\s+)?{_FW_COMPONENT}\s+up\s+to\s+date{_END}", re.I), "/print-firmware"),
    # Firmware lines Print Handy reports but never applies (the firmware feed,
    # #1280 section I): questions about them get the read-only report, and
    # there is deliberately no "update X" form for them.
    (re.compile(rf"^{_LEAD}(?:check\s+for|look\s+for|is\s+there)\s+(?:an?\s+|any\s+)?(?:new\s+)?(?:forge[\s-]?x|ff5m|open[\s-]?creator)\s+(?:updates?|releases?|firmware){_END}", re.I), "/print-firmware"),
    (re.compile(rf"^(?:new\s+)?(?:forge[\s-]?x|ff5m|open[\s-]?creator)\s+(?:updates?|releases?|version){_END}", re.I), "/print-firmware"),
    (re.compile(rf"^{_LEAD}(?:show|open|check)\s+(?:me\s+)?(?:the\s+)?firmware\s+(?:feed|releases){_END}", re.I), "/print-firmware"),
    (re.compile(rf"^{_LEAD}check\s+(?:the\s+)?{_FW_COMPONENT}{_END}", re.I), "/print-firmware"),
    # "update helix" (the owner's words, 2026-11-11): the update form for
    # the named component, its name through firmware_update's one alias
    # table ("helix", "helix screen" -> helixscreen; "guppy" ->
    # guppyscreen). Only ever the did-you-mean prompt; the command then
    # resolves the name against the live status and asks its own
    # confirmation before anything is sent. This supersedes the bare-report
    # route #1298 first gave "update <name>": an instruction to update a
    # named component is an update ask, not a check.
    (re.compile(rf"^{_LEAD}(?:update|upgrade)\s+(?:the\s+|my\s+)?{_FW_COMPONENT}(?:\s+(?:firmware|now|please))?{_END}", re.I), "/print-firmware update {c}"),
    # Cost (#1625): what a print cost or will cost. The amounts, when
    # the ask carries them, ride along as the command's argument and
    # the handler parses them; the bare forms ask about the last print.
    # "coast" is the typo corpus's folded form of "cost", so the
    # patterns accept both spellings or the folded pass never matches.
    (re.compile(rf"^(?:please\s+)?how\s+much\s+(?:will|would)\s+this\s+(?:cost|coast){_END}", re.I), "/print-cost"),
    (re.compile(rf"^(?:please\s+)?how\s+much\s+(?:will|would)\s+this\s+(?:cost|coast)\s*,?\s*(?P<q>.+?){_END}", re.I), "/print-cost {q}"),
    (re.compile(rf"^(?:please\s+)?how\s+much\s+(?:did|does)\s+(?:that\s+print|this\s+print|that|this|the\s+print|it)\s+(?:cost|coast){_END}", re.I), "/print-cost"),
    # The month asks carry "this month" through as the argument, so the
    # handler answers the month's recorded spend, not the last print.
    (re.compile(rf"^(?:please\s+)?what\s+did\s+filament\s+(?:cost|coast)\s+this\s+month{_END}", re.I), "/print-cost this month"),
    (re.compile(rf"^(?:please\s+)?(?:what|how\s+much)\s+did\s+i\s+spend\s+(?:on\s+(?:filament|printing|prints)\s+)?this\s+month{_END}", re.I), "/print-cost this month"),
    (re.compile(rf"^{_LEAD}price\s+(?:this|that)\s+print{_END}", re.I), "/print-cost"),
    (re.compile(rf"^(?:please\s+)?what\s+(?:did|does)\s+(?:that|this)\s+print\s+(?:cost|coast){_END}", re.I), "/print-cost"),
    (re.compile(rf"^(?:please\s+)?print\s+(?:cost|coast){_END}", re.I), "/print-cost"),
    # Channel switches (#1634): "switch helixscreen to beta", "change the
    # helixscreen channel to beta", "put helixscreen on stable". Only ever
    # the did-you-mean prompt; the command then shows the exact one-line
    # config change and asks its own confirmation before anything is sent.
    (re.compile(rf"^{_LEAD}(?:switch|change|move|put)\s+(?:the\s+)?{_FW_COMPONENT}\s+(?:(?:update\s+)?channel\s+to|to|on)(?:\s+the)?\s*beta(?:\s+channel)?{_END}", re.I), "/print-firmware channel {c} beta"),
    (re.compile(rf"^{_LEAD}(?:switch|change|move|put)\s+(?:the\s+)?{_FW_COMPONENT}\s+(?:(?:update\s+)?channel\s+to|to|on)(?:\s+the)?\s*stable(?:\s+channel)?{_END}", re.I), "/print-firmware channel {c} stable"),
    (re.compile(rf"^{_LEAD}(?:switch|change|move|put)\s+(?:the\s+)?{_FW_COMPONENT}\s+(?:(?:update\s+)?channel\s+to|to|on)(?:\s+the)?\s*dev(?:\s+channel)?{_END}", re.I), "/print-firmware channel {c} dev"),
    # Colors, macros, live view, plate reference, setup and help: the
    # registered commands that had no plain-language route at all. Every
    # command carries a fuzzy equivalent (the coverage guard in
    # tests/bot/test_command_fuzzy_coverage.py enumerates the registry
    # and fails a command that has none).
    (re.compile(rf"^{_LEAD}(?:choose|pick|assign|set)\s+(?:the\s+)?colors?\s+for\s+(?:the|this|that)\s+(?:print|job|parts?){_END}", re.I), "/print-colors"),
    (re.compile(rf"^{_LEAD}(?:choose|pick|assign)\s+(?:the\s+)?colors?{_END}", re.I), "/print-colors"),
    (re.compile(rf"^{_LEAD}(?:list|show)\s+(?:me\s+)?(?:the\s+)?macros{_END}", re.I), "/print-macros"),
    (re.compile(rf"^(?:what|which)\s+macros\s+(?:are\s+there|do\s+i\s+have|can\s+i\s+run){_END}", re.I), "/print-macros"),
    (re.compile(rf"^{_LEAD}run\s+(?:a|the)\s+macro{_END}", re.I), "/print-macros run"),
    (re.compile(rf"^{_LEAD}(?:start|open|show|watch)\s+(?:me\s+)?(?:the\s+)?live\s+(?:camera|view|stream|feed){_END}", re.I), "/print-live"),
    (re.compile(rf"^{_LEAD}watch\s+(?:the\s+)?print\s+live{_END}", re.I), "/print-live"),
    (re.compile(rf"^{_LEAD}(?:stop|end)\s+(?:the\s+)?live\s+(?:camera|view|stream|feed){_END}", re.I), "/print-live stop"),
    (re.compile(rf"^(?:please\s+)?live\s+camera\s+status{_END}", re.I), "/print-live status"),
    (re.compile(rf"^{_LEAD}(?:calibrate\s+(?:the\s+)?plate|plate\s+calibration|capture\s+(?:the\s+)?plate\s+reference){_END}", re.I), "/print-plate-calibrate"),
    (re.compile(rf"^{_LEAD}capture\s+(?:the\s+)?plate{_END}", re.I), "/print-plate-calibrate"),
    (re.compile(rf"^{_LEAD}add\s+(?:a\s+|an\s+|another\s+)?printer{_END}", re.I), "/print-handy add-printer"),
    # "set up <bot name>" is built per configured name: see _setup_intent.
    (re.compile(rf"^(?:please\s+)?what\s+can\s+you\s+do{_END}", re.I), "/print-help"),
    (re.compile(rf"^{_LEAD}(?:list|show)\s+(?:me\s+)?(?:the\s+)?commands{_END}", re.I), "/print-help"),
    # Symptoms with a dedicated troubleshooting flow
    (re.compile(rf"^(?:the\s+|my\s+)?nozzle\s+is\s+clogged{_END}", re.I), "/print-troubleshoot clog"),
    (re.compile(rf"^clogged\s+nozzle{_END}", re.I), "/print-troubleshoot clog"),
    (re.compile(rf"^(?:the\s+)?filament\s+is\s+(?:stuck|jammed){_END}", re.I), "/print-troubleshoot clog"),
    (re.compile(rf"^(?:the\s+|my\s+)?extruder\s+is\s+clicking{_END}", re.I), "/print-troubleshoot clog"),
    (re.compile(rf"^extruder\s+clicking{_END}", re.I), "/print-troubleshoot clog"),
    (re.compile(rf"^(?:the\s+)?(?:filament|extruder)\s+(?:won't|wont|not)\s+(?:feed|extrude){_END}", re.I), "/print-troubleshoot clog"),
    (re.compile(rf"^(?:the\s+|my\s+)?extruder\s+(?:is\s+)?skipping{_END}", re.I), "/print-troubleshoot clog"),
    (re.compile(rf"^(?:(?:the\s+|my\s+)?nozzle\s+is\s+jammed|jammed\s+nozzle){_END}", re.I), "/print-troubleshoot clog"),
    (re.compile(rf"^(?:blocked\s+hotend|(?:the\s+|my\s+)?hotend\s+is\s+(?:clogged|jammed)){_END}", re.I), "/print-troubleshoot clog"),
    (re.compile(rf"^under\s*(?:extruding|extrusion){_END}", re.I), "/print-troubleshoot clog"),
    (re.compile(rf"^nothing\s+is\s+coming\s+out\s+of\s+(?:the\s+)?nozzle{_END}", re.I), "/print-troubleshoot clog"),
    (re.compile(rf"^(?:the\s+)?filament\s+(?:is\s+)?not\s+coming\s+out(?:\s+of\s+(?:the\s+)?nozzle)?{_END}", re.I), "/print-troubleshoot clog"),
    (re.compile(rf"^first\s+layer\s+(?:is\s+)?(?:not\s+sticking|won't\s+stick|wont\s+stick|peeling|coming\s+(?:loose|off)){_END}", re.I), "/print-troubleshoot first-layer"),
    (re.compile(rf"^(?:the\s+)?prints?\s+(?:won't|wont|not)\s+stick(?:ing)?{_END}", re.I), "/print-troubleshoot first-layer"),
    (re.compile(rf"^bed\s+adhesion\s+(?:problem|problems|issue|issues|trouble){_END}", re.I), "/print-troubleshoot first-layer"),
    (re.compile(rf"^spaghetti\s+on\s+(?:the\s+)?bed{_END}", re.I), "/print-troubleshoot first-layer"),
    (re.compile(rf"^(?:the\s+|my\s+)?print\s+(?:is\s+|turned\s+into\s+|became\s+)?spaghetti{_END}", re.I), "/print-troubleshoot first-layer"),
    (re.compile(rf"^(?:(?:it'?s|it\s+is|(?:the\s+|my\s+)?print\s+is)\s+)?making\s+(?:spaghetti|pasta){_END}", re.I), "/print-troubleshoot first-layer"),
    (re.compile(rf"^(?:(?:the\s+|my\s+)?print\s+)?turned\s+into\s+(?:pasta|noodles){_END}", re.I), "/print-troubleshoot first-layer"),
    (re.compile(rf"^(?:extruding\s+into\s+thin\s+air|printing\s+(?:in\s+mid\s*air|air)){_END}", re.I), "/print-troubleshoot first-layer"),
    (re.compile(rf"^(?:(?:bird'?s?|birds)\s+nest|filament\s+pile)\s+on\s+(?:the\s+)?bed{_END}", re.I), "/print-troubleshoot first-layer"),
    (re.compile(rf"^(?:bed\s+)?adhesion\s+failed{_END}", re.I), "/print-troubleshoot first-layer"),
    (re.compile(rf"^(?:(?:the\s+|my\s+)?(?:print|part)\s+)?(?:popped\s+off\s+(?:the\s+)?bed|came\s+(?:loose|off\s+(?:the\s+)?bed)){_END}", re.I), "/print-troubleshoot first-layer"),
    (re.compile(rf"^(?:the\s+|my\s+)?print\s+unstuck{_END}", re.I), "/print-troubleshoot first-layer"),
    (re.compile(rf"^first\s+layer\s+peeled{_END}", re.I), "/print-troubleshoot first-layer"),
    (re.compile(rf"^corners\s+are\s+(?:lifting|warping){_END}", re.I), "/print-troubleshoot first-layer"),
    (re.compile(rf"^(?:the\s+|my\s+)?print\s+is\s+warping{_END}", re.I), "/print-troubleshoot first-layer"),
    (re.compile(rf"^warping\s+off\s+(?:the\s+)?bed{_END}", re.I), "/print-troubleshoot first-layer"),
    (re.compile(rf"^elephant'?s?\s+foot{_END}", re.I), "/print-troubleshoot first-layer"),
    (re.compile(rf"^(?:i\s+have\s+|there\s+is\s+)?heat\s+creep{_END}", re.I), "/print-troubleshoot heat-creep"),
    (re.compile(rf"^(?:the\s+|my\s+)?bed\s+is\s+(?:not\s+level|unlevel|uneven){_END}", re.I), "/print-troubleshoot bed-level"),
    (re.compile(rf"^uneven\s+first\s+layer{_END}", re.I), "/print-troubleshoot bed-level"),
    (re.compile(rf"^first\s+layer\s+is\s+uneven{_END}", re.I), "/print-troubleshoot bed-level"),
    (re.compile(rf"^(?:i\s+have\s+)?(?:ringing|ghosting)\s+(?:in|on)\s+(?:my\s+|the\s+)?prints?{_END}", re.I), "/print-troubleshoot ringing"),
    (re.compile(rf"^(?:ringing|ghosting){_END}", re.I), "/print-troubleshoot ringing"),
    (re.compile(rf"^echoing\s+on\s+(?:the\s+)?walls{_END}", re.I), "/print-troubleshoot ringing"),
    (re.compile(rf"^(?:ripples|vibrations)\s+on\s+(?:the\s+|my\s+)?prints?{_END}", re.I), "/print-troubleshoot ringing"),
    # Dedicated defect lookups
    (re.compile(rf"^(?:stringing\s+on\s+(?:my\s+|the\s+)?print|too\s+much\s+stringing|getting\s+fine\s+hairs|filament\s+whiskers|cobwebs\s+on\s+(?:the\s+)?print|oozing\s+between\s+parts){_END}", re.I), "/print-lookup stringing"),
    (re.compile(rf"^(?:layers?\s+shifted|layers\s+are\s+misaligned|(?:the\s+|my\s+)?print\s+shifted|staircase\s+effect|skipped\s+steps){_END}", re.I), "/print-lookup layer shift"),
    (re.compile(rf"^(?:layers\s+are\s+splitting|layer\s+separation|delamination|cracking\s+between\s+layers){_END}", re.I), "/print-lookup layer separation"),
    (re.compile(rf"^(?:wet\s+filament|popping\s+sounds?\s+from\s+(?:the\s+)?nozzle|steam\s+from\s+(?:the\s+)?nozzle|bubbly\s+extrusion){_END}", re.I), "/print-lookup wet filament"),
    # Shopping (#1524 tranche 2): "buy filament", "I bought filament",
    # "add a spool", "load a spool" and "how much filament" are five
    # different intents. Buying is shopping guidance — it must never
    # route to the guided /spool creation flow, and a past-tense purchase
    # ("I bought ...") gets no suggestion at all: the purchase-intake
    # handler owns recording a purchase, and only from a link or an
    # image, with its own offer question. Running low starts with seeing
    # what is loaded and how much is left.
    (re.compile(rf"^where\s+(?:can|do)\s+i\s+(?:buy|get|order)\s+(?:more\s+)?(?:filament|spools?){_END}", re.I), "/print-lookup buying filament"),
    (re.compile(rf"^{_LEAD}(?:buy|order|purchase)\s+(?:more\s+)?(?:filament|spools?|pla|petg|abs|asa|tpu|nylon){_END}", re.I), "/print-lookup buying filament"),
    (re.compile(rf"^(?:i\s+)?(?:need|want)\s+(?:more\s+)?(?:filament|spools?|pla|petg|abs|asa|tpu|nylon){_END}", re.I), "/print-lookup buying filament"),
    (re.compile(rf"^(?:i'm\s+|im\s+|we're\s+|we\s+are\s+)?running\s+low\s+on\s+(?:filament|spools?|pla|petg|abs|tpu){_END}", re.I), "/print-spools"),
    # Model sites
    (re.compile(rf"^(?:what|which)\s+(?:model\s+)?sites?\s+(?:can|do)\s+you\s+(?:print|download)\s+from{_END}", re.I), "/print-source"),
    (re.compile(rf"^(?:can|do)\s+you\s+(?:print|download)\s+from\s+(?:printables|thingiverse|makerworld|my\s*mini\s*factory|myminifactory|thangs|nexprint|cults(?:3d)?|creality\s+cloud){_END}", re.I), "/print-source"),
    # Design requests carry their own words as the prompt
    (re.compile(rf"^(?:please\s+)?(?:design|model)\s+(?:me\s+)?(?P<q>(?:a|an|the)\s+.+?){_END}", re.I), "/print-design {q}"),
    # Errors and questions: the lookup takes the person's own words.
    (re.compile(rf"^(?:please\s+)?what\s+does\s+(?P<q>.+?)\s+mean{_END}", re.I), "/print-lookup {q}"),
    (re.compile(rf"^(?:please\s+)?what\s+is\s+(?P<q>(?:error|hms)\s+.+?){_END}", re.I), "/print-lookup {q}"),
    (re.compile(rf"^(?:the\s+)?(?P<q>thermal\s+runaway|heating\s+failed|heater\s+error|temperature\s+error|filament\s+runout|layer\s+shift){_END}", re.I), "/print-lookup {q}"),
    (re.compile(rf"^(?:i\s+)?(?:got|get|getting|have|keep\s+getting)\s+(?:a\s+|an\s+|the\s+)?(?P<q>.+?\s+error|error){_END}", re.I), "/print-lookup {q}"),
    (re.compile(rf"^how\s+to\s+(?P<q>.+?){_END}", re.I), "/print-lookup {q}"),
    (re.compile(rf"^why\s+(?:does|do|did|is|are|won't|wont|can't|cant|doesn't|keeps?)\s+(?P<q>.+?){_END}", re.I), "/print-lookup {q}"),
)


# "spend" is subtracted for the same reason as the firmware sub-words
# below: the cost month pattern matches it literally, but as a correction
# target it hijacks the speed typos ("what does pseed mean" must stay
# "/print-lookup speed", not "/print-lookup spend").
_INTENT_REGEX_JUNK = {"please", "the", "and", "from", "for", "you", "can", "what", "which", "where",
                      "spend"}


def _intent_vocabulary():
    """Every literal word the task-intent patterns match, for typo correction."""
    # The firmware component group contributes only the names the earlier
    # firmware patterns already carried: its sub-words ("screen", "helix",
    # "guppy") would otherwise become correction targets for every other
    # route ("what does screns mean" must stay "screens").
    words = set(_FW_COMPONENT_VOCABULARY)
    for pattern, _cmd in (*_TASK_INTENT_SUGGESTIONS, _setup_intent()):
        source = re.sub(r"\\[a-zA-Z]", " ", pattern.pattern.replace(_FW_COMPONENT, " "))
        words.update(w.casefold() for w in re.findall(r"[A-Za-z]{5,}", source))
    return frozenset(words - _INTENT_REGEX_JUNK)


def _setup_intent(bot_name=None):
    """("set up <name>", "/print-handy setup") for the configured bot name.

    "print handy" always counts too: ``/print-handy`` is the command's name
    whatever the bot is called, so "set up print handy" keeps working on a
    renamed bot, and "set up shop bot" works on one named "Shop Bot".
    """
    names = {"print handy", " ".join(str(bot_name or "").split()).casefold()} - {""}
    alternatives = "|".join(r"\s+".join(re.escape(word) for word in name.split())
                            for name in sorted(names, key=len, reverse=True))
    return (re.compile(rf"^(?:please\s+)?(?:set\s+up|setup)\s+(?:{alternatives}){_END}", re.I),
            "/print-handy setup")


_INTENT_VOCABULARY = _intent_vocabulary()


def _task_suggestion(text, bot_name=None):
    clean = " ".join(str(text or "").split()).strip()
    if not clean or clean.startswith("/"):
        return None
    found = _match_task_intent(clean, bot_name)
    if found:
        return found
    # Upfront typo-folding resolves curated phrases and single-word typos
    # (e.g. "spegatti" -> "spaghetti", "cancle" -> "cancel", "seperate" -> "separate").
    folded = fuzzy.typo_fold(clean)
    if folded.casefold() != clean.casefold():
        found = _match_task_intent(folded, bot_name)
        if found:
            return found
    # Second pass: correct near-miss typos against the intent vocabulary.
    corrected = fuzzy.correct_typos(folded, _INTENT_VOCABULARY)
    if corrected.casefold() != folded.casefold():
        found = _match_task_intent(corrected, bot_name)
        if found:
            return found
    return _bed_down_fuzzy(clean) or _bed_down_fuzzy(folded)


def _match_task_intent(clean, bot_name=None):
    setup, setup_cmd = _setup_intent(bot_name)
    if setup.match(clean):
        return setup_cmd
    for pattern, cmd in _TASK_INTENT_SUGGESTIONS:
        match = pattern.match(clean)
        if not match:
            continue
        if "{c}" in cmd:
            from .. import firmware_update
            component = firmware_update.canonical_component(match.group("c"))
            return cmd.replace("{c}", component)
        if "{q}" in cmd:
            query = " ".join((match.groupdict().get("q") or "").split()).strip(" ?.!")
            if not query:
                continue
            query_folded = fuzzy.typo_fold(query)
            query_corrected = fuzzy.correct_typos(query_folded, _INTENT_VOCABULARY)
            return cmd.replace("{q}", query_corrected)
        return cmd
    return None


# Typo forms of the prepare-to-clean request ("rest the bed", "seperate the
# nozzle", "bed dwn") go through the shared fuzzy scorer. The floor is high on
# purpose: "reset the printer" (89), "heat the bed" (80) and "level the bed"
# (77) stay below it, and the result is only ever the did-you-mean prompt.
BED_DOWN_PHRASES = (
    "reset the bed", "reset the printer bed", "lower the bed", "drop the bed",
    "move the bed down", "bed down", "separate the nozzle", "separate the bed and nozzle",
    "get the nozzle off the part", "prepare to clean", "get ready to clean", "clean the nozzle",
)
BED_DOWN_FUZZY_FLOOR = 90


def _bed_down_fuzzy(clean):
    if len(clean.split()) > 8:
        return None
    if fuzzy.best_fuzzy_score(clean, BED_DOWN_PHRASES) >= BED_DOWN_FUZZY_FLOOR:
        return "/print-bed-down"
    return None


def _names_command(reply, command):
    """True when a model reply mentions the slash command it also asked to run (#1373)."""
    name = str(command or '').strip().split(' ')[0].casefold()
    if not name.startswith('/'):
        return False
    # The whole command, not a prefix of another one ("/print" is not "/print-stop").
    return re.search(rf'(?<![\w/-]){re.escape(name)}(?![\w-])', str(reply or '').casefold()) is not None


def command_suggestion(text, bot_name=None):
    """The canonical command a near-miss message most likely meant, else None (#468 stage 3).

    ``bot_name`` is the configured name, for "set up <bot name>".

    Only the first word is matched — "/spol list" suggests "/spool list",
    arguments carried over untouched — against command names and aliases,
    with one addition since #1496: an exact command whose subcommand
    word is itself a near miss suggests the corrected form through
    ``subcommand_suggestion``, which ``process`` also offers before
    dispatch (the handlers answer unknown subcommand words themselves,
    so that path never reaches this fallback)
    with the shared fuzzy scorer (``core.fuzzy``, windowed so "/stop" can
    mean "/print-stop"). The message must be command-shaped (first word
    starts with "/"): plain chat is never a near miss, however similar a
    word in it looks — "hello there" would otherwise score 67 against the
    "help" window of print-help. An exact first word is not a near miss
    and returns None, and the shared confidence/conflict policy
    (``fuzzy.decide``) governs the rest: a single uncontested contender —
    direct or grey-zone — is the suggestion; a contested top (another
    command within the clear margin) returns None here rather than a
    coin flip, and ``command_suggestion_candidates`` offers the
    contenders as a pick-list instead — with one refinement: a tie
    between exactly one whole-name match and window-only matches
    resolves to the whole name (``_whole_name_winner``), so adding a
    command whose name contains another's ("/print-spools" contains
    "spools") cannot degrade that command's near misses. The 65 floor plus the coverage
    guard keep the rest of chat ("can you make my printer go brrr")
    suggestion-free. Deterministic, offline, no LLM involved — it runs
    in the fallback after every model lane has declined, so #466's
    no-LLM posture keeps it too.
    """
    task = _task_suggestion(text, bot_name)
    if task is not None:
        return task
    subcommand = subcommand_suggestion(text)
    if subcommand is not None:
        return subcommand
    scored, rest, wholes = _command_scores(text)
    if not scored:
        return None
    decision = fuzzy.decide(scored)
    if decision.kind == "none":
        return None
    if len(decision.contenders) != 1:
        winner = _whole_name_winner(decision, wholes)
        if winner is None:
            return None
        suggestion = f"/{winner.name}" + (f" {rest}" if rest else "")
    else:
        suggestion = f"/{decision.best.name}" + (f" {rest}" if rest else "")
    # A near miss in both words ("/spol chnage") gets both corrections:
    # the carried-over tail is the subcommand layer's to fix or leave.
    return subcommand_suggestion(suggestion) or suggestion


def command_suggestion_candidates(text):
    """The contending commands for a near miss with no single clear answer.

    When ``fuzzy.decide`` calls the top contested — a runner-up within
    the clear margin — there is no honest single suggestion, so the
    caller offers these canonical commands (arguments carried over) as a
    did-you-mean pick-list and lets the user choose. Capped at
    ``MAX_SUGGESTION_CANDIDATES`` so the choice fits a prompt. Returns []
    whenever a single suggestion exists or nothing scores at all.
    """
    scored, rest, wholes = _command_scores(text)
    if not scored:
        return []
    decision = fuzzy.decide(scored)
    if decision.kind != "suggest" or len(decision.contenders) < 2:
        return []
    if _whole_name_winner(decision, wholes) is not None:
        return []  # the tie-break resolves it: a single suggestion exists
    suffix = f" {rest}" if rest else ""
    return [f"/{cmd.name}{suffix}" for cmd in decision.contenders[:MAX_SUGGESTION_CANDIDATES]]


def agent_unavailable_text():
    """The honest reply when a configured [agent] endpoint did not answer (#524 stage 3).

    The unmatched reply would be the wrong wording here: the message may
    have been perfectly understandable — the configured agent endpoint
    failed, timed out or answered nonsense. Name the real cause, like the
    rewrite refusal does, and say what still works.
    """
    return ("The chat agent didn't answer, so nothing happened. "
            "Commands and links still work — post a link or type `/print-help`.")


# Explicit AI invocation: "ask ai: …", "hey ai, …", "ai: …". The person
# named the model lane; the invocation words are not part of the question.
_AI_INVOCATION = re.compile(
    r"^(?:please\s+)?(?:(?:ask|use|try|consult|hey)\s+(?:the\s+|your\s+)?ai(?:\s+assistant)?|ai)"
    r"\s*[:,]?\s+(?P<q>.+)$", re.I)


def _ai_invocation(text):
    """The bare question inside an explicit ask-the-AI message, else None."""
    clean = " ".join(str(text or "").split()).strip()
    match = _AI_INVOCATION.match(clean)
    if not match:
        return None
    return match.group("q").strip() or None


def ai_endpoint_notice_text():
    """The one-time notice: mentioned messages go to the configured AI endpoint (#1524).

    Sent once per user before their first conversational-lane call, so
    nobody's words leave for an endpoint without being told once.
    """
    return ("A quick note: when you mention me or ask the AI, that message is sent to this "
            "bot's configured AI endpoint to answer. Plain questions and commands are "
            "handled locally and never leave this machine.")


def ai_rate_limited_text():
    """The reply when a user is over the conversational lane's rate limit."""
    return ("You've hit the AI chat rate limit for now, so I didn't send that. "
            "Commands and plain questions still work — type `/print-help`.")


def ai_not_enabled_text():
    """The honest reply to an explicit AI invocation when no lane is enabled.

    Not the generic unmatched wording: the person asked for a specific
    thing, and it is the AI — not their phrasing — that is missing here.
    """
    return ("AI chat isn't switched on for this bot, so I can't ask it. "
            "Commands and links still work — type `/print-help` for everything I can do.")


def ai_no_answer_text():
    """The honest reply when an explicitly invoked AI lane produced no answer."""
    return ("I asked the AI, but it didn't answer, so nothing happened. "
            "Commands and links still work — type `/print-help`.")


def listed_users(cfg, section="discord"):
    """The raw allow list of a platform's settings section, without the approver."""
    if section == "matrix":  # Matrix IDs are @user:server strings, not numbers (#164 stage 3)
        return settings.matrix_user_ids(cfg[section].get("allowed_user_ids"))
    return settings.user_ids(cfg[section].get("allowed_user_ids"))


def allowed_users(cfg, section="discord"):
    """Allowed user IDs from a platform's settings section, plus its approver."""
    ids = listed_users(cfg, section)
    for approver in settings.approver_ids(cfg[section]):
        if approver not in ids:
            ids.append(approver)
    return ids


def denied_users(cfg, section="discord"):
    """The opt-in deny list of a platform's settings section (#386)."""
    if section == "matrix":
        return settings.matrix_user_ids(cfg[section].get("denied_user_ids"))
    return settings.user_ids(cfg[section].get("denied_user_ids"), key="denied_user_ids")


def discord_chat_llm_enabled(cfg):
    """Discord's operator-facing chat-LLM switch (#466): [discord] chat_llm_enabled.

    Off by default since #1376 (AI is for designing models): Discord runs
    as a pure bot (unmatched text gets the short not-understood reply,
    #467) while [llm] stays configured for the features that inherit it —
    /print-design resolves its endpoint from [design]/[llm] independently,
    and the camera checks keep their own vision.ai_enabled gate — so the
    chat switch changes none of them. True brings the stray-chat lanes back.
    """
    discord = (cfg or {}).get("discord") or {}
    return bool(discord.get("chat_llm_enabled", False))


def platform_name_of(source):
    """The platform name a source came from ('discord' for sources that do not say)."""
    value = getattr(getattr(source, "platform", None), "value", None) or getattr(source, "platform", None)
    return str(value or "discord")


def session_key(source):
    """One live session per person per channel or thread, like the gateway's group sessions."""
    return f"{platform_name_of(source)}:{source.chat_id}:{source.thread_id or ''}:{source.user_id}"


def make_source(user_id, chat_id, thread_id=None, is_bot=False, platform_name="discord",
                printer_id=None, shared_printer_ids=None):
    # printer_id is the roster printer this source's channel routes to (#335);
    # None means the configured single/default printer, as before routing existed.
    # shared_printer_ids is set instead when the channel is shared (#536): the
    # message is served but no printer is decided — printer-bound handlers ask,
    # and a shared channel never falls through to the default printer.
    return SimpleNamespace(platform=SimpleNamespace(value=str(platform_name)), profile=PROFILE,
                           user_id=str(user_id),
                           chat_id=str(chat_id), thread_id=str(thread_id) if thread_id else None,
                           is_bot=bool(is_bot), profile_route_rejected=False,
                           printer_id=str(printer_id) if printer_id else None,
                           shared_printer_ids=tuple(str(pid) for pid in shared_printer_ids)
                           if shared_printer_ids else None)


def make_event(source, text, message_id, media_urls=(), metadata=None, prov=None):
    # A chat user's text is third-party text: a handler that echoes an
    # argument back must never turn a typed MEDIA: line into an attachment.
    # ``prov`` is the invocation provenance (#1524): stamped by the
    # platform adapter and carried unchanged through process/fallback.
    meta = dict(metadata or {})
    if prov is not None:
        meta = provenance.stamp(meta, prov)
    elif "provenance" in meta and not isinstance(meta["provenance"], provenance.Provenance):
        meta["provenance"] = provenance.from_event(
            SimpleNamespace(metadata=meta), platform_name_of(source))
    return SimpleNamespace(source=source, text=rest.inert_media(text), media_urls=list(media_urls),
                           message_id=str(message_id), metadata=meta, internal=False)


_MESSAGE_LINK = re.compile(
    r"^\s*<?https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)/?>?\s*$")


def parse_message_link(text):
    """(guild_id, channel_id, message_id) when ``text`` is only a Discord message link, else None."""
    match = _MESSAGE_LINK.match(str(text or ""))
    return match.groups() if match else None


def channel_scope(channel):
    """(chat_id, thread_id) for a channel or a thread in one."""
    parent = getattr(channel, "parent_id", None)
    if parent:
        return str(parent), str(channel.id)
    return str(getattr(channel, "id", "")), None


def safe_name(name):
    name = Path(str(name or "model")).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    if len(cleaned) <= 120:
        return cleaned or "model"
    # Truncate the stem, never the extension (P2-39): save_attachments checks
    # the suffix, so a name cut through its extension was silently skipped.
    stem, dot, ext = cleaned.rpartition(".")
    if dot and 0 < len(ext) < 119:
        return f"{stem[:119 - len(ext)]}.{ext}"
    return cleaned[:120] or "model"


# The slash command a handler is answering, for the current asyncio task only
# (#1475). Each message and each slash command runs in its own task with its
# own copy of the context, so one command's interaction can never receive
# another command's replies: the shared adapter object is never swapped.
_SLASH_ADAPTER: contextvars.ContextVar = contextvars.ContextVar("print_handy_slash_adapter", default=None)


def _slash_adapter():
    """The current task's slash proxy while its command is still running, else None.

    A task spawned by a slash flow inherits the proxy with its context; once
    the command has returned (``closed``) such a task posts through the real
    adapter again instead of an interaction that is no longer waiting."""
    proxy = _SLASH_ADAPTER.get()
    if proxy is None or object.__getattribute__(proxy, "_delivery").closed:
        return None
    return proxy


class _RoutedAdapter:
    """``adapter`` attribute that answers with the current task's slash proxy, when one is live."""

    @property
    def adapter(self):
        return _slash_adapter() or self._adapter

    @adapter.setter
    def adapter(self, value):
        self._adapter = value


class Gateway(_RoutedAdapter):
    """The gateway calls the handlers make: may this person act, where do replies go, which session.

    Channel scope is the routing map (#335): a source is in scope when its
    chat is one a roster printer claims. With no roster map (a config that
    carries no [[printers]] list) the legacy single-chat check applies.
    Shared chats (#536) are in scope too, but route to no printer:
    :meth:`shared_printer_ids_for` names the candidates a handler must
    choose between by asking.
    """

    def __init__(self, adapter, chat_id, channel_printers=None, shared_printers=None,
                 setup_only=False):
        self.adapter = adapter
        # Setup-only mode (bootstrap): the install has a platform
        # credential but no chat/users/printer configuration yet. The
        # gateway itself still authorizes nobody new — admission is
        # the guided dispatcher's setup-only branch, which admits only
        # the setup surface; every other handler keeps using
        # _is_user_authorized_for_source and stays refused.
        self.setup_only = bool(setup_only)
        self.chat_id = str(chat_id)
        self.channel_printers = dict(channel_printers or {})
        self.shared_printers = {str(chat): [str(pid) for pid in ids]
                                for chat, ids in (shared_printers or {}).items()}

    def _is_user_authorized_for_source(self, source):
        chat = str(getattr(source, "chat_id", ""))
        routed = (chat in self.channel_printers or chat in self.shared_printers) \
            if (self.channel_printers or self.shared_printers) else chat == self.chat_id
        # /print-live from the configured live voice channel's own chat (#1504):
        # run_slash marks only that one command's source, after checking the
        # channel; the user allowlist below still applies unchanged.
        routed = routed or bool(getattr(source, "live_voice_channel", False))
        return routed and self.adapter.is_allowed_id(getattr(source, "user_id", None))

    def printer_id_for(self, source):
        """The roster printer a source's channel routes to (#335), else None (the default printer)."""
        pid = self.channel_printers.get(str(getattr(source, "chat_id", "")))
        if pid:
            return pid
        pid = getattr(source, "printer_id", None)
        return str(pid) if pid else None

    def shared_printer_ids_for(self, source):
        """The roster ids sharing a source's channel (#536), else None.

        None means the channel decides (a single claim, or the legacy
        default) — handlers treat that as decided, never as undecided.
        """
        ids = self.shared_printers.get(str(getattr(source, "chat_id", "")))
        if ids:
            return list(ids)
        stamped = getattr(source, "shared_printer_ids", None)
        return [str(pid) for pid in stamped] if stamped else None

    def _delivery_adapter_for(self, source):
        return self.adapter

    def _session_key_for_source(self, source):
        return session_key(source)


class _SlashDelivery:
    """Where one slash command's answers go: its own interaction (#1475).

    The interaction is deferred first; every ``send`` a handler makes while
    it runs is delivered as the interaction's followup — the one visible
    response — instead of a separate channel post next to an echoed
    command line. Button questions (``send_clarify``) still post through
    the real adapter: they are prompts with working buttons, recorded the
    way every typed command's prompts are.
    """

    def __init__(self, core, interaction, view=None):
        self.core = core
        self.interaction = interaction
        self.view = view
        self.sent = False
        self.prompted = False
        self.closed = False  # set when run_slash returns: later sends use the real adapter

    async def deliver(self, content, files=None):
        adapter = self.core._adapter  # the real adapter, never this flow's own proxy
        send_followup = getattr(adapter, "send_followup", None)
        if send_followup is not None:
            result = await send_followup(self.interaction, content, files=files,
                                         view=self.view if not self.sent else None)
        else:  # an adapter with no interaction delivery: the followup itself
            await self.interaction.followup.send(content)
            result = platform.SendResult(True)
        self.sent = True
        return result


class _SlashAdapter:
    """The platform adapter as a slash flow sees it: sends rerouted, rest delegated."""

    def __init__(self, real, delivery):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_delivery", delivery)

    def __getattr__(self, item):
        return getattr(object.__getattribute__(self, "_real"), item)

    def __setattr__(self, key, value):
        setattr(object.__getattribute__(self, "_real"), key, value)

    async def send(self, chat_id, content, reply_to=None, metadata=None, files=None):
        return await object.__getattribute__(self, "_delivery").deliver(content, files=files)

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        delivery = object.__getattribute__(self, "_delivery")
        delivery.prompted = True
        return await object.__getattribute__(self, "_real").send_clarify(
            chat_id, question, choices, clarify_id, session_key, metadata=metadata)


class BotCore(_RoutedAdapter):
    def __init__(self, client, cfg=None, *, router=None, llm_opener=None, agent_opener=None,
                 platform_name=None, adapter=None, chat_id=None, use_llm=None):
        """Run the handlers against one platform adapter.

        With no ``adapter`` the adapter is built for ``platform_name`` (or
        ``[bot] platform``, default Discord) from that platform's settings
        section — for Discord exactly the values used before the interface
        existed. Tests and later platform stages pass their own adapter
        implementing ``print_handy.bot.platform``; the core then never
        touches a platform library.
        """
        self.cfg = cfg if cfg is not None else _config.cfg()
        self.client = client
        self.platform_name = str(platform_name or (self.cfg.get("bot") or {}).get("platform")
                                 or getattr(adapter, "name", None) or "discord")
        section = self.cfg.get(self.platform_name) if isinstance(self.cfg.get(self.platform_name), dict) else {}
        discord = self.cfg.get("discord") or {}
        if adapter is not None:
            self.adapter = adapter
        else:
            module = platform.platform_module(self.platform_name)
            if module is None:
                raise ValueError(f"platform {self.platform_name!r} is not implemented yet (#164)")
            access_section = self.platform_name if section else "discord"
            # The adapter gets the raw list and the approver separately:
            # the access mode latches on the list alone, so setting only
            # an approval user does not opt into a whitelist (#386).
            self.adapter = module.StandaloneAdapter(
                client, listed_users(self.cfg, access_section),
                denied=denied_users(self.cfg, access_section),
                approver=settings.approver_ids(section) or None,
                approve_all=bool(section.get("approval_all_users")),
                upload_max_bytes=section.get("upload_max_bytes") or discord.get("upload_max_bytes"))
        self.chat_id = str(chat_id if chat_id is not None
                           else section.get("chat_id") or (discord.get("chat_id") if not section else "")
                           or "")
        # The channel → printer maps (#335): built once at startup. A chat
        # claimed once routes to its printer; a chat claimed by 2+ entries
        # is shared (#536) — served, but with no printer decided.
        self.channel_printers = routing.channel_map(self.cfg, self.platform_name)
        self.shared_printers = routing.shared_channel_map(self.cfg, self.platform_name)
        # The live camera's voice channel (#1504): its own text chat accepts
        # /print-live, and only /print-live. Discord only, like the command.
        live = self.cfg.get("live_stream") or {}
        self.live_voice_channel_id = (str(live.get("voice_channel_id") or "").strip()
                                      if self.platform_name == "discord" else "")
        self.gateway = Gateway(self.adapter, self.chat_id, self.channel_printers,
                               self.shared_printers,
                               setup_only=bool((self.cfg.get("bot") or {}).get("setup_only")))
        self.router = router if router is not None else Router()
        self.router.attach_platform(self.platform_name, client, self.adapter)
        if client is not None and hasattr(client, "add_listener") and hasattr(self.adapter, "handle_click"):
            client.add_listener(self.on_interaction, "on_interaction")
        self.upload_dir = Path(self.cfg["paths"]["cache"]) / f"{self.platform_name}-uploads"
        # Pure-bot platforms (the adapter reports uses_llm=False: Telegram
        # and Matrix, #164 stages 2-3) never reach the LLM fallback, even
        # when [llm] is configured: their unmatched text gets the short
        # unmatched reply only (#73, #467). Discord has the operator-facing
        # version of the same choice (#466): [discord] chat_llm_enabled,
        # off by default since #1376, keeps Discord a pure bot too without
        # unconfiguring [llm] for design. Web chat passes its own
        # [webui] chat_llm_enabled in as use_llm.
        # The switch gates every stray-chat model call (#524 stage 3): the
        # bounded [llm] call and the [agent] lane alike — use_llm is the one
        # gate the fallback checks for both.
        self.chat_llm_enabled = discord_chat_llm_enabled(self.cfg) if self.platform_name == "discord" else True
        self.use_llm = (bool(getattr(self.adapter, "uses_llm", True)) and self.chat_llm_enabled) \
            if use_llm is None else bool(use_llm)
        self.llm_opener = llm_opener
        self.agent_opener = agent_opener
        # #1524 tranche 6: the conversational lane's per-user rate limit
        # and the one-time endpoint notice. Both are process-local.
        from . import ai_tools as _ai_tools
        _nl_settings = self.cfg.get("natural_language") or {}
        self._ai_rate = _ai_tools.RateLimiter(
            max_calls=int(_nl_settings.get("ai_rate_limit_per_hour") or 20))
        self._ai_noticed: set = set()
        self.agent_unavailable = agent_unavailable_text()
        self._refusal_notices: dict[str, float] = {}  # user_id -> last notice time (#386)
        self._elsewhere_notices: dict[tuple, float] = {}  # (user_id, chat_id) -> last notice time
        # Did-you-mean waits running off the message path on serial-polling
        # platforms (P1-109): the tasks are tracked so they are never
        # garbage-collected mid-wait and their failures are logged.
        self._suggestion_tasks: set[asyncio.Task] = set()
        self._signal_tasks: set[asyncio.Task] = set()  # in-flight typing indicators
        # Per-session ordering for the model lane, on the event loop: a
        # queued ask waits on an asyncio lock, not in a worker thread.
        self._lane_turns = SessionTurns()
        # bot_name counts only when the operator set it (#1394 review): left
        # at the built-in default, the bot goes by its own account's name, so
        # renaming the account alone renames the bot in replies and mentions.
        self.configured_bot_name = mention.configured_name(section.get("bot_name") or discord.get("bot_name"))
        accounts = mention.account_names(self.platform_name, client)
        self.bot_name = self.configured_bot_name or (accounts[0] if accounts else mention.DEFAULT_BOT_NAME)
        # Help is a platform surface: a platform that cannot run the live
        # command does not advertise it (P2-123).
        self.help = commands.help_text(
            self.bot_name,
            supports_live_stream=bool(getattr(self.adapter, "supports_live_stream", False)))
        self.unmatched = unmatched_text(self.bot_name)
        self.started_at = time.time()
        self._pending_launch_announced = False

    async def announce_pending_launch(self):
        """One startup notice for a launch lease a hard kill left behind (#838).

        `launch_state.acquire()` self-settles from Moonraker history when the
        file did run, but a kill after `lease.submitting()` and before the
        start reached Moonraker never settles: every start is refused until
        an operator reconciles it, and until now nothing told them. Post one
        message naming the file and the reconcile command — once per process,
        however many startup passes run. Fail-soft: a launch database that
        cannot be read posts nothing and never blocks startup.
        """
        if self._pending_launch_announced:
            return
        self._pending_launch_announced = True
        try:
            from ..core import launch_state
            rows = await asyncio.to_thread(launch_state.pending)
        except Exception:
            logger.exception("could not check for a pending launch at startup")
            return
        # Only a start that may have been sent: a `leased` row never reached the printer and the
        # next acquire() releases it by itself, so it is no reason to send the operator to a shell.
        rows = [r for r in rows or () if r.get("phase") in ("submitting", "uncertain")]
        if not rows or not self.chat_id:
            return
        row = rows[0]
        file_name = row.get("remote") or row.get("job_json") or "a print file"
        extra = f" ({len(rows)} launches are held)" if len(rows) > 1 else ""
        try:
            await self.adapter.send(
                chat_id=self.chat_id,
                content=(f"Print Handy restarted while a print start was in flight: {file_name} "
                         f"may or may not have started{extra}. New prints wait until it is settled. "
                         "That happens by itself when the printer shows that file printing, or about "
                         "a minute after the printer gives up on it. If new prints stay blocked, "
                         "ask whoever runs Print Handy to settle it on the server."))
        except Exception:
            logger.exception("could not post the pending-launch notice")

    # ------------------------------------------------------------------ routing

    def printer_for_chat(self, chat_id):
        """(routed, printer_id) for one chat. Unrouted chats are dropped silently (#335).

        With a roster map, a chat routes exactly when a printer claims it and
        the id is that printer's. A shared chat (#536) routes with id None —
        served but undecided; the shared candidates ride on the source via
        :meth:`shared_ids_for_chat`. Without a roster map the legacy single
        chat routes, to the configured single printer (None).
        """
        chat_id = str(chat_id)
        if self.channel_printers or self.shared_printers:
            if chat_id in self.channel_printers:
                return True, self.channel_printers[chat_id]
            if chat_id in self.shared_printers:
                return True, None
            return False, None
        if self.chat_id and chat_id == self.chat_id:
            return True, None
        return False, None

    def shared_ids_for_chat(self, chat_id):
        """The roster ids sharing a chat (#536), or None when it is not shared."""
        ids = self.shared_printers.get(str(chat_id))
        return list(ids) if ids else None

    # ------------------------------------------------------------------ messages

    def provenance_for(self, addressed, message=None, *, channel_kind=None):
        """The stamped provenance for one addressed message (#1524).

        The value comes from the mention evidence in ``addressed``; a
        typed "ask ai:" opening with no stronger evidence becomes
        typed_ai_phrase. Channel kind defaults from the message: a
        Discord message with no guild is a direct message, Telegram
        private chats are direct messages, everything else is shared.
        Edited messages are stamped edited so they never re-trigger the
        conversational lane.
        """
        value = getattr(addressed, "provenance", "none") or "none"
        if value == "none" and _ai_invocation(getattr(addressed, "text", "")):
            value = "typed_ai_phrase"
        if channel_kind is None:
            channel_kind = "shared"
            kinds = " ".join(str(getattr(message, attr, "") or "") for attr in
                             ("chat_type", "conversation_type")) if message is not None else ""
            channel = getattr(message, "channel", None) if message is not None else None
            kinds += " " + str(getattr(channel, "type", "") or "")
            if "private" in kinds.casefold():
                channel_kind = "dm"
            elif self.platform_name == "webui":
                channel_kind = "webui"
        edited = bool(message is not None and (getattr(message, "edited_timestamp", None)
                                               or getattr(message, "edited_at", None)
                                               or getattr(message, "edited", False)))
        return provenance.Provenance(value=value, platform=self.platform_name,
                                     channel_kind=channel_kind, edited=edited)

    def addressed(self, text, message=None):
        """``text`` with a mention of this bot recognised and stripped (see ``bot/mention.py``).

        The bot's identity is read from the platform's logged-in account on
        every call, plus ``bot_name`` when the operator set it; never a
        hard-coded name.
        """
        identity = mention.identity_for(self.platform_name, self.client, self.configured_bot_name, message)
        return mention.address(text, identity)

    async def handle_message(self, message):
        author = getattr(message, "author", None)
        me = getattr(getattr(self.client, "user", None), "id", None)
        if author is None or getattr(author, "bot", False) or (me is not None and author.id == me):
            return None
        chat_id, thread_id = channel_scope(message.channel)
        routed, printer_id = self.printer_for_chat(chat_id)
        # A mention of the bot ("@Print Handy spool list") is stripped here,
        # before any gate sees the text; a message with none is unchanged.
        addressed = self.addressed(getattr(message, "content", ""), message)
        if addressed.elsewhere:
            return None  # "/cmd@OtherBot": a command for a different bot
        if not routed and not self.gateway.setup_only:
            if addressed.mentioned and self.adapter.is_allowed_id(author.id):
                # Only someone the access lists admit hears where the bot works.
                await self._note_elsewhere(author.id, chat_id, thread_id)
            return None
        source = make_source(author.id, chat_id, thread_id, platform_name=self.platform_name,
                             printer_id=printer_id,
                             shared_printer_ids=self.shared_ids_for_chat(chat_id))
        if self.gateway.setup_only:
            # Setup-only admission reaches only the guided dispatcher
            # (the first router hook), which enforces the Q5 bind and
            # refuses every non-setup command itself; no other handler
            # is authorized by this branch. Nothing is downloaded here:
            # nobody has been admitted yet, and setup takes no attachments.
            event = make_event(source, addressed.text, message.id,
                               prov=self.provenance_for(addressed, message))
            return await self.process(event)
        if not self.gateway._is_user_authorized_for_source(source):
            # In the print channel but refused by an access list: never
            # silent (#386) — an INFO log naming the user and the reason,
            # plus one throttled in-channel notice.
            await self._note_refusal(source)
            return None
        if addressed.text.strip() or getattr(message, "attachments", None):
            self._signal_working(source)
        content_from, text = message, addressed.text
        if parse_message_link(text):
            content_from = await self._linked_message(message)
            if isinstance(content_from, str):
                await self.adapter.send(chat_id=source.chat_id, content=content_from,
                                        metadata={"thread_id": source.thread_id} if source.thread_id else None)
                return None
            linked = self.addressed(getattr(content_from, "content", ""), content_from)
            if linked.elsewhere:
                return None  # the linked message was meant for a different bot
            text = linked.text
        media = await self.save_attachments(content_from)
        prov = self.provenance_for(addressed, message)
        if content_from is not message:
            prov = self.provenance_for(self.addressed(getattr(content_from, "content", ""), content_from),
                                       content_from)
        event = make_event(source, text, message.id, media, prov=prov)
        return await self.process(event)

    async def _note_elsewhere(self, user_id, chat_id, thread_id=None):
        """Answer a mention outside the print channels, once per user and channel per period.

        The same words a slash command gets there. Nothing else runs: the
        channel is not routed, so no command is handled in it. Throttle
        entries older than the period are dropped, so the map stays small.
        """
        now = time.time()
        self._elsewhere_notices = {k: t for k, t in self._elsewhere_notices.items()
                                   if now - t < REFUSAL_NOTICE_PERIOD_S}
        key = (str(user_id), str(chat_id))
        if key in self._elsewhere_notices:
            return
        self._elsewhere_notices[key] = now
        try:
            await self.adapter.send(chat_id=chat_id, content=f"{self.bot_name} only works in its print channel.",
                                    metadata={"thread_id": thread_id} if thread_id else None)
        except Exception as exc:  # no permission to post there, or the platform is down
            logger.info("could not answer a mention in channel %s: %r", chat_id, exc)

    async def _linked_message(self, message):
        """The message a pasted Discord message link points at, or the reason it cannot be used.

        Pasting a link to an earlier message in a print channel runs that
        message's model link or file again, as the person who pasted it: the
        same access check already passed for them, and every button after it
        still asks them. Only links into this server's print channels are
        followed, so a link never reads a channel the bot does not serve.
        """
        guild_id, channel_id, message_id = parse_message_link(message.content)
        guild = getattr(message, "guild", None)
        if guild is None or str(getattr(guild, "id", "")) != guild_id:
            return "That message link points outside this server, so nothing happened."
        routed, _printer = self.printer_for_chat(channel_id)
        if not routed and channel_id != channel_scope(message.channel)[0]:
            return "That message link points at a channel I do not print from, so nothing happened."
        try:
            channel = self.client.get_channel(int(channel_id)) or await self.client.fetch_channel(int(channel_id))
            linked = await channel.fetch_message(int(message_id))
        except Exception as exc:  # deleted message, no access, Discord down
            logger.info("could not read linked message %s/%s: %r", channel_id, message_id, exc)
            return "I could not open that message (it may be deleted), so nothing happened."
        if getattr(getattr(linked, "author", None), "bot", False):
            return ("That message link points at one of my own posts. Link the message with the "
                    "model link or file instead.")
        if parse_message_link(getattr(linked, "content", "")):
            return "That message is itself a message link. Link the message with the model link or file."
        logger.info("message %s re-runs linked message %s/%s", message.id, channel_id, message_id)
        return linked

    def _signal_working(self, source):
        """Fire the platform's typing indicator for an accepted message, off the message path.

        Only called once a message is admitted (author, channel and access
        list all passed) and has something to act on, so ignored messages
        never show the bot typing. It is cosmetic: it never blocks the
        handler, never raises into it, and a failure is logged at DEBUG.
        """
        try:
            signal = getattr(self.adapter, "signal_working", None)
            if signal is None:
                return
            loop = asyncio.get_running_loop()
            metadata = {"thread_id": source.thread_id} if source.thread_id else None

            async def _run():
                try:
                    await asyncio.wait_for(signal(source.chat_id, metadata=metadata), WORKING_SIGNAL_TIMEOUT_S)
                except Exception as exc:  # cosmetic only: never surfaces
                    logger.debug("typing indicator failed on %s: %r", self.platform_name, exc)

            task = loop.create_task(_run())
            self._signal_tasks.add(task)
            task.add_done_callback(self._signal_tasks.discard)
        except Exception as exc:
            logger.debug("could not start the typing indicator: %r", exc)

    async def _note_refusal(self, source):
        """Log a list refusal at INFO and tell the channel, once per user per period (#386)."""
        reason = self.adapter.refusal_reason(source.user_id)
        if reason is None:
            return  # authorized after all (a race with a config reload); nothing to note
        logger.info("ignoring message from %s user %s in channel %s: %s",
                    self.platform_name, source.user_id, source.chat_id, reason)
        now = time.time()
        last = self._refusal_notices.get(str(source.user_id), 0.0)
        if now - last < REFUSAL_NOTICE_PERIOD_S:
            return
        self._refusal_notices[str(source.user_id)] = now
        metadata = {"thread_id": source.thread_id} if source.thread_id else None
        await self.adapter.send(chat_id=source.chat_id, content=REFUSAL_NOTICE, metadata=metadata)

    async def process(self, event, *, allow_llm=True, _llm_replied=False):
        source = event.source
        metadata = {"thread_id": source.thread_id} if source.thread_id else None
        text = event.text.strip()
        if text.casefold() in HELP_WORDS:
            await self.adapter.send(chat_id=source.chat_id, content=self.help, metadata=metadata)
            return {"action": "skip", "reason": "help"}
        if text and not self.gateway.setup_only \
                and not (getattr(event, "metadata", None) or {}).get("subcommand_fallthrough"):
            # A near-miss subcommand word (#1496): the handlers answer an
            # unknown subcommand word themselves, so the fallback's
            # did-you-mean never sees it — offer the correction here,
            # before dispatch. A decline or an expiry falls through and
            # the handler answers the original text exactly as today.
            sub_suggestion = subcommand_suggestion(text)
            if sub_suggestion is not None:
                offered = await self._offer_subcommand_suggestion(event, sub_suggestion, metadata)
                if offered is not None:
                    return offered
        result = await self.router.dispatch(event, self.gateway)
        if result is None and self.gateway.setup_only:
            from .guided import SETUP_ONLY_REFUSAL
            await self.adapter.send(chat_id=source.chat_id, content=SETUP_ONLY_REFUSAL,
                                    metadata=metadata)
            return {"action": "skip", "reason": "setup-only mode"}
        if result is not None:
            if result.get("action") == "rewrite":
                # A rewrite hands the rest to an agent. There is none here: say so, start nothing.
                await self.adapter.send(chat_id=source.chat_id, metadata=metadata,
                                        content="That needs the chat agent, which is not running. Nothing started.")
            return result
        if text and self.adapter.typed_approval_blocked(session_key(source), source.user_id):
            # A typed "yes" is the approval button's twin (2.2.5 review): the
            # prompt stays open for the approval user.
            await self.adapter.send(chat_id=source.chat_id, content=APPROVAL_ONLY_NOTICE, metadata=metadata)
            return {"action": "skip", "reason": "typed approval refused"}
        if text and await self.adapter.answer_typed(session_key(source), text, user_id=source.user_id):
            return {"action": "skip", "reason": "typed answer to an open question"}
        if not text:
            return None
        return await self.fallback(event, allow_llm=allow_llm, _llm_replied=_llm_replied)

    async def fallback(self, event, *, allow_llm=True, _llm_replied=False):
        """No handler took it: a model lane tries first (#437), the short unmatched reply is last.

        Order of handling for a non-command message: explicit commands,
        deterministic handlers, links/files and typed answers to an open
        prompt have already run in ``process`` and are never rerouted here.
        What is left goes to a stray-chat lane when one is enabled on a
        ``uses_llm`` platform — the [agent] lane first when an agent
        endpoint is configured (#524 stage 3: an operator who configures
        one means the agent to be the chat brain; its reply posts as-is
        and can never name a command), else the bounded [llm] call: it may
        answer directly, or name one allowlisted command, which then runs
        through the exact same handler (and confirmation) as a typed
        command. Only when no lane answers does the #468 stage-3
        did-you-mean prompt get its chance at a near-miss command, and
        only when it does not fire (or is declined) does the short
        unmatched reply go out (#467 — the full command list is an answer to
        ``/print-help``, never an error response) — once, and never
        alongside a lane reply for the same message. A configured agent
        endpoint that fails gets the honest agent-unavailable line
        instead: the message may have been fine; the endpoint was not.
        Both lanes sit behind the one ``use_llm`` gate, so
        ``discord.chat_llm_enabled=false`` (#466) means no stray-chat
        model call of any kind.
        An explicit invocation ("ask ai: …") is recognised before the
        lanes run: the invocation words are stripped and the lanes are
        asked the bare question. When no lane answers an explicit
        invocation — or none is enabled at all — the stripped question
        still gets the deterministic suggestion chance ("ask ai change
        the filament" on a no-LLM platform is a spool change wearing an
        invocation), and the terminal reply names the AI instead of the
        generic unmatched wording: the person asked for a specific
        thing, and it was the AI, not their phrasing, that was missing.
        """
        source = event.source
        metadata = {"thread_id": source.thread_id} if source.thread_id else None
        invoke = _ai_invocation(event.text)
        lane_text = invoke if invoke is not None else event.text
        # #1524: provenance decides whether a general conversational lane
        # may run at all, and a deterministic match always beats the AI
        # lane. An unstamped event (a direct process call) derives its
        # provenance from the text evidence, exactly as handle_message
        # would have stamped it.
        from . import natural_language
        prov = provenance.from_event(event, self.platform_name)
        if "provenance" not in (getattr(event, "metadata", None) or {}):
            prov = self.provenance_for(self.addressed(event.text), None,
                                       channel_kind="webui" if self.platform_name == "webui" else "shared")
        if prov.value == "none" and invoke is not None:
            prov = provenance.Provenance(
                value="typed_ai_phrase", platform=self.platform_name,
                channel_kind=prov.channel_kind, edited=prov.edited)
        nl_cfg = (self.cfg.get("natural_language") or {}) if isinstance(self.cfg, dict) else {}
        lane_unlocked = prov.unlocks_for_channel(
            source.chat_id, ai_disabled_channels=nl_cfg.get("ai_disabled_channel_ids") or ())
        lane_allowed = bool(allow_llm and self.use_llm and lane_unlocked)
        if not _llm_replied:
            # Deterministic-first: a direct natural-language answer (the
            # #1524 capability layer) always beats the AI lane — a
            # mentioned "how much purple PLA" gets the Spoolman answer,
            # never a model call. Did-you-mean suggestions stay after the
            # lanes, as before: a suggestion is a clarification prompt,
            # not an answer.
            det = await self._natural_language_answer(event, metadata)
            if det is not None:
                await self.adapter.send(chat_id=source.chat_id, content=det,
                                        reply_to=event.message_id, metadata=metadata)
                return {"action": "skip", "reason": "natural language answer"}
        if lane_allowed and (agent.configured(self.cfg) or llm.configured(self.cfg)):
            # #1524 tranche 6: per-user rate limit and the one-time
            # endpoint notice gate every conversational-lane call.
            if not self._ai_rate.allow(source.user_id):
                await self.adapter.send(chat_id=source.chat_id, content=ai_rate_limited_text(),
                                        reply_to=event.message_id, metadata=metadata)
                return {"action": "skip", "reason": "ai rate limited"}
            if str(source.user_id) not in self._ai_noticed:
                self._ai_noticed.add(str(source.user_id))
                await self.adapter.send(chat_id=source.chat_id,
                                        content=ai_endpoint_notice_text(), metadata=metadata)
            from . import natural_language as _nl
            _nl.record_route("ai_lane")
        if lane_allowed and agent.configured(self.cfg):
            try:
                reply = await asyncio.to_thread(agent.ask, lane_text, session_key(source),
                                                cfg=self.cfg, opener=self.agent_opener)
            except Exception as exc:
                logger.warning("agent reply failed, sending the agent-unavailable reply: %r", exc)
                await self.adapter.send(chat_id=source.chat_id, content=self.agent_unavailable,
                                        reply_to=event.message_id, metadata=metadata)
                return {"action": "skip", "reason": "agent unavailable"}
            # The endpoint's text is untrusted (#532): it may name a local file, never attach one.
            await self.adapter.send(chat_id=source.chat_id, content=rest.inert_media(reply),
                                    reply_to=event.message_id, metadata=metadata)
            return {"action": "skip", "reason": "agent reply"}
        if lane_allowed and llm.configured(self.cfg):
            try:
                # One turn per session at a time, queued on the loop: the
                # blocking HTTP call alone runs in a worker thread, so asks
                # waiting behind it hold no thread (llm.ask's own session
                # lock is then never contended from here).
                from . import ai_tools

                def _tool_runner(name, arguments):
                    return ai_tools.run_tool(self, name, arguments)

                async with self._lane_turns.turn(session_key(source)):
                    reply, proposal = await asyncio.to_thread(
                        llm.ask, lane_text, self.help, session=session_key(source),
                        cfg=self.cfg, opener=self.llm_opener, tool_runner=_tool_runner)
            except Exception as exc:
                logger.warning("LLM reply failed, sending the unmatched reply: %r", exc)
            else:
                if proposal:
                    # #1524 tranche 6: the model proposed an action as a
                    # capability plus typed slots. The runtime builds the
                    # command from the slots, the allowlist gate decides
                    # whether the lane may run it at all, and the normal
                    # handler's own confirmation is what the user sees —
                    # a mention is never consent to mutate. A proposal
                    # whose built command is not allowlisted is dropped
                    # and the reply stands alone.
                    built = ai_tools.proposal_command(proposal["capability"],
                                                      proposal["slots"])
                    command = llm.allowed_command(built) if built else None
                    if command:
                        # A reply that tells the person to type the very
                        # command about to run contradicts the confirm
                        # that follows it (#1373): the command's own
                        # output is the answer, so that reply is dropped.
                        replied = (bool(reply) and command.casefold() not in HELP_WORDS
                                   and not _names_command(reply, command))
                        if replied:
                            await self.adapter.send(
                                chat_id=source.chat_id, content=rest.inert_media(reply),
                                reply_to=event.message_id, metadata=metadata)
                        routed = make_event(source, command, f"{event.message_id}:llm",
                                            media_urls=event.media_urls,
                                            metadata={"llm_command": True})
                        return await self.process(routed, allow_llm=False,
                                                  _llm_replied=replied)
                if reply:
                    await self.adapter.send(chat_id=source.chat_id, content=rest.inert_media(reply),
                                            reply_to=event.message_id, metadata=metadata)
                    return {"action": "skip", "reason": "llm reply"}
        if _llm_replied:
            # The LLM already answered this message and its named command
            # fell through: a solved reply stands alone, no help after it.
            return {"action": "skip", "reason": "llm reply"}
        suggestion = command_suggestion(event.text, self.bot_name)
        if suggestion is not None:
            # #468 stage 3: a near-miss command gets a did-you-mean prompt
            # instead of the plain unmatched reply.
            natural_language.record_route("suggestion")
            return await self._offer_suggestion(event, suggestion, metadata)
        candidates = command_suggestion_candidates(event.text)
        if candidates:
            natural_language.record_route("suggestion")
            return await self._offer_suggestion_choice(event, candidates, metadata)
        if invoke is not None:
            # An explicit invocation no lane answered: the stripped
            # question gets the deterministic chance on its own words,
            # then — only when the invocation's provenance actually
            # unlocks the lane (a typed "ask ai:" in a shared channel
            # does not, by owner decision) — the AI-specific terminal
            # reply; otherwise the plain unmatched reply below.
            suggestion = command_suggestion(invoke, self.bot_name)
            if suggestion is not None:
                natural_language.record_route("suggestion")
                return await self._offer_suggestion(event, suggestion, metadata)
            candidates = command_suggestion_candidates(invoke)
            if candidates:
                natural_language.record_route("suggestion")
                return await self._offer_suggestion_choice(event, candidates, metadata)
            if lane_unlocked:
                if lane_allowed and (agent.configured(self.cfg) or llm.configured(self.cfg)):
                    await self.adapter.send(chat_id=source.chat_id, content=ai_no_answer_text(),
                                            reply_to=event.message_id, metadata=metadata)
                    return {"action": "skip", "reason": "ai no answer"}
                await self.adapter.send(chat_id=source.chat_id, content=ai_not_enabled_text(),
                                        reply_to=event.message_id, metadata=metadata)
                return {"action": "skip", "reason": "ai not enabled"}
        await self.adapter.send(chat_id=source.chat_id, content=self.unmatched, reply_to=event.message_id,
                                metadata=metadata)
        return {"action": "skip", "reason": "command list"}

    async def _natural_language_answer(self, event, metadata):
        """A direct deterministic natural-language answer (#1524).

        Delegates to the capability layer (bot/natural_language.py):
        read-only intents answer directly from real services, in shadow
        mode the layer only logs disagreements and this returns None.
        Any failure in the layer reads as no answer — the lanes and the
        suggestion path below still get their chance.
        """
        try:
            from . import natural_language
            result = natural_language.answer(self, event, metadata)
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception:
            logger.exception("natural-language layer failed; continuing to the fallback")
            return None

    def _natural_language_status(self):
        try:
            from . import natural_language as _nl
            return {"routes": _nl.route_counters(),
                    "unmatched_logged": len(_nl.export_unmatched()),
                    "unmatched_recent": [row["phrase"] for row in _nl.export_unmatched()[-20:]],
                    "shadow_disagreements": len(_nl.shadow_disagreements())}
        except Exception:
            logger.debug("natural-language diagnostics unavailable", exc_info=True)
            return {"routes": {}, "unmatched_logged": 0, "unmatched_recent": [],
                    "shadow_disagreements": 0}

    def _background_suggestion(self, coro):
        """Run a did-you-mean wait off the message path (P1-109).

        On a serial-polling platform (Telegram, Matrix) the polling loop
        awaits each message's handling before fetching the next update —
        the very update that answers the prompt — so waiting inline for up
        to SUGGESTION_TIMEOUT_S starved the prompt of its own answer and
        delayed every other user. The prompt is registered and posted
        inline (so the next update already finds it open), and only the
        wait and its dispatch continuation run in this managed background
        task; event-driven platforms (Discord) keep the fully inline flow,
        whose caller awaits the outcome.
        """
        task = asyncio.ensure_future(coro)
        self._suggestion_tasks.add(task)

        def _done(done_task):
            self._suggestion_tasks.discard(done_task)
            if not done_task.cancelled() and done_task.exception() is not None:
                logger.warning("background did-you-mean flow failed: %r", done_task.exception())

        task.add_done_callback(_done)
        return {"action": "skip", "reason": "did-you-mean suggestion pending"}

    async def _deliver_suggestion(self, event, question, labels, metadata):
        """Register and post a did-you-mean prompt; its clarify_id, or None
        when delivery failed (the registry row is cleaned up either way)."""
        source = event.source
        registry = prompts.gateway()
        clarify_id = f"fuzzy-{uuid.uuid4().hex[:12]}"
        try:
            registry.register(clarify_id, session_key(source), question, labels)
            result = await self.adapter.send_clarify(chat_id=source.chat_id, question=question,
                                                      choices=labels, clarify_id=clarify_id,
                                                      session_key=session_key(source), metadata=metadata)
            if not getattr(result, "success", False):
                raise RuntimeError("did-you-mean delivery failed")
        except Exception:
            logger.warning("did-you-mean suggestion failed; sending the unmatched reply", exc_info=True)
            registry.resolve_gateway_clarify(clarify_id, registry.CANCELLED)
            registry.wait_for_response(clarify_id, 0.001)
            return None
        return clarify_id

    async def _wait_suggestion(self, clarify_id):
        """(answer, delivered): wait out an open prompt, then close its row.

        The wait runs on the event loop (``await_prompt``), not in a worker
        thread: each open did-you-mean prompt used to hold a default-pool
        thread for up to SUGGESTION_TIMEOUT_S, and on a small container a
        handful of them starved every other background call.
        """
        registry = prompts.gateway()
        answer, delivered = None, True
        try:
            answer = await await_prompt(registry, clarify_id, SUGGESTION_TIMEOUT_S)
        except Exception:
            logger.warning("did-you-mean suggestion failed; sending the unmatched reply", exc_info=True)
            delivered = False
        finally:
            registry.resolve_gateway_clarify(clarify_id, registry.CANCELLED)
            registry.wait_for_response(clarify_id, 0.001)
        return answer, delivered

    async def _suggestion_unmatched(self, event, metadata):
        await self.adapter.send(chat_id=event.source.chat_id, content=self.unmatched,
                                reply_to=event.message_id, metadata=metadata)
        return {"action": "skip", "reason": "command list"}

    async def _offer_suggestion(self, event, suggestion, metadata):
        """Ask "Did you mean …?" and dispatch only on an explicit yes (#468 stage 3).

        The prompt lives in the shared clarify registry, so it works on
        every platform the bot serves and can be answered by button or by
        typing "yes". A yes re-dispatches the corrected text through
        ``process`` exactly as if it had been typed — the suggested command
        still runs its own confirmation and names its target before
        anything prints, so the suggestion itself starts nothing. A no, a
        typed non-answer, or the 60-second expiry closes the prompt; a no
        (or a delivery failure) still gets the plain unmatched reply, so
        the decline path is byte-identical to today's fallback.
        """
        question = _suggestion_question(suggestion)
        clarify_id = await self._deliver_suggestion(
            event, question, [SUGGEST_YES_LABEL, SUGGEST_NO_LABEL], metadata)
        if clarify_id is None:
            return await self._suggestion_unmatched(event, metadata)
        if getattr(self.adapter, "polls_serially", False):
            return self._background_suggestion(
                self._answer_suggestion(event, suggestion, metadata, clarify_id))
        return await self._answer_suggestion(event, suggestion, metadata, clarify_id)

    async def _answer_suggestion(self, event, suggestion, metadata, clarify_id):
        source = event.source
        answer, delivered = await self._wait_suggestion(clarify_id)
        if not delivered:
            return await self._suggestion_unmatched(event, metadata)
        if answer == SUGGEST_YES_LABEL or (isinstance(answer, str) and answer.strip().casefold() in {"yes", "y"}):
            await self.adapter.close_prompt(clarify_id, _running_note(suggestion))
            # The corrected dispatch carries the original attachments (P2-222):
            # accepting a suggestion must behave like typing the correct
            # command with the same files attached.
            routed = make_event(source, suggestion, f"{event.message_id}:fuzzy",
                                media_urls=event.media_urls, metadata={"fuzzy_command": True})
            return await self.process(routed, allow_llm=False)
        if answer is None:
            await self.adapter.close_prompt(clarify_id, "That suggestion expired — nothing ran.")
            return {"action": "skip", "reason": "did-you-mean suggestion expired"}
        await self.adapter.close_prompt(clarify_id, "OK — nothing ran.")
        return await self._suggestion_unmatched(event, metadata)

    async def _offer_subcommand_suggestion(self, event, suggestion, metadata):
        """Ask "Did you mean …?" for a near-miss subcommand word (#1496).

        The same clarify prompt as ``_offer_suggestion``, with one
        difference: the original message has a handler answer of its own
        (a usage line, a menu, a report), so a decline, an expiry or a
        delivery failure re-dispatches the original text — marked so it
        is not offered twice — instead of the plain unmatched reply.
        Only an explicit yes runs the corrected form, through
        ``process`` exactly as if it had been typed. Returns None only
        when the prompt could not be delivered at all, so ``process``
        falls through to dispatch itself.
        """
        question = _suggestion_question(suggestion)
        clarify_id = await self._deliver_suggestion(
            event, question, [SUGGEST_YES_LABEL, SUGGEST_NO_LABEL], metadata)
        if clarify_id is None:
            return None
        if getattr(self.adapter, "polls_serially", False):
            return self._background_suggestion(
                self._answer_subcommand_suggestion(event, suggestion, metadata, clarify_id))
        return await self._answer_subcommand_suggestion(event, suggestion, metadata, clarify_id)

    async def _answer_subcommand_suggestion(self, event, suggestion, metadata, clarify_id):
        source = event.source
        answer, delivered = await self._wait_suggestion(clarify_id)
        if not delivered:
            # The wait itself failed (background path included): the
            # handler's own answer is the honest outcome, so dispatch
            # the original here rather than dropping the message.
            original = make_event(source, event.text, f"{event.message_id}:fuzzy-fallthrough",
                                  media_urls=event.media_urls,
                                  metadata={"subcommand_fallthrough": True})
            return await self.process(original, allow_llm=False)
        if answer == SUGGEST_YES_LABEL or (isinstance(answer, str) and answer.strip().casefold() in {"yes", "y"}):
            await self.adapter.close_prompt(clarify_id, _running_note(suggestion))
            routed = make_event(source, suggestion, f"{event.message_id}:fuzzy",
                                media_urls=event.media_urls, metadata={"fuzzy_command": True})
            return await self.process(routed, allow_llm=False)
        if answer is None:
            await self.adapter.close_prompt(clarify_id, "That suggestion expired — nothing ran.")
        else:
            await self.adapter.close_prompt(clarify_id, "OK — nothing ran.")
        original = make_event(source, event.text, f"{event.message_id}:fuzzy-fallthrough",
                              media_urls=event.media_urls,
                              metadata={"subcommand_fallthrough": True})
        return await self.process(original, allow_llm=False)

    async def _offer_suggestion_choice(self, event, candidates, metadata):
        """Ask "Did you mean one of these?" for a contested near miss.

        The shared policy (``fuzzy.decide``) found several commands
        within the clear margin of the top score, so no single suggestion
        is honest. The contenders are offered through the same clarify
        registry as the single-suggestion prompt; only an explicit pick
        re-dispatches, through ``process`` exactly as if typed — the
        picked command still runs its own confirmation and names its
        target before anything prints. "None of these", a non-answer, a
        delivery failure or the 60-second expiry all fall back to the
        plain unmatched reply path, and nothing runs.
        """
        question = _suggestion_question(None, candidates)
        clarify_id = await self._deliver_suggestion(
            event, question, [*candidates, SUGGEST_NONE_LABEL], metadata)
        if clarify_id is None:
            return await self._suggestion_unmatched(event, metadata)
        if getattr(self.adapter, "polls_serially", False):
            return self._background_suggestion(
                self._answer_suggestion_choice(event, candidates, metadata, clarify_id))
        return await self._answer_suggestion_choice(event, candidates, metadata, clarify_id)

    async def _answer_suggestion_choice(self, event, candidates, metadata, clarify_id):
        source = event.source
        answer, delivered = await self._wait_suggestion(clarify_id)
        if not delivered:
            return await self._suggestion_unmatched(event, metadata)
        if answer in candidates:
            await self.adapter.close_prompt(clarify_id, _running_note(answer))
            routed = make_event(source, answer, f"{event.message_id}:fuzzy",
                                media_urls=event.media_urls, metadata={"fuzzy_command": True})
            return await self.process(routed, allow_llm=False)
        if answer is None:
            await self.adapter.close_prompt(clarify_id, "That suggestion expired — nothing ran.")
            return {"action": "skip", "reason": "did-you-mean suggestion expired"}
        await self.adapter.close_prompt(clarify_id, "OK — nothing ran.")
        return await self._suggestion_unmatched(event, metadata)

    def prune_uploads(self, now=None):
        now = time.time() if now is None else now
        for old in self.upload_dir.glob("doc_*"):
            try:
                if now - old.stat().st_mtime > UPLOAD_KEEP_S:
                    old.unlink()
            except OSError:
                logger.debug("Failed to remove expired upload %s", old, exc_info=True)

    async def save_attachments(self, message):
        """Save dropped .3mf/.stl/.gcode/.zip (and .scad, for design) files where readers look. Others are ignored."""
        self.prune_uploads()
        paths = []
        for attachment in getattr(message, "attachments", None) or []:
            name = safe_name(getattr(attachment, "filename", ""))
            # Sliced G-code is saved too: it prints only as an explicit yolo
            # (core/yolo.py), and is never silently dropped.
            if (Path(name).suffix.lower() not in MODEL_FILE_EXTS | ARCHIVE_EXTS
                    and not name.lower().endswith(".gcode")):
                continue
            if int(getattr(attachment, "size", 0) or 0) > MAX_UPLOAD_BYTES:
                logger.warning("skipping %s: larger than %d bytes", name, MAX_UPLOAD_BYTES)
                continue
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            # doc_<12 hex>_ is the gateway cache prefix; the intake strips it for the job title.
            target = self.upload_dir / f"doc_{uuid.uuid4().hex[:12]}_{name}"
            await attachment.save(target)
            paths.append(str(target))
        return paths

    # ------------------------------------------------------------------ interactions

    async def on_interaction(self, interaction):
        await self.adapter.handle_click(interaction)

    def _print_channel_refusal(self, name=None):
        """The slash refusal for an unrouted channel, naming where to go (#1504)."""
        channels = list(self.channel_printers) + [c for c in self.shared_printers
                                                  if c not in self.channel_printers]
        if not channels and self.chat_id:
            channels = [self.chat_id]
        text = f"{self.bot_name} only works in its print channel"
        if self.platform_name == "discord" and channels:
            text += ": " + ", ".join(f"<#{c}>" for c in channels[:3])
        text += "."
        if name == "print-live" and self.live_voice_channel_id:
            text += f" /print-live also works in <#{self.live_voice_channel_id}>."
        return text

    async def run_slash(self, interaction, name, values, *, view=None):
        """A slash command runs as its typed text, after the same channel and user checks.

        One visible response (#1475): the interaction is deferred and the
        handler's answer is delivered as the interaction's own followup —
        no echoed command line in the channel and no separate answer post.
        Read-only commands (the table's ``ephemeral`` ones) defer
        ephemerally, so only the asker sees the answer.
        """
        user = interaction.user
        chat_id, thread_id = channel_scope(interaction.channel)
        routed, printer_id = self.printer_for_chat(chat_id)
        live_voice = (not routed and name == "print-live" and bool(self.live_voice_channel_id)
                      and str(chat_id) == self.live_voice_channel_id and thread_id is None)
        source = make_source(user.id, chat_id, thread_id, getattr(user, "bot", False),
                             platform_name=self.platform_name, printer_id=printer_id,
                             shared_printer_ids=self.shared_ids_for_chat(chat_id))
        if live_voice:
            # The live session is install-wide (one voice channel), so the
            # command needs no printer of its own; the source is marked so the
            # gateway's authorization treats this one command as routed.
            source.live_voice_channel = True
            routed = True
        if not routed and not self.gateway.setup_only:
            await interaction.response.send_message(self._print_channel_refusal(name), ephemeral=True)
            return None
        if not self.gateway.setup_only and not self.gateway._is_user_authorized_for_source(source):
            logger.info("refused slash command from %s user %s in channel %s: %s",
                        self.platform_name, source.user_id, source.chat_id,
                        self.adapter.refusal_reason(source.user_id))
            await interaction.response.send_message("Not allowed. Nothing changed.", ephemeral=True)
            return None
        text = commands.as_text(name, values)
        command = commands.by_name(name)
        await interaction.response.defer(ephemeral=bool(command and command.ephemeral))
        delivery = _SlashDelivery(self, interaction, view=view)
        # Context-local, never a swap of the shared adapter: a message or a
        # second slash command handled while this one awaits a button keeps
        # its own delivery (review of #1484).
        token = _SLASH_ADAPTER.set(_SlashAdapter(self._adapter, delivery))
        try:
            if command is not None and command.name == "print-help":
                # Slash help is the short form plus the command menu (the
                # view), never the full wall the typed form still answers.
                await delivery.deliver(commands.short_help_text(self.bot_name))
                return {"action": "skip", "reason": "help"}
            result = await self.process(make_event(source, text, interaction.id), allow_llm=False)
        except Exception:
            logger.exception("slash command %s failed", text)
            if not delivery.sent and not delivery.prompted:
                await delivery.deliver("That command failed, so nothing was changed by it. "
                                       "The failure is in the bot log.")
            return {"action": "skip", "reason": "slash command failed"}
        finally:
            delivery.closed = True
            _SLASH_ADAPTER.reset(token)
        if not delivery.sent and not delivery.prompted:
            await _SlashDelivery(self, interaction).deliver("Done.")
        elif not delivery.sent and delivery.prompted:
            await _SlashDelivery(self, interaction).deliver("Answer the prompt above to continue.")
        return result

    # ------------------------------------------------------------------ status

    async def check_listener_outages(self):
        """Tell the print channel when a required listener goes down or recovers (#1020).

        The listener is what normally posts every alert, so its death is the
        one failure it cannot report itself. Runs from the bot heartbeat, uses
        the bot's own send path, and never raises into the heartbeat.

        Escalations (#1020 B3/B4, C4): a printer whose supervisor restart
        budget is exhausted gets the louder critical notice (pinned where
        the adapter supports it), every failure notice carries the
        diagnostics bundle, and while a listener is down the bot also
        polls Moonraker directly for a degraded pause/error/stop notice.
        """
        try:
            from .. import supervisor_state
            from . import outage_alert

            statuses = await asyncio.to_thread(self._listener_statuses)
            if not statuses:
                return []
            state_dir = await asyncio.to_thread(settings.state_dir, self.cfg)
            records = await asyncio.to_thread(supervisor_state.read_all, self.cfg)
            exhausted = {pid for pid, rec in (records or {}).items() if rec.get("exhausted")}
            loop = asyncio.get_running_loop()
            delivered: dict[str, tuple] = {}

            def send(printer_id, text):
                # The printer's own channel, shared channels included
                # (#536): the exclusive channel map deliberately omits a
                # shared chat, so routing resolves the claim instead.
                chat = routing.chat_for_printer(self.cfg, self.platform_name, printer_id) or self.chat_id
                if not chat:
                    return False
                future = asyncio.run_coroutine_threadsafe(
                    self.adapter.send(chat_id=chat, content=text), loop)
                try:
                    result = future.result(timeout=30)
                except Exception:
                    logger.exception("could not post the listener outage notice for %s", printer_id)
                    return False
                # Adapters report a failed delivery as
                # SendResult(success=False), not an exception: an unchecked
                # result would persist the notice as delivered and the
                # promised retry would never happen.
                if result is not None and not getattr(result, "success", False):
                    logger.warning("listener outage notice for %s was not delivered: %s",
                                   printer_id, getattr(result, "error", None) or "send failed")
                    return False
                message_id = getattr(result, "message_id", None)
                if message_id:
                    delivered[printer_id] = (chat, message_id)
                return True

            def attach(kind):
                from .. import diagnostics_bundle

                return str(diagnostics_bundle.write_bundle(data=self.cfg))

            def pin(printer_id):
                pin_message = getattr(self.adapter, "pin_message", None)
                target = delivered.get(printer_id)
                if pin_message is None or not target:
                    return
                chat, message_id = target
                future = asyncio.run_coroutine_threadsafe(pin_message(chat, message_id), loop)
                future.result(timeout=30)

            messages = await asyncio.to_thread(
                lambda: outage_alert.tick(statuses, state_dir, send,
                                          exhausted=exhausted, attach=attach, pin=pin))
            await self._check_degraded(statuses, state_dir)
            return messages
        except Exception:
            logger.exception("listener outage check failed")
            return []

    async def check_firmware_updates(self):
        """Start any due ZMod update checks ([firmware] update_alerts).

        Runs from the bot heartbeat on every platform. It only schedules:
        each due printer is checked in a background task (see
        print_handy/bot/firmware_alert.py), so the heartbeat never waits on
        a printer or on GitHub, and it never raises into the heartbeat.
        """
        try:
            alerter = getattr(self, "firmware_alerts", None)
            if alerter is None:
                from .firmware_alert import FirmwareAlerter

                alerter = self.firmware_alerts = FirmwareAlerter(self)
            # ZMod update alerts, then release notices from the firmware
            # feed for every printer the feed covers (#1280 section I).
            return alerter.tick() + alerter.tick_feed()
        except Exception:
            logger.exception("firmware update alert check failed")
            return []

    async def _check_degraded(self, statuses, state_dir):
        """Degraded notices from a direct Moonraker poll (#1020 B4).

        While a required listener is down, ask the printer itself: a
        pause, error or stop it reports gets one notice through the bot,
        persisted so a state is announced once per outage. Best-effort:
        never raises into the outage check.
        """
        try:
            from . import outage_alert

            entries = {entry.get("id"): entry for entry in settings.printers(self.cfg)}
            raw = await asyncio.to_thread(outage_alert.load_degraded, state_dir)
            state = {k: v for k, v in raw.items() if isinstance(v, dict)}
            changed = False
            for printer_id, info in sorted((statuses or {}).items()):
                if info.get("ok") is True and printer_id in state:
                    # Remembered for a while after recovery, so a listener
                    # that keeps restarting does not re-post the notice.
                    changed = outage_alert.degraded_recovered(state, printer_id) or changed
                if not info.get("required") or info.get("ok") is not False:
                    continue
                changed = outage_alert.degraded_down(state, printer_id) or changed
                polled = await asyncio.to_thread(_poll_moonraker_state,
                                                 entries.get(printer_id) or {})
                if polled is None:
                    continue
                printer_state, filename = polled
                previous = state.get(printer_id)
                text = outage_alert.degraded_notice(state, printer_id, printer_state,
                                                    filename=filename)
                if text is None:
                    continue
                chat = (routing.chat_for_printer(self.cfg, self.platform_name, printer_id)
                        or self.chat_id)
                delivered_ok = False
                if chat:
                    try:
                        result = await self.adapter.send(chat_id=chat, content=text)
                        delivered_ok = result is None or bool(getattr(result, "success", False))
                    except Exception:
                        logger.exception("could not post the degraded notice for %s", printer_id)
                if delivered_ok:
                    changed = True
                elif previous is None:
                    state.pop(printer_id, None)
                else:
                    state[printer_id] = previous
            if changed:
                await asyncio.to_thread(outage_alert.save_degraded, state_dir, state)
        except Exception:
            logger.exception("degraded listener check failed")

    def _listener_statuses(self):
        """Per-printer listener health (#336), aggregated over the roster.

        Diagnostics only: a failure to compute it reads as an empty map,
        never as a status() failure — the heartbeat must keep beating.
        """
        try:
            from .. import healthcheck

            return healthcheck.listener_statuses(self.cfg)
        except Exception:
            return {}

    def status(self, connected):
        user = getattr(self.client, "user", None)
        return {"mode": "standalone", "pid": os.getpid(), "started_at": self.started_at,
                "platform": self.platform_name,
                "heartbeat_at": time.time(), "connected": bool(connected),
                "user": str(user) if user else None, "chat_id": self.chat_id,
                "channel_printers": dict(self.channel_printers),
                "shared_printers": {chat: list(ids) for chat, ids in self.shared_printers.items()},
                "listeners": self._listener_statuses(),
                "allowed_users": len(self.adapter.allowed),
                "access": self.adapter.access_description(),
                "handlers": len(self.router.dispatch_hooks),
                "llm": bool(self.use_llm and llm.configured(self.cfg)),
                "agent": bool(self.use_llm and agent.configured(self.cfg)),
                "open_prompts": prompts.open_count(),
                # #1524: local route counters and the opt-in unmatched
                # log's export — diagnostics only, never transmitted.
                "natural_language": self._natural_language_status()}
