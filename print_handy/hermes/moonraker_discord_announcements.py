#!/usr/bin/env python3
"""Emit one Discord announcement on print completion."""

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NamedTuple

from ..core import spoolman_client
from . import _config
from . import discord_print_summary as print_summary
from . import plan_timelapse

OPENVIKING_URL = _config.service_url("openviking_url")


def _profile_env_value(name):
    """Read ONE key from the profile .env (the systemd unit loads no EnvironmentFile)."""
    env_path = _config.hermes_home() / ".env"
    try:
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


OPENVIKING_API_KEY = os.environ.get("OPENVIKING_API_KEY") or _profile_env_value("OPENVIKING_API_KEY")
OPENVIKING_VERIFY_TIMEOUT = float(os.environ.get("OPENVIKING_VERIFY_TIMEOUT", "15"))
DISCORD_CHAT_ID = _config.discord_chat_id()

from .print_records import (
    build_record_data,
    load_context,
    plan_is_complete,
    utc_now,
    write_completed_record,
    format_multipart_summary,
    remaining_plate_choices,
    PLANS_DIR,
    _safe_slug,
)

# Stdout line telling the listener to post the interactive plate picker for this plan after the completion post.
PLATE_PICKER_MARKER = "PLATE_PICKER:"
# Stdout line for a plate of a multi-plate plan: the listener keeps or combines clips instead of posting one.
TIMELAPSE_PLAN_MARKER = plan_timelapse.PLAN_MARKER

BASE = _config.moonraker_url()
SPOOLMAN_BASE = _config.service_url("spoolman_url")
API_KEY = os.environ.get("MOONRAKER_API_KEY", "")
STATE_PATH = _config.state_file("MOONRAKER_ANNOUNCEMENT_STATE", "moonraker_discord_announcements.json", per_instance=True)
MEDIA_DIR = _config.cache_dir() / "moonraker-announcements"
# Remote timelapse archive over ssh. An empty host turns the archive (and the posted video) off.
# The ssh login comes from ~/.ssh/config for that host. Some NAS hosts drop scp, so transfers stream over ssh.
_TIMELAPSE = _config.cfg()["timelapse"]
TIMELAPSE_ARCHIVE_HOST = str(_TIMELAPSE["archive_host"])
TIMELAPSE_ARCHIVE_DIR = str(_TIMELAPSE["archive_dir"])
TIMELAPSE_SSH_KEY = str(_TIMELAPSE["ssh_key"])
TIMELAPSE_RENDER_WAIT_SECONDS = float(_TIMELAPSE["render_wait_s"])
TIMELAPSE_RENDER_POLL_SECONDS = float(_TIMELAPSE["render_poll_s"])
TIMELAPSE_MAX_BYTES = int(_TIMELAPSE["max_bytes"])
TIMELAPSE_LOCAL_RETENTION_DAYS = float(_TIMELAPSE["local_retention_days"])
# The distinct outcomes of the printer-rendered archive lookup (#540). The
# completion flow names exactly one per job; only ARCHIVE_NOT_RENDERED means
# a bounded wait actually elapsed, so no other state may be reported as a
# render timeout.
ARCHIVE_OFF = "archive_off"  # archive_host/archive_dir unset: lookup skipped, no wait at all
ARCHIVE_NOT_RENDERED = "not_rendered"  # the bounded wait elapsed and no rendered video appeared
ARCHIVE_FAILED = "archive_failed"  # a rendered video was found, but downloading/archiving it failed
ARCHIVED = "archived"  # the rendered video was archived; path holds its archive location


class ArchiveOutcome(NamedTuple):
    """The result of archive_completed_timelapse: the state, the archive path when archived, and failure detail."""

    state: str
    path: str | None = None
    detail: str = ""
ACTIVE_STATES = {"Printing", "Paused", "Pausing", "Resuming"}
# Pinned to the print profile from config. The multiplex gateway exports the launching profile's
# HERMES_HOME to cron scripts, which would point profile-relative files at the wrong profile.
PROFILE_HOME = _config.pinned_hermes_home()


def _context_value(context, *keys):
    """Read batch metadata from flat or nested print context."""
    sources = [context] if isinstance(context, dict) else []
    if isinstance(context, dict):
        sources.extend(value for key in ("batch", "job", "print", "copies")
                       if isinstance(value := context.get(key), dict))
    for source in sources:
        for key in keys:
            if source.get(key) is not None:
                return source[key]
    return None


def _queued_jobs():
    """Return Moonraker's queue; unavailable status fails closed."""
    try:
        result = get_json("/server/job_queue/status").get("result") or {}
        return result.get("queued_jobs") or result.get("queue") or []
    except Exception as exc:
        print(f"Print queue status unavailable: {exc}", file=sys.stderr)
        return None


def _batch_fully_complete(context):
    """True only once all copies and multipart steps are complete."""
    total = _context_value(context, "total_copies", "copies", "copy_count", "batch_size")
    index = _context_value(context, "copy_index", "copy_number", "batch_index")
    try:
        if total is not None and int(total) > 0 and index is not None and int(index) < int(total):
            return False
    except (TypeError, ValueError):
        return False
    plan_id = _context_value(context, "multipart_plan_id", "plan_id")
    if plan_id and not plan_is_complete(str(plan_id)):
        return False
    return _queued_jobs() == []


def _openviking_request(method, path, payload=None, timeout=10):
    headers = {"Accept": "application/json"}
    if OPENVIKING_API_KEY:
        # OpenViking's authenticated client sends both forms on every request.
        headers["X-API-Key"] = OPENVIKING_API_KEY
        headers["Authorization"] = f"Bearer {OPENVIKING_API_KEY}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(f"{_config.require(OPENVIKING_URL, 'services.openviking_url')}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def write_and_verify_print_memory(filename, stats, context):
    """Write the durable print line and require verified memory_diff output."""
    spool = context.get("active_spool") if isinstance(context, dict) else {}
    spool = spool if isinstance(spool, dict) else {}
    filament_data = spool.get("filament") if isinstance(spool.get("filament"), dict) else {}
    filament = " ".join(str(value).strip() for value in (
        spool.get("vendor") or filament_data.get("vendor"),
        spool.get("material") or filament_data.get("material"),
        spool.get("color") or filament_data.get("color"),
    ) if value).strip()
    filament = filament or str(spool.get("name") or "filament not recorded")
    used = stats.get("filament_used") if isinstance(stats, dict) else None
    line = f"Print {Path(str(filename)).stem}: success; filament {filament}; used {used if used is not None else 'not recorded'} mm; date {utc_now().split('T', 1)[0]}."
    sid = f"hermes-remember-print-{hashlib.sha256(line.encode()).hexdigest()[:16]}"
    try:
        _openviking_request("POST", f"/api/v1/sessions/{sid}/messages", {"role": "user", "parts": [{"type": "text", "text": line}]})
        _openviking_request("POST", f"/api/v1/sessions/{sid}/commit", {"keep_recent_count": 0})
        deadline = time.time() + OPENVIKING_VERIFY_TIMEOUT
        while time.time() < deadline:
            listing = _openviking_request("GET", f"/api/v1/fs/ls?uri=viking://user/hermes/sessions/{sid}", timeout=5)
            if "memory_diff.json" in json.dumps(listing):
                diff_text = json.dumps(listing)
                if '"adds": 0' not in diff_text or '"updates": 0' not in diff_text:
                    return True, line
                raise RuntimeError("memory_diff.json reported zero adds/updates")
            time.sleep(0.5)
        raise RuntimeError("memory_diff.json was not found")
    except Exception as exc:
        print(f"Print memory write/verification failed: {exc}", file=sys.stderr)
        return False, line


def get_job():
    """Read a stable Moonraker-native job from Moonraker native state."""
    payload = get_json("/printer/objects/query?print_stats&virtual_sdcard")
    status = (payload.get("result") or {}).get("status") or {}
    stats = status.get("print_stats") or {}
    virtual_sdcard = status.get("virtual_sdcard") or {}

    native_state = str(stats.get("state") or "standby").lower()
    state_map = {
        "printing": "Printing",
        "paused": "Paused",
        "complete": "Completed",
        "error": "Error",
        "cancelled": "Operational",
        "standby": "Operational",
    }
    state = state_map.get(native_state, "Operational")
    filename = str(stats.get("filename") or "")

    if native_state == "complete":
        completion = 100.0
    elif virtual_sdcard.get("is_active") and virtual_sdcard.get("file_size"):
        completion = float(virtual_sdcard.get("progress") or 0.0) * 100.0
    else:
        completion = None

    if filename:
        file_info = {
            "name": filename,
            "display": filename,
            "path": filename,
            "origin": "local",
        }
    else:
        file_info = {"name": None}

    return {
        "job": {"file": file_info},
        "progress": {
            "completion": completion,
            "printTime": stats.get("print_duration"),
        },
        "state": state,
        "stats": stats,
    }


def get_json(path, timeout=10):
    headers = {"Accept": "application/json"}
    if API_KEY:
        headers["X-Api-Key"] = API_KEY
    request = urllib.request.Request(f"{_config.require(BASE, 'services.moonraker_url')}{path}", headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def get_file_metadata(filename):
    """Read Moonraker's confirmed metadata without changing printer state."""
    encoded = urllib.parse.quote(str(filename), safe="/")
    payload = get_json(f"/server/files/metadata?filename={encoded}")
    return payload.get("result") or {}


def _job_gcode_name(job_key):
    """Moonraker print filename, the token the timelapse plugin embeds in the MP4 name."""
    return Path(str(job_key or "")).name


def _timelapse_matches_job(video_name, job_key):
    """True only for timelapse_<full gcode name>_<stamp>.mp4 from this exact job.

    mainsail-crew moonraker-timelapse render() keeps the whole remote name,
    including .gcode, then appends time_format_code (%Y%m%d_%H%M here):
    timelapse_{print_stats.filename}_{date_time}.mp4. Stripping the suffix
    matched nothing the plugin actually writes.
    """
    gcode = _job_gcode_name(job_key)
    name = Path(str(video_name or "")).name
    if not gcode or Path(name).suffix.lower() != ".mp4":
        return False
    if ".." in Path(name).parts or "/" in str(video_name) or name != str(video_name):
        return False
    prefix = f"timelapse_{gcode}_"
    if not name.startswith(prefix) or len(name) != len(prefix) + len("YYYYMMDD_HHMM") + 4:
        return False
    stamp = name[len(prefix):-4]
    return bool(re.fullmatch(r"[0-9]{8}_[0-9]{4}", stamp))


def _timelapse_candidates(job_key):
    """List matching MP4s from Moonraker's registered timelapse root, newest first."""
    # The recursive root listing walks hundreds of frame JPEGs and took ~10 s, which
    # tripped the 10 s timeout on every print. The top-level directory call is ~0.3 s.
    payload = get_json("/server/files/directory?path=timelapse&extended=false", timeout=30)
    result = payload.get("result") or {}
    rows = result.get("files") if isinstance(result, dict) else result
    rows = rows or []
    matches = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or row.get("filename") or row.get("name") or "")
        if _timelapse_matches_job(path, job_key):
            matches.append((float(row.get("modified") or row.get("modified_time") or 0), Path(path).name))
    return [path for _, path in sorted(matches, reverse=True)]


def timelapse_archive_name(job_key, video_path, completed_at=None):
    """Stable collision-resistant filename tied to the exact completed job and source video."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    value = completed_at or utc_now()
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    local = parsed.astimezone(ZoneInfo("America/New_York"))
    stamp = local.strftime("%Y%m%d-%H%M%S%z")
    job = _safe_slug(Path(_job_gcode_name(job_key)).stem)[:48] or "print"
    digest = hashlib.sha256(f"{job_key}|{video_path}".encode()).hexdigest()[:12]
    return f"{stamp}-{job}-{digest}.mp4"


def download_timelapse(video_name, job_key):
    """GET the rendered MP4 through Moonraker's timelapse file root. Never SCP the printer."""
    name = Path(str(video_name)).name
    if not _timelapse_matches_job(name, job_key):
        raise ValueError("timelapse filename does not match the completed job")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    destination = MEDIA_DIR / f"source-{hashlib.sha256(name.encode()).hexdigest()[:16]}-{name}"
    if destination.is_file() and destination.stat().st_size:
        return destination
    temporary = destination.with_name(f".{destination.name}.partial")
    encoded = urllib.parse.quote(name)
    request = urllib.request.Request(f"{_config.require(BASE, 'services.moonraker_url')}/server/files/timelapse/{encoded}")
    if API_KEY:
        request.add_header("X-Api-Key", API_KEY)
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as handle:
            copied = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > TIMELAPSE_MAX_BYTES:
                    raise RuntimeError("timelapse download exceeded size cap")
                handle.write(chunk)
        if copied == 0 or not temporary.is_file():
            raise RuntimeError("downloaded timelapse is empty")
        os.replace(temporary, destination)
        return destination
    finally:
        if temporary.exists():
            temporary.unlink()


def _archive_ssh(*remote_args):
    _config.require(TIMELAPSE_ARCHIVE_HOST, "timelapse.archive_host")
    ssh = shutil.which("ssh") or "ssh"
    command = [ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR"]
    if TIMELAPSE_SSH_KEY:
        command += ["-i", TIMELAPSE_SSH_KEY]
    return command + [TIMELAPSE_ARCHIVE_HOST, *remote_args]


def archive_timelapse(job_key, video_path, completed_at=None):
    """Download a matching MP4 over HTTP, then copy that local file to the archive host.

    The printer original stays on the printer. The archive SSH key cannot log
    into the printer, so the source is never root@printer SCP.
    """
    _config.require(TIMELAPSE_ARCHIVE_HOST, "timelapse.archive_host")
    _config.require(TIMELAPSE_ARCHIVE_DIR, "timelapse.archive_dir")
    local = download_timelapse(video_path, job_key)
    remote_name = timelapse_archive_name(job_key, video_path, completed_at)
    remote_final = _upload_to_archive(local, remote_name)
    # The archive host now holds the durable copy; the HTTP staging copy is redundant.
    Path(local).unlink(missing_ok=True)
    return remote_final


def archive_local_timelapse(plan_id, local, completed_at=None):
    """Copy an already local MP4 (a plan's combined timelapse) to the archive host; None when the archive is off."""
    if not TIMELAPSE_ARCHIVE_HOST or not TIMELAPSE_ARCHIVE_DIR:
        return None
    name = timelapse_archive_name(f"{plan_id}.gcode", f"combined-{Path(str(local)).name}", completed_at)
    return _upload_to_archive(local, name)


def _upload_to_archive(local, remote_name):
    remote_tmp = f"{TIMELAPSE_ARCHIVE_DIR}/.{remote_name}.partial"
    remote_final = f"{TIMELAPSE_ARCHIVE_DIR}/{remote_name}"
    with open(local, "rb") as source:
        subprocess.run(
            _archive_ssh(f"mkdir -p {shlex.quote(TIMELAPSE_ARCHIVE_DIR)} && cat > {shlex.quote(remote_tmp)}"),
            stdin=source, check=True, capture_output=True, timeout=180,
        )
    subprocess.run(
        _archive_ssh(f"test -s {shlex.quote(remote_tmp)} && mv {shlex.quote(remote_tmp)} {shlex.quote(remote_final)}"),
        check=True, capture_output=True, timeout=30,
    )
    return remote_final


def prune_local_timelapses(now=None):
    """Retention for the local Discord cache only: drop MP4s older than the window.

    The remote archive is the durable copy and is kept indefinitely. The
    printer original is never deleted by Hermes.
    """
    if TIMELAPSE_LOCAL_RETENTION_DAYS <= 0 or not MEDIA_DIR.is_dir():
        return []
    cutoff = (now if now is not None else time.time()) - TIMELAPSE_LOCAL_RETENTION_DAYS * 86400
    removed = []
    for path in MEDIA_DIR.glob("*.mp4"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    return removed


def materialize_archived_timelapse(remote_path):
    """Download a successfully archived MP4 into the local media cache for Discord.

    A remote archive path is not a local file. hermes send only sees this copy.
    """
    remote = Path(str(remote_path))
    archive_root = Path(TIMELAPSE_ARCHIVE_DIR)
    if remote.parent != archive_root or remote.suffix.lower() != ".mp4" or remote.name.startswith("."):
        raise ValueError("timelapse archive path is outside the configured archive directory")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    prune_local_timelapses()
    destination = MEDIA_DIR / remote.name
    if destination.is_file() and destination.stat().st_size:
        return destination
    temporary = destination.with_name(f".{destination.name}.partial")
    try:
        with temporary.open("wb") as handle:
            subprocess.run(
                _archive_ssh(f"cat {shlex.quote(str(remote))}"),
                stdout=handle, stderr=subprocess.PIPE, check=True, timeout=180,
            )
        if not temporary.is_file() or not temporary.stat().st_size:
            raise RuntimeError("downloaded timelapse is empty")
        os.replace(temporary, destination)
        return destination
    finally:
        if temporary.exists():
            temporary.unlink()


def archive_completed_timelapse(job_key, completed_at=None, wait=None):
    """Wait a bounded interval for the completed job's MP4, then archive if available.

    Returns an ArchiveOutcome naming what actually happened (#540): with no
    timelapse.archive_host configured the archive is off and the lookup is
    skipped outright (ARCHIVE_OFF — no search, no wait); a rendered video
    that fails to download or archive is ARCHIVE_FAILED; only a full bounded
    wait with no render appearing is ARCHIVE_NOT_RENDERED.
    """
    if not TIMELAPSE_ARCHIVE_HOST or not TIMELAPSE_ARCHIVE_DIR:
        print("Timelapse archive is off (timelapse.archive_host / archive_dir not configured)", file=sys.stderr)
        return ArchiveOutcome(ARCHIVE_OFF)
    deadline = time.monotonic() + max(0, TIMELAPSE_RENDER_WAIT_SECONDS if wait is None else wait)
    last_lookup_error = None
    while True:
        try:
            candidates = _timelapse_candidates(job_key)
        except Exception as exc:  # transient Moonraker hiccup: keep waiting until the deadline
            print(f"Timelapse lookup unavailable (will retry): {exc}", file=sys.stderr)
            candidates = []
            last_lookup_error = exc
        if candidates:
            try:
                return ArchiveOutcome(ARCHIVED, archive_timelapse(job_key, candidates[0], completed_at))
            except Exception as exc:
                print(f"Timelapse archive failed: {exc}", file=sys.stderr)
                return ArchiveOutcome(ARCHIVE_FAILED, detail=str(exc))
        if time.monotonic() >= deadline:
            detail = f"lookup unavailable throughout the wait: {last_lookup_error}" if last_lookup_error else ""
            return ArchiveOutcome(ARCHIVE_NOT_RENDERED, detail=detail)
        time.sleep(max(0.1, TIMELAPSE_RENDER_POLL_SECONDS))


def partial_timelapse_notice(plan_id, filename):
    """For a stopped print of a plan: the notice text, plus the marker line, when plate clips exist so far."""
    if not plan_id or not plan_timelapse.clips(plan_id):
        return None
    print(f"{TIMELAPSE_PLAN_MARKER}{plan_id}:partial:0")
    return f"Print stopped: {print_summary.safe_label(filename)}. Plan cancelled partway; partial timelapse of the plates finished so far."


def _timelapse_marker(job_key):
    """Completion handoff carrying the exact Moonraker filename.

    stdout is captured by the listener and this line is stripped before the
    Discord post. Slugging the human title cannot round trip: safe_label turns
    underscores into spaces and _safe_slug does not reverse that.
    """
    name = _job_gcode_name(job_key)
    if not name or "/" in name or name in {".", ".."}:
        return ""
    return name


def _timelapse_job_key(marker):
    """Accept the marker only when it is the exact current job filename."""
    state = load_state()
    key = str(state.get("job_key") or "") if isinstance(state, dict) else ""
    return key if marker and marker == _timelapse_marker(key) else ""


def get_active_spool():
    """Read the active Spoolman spool when Moonraker exposes it."""
    payload = get_json("/server/spoolman/spool_id")
    result = payload.get("result")
    if isinstance(result, dict):
        spool = dict(result)
        spool_id = result.get("spool_id") or result.get("id")
    else:
        if result in (None, ""):
            return {}
        spool = {"spool_id": result}
        spool_id = result
    if spool_id in (None, ""):
        return spool
    # The Spoolman detail fetch is bookkeeping enrichment (#387): through
    # the one client, best-effort. When Spoolman does not answer, the
    # completion record is still written — with the usage marked pending
    # for later reconciliation instead of silently unrecorded.
    if SPOOLMAN_BASE:
        details = spoolman_client.get_spool(spool_id, base_url=SPOOLMAN_BASE, timeout=10)
        if isinstance(details, dict):
            spool.update(details)
        else:
            spool["spoolman_unavailable"] = True
    return spool


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".moonraker-announcements-", dir=STATE_PATH.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, STATE_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ------------------------------------------------------- pending completion
# The pending completion (message + its timelapse job key) is filed per
# printer (#536): one state root serves every roster printer's announcer,
# and a single shared slot let one printer's completion overwrite — or be
# cleared by — another's. The pre-#536 single slots are adopted on read as
# the serving printer's: in a one-printer install they always were.


def _pending_printer_key():
    """The key pending completion state is filed under: the printer served."""
    try:
        from .. import listeners
        pid = listeners.instance_printer_id()
    except Exception:
        pid = None
    if pid:
        return str(pid)
    try:
        entries = (_config.cfg() or {}).get("printers") or []
        if entries:
            return str(entries[0].get("id"))
    except Exception:
        pass
    return "default"


def _pending_get(state, plural, singular):
    bucket = state.get(plural)
    if isinstance(bucket, dict):
        value = bucket.get(_pending_printer_key())
        if value:
            return value
    return state.get(singular)


def _pending_set(state, plural, singular, value):
    bucket = state.get(plural)
    if not isinstance(bucket, dict):
        bucket = {}
        state[plural] = bucket
    bucket[_pending_printer_key()] = value
    state.pop(singular, None)


def _pending_clear(state, plural, singular, value=None):
    bucket = state.get(plural)
    if isinstance(bucket, dict):
        key = _pending_printer_key()
        if key in bucket and (value is None or bucket[key] == value):
            bucket.pop(key, None)
    if state.get(singular) is not None and (value is None or state.get(singular) == value):
        state.pop(singular, None)


def pending_completion_message(state):
    """This printer's pending completion message, or None."""
    return _pending_get(state, "pending_completion_messages", "pending_completion_message")


def set_pending_completion_message(state, message):
    _pending_set(state, "pending_completion_messages", "pending_completion_message", message)


def clear_pending_completion_message(state, message=None):
    _pending_clear(state, "pending_completion_messages", "pending_completion_message", message)


def pending_timelapse_job_key(state):
    """This printer's pending timelapse job key, or None."""
    return _pending_get(state, "pending_timelapse_job_keys", "pending_timelapse_job_key")


def set_pending_timelapse_job_key(state, job_key):
    _pending_set(state, "pending_timelapse_job_keys", "pending_timelapse_job_key", job_key)


def clear_pending_timelapse_job_key(state, job_key=None):
    _pending_clear(state, "pending_timelapse_job_keys", "pending_timelapse_job_key", job_key)


HISTORY_STATE = {"completed": "complete", "cancelled": "cancelled"}


def finalize_from_history(job_key):
    """Finalize a stopped job's note after Klipper cleared the filename.

    On cancel Klipper often drops straight to standby with an empty filename,
    so the live key changes before the stop branch can see it. Moonraker job
    history still names the job and has its duration, filament, and end time.
    """
    jobs = (get_json("/server/history/list?limit=1&order=desc").get("result") or {}).get("jobs") or []
    if not jobs or jobs[0].get("filename") != job_key or jobs[0].get("status") in (None, "in_progress"):
        return None
    job = jobs[0]
    stats = {"state": HISTORY_STATE.get(job["status"], "error"), "print_duration": job.get("print_duration"),
             "filament_used": job.get("filament_used"), "start_time": job.get("start_time")}
    ended = job.get("end_time")
    completed_at = datetime.fromtimestamp(ended, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ended else None
    result = record_completion(job_key, job_key, stats, completed_at=completed_at)
    result["state"] = stats["state"]
    return result


def _finished_history_job(filename):
    """Moonraker's newest finished history entry for filename, or None.

    History is the stable identity source for a physical print (P2-150):
    its start_time names the same print on every completion retry,
    unlike the wall clock. An unavailable or unmatched read changes
    nothing — the record falls back to today's behaviour.
    """
    try:
        jobs = (get_json("/server/history/list?limit=50&order=desc").get("result") or {}).get("jobs") or []
    except Exception:
        return None
    for job in jobs:
        if isinstance(job, dict) and job.get("filename") == filename \
                and job.get("status") not in (None, "in_progress") and job.get("start_time"):
            return job
    return None


def record_completion(job_key, filename, stats, completed_at=None):
    """Persist confirmed completion data; unavailable optional reads stay absent."""
    context = load_context(job_key)
    context["job_key"] = job_key
    stats = dict(stats or {})
    if not context.get("started_at") and not (stats.get("start_time") or stats.get("print_start_time")):
        history_job = _finished_history_job(filename)
        if history_job is not None:
            stats["start_time"] = history_job["start_time"]
            if completed_at is None and history_job.get("end_time"):
                completed_at = datetime.fromtimestamp(
                    history_job["end_time"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        metadata = get_file_metadata(filename)
    except Exception as exc:
        print(f"Completion metadata unavailable: {exc}", file=sys.stderr)
        metadata = {}
    try:
        active_spool = get_active_spool()
    except Exception as exc:
        print(f"Active Spoolman spool unavailable: {exc}", file=sys.stderr)
        active_spool = {}
    data = build_record_data(
        filename,
        stats=stats,
        metadata=metadata,
        context=context,
        active_spool=active_spool,
        completed_at=completed_at or utc_now(),
    )
    result = write_completed_record(data)
    result.update({"context": context, "completed_at": data["completed_at"]})
    return result


def queue_completion_record(previous, job_key, filename, stats):
    record_completion(job_key, filename, stats)
    previous["record_written"] = True


def main():
    try:
        payload = get_job()
    except Exception as exc:
        print(f"Moonraker announcement check failed: {exc}", file=sys.stderr)
        return 0

    job = payload.get("job") or {}
    progress = payload.get("progress") or {}
    state = str(payload.get("state") or "")
    filename = str((job.get("file") or {}).get("display") or (job.get("file") or {}).get("name") or "current print")
    completion = progress.get("completion")
    try:
        completion = float(completion) if completion is not None else None
    except (TypeError, ValueError):
        completion = None

    previous = load_state()
    job_key = str((job.get("file") or {}).get("path") or filename)
    previous_key = previous.get("job_key")

    if job_key != previous_key and previous.get("printing") and previous_key and not previous.get("record_written"):
        try:
            stopped = finalize_from_history(previous_key)
            if stopped and stopped.get("state") in ("cancelled", "error"):
                notice = partial_timelapse_notice(stopped.get("plan_id"), previous_key)
                if notice:
                    print(notice)
        except Exception as exc:
            print(f"Stopped print record unavailable: {exc}", file=sys.stderr)

    # A new job gets a fresh one-shot announcement state. Pending completions
    # are per-printer (#536) and survive: another printer's undelivered
    # completion is not this job's to discard.
    if job_key != previous_key:
        fresh = {
            "job_key": job_key,
            "completion": completion,
            "printing": state in ACTIVE_STATES,
            "announced_completion": False,
            "record_written": False,
            "print_started_at": time.time() if state in ACTIVE_STATES else None,
        }
        for pending_key in ("pending_completion_messages", "pending_timelapse_job_keys"):
            if isinstance(previous.get(pending_key), dict) and previous[pending_key]:
                fresh[pending_key] = previous[pending_key]
        previous = fresh
        save_state(previous)
        return 0

    printing = state in ACTIVE_STATES
    message = None

    # Completion is a transition out of an active print at 100 percent.
    was_printing = bool(previous.get("printing", False))
    if message is None and not printing and was_printing and completion is not None and completion >= 100.0 and not previous.get("announced_completion", False):
        # The event listener consumes this handoff only on the successful transition.
        # Do not render synchronously in the printer status path. The listener sees
        # this marker and performs bounded post-completion retrieval before delivery.
        marker = _timelapse_marker(job_key)
        if marker:
            print(f"TIMELAPSE_POSTPROCESS:{marker}")
            # Remember which job the pending completion's video leg belongs
            # to, so a listener restart during the render wait can re-drive
            # the retrieval instead of posting the pending text bare and
            # losing the video (#373).
            set_pending_timelapse_job_key(previous, job_key)
        # No roast here (#374): the one roast per print belongs to the start
        # announcement, added when printing begins. Completion never invokes
        # the roast generator.
        message = print_summary.format_event("complete", filename)
        try:
            queue_completion_record(previous, job_key, filename, payload.get("stats") or {})
        except Exception as exc:
            print(f"Completed print record unavailable: {exc}", file=sys.stderr)
            previous["record_error"] = str(exc)
        context = load_context(job_key)
        multipart_id = context.get("multipart_plan_id") if isinstance(context, dict) else None
        if multipart_id:
            try:
                plan_path = PLANS_DIR / f"{_safe_slug(str(multipart_id))}.json"
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                stage = plan_timelapse.plan_stage(plan, context.get("part_id"))
                if stage:
                    print(f"{TIMELAPSE_PLAN_MARKER}{plan.get('plan_id') or _safe_slug(str(multipart_id))}:{stage[0]}:{stage[1]}")
                summary = format_multipart_summary(plan, context.get("part_id"), picker=True)
                if summary:
                    message += "\n" + summary
                    if remaining_plate_choices(plan):
                        # The listener posts the interactive picker after this completion message.
                        print(f"{PLATE_PICKER_MARKER}{plan.get('plan_id') or _safe_slug(str(multipart_id))}")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(f"Multipart continuation summary unavailable: {exc}", file=sys.stderr)
        batch_complete = _batch_fully_complete(context)
        previous["batch_complete"] = batch_complete
        if batch_complete:
            memory_ok, memory_line = write_and_verify_print_memory(filename, payload.get("stats") or {}, context)
            previous["memory_verified"] = memory_ok
            previous["memory_line"] = memory_line
        else:
            memory_ok = False
            previous["memory_verified"] = False
            previous["memory_line"] = "deferred until the complete batch/job finishes"
        # Keep the completion retryable until the listener confirms Discord delivery.
        set_pending_completion_message(previous, message)

    # A cancel or error also finalizes the print record, without an announcement
    # or session cleanup, so every started print ends with a dated outcome.
    native = str((payload.get("stats") or {}).get("state") or "").lower()
    if was_printing and not printing and native in ("cancelled", "error") and not previous.get("record_written", False):
        try:
            record = record_completion(job_key, filename, payload.get("stats") or {})
            previous["record_written"] = True
            if message is None:
                message = partial_timelapse_notice(record.get("plan_id"), filename)
            previous["record_path"] = record.get("record_path")
            previous.pop("record_error", None)
        except Exception as exc:
            print(f"Stopped print record unavailable: {exc}", file=sys.stderr)
            previous["record_error"] = str(exc)

    # A transient metadata or vault failure must not permanently lose the
    # record merely because the Discord announcement already succeeded.
    if message is None and not printing and completion is not None and completion >= 100.0 and not previous.get("record_written", False):
        try:
            queue_completion_record(previous, job_key, filename, payload.get("stats") or {})
            previous.pop("record_error", None)
        except Exception as exc:
            previous["record_error"] = str(exc)

    previous.update({"job_key": job_key, "completion": completion, "printing": printing})
    save_state(previous)
    if message:
        print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
