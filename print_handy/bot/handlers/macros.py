"""The macros chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio, time

from .common import _confirm, _control_preamble, _decide_printer, _live_key_for, _seen
from ...runtime import _config

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy.bot.handlers")


async def macros_dispatch(event, gateway, **kwargs):
    """/print-macros [run <name> [KEY=VALUE ...]]: the configured Moonraker printer's macros (issue #291).

    Bare form (stage 1) lists what the printer actually defines: one GET of
    Moonraker's /printer/objects/list through the spool client, classified
    against the print_handy/data/firmware-commands/ registry. The run form (stage 2) re-discovers that
    set at run time and runs only a name from it — never a typed name on
    trust, never free-typed G-code, never an internal (_-prefixed) macro —
    behind the usual _confirm button, with the printer's print state checked
    before the prompt and again immediately before the single
    /printer/gcode/script POST. Printers whose connection is not Moonraker
    have no macro namespace and get a plain explanation instead of an error.
    """
    source, adapter = _control_preamble(event, gateway, '/print-macros', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'macros UI delivery unavailable'}
    from print_handy import macros, printers
    from print_handy.runtime import spool_sync
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    decided = await _decide_printer(source, gateway, adapter, metadata,
                                    question='Macros on which printer?')
    if decided is None:
        return {'action': 'skip', 'reason': 'shared channel: no printer chosen for macros'}
    pid, chosen = decided
    live_key = _live_key_for(source, gateway, chosen)
    mr = spool_sync.moonraker_for(pid)
    args = str(getattr(event, 'text', '')).strip()[len('/print-macros'):].split()
    model_key = None
    printer_adapter = None  # the chosen printer's adapter: its heater ceiling guards macro arguments (#1147)
    try:
        if pid:
            from print_handy import settings
            model_key = settings.by_id(pid, _config.cfg())["model"]
            printer_adapter = printers.get(model_key, settings.view_for(pid, _config.cfg()))
            connection = printer_adapter.connection
        else:
            printer_adapter = printers.current()
            connection = printer_adapter.connection
            model_key = getattr(printer_adapter, "key", None)
    except Exception:
        logger.exception('/print-macros could not resolve the configured printer')
        connection = None
        model_key = None
    if connection != 'moonraker':
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=macros.not_moonraker_text(connection))
        return {'action': 'skip', 'reason': 'print macros not applicable to this printer'}
    if not args:
        try:
            listing = await asyncio.to_thread(macros.discover, mr, printer=model_key)
            message = macros.listing_text(listing)
        except Exception:
            logger.exception('/print-macros failed')
            message = macros.COULD_NOT_READ_TEXT
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
        return {'action': 'skip', 'reason': 'print macros handled'}
    if args[0].casefold() != 'run' or len(args) < 2:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=('Use `/print-macros` to list the macros your printer defines, or '
                                    '`/print-macros run <name> [KEY=VALUE ...]` to run one of them '
                                    '(it asks first, and refuses while a print is running). '
                                    'Nothing was sent to the printer.'))
        return {'action': 'skip', 'reason': 'print macros usage shown'}
    key = ('print-macros-run', source.chat_id, source.thread_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='A macro run prompt is already open here. Nothing sent.')
        return {'action': 'skip', 'reason': 'macro run prompt already active'}
    _seen[key] = time.monotonic()
    try:
        try:
            prepared = await asyncio.to_thread(macros.prepare_run, mr,
                                               args[1], args[2:], printer=model_key,
                                               heater_adapter=printer_adapter)
        except (macros.MacroRefusal, macros.MacroError) as exc:
            message = str(exc)
        else:
            answer = await _confirm(adapter, source, metadata, 'print-macros',
                                    macros.run_prompt_text(prepared),
                                    [macros.RUN_LABEL, macros.CANCEL_LABEL],
                                    (globals().get('_config').safety_confirm_timeout_s()
                                     if globals().get('_config') else 600),
                                    live_key=live_key)
            if answer != macros.RUN_LABEL:
                message = 'Macro not run. Nothing sent.'
            else:
                try:
                    message = await asyncio.to_thread(macros.send_run, mr, prepared)
                except (macros.MacroRefusal, macros.MacroError) as exc:
                    message = str(exc)
    except Exception:
        logger.exception('/print-macros run failed')
        message = 'The macro run stopped on an error. No confirmation was completed, so nothing was sent.'
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'print macros run handled'}


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/print-macros', True)
handle = macros_dispatch
handle.dispatch_gate = match
