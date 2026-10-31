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

import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

from ..core import fuzzy
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
_CONTROL_LOCK = __import__('threading').Lock()
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
    if not _CONTROL_LOCK.acquire(blocking=False):
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
        _CONTROL_LOCK.release()


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


def resume(expected_filename: str = "", wait_s: float = 20, poll_s: float = 1.0,
           printer_id: str | None = None) -> tuple[bool, str]:
    """One Moonraker resume for the confirmed paused file, then read back. Never retried."""
    before = status(printer_id)
    if not resumable(before):
        return False, not_resumable_text(before)
    if expected_filename and before["filename"] and before["filename"] != expected_filename:
        return False, "A different print is paused than the one you confirmed. Nothing sent."
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
    return True, f"Resume sent, but the printer still reports {after['state']} after {wait_s:.0f}s. Check the printer screen. Not retrying."


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
    """Archived jobs that were actually sent to the printer, most recent first.

    Each job carries a permanent print number: assigned once, oldest launch first,
    and never reused or renumbered. New jobs get the next number. Only writes the
    number registry, and only when a job is seen for the first time.
    """
    archive = (archive or ARCHIVE).resolve()
    rows: list[dict[str, Any]] = []
    first_launch: dict[Path, float] = {}
    seen: set[Path] = set()
    for record in _launch_records(archive):
        job_path = _job_for(_artifact_for(record).resolve(), archive)
        if job_path is None:
            continue
        mtime = record.stat().st_mtime
        first_launch[job_path] = min(first_launch.get(job_path, mtime), mtime)
        if job_path in seen:
            continue
        seen.add(job_path)
        try:
            title = json.loads(job_path.read_text(encoding="utf-8")).get("title")
        except (OSError, ValueError):
            title = None
        rows.append({"title": str(title or job_path.parent.name),
                     "job_json": job_path, "record": record, "launched_at": mtime})
    numbers = assign_print_numbers(first_launch, numbers_path)
    for row in rows:
        row["index"] = numbers[str(row["job_json"])]
    return rows


def assign_print_numbers(first_launch: dict[Path, float], numbers_path: Path | None = None) -> dict[str, int]:
    """Permanent job numbers. Existing numbers never change; new jobs append in launch order."""
    path = Path(numbers_path or PRINT_NUMBERS)
    try:
        numbers = json.loads(path.read_text(encoding="utf-8")).get("numbers") or {}
    except (OSError, ValueError, AttributeError):
        numbers = {}
    numbers = {str(k): int(v) for k, v in numbers.items()}
    fresh = sorted((p for p in first_launch if str(p) not in numbers), key=lambda p: (first_launch[p], str(p)))
    if fresh:
        nxt = max(numbers.values(), default=0) + 1
        for p in fresh:
            numbers[str(p)] = nxt
            nxt += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"numbers": numbers}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    return numbers


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


def resolve_last_print(archive: Path | None = None) -> dict[str, Any]:
    """Exact target for 'print the last one again', from local launch records only. Read only."""
    from .print_next import NextPlateError

    archive = (archive or ARCHIVE).resolve()
    records = _launch_records(archive)
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


def _read_queue(path: Path | None = None) -> dict[str, Any]:
    try:
        value = json.loads(Path(path or REPRINT_QUEUE).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_queue(value: dict[str, Any] | None, path: Path | None = None) -> None:
    target = Path(path or REPRINT_QUEUE)
    if not value:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def queue_copies(target: dict[str, Any], path: Path | None = None) -> None:
    """Remember the remaining copies of a multi-copy reprint. Starts nothing."""
    copies = int(target.get("copies") or 1)
    if copies <= 1:
        _write_queue(None, path)
        return
    _write_queue({"job_json": target["job_json"], "gcode": target["gcode"], "title": target["title"],
                  "gcode_sha256": target["gcode_sha256"], "total": copies, "started": 1,
                  "queued_at": time.time()}, path)


def pending_copy(path: Path | None = None) -> dict[str, Any] | None:
    q = _read_queue(path)
    return q if q and int(q.get("started", 0)) < int(q.get("total", 0)) else None


def next_copy_target(path: Path | None = None, archive: Path | None = None) -> dict[str, Any]:
    """Exact target for the next queued copy, re-verified against the archive. Read only."""
    from .print_next import NextPlateError, _sha256

    q = pending_copy(path)
    if q is None:
        raise NextPlateError("no copies are queued")
    archive = (archive or ARCHIVE).resolve()
    gcode = Path(q["gcode"]).resolve()
    job_path = Path(q["job_json"]).resolve()
    if not gcode.is_file() or not job_path.is_file() or not job_path.is_relative_to(archive):
        raise NextPlateError("the queued copy's files are gone; queue cleared")
    n = int(q["started"]) + 1
    # `copy` keys print_next's one-start claim, so it must be unique per start like other reprints.
    return {"plan_id": "print-again", "part_id": job_path.parent.name[:60], "label": q["title"], "where": "",
            "title": q["title"], "copy": int(time.time()), "copy_number": n, "quantity": int(q["total"]),
            "copies": int(q["total"]),
            "reprint": True, "resliced": False, "job_json": str(job_path), "gcode": str(gcode),
            "gcode_sha256": _sha256(gcode)}


def mark_copy_started(path: Path | None = None) -> None:
    q = _read_queue(path)
    if q:
        q["started"] = int(q.get("started", 0)) + 1
        _write_queue(q if q["started"] < int(q.get("total", 0)) else None, path)


def cancel_copies(path: Path | None = None) -> bool:
    had = bool(_read_queue(path))
    _write_queue(None, path)
    return had


def next_copy_prompt(finished_filename: str, path: Path | None = None) -> str | None:
    """Completion line for a multi-copy reprint, or None when this file isn't the queued one.

    Uploaded names embed the G-code SHA-256, so the match is exact."""
    q = pending_copy(path)
    if q is None or not q.get("gcode_sha256") or q["gcode_sha256"] not in str(finished_filename or ""):
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
