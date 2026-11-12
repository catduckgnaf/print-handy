"""The typed quantity chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""

from .common import _adapter_for, _delete_secret_message, _is_print_discord_event


async def typed_quantity_dispatch(event, gateway, **kwargs):
    """A typed reply ("4", "3 copies") while a print quantity prompt is open answers that prompt."""
    if not _is_print_discord_event(event):
        return None
    source = event.source
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    from print_handy.core.link_intake import typed_reply
    error = typed_reply(source, getattr(event, 'text', ''))
    if error is None:
        return None
    from print_handy.core.link_intake import take_secret_reply
    if error == '' and take_secret_reply():  # a pasted login token: remove that message
        await _delete_secret_message(event, source, _adapter_for(gateway, source))
    if error:
        adapter = _adapter_for(gateway, source)
        if adapter is not None:
            await adapter.send(chat_id=source.chat_id, content=error,
                               metadata={'thread_id': source.thread_id} if source.thread_id else None)
    return {'action': 'skip', 'reason': 'typed print quantity handled'}


# --- handler interface (#1323) ---
from ._gates import free_text  # noqa: E402

match = free_text
handle = typed_quantity_dispatch
