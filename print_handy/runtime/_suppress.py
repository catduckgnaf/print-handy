"""Rate-limit expected repeated failures within one runtime operation."""
from __future__ import annotations

import logging


class RepeatedFailure:
    """Log the first failure at WARNING and subsequent failures at DEBUG.

    Create one instance per polling/wait operation; no global suppression means
    a later operation always reports its own first failure.
    """

    def __init__(self, logger: logging.Logger, message: str):
        self.logger = logger
        self.message = message
        self.failed = False

    def __call__(self, exc: BaseException) -> None:
        if self.failed:
            self.logger.debug("%s: %r", self.message, exc, exc_info=True)
        else:
            self.failed = True
            self.logger.warning("%s: %r", self.message, exc, exc_info=True)


def repeated_failure(message: str, *, logger: logging.Logger | None = None) -> RepeatedFailure:
    """Build a per-operation first-warning/subsequent-debug logger."""
    return RepeatedFailure(logger or logging.getLogger(__name__), message)
