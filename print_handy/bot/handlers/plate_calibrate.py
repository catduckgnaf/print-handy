"""The plate calibrate chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import asyncio

from .common import _confirm, _control_preamble
from ...runtime import _config


async def plate_calibrate_dispatch(event, gateway, **kwargs):
    """/print-plate-calibrate [capture|status]: calibrate the local empty-plate reference (#455 item 4, Stage 1).

    capture snapshots the camera, stages the frame and shows it back;
    only the operator's "Make this my empty plate" answer promotes it
    to the live reference — Retake (or no answer) discards the staged
    frame and the previous reference keeps judging. The calibration
    logic lives in print_handy.runtime.local_empty so a Web UI surface
    can drive the same flow later; this command is the v1 surface.
    Nothing here moves the printer or starts a print.
    """
    source, adapter = _control_preamble(event, gateway, '/print-plate-calibrate', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'plate calibration UI delivery unavailable'}
    from print_handy.runtime import local_empty
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    cfg = _config.cfg()
    args = str(getattr(event, 'text', '')).strip()[len('/print-plate-calibrate'):].split()
    verb = args[0].casefold() if args else 'status'
    if verb == 'capture':
        if not local_empty.empty_enabled(cfg):
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=('Plate calibration is off: set vision.local_empty_enabled to true '
                                        'first, then run `/print-plate-calibrate capture` with the plate empty.'))
            return {'action': 'skip', 'reason': 'plate calibration not enabled'}
        try:
            frame = await asyncio.to_thread(local_empty.capture_frame, cfg)
            pending = await asyncio.to_thread(local_empty.begin_calibration, cfg, frame)
        except Exception as exc:
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=f'No plate reference captured: {exc}. Nothing changed.')
            return {'action': 'skip', 'reason': 'plate calibration capture failed'}
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=('This frame would become the empty plate reference the idle checks '
                                    'compare against.\nMEDIA:' + str(pending)))
        try:
            timeout = _config.safety_confirm_timeout_s()
        except Exception:
            timeout = 600
        live_key = gateway._session_key_for_source(source)
        answer = await _confirm(adapter, source, metadata, 'plate-calibrate',
                                'Make this frame your empty plate reference?',
                                ['Make this my empty plate', 'Retake'], timeout,
                                live_key=live_key)
        if answer == 'Make this my empty plate':
            stored = await asyncio.to_thread(local_empty.confirm_calibration,
                                             cfg, frame, force=True)
            if stored:
                _state, detail = local_empty.reference_status(cfg)
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content=f'Empty plate reference saved. {detail}')
                return {'action': 'skip', 'reason': 'plate reference captured'}
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=('The staged frame changed under the prompt, so nothing was '
                                        'stored. Run `/print-plate-calibrate capture` again.'))
            return {'action': 'skip', 'reason': 'plate reference capture refused'}
        await asyncio.to_thread(local_empty.discard_calibration, cfg)
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=('Reference not changed. `/print-plate-calibrate capture` '
                                    'takes another frame.'))
        return {'action': 'skip', 'reason': 'plate calibration discarded'}
    if verb == 'status':
        state, detail = local_empty.reference_status(cfg)
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=f'Plate reference: {state}: {detail}')
        return {'action': 'skip', 'reason': 'plate calibration status shown'}
    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                       content=('Use `/print-plate-calibrate capture` or '
                                '`/print-plate-calibrate status`. Nothing changed.'))
    return {'action': 'skip', 'reason': 'plate calibration usage shown'}


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/print-plate-calibrate', True)
handle = plate_calibrate_dispatch
handle.dispatch_gate = match
