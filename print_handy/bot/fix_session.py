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

import json
import logging
import os
import re
import threading
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
BUSY_REPLY = ('A repair step is still running. Send your message again when it '
              'finishes. Nothing was changed.')

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
    # One turn or acceptance at a time: acceptance must not repoint the job while a
    # turn can still change current_file (#1778 review).
    lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)

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
    """Whether ``text`` is a model repair request that may start a session.

    Only the shared #978 repair reading counts, so the #985 guards hold: a
    question about a defect ("How do I fix this layer shift?") and a request
    about the printer itself ("fix this printer") are never repair requests.
    """
    from print_handy.core import ai_fix
    is_repair, _ = ai_fix.split_repair_request(text)
    return bool(is_repair)


# Only an explicit "accept" ends the session with the fixed file. "yes" and
# "do it" answer the model's own questions ("Shall I also sink it 1 mm?") and
# are ordinary turns (#1778 review).
_ACCEPT = frozenset(('accept',))
_CANCEL = frozenset(('cancel', 'abort', 'stop', 'nevermind'))


def _same_path(left, right) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return str(left) == str(right)


def _file_bytes(path) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _tool_schemas(session: FixSession) -> list:
    """OpenAI function schemas for the bound print-repair session (``TOOLS``)."""
    if session.skill_session is None:
        return []
    from print_handy.skills.print_repair.tools import openai_tools
    return openai_tools()


# Absolute paths (POSIX or Windows) in a tool or library error: only the last
# component is shown in chat, never the host's directory layout.
_ABS_PATH_RE = re.compile(r"(?:\b[A-Za-z]:[\\/]|(?<![\w.])/)(?:[^\s'\"\\/]+[\\/])+([^\s'\"\\/]+)")


def safe_text(text, limit: int = 1500) -> str:
    """Text that is safe to post: no mentions, links, newlines or absolute paths (#902)."""
    from print_handy.core import ai_fix
    return ai_fix.clean_summary(_ABS_PATH_RE.sub(r'\1', str(text or '')), limit=limit)


def _tool_failure(outcome) -> str | None:
    """The error string when a ``Session.run`` result is not a real success."""
    if not isinstance(outcome, dict) or not outcome.get('ok'):
        if isinstance(outcome, dict):
            return safe_text(outcome.get('error') or 'the tool failed', 300)
        return 'the tool failed'
    result = outcome.get('result')
    if isinstance(result, dict) and result.get('ok') is False:
        return safe_text(result.get('error') or 'the tool failed', 300)
    return None


def _remember(session: FixSession, reply: str, *, accepted: bool = False,
              cancelled: bool = False) -> dict[str, Any]:
    session.history.append({'role': 'assistant', 'content': reply, 'ts': time.time()})
    return {'reply': reply, 'accepted': accepted, 'cancelled': cancelled}


def _run_tool_calls(session: FixSession, calls) -> tuple[str | None, list[str]]:
    """Execute tool calls. Returns the last new mesh (workspace-relative) and errors.

    ``current_file`` moves only to a workspace file whose bytes differ from
    the file the session is already holding. The original is never overwritten.
    """
    changed = None
    errors = []
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get('name'), str) or not call['name']:
            errors.append('a tool call had no name')
            continue
        name = call['name']
        try:
            outcome = session.skill_session.run(name, call.get('arguments'))
        except Exception:
            logger.exception('repair tool %s failed', name)
            errors.append(f'{safe_text(name, 60)} failed.')
            continue
        failure = _tool_failure(outcome)
        if failure:
            errors.append(failure)
            continue
        result = outcome.get('result') if isinstance(outcome, dict) else None
        new_mesh = result.get('new_mesh') if isinstance(result, dict) else None
        if not new_mesh:
            continue
        try:
            path = session.skill_session.path(str(new_mesh))
        except Exception:
            logger.exception('repair tool %s named an unusable file', name)
            errors.append(f'{safe_text(name, 60)} named a file outside the repair workspace.')
            continue
        if _same_path(path, session.original_file):
            errors.append(f'{name} targeted the original file, which was left unchanged')
            continue
        new_bytes = _file_bytes(path)
        if new_bytes is None or new_bytes == _file_bytes(session.current_file):
            if not _same_path(path, session.current_file):
                path.unlink(missing_ok=True)
            errors.append(f'{name} did not change the mesh')
            continue
        session.current_file = path
        changed = str(new_mesh).replace(chr(92), "/")
    return changed, errors


def _atomic_json(path: Path, job: dict) -> None:
    """Write JSON via a temp file in the same directory, then ``os.replace``."""
    payload = json.dumps(job, indent=2) + '\n'
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp')
    try:
        tmp.write_text(payload, encoding='utf-8')
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _repoint_job(session: FixSession) -> None:
    """Point JOB.json's model file at the fixed copy. The original bytes stay put.

    The intake field is ``downloaded_file`` (see ``jobs.store.write_job`` and
    the intake that records the downloaded model). A matching row in
    ``selected_sources`` is updated the same way. The write is atomic.
    """
    from print_handy.jobs.store import write_job

    path = Path(session.job_path)
    job = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(job, dict):
        raise ValueError('the job record is not an object')
    new_file = Path(session.current_file).resolve()
    original = Path(session.original_file).resolve()
    if new_file == original or not new_file.is_file():
        raise ValueError('there is no fixed file to point the job at')
    job['downloaded_file'] = str(new_file)
    rows = job.get('selected_sources')
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict) or not row.get('downloaded_file'):
                continue
            try:
                same = Path(row['downloaded_file']).resolve() == original
            except OSError:
                same = False
            if same:
                row['downloaded_file'] = str(new_file)
    if path.name == 'JOB.json':
        write_job(path.parent, job)
    else:
        _atomic_json(path, job)
    written = json.loads(path.read_text(encoding='utf-8'))
    got = written.get('downloaded_file') if isinstance(written, dict) else None
    if not got or Path(got).resolve() != new_file:
        raise ValueError('the job file was not updated')


def _accept(session: FixSession, *, resume_intake=None) -> dict[str, Any]:
    """Accept only after the normal verified intake flow has completed."""
    if _same_path(session.current_file, session.original_file) or not Path(session.current_file).is_file():
        reply = 'There is no fixed file to accept and the original is unchanged.'
        return _remember(session, reply)
    if resume_intake is None:
        return _remember(session, 'The fixed file is ready, but normal intake has not run. '
                         'Use the chat acceptance flow to verify and re-slice it. '
                         'The job has not changed.')
    try:
        from print_handy.core.intake_resources import check_source
        check_source(session.current_file)
        if not resume_intake(session):
            return _remember(session, 'Normal intake did not complete; the fixed file was not accepted.')
        _repoint_job(session)
    except Exception:
        logger.exception('normal intake or the job update failed for an AI fix session')
        reply = 'The normal intake or job update failed. The fixed file was not accepted.'
        return _remember(session, reply)
    msg = end_session(session, 'accept')
    return _remember(session, msg, accepted=True, cancelled=False)


def _completion(completion) -> tuple[str, list]:
    """``ai_complete`` returns ``content`` and ``tool_calls`` of ``{name, arguments}``."""
    if not isinstance(completion, dict):
        raise ValueError('ai_complete did not return an object')
    calls = completion.get('tool_calls') or []
    if not isinstance(calls, list):
        raise ValueError('tool_calls must be a list')
    return str(completion.get('content') or ''), calls


def process_turn(session: FixSession, user_message: str, *, ai_complete,
                 resume_intake=None, locked: bool = False) -> dict[str, Any]:
    """Run one fix-session turn.

    Contract for the model call::

        ai_complete(session, user_message, tools) -> dict

    ``tools`` is the print-repair function schemas (``TOOLS`` / ``openai_tools``).
    The dict has ``content`` (str) and ``tool_calls`` (a list of
    ``{"name", "arguments"}``). Each call is ``session.skill_session.run``.
    A tool result that is not ok is reported as a repair that did not change
    the file. A result that is ok and carries ``new_mesh`` becomes
    ``current_file`` (resolved inside the skill workspace) and is named in
    the reply. If ``ai_complete`` raises, the reply says the repair step
    failed and the original file is unchanged.

    Accept (``accept`` only) repoints the job only when ``current_file`` is a
    different file and the job record was written. Otherwise the session stays
    open and nothing is called accepted.

    Turns and acceptance for one session run one at a time under ``session.lock``.
    A caller that already holds the lock (the chat acceptance, which keeps it across
    intake) passes ``locked=True``. Otherwise the lock is never waited on: this
    runs on a shared worker thread, so a turn that finds the lock held returns
    ``BUSY_REPLY`` (with ``busy`` true) at once and changes nothing.
    """
    if locked:
        return _process_turn_locked(session, user_message, ai_complete=ai_complete,
                                    resume_intake=resume_intake)
    if not session.lock.acquire(blocking=False):
        return {'reply': BUSY_REPLY, 'accepted': False, 'cancelled': False, 'busy': True}
    try:
        return _process_turn_locked(session, user_message, ai_complete=ai_complete,
                                    resume_intake=resume_intake)
    finally:
        session.lock.release()


def _process_turn_locked(session: FixSession, user_message: str, *, ai_complete,
                         resume_intake=None) -> dict[str, Any]:
    """The body of ``process_turn``; the caller holds ``session.lock``."""
    if session.accepted or session.cancelled:
        # A turn that waited on the lock while the session ended must not touch
        # current_file or the job.
        return {'reply': 'This repair session already ended; nothing was changed.',
                'accepted': False, 'cancelled': session.cancelled}
    session.history.append({'role': 'user', 'content': user_message, 'ts': time.time()})
    low = user_message.lower().strip()
    if low in _ACCEPT:
        return _accept(session, resume_intake=resume_intake)
    if low in _CANCEL:
        msg = end_session(session, 'cancel')
        return _remember(session, msg, accepted=False, cancelled=True)

    if session.skill_session is None:
        reply = 'Skill tools unavailable. Session can only accept or cancel.'
        return _remember(session, reply)

    try:
        schemas = _tool_schemas(session)
        completion = ai_complete(session, user_message, schemas)
        content, calls = _completion(completion)
    except Exception:
        logger.exception('AI fix step failed')
        reply = 'The repair step failed and the original file is unchanged.'
        return _remember(session, reply)

    content = safe_text(content)
    changed, errors = _run_tool_calls(session, calls)
    if errors and _same_path(session.current_file, session.original_file):
        reply = 'The repair did not change the file. ' + ' '.join(errors)
    elif errors and changed:
        reply = f'The repaired file is {changed}. ' + ' '.join(errors)
    elif changed:
        lead = f'{content.strip()} ' if content.strip() else ''
        reply = f'{lead}The repaired file is {changed}.'
    elif content.strip():
        reply = f'{content.strip()} The original file is unchanged.'
    else:
        reply = 'No repair was applied. The original file is unchanged.'
    return _remember(session, reply)
