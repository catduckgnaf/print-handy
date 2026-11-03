"""Observe spool changes made at the HelixScreen panel (#791, step 1).

HelixScreen records which Spoolman spool sits in each IFS slot in Moonraker's
``lane_data`` database namespace (``lane1`` .. ``lane4``, ``spool_id`` is the
Spoolman id). This module polls that namespace on the listener's existing
cadence and reconciles a screen-side load or swap into Spoolman's ``ph_slot``
assignment through ``spool_sync.bind_slot``, announcing it once.

Rules, from the issue's hardening list:

* Unknown is not empty. An unreachable Moonraker or Spoolman, an absent
  namespace or a lane with no data changes nothing; a lane missing from
  ``lane_data`` is never read as "slot emptied".
* No silent guess. A lane is bound only when its Spoolman id resolves to a
  live spool whose material agrees with the lane and which is not locked to a
  different color. Anything else is a named conflict, announced once.
* Never rebind under a print. While printing, paused or unknown, a change is
  noted once and applied after the print.
* The first poll seeds silently (the #483 watcher's rule): existing drift is
  history, not news. Conflicts are still named on the first poll.
* Same per-printer control lock as reconcile and the other slot writers.

The ZMod/Helix specific reading lives in ``read_lanes`` alone; the rest works
on plain ``{slot: record}`` dicts.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from . import _config, spool_sync

OBSERVER_STATE = _config.state_file("LANE_OBSERVER_STATE", "lane_observer.json", per_instance=True)
LANE_NAMESPACE = "lane_data"
_BUSY_STATES = ("printing", "paused", "unknown")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def read_lanes(mr: spool_sync.Client = spool_sync.moonraker) -> dict[int, dict[str, Any]] | None:
    """``{slot: lane record}`` from Moonraker's ``lane_data``, or None when it cannot be read.

    None covers every "unknown" case: Moonraker down, the namespace absent,
    or an unexpected shape. Only ``laneN`` keys with N in 1-4 and a dict value
    are returned; the other keys HelixScreen keeps there (``seated``) are not lanes.
    """
    try:
        res = mr("GET", f"/server/database/item?namespace={LANE_NAMESPACE}", None)
        value = res["result"]["value"]
    except Exception:
        return None
    if not isinstance(value, dict):
        return None
    lanes: dict[int, dict[str, Any]] = {}
    for key, rec in value.items():
        match = re.fullmatch(r"lane(\d+)", str(key))
        if match and isinstance(rec, dict) and int(match.group(1)) in spool_sync.SLOTS:
            lanes[int(match.group(1))] = rec
    return lanes


def _spool_id(rec: dict[str, Any]) -> int | None:
    raw = rec.get("spool_id")
    if isinstance(raw, bool) or raw in (None, "", 0):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _material_key(value: Any) -> str:
    return re.sub(r"[\s+]", "", str(value or "")).casefold()


def _label(spool: dict[str, Any]) -> str:
    filament = spool.get("filament") or {}
    name = str(filament.get("name") or "").strip()
    return f"#{spool.get('id')} {name}".strip()


def _judge(slot: int, lane: dict[str, Any], by_id: dict[int, dict[str, Any]],
           pid: str) -> tuple[str, str]:
    """('ok', '') when the lane names a spool that is safe to bind, else (conflict key, text)."""
    sid = _spool_id(lane)
    spool = by_id.get(sid) if sid is not None else None
    if sid is None:
        return "unlinked", (f"Slot {slot}: the screen holds a spool with no Spoolman link "
                            f"({lane.get('material') or 'unknown material'}). Nothing was changed; "
                            f"assign the real spool with /spool.")
    if spool is None:
        return f"missing:{sid}", (f"Slot {slot}: the screen links spool #{sid}, which Spoolman no "
                                  f"longer has. Nothing was changed; relink the slot at the screen or with /spool.")
    if spool.get("archived"):
        return f"archived:{sid}", (f"Slot {slot}: the screen is linked to spool {_label(spool)}, which is "
                                   f"archived. Nothing was changed; relink the slot at the screen or with /spool.")
    spool_material = (spool.get("filament") or {}).get("material")
    lane_material = lane.get("material")
    if lane_material and spool_material and _material_key(lane_material) != _material_key(spool_material):
        return f"material:{sid}", (f"Slot {slot}: the screen says {lane_material} but Spoolman says spool "
                                   f"{_label(spool)} is {spool_material}. Nothing was changed; fix whichever is wrong.")
    if lane.get("helix_locked_color"):
        lane_hex = spool_sync.screen_hex(lane.get("color"))
        spool_hex = spool_sync.screen_hex((spool.get("filament") or {}).get("color_hex"))
        if lane_hex and spool_hex and lane_hex != spool_hex:
            return f"color:{sid}:{lane_hex}", (f"Slot {slot}: the screen color is locked to #{lane_hex} but Spoolman "
                                               f"has #{spool_hex} for spool {_label(spool)}. Nothing was changed; "
                                               f"clear the lock at the screen or fix the Spoolman color.")
    other = spool_sync.spool_ph_slot(spool)
    if other is not None and other[0] == pid and other[1] != slot:
        return f"elsewhere:{sid}", (f"Slot {slot}: the screen links spool {_label(spool)}, which Spoolman "
                                    f"assigns to slot {other[1]}. Nothing was changed; move one of them.")
    return "ok", ""


def observe(printer_id: str | None = None, *, client: spool_sync.Client = spool_sync.spoolman,
            mr: spool_sync.Client = spool_sync.moonraker, state_path: Path | None = None,
            observer_path: Path | None = None) -> dict[str, Any]:
    """One poll: fold screen-side spool changes into Spoolman, return what to announce.

    Returns ``{"ok", "printer_id", "events": [{"slot", "kind", "text"}], "error"}``.
    Never raises for a Spoolman, Moonraker or lock failure; on any of those
    nothing is written and ``ok`` is False with the reason in ``error``.
    Idempotent: a second poll over the same facts returns no events.
    """
    pid = printer_id or spool_sync._default_printer_id()
    result: dict[str, Any] = {"ok": False, "printer_id": pid, "events": [], "error": None}
    if not pid:
        result["error"] = "Lane observer: no printer could be derived from the configuration"
        return result
    lanes = read_lanes(mr)
    if lanes is None:
        result["error"] = "Lane observer: the screen's lane data could not be read, so nothing was changed"
        return result
    try:
        rows = spool_sync._call_client(client, "GET", "/api/v1/spool?allow_archived=true", None)
    except Exception:
        rows = None
    if not isinstance(rows, list):
        result["error"] = "Lane observer: Spoolman did not answer, so nothing was changed"
        return result
    by_id = {int(r["id"]): r for r in rows if isinstance(r, dict) and r.get("id") is not None}
    assigned: dict[int, int] = {}
    for row in by_id.values():
        parsed = spool_sync.spool_ph_slot(row)
        if parsed is not None and parsed[0] == pid and not row.get("archived"):
            assigned[parsed[1]] = int(row["id"])

    path = Path(observer_path or OBSERVER_STATE)
    state = _read_json(path)
    seeded = bool(state.get("seeded"))
    seen = {str(k): v for k, v in (state.get("seen") or {}).items() if isinstance(v, int)}
    announced = {str(k): str(v) for k, v in (state.get("announced") or {}).items()}

    lock = spool_sync._reconcile_lock(pid)
    if lock is not None:
        try:
            if not lock.acquire(blocking=False):
                result["error"] = "Lane observer: another printer control action is in flight; will retry"
                return result
        except Exception:
            lock = None
    try:
        busy = spool_sync.printer_state(mr) in _BUSY_STATES
        live_keys: dict[str, str] = {}
        for slot in sorted(lanes):
            lane = lanes[slot]
            sid = _spool_id(lane)
            skey = str(slot)
            if sid is not None and assigned.get(slot) == sid:
                seen[skey] = sid  # screen and Spoolman agree: nothing to do or say
                continue
            verdict, text = _judge(slot, lane, by_id, pid)
            if verdict != "ok":
                live_keys[skey] = verdict
                if announced.get(skey) != verdict:
                    result["events"].append({"slot": slot, "kind": "conflict", "text": text})
                continue
            changed = seeded and seen.get(skey) != sid
            if sid is None:
                continue  # _judge already returned a conflict for an unlinked lane
            if not seeded:
                # First poll: record what the screen holds without binding, but
                # name a disagreement with Spoolman once instead of hiding it.
                seen[skey] = sid
                key = f"drift:{sid}"
                live_keys[skey] = key
                prior = assigned.get(slot)
                before = f"spool #{prior}" if prior else "no spool"
                result["events"].append({"slot": slot, "kind": "conflict", "text": (
                    f"Slot {slot}: the screen holds spool {_label(by_id[sid])} but Spoolman assigns {before}. "
                    f"Nothing was changed; use /spool to say which is right.")})
                continue
            if not changed:
                seen[skey] = sid  # a drift Print Handy already knew about
                continue
            if busy:
                key = f"deferred:{sid}"
                live_keys[skey] = key
                if announced.get(skey) != key:
                    result["events"].append({"slot": slot, "kind": "deferred", "text": (
                        f"Slot {slot}: the screen now holds spool {_label(by_id[sid])}. The printer is busy, "
                        f"so Print Handy will record it after the print.")})
                continue
            try:
                spool_sync.bind_slot(slot, sid, client=client, mr=mr, state_path=state_path,
                                     push_screen_update=False, printer_id=pid,
                                     note="Observed from the screen (#791)")
            except spool_sync.SpoolError as exc:
                key = f"refused:{sid}:{exc}"
                live_keys[skey] = key
                if announced.get(skey) != key:
                    result["events"].append({"slot": slot, "kind": "conflict", "text": (
                        f"Slot {slot}: the screen holds spool {_label(by_id[sid])} but Print Handy "
                        f"could not record it: {exc}")})
                continue
            seen[skey] = sid
            prior = assigned.get(slot)
            before = f"spool #{prior}" if prior else "no spool"
            result["events"].append({"slot": slot, "kind": "changed", "text": (
                f"Slot {slot} changed at the screen: {before} -> spool {_label(by_id[sid])}. "
                f"Print Handy's records now match.")})
        # Announced markers persist only while the condition still holds, so a
        # fixed conflict that comes back is announced again, and a standing one never repeats.
        announced = {k: v for k, v in live_keys.items()}
        state = {"seeded": True, "seen": seen, "announced": announced}
        _write_json(path, state)
        result["ok"] = True
        return result
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass


def events_text(result: dict[str, Any]) -> list[str]:
    """The lines to announce for one observe() result (empty when nothing changed)."""
    return [str(e["text"]) for e in result.get("events") or []]
