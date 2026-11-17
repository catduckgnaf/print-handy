"""The print pause chat handler (#1524).

/print-pause: pause the print that is running. The plain-language table in
``bot/runtime.py`` has always suggested this command for "pause the print",
but nothing registered it, so the suggestion led nowhere.

Pause is the one deliberate act the alert and status buttons already take
without a second question: it is reversible and it is time-critical (the
print is going wrong now). Typing the command is that act, so there is no
confirm prompt here. The send itself is the status handler's own pause
path (``print_status._pause``): Bambu LAN and the adapter families through
the control service, Moonraker through the camera alert's Pause, which
re-reads, sends once and reads back; a printer with no live connection says
so and sends nothing.
"""
import asyncio
import logging
import time

from .common import (
    _control_preamble, _failure_post, _pick_by_status, _pkw, _routed_form, _routed_printer_id,
    _seen, _undecided_printer_ids,
)
from .print_status import _pause, _real_printer_id

logger = logging.getLogger(__name__)

RESUME_HINT = "Say /print-resume to carry on."


def not_pausable_text(st):
    """Why there is nothing to pause, in the state's own words."""
    state = str(st.get('state') or 'unknown')
    if state == 'paused':
        return f'Already paused. Nothing sent. {RESUME_HINT}'
    return f'Nothing to pause: the printer is {state}. Nothing sent.'


def _job_status(**kw):
    """The pause pre-read: job state only, no Bambu camera grab (time-critical path)."""
    from print_handy.runtime import print_stop
    return print_stop.status(camera=False, **kw)


async def print_pause_dispatch(event, gateway, **kwargs):
    """/print-pause: at most one pause for the print that is running. Never stops or restarts anything."""
    source, adapter = _control_preamble(event, gateway, '/print-pause')
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'print pause UI delivery unavailable'}
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    pid = _routed_printer_id(source, gateway)
    decided_status = None
    candidates = _undecided_printer_ids(source, gateway)
    if candidates:
        # A shared channel: the printer that is printing is the target; two
        # printing (or one printing and one unreadable) is a question; none
        # refuses by name.
        try:
            picked = await _pick_by_status(
                source, gateway, adapter, metadata, candidates, _job_status,
                lambda st: st.get('state') == 'printing',
                question='Pause which printer?',
                nothing_text=lambda names: f'Nothing to pause: no print running on {names}. Nothing sent.')
        except Exception as exc:
            logger.exception('print-pause could not choose a printer')
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=_failure_post('Pause', exc, 'Check the printer screen.',
                                                     subsystem='printer'))
            return {'action': 'skip', 'reason': 'print pause target failed'}
        if picked is None:
            return {'action': 'skip', 'reason': 'shared channel: no pause target decided'}
        chosen, decided_status = picked
        pid = _routed_form(chosen)
    # One pause per printer at a time, taken once the printer is decided:
    # an open picker never blocks a pause for another printer (#1524 review).
    key = ('print-pause', _real_printer_id(pid) or '')
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='A pause for this printer is already in progress. Nothing sent.')
        return {'action': 'skip', 'reason': 'pause already active'}
    _seen[key] = time.monotonic()
    try:
        st = decided_status if decided_status is not None else await asyncio.to_thread(
            _job_status, **_pkw(pid))
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
