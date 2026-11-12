"""Check that this machine can run Print Handy: settings, templates, core, slicer, services.

    python3 -m print_handy.doctor

Exits 1 when any check fails. Services and the slicer are skipped when local.toml does not set them.
"""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from . import diagnostics, settings, state_files, state_volume
from .core import resources, spoolman_client

# Moonraker and Spoolman endpoints that answer without auth when the service is up.
SERVICE_PATHS = {"moonraker_url": "/printer/info", "spoolman_url": "/api/v1/health"}


def check_settings(data):
    printer = data["printer"]
    positions = (f"{printer['tool_count']} toolhead(s)" if printer.get("tool_count", 1) != 1
                 else f"{printer['filament_slots']} slot(s)")
    return (f"{printer['name']}, bed {printer['bed_mm'][0]:g}x{printer['bed_mm'][1]:g} mm, "
                  f"{positions}, {printer['nozzle_diameter_mm']:g} mm nozzle")


def check_state_volume(data):
    """Which state volume is in use (#914): the configured data folder, and
    whether the legacy /data/hermes volume is the one actually holding the
    data (entrypoint symlink, an explicit legacy path, or both present)."""
    return state_volume.describe_state_volume(data["paths"]["state_home"])


def check_model(data):
    from . import printers
    adapter = printers.get(data["printer"]["model"], data)
    if adapter.multi_slot:
        kind = f"{adapter.slot_count} slots (multi-slot)"
    elif adapter.multi_tool:
        kind = f"{adapter.tool_count} toolheads (multi-tool)"
    else:
        kind = "single extruder"
    return f"{adapter.key} ({adapter.label}), {kind}"


def check_templates(data):
    folder = data["orca"]["templates"]
    machine = None
    for name in ("machine", "process", "filament"):
        profile = json.loads((folder / f"{name}.json").read_text())
        machine = profile if name == "machine" else machine
    corners = [tuple(float(v) for v in c.split("x")) for c in machine["printable_area"]]
    area = [max(x for x, _ in corners) - min(x for x, _ in corners),
            max(y for _, y in corners) - min(y for _, y in corners)]
    if area != data["printer"]["bed_mm"]:
        raise RuntimeError(f"machine.json printable area {area} does not match settings bed_mm {data['printer']['bed_mm']}")
    return str(folder)


REQUIRED_HARDWARE = (("spool", "material", str), ("spool", "density", (int, float)),
                     ("temps_c", "nozzle", (int, float)), ("temps_c", "bed", (int, float)),
                     ("nozzle", "diameter_mm", (int, float)))


def check_hardware(data):
    """The printer hardware record the pipeline reads with no fallback. A fresh install has none."""
    path = settings.state_dir(data) / "printer_hardware.json"
    if not path.is_file():
        raise RuntimeError(f"{path} is missing; create it with "
                           "`python3 -m print_handy.core.printer_state provision`, then record the "
                           "real hardware with `printer_state set` before slicing")
    record = json.loads(path.read_text())
    missing = [key for key in ("spool", "temps_c", "nozzle") if not isinstance(record.get(key), dict)]
    if missing:
        raise RuntimeError(f"{path} lacks {', '.join(missing)}")
    # The fields slicing reads directly; a section without them passes here but fails mid-slice.
    missing = [f"{section}.{key}" for section, key, kind in REQUIRED_HARDWARE
               if not isinstance(record[section].get(key), kind) or isinstance(record[section].get(key), bool)]
    if missing:
        raise RuntimeError(f"{path} lacks {', '.join(missing)}")
    nozzle = record["nozzle"].get("diameter_mm")
    if nozzle is None or float(nozzle) != data["printer"]["nozzle_diameter_mm"]:
        raise RuntimeError(f"recorded nozzle {nozzle} mm does not match settings {data['printer']['nozzle_diameter_mm']:g} mm")
    return f"{path.name}, {float(nozzle):g} mm nozzle"


def check_strays(data):
    """Print Handy state files outside the profile state folder. Returns a warning, or None."""
    strays = state_files.find_strays(data)
    if not strays:
        return None
    return (f"{len(strays)} outside {settings.state_dir(data)}: {state_files.describe(strays)} "
            "(python3 -m print_handy.state_files --remove, or scripts/home-update.sh)")


def check_core(data):
    core = Path(__file__).resolve().parent / "core"
    names = sorted(p.stem for p in core.glob("*.py") if p.stem != "__init__")
    for name in names:
        importlib.import_module(f"print_handy.core.{name}")
    return f"{len(names)} modules import"


def check_orca(data):
    command = data["orca"]["command"]
    if not shutil.which(command[0]):
        raise RuntimeError(f"{command[0]} is not on PATH")
    # OrcaSlicer writes 00000.log into its working directory and aborts when it cannot; in Docker
    # the default cwd (/app) is not writable by the container user, so probe from a scratch dir.
    with tempfile.TemporaryDirectory(prefix="print-handy-doctor-") as scratch:
        run = subprocess.run([*command, "--help"], capture_output=True, text=True, timeout=120, cwd=scratch)
    if run.returncode != 0:
        raise RuntimeError(f"{' '.join(command)} --help exited {run.returncode}")
    return " ".join(command)


def check_pids(data):
    """(text, warn) for processes against the container PID limit; None when no limit exists."""
    return resources.pid_report()


def check_service(data, key):
    url = data.get("services", {}).get(key)
    if not url:
        return None
    if key == "spoolman_url":
        # Through the one Spoolman client (#387), like every Spoolman call.
        ok, detail = spoolman_client.health(base_url=url, timeout=5)
        if not ok:
            raise RuntimeError(f"{url} Spoolman health check failed: {detail}")
        return url
    with urllib.request.urlopen(url.rstrip("/") + SERVICE_PATHS[key], timeout=5) as resp:
        if resp.status != 200:
            raise RuntimeError(f"{url} answered {resp.status}")
    return url


def _moonraker_transport(base_url):
    """The firmware reader's transport shape over plain urllib, as check_service does it."""
    def call(method, path, body=None):
        payload = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base_url.rstrip("/") + path, data=payload, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode() or "{}")
    return call


def check_zmod_version(data):
    """The installed ZMod version, read live from Moonraker (#562 Part 2, Gap 2).

    Doctor is where Print Handy's live service checks already live, so the
    version rides here — and into the diagnostics bundle, which embeds
    doctor's output — instead of the bundle growing its own Moonraker
    transport. Only a ZMod-family printer has a ZMod version: anything
    else, and a ZMod printer with no Moonraker URL configured, skips. The
    read goes through firmware_update's update-status reader and is
    fail-soft: an unreachable Moonraker or an absent `zmod` component
    reports the version as unknown, named as such — never a failure, and
    never blocking the rest of the doctor.
    """
    from . import firmware_update
    entry = firmware_update.printer_entry(data["printer"]["model"])
    if entry is None or entry.firmware_family != "zmod":
        return None
    url = data.get("services", {}).get("moonraker_url")
    if not url:
        return None
    try:
        status = firmware_update.fetch_status(_moonraker_transport(url))
    except Exception as exc:
        return f"unknown (Moonraker's update status could not be read: {exc})"
    zmod = status.component("zmod")
    if zmod is not None and zmod.current and zmod.source == "system_info":
        return f"{zmod.current} (from the printer's OS info)"
    if zmod is not None and zmod.current:
        return zmod.current
    return "unknown (not reported by Moonraker)"


def check_purge(data):
    """Where each ZMod printer's color-change purge goes, and which ZMod plugins are enabled (#832).

    Re-reads the printer (read-only, short timeouts) and caches the answer; when the printer cannot
    be read it says so and names what it falls back to. Skips when no roster printer runs ZMod.
    """
    from . import printers
    from .core import purge
    lines = []
    for entry in settings.printers(data):
        view = settings.view_for(entry["id"], data)
        adapter = printers.get(entry["model"], view)
        if not getattr(adapter, "has_native_screen", False):
            continue
        try:
            record = purge.refresh({"moonraker_url": view["services"].get("moonraker_url", "")},
                                   adapter, entry["id"], data)
        except Exception as exc:
            record = {"error": str(exc)}
        row = purge.status_row(entry["id"], data, adapter=adapter)
        if row is None:
            continue
        text = purge.describe(row)
        if record.get("error"):
            text += (f"; the printer could not be read now ({record['error']}), so the last reading or "
                     "today's prime-tower behavior is used")
        lines.append(f"{entry['id']}: {text}")
    return " | ".join(lines) or None


def check_bot(data):
    """Where the bot talks to Discord, and for whom."""
    from .bot.platform import describe_access
    from .bot.runtime import allowed_users, denied_users, listed_users
    chat = data["discord"]["chat_id"]
    if not chat:
        raise RuntimeError("standalone, but discord.chat_id is empty")
    # An empty allow list is a valid Discord mode (#386): the channel is
    # the boundary. Doctor states the mode instead of refusing to run.
    access = describe_access(len(allowed_users(data)), len(denied_users(data)),
                              not listed_users(data))
    return f"standalone, print channel {chat}, access: {access}"


def check_logging(data):
    """The [logging] setup (#390 stage 5): a configured log file must be writable.

    A log file whose folder is missing or unwritable fails silently at
    runtime (logs.configure falls back to stderr and the operator never
    learns the file they configured is not being written), so the doctor
    probes it here with the remedy. Levels were already validated when the
    settings loaded; this row reports how many subsystem overrides apply.
    """
    cfg = data.get("logging") or {}
    levels = cfg.get("levels") or {}
    suffix = f", {len(levels)} subsystem level override(s)" if levels else ""
    path = str(cfg.get("file") or "").strip()
    if not path:
        return f"stderr only{suffix}"
    target = Path(path).expanduser()
    if not target.parent.is_dir():
        raise RuntimeError(
            f"log file folder {target.parent} does not exist; create it or fix [logging] file "
            f"{diagnostics.bracket_reference("PH_LOGGING_CONFIGURATION")}")
    if target.is_dir():
        raise RuntimeError(
            f"log file {target} is a folder, not a file; fix [logging] file "
            f"{diagnostics.bracket_reference("PH_LOGGING_CONFIGURATION")}")
    if target.exists():
        # The configured file itself is the thing the logger must append
        # to; a writable folder around an unwritable file is still broken.
        try:
            with target.open("a"):
                pass
        except OSError as exc:
            raise RuntimeError(
                f"log file {target} is not writable ({exc}); fix its permissions "
                f"or change [logging] file {diagnostics.bracket_reference("PH_LOGGING_CONFIGURATION")}") from exc
    else:
        # No file yet (the logger creates it at first write): probe the
        # folder, under a unique temporary name so concurrent doctor runs
        # — and a stale probe left by a crashed one — never collide.
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent,
                                             prefix=".doctor-write-test-",
                                             delete=False) as probe:
                probe_path = Path(probe.name)
            probe_path.unlink()
        except OSError as exc:
            raise RuntimeError(
                f"log file folder {target.parent} is not writable ({exc}); fix its permissions "
                f"or change [logging] file {diagnostics.bracket_reference("PH_LOGGING_CONFIGURATION")}") from exc
    return f"file {target}{suffix}"


def check_listener_heartbeats(data):
    """Per-printer listener liveness as a warning (#390 stage 5).

    Returns a warning string, or None. Follows check_strays' contract: a
    dead listener is a named, actionable warning here — the container
    healthcheck owns failing on it, and a doctor run on a host where the
    listeners legitimately are not running must not fail the whole check.
    """
    from . import healthcheck
    statuses = healthcheck.listener_statuses(data)
    required = {pid: s for pid, s in statuses.items() if s["required"]}
    if not required:
        return None
    down = [f"{pid} ({s['reason']})" for pid, s in sorted(required.items()) if not s["ok"]]
    if not down:
        return None
    return (f"listener heartbeat not healthy for {', '.join(down)}; check that printer's "
            f"listener is running (in the container it restarts on its own; see "
            f"docs/DIAGNOSTICS.md) {diagnostics.bracket_reference("PH_LISTENER_TRANSIENT")}")


def check_debug_mode(data):
    """Debug log mode (#478) as a row: the live session line, or None.

    Reading the state can end an expired session (the auto-off); that is
    the mode working as designed, never a doctor failure.
    """
    from . import debug_mode
    return debug_mode.check_row(data)


def _bot_token(data):
    from .runtime import _config
    name = data["discord"]["token_env"]
    return _config.profile_env_value(name), name


def check_discord_login(data):
    """Log in over REST with the bot token."""
    token, name = _bot_token(data)
    if not token:
        raise RuntimeError(f"${name} is empty; the standalone bot needs its Discord bot token there")
    from .bot import rest
    me = rest.whoami(token)
    return f"{me.get('username')} ({me.get('id')})"


def run(out=sys.stdout):
    failed = False

    def report(label, func, *args):
        nonlocal failed
        try:
            detail = func(*args)
        except Exception as exc:  # every failure is reported, none stops the other checks
            failed = True
            print(f"FAIL  {label}: {exc}", file=out)
            return None
        print(f"{'ok' if detail is not None else 'skip':<5} {label}" + (f": {detail}" if detail else " (not set in local.toml)"), file=out)
        return detail

    try:
        data = settings.load()
    except settings.SettingsError as exc:
        print(f"FAIL  settings: {exc}", file=out)
        return 1
    report("settings", check_settings, data)
    report("state volume", check_state_volume, data)
    report("printer model", check_model, data)
    report("orca templates", check_templates, data)
    report("core", check_core, data)
    report("hardware record", check_hardware, data)
    report("orca cli", check_orca, data)
    for key in SERVICE_PATHS:
        report(key.replace("_url", ""), check_service, data, key)
    report("zmod version", check_zmod_version, data)
    report("zmod purge", check_purge, data)
    report("bot mode", check_bot, data)
    report("logging", check_logging, data)
    report("discord login", check_discord_login, data)
    try:
        model_warning = settings.defaulted_model_warning(data)
    except Exception as exc:  # a state problem is a warning here, never a failed check
        model_warning = f"could not check: {exc}"
    if model_warning:
        print(f"WARN  default model: {model_warning}", file=out)
    try:
        unknown_warning = settings.unknown_settings_warning()
    except Exception as exc:  # a state problem is a warning here, never a failed check
        unknown_warning = f"could not check: {exc}"
    if unknown_warning:
        print(f"WARN  unknown settings: {unknown_warning}", file=out)
    try:
        pids = check_pids(data)
    except Exception as exc:  # an unreadable cgroup is a warning here, never a failed check
        pids = (f"could not check: {exc}", True)
    if pids is not None:
        print(f"{'WARN' if pids[1] else 'ok  '}  pid limit: {pids[0]}", file=out)
    try:
        warning = check_strays(data)
    except Exception as exc:  # a scan problem is a warning, never a failed check
        warning = f"could not scan: {exc}"
    print(f"WARN  stray state files: {warning}" if warning else "ok    stray state files: none", file=out)
    try:
        heartbeat_warning = check_listener_heartbeats(data)
    except Exception as exc:  # an unresolvable roster is a warning here, never a failed check
        heartbeat_warning = f"could not check: {exc}"
    print(f"WARN  listener heartbeats: {heartbeat_warning}" if heartbeat_warning
          else "ok    listener heartbeats: healthy or not required", file=out)
    try:
        debug_row = check_debug_mode(data)
    except Exception as exc:  # a state problem is reported, never a failed check
        debug_row = f"could not check: {exc}"
    print(f"WARN  debug mode: {debug_row}" if debug_row else "ok    debug mode: off", file=out)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
