"""The coldpull chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import time

from .common import _coldpull_workflow, _control_preamble, _decide_printer, _live_key_for, _pkw, _seen

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy.bot.handlers")


async def coldpull_dispatch(event, gateway, **kwargs):
    source, adapter = _control_preamble(event, gateway, '/print-coldpull')
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'cold-pull UI delivery unavailable'}
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    key = ('coldpull', source.chat_id, source.thread_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content='A cold pull is already in progress. Nothing sent.')
        return {'action': 'skip', 'reason': 'cold-pull prompt already active'}
    _seen[key] = time.monotonic()
    try:
        decided = await _decide_printer(source, gateway, adapter, metadata,
                                        question='Cold pull on which printer?')
        if decided is None:
            return {'action': 'skip', 'reason': 'shared channel: no printer chosen for cold pull'}
        pid, chosen = decided
        _record, message = await _coldpull_workflow(adapter, source, metadata, source_tag='coldpull',
                                                    live_key=_live_key_for(source, gateway, chosen),
                                                    **_pkw(pid))
    except Exception as exc:
        logger.exception('/print-coldpull failed')
        message = 'The cold pull stopped early. Do not retry until the printer state is checked on its screen.'
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'cold pull handled'}


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/print-coldpull', False)
handle = coldpull_dispatch
handle.dispatch_gate = match
