"""The print pause chat handler (#1524).

/print-pause: pause the print that is running. The plain-language table in
``bot/runtime.py`` has always suggested this command for "pause the print",
but nothing registered it, so the suggestion led nowhere.

Pause is the one deliberate act the alert and status buttons already take
without a second question: it is reversible and it is time-critical (the
print is going wrong now). Typing the command is that act, so there is no
confirm prompt here. The send itself is the status handler's own pause
path (``print_status._pause``): Bambu through the control service,
Moonraker through the camera alert's Pause, which re-reads, sends once and
reads back, and a family with no pause surface says so and sends nothing.
"""
import asyncio
import logging
import time

from .common import (
    _ask_which_printer, _busy_label, _candidate_statuses, _control_preamble, _failure_post,
    _pkw, _printer_label, _routed_form, _routed_printer_id, _seen, _undecided_printer_ids,
)
from .print_status import _pause

logger = logging.getLogger(__name__)

RESUME_HINT = "Say /print-resume to carry on."


def not_pausable_text(st):
    """Why there is nothing to pause, in the state's own words."""
    state = str((st or {}).get('state') or 'unknown')
    if state == 'paused':
        return f'Already paused. Nothing sent. {RESUME_HINT}'
    return f'Nothing to pause: the printer is {state}. Nothing sent.'


async def print_pause_dispatch(event, gateway, **kwargs):
    """/print-pause: at most one pause for the print that is running. Never stops or restarts anything."""
    source, adapter = _control_preamble(event, gateway, '/print-pause')
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'print pause UI delivery unavailable'}
    from print_handy.runtime import print_stop
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    pid = _routed_printer_id(source, gateway)
    key = ('print-pause', source.chat_id, source.thread_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='A pause is already in progress. Nothing sent.')
        return {'action': 'skip', 'reason': 'pause already active'}
    _seen[key] = time.monotonic()
    try:
        decided_status = None
        if not pid:
            candidates = _undecided_printer_ids(source, gateway)
            if candidates:
                # A shared channel: the printer that is printing is the
                # target; two printing is a question; none refuses by name.
                statuses = await _candidate_statuses(candidates, print_stop.status)
                printing = {cid: st for cid, st in statuses.items()
                            if isinstance(st, dict) and st.get('state') == 'printing'}
                chosen = None
                if len(printing) == 1:
                    chosen = next(iter(printing))
                elif len(printing) > 1:
                    chosen = await _ask_which_printer(
                        source, gateway, adapter, list(printing),
                        question='More than one printer is printing. Pause which one?',
                        label_for=lambda cid: _busy_label(printing.get(cid)), metadata=metadata)
                if chosen is None:
                    names = ' or '.join(_printer_label(c) for c in candidates)
                    await adapter.send(
                        chat_id=source.chat_id, metadata=metadata,
                        content=('No printer chosen. Nothing sent.' if printing else
                                 f'Nothing to pause: no print running on {names}. Nothing sent.'))
                    return {'action': 'skip', 'reason': 'shared channel: no pause target decided'}
                pid, decided_status = _routed_form(chosen), printing[chosen]
        st = decided_status if decided_status is not None else await asyncio.to_thread(
            print_stop.status, **_pkw(pid))
        if st.get('state') != 'printing':
            message = not_pausable_text(st)
        else:
            from print_handy import printer_control
            control = printer_control.for_printer(pid)
            message = await asyncio.to_thread(_pause, control, pid, st.get('filename') or '')
    except Exception as exc:
        logger.exception('print-pause failed')
        message = _failure_post('Pause', exc, 'Check the printer screen.', subsystem='printer')
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic print pause handled'}


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/print-pause', False)
handle = print_pause_dispatch
handle.dispatch_gate = match
