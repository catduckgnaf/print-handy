"""The nozzle chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio, time

from .common import _confirm, _control_preamble, _decide_printer, _live_key_for, _pkw, _seen
from ...hermes import _config

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy_print_feedback")


_NOZZLE_TYPE_LABELS = {'brass': 'Brass', 'hardened_steel': 'Hardened steel',
                       'stainless_steel': 'Stainless steel', 'tungsten_carbide': 'Tungsten carbide',
                       'diamond': 'Diamond'}


async def _nozzle_change_workflow(adapter, source, metadata, *, printer_id=None, live_key=None):
    """The guided /nozzle change walk (#513): select -> confirmed heat -> step
    confirmations -> cooldown -> record -> calibration offers.

    Heating goes only through nozzle_change's Moonraker path after its own
    explicit confirmation; any other connection gets manual-heat
    instructions and the same step confirmations. Cancelling after heat
    turns the heater off and records nothing; a UI failure after heat
    attempts the same heater-off before the failure surfaces.
    """
    from print_handy.hermes import nozzle_change as nc
    from print_handy.hermes import print_control as pc

    question_timeout = (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800)
    safety_timeout = (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600)

    # Capabilities first (P2-195): the prompts offer only what this printer
    # supports, and the selection is validated against the same sets before
    # any heating or manual-change instruction — finish_change used to be
    # the first check, after the physical swap and cooldown.
    try:
        connection = pc._adapter_for(printer_id).connection
        supported_sizes = pc._adapter_for(printer_id).supported_nozzle_sizes
        supported_types = pc._adapter_for(printer_id).supported_nozzle_types
    except Exception:
        connection, supported_sizes, supported_types = None, None, None
    diameters = [d for d in nc.GUIDED_DIAMETERS_MM
                 if supported_sizes is None
                 or any(abs(float(s) - d) < 0.01 for s in supported_sizes)]
    types = [t for t in nc.GUIDED_TYPES if supported_types is None or t in supported_types]
    if not diameters or not types:
        return ('This printer reports nozzle capabilities the guided walk cannot offer '
                '(supported diameters: '
                f"{', '.join(f'{s:g}' for s in supported_sizes or ()) or 'unknown'}; "
                'supported types: '
                f"{', '.join(supported_types or ()) or 'unknown'}). "
                'Nothing sent and nothing changed.')

    dia_answer = await _confirm(adapter, source, metadata, 'nozzle-diameter',
        'Which diameter is the replacement nozzle?',
        [f'{d:g} mm' for d in diameters] + ['Cancel'], question_timeout, live_key=live_key)
    dia = next((d for d in nc.GUIDED_DIAMETERS_MM if str(dia_answer).strip().casefold() == f'{d:g} mm'), None)
    if dia is None:
        return 'Cancelled or expired. Nothing sent and nothing changed.'
    type_answer = await _confirm(adapter, source, metadata, 'nozzle-type',
        'Which material is the replacement nozzle?',
        [_NOZZLE_TYPE_LABELS[t] for t in types] + ['Cancel'], question_timeout, live_key=live_key)
    nozzle_type = next((t for t in nc.GUIDED_TYPES
                        if str(type_answer).strip().casefold() in (_NOZZLE_TYPE_LABELS[t].casefold(), t)), None)
    if nozzle_type is None:
        return 'Cancelled or expired. Nothing sent and nothing changed.'

    try:
        # A typed answer can still name a guided option the filtered
        # buttons did not offer; refuse it with the record path's own rule.
        from print_handy.core import nozzle as _nozzle
        _nozzle.validate_nozzle_setting(dia, nozzle_type,
                                        supported_sizes=supported_sizes,
                                        supported_types=supported_types)
    except Exception as exc:
        return (f'That replacement cannot be used on this printer: {exc} '
                'Nothing was sent and nothing changed — the physical change was not started.')
    live = nc.supports_live_heat(connection)
    kw = {'printer_id': printer_id} if printer_id else {}

    if live:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
            content=(f'Changing to a {dia:g} mm {_NOZZLE_TYPE_LABELS[nozzle_type]} nozzle. '
                     'Be at the printer with the new nozzle and a torque tool ready. '
                     'Nothing heats until you confirm.'))
        confirmed = await _confirm(adapter, source, metadata, 'nozzle-heat',
            f'I am at the printer and authorize heating the hotend to {nc.LOOSEN_TEMP_C} °C for the nozzle change.',
            [f'Confirm heat to {nc.LOOSEN_TEMP_C} °C', 'Cancel'], safety_timeout, live_key=live_key)
        if confirmed != f'Confirm heat to {nc.LOOSEN_TEMP_C} °C':
            return 'Cancelled or expired. Nothing sent.'
        import time as _time
        ok, detail = await asyncio.to_thread(nc.heat_for_loosen, confirmed_at=_time.time(), **kw)
        if not ok:
            return detail
        # The hotend is now at the loosen temperature. Any failure in the UI
        # steps below (a chat send raising, a _confirm whose button delivery
        # fails) must not leave it heating: attempt heater-off in cleanup
        # that does not itself depend on chat delivery, then let the
        # original failure surface (P1-105).
        try:
            await adapter.send(chat_id=source.chat_id, metadata=metadata, content=detail)
            removed = await _confirm(adapter, source, metadata, 'nozzle-removed',
                'Remove the old nozzle hot, thread the new nozzle in by hand, then confirm.',
                [nc.REMOVED_LABEL, 'Cancel'], question_timeout, live_key=live_key)
            if removed != nc.REMOVED_LABEL:
                _off, off_detail = await asyncio.to_thread(nc.heater_off, **kw)
                return f'Cancelled. {off_detail}'
            torqued = await _confirm(adapter, source, metadata, 'nozzle-torque',
                f'Torque the new nozzle hot ({nc.TORQUE_HINT}), while the hotend stays at {nc.LOOSEN_TEMP_C} °C.',
                [nc.TORQUED_LABEL, 'Cancel'], question_timeout, live_key=live_key)
            if torqued != nc.TORQUED_LABEL:
                _off, off_detail = await asyncio.to_thread(nc.heater_off, **kw)
                return f'Cancelled. {off_detail}'
            ok, detail = await asyncio.to_thread(nc.cooldown, **kw)
            if not ok:
                return detail
            await adapter.send(chat_id=source.chat_id, metadata=metadata, content=detail)
        except BaseException:
            try:
                await asyncio.shield(asyncio.to_thread(nc.heater_off, **kw))
            except Exception:
                logger.exception('/nozzle change heater-off cleanup failed')
            raise
    else:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=nc.manual_heat_instructions())
        done = await _confirm(adapter, source, metadata, 'nozzle-manual',
            f'Confirm when the nozzle is changed, torqued hot, and the hotend has cooled below {nc.SAFE_TEMP_C} °C.',
            ['Changed and cooled — record it', 'Cancel'], question_timeout, live_key=live_key)
        if done != 'Changed and cooled — record it':
            return 'Cancelled. Nothing was recorded.'

    try:
        message = await asyncio.to_thread(nc.finish_change, {}, dia, nozzle_type,
                                          printer_id=printer_id,
                                          supported_sizes=supported_sizes,
                                          supported_types=supported_types)
    except Exception:
        logger.exception('/nozzle change record failed')
        return ('The physical change is done, but recording it in the printer state failed. '
                'Record it with `printer_state set --nozzle '
                f'{dia:g} --nozzle-type {nozzle_type}` before printing.')
    mesh = await _confirm(adapter, source, metadata, 'nozzle-mesh',
        'Run bed mesh leveling now? It homes and probes the bed; the saved mesh predates this nozzle.',
        ['Run Bed Mesh Leveling', 'Skip for now'], question_timeout, live_key=live_key)
    if mesh == 'Run Bed Mesh Leveling':
        if live:
            import time as _time
            _ok, mesh_detail = await asyncio.to_thread(nc.run_bed_mesh, confirmed_at=_time.time(), **kw)
            message += f'\n{mesh_detail}'
        else:
            message += ('\nBed mesh leveling must be run on the printer\'s own screen for this '
                        'printer — Print Handy cannot send it from here.')
    return message


async def nozzle_dispatch(event, gateway, **kwargs):
    """/nozzle change: the guided physical nozzle change (#513)."""
    source, adapter = _control_preamble(event, gateway, '/nozzle', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'nozzle change UI delivery unavailable'}
    text = str(getattr(event, 'text', '')).strip()
    verb = text[len('/nozzle'):].strip().casefold()
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    if verb not in ('', 'change'):
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='Usage: `/nozzle change` — the guided nozzle change. Nothing was sent.')
        return {'action': 'skip', 'reason': 'nozzle usage shown'}
    key = ('nozzle', source.chat_id, source.thread_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='A nozzle change is already in progress. Nothing sent.')
        return {'action': 'skip', 'reason': 'nozzle change already active'}
    _seen[key] = time.monotonic()
    try:
        decided = await _decide_printer(source, gateway, adapter, metadata,
                                        question='Nozzle change on which printer?')
        if decided is None:
            return {'action': 'skip', 'reason': 'shared channel: no printer chosen for nozzle change'}
        pid, chosen = decided
        message = await _nozzle_change_workflow(adapter, source, metadata,
                                                live_key=_live_key_for(source, gateway, chosen),
                                                **_pkw(pid))
    except Exception:
        logger.exception('/nozzle change failed')
        message = ('The nozzle change stopped early. If the hotend was heating, turn the heater off '
                   'on the printer screen and check the printer before retrying.')
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'nozzle change handled'}


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/nozzle', True)
handle = nozzle_dispatch
handle.dispatch_gate = match
