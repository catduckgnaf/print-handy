"""The color selection chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import json, logging, time
from pathlib import Path

from .common import _adapter_for, _failure_post, _is_bot_platform, _is_hosting_profile, _pkw, _seen

# Per-module logger (#1404): records name the handler module that
# made them, so [logging] levels rules can target one handler.
logger = logging.getLogger(__name__)


def resolve_job_ref(ref, archive=None):
    """The archived JOB.json a /print-colors argument names, or None.

    The slash form's job option is picked from autocomplete (#1475), so the
    argument is a job reference a person can supply — a print number, an
    archive folder name or a job title — never a server file path. The
    legacy typed form (a full path to an archived JOB.json) still resolves
    here, so old messages and scripts keep working.
    """
    from print_handy.core import model_job
    archive = Path(archive if archive is not None else model_job.ARCHIVE)
    raw = str(ref or '').strip()
    if not raw:
        return None
    direct = Path(raw).expanduser()
    if direct.name == 'JOB.json' and direct.is_file():
        return direct.resolve()
    if raw.isdigit():  # a print number, the autocomplete value for printed jobs
        try:
            from print_handy.runtime import print_control
            for row in print_control.printed_jobs(archive):
                if row.get('index') == int(raw):
                    return Path(row['job_json']).resolve()
        except Exception:
            logger.debug("print number lookup for color selection failed", exc_info=True)
    try:
        for candidate in sorted(archive.glob('*/JOB.json')):
            if candidate.parent.name == raw:
                return candidate.resolve()
            try:
                title = json.loads(candidate.read_text()).get('title')
            except Exception:
                logger.debug("archived JOB.json title unreadable: %s", candidate, exc_info=True)
                title = None
            if title and str(title) == raw:
                return candidate.resolve()
    except OSError:
        logger.debug("archive scan for color selection failed", exc_info=True)
    return None


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
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    path = resolve_job_ref(text[len('/print-colors '):], ARCHIVE)
    if path is None:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=('No archived job matches that. Pick one with '
                                    '`/print-colors` in Discord, or use a print number '
                                    'from `/print-again list`. Nothing was changed.'))
        return {'action': 'skip', 'reason': 'color selection job not found'}
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
