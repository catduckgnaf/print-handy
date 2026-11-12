"""The runout intake chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio

from .common import (
    _adapter_for, _answer_spool_question, _cancel_spool_question_print, _is_bot_platform,
    _is_hosting_profile, _routed_printer_id,
)

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy.bot.handlers")


def _discord_operator(event, gateway):
    """True for an authorized human Discord message in this profile."""
    source = getattr(event, 'source', None)
    if (source is None or not _is_bot_platform(source)
            or not _is_hosting_profile(getattr(source, 'profile', None))
            or getattr(event, 'internal', False) or getattr(source, 'is_bot', False)
            or getattr(source, 'profile_route_rejected', False)):
        return None
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    return source


async def runout_intake_dispatch(event, gateway, **kwargs):
    """Answer a pending filament-runout pause. Inventory writes only — except cancel.

    A generic pause leaves no pending record, so an ordinary message is ignored.
    Nothing here resumes, heats, or moves the printer. The one exception is an
    unambiguous typed cancel ("cancel the print"), which routes to the same
    print_stop.cancel path as the Cancel print button and /print-stop.
    """
    source = _discord_operator(event, gateway)
    text = str(getattr(event, 'text', '')).strip()
    if source is None or not text or text.startswith('/'):
        return None
    from print_handy.runtime import moonraker_event_listener as listener
    context = listener.load_runout_pause()
    if context is None:
        return None
    if _routed_printer_id(source, gateway):
        # The pending runout belongs to the listener's printer — the default
        # printer until the listener itself is per-printer (Stage 2d). A
        # routed channel must not answer it or bind its printer's slots.
        return None
    adapter = _adapter_for(gateway, source)
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    if listener.parse_cancel_print_reply(text):
        from print_handy.runtime import spool_sync
        try:
            message = await asyncio.to_thread(_cancel_spool_question_print, listener, spool_sync, context)
        except Exception as exc:
            logger.exception('spool question cancel failed')
            await adapter.send(chat_id=source.chat_id, metadata=metadata, content=(
                'The print was not cancelled. The spool question is still open and usage '
                'is still paused — answer it, or try cancelling again.'))
            return {'action': 'skip', 'reason': 'spool question cancel failed, question left open'}
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
        return {'action': 'skip', 'reason': 'spool question print cancelled'}
    choice = listener.parse_runout_reply(text)
    if text.strip().casefold() in ('same', 'same spool', 'no change'):
        choice = {'status': 'same'}
    if choice['status'] in ('need_choice', 'need_spool_number', 'need_new_fields', 'unparsed'):
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=(
            'Spool question is still open and usage is still paused. Reply with a spool number, '
            '`same filament` for a fresh roll of the run-out filament, `refeed` or `fix feed` '
            'if the same physical spool stays in and you cleared its stuck feed or jam, or '
            '`cancel the print` — or tap a button on the question in Discord. '
            'Nothing was changed.'))
        return {'action': 'skip', 'reason': 'spool reply needs a choice'}
    from print_handy.runtime import spool_sync
    try:
        message = await asyncio.to_thread(_answer_spool_question, listener, spool_sync, context, choice)
    except Exception as exc:
        logger.exception('spool question answer failed')
        # Only an answer that created a spool before failing says so; every
        # other failure changed nothing and keeps the pinned fail-closed copy.
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=str(exc) if getattr(exc, 'spool_created', None)
                           or getattr(exc, 'color_sanity', None) else
                           'That answer did not work, so nothing was changed. Usage is still paused; please answer again.')
        return {'action': 'skip', 'reason': 'spool answer failed closed'}
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'spool question answered'}


# --- handler interface (#1323) ---
from ._gates import free_text  # noqa: E402

match = free_text
handle = runout_intake_dispatch
