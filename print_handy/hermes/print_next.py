"""Resolve and start the next required plate of a multipart plan.

Used by the ``/print-next <plan_id>`` Discord command in the print-feedback
plugin. Resolution is exact: the plan part must record the archived
``job_json`` and absolute ``gcode_path`` written when the plate was sliced.
Bare filenames are never searched for, because names like ``plate_1.gcode``
repeat across jobs and a guess could start the wrong plate.

Starting always goes through ``python3 -m print_handy.core.model_job print-now`` in a subprocess, so the
existing busy, mesh, filament, artifact binding, approval and upload gates stay
authoritative. This module never uploads or starts anything itself.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import settings
from . import _config

ARCHIVE = _config.archive_dir()
CLAIMS_DIR = _config.state_dir() / "print-next-claims"
# print-now runs as a module in a subprocess: python3 -m print_handy.core.model_job print-now ...
MODEL_JOB_MODULE = "print_handy.core.model_job"
START_LABEL = "Start next plate · bed is clear"
CANCEL_LABEL = "Cancel"
START_TIMEOUT_S = 420
STOP_GRACE_S = 2.0
STOP_REAP_TIMEOUT_S = 15.0


class NextPlateError(ValueError):
    """The plan cannot name one exact, current artifact. Nothing was started."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def remaining_parts(plan: Mapping[str, Any]) -> list[tuple[dict[str, Any], int]]:
    """Required parts with copies left, in plan order, each with its completed copy count."""
    from .print_records import _plan_parts

    rows = []
    for part in _plan_parts(plan.get("parts")):
        if part.get("optional") is True or part.get("required") is False:
            continue
        if part.get("alternative_group") and part.get("selected") is not True:
            continue
        quantity = part["quantity"]
        done = quantity if part.get("status") == "completed" else part["completed_quantity"]
        if done < quantity:
            rows.append((part, done))
    return rows


def next_required_part(plan: Mapping[str, Any], part_id: str | None = None) -> tuple[dict[str, Any], int] | None:
    """First required part with copies left (same ordering as the completion summary).

    With ``part_id`` only that exact remaining part qualifies; a completed or unknown id gives None.
    """
    rows = remaining_parts(plan)
    if part_id is None:
        return rows[0] if rows else None
    return next((row for row in rows if str(row[0].get("id")) == str(part_id)), None)


def has_exact_start_target(part: Mapping[str, Any] | None) -> bool:
    return bool(part and part.get("job_json") and part.get("gcode_path"))


def load_plan(plan_id: str, plans_dir: Path | None = None) -> dict[str, Any]:
    from .print_records import PLANS_DIR, _safe_slug

    raw = str(plan_id or "").strip()
    slug = _safe_slug(raw, "")
    if not slug or slug != raw:
        raise NextPlateError("unknown plan id")
    path = (plans_dir or PLANS_DIR) / f"{slug}.json"
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise NextPlateError(f"no plan named {slug}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise NextPlateError(f"plan {slug} is unreadable") from exc
    if not isinstance(plan, dict):
        raise NextPlateError(f"plan {slug} is malformed")
    plan.setdefault("plan_id", slug)
    return plan


def resolve_next(plan: Mapping[str, Any], archive: Path | None = None, part_id: str | None = None) -> dict[str, Any]:
    """Return the exact artifact (the next required plate, or ``part_id``), or raise NextPlateError. Read only."""
    archive = (archive or ARCHIVE).resolve()
    found = next_required_part(plan, part_id)
    if found is None:
        if part_id is not None:
            raise NextPlateError(f"plate {part_id} is already complete or is not part of this plan")
        raise NextPlateError("every required part is already complete")
    part, done = found
    if not has_exact_start_target(part):
        raise NextPlateError(f"the plan does not record the exact sliced file for {part['label']}")
    job_path = Path(str(part["job_json"])).expanduser().resolve()
    gcode = Path(str(part["gcode_path"])).expanduser().resolve()
    if job_path.name != "JOB.json" or not job_path.is_relative_to(archive) or not job_path.is_file():
        raise NextPlateError("recorded job is not an archived JOB.json")
    if not gcode.is_relative_to(job_path.parent) or gcode.suffix != ".gcode" or not gcode.is_file():
        raise NextPlateError("recorded G-code is missing or outside its job folder")
    if part.get("gcode_file") and Path(str(part["gcode_file"])).name != gcode.name:
        raise NextPlateError("recorded G-code name does not match the plan part")
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NextPlateError("recorded JOB.json is unreadable") from exc
    known = {Path(str(job.get("gcode"))).resolve()} if job.get("gcode") else set()
    known |= {Path(str(row["gcode"])).resolve() for row in job.get("derived_plates") or []
              if isinstance(row, dict) and row.get("gcode")}
    if gcode not in known:
        raise NextPlateError("recorded G-code is not a current artifact of its JOB.json")
    run, plate = part.get("run", part.get("run_id")), part.get("plate", part.get("plate_id"))
    where = ", ".join(x for x in (f"Run {run}" if run is not None else "",
                                  f"Plate {plate}" if plate is not None else "") if x)
    return {"plan_id": plan.get("plan_id"), "part_id": part["id"], "label": part["label"],
            "where": where, "title": job.get("title") or job_path.parent.name,
            "copy": done + 1, "quantity": part["quantity"], "reprint": done > 0,
            "job_json": str(job_path), "gcode": str(gcode), "gcode_sha256": _sha256(gcode)}


def describe_plate(part: Mapping[str, Any]) -> dict[str, Any]:
    """Display facts for one plate: parts, colors, duration. Read only; unknowns stay unknown."""
    from . import discord_print_summary as summary

    gcode = str(part.get("gcode_path") or "")
    plate: dict[str, Any] = {}
    try:
        if gcode:
            plate = summary.find_job(gcode)[1] or {}
    except Exception:
        plate = {}
    seconds = plate.get("seconds")
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds < 0:
        try:
            seconds = summary.gcode_metadata(gcode)[0] if gcode else None
        except Exception:
            seconds = None
    parts = ", ".join(str(x) for x in plate.get("parts") or []) or str(part.get("label") or part.get("id"))
    color = " ".join(str(x) for x in (plate.get("color"), plate.get("material")) if x)
    return {"parts": parts, "colors": color or "colors unknown",
            "duration": f"~{summary.human(seconds)}" if isinstance(seconds, (int, float)) and seconds >= 0 else "time unknown"}


def plate_choice_label(part: Mapping[str, Any], number: int | None = None) -> str:
    """'Plate N: <parts> · <colors> · ~<duration>' for pickers. Discord caps option labels at 100."""
    info = describe_plate(part)
    number = number if number is not None else part.get("plate")
    head = f"Plate {number}" if number is not None else str(part.get("label") or part.get("id"))
    return f"{head}: {info['parts']} · {info['colors']} · {info['duration']}"[:100]


PREPARING_MARK = "PREPARATION STILL RUNNING"
SUBMITTED_MARK = "START SUBMITTED; AWAITING CONFIRMATION"  # what model_job print-now really prints
START_MESSAGES = _config.state_file("PRINT_HANDY_START_MESSAGES", "print_handy_start_messages.json")
START_MESSAGE_TTL_S = float(_config.cfg()["listener"]["start_claim_ttl_s"])


# The uniform outcome line print-now ends with: `START READBACK {json with phase}`. The mapped native route's
# older "MAPPED START READBACK {...}" contains the same marker, so one parser reads both.
OUTCOME_MARK = "START READBACK "
# Phases that mean the start was sent but the printer has not confirmed it.
UNRESOLVED_PHASES = frozenset({"awaiting_confirmation", "preparing_or_unresolved", "submitted_unconfirmed"})


def start_outcome(detail: str) -> dict[str, Any]:
    """The last `START READBACK {json}` object in print-now's output, or {} when there is none."""
    for line in reversed(str(detail or "").splitlines()):
        at = line.find(OUTCOME_MARK)
        if at < 0:
            continue
        try:
            value = json.loads(line[at + len(OUTCOME_MARK):])
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def outcome_unreadable(detail: str) -> bool:
    """Whether the output carries a START READBACK fragment that does not parse (a cut-off or damaged line).

    The intake treats that as unresolved, never as started: the line existed, so a phase was reported.
    """
    for line in reversed(str(detail or "").splitlines()):
        if "READBACK" not in line:
            continue
        at = line.find("{")
        if at < 0:
            return True
        try:
            return not isinstance(json.loads(line[at:]), dict)
        except ValueError:
            return True
    return False


def start_unresolved(outcome: Mapping[str, Any]) -> bool:
    """Whether a parsed outcome says the start was sent but is not confirmed (awaiting_confirmation,
    preparing_or_unresolved). The phase decides; without one, an unknown/unconfirmed outcome does."""
    phase = str(outcome.get("phase") or "")
    if phase:
        return phase in UNRESOLVED_PHASES
    return outcome.get("outcome") in ("unknown", "unconfirmed")


def still_preparing(detail: str) -> bool:
    """print-now accepted the start but native heating/leveling is still running.

    It kept its start claim, so the listener will not post a second "Print started" when
    print_stats reaches printing; it edits (or follows up on) the intake's one message.
    """
    text = str(detail or "")
    if PREPARING_MARK in text or SUBMITTED_MARK in text:
        return True
    outcome = start_outcome(text)
    return bool(outcome.get("started")) and outcome.get("outcome") != "printing"


def started_file(detail: str) -> str | None:
    """The Moonraker file name print-now reports in its START SUBMITTED / ACCEPTED JSON line."""
    for line in reversed(str(detail or "").splitlines()):
        start = line.find("{")
        if start < 0:
            continue
        try:
            value = json.loads(line[start:])
        except ValueError:
            continue
        name = value.get("file") if isinstance(value, dict) else None
        if name:
            return Path(str(name)).name
    return None


def _read_start_messages() -> dict[str, Any]:
    try:
        value = json.loads(START_MESSAGES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _timestamp(value: Any) -> float | None:
    """A recorded row's timestamp as a float, or None when it cannot be read as one (#404)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def record_start_message(filename: str, message_id: str, now: float | None = None) -> None:
    """Remember the one Discord start message so the listener can edit it after warmup."""
    now = time.time() if now is None else now
    rows = {}
    for k, v in _read_start_messages().items():
        if not isinstance(v, dict):
            continue
        ts = _timestamp(v.get("ts") or 0)
        if ts is not None and now - ts < START_MESSAGE_TTL_S:
            rows[k] = v
    rows[Path(str(filename)).name] = {"message_id": str(message_id), "ts": now}
    START_MESSAGES.parent.mkdir(parents=True, exist_ok=True)
    tmp = START_MESSAGES.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows), encoding="utf-8")
    os.replace(tmp, START_MESSAGES)


def take_start_message(filename: str, now: float | None = None) -> str | None:
    """The recorded start message id for this file, consumed so a replay cannot edit it twice."""
    now = time.time() if now is None else now
    rows = _read_start_messages()
    row = rows.pop(Path(str(filename or "")).name, None)
    ts = _timestamp(row.get("ts") or 0) if isinstance(row, dict) else None
    if ts is None or not 0 <= now - ts < START_MESSAGE_TTL_S:
        return None
    tmp = START_MESSAGES.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows), encoding="utf-8")
    os.replace(tmp, START_MESSAGES)
    return str(row.get("message_id") or "") or None


def question_text(target: Mapping[str, Any]) -> str:
    copy = f" (copy {target['copy']} of {target['quantity']})" if target["quantity"] > 1 else ""
    where = f", {target['where']}" if target["where"] else ""
    return (f"Next plate for {target['title']}: {target['label']}{copy}{where}.\n"
            "Starting uploads and prints immediately. Confirm the bed is clear.")


def _target_printer_id(target: Mapping[str, Any]) -> str | None:
    """The roster printer a start target's job belongs to, from its JOB.json (#335).

    print-now keys everything off the job record; the pre-start idle check
    must look at the same printer. A target without a readable job record
    names no printer and the check falls back to the configured one.
    """
    job_json = target.get("job_json") if isinstance(target, Mapping) else None
    if not job_json:
        return None
    try:
        job = json.loads(Path(str(job_json)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = job.get("printer_id") if isinstance(job, dict) else None
    return str(pid) if pid else None


def live_idle_check(printer_id: str | None = None) -> None:
    """Fresh read-only printer state query. Raises unless the printer is provably idle.

    With a printer_id (#335) the query is that roster printer's: its adapter,
    its state record, its Moonraker. Checking the configured printer instead
    would wave through — or block — a start on the wrong machine.
    """
    from print_handy import printers
    from print_handy.core import printer_state
    from print_handy.core.model_job import _printer_phase

    if printer_id:
        from print_handy import settings
        entry = settings.by_id(printer_id)
        adapter = printers.get(entry["model"], settings.view_for(printer_id))
        ok, problems, facts = printer_state.check(printer_state.load(printer_id),
                                                  printer_id=printer_id, adapter=adapter)
        if getattr(adapter, "transport", None) == "bambulab_lan" and not facts.get("lan_configured"):
            raise RuntimeError("Bambu LAN status is not configured ([bambu] lan_host and lan_serial plus the access code)")
        if not ok:
            raise RuntimeError("; ".join(problems) or "printer readiness problems")
        return

    transport = getattr(printers.current(), "transport", None)
    if transport in ("bambulab_lan", "octoprint"):
        # No Moonraker on these printers: _printer_phase would build a bare /printer/objects/query
        # path and fail with "unknown url type". printer_state.check already reads the adapter's own
        # live status and readiness problems (the signature bit, HMS errors, busy state); print-now
        # repeats every gate after this (#269).
        ok, problems, facts = printer_state.check(printer_state.load())
        if transport == "bambulab_lan" and not facts.get("lan_configured"):
            raise RuntimeError("Bambu LAN status is not configured ([bambu] lan_host and lan_serial plus the access code)")
        if not ok:
            raise RuntimeError("; ".join(problems) or "printer readiness problems")
        return
    state, filename, busy = _printer_phase(printer_state.load())
    if state not in ("standby", "complete", "cancelled") or busy == "Printing":
        raise RuntimeError(f"printer is not idle ({state}, {filename}, {busy})")


def claim_path(target: Mapping[str, Any], claims_dir: Path | None = None) -> Path:
    """One durable claim per plan, part, copy and exact artifact."""
    from .print_records import _safe_slug

    name = "--".join((_safe_slug(str(target["plan_id"]), "plan"), _safe_slug(str(target["part_id"]), "part"),
                      f"copy{int(target['copy'])}", str(target["gcode_sha256"])[:16]))
    return (claims_dir or CLAIMS_DIR) / f"{name}.json"


def _take_claim(path: Path, target: Mapping[str, Any]) -> str | None:
    """Atomically create the claim. Returns an error string if it already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        try:
            when = json.loads(path.read_text(encoding="utf-8")).get("claimed_at", "earlier")
        except (OSError, json.JSONDecodeError):
            when = "earlier"
        return (f"REFUSED: this exact plate (copy {target['copy']}) was already started from a button at {when}. "
                f"Nothing uploaded or started. If that start really failed, check the printer, then delete {path} to allow one new try.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"claimed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "outcome": "pending",
                   **{k: target[k] for k in ("plan_id", "part_id", "copy", "gcode", "gcode_sha256")}}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    return None


def _settle_claim(path: Path, outcome: str, release: bool) -> None:
    if release:
        path.unlink(missing_ok=True)
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data["outcome"] = outcome
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


async def _stop_and_reap(proc, communication=None) -> bool:
    """Stop the exact print-now child, escalate if needed, and reap it within bounded waits."""
    if proc.returncode is None:
        try:
            proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), STOP_GRACE_S)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), STOP_REAP_TIMEOUT_S)
            except asyncio.TimeoutError:
                pass
        except Exception:
            pass
    reaped = proc.returncode is not None
    if communication is not None:
        if reaped and not communication.done():
            try:
                await asyncio.wait_for(asyncio.shield(communication), 5.0)
            except asyncio.TimeoutError:
                communication.cancel()
            except Exception:
                pass
        if communication.done():
            try:
                communication.result()
            except (asyncio.CancelledError, Exception):
                pass
    return reaped


def _uncertain_stop_detail(reason: str, reaped: bool) -> str:
    stopped = ('The print-now process was stopped and reaped.' if reaped else
               'The print-now process could not be confirmed stopped.')
    return (f"UNCERTAIN: {reason} {stopped} The plate may already have uploaded or started. "
            "Check the printer before doing anything else. Not retrying.")


async def start(target: Mapping[str, Any], model_job: Path | None = None,
                timeout: float = START_TIMEOUT_S, idle_check: Callable[[], None] | None = None,
                claims_dir: Path | None = None) -> tuple[bool, str]:
    """Run print-now once for the exact artifact. Never retried, never replayed."""
    gcode = Path(target["gcode"])
    if not gcode.is_file() or _sha256(gcode) != target["gcode_sha256"]:
        return False, "REFUSED: the next plate's G-code changed after the prompt. Nothing uploaded or started."
    if idle_check is not None:
        check = idle_check
    else:
        target_printer = _target_printer_id(target)
        check = (lambda: live_idle_check(target_printer)) if target_printer else live_idle_check
    try:
        await asyncio.to_thread(check)
    except Exception as exc:  # fail closed on any read error
        return False, f"REFUSED: fresh printer check failed: {exc}. Nothing uploaded or started."
    claim = claim_path(target, claims_dir)
    refused = _take_claim(claim, target)
    if refused:
        return False, refused
    runner = [str(model_job)] if model_job else [sys.executable, "-m", MODEL_JOB_MODULE]
    argv = [*runner, "print-now", str(gcode)]
    if target.get("accept_spool_change"):
        argv.append("--accept-spool-change")
    if target.get("reprint"):
        argv.append("--reprint")
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        cwd=str(settings.ROOT)))
    try:
        proc = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        # Shield spawn too: cancellation must not orphan a child whose handle was never returned.
        try:
            proc = await asyncio.shield(spawn)
        except OSError:
            _settle_claim(claim, "spawn_failed", release=True)
            return False, "REFUSED: print-now could not be spawned. Nothing uploaded or started."
        reaped = await _stop_and_reap(proc)
        _settle_claim(claim, "uncertain_cancelled", release=False)
        return False, _uncertain_stop_detail("Cancellation arrived while print-now was starting.", reaped)
    except OSError as exc:
        _settle_claim(claim, "spawn_failed", release=True)
        return False, f"REFUSED: could not run print-now: {exc}. Nothing uploaded or started."
    communication = asyncio.create_task(proc.communicate())
    try:
        out, _ = await asyncio.wait_for(asyncio.shield(communication), timeout)
    except asyncio.TimeoutError:
        reaped = await _stop_and_reap(proc, communication)
        _settle_claim(claim, "uncertain_timeout", release=False)
        return False, _uncertain_stop_detail("print-now timed out.", reaped)
    except asyncio.CancelledError:
        reaped = await _stop_and_reap(proc, communication)
        _settle_claim(claim, "uncertain_cancelled", release=False)
        return False, _uncertain_stop_detail("Stop was requested after print-now launched.", reaped)
    text = out.decode(errors="replace").strip()
    tail = "\n".join(text.splitlines()[-6:])[-1500:]
    ok = proc.returncode == 0
    # A clean pre-upload refusal releases the claim so a fixed problem can be retried with a new tap.
    safe_refusal = not ok and text.splitlines()[-1:] and text.splitlines()[-1].startswith("REFUSED:")
    _settle_claim(claim, "started" if ok else ("refused" if safe_refusal else "failed_uncertain"),
                  release=bool(safe_refusal))
    return ok, tail or f"print-now exited {proc.returncode} with no output"
