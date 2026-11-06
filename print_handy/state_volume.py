"""Which state volume an install is using (#914).

The main state volume was ``/data/hermes`` until 2.2.5; its neutral name is
``/data/state``. Nothing is renamed in place: the container entrypoint
keeps a legacy install working by linking ``/data/state`` to the legacy
folder when only the legacy volume holds data, and an explicit
``paths.hermes_home`` pointing at the old path is honored unchanged. So
which volume is in use is a fact about the filesystem, not just about
configuration — this module reads that fact, for the doctor row and the
container healthcheck. The legacy name stays accepted for at least two
minor releases after 2.2.5.
"""
from __future__ import annotations

from pathlib import Path

NEW_NAME = "state"
LEGACY_NAME = "hermes"
DEPRECATION = ("the legacy /data/hermes name is deprecated but stays "
               "accepted for at least two minor releases")


def _has_files(folder: Path) -> bool:
    """Any file under folder; seeded empty subfolders alone are not data."""
    try:
        return folder.is_dir() and any(entry.is_file() for entry in folder.rglob("*"))
    except OSError:
        return False


def describe_state_volume(home) -> str:
    """One line naming the configured data folder and the volume in use."""
    home = Path(home).expanduser()
    if home.is_symlink():
        return (f"{home} -> {home.resolve()} (legacy /data/hermes volume in use "
                f"through the /data/state symlink; {DEPRECATION})")
    if home.name == LEGACY_NAME:
        return (f"{home} (legacy /data/hermes volume in use; rename the host "
                f"folder to 'state' and map /data/state to adopt the neutral "
                f"name — {DEPRECATION})")
    legacy = home.parent / LEGACY_NAME
    if home.name == NEW_NAME and _has_files(home) and _has_files(legacy):
        return (f"{home} (both /data/state and the legacy /data/hermes volumes "
                f"hold data; /data/state wins and /data/hermes is ignored)")
    return str(home)
