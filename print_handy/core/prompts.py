"""Button question registry: register, wait, resolve, cancel. No Hermes needed.

The intake asks a question in four steps: register an id, send the buttons, wait in a worker
thread, then resolve the id with CANCELLED to clean up. A button click (or a typed answer) resolves
the id from the event loop. This module is the same API as Hermes' ``tools.clarify_gateway``,
which owns the clicks when the Hermes gateway runs the plugin.

``gateway()`` picks the registry. The standalone bot calls ``activate()`` so its own buttons and
this registry always meet. Under Hermes the gateway's module is used, so Hermes' Discord adapter
can resolve what the plugin registered. Without Hermes installed, this module is the only choice.
"""
from __future__ import annotations

import sys
import threading
import time

CANCELLED = "__cancelled__"

_lock = threading.Lock()
_prompts: dict[str, "_Prompt"] = {}
_active = False


class _Prompt:
    __slots__ = ("session_key", "question", "choices", "event", "answer", "created", "approval")

    def __init__(self, session_key, question, choices, approval=False):
        self.session_key = str(session_key or "")
        self.question = str(question or "")
        self.choices = list(choices or [])
        self.event = threading.Event()
        self.answer = None
        self.created = time.monotonic()
        self.approval = bool(approval)


def register(clarify_id, session_key, question, choices, approval=False):
    """Open a prompt. Call before the buttons are sent: a fast click must find it.

    ``approval`` marks a print approval (P1-93): a prompt whose answer starts
    a print. Platform adapters resolve those only for the approval user when
    one is configured, however open the channel otherwise is.
    """
    with _lock:
        _prompts[str(clarify_id)] = _Prompt(session_key, question, choices, approval)
        if approval:
            _approval_marks[str(clarify_id)] = time.monotonic()


# Approval marks live outside the registry on purpose: a flow may register
# its prompt with the Hermes clarify gateway (whose register() takes no
# approval flag) while the standalone adapters still need to know the prompt
# is an approval. Marks are consulted by is_approval() whenever the prompt
# itself is not in this registry, and pruned a day after they are made —
# far beyond any prompt's wait.
_APPROVAL_MARK_TTL_S = 24 * 3600
_approval_marks: dict[str, float] = {}


def mark_approval(clarify_id):
    """Mark an already-registered prompt as a print approval (P1-93)."""
    with _lock:
        _approval_marks[str(clarify_id)] = time.monotonic()
        prompt = _prompts.get(str(clarify_id))
        if prompt is not None:
            prompt.approval = True


def forget_approval(clarify_id):
    """Drop a prompt's approval mark (it resolved, expired or was cancelled)."""
    with _lock:
        _approval_marks.pop(str(clarify_id), None)


def is_approval(clarify_id):
    """True while ``clarify_id`` names a print-approval prompt (P1-93)."""
    key = str(clarify_id)
    with _lock:
        prompt = _prompts.get(key)
        if prompt is not None and not prompt.event.is_set():
            return bool(prompt.approval)
        marked = _approval_marks.get(key)
        if marked is None:
            return False
        if time.monotonic() - marked > _APPROVAL_MARK_TTL_S:
            _approval_marks.pop(key, None)
            return False
        return True


def resolve_gateway_clarify(clarify_id, answer):
    """Answer an open prompt. True for the first answer, False when it is unknown or already answered."""
    with _lock:
        prompt = _prompts.get(str(clarify_id))
        if prompt is None or prompt.event.is_set():
            return False
        prompt.answer = answer
        prompt.event.set()
        _approval_marks.pop(str(clarify_id), None)
        return True


def wait_for_response(clarify_id, timeout=None):
    """Block until the prompt is answered. Returns the answer, or None on timeout or an unknown id.

    The prompt is closed either way, so a late click finds nothing to resolve.
    """
    with _lock:
        prompt = _prompts.get(str(clarify_id))
    if prompt is None:
        return None
    got = prompt.event.wait(timeout)
    with _lock:
        if _prompts.get(str(clarify_id)) is prompt:
            _prompts.pop(str(clarify_id), None)
        _approval_marks.pop(str(clarify_id), None)
    return prompt.answer if got else None


def is_open(clarify_id):
    """True while the prompt is registered and unanswered."""
    with _lock:
        prompt = _prompts.get(str(clarify_id))
        return prompt is not None and not prompt.event.is_set()


def choices(clarify_id):
    """The button labels a prompt was registered with, or None when it is not open."""
    with _lock:
        prompt = _prompts.get(str(clarify_id))
        return list(prompt.choices) if prompt is not None and not prompt.event.is_set() else None


def session_key(clarify_id):
    """The session key an open prompt was registered under, else None."""
    with _lock:
        prompt = _prompts.get(str(clarify_id))
        return prompt.session_key if prompt is not None and not prompt.event.is_set() else None


def open_prompt_for(session_key):
    """The newest open prompt registered under ``session_key``, else None.

    The registry-level twin of ``BasePlatformAdapter.open_prompt``: adapters
    that keep their own prompt rows scan those, but a caller holding only
    the registry (the plugin's typed-answer route, #524) asks here.
    Registration order decides "newest", as in the adapters.
    """
    with _lock:
        hits = [cid for cid, prompt in _prompts.items()
                if prompt.session_key == str(session_key) and not prompt.event.is_set()]
    return hits[-1] if hits else None


#: Separator between a live session key and the printer an intake in a
#: shared channel was decided for (#536). Prompts of a decided intake
#: register under ``<session key>:printer:<roster id>`` so two intakes in
#: one shared channel never share a typed-answer route.
PRINTER_KEY_SEP = ":printer:"


def printer_scoped_key(session_key, printer_id):
    """``session_key`` suffixed with the decided printer (#536)."""
    if not session_key or not printer_id:
        return session_key
    return f"{session_key}{PRINTER_KEY_SEP}{printer_id}"


def open_prompt_scoped(session_key):
    """The newest open prompt for a session, honouring printer-scoped keys (#536).

    Exact key first — every pre-#536 route behaves as before. Otherwise the
    prompts registered under ``<session_key>:printer:<id>`` (a shared
    channel's decided intakes): when exactly one printer has open prompts
    there, its newest; when two printers do, None — a typed reply must
    never be guessed across printers.
    """
    exact = open_prompt_for(session_key)
    if exact is not None:
        return exact
    prefix = f"{session_key}{PRINTER_KEY_SEP}"
    with _lock:
        hits = [(cid, prompt.session_key) for cid, prompt in _prompts.items()
                if prompt.session_key.startswith(prefix) and not prompt.event.is_set()]
    printers = {key[len(prefix):] for _cid, key in hits}
    if len(printers) != 1:
        return None
    return hits[-1][0] if hits else None


def cancel_session(session_key):
    """Cancel every open prompt of one session key. Returns how many were cancelled."""
    with _lock:
        hits = [p for p in _prompts.values() if p.session_key == str(session_key) and not p.event.is_set()]
        for prompt in hits:
            prompt.answer = CANCELLED
            prompt.event.set()
    return len(hits)


def open_count():
    with _lock:
        return sum(1 for p in _prompts.values() if not p.event.is_set())


def activate(on=True):
    """The standalone bot owns the buttons: always use this registry, even when Hermes is importable."""
    global _active
    _active = bool(on)


def active():
    return _active


def gateway():
    """The registry the current runtime's buttons resolve: this module, or Hermes' clarify gateway."""
    if _active:
        return sys.modules[__name__]
    try:
        from tools import clarify_gateway
    except ImportError:
        return sys.modules[__name__]
    return clarify_gateway
