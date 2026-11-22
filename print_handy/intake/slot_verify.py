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

A slot can also be in doubt when both sides agree, because both can be
stale: the 2026-11-21 swap happened during a runout pause, the slot read
loaded before and after, the screen still named spool #21, and the runout
question closed as a refeed with nobody saying so. The listener marks such a
slot as an unconfirmed reload (``mark_unconfirmed``): a runout question that
closes without a chat answer, or an idle insertion prompt. Intake asks about
a marked slot once. The mark ends with any answer for that slot, and goes
stale on its own as soon as the slot is bound to a different spool.

Advisory, never a gate: the question is asked only about a slot the print
selected or uses, after the colors are chosen, and an unanswered or skipped
question, a failed relink or an unreadable screen lane, Spoolman or ledger
means a log line (and a warning in chat), never a held or cancelled intake.
Only the operator's explicit Cancel ends the intake. Nothing is rebound
without an answer.
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
SKIP_LABEL = "Skip, print as is"
ADVISORY_TIMEOUT_S = 60  # an ignored warning must not hold intake for the full question timeout
MAX_SHELF_CHOICES = 3


@dataclass
class SlotDoubt:
    """One slot whose screen lane and Print Handy record disagree."""
    slot: int
    # "spool" (different spool ids), "material" (same spool, different family)
    # or "reload" (both sides agree, but the slot was reloaded unconfirmed).
    reason: str
    record_spool_id: int | None
    record_label: str
    record_material: str
    screen_spool_id: int | None
    screen_material: str
    screen_label: str = ""
    fingerprint: str = ""
    reload_cause: str = ""  # for "reload": "runout" or "insertion"
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


def unconfirmed_mark(marks: dict[str, Any] | None, slot: int, record_spool_id: int | None) -> dict | None:
    """The slot's unconfirmed-reload mark, or None when there is none or it went stale.

    A mark names the spool the record held when it was set; once the slot is
    bound to a different spool, someone has said what is loaded, so the mark
    no longer counts.
    """
    mark = (marks or {}).get(str(int(slot)))
    if not isinstance(mark, dict):
        return None
    return mark if _int(mark.get("spool_id")) == record_spool_id else None


def slot_doubts(slots: dict[str, dict[str, Any]], lanes: dict[int, dict[str, Any]] | None,
                *, confirmed: dict[str, Any] | None = None,
                unconfirmed: dict[str, Any] | None = None) -> list[SlotDoubt]:
    """Slots whose screen lane disagrees with Print Handy's record, or that were reloaded unconfirmed.

    ``slots`` is ``configured_slots(state)``; ``lanes`` is
    ``lane_data_sync.parse_lane_data(...)`` (None when unknown). A lane in
    doubt with itself (``slot_issue``) or naming no material and no spool
    says nothing. A disagreement the operator already confirmed (same
    fingerprint in ``confirmed``) is not asked again. A slot with a live
    mark in ``unconfirmed`` is asked about even when the lane agrees or is
    unknown: both sides can be stale after a reload nobody confirmed.
    """
    from ..runtime.lane_data_sync import lane_matches_spool_material

    lanes = lanes or {}
    confirmed = confirmed or {}
    out: list[SlotDoubt] = []
    for key, record in sorted((slots or {}).items(), key=lambda item: int(item[0])):
        slot = int(key)
        if not isinstance(record, dict):
            continue
        lane = lanes.get(slot)
        usable = isinstance(lane, dict) and not lane.get("slot_issue")
        lane = lane if usable else {}
        screen_id = _int(lane.get("spool_id"))
        screen_material = str(lane.get("material") or "").strip()
        record_id = _int(record.get("spoolman_id"))
        record_material = str(record.get("material") or "").strip()
        print_ = fingerprint(slot, lane, record)
        mark = unconfirmed_mark(unconfirmed, slot, record_id)
        if screen_id is not None and record_id is not None and screen_id != record_id:
            reason = "spool"
        elif (screen_material and record_material
              and not lane_matches_spool_material(screen_material, record_material, None)):
            reason = "material"
        elif mark is not None:
            reason = "reload"
        else:
            continue
        if reason != "reload" and confirmed.get(str(slot)) == print_ and mark is None:
            continue
        out.append(SlotDoubt(slot=slot, reason=reason, record_spool_id=record_id,
                             record_label=_record_label(record), record_material=record_material,
                             screen_spool_id=screen_id, screen_material=screen_material,
                             screen_label=str(lane.get("name") or "").strip(), fingerprint=print_,
                             reload_cause=str((mark or {}).get("cause") or "")))
    return out


def add_choices(doubt: SlotDoubt, shelf: list[dict[str, Any]], spool_label: Callable[[int], str]) -> SlotDoubt:
    """Fill the answer buttons: the screen's spool, matching shelf spools, keep, cancel."""
    from ..runtime.lane_data_sync import lane_matches_spool_material

    choices: list[dict[str, Any]] = []
    if doubt.screen_spool_id is not None and doubt.screen_spool_id != doubt.record_spool_id:
        label = spool_label(doubt.screen_spool_id) or doubt.screen_label or "the spool the screen names"
        choices.append({"kind": "bind", "spool_id": doubt.screen_spool_id,
                        "label": f"#{doubt.screen_spool_id} {label} (screen)"})
    # A reload with no screen material to go on may have changed the material
    # too, so any shelf spool is a fair answer.
    any_material = doubt.reason == "reload" and not doubt.screen_material
    if doubt.screen_material or any_material:
        taken = {choice.get("spool_id") for choice in choices}
        taken.add(doubt.record_spool_id)
        for row in shelf or []:
            if len([c for c in choices if c["kind"] == "bind"]) >= MAX_SHELF_CHOICES:
                break
            spool_id = _int(row.get("spool_id"))
            if spool_id is None or spool_id in taken:
                continue
            if any_material or lane_matches_spool_material(doubt.screen_material,
                                                           str(row.get("material") or ""), None):
                name = str(row.get("name") or "").strip()
                material = str(row.get("material") or "").strip()
                text = name if material and name.casefold().endswith(material.casefold()) else f"{name} {material}"
                choices.append({"kind": "bind", "spool_id": spool_id, "label": f"#{spool_id} {text.strip()}"})
                taken.add(spool_id)
    keep = f"Keep #{doubt.record_spool_id} {doubt.record_label}" if doubt.record_spool_id else f"Keep {doubt.record_label}"
    choices.append({"kind": "keep", "label": keep})
    choices.append({"kind": "skip", "label": SKIP_LABEL})
    choices.append({"kind": "cancel", "label": CANCEL_LABEL})
    doubt.choices = choices
    return doubt


def question(doubt: SlotDoubt) -> str:
    """The plain question for one slot in doubt."""
    from ..core import filament_color

    where = filament_color.slot_display(doubt.slot)
    record = (f"spool #{doubt.record_spool_id} {doubt.record_label}" if doubt.record_spool_id
              else doubt.record_label)
    if doubt.reason == "reload":
        when = {"runout": "after a runout", "insertion": "while the printer was idle"}.get(
            doubt.reload_cause, "")
        reloaded = f"{where} was reloaded {when}".rstrip()
        return (f"Before slicing: {reloaded} and nobody confirmed which spool went in. Print Handy "
                f"still has {record} there. Which is loaded? Slicing goes on if you skip this.")
    if doubt.reason == "spool":
        screen = f"spool #{doubt.screen_spool_id}"
        if doubt.screen_material:
            screen += f" ({doubt.screen_material})"
    else:
        screen = doubt.screen_material or "a different filament"
    return (f"Before slicing: the printer screen says {where} holds {screen}, but Print Handy has "
            f"{record} there. Which is loaded? Slicing goes on if you skip this.")


# ------------------------------------------------------------------ ledger

def _path():
    from ..runtime import _config
    return _config.state_dir() / "slot-verify.json"


def _section(name: str, path=None) -> dict[str, Any]:
    try:
        data = json.loads((path or _path()).read_text())
    except (OSError, ValueError):
        return {}
    return data.get(name, {}) if isinstance(data, dict) and isinstance(data.get(name), dict) else {}


def _write(path, *, confirmed: dict[str, Any], unconfirmed: dict[str, Any]) -> None:
    target = path or _path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"confirmed": confirmed, "unconfirmed": unconfirmed,
                               "updated_at": time.time()}, indent=1) + "\n")
    os.replace(tmp, target)


def load_confirmed(path=None) -> dict[str, Any]:
    return _section("confirmed", path)


def load_unconfirmed(path=None) -> dict[str, Any]:
    return _section("unconfirmed", path)


def remember_keep(doubt: SlotDoubt, path=None) -> None:
    """Remember that the operator kept the record for exactly this disagreement; ends any reload mark."""
    confirmed = load_confirmed(path)
    unconfirmed = load_unconfirmed(path)
    if doubt.reason != "reload":
        confirmed[str(doubt.slot)] = doubt.fingerprint
    unconfirmed.pop(str(doubt.slot), None)
    _write(path, confirmed=confirmed, unconfirmed=unconfirmed)


def mark_unconfirmed(slot, spool_id, cause: str, path=None) -> bool:
    """Mark a slot as reloaded with nobody confirming which spool went in (listener side).

    ``spool_id`` is the spool the record holds for the slot now; ``cause`` is
    "runout" or "insertion". Never raises: a mark that cannot be written is
    a log line, and intake then simply does not ask.
    """
    try:
        key = str(int(slot))
        unconfirmed = load_unconfirmed(path)
        unconfirmed[key] = {"spool_id": _int(spool_id), "cause": str(cause), "at": time.time()}
        _write(path, confirmed=load_confirmed(path), unconfirmed=unconfirmed)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("slot %r reload mark not written (%r)", slot, exc)
        return False
    logger.info("slot %s marked as reloaded unconfirmed (%s), record spool #%s", key, cause, spool_id)
    return True


def clear_unconfirmed(slot, path=None) -> None:
    """End a slot's reload mark: someone said what is loaded. Never raises."""
    try:
        unconfirmed = load_unconfirmed(path)
        if unconfirmed.pop(str(int(slot)), None) is None:
            return
        _write(path, confirmed=load_confirmed(path), unconfirmed=unconfirmed)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("slot %r reload mark not cleared (%r)", slot, exc)


# ------------------------------------------------------------------ intake hook

def read_doubts(state: dict[str, Any], printer_id: str | None) -> list[SlotDoubt]:
    """The slots in doubt right now, with their answer buttons. Fail-soft: [] on any read failure."""
    from ..jobs.filament import configured_slots
    from ..runtime import lane_data_sync, spool_sync

    try:
        mr = spool_sync.moonraker_for(printer_id)
        lanes = lane_data_sync.parse_lane_data(spool_sync.read_lane_data(mr=mr))
    except Exception as exc:
        # Reload marks still count: they do not need the screen.
        logger.info("slot verification: screen lanes unreadable (%r); checking reload marks only", exc)
        lanes = None
    doubts = slot_doubts(configured_slots(state), lanes, confirmed=load_confirmed(),
                         unconfirmed=load_unconfirmed())
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
                       send: Callable[[str], Awaitable[Any]],
                       used_slots: set[int] | None = None,
                       rebound: list[int] | None = None) -> bool:
    """Advise about slots in doubt that the print uses. Returns False only when the operator cancels.

    ``ask(question, labels)`` returns the picked index; ``send(text)`` posts a
    line. ``used_slots`` limits the advice to the slots the print selected
    (None = every slot in doubt). An unanswered question (it raises, or
    returns nothing), Skip and a failed relink all warn and go on. A rebind
    reloads ``state`` in place and appends the slot to ``rebound`` so the
    caller can refresh the choices that named it.
    """
    import asyncio

    from ..core import printer_state
    from ..runtime import spool_sync

    doubts = await asyncio.to_thread(read_doubts, state, printer_id)
    if used_slots is not None:
        wanted = {int(slot) for slot in used_slots}
        for doubt in doubts:
            if doubt.slot not in wanted:
                logger.info("slot %s in doubt (%s) but this print does not use it; not asking", doubt.slot,
                            doubt.reason)
        doubts = [doubt for doubt in doubts if doubt.slot in wanted]
    for doubt in doubts:
        logger.info("slot %s in doubt (%s): screen spool=%s material=%r, record spool=%s material=%r",
                    doubt.slot, doubt.reason, doubt.screen_spool_id, doubt.screen_material,
                    doubt.record_spool_id, doubt.record_material)
        labels = [choice["label"] for choice in doubt.choices]
        try:
            pick = await ask(question(doubt), labels)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if type(exc).__name__ == "PromptCancelled":
                raise  # an explicit Cancel or Stop typed at the prompt still cancels the job
            logger.info("slot %s spool question not answered (%r); continuing", doubt.slot, exc)
            pick = None
        choice = doubt.choices[pick] if isinstance(pick, int) and 0 <= pick < len(doubt.choices) else None
        if choice is None or choice["kind"] == "skip":
            await send(f"Slot {doubt.slot} may not hold the spool Print Handy has ({doubt.record_label}). "
                       "Continuing without changing it. Use /spool change if the record is wrong.")
            continue
        if choice["kind"] == "cancel":
            await send("Cancelled. Nothing was sliced, uploaded or started.")
            return False
        if choice["kind"] == "keep":
            await asyncio.to_thread(remember_keep, doubt)
            again = ("" if doubt.reason == "reload"
                     else " I won't ask again unless the screen or the record changes.")
            await send(f"Keeping {choice['label'][len('Keep '):]} in slot {doubt.slot}.{again}")
            continue
        try:
            result = await asyncio.to_thread(
                spool_sync.bind_slot, doubt.slot, int(choice["spool_id"]),
                mr=spool_sync.moonraker_for(printer_id), printer_id=printer_id,
                prior_disposition="shelf", expected_prior_spool_id=doubt.record_spool_id,
                note="Confirmed at intake: the screen and the record disagreed (#1788)")
        except Exception as exc:
            # The relink is optional: the print goes on with the record as it was.
            logger.warning("slot %s rebind to #%s failed: %r", doubt.slot, choice["spool_id"], exc)
            await send(f"Could not link spool #{choice['spool_id']} to slot {doubt.slot} ({exc}). "
                       "Continuing with the recorded spool. Use /spool change if the record is wrong.")
            continue
        await asyncio.to_thread(clear_unconfirmed, doubt.slot)
        await send(spool_sync.bind_text(result))
        fresh = await asyncio.to_thread(printer_state.load, **({"printer_id": printer_id} if printer_id else {}))
        state.clear()
        state.update(fresh)
        if rebound is not None:
            rebound.append(doubt.slot)
    return True
