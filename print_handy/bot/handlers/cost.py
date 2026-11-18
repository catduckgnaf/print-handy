"""The print cost chat handler (#1625).

/print-cost: what a print cost or will cost, from the one core engine.
Read-only and informational: it never blocks, delays or refuses a
print. With grams and hours in the ask ("how much will this cost,
50 g, 3 h, in PETG") it answers an estimate with provenance; with no
job in the ask it answers from the newest frozen completion record;
with neither it asks which print is meant instead of guessing.
"""
import logging
import asyncio
import re

from .common import _control_preamble

logger = logging.getLogger(__name__)

_MATERIALS = ("PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC")


def _parse_ask(text):
    """(grams, hours, material) named in a cost ask, each optional."""
    grams = None
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:g|grams?)\b", text, re.I)
    if match:
        grams = float(match.group(1))
    hours = None
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:h|hours?)\b", text, re.I)
    if match:
        hours = float(match.group(1))
    else:
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:m|min|minutes?)\b", text, re.I)
        if match:
            hours = float(match.group(1)) / 60.0
    material = None
    for name in _MATERIALS:
        if re.search(rf"\b{name}\b", text, re.I):
            material = name
            break
    return grams, hours, material


_THIS = re.compile(r"\b(?:will|would)\s+this\b|\bprice\s+this\b|\bthis\s+(?:file|model|job)\b", re.I)
_MONTH = re.compile(r"\bthis\s+month\b|\bper\s+month\b|\bmonthly\b", re.I)


def _records(data):
    from print_handy.core import print_stats
    vault = ((data.get("paths") or {}).get("records_vault")) or "."
    try:
        records, _skipped = print_stats.load_records(vault)
    except Exception as exc:
        logger.warning("cost: could not read the job records: %s", exc)
        return []
    return records


def _newest_unprinted_job(records, archive=None):
    """The newest sliced JOB.json no completion record names, or None.

    "How much will this cost" is about the file in hand: the newest
    sliced job that has not printed yet (#1632 review), never the last
    finished print.
    """
    from print_handy.runtime import discord_print_summary as dps
    # Sliced G-code is always named plate_N.gcode in the archive, so the
    # link to a record is the G-code's SHA-256, which the printer-side
    # file name carries; a job with no recorded hash matches on title.
    printed = " ".join(str(r.get("job_key") or r.get("gcode_file") or "") for r in records)
    printed_titles = {str(r.get("title") or "") for r in records} - {""}
    jobs = []
    for path, job in dps._job_files(archive or dps.ARCHIVE):
        if not job.get("gcode"):
            continue
        hashes = {str(job.get("gcode_full_verified_sha256") or "")}
        hashes |= {str(a.get("gcode_sha256") or "") for a in job.get("approvals") or ()
                   if isinstance(a, dict)}
        hashes.discard("")
        if hashes and any(h in printed for h in hashes):
            continue
        if not hashes and str(job.get("title") or "") in printed_titles:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        jobs.append((mtime, job))
    return max(jobs, key=lambda pair: pair[0])[1] if jobs else None


def _month_answer(records, workspace, now=None):
    from datetime import datetime, timezone
    from print_handy.core import cost as _cost
    now = now or datetime.now(timezone.utc)
    prefix = now.strftime("%Y-%m")
    month = [r for r in records if str(r.get("started_at") or "").startswith(prefix)]
    agg = _cost.aggregate_costs(month)
    label = now.strftime("%B %Y")
    if not agg["has_priced"]:
        return (f"No priced prints in {label} yet: no print this month has a recorded "
                "cost. Costs are recorded when a print finishes.")
    cur = agg["currency_code"]
    lead = (f"{label}: {cur} {agg['out_of_pocket_total']:.2f} out of pocket "
            f"(filament, power and consumables) across {agg['priced_count']} priced print(s)")
    if workspace.cost_mode == "seller":
        lead += f"; {cur} {agg['total_spend']:.2f} with labor and machine wear"
    lines = [lead + "."]
    if agg["failed_count"]:
        lines.append(f"Failed or cancelled prints cost {cur} {agg['failure_total']:.2f} of that.")
    if agg["note"]:
        lines.append(agg["note"])
    return "\n".join(lines)


def cost_answer(text, data=None, *, archive=None, spool_lookup=None):
    """The cost answer text for one ask. Pure apart from settings/records reads.

    Amounts in the ask: a compact estimate. "this month": the month's
    recorded spend. "will this cost" / "price this": the newest sliced
    job that has not printed yet. "did that cost" and bare asks: the
    newest frozen completion record. Nothing to go on: ask, don't guess.
    """
    from print_handy import settings as _settings
    from print_handy.core import cost as _cost

    data = data if data is not None else _settings.current()
    workspace = _cost.WorkspaceCost.from_settings(data)
    grams, hours, material = _parse_ask(text)
    if grams is not None:
        profile, _ = _cost.printer_profile_for(data)
        line = _cost.MaterialLine(name=material or "Filament", material=material or "",
                                  grams=grams)
        job = _cost.JobCostInput(lines=[line], print_hours=hours or 0.0,
                                 grams_source="manual entry in chat")
        result = _cost.compute(job, profile, workspace)
        return _cost.format_chat_summary(result, title="estimate")
    records = _records(data)
    if _MONTH.search(text):
        return _month_answer(records, workspace)
    if _THIS.search(text):
        try:
            job = _newest_unprinted_job(records, archive)
            result = (_cost.estimate_for_job(job, data, spool_lookup=spool_lookup or _spool)
                      if job else None)
        except Exception as exc:
            logger.warning("cost: could not estimate the pending job: %s", exc)
            result = None
        if result is not None:
            return _cost.format_chat_summary(result, title=f"{job.get('title') or 'this print'} "
                                                           "(estimate before printing)")
        return ("Cannot estimate this one yet: there is no sliced file waiting to print. "
                "Send the model and I will price it once it is sliced, or tell me the "
                "grams and hours (for example \"how much will this cost, 50 g, 3 h\"). "
                "Cost is information only; nothing about a print is blocked by this.")
    priced = [r for r in records if isinstance(r.get("cost"), dict)]
    if priced:
        record = priced[-1]
        result = _cost.CostResult.from_dict(record["cost"])
        title = record.get("title") or record.get("gcode_file") or "last print"
        return _cost.format_chat_summary(result, title=str(title))
    return ("Cannot estimate yet: no completed print has a recorded cost, and this "
            "ask names no amounts. Tell me the grams and hours (for example "
            "\"how much will this cost, 50 g, 3 h\"), or open the Web UI cost "
            "calculator and drop the G-code or sliced 3MF file there. "
            "Cost is information only; nothing about a print is blocked by this.")


def _spool(spool_id):
    try:
        from print_handy.core import spoolman_client
        return spoolman_client.get_spool(int(spool_id))
    except Exception as exc:
        logger.warning("cost: Spoolman spool %s unavailable: %s", spool_id, exc)
        return None


async def cost_dispatch(event, gateway, **kwargs):
    """/print-cost: the compact cost breakdown for the right job (#1625)."""
    source, adapter = _control_preamble(event, gateway, '/print-cost', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'cost UI delivery unavailable'}
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    text = str(getattr(event, 'text', '')).strip()
    try:
        message = await asyncio.to_thread(cost_answer, text)
    except Exception:
        logger.exception('/print-cost failed')
        message = 'Could not work out a cost just now. Nothing was changed.'
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'print cost handled'}


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/print-cost', True)
handle = cost_dispatch
handle.dispatch_gate = match
