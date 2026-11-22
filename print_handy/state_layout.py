"""The per-printer state layout, and the pre-#334 root files an upgrade must move (#1817).

Every per-printer file lives at ``<state>/printers/<id>/<name>``, the default
printer's included. Releases before 2.4 kept the default printer's files at
``<state>/<name>`` and adopted them on read; that adoption is gone. A root file
whose per-printer copy is missing is *stranded*: nothing reads it any more, so
the default printer would start empty (no loaded spools, maintenance hours
reset, a held launch lease forgotten). A root file next to its per-printer copy
is *stale*: a leftover the code never reads, safe to delete.

``root_files`` finds both kinds. The doctor reports them, and the loaders of
the files that hold real data (``stranded_error``) refuse with the exact move
instead of starting empty.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from . import settings

# (name at the state root, name under printers/<id>/, required). A required
# file holds data an upgrade must not lose, so while one is stranded the doctor
# fails and its loader refuses. The others are queues and listener memory that
# start over harmlessly; a stranded one is a warning.
ROOT_FILES = (
    ("printer_hardware.json", "printer_hardware.json", True),
    ("maintenance.json", "maintenance.json", True),
    ("print_handy_launches.sqlite3", "jobs.db", True),
    ("reprint_queue.json", "reprint_queue.json", False),
    ("screen_sync_pending.json", "screen_sync_pending.json", False),
    ("multislot_usage_charges.json", "multislot_usage_charges.json", False),
    ("spoolman_known_spools.json", "spoolman_known_spools.json", False),
    ("runout_pause.json", "runout_pause.json", False),
    ("ifs_pending_prompts.json", "ifs_pending_prompts.json", False),
    ("ifs_presence_edges.json", "ifs_presence_edges.json", False),
    ("lifecycle_notifications.json", "lifecycle_notifications.json", False),
    ("native_error_alerted.json", "native_error_alerted.json", False),
    ("lane_data_watch.json", "lane_data_watch.json", False),
    ("roast_state.json", "roast_state.json", False),
    ("family_listener.json", "family_listener.json", False),
    ("moonraker_discord_announcements.json", "moonraker_discord_announcements.json", False),
    ("stops", "stops", False),
)
# SQLite write-ahead sidecars that must travel with a database file.
_SQLITE_SIDECARS = ("-wal", "-shm")


class StrandedStateError(RuntimeError):
    """A required file is still at the pre-2.4 root; the message names the move."""


@dataclass(frozen=True)
class RootFile:
    root: Path
    target: Path
    required: bool

    @property
    def stale(self) -> bool:
        """The per-printer copy exists: the root file is a leftover nobody reads."""
        return self.target.exists()

    def move_command(self) -> str:
        """The exact shell commands that move this file (and any SQLite sidecars) into place."""
        moves = [(self.root, self.target)]
        if self.root.suffix == ".sqlite3":
            for side in _SQLITE_SIDECARS:
                extra = self.root.with_name(self.root.name + side)
                if extra.exists():
                    moves.append((extra, self.target.with_name(self.target.name + side)))
        parts = [f"mkdir -p {shlex.quote(str(self.target.parent))}"]
        parts += [f"mv {shlex.quote(str(src))} {shlex.quote(str(dst))}" for src, dst in moves]
        return " && ".join(parts)


def root_files(state_root: Path, default_id: str) -> list[RootFile]:
    """Every pre-#334 root file present under ``state_root``, mapped to the default printer's folder."""
    state_root = Path(state_root)
    folder = state_root / "printers" / str(default_id)
    return [RootFile(state_root / old, folder / new, required)
            for old, new, required in ROOT_FILES if (state_root / old).exists()]


def for_settings(data=None) -> list[RootFile]:
    """``root_files`` for one resolved settings dict (the doctor's view)."""
    return root_files(settings.state_dir(data), settings.default_printer_id(data))


def stranded_error(state_root: Path, default_id: str, name: str) -> str | None:
    """The refusal for a required file still at the root with no per-printer copy, else None.

    ``name`` is the per-printer name (``printer_hardware.json``, ``maintenance.json``,
    ``jobs.db``). Loaders call this when the per-printer file is missing, so an
    un-moved upgrade fails loudly instead of starting the printer empty.
    """
    for entry in root_files(state_root, default_id):
        if entry.target.name == name and not entry.stale:
            return stranded_message([entry])
    return None


def stranded_message(entries: list[RootFile]) -> str:
    """One refusal naming every stranded file and the move that fixes it."""
    names = ", ".join(e.root.name for e in entries)
    commands = "; ".join(e.move_command() for e in entries)
    return (f"pre-2.4 state at the state root is no longer read ({names}); stop Print Handy "
            f"and move it into the default printer's folder: {commands}")
