"""User-scoped conversational repairs on every chat transport (#1778).

An explicit /fix with a model upload starts a confined workspace. The same
user's follow-up messages in the same chat/thread operate on that workspace.
Acceptance re-enters the existing model intake, including normal slice and
confirmation gates. No synthetic acceptance bypasses those gates.
"""
import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

from .common import _adapter_for, _is_print_discord_event, _confirm
from .. import fix_session
from ... import ai_routes, settings

logger = logging.getLogger(__name__)


def _is_start(text):
    stripped = str(text or '').strip()
    return (stripped == '/fix' or stripped.startswith('/fix ')
            or stripped == fix_session.WORK_WITH_AI_LABEL
            or fix_session.is_fix_instruction(stripped))


def _source_file(event):
    from print_handy.core import prepared_intake
    choices = []
    explicit = prepared_intake.model_file(event)
    if explicit is not None:
        choices.append(Path(explicit))
    for path in prepared_intake.model_files(event):
        candidate = Path(path)
        if candidate not in choices:
            choices.append(candidate)
    return choices[0] if len(choices) == 1 and choices[0].is_file() else None


def _new_session(event):
    """Copy an explicitly supplied model into a private job workspace."""
    source = event.source
    selected = _source_file(event)
    if selected is None:
        return None
    if selected.suffix.lower() not in ('.stl', '.3mf'):
        return None
    from print_handy.core.intake_resources import check_source
    check_source(selected)
    root = Path(settings.current()['paths']['cache']) / 'fix-sessions'
    root.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix='ph-fix-', dir=root))
    try:
        target = workspace / 'source' / selected.name
        target.parent.mkdir()
        shutil.copyfile(selected, target)
        job_path = workspace / 'JOB.json'
        job_path.write_text(json.dumps({
            'downloaded_file': str(target.resolve()),
            'selected_source': {'type': 'upload', 'path': str(target.resolve())},
        }), encoding='utf-8')
        return fix_session.start_session(
            str(source.user_id), str(source.chat_id),
            getattr(source, 'thread_id', None), job_path, target)
    except BaseException:
        shutil.rmtree(workspace, ignore_errors=True)
        raise


def _complete(session, message, tools):
    """Ask the configured heavy tier for an exact JSON tool plan.

    ai_routes is Print Handy's sole model transport; its current interface
    returns text, so tool calls are parsed and verified locally by Session.
    """
    from print_handy.skills import print_repair
    relative = session.current_file.relative_to(session.skill_session.workspace).as_posix()
    prompt = (print_repair.load().system_prompt()
              + '\nReturn ONLY a JSON object with keys "content" (string) and '
                '"tool_calls" (array of {"name": string, "arguments": object}). '
                'Call only the listed tools. Do not claim changes unless a tool '
                'succeeds. Never start a print.\n'
              + 'Allowed tools: ' + json.dumps(tools) + '\n'
              + 'Current mesh path: ' + json.dumps(relative))
    messages = [{'role': 'system', 'content': prompt}]
    for item in session.history[-12:]:
        if item.get('role') in ('user', 'assistant'):
            messages.append({'role': item['role'], 'content': str(item.get('content') or '')[:3000]})
    messages.append({'role': 'user', 'content': message})
    answer, _served_by = ai_routes.call('heavy', messages, max_tokens=900)
    return json.loads(answer)


async def _send(adapter, source, text):
    return await adapter.send(chat_id=source.chat_id, content=text,
                              metadata={'thread_id': source.thread_id}
                              if getattr(source, 'thread_id', None) else None)


async def _accept(event, gateway, adapter, session):
    """Verified normal intake BEFORE committing accepted/repointed state.

    The session lock is held from here until the job is repointed, so a repair
    turn cannot change current_file between intake and acceptance.
    """
    source = event.source
    if not session.lock.acquire(blocking=False):
        await _send(adapter, source, 'A repair step is still running. Type accept again '
                    'when it finishes. Nothing was accepted.')
        return {'action': 'skip', 'reason': 'repair step still running'}
    try:
        return await _accept_locked(event, gateway, adapter, session)
    finally:
        session.lock.release()


async def _accept_locked(event, gateway, adapter, session):
    source = event.source
    if not session.active():
        await _send(adapter, source, 'This repair session already ended. Nothing was changed.')
        return {'action': 'skip', 'reason': 'repair session ended'}
    if fix_session._same_path(session.current_file, session.original_file):
        await _send(adapter, source, 'There is no repaired copy to accept. The original is unchanged.')
        return {'action': 'skip', 'reason': 'no repaired file to accept'}
    from print_handy.core.intake_resources import check_source
    try:
        await asyncio.to_thread(check_source, session.current_file)
        model_link_dispatch = globals().get('model_link_dispatch')
        if model_link_dispatch is None:
            from print_handy.bot.handlers.model_link import model_link_dispatch
        synthetic = SimpleNamespace(
            source=source, text='', media_urls=[str(session.current_file)],
            internal=False, message_id=f'fix-intake-{session.started_at}',
            metadata={})
        result = await model_link_dispatch(synthetic, gateway)
        reason = result.get('reason') if isinstance(result, dict) else None
        if reason not in ('sliced and start handled by button flow',
                          'operator did not start the sliced plate'):
            await _send(adapter, source, 'Normal intake did not finish verifying the repaired file. '
                        'The fix session stays open; nothing was accepted.')
            return {'action': 'skip', 'reason': 'repair intake incomplete'}
        output = fix_session.process_turn(
            session, 'accept', ai_complete=lambda *_: None,
            resume_intake=lambda _session: True, locked=True)
    except Exception:
        logger.exception('normal intake failed during fix acceptance')
        await _send(adapter, source, 'Normal intake failed. The repair was not accepted; '
                    'the original job is unchanged.')
        return {'action': 'skip', 'reason': 'repair intake failed'}
    await _send(adapter, source, output['reply'])
    return {'action': 'skip', 'reason': 'repair accepted' if output['accepted']
            else 'repair acceptance refused'}


async def offer_failed_intake(event, gateway, adapter):
    """Offer a scoped, explicit repair action after a failed uploaded-model slice.

    Only the requesting user's uploaded file is eligible: a recent unrelated
    archived job is never treated as the source of a repair. This uses the
    same adapter confirmation UI on Discord, Telegram, Matrix and Web chat.
    """
    if not ai_routes.ready('heavy') or _source_file(event) is None:
        return None
    source = event.source
    metadata = {'thread_id': source.thread_id} if getattr(source, 'thread_id', None) else None
    live_key = (gateway._session_key_for_source(source)
                if hasattr(gateway, '_session_key_for_source') else None)
    choice = await _confirm(
        adapter, source, metadata, 'fix-problem-file',
        'That model could not be sliced. Work on a repaired copy with AI?',
        [fix_session.WORK_WITH_AI_LABEL, 'Not now'], 600,
        **({'live_key': live_key} if live_key is not None else {}))
    if choice != fix_session.WORK_WITH_AI_LABEL:
        return None
    try:
        session = await asyncio.to_thread(_new_session, event)
    except Exception:
        logger.exception('cannot start repair for rejected intake model')
        session = None
    if session is None:
        await _send(adapter, source, 'The original upload is no longer available. '
                    'Reattach the STL or 3MF with /fix to start a repair. Nothing changed.')
        return {'action': 'skip', 'reason': 'repair original unavailable'}
    await _send(adapter, source, fix_session.SESSION_START
                + ' Describe what needs fixing; accept or cancel when ready.')
    return {'action': 'skip', 'reason': 'repair session started after slice refusal'}


async def fix_dispatch(event, gateway, **_kwargs):
    if not _is_print_discord_event(event):
        return None
    source = event.source
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    text = str(getattr(event, 'text', '') or '').strip()
    session = fix_session.get_session(str(source.user_id), str(source.chat_id),
                                      getattr(source, 'thread_id', None))
    starting = _is_start(text)
    if session is None and not starting:
        return None
    # Commands for other features and unrelated new model uploads are always
    # left to their existing handlers, even while this conversation is open.
    if session is not None and text.startswith('/') and not text.startswith('/fix'):
        return None
    if session is not None and not starting and _source_file(event) is not None:
        return None
    adapter = _adapter_for(gateway, source)
    if adapter is None:
        return None
    if session is None:
        if not ai_routes.ready('heavy'):
            await _send(adapter, source, 'The repair AI is not configured. Nothing was changed.')
            return {'action': 'skip', 'reason': 'repair AI unavailable'}
        try:
            session = await asyncio.to_thread(_new_session, event)
        except Exception:
            logger.exception('repair intake source could not be prepared')
            await _send(adapter, source, 'That file could not be prepared for repair. '
                        'Nothing was changed or printed.')
            return {'action': 'skip', 'reason': 'repair setup failed'}
        if session is None:
            await _send(adapter, source, 'Attach one STL or 3MF file with /fix to start '
                        'a private repair session.')
            return {'action': 'skip', 'reason': 'repair needs a model'}
        await _send(adapter, source, fix_session.SESSION_START
                    + ' Describe the repair, then type accept or cancel.')
        return {'action': 'skip', 'reason': 'repair session started'}
    if text.lower() in fix_session._ACCEPT:
        return await _accept(event, gateway, adapter, session)
    if text.lower() in fix_session._CANCEL:
        output = await asyncio.to_thread(
            fix_session.process_turn, session, text, ai_complete=lambda *_: None)
    elif starting and text == '/fix':
        await _send(adapter, source, 'A repair session is open. Describe the change, '
                    'or type accept or cancel.')
        return {'action': 'skip', 'reason': 'repair session status'}
    else:
        output = await asyncio.to_thread(
            fix_session.process_turn, session, text, ai_complete=_complete)
    await _send(adapter, source, output['reply'])
    return {'action': 'skip', 'reason': 'repair session handled'}


# --- handler interface (#1323) ---
from ._gates import free_text

match = free_text
handle = fix_dispatch
