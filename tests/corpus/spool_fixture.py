"""Build a printer-state fixture by running the real create_spool and bind_slot flows (#594).

The corpus used to slice from a handwritten printer_hardware.json. Here the slot map is produced by
the production spool code against an in-memory Spoolman and Moonraker, so a change to how spools are
created or bound changes what the slicing journeys see.
"""
from __future__ import annotations

import json
from pathlib import Path

from print_handy.hermes import spool_sync

SPOOLS = (  # slot, brand, material, color, grams (the text /spool new accepts)
    (1, "Corpus", "PLA", "White", 1000),
    (2, "Corpus", "PLA", "Pink", 1000),
    (3, "Corpus", "PLA", "Black", 1000),
    (4, "Corpus", "PLA", "Dark Green", 1000),
)


class FakeSpoolman:
    """The Spoolman REST surface create_spool and bind_slot use, kept in memory."""

    def __init__(self, fail_first=0):
        self.vendors, self.filaments, self.spools, self.calls = [], [], [], []
        self.fail_first = fail_first  # simulate a Spoolman that answers 503 while restarting

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        if self.fail_first > 0:
            self.fail_first -= 1
            raise spool_sync.SpoolError("Spoolman answered 503 (restarting)")
        parts = path.strip("/").split("/")[2:]
        table = {"vendor": self.vendors, "filament": self.filaments, "spool": self.spools}[parts[0]]
        if method == "GET" and len(parts) == 1:
            return list(table)
        if method == "GET":
            return next((dict(r) for r in table if r["id"] == int(parts[1])), None)
        if method == "POST":
            row = {**body, "id": len(table) + 1}
            if parts[0] == "spool":
                row["filament"] = next(f for f in self.filaments if f["id"] == body["filament_id"])
            if parts[0] == "filament" and body.get("vendor_id"):
                row["vendor"] = next(v for v in self.vendors if v["id"] == body["vendor_id"])
            table.append(row)
            return dict(row)
        if method == "PATCH":
            row = next(r for r in table if r["id"] == int(parts[1]))
            row.update(body)
            return dict(row)
        raise AssertionError(f"unexpected Spoolman call {method} {path}")


def fake_moonraker(method, path, body=None):
    return {"result": {"status": {}}}


def build_state(root: Path, skeleton: Path, *, spoolman=None, spools=SPOOLS) -> tuple[Path, FakeSpoolman]:
    """Copy the hardware skeleton minus its handwritten slot map, then create and bind every spool."""
    spoolman = spoolman or FakeSpoolman()
    state = json.loads(skeleton.read_text())
    state["filament_slots"] = {}
    state["spool"] = {"spoolman_id": None}
    path = root / "printer_hardware.json"
    path.write_text(json.dumps(state))
    for slot, brand, material, color, grams in spools:
        made = spool_sync.create_spool(spool_sync.parse_new(f"{brand}, {material}, {color}, {grams}"), client=spoolman)
        spool_sync.bind_slot(slot, made["spool_id"], client=spoolman, mr=fake_moonraker, state_path=path,
                             push_screen_update=False)
    # slice_model also wants the active spool; that is the one in slot 1, read back from the built state.
    built = json.loads(path.read_text())
    first = built["filament_slots"].get("1")
    if first:
        built["spool"] = {"spoolman_id": first["spoolman_id"], "name": first.get("color"), "material": first.get("material"),
                          "density": 1.24, "diameter_mm": 1.75, "color_code": first.get("hex")}
        path.write_text(json.dumps(built))
    return path, spoolman
