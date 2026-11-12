"""The upload wait chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import re, time

from .common import _is_print_discord_event
from ...hermes import _config


# Upload-wait freeze length. Read at use time so a settings override in tests is honored.
_upload_waits = {}


def _wait_key(source):
    return (getattr(source, 'profile', None), str(getattr(source, 'chat_id', '')),
            str(getattr(source, 'thread_id', '') or ''))


async def upload_wait_dispatch(event, gateway, **kwargs):
    """Freeze a Discord intake while uploads arrive; only an explicit resume releases it."""
    if not _is_print_discord_event(event):
        return None
    source = event.source
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    text = str(getattr(event, 'text', '') or '').strip().casefold()
    key = _wait_key(source)
    now = time.monotonic()
    until = _upload_waits.get(key, 0)
    if until <= now:
        _upload_waits.pop(key, None)
        until = 0
    waiting = bool(re.search(r'\b(wait|hold|pause)\b.*\b(upload|attach|file)s?\b', text))
    resume = bool(re.search(r'\b(resume|continue|uploads? (?:are )?(?:done|complete|finished))\b', text))
    if waiting:
        _upload_waits[key] = now + (globals().get('_config').upload_wait_s() if globals().get('_config') else 3600)
        return {'action': 'skip', 'reason': 'upload wait active'}
    if until:
        if resume:
            _upload_waits.pop(key, None)
            return None
        return {'action': 'skip', 'reason': 'upload wait active; explicit resume required'}
    return None


# --- handler interface (#1323) ---
from ._gates import free_text  # noqa: E402

match = free_text
handle = upload_wait_dispatch
