"""The color selection chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import json, time
from pathlib import Path

from .common import _adapter_for, _failure_post, _is_bot_platform, _is_hosting_profile, _pkw, _seen


async def color_selection_dispatch(event, gateway, **kwargs):
    """Explicit color command: real Discord UI, no physical or slicing actions."""
    source = getattr(event, 'source', None)
    text = str(getattr(event, 'text', '')).strip()
    if not text.startswith('/print-colors '):
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
        return {'action': 'skip', 'reason': 'color UI delivery unavailable'}
    from print_handy.core import printer_state
    from print_handy.core.discord_color_intake import collect
    from print_handy.core.link_intake import PromptCancelled
    from print_handy.core import model_job
    ARCHIVE = model_job.ARCHIVE
    path = Path(text[len('/print-colors '):]).expanduser().resolve()
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    key = ('color-ui', source.chat_id, source.thread_id)
    if key in _seen:
        return {'action': 'skip', 'reason': 'color selection already active'}
    _seen[key] = time.monotonic()
    try:
        if path.name != 'JOB.json' or not path.is_relative_to(ARCHIVE.resolve()):
            raise ValueError('color command requires an archived JOB.json')
        # Colors bind against the JOB's printer (#335): its recorded
        # printer_id picks the state record, never the channel's or the
        # configured printer's.
        try:
            job_pid = model_job._job_printer_id(json.loads(path.read_text()))
        except Exception:
            job_pid = None
        result = await collect(path, printer_state.load(**_pkw(job_pid)), adapter, source.chat_id,
                               f'print-colors:{source.chat_id}:{source.thread_id}', metadata)
        message = json.dumps(result)
    except PromptCancelled:
        message = 'Cancelled. No colors changed, nothing sliced, nothing started.'
    except Exception as exc:
        message = _failure_post('Color selection', exc, 'No slicing, upload or print started.', subsystem='intake')
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic color selection handled'}


# --- handler interface (#1323) ---
from ._gates import prefix_gate  # noqa: E402

match = prefix_gate('/print-colors ', '/print-colors')
handle = color_selection_dispatch
handle.dispatch_gate = match
