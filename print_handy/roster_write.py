"""Append a printer to the [[printers]] roster in the settings file (#537).

``settings.py`` loads and validates but has no writer, and the writers that
exist cannot do this job: the setup wizard's merge is existing-keys-win (it
can create a fresh roster but never append to one, and guided setup refuses
outright when the file already describes a printer), while the Web UI write
path covers only its global catalog fields — the roster is deliberately not
one of them.

``add_printer`` follows the Web UI's write pattern, which is the strongest
in the codebase:

1. The target is the file ``settings.load()`` would read
   (``webui.config.settings_file_path``: ``$PRINT_HANDY_CONFIG``, else
   ``config/local.yaml``, else the legacy ``local.toml``). A TOML target is
   refused — every writer in the codebase is YAML-only.
2. The existing file must round-trip through the setup wizard's strict
   parse (``_load_for_rewrite``); a file that cannot be re-emitted
   faithfully is left byte-for-byte unchanged. Like the wizard merge, a
   rewrite does not preserve comments; a timestamped backup of the previous
   file is always kept next to it.
3. The new entry is appended to the declared roster. When the file has no
   roster yet — the common case, an existing single-printer install — the
   legacy per-printer settings are first converted: the printer exactly as
   it resolves today becomes entry 1 (its connection values carried over,
   derived URLs and schema defaults left derived/defaulted), the legacy
   keys are removed from the file, and the new printer becomes entry 2.
   Without the conversion the result would be the mixed shape
   ``settings._check_mixed_shape`` refuses at startup.
4. The whole candidate is validated by writing it to a temp file and
   running the real ``settings.load()`` over it (with the caller's home
   layer and environment, so environment offenders surface with the
   loader's own message). Only then is the previous file backed up and the
   candidate written atomically.

Nothing here restarts anything: a written roster goes live at the next
service/container start, which the caller must say plainly.
"""
from __future__ import annotations

import copy
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from . import settings, setup_wizard
from .webui.config import settings_file_path

_HEADER = "# Written by Print Handy guided printer onboarding (#537). Edit freely.\n"

# Roster entry keys in the order a hand-written entry would use.
_ENTRY_ORDER = ("id", "model", "name", "chat_id", "moonraker_url", "moonraker_ws_url",
                "octoprint_url", "camera_snapshot_url", "lan_host", "lan_serial",
                "lan_access_code_env", "start_confirm_s", "link_host", "api_key_env",
                "host", "nozzle_type", "tool_count")

# The legacy per-printer keys a conversion removes from the file, per section —
# exactly the file-side list settings._check_mixed_shape refuses alongside a
# declared roster. Everything else in those sections (Spoolman, the slicer
# command, …) is global and stays.
_LEGACY_KEYS = {
    "printer": ("model", "name", "vendor", "nozzle_type", "tool_count"),
    "services": ("moonraker_url", "moonraker_ws_url", "octoprint_url", "camera_snapshot_url"),
    "bambu": ("lan_host", "lan_serial", "lan_access_code_env", "start_confirm_s"),
    "prusa": ("link_host", "api_key_env"),
    "elegoo_sdcp": ("host",),
    "flashforge_stock": ("host",),
}


class RosterWriteError(RuntimeError):
    """Adding the printer was refused; the settings file was not changed."""


def _file_entry(entry):
    """An entry as written to the file: schema keys only, canonical order,
    empty values omitted (an absent key and an empty one resolve the same,
    and the file stays readable)."""
    out = {}
    for key in _ENTRY_ORDER:
        if key not in entry:
            continue
        value = entry[key]
        if value is None or value == "":
            continue
        out[key] = value
    return out


def _converted_first_entry(resolved):
    """The file form of the printer an install already runs, from its
    *resolved* roster entry: connection values carried over verbatim;
    derived values (moonraker_ws_url, camera_snapshot_url) and schema
    defaults (the shared secret env names, start_confirm_s) left out so
    they keep deriving/defaulting exactly as they do today."""
    entry = {"id": resolved["id"], "model": resolved["model"], "name": resolved["name"]}
    if resolved.get("chat_id"):
        entry["chat_id"] = resolved["chat_id"]
    for key in ("moonraker_url", "octoprint_url", "lan_host", "lan_serial", "link_host", "host"):
        if resolved.get(key):
            entry[key] = resolved[key]
    base = str(resolved.get("moonraker_url") or "").rstrip("/")
    if base:
        derived_ws = base.replace("http", "ws", 1) + "/websocket"
        if resolved.get("moonraker_ws_url") and resolved["moonraker_ws_url"] != derived_ws:
            entry["moonraker_ws_url"] = resolved["moonraker_ws_url"]
        host = base.split("://", 1)[1].split("/", 1)[0].rsplit(":", 1)[0] if "://" in base else ""
        derived_cam = f"http://{host}/webcam/?action=snapshot" if host else ""
        if resolved.get("camera_snapshot_url") and resolved["camera_snapshot_url"] != derived_cam:
            entry["camera_snapshot_url"] = resolved["camera_snapshot_url"]
    elif resolved.get("camera_snapshot_url"):
        # No Moonraker URL means no camera URL was derived from one, so a
        # resolved camera URL can only be an explicit one; keep it, or the
        # legacy strip below removes the only copy.
        entry["camera_snapshot_url"] = resolved["camera_snapshot_url"]
    if resolved.get("lan_access_code_env") not in (None, "", "BAMBU_LAN_PASSWORD"):
        entry["lan_access_code_env"] = resolved["lan_access_code_env"]
    if resolved.get("start_confirm_s") not in (None, 90):
        entry["start_confirm_s"] = resolved["start_confirm_s"]
    if resolved.get("api_key_env") not in (None, "", "PRUSA_LINK_API_KEY"):
        entry["api_key_env"] = resolved["api_key_env"]
    for key in ("nozzle_type", "tool_count"):
        if key in resolved:
            entry[key] = resolved[key]
    return entry


def _strip_legacy(candidate):
    for section, keys in _LEGACY_KEYS.items():
        values = candidate.get(section)
        if not isinstance(values, dict):
            continue
        for key in keys:
            values.pop(key, None)
        if not values:
            candidate.pop(section, None)


def _backup(target, stamp):
    """A timestamped copy of the current file next to it (the Web UI pattern)."""
    backup = target.with_name(f"{target.name}.bak-{stamp}")
    counter = 1
    while backup.exists():
        backup = target.with_name(f"{target.name}.bak-{stamp}-{counter}")
        counter += 1
    shutil.copyfile(target, backup)
    return backup


def _validate_candidate(candidate, target, *, home, environ):
    """Run the candidate mapping through the real settings loader, written
    to a temp file the loader reads; the loader's own message on failure."""
    text = setup_wizard._dump(candidate)
    fd, tmp = tempfile.mkstemp(prefix=".roster-candidate.", suffix=".yaml",
                               dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        return settings.load(home=home, local=Path(tmp), environ=environ)
    except settings.SettingsError as exc:
        raise RosterWriteError(f"the updated settings do not validate, so nothing was "
                               f"written: {exc}") from None
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# The settings file baked into the Docker image (Dockerfile: PRINT_HANDY_CONFIG=
# /app/config/docker.yaml). It is root-owned and lives in the image, not on a
# volume, so a write fails for the container user and would not survive a
# container recreate even if it succeeded.
_IMAGE_SETTINGS = Path(__file__).resolve().parent.parent / "config" / "docker.yaml"


def _unwritable_reason(target):
    """Why ``target`` cannot take a durable roster write, or None."""
    try:
        baked = target.resolve() == _IMAGE_SETTINGS
    except OSError:
        baked = False
    if baked:
        return f"{target} is the settings file baked into the Docker image"
    if not os.access(target, os.W_OK) or not os.access(target.parent, os.W_OK):
        return f"{target} (or its folder) is not writable by this process"
    return None


def write_refusal(environ=None):
    """The refusal text when the settings file can't take a roster write, else None.

    For callers that should refuse before walking anyone through a flow
    (``/print-handy add-printer``). A missing file is not refused here:
    ``add_printer`` names that case itself (run setup first).
    """
    target = settings_file_path(os.environ if environ is None else environ)
    if target.suffix.lower() == ".toml" or not target.exists():
        return None
    reason = _unwritable_reason(target)
    return str(_refusal(reason)) if reason else None


def _refusal(reason):
    return RosterWriteError(
        f"{reason}, so the printer can't be added from chat. On Docker, copy the "
        "settings file onto a mounted volume (for example /data/hermes/local.yaml), "
        "set PRINT_HANDY_CONFIG to that path, recreate the container, then try "
        "again, or reply `manual` for the YAML to add by hand. Nothing was changed.")


def add_printer(new_entry, *, config_path=None, home=None, environ=None, timestamp=None):
    """Append ``new_entry`` (a roster entry dict) to the settings file's roster.

    Returns ``{"path", "backup", "converted", "printer_ids"}``: whether a
    legacy single-printer config was converted, and the roster's ids in
    order after the write. Raises :class:`RosterWriteError` — writing
    nothing — when the target is TOML, missing, unroundtrippable, the id or
    display name is already taken, or the candidate fails validation.
    """
    environ = os.environ if environ is None else environ
    target = Path(config_path) if config_path is not None else settings_file_path(environ)
    if target.suffix.lower() == ".toml":
        raise RosterWriteError(
            f"{target} is a TOML settings file; guided onboarding writes YAML only. "
            "Add the [[printers]] entry by hand (docs/SETTINGS.md, 'Printers list') "
            "or migrate the file to config/local.yaml first.")
    if not target.is_file():
        raise RosterWriteError(
            f"there is no settings file at {target} yet — run `/print-handy setup` "
            "for the first printer before adding another.")
    reason = _unwritable_reason(target)
    if reason:
        raise _refusal(reason)
    parsed = setup_wizard._load_for_rewrite(target.read_text(encoding="utf-8"))
    if parsed is None:
        raise RosterWriteError(
            f"{target} cannot be safely rewritten (it does not parse, repeats a key, or "
            "would change meaning when re-emitted), so it was left unchanged. Fix it by "
            "hand (docs/CONFIGURATION-YAML.md) or move it aside, then try again.")
    try:
        current = settings.load(home=home, local=target, environ=environ)
    except settings.SettingsError as exc:
        raise RosterWriteError(f"the current settings do not load, so nothing was "
                               f"changed: {exc}") from None
    existing = current["printers"]
    new_id = str(new_entry.get("id") or "")
    if new_id in {entry["id"] for entry in existing}:
        raise RosterWriteError(f"a printer with id {new_id!r} already exists")
    new_name = str(new_entry.get("name") or "")
    if new_name and new_name.casefold() in {str(e["name"]).casefold() for e in existing}:
        raise RosterWriteError(f"a printer named {new_name!r} already exists — in a shared "
                               "channel the name is how prints are told apart, so each "
                               "printer needs its own")

    candidate = copy.deepcopy(parsed)
    converted = False
    if isinstance(candidate.get("printers"), list):
        candidate["printers"] = [*candidate["printers"], _file_entry(new_entry)]
    else:
        # No roster in THIS file: a legacy single-printer config resolves
        # to exactly one entry, but a roster inherited from the home layer
        # resolves to several — convert every resolved entry, or writing
        # only the first would shadow the inherited roster and drop the
        # rest.
        candidate["printers"] = [_converted_first_entry(entry) for entry in existing]
        candidate["printers"].append(_file_entry(new_entry))
        _strip_legacy(candidate)
        converted = True
    try:
        resolved = _validate_candidate(candidate, target, home=home, environ=environ)
    except OSError as exc:  # the temp candidate could not be created next to the file
        raise _refusal(f"{target} could not be written ({exc.strerror or exc})") from None
    ids = [entry["id"] for entry in resolved["printers"]]
    if new_id not in ids:  # the loader resolved a different roster than was written
        raise RosterWriteError("the updated settings resolved without the new printer, "
                               "so nothing was written")
    missing = [entry["id"] for entry in existing if entry["id"] not in ids]
    if missing:  # no write may silently drop a printer that was already there
        raise RosterWriteError(f"the updated settings resolved without existing "
                               f"printer(s) {', '.join(missing)}, so nothing was written")
    stamp = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        backup = _backup(target, stamp)
        setup_wizard._atomic_write(target, _HEADER + setup_wizard._dump(candidate), 0o644)
    except OSError as exc:
        raise _refusal(f"{target} could not be written ({exc.strerror or exc})") from None
    return {"path": target, "backup": backup, "converted": converted, "printer_ids": ids}
