"""The troubleshoot chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import time

from .common import _confirm, _control_preamble, _seen
from ...runtime import _config

# Per-module logger (#1404): records name the handler module that
# made them, so [logging] levels rules can target one handler.
logger = logging.getLogger(__name__)


TROUBLESHOOT_STOP_LABEL = 'Stop here'


async def troubleshoot_dispatch(event, gateway, **kwargs):
    """/print-troubleshoot [flow]: a guided physical troubleshooting walkthrough (issue #169).

    Step-by-step advice with the flow's safety warnings first. Questions are the
    bot's own buttons; answers only move the walkthrough forward. Nothing is sent
    to the printer at any step: no heat, no motion, no extrusion, no macro. Where
    a fix is an action (a cold pull), the flow points at the existing guarded
    command, which asks for its own confirmation separately.
    """
    source, adapter = _control_preamble(event, gateway, '/print-troubleshoot', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'troubleshoot UI delivery unavailable'}
    from print_handy import knowledge
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    arg = str(getattr(event, 'text', '')).strip()[len('/print-troubleshoot'):].strip().casefold()
    registry = knowledge.default_registry()
    flow = registry.flow(arg) if arg else None
    if flow is None and arg:
        for match in registry.lookup(arg, limit=5):
            if match.kind == 'flow':
                flow = match.item
                break
            if match.kind == 'symptom' and match.item.flow:
                flow = registry.flow(match.item.flow)
                break
    if flow is None:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='Which problem?\n' + knowledge.flows_text(registry)
                           + '\nYou can also describe the symptom, e.g. `/print-troubleshoot extruder clicking`.')
        return {'action': 'skip', 'reason': 'troubleshoot usage shown'}
    key = ('troubleshoot', source.chat_id, source.thread_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='A troubleshooting walkthrough is already open here. Finish or stop that one first.')
        return {'action': 'skip', 'reason': 'troubleshoot already active'}
    _seen[key] = time.monotonic()
    completed = False
    try:
        session = knowledge.FlowSession(flow)
        timeout = (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600)
        while True:
            step = session.step()
            if step.done:
                completed = True
                break
            if step.safety:
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content=f"**{flow.title}** — safety first:\n"
                                           + '\n'.join(f'- {line}' for line in step.safety))
            if step.kind == 'advice':
                await adapter.send(chat_id=source.chat_id, metadata=metadata, content=step.text)
                if session.advance().done:
                    completed = True
                    break
                continue
            answer = await _confirm(adapter, source, metadata, 'print-troubleshoot', step.text,
                                    [*step.choices, TROUBLESHOOT_STOP_LABEL], timeout,
                                    live_key=gateway._session_key_for_source(source))
            if answer is None or answer == TROUBLESHOOT_STOP_LABEL:
                break
            session.answer(answer)
    except Exception:
        logger.exception('/print-troubleshoot failed')
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='Troubleshooting stopped on an error. Nothing was sent to the printer.')
        return {'action': 'skip', 'reason': 'troubleshoot failed'}
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                       content=(f'{flow.title}: walkthrough complete. ' if completed
                                else 'Troubleshooting stopped. ') + knowledge.ADVISORY_NOTE)
    return {'action': 'skip', 'reason': 'troubleshoot handled'}


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/print-troubleshoot', True)
handle = troubleshoot_dispatch
handle.dispatch_gate = match
