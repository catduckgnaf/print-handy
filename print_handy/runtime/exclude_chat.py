"""Chat control for cancelling one named object (#620).

The progress post lists the objects. Choosing one asks once. Only a
confirmed exclude builds the Moonraker script, and this module sends it
only when that script names the chosen object. The last remaining object
is not an exclude: the caller uses the existing stop confirm. A failed
send is reported and is not described as cancelled.

Both the ask and the send read the live printer first. Klipper's own
excluded list decides what is still printing, and a different file, an idle
printer, or a failed read means nothing is sent.
"""
from __future__ import annotations

import json
import urllib.request

from ..core.exclude_object import exclude_plan

PREFIX = "pbexclude"
_ACTIONS = ("pick", "go", "stop", "keep")


def read_live(printer_id=None) -> dict:
    """Live state, file, and Klipper's excluded objects. Raises when any of it cannot be read."""
    from . import print_stop
    pid = printer_id or None
    st = print_stop.status(pid)
    data = print_stop._send("/printer/objects/query?exclude_object", printer_id=pid)
    obj = (((data or {}).get("result") or {}).get("status") or {}).get("exclude_object")
    excluded = obj.get("excluded_objects") if isinstance(obj, dict) else None
    if not isinstance(excluded, list):
        raise RuntimeError("the printer did not report its excluded objects")
    return {"state": st.get("state"), "filename": st.get("filename") or "",
            "excluded": [str(name) for name in excluded]}


def still_printing(names, live) -> list:
    """The file's object names Klipper has not excluded. Klipper stores and
    reports object names uppercased, so the match ignores case."""
    gone = {str(name).upper() for name in (live or {}).get("excluded") or []}
    return [name for name in names if str(name).upper() not in gone]


def _changed(filename, live) -> str:
    """Why the live printer no longer matches the alert, or "" when it still does."""
    from .print_stop import ACTIVE
    if not isinstance(live, dict):
        return "Could not check the printer."
    if live.get("state") not in ACTIVE:
        return f"The printer is {live.get('state')}."
    if not filename or live.get("filename") != filename:
        return "A different print is running now."
    return ""


def surface_text(names) -> str:
    """The progress-post control line. Empty when the file named nothing."""
    names = [str(name) for name in names or [] if str(name)]
    if not names:
        return ""
    shown = ", ".join(names[:25])
    more = "" if len(names) <= 25 else f" and {len(names) - 25} more"
    return (f"Objects still printing: {shown}{more}.\n"
            f"To cancel one, use the menu or type `/exclude {names[0]}`. "
            "You will be asked before anything is sent. "
            "The last object uses the stop confirm instead of an exclude.")


def components(aid: str, names) -> list[dict]:
    """A select menu. Choosing a row only asks. It does not send."""
    options = []
    for index, name in enumerate(list(names or [])[:25]):
        label = f"Cancel {name}"[:100]
        options.append({"label": label, "value": str(index)})
    if not options:
        return []
    return [{"type": 1, "components": [{
        "type": 3,
        "custom_id": f"{PREFIX}:pick:{aid}",
        "placeholder": "Cancel one object",
        "min_values": 1,
        "max_values": 1,
        "options": options,
    }]}]


def parse_custom_id(custom_id: str):
    """Return (action, alert id) for an exclude control, else None."""
    parts = str(custom_id or "").split(":")
    if len(parts) == 3 and parts[0] == PREFIX and parts[1] in _ACTIONS and parts[2]:
        return parts[1], parts[2]
    return None


def prepare(alert, index, live=None) -> dict:
    """The ask step. Never sends, and never builds a script for the last object.

    ``live()`` returns :func:`read_live`'s dict. Without it, or when it
    raises, nothing can be verified and the answer is a refusal.
    """
    if not isinstance(alert, dict) or alert.get("kind") != "exclude-pick":
        return {"step": "stale", "text": "That object list is old or was already used. Nothing sent."}
    names = [str(name) for name in alert.get("names") or []]
    try:
        target = names[int(index)]
    except (TypeError, ValueError, IndexError):
        return {"step": "refuse", "text": "That object is not on this plate. Nothing sent."}
    try:
        now = live() if live else None
    except Exception:
        now = None
    why = _changed(alert.get("filename"), now)
    if why:
        return {"step": "refuse", "text": why + " Nothing sent."}
    left = still_printing(names, now)
    plan = exclude_plan(names, target, confirmed=False, remaining=left)
    base = {"filename": alert.get("filename") or "", "printer_id": alert.get("printer_id") or "",
            "name": target, "names": names}
    if plan["action"] == "cancel_print":
        return {**base, "step": "cancel_print",
                "text": plan["reason"] + " Stop the whole print? This is the same confirm as /print-stop."}
    if plan["action"] != "confirm" or plan.get("body"):
        return {"step": "refuse", "text": plan["reason"] + " Nothing sent."}
    return {**base, "step": "confirm", "text": plan["reason"]}


def confirm_send(alert, sender, live=None) -> dict:
    """Send one confirmed exclude. ``sender(script)`` raises when the call fails.

    ``live()`` is read just before the send: the same file must still be
    printing or paused, and the object must still be one of its remaining
    ones. No ``live``, or a failed read, sends nothing.

    A plan with no body, including the last object, is not sent. A raised
    sender is reported and is not described as cancelled.
    """
    if not isinstance(alert, dict) or alert.get("kind") != "exclude-go":
        return {"sent": False, "text": "That confirm is old or was already used. Nothing sent."}
    names = [str(name) for name in alert.get("names") or []]
    target = str(alert.get("name") or "")
    try:
        now = live() if live else None
    except Exception:
        now = None
    why = _changed(alert.get("filename"), now)
    if why:
        return {"sent": False, "text": why + " Nothing was sent, and nothing was excluded."}
    left = still_printing(names, now)
    plan = exclude_plan(names, target, confirmed=True, remaining=left)
    body = plan.get("body") or None
    script = ((body or {}).get("params") or {}).get("script")
    expected = f"EXCLUDE_OBJECT NAME={target}"
    if plan["action"] != "exclude" or script != expected:
        return {"sent": False, "text": plan["reason"] + " Nothing was excluded."}
    try:
        sender(script)
    except Exception as exc:
        return {"sent": False,
                "text": f"Exclude was not sent for {target}. It was not cancelled. {exc}"}
    still_on = [name for name in left if name != target]
    still = ", ".join(still_on) if still_on else "none"
    return {"sent": True, "script": script,
            "text": (f"Exclude sent for {target}. Still printing: {still}. "
                     "Check the printer screen.")}


def post_picker(token: str, channel_id: str, content: str, aid: str, names) -> bool:
    """Post the select menu. False means the caller should post plain text."""
    rows = components(aid, names)
    if not token or not channel_id or not rows:
        return False
    body = json.dumps({"content": content[:1990], "components": rows,
                       "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        data=body, method="POST",
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (hermes-3dprinting, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False
