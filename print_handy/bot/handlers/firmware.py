"""The firmware chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio, time

from .common import _confirm, _control_preamble, _decide_printer, _live_key_for, _seen
from ...runtime import _config

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy_print_feedback")


async def firmware_dispatch(event, gateway, **kwargs):
    """/print-firmware: the configured printer's firmware update state (issue #561).

    Bare form (stage 1) reports Moonraker's Update Manager state: one
    user-invoked check (a refresh, then the status read) through the spool
    client, rendered by print_handy.firmware_update with the ZMod pairing
    and Klipper 13 MCU extras where the data exposes them. Printers whose
    connection is not Moonraker have no Update Manager to read and get a
    plain explanation instead of an error. The report path is read-only.

    The update form (stage 2) applies one component: eligibility comes
    from the printer's registry profile (vendor Klipper forks and
    non-Moonraker printers are refused before anything is read), the
    component is resolved against the live status, the printer must be
    idle, and the usual _confirm button shows the exact version change
    before the single upgrade POST; the result is verified by re-reading
    the status, never taken from the POST alone.
    """
    source, adapter = _control_preamble(event, gateway, '/print-firmware', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'firmware UI delivery unavailable'}
    from print_handy import firmware_update, printers
    from print_handy.runtime import spool_sync
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    decided = await _decide_printer(source, gateway, adapter, metadata,
                                    question='Firmware on which printer?')
    if decided is None:
        return {'action': 'skip', 'reason': 'shared channel: no printer chosen for firmware'}
    pid, chosen = decided
    mr = spool_sync.moonraker_for(pid)
    args = str(getattr(event, 'text', '')).strip()[len('/print-firmware'):].split()
    entry, connection = None, None
    try:
        if pid:
            from print_handy import settings
            model_key = settings.by_id(pid, _config.cfg())["model"]
            connection = printers.get(model_key,
                                      settings.view_for(pid, _config.cfg())).connection
        else:
            current = printers.current()
            model_key = current.model.key
            connection = current.connection
        entry = firmware_update.printer_entry(model_key)
    except Exception:
        logger.exception('/print-firmware could not resolve the configured printer')
    if args and args[0].casefold() == 'guide':
        if len(args) != 2 or args[1].casefold() not in ('zmod', 'stock'):
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=('Guides exist for the ZMod base '
                                        '(`/print-firmware guide zmod`) and for the stock '
                                        'Flashforge firmware under it (`/print-firmware '
                                        'guide stock`). Nothing was sent to the printer.'))
            return {'action': 'skip', 'reason': 'print firmware usage shown'}
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=firmware_update.guide_text(args[1].casefold(), entry))
        return {'action': 'skip', 'reason': 'print firmware guide shown'}
    if args and args[0].casefold() == 'update':
        # One component, whose name may be typed in words ("helix screen",
        # "guppy screen"): the words after "update" are the one name, and
        # firmware_update resolves it (aliases included) against the live
        # status only.
        if len(args) < 2:
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=('Use `/print-firmware update <component>` with one '
                                        'component name from the `/print-firmware` report '
                                        '(for example `/print-firmware update klipper`). '
                                        'One component at a time. Nothing was sent to '
                                        'the printer.'))
            return {'action': 'skip', 'reason': 'print firmware usage shown'}
        if connection != 'moonraker' or not firmware_update.is_apply_eligible(entry):
            reason = firmware_update.apply_block_reason(entry)
            message = (reason + ' Nothing was sent to the printer.') if reason else \
                firmware_update.not_moonraker_text(connection)
            await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
            return {'action': 'skip', 'reason': 'print firmware update refused for this printer'}
        key = ('print-firmware-update', source.chat_id, source.thread_id)
        if key in _seen:
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content='A firmware update prompt is already open here. Nothing sent.')
            return {'action': 'skip', 'reason': 'firmware update prompt already active'}
        _seen[key] = time.monotonic()
        live_key = _live_key_for(source, gateway, chosen)
        async def ask(question, choices):
            return await _confirm(adapter, source, metadata, 'print-firmware', question, choices,
                                  (globals().get('_config').safety_confirm_timeout_s()
                                   if globals().get('_config') else 600),
                                  live_key=live_key)

        try:
            # The one confirmed apply flow, shared with the update alert's
            # Update button (print_handy.firmware_alerts).
            message = await firmware_update.confirmed_update(mr, entry, ' '.join(args[1:]), ask)
        except Exception:
            logger.exception('/print-firmware update failed')
            message = ('The firmware update stopped on an error. No confirmation was '
                       'completed, so nothing was sent.')
        finally:
            _seen.pop(key, None)
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
        return {'action': 'skip', 'reason': 'print firmware update handled'}
    if args:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=('Use `/print-firmware` to see the firmware versions '
                                    'your printer reports and what updates are available, '
                                    '`/print-firmware update <component>` to apply one, or '
                                    '`/print-firmware guide zmod` / `/print-firmware guide '
                                    'stock` for the manual update guides. Nothing was sent '
                                    'to the printer.'))
        return {'action': 'skip', 'reason': 'print firmware usage shown'}
    if connection != 'moonraker':
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=firmware_update.guidance_text(entry))
        return {'action': 'skip', 'reason': 'print firmware not applicable to this printer'}
    try:
        message = await asyncio.to_thread(firmware_update.firmware_report, mr, entry,
                                          refresh=True)
    except Exception:
        logger.exception('/print-firmware failed')
        message = firmware_update.COULD_NOT_READ_TEXT
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'print firmware handled'}


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/print-firmware', True)
handle = firmware_dispatch
handle.dispatch_gate = match
