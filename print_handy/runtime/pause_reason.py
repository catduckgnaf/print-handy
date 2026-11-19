"""Tell the printer why Print Handy paused it (prestonbrown/helixscreen#2130).

A pause sent through Moonraker's ``/printer/print/pause`` carries no reason, so
a touchscreen on the printer shows a bare "Paused" and the person standing at
it has no idea why. Right before Print Handy pauses a Klipper printer it sets
the printer's display message:

    M117 Paused by Print Handy: possible spaghetti

``display_status.message`` is standard Klipper status. HelixScreen reads it as
the pause cause when it has the fixed ``Paused by <source>: <reason>`` shape,
and the printer's own LCD, Mainsail and Fluidd show it as the printer message.

Screens that never show the display message (Guppy Screen and its forks) do
render Klipper ``action:prompt`` dialogs, so a printer can opt in to a prompt
with the same text: ``[printer] pause_prompt = true``. It stays off by
default, because a screen that already shows the cause would then show it
twice.

On resume the message is cleared only while it is still ours, so a macro's
own ``M117`` text is never wiped.

Everything here is best effort: a failed announcement or clean-up is logged
and never blocks, delays or retries the pause or resume it accompanies.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

SOURCE = "Print Handy"
PREFIX = f"Paused by {SOURCE}: "
PROMPT_TITLE = f"Paused by {SOURCE}"
PROMPT_SETTING = "pause_prompt"
# Fits HelixScreen's 128-byte display message buffer with the prefix.
MAX_REASON = 80

# Why a camera auto-pause fired, by the verdict that tripped it
# (visual_watch.AUTO_PAUSE_VERDICTS).
VERDICT_REASONS = {
    "spaghetti": "possible spaghetti",
    "detached": "the part may have come loose",
    "air": "possibly printing in mid-air",
}
CAMERA_REASON = "the camera saw a likely failed print"
CHAT_REASON = "paused from chat"
ALERT_REASON = "paused from chat after a camera alert"

GCODE_PATH = "/printer/gcode/script"
STATUS_PATH = "/printer/objects/query?display_status=message"

Send = Callable[..., Any]


def verdict_reason(verdict: str | None) -> str:
    """The reason for a camera auto-pause tripped by ``verdict``."""
    return VERDICT_REASONS.get(str(verdict or ""), CAMERA_REASON)


def _clean(reason: str) -> str:
    """One G-code-safe line: Klipper drops ``;`` and ``#`` as comments, and
    ``"`` would end a RESPOND MSG. Collapsed, trimmed and capped."""
    text = re.sub(r'[;#"\\]', ",", str(reason or ""))
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = " ".join(text.split())
    if len(text) > MAX_REASON:
        text = text[:MAX_REASON - 3].rstrip() + "..."
    return text


def message(reason: str) -> str:
    """The display message for ``reason``: ``Paused by Print Handy: <reason>``."""
    return PREFIX + (_clean(reason) or "paused")


def is_ours(display_message: Any) -> bool:
    """True when the printer's display message is one Print Handy set for a pause."""
    return isinstance(display_message, str) and display_message.startswith(PREFIX)


def prompt_enabled(printer_view: Any) -> bool:
    """``[printer] pause_prompt`` for this printer; off unless it is exactly true."""
    table = printer_view.get("printer") if isinstance(printer_view, dict) else None
    return isinstance(table, dict) and table.get(PROMPT_SETTING) is True


def _respond(msg: str) -> str:
    return f'RESPOND TYPE=command MSG="{msg}"'


def prompt_script(reason: str) -> str:
    """The ``action:prompt`` dialog for ``reason``, with one OK button that closes it."""
    return "\n".join([
        _respond(f"action:prompt_begin {PROMPT_TITLE}"),
        _respond(f"action:prompt_text {_clean(reason) or 'paused'}"),
        _respond("action:prompt_footer_button OK|RESPOND TYPE=command MSG=action:prompt_end"),
        _respond("action:prompt_show"),
    ])


def _script(send: Send, script: str, printer_id: str | None) -> None:
    kw = {"printer_id": printer_id} if printer_id else {}
    send(GCODE_PATH, method="POST", data={"script": script}, timeout=10, **kw)


def announce(send: Send, reason: str, *, prompt: bool = False,
             printer_id: str | None = None) -> bool:
    """Set the pause reason on the printer before a pause. True when it was sent.

    ``send`` is ``print_control._send``. Never raises: a printer that refuses
    the message still gets its pause.
    """
    script = f"M117 {message(reason)}"
    if prompt:
        script += "\n" + prompt_script(reason)
    try:
        _script(send, script, printer_id)
        return True
    except Exception:
        logger.warning("pause reason not sent to the printer; pausing anyway", exc_info=True)
        return False


def clear(send: Send, *, prompt: bool = False, printer_id: str | None = None) -> bool:
    """Clear the pause reason after a resume, only while it is still ours. True when cleared.

    Reads ``display_status.message`` first: a message a macro set since the
    pause is left alone. Never raises.
    """
    kw = {"printer_id": printer_id} if printer_id else {}
    try:
        data = send(STATUS_PATH, **kw)
        status = ((data or {}).get("result") or {}).get("status") or {}
        current = (status.get("display_status") or {}).get("message")
    except Exception:
        logger.debug("display message unreadable; pause reason left in place", exc_info=True)
        return False
    if not is_ours(current):
        return False
    script = "M117"
    if prompt:
        script += "\n" + _respond("action:prompt_end")
    try:
        _script(send, script, printer_id)
        return True
    except Exception:
        logger.warning("pause reason not cleared from the printer", exc_info=True)
        return False
