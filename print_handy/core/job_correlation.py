"""Safe, local per-job correlation IDs for log context (#1414)."""
from __future__ import annotations

import re
import uuid

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
