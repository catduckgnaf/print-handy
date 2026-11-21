"""Verify a slot before intake uses it, when the printer screen and Print Handy disagree (#1788).

The printer screen (HelixScreen, through Moonraker's ``lane_data``) and Print
Handy's own slot record can disagree about what a slot holds. On 2026-11-21 a
PETG spool went into slot 3 and the screen was set to PETG while Print
Handy's record still said spool #21, Black PLA+: the lane watcher posted a
notice and rebound nothing, so a print started in that window would have
been sliced as PLA+ with PETG in the nozzle.

Neither side is proof of what is loaded, so intake asks before it offers the
colors: one question per slot in doubt, with the spool the screen names, the
shelf spools of the screen's material, and "keep" for the record. The answer
binds through ``spool_sync.bind_slot`` (the path ``/spool change`` uses), so
Spoolman, the slot record and the screen converge. A "keep" answer is
remembered for that exact screen/record pair, so the same disagreement is not
asked on every print; any change on either side asks again.

Fail-soft by design: an unreadable screen lane, Spoolman or ledger means no
doubt and a log line, never a blocked intake.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

FILE_NAME = "slot-verify.json"  # keep in step with _path() (tests/test_state_files.py reads the literal)
CANCEL_LABEL = "Cancel"
MAX_SHELF_CHOICES = 3


@dataclass
class SlotDoubt:
    """One slot whose screen lane and Print Handy record disagree."""
    slot: int
    reason: str  # "spool" (different spool ids) or "material" (same spool, different family)
    record_spool_id: int | None
    record_label: str
    record_material: str
    screen_spool_id: int | None
    screen_material: str
    screen_label: str = ""
    fingerprint: str = ""
    choices: list[dict[str, Any]] = field(default_factory=list)


def _int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _record_label(record: dict[str, Any]) -> str:
    color = str(record.get("color") or record.get("name") or "").strip()
    material = str(record.get("material") or "").strip()
    if color and material and color.casefold().endswith(material.casefold()):
        return color
    return " ".join(part for part in (color, material) if part) or "an unknown spool"


def fingerprint(slot: int, lane: dict[str, Any], record: dict[str, Any]) -> str:
    """The exact disagreement: the screen's spool and material against the record's spool and material."""
    return json.dumps([int(slot), _int(lane.get("spool_id")), str(lane.get("material") or "").strip().casefold(),
                       _int(record.get("spoolman_id")), str(record.get("material") or "").strip().casefold()])


def slot_doubts(slots: dict[str, dict[str, Any]], lanes: dict[int, dict[str, Any]] | None,
                *, confirmed: dict[str, Any] | None = None) -> list[SlotDoubt]:
    """Slots whose screen lane disagrees with Print Handy's record, in slot order.

    ``slots`` is ``configured_slots(state)``; ``lanes`` is
    ``lane_data_sync.parse_lane_data(...)`` (None when unknown). A lane in
    doubt with itself (``slot_issue``) or naming no material and no spool
    says nothing. A disagreement the operator already confirmed (same
    fingerprint in ``confirmed``) is not asked again.
    """
    from ..runtime.lane_data_sync import lane_matches_spool_material

    if not lanes:
        return []
    confirmed = confirmed or {}
    out: list[SlotDoubt] = []
    for key, record in sorted((slots or {}).items(), key=lambda item: int(item[0])):
        slot = int(key)
        lane = lanes.get(slot)
        if not isinstance(lane, dict) or lane.get("slot_issue") or not isinstance(record, dict):
            continue
        screen_id = _int(lane.get("spool_id"))
        screen_material = str(lane.get("material") or "").strip()
        record_id = _int(record.get("spoolman_id"))
        record_material = str(record.get("material") or "").strip()
        if screen_id is not None and record_id is not None and screen_id != record_id:
            reason = "spool"
        elif (screen_material and record_material
              and not lane_matches_spool_material(screen_material, record_material, None)):
            reason = "material"
        else:
            continue
        print_ = fingerprint(slot, lane, record)
        if confirmed.get(str(slot)) == print_:
            continue
        out.append(SlotDoubt(slot=slot, reason=reason, record_spool_id=record_id,
                             record_label=_record_label(record), record_material=record_material,
                             screen_spool_id=screen_id, screen_material=screen_material,
                             screen_label=str(lane.get("name") or "").strip(), fingerprint=print_))
    return out


def add_choices(doubt: SlotDoubt, shelf: list[dict[str, Any]], spool_label: Callable[[int], str]) -> SlotDoubt:
    """Fill the answer buttons: the screen's spool, matching shelf spools, keep, cancel."""
    from ..runtime.lane_data_sync import lane_matches_spool_material

    choices: list[dict[str, Any]] = []
    if doubt.screen_spool_id is not None and doubt.screen_spool_id != doubt.record_spool_id:
        label = spool_label(doubt.screen_spool_id) or doubt.screen_label or "the spool the screen names"
        choices.append({"kind": "bind", "spool_id": doubt.screen_spool_id,
                        "label": f"#{doubt.screen_spool_id} {label} (screen)"})
    if doubt.screen_material:
        taken = {choice.get("spool_id") for choice in choices}
        for row in shelf or []:
            if len([c for c in choices if c["kind"] == "bind"]) >= MAX_SHELF_CHOICES:
                break
            spool_id = _int(row.get("spool_id"))
            if spool_id is None or spool_id in taken:
                continue
            if lane_matches_spool_material(doubt.screen_material, str(row.get("material") or ""), None):
                name = str(row.get("name") or "").strip()
                material = str(row.get("material") or "").strip()
                text = name if material and name.casefold().endswith(material.casefold()) else f"{name} {material}"
                choices.append({"kind": "bind", "spool_id": spool_id, "label": f"#{spool_id} {text.strip()}"})
                taken.add(spool_id)
    keep = f"Keep #{doubt.record_spool_id} {doubt.record_label}" if doubt.record_spool_id else f"Keep {doubt.record_label}"
    choices.append({"kind": "keep", "label": keep})
    choices.append({"kind": "cancel", "label": CANCEL_LABEL})
    doubt.choices = choices
    return doubt


def question(doubt: SlotDoubt) -> str:
    """The plain question for one slot in doubt."""
    from ..core import filament_color

    where = filament_color.slot_display(doubt.slot)
    if doubt.reason == "spool":
        screen = f"spool #{doubt.screen_spool_id}"
        if doubt.screen_material:
            screen += f" ({doubt.screen_material})"
    else:
        screen = doubt.screen_material or "a different filament"
    record = (f"spool #{doubt.record_spool_id} {doubt.record_label}" if doubt.record_spool_id
              else doubt.record_label)
    return (f"Before slicing: the printer screen says {where} holds {screen}, but Print Handy has "
            f"{record} there. Which is loaded? Nothing is sliced until you answer.")


# ------------------------------------------------------------------ ledger

def _path():
    from ..runtime import _config
    return _config.state_dir() / "slot-verify.json"


def load_confirmed(path=None) -> dict[str, Any]:
    try:
        data = json.loads((path or _path()).read_text())
    except (OSError, ValueError):
        return {}
    return data.get("confirmed", {}) if isinstance(data, dict) and isinstance(data.get("confirmed"), dict) else {}


def remember_keep(doubt: SlotDoubt, path=None) -> None:
    """Remember that the operator kept the record for exactly this disagreement."""
    target = path or _path()
    confirmed = load_confirmed(target)
    confirmed[str(doubt.slot)] = doubt.fingerprint
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"confirmed": confirmed, "updated_at": time.time()}, indent=1) + "\n")
    os.replace(tmp, target)


# ------------------------------------------------------------------ intake hook

def read_doubts(state: dict[str, Any], printer_id: str | None) -> list[SlotDoubt]:
    """The slots in doubt right now, with their answer buttons. Fail-soft: [] on any read failure."""
    from ..jobs.filament import configured_slots
    from ..runtime import lane_data_sync, spool_sync

    try:
        mr = spool_sync.moonraker_for(printer_id)
        lanes = lane_data_sync.parse_lane_data(spool_sync.read_lane_data(mr=mr))
    except Exception as exc:
        logger.info("slot verification skipped: screen lanes unreadable (%r)", exc)
        return []
    doubts = slot_doubts(configured_slots(state), lanes, confirmed=load_confirmed())
    if not doubts:
        return []
    try:
        shelf = spool_sync.shelf_spools(printer_id=printer_id)
    except Exception as exc:
        logger.info("slot verification: shelf unreadable (%r); offering the screen's spool and keep only", exc)
        shelf = []

    def spool_label(spool_id: int) -> str:
        try:
            spool = spool_sync.spoolman("GET", f"/api/v1/spool/{int(spool_id)}", None) or {}
        except Exception as exc:
            # The button falls back to the screen's own name for the spool.
            logger.info("slot verification: spool #%s label unreadable (%r)", spool_id, exc)
            return ""
        return lane_data_sync._spool_label(spool) if isinstance(spool, dict) else ""

    return [add_choices(doubt, shelf, spool_label) for doubt in doubts]


async def verify_slots(state: dict[str, Any], printer_id: str | None,
                       ask: Callable[[str, list[str]], Awaitable[Any]],
                       send: Callable[[str], Awaitable[Any]]) -> bool:
    """Ask about every slot in doubt before intake offers colors. Returns False when the operator cancels.

    ``ask(question, labels)`` returns the picked index; ``send(text)`` posts a
    line. A rebind reloads ``state`` in place, so the caller's colors and
    slicing see the confirmed spool.
    """
    import asyncio

    from ..core import printer_state
    from ..runtime import spool_sync

    doubts = await asyncio.to_thread(read_doubts, state, printer_id)
    for doubt in doubts:
        logger.info("slot %s in doubt (%s): screen spool=%s material=%r, record spool=%s material=%r",
                    doubt.slot, doubt.reason, doubt.screen_spool_id, doubt.screen_material,
                    doubt.record_spool_id, doubt.record_material)
        labels = [choice["label"] for choice in doubt.choices]
        pick = await ask(question(doubt), labels)
        choice = doubt.choices[pick] if isinstance(pick, int) and 0 <= pick < len(doubt.choices) else None
        if choice is None or choice["kind"] == "cancel":
            await send("Cancelled. Nothing was sliced, uploaded or started.")
            return False
        if choice["kind"] == "keep":
            await asyncio.to_thread(remember_keep, doubt)
            await send(f"Keeping {choice['label'][len('Keep '):]} in "
                       f"slot {doubt.slot}. I won't ask again unless the screen or the record changes.")
            continue
        try:
            result = await asyncio.to_thread(
                spool_sync.bind_slot, doubt.slot, int(choice["spool_id"]),
                mr=spool_sync.moonraker_for(printer_id), printer_id=printer_id,
                prior_disposition="shelf", expected_prior_spool_id=doubt.record_spool_id,
                note="Confirmed at intake: the screen and the record disagreed (#1788)")
        except Exception as exc:
            # Nothing is sliced from a slot whose binding just failed: stop and say so.
            logger.warning("slot %s rebind to #%s failed: %r", doubt.slot, choice["spool_id"], exc)
            await send(f"Could not link spool #{choice['spool_id']} to slot {doubt.slot} ({exc}). "
                       "Nothing was sliced. Fix the slot with /spool change, then post the model again.")
            return False
        await send(spool_sync.bind_text(result))
        fresh = await asyncio.to_thread(printer_state.load, **({"printer_id": printer_id} if printer_id else {}))
        state.clear()
        state.update(fresh)
    return True
