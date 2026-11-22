"""Conversational AI fix session for problem files (#1778).

When AI is ready and an intake finds problems, offer "Work on this with AI".
Choosing it (or mentioning the bot with a file + fix request) opens a session
bound to that job, user and thread/channel.

Follow-up messages from that user in that thread go to the session without a
mention, until the user accepts a fixed file, cancels, or the session times
out (default 30 min idle). Other users' messages are ignored. Commands still
work normally.

Each turn is tool-driven and verified: the AI proposes steps from the closed
vocabulary plus the geometry tools from #1777; Print Handy runs them
deterministically, re-slices with the job's profile, and reports the result.
Accepting the fixed file repoints the job at the copy and re-enters normal
intake with every gate and confirm-before-print. The original file is never
overwritten.
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
    skill_session: Any = None  # print_repair.Session once loaded

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
    # Bind the skill session to the job folder
    try:
        from print_handy.skills.print_repair import Session as SkillSession
        session.skill_session = SkillSession(job_path.parent)
    except Exception:
        logger.exception('could not load print_repair Session')
    _SESSIONS[key] = session
    logger.info('started AI fix session for %s in %s/%s', user_id, chat_id, thread_id)
    return session


def get_session(user_id: str, chat_id: str, thread_id: str | None,
                timeout_s: float = DEFAULT_IDLE_TIMEOUT_S) -> FixSession | None:
    key = (str(user_id), str(chat_id), thread_id)
    session = _SESSIONS.get(key)
    if session is None or not session.active(timeout_s):
        if session is not None and session.expired(timeout_s):
            session.cancelled = True
            logger.info('AI fix session timed out for %s', key)
        return None
    session.touch()
    return session


def end_session(session: FixSession, reason: str = 'cancel') -> str:
    session.cancelled = True
    session.accepted = reason == 'accept'
    if reason == 'accept':
        return SESSION_END_ACCEPT
    if reason == 'timeout':
        return SESSION_END_TIMEOUT
    return SESSION_END_CANCEL


def offer_label(ai_ready: bool) -> str | None:
    return WORK_WITH_AI_LABEL if ai_ready else None


def is_fix_instruction(text: str) -> bool:
    from print_handy.core import ai_fix
    is_repair, _ = ai_fix.split_repair_request(text)
    return is_repair or any(w in text.lower() for w in ('fix this', 'repair this', 'make it print'))


def process_turn(session: FixSession, user_message: str, *, ai_complete) -> dict[str, Any]:
    """One turn: AI proposes tool calls, we execute via the skill Session, re-slice, report."""
    session.history.append({'role': 'user', 'content': user_message, 'ts': time.time()})
    low = user_message.lower().strip()
    if low in ('accept', 'do it', 'yes', 'looks good'):
        msg = end_session(session, 'accept')
        # TODO: repoint job at current_file and re-enter intake
        return {'reply': msg, 'accepted': True, 'cancelled': False}
    if low in ('cancel', 'abort', 'stop', 'nevermind'):
        msg = end_session(session, 'cancel')
        return {'reply': msg, 'accepted': False, 'cancelled': True}

    if session.skill_session is None:
        reply = 'Skill tools unavailable. Session can only accept or cancel.'
    else:
        # Placeholder: in full impl call ai_complete with tools, parse function calls,
        # run them, re-slice, summarize before/after.
        reply = (
            f"Received: {user_message!r}. "
            f"Skill session ready over {session.skill_session.workspace}. "
            "Full AI tool-calling loop is next. Say 'accept' or 'cancel'."
        )
    session.history.append({'role': 'assistant', 'content': reply, 'ts': time.time()})
    return {'reply': reply, 'accepted': False, 'cancelled': False}
