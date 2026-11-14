"""The model link chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging

from .common import (
    _EditHost, _INTAKE_UNAVAILABLE_REASON, _adapter_for, _ask_which_printer, _busy_label,
    _candidate_statuses, _channel_printer_id, _is_bot_platform, _is_hosting_profile, _live_key_for,
    _pkw, _plain_print_number, _recent_model_link, _routed_form, _routed_printer_id, _state_dir,
    _undecided_printer_ids,
)

# Per-module logger (#1404): records name the handler module that
# made them, so [logging] levels rules can target one handler.
logger = logging.getLogger(__name__)


def _is_affirmative_followup(text: str) -> bool:
    if not text:
        return False
    import re
    t = text.strip().lower()
    # "don't print it", "do not do it", "did you print it?", "why did it
    # print it": negations and questions about what happened are not a
    # go-ahead, so they never pull in the remembered link (#979 review).
    from print_handy.core.prepared_intake import _REPRINT_NEGATION_RE, _REPRINT_QUESTION_RE
    if _REPRINT_NEGATION_RE.search(t) or _REPRINT_QUESTION_RE.match(t):
        return False
    patterns = [
        r"\b(let's|lets)\s+(do\s+it|print\s+(it|this|that))\b",
        r"\bprint\s+(it|this|that|now)\b",
        r"\bgo\s+ahead\b",
        r"\bdo\s+it\b",
        r"\bstart\s+(it|this|printing)\b",
        r"\byes,?\s+(please\s+)?(print|do\s+it)\b",
        r"\b(sounds|looks)\s+good,?\s*(print|do\s+it)?\b",
        r"\byep,?\s*(print|do\s+it)?\b",
    ]
    return any(re.search(p, t) for p in patterns)


async def model_link_dispatch(event, gateway, **kwargs):
    source = getattr(event, 'source', None)
    if (source is None or not _is_bot_platform(source)
            or not _is_hosting_profile(getattr(source, 'profile', None))
            or getattr(event, 'internal', False) or getattr(source, 'is_bot', False)
            or getattr(source, 'profile_route_rejected', False)):
        return None
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    from print_handy.core.link_intake import model_link
    from print_handy.core.prepared_intake import (prepare_link as collect_link, model_file,
                                                  reprint_request, last_printed_job)
    from print_handy.core import prepared_intake as _intake_module
    _model_files = getattr(_intake_module, 'model_files', lambda event: [])
    _archive_files = getattr(_intake_module, 'archive_files', lambda event: [])

    def model_files(event):
        # A dropped .zip of model sources is an upload too (#1204 item 3).
        return _model_files(event) or _archive_files(event)
    from print_handy.core import yolo as _yolo
    text = getattr(event, 'text', '')
    reuse = None
    followup_link = False
    # An already-sliced G-code drop prints only as an explicit yolo (core/yolo.py).
    gcode_drop = None if model_link(text) else _yolo.gcode_file(event)
    if not (model_link(text) or (model_file(event) or model_files(event)) or gcode_drop):
        if reprint_request(text) and _plain_print_number(text) is not None:
            # "reprint 57" / "print #57 again" names an exact print: that is
            # print_again_dispatch's numbered reprint (#436), never "the last
            # print" (#979 review). This hook runs first, so it steps aside.
            return None
        if reprint_request(text):
            # "Again" wins over "it" (#979): a reprint phrase refers to the
            # print history, never to the channel's remembered link, so it is
            # resolved before the affirmative-followup prepend below — and
            # from launch records, the same source /print-again uses, scoped
            # to this channel's printer when the channel decides one (the
            # printer filter is also the dedicated channel's owner check: a
            # job owned by another printer is never offered here).
            # "print the previous one in black" lands here too: selection is
            # by launch, execution still re-slices from the saved source.
            from print_handy.core.prepared_intake import LastPrintNotReusable
            refusal = None
            try:
                reuse = last_printed_job(printer_id=_channel_printer_id(source, gateway))
            except LastPrintNotReusable as exc:
                # The newest print cannot be re-sliced (its source file is
                # gone). Never fall back to an older model (#979: ask or
                # refuse, do not guess): name it and point at the exact
                # G-code reprint instead.
                reuse = None
                refusal = (f'The last print, {exc.title}, cannot be prepared again: {exc.reason}. '
                           'Use /print-again to reprint its exact G-code. Nothing uploaded or started.',
                           'last print not re-sliceable')
            if reuse is None:
                adapter = _adapter_for(gateway, source)
                if adapter is None:
                    return {'action': 'skip', 'reason': 'reprint refusal delivery unavailable'}
                content, reason = refusal or (
                    'Nothing to print again: no archived print has been started yet. '
                    'Nothing uploaded or started.', 'reprint requested with no printed job')
                await adapter.send(
                    chat_id=source.chat_id, content=content,
                    metadata={'thread_id': source.thread_id} if source.thread_id else None)
                return {'action': 'skip', 'reason': reason}
        elif _is_affirmative_followup(text):
            recent_url = _recent_model_link(source)
            if recent_url:
                text = f"{recent_url} {text}"
                setattr(event, 'text', text)
                followup_link = True
        if reuse is None and not (model_link(text) or (model_file(event) or model_files(event))):
            return None
    linked = model_link(text)
    if linked and not followup_link:
        # Only a link present in the user's own message is (re-)stored. A
        # link the dispatcher itself prepended for a follow-up must not
        # refresh the stored row's TTL, or repeated follow-ups would keep a
        # link "fresh" forever (#979). Stored rows carry provenance: who
        # posted the link, and the printer this channel routes to when known.
        from print_handy.bot import recent_links
        recent_links.remember(source.chat_id, linked, getattr(source, 'thread_id', None),
                              user_id=getattr(source, 'user_id', None),
                              printer_id=_channel_printer_id(source, gateway))
    event.metadata['fresh_model_intake_handled'] = True
    event.metadata['live_session_key'] = gateway._session_key_for_source(source)
    adapter = _adapter_for(gateway, source)
    if adapter is None:
        return {'action': 'skip', 'reason': 'new model color UI delivery unavailable'}
    # No separate receipt before the slow download/inventory: the preview
    # and color picker are the acknowledgement.
    from print_handy.core import printer_state
    from print_handy.core.link_intake import PromptCancelled
    root = _state_dir() / 'model-link-intakes'
    pid = _routed_printer_id(source, gateway)
    candidates = _undecided_printer_ids(source, gateway)
    if candidates:
        # A shared channel decides nothing (#536): ask BEFORE any printer
        # state loads — pid=None here has always meant the default printer,
        # and falling through would silently print on it. A reprint reuses
        # an archived job, and the job already knows its printer.
        chosen = None
        if reuse is not None:
            try:
                from print_handy.core import model_job
                chosen = model_job._job_printer_id(reuse)
            except Exception:
                chosen = None
            if chosen not in candidates:
                chosen = None
        if chosen is None:
            from print_handy.runtime import print_control as _print_control
            statuses = await _candidate_statuses(candidates, _print_control.status)

            def _picker_label(candidate, _statuses=statuses):
                return _busy_label(_statuses.get(candidate))

            chosen = await _ask_which_printer(source, gateway, adapter, candidates,
                                              question='Which printer should I print this on?',
                                              label_for=_picker_label,
                                              metadata={'thread_id': source.thread_id}
                                              if source.thread_id else None)
        if chosen is None:
            await adapter.send(chat_id=source.chat_id,
                content='No printer chosen, so nothing was sliced or sent anywhere.',
                metadata={'thread_id': source.thread_id} if source.thread_id else None)
            return {'action': 'skip', 'reason': 'shared channel: no printer chosen for intake'}
        pid = _routed_form(chosen)
        try:
            source.printer_id = chosen
        except Exception:
            pass
        # The intake's questions register under a printer-scoped session key
        # so a second intake in this channel can never take their answers.
        event.metadata['live_session_key'] = _live_key_for(source, gateway, chosen)
    try:
        # The intake channel's printer owns the job end to end (#335): its
        # state record feeds the flow and its id is stamped on the job.
        # A link or file plus an instruction in one message (#903): an edit
        # applies inside the intake, after the job is prepared and before any
        # slice is armed or a start is offered; a question is answered after.
        if gcode_drop is not None:
            from print_handy.core.prepared_intake import print_gcode_file
            return await print_gcode_file(event, adapter, root, gcode_drop, **_pkw(pid))
        # "yolo" is a mode, not an edit or a question about the model.
        instruction = None if reuse is not None else _model_instruction(_yolo.strip(text), linked)
        edit_hook = _instruction_edit_hook(event, gateway, source, adapter, instruction)
        result = await collect_link(event, adapter, printer_state.load(**_pkw(pid)), root,
                                    reuse=reuse, **_pkw(pid),
                                    **({'before_start': edit_hook} if edit_hook else {}),
                                    **({'confirm_target': True} if followup_link else {}))
    except PromptCancelled:
        await adapter.send(chat_id=source.chat_id,
            content='Cancelled. Nothing sliced, nothing uploaded, nothing started. '
                    'The colors and job are saved if you want to resume.',
            metadata={'thread_id': source.thread_id} if source.thread_id else None)
        return {'action': 'skip', 'reason': 'operator cancelled intake'}
    except Exception:
        logger.exception('New model intake failed; original event must not reach stale agent context')
        try:
            await adapter.send(chat_id=source.chat_id,
                content='New model color intake failed. No previous job was resumed and nothing started.',
                metadata={'thread_id': source.thread_id} if source.thread_id else None)
        except Exception:
            logger.exception('New model intake failure delivery also failed')
        return {'action': 'skip', 'reason': _INTAKE_UNAVAILABLE_REASON}
    # A question beside the model is answered from the job the intake just
    # prepared; edits were applied inside the intake (edit_hook above).
    if instruction and edit_hook is None:
        from print_handy.bot import edit_chat
        try:
            follow = await edit_chat.follow_up(event, gateway, source, adapter, instruction,
                                               host=_EditHost())
        except Exception:
            logger.exception('instruction beside the model failed')
            follow = None
        if isinstance(follow, dict):
            return follow
    return result


def _instruction_edit_hook(event, gateway, source, adapter, instruction):
    """The intake hook that applies an edit carried beside a model link or
    file before the start choice exists, or None for no edit (#903)."""
    if not instruction:
        return None
    from print_handy.core import model_questions
    if model_questions.classify(instruction) is not None:
        return None
    from print_handy.bot import edit_chat
    return edit_chat.before_start_hook(event, gateway, source, adapter, instruction,
                                       host=_EditHost())


def _model_instruction(text, url):
    from print_handy.bot import edit_chat
    try:
        return edit_chat.instruction_after_model(text, url)
    except Exception:
        return None


# --- handler interface (#1323) ---
from ._gates import free_text  # noqa: E402

match = free_text
handle = model_link_dispatch
