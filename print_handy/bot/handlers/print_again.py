"""The print again chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio, json, time
from pathlib import Path

from .common import (
    _announce_command_start, _channel_printer_id, _confirm, _control_gate, _control_preamble,
    _failure_post, _live_key_for, _offer_force_restart, _pkw, _plain_print_number, _printer_label,
    _routed_form, _routed_printer_id, _seen, _start_refused_needs_restart, _undecided_printer_ids,
    intake_plain_start_failure,
)
from ...runtime import _config

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy_print_feedback")


def _job_owner_pid(job_json):
    """The roster printer a JOB.json belongs to: its recorded printer_id, else the default printer."""
    try:
        from print_handy.core import model_job
        return model_job._job_printer_id(json.loads(Path(job_json).read_text()))
    except Exception:
        return None


def _reprint_queue_printer(source, gateway, channel_pid, pc):
    """(printer id whose reprint queue this channel reads, refusal text or None).

    A decided channel reads its own printer's queue. A shared channel
    decides no printer (channel_pid is None), but the writer files copies
    under the job owner's queue, so next/stop look for the candidate
    printer that holds pending copies: one match is the queue, none falls
    back to the legacy default file, several are ambiguous and refused
    (2.2.5 review).
    """
    if channel_pid is not None:
        return channel_pid, None
    candidates = _undecided_printer_ids(source, gateway) or []
    holding = [pid for pid in candidates if pc.pending_copy(printer_id=pid)]
    if len(holding) > 1:
        names = ', '.join(_printer_label(pid) for pid in holding)
        return None, (f'Copies are queued on more than one printer here ({names}). '
                      'Use a printer-specific channel for /print-again next or stop. Nothing changed.')
    return (holding[0] if holding else None), None


async def print_again_dispatch(event, gateway, **kwargs):
    """/print-again [list | N | name]: an archived printed job from the start, behind one bed-clear button."""
    text = str(getattr(event, 'text', '')).strip()
    plain_number = _plain_print_number(text)
    if plain_number is not None:
        # 'print 57' is exactly '/print-again 57' (#436): same registry,
        # same confirmation, same unknown-number reply — never the LLM and
        # never the generic help text.
        source, adapter = _control_gate(event, gateway)
    else:
        source, adapter = _control_preamble(event, gateway, '/print-again', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'print again UI delivery unavailable'}
    from print_handy.runtime import print_control as pc
    from print_handy.runtime import print_next
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    pid = _routed_printer_id(source, gateway)
    # Ownership compares against the channel's actual printer (P1-77): the
    # routed form's None for the default printer must not skip these checks
    # in the default printer's own channel.
    channel_pid = _channel_printer_id(source, gateway)
    query = str(plain_number) if plain_number is not None else text[len('/print-again'):].strip()
    if query.casefold() in ('list', 'ls', 'history'):
        rows = await asyncio.to_thread(pc.printed_jobs)
        if channel_pid is not None:
            # A routed channel lists only its own printer's prints (#335).
            rows = [row for row in rows if _job_owner_pid(row['job_json']) == channel_pid]
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=pc.printed_list_text(rows))
        return {'action': 'skip', 'reason': 'print again list handled'}
    queue_pid, queue_refusal = channel_pid, None
    if query.casefold() in ('stop', 'cancel', 'clear', 'next', 'continue'):
        queue_pid, queue_refusal = await asyncio.to_thread(_reprint_queue_printer, source, gateway, channel_pid, pc)
        if queue_refusal:
            await adapter.send(chat_id=source.chat_id, metadata=metadata, content=queue_refusal)
            return {'action': 'skip', 'reason': 'print again queue ambiguous across printers'}
    if query.casefold() in ('stop', 'cancel', 'clear'):
        if channel_pid is not None:
            # Queues are per printer (P2-135): this channel reads and drops
            # only its own printer's queue.
            pending = await asyncio.to_thread(pc.pending_copy, printer_id=channel_pid)
            pending_owner = _job_owner_pid(pending['job_json']) if pending else None
            if pending_owner is not None and pending_owner != channel_pid:
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content=f"The queued copies belong to {_printer_label(_job_owner_pid(pending['job_json']))}, "
                                           "not this channel's printer. Nothing dropped.")
                return {'action': 'skip', 'reason': 'print again queue belongs to another printer'}
        had = await asyncio.to_thread(pc.cancel_copies, printer_id=queue_pid)
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='Remaining copies dropped. The current print is untouched.' if had
                           else 'No copies were queued.')
        return {'action': 'skip', 'reason': 'print again queue cleared'}
    key = ('print-again', source.chat_id, source.thread_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content='A print again prompt is already open. Nothing started.')
        return {'action': 'skip', 'reason': 'print again prompt already active'}
    _seen[key] = time.monotonic()
    scope_targets = None
    try:
        is_next = query.casefold() in ('next', 'continue')
        if is_next:
            target = await asyncio.to_thread(pc.next_copy_target, printer_id=queue_pid)
            question = pc.next_copy_question(target)
        else:
            query, copies = pc.split_count(query)
            if not query and channel_pid is not None:
                # Bare /print-again asks for this channel's printer's own
                # last print (#979): resolve inside that printer's launches
                # instead of resolving globally and refusing afterwards when
                # another printer printed more recently. Named and numbered
                # queries keep the exact global resolution and the owner
                # refusal below, unchanged.
                target = await asyncio.to_thread(pc.resolve_last_print, printer_id=channel_pid)
            else:
                target = await asyncio.to_thread(pc.resolve_print, query)
            target['copies'] = copies
            question = pc.again_question(target)
        target_owner = _job_owner_pid(target['job_json'])
        if channel_pid is not None and target_owner is not None and target_owner != channel_pid:
            # The channel is the target (#335): never offer another printer's
            # job for a bed-clear confirmation in this channel. An owner that
            # cannot be read from the record proves no mismatch; resolve_print
            # built the target from that same archive, and the start path
            # re-reads and validates the record before anything moves.
            raise print_next.NextPlateError(
                f"that print belongs to {_printer_label(target_owner)}, "
                f"not this channel's printer ({_printer_label(channel_pid)})")
        live_key = gateway._session_key_for_source(source)
        if not pid:
            candidates = _undecided_printer_ids(source, gateway)
            if candidates:
                # Shared channel (#536): the job record names its printer.
                _owner = _job_owner_pid(target['job_json'])
                pid = _routed_form(_owner)
                live_key = _live_key_for(source, gateway, _owner)
        if not is_next and copies == 1 and pc.parse_plate_ref(query) is None:
            # A project plate picked by number or title asks its scope first
            # (#1008): never silently reprint one plate of a multi-plate
            # project, and never guess the whole project either. An exact
            # plate alias (82A) already named its scope, so it skips this.
            info = await asyncio.to_thread(pc.target_project_info, target)
            if info is not None:
                has_remaining = any(p['completed_quantity'] < p['quantity'] for p in info['plates'])
                picked = await _confirm(
                    adapter, source, metadata, 'print-again-scope',
                    pc.scope_question(target, info),
                    list(pc.projects.scope_choices(True, has_remaining)),
                    (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800),
                    live_key=live_key)
                if picked == 'Choose a plate':
                    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                       content=pc.project_plate_list_text(info))
                    return {'action': 'skip', 'reason': 'print again project plate list shown'}
                if picked in ('Entire project', 'Remaining unprinted plates'):
                    scope_targets = await asyncio.to_thread(
                        pc.project_scope_targets, target,
                        'entire' if picked == 'Entire project' else 'remaining')
                    target = scope_targets[0]
                    question = pc.again_question(target)
                elif picked != 'This plate only':
                    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                       content='Cancelled or expired. Nothing uploaded or started.')
                    return {'action': 'skip', 'reason': 'print again scope cancelled'}
        answer = await _confirm(adapter, source, metadata, 'print-again', question,
                                [pc.AGAIN_LABEL, pc.AGAIN_CANCEL_LABEL], (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800),
                                live_key=live_key, approval=True)
        if answer != pc.AGAIN_LABEL:
            message = 'Cancelled or expired. Nothing uploaded or started.'
        else:
            from print_handy.runtime import discord_print_summary as print_summary
            gcode_seconds, filament_g = print_summary.gcode_metadata(target['gcode'])
            # target['index'] is the print being repeated (the row the
            # operator picked); the reprint mints its own new number at start
            # and the start announcement shows it.
            previous = target.get('index')
            if not (isinstance(previous, int) and not isinstance(previous, bool) and previous > 0):
                previous = pc.latest_number_for_job(target['job_json'])
            estimate = pc.start_estimate_text(target, gcode_seconds, filament_g)
            estimate = estimate[:1].upper() + estimate[1:]  # its own sentence now
            number_line = (f"A reprint of print #{previous}; it gets its own new print number when it starts. "
                           if previous else 'A reprint; it gets its own new print number when it starts. ')
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=f"Preparing to start {target['title']}. {number_line}{estimate}.")
            # A reprint of a project plate is a new logical run (#1008
            # acceptance item 5): name it before the start so the start
            # record carries the new run id and the original plan's counters
            # never move. Bookkeeping only; the approval above is this
            # start's own and is not reused.
            await asyncio.to_thread(pc.arm_reprint_run, target)
            ok, detail = await print_next.start(target)
            if not ok:
                await asyncio.to_thread(pc.disarm_reprint_run, target)
            if ok:
                # Queue bookkeeping lands in the job owner's own queue file,
                # never another printer's (P2-135).
                queue_owner = target_owner if target_owner is not None else channel_pid
                if is_next:
                    await asyncio.to_thread(pc.mark_copy_started, printer_id=queue_owner)
                elif scope_targets:
                    await asyncio.to_thread(pc.queue_project_sequence, scope_targets,
                                            printer_id=queue_owner)
                else:
                    await asyncio.to_thread(pc.queue_copies, target, printer_id=queue_owner)
                # The start claimed its announcement in model_job, so the
                # listener will not post one; post and register it here
                # (the listener edits this post after warmup) (P2-56).
                await _announce_command_start(
                    lambda content: adapter.send(chat_id=source.chat_id, content=content, metadata=metadata),
                    target, detail,
                    chat_id=str(source.chat_id), thread_id=str(source.thread_id) if source.thread_id else None)
            message = (None if ok
                       else f"Did not start {target['title']}. Not retrying.\n"
                            + intake_plain_start_failure(detail))
            if not ok and _start_refused_needs_restart(detail):
                message += '\n' + await _offer_force_restart(adapter, source, metadata, pc, detail,
                                                             live_key=live_key,
                                                             **_pkw(pid))
    except print_next.NextPlateError as exc:
        message = f'Nothing to print again: {exc}. Nothing uploaded or started.'
    except Exception as exc:
        logger.exception('print-again failed')
        message = _failure_post('Print again', exc, 'Check the printer screen before retrying.', subsystem='printer')
    finally:
        _seen.pop(key, None)
    if message:
        await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic print again handled'}


# --- handler interface (#1323) ---
from ._gates import free_text  # noqa: E402

match = free_text
handle = print_again_dispatch
