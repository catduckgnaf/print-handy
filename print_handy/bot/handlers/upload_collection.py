"""The upload collection chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""

from .common import (
    _INTAKE_UNAVAILABLE_REASON, _adapter_for, _channel_printer_id, _is_print_discord_event,
    _live_key_for, _question_timeout,
)
from .model_link import model_link_dispatch


async def upload_collection_dispatch(event, gateway, **kwargs):
    """Collect one project's files across several messages (#1204 item 2).

    Added for #1204: one hook, placed directly before model_link_dispatch.
    It claims a message only when the message is about this user's
    collection in this chat, thread and printer ("add to project", "done
    adding", "cancel project", or an upload while that collection is open);
    everything else returns None and reaches model_link_dispatch exactly as
    before. "Done adding" runs model_link_dispatch itself on the collected
    files, so printer choice, intake and every gate are the usual ones.
    """
    if not _is_print_discord_event(event):
        return None
    source = event.source
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    from print_handy.core import upload_collection
    from print_handy.core.link_intake import model_link
    text = getattr(event, 'text', '') or ''
    if model_link(text):
        return None  # a model link keeps its own intake; collections gather uploads
    from print_handy.core.prepared_intake import archive_files, model_files
    files = model_files(event)
    if upload_collection.intent(text) is None and not files:
        return None  # the common case: no state read at all
    adapter = _adapter_for(gateway, source)
    if adapter is None:
        return None

    async def run_intake(paths, row):
        from types import SimpleNamespace
        collected = SimpleNamespace(
            source=source, text='', internal=False, media_urls=[str(p) for p in paths],
            message_id=f"collection-{row['collection_id']}",
            metadata={'upload_collection': {
                'collection_id': row['collection_id'],
                'files': [{k: f.get(k) for k in ('n', 'name', 'sha256', 'size', 'message_id')}
                          for f in row.get('files') or []]}})
        result = await model_link_dispatch(collected, gateway)
        if isinstance(result, dict) and result.get('reason') == _INTAKE_UNAVAILABLE_REASON:
            raise upload_collection.IntakeFailed(result['reason'])  # reopen the collection
        return result

    return await upload_collection.handle(
        event, adapter, printer_id=_channel_printer_id(source, gateway), files=files,
        run_intake=run_intake, timeout=_question_timeout(), live_key=_live_key_for(source, gateway),
        archives=archive_files(event))


# --- handler interface (#1323) ---
from ._gates import free_text  # noqa: E402

match = free_text
handle = upload_collection_dispatch
