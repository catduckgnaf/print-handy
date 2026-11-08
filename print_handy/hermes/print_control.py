"""Printer control for Discord: resume a paused print, print the last job again, force restart.

Backs the ``/print-resume``, ``/print-again`` and ``/print-reboot`` commands in the
print-feedback plugin. Rules learned the hard way (2026-10-10 desk stand):

* A Moonraker resume only works while Klipper is ready and the job is paused.
  After an emergency stop Klipper is shut down; the only live recovery is the
  Resume offer on the AD5X screen. Restarting firmware or rebooting wipes it,
  so nothing here restarts anything as part of a resume.
* "Print it again" means the exact archived job, resolved from local launch
  records. It never downloads from the printer and never re-slices.
* Every start goes through ``print_handy.core.model_job print-now`` via ``print_next.start``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..core import fuzzy, projects
from . import _config

BASE = _config.moonraker_url()
ARCHIVE = _config.archive_dir()
PRINT_NUMBERS = _config.state_file("PRINT_NUMBERS_STATE", "print_numbers.json")
REPRINT_QUEUE = _config.state_file("REPRINT_QUEUE_STATE", "reprint_queue.json")
MAX_COPIES = 20

RESUME_LABEL = "Resume print now"
KEEP_LABEL = "Keep paused"
AGAIN_LABEL = "Print again · bed is clear"
AGAIN_CANCEL_LABEL = "Cancel"
REBOOT_LABEL = "Force restart printer now"
CANCEL_REBOOT_LABEL = "Cancel restart"


def _lock_for(printer_id: str | None = None):
    """The interprocess control lock for one printer (P1-33).

    Shared with every other process acting on the same printer — the
    standalone ph-purge / ph-load-slot CLIs, the nozzle-change flow and
    model_job.print_now — through a flock file in the printer's state dir
    (print_handy.core.control_lock). The state dir and the default
    printer's identity resolve through _config, like every other path
    here, so an override install and the live install agree on the file.
    """
    from .. import settings
    from ..core import control_lock as _cl

    key = printer_id or settings.default_printer_id(_config.cfg())
    return _cl.control_lock(key, state_dir=_config.state_dir())


_CONTROL_LOCK = _lock_for()
# Profile name -> (hot C, cold C), from [cold_pull] in the home configuration.
COLD_PULL_PROFILES = {name: (int(pair[0]), int(pair[1])) for name, pair in _config.cfg()["cold_pull"].items()}


def _adapter_for(printer_id: str | None):
    """The configured printer's adapter, or one roster entry's adapter bound to its own view (#335)."""
    from print_handy import printers
    if not printer_id:
        return printers.current()
    from print_handy import settings
    return printers.get(settings.by_id(printer_id)["model"], settings.view_for(printer_id))


def _base_for(printer_id: str | None) -> str:
    """The Moonraker base URL for a roster printer (#335); the configured one when printer_id is None."""
    if not printer_id:
        return BASE
    from print_handy import settings
    return str(settings.view_for(printer_id)["services"]["moonraker_url"])


def firmware_name(printer_id: str | None = None) -> str:
    """Configured firmware name for short user-facing status messages."""
    return _adapter_for(printer_id).firmware_name


def display_name(printer_id: str | None = None) -> str:
    """Configured printer name for short user-facing status messages."""
    return _adapter_for(printer_id).display_name


def cold_pull_allowed(temp: int, cold: int) -> bool:
    """A configured (hot, cold) pair, or the one-time PLA retry at cold + 5 C."""
    allowed = set(COLD_PULL_PROFILES.values())
    if 'pla_trial' in COLD_PULL_PROFILES:
        hot, base_cold = COLD_PULL_PROFILES['pla_trial']
        allowed.add((hot, base_cold + 5))
    return (temp, cold) in allowed


def coldpull(temp: int, cold: int, *, confirmed_at: float, gcode_sender=None, reader=None,
             printer_id: str | None = None) -> tuple[bool, str]:
    """Run one operator-confirmed cold pull, fail-closed on fresh state and readback."""
    if not cold_pull_allowed(temp, cold):
        return False, 'Refused: unsupported cold-pull temperatures.'
    if time.time() - float(confirmed_at) > 300 or float(confirmed_at) > time.time() + 2:
        return False, 'Refused: confirmation expired. Nothing sent.'
    lock = _lock_for(printer_id)
    if not lock.acquire(blocking=False):
        return False, 'Refused: another printer control action is in flight. Nothing sent.'
    try:
        if reader:
            st = reader()
        else:
            data = _send('/printer/objects/query?print_stats&filament_switch_sensor%20head_switch_sensor&extruder',
                         printer_id=printer_id)
            st = ((data.get('result') or {}).get('status') or {})
        job_state = ((st.get('print_stats') or {}).get('state') or 'unknown').casefold()
        sensor = (st.get('filament_switch_sensor head_switch_sensor') or {}).get('filament_detected')
        if job_state in {'printing', 'paused'}:
            return False, f'Refused: printer is {job_state}. Nothing sent.'
        if job_state not in {'standby', 'complete', 'cancelled', 'error'}:
            return False, f'Refused: printer state is {job_state}. Nothing sent.'
        if sensor is not True:
            return False, 'Refused: no filament at the head sensor. Load filament on the screen first. Nothing sent.'
        script = f'_COLDPULL_LOAD_MATERIAL T=0 TEMP={int(temp)} COLD={int(cold)}'
        if gcode_sender:
            gcode_sender(script)
            after = reader() if reader else {}
        else:
            _send('/printer/gcode/script', method='POST', data={'script': script}, timeout=600,
                  printer_id=printer_id)
            after = ((_send('/printer/objects/query?extruder', printer_id=printer_id).get('result') or {}).get('status') or {})
        extruder = after.get('extruder') or {}
        target = extruder.get('target')
        if target != 0:
            return False, f'Cold pull command returned, but heater target readback is {target!r}, not 0. Check the printer; do not retry.'
        return True, 'Cold pull finished; heater target is 0.'
    finally:
        lock.release()


INTERRUPTED_HINT = ("If the printer screen offers Resume, tap it there. Don't restart the printer first: "
                    "a restart wipes the screen's resume. To start over from the beginning instead, "
                    "clear the bed and type /print-again.")


def _call(path: str, method: str = "GET", data: dict[str, Any] | None = None, timeout: float = 10,
          base: str | None = None) -> dict[str, Any]:
    body = None
    headers = {}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif method == "POST":
        body = b""
    req = urllib.request.Request(_config.require(BASE if base is None else base, "services.moonraker_url") + path, method=method, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def _send(path: str, method: str = "GET", data: dict[str, Any] | None = None, timeout: float = 10,
          printer_id: str | None = None) -> dict[str, Any]:
    """_call against one printer's Moonraker; the no-printer call shape is unchanged."""
    if printer_id:
        return _call(path, method=method, data=data, timeout=timeout, base=_base_for(printer_id))
    if data is None:
        return _call(path, method=method, timeout=timeout)
    return _call(path, method=method, data=data, timeout=timeout)


def status(printer_id: str | None = None) -> dict[str, Any]:
    """Moonraker job state plus Klipper host state (ready, shutdown, error, startup)."""
    try:
        data = _send("/printer/objects/query?print_stats&virtual_sdcard=progress&webhooks",
                     printer_id=printer_id)
    except Exception:
        info = (_send("/server/info", printer_id=printer_id).get("result") or {})
        return {"state": "unknown", "klippy": str(info.get("klippy_state") or "unknown"), "filename": "",
                "layer": None, "total_layer": None, "progress": None, "print_duration": None}
    s = (data.get("result") or {}).get("status") or {}
    ps = s.get("print_stats") or {}
    info = ps.get("info") or {}
    return {
        "state": str(ps.get("state") or "unknown"),
        "klippy": str((s.get("webhooks") or {}).get("state") or "unknown"),
        "filename": ps.get("filename") or "",
        "layer": info.get("current_layer"),
        "total_layer": info.get("total_layer"),
        "progress": (s.get("virtual_sdcard") or {}).get("progress"),
        "print_duration": ps.get("print_duration"),
    }


def where_text(st: dict[str, Any]) -> str:
    bits = []
    if st.get("layer") and st.get("total_layer"):
        bits.append(f"layer {st['layer']}/{st['total_layer']}")
    if isinstance(st.get("progress"), (int, float)) and st["progress"]:
        bits.append(f"{st['progress'] * 100:.0f}% done")
    return ", ".join(bits)


def resumable(st: dict[str, Any]) -> bool:
    return st.get("klippy") == "ready" and st.get("state") == "paused"


def not_resumable_text(st: dict[str, Any]) -> str:
    """Plain answer when Discord cannot resume. Never suggests a restart."""
    if st.get("state") == "printing":
        return "Nothing to resume: it's already printing. Nothing sent."
    if st.get("klippy") != "ready":
        return f"The printer's controller is {st.get('klippy')}, so I can't resume from here. Nothing sent.\n{INTERRUPTED_HINT}"
    return f"Nothing paused to resume (printer is {st.get('state')}). Nothing sent.\n{INTERRUPTED_HINT}"


HEAD_EMPTY_TEXT = (
    "The toolhead filament sensor still reads empty, so filament has not reached the head. "
    "Resuming now would only pause again. Feed the filament at the printer until it reaches "
    "the toolhead, then tap Resume. Nothing sent."
)


def head_switch_sensors(objects: Any) -> list[str]:
    """Switch sensors that sit at the toolhead: named ``head`` when any are, else all on a one-extruder printer.

    Motion sensors are left out: they read empty whenever filament is not
    moving, which is always the case while paused.
    """
    if not isinstance(objects, list):
        return []
    switches = [o for o in objects if isinstance(o, str) and o.startswith("filament_switch_sensor ")]
    head = [o for o in switches if "head" in o.split(" ", 1)[1].lower()]
    if head:
        return head
    multi = any(isinstance(o, str) and re.fullmatch(r"extruder\d+", o) for o in objects)
    return [] if multi else switches


def active_head_sensors(names: list[str], active: Any) -> list[str]:
    """The head sensors that belong to the active extruder on a multi-extruder printer.

    A sensor belongs to a tool when the extruder name is a whole token of its
    suffix ("head_extruder1" -> extruder1), so "extruder1" never matches the
    "extruder" tool. An idle tool's empty sensor must not block a resume
    (review of #878; same rule as moonraker_event_listener.head_sensor_from_status).
    """
    if not isinstance(active, str) or not active:
        return []
    def tokens(name: str) -> set[str]:
        return {t for t in re.split(r"[^0-9A-Za-z]+", name.split(" ", 1)[-1].replace("head", " ")) if t}
    return [n for n in names if active in tokens(n)]


def head_filament_loaded(printer_id: str | None = None) -> bool | None:
    """False when an enabled toolhead switch sensor reads empty; True when all read loaded; None when unknown.

    Read only. 2026-11-04: three resumes were sent while the head sensor
    still read empty after a runout, and each one paused again at once.
    """
    try:
        listing = _send("/printer/objects/list", printer_id=printer_id)
        objects = (listing.get("result") or {}).get("objects")
        names = head_switch_sensors(objects)
        if not names:
            return None
        multi = isinstance(objects, list) and any(
            isinstance(o, str) and re.fullmatch(r"extruder\d+", o) for o in objects)
        query = "&".join(urllib.parse.quote(n, safe="") for n in names + (["toolhead"] if multi else []))
        data = _send(f"/printer/objects/query?{query}", printer_id=printer_id)
        st = (data.get("result") or {}).get("status") or {}
    except Exception:
        return None
    if multi:
        names = active_head_sensors(names, (st.get("toolhead") or {}).get("extruder"))
        if not names:
            return None  # the active tool's sensor is unknown: do not block on another tool's
    readings = [s.get("filament_detected") for s in (st.get(n) for n in names)
                if isinstance(s, dict) and s.get("enabled") is True
                and isinstance(s.get("filament_detected"), bool)]
    if not readings:
        return None
    return all(readings)


def resume(expected_filename: str = "", wait_s: float = 20, poll_s: float = 1.0,
           printer_id: str | None = None) -> tuple[bool, str]:
    """One Moonraker resume for the confirmed paused file, then read back. Never retried.

    Refused without sending when the toolhead filament sensor reads empty.
    An unreadable sensor does not block the resume.
    """
    before = status(printer_id)
    if not resumable(before):
        return False, not_resumable_text(before)
    if expected_filename and before["filename"] and before["filename"] != expected_filename:
        return False, "A different print is paused than the one you confirmed. Nothing sent."
    if head_filament_loaded(printer_id) is False:
        return False, HEAD_EMPTY_TEXT
    _send("/printer/print/resume", method="POST", timeout=15, printer_id=printer_id)
    deadline = time.monotonic() + wait_s
    after = before
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        try:
            after = status(printer_id)
        except Exception:
            continue
        if after["state"] == "printing":
            break
    if after["state"] == "printing":
        where = where_text(after)
        return True, f"Resumed. Printer now reports printing{', ' + where if where else ''}."
    # #837: the resume was sent but never took, so this is not a success —
    # callers that trust the boolean (alert buttons, /print-resume) must
    # not report a resumed print while the printer still reports paused.
    return False, f"Resume sent, but the printer still reports {after['state']} after {wait_s:.0f}s. Check the printer screen. Not retrying."


def _artifact_for(record: Path) -> Path:
    """plate_1.launch.json / plate_1.upload.json -> plate_1.gcode in the same folder."""
    return record.with_name(record.name.split(".", 1)[0] + ".gcode")


def _job_for(artifact: Path, archive: Path) -> Path | None:
    for parent in artifact.parents:
        if parent == archive or not parent.is_relative_to(archive):
            return None
        if (parent / "JOB.json").is_file():
            return parent / "JOB.json"
    return None


def _current_artifacts(job: dict[str, Any]) -> set[Path]:
    known = {Path(str(job["gcode"])).resolve()} if job.get("gcode") else set()
    known |= {Path(str(r["gcode"])).resolve() for r in job.get("derived_plates") or []
              if isinstance(r, dict) and r.get("gcode")}
    return known


AGAIN_LIST_LIMIT = 10


def _launch_records(archive: Path) -> list[Path]:
    records = [p for pat in ("*/sliced/*/*/*.launch.json", "*/sliced/*/*/*.upload.json") for p in archive.glob(pat)]
    return sorted(records, key=lambda p: p.stat().st_mtime, reverse=True)


def printed_jobs(archive: Path | None = None, numbers_path: Path | None = None) -> list[dict[str, Any]]:
    with _RegistryLock(numbers_path):
        return _printed_jobs_locked(archive, numbers_path)


def _printed_jobs_locked(archive: Path | None = None, numbers_path: Path | None = None) -> list[dict[str, Any]]:
    """Archived prints that were actually sent to the printer, most recent first.

    One row per PRINT (launch), not per job: printing the same job again is a
    new print with its own permanent number. Numbers are assigned oldest
    launch first and never reused or renumbered. A job's first print keeps
    the number the pre-launch-numbering registry gave it, so old numbers and
    'print 84' lookups keep resolving to the same job. Only writes the number
    registry, and only when a launch is seen for the first time.
    """
    archive = (archive or ARCHIVE).resolve()
    # Group the launch records by artifact: a launch and its upload record are
    # two files for one print, while a later reprint of the same artifact
    # rewrites those same files (only its newest launch survives on disk —
    # earlier launches of it survive as registry launch entries, below).
    groups: dict[str, dict[str, Any]] = {}
    for record in _launch_records(archive):
        artifact = _artifact_for(record).resolve()
        job_path = _job_for(artifact, archive)
        if job_path is None:
            continue
        mtime = record.stat().st_mtime
        group = groups.get(str(artifact))
        if group is None:
            groups[str(artifact)] = {"job_json": job_path, "record": record,
                                     "launched_at": mtime, "artifact": str(artifact)}
        else:
            group["launched_at"] = max(group["launched_at"], mtime)
            if mtime >= group["record"].stat().st_mtime:
                group["record"] = record
    if not groups:
        return []
    first_launch: dict[Path, float] = {}
    for group in groups.values():
        job_path = group["job_json"]
        first_launch[job_path] = min(first_launch.get(job_path, group["launched_at"]),
                                     group["launched_at"])
    numbers = assign_print_numbers(first_launch, numbers_path)
    registry = _load_number_registry(numbers_path)
    launches: list[dict[str, Any]] = registry["launches"]
    dirty = False
    # Seed a launch entry for every job the registry already numbers: its
    # number belongs to the job's earliest surviving launch group.
    for job_key, number in sorted(numbers.items(), key=lambda item: item[1]):
        if any(entry["number"] == number for entry in launches):
            continue
        job_groups = sorted((g for g in groups.values() if str(g["job_json"]) == job_key),
                            key=lambda g: (g["launched_at"], g["artifact"]))
        if not job_groups:
            continue
        first = job_groups[0]
        launches.append({"number": number, "job": job_key, "artifact": first["artifact"],
                         "at": first["launched_at"]})
        dirty = True
    # Number every surviving launch group, oldest first. A group matches the
    # newest launch entry for its artifact when the entry was minted around
    # the launch (minting happens seconds before or after the record lands);
    # a group newer than every entry for its artifact is a launch nobody
    # minted (printer screen, family path) and gets the next number.
    next_number = max([*numbers.values(), *(e["number"] for e in launches)], default=0) + 1
    group_number: dict[str, int] = {}
    used_entries: set[int] = set()
    for group in sorted(groups.values(), key=lambda g: (g["launched_at"], g["artifact"])):
        # An entry names this group when it was minted for the same artifact,
        # or — for mints that did not record an artifact — for the same job at
        # the same time. Each entry can name only one group.
        entries = sorted((e for e in launches
                          if e["number"] not in used_entries
                          and (e.get("artifact") == group["artifact"]
                               or (not e.get("artifact") and e.get("job") == str(group["job_json"])))),
                         key=lambda e: e["at"])
        matched = entries[-1] if entries and entries[-1]["at"] >= group["launched_at"] - LAUNCH_MATCH_WINDOW_S else None
        if matched is not None:
            group_number[group["artifact"]] = matched["number"]
            used_entries.add(matched["number"])
            continue
        entry = {"number": next_number, "job": str(group["job_json"]),
                 "artifact": group["artifact"], "at": group["launched_at"]}
        next_number += 1
        launches.append(entry)
        group_number[group["artifact"]] = entry["number"]
        dirty = True
    if dirty:
        _save_number_registry(registry, numbers_path)

    def title_of(job_path: Path) -> str:
        try:
            title = json.loads(job_path.read_text(encoding="utf-8")).get("title")
        except (OSError, ValueError):
            title = None
        return str(title or job_path.parent.name)

    rows: list[dict[str, Any]] = []
    matched_numbers: set[int] = set()
    for group in groups.values():
        number = group_number[group["artifact"]]
        matched_numbers.add(number)
        rows.append({"title": title_of(group["job_json"]), "job_json": group["job_json"],
                     "record": group["record"], "launched_at": group["launched_at"],
                     "index": number})
    # Earlier launches of an artifact whose record file was later rewritten
    # by a reprint: still real prints with their own numbers. Their reprint
    # target is the same job/artifact, reached through its surviving record.
    for entry in launches:
        if entry["number"] in matched_numbers:
            continue
        group = groups.get(str(entry.get("artifact") or ""))
        job_path = Path(str(entry.get("job") or ""))
        if group is None and job_path:
            job_groups = [g for g in groups.values() if str(g["job_json"]) == str(job_path)]
            group = max(job_groups, key=lambda g: g["launched_at"]) if job_groups else None
        if group is None or not job_path.is_file():
            continue
        matched_numbers.add(entry["number"])
        rows.append({"title": title_of(job_path), "job_json": job_path,
                     "record": group["record"], "launched_at": float(entry["at"]),
                     "index": entry["number"]})
    rows.sort(key=lambda r: (-r["launched_at"], -r["index"]))
    return rows


# A launch entry minted at start time and the launch record file for the same
# print land seconds apart (the record is written just before the mint on the
# generic path, just after it on the mapped path). An entry this close to a
# surviving record group is that group's launch; anything older is history.
LAUNCH_MATCH_WINDOW_S = 3600.0


_REGISTRY_HELD = threading.local()
_REGISTRY_THREAD_LOCK = threading.RLock()


class _RegistryLock:
    """Serialize every read-modify-write of the print-number registry.

    Each printer starts prints from its own print-now process, and the bot
    renders /print-again lists in another: without a lock two starts at once
    can mint the same number, and a list written over a start drops that
    start's launch entry. An O_EXCL lock file (cross-process) plus an RLock
    (threads); re-entrant per thread, since printed_jobs() calls
    assign_print_numbers() while holding it. A lock file older than a minute
    belongs to a crashed holder and is broken.
    """

    def __init__(self, numbers_path: Path | None = None, wait_s: float = 10.0, stale_s: float = 60.0):
        path = Path(numbers_path or PRINT_NUMBERS)
        self.lock = path.with_suffix(path.suffix + ".lock")
        self.key = str(self.lock.resolve())
        self.wait_s, self.stale_s = wait_s, stale_s

    def _depths(self) -> dict[str, int]:
        if not hasattr(_REGISTRY_HELD, "depths"):
            _REGISTRY_HELD.depths = {}
        return _REGISTRY_HELD.depths

    def __enter__(self):
        depths = self._depths()
        if depths.get(self.key):
            depths[self.key] += 1
            return self
        _REGISTRY_THREAD_LOCK.acquire()
        try:
            self.lock.parent.mkdir(parents=True, exist_ok=True)
            deadline = time.monotonic() + self.wait_s
            while True:
                try:
                    os.close(os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                    break
                except FileExistsError:
                    try:
                        if time.time() - self.lock.stat().st_mtime > self.stale_s:
                            self.lock.unlink(missing_ok=True)
                            continue
                    except OSError:
                        continue
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"print number registry is locked: {self.lock}")
                    time.sleep(0.05)
        except BaseException:
            _REGISTRY_THREAD_LOCK.release()
            raise
        depths[self.key] = 1
        return self

    def __exit__(self, *exc):
        depths = self._depths()
        depths[self.key] -= 1
        if not depths[self.key]:
            del depths[self.key]
            try:
                self.lock.unlink(missing_ok=True)
            finally:
                _REGISTRY_THREAD_LOCK.release()
        return False


def _load_number_registry(numbers_path: Path | None = None) -> dict[str, Any]:
    """The print-number registry: {'numbers': {job: first number}, 'launches': [...]}.

    'numbers' is the original per-job map and keeps its meaning (a job's FIRST
    print number) so every existing reader keeps working. 'launches' is the
    per-print ledger: one entry per launch, minted in launch order.
    """
    path = Path(numbers_path or PRINT_NUMBERS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    numbers = data.get("numbers") or {}
    numbers = {str(k): int(v) for k, v in numbers.items()
               if isinstance(v, int) and not isinstance(v, bool) and v > 0}
    launches: list[dict[str, Any]] = []
    for entry in data.get("launches") or []:
        if not isinstance(entry, dict):
            continue
        number = entry.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            continue
        try:
            at = float(entry.get("at") or 0)
        except (TypeError, ValueError):
            at = 0.0
        launches.append({"number": number, "job": str(entry.get("job") or ""),
                         "artifact": str(entry.get("artifact") or ""), "at": at})
    launches.sort(key=lambda e: (e["number"]))
    return {"numbers": numbers, "launches": launches}


def _job_json_key(job_path: Path | str) -> str:
    """The registry key for a job: its resolved JOB.json (a job folder works too)."""
    path = Path(job_path)
    if path.name != "JOB.json":
        path = path / "JOB.json"
    return str(path.resolve())


def latest_number_for_job(job_path: Path | str, numbers_path: Path | None = None) -> int | None:
    """The number of a job's NEWEST print (the one running or just finished).

    A job printed three times has three numbers; a reader that only knows the
    job (a listener render with no start-record context) means its latest
    print, never its first. Falls back to the legacy first-print number for
    a job printed before per-launch numbering. Read-only.
    """
    key = _job_json_key(job_path)
    registry = _load_number_registry(numbers_path)
    own = [e["number"] for e in registry["launches"] if e.get("job") == key]
    if own:
        return max(own)
    return registry["numbers"].get(key)


def number_for_launch(job_path: Path | str, at: float, numbers_path: Path | None = None,
                      exclude: set[int] | frozenset[int] = frozenset()) -> int | None:
    """The number minted for the launch of ``job_path`` nearest ``at``, or None.

    For a record that lost its own number: matches the job's launch entry
    closest in time (within LAUNCH_MATCH_WINDOW_S), skipping ``exclude``.
    A job with launch entries but none near ``at`` gets None — never another
    print's number. A job with no launch entries at all (printed before
    per-launch numbering) keeps its legacy first-print number. Read-only.
    """
    key = _job_json_key(job_path)
    registry = _load_number_registry(numbers_path)
    own = [e for e in registry["launches"] if e.get("job") == key]
    if not own:
        legacy = registry["numbers"].get(key)
        return legacy if legacy not in exclude else None
    near = [e for e in own if e["number"] not in exclude
            and abs(float(e.get("at") or 0) - at) <= LAUNCH_MATCH_WINDOW_S]
    return min(near, key=lambda e: abs(float(e.get("at") or 0) - at))["number"] if near else None


def _save_number_registry(registry: dict[str, Any], numbers_path: Path | None = None) -> None:
    path = Path(numbers_path or PRINT_NUMBERS)
    payload = {"numbers": registry["numbers"],
               "launches": sorted(registry["launches"], key=lambda e: e["number"])}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def assign_print_numbers(first_launch: dict[Path, float], numbers_path: Path | None = None) -> dict[str, int]:
    with _RegistryLock(numbers_path):
        return _assign_print_numbers_locked(first_launch, numbers_path)


def _assign_print_numbers_locked(first_launch: dict[Path, float], numbers_path: Path | None = None) -> dict[str, int]:
    """Permanent FIRST-print numbers per job. Existing numbers never change; new jobs append in launch order."""
    registry = _load_number_registry(numbers_path)
    numbers = registry["numbers"]
    fresh = sorted((p for p in first_launch if str(p) not in numbers), key=lambda p: (first_launch[p], str(p)))
    if fresh:
        nxt = max([*numbers.values(), *(e["number"] for e in registry["launches"])], default=0) + 1
        for p in fresh:
            numbers[str(p)] = nxt
            nxt += 1
        _save_number_registry(registry, numbers_path)
    return numbers


def mint_launch_number(job_path: Path, at: float | None = None, artifact: Any = None,
                       numbers_path: Path | None = None) -> int:
    with _RegistryLock(numbers_path):
        return _mint_launch_number_locked(job_path, at, artifact, numbers_path)


def _mint_launch_number_locked(job_path: Path, at: float | None = None, artifact: Any = None,
                       numbers_path: Path | None = None) -> int:
    """Mint the next permanent print number for ONE launch of a job.

    Every launch mints its own number, including a reprint of a job that
    already has one. The job's first mint also becomes its legacy 'numbers'
    entry, so old readers (and the number itself, for 'print N' lookups of
    that first print) keep working unchanged.
    """
    registry = _load_number_registry(numbers_path)
    numbers, launches = registry["numbers"], registry["launches"]
    number = max([*numbers.values(), *(e["number"] for e in launches)], default=0) + 1
    key = str(Path(job_path))
    launches.append({"number": number, "job": key,
                     "artifact": str(artifact) if artifact else "", "at": float(at or time.time())})
    if key not in numbers:
        numbers[key] = number
    _save_number_registry(registry, numbers_path)
    return number


def _fold(text: str) -> str:
    """The shared fold — ``core.fuzzy.norm`` is the one implementation.

    Title matching therefore folds the same way everywhere: diacritics
    fold to their base letters and CJK titles stay matchable by name
    instead of folding away to nothing.
    """
    return fuzzy.norm(text)


def match_printed(query: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Case and punctuation insensitive title match. An exact title beats substring hits."""
    q = _fold(query)
    if not q:
        return []
    exact = [r for r in rows if _fold(r["title"]) == q]
    if exact:
        return exact
    words = q.split()
    return [r for r in rows if all(w in _fold(r["title"]) for w in words)]


FUZZY_SCAN_LIMIT = 500


def fuzzy_printed(query: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """#468 stage 2: typo-tolerant near matches over printed titles, best score first.

    Suggestion-only by construction: resolve_print turns these into a
    numbered pick-list and still refuses — a fuzzy hit is never resolved to
    a target, not even a single high-confidence one, and print numbers never
    reach here (the bare-integer path in resolve_print stays exact, #436).
    Scores come from core.fuzzy (floor 65, windowed for long titles); ties
    keep the rows' newest-first order. Only the newest ``FUZZY_SCAN_LIMIT``
    rows are fuzzy-scored — the archive grows unbounded and the windowed
    scorer is the expensive pass; exact and substring matching above still
    scan every row, so an old print remains findable by its real name.
    """
    scored = [(fuzzy.windowed_fuzzy_score(query, r["title"]), pos, r)
              for pos, r in enumerate(rows[:FUZZY_SCAN_LIMIT])]
    scored = [entry for entry in scored if entry[0]]
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    return [row for _score_value, _pos, row in scored]


def printed_list_text(rows: list[dict[str, Any]], limit: int = AGAIN_LIST_LIMIT) -> str:
    if not rows:
        return "No archived print has been started yet."
    lines = [f"{r['index']}. {r['title']} ({_et(r['launched_at'])})" for r in rows[:limit]]
    more = f"\n…and {len(rows) - limit} older. Search by name: /print-again <name>" if len(rows) > limit else ""
    return "Printed before, newest first:\n" + "\n".join(lines) + more + "\nReprint one with /print-again <number> or /print-again <name>."


def _et(ts: float) -> str:
    from datetime import datetime
    local = datetime.fromtimestamp(ts, _config.tz())
    return local.strftime("%b %-d, %-I:%M %p ") + _config.tz_label(local)


def resolve_print(query: str = "", archive: Path | None = None) -> dict[str, Any]:
    """Resolve '/print-again [N | name]' to one exact target. Never guesses between several.

    Empty query -> the last print. A bare integer -> that row of printed_jobs().
    Anything else -> case insensitive title match; several matches raise with a
    numbered list so the operator can pick by number. When nothing matches
    exactly, typo-close titles (#468 stage 2) raise the same kind of numbered
    list as a "Did you mean" pick-list — a fuzzy hit never resolves directly.
    """
    from .print_next import NextPlateError

    archive = (archive or ARCHIVE).resolve()
    query = str(query or "").strip()
    if not query:
        return resolve_last_print(archive)
    rows = printed_jobs(archive)
    if not rows:
        raise NextPlateError("no archived print has been started yet")
    if re.fullmatch(r"#?\d+", query):
        n = int(query.lstrip("#"))
        hit = next((r for r in rows if r["index"] == n), None)
        if hit is None:
            raise NextPlateError(f"there is no print #{n}. Type /print-again list")
        return _target_from_record(hit["record"], archive)
    if parse_plate_ref(query):
        # An exact plate alias (82A) targets that plate (#1008). When no
        # project plate carries the alias, fall through to the title paths —
        # a model can legitimately be named like one.
        try:
            return resolve_plate_ref(query, archive)
        except NextPlateError:
            pass
    hits = match_printed(query, rows)
    if not hits:
        near = fuzzy_printed(query, rows)
        if near:
            options = "\n".join(f"{r['index']}. {r['title']} ({_et(r['launched_at'])})" for r in near[:AGAIN_LIST_LIMIT])
            raise NextPlateError(
                f"no printed job matches \"{query}\" exactly. Did you mean one of these?\n{options}\n"
                "Pick one with /print-again <number>")
        raise NextPlateError(f"no printed job matches \"{query}\". Type /print-again list")
    if len(hits) > 1:
        options = "\n".join(f"{r['index']}. {r['title']} ({_et(r['launched_at'])})" for r in hits[:AGAIN_LIST_LIMIT])
        raise NextPlateError(f"\"{query}\" matches {len(hits)} printed jobs:\n{options}\nPick one with /print-again <number>")
    return _target_from_record(hits[0]["record"], archive)


_PLATE_REF = re.compile(r"#?(\d+)([A-Za-z]+)")


def parse_plate_ref(query: str) -> tuple[int, str] | None:
    """'82A' / '#82aa' -> (82, 'AA'); anything else -> None. Read only."""
    m = _PLATE_REF.fullmatch(str(query or "").strip())
    if not m:
        return None
    return int(m.group(1)), m.group(2).upper()


def _project_number_of(job_path: Path, rows: list[dict[str, Any]]) -> int | None:
    """A job's registry number: its earliest launch's row index.

    The project number anchors on the job's own permanent number (the first
    plate launched claims it for the project; see print_records), which is
    exactly the earliest printed_jobs row for this job.
    """
    mine = [r for r in rows if Path(r["job_json"]).resolve() == job_path.resolve()]
    if not mine:
        return None
    return int(min(mine, key=lambda r: r["launched_at"])["index"])


def project_for_job(job_path: Path, archive: Path | None = None,
                    numbers_path: Path | None = None, plans_dir: Path | None = None,
                    _rows: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The multi-plate project an archived job belongs to, or None.

    Plates and component labels come from the JOB.json's embedded
    multipart_plan (slice-time structure); completion counts are refreshed
    from the live plan file when one exists, because the embedded copy is
    never updated after slicing. The project number comes from the plan
    state when a launch claimed one, else from the job's registry number —
    never invented (core.projects.project_ref refuses unknown numbers).
    """
    job_path = Path(job_path)
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    plan = job.get("multipart_plan")
    if not job.get("project_id") or not isinstance(plan, dict):
        return None
    parts = [p for p in plan.get("parts") or [] if isinstance(p, dict)]
    if len(parts) < 2:
        return None
    live: dict[str, Any] = {}
    if plan.get("plan_id"):
        try:
            from .print_next import load_plan

            live = load_plan(str(plan["plan_id"]), plans_dir) or {}
        except Exception:
            live = {}
    live_parts = {str(p.get("id")): p for p in live.get("parts") or [] if isinstance(p, dict)}
    number: int | None = None
    for source in (live, plan):
        n = source.get("project_number")
        if isinstance(n, int) and not isinstance(n, bool) and n > 0:
            number = n
            break
    if number is None:
        rows = _rows if _rows is not None else printed_jobs(archive, numbers_path)
        number = _project_number_of(job_path, rows)
    plates: list[dict[str, Any]] = []
    for pos, part in enumerate(parts):
        suffix = part.get("project_plate_suffix")
        if not isinstance(suffix, str) or not re.fullmatch(r"[A-Z]+", suffix):
            suffix = projects.plate_suffix(pos)
        live_part = live_parts.get(str(part.get("id")), {})

        def _count(key: str, default: int) -> int:
            for source in (live_part, part):
                value = source.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    return value
            return default

        quantity = _count("quantity", 1) or 1
        plates.append({
            "part_id": part.get("id"), "suffix": suffix,
            "ref": projects.project_ref(number, suffix),
            "label": str(part.get("label") or part.get("id") or f"plate {pos + 1}"),
            "gcode": part.get("gcode_path"),
            "quantity": quantity,
            "completed_quantity": min(_count("completed_quantity", 0), quantity),
        })
    return {"project_id": str(job["project_id"]),
            "project_number": number,
            "title": str(plan.get("title") or job.get("title") or job_path.parent.name),
            "plan_id": plan.get("plan_id"),
            "job_json": str(job_path),
            "plates": plates}


def target_project_info(target: dict[str, Any], archive: Path | None = None,
                        numbers_path: Path | None = None,
                        plans_dir: Path | None = None) -> dict[str, Any] | None:
    """The project a resolved /print-again target belongs to, or None."""
    if not target.get("job_json"):
        return None
    return project_for_job(Path(str(target["job_json"])), archive, numbers_path, plans_dir)


def project_plate_for_target(info: dict[str, Any],
                             target: dict[str, Any]) -> dict[str, Any] | None:
    """The plate of ``info`` whose G-code the resolved target would print."""
    gcode = str(Path(str(target.get("gcode") or "")).resolve())
    for plate in info["plates"]:
        if plate.get("gcode") and str(Path(str(plate["gcode"])).resolve()) == gcode:
            return plate
    return None


def _launch_record_for(gcode: Path) -> Path | None:
    stem = gcode.name.split(".", 1)[0]
    for suffix in (".launch.json", ".upload.json"):
        candidate = gcode.with_name(stem + suffix)
        if candidate.is_file():
            return candidate
    return None


def _plate_target(plate: dict[str, Any], info: dict[str, Any],
                  archive: Path) -> dict[str, Any]:
    """A start target for one project plate, from its own launch record.

    Only plates that were actually launched can be reprinted this way; a
    plate that never printed has no verified G-code launch to repeat and
    stays on the normal /print-next path.
    """
    from .print_next import NextPlateError

    if not plate.get("gcode"):
        raise NextPlateError(f"plate {plate['suffix']} has no recorded G-code")
    record = _launch_record_for(Path(str(plate["gcode"])).resolve())
    if record is None:
        raise NextPlateError(
            f"plate {plate['ref'] or plate['suffix']} was never printed; "
            "it cannot be reprinted with /print-again")
    target = _target_from_record(record, archive)
    target.update({"project_id": info["project_id"],
                   "project_number": info["project_number"],
                   "project_title": info["title"],
                   "project_plate_ref": plate["ref"],
                   "project_plate_suffix": plate["suffix"]})
    return target


def resolve_plate_ref(query: str, archive: Path | None = None,
                      numbers_path: Path | None = None,
                      plans_dir: Path | None = None) -> dict[str, Any]:
    """Resolve an exact plate alias ('82A') to that plate's reprint target.

    Exact by construction: the number must be a project's anchored number
    and the suffix one of its plates. Anything else raises — an alias never
    falls back to a fuzzy or title guess.
    """
    from .print_next import NextPlateError

    parsed = parse_plate_ref(query)
    if parsed is None:
        raise NextPlateError(f"\"{query}\" is not a plate reference like 82A")
    number, suffix = parsed
    archive = (archive or ARCHIVE).resolve()
    rows = printed_jobs(archive, numbers_path)
    seen: set[str] = set()
    for row in rows:
        job_key = str(Path(row["job_json"]).resolve())
        if job_key in seen:
            continue
        seen.add(job_key)
        info = project_for_job(Path(job_key), archive, numbers_path, plans_dir, _rows=rows)
        if info is None or info["project_number"] != number:
            continue
        for plate in info["plates"]:
            if plate["suffix"] == suffix:
                return _plate_target(plate, info, archive)
        raise NextPlateError(
            f"project {number} has no plate {number}{suffix}; its plates are "
            + ", ".join(p["ref"] or p["suffix"] for p in info["plates"]))
    raise NextPlateError(f"there is no project print {number}{suffix}. Type /print-again list")


def project_scope_targets(target: dict[str, Any], scope: str,
                          archive: Path | None = None,
                          numbers_path: Path | None = None,
                          plans_dir: Path | None = None) -> list[dict[str, Any]]:
    """Ordered reprint targets for a project scope ('entire' or 'remaining').

    'remaining' keeps only plates whose live plan counts are unfinished.
    Plates that were never launched are skipped with the rest — reprinting
    repeats verified launches; unprinted plates belong to /print-next.
    Every target is annotated with its scope note for the confirmations.
    """
    from .print_next import NextPlateError

    archive = (archive or ARCHIVE).resolve()
    info = target_project_info(target, archive, numbers_path, plans_dir)
    if info is None:
        raise NextPlateError("that print is not part of a multi-plate project")
    plates = info["plates"]
    if scope == "remaining":
        plates = [p for p in plates if p["completed_quantity"] < p["quantity"]]
        if not plates:
            raise NextPlateError(
                f"every plate of project {info['project_number']} is already printed; "
                "nothing remains. Pick 'Entire project' to print it all again")
    elif scope != "entire":
        raise NextPlateError(f"unknown reprint scope \"{scope}\"")
    targets: list[dict[str, Any]] = []
    for plate in plates:
        try:
            targets.append(_plate_target(plate, info, archive))
        except NextPlateError:
            continue
    if not targets:
        raise NextPlateError("none of those plates has a verified earlier print to repeat")
    label = (f"entire project {info['project_number']}" if scope == "entire"
             else f"the remaining plates of project {info['project_number']}")
    for pos, t in enumerate(targets):
        ref = t.get("project_plate_ref") or t.get("project_plate_suffix")
        t["scope_note"] = (f"Scope: {label} — {len(targets)} "
                           f"{'plate' if len(targets) == 1 else 'plates'}. "
                           f"This is plate {ref}, {pos + 1} of {len(targets)}; "
                           "each following plate asks for its own bed-clear confirmation.")
    return targets


def scope_question(target: dict[str, Any], info: dict[str, Any]) -> str:
    """The /print-again scope question for a project plate (#1008).

    A choice, never an authorization: whichever scope is picked, the first
    plate still goes through the normal bed-clear confirmation, and every
    later plate asks again via /print-again next.
    """
    plate = project_plate_for_target(info, target)
    ref = (plate or {}).get("ref") or (plate or {}).get("suffix") or "one plate"
    number = info["project_number"]
    head = f"Project {number} — {info['title']}" if number else f"Project — {info['title']}"
    return (f"{head} has {len(info['plates'])} plates, and you picked plate {ref}. "
            "Print just this plate, the entire project, or only the plates that are not finished yet?")


def project_plate_list_text(info: dict[str, Any]) -> str:
    """The 'Choose a plate' answer: every plate with its exact alias."""
    number = info["project_number"]
    head = f"Project {number} — {info['title']}:" if number else f"Project — {info['title']}:"
    lines = [head]
    for plate in info["plates"]:
        state = ("printed" if plate["completed_quantity"] >= plate["quantity"]
                 else "not finished")
        ref = plate["ref"]
        pick = f"/print-again {ref}" if ref else "its print number from /print-again list"
        lines.append(f"{ref or plate['suffix']}: {plate['label']} — {state}. Reprint it with {pick}.")
    lines.append("Nothing started.")
    return "\n".join(lines)


def _record_printer_id(record: Path, archive: Path) -> str | None:
    """The roster printer a launch record's job belongs to (its JOB.json's
    recorded printer_id, else the default printer), or None when unreadable."""
    job_path = _job_for(_artifact_for(record).resolve(), archive)
    if job_path is None:
        return None
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    from ..core import model_job

    return str(model_job._job_printer_id(job))


def resolve_last_print(archive: Path | None = None, printer_id: str | None = None) -> dict[str, Any]:
    """Exact target for 'print the last one again', from local launch records only. Read only.

    With printer_id, only that printer's launches count, so a routed channel
    gets its own printer's last print instead of a refusal when another
    printer printed more recently (#979).
    """
    from .print_next import NextPlateError

    archive = (archive or ARCHIVE).resolve()
    records = _launch_records(archive)
    if printer_id is not None:
        records = [r for r in records if _record_printer_id(r, archive) == str(printer_id)]
    if not records:
        raise NextPlateError("no archived print has been started yet")
    return _target_from_record(records[0], archive)


def _target_from_record(record: Path, archive: Path) -> dict[str, Any]:
    from .print_next import NextPlateError, _sha256

    launched = _artifact_for(record).resolve()
    job_path = _job_for(launched, archive)
    if job_path is None:
        raise NextPlateError(f"the launch record {record} has no archived JOB.json")
    job = json.loads(job_path.read_text(encoding="utf-8"))
    current = _current_artifacts(job)
    if launched in current and launched.is_file():
        gcode, resliced = launched, False
    elif job.get("gcode") and not job.get("derived_plates") and Path(job["gcode"]).is_file():
        gcode, resliced = Path(job["gcode"]).resolve(), True
    else:
        raise NextPlateError("the last print's slice is no longer current and the job has several plates; re-slice it first")
    title = job.get("title") or job_path.parent.name
    return {"plan_id": "print-again", "part_id": job_path.parent.name[:60], "label": title, "where": "",
            "title": title, "copy": int(time.time()), "quantity": 1, "reprint": True, "resliced": resliced,
            "job_json": str(job_path), "gcode": str(gcode), "gcode_sha256": _sha256(gcode)}


def _summary():
    from . import discord_print_summary
    return discord_print_summary


def _copy_figures(gcode) -> tuple[float | None, float | None]:
    """(printing seconds, filament grams) for ONE copy of a reprint G-code; (None, None) when unknown."""
    try:
        return _summary().gcode_metadata(gcode)
    except Exception:
        return None, None


def _known(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _totals_clause(seconds, grams, count: int, label: str) -> str:
    """'all 3 copies: ~3h 0m printing (plus ~5m warmup per copy), ~36 g filament'; '' when nothing is known."""
    summary = _summary()
    bits = []
    if _known(seconds):
        bits.append(f"~{summary.human(seconds * count)} printing "
                    f"(plus ~{summary.human(summary.WARMUP_S)} warmup per copy)")
    if _known(grams):
        bits.append(f"~{grams * count:g} g filament")
    return f"{label}: " + ", ".join(bits) if bits else ""


def _per_copy_clause(seconds, grams) -> str:
    """'~1h 0m printing, ~12 g filament' for one copy; '' when nothing is known."""
    summary = _summary()
    bits = []
    if _known(seconds):
        bits.append(f"~{summary.human(seconds)} printing")
    if _known(grams):
        bits.append(f"~{grams:g} g filament")
    return ", ".join(bits)


def again_question(target: dict[str, Any]) -> str:
    note = " It was re-sliced since the last run, so this uses the current slice." if target.get("resliced") else ""
    if target.get("scope_note"):
        note += f" {target['scope_note']}"
    copies = int(target.get("copies") or 1)
    many = (f" Copy 1 of {copies}; after each one finishes I'll ask you to clear the bed for the next."
            if copies > 1 else "")
    figures = ""
    if copies > 1:
        # Sequential copies (#438): quote this copy's figures and the all-copies
        # totals, never one copy's numbers as the job's. Unknown stays absent.
        seconds, grams = _copy_figures(target.get("gcode"))
        per, totals = _per_copy_clause(seconds, grams), _totals_clause(seconds, grams, copies, f"All {copies} copies")
        if per and totals:
            figures = f" Each copy: {per}. {totals}."
    return f"Print {target['title']} again from the start?{note}{many}{figures}\nConfirm the bed is clear."


def next_copy_question(target: dict[str, Any]) -> str:
    """Confirmation for `/print-again next`: this copy's figures plus the remaining totals (#438)."""
    if target.get("project_sequence"):
        ref = target.get("project_plate_ref") or target.get("project_plate_suffix") or "next"
        question = (f"Print plate {ref} ({target['title']}) of project "
                    f"{target.get('project_number')}?")
        per = _per_copy_clause(*_copy_figures(target.get("gcode")))
        if per:
            question += f"\nThis plate: {per}."
        return question + "\nConfirm the bed is clear."
    n, total = int(target["copy_number"]), int(target["copies"])
    question = f"Print copy {n} of {total} of {target['title']}?"
    seconds, grams = _copy_figures(target.get("gcode"))
    left = total - n + 1
    sentences = []
    per = _per_copy_clause(seconds, grams)
    if per:
        sentences.append(f"This copy: {per}.")
    if left > 1:
        totals = _totals_clause(seconds, grams, left, f"All {left} remaining copies")
        if totals:
            sentences.append(totals + ".")
    if sentences:
        question += "\n" + " ".join(sentences)
    return question + "\nConfirm the bed is clear."


def gcode_start_targets(text: str) -> dict[str, float]:
    """Nozzle and bed targets the G-code's start sequence sets, read from the file that is uploaded.

    START_PRINT/PRINT_START arguments win; otherwise the first M104/M109 and M140/M190 S values.
    Missing values are left out, never guessed.
    """
    targets: dict[str, float] = {}
    for line in str(text or "").splitlines()[:4000]:
        line = line.split(";", 1)[0].strip()
        if not line:
            continue
        word = line.split()[0].upper()
        if word in ("START_PRINT", "PRINT_START"):
            for key, names in (("nozzle", ("EXTRUDER_TEMP", "HOTEND", "EXTRUDER")), ("bed", ("BED_TEMP", "BED"))):
                for name in names:
                    m = re.search(rf"\b{name}=(\d+(?:\.\d+)?)", line, re.I)
                    if m and float(m.group(1)) > 0:
                        targets[key] = float(m.group(1))
                        break
            if len(targets) == 2:
                break
        elif word in ("M104", "M109", "M140", "M190"):
            key = "nozzle" if word in ("M104", "M109") else "bed"
            m = re.search(r"\bS(\d+(?:\.\d+)?)", line)
            if key not in targets and m and float(m.group(1)) > 0:
                targets[key] = float(m.group(1))
        elif word.startswith("G1") and targets:
            break
    return targets


def start_targets_text(text: str) -> str:
    """'Nozzle 220 °C · Bed 60 °C' for the Preparing to start post, or '' when the file sets neither."""
    targets = gcode_start_targets(text)
    parts = [f"{label} {targets[key]:g} °C" for key, label in (("nozzle", "Nozzle"), ("bed", "Bed")) if key in targets]
    return " · ".join(parts)


def start_estimate_text(target: dict[str, Any], seconds, grams) -> str:
    """The estimate clause of the 'Preparing to start' post for a (re)print.

    One copy keeps the historical wording exactly. Sequential copies (#438)
    label the figures as this copy's and add the remaining-copies totals, so
    one copy's time is never presented as the whole job's.
    """
    summary = _summary()
    if _known(seconds):
        base = (f"estimated print time ~{summary.human(seconds)}, plus warmup "
                f"~{summary.human(seconds + summary.WARMUP_S)} total")
    else:
        base = "estimated print time unknown"
    copies = int(target.get("copies") or 1)
    if copies <= 1:
        return base
    left = max(1, copies - int(target.get("copy_number") or 1) + 1)
    if left <= 1:
        return base
    label = f"all {copies} copies" if int(target.get("copy_number") or 1) == 1 else f"all {left} remaining copies"
    totals = _totals_clause(seconds, grams, left, label)
    if not totals:
        return base
    return f"{base}{' for this copy' if _known(seconds) else ''}; {totals}"


def split_count(query: str) -> tuple[str, int]:
    """'12 x3', 'shelf x 3', '12 3 copies', 'x2' -> (query, copies). Bare numbers are never counts."""
    q = str(query or "").strip()
    m = re.fullmatch(r"(.*?)\s*(?:\bx\s*(\d+)|\b(\d+)\s+(?:copies|copy|times))", q, flags=re.I)
    if m:
        copies = int(m.group(2) or m.group(3))
        if not 1 <= copies <= MAX_COPIES:
            from .print_next import NextPlateError
            raise NextPlateError(f"copies must be 1-{MAX_COPIES}")
        return m.group(1).strip(), copies
    return q, 1


def _default_printer_id() -> str | None:
    try:
        return str(_config.cfg()["printers"][0]["id"])
    except Exception:
        return None


def _queue_path(path: Path | None = None, printer_id: str | None = None) -> Path:
    """The reprint queue file for one printer (P2-135).

    Pending copies are per printer: one global file let printer B's
    /print-again delete or replace printer A's remaining copies. The
    default printer keeps the legacy singleton path (its queue was always
    the singleton's, so nothing is orphaned at upgrade); every other
    printer gets <state>/printers/<id>/reprint_queue.json, the #334
    layout. An explicit path always wins.
    """
    if path is not None:
        return Path(path)
    if printer_id and str(printer_id) != _default_printer_id():
        return _config.state_dir() / "printers" / str(printer_id) / "reprint_queue.json"
    return REPRINT_QUEUE


def _read_queue(path: Path | None = None, printer_id: str | None = None) -> dict[str, Any]:
    try:
        value = json.loads(_queue_path(path, printer_id).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_queue(value: dict[str, Any] | None, path: Path | None = None,
                 printer_id: str | None = None) -> None:
    target = _queue_path(path, printer_id)
    if not value:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def queue_copies(target: dict[str, Any], path: Path | None = None,
                 printer_id: str | None = None) -> None:
    """Remember the remaining copies of a multi-copy reprint. Starts nothing."""
    copies = int(target.get("copies") or 1)
    if copies <= 1:
        _write_queue(None, path, printer_id)
        return
    _write_queue({"job_json": target["job_json"], "gcode": target["gcode"], "title": target["title"],
                  "gcode_sha256": target["gcode_sha256"], "total": copies, "started": 1,
                  "queued_at": time.time(), "claim_nonce": secrets.randbits(60) + 1}, path, printer_id)


def queue_project_sequence(targets: list[dict[str, Any]], path: Path | None = None,
                           printer_id: str | None = None) -> None:
    """Remember the plates of a project-scope reprint not yet started (#1008).

    Written after the first plate starts, mirroring queue_copies: ``started``
    counts the plates already started, and /print-again next walks the rest
    in order, each behind its own bed-clear confirmation. Starts nothing.
    A fresh run's plates are a new logical run — the original project's plan
    counters are never touched by reprints.
    """
    if len(targets) <= 1:
        _write_queue(None, path, printer_id)
        return
    first = targets[0]
    _write_queue({"kind": "project_sequence",
                  "project_id": first.get("project_id"),
                  "project_number": first.get("project_number"),
                  "title": first.get("project_title") or first["title"],
                  "entries": [{"job_json": t["job_json"], "gcode": t["gcode"],
                               "title": t["title"], "gcode_sha256": t["gcode_sha256"],
                               "plate_ref": t.get("project_plate_ref"),
                               "plate_suffix": t.get("project_plate_suffix")}
                              for t in targets],
                  "total": len(targets), "started": 1,
                  "queued_at": time.time(), "claim_nonce": secrets.randbits(60) + 1}, path, printer_id)


def pending_copy(path: Path | None = None, printer_id: str | None = None) -> dict[str, Any] | None:
    q = _read_queue(path, printer_id)
    return q if q and int(q.get("started", 0)) < int(q.get("total", 0)) else None


def _queued_claim_copy(queue: dict[str, Any], index: int) -> int:
    """Stable per-queued-entry claim identity, including older saved queues."""
    nonce = queue.get("claim_nonce")
    if type(nonce) is not int or nonce <= 0:
        packed = json.dumps(queue, sort_keys=True, separators=(",", ":")).encode("utf-8")
        nonce = int.from_bytes(hashlib.sha256(packed).digest()[:8], "big") >> 4
    return nonce + index


def _queued_gcode_hash(gcode: Path, expected: Any) -> str:
    """Refuse altered or unidentifiable artifacts before asking to start."""
    from .print_next import NextPlateError, _sha256

    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise NextPlateError("the queued print has no valid G-code fingerprint; nothing started")
    if _sha256(gcode) != expected:
        raise NextPlateError("the queued G-code changed since it was queued; nothing started. "
                             "Cancel the queue and select the original print again")
    return expected


def next_copy_target(path: Path | None = None, archive: Path | None = None,
                     printer_id: str | None = None) -> dict[str, Any]:
    """Exact target for the next queued copy, re-verified against the archive. Read only."""
    from .print_next import NextPlateError

    q = pending_copy(path, printer_id)
    if q is None:
        raise NextPlateError("no copies are queued")
    archive = (archive or ARCHIVE).resolve()
    if q.get("kind") == "project_sequence":
        entries = q.get("entries") or []
        idx = int(q.get("started", 0))
        if idx >= len(entries):
            raise NextPlateError("the queued project plates are all started")
        entry = entries[idx]
        gcode = Path(entry["gcode"]).resolve()
        job_path = Path(entry["job_json"]).resolve()
        if not gcode.is_file() or not job_path.is_file() or not job_path.is_relative_to(archive):
            raise NextPlateError("the queued plate's files are gone; queue cleared")
        expected = _queued_gcode_hash(gcode, entry.get("gcode_sha256"))
        return {"plan_id": "print-again", "part_id": job_path.parent.name[:60],
                "label": entry["title"], "where": "",
                "title": entry["title"], "copy": _queued_claim_copy(q, idx + 1), "copy_number": idx + 1,
                "quantity": int(q["total"]), "copies": int(q["total"]),
                "reprint": True, "resliced": False, "job_json": str(job_path),
                "gcode": str(gcode), "gcode_sha256": expected,
                "project_sequence": True, "project_id": q.get("project_id"),
                "project_number": q.get("project_number"),
                "project_title": q.get("title"),
                "project_plate_ref": entry.get("plate_ref"),
                "project_plate_suffix": entry.get("plate_suffix")}
    gcode = Path(q["gcode"]).resolve()
    job_path = Path(q["job_json"]).resolve()
    if not gcode.is_file() or not job_path.is_file() or not job_path.is_relative_to(archive):
        raise NextPlateError("the queued copy's files are gone; queue cleared")
    n = int(q["started"]) + 1
    expected = _queued_gcode_hash(gcode, q.get("gcode_sha256"))
    # Repeated taps on the same entry must hit the same one-start claim.
    return {"plan_id": "print-again", "part_id": job_path.parent.name[:60], "label": q["title"], "where": "",
            "title": q["title"], "copy": _queued_claim_copy(q, n), "copy_number": n, "quantity": int(q["total"]),
            "copies": int(q["total"]),
            "reprint": True, "resliced": False, "job_json": str(job_path), "gcode": str(gcode),
            "gcode_sha256": expected}


def mark_copy_started(path: Path | None = None, printer_id: str | None = None) -> None:
    q = _read_queue(path, printer_id)
    if q:
        q["started"] = int(q.get("started", 0)) + 1
        _write_queue(q if q["started"] < int(q.get("total", 0)) else None, path, printer_id)


def cancel_copies(path: Path | None = None, printer_id: str | None = None) -> bool:
    had = bool(_read_queue(path, printer_id))
    _write_queue(None, path, printer_id)
    return had


def next_copy_prompt(finished_filename: str, path: Path | None = None,
                     printer_id: str | None = None) -> str | None:
    """Completion line for a multi-copy reprint, or None when this file isn't the queued one.

    Uploaded names embed the G-code SHA-256, so the match is exact."""
    q = pending_copy(path, printer_id)
    if q is None:
        return None
    if q.get("kind") == "project_sequence":
        entries = q.get("entries") or []
        started = int(q.get("started", 0))
        if not 1 <= started <= len(entries):
            return None
        done = entries[started - 1]
        if not done.get("gcode_sha256") or done["gcode_sha256"] not in str(finished_filename or ""):
            return None
        ref = done.get("plate_ref") or done.get("plate_suffix") or ""
        head = f"Plate {ref} of project {q.get('project_number')} ({q.get('title')}) is done."
        if started < len(entries):
            nxt = entries[started]
            nref = nxt.get("plate_ref") or nxt.get("plate_suffix") or ""
            return (f"{head} Clear the bed, then type `/print-again next` for plate "
                    f"{nref} ({nxt['title']}). `/print-again stop` drops the remaining plates.")
        return f"{head} That was the last plate of the queued project run."
    if not q.get("gcode_sha256") or q["gcode_sha256"] not in str(finished_filename or ""):
        return None
    started, total = int(q["started"]), int(q["total"])
    left = total - started
    rest = ""
    if left > 0:
        # Updated remaining figures for the copies still to go (#438); unknown stays absent.
        seconds, grams = _copy_figures(q.get("gcode"))
        summary = _summary()
        bits = []
        if _known(seconds):
            bits.append(f"~{summary.human(seconds * left)} printing "
                        f"(plus ~{summary.human(summary.WARMUP_S)} warmup per copy)")
        if _known(grams):
            bits.append(f"~{grams * left:g} g filament")
        if bits:
            rest = f" Still to print: {left} {'copy' if left == 1 else 'copies'} ({', '.join(bits)})."
    return (f"Copy {started} of {total} of {q['title']} is done.{rest} "
            f"Clear the bed, then type `/print-again next` for copy {started + 1}. "
            "`/print-again stop` drops the remaining copies.")


def _moonraker_idle_view(printer_id: str) -> dict[str, Any]:
    """The clear_job.safe_idle Moonraker shape, read from one roster printer's Moonraker (#335).

    clear_job.moonraker() shells to the configured printer's control tool, so
    a routed reboot reads the same facts (Klipper ready, print standby, heater
    targets) from the target printer's own Moonraker instead.
    """
    data = _send("/printer/objects/query?print_stats&extruder&heater_bed&webhooks", printer_id=printer_id)
    s = (data.get("result") or {}).get("status") or {}
    return {"state": str((s.get("webhooks") or {}).get("state") or "unknown"),
            "print": {"state": str((s.get("print_stats") or {}).get("state") or "unknown")},
            "extruder": {"target": (s.get("extruder") or {}).get("target")},
            "bed": {"target": (s.get("heater_bed") or {}).get("target")}}


def force_reboot(
    wait_grace_s: float = 30,
    max_wait_s: float = 120,
    poll_s: float = 2.0,
    gcode_sender=None,
    moonraker_reader=None,
    native_reader=None,
    state_reader=None,
    printer_id: str | None = None,
) -> tuple[bool, str]:
    """Send the ZMod REBOOT macro once, then require Moonraker and native idle with zero targets."""
    st = (state_reader or (lambda: status(printer_id)))()
    if st.get("state") == "printing":
        return False, "Refused: it's actively printing. Use /print-stop first if you really want it dead. Nothing sent."
    from print_handy.core.clear_job import safe_idle

    if gcode_sender is not None:
        gcode_sender("REBOOT")
    else:
        try:
            _send("/printer/gcode/script", method="POST", data={"script": "REBOOT"}, timeout=15,
                  printer_id=printer_id)
        except Exception:
            pass  # the macro drops the connection as it reboots; readback below decides the outcome

    def default_mr():
        if printer_id:
            return _moonraker_idle_view(printer_id)
        from print_handy.core.clear_job import moonraker
        return moonraker()

    def default_native():
        from print_handy.core import printer_state
        from print_handy.core.model_job import read_stock_detail
        return read_stock_detail(printer_state.load(printer_id))

    mr_read = moonraker_reader or default_mr
    nat_read = native_reader or default_native
    time.sleep(wait_grace_s)
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        try:
            m, n = mr_read(), nat_read()
            if isinstance(m, dict) and isinstance(n, dict) and safe_idle(m, n):
                return True, "Restart complete. The printer is idle with heaters off."
        except Exception:
            pass
        time.sleep(poll_s)
    return False, f"Restart sent, but the printer didn't report idle within {wait_grace_s + max_wait_s:.0f}s. Check the printer screen. Not retrying."
