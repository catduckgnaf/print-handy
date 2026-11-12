"""Offer the next plate when the empty-bed check says the bed is clear (#621).

The offer does not start a print. On Discord it carries the existing
next-plate confirm button (``pbplate:go``), and that button runs
``print_next.start``. Dismiss (``pbplate:drop``) leaves the queue alone.
Telegram and Matrix have no buttons, so the offer names ``/print-next``,
which still asks before it starts. One offer per plate, and one "not clear"
note per plate. No camera verdict means no message.
"""
from __future__ import annotations

import json
import time

from . import _config

CHECK_S = 300


def confirm_components(aid: str, *, confirm_label: str, cancel_label: str) -> list[dict]:
    """The existing next-plate confirm, plus a dismiss that does not start."""
    return [{"type": 1, "components": [
        {"type": 2, "style": 3, "label": str(confirm_label)[:80],
         "custom_id": f"pbplate:go:{aid}"},
        {"type": 2, "style": 2, "label": str(cancel_label)[:80],
         "custom_id": f"pbplate:drop:{aid}"},
    ]}]


def _state_path():
    return _config.state_file("PRINT_HANDY_NEXT_PLATE_OFFER", "next_plate_offer.json")


def decide(label, verdict, *, said, plan_id) -> dict:
    """What to say for one observation. ``said`` is a set of actions already posted.

    ``verdict`` None means there is no camera result. That is not an empty bed.
    """
    if not label:
        return {"action": "none", "text": ""}
    if verdict is None:
        return {"action": "none", "text": ""}
    if verdict == "empty_idle":
        if "offer" in said:
            return {"action": "none", "text": ""}
        return {
            "action": "offer",
            "text": (f"Bed looks clear. Send {label}?\n"
                     f"Use `/print-next {plan_id}` to start it. Nothing starts until you confirm. "
                     "Ignore this to leave the queue alone."),
        }
    if "not_clear" in said:
        return {"action": "none", "text": ""}
    return {
        "action": "not_clear",
        "text": ("The bed does not look clear, so the next plate is not offered. "
                 f"Use `/print-next {plan_id}` when it is clear."),
    }


def _load() -> dict:
    try:
        data = json.loads(_state_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def remember(plan_id: str) -> None:
    """A multi-plate job finished a plate and still has another queued.

    Every finished plate starts a fresh look, so each plate gets its own offer.
    """
    _save({"plan_id": plan_id, "said": [], "checked_at": 0})


def clear() -> None:
    """Stop looking for the bed. Dismissing an offer ends the checks for this plate."""
    _save({})


def due(now=None) -> bool:
    data = _load()
    if not data.get("plan_id"):
        return False
    now = time.time() if now is None else now
    return now - float(data.get("checked_at") or 0) >= CHECK_S


def observe(verdict, now=None) -> dict:
    """Apply one camera verdict. Returns the offer choice. Starts nothing."""
    data = _load()
    plan_id = data.get("plan_id")
    now = time.time() if now is None else now
    empty = {"action": "none", "text": "", "plan_id": "", "part_id": ""}
    if not plan_id:
        return empty
    data["checked_at"] = now
    label = ""
    part_id = ""
    try:
        from . import print_next
        from .print_records import remaining_plate_choices
        plan = print_next.load_plan(plan_id)
        parts = remaining_plate_choices(plan)
        if parts:
            part_id = str(parts[0]["id"])
            label = print_next.plate_choice_label(parts[0])
        else:
            data["plan_id"] = ""
    except Exception:
        label = ""
    said = set(data.get("said") or [])
    choice = decide(label, verdict, said=said, plan_id=str(plan_id))
    choice = {**choice, "plan_id": str(plan_id), "part_id": part_id}
    if choice["action"] != "none":
        said.add(choice["action"])
        data["said"] = sorted(said)
    _save(data)
    return choice
