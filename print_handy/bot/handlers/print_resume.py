"""The print resume chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio, time

from .common import (
    _confirm, _control_preamble, _failure_post, _live_key_for, _pick_by_status, _pkw,
    _routed_form, _routed_printer_id, _seen, _undecided_printer_ids,
)
from ...runtime import _config

# Per-module logger (#1404): records name the handler module that
# made them, so [logging] levels rules can target one handler.
logger = logging.getLogger(__name__)


async def print_resume_dispatch(event, gateway, **kwargs):
    """/print-resume: one confirm button, then at most one Moonraker resume. Never restarts anything."""
    source, adapter = _control_preamble(event, gateway, '/print-resume')
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'print resume UI delivery unavailable'}
    from print_handy.runtime import print_control as pc
    from print_handy.runtime import discord_print_summary as summary
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    pid = _routed_printer_id(source, gateway)
    key = ('print-resume', source.chat_id, source.thread_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content='A resume prompt is already open. Nothing sent.')
        return {'action': 'skip', 'reason': 'resume prompt already active'}
    _seen[key] = time.monotonic()
    live_key = gateway._session_key_for_source(source)
    decided_status = None
    candidates = _undecided_printer_ids(source, gateway)
    if candidates:
        # "Resume the current print" in a shared channel (#536): the
        # printer with a resumable print is the target; two resumable
        # prints (or one plus an unreadable printer) is a question; none
        # refuses, naming the printers.
        picked = None
        try:
            picked = await _pick_by_status(
                source, gateway, adapter, metadata, candidates, pc.status, pc.resumable,
                question='Resume which printer?',
                nothing_text=lambda names: f'Nothing to resume: no paused print on {names}. Nothing sent.')
        finally:
            # The prompt lock is released if the picker fails or decides
            # nothing (#1524 review: an exception here used to leave it held).
            if picked is None:
                _seen.pop(key, None)
        if picked is None:
            return {'action': 'skip', 'reason': 'shared channel: no resume target decided'}
        chosen, decided_status = picked
        pid = _routed_form(chosen)
        live_key = _live_key_for(source, gateway, chosen)
    try:
        st = decided_status if decided_status is not None else await asyncio.to_thread(pc.status, **_pkw(pid))
        if not pc.resumable(st):
            message = pc.not_resumable_text(st)
        else:
            name = summary.format_event('started', st['filename'], emit=False).splitlines()[0].split(': ', 1)[-1]
            where = pc.where_text(st)
            answer = await _confirm(adapter, source, metadata, 'print-resume',
                                    f"Resume {name}{' (' + where + ')' if where else ''}? Make sure nothing is in the way.",
                                    [pc.RESUME_LABEL, pc.KEEP_LABEL], (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600),
                                    live_key=live_key)
            if answer != pc.RESUME_LABEL:
                message = 'Left it paused. Nothing sent.'
            else:
                from print_handy import printer_control
                _sent, message = await asyncio.to_thread(printer_control.for_printer(pid).resume, st['filename'])
    except Exception as exc:
        logger.exception('print-resume failed')
        message = _failure_post('Resume', exc, 'Check the printer screen.', subsystem='printer')
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic print resume handled'}


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/print-resume', False)
handle = print_resume_dispatch
handle.dispatch_gate = match
