"""Web UI toast notifications (#1610).

A small, general notification system for the Web UI: a notification is
a short card with a title, a body, one action link and a dismiss link,
rendered as a stack at the top of every page (the server inserts it
into shell pages as they are sent; see ``server._send``). Sources are
functions in :data:`SOURCES` that read the settings mapping and return
:class:`Notification` objects; a later feature adds a source, not a
second mechanism.

The first source is firmware update availability. "An update is
available" is only ever said from a live read of the printer's update
state, and the Web UI makes no printer reads of its own on a schedule —
so the observation is recorded when a read already happens: the
Firmware page's "Check installed versions" read, and the update flow's
own reads. The toast then carries that observation across pages until
the update is applied, a later read supersedes it, or the operator
dismisses it. A dismissal is keyed to the exact offered version, so a
newer release notifies again — the same one-alert-per-release rule the
chat alerts ([firmware] update_alerts) follow; those alerts gate this
source too, so one setting silences both surfaces.

State lives in ``webui_notifications.json`` under the state dir:
``{"observations": {printer_id: {...}}, "dismissed": {id: timestamp}}``.
Every write is best-effort and atomic (write beside, then replace); a
notification problem must never break a page or an update.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .. import settings
from . import shell

logger = logging.getLogger(__name__)

STATE_NAME = "webui_notifications.json"

_esc = shell._esc


@dataclass(frozen=True)
class Notification:
    """One toast: a stable id, a title, a body and one action."""

    id: str
    kind: str
    title: str
    body: str
    action_label: str
    action_url: str


def _state_path(data) -> Path:
    return settings.state_dir(data) / STATE_NAME


def _load(data) -> dict:
    try:
        raw = json.loads(_state_path(data).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"observations": {}, "dismissed": {}}
    if not isinstance(raw, dict):
        return {"observations": {}, "dismissed": {}}
    observations = raw.get("observations")
    dismissed = raw.get("dismissed")
    return {"observations": observations if isinstance(observations, dict) else {},
            "dismissed": dismissed if isinstance(dismissed, dict) else {}}


def _save(data, state) -> None:
    path = _state_path(data)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.warning("could not save Web UI notification state", exc_info=True)


def _update_id(printer_id, component_name, remote) -> str:
    return f"update:{printer_id}:{component_name}:{remote}"


def record_observation(data, printer_id, printer_name, status) -> None:
    """Record one live update-status read for a printer.

    ``status`` is a ``firmware_update.UpdateStatus``. The printer's
    observed updates are replaced wholesale: a component no longer
    offered (updated, or no longer reported) stops notifying.
    """
    updates = [{"component": component.name, "current": component.current,
                "remote": component.remote}
               for component in status.updates_available]
    state = _load(data)
    state["observations"][str(printer_id)] = {
        "name": str(printer_name or printer_id), "at": time.time(),
        "updates": updates}
    _save(data, state)


def clear_observed_update(data, printer_id, component_name) -> None:
    """Drop one component from a printer's observed updates.

    Called when an update for it finishes: the offer that was applied
    must stop notifying even before the next live read.
    """
    state = _load(data)
    observed = state["observations"].get(str(printer_id))
    if not isinstance(observed, dict):
        return
    kept = [row for row in observed.get("updates", [])
            if row.get("component") != component_name]
    if len(kept) == len(observed.get("updates", [])):
        return
    observed["updates"] = kept
    _save(data, state)


def dismiss(data, notification_id) -> None:
    """Remember a dismissal; the id names the exact offer dismissed."""
    state = _load(data)
    state["dismissed"][str(notification_id)] = time.time()
    _save(data, state)


def update_notifications(data) -> list:
    """The firmware-update source: one toast per observed offer."""
    if not (data.get("firmware") or {}).get("update_alerts", True):
        return []
    state = _load(data)
    dismissed = state["dismissed"]
    out = []
    for printer_id, observed in state["observations"].items():
        if not isinstance(observed, dict):
            continue
        printer_name = str(observed.get("name") or printer_id)
        for row in observed.get("updates", []):
            component = str(row.get("component") or "")
            remote = str(row.get("remote") or "")
            if not component or not remote:
                continue
            notif_id = _update_id(printer_id, component, remote)
            if notif_id in dismissed:
                continue
            current = str(row.get("current") or "version not reported")
            out.append(Notification(
                id=notif_id, kind="update", title="Update available",
                body=(f"{component} {remote} is available for {printer_name} "
                      f"(installed: {current})."),
                action_label="Review and update",
                action_url=f"/firmware?printer={quote(str(printer_id), safe='')}"))
    return out


SOURCES = (update_notifications,)


def current_notifications(data) -> list:
    """Every source's live notifications, in source order."""
    out = []
    for source in SOURCES:
        out.extend(source(data))
    return out


def render_toasts(notifications, *, back="/") -> str:
    """The toast stack HTML for a page, or "" when there is nothing to say.

    ``back`` is the path the dismiss links return to (the page being
    served); callers pass a validated local path.
    """
    if not notifications:
        return ""
    cards = []
    for notif in notifications:
        dismiss_url = (f"/notifications/dismiss?id={quote(notif.id, safe='')}"
                       f"&back={quote(back, safe='/')}")
        cards.append(
            '<div class="toast">'
            f'<div class="toast-body"><strong>{_esc(notif.title)}</strong><br>'
            f'{_esc(notif.body)}</div>'
            f'<a class="button accent" href="{_esc(notif.action_url)}">'
            f'{_esc(notif.action_label)}</a>'
            f'<a class="toast-dismiss" href="{_esc(dismiss_url)}">Dismiss</a>'
            "</div>")
    return '<div class="toasts">' + "".join(cards) + "</div>"


def toasts_for_page(data, *, back="/") -> str:
    """The current toast stack for one page render; never raises.

    Notifications are decoration on a page that must still load: any
    failure here is logged and the page goes out without toasts.
    """
    try:
        return render_toasts(current_notifications(data), back=back)
    except Exception:
        logger.warning("could not build Web UI notifications", exc_info=True)
        return ""


__all__ = ["Notification", "STATE_NAME", "clear_observed_update",
           "current_notifications", "dismiss", "record_observation",
           "render_toasts", "toasts_for_page", "update_notifications"]
