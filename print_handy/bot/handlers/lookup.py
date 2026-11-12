"""The lookup chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio

from .common import _control_preamble

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy_print_feedback")


async def lookup_dispatch(event, gateway, **kwargs):
    """/print-lookup <query>: a read-only search of the root registries (issue #169).

    Answers from knowledge/, firmware/ and printers/ data only. There is no printer
    client on this path: a lookup cannot heat, move, extrude or change anything.
    """
    source, adapter = _control_preamble(event, gateway, '/print-lookup', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'lookup UI delivery unavailable'}
    from print_handy import knowledge
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    query = str(getattr(event, 'text', '')).strip()[len('/print-lookup'):].strip()
    try:
        message = await asyncio.to_thread(knowledge.lookup_text, query)
    except Exception:
        logger.exception('/print-lookup failed')
        message = 'Lookup failed; the registries could not be read. Nothing was sent to the printer.'
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'print lookup handled'}


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/print-lookup', True)
handle = lookup_dispatch
handle.dispatch_gate = match
