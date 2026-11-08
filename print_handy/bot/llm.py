"""Optional natural language for the standalone bot through any OpenAI-compatible chat endpoint.

The model only words a reply and may name one command from a fixed list. A named command runs
through the same handlers as a typed one, so every printer action still waits for its button.
Commands that change inventory or the printer screen without a button are never allowed here.
The model gets no tools, no files, no links and no printer access.
"""
from __future__ import annotations

import json
import re
import threading

from .. import ai_routes
from ..hermes import _config

# Conversation is intentionally short-lived and process-local.  It makes a
# normal back-and-forth useful without turning the chat lane into another
# records store (or leaking one channel's context into another).
_HISTORY: dict[str, list[dict[str, str]]] = {}
_HISTORY_LOCK = threading.Lock()
# One lock per session, serializing a whole ask (snapshot -> request ->
# append). _HISTORY_LOCK alone only makes each read and append atomic: two
# overlapping asks for the same session could snapshot before the other's
# turn landed, so the later ask was answered without that context and the
# turns were stored in completion order rather than ask order. Entries live
# and die with the session's history (see clear_history), so the lock store
# grows no faster than the history store it mirrors.
_HISTORY_LOCKS: dict[str, threading.Lock] = {}
MAX_HISTORY_MESSAGES = 8
MAX_HISTORY_CHARS = 1200

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


def system_prompt(help_text):
    return (f"You are {_config.bot_name()}, the Discord helper for a 3D printer. You cannot see the printer, "
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
    """Forget one chat session (or all sessions).  Primarily useful at logout/tests."""
    with _HISTORY_LOCK:
        if session is None:
            _HISTORY.clear()
            _HISTORY_LOCKS.clear()
        else:
            _HISTORY.pop(str(session), None)
            _HISTORY_LOCKS.pop(str(session), None)


def _session_lock(key):
    """The lock serializing one session's asks, created on first use."""
    with _HISTORY_LOCK:
        lock = _HISTORY_LOCKS.get(key)
        if lock is None:
            lock = _HISTORY_LOCKS[key] = threading.Lock()
        return lock


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
    with _HISTORY_LOCK:
        history = [dict(message) for message in _HISTORY.get(key, ())] if key else []
    answer, _served_by = ai_routes.call(
        "chat", [{"role": "system", "content": system_prompt(help_text)}, *history,
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
        with _HISTORY_LOCK:
            _HISTORY[key] = (_HISTORY.get(key, []) + turn)[-MAX_HISTORY_MESSAGES:]
    return reply, allowed_command(command)
