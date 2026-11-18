"""Publish Print Handy cost records to Moonraker for other tools (#1643).

When Print Handy and HelixScreen run on the same printer, Print Handy is
the source of truth for print cost: it already computes a pre-print
estimate and freezes the actual cost on the job record at completion
(#1625). This module writes both figures into a Moonraker database
namespace HelixScreen can read, so the screen shows the same number as
the chat and the Web UI instead of computing its own. HelixScreen keeps
its own calculation wherever no record exists.

The namespace name and record shape below are PROPOSED, pending
agreement on prestonbrown/helixscreen#2132. Print Handy adapts to what
the HelixScreen maintainer prefers; until that agreement lands this
ships as a draft-side implementation of our own proposal (docs/COST.md,
"Sharing cost with HelixScreen").

Moonraker database rules this design follows (Moonraker's Database
Management API docs, checked 2026-11-17):

- Each client defines its own namespace; a POST creates the namespace
  when it does not exist and overwrites an existing key. Moonraker
  reserves several namespaces for internal use (among them
  ``moonraker``, ``gcode_metadata`` and ``update_manager``); clients may
  read those but not modify them. ``print_handy_cost`` is none of them,
  and nothing here ever writes outside it (the ``spoolman`` namespace
  included).
- A key given as a string treats "." as a delimiter into nested fields,
  which would shred a file name like ``benchy.gcode``. A key given as an
  array of strings treats each element as one literal field, and the
  docs name exactly this case: "useful for scenarios where a namespace
  contains fields that include a '.' character, such as a file name."
  The key here is therefore the one-element array ``[file name]``, with
  the file name exactly as Moonraker reports it, path included, so a
  name with "/" stays one literal field too.

Publishing is best effort, always: a failed or slow write is logged and
swallowed, and never blocks, delays or fails a print send or a
completion flow. Printers that are not Moonraker-backed never call this
module, so for them absence is normal, not an error. There is no
setting: whenever a cost record exists for a Moonraker-backed printer,
it is published.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from .. import moonraker as _transport

logger = logging.getLogger(__name__)

# The proposed namespace and record schema version. Readers fail soft on
# a schema newer than they know: they keep their own calculation.
NAMESPACE = "print_handy_cost"
SCHEMA = 1
DATABASE_PATH = "/server/database/item"

# The engine's human price-source labels (core.cost.resolve_price) mapped
# to stable codes for machine readers. The label itself is carried too.
_SOURCE_CODES = (
    ("spool price (Spoolman spool", "spoolman_spool"),
    ("filament profile price (Spoolman)", "spoolman_filament"),
    ("bottle price by volume (Spoolman)", "spoolman_bottle"),
    ("slicer-embedded price", "slicer"),
    ("manual price", "manual"),
    ("workspace default cost per kilogram", "default"),
)


def price_source_code(label: Any) -> str:
    """The stable price-source code for one engine price-source label."""
    text = str(label or "")
    for prefix, code in _SOURCE_CODES:
        if text.startswith(prefix):
            return code
    return "unknown"


def state_for(cost: Any) -> str:
    """The record lifecycle state for a cost dict: estimate or final.

    The engine marks a pre-print quote with outcome "estimate"; a cost
    frozen at completion carries the print's outcome (completed, failed
    or cancelled) and is final.
    """
    outcome = str((cost or {}).get("outcome") or "estimate")
    return "estimate" if outcome == "estimate" else "final"


def _amount(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if out == out and out not in (float("inf"), float("-inf")) else 0.0


def build_record(filename: Any, cost: Any, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The Moonraker record for one file from a cost dict, or None.

    ``cost`` is the engine's plain-data shape: an estimate result or the
    frozen per-job record (``core.cost.freeze``), never recomputed here.
    The per-tool lines are the engine's per-material lines, each with its
    price source label and stable source code.
    """
    if not isinstance(cost, dict) or not str(filename or ""):
        return None
    tools = []
    for entry in cost.get("material_lines") or []:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("price_source") or "")
        tools.append({
            "name": str(entry.get("name") or ""),
            "material": str(entry.get("material") or ""),
            "color": str(entry.get("color") or ""),
            "grams": _amount(entry.get("grams")),
            "volume_ml": _amount(entry.get("volume_ml")),
            "amount": _amount(entry.get("amount")),
            "price_source": label,
            "price_source_code": price_source_code(label),
            "spool_id": entry.get("spool_id"),
        })
    lines = [{"key": str(entry.get("key") or ""), "label": str(entry.get("label") or ""),
              "amount": _amount(entry.get("amount"))}
             for entry in cost.get("lines") or [] if isinstance(entry, dict)]
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema": SCHEMA,
        "state": state_for(cost),
        "file": str(filename),
        "outcome": str(cost.get("outcome") or "estimate"),
        "out_of_pocket": _amount(cost.get("out_of_pocket")),
        "full_cost": _amount(cost.get("cost_per_build")),
        "cost_per_part": _amount(cost.get("cost_per_part")),
        "parts_per_build": int(_amount(cost.get("parts_per_build")) or 1),
        "currency": str(cost.get("currency_code") or "USD"),
        "tools": tools,
        "lines": lines,
        "updated_at": stamp,
    }


def _post(base_url: str, payload: dict[str, Any]) -> Any:
    """One database POST through the one Moonraker transport (#1466)."""
    return _transport.moonraker_json(base_url, DATABASE_PATH, method="POST",
                                     data=payload, timeout=5)


def publish(base_url: Any, filename: Any, cost: Any, *,
            post: Callable[[str, dict[str, Any]], Any] | None = None,
            now: datetime | None = None) -> bool:
    """Write one file's cost record to the Moonraker database. Best effort.

    Returns True only when the write went out. No Moonraker base URL (a
    printer that is not Moonraker-backed) or no cost record is a normal
    no-op, and any failure is logged and swallowed: publishing never
    blocks, delays or fails the send or completion that called it.
    """
    record = build_record(filename, cost, now=now)
    if record is None or not str(base_url or ""):
        return False
    payload = {"namespace": NAMESPACE, "key": [record["file"]], "value": record}
    slot = (str(base_url), record["file"])
    # Both writes run on background threads, so they can finish out of order:
    # writes for one file are serialized, and once a final record went out an
    # estimate for that file is skipped, so a late estimate can never replace
    # the frozen cost (#1652 review). A later send of the same file starts a
    # new print and clears the mark.
    with _file_lock(slot):
        if record["state"] == "estimate" and slot in _FINAL_SENT and not _new_send(slot, record):
            logger.info("cost publish: estimate for %s skipped; its final cost is already published",
                        record["file"])
            return False
        try:
            (post or _post)(str(base_url), payload)
        except Exception as exc:  # best effort by design; the print never waits on this
            logger.warning("cost publish to Moonraker failed for %s: %s", record["file"], exc)
            return False
        if record["state"] == "final":
            _FINAL_SENT[slot] = record["updated_at"]
        else:
            _FINAL_SENT.pop(slot, None)
    return True


# (moonraker url, file) -> updated_at of the final record published for it.
_FINAL_SENT: dict[tuple[str, str], str] = {}
_FILE_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _file_lock(slot):
    with _FILE_LOCKS_GUARD:
        return _FILE_LOCKS.setdefault(slot, threading.Lock())


def _new_send(slot, record) -> bool:
    """True when this estimate is for a later send than the published final.

    The send path stamps an estimate at the moment of the send, before its
    background thread prices it, so a stale estimate always carries a time
    at or before the final's; one stamped after it is a new print of the
    same file and must be published.
    """
    return str(record.get("updated_at") or "") > str(_FINAL_SENT.get(slot) or "")
