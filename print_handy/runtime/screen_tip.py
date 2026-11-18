"""Mirror the quip Print Handy just posted in chat onto the printer's touchscreen.

HelixScreen's tips widget reads one Moonraker database item and shows it in
place of its next tip, with ``details`` behind the tap. This module writes that
item: the exact line the chat message carried, never a fresh pick. It is best
effort and fire-and-forget: a printer without Moonraker, an unreachable one, or
any error leaves the chat message untouched.

The record shape is shared with HelixScreen (``include/featured_tip.h``):
namespace ``helix-screen``, key ``featured_tip``, value
``{"id", "title", "details", "source", "posted_at"}``. ``id`` is stable for one
chat event, so a replayed or re-rendered message does not show twice.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import logging
import threading
import time
from typing import Any, Iterator

logger = logging.getLogger(__name__)

NAMESPACE = "helix-screen"
KEY = "featured_tip"
SOURCE = "Print Handy"
TIMEOUT_S = 5

# The printer the current intake is routed to; unset means the default printer.
_PRINTER: ContextVar[str | None] = ContextVar("screen_tip_printer", default=None)


@contextmanager
def routed_to(printer_id: str | None) -> Iterator[None]:
    """Send quips shared inside this block to ``printer_id``'s screen."""
    token = _PRINTER.set(str(printer_id) if printer_id else None)
    try:
        yield
    finally:
        _PRINTER.reset(token)


def _moonraker_url(printer_id: str | None) -> str:
    from .. import settings
    if printer_id:
        try:
            return str(settings.by_id(str(printer_id).split(":")[-1]).get("moonraker_url") or "")
        except Exception:
            logger.debug("screen tip: unknown printer %r, using the default", printer_id)
    return str(settings.current()["services"].get("moonraker_url") or "")


def record(*, event_id: str, title: str, details: str = "", now: float | None = None) -> dict[str, Any]:
    """The database value HelixScreen reads."""
    return {
        "id": event_id,
        "title": title,
        "details": details,
        "source": SOURCE,
        "posted_at": round(time.time() if now is None else now, 3),
    }


def _post(url: str, value: dict[str, Any]) -> None:
    from .. import moonraker
    moonraker.url_json(url.rstrip("/") + "/server/database/item", method="POST",
                       data={"namespace": NAMESPACE, "key": KEY, "value": value},
                       headers={"Content-Type": "application/json"},
                       timeout=TIMEOUT_S, moonraker=True)


def publish(*, event_id: str, title: str, details: str = "") -> bool:
    """Queue the shared quip for the routed printer's screen. True when a write was queued."""
    try:
        if not title:
            return False
        url = _moonraker_url(_PRINTER.get())
        if not url:
            return False
        value = record(event_id=event_id, title=title, details=details)

        def send() -> None:
            try:
                _post(url, value)
            except Exception as exc:
                logger.info("screen tip not published: %s", exc)

        threading.Thread(target=send, name="screen-tip", daemon=True).start()
        return True
    except Exception:
        logger.debug("screen tip unavailable", exc_info=True)
        return False


__all__ = ("KEY", "NAMESPACE", "SOURCE", "publish", "record", "routed_to")
