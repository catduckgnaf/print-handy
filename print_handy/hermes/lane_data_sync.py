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
- The namespace has a second author: ZMod's ``orca_lanes`` publishing
  (#851) writes minimal records (lane/material/color/name, no spool id)
  under 0-based keys. Those are not screen lanes — read under the
  screen's 1-based keys they would shift every slot by one — so the
  parser skips records carrying no screen-side field.
- Because ZMod's keys collide with the screen's at ``lane1``-``lane3``,
  ZMod deleting its own key (a slot whose type became ``?``) also
  deletes a screen record (#852). Once that author has been seen, a
  vanished spool-bearing lane converges to "unloaded" only when the
  printer's own presence read agrees the slot is empty; see
  ``LaneDataWatcher._vanished_unload_blocked``.
- Mid-print changes are noted once and applied only when the print ends,
  through the same deferral posture as ``push_screen``; the one exception
  is a load into a slot with an open runout/swap spool question on a
  paused print, which is fed into the existing runout recovery through
  the caller-supplied ``runout_feed`` hook (the listener wires it to
  ``bind_slot`` with the runout disposition, ``resume_usage`` and
  clearing the question — the same steps a chat answer takes).
- A lane whose spool already claims the slot through ``ph_slot`` is the
  echo of Print Handy's own bind (which writes the claim and the lane
  record together), not a screen-side change: it is adopted silently,
  with no announcement and no mid-print note (#1063).
- Two keyings share the namespace (#1304). The screen shape keys
  ``lane1``..``lane4`` with a 0-based inner ``lane`` one below the key
  (``lane1`` -> ``"lane": 0`` = slot 1); ZMod 1.7.6 ``orca_lanes`` keys
  ``lane0``..``lane3`` with the inner ``lane`` equal to the key
  (``lane0`` -> ``"lane": 0`` = slot 1). zmod_nfc enriches the slot's
  existing entry, found by its inner ``lane`` field, with ``spool_id``
  and the rest, so without HelixScreen the spool-bearing record is
  orca-keyed. The inner ``lane`` field is therefore the slot authority
  whenever it is consistent with either keying, and the record
  converges into slot ``lane + 1``. A record whose inner ``lane`` fits
  neither keying is self-contradicting: it is named once as a slot
  disagreement and never converged into any slot (the print-start
  self-check applies the same distrust, #1020 D3). Two records that
  resolve to the same physical slot (one under each keying) collapse
  to one when they say the same thing, and are otherwise named once as
  a slot conflict and converge nothing until one of them is removed.

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


def _slot(slot: int) -> str:
    """A slot named with its base in user-visible text (#1020 D1)."""
    return filament_color.slot_display(slot)

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
        # The inner ``lane`` value converted to a 1-based slot, exactly
        # once, here (#1020 D2): consumers compare ``lane_slot`` with
        # ``slot`` instead of re-converting (or forgetting to).
        "lane_slot": spool_sync.slot_from_lane_index0(record.get("lane")),
        "scan_time": str(scan_time),
        "color": str(color or ""),
        "material": str(material or ""),
        "name": str(record.get("name") or ""),
        "locked_color": record.get("helix_locked_color") is True,
        "locked_material": record.get("helix_locked_material") is True,
        "raw": record,
    }


# Fields only the screen-side writers (HelixScreen, and Print Handy writing
# on its behalf) ever put on a lane record. ZMod's own ``orca_lanes``
# publishing (#851) writes the same namespace in a minimal shape — just
# lane/material/color/name — under 0-based keys (slot 1 is ``lane0``), so a
# marker-less record read under the screen's 1-based keys would hand every
# slot the next slot's data. ZMod records also name no spool, so there is
# nothing the watcher could converge from them; they are skipped here.
_SCREEN_MARKERS = ("spool_id", "scan_time", "scanned_at", "timestamp")


def _screen_authored(record: dict[str, Any]) -> bool:
    """True when a lane record carries a field only screen-side writers use."""
    return any(key in record for key in _SCREEN_MARKERS) or any(
        isinstance(key, str) and key.startswith("helix_") for key in record)


def foreign_lane_slots(value: Any) -> set[int]:
    """Slots whose ``laneN`` key holds a marker-less (ZMod ``orca_lanes``) record.

    Such a key is occupied by another writer, not cleared: the watcher must
    read the slot as unknown and change nothing, never as an unload (#852).
    """
    if not isinstance(value, dict):
        return set()
    slots = set()
    for key, record in value.items():
        match = re.fullmatch(r"lane(\d+)", str(key))
        if match and isinstance(record, dict) and not _screen_authored(record):
            slots.add(int(match.group(1)))
    return slots


def _resolve_slot(key_n: int, record: dict[str, Any]) -> tuple[int | None, str]:
    """(physical slot, keying) for one spool-bearing ``laneN`` record (#1304).

    ``keying`` is ``"screen"`` (``key == inner + 1``), ``"orca"`` (``key ==
    inner``, ZMod 1.7.6 ``orca_lanes`` including ``lane0``), ``"keyed"``
    (no usable inner ``lane``: the key's screen meaning, as before #1304)
    or ``"disagreement"`` (the inner ``lane`` fits neither keying; the slot
    returned is only where the disagreement is reported — the screen
    meaning of the key, or slot 1 for ``lane0`` — never a converge target).
    """
    inner = spool_sync.slot_from_lane_index0(record.get("lane"))
    if inner is not None:
        if key_n == inner:
            return inner, "screen"
        if key_n == inner - 1:
            return inner, "orca"
        if key_n in spool_sync.SLOTS:
            return key_n, "disagreement"
        if key_n + 1 in spool_sync.SLOTS:
            return key_n + 1, "disagreement"
        return None, "disagreement"
    return (key_n if key_n in spool_sync.SLOTS else None), "keyed"


# Deterministic order among records claiming one physical slot.
_KEYING_ORDER = {"screen": 0, "keyed": 1, "orca": 2, "disagreement": 3}


def _says(lane: dict[str, Any]) -> tuple[Any, str, str]:
    return (lane.get("spool_id"), _norm_text(lane.get("material")), _norm_color(lane.get("color")))


def parse_lane_data(value: Any) -> dict[int, dict[str, Any]] | None:
    """{slot: normalized lane} from a raw ``lane_data`` namespace value, or None when unknown.

    Spool-bearing (screen-marked) records under ``laneN`` keys are read in
    either keying (#1304): the screen shape (``lane1``..``lane4``, inner
    ``lane`` = key - 1) or ZMod 1.7.6 ``orca_lanes`` keying
    (``lane0``..``lane3``, inner ``lane`` = key) that zmod_nfc enriches
    when HelixScreen is absent. Each converges into slot ``lane + 1``. A
    record fitting neither keying carries ``slot_issue="disagreement"``;
    two differing records resolving to one slot carry
    ``slot_issue="conflict"`` (identical ones collapse silently to the
    first in a fixed keying order). Marker-less records in ZMod's minimal
    ``orca_lanes`` shape (#851) name no spool and are skipped (see
    ``_SCREEN_MARKERS``). A value that is not a mapping at all is unknown
    (None), while a mapping with no lane keys is a genuinely empty
    namespace ({}) — every lane was cleared.
    """
    if not isinstance(value, dict):
        return None
    claims: dict[int, list[dict[str, Any]]] = {}
    for key, record in value.items():
        match = re.fullmatch(r"lane(\d+)", str(key))
        if not match or not isinstance(record, dict):
            continue
        if not _screen_authored(record):
            continue
        slot, keying = _resolve_slot(int(match.group(1)), record)
        if slot is None:
            continue
        lane = normalize_lane(slot, record)
        lane.update(key=str(key), keying=keying,
                    slot_issue="disagreement" if keying == "disagreement" else None)
        claims.setdefault(slot, []).append(lane)
    lanes: dict[int, dict[str, Any]] = {}
    for slot, found in claims.items():
        found.sort(key=lambda lane: (_KEYING_ORDER[lane["keying"]], lane["key"]))
        first = found[0]
        if len(found) > 1 and (any(lane["slot_issue"] for lane in found)
                               or len({_says(lane) for lane in found}) > 1):
            first = {**first, "slot_issue": "conflict",
                     "claims": [{"key": lane["key"], "keying": lane["keying"],
                                 "spool_id": lane["spool_id"], "material": lane["material"],
                                 "color": lane["color"]} for lane in found]}
        lanes[slot] = first
    return lanes


def slot_agreement_warning(*, job_slot=None, ffm_channel=None, ifs_chan=None,
                           helix_slot=None, filename=None) -> str | None:
    """The print-start slot self-check (#1020 D3).

    Compares the four slot sources — the job's requested
    ``physical_slot``, ZMod ``FFMInfo.channel``, ``zmod_ifs`` ``Chan``
    and the HelixScreen seated slot — after conversion (every input
    here is already a 1-based slot; the Helix value arrives converted
    by :func:`spool_sync.slot_from_lane_index0`). Returns a warning
    naming every source when at least two known sources disagree, and
    None when they agree or fewer than two are known: a partial read
    never cries wolf.
    """
    known = [(label, value) for label, value in (
        ("the job requested", job_slot),
        ("FFMInfo.channel reports", ffm_channel),
        ("zmod_ifs Chan reports", ifs_chan),
        ("HelixScreen seated", helix_slot),
    ) if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 4]
    if len({value for _label, value in known}) < 2:
        return None
    from print_handy.core import filament_color

    detail = "; ".join(f"{label} {filament_color.slot_display(value)}"
                       for label, value in known)
    subject = f" for {filename}" if filename else ""
    return (f"**Slot check warning{subject}:** the slot sources disagree — {detail}. "
            "The filament may not be where the job expects it. Check the loaded slot "
            "at the printer before trusting slot-based alerts for this print.")


def helix_seated_slot(lanes: dict[int, dict[str, Any]] | None,
                      spool_id: Any) -> int | None:
    """Where HelixScreen believes the job's spool sits, or None (#1020 D3).

    ``lanes`` is :func:`parse_lane_data` output. The seated slot is the
    outer key of the record naming ``spool_id`` — unless that record's
    inner ``lane`` converts to a different slot, in which case the
    converted value is what HelixScreen's own numbering means, and it
    is returned so the self-check can disagree with it.
    """
    if not lanes or spool_id is None:
        return None
    for slot, lane in lanes.items():
        if lane.get("spool_id") == spool_id:
            converted = lane.get("lane_slot")
            return converted if converted is not None else slot
    return None


def lane_fingerprint(lane: dict[str, Any]) -> dict[str, Any]:
    """The diffed fields for one lane: spool_id / lane / scan_time / color / material / locks.

    The helix_locked_* flags are diffed too: removing a lock that blocked a
    lane (without changing its spool, material or color) is a material
    change — the lane must be reprocessed so the assignment can converge.
    """
    return {
        "spool_id": lane.get("spool_id"),
        "lane": lane.get("lane"),
        "scan_time": str(lane.get("scan_time") or ""),
        "color": _norm_color(lane.get("color")),
        "material": _norm_text(lane.get("material")),
        "locked_color": bool(lane.get("locked_color")),
        "locked_material": bool(lane.get("locked_material")),
        "slot_issue": _slot_issue_token(lane),
    }


def _slot_issue_token(lane: dict[str, Any]) -> str:
    """'' for a self-consistent lane; otherwise what makes it untrustworthy (#1304).

    Part of the action key, so a correction of only the inner ``lane``
    field (or the removal of one of two conflicting records) is a change
    to act on, not a rescan — and a converged lane turning
    self-contradicting is named.
    """
    issue = lane.get("slot_issue")
    if issue == "disagreement":
        return f"disagreement:{lane.get('key')}:{lane.get('lane_slot')}"
    if issue == "conflict":
        return "conflict:" + ",".join(
            f"{c['key']}={c['spool_id']}/{_norm_text(c['material'])}/{_norm_color(c['color'])}"
            for c in lane.get("claims") or [])
    return ""


def _action_key(fingerprint: dict[str, Any] | None) -> str:
    """What a lane materially says: which spool, of what material and color, under which locks.

    A fresh ``scan_time`` (or a re-keyed record whose inner ``lane``
    still resolves to the same slot) with the same spool, material,
    color and locks is a rescan of the same physical state, not a change
    to act on or announce. A lock change is not a rescan: it can unblock
    (or newly block) the lane's convergence; nor is a change in the
    record's slot trust (#1304): a disagreement or conflict appearing or
    being corrected changes what the watcher may do with the lane.
    Baselines written before #1304 carry no ``slot_issue`` and read as
    consistent, so an upgrade re-announces nothing.
    """
    fp = fingerprint or {}
    key = (f"{fp.get('spool_id')}|{fp.get('material')}|{fp.get('color')}"
           f"|{fp.get('locked_color')}|{fp.get('locked_material')}")
    issue = fp.get("slot_issue") or ""
    return f"{key}|{issue}" if issue else key


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



# Speed and flow grades a screen catalog may add or drop ("PETG HF", "High
# Speed PLA", "Hyper PLA"). They matter for slicing (material_family keeps
# them distinct), but not for whether a lane and a spool name the same
# filament, so the identity check ignores them.
_SPEED_GRADE = re.compile(r"\b(?:HF|HS|HIGH\s*FLOW|HIGH\s*SPEED|HYPER|RAPID)\b", re.I)


def _same_base_material(a: Any, b: Any) -> bool:
    """Whether a lane's material and a spool's material are the same base filament.

    HelixScreen's generic catalog writes "PLA" for a PLA+ spool (seen live on
    an AD5X after the HelixScreen 1.0.8 update), so a raw text comparison
    called the same spool a different filament. The family rules of
    ``printer_state.material_family`` apply (PLA+, PLA Pro, Matte and Silk
    PLA are PLA), plus speed and flow grades are ignored (PETG vs PETG-HF).
    Reinforcement stays a real difference: PLA and PLA-CF never match.
    """
    from ..core.printer_state import material_family

    def base(material: Any) -> str:
        text = _SPEED_GRADE.sub(" ", re.sub(r"[-_]", " ", str(material or "")))
        return material_family(text)

    fa, fb = base(a), base(b)
    return bool(fa) and fa == fb


def _lock_conflicts(lane: dict[str, Any], spool: dict[str, Any]) -> list[str]:
    """Named differences between screen-locked values and the Spoolman record."""
    filament = (spool or {}).get("filament") or {}
    conflicts = []
    if lane.get("locked_material") and lane.get("material"):
        spool_material = str(filament.get("material") or "").strip()
        if spool_material and not _same_base_material(lane["material"], spool_material):
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
        if not spool_sync._set_ph_slot(client, int(spool_id),
                                       spool_sync.format_ph_slot(printer_id, int(slot))):
            # A rejected write must not be followed by clearing the previous
            # spool's claim or by adopting the lane: the slot keeps its old
            # mapping and a later poll retries the converge.
            raise spool_sync.SpoolError(
                f"Spoolman did not accept the slot assignment for spool #{spool_id}; "
                "nothing was changed")
    spool_sync._clear_ph_slot_claims(client, printer_id, int(slot), keep_spool_id=spool_id)
    result = spool_sync.reconcile(printer_id, client=client, mr=mr,
                                  state_path=state_path, pending_path=pending_path)
    if not result.get("ok"):
        raise spool_sync.SpoolError(str(result.get("error") or "spool reconcile did not complete"))
    # Verify the reconcile actually produced the requested state: an ok
    # result whose assignments name a different spool (or still name one,
    # for an unload) means the converge did not happen, so the caller must
    # not adopt the lane or announce success.
    settled = (result.get("assignments") or {}).get(int(slot))
    if settled != spool_id:
        raise spool_sync.SpoolError(
            f"spool reconcile settled {_slot(slot)} on {settled!r}, not the screen's "
            f"{spool_id!r}; the change was not adopted")
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
    ``presence_fn`` returns the printer's current slot presence,
    ``{slot: True | False | None}`` (the listener's IFS read), or None when
    there is no current read; it is consulted only for a vanished lane
    beside a key-colliding writer (#852).
    """

    def __init__(self, state_path, *, mr: Client = None, client: Client = None,
                 printer_id: str | None = None, clock: Callable[[], float] = time.time,
                 printer_state_fn: Callable[..., str] = None,
                 apply_fn: Callable[..., Any] = None,
                 runout_context_fn: Callable[[], dict[str, Any] | None] = None,
                 runout_feed_fn: Callable[..., bool] = None,
                 presence_fn: Callable[[], dict[int, bool | None] | None] = None,
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
        self.presence_fn = presence_fn
        # The hardware record / deferred-screen files the reconcile writes;
        # None keeps spool_sync's own per-printer defaults.
        self.state_file = Path(state_file) if state_file else None
        self.pending_file = Path(pending_file) if pending_file else None
        self._print_state: str | None = None
        self._presence: dict[int, bool | None] | None = None
        self._colliding = False

    # -- injected helpers ------------------------------------------------

    def _print_state_now(self) -> str:
        if self._print_state is None:
            try:
                self._print_state = str(self.printer_state_fn(self.mr))
            except Exception:
                self._print_state = "unknown"
        return self._print_state

    def _slot_presence(self, slot: int) -> bool | None:
        """The printer's presence for one slot this poll: True, False, or None (unknown)."""
        if self._presence is None:
            slots = None
            if self.presence_fn is not None:
                try:
                    slots = self.presence_fn()
                except Exception:
                    slots = None
            self._presence = slots if isinstance(slots, dict) else {}
        value = self._presence.get(int(slot))
        return value if isinstance(value, bool) else None

    def _lookup_via_list(self, spool_id: int) -> tuple[str, dict[str, Any] | None]:
        """Confirm a spool's fate from a successful spool list.

        The production transport wraps every failure — including a deleted
        spool's 404 — in SpoolError, so a single-spool GET cannot tell
        "gone" from "Spoolman is down". A list that answers settles it: the
        spool is either in it (live/archived) or genuinely missing. A list
        that does not answer proves nothing (unknown).
        """
        try:
            rows = spool_sync._call_client(self.client, "GET",
                                           "/api/v1/spool?allow_archived=true", None)
        except Exception:
            return "unknown", None
        if not isinstance(rows, list):
            return "unknown", None
        for row in rows:
            if isinstance(row, dict) and row.get("id") is not None \
                    and int(row["id"]) == int(spool_id):
                return ("archived", row) if row.get("archived") else ("live", row)
        return "missing", None

    def _lookup_spool(self, spool_id: int) -> tuple[str, dict[str, Any] | None]:
        """("live" | "archived" | "missing" | "unknown", spool record)."""
        try:
            row = spool_sync._call_client(self.client, "GET", f"/api/v1/spool/{int(spool_id)}", None)
        except KeyError:
            return "missing", None  # a 404-shaped answer: the record is gone
        except spool_sync.SpoolError:
            # The production transport raises this for a 404 (deleted spool)
            # and for a genuine outage alike: confirm through the list.
            return self._lookup_via_list(spool_id)
        except Exception:
            return "unknown", None
        if not isinstance(row, dict) or row.get("id") is None:
            return "missing", None
        return ("archived", row) if row.get("archived") else ("live", row)

    def _already_converged(self, slot: int, spool_id: int, spool: dict[str, Any]) -> bool:
        """True when Spoolman already claims this spool for this slot (#1063).

        ``ph_slot`` is Print Handy's own assignment field: a lane whose
        spool already claims (this printer, this slot) through it names a
        state Print Handy's bookkeeping already holds, so the lane record
        can only be the echo of a Print Handy write (bind_slot writes the
        claim and the lane record together). No derivable printer id
        means no claim can be checked, and the lane is treated as a
        screen change exactly as before.
        """
        printer = self.printer_id or spool_sync._default_printer_id()
        if not printer:
            return False
        return spool_sync.spool_ph_slot(spool) == (printer, int(slot))

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
        """A settled change supersedes an earlier mid-print or unverified note for the same slot."""
        if noted.pop(str(slot), None) is not None:
            for key in [k for k, entry in pending.items()
                        if isinstance(entry, dict)
                        and (entry.get("event") or {}).get("kind")
                        in ("noted_mid_print", "unverified_unload")
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
        self._presence = None
        raw = spool_sync.read_lane_data(self.mr)
        lanes = parse_lane_data(raw)
        if lanes is None:
            return []  # unknown, never empty: change nothing at all
        state = _read_json(self.state_path)
        fingerprints = {slot: lane_fingerprint(lane) for slot, lane in lanes.items()}
        foreign = foreign_lane_slots(raw)
        # Sticky: ZMod deletes its keys as slots go to type "?", so the
        # evidence can vanish in the very poll that matters (#852).
        self._colliding = bool(foreign) or state.get("colliding_author") is True
        if not state.get("seeded") or not isinstance(state.get("lanes"), dict):
            _write_json(self.state_path,
                        {"seeded": True,
                         "lanes": {str(slot): fp for slot, fp in sorted(fingerprints.items())},
                         "pending": {}, "noted": {}, "colliding_author": self._colliding})
            return []
        seen: dict[int, dict[str, Any]] = {}
        for key, value in state["lanes"].items():
            if str(key).isdigit() and isinstance(value, dict):
                seen[int(key)] = value
        pending = state.get("pending") if isinstance(state.get("pending"), dict) else {}
        noted = state.get("noted") if isinstance(state.get("noted"), dict) else {}
        dirty = False
        for slot in sorted(set(seen) | set(lanes)):
            if slot not in lanes and slot in foreign:
                continue  # another writer holds the key (#852): unknown, not unloaded
            if slot in lanes and str(noted.get(str(slot)) or "").startswith("unverified:"):
                # The record is back: drop the note and any undelivered warning for it, so a
                # later vanish is named again and an obsolete warning is never retried.
                self._drop_noted(pending, noted, slot)
                dirty = True
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
                   "pending": pending, "noted": noted, "colliding_author": self._colliding}
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

        # -- the record cannot be trusted to name its slot (#1304) ---------
        # Caught before the cleared-lane branch on purpose: a clear-shaped
        # (unload) record that fits neither keying is as self-contradicting
        # as a spool-bearing one, and unloading a slot from it could empty
        # the wrong slot. Fail closed: name it, change nothing.
        issue = lane.get("slot_issue") if lane is not None else None
        if lane is not None and issue:
            # Any pending mid-print or unverified note for this slot
            # promised a converge this record now blocks: withdraw it.
            self._drop_noted(pending, noted, slot)
            if issue == "conflict":
                claims = "; ".join(
                    f"`{c['key']}` ({'ZMod' if c['keying'] == 'orca' else 'screen'} keying) "
                    + (f"names spool #{c['spool_id']}" if c["spool_id"] is not None
                       else "names no spool")
                    for c in lane.get("claims") or [])
                self._queue(pending, noted, slot, "slot_conflict",
                            f"**HelixScreen lane needs attention:** two lane records describe "
                            f"{_slot(slot)} and they differ — {claims}. Nothing was changed: "
                            "no slot was rebound from either. Remove the stale record at the "
                            "printer, or relink the slot with "
                            f"`/spool change {slot} <spool#>`.",
                            action, spool_id=None)
            else:
                key = str(lane.get("key") or f"lane{slot}")
                key_n = int(key[4:]) if key[4:].isdigit() else slot
                inner_slot = lane.get("lane_slot")
                screen_meaning = _slot(key_n) if key_n in spool_sync.SLOTS else "no slot"
                orca_meaning = (_slot(key_n + 1) if key_n + 1 in spool_sync.SLOTS
                                else "no slot")
                spool_part = (f" naming spool #{lane['spool_id']}"
                              if lane.get("spool_id") is not None else "")
                self._queue(pending, noted, slot, "slot_disagreement",
                            f"**HelixScreen lane needs attention:** the lane record under "
                            f"`{key}`{spool_part} says in its own lane field that it describes "
                            f"{_slot(inner_slot)}, but `{key}` is {screen_meaning} in the "
                            f"screen keying and {orca_meaning} in ZMod's, so Print Handy "
                            "cannot tell which slot it describes. Nothing was changed: no "
                            "slot was rebound from it. Correct the record at the printer, or "
                            "relink the slot it really describes with "
                            f"`/spool change {inner_slot} <spool#>`.",
                            action, spool_id=lane.get("spool_id"))
            adopt()
            return True

        # -- cleared lane --------------------------------------------------
        if lane is None or (lane["spool_id"] is None and not lane["material"] and not lane["color"]):
            if not old_fp or old_fp.get("spool_id") is None or old_fp.get("slot_issue"):
                # Nothing was recorded loaded — or what was recorded was an
                # untrusted record nothing converged from (#1304): a silent clear.
                adopt()
                return True
            if lane is None and self._colliding:
                blocked = self._vanished_unload_blocked(slot, old_fp, pending, noted)
                if blocked is not None:
                    return blocked
            if self._defer_mid_print(slot, action, pending, noted,
                                     f"{_slot(slot)} was unloaded"):
                return True
            try:
                self._apply(slot, None)
            except Exception:
                return False  # not adopted: a later poll retries the converge
            self._drop_noted(pending, noted, slot)
            self._queue(pending, noted, slot, "unloaded",
                        f"**HelixScreen change:** {_slot(slot)} was unloaded at the printer "
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
                        f"**HelixScreen change:** {_slot(slot)} shows {what} loaded at the "
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
                        f"**HelixScreen lane needs attention:** {_slot(slot)} on the screen is "
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
                        f"**HelixScreen lane needs attention:** {_slot(slot)} — {named}. "
                        "Nothing was overwritten: a value declared on the screen outranks "
                        "Print Handy. Fix the locked value on the screen, or relink the "
                        f"slot with `/spool change {slot} <spool#>`.",
                        action, spool_id=spool_id)
            adopt()
            return True
        filament = spool.get("filament") or {}
        lane_material = _norm_text(lane["material"])
        spool_material = _norm_text(filament.get("material"))
        if lane_material and spool_material and not _same_base_material(lane_material, spool_material):
            self._queue(pending, noted, slot, "identity_conflict",
                        f"**HelixScreen lane needs attention:** {_slot(slot)} on the screen names "
                        f"spool #{spool_id} as {lane['material']}, but Spoolman records that "
                        f"spool as {filament.get('material')}. Nothing was rebound. Check which "
                        f"spool is really in the slot, then relink it with "
                        f"`/spool change {slot} <spool#>`.",
                        action, spool_id=spool_id)
            adopt()
            return True

        # -- Print Handy's own bind, already converged (#1063) ---------------
        if self._already_converged(slot, spool_id, spool):
            # The lane record is the echo of a bind Print Handy itself made
            # (a Discord answer, /spool change): bind_slot wrote both the
            # lane record and this spool's ph_slot claim, so Print Handy's
            # records already say exactly what the screen says. Adopt
            # silently — no announcement, no mid-print note deferring
            # bookkeeping that already happened, and never attribute the
            # change to a separate screen action.
            self._drop_noted(pending, noted, slot)
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
                                    f"{_slot(slot)} at the printer screen while a print is paused "
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
                            f"was loaded into {_slot(slot)} at the printer screen during a paused "
                            "runout. It was fed into the runout recovery: the slot is bound to "
                            "it and filament usage is charged to it again.",
                            action, spool_id=spool_id)
                adopt()
                return True

        # -- mid-print: note once, apply when the print ends -----------------
        if self._defer_mid_print(slot, action, pending, noted,
                                  f"spool #{spool_id} was loaded into {_slot(slot)}"):
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
                    f"**HelixScreen change:** {_slot(slot)} now holds spool #{spool_id} "
                    f"({_spool_label(spool)}), loaded at the printer screen.{swap} "
                    "Print Handy's records were updated from Spoolman to match.",
                    action, spool_id=spool_id)
        adopt()
        return True

    def _vanished_unload_blocked(self, slot: int, old_fp: dict[str, Any],
                                 pending: dict[str, Any], noted: dict[str, Any]) -> bool | None:
        """Cross-check a vanished spool-bearing lane against printer presence (#852).

        Called only once a key-colliding writer (ZMod ``orca_lanes``) has
        been seen in the namespace, and only for a record that disappeared
        — an explicit empty screen record cannot come from ZMod, so it
        stays a screen clear. Returns None when the unload may converge as
        a #791 screen clear (the printer reports the slot empty); otherwise
        the ``_process_lane`` result, with the baseline left un-adopted so
        each later poll re-checks:

        - presence says loaded: the absence is the collision, not an
          unload. Nothing is written; the change is named once as
          unverified, and converges by itself if the slot later reads empty.
        - presence unknown (no current read, a failed read, the slot
          unreported): nothing changes and nothing is said. Waiting for the
          absence to persist across N polls was rejected: a ZMod delete
          persists for as long as the slot stays type ``?``, so persistence
          cannot tell it from a clear. Presence is re-read on every listener
          tick and a failing read is already logged there, so the next
          readable answer settles the slot.
        """
        present = self._slot_presence(slot)
        if present is False:
            return None
        if present is None:
            return False
        marker = f"unverified:{_action_key(None)}"
        if noted.get(str(slot)) == marker:
            return False  # already named: stay silent and keep waiting
        noted[str(slot)] = marker
        spool_id = old_fp.get("spool_id")
        self._queue(pending, noted, slot, "unverified_unload",
                    f"**HelixScreen change not verified:** {_slot(slot)}'s lane record "
                    f"(spool #{spool_id}) disappeared from Moonraker `lane_data`, but the "
                    f"printer still reports filament loaded in {_slot(slot)}. ZMod's OrcaSlicer "
                    "lane sync writes the same namespace and can delete Print Handy's record, "
                    "so this was not treated as an unload: nothing was changed. If the slot "
                    "really was emptied, Print Handy unloads it once the printer reports it "
                    f"empty; to record another spool, use `/spool change {slot} <spool#>`.",
                    marker, spool_id=spool_id)
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
