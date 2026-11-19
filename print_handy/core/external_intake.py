"""Opt-in watched import directory for external sliced jobs (#1525, H).

A Print Handy server cannot magically watch a user's desktop slicer.
This is the explicit, opt-in version: one configured directory, nothing
else. The watcher only *notices* files:

* config-gated: scanning does nothing unless the operator enabled it
  and named the one allowlisted directory;
* a file becomes a draft job card only after it is stable (same size
  and mtime across two scans) and under the size limit;
* deduplication is by content hash plus path, so a re-scan or a copied
  file never makes a second card;
* a card is a draft: it never prints, never sends a printer command
  and never charges a spool.

Accounting enters only through `confirm_mark_printed`, the operator's
explicit declaration that a physical print happened. It charges through
the caller's existing accounting callback exactly once per print
identity: the identity is recorded in a ledger set the caller persists,
and a Print Handy-launched print whose identity is already in the
ledger is refused as already recorded — the same physical print can
never be charged twice.
"""
from __future__ import annotations

import hashlib
from threading import Lock
from pathlib import Path

from . import source_descriptor

DEFAULT_MAX_BYTES = 512 * 1024 * 1024
# identity -> [lock, holders]; an entry lives only while a confirmation of
# that identity is running, so the table never grows with print history.
_CONFIRM_LOCKS: dict[str, list] = {}
_CONFIRM_LOCKS_GUARD = Lock()
ACCEPTED_SUFFIXES = (".3mf", ".gcode", ".gco")


class IntakeRefused(RuntimeError):
    """An intake or confirmation the watcher will not perform."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def scan(directory, *, enabled=False, max_bytes=DEFAULT_MAX_BYTES,
         state=None) -> tuple[list[dict], dict]:
    """Draft job cards for newly stable files in the one watched folder.

    ``state`` is the caller's persisted scan state (path -> size/mtime
    sightings plus seen hashes); pass the returned state back on the
    next scan. Returns (new draft cards, new state). With ``enabled``
    false, or a directory that was never configured, this is a no-op.
    """
    state = {"seen": {}, "hashes": {}} if state is None else state
    if not enabled:
        return [], state
    root = Path(directory) if directory else None
    if root is None or not root.is_dir():
        return [], state
    root = root.resolve()
    cards = []
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.suffix.lower() not in ACCEPTED_SUFFIXES:
            continue
        try:
            resolved = path.resolve()
            if resolved.parent != root:
                continue  # links out of the allowlisted folder are not intake
            stat = path.stat()
        except OSError:
            continue
        key = str(path)
        sighting = {"size": stat.st_size, "mtime": int(stat.st_mtime)}
        previous = state["seen"].get(key)
        state["seen"][key] = sighting
        if previous != sighting:
            continue  # not stable yet: first sighting, or still changing
        if stat.st_size > max_bytes:
            continue
        digest = _sha256(path)
        dedupe_key = f"{digest}:{key}"
        if dedupe_key in state["hashes"]:
            continue
        state["hashes"][dedupe_key] = True
        descriptor = (source_descriptor.describe(path)
                      if path.suffix.lower() == ".3mf" else
                      {"kind": "gcode-only", "user_class": "sliced-only",
                       "problems": []})
        cards.append({"schema_version": 1, "kind": "external-draft",
                      "path": key, "sha256": digest, "size": stat.st_size,
                      "source_kind": descriptor.get("kind"),
                      "source_class": descriptor.get("user_class"),
                      "status": "planned",
                      "note": "Draft only: nothing was printed, sent or "
                              "charged. Confirm a physical print explicitly "
                              "to record accounting."})
    return cards, state


def print_identity(card: dict, printer_id: str, print_id: str) -> str:
    """The ledger identity of one physical print of an imported file."""
    return f"external:{card.get('sha256')}:{printer_id}:{print_id}"


def confirm_mark_printed(card: dict, *, printer_id: str, print_id: str,
                         ledger: set, charge) -> dict:
    """Record one operator-declared physical print, exactly once.

    ``ledger`` is the caller's persisted set of accounting identities
    (the same ledger discipline `multislot_usage` uses). ``charge`` is
    the existing accounting entry point, called with the identity; it
    is never called when the identity is already recorded — including
    when a Print Handy-launched print already charged this file on this
    printer under this print id.
    """
    identity = print_identity(card, printer_id, print_id)
    entry = _hold_confirm_lock(identity)
    try:
        with entry[0]:
            if identity in ledger:
                raise IntakeRefused(
                    f"print {print_id} of this file on {printer_id} is already "
                    "recorded; refusing to charge the same physical print twice.")
            if card.get("status") == "printed":
                raise IntakeRefused(
                    "this draft card was already confirmed as printed; a reprint "
                    "needs its own print id.")
            result = charge(identity=identity, card=card, printer_id=printer_id)
            ledger.add(identity)
            return {"identity": identity, "status": "printed",
                    "accounting": result}
    finally:
        _release_confirm_lock(identity, entry)


def _hold_confirm_lock(identity: str) -> list:
    """The identity's lock entry, counted as held by this caller (#1682)."""
    with _CONFIRM_LOCKS_GUARD:
        entry = _CONFIRM_LOCKS.get(identity)
        if entry is None:
            entry = _CONFIRM_LOCKS[identity] = [Lock(), 0]
        entry[1] += 1
        return entry


def _release_confirm_lock(identity: str, entry: list) -> None:
    """Drop the entry once no confirmation of the identity holds it."""
    with _CONFIRM_LOCKS_GUARD:
        entry[1] -= 1
        if entry[1] == 0 and _CONFIRM_LOCKS.get(identity) is entry:
            del _CONFIRM_LOCKS[identity]
