"""Safe, local per-job correlation IDs for log context (#1414)."""
from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_FIELD = "job_correlation_id"
_PATTERN = re.compile(r"[0-9a-f]{32,64}")


def ensure(job: dict) -> str:
    """Reuse a valid intake ID or mint an opaque random ID; never derive it from paths/secrets."""
    current = job.get(_FIELD)
    if isinstance(current, str) and _PATTERN.fullmatch(current):
        return current
    intake_id = job.get("intake_id")
    value = intake_id if isinstance(intake_id, str) and _PATTERN.fullmatch(intake_id) else uuid.uuid4().hex
    job[_FIELD] = value
    return value


def from_context(context: dict | None) -> str | None:
    """Return a valid ID from a local start context, if present."""
    value = (context or {}).get(_FIELD)
    return value if isinstance(value, str) and _PATTERN.fullmatch(value) else None


def persist(job_json: Path | str | None, value: str) -> None:
    """Record ``value`` in JOB.json without rewriting any other field.

    Reads the file as it is now and adds only ``job_correlation_id`` when it is
    absent, so a field another step wrote since the caller's read (the first
    plate's ``project_number`` claim) is never overwritten with a stale copy.
    Diagnostics only: a failure is logged and never reaches the print path.
    """
    if not job_json or not _PATTERN.fullmatch(value or ""):
        return
    from ..jobs.store import write_job
    path = Path(job_json)
    try:
        current = json.loads(path.read_text())
        if not isinstance(current, dict) or _PATTERN.fullmatch(str(current.get(_FIELD) or "")):
            return
        current[_FIELD] = value
        write_job(path.parent, current)
    except (OSError, ValueError):
        logger.warning("could not record the job correlation id in %s", path.name, exc_info=True)
