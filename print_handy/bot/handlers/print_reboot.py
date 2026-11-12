"""The print reboot chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio, re, time

from .common import (
    _adapter_for, _confirm, _control_preamble, _decide_printer, _failure_post,
    _is_print_discord_event, _live_key_for, _routed_printer_id, _seen,
)
from ...runtime import _config

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy.bot.handlers")


def _print_restart_request(event):
    """Recognize direct natural-language requests, not mere mentions of restarting."""
    text = str(getattr(event, 'text', '') or '').strip().casefold()
    if re.search(r'\b(?:do not|don.t|never|avoid|without)\b.{0,30}\b(?:restart|reboot|power[- ]?cycle)\b', text):
        return False
    if re.search(r'\b(?:should|could|would|can|may|need to|want to)\b.{0,25}\b(?:restart|reboot|power[- ]?cycle)\b', text):
        return True
    return bool(re.fullmatch(r'(?:please\s+)?(?:restart|reboot)(?:\s+(?:the\s+)?(?:printer|machine|firmware))?[.! ]*', text)
                or re.search(r'\b(?:restart|reboot)\s+(?:the\s+)?(?:printer|machine|firmware)\b', text))


async def print_reboot_dispatch(event, gateway, **kwargs):
    """Natural restart requests use the safe ZMod REBOOT button flow."""
    source, adapter = _control_preamble(event, gateway, '/print-reboot')
    if source is None:
        if not _is_print_discord_event(event):
            return None
        text = str(getattr(event, 'text', '') or '').strip().casefold()
        if not _print_restart_request(event):
            return None
        source = event.source
        if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
            return None
        adapter = _adapter_for(gateway, source)
        if adapter is None:
            return {'action': 'skip', 'reason': 'print reboot UI delivery unavailable'}
    if adapter is None:
        return {'action': 'skip', 'reason': 'print reboot UI delivery unavailable'}
    from print_handy.runtime import print_control as pc
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    # The routing helpers are resolved defensively: an offline regression
    # test execs this function alone in a bare namespace (test_reboot_standby).
    _route = globals().get('_routed_printer_id')
    pid = _route(source, gateway) if _route else None
    pkw = {'printer_id': pid} if pid else {}
    key = ('print-reboot', source.chat_id, source.thread_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content='A restart is already open or running. Nothing sent.')
        return {'action': 'skip', 'reason': 'reboot already active'}
    _seen[key] = time.monotonic()
    try:
        live_key = gateway._session_key_for_source(source)
    except Exception:
        live_key = None  # a bare-namespace host may not offer session keys
    _decide = globals().get('_decide_printer')
    if _decide is not None and not pid:
        decided = await _decide(source, gateway, adapter, metadata,
                                question='Restart which printer?')
        if decided is None:
            _seen.pop(key, None)
            return {'action': 'skip', 'reason': 'shared channel: no printer chosen for reboot'}
        pid, chosen = decided
        pkw = {'printer_id': pid} if pid else {}
        _scoped = globals().get('_live_key_for')
        if _scoped is not None:
            live_key = _scoped(source, gateway, chosen)
    try:
        st = await asyncio.to_thread(pc.status, **pkw)
        if st.get('klippy') == 'shutdown' and st.get('state') != 'printing':
            # Emergency stop: REBOOT is a G-code macro Klipper rejects while shut down.
            # One FIRMWARE_RESTART clears it; no motion, no heat. Still needs the confirm button.
            from print_handy.runtime import print_alert_buttons as pab
            reason = await asyncio.to_thread(pab.shutdown_message, **pkw)
            answer = await _confirm(adapter, source, metadata, 'print-reboot',
                                    "The printer is in an emergency stop. " + pab.explain(reason)
                                    + "\nClear it with one firmware restart? No motion or heat. This ends the "
                                    "stopped job for good. Only do this after the bed and nozzle are clear.",
                                    [pc.REBOOT_LABEL, pc.CANCEL_REBOOT_LABEL], (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600),
                                    live_key=live_key)
            if answer != pc.REBOOT_LABEL:
                message = 'Not restarted. Nothing sent.'
            else:
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content='Clearing the emergency stop now. Up to a minute.')
                from print_handy import printer_control
                _ok, message = await asyncio.to_thread(
                    printer_control.for_printer(pkw.get('printer_id')).clear_shutdown)
                after = await asyncio.to_thread(pc.status, **pkw)
                message += f" Job state now {after.get('state')!r}."
        elif st.get('state') not in {'idle', 'standby', 'complete', 'cancelled', 'error'} or st.get('klippy') != 'ready':
            message = (f"Refused: printer job state is {st.get('state')!r}, controller state is "
                       f"{st.get('klippy')!r}. Check the printer screen before any restart. Nothing sent.")
        else:
            answer = await _confirm(adapter, source, metadata, 'print-reboot',
                                    "Force restart the AD5X? This clears a stuck screen or heaters, and it wipes "
                                    "any Resume the printer screen is offering. Takes about two minutes.",
                                    [pc.REBOOT_LABEL, pc.CANCEL_REBOOT_LABEL], (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600),
                                    live_key=live_key)
            if answer != pc.REBOOT_LABEL:
                message = 'Not restarted. Nothing sent.'
            else:
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content='Restarting the printer now. About two minutes.')
                from print_handy import printer_control
                _ok, message = await asyncio.to_thread(
                    printer_control.for_printer(pkw.get('printer_id')).force_reboot)
    except Exception as exc:
        logger.exception('print-reboot failed')
        message = _failure_post('Restart', exc, 'Check the printer screen.', subsystem='printer')
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic print reboot handled'}


# --- handler interface (#1323) ---
from ._gates import free_text  # noqa: E402

match = free_text
handle = print_reboot_dispatch
