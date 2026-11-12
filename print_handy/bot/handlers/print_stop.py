"""The print stop chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio, time

from .common import (
    _adapter_for, _ask_which_printer, _busy_label, _candidate_statuses, _failure_post,
    _is_bot_platform, _is_hosting_profile, _live_key_for, _pkw, _printer_label, _routed_form,
    _routed_printer_id, _seen, _undecided_printer_ids,
)
from ...runtime import _config

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy_print_feedback")


async def print_stop_dispatch(event, gateway, **kwargs):
    """/print-stop: one confirm button, then at most one Moonraker cancel for the confirmed file."""
    source = getattr(event, 'source', None)
    text = str(getattr(event, 'text', '')).strip()
    if text != '/print-stop':
        return None
    if (source is None or not _is_bot_platform(source)
            or not _is_hosting_profile(getattr(source, 'profile', None))
            or getattr(event, 'internal', False) or getattr(source, 'is_bot', False)
            or getattr(source, 'profile_route_rejected', False)):
        return None
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    adapter = _adapter_for(gateway, source)
    if adapter is None:
        return {'action': 'skip', 'reason': 'print stop UI delivery unavailable'}
    import uuid
    from print_handy.runtime import print_stop
    from print_handy.runtime import discord_print_summary as summary
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    pid = _routed_printer_id(source, gateway)
    key = ('print-stop', source.chat_id, source.thread_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='A stop prompt is already open. Nothing sent.')
        return {'action': 'skip', 'reason': 'stop prompt already active'}
    _seen[key] = time.monotonic()
    live_key = gateway._session_key_for_source(source)
    decided_status = None
    if not pid:
        candidates = _undecided_printer_ids(source, gateway)
        if candidates:
            # "Stop the current print" in a shared channel (#536): the
            # printer with a print running is the target. Two running
            # prints is a question, never a guess; none running refuses,
            # naming the printers it checked.
            statuses = await _candidate_statuses(candidates, print_stop.status)
            active = {cid: st for cid, st in statuses.items()
                      if isinstance(st, dict) and st.get('state') in print_stop.ACTIVE}
            chosen = None
            if len(active) == 1:
                chosen = next(iter(active))
            elif len(active) > 1:
                chosen = await _ask_which_printer(
                    source, gateway, adapter, list(active),
                    question='More than one printer has a print running. Stop which one?',
                    label_for=lambda cid: _busy_label(active.get(cid)), metadata=metadata)
            if chosen is None:
                _seen.pop(key, None)
                names = ' or '.join(_printer_label(c) for c in candidates)
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content=('No printer chosen. Nothing sent.' if active else
                                            f'Nothing to stop: no print is running on {names}. Nothing sent.'))
                return {'action': 'skip', 'reason': 'shared channel: no stop target decided'}
            pid, decided_status = _routed_form(chosen), active[chosen]
            live_key = _live_key_for(source, gateway, chosen)
    try:
        st = decided_status if decided_status is not None else await asyncio.to_thread(print_stop.status, **_pkw(pid))
        if st.get('camera_image_path'):
            # Bambu camera parity (#294): show the camera image alongside the status.
            try:
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content=f"Camera now — {st['filename'] or 'current print'}:\nMEDIA:{st['camera_image_path']}")
            except Exception:
                pass
        if st['state'] not in print_stop.ACTIVE:
            message = f"Nothing to stop: printer is {st['state']}. Nothing sent."
        else:
            from print_handy.core.prompts import gateway as _prompts
            cm = _prompts()
            name = summary.format_event('started', st['filename'], emit=False).splitlines()[0].split(': ', 1)[-1]
            where = print_stop.where_text(st)
            question = f"Stop {name} ({st['state']}{', ' + where if where else ''})? This cancels the print."
            choices = [print_stop.STOP_LABEL, print_stop.KEEP_LABEL]
            cid = uuid.uuid4().hex
            session_key = f'print-stop:{source.chat_id}:{source.thread_id}'
            # Register under the live chat session key (the _confirm pattern):
            # typed replies route by that key, so a flow-key registration can
            # never be answered on buttonless platforms. send_clarify keeps
            # the flow key for delivery.
            cm.register(cid, live_key or session_key, question, choices)
            try:
                sent = await adapter.send_clarify(chat_id=str(source.chat_id), question=question,
                                                  choices=choices, clarify_id=cid,
                                                  session_key=session_key, metadata=metadata)
                if not getattr(sent, 'success', False):
                    raise RuntimeError('button delivery failed')
                answer = await asyncio.to_thread(cm.wait_for_response, cid, (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600))
            finally:
                cm.resolve_gateway_clarify(cid, cm.CANCELLED)
            if answer != print_stop.STOP_LABEL:
                message = 'Kept printing. Nothing sent.'
            else:
                from print_handy import printer_control
                _sent, message = await asyncio.to_thread(printer_control.for_printer(pid).cancel, st['filename'])
    except Exception as exc:
        logger.exception('print-stop failed')
        message = _failure_post('Stop', exc, 'Check the printer screen.', subsystem='printer')
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic print stop handled'}


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/print-stop', False)
handle = print_stop_dispatch
handle.dispatch_gate = match
