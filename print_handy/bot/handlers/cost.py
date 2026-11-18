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


def cost_answer(text, data=None):
    """The cost answer text for one ask. Pure apart from settings/records reads."""
    from print_handy import settings as _settings
    from print_handy.core import cost as _cost

    data = data if data is not None else _settings.current()
    workspace = _cost.WorkspaceCost.from_settings(data)
    profile, _ = _cost.printer_profile_for(data)
    grams, hours, material = _parse_ask(text)
    if grams is not None:
        line = _cost.MaterialLine(name=material or "Filament", material=material or "",
                                  grams=grams)
        job = _cost.JobCostInput(lines=[line], print_hours=hours or 0.0,
                                 grams_source="manual entry in chat")
        result = _cost.compute(job, profile, workspace)
        return _cost.format_chat_summary(result, title="estimate")
    # No amounts in the ask: the newest frozen completion record.
    try:
        from print_handy.core import print_stats
        vault = ((data.get("paths") or {}).get("records_vault")) or "."
        records, _skipped = print_stats.load_records(vault)
    except Exception as exc:
        logger.warning("cost: could not read the job records: %s", exc)
        records = []
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
