"""The alert system alerts on itself (#1020).

Every print alert comes from the per-printer listener. When a listener hangs,
nothing else tells the operator, because the bot stays connected and looks
alive. This module turns the listener health the bot already computes into
two Discord posts per outage: one when a required listener has been down past
a grace period, one when it recovers.

Pure decision logic (:func:`decide`) is separate from state (:func:`load`,
:func:`save`) and delivery (:func:`tick`), so the policy is testable without
Discord, a clock or a container. State is persisted so a bot restart neither
repeats a down notice nor loses a pending recovery notice.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

#: Seconds a required listener must stay unhealthy before the first notice.
DOWN_AFTER_S = 180
#: Seconds before a still-down listener is mentioned again (hourly reminder).
REMIND_EVERY_S = 3600
STATE_NAME = "listener_outage_alerts.json"


def decide(statuses, state, now, *, down_after=DOWN_AFTER_S, remind_every=REMIND_EVERY_S):
    """(messages, new_state) for one tick.

    ``statuses`` is ``healthcheck.listener_statuses`` output
    ``{printer_id: {"required": bool, "ok": bool|None, "reason": str}}``.
    ``state`` is ``{printer_id: {"since": ts, "notified_at": ts|None}}`` for
    printers currently considered down. Each message is
    ``(printer_id, kind, text)`` with kind ``down``, ``still_down`` or ``up``.
    """
    messages = []
    new_state = {}
    for printer_id, info in sorted((statuses or {}).items()):
        if not info.get("required"):
            continue
        entry = (state or {}).get(printer_id)
        if info.get("ok") is False:
            since = (entry or {}).get("since", now)
            notified = (entry or {}).get("notified_at")
            if notified is None and now - since >= down_after:
                messages.append((printer_id, "down", down_text(printer_id, info.get("reason"), since)))
                notified = now
            elif notified is not None and now - notified >= remind_every:
                messages.append((printer_id, "still_down", still_down_text(printer_id, since, now)))
                notified = now
            new_state[printer_id] = {"since": since, "notified_at": notified}
        elif info.get("ok") is True and entry is not None:
            if entry.get("notified_at") is not None:
                messages.append((printer_id, "up", up_text(printer_id, entry["since"], now)))
        # ok is None (unknown) neither raises nor clears an outage: keep the entry.
        elif entry is not None:
            new_state[printer_id] = entry
    return messages, new_state


def _clock(ts):
    return time.strftime("%-I:%M %p", time.localtime(ts)).lower().replace(" ", "")


def _span(seconds):
    minutes = max(1, int(seconds // 60))
    if minutes < 120:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def down_text(printer_id, reason, since):
    return (f"**Alerts for {printer_id} are down.** Its listener has not reported since about {_clock(since)} "
            f"({reason or 'heartbeat stale'}). Print starts, pauses, errors and completions will NOT be posted "
            f"until it recovers. The container supervisor is restarting it. Check the printer directly, and run "
            f"`/print-handy bundle` for a sanitized log bundle if this repeats.")


def still_down_text(printer_id, since, now):
    return (f"**Alerts for {printer_id} are still down** after {_span(now - since)}. "
            f"Automatic restarts have not recovered it. Run `/print-handy bundle` and file an issue.")


def up_text(printer_id, since, now):
    return f"Alerts for {printer_id} are back after {_span(now - since)}. Anything that happened in between was not posted."


def state_path(state_dir):
    return Path(state_dir) / STATE_NAME


def load(state_dir):
    try:
        data = json.loads(state_path(state_dir).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(state_dir, state):
    target = state_path(state_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({"_doc": "Listener outage notices already sent; written by the bot, one entry per printer currently down.",
                               **state}, indent=1, sort_keys=True))
    os.replace(tmp, target)


def tick(statuses, state_dir, send, *, now=None, channel_for=None):
    """Run one tick; ``send(printer_id, text)`` returns True when delivered.

    A message that fails to send is retried next tick (its state change is
    withheld), so a Discord blip never swallows the notice.
    """
    now = time.time() if now is None else now
    state = {k: v for k, v in load(state_dir).items() if isinstance(v, dict)}
    messages, new_state = decide(statuses, state, now)
    for printer_id, kind, text in messages:
        if send(printer_id, text):
            continue
        if kind == "up":
            new_state[printer_id] = state[printer_id]
        elif kind == "down":
            new_state[printer_id] = {"since": state.get(printer_id, {}).get("since", now), "notified_at": None}
        elif kind == "still_down":
            new_state[printer_id] = state[printer_id]
    if new_state != state:
        save(state_dir, new_state)
    return messages
