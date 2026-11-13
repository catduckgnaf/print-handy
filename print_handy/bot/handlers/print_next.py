"""The print next chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio, json, time
from pathlib import Path

from .common import (
    SPOOL_CHANGED, USE_LOADED_SPOOL, _adapter_for, _announce_command_start, _channel_printer_id,
    _confirm, _failure_post, _is_bot_platform, _is_hosting_profile, _live_key_for,
    _offer_force_restart, _pkw, _printer_label, _routed_form, _routed_printer_id, _seen,
    _spool_change_text, _start_refused_needs_restart, _undecided_printer_ids,
    intake_plain_start_failure,
)
from ...runtime import _config

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy.bot.handlers")


async def _offer_spool_change(adapter, source, metadata, print_next, target, detail, live_key=None):
    """A slot's spool was swapped after slicing (ran out, changed color). Ask once; on yes, start with it.

    Only the slice-time spool id is waived; material family and live slot checks still run.
    """
    import uuid
    from print_handy.core.prompts import gateway as _prompts
    cm = _prompts()
    question = _spool_change_text(target, detail)
    choices = [USE_LOADED_SPOOL, print_next.CANCEL_LABEL]
    cid = uuid.uuid4().hex
    session_key = f'print-next-spool:{source.chat_id}:{source.thread_id}'
    # Register under the live chat session key (the _confirm pattern):
    # typed replies route by that key, so a flow-key registration can
    # never be answered on buttonless platforms. send_clarify keeps
    # the flow key for delivery.
    cm.register(cid, live_key or session_key, question, choices)
    from print_handy.core import prompts as _prompt_marks
    _prompt_marks.mark_approval(cid)  # a yes starts the print: a print approval (P1-93)
    try:
        sent = await adapter.send_clarify(chat_id=str(source.chat_id), question=question, choices=choices,
                                          clarify_id=cid, session_key=session_key, metadata=metadata)
        if not getattr(sent, 'success', False):
            return False, detail
        answer = await _prompt_marks.await_answer(cid, (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800), registry=cm)
    finally:
        cm.resolve_gateway_clarify(cid, cm.CANCELLED)
        _prompt_marks.forget_approval(cid)
    if answer != USE_LOADED_SPOOL:
        return False, 'Cancelled. Nothing uploaded or started.'
    return await print_next.start({**target, 'accept_spool_change': True})


async def _start_with_offers(adapter, source, metadata, print_next, target, live_key=None):
    """One chat start that asks instead of hard-blocking on material warnings or a swapped spool (#1336).

    A refusal that names only acceptable material warnings (sliced vs loaded
    material, abrasive filament on a soft nozzle, a material the maker does not
    recommend, an AMS tray or slot holding another material) asks Reslice for
    the loaded material (the default) / Print as sliced / Cancel; Print as
    sliced restarts with that answer bound to this exact G-code. A slot spool
    swapped since slicing asks Use the loaded spool, as before. Returns
    (ok, detail, plain): plain is True when detail is already chat wording.
    """
    from print_handy.core import material_accept
    timeout = globals().get('_config').question_timeout_s() if globals().get('_config') else 1800
    target = dict(target)
    ok, detail = await print_next.start(target)
    for _round in range(3):
        if ok:
            break
        warnings = material_accept.pending(detail)
        if warnings:
            choices = material_accept.choices(warnings)
            answer = await _confirm(adapter, source, metadata, 'print-material', material_accept.question(warnings),
                                    choices, timeout, live_key=live_key, approval=True)
            if answer != material_accept.print_label(warnings):
                return False, (material_accept.reslice_message(warnings) if answer == choices[0]
                               else 'Cancelled. Nothing uploaded or started.'), True
            # A new dict per start: an earlier start's target is never changed after the fact.
            target = {**target, 'accept_material': sorted(set(target.get('accept_material') or ())
                                                          | {w['key'] for w in warnings})}
            ok, detail = await print_next.start(target)
            continue
        if SPOOL_CHANGED in str(detail) and not target.get('accept_spool_change'):
            ok, detail = await _offer_spool_change(adapter, source, metadata, print_next, target, detail,
                                                    live_key=live_key)
            if detail == 'Cancelled. Nothing uploaded or started.':
                return False, detail, True
            target = {**target, 'accept_spool_change': True}
            continue
        break
    return ok, detail, False


async def print_next_dispatch(event, gateway, **kwargs):
    """/print-next <plan_id> [part_id]: one bed-clear button for the exact next (or named) plate. One start per tap."""
    source = getattr(event, 'source', None)
    text = str(getattr(event, 'text', '')).strip()
    if not (text == '/print-next' or text.startswith('/print-next ')):
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
        return {'action': 'skip', 'reason': 'next plate UI delivery unavailable'}
    import uuid
    from print_handy.runtime import print_next
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    args = text[len('/print-next'):].split()
    plan_id = args[0] if args else ''
    part_id = args[1] if len(args) > 1 else None
    pid = _routed_printer_id(source, gateway)
    key = ('print-next', plan_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='A start prompt for this plan is already open. Nothing started.')
        return {'action': 'skip', 'reason': 'next plate prompt already active'}
    _seen[key] = time.monotonic()
    try:
        if not plan_id:
            raise print_next.NextPlateError('usage: /print-next <plan_id> [part_id]')
        plan = print_next.load_plan(plan_id)
        target = print_next.resolve_next(plan) if part_id is None else print_next.resolve_next(plan, part_id=part_id)
        channel_pid = _channel_printer_id(source, gateway)
        if channel_pid is not None:
            # The channel is the target (#335): a plan whose job belongs to
            # another printer is refused here, before any prompt is shown.
            # The comparison uses the channel's actual printer (P1-77): the
            # routed form normalizes the default printer to None, which used
            # to skip this check in the default printer's own channel.
            from print_handy.core import model_job as _mj
            _job = json.loads(Path(target['job_json']).read_text())
            _job_pid = _mj._job_printer_id(_job)
            if _job_pid != channel_pid:
                raise print_next.NextPlateError(
                    f"that plan belongs to {_printer_label(_job_pid)}, not this channel's printer "
                    f"({_printer_label(channel_pid)})")
        live_key = gateway._session_key_for_source(source)
        if not pid:
            candidates = _undecided_printer_ids(source, gateway)
            if candidates:
                # A shared channel decides nothing (#536), but the plan's
                # job already knows its printer: resolve from the job
                # record instead of asking — or worse, defaulting.
                from print_handy.core import model_job as _mj
                _job = json.loads(Path(target['job_json']).read_text())
                _owner = _mj._job_printer_id(_job)
                pid = _routed_form(_owner)
                live_key = _live_key_for(source, gateway, _owner)
        if part_id is None:
            # Several plates can go next: ask which, the same question on
            # every transport through the shared prompt layer (#1204 item 5),
            # instead of silently taking plan order where no Discord picker
            # exists. A pick is a choice, not an approval: the bed-clear
            # confirmation below is still asked for the picked plate.
            asked = print_next.plate_question(plan)
            if asked is not None:
                plate_q, plate_labels, plate_ids = asked
                picked = await _confirm(
                    adapter, source, metadata, 'print-next-plate', plate_q, plate_labels,
                    (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800),
                    live_key=live_key)
                if picked not in plate_labels[:-1]:
                    raise print_next.NextPlateError('no plate was picked (cancelled, Later or expired)')
                target = print_next.resolve_next(plan, part_id=plate_ids[plate_labels.index(picked)])
        from print_handy.runtime import discord_print_summary as print_summary
        job, plate = print_summary.find_job(target['gcode'])
        seconds = (plate or {}).get('seconds')
        # No number here: this plate gets its own new print number when it
        # starts, and the start announcement shows it.
        target['estimate_text'] = (f"Estimated print time ~{print_summary.human(seconds)}, "
                                   f"~{print_summary.human(seconds + print_summary.WARMUP_S)} total including warmup"
                                   if isinstance(seconds, (int, float)) else
                                   "Estimated print time unknown")
        from print_handy.core.prompts import gateway as _prompts
        cm = _prompts()
        choices = [print_next.START_LABEL, print_next.CANCEL_LABEL]
        question = print_next.question_text(target)
        cid = uuid.uuid4().hex
        session_key = f'print-next:{source.chat_id}:{source.thread_id}'
        # Register under the live chat session key (the _confirm pattern):
        # typed replies route by that key, so a flow-key registration can
        # never be answered on buttonless platforms. send_clarify keeps
        # the flow key for delivery.
        cm.register(cid, live_key or session_key, question, choices)
        from print_handy.core import prompts as _prompt_marks
        _prompt_marks.mark_approval(cid)  # the bed-clear button starts a print (P1-93)
        try:
            sent = await adapter.send_clarify(chat_id=str(source.chat_id), question=question,
                                              choices=choices, clarify_id=cid,
                                              session_key=session_key, metadata=metadata)
            if not getattr(sent, 'success', False):
                raise RuntimeError('button delivery failed')
            answer = await _prompt_marks.await_answer(cid, (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800), registry=cm)
        finally:
            cm.resolve_gateway_clarify(cid, cm.CANCELLED)
            _prompt_marks.forget_approval(cid)
        if answer != print_next.START_LABEL:
            message = 'Cancelled or expired. Nothing uploaded or started.'
        else:
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=f"Preparing to start {target['label']}. {target.get('estimate_text', 'Estimated print time unknown')}.")
            ok, detail, plain = await _start_with_offers(adapter, source, metadata, print_next, target,
                                                         live_key=live_key)
            if plain:
                await adapter.send(chat_id=source.chat_id, content=detail, metadata=metadata)
                return {'action': 'skip', 'reason': 'deterministic next plate handled'}
            if ok:
                await _announce_command_start(
                    lambda content: adapter.send(chat_id=source.chat_id, content=content, metadata=metadata),
                    target, detail, job=job, plate=plate,
                    chat_id=str(source.chat_id), thread_id=str(source.thread_id) if source.thread_id else None)
            message = (None if ok
                       else f"Did not start {target['label']}. Not retrying.\n"
                            + intake_plain_start_failure(detail))
            if not ok and _start_refused_needs_restart(detail):
                from print_handy.runtime import print_control as pc
                message += '\n' + await _offer_force_restart(adapter, source, metadata, pc, detail,
                                                             live_key=live_key,
                                                             **_pkw(pid))
    except print_next.NextPlateError as exc:
        message = f'No next plate: {exc}. Nothing uploaded or started.'
    except Exception as exc:
        logger.exception('print-next failed')
        message = _failure_post('Next plate', exc, 'Check the printer screen before retrying.', subsystem='printer')
    finally:
        _seen.pop(key, None)
    if message:  # None: start accepted, printer preparing; the listener posts "Print started".
        await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic next plate handled'}


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/print-next', True)
handle = print_next_dispatch
handle.dispatch_gate = match
