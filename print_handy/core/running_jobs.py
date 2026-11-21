"""Running intakes, by chat, for the job owner's plain text (#1789).

While a print job is being prepared, the owner's plain text in that
channel or thread belongs to the job, not to the generic fallback: a stop
word cancels it, a status word reports where it is, and anything else
gets a job-aware reply. Only the owner's text is captured, only while
the job is still before upload, and only in the job's channel or thread.

The registry is in-process only: the intake that registers a job runs
on the same event loop as the chat that typed the text, and the heavy
work it cancels runs in child processes owned by that loop, so no
cross-process lookup is needed. A job that ends (cancelled, failed,
started, or refused) unregisters in ``_prepare_link``'s ``finally``.
"""
from __future__ import annotations

import asyncio
import time

#: Phrases that stop the running job. Matched on the whole message, so a
#: longer sentence that happens to contain "cancel" still goes to the
#: job-aware reply instead of cancelling by accident.
_CANCEL_PHRASES = {
    "cancel", "stop", "never mind", "nevermind", "abort",
    "don't print it", "dont print it", "do not print it",
    "cancel this", "cancel it", "cancel the job", "cancel the print",
    "cancel that", "stop this", "stop it", "stop the job",
    "stop the print", "stop printing", "abort this", "cancel please",
    "please cancel", "please stop", "never mind then",
}
_CANCEL_PREFIXES = ("cancel the ", "cancel this ", "stop the ", "stop this ")
_CANCEL_PREFIX_TAILS = {"job", "print", "printing", "it", "that"}

#: Phrases that ask where the running job is.
_STATUS_PHRASES = {
    "status", "whats happening", "what's happening", "what is happening",
    "is it stuck", "how is it going", "how's it going",
    "what is it doing", "whats it doing", "what's it doing",
    "where is it up to", "is it still going", "are we stuck",
    "what's the status", "whats the status", "what is the status",
    "status update", "is it done", "is it done yet", "how far along is it",
}


def _normalize(text):
    """One line, case-folded, single spaces, no trailing punctuation."""
    folded = " ".join(str(text or "").strip().casefold().split())
    return folded.rstrip(".!?,").strip()


def classify_owner_text(text):
    """What the owner's plain text means for their running job.

    Returns ``"cancel"``, ``"status"``, ``"other"``, or None for empty
    text. "other" is any non-empty text that is neither a stop word nor
    a status word; it still belongs to the job, not to the fallback.
    """
    raw = " ".join(str(text or "").strip().casefold().split())
    if not raw:
        return None
    if set(raw) <= {"?"}:
        return "status"
    folded = _normalize(raw)
    if not folded:
        return None
    if folded in _CANCEL_PHRASES:
        return "cancel"
    for prefix in _CANCEL_PREFIXES:
        if folded.startswith(prefix) and folded[len(prefix):] in _CANCEL_PREFIX_TAILS:
            return "cancel"
    if folded in _STATUS_PHRASES:
        return "status"
    return "other"


def format_elapsed(seconds):
    """Elapsed time as chat reads it: "45 s", "2 min 5 s", "1 h 3 min"."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} min {secs} s" if secs else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


def platform_name(source):
    """The platform name for a chat source, as the intake registers it."""
    platform = getattr(source, "platform", None)
    if isinstance(platform, str) and platform:
        return platform
    value = getattr(platform, "value", None)
    if isinstance(value, str) and value:
        return value
    return "discord"


class RunningJob:
    """One intake preparing a job, while its owner's text may reach it."""

    def __init__(self, *, platform, chat_id, thread_id, owner_user_id, intake_id,
                 job=None, label=None):
        self.platform = str(platform)
        self.chat_id = str(chat_id)
        self.thread_id = str(thread_id or "") or None
        self.owner_user_id = str(owner_user_id or "")
        self.intake_id = intake_id
        self.job = job
        self._label = label
        self.step = "preparing your print"
        self.step_started_at = time.monotonic()
        self.started_at = self.step_started_at
        self.cancel_event = asyncio.Event()
        #: False once upload starts: the start gates and the printer's own
        #: cancel apply from there, as today (#1789 scope).
        self.cancellable = True

    @property
    def label(self):
        """The job's name in chat: its title, or "print" when unknown."""
        job = self.job or {}
        title = str(job.get("title") or "").strip()
        if title:
            return title
        if self._label:
            return self._label
        return "print"

    def attach_job(self, job):
        """Bind the intake's job dict once it exists, for its title."""
        self.job = job

    def set_step(self, text):
        """Name the step now running, from a progress message's text."""
        cleaned = str(text or "")
        for suffix in (" Press Stop to cancel before it starts.", " Press Stop to cancel."):
            if cleaned.endswith(suffix):
                cleaned = cleaned[:-len(suffix)]
                break
        cleaned = cleaned.strip().rstrip(".").strip()
        if not cleaned:
            return
        self.step = cleaned[0].lower() + cleaned[1:]
        self.step_started_at = time.monotonic()

    def step_elapsed_s(self):
        """Seconds since the current step started."""
        return time.monotonic() - self.step_started_at

    def request_cancel(self):
        """Ask the job to stop at its next safe point. True when new."""
        was_set = self.cancel_event.is_set()
        self.cancel_event.set()
        return not was_set

    def mark_past_cancel_point(self):
        """Upload started: typed text no longer cancels this job."""
        self.cancellable = False


_JOBS: dict[tuple[str, str, str], list[RunningJob]] = {}


def _key(platform, chat_id, thread_id):
    return (str(platform), str(chat_id), str(thread_id or ""))


def register(job):
    """Track a running job; the newest job in a channel answers first."""
    _JOBS.setdefault(_key(job.platform, job.chat_id, job.thread_id), []).append(job)
    return job


def unregister(job):
    """Drop a finished job; never raises for an unknown job."""
    jobs = _JOBS.get(_key(job.platform, job.chat_id, job.thread_id))
    if not jobs:
        return
    try:
        jobs.remove(job)
    except ValueError:
        return
    if not jobs:
        del _JOBS[_key(job.platform, job.chat_id, job.thread_id)]


def lookup(platform, chat_id, thread_id):
    """The newest running job in a channel or thread, else None."""
    jobs = _JOBS.get(_key(platform, chat_id, thread_id))
    return jobs[-1] if jobs else None


def lookup_by_intake(intake_id):
    """The running job for one intake id, else None.

    Steps look their job up this way, never by channel: two intakes in
    one channel are separate jobs, and cancelling the newest must not
    stop the older one at its next step.
    """
    for jobs in _JOBS.values():
        for job in jobs:
            if job.intake_id == intake_id:
                return job
    return None


def lookup_all(platform, chat_id, thread_id):
    """Every running job in a channel or thread, oldest first."""
    return list(_JOBS.get(_key(platform, chat_id, thread_id)) or [])


def lookup_for_source(source):
    """The newest running job for a chat source, else None."""
    return lookup(platform_name(source), getattr(source, "chat_id", None),
                  getattr(source, "thread_id", None))


def clear():
    """Drop every tracked job (tests only)."""
    _JOBS.clear()


def cancel_ack(job):
    """The immediate acknowledgement of a typed cancel (#1789)."""
    return f"Cancelling the {job.label} job. Nothing was uploaded or started."


def already_cancelling_reply(job):
    """A second cancel while the first is still stopping the job."""
    return f"Already cancelling the {job.label} job. Nothing was uploaded or started."


def status_reply(job, count=1):
    """The current step and how long it has run (#1789)."""
    text = f"Still {job.step} ({format_elapsed(job.step_elapsed_s())} so far)."
    if count > 1:
        text += f" This is the newest of {count} jobs running here."
    return text


def job_aware_reply(job, count=1):
    """Any other owner text during a running job, never the generic help."""
    text = (f"Your {job.label} job is still preparing "
            f"({job.step}, {format_elapsed(job.step_elapsed_s())}). "
            "Say *cancel* to stop it, or wait for its next question.")
    if count > 1:
        text += f" This is the newest of {count} jobs running here."
    return text


async def handle_owner_text(adapter, source, text, metadata=None):
    """Route the owner's plain text to their running job, if one is live.

    Returns ``"cancel"``, ``"status"`` or ``"other"`` when the text was
    answered here, else None: no running job, a job past its cancel
    point, another user's text, or empty text all fall through to the
    caller's fallback exactly as today.
    """
    job = lookup_for_source(source)
    if job is None or not job.cancellable:
        return None
    owner = str(getattr(source, "user_id", "") or "")
    if not owner or owner != job.owner_user_id:
        return None
    kind = classify_owner_text(text)
    if kind is None:
        return None
    count = len(lookup_all(job.platform, job.chat_id, job.thread_id))
    if kind == "cancel":
        first = job.request_cancel()
        content = cancel_ack(job) if first else already_cancelling_reply(job)
    elif kind == "status":
        content = status_reply(job, count)
    else:
        content = job_aware_reply(job, count)
    await adapter.send(chat_id=str(source.chat_id), content=content,
                       metadata=metadata if metadata is not None else None)
    return kind
