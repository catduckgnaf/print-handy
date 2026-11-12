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

Escalations and degraded mode (#1020 B3/B4, C4): when the supervisor's
persisted record shows a printer's restart budget is exhausted, the outage
escalates once to a louder **critical** notice naming the exact
diagnostics-bundle commands (the runtime pins it where the platform
supports pinning, through :func:`tick`'s ``pin`` callback); every failure
notice carries the diagnostics bundle as an attachment when the runtime
supplies one (``attach``); and while a listener is down,
:func:`degraded_notice` decides the direct-poll notices for a printer
that itself reports a pause, error or stop.
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


def decide(statuses, state, now, *, down_after=DOWN_AFTER_S, remind_every=REMIND_EVERY_S,
           exhausted=None):
    """(messages, new_state) for one tick.

    ``statuses`` is ``healthcheck.listener_statuses`` output
    ``{printer_id: {"required": bool, "ok": bool|None, "reason": str}``.
    ``state`` is ``{printer_id: {"since": ts, "notified_at": ts|None}}`` for
    printers currently considered down. Each message is
    ``(printer_id, kind, text)`` with kind ``down``, ``still_down``,
    ``critical`` or ``up``.

    ``exhausted`` names the printers whose supervisor restart budget is
    spent (#1020 B3): their outage escalates once to the louder
    ``critical`` notice (state key ``critical_at``), which replaces the
    pending down or reminder notice for that tick.
    """
    exhausted_ids = set(exhausted or ())
    messages = []
    new_state = {}
    for printer_id, info in sorted((statuses or {}).items()):
        if not info.get("required"):
            continue
        entry = (state or {}).get(printer_id)
        if info.get("ok") is False:
            since = (entry or {}).get("since", now)
            notified = (entry or {}).get("notified_at")
            critical = (entry or {}).get("critical_at")
            spent = printer_id in exhausted_ids
            if spent and critical is None and now - since >= down_after:
                messages.append((printer_id, "critical",
                                 critical_text(printer_id, info.get("reason"), since, now)))
                critical = now
                notified = now
            elif notified is None and now - since >= down_after:
                messages.append((printer_id, "down", down_text(printer_id, info.get("reason"), since)))
                notified = now
            elif notified is not None and now - notified >= remind_every:
                text = still_down_text(printer_id, since, now)
                if spent:
                    text += (" Automatic restarts are exhausted — this will not fix itself. "
                             "Collect the bundle with `/print-handy bundle` and file an issue.")
                messages.append((printer_id, "still_down", text))
                notified = now
            kept = {"since": since, "notified_at": notified}
            if critical is not None:
                kept["critical_at"] = critical
            new_state[printer_id] = kept
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


def critical_text(printer_id, reason, since, now):
    """The budget-exhausted escalation (#1020 B3): louder than a down
    notice, and it names both exact bundle commands."""
    return (f"**CRITICAL: alerts for {printer_id} are DOWN, and the supervisor has exhausted its "
            f"restart budget — the listener will not come back on its own.** (Down for "
            f"{_span(now - since)}; last check: {reason or 'heartbeat stale'}.) Nothing for this "
            f"printer will be posted until it is fixed. Collect a diagnostics bundle and file an "
            f"issue with it: run `/print-handy bundle` in chat, or "
            f"`python3 -m print_handy.diagnostics_bundle` on the host.")


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


#: Notice kinds that carry the diagnostics bundle (#1020 C4).
ATTACH_KINDS = frozenset({"down", "still_down", "critical"})


def tick(statuses, state_dir, send, *, now=None, channel_for=None, exhausted=None,
         attach=None, pin=None):
    """Run one tick; ``send(printer_id, text)`` returns True when delivered.

    A message that fails to send is retried next tick (its state change is
    withheld), so a Discord blip never swallows the notice.

    ``exhausted`` (printer ids with a spent supervisor restart budget) is
    passed to :func:`decide`; ``attach(kind)`` may return a diagnostics
    bundle path, appended to failure notices as a ``MEDIA:`` marker
    (#1020 C4); ``pin(printer_id)`` is called after a critical notice is
    delivered, for platforms that support pinning (#1020 B3). Both
    callbacks are best-effort: their failures never block a notice.
    """
    now = time.time() if now is None else now
    state = {k: v for k, v in load(state_dir).items() if isinstance(v, dict)}
    messages, new_state = decide(statuses, state, now, exhausted=exhausted)
    bundle_path = False  # False = not asked yet, None = unavailable
    for printer_id, kind, text in messages:
        if kind in ATTACH_KINDS and attach is not None:
            if bundle_path is False:
                try:
                    bundle_path = attach(kind)
                except Exception:
                    bundle_path = None
            if bundle_path:
                text = f"{text}\nMEDIA:{bundle_path}"
        if send(printer_id, text):
            if kind == "critical" and pin is not None:
                try:
                    pin(printer_id)
                except Exception:
                    pass
            continue
        if kind == "up":
            new_state[printer_id] = state[printer_id]
        elif kind == "down":
            new_state[printer_id] = {"since": state.get(printer_id, {}).get("since", now), "notified_at": None}
        elif kind in ("still_down", "critical"):
            new_state[printer_id] = state[printer_id]
    if new_state != state:
        save(state_dir, new_state)
    return messages


#: Printer-reported states worth a degraded notice while the listener is
#: down (#1020 B4). "Stop" is Moonraker's cancelled state.
DEGRADED_STATES = frozenset({"paused", "error", "cancelled"})
DEGRADED_STATE_NAME = "listener_degraded_notices.json"
#: Seconds a printer's degraded notices stay remembered after its listener
#: recovers. A listener that keeps restarting is healthy for a minute or two
#: between failures; forgetting at once re-posted the same notice on every
#: restart (five times in one evening).
DEGRADED_RECOVERY_HOLD_S = 600


def degraded_recovered(state, printer_id, now=None, *, hold=DEGRADED_RECOVERY_HOLD_S):
    """Note that a printer's listener is healthy again; True when ``state`` changed.

    The printer's entry is kept for ``hold`` seconds after the first healthy
    check, so a listener that goes down again soon does not repeat the
    notice for the state it already reported. A later down clears the mark.
    """
    entry = (state or {}).get(printer_id)
    if not isinstance(entry, dict):
        return False
    now = time.time() if now is None else now
    since = entry.get("recovered_at")
    if not isinstance(since, (int, float)):
        entry["recovered_at"] = now
        return True
    if now - since >= hold:
        state.pop(printer_id, None)
        return True
    return False


def degraded_down(state, printer_id):
    """The listener is down again: a pending recovery mark no longer applies."""
    entry = (state or {}).get(printer_id)
    if isinstance(entry, dict) and "recovered_at" in entry:
        entry.pop("recovered_at", None)
        return True
    return False


def _job_label(filename):
    """The print's display name, never the raw remote path with its hash."""
    try:
        from ..runtime.discord_print_summary import safe_label

        return safe_label(filename, "")
    except Exception:
        return Path(str(filename or "")).name


def degraded_path(state_dir):
    return Path(state_dir) / DEGRADED_STATE_NAME


def load_degraded(state_dir):
    try:
        data = json.loads(degraded_path(state_dir).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_degraded(state_dir, state):
    target = degraded_path(state_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({"_doc": "Degraded (direct-poll) notices already sent while a listener was down; written by the bot.",
                               **state}, indent=1, sort_keys=True))
    os.replace(tmp, target)


def degraded_notice(state, printer_id, printer_state, *, filename="", now=None):
    """The degraded notice for one direct poll, or None (#1020 B4).

    ``state`` is the persisted degraded map; it is mutated only when a
    notice is due. A notice is due when the printer reports a pause,
    error or stop state that differs from the last one noticed for it —
    one notice per reported state per outage.
    """
    if printer_state not in DEGRADED_STATES:
        return None
    previous = (state or {}).get(printer_id) or {}
    if (previous.get("state"), previous.get("filename") or "") == (printer_state, filename):
        return None
    state[printer_id] = {"state": printer_state, "filename": filename,
                         "at": time.time() if now is None else now}
    label = _job_label(filename) if filename else ""
    where = f" on {label}" if label else ""
    return (f"**Degraded alert for {printer_id}:** its listener is down, so this notice comes "
            f"from a direct check of the printer, not the usual alert path. The printer reports "
            f"it is **{printer_state}**{where}. Times and details may be incomplete until the "
            f"listener recovers.")
