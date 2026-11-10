"""Optional natural language for the standalone bot through any OpenAI-compatible chat endpoint.

The model only words a reply and may name one command from a fixed list. A named command runs
through the same handlers as a typed one, so every printer action still waits for its button.
Commands that change inventory or the printer screen without a button are never allowed here.
The model gets no tools, no files, no links and no printer access.
"""
from __future__ import annotations

import collections
import contextlib
import json
import re
import threading
import time

from .. import ai_routes
from ..hermes import _config

# Conversation is intentionally short-lived and process-local.  It makes a
# normal back-and-forth useful without turning the chat lane into another
# records store (or leaking one channel's context into another).
# session -> (messages, last used, monotonic), least recently used first.
_HISTORY: "collections.OrderedDict[str, tuple[list[dict[str, str]], float]]" = collections.OrderedDict()
_HISTORY_LOCK = threading.Lock()
# One lock per session, serializing a whole ask (snapshot -> request ->
# append). _HISTORY_LOCK alone only makes each read and append atomic: two
# overlapping asks for the same session could snapshot before the other's
# turn landed, so the later ask was answered without that context and the
# turns were stored in completion order rather than ask order. Each entry
# is [lock, users]: a lock someone holds or waits on is never dropped, so a
# session can never get a second lock and two concurrent turns.
_HISTORY_LOCKS: dict[str, list] = {}
MAX_HISTORY_MESSAGES = 8
MAX_HISTORY_CHARS = 1200
# Bounds on the process-local store: at most this many sessions remembered
# (least recently used forgotten first), each forgotten after this long idle.
MAX_SESSIONS = 256
HISTORY_TTL_S = 2 * 3600

# What the model may run. Each of these is read-only or asks for a button before acting.
ALLOWED = (
    re.compile(r"/print-help"),
    re.compile(r"/spool(?: list)?"),
    re.compile(r"/print-spools"),
    re.compile(r"/print-again list"),
    re.compile(r"/print-again(?! (?:stop|cancel|clear)\b)(?: [\w .#-]{1,60})?", re.I),
    re.compile(r"/print-next(?: [\w.-]{1,80}){0,2}"),
    re.compile(r"/print-(?:stop|resume|reboot|coldpull|bed-down)"),
    # The guided nozzle change asks for its own confirmations before heating (#513).
    re.compile(r"/nozzle(?: change)?"),
    re.compile(r"/print-maintenance(?: (?:full|quick))?"),
    re.compile(r"/print-lookup(?: [\w .#/-]{1,80})?"),
    re.compile(r"/print-source"),
    re.compile(r"/print-troubleshoot(?: (?:first-layer|clog|heat-creep|bed-level|ringing|prepare-to-clean))?"),
    re.compile(r"/print-macros"),
    # Firmware reporting and the manual guides are read-only; `update` asks
    # for its own confirmation but changes the printer, so it stays out (#561).
    re.compile(r"/print-firmware"),
    re.compile(r"/print-firmware guide (?:zmod|stock)"),
    # The calibration menu, records and Z-offset report are read-only; a
    # calibration run or an accept may never come from the model (#565).
    re.compile(r"/print-calibration"),
    re.compile(r"/print-calibration records"),
    re.compile(r"/print-calibration zoffset"),
    # Design asks for its own buttons before anything is sliced (#388 Stage 2);
    # /design is its short alias (#388 close-out).
    re.compile(r"/print-design(?: [\w .#/-]{1,160})?", re.I),
    re.compile(r"/design(?: [\w .#/-]{1,160})?", re.I),
)


def configured(cfg=None):
    """Whether the chat tier has an endpoint: [ai.chat], else the [llm] settings."""
    return ai_routes.configured("chat", cfg or _config.cfg())


def allowed_command(command):
    """The command when the model may run it, else None."""
    command = re.sub(r"\s+", " ", str(command or "").strip())
    if not command:
        return None
    name, _, rest = command.partition(" ")
    command = (name.casefold() + (" " + rest if rest else ""))
    return command if any(p.fullmatch(command) for p in ALLOWED) else None


_PLATFORM_LABELS = {"discord": "Discord", "telegram": "Telegram", "matrix": "Matrix",
                    "webui": "web chat"}


def system_prompt(help_text, platform=None):
    """The chat lane's system prompt, naming the platform being served.

    The lane serves every platform, so the prompt must not call itself
    "the Discord helper" on Telegram (#1146): a known platform is named,
    anything else stays neutral ("the chat helper").
    """
    label = _PLATFORM_LABELS.get(str(platform or "").strip().lower())
    who = f"the {label} helper" if label else "the chat helper"
    return (f"You are {_config.bot_name()}, {who} for a 3D printer. You cannot see the printer, "
            "start, stop or change anything yourself. Every printer action is a command that asks the "
            "operator to press a button first. Answer in at most two short sentences. If one of these "
            "commands does what the person asks, name it in \"command\"; otherwise use null. Never invent "
            "commands, G-code or settings.\n\n" + help_text +
            '\n\nReply with JSON only: {"reply": "<text>", "command": "<command or null>"}')


def _parse(content):
    text = str(content or "").strip()
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return str(data.get("reply") or "").strip(), data.get("command")
        except ValueError:
            pass
    return text, None


def clear_history(session=None):
    """Forget one chat session (or all sessions).  Primarily useful at logout/tests.

    A session lock that an ask holds or waits on stays: dropping it would let
    the next ask take a fresh lock and run beside the one in flight.
    """
    with _HISTORY_LOCK:
        keys = list(_HISTORY_LOCKS) if session is None else [str(session)]
        if session is None:
            _HISTORY.clear()
        else:
            _HISTORY.pop(str(session), None)
        for key in keys:
            entry = _HISTORY_LOCKS.get(key)
            if entry is not None and entry[1] == 0:
                del _HISTORY_LOCKS[key]


def _prune(now):
    """Drop expired and least-recently-used sessions (caller holds _HISTORY_LOCK)."""
    for key in [k for k, (_messages, used) in _HISTORY.items() if now - used > HISTORY_TTL_S]:
        del _HISTORY[key]
    while len(_HISTORY) > MAX_SESSIONS:
        _HISTORY.popitem(last=False)
    for key in [k for k, (_lock, users) in _HISTORY_LOCKS.items() if users == 0 and k not in _HISTORY]:
        del _HISTORY_LOCKS[key]


def _history(key):
    """A copy of one session's live history (expired history reads as none)."""
    now = time.monotonic()
    with _HISTORY_LOCK:
        _prune(now)
        entry = _HISTORY.get(key)
        return [dict(message) for message in entry[0]] if entry else []


@contextlib.contextmanager
def _session_lock(key):
    """Hold the lock serializing one session's asks, created on first use."""
    with _HISTORY_LOCK:
        entry = _HISTORY_LOCKS.get(key)
        if entry is None:
            entry = _HISTORY_LOCKS[key] = [threading.Lock(), 0]
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _HISTORY_LOCK:
            entry[1] -= 1


def ask(text, help_text, *, session=None, cfg=None, opener=None):
    """(reply, command or None). Raises on transport errors; the caller falls back to the help text.

    Goes through the chat tier (ai_routes), so [ai.chat] may add a fallback model.
    Asks for one session are serialized on that session's lock, so a follow-up
    always sees the previous turn and turns are stored in ask order; different
    sessions still run concurrently.
    """
    user_text = str(text)[:2000]
    key = str(session or "")
    if key:
        with _session_lock(key):
            return _ask_turn(user_text, help_text, key, cfg, opener)
    return _ask_turn(user_text, help_text, key, cfg, opener)


def _ask_turn(user_text, help_text, key, cfg, opener):
    history = _history(key) if key else []
    # Session keys are "platform:chat:thread:user" (bot.runtime.session_key);
    # the first segment names the platform the prompt should claim to serve.
    platform = key.split(":", 1)[0] if key else None
    answer, _served_by = ai_routes.call(
        "chat", [{"role": "system", "content": system_prompt(help_text, platform)}, *history,
                 {"role": "user", "content": user_text}],
        max_tokens=200, temperature=0.2, cfg=cfg or _config.cfg(), opener=opener)
    reply, command = _parse(answer)
    reply = reply[:1500]
    # Do not cache a failed/empty turn, and do not retain unbounded prompts or
    # replies.  The request snapshot above also means concurrent messages can
    # never mutate a payload already on its way to the provider.
    if key and reply:
        turn = [{"role": "user", "content": user_text[:MAX_HISTORY_CHARS]},
                {"role": "assistant", "content": reply[:MAX_HISTORY_CHARS]}]
        now = time.monotonic()
        with _HISTORY_LOCK:
            previous = _HISTORY.pop(key, None)
            messages = previous[0] if previous and now - previous[1] <= HISTORY_TTL_S else []
            _HISTORY[key] = ((messages + turn)[-MAX_HISTORY_MESSAGES:], now)  # most recent last
            _prune(now)
    return reply, allowed_command(command)
