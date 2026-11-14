"""The calibration chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio, time

from .common import _confirm, _control_preamble, _decide_printer, _live_key_for, _seen
from ...runtime import _config

# Per-module logger (#1404): records name the handler module that
# made them, so [logging] levels rules can target one handler.
logger = logging.getLogger(__name__)


def _calibration_records_path(pid, chosen):
    """The decided printer's calibration records file, or None when unresolvable.

    Records are bookkeeping (the live_dispatch rule): any failure here
    yields None and the flows run unrecorded rather than failing.
    """
    try:
        from print_handy import settings
        roster_id = chosen or pid or settings.default_printer_id(_config.cfg())
        # The literal name, built through the per-printer state dir like
        # every other state file (must match calibration.RECORDS_NAME).
        return settings.printer_state_dir(roster_id) / "calibration_records.json"
    except Exception:
        return None


async def calibration_dispatch(event, gateway, **kwargs):
    """/print-calibration [mesh|screws|pid ...|shaper|belts|zoffset|records] (issue #565).

    The pattern is the product: every driven calibration reads and shows
    the BEFORE values, confirms with its warnings, runs idle-only
    (print_stats checked at prepare and again at send), polls for the
    result, presents the NEW values and the signed differences, and asks
    Accept / Discard before exactly the documented save is sent once —
    Discard sends the documented revert or nothing, never a save. The
    per-calibration primitives and verdicts are the Stage 0 evidence on
    #565 (print_handy.calibration's docstring): PID uses raw PID_CALIBRATE
    (never ZMod's auto-saving PID_TUNE macros), the mesh is measured with
    SAVE=0 / in memory only, the shaper only recommends until SAVE_CONFIG,
    Z offset is report + guide in every ZMod mode, ZMod bed screws are a
    guide (the macro waits for its own confirmation at the printer),
    generic-Klipper screws are a measure/adjust/re-measure loop, and
    belts are a report-only run. A record is written for every finished
    run (accepted / discarded / failed) and every report or guide.
    """
    source, adapter = _control_preamble(event, gateway, '/print-calibration', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'calibration UI delivery unavailable'}
    from print_handy import calibration, printers
    from print_handy.runtime import spool_sync
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    decided = await _decide_printer(source, gateway, adapter, metadata,
                                    question='Calibration on which printer?')
    if decided is None:
        return {'action': 'skip', 'reason': 'shared channel: no printer chosen for calibration'}
    pid, chosen = decided
    mr = spool_sync.moonraker_for(pid)
    args = str(getattr(event, 'text', '')).strip()[len('/print-calibration'):].split()
    entry, connection = None, None
    try:
        if pid:
            from print_handy import settings
            model_key = settings.by_id(pid, _config.cfg())["model"]
            connection = printers.get(model_key,
                                      settings.view_for(pid, _config.cfg())).connection
        else:
            current = printers.current()
            model_key = current.model.key
            connection = current.connection
        entry = calibration.printer_entry(model_key)
    except Exception:
        logger.exception('/print-calibration could not resolve the configured printer')
    label = entry.label if entry is not None else 'your printer'
    records_path = _calibration_records_path(pid, chosen)
    timeout = (globals().get('_config').safety_confirm_timeout_s()
               if globals().get('_config') else 600)
    live_key = _live_key_for(source, gateway, chosen)

    async def send(content):
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=content)

    def record(**kwargs):
        if records_path is None:
            return
        try:
            calibration.append_record(
                records_path, calibration.make_record(printer=label, **kwargs))
        except Exception:
            pass  # the record is bookkeeping; the flow's answer still stands

    verb = args[0].casefold() if args else ''
    if verb == 'records':
        records = calibration.read_records(records_path) if records_path else []
        await send(calibration.records_text(records, label))
        return {'action': 'skip', 'reason': 'print calibration records shown'}
    if connection != 'moonraker':
        await send(calibration.not_moonraker_text(connection))
        return {'action': 'skip', 'reason': 'print calibration not applicable to this printer'}
    block = calibration.block_reason(entry)
    if verb in ('', 'mesh', 'screws', 'pid', 'shaper', 'belts', 'zoffset') \
            and block is not None:
        await send(block + ' Nothing was sent to the printer.')
        return {'action': 'skip', 'reason': 'print calibration refused for this printer'}

    facts = None
    if verb in ('', 'mesh', 'screws', 'shaper', 'belts', 'zoffset'):
        try:
            facts = await asyncio.to_thread(calibration.fetch_facts, mr)
        except Exception:
            logger.exception('/print-calibration could not read the printer')
            await send(calibration.COULD_NOT_READ_TEXT)
            return {'action': 'skip', 'reason': 'print calibration handled'}

    if verb == '':
        await send(calibration.menu_text(entry, calibration.available_options(entry, facts)))
        return {'action': 'skip', 'reason': 'print calibration menu shown'}

    if verb == 'zoffset':
        try:
            message = await asyncio.to_thread(calibration.zoffset_report, mr, entry, facts)
        except Exception:
            logger.exception('/print-calibration zoffset failed')
            message = calibration.COULD_NOT_READ_TEXT
        else:
            record(calibration='zoffset', outcome='reported')
        await send(message)
        return {'action': 'skip', 'reason': 'print calibration zoffset shown'}

    async def gated_flow(*, record_name, prepare, prompt_text, execute, result_text,
                         accept, discard, before_of, after_of, differences_of, detail=''):
        """One accept-gated run: prepare -> confirm -> execute -> present -> accept/discard."""
        key = ('print-calibration', source.chat_id, source.thread_id)
        if key in _seen:
            await send('A calibration prompt is already open here. Nothing sent.')
            return {'action': 'skip', 'reason': 'calibration prompt already active'}
        _seen[key] = time.monotonic()
        try:
            try:
                prepared = await asyncio.to_thread(prepare)
            except (calibration.CalibrationRefusal, calibration.CalibrationError) as exc:
                await send(str(exc))
                return {'action': 'skip', 'reason': 'print calibration refused'}
            answer = await _confirm(adapter, source, metadata, 'print-calibration',
                                    prompt_text(prepared),
                                    [calibration.RUN_LABEL, calibration.CANCEL_LABEL],
                                    timeout, live_key=live_key)
            if answer != calibration.RUN_LABEL:
                await send('Calibration not run. Nothing sent.')
                return {'action': 'skip', 'reason': 'print calibration cancelled'}
            await send('Running — this can take several minutes. Print Handy will '
                       'post the result here when the printer reports it.')
            try:
                result = await asyncio.to_thread(execute, prepared)
            except (calibration.CalibrationRefusal, calibration.CalibrationError) as exc:
                record(calibration=record_name, outcome='failed', detail=detail)
                await send(str(exc))
                return {'action': 'skip', 'reason': 'print calibration failed'}
            answer = await _confirm(adapter, source, metadata, 'print-calibration',
                                    result_text(result),
                                    [calibration.ACCEPT_LABEL, calibration.DISCARD_LABEL],
                                    timeout, live_key=live_key)
            if answer == calibration.ACCEPT_LABEL:
                action, outcome = accept, 'accepted'
            elif answer == calibration.DISCARD_LABEL:
                action, outcome = discard, 'discarded'
            else:
                # No answer is not a Discard: a discard can restart the
                # firmware, and a print may have started in the meantime.
                record(calibration=record_name, outcome='unanswered',
                       before=before_of(result), after=after_of(result),
                       differences=differences_of(result), detail=detail)
                await send(calibration.UNANSWERED_TEXT)
                return {'action': 'skip', 'reason': 'print calibration result unanswered'}
            try:
                message = await asyncio.to_thread(action, result)
            except (calibration.CalibrationRefusal, calibration.CalibrationError) as exc:
                record(calibration=record_name, outcome='failed',
                       before=before_of(result), after=after_of(result),
                       differences=differences_of(result), detail=detail)
                await send(str(exc))
                return {'action': 'skip', 'reason': 'print calibration failed'}
            record(calibration=record_name, outcome=outcome,
                   before=before_of(result), after=after_of(result),
                   differences=differences_of(result), detail=detail)
            await send(message)
            return {'action': 'skip', 'reason': 'print calibration handled'}
        except Exception:
            logger.exception('/print-calibration flow failed')
            await send('The calibration stopped on an error. If a run was under way, '
                       'check the printer before trying again.')
            return {'action': 'skip', 'reason': 'print calibration failed'}
        finally:
            _seen.pop(key, None)

    if verb == 'mesh':
        option = calibration.option_for(calibration.available_options(entry, facts), 'mesh')
        if option is None:
            await send('Live discovery found no bed-mesh calibration Print Handy can '
                       'run or report on this printer (no ZMod AUTO_FULL_BED_LEVEL, '
                       'no [bed_mesh] configuration). Nothing was sent to the printer.')
            return {'action': 'skip', 'reason': 'print calibration mesh not offered'}
        if option.mode == 'report':
            await send(calibration.mesh_report_text(entry, facts))
            record(calibration='mesh', outcome='reported')
            return {'action': 'skip', 'reason': 'print calibration mesh reported'}
        return await gated_flow(
            record_name='mesh',
            prepare=lambda: calibration.prepare_mesh(mr, entry, facts),
            prompt_text=calibration.mesh_prompt_text,
            execute=lambda prepared: calibration.execute_mesh(mr, prepared),
            result_text=calibration.mesh_result_text,
            accept=lambda result: calibration.accept_mesh(mr, result),
            discard=lambda result: calibration.discard_mesh(mr, result),
            before_of=lambda result: result.before.as_dict() if result.before else None,
            after_of=lambda result: result.after.as_dict(),
            differences_of=lambda result: calibration.mesh_differences(result.before,
                                                                       result.after))

    if verb == 'shaper':
        option = calibration.option_for(calibration.available_options(entry, facts), 'shaper')
        if option is None:
            await send('Live discovery found no input-shaper calibration on this '
                       'printer (no ZMod ZSHAPER macro, no resonance tester '
                       'configured). Nothing was sent to the printer.')
            return {'action': 'skip', 'reason': 'print calibration shaper not offered'}
        return await gated_flow(
            record_name='shaper',
            prepare=lambda: calibration.prepare_shaper(mr, entry, facts),
            prompt_text=calibration.shaper_prompt_text,
            execute=lambda prepared: calibration.execute_shaper(mr, prepared),
            result_text=calibration.shaper_result_text,
            accept=lambda result: calibration.accept_shaper(mr, result),
            discard=lambda result: calibration.discard_shaper(mr, result),
            before_of=lambda result: result.before.as_dict(),
            after_of=lambda result: result.after.as_dict(),
            differences_of=lambda result: calibration.shaper_differences(result.before,
                                                                         result.after))

    if verb == 'pid':
        heater, target = None, None
        rest = args[1:]
        if rest and rest[0].casefold() in ('extruder', 'bed'):
            heater = 'extruder' if rest[0].casefold() == 'extruder' else 'heater_bed'
            rest = rest[1:]
        if rest:
            target = rest[0]
        if heater is None:
            answer = await _confirm(adapter, source, metadata, 'print-calibration',
                                    'PID calibration for which heater?',
                                    ['Extruder PID', 'Bed PID'], timeout, live_key=live_key)
            if answer == 'Extruder PID':
                heater = 'extruder'
            elif answer == 'Bed PID':
                heater = 'heater_bed'
            else:
                await send('No heater chosen. Nothing sent.')
                return {'action': 'skip', 'reason': 'print calibration pid cancelled'}
        if target is None:
            targets = calibration.HEATER_TARGETS[heater]
            labels = [f'{t} °C' + (' (default)' if t == targets[0] else '')
                      for t in targets]
            answer = await _confirm(
                adapter, source, metadata, 'print-calibration',
                f'Calibrate the {calibration.HEATER_LABELS[heater].lower()} PID at '
                'which temperature? (Calibrate at the temperature you print at.)',
                labels, timeout, live_key=live_key)
            if answer not in labels:
                await send('No temperature chosen. Nothing sent.')
                return {'action': 'skip', 'reason': 'print calibration pid cancelled'}
            target = targets[labels.index(answer)]
        return await gated_flow(
            record_name='pid_extruder' if heater == 'extruder' else 'pid_bed',
            prepare=lambda: calibration.prepare_pid(mr, entry, heater, target),
            prompt_text=calibration.pid_prompt_text,
            execute=lambda prepared: calibration.execute_pid(mr, prepared),
            result_text=calibration.pid_result_text,
            accept=lambda result: calibration.accept_pid(mr, result),
            discard=lambda result: calibration.discard_pid(mr, result),
            before_of=lambda result: result.before.as_dict(),
            after_of=lambda result: result.after.as_dict(),
            differences_of=lambda result: calibration.pid_differences(result.before,
                                                                      result.after),
            detail=f'target {target} °C')

    if verb == 'screws':
        option = calibration.option_for(calibration.available_options(entry, facts), 'screws')
        if option is None:
            await send('Live discovery found no bed-screw calibration on this printer '
                       '(no ZMod BED_LEVEL_SCREWS_TUNE, no [screws_tilt_adjust] or '
                       '[bed_screws] configuration). Nothing was sent to the printer.')
            return {'action': 'skip', 'reason': 'print calibration screws not offered'}
        if option.mode == 'guide':
            guide = calibration.SCREWS_ZMOD_GUIDE \
                if entry is not None and entry.firmware_family == 'zmod' \
                else calibration.SCREWS_KLIPPER_GUIDE
            await send(guide)
            record(calibration='screws', outcome='reported', detail='guide shown')
            return {'action': 'skip', 'reason': 'print calibration screws guide shown'}
        key = ('print-calibration', source.chat_id, source.thread_id)
        if key in _seen:
            await send('A calibration prompt is already open here. Nothing sent.')
            return {'action': 'skip', 'reason': 'calibration prompt already active'}
        _seen[key] = time.monotonic()
        try:
            try:
                await asyncio.to_thread(calibration.prepare_screws, mr, entry, facts)
            except (calibration.CalibrationRefusal, calibration.CalibrationError) as exc:
                await send(str(exc))
                return {'action': 'skip', 'reason': 'print calibration refused'}
            answer = await _confirm(adapter, source, metadata, 'print-calibration',
                                    calibration.screws_prompt_text(),
                                    [calibration.RUN_LABEL, calibration.CANCEL_LABEL],
                                    timeout, live_key=live_key)
            if answer != calibration.RUN_LABEL:
                await send('Screw measurement not run. Nothing sent.')
                return {'action': 'skip', 'reason': 'print calibration cancelled'}
            first = last = None
            rounds = 0
            while True:
                try:
                    reading = await asyncio.to_thread(calibration.measure_screws, mr)
                except (calibration.CalibrationRefusal,
                        calibration.CalibrationError) as exc:
                    record(calibration='screws', outcome='failed',
                           before=first.as_dict() if first else None)
                    await send(str(exc))
                    return {'action': 'skip', 'reason': 'print calibration failed'}
                if first is None:
                    first = reading
                text = calibration.screws_text(reading, last)
                last = reading
                rounds += 1
                if rounds >= 3:
                    record(calibration='screws', outcome='accepted',
                           before=first.as_dict(), after=last.as_dict(),
                           differences=calibration.screws_differences(first, last))
                    await send(text + '\n\nThree measurements is the sensible limit '
                               'for one session — the record of this session was '
                               'written (`/print-calibration records`). If the bed '
                               'is still out, check for a mechanical problem (a loose '
                               'screw or a warped plate) rather than more rounds.')
                    return {'action': 'skip', 'reason': 'print calibration handled'}
                answer = await _confirm(adapter, source, metadata, 'print-calibration',
                                        text, [calibration.MEASURE_AGAIN_LABEL,
                                               calibration.FINISH_LABEL],
                                        timeout, live_key=live_key)
                if answer != calibration.MEASURE_AGAIN_LABEL:
                    record(calibration='screws', outcome='accepted',
                           before=first.as_dict(), after=last.as_dict(),
                           differences=calibration.screws_differences(first, last))
                    await send('Bed screw session finished — the adjustments you made '
                               'are physical, so there is nothing to save and nothing '
                               'was sent. The session record was written '
                               '(`/print-calibration records`). If the bed moved a '
                               'lot, refresh the bed mesh before the next print.')
                    return {'action': 'skip', 'reason': 'print calibration handled'}
        except Exception:
            logger.exception('/print-calibration screws failed')
            await send('The screw measurement stopped on an error. Nothing was saved '
                       '(screw adjustments are physical).')
            return {'action': 'skip', 'reason': 'print calibration failed'}
        finally:
            _seen.pop(key, None)

    if verb == 'belts':
        option = calibration.option_for(calibration.available_options(entry, facts), 'belts')
        if option is None:
            await send('Live discovery found no belt check on this printer (ZMod\'s '
                       'BELTS_SHAPER_CALIBRATION macro is not defined). Nothing was '
                       'sent to the printer.')
            return {'action': 'skip', 'reason': 'print calibration belts not offered'}
        key = ('print-calibration', source.chat_id, source.thread_id)
        if key in _seen:
            await send('A calibration prompt is already open here. Nothing sent.')
            return {'action': 'skip', 'reason': 'calibration prompt already active'}
        _seen[key] = time.monotonic()
        try:
            try:
                prepared = await asyncio.to_thread(calibration.prepare_belts, mr, entry, facts)
            except (calibration.CalibrationRefusal, calibration.CalibrationError) as exc:
                await send(str(exc))
                return {'action': 'skip', 'reason': 'print calibration refused'}
            answer = await _confirm(adapter, source, metadata, 'print-calibration',
                                    calibration.belts_prompt_text(),
                                    [calibration.RUN_LABEL, calibration.CANCEL_LABEL],
                                    timeout, live_key=live_key)
            if answer != calibration.RUN_LABEL:
                await send('Belt check not run. Nothing sent.')
                return {'action': 'skip', 'reason': 'print calibration cancelled'}
            await send('Running the belt check — this can take several minutes. '
                       'Print Handy will post what the printer reports.')
            try:
                message = await asyncio.to_thread(calibration.execute_belts, mr, prepared)
            except (calibration.CalibrationRefusal, calibration.CalibrationError) as exc:
                record(calibration='belts', outcome='failed')
                await send(str(exc))
                return {'action': 'skip', 'reason': 'print calibration failed'}
            record(calibration='belts', outcome='reported')
            await send(message)
            return {'action': 'skip', 'reason': 'print calibration handled'}
        except Exception:
            logger.exception('/print-calibration belts failed')
            await send('The belt check stopped on an error. Nothing was applied or '
                       'saved either way.')
            return {'action': 'skip', 'reason': 'print calibration failed'}
        finally:
            _seen.pop(key, None)

    await send('Use `/print-calibration` for the menu of what this printer offers, or '
               '`/print-calibration mesh`, `screws`, `pid extruder|bed [temp]`, '
               '`shaper`, `belts`, `zoffset` or `records`. Nothing was sent to '
               'the printer.')
    return {'action': 'skip', 'reason': 'print calibration usage shown'}


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/print-calibration', True)
handle = calibration_dispatch
handle.dispatch_gate = match
