"""The exclude chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio, functools, time
from pathlib import Path

from .common import (
    _confirm, _control_preamble, _failure_post, _pkw, _routed_printer_id, _seen,
    _undecided_printer_ids,
)
from ...runtime import _config

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy_print_feedback")


async def exclude_dispatch(event, gateway, **kwargs):
    """/exclude <name>: ask once, then one Moonraker exclude. The last object is /print-stop's confirm."""
    source, adapter = _control_preamble(event, gateway, '/exclude', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'exclude UI delivery unavailable'}
    from print_handy.core.exclude_object import exclude_plan, object_names
    from print_handy.runtime import discord_print_summary as summary
    from print_handy.runtime import exclude_chat, print_stop
    from print_handy.runtime import print_control as pc
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    text = str(getattr(event, 'text', '')).strip()
    target = text[len('/exclude'):].strip()
    pid = _routed_printer_id(source, gateway)
    if _undecided_printer_ids(source, gateway):
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='This chat is not tied to one printer. Nothing excluded.')
        return {'action': 'skip', 'reason': 'shared channel: no exclude target'}
    key = ('exclude', source.chat_id, source.thread_id, target)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='An exclude prompt for that object is already open. Nothing sent.')
        return {'action': 'skip', 'reason': 'exclude prompt already active'}
    _seen[key] = time.monotonic()
    live_key = gateway._session_key_for_source(source)
    try:
        live = functools.partial(exclude_chat.read_live, pid)
        try:
            st = await asyncio.to_thread(live)
        except Exception:
            st = None
        if st is None:
            message = 'Could not check the printer, so no exclude was offered. Nothing sent.'
        elif st.get('state') not in print_stop.ACTIVE:
            message = f"Nothing to exclude: printer is {st.get('state')}. Nothing sent."
        else:
            _job, plate = await asyncio.to_thread(summary.find_job, st.get('filename') or '')
            path = (plate or {}).get('gcode') if isinstance(plate, dict) else None
            try:
                gcode = Path(path).read_text(encoding='utf-8', errors='replace') if path else ''
            except OSError:
                gcode = ''
            names = object_names(gcode)
            left = exclude_chat.still_printing(names, st)
            plan = exclude_plan(names, target, confirmed=False, remaining=left)
            if plan['action'] == 'refuse' or not names:
                message = plan['reason'] + ' Nothing sent.'
            elif plan['action'] == 'cancel_print':
                answer = await _confirm(adapter, source, metadata, 'exclude-stop',
                                        plan['reason'] + ' Stop the whole print?',
                                        [print_stop.STOP_LABEL, print_stop.KEEP_LABEL],
                                        (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600),
                                        live_key=live_key)
                if answer != print_stop.STOP_LABEL:
                    message = 'Kept printing. Nothing sent.'
                else:
                    from print_handy import printer_control
                    _sent, message = await asyncio.to_thread(printer_control.for_printer(pid).cancel,
                                                             st.get('filename') or '')
            else:
                answer = await _confirm(adapter, source, metadata, 'exclude',
                                        plan['reason'],
                                        ['Cancel this object', 'Keep it'],
                                        (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600),
                                        live_key=live_key)
                if answer != 'Cancel this object':
                    message = 'Kept that object. Nothing sent.'
                else:
                    def sender(script):
                        pc._send('/printer/gcode/script', method='POST', data={'script': script},
                                 timeout=30, **_pkw(pid))
                    result = await asyncio.to_thread(
                        exclude_chat.confirm_send,
                        {'kind': 'exclude-go', 'name': target, 'names': names,
                         'filename': st.get('filename') or ''}, sender, live)
                    message = result['text']
    except Exception as exc:
        logger.exception('exclude failed')
        message = _failure_post('Exclude', exc, 'Check the printer screen. Nothing was excluded.', subsystem='printer')
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic exclude handled'}


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/exclude', True)
handle = exclude_dispatch
handle.dispatch_gate = match
