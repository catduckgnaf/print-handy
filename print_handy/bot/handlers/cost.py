"""The print cost chat handler (#1625).

/print-cost: what a print cost or will cost, from the one core engine.
Read-only and informational: it never blocks, delays or refuses a
print. With grams and hours in the ask ("how much will this cost,
50 g, 3 h, in PETG") it answers an estimate with provenance; with no
job in the ask it answers from the newest frozen completion record;
with neither it asks which print is meant instead of guessing.

A file dropped in chat with a cost ask (the command, or a plain
cost phrase about the file) prices that attachment instead (#1661):
the saved upload is parsed by the existing cost parsers and priced by
the Web UI calculator's own auto-fill computation. The ask stays
read-only — it never enters the print intake flow, creates no job and
asks nothing; intake only ever sees the file when the operator asks
to print it, as its own step.
"""
import logging
import asyncio
import re
from pathlib import Path

from .common import _command_matches, _control_gate, _control_preamble

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


def _display_name(path):
    """The operator's file name for a saved upload (the doc_ cache prefix stripped)."""
    return re.sub(r"^doc_[0-9a-f]{12}_", "", Path(str(path)).name)


def attachment_cost_answer(path, data=None):
    """The cost answer for one dropped file (#1661).

    The file is parsed by the existing cost parsers and priced by the
    same auto-fill computation the Web UI calculator runs for a dropped
    file, so the two surfaces cannot disagree. An unsliced file, and a
    sliced file naming no usage at all, get the explicit
    cannot-estimate answer naming what is missing — never a confident
    0.00. Read-only: nothing here creates a job, a prompt or a printer
    action.
    """
    from print_handy import settings as _settings
    from print_handy.core import cost_parse

    data = data if data is not None else _settings.current()
    name = _display_name(path)
    try:
        usage = cost_parse.usage_from_file(path)
    except Exception:
        logger.exception("cost: could not parse the dropped file %s", path)
        return (f"Cannot estimate {name} yet: the file could not be read. "
                "Cost is information only; it never blocks a print.")
    if not usage.sliced:
        return (f"Cannot estimate {name} yet: {usage.not_sliced_reason}. "
                "Cost is information only; it never blocks a print.")
    if usage.total_grams is None and usage.volume_ml is None and not usage.time_seconds:
        return (f"Cannot estimate {name} yet: the file names no filament "
                "usage, resin volume or print time to price. "
                "Cost is information only; it never blocks a print.")
    from print_handy.core import cost as _cost
    from print_handy.webui import cost_page as _cost_page
    job, profile, workspace = _cost_page.job_from_usage(usage, data)
    result = _cost.compute(job, profile, workspace)
    return _cost.format_chat_summary(result, title=f"{name} (estimate before printing)")


_ATTACHMENT_ASK = re.compile(
    r"\b(?:cost|coast|price)\b[^.?!]*\b(?:this|that)\s+(?:file|print|job|model|g-?code|3mf)\b"
    r"|\b(?:this|that)\s+(?:file|print|job|model|g-?code|3mf)\b[^.?!]*\b(?:cost|coast|price)\b"
    r"|\b(?:will|would|did|does)\s+(?:this|that)\b[^.?!]*\b(?:cost|coast)\b"
    r"|\bprice\s+(?:this|that)\b", re.I)


def _is_attachment_ask(text):
    """A plain-language cost ask about a file in hand (#1661)."""
    return bool(_ATTACHMENT_ASK.search(text or ""))


def _attachment_path(event):
    """The first dropped file this event carries, or None."""
    for raw in getattr(event, 'media_urls', None) or []:
        path = Path(str(raw))
        if path.is_file():
            return path
    return None


def _attachment_decides(text):
    """Whether an attached file is the subject of a cost ask (#1661).

    Amounts in the ask keep winning (the manual estimate), and a month
    ask keeps answering the month's spend; otherwise the file is what
    the ask is about.
    """
    if _MONTH.search(text):
        return False
    grams, _hours, _material = _parse_ask(text)
    return grams is None


def _dispatch_answer(text, event):
    path = _attachment_path(event)
    if path is not None and _attachment_decides(text):
        return attachment_cost_answer(path)
    return cost_answer(text)


async def cost_dispatch(event, gateway, **kwargs):
    """/print-cost: the compact cost breakdown for the right job (#1625).

    With a file attached, the ask prices the attachment instead
    (#1661); the plain-language file ask takes the same gate and
    authorization as the command form.
    """
    source, adapter = _control_preamble(event, gateway, '/print-cost', with_args=True)
    text = str(getattr(event, 'text', '')).strip()
    if source is None and _is_attachment_ask(text):
        source, adapter = _control_gate(event, gateway)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'cost UI delivery unavailable'}
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    try:
        message = await asyncio.to_thread(_dispatch_answer, text, event)
    except Exception:
        logger.exception('/print-cost failed')
        message = 'Could not work out a cost just now. Nothing was changed.'
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'print cost handled'}


def _cost_gate(text):
    """The dispatch gate: the /print-cost command form, or a plain
    cost ask about a file in hand (#1661). The handler itself decides
    from the event whether the attachment or another answer applies."""
    return (_command_matches(text, '/print-cost', True)
            or _is_attachment_ask(text))


_cost_gate.command = '/print-cost'
_cost_gate.with_args = True

# --- handler interface (#1323) ---

match = _cost_gate
handle = cost_dispatch
handle.dispatch_gate = match
