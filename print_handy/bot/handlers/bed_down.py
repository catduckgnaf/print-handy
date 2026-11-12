"""The bed down chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio, time

from .common import (
    _confirm, _control_preamble, _decide_printer, _failure_post, _live_key_for, _pkw,
    _routed_printer_id, _seen,
)
from ...runtime import _config

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy.bot.handlers")


BED_DOWN_LABEL = 'Separate bed and nozzle'
CANCEL_BED_DOWN_LABEL = 'Cancel'


async def bed_down_dispatch(event, gateway, **kwargs):
    """/print-bed-down: prepare to clean by separating the bed and nozzle.

    The plain-language forms ("reset the bed", "lower the bed", "prepare to
    clean") reach this only through the did-you-mean prompt, so a fuzzy
    match never moves anything by itself. Refused while a print is active;
    asks before moving; and print_handy.runtime.bed_separate moves only when
    Klipper reports Z homed and the bounded target fits, otherwise it posts
    instructions and sends nothing. No homing, no heat.
    """
    source, adapter = _control_preamble(event, gateway, '/print-bed-down')
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'bed down UI delivery unavailable'}
    from print_handy.runtime import bed_separate as bs
    from print_handy.runtime import print_control as pc
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    _route = globals().get('_routed_printer_id')
    pid = _route(source, gateway) if _route else None
    key = ('print-bed-down', source.chat_id, source.thread_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='A bed separation is already open or running. Nothing sent.')
        return {'action': 'skip', 'reason': 'bed down already active'}
    _seen[key] = time.monotonic()
    try:
        live_key = gateway._session_key_for_source(source)
    except Exception:
        live_key = None
    try:
        _decide = globals().get('_decide_printer')
        if _decide is not None and not pid:
            decided = await _decide(source, gateway, adapter, metadata,
                                    question='Separate the bed and nozzle on which printer?')
            if decided is None:
                return {'action': 'skip', 'reason': 'shared channel: no printer chosen for bed down'}
            pid, chosen = decided
            _scoped = globals().get('_live_key_for')
            if _scoped is not None:
                live_key = _scoped(source, gateway, chosen)
        connection = await asyncio.to_thread(bs.printer_connection, pid)
        st = None
        if bs.supports_live_move(connection):
            try:
                st = await asyncio.to_thread(pc.status, **_pkw(pid))
            except Exception:
                logger.warning('print-bed-down: status read failed', exc_info=True)
                st = None
        state = st.get('state') if isinstance(st, dict) else None
        if not bs.supports_live_move(connection):
            message = bs.manual_instructions(
                bs.separate_mm(), 'This printer is not on a Moonraker/Klipper connection.')
        elif st is None:
            message = bs.manual_instructions(
                bs.separate_mm(), "I couldn't read the printer state, so nothing moves.")
        elif state in {'printing', 'paused'}:
            message = (f"Refused: the printer is {state}. Separating the bed and nozzle is only offered "
                       "when the printer is idle or stopped. Nothing sent.")
        else:
            distance = bs.separate_mm()
            motion = await asyncio.to_thread(bs.printer_motion, pid)
            answer = await _confirm(adapter, source, metadata, 'print-bed-down',
                                    bs.confirm_question(distance, motion),
                                    [BED_DOWN_LABEL, CANCEL_BED_DOWN_LABEL],
                                    (globals().get('_config').safety_confirm_timeout_s()
                                     if globals().get('_config') else 600),
                                    live_key=live_key)
            if answer != BED_DOWN_LABEL:
                message = 'Not moved. Nothing sent.'
            else:
                _ok, message = await asyncio.to_thread(
                    bs.separate, confirmed_at=time.time(), distance_mm=distance, printer_id=pid)
    except Exception as exc:
        logger.exception('print-bed-down failed')
        message = _failure_post('Bed separation', exc, 'Check the printer screen.', subsystem='printer')
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic bed down handled'}


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/print-bed-down', False)
handle = bed_down_dispatch
handle.dispatch_gate = match
