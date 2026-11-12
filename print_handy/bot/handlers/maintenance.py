"""The maintenance chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio, time

from .common import (
    _cold_pull_temps, _coldpull_workflow, _confirm, _control_preamble, _decide_printer,
    _live_key_for, _maintenance_hours_now, _pkw, _routed_printer_id, _seen,
)
from ...runtime import _config

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy.bot.handlers")


async def maintenance_dispatch(event, gateway, **kwargs):
    """Guided preventive maintenance checklist using the shared cold-pull path."""
    source, adapter = _control_preamble(event, gateway, '/print-maintenance', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'maintenance UI delivery unavailable'}
    import json as _json
    import uuid
    from print_handy.runtime import maintenance
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    pid = _routed_printer_id(source, gateway)
    mode = str(getattr(event, 'text', '')).strip()[len('/print-maintenance'):].strip().casefold() or 'full'
    if adapter is None:
        return {'action': 'skip', 'reason': 'maintenance UI delivery unavailable'}
    if mode not in {'full', 'quick'}:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='Use `/print-maintenance full` or `/print-maintenance quick`. '
                                   'To get ready to clean the nozzle or bed, use `/print-bed-down`.')
        return {'action': 'skip', 'reason': 'maintenance usage shown'}
    key = ('maintenance', source.chat_id, source.thread_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content='A maintenance checklist is already open.')
        return {'action': 'skip', 'reason': 'maintenance already active'}
    _seen[key] = time.monotonic()
    try:
        from print_handy.runtime import print_control as pc
        decided = await _decide_printer(source, gateway, adapter, metadata,
                                        question='Maintenance on which printer?')
        if decided is None:
            return {'action': 'skip', 'reason': 'shared channel: no printer chosen for maintenance'}
        pid, chosen = decided
        live_key = _live_key_for(source, gateway, chosen)
        st = await asyncio.to_thread(pc.status, **_pkw(pid))
        if st.get('state') in {'printing', 'paused'}:
            message = f"Maintenance is unavailable while the printer is {st['state']}. Nothing changed."
        else:
            state = maintenance.load(**_pkw(pid))
            session = state.get('pending')
            count = 4 if mode == 'full' else 2
            if not isinstance(session, dict) or session.get('kind') != mode:
                session = maintenance.begin_session(mode, state, **_pkw(pid))
            instructions = [
                'Extruder gears: open the tension lever, brush out filament dust, and check the last filament end for a ground notch.',
                '4-in-1 connector and slot tubes: pull them and clear any broken filament.',
                'IFS pinch rollers: clear shavings from each roller shaft and confirm the lever springs are seated.',
                'Spools: check for tangles, crossed wraps, free ends, and note old or brittle spools.',
            ][:count]
            session['steps'] = session.get('steps', [None] * count)
            session['problems'] = session.get('problems', [])
            buttons = ['Done', 'Skip', 'Problem found']
            for i, instruction in enumerate(instructions):
                if i < len(session['steps']) and session['steps'][i] is not None:
                    continue
                answer = await _confirm(adapter, source, metadata, f'maintenance-{i}',
                                        f'{instruction} Choose an outcome.', buttons, (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800),
                                        live_key=live_key)
                action = 'problem' if answer == 'Problem found' else 'done' if answer == 'Done' else 'skip'
                session['steps'][i] = action
                if action == 'problem':
                    session['problems'].append(instruction)
                maintenance.save_session(session, state, **_pkw(pid))
            if mode == 'full':
                cold_pull_runs = [r for r in maintenance.load(**_pkw(pid)).get('runs', []) if r.get('cold_pull')]
                last_pull = cold_pull_runs[-1].get('cold_pull', {}) if cold_pull_runs else {}
                pla_hot, pla_cold, petg_hot, petg_cold, retry_cold = _cold_pull_temps()
                options = [f'Run PLA (trial) cold pull {pla_hot}/{pla_cold}',
                           f'Run PETG cold pull {petg_hot}/{petg_cold}', 'Skip cold pull']
                if last_pull.get('next_cold') == retry_cold:
                    options[0] = f'Run PLA (trial) cold pull {pla_hot}/{retry_cold}'
                answer = await _confirm(adapter, source, metadata, 'maintenance-coldpull',
                    'Cold pull requires you at the printer. Bed must be clear; it homes. Screen may look frozen for a few minutes.', options, (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800),
                    live_key=live_key)
                if answer and answer != 'Skip cold pull':
                    cold = retry_cold if answer.endswith(f'{pla_hot}/{retry_cold}') else None
                    record, _cold_message = await _coldpull_workflow(
                        adapter, source, metadata, source_tag='maintenance', initial_cold=cold,
                        selected_material=answer, live_key=live_key,
                        **_pkw(pid))
                    session['cold_pull'] = record.get('cold_pull') if record else {'result': 'cancelled'}
                    if session.get('cold_pull'):
                        session['cold_pull']['source'] = 'maintenance'
                    if record:
                        session['cold_pull_record'] = record
                        # The shared workflow already persisted the physical pull.
                        # Maintenance completion must not append a duplicate record.
                        session['cold_pull_record_persisted'] = True
            session['completed'] = True
            session['hours_total'] = await (asyncio.to_thread(_maintenance_hours_now, pid) if pid else asyncio.to_thread(_maintenance_hours_now))
            cold_pull_record = session.pop('cold_pull_record', None)
            cold_pull_persisted = session.pop('cold_pull_record_persisted', False)
            if cold_pull_record is not None and not cold_pull_persisted:
                # Defensive: the shared cold-pull workflow persists its own
                # record before returning it, so this only fires if that
                # ever changes; never let the pull go unrecorded.
                maintenance.append_run(cold_pull_record, **_pkw(pid))
            # The completed session is a run of its own kind and must be
            # saved even when a cold pull ran: status and reminder lookups
            # search for kind == mode, and skipping this append made a full
            # maintenance with a cold pull look like it never happened.
            run = session
            if cold_pull_persisted:
                # The pull's own run already carries the cold_pull record, and
                # the dirty-pull counter reads every run with one: keep only
                # its result here so each pull counts once (2.2.5 review).
                run = {**session, 'cold_pull': None,
                       'cold_pull_result': (session.get('cold_pull') or {}).get('result')}
            maintenance.append_run(run, **_pkw(pid))
            # Reload before clearing the pending session: `state` was loaded
            # before the appends above, so saving it as-is would overwrite
            # the file with its stale runs list and erase them again.
            state = maintenance.load(**_pkw(pid))
            state.pop('pending', None)
            maintenance.save(state, **_pkw(pid))
            message = f'{mode.title()} maintenance checklist saved. '
            if session.get('cold_pull', {}).get('result') == 'failed':
                message += f" Cold pull failed: {session['cold_pull'].get('detail', 'check printer')}"
            elif session.get('cold_pull', {}).get('result') not in (None, 'cancelled', 'awaiting_tip'):
                message += f" Cold pull recorded: {session['cold_pull']['result']}."
            elif session.get('cold_pull', {}).get('result') == 'awaiting_tip':
                message += ' Cold pull completed; tip result is awaiting operator input.'
            else:
                message += ' No printer action was used.'
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    except Exception as exc:
        logger.exception('maintenance workflow failed')
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content='The maintenance checklist stopped early. No further printer action was sent — check the printer screen before retrying.')
    finally:
        _seen.pop(key, None)
    return {'action': 'skip', 'reason': 'maintenance handled'}


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/print-maintenance', True)
handle = maintenance_dispatch
handle.dispatch_gate = match
