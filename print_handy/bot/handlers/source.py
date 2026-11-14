"""The source chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio

from .common import _control_preamble

# Per-module logger (#1404): records name the handler module that
# made them, so [logging] levels rules can target one handler.
logger = logging.getLogger(__name__)


async def source_dispatch(event, gateway, **kwargs):
    """/print-source: which model sites are ready, need a login, or are listing-only (#462, #464).

    Read-only: it checks whether each site's credential is saved (and, for a
    JWT, not expired) without contacting any site, and never shows a value.
    """
    source, adapter = _control_preamble(event, gateway, '/print-source')
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'source UI delivery unavailable'}
    from print_handy.core import site_accounts
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    try:
        message = await asyncio.to_thread(site_accounts.source_report)
    except Exception:
        logger.exception('/print-source failed')
        message = 'Could not read the saved site logins. Nothing was changed.'
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'print source handled'}


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/print-source', False)
handle = source_dispatch
handle.dispatch_gate = match
