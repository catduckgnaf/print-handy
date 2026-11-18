"""Put the roast of the printer's last print on its touchscreen.

HelixScreen's tips widget reads one Moonraker database item and shows it in
place of its tips, with ``details`` behind the tap. This module writes that
item: the exact roast line the print's completion, failure, cancel or
spaghetti post carried, never a fresh pick. It is best effort and
fire-and-forget: a printer without Moonraker, an unreachable one, or any error
leaves the chat post untouched.

The record shape is shared with HelixScreen (``include/featured_tip.h``):
namespace ``helix-screen``, key ``featured_tip``, value
``{"id", "title", "details", "source", "posted_at"}``. ``id`` is stable for one
print's post, so a retried delivery does not show twice.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any, Mapping

logger = logging.getLogger(__name__)

NAMESPACE = "helix-screen"
KEY = "featured_tip"
SOURCE = "Print Handy"
TIMEOUT_S = 5

# How each roast bank's print ended, for the line behind the tap.
_ENDINGS = {
    "completed": "finished",
    "failed": "stopped with an error",
    "cancelled": "was cancelled",
    "spaghetti": "turned into spaghetti",
}


def _moonraker_url(printer_id: str | None) -> str:
    from .. import settings
    if printer_id:
        try:
            return str(settings.by_id(str(printer_id)).get("moonraker_url") or "")
        except Exception:
            logger.debug("screen tip: unknown printer %r, using the default", printer_id)
    return str(settings.current()["services"].get("moonraker_url") or "")


def event_id(status: str, key: str | None, line: str) -> str:
    """A stable id for one print's roast: its post key, else the line itself."""
    basis = f"{status}:{key}" if key else f"{status}:{line}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def details(title: str, status: str, facts: Mapping[str, Any] | None = None) -> str:
    """What a tap on the roast opens: which print it was about and how it went."""
    facts = dict(facts or {})
    bits = []
    try:
        if facts.get("duration_s"):
            from ..core.roast import human_duration
            bits.append(human_duration(float(facts["duration_s"])))
    except (TypeError, ValueError):
        pass
    material = " ".join(str(facts[k]).strip() for k in ("color", "material")
                        if str(facts.get(k) or "").strip())
    if material:
        bits.append(material)
    if facts.get("grams"):
        try:
            bits.append(f"{round(float(facts['grams']))} g")
        except (TypeError, ValueError):
            pass
    ending = _ENDINGS.get(status, "finished")
    text = f"Your last print, {title}, {ending}."
    return f"{text} {', '.join(bits)}." if bits else text


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


def publish_roast(line: str, *, title: str, status: str = "completed", key: str | None = None,
                  facts: Mapping[str, Any] | None = None, printer_id: str | None = None) -> bool:
    """Queue a print's roast for that printer's screen. True when a write was queued.

    ``printer_id`` is the roster id of the printer the print ran on; None is
    the default printer.
    """
    try:
        if not line:
            return False
        url = _moonraker_url(printer_id)
        if not url:
            return False
        value = record(event_id=event_id(status, key, line), title=line,
                       details=details(title, status, facts))

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


__all__ = ("KEY", "NAMESPACE", "SOURCE", "details", "event_id", "publish_roast", "record")
