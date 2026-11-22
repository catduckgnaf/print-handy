"""Conversational AI fix session for problem files (#1778).

When AI is ready and an intake finds problems, offer "Work on this with AI".
Choosing it (or mentioning the bot with a file + fix request) opens a session
bound to that job, user and thread/channel.

Follow-up messages from that user in that thread go to the session without a
mention, until the user accepts a fixed file, cancels, or the session times
out (default 30 min idle). Other users' messages are ignored. Commands still
work normally.

Each turn is tool-driven and verified: the AI proposes steps from the closed
vocabulary (plus geometry tools from #1777 when available); Print Handy runs
them deterministically, re-slices with the job's profile, and reports the
result. Accepting the fixed file repoints the job at the copy and re-enters
normal intake with every gate and confirm-before-print. The original file is
never overwritten.

This is a draft implementation of the session routing and lifecycle. The full
verified tool loop reuses existing ai_fix vocabulary for now; geometry tools
from #1777 will be wired in next.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

WORK_WITH_AI_LABEL = 'Work on this with AI'
SESSION_START = "I'm listening in this thread until you accept or cancel."
SESSION_END_ACCEPT = 'Session ended: the fixed file was accepted. Re-entering normal intake.'
SESSION_END_CANCEL = 'Session ended: cancelled. The original file is unchanged.'
SESSION_END_TIMEOUT = 'Session ended: idle timeout. The original file is unchanged.'

DEFAULT_IDLE_TIMEOUT_S = 30 * 60

# Active sessions: key = (user_id, chat_id, thread_id or None)
_SESSIONS: dict[tuple, 'FixSession'] = {}


@dataclass
class FixSession:
    user_id: str
    chat_id: str
    thread_id: str | None
    job_path: Path
    original_file: Path
    current_file: Path
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    accepted: bool = False
    cancelled: bool = False
    history: list[dict] = field(default_factory=list)

    @property
    def key(self) -> tuple:
        return (str(self.user_id), str(self.chat_id), self.thread_id)

    def touch(self) -> None:
        self.last_activity = time.time()

    def expired(self, timeout_s: float = DEFAULT_IDLE_TIMEOUT_S) -> bool:
        return (time.time() - self.last_activity) > timeout_s

    def active(self, timeout_s: float = DEFAULT_IDLE_TIMEOUT_S) -> bool:
        return not self.accepted and not self.cancelled and not self.expired(timeout_s)


def start_session(user_id: str, chat_id: str, thread_id: str | None,
                  job_path: Path, original_file: Path) -> FixSession:
    """Open a new fix session. Ends any existing session for this key."""
    key = (str(user_id), str(chat_id), thread_id)
    if key in _SESSIONS:
        _SESSIONS[key].cancelled = True
    session = FixSession(
        user_id=str(user_id),
        chat_id=str(chat_id),
        thread_id=thread_id,
        job_path=job_path,
        original_file=original_file,
        current_file=original_file,
    )
    _SESSIONS[key] = session
    logger.info('started AI fix session for %s in %s/%s', user_id, chat_id, thread_id)
    return session


def get_session(user_id: str, chat_id: str, thread_id: str | None,
                timeout_s: float = DEFAULT_IDLE_TIMEOUT_S) -> FixSession | None:
    """Return the active session for this user+thread, or None."""
    key = (str(user_id), str(chat_id), thread_id)
    session = _SESSIONS.get(key)
    if session is None or not session.active(timeout_s):
        if session is not None and session.expired(timeout_s):
            session.cancelled = True  # mark ended
            logger.info('AI fix session timed out for %s', key)
        return None
    session.touch()
    return session


def end_session(session: FixSession, reason: str = 'cancel') -> str:
    """End the session and return the end message."""
    session.cancelled = True
    session.accepted = reason == 'accept'
    if reason == 'accept':
        return SESSION_END_ACCEPT
    if reason == 'timeout':
        return SESSION_END_TIMEOUT
    return SESSION_END_CANCEL


def offer_label(ai_ready: bool) -> str | None:
    """Return the offer label when AI is ready, else None."""
    return WORK_WITH_AI_LABEL if ai_ready else None


def is_fix_instruction(text: str) -> bool:
    """True if the message looks like a request to fix the attached file."""
    from print_handy.core import ai_fix
    is_repair, _ = ai_fix.split_repair_request(text)
    return is_repair or any(w in text.lower() for w in ('fix this', 'repair this', 'make it print'))


# ---------------------------------------------------------------------------
# Stub for the verified tool loop. Full implementation will call geometry
# tools from #1777, run them, re-slice, and report before/after.
# ---------------------------------------------------------------------------

def process_turn(session: FixSession, user_message: str, *, ai_complete) -> dict[str, Any]:
    """One turn of the session: propose, execute, verify, report.

    Draft: records the message and returns a placeholder. Real loop will:
    1. Call AI with skill tools (#1777) + closed vocabulary.
    2. Execute proposed tools deterministically on a copy.
    3. Re-slice with job profile.
    4. Report warnings, time, grams, preview.
    5. On 'accept' / 'do it', repoint job and re-enter intake.
    """
    session.history.append({'role': 'user', 'content': user_message, 'ts': time.time()})
    # Placeholder response until full tool loop is wired.
    reply = (
        f"(draft session) Received: {user_message!r}. "
        "Full verified tool loop (geometry tools from #1777 + re-slice) is next. "
        "Say 'accept' to finish or 'cancel' to abort."
    )
    session.history.append({'role': 'assistant', 'content': reply, 'ts': time.time()})
    return {'reply': reply, 'accepted': False, 'cancelled': False}
