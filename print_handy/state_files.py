"""Find and remove Print Handy state files that live outside the profile state folder.

    python3 -m print_handy.state_files [--dry-run] [--remove]

Reports by default. --remove deletes a stray only when the real copy exists in
<hermes_home>/state, or the stray holds nothing (empty file, {}, [], empty folder). Anything else is
kept and reported. --dry-run with --remove lists what would be removed and touches nothing.

Only fixed places are scanned, never a recursive walk of the home folder:
the Hermes root state folder, the state folders of other profiles, the legacy dotfiles in $HOME,
and print_bitch_* names left in the configured state folder by the rename.
"""
from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import settings

# Every state file or folder name the code uses under <hermes_home>/state. tests/test_state_files.py
# greps the source and fails when a name appears there that is missing here.
KNOWN = (
    "bot_health.json",
    "calibration_records.json",
    "control.lock",
    "family_listener.json",
    "gcode-bounds-cache",
    "ifs_pending_prompts.json",
    "ifs_presence_edges.json",
    "jobs.db",
    "lane_data_watch.json",
    "last_bot_cancel.json",
    "lifecycle_notifications.json",
    "listener_health.json",
    "live_session.json",
    "maintenance.json",
    "model-link-intakes",
    "moonraker_discord_announcements.json",
    "native_error_alerted.json",
    "next_plate_offer.json",
    "orca-profiles",
    "print-next-claims",
    "print_alert_buttons.json",
    "print_handy_context.json",
    "print_handy_launches.sqlite3",
    "print_handy_start_claims.json",
    "print_handy_start_messages.json",
    "print_markers",
    "print_numbers.json",
    "printer_hardware.json",
    "printers",
    "recent_model_links.json",
    "reprint_queue.json",
    "roast_state.json",
    "runout_pause.json",
    "screen_sync_pending.json",
    "spoolman_known_spools.json",
    "start-recordings",
    "stops",
    "timelapse_plans",
    "webui-inbox",
    "webui_upload_links.json",
)

# Files the cutover (#45) moved from $HOME into the profile state folder.
LEGACY_HOME = {
    ".print_bitch_context.json": "print_handy_context.json",
    ".print_bitch_start_claims.json": "print_handy_start_claims.json",
    ".moonraker_discord_announcements.json": "moonraker_discord_announcements.json",
}

OLD_PREFIX, NEW_PREFIX = "print_bitch", "print_handy"
EMPTY_TEXT = {"", "{}", "[]", "null"}


@dataclass(frozen=True)
class Stray:
    path: Path
    reason: str
    canonical: Path
    shared: bool = False  # in a folder another profile may own: only empty or identical copies are deleted
    report_only: bool = False  # another profile's own state: never deleted, only reported


def _present(path):
    return path.is_symlink() or path.exists()


def _contained(path, folder):
    """True when path (symlinks resolved) is the folder or lives inside it."""
    try:
        real = path.resolve()
        base = folder.resolve()
    except OSError:
        return False
    return real == base or base in real.parents


def _scan(folder, state, reason, report_only=False):
    """Known names directly inside folder, unless folder is the configured state folder."""
    if not folder.is_dir() or _contained(folder, state):
        return
    for name in KNOWN:
        path = folder / name
        if _present(path) and not _contained(path, state):
            yield Stray(path, reason, state / name, shared=True, report_only=report_only)


def find_strays(config=None):
    config = config or settings.current()
    state = settings.state_dir(config)
    hermes_home = Path(config["paths"]["hermes_home"])
    found = []
    # The Hermes root and sibling profiles, only when the configured profile is <root>/profiles/<name>.
    if hermes_home.parent.name == "profiles":
        root = hermes_home.parent.parent
        found += _scan(root / "state", state, "Hermes root state folder")
        for profile in sorted(hermes_home.parent.iterdir()):
            if profile.is_dir() and not profile.is_symlink() and profile.resolve() != hermes_home.resolve():
                # A sibling may be another Print Handy install with its own state: report, never delete.
                found += _scan(profile / "state", state, f"state folder of profile {profile.name}", report_only=True)
    # Legacy dotfiles in $HOME that the cutover moved into the profile.
    home = Path.home()
    for old, new in LEGACY_HOME.items():
        if _present(home / old):
            found.append(Stray(home / old, "legacy file in the home folder", state / new))
    # print_bitch_* leftovers from the rename, next to their print_handy_* replacements.
    for name in KNOWN:
        if NEW_PREFIX not in name:
            continue
        old = state / name.replace(NEW_PREFIX, OLD_PREFIX)
        if _present(old):
            found.append(Stray(old, "old name left by the rename", state / name))
    return found


def _empty(path):
    if path.is_symlink():
        return False
    if path.is_dir():
        return not any(path.iterdir())
    try:
        if path.stat().st_size <= 2:
            return True
        return path.read_text(errors="replace").strip() in EMPTY_TEXT
    except OSError:
        return False


def _identical(path, canonical):
    """Same bytes as the profile copy (files only; a folder never counts)."""
    if path.is_symlink() or not path.is_file() or not canonical.is_file():
        return False
    try:
        return path.read_bytes() == canonical.read_bytes()
    except OSError:
        return False


def _delete(path):
    if path.is_symlink() or path.is_file():
        path.unlink()  # a symlink is unlinked, never followed
    else:
        shutil.rmtree(path)


def remove_strays(strays, dry_run=False):
    """Delete strays that are safe to drop. Returns (removed, kept) lists of (Stray, note)."""
    removed, kept = [], []
    for stray in strays:
        if stray.report_only:
            kept.append((stray, "kept: another profile's state; check by hand"))
            continue
        if _empty(stray.path):
            note = "empty"
        elif stray.shared and _identical(stray.path, stray.canonical):
            note = "same as profile copy"
        elif stray.shared and stray.path.is_symlink() and _present(stray.canonical):
            note = "symlink; profile copy exists"  # unlinking a link loses no data
        elif stray.shared:
            why = "differs from the profile copy" if _present(stray.canonical) else "no profile copy"
            kept.append((stray, f"kept: {why}; another profile may own it, check by hand"))
            continue
        elif _present(stray.canonical) and not _contained(stray.path, stray.canonical.parent):
            note = "profile copy exists"
        else:
            kept.append((stray, "kept: no profile copy"))
            continue
        try:
            if not dry_run:
                _delete(stray.path)
        except OSError as exc:
            kept.append((stray, f"kept: {exc}"))
            continue
        removed.append((stray, note))
    return removed, kept


def describe(strays):
    return ", ".join(str(s.path) for s in strays)


def main(argv=None, out=sys.stdout):
    args = list(sys.argv[1:] if argv is None else argv)
    if [a for a in args if a not in ("--dry-run", "--remove")]:
        print("usage: python3 -m print_handy.state_files [--dry-run] [--remove]", file=sys.stderr)
        return 2
    strays = find_strays()
    if not strays:
        print("No stray Print Handy state files.", file=out)
        return 0
    if "--remove" not in args:
        for s in strays:
            print(f"stray {s.path} ({s.reason}; profile copy would be {s.canonical})", file=out)
        return 0
    dry = "--dry-run" in args
    removed, kept = remove_strays(strays, dry_run=dry)
    for s, note in removed:
        print(f"{'would remove' if dry else 'removed'} {s.path} ({note})", file=out)
    for s, note in kept:
        print(f"{note} {s.path}", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
