"""Button question registry: register, wait, resolve, cancel.

The intake asks a question in four steps: register an id, send the buttons, wait for the answer
(``await_answer``, on the event loop), then resolve the id with CANCELLED to clean up. A button
click (or a typed answer) resolves the id from the event loop. ``gateway()`` returns this module:
it is the one registry the bot's buttons resolve in (the API shape is kept from when a host
gateway could own the clicks).
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time

CANCELLED = "__cancelled__"

_lock = threading.Lock()
_prompts: dict[str, "_Prompt"] = {}


class _Prompt:
    __slots__ = ("session_key", "question", "choices", "event", "answer", "created", "approval",
                 "reserved")

    def __init__(self, session_key, question, choices, approval=False):
        self.session_key = str(session_key or "")
        self.question = str(question or "")
        self.choices = list(choices or [])
        self.event = threading.Event()
        self.answer = None
        self.created = time.monotonic()
        self.approval = bool(approval)
        self.reserved = None  # an answer recorded but not yet committed (P2-166)


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
# its prompt through a path that cannot carry the approval flag while the
# adapters still need to know the prompt is an approval. Marks are
# consulted by is_approval() whenever the prompt
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


def reserve_gateway_clarify(clarify_id, answer):
    """Record an answer without waking the waiter yet (P2-166).

    A platform that must finish network I/O before the flow may continue
    (Matrix posts its close-out note first, #416) reserves the answer the
    moment it arrives: the waiter's timeout then holds for the commit
    instead of discarding the prompt, and the reserver commits with
    :func:`resolve_gateway_clarify` — the same answer — once its I/O is
    done. First answer wins: a reservation refuses later answers exactly
    like a resolution does. True when this call reserved the prompt.
    """
    with _lock:
        prompt = _prompts.get(str(clarify_id))
        if prompt is None or prompt.event.is_set() or prompt.reserved is not None:
            return False
        prompt.reserved = answer
        return True


def resolve_gateway_clarify(clarify_id, answer):
    """Answer an open prompt. True for the first answer, False when it is unknown or already answered.

    A prompt with a reserved answer (P2-166) resolves only with that
    answer — the reserver committing its reservation; any other answer
    arrived second and loses."""
    with _lock:
        prompt = _prompts.get(str(clarify_id))
        if prompt is None or prompt.event.is_set():
            return False
        if prompt.reserved is not None and answer != prompt.reserved:
            return False
        prompt.answer = answer
        prompt.event.set()
        _approval_marks.pop(str(clarify_id), None)
        return True


# How long a wait may outlast its own timeout for a reserved prompt's
# commit (P2-166). The reserver's remaining I/O is one close-out note
# send — a single HTTP call with its own 30 s timeout — so this is
# generous headroom, never the normal path.
_RESERVE_GRACE_S = 120


def wait_for_response(clarify_id, timeout=None):
    """Block until the prompt is answered. Returns the answer, or None on timeout or an unknown id.

    The prompt is closed either way, so a late click finds nothing to resolve.
    A prompt whose answer is reserved (P2-166) is not discarded when the
    timeout passes: its reserver is mid-commit, so the wait extends once,
    by a bounded grace period, for the commit to land.
    """
    with _lock:
        prompt = _prompts.get(str(clarify_id))
    if prompt is None:
        return None
    deadline = None if timeout is None else time.monotonic() + timeout
    extended = False
    while True:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        got = prompt.event.wait(remaining)
        if got:
            break
        with _lock:
            reserved = prompt.reserved is not None and _prompts.get(str(clarify_id)) is prompt
        if not reserved or extended:
            break
        extended = True
        deadline = time.monotonic() + _RESERVE_GRACE_S
    with _lock:
        if _prompts.get(str(clarify_id)) is prompt:
            _prompts.pop(str(clarify_id), None)
        _approval_marks.pop(str(clarify_id), None)
    return prompt.answer if got else None


# The blocking wait as this module defines it. await_answer waits on the
# loop only for this registry with this wait: a stand-in registry, or a
# replaced blocking wait, keeps the thread wait it was written against.
_BLOCKING_WAIT = wait_for_response

#: How often an on-loop wait looks for its answer. A press resolves the
#: prompt from wherever it arrives (a button, a typed reply, a session
#: cancel), so checking on the loop costs no thread while it waits.
POLL_S = 0.1


async def await_answer(clarify_id, timeout=None, *, registry=None):
    """The answer to an open prompt, or None on timeout: ``wait_for_response`` without a thread.

    The same contract as ``wait_for_response`` (the prompt is closed either
    way, so a late press finds nothing), but the wait is an ``asyncio.sleep``
    loop on the running event loop. A question can stay open for half an
    hour; parking a worker thread on it that long took a thread from the
    small default pool of a 1-CPU container, and a few open questions
    stalled every other ``asyncio.to_thread`` call, print starts included.
    Only the timeout edge uses a thread: a prompt whose answer is reserved
    mid-commit (P2-166) gets the registry's bounded grace there, as before.
    Cancelling the wait closes the prompt row, as the parked thread did.
    """
    this = sys.modules[__name__]
    registry = this if registry is None else registry
    if registry is not this or registry.wait_for_response is not _BLOCKING_WAIT:
        return await asyncio.to_thread(registry.wait_for_response, clarify_id, timeout)
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + max(0.0, float(timeout))
    try:
        while is_open(clarify_id):
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return await asyncio.to_thread(wait_for_response, clarify_id, 0)
            await asyncio.sleep(POLL_S if remaining is None else min(POLL_S, remaining))
    except asyncio.CancelledError:
        _discard(clarify_id)
        raise
    return wait_for_response(clarify_id, 0)


def _discard(clarify_id):
    """Close a prompt whose waiter is gone: a later press finds nothing to resolve."""
    with _lock:
        prompt = _prompts.pop(str(clarify_id), None)
        if prompt is not None and not prompt.event.is_set():
            prompt.answer = CANCELLED
            prompt.event.set()
        _approval_marks.pop(str(clarify_id), None)


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


def gateway():
    """The registry the bot's buttons resolve: this module, the only one left."""
    return sys.modules[__name__]
