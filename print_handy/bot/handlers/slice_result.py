"""The slice-result chat handler (#1525, tranche G wiring).

/print-slice-result [job]: the per-plate statistics card for an archived
job — thumbnail presence, layer count, estimated time and estimated
grams per logical filament and per mapped slot, from
``core.slice_result`` (the production caller the Web UI's slice-result
page shares). Read-only: it slices nothing, starts nothing and charges
no spool; grams are the slicer's estimates and a value the job's files
do not prove is reported as unknown.
"""
import asyncio
import logging

from .common import _control_preamble, _state_dir

# Per-module logger (#1404): records name the handler module that
# made them, so [logging] levels rules can target one handler.
logger = logging.getLogger(__name__)


def _resolve_job_id(ref):
    """The archived job id a /print-slice-result argument names, or the
    newest archived job when no argument is given (blocking file work,
    called through asyncio.to_thread)."""
    from pathlib import Path

    from print_handy.core import model_job
    if ref:
        from .color_selection import resolve_job_ref
        job_path = resolve_job_ref(ref, model_job.ARCHIVE)
        return job_path.parent.name if job_path is not None else None
    folders = sorted(Path(model_job.ARCHIVE).glob('*/JOB.json'),
                     key=lambda p: p.stat().st_mtime if p.exists() else 0,
                     reverse=True)
    return folders[0].parent.name if folders else None


async def slice_result_dispatch(event, gateway, **kwargs):
    source, adapter = _control_preamble(event, gateway, '/print-slice-result', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'slice result delivery unavailable'}
    from print_handy.core import model_job, slice_result
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    text = str(getattr(event, 'text', '')).strip()
    ref = text[len('/print-slice-result'):].strip()
    try:
        job_id = await asyncio.to_thread(_resolve_job_id, ref)
        artifacts = (await asyncio.to_thread(
            slice_result.result_for_job, model_job.ARCHIVE, job_id)
            if job_id else None)
        if artifacts is None:
            message = ('No sliced job matches that yet. Slice something first, or '
                       'name a job from `/print-again list`. Nothing was changed.')
        else:
            message = slice_result.render_text(artifacts)
            # The actual thumbnail rides along when the plate carries
            # one: the delivery layer turns the MEDIA: marker into the
            # attachment (the diagnostics-bundle pattern).
            png = await asyncio.to_thread(
                slice_result.thumbnail_png, model_job.ARCHIVE, job_id, 0)
            if png:
                folder = _state_dir() / 'slice-result-thumbnails'
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / f'{job_id}-plate-1.png'
                target.write_bytes(png)
                message += f'\nMEDIA:{target}'
    except Exception:
        logger.exception('/print-slice-result failed')
        message = 'Could not read that job\'s slice result. Nothing was changed.'
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'slice result handled'}


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/print-slice-result', True)
handle = slice_result_dispatch
handle.dispatch_gate = match
