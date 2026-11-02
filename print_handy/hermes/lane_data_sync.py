"""HelixScreen inbound spool sync: watch Moonraker ``lane_data`` (#791).

Print Handy pushes spool state to the printer screen on a bind (the
``CHANGE_ZCOLOR`` from ``spool_sync.push_screen`` and the ``lane_data``
record from ``spool_sync.write_lane_record``). This module is the other
direction: it polls the shared Moonraker database namespace ``lane_data``
on the listener's existing cadence, diffs each lane against persisted
watcher state, and converges Print Handy's records to what the screen
says is physically loaded.

Converge semantics, shared with #798: the screen wins for what is
physically loaded, Spoolman wins for spool record data. A screen-side
assignment is written into the Spoolman ``ph_slot`` extra field and the
local slot cache is then rebuilt by ``spool_sync.reconcile`` — this
module never writes the cache itself.

Hardening (the drift classes from #791):

- A lane locked on the screen (``helix_locked_color`` /
  ``helix_locked_material`` true) whose locked value differs from the
  Spoolman record, and a lane linked to an archived or deleted spool,
  get a named prompt. Nothing is silently overwritten.
- A lane the screen exposes only by material/color (no Spoolman id) is
  named as unresolved in its prompt. No spool is guessed or bound.
- Unreachable or absent ``lane_data`` reads as unknown: the poll changes
  nothing, not even the watcher state file.
- Mid-print changes are noted once and applied only when the print ends,
  through the same deferral posture as ``push_screen``; the one exception
  is a load into a slot with an open runout/swap spool question on a
  paused print, which is fed into the existing runout recovery through
  the caller-supplied ``runout_feed`` hook (the listener wires it to
  ``bind_slot`` with the runout disposition, ``resume_usage`` and
  clearing the question — the same steps a chat answer takes).

Announcements follow the #483 watcher pattern: an event waits in a
persisted "pending" map until the caller reports it delivered, is
retried every ``LANE_RETRY_S``, and the first successful poll only seeds
the baseline silently.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from ..core import filament_color
from . import spool_sync

# An undelivered lane announcement is retried this often (the #483 cadence).
LANE_RETRY_S = 300.0

Client = Callable[..., Any]


# ---------------------------------------------------------------- state file

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
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- lane records

def _norm_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _norm_color(value: Any) -> str:
    hex_code = spool_sync.screen_hex(value)
    return hex_code.casefold() if hex_code else _norm_text(value)


def _spool_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def normalize_lane(slot: int, record: dict[str, Any]) -> dict[str, Any]:
    """One raw ``lane_data`` record in watcher terms.

    The color may be spelled ``color``, ``color_hex`` or ``hex`` depending
    on the writer; the material ``material`` or ``type``. Everything else
    (``spool_id``, ``lane``, ``scan_time``, the ``helix_locked_*`` flags)
    uses the names the shared namespace defines.
    """
    color = record.get("color")
    if color is None:
        color = record.get("color_hex") or record.get("hex") or ""
    material = record.get("material")
    if material is None:
        material = record.get("type") or ""
    scan_time = record.get("scan_time")
    if scan_time is None:
        scan_time = record.get("scanned_at") or record.get("timestamp") or ""
    return {
        "slot": int(slot),
        "spool_id": _spool_id(record.get("spool_id")),
        "lane": record.get("lane"),
        "scan_time": str(scan_time),
        "color": str(color or ""),
        "material": str(material or ""),
        "name": str(record.get("name") or ""),
        "locked_color": record.get("helix_locked_color") is True,
        "locked_material": record.get("helix_locked_material") is True,
        "raw": record,
    }


def parse_lane_data(value: Any) -> dict[int, dict[str, Any]] | None:
    """{slot: normalized lane} from a raw ``lane_data`` namespace value, or None when unknown.

    The outer keys are ``laneN`` (1-based); a value that is not a mapping
    at all is unknown (None), while a mapping with no lane keys is a
    genuinely empty namespace ({}) — every lane was cleared.
    """
    if not isinstance(value, dict):
        return None
    lanes: dict[int, dict[str, Any]] = {}
    for key, record in value.items():
        match = re.fullmatch(r"lane(\d+)", str(key))
        if not match or not isinstance(record, dict):
            continue
        slot = int(match.group(1))
        if slot in spool_sync.SLOTS:
            lanes[slot] = normalize_lane(slot, record)
    return lanes


def lane_fingerprint(lane: dict[str, Any]) -> dict[str, Any]:
    """The diffed fields for one lane: spool_id / lane / scan_time / color / material."""
    return {
        "spool_id": lane.get("spool_id"),
        "lane": lane.get("lane"),
        "scan_time": str(lane.get("scan_time") or ""),
        "color": _norm_color(lane.get("color")),
        "material": _norm_text(lane.get("material")),
    }


def _action_key(fingerprint: dict[str, Any] | None) -> str:
    """What a lane materially says: which spool, of what material and color.

    A fresh ``scan_time`` (or a rewritten inner ``lane``) with the same
    spool, material and color is a rescan of the same physical state, not
    a change to act on or announce.
    """
    fp = fingerprint or {}
    return f"{fp.get('spool_id')}|{fp.get('material')}|{fp.get('color')}"


def _spool_label(spool: dict[str, Any]) -> str:
    filament = (spool or {}).get("filament") or {}
    parts = [str(filament.get("name") or "").strip(), str(filament.get("material") or "").strip()]
    return " ".join(part for part in parts if part) or "unknown filament"


def _color_agrees(lane_color: str, spool: dict[str, Any]) -> bool:
    """True when the lane's color matches the Spoolman filament's color by hex or by name."""
    filament = (spool or {}).get("filament") or {}
    lane_hex = spool_sync.screen_hex(lane_color)
    spool_hex = spool_sync.screen_hex(filament.get("color_hex")) \
        or spool_sync.screen_hex(spool_sync.first_hex_of(filament)) \
        or spool_sync.screen_hex(filament_color.derive_hex(
            filament.get("name"), filament.get("material")))
    if lane_hex and spool_hex:
        return lane_hex == spool_hex
    if lane_hex or spool_hex:
        return False
    return _norm_text(lane_color) == _norm_text(filament.get("name"))


def _lock_conflicts(lane: dict[str, Any], spool: dict[str, Any]) -> list[str]:
    """Named differences between screen-locked values and the Spoolman record."""
    filament = (spool or {}).get("filament") or {}
    conflicts = []
    if lane.get("locked_material") and lane.get("material"):
        spool_material = str(filament.get("material") or "").strip()
        if spool_material and _norm_text(lane["material"]) != _norm_text(spool_material):
            conflicts.append(
                f"the material is locked on the screen to {lane['material']}, "
                f"but spool #{spool.get('id')} is {spool_material} in Spoolman")
    if lane.get("locked_color") and lane.get("color"):
        if not _color_agrees(str(lane["color"]), spool):
            spool_color = str(filament.get("name") or "").strip() or "an unnamed color"
            conflicts.append(
                f"the color is locked on the screen to {lane['color']}, "
                f"but spool #{spool.get('id')} is {spool_color} in Spoolman")
    return conflicts


# ---------------------------------------------------------------- converge

def apply_screen_assignment(printer_id: str, slot: int, spool_id: int | None, *,
                            client: Client = spool_sync.spoolman, mr: Client = spool_sync.moonraker,
                            state_path: Path | None = None,
                            pending_path: Path | None = None) -> dict[str, Any]:
    """Converge one slot to the screen's physical state, through the reconciler (#791/#798).

    The screen's spool claims the slot in Spoolman ``ph_slot`` (or every
    claim on the slot is cleared, for an unload), then
    ``spool_sync.reconcile`` rebuilds the local cache and the screens from
    Spoolman. A reconcile that does not complete raises SpoolError, so the
    caller leaves the lane un-adopted and retries on a later poll instead
    of announcing a convergence that did not happen.
    """
    if spool_id is not None:
        spool_sync._set_ph_slot(client, int(spool_id), spool_sync.format_ph_slot(printer_id, int(slot)))
    spool_sync._clear_ph_slot_claims(client, printer_id, int(slot), keep_spool_id=spool_id)
    result = spool_sync.reconcile(printer_id, client=client, mr=mr,
                                  state_path=state_path, pending_path=pending_path)
    if not result.get("ok"):
        raise spool_sync.SpoolError(str(result.get("error") or "spool reconcile did not complete"))
    return result


# ---------------------------------------------------------------- watcher

class LaneDataWatcher:
    """Diff ``lane_data`` against persisted state; produce each announcement exactly once.

    Injected seams (all optional, all defaulting to the live services):
    ``mr`` / ``client`` are the Moonraker / Spoolman callables,
    ``printer_state_fn`` reports the print state, ``apply_fn`` converges
    one slot (default :func:`apply_screen_assignment`), and
    ``runout_context_fn`` / ``runout_feed_fn`` wire the runout recovery:
    the context function returns the open runout/swap spool question (the
    listener's ``load_runout_pause()`` record) or None, and the feed
    function binds a screen-loaded spool into that question's slot and
    resumes usage, returning True when the question was answered.
    """

    def __init__(self, state_path, *, mr: Client = None, client: Client = None,
                 printer_id: str | None = None, clock: Callable[[], float] = time.time,
                 printer_state_fn: Callable[..., str] = None,
                 apply_fn: Callable[..., Any] = None,
                 runout_context_fn: Callable[[], dict[str, Any] | None] = None,
                 runout_feed_fn: Callable[..., bool] = None,
                 state_file: Path | None = None, pending_file: Path | None = None):
        self.state_path = Path(state_path)
        self.mr = mr or spool_sync.moonraker
        self.client = client or spool_sync.spoolman
        self.printer_id = printer_id
        self.clock = clock
        self.printer_state_fn = printer_state_fn or spool_sync.printer_state
        self.apply_fn = apply_fn
        self.runout_context_fn = runout_context_fn
        self.runout_feed_fn = runout_feed_fn
        # The hardware record / deferred-screen files the reconcile writes;
        # None keeps spool_sync's own per-printer defaults.
        self.state_file = Path(state_file) if state_file else None
        self.pending_file = Path(pending_file) if pending_file else None
        self._print_state: str | None = None

    # -- injected helpers ------------------------------------------------

    def _print_state_now(self) -> str:
        if self._print_state is None:
            try:
                self._print_state = str(self.printer_state_fn(self.mr))
            except Exception:
                self._print_state = "unknown"
        return self._print_state

    def _lookup_spool(self, spool_id: int) -> tuple[str, dict[str, Any] | None]:
        """("live" | "archived" | "missing" | "unknown", spool record)."""
        try:
            row = spool_sync._call_client(self.client, "GET", f"/api/v1/spool/{int(spool_id)}", None)
        except KeyError:
            return "missing", None  # a 404-shaped answer: the record is gone
        except spool_sync.SpoolError:
            return "unknown", None  # Spoolman did not answer: proves nothing
        except Exception:
            return "unknown", None
        if not isinstance(row, dict) or row.get("id") is None:
            return "missing", None
        return ("archived", row) if row.get("archived") else ("live", row)

    def _apply(self, slot: int, spool_id: int | None) -> None:
        if self.apply_fn is not None:
            self.apply_fn(slot, spool_id)
            return
        pid = self.printer_id or spool_sync._default_printer_id()
        if not pid:
            raise spool_sync.SpoolError("no printer could be derived from the configuration")
        apply_screen_assignment(pid, slot, spool_id, client=self.client, mr=self.mr,
                                state_path=self.state_file, pending_path=self.pending_file)

    # -- event plumbing ----------------------------------------------------

    @staticmethod
    def _queue(pending: dict[str, Any], noted: dict[str, Any], slot: int, kind: str,
               text: str, action: str, *, spool_id: int | None = None) -> str:
        key = f"{slot}:{kind}:{action}"
        pending[key] = {"since": 0.0, "tried": 0.0,
                        "event": {"key": key, "slot": slot, "kind": kind,
                                  "spool_id": spool_id, "text": text}}
        return key

    def _drop_noted(self, pending: dict[str, Any], noted: dict[str, Any], slot: int) -> None:
        """A settled change supersedes an earlier mid-print note for the same slot."""
        if noted.pop(str(slot), None) is not None:
            for key in [k for k, entry in pending.items()
                        if isinstance(entry, dict)
                        and (entry.get("event") or {}).get("kind") == "noted_mid_print"
                        and (entry.get("event") or {}).get("slot") == slot]:
                del pending[key]

    # -- the poll ----------------------------------------------------------

    def poll(self) -> list[dict[str, Any]]:
        """Events to announce now: new changes, plus undelivered ones due for a retry.

        Side effects (the ``ph_slot`` write and reconcile for an applied
        change, the runout feed for a runout load) happen here, once, when
        the change is first adopted into the persisted baseline — the
        pending/retry machinery below only re-announces, it never
        re-applies.
        """
        self._print_state = None
        lanes = parse_lane_data(spool_sync.read_lane_data(self.mr))
        if lanes is None:
            return []  # unknown, never empty: change nothing at all
        state = _read_json(self.state_path)
        fingerprints = {slot: lane_fingerprint(lane) for slot, lane in lanes.items()}
        if not state.get("seeded") or not isinstance(state.get("lanes"), dict):
            _write_json(self.state_path,
                        {"seeded": True,
                         "lanes": {str(slot): fp for slot, fp in sorted(fingerprints.items())},
                         "pending": {}, "noted": {}})
            return []
        seen: dict[int, dict[str, Any]] = {}
        for key, value in state["lanes"].items():
            if str(key).isdigit() and isinstance(value, dict):
                seen[int(key)] = value
        pending = state.get("pending") if isinstance(state.get("pending"), dict) else {}
        noted = state.get("noted") if isinstance(state.get("noted"), dict) else {}
        dirty = False
        for slot in sorted(set(seen) | set(lanes)):
            old_fp = seen.get(slot)
            lane = lanes.get(slot)
            cur_fp = fingerprints.get(slot)
            if cur_fp == old_fp:
                continue
            if lane is not None and old_fp is not None \
                    and _action_key(cur_fp) == _action_key(old_fp):
                seen[slot] = cur_fp  # a rescan of the same physical state: adopt silently
                dirty = True
                continue
            if self._process_lane(slot, lane, old_fp, seen, pending, noted):
                dirty = True
        now = self.clock()
        due = []
        for key in sorted(pending):
            entry = pending[key]
            if not isinstance(entry, dict) or not isinstance(entry.get("event"), dict):
                del pending[key]
                dirty = True
                continue
            if not entry.get("since"):
                entry["since"] = now
            if now - float(entry.get("tried") or 0) >= LANE_RETRY_S:
                entry["tried"] = now
                due.append(entry["event"])
                dirty = True
        updated = {"seeded": True,
                   "lanes": {str(slot): fp for slot, fp in sorted(seen.items())},
                   "pending": pending, "noted": noted}
        if dirty or updated != state:
            _write_json(self.state_path, updated)
        return due

    def _process_lane(self, slot: int, lane: dict[str, Any] | None,
                      old_fp: dict[str, Any] | None, seen: dict[int, dict[str, Any]],
                      pending: dict[str, Any], noted: dict[str, Any]) -> bool:
        """Act on one materially changed lane. Returns True when watcher state changed."""
        cur_fp = lane_fingerprint(lane) if lane is not None else None
        action = _action_key(cur_fp)

        def adopt() -> None:
            if cur_fp is None:
                seen.pop(slot, None)
            else:
                seen[slot] = cur_fp

        # -- cleared lane --------------------------------------------------
        if lane is None or (lane["spool_id"] is None and not lane["material"] and not lane["color"]):
            if not old_fp or old_fp.get("spool_id") is None:
                adopt()
                return True  # nothing was recorded loaded: a silent clear
            if self._defer_mid_print(slot, action, pending, noted,
                                     f"slot {slot} was unloaded"):
                return True
            try:
                self._apply(slot, None)
            except Exception:
                return False  # not adopted: a later poll retries the converge
            self._drop_noted(pending, noted, slot)
            self._queue(pending, noted, slot, "unloaded",
                        f"**HelixScreen change:** slot {slot} was unloaded at the printer "
                        f"screen (spool #{old_fp['spool_id']} removed). Print Handy's records "
                        "were updated from Spoolman to match.",
                        action, spool_id=None)
            adopt()
            return True

        spool_id = lane["spool_id"]

        # -- no Spoolman id: never guess a spool ---------------------------
        if spool_id is None:
            what = " / ".join(part for part in (lane["material"], lane["color"]) if part) or "filament"
            self._queue(pending, noted, slot, "unresolved",
                        f"**HelixScreen change:** slot {slot} shows {what} loaded at the "
                        "printer screen, but the screen does not name a Spoolman spool for "
                        "it. No spool was guessed or bound. Record it with "
                        f"`/spool change {slot} <spool#>`.",
                        action)
            adopt()
            return True

        # -- identity checks before any write ------------------------------
        status, spool = self._lookup_spool(spool_id)
        if status == "unknown":
            return False  # Spoolman silent: change nothing, retry on a later poll
        if status in ("archived", "missing"):
            fate = "is archived in Spoolman" if status == "archived" \
                else "no longer exists in Spoolman"
            self._queue(pending, noted, slot, "stale_spool",
                        f"**HelixScreen lane needs attention:** slot {slot} on the screen is "
                        f"linked to spool #{spool_id}, which {fate}. Nothing was overwritten. "
                        f"Relink the slot with `/spool change {slot} <spool#>`, or clear the "
                        "lane on the screen.",
                        action, spool_id=spool_id)
            adopt()
            return True
        assert spool is not None
        conflicts = _lock_conflicts(lane, spool)
        if conflicts:
            named = "; ".join(conflicts)
            self._queue(pending, noted, slot, "locked",
                        f"**HelixScreen lane needs attention:** slot {slot} — {named}. "
                        "Nothing was overwritten: a value declared on the screen outranks "
                        "Print Handy. Fix the locked value on the screen, or relink the "
                        f"slot with `/spool change {slot} <spool#>`.",
                        action, spool_id=spool_id)
            adopt()
            return True
        filament = spool.get("filament") or {}
        lane_material = _norm_text(lane["material"])
        spool_material = _norm_text(filament.get("material"))
        if lane_material and spool_material and lane_material != spool_material:
            self._queue(pending, noted, slot, "identity_conflict",
                        f"**HelixScreen lane needs attention:** slot {slot} on the screen names "
                        f"spool #{spool_id} as {lane['material']}, but Spoolman records that "
                        f"spool as {filament.get('material')}. Nothing was rebound. Check which "
                        f"spool is really in the slot, then relink it with "
                        f"`/spool change {slot} <spool#>`.",
                        action, spool_id=spool_id)
            adopt()
            return True

        # -- a runout/swap question is open for this slot -------------------
        if self._print_state_now() == "paused" and self.runout_context_fn is not None:
            try:
                context = self.runout_context_fn()
            except Exception:
                context = None
            if isinstance(context, dict) and context.get("slot") == slot \
                    and context.get("cause") in ("runout", "swap"):
                if self.runout_feed_fn is None:
                    # Detection without a wired feed: name it once (the noted
                    # suppression) and change nothing — the open question in
                    # chat remains the way to answer.
                    if noted.get(str(slot)) != action:
                        noted[str(slot)] = action
                        self._queue(pending, noted, slot, "runout_load",
                                    f"**HelixScreen change:** spool #{spool_id} was loaded into "
                                    f"slot {slot} at the printer screen while a print is paused "
                                    "for a runout. No runout feed is wired here, so nothing was "
                                    "changed — answer the spool question in chat with "
                                    f"`{spool_id}` so usage tracking resumes on the right spool.",
                                    action, spool_id=spool_id)
                        return True
                    return False
                try:
                    fed = bool(self.runout_feed_fn(slot, spool_id, context))
                except Exception:
                    fed = False
                if not fed:
                    return False  # the question stays open; retry on a later poll
                self._drop_noted(pending, noted, slot)
                self._queue(pending, noted, slot, "runout_load",
                            f"**HelixScreen change:** spool #{spool_id} ({_spool_label(spool)}) "
                            f"was loaded into slot {slot} at the printer screen during a paused "
                            "runout. It was fed into the runout recovery: the slot is bound to "
                            "it and filament usage is charged to it again.",
                            action, spool_id=spool_id)
                adopt()
                return True

        # -- mid-print: note once, apply when the print ends -----------------
        if self._defer_mid_print(slot, action, pending, noted,
                                  f"spool #{spool_id} was loaded into slot {slot}"):
            return True

        # -- the plain screen-side load/swap ---------------------------------
        try:
            self._apply(slot, spool_id)
        except Exception:
            return False
        self._drop_noted(pending, noted, slot)
        prior = (old_fp or {}).get("spool_id")
        swap = f" Spool #{prior} was replaced." if prior and prior != spool_id else ""
        self._queue(pending, noted, slot, "loaded",
                    f"**HelixScreen change:** slot {slot} now holds spool #{spool_id} "
                    f"({_spool_label(spool)}), loaded at the printer screen.{swap} "
                    "Print Handy's records were updated from Spoolman to match.",
                    action, spool_id=spool_id)
        adopt()
        return True

    def _defer_mid_print(self, slot: int, action: str, pending: dict[str, Any],
                         noted: dict[str, Any], what: str) -> bool:
        """True when this change must wait: noted exactly once, baseline not adopted."""
        state = self._print_state_now()
        if state not in ("printing", "paused", "unknown"):
            return False
        if noted.get(str(slot)) == action:
            return True  # already noted: stay silent and keep waiting
        noted[str(slot)] = action
        self._queue(pending, noted, slot, "noted_mid_print",
                    f"**HelixScreen change noted:** {what} at the printer screen while the "
                    f"printer state is {state}. Nothing was rebound mid-print; the change "
                    "will be picked up when the print ends.",
                    action)
        return True

    def delivered(self, key: str) -> None:
        """Clear one announcement's pending entry once it has reached the chat."""
        state = _read_json(self.state_path)
        pending = state.get("pending")
        if isinstance(pending, dict) and pending.pop(str(key), None) is not None:
            _write_json(self.state_path, state)


def lane_event_text(event: dict[str, Any]) -> str:
    """The announcement text for one lane event (built at detection time)."""
    return str((event or {}).get("text") or "")
