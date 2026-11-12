"""The roast chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio

from .common import _control_gate
from ...runtime import _config

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy_print_feedback")


async def roast_dispatch(event, gateway, **kwargs):
    """"roast my last print" / "roast print 84" / "roast the benchie" (#1002).

    Plain language, typo tolerant through the shared fuzzy layer, and
    read-only: the print is resolved the way 'print 84' and /print-again
    resolve it, and the line comes from the local roast corpus — no LLM,
    nothing sent to the printer. Off with vision.roast_enabled=false.
    """
    from print_handy.runtime import roast_chat
    text = str(getattr(event, 'text', '')).strip()
    if roast_chat.roast_request(text) is None:
        return None
    source, adapter = _control_gate(event, gateway)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'roast UI delivery unavailable'}
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    vision = _config.cfg().get('vision') or {}
    if not vision.get('roast_enabled', True):
        message = 'Roasts are turned off (vision.roast_enabled is false).'
    else:
        from print_handy.runtime import _config as hermes_config
        state_path = hermes_config.state_file('PRINT_HANDY_ROAST_STATE', 'roast_state.json')
        try:
            message = await asyncio.to_thread(roast_chat.roast_reply, text, state_path=state_path,
                                              tz=hermes_config.tz())
        except Exception:
            logger.exception('roast request failed')
            message = 'No roast this time; the print history could not be read.'
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message or 'No roast this time.')
    return {'action': 'skip', 'reason': 'roast handled'}


# --- handler interface (#1323) ---
from ._gates import free_text  # noqa: E402

match = free_text
handle = roast_dispatch
