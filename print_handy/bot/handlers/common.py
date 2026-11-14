"""Handler helpers shared by more than one chat command: the gateway plumbing, prompts, printer
routing and state the handlers use.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio, contextlib, functools, json, re, time
from collections import OrderedDict
from pathlib import Path

from ...runtime import _config

# Per-module logger (#1404): records name the handler module that
# made them, so [logging] levels rules can target one handler.
logger = logging.getLogger(__name__)


def _state_home():
    return _config.state_home()


def _state_dir():
    return _config.state_dir()


# ---------------------------------------------------------------------------
# Hosting profile identity (#574)
#
# The bot stamps every source it builds with the fixed hosting profile
# (print_handy.bot.runtime.PROFILE, _config.DEFAULT_PROFILE); the gates
# below answer only sources carrying that stamp. The name used to be
# derived from the Hermes install layout; with that install gone the
# identity is the constant.
def _hosting_profile():
    """The profile identity this module answers for: the fixed hosting profile."""
    return _config.hosting_profile(Path(__file__))[0]


def _is_hosting_profile(value):
    """True when an event's profile value is the hosting profile's."""
    return bool(value) and value == _hosting_profile()


_seen = OrderedDict()
ACK_TIMEOUT = 1.5
SEEN_TTL = 3600
SEEN_LIMIT = 1024


def _failure_post(label, exc, note, *, subsystem):
    """One Discord failure post meeting the #390 bar, in #467 plain language.

    ``label`` says what was being done, in the person's words; ``note``
    says what state things are in and what to do next. The raw exception
    text is *not* posted (#467): it is classified for the stable greppable
    ``PH_<SUBSYSTEM>_<CATEGORY>`` marker that ends the post, and the full
    detail stays in the log and the diagnostics bundle. Built through the
    shared ``diagnostics.plain_post`` so every platform surface uses the
    one wording system.
    """
    from print_handy import diagnostics
    known = diagnostics.known_cause(exc)
    if known is not None:
        # #590: a recognised, operator-fixable cause is named with its
        # remedy instead of only the generic note and an UNKNOWN marker.
        return diagnostics.plain_post(
            f"{label} didn't work. {known.cause}", next_step=f"{note} {known.remedy}",
            subsystem=subsystem, exc=exc)
    return diagnostics.plain_post(
        f"{label} didn't work.", next_step=note,
        subsystem=subsystem, exc=exc)


# Platforms whose standalone sources these handlers serve (#164): Discord,
# Telegram (stage 2) and Matrix (stage 3), plus the Web UI chat (#1106),
# whose BotCore runs in the Web UI process on the same handlers. The gate
# was never really "is this Discord" but "is this our bot's print source";
# Telegram/Matrix sources exist when the operator runs the bot on that
# platform, and a webui source exists whenever the Web UI chat page is
# used. Platform-specific commands still gate themselves (the live
# stream command is Discord-only, below).
BOT_PLATFORMS = frozenset({'discord', 'telegram', 'matrix', 'webui'})


def _platform_value(source):
    return getattr(getattr(source, 'platform', None), 'value', None)


def _is_bot_platform(source):
    return _platform_value(source) in BOT_PLATFORMS


def _is_print_discord_event(event):
    source = getattr(event, 'source', None)
    return (source is not None
            and _is_bot_platform(source)
            and _is_hosting_profile(getattr(source, 'profile', None))
            and not getattr(event, 'internal', False)
            and not getattr(source, 'is_bot', False)
            and not getattr(source, 'profile_route_rejected', False))


def _routed_printer_id(source, gateway=None):
    """The roster printer this event's channel routes to (#335), or None.

    None means the configured single/default printer, and every handler then
    takes its legacy path unchanged — including a channel routed to the
    DEFAULT printer, whose state, lease and control paths are the legacy
    ones (Stage 2b). Routing engages only for the other printers. The
    standalone gateway knows the channel map; a bot-runtime source also
    carries the id stamped on it. Falling back to "the current printer"
    for a channel that routes elsewhere would be a wrong-printer defect,
    so an id found here is always honoured, never re-derived.
    """
    pid = None
    if gateway is not None:
        printer_for = getattr(gateway, 'printer_id_for', None)
        if printer_for is not None:
            try:
                pid = printer_for(source)
            except Exception:
                pid = None
    if not pid:
        pid = getattr(source, 'printer_id', None)
    if not isinstance(pid, str) or not pid:
        return None  # only a real stamped id routes; anything else is the legacy path
    try:
        from print_handy import settings
        if str(pid) == str(settings.default_printer_id(_config.cfg())):
            return None
    except Exception:
        pass
    return str(pid)


def _pkw(pid):
    """Call kwargs carrying a routed printer: {} when unrouted, so legacy call shapes stay exact."""
    return {'printer_id': pid} if pid else {}


def _channel_printer_id(source, gateway=None):
    """The actual roster id of the printer this event's channel serves (P1-77).

    ``_routed_printer_id`` normalizes the default printer to None so its
    control paths stay legacy — but ownership checks must compare a job
    against the channel's real printer, default included, or the default
    channel silently skips them. A shared channel (#536) decides no
    printer: None here too, and the caller resolves from the job instead.
    """
    pid = _routed_printer_id(source, gateway)
    if pid:
        return pid
    if _shared_printer_ids(source, gateway):
        return None  # undecided shared channel: never the default by fall-through
    try:
        from print_handy import settings
        return str(settings.default_printer_id(_config.cfg()))
    except Exception:
        return None


def _printer_label(pid):
    """A routed printer's display name for user-facing text (the default printer is named, design §6.1)."""
    try:
        from print_handy import settings
        entry = settings.by_id(pid, _config.cfg())
        return entry.get('name') or str(pid)
    except Exception:
        return str(pid)


def _routed_form(printer_id):
    """A real roster id in ``_routed_printer_id`` form: None for the default printer."""
    if not printer_id:
        return None
    try:
        from print_handy import settings
        if str(printer_id) == str(settings.default_printer_id(_config.cfg())):
            return None
    except Exception:
        pass
    return str(printer_id)


def _shared_printer_ids(source, gateway=None):
    """The roster ids sharing this event's channel (#536), or None when decided.

    Standalone, the gateway's shared map (or the stamp BotCore put on the
    source) answers. A host with no channel map at all serves one
    channel per profile, so a roster with more than one printer makes every
    unrouted event undecided among the whole roster. A gateway that HAS a
    channel map decides authoritatively: no shared entry there means the
    channel is not shared, whatever the roster holds.
    """
    if gateway is not None:
        shared_for = getattr(gateway, 'shared_printer_ids_for', None)
        if shared_for is not None:
            try:
                ids = shared_for(source)
            except Exception:
                ids = None
            if ids:
                return [str(pid) for pid in ids]
        if getattr(gateway, 'printer_id_for', None) is not None:
            return None
    stamped = getattr(source, 'shared_printer_ids', None)
    if stamped:
        return [str(pid) for pid in stamped]
    try:
        from print_handy import settings
        entries = settings.printers(_config.cfg())
    except Exception:
        return None
    if len(entries) > 1:
        return [str(entry['id']) for entry in entries]
    return None


def _undecided_printer_ids(source, gateway=None):
    """Candidate roster ids when this event's channel is shared and no printer
    is decided yet — None when routing (or the legacy default) already decided."""
    if _routed_printer_id(source, gateway):
        return None
    return _shared_printer_ids(source, gateway)


def _live_key_for(source, gateway, printer_id=None):
    """The live session key prompts register under, printer-scoped once decided (#536).

    In a shared channel a decided flow's questions register under
    ``<session key>:printer:<id>`` so two same-user flows in one channel
    never share a typed-answer route; undecided or routed flows keep the
    plain key, byte-identical to before.
    """
    key = None
    session_for = getattr(gateway, '_session_key_for_source', None) if gateway is not None else None
    if session_for is not None:
        try:
            key = session_for(source)
        except Exception:
            key = None
    if key and printer_id:
        from print_handy.core import prompts as _prompts_mod
        return _prompts_mod.printer_scoped_key(key, printer_id)
    return key


def _question_timeout():
    cfg = globals().get('_config')
    return cfg.question_timeout_s() if cfg is not None else 1800


async def _ask_which_printer(source, gateway, adapter, candidates, *, question=None, label_for=None,
                             metadata=None):
    """Ask which printer, in a shared channel (#536). Returns the chosen roster id, or None.

    Choices are the roster display names; ``label_for`` may add a per-printer
    suffix (a busy printer is labelled with its state, so picking it is
    deliberate). Duplicate display names are disambiguated with the roster
    id — two printers may share a name, never an id. ``metadata`` is the
    caller's delivery metadata, passed through to ``_confirm`` like every
    other prompt: the adapter addresses the question from it (a thread's
    picker belongs in the thread, not the parent channel).
    """
    names = {}
    for pid in candidates:
        names.setdefault(_printer_label(pid), []).append(pid)
    labels = {}
    for pid in candidates:
        label = _printer_label(pid)
        if len(names[label]) > 1:
            label = f'{label} ({pid})'
        if label_for is not None:
            try:
                suffix = label_for(pid)
            except Exception:
                suffix = None
            if suffix:
                label = f'{label} — {suffix}'
        labels[label] = pid
    answer = await _confirm(adapter, source, metadata, 'which-printer',
                            question or 'Which printer should I use?',
                            list(labels), timeout=_question_timeout(),
                            live_key=_live_key_for(source, gateway))
    if answer not in labels:
        return None
    return labels[answer]


async def _decide_printer(source, gateway, adapter, metadata, *, question=None):
    """Decide the printer for a printer-bound handler.

    Returns ``(pid, chosen_id)``: pid in routed form (None = the default
    printer), chosen_id the real roster id when a shared channel's picker
    decided (None when routing decided). Returns None when the picker was
    shown and not answered — the caller stops; nothing was sent anywhere.
    """
    pid = _routed_printer_id(source, gateway)
    candidates = _undecided_printer_ids(source, gateway)
    if not candidates:
        return pid, None
    chosen = await _ask_which_printer(source, gateway, adapter, candidates, question=question,
                                      metadata=metadata)
    if chosen is None:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='No printer chosen, so nothing was sent to any printer.')
        return None
    return _routed_form(chosen), chosen


def _busy_label(status):
    """A short busy/idle suffix for a picker choice from a status dict, or None."""
    if not isinstance(status, dict):
        return None
    state = str(status.get('state') or '')
    if state == 'printing':
        name = str(status.get('filename') or '').strip()
        return f'printing {name}' if name else 'printing'
    if state == 'paused':
        return 'paused'
    return None


async def _candidate_statuses(candidates, status_fn):
    """{roster id: status dict or None} for picker candidates, best effort."""
    out = {}
    for pid in candidates:
        try:
            out[pid] = await asyncio.to_thread(status_fn, **_pkw(_routed_form(pid)))
        except Exception:
            out[pid] = None
    return out


def _adapter_for(gateway, source):
    """The adapter a dispatch handler renders through: the host adapter."""
    return gateway._delivery_adapter_for(source)


def _design_root(cfg):
    from print_handy.core import design
    cache = (cfg.get('paths') or {}).get('cache')
    return design.sessions_root(cache) if cache else design.sessions_root(_state_home() / 'cache')


def _design_transport(cfg):
    """(complete, kind) for a design turn, or (None, '') when no model is reachable.

    The HTTP transport is the only path (#524 Stage 3): its endpoint
    resolves from [design] inheriting [llm].
    """
    from print_handy.core import design
    if design.resolve_http_config(cfg) is not None:
        def http(messages):
            return asyncio.to_thread(design.http_complete, cfg, messages)

        return http, 'http'
    return None, ''


async def _design_handoff(event, gateway, adapter, session, stl_path):
    """Hand a finished design STL to prepared intake, exactly like a dropped file.

    The design flow slices and prints nothing itself: prepare_link runs its
    own fit, colour and confirmation gates from here, unchanged.

    A multi-part design (#1204 item 4) hands its part files over together,
    exactly like several files dropped in one message, so they become one
    project_v1 project through the same builder and continue plate by plate
    through the same next-plate flow. Slice it is not an approval: intake
    still asks before anything starts, and each later plate asks again.
    """
    import copy as _copy
    import uuid as _uuid
    from print_handy.core import design as _design
    from print_handy.core import printer_state
    from print_handy.core.link_intake import PromptCancelled
    from print_handy.core.prepared_intake import prepare_link
    source = event.source
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    try:
        part_paths, record = _design.multipart_handoff(_design_root(_config.cfg()), session)
    except _design.DesignError as exc:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=f'The design could not be handed to intake: {exc} '
                                   'Nothing was sliced or started.')
        return {'action': 'skip', 'reason': 'design intake handoff failed'}
    if part_paths:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=f'Handing the {len(part_paths)} parts of the design to the normal '
                                   'intake as one project — it asks the usual questions before '
                                   'anything is sliced, and each plate is started separately.')
    else:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='Handing the design to the normal intake — it asks the usual '
                                   'questions before anything is sliced.')
    handed = _copy.copy(event)
    handed.text = ''
    handed.media_urls = [str(p) for p in part_paths] or [str(stl_path)]
    handed.message_id = f"design-{session['id']}-{_uuid.uuid4().hex[:8]}"
    handed.metadata = dict(getattr(event, 'metadata', None) or {})
    handed.metadata['live_session_key'] = gateway._session_key_for_source(source)
    if record is not None:
        handed.metadata['design_handoff'] = record
    else:
        handed.metadata.pop('design_handoff', None)
    root = _state_dir() / 'model-link-intakes'
    pid = _routed_printer_id(source, gateway)
    candidates = _undecided_printer_ids(source, gateway) if not pid else None
    if candidates:
        # A designed part is a new print (#536): in a shared channel it asks
        # which printer, like a dropped link — design sessions are keyed by
        # channel, so the conversation stays one design; only its print
        # target was undecided.
        from print_handy.runtime import print_control as _print_control
        statuses = await _candidate_statuses(candidates, _print_control.status)
        chosen = await _ask_which_printer(source, gateway, adapter, candidates,
                                          question='Which printer should I print this design on?',
                                          label_for=lambda cid: _busy_label(statuses.get(cid)),
                                          metadata=metadata)
        if chosen is None:
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content='No printer chosen, so nothing was sliced or sent anywhere. '
                                       'The design files are saved.')
            return {'action': 'skip', 'reason': 'shared channel: no printer chosen for design intake'}
        pid = _routed_form(chosen)
        handed.metadata['live_session_key'] = _live_key_for(source, gateway, chosen)
    try:
        return await prepare_link(handed, adapter, printer_state.load(**_pkw(pid)), root,
                                  **_pkw(pid))
    except PromptCancelled:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='Cancelled. Nothing sliced, nothing uploaded, nothing started.')
        return {'action': 'skip', 'reason': 'operator cancelled design intake'}
    except Exception:
        logger.exception('design intake handoff failed')
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='The intake handoff failed. The design files are saved; '
                                   'nothing was sliced or started.')
        return {'action': 'skip', 'reason': 'design intake handoff failed'}


DESIGN_WORKING_NOTE = ('Designing it now. I check the part before I show it, so this can take '
                       'a minute or two.')
_DESIGN_TYPING_EVERY_S = 8  # a typing indicator lasts about 10 s on Discord and Matrix


async def _run_design_turn(engine, session, instruction, adapter, source, metadata):
    """engine.run_turn with the chat kept informed (#1206).

    A turn can take minutes (up to 1 + repair_attempts model calls and
    renders) and used to post nothing until it ended. A model turn is
    acknowledged once up front, and the typing indicator is renewed until
    the turn returns. Undo and plain parameter edits are quick and get no
    acknowledgement. Both signals are cosmetic and never fail the turn.
    """
    if engine.will_call_model(session, instruction):
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=DESIGN_WORKING_NOTE)
    signal = getattr(adapter, 'signal_working', None)

    async def keep_typing():
        while True:
            try:
                await asyncio.wait_for(signal(source.chat_id, metadata=metadata), 10)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug('design typing indicator failed: %r', exc)
            await asyncio.sleep(_DESIGN_TYPING_EVERY_S)

    task = asyncio.create_task(keep_typing()) if signal is not None else None
    try:
        return await engine.run_turn(session, instruction)
    finally:
        if task is not None:
            task.cancel()


# owner -> phase: "turn" while a model call/render runs, "prompt" while the
# Slice it / Change it / Cancel question is open and waiting for an answer.
_DESIGN_BUSY: dict = {}

_DESIGN_UNEXPECTED_FAILURE = (
    'That design change did not work, and I could not tell why from here. Nothing was '
    'sliced or started. If it keeps happening, run `/print-handy bundle` and share the '
    'bundle so it can be looked into.')


def _design_owner(source):
    """The per-user design key: one design conversation per user per channel/thread."""
    return f"{source.chat_id}:{source.thread_id or ''}:{getattr(source, 'user_id', '')}"


@contextlib.contextmanager
def _design_claim(owner):
    """Hold the owner's design claim (P2-151) for one whole design dispatch:
    every turn, and the question after it. Released however it ends."""
    _DESIGN_BUSY[owner] = 'turn'
    try:
        yield
    finally:
        _DESIGN_BUSY.pop(owner, None)


async def _design_busy_reply(adapter, source):
    """The refusal for a second design request while the owner's claim is held."""
    if adapter is not None:
        metadata = {'thread_id': source.thread_id} if source.thread_id else None
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='A design turn is already running. Let it finish, '
                                   'then send your change again. Nothing was changed. '
                                   'Model links, files and print commands still work meanwhile.')
    return {'action': 'skip', 'reason': 'design turn already running'}


def _design_engine(cfg, complete):
    """The design engine over the same session folder _design_root reads."""
    from print_handy.core import design
    return design.DesignEngine((cfg.get('paths') or {}).get('cache') or (_state_home() / 'cache'),
                               complete, cfg=cfg)


def _design_failure_text(exc):
    """What to post when a design turn raises: the engine's own words for a
    DesignError, and an honest "could not tell why" (logged) for anything else."""
    from print_handy.core import design
    if isinstance(exc, design.DesignError):
        return f'That design change did not work: {exc} Nothing was sliced or started.'
    logger.error('design turn failed unexpectedly', exc_info=exc)
    return _DESIGN_UNEXPECTED_FAILURE


async def _design_slice(event, gateway, adapter, engine, session, stl_path):
    """Slice it: hand the version on screen to prepared intake, unless it is not
    a printable solid. Only a dropped model's own baseline (or an undo back to
    it) can be one; model turns with a "fail" finding are refused in the engine.
    Such a mesh is never handed over (#1206), so the session waits instead."""
    from print_handy.core import design
    failed = design.unprintable(session.get('findings'))
    if failed:
        engine.awaiting_revision(session)
        source = event.source
        metadata = {'thread_id': source.thread_id} if source.thread_id else None
        await adapter.send(
            chat_id=source.chat_id, metadata=metadata,
            content=('This version is not a printable solid (' + '; '.join(failed) + '), so I '
                     'will not hand it to the intake. Say a change that fixes it, or "cancel". '
                     'To print the file as it is, drop it again without a design phrase: the '
                     'intake checks it on its own before anything is sliced.'))
        return {'action': 'skip', 'reason': 'design not printable'}
    engine.mark_handed_off(session)
    return await _design_handoff(event, gateway, adapter, session, stl_path)


async def _design_conversation(event, gateway, adapter, engine, result, *, note=''):
    """Post a design turn, then ask "What next for this design?" until the
    owner slices it, asks for a change, or cancels.

    Shared by /print-design and the edit chat's geometry route (#1206), so
    both post the same reply (what was made, measured, assumed, and any
    check finding that survived the repairs), take the same typed answers
    (a button word or number is that button, any other text is the next
    turn), and mark the owner's claim "prompt" while the question is open
    so design_dispatch answers typed text there.
    """
    from print_handy.core import design
    source = event.source
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    owner_phase_key = _design_owner(source)

    async def say(content):
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=content)

    timeout = (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800)
    while True:
        session = result['session']
        lines = design.reply_lines(result)
        if note:
            lines.append(note)
        changes = design.format_param_changes(result.get('param_changes') or [])
        if changes:
            lines.append('Parameters changed: ' + '; '.join(changes))
        lines.append(design.change_hint(session) + ' Slice it sends it through the normal '
                     'intake; nothing prints without the usual confirmation.')
        if result.get('preview_path'):
            lines.append(f"MEDIA:{result['preview_path']}")
        else:
            reason = result.get('preview_error') or 'renderer unavailable'
            lines.append(f"(No preview: {reason}. Check the dimensions above before slicing.)")
        await say('\n'.join(lines))
        note = ''
        if owner_phase_key in _DESIGN_BUSY:
            _DESIGN_BUSY[owner_phase_key] = 'prompt'
        try:
            answer = await _confirm(adapter, source, metadata, 'print-design',
                                    'What next for this design?', list(design.CHOICES), timeout,
                                    live_key=gateway._session_key_for_source(source))
        finally:
            if owner_phase_key in _DESIGN_BUSY:
                _DESIGN_BUSY[owner_phase_key] = 'turn'
        if isinstance(answer, str) and answer not in design.CHOICES:
            # A typed "cancel", "slice it" or "2" is that button (#1206);
            # only other text is a change.
            answer = design.typed_choice(answer) or answer
        if answer == design.SLICE_LABEL:
            return await _design_slice(event, gateway, adapter, engine, session,
                                       result['stl_path'])
        if answer == design.CHANGE_LABEL:
            engine.awaiting_revision(session)
            await say('Say the change in your next message — e.g. "make it 5 mm taller". '
                      f'({design.MAX_TURNS} turns per design; this one has had {session["turns"]}.)')
            return {'action': 'skip', 'reason': 'design awaiting revision'}
        typed = str(answer or '').strip()
        from print_handy.core.prompts import CANCELLED
        # A closed question (a session reset or shutdown resolves it with
        # CANCELLED) ends the design; it is never a change to run.
        if answer is None or not typed or typed in (design.CANCEL_LABEL, CANCELLED):
            break
        # A change typed instead of pressing a button ("make it 5 mm taller")
        # is the revision itself: run it as the next turn of the same design.
        try:
            result = await _run_design_turn(engine, session, typed, adapter, source, metadata)
        except design.DesignError as exc:
            engine.awaiting_revision(session)
            await say(f'That change did not work: {exc} The previous version is kept; '
                      'say another change, or /print-design <a new part> to start over.')
            return {'action': 'skip', 'reason': 'design revision failed'}
        except Exception:
            logger.exception('design revision failed unexpectedly')
            engine.awaiting_revision(session)
            await say('That change did not work, and I could not tell why from here. The previous '
                      'version is kept; nothing was sliced or started.')
            return {'action': 'skip', 'reason': 'design revision failed'}
    engine.cancel(session)
    await say('Design cancelled. The files stay in the session folder for a while; nothing '
              'was sliced or started.')
    return {'action': 'skip', 'reason': 'design cancelled'}


def intake_plain_start_failure(detail):
    """Plain chat wording for a print-now detail tail (#467).

    The tail itself (REFUSED lines, upload/start stderr) stays in the
    receipt and log; chat gets prepared_intake's translation, which the
    intake flow already uses for the same tails.
    """
    from print_handy.core import prepared_intake
    return prepared_intake.plain_start_failure(detail)


USE_LOADED_SPOOL = 'Use the loaded spool · bed is clear'
SPOOL_CHANGED = 'spool changed since slicing'


def _spool_change_text(target, detail):
    found = re.search(r'slot (\d+) spool changed since slicing \(was (\d+), now (\d+)\)', str(detail))
    where = f"Slot {found.group(1)} now holds spool #{found.group(3)} (it was #{found.group(2)} at slicing). " if found else ''
    return (f"{where}Print {target['label']} with the spool that is loaded now? "
            "Same material family is still required.")


async def _announce_command_start(send, target, detail, job=None, plate=None, chat_id=None, thread_id=None):
    """Post and register the one start announcement for a command start.

    /print-next, /print-again and the plate buttons start through
    print_next.start, whose model_job run claims the start announcement —
    and that claim suppresses the listener's fallback post. The intake
    flow posts its own start message for the listener to edit, but these
    handlers posted none, so their starts were never announced (P2-56).
    Post the canonical message here, mirroring the intake: a
    still-preparing start registers its message id so the listener edits
    this post after warmup. Start posts never carry the roast; it belongs to
    the completion post.
    """
    from print_handy.runtime import discord_print_summary as print_summary
    from print_handy.runtime import print_next
    if job is None:
        with contextlib.suppress(Exception):
            job, plate = print_summary.find_job(target['gcode'])
    message = print_summary.format_started(job, plate)
    # No roast here: the one roast per print rides the completion post (#874).
    remote = print_next.started_file(detail) if print_next.still_preparing(detail) else None
    sent = await send(message)
    message_id = getattr(sent, 'message_id', None) or getattr(sent, 'id', None)
    if remote and message_id:
        with contextlib.suppress(OSError):
            # The originating channel/thread, so the warmup edit does not fall
            # back to the configured channel (2.2.5 review, P2-11).
            print_next.record_start_message(remote, str(message_id), chat_id=chat_id, thread_id=thread_id)
    return sent


def _maintenance_hours_now(printer_id=None):
    from print_handy.runtime import print_control
    payload = (print_control._send('/server/history/totals', printer_id=printer_id)
               if printer_id else print_control._call('/server/history/totals'))
    result = payload.get('result') or {}
    seconds = result.get('total_time', result.get('print_time'))
    if seconds is None:
        raise RuntimeError('Moonraker history totals did not include total_time or print_time')
    return float(seconds) / 3600.0


def _cold_pull_temps():
    """(pla_hot, pla_cold, petg_hot, petg_cold, pla_retry_cold) from [cold_pull] in the home configuration."""
    from print_handy.runtime import print_control as pc
    pla_hot, pla_cold = pc.COLD_PULL_PROFILES['pla_trial']
    petg_hot, petg_cold = pc.COLD_PULL_PROFILES['petg']
    return pla_hot, pla_cold, petg_hot, petg_cold, pla_cold + 5


async def _coldpull_workflow(adapter, source, metadata, *, source_tag, initial_cold=None, selected_material=None,
                             printer_id=None, live_key=None):
    """Shared user interaction for the standalone and maintenance cold-pull paths."""
    from print_handy.runtime import maintenance
    from print_handy.runtime import print_control as pc

    if printer_id is None:
        # No explicit printer (the standalone /print-coldpull call): the
        # source itself carries the channel's routed printer (#335).
        _route = globals().get('_routed_printer_id')
        printer_id = _route(source) if _route else None

    pla_hot, pla_cold, petg_hot, petg_cold, retry_cold = _cold_pull_temps()
    answer = selected_material
    if answer is None:
        answer = await _confirm(adapter, source, metadata, f'{source_tag}-material',
            'Choose material for this cold pull.',
            [f'PLA (trial) {pla_hot}/{pla_cold}', f'PETG {petg_hot}/{petg_cold}'], (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800),
            live_key=live_key)
    if not answer:
        return None, 'Cancelled or expired. Nothing sent.'
    is_pla = answer.startswith('PLA') or answer.startswith('Run PLA')
    temp, cold = (pla_hot, pla_cold) if is_pla else (petg_hot, petg_cold)
    if is_pla and (answer.endswith(f'{pla_hot}/{retry_cold}') or initial_cold == retry_cold):
        cold = retry_cold
    state = maintenance.load(**_pkw(printer_id))
    pulls = [r.get('cold_pull', {}) for r in state.get('runs', []) if r.get('cold_pull')]
    prior = pulls[-1] if pulls else {}
    if is_pla and (initial_cold == retry_cold or prior.get('next_cold') == retry_cold):
        cold = retry_cold
        if initial_cold != retry_cold:
            retry_answer = await _confirm(adapter, source, metadata, f'{source_tag}-temp',
                f'Previous PLA tip snapped or did not release. Try COLD={retry_cold} once?',
                [f'Use COLD={retry_cold}', f'Keep COLD={pla_cold}'], (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800),
                live_key=live_key)
            if retry_answer != f'Use COLD={retry_cold}':
                cold = pla_cold
    dirty_count = 0
    for pull in reversed(pulls):
        if pull.get('result') != 'dirty':
            break
        dirty_count += 1
    if dirty_count:
        repeat = await _confirm(adapter, source, metadata, f'{source_tag}-repeat',
            'A prior pull was dirty. Repeat once?', ['Run one repeat', 'Cancel'], (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800),
            live_key=live_key)
        if repeat != 'Run one repeat':
            return None, 'Cancelled. Nothing sent.'
    await adapter.send(chat_id=source.chat_id, metadata=metadata,
        content='Bed must be clear; it homes. Screen may look frozen for a few minutes. Confirm only at the printer.')
    confirmed = await _confirm(adapter, source, metadata, f'{source_tag}-actuate',
        'I am present, the bed is clear, and authorize one cold pull.', ['Confirm cold pull', 'Cancel'], (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600),
        live_key=live_key)
    if confirmed != 'Confirm cold pull':
        return None, 'Cancelled or expired. Nothing sent.'
    import time as _time
    # Capture authorization only after the successful button response.
    confirmed_at = _time.time()
    ok, detail = await asyncio.to_thread(pc.coldpull, temp, cold, confirmed_at=confirmed_at, **_pkw(printer_id))
    record = {'kind': 'coldpull', 'source': source_tag,
              'cold_pull': {'temp': temp, 'cold': cold, 'result': 'awaiting_tip' if ok else 'failed', 'detail': detail}}
    if not ok:
        record['hours_total'] = await (asyncio.to_thread(_maintenance_hours_now, printer_id) if printer_id else asyncio.to_thread(_maintenance_hours_now))
        maintenance.append_run(record, **_pkw(printer_id))
        return record, detail
    tip = await _confirm(adapter, source, metadata, f'{source_tag}-tip', 'How did the pulled tip look?',
                         ['Clean', 'Dirty', 'Snapped', "Didn't release"], (globals().get('_config').cold_pull_outcome_s() if globals().get('_config') else 1800),
                         live_key=live_key)
    result = {'Clean': 'clean', 'Dirty': 'dirty', 'Snapped': 'snapped', "Didn't release": 'didnt_release'}.get(tip)
    if not result:
        record['hours_total'] = await (asyncio.to_thread(_maintenance_hours_now, printer_id) if printer_id else asyncio.to_thread(_maintenance_hours_now))
        maintenance.append_run(record, **_pkw(printer_id))
        return record, 'Cold pull finished; heater target is 0, but the tip result was not recorded.'
    guidance = maintenance.result_guidance(result, dirty_count)
    record['cold_pull'].update({'result': result, **guidance})
    if guidance['offer_cold_95'] and temp == pla_hot:
        record['cold_pull']['next_cold'] = retry_cold
    record['hours_total'] = await (asyncio.to_thread(_maintenance_hours_now, printer_id) if printer_id else asyncio.to_thread(_maintenance_hours_now))
    maintenance.append_run(record, **_pkw(printer_id))
    message = detail
    if guidance['offer_repeat']:
        message += ' One dirty pull can be repeated.'
    if guidance['suggest_nozzle_swap']:
        message += ' Two dirty pulls in a row: consider a nozzle swap.'
    if guidance['offer_cold_95'] and temp == pla_hot:
        message += f' Next PLA attempt can use COLD={retry_cold}.'
    return record, message


def _require_clicked_question_current(spool_sync, context, current):
    """Refuse a spool-question action whose clicked question is not the pending one.

    Button clicks carry the context of the question they were posted for,
    and a question answered in text leaves its buttons behind (the alert
    record is retained for hours). If a newer question has opened since,
    substituting the current record would silently apply the stale click
    to the newer print — a stale Cancel would cancel a print the operator
    never confirmed. Compare the question identity (recorded_at, then
    filename) with the loaded record and refuse a mismatch before any
    printer or inventory action. A caller that passes the record it just
    loaded itself (the text-answer path) always matches.
    """
    clicked = context or {}
    clicked_at = clicked.get('recorded_at')
    if isinstance(clicked_at, (int, float)) and clicked_at != current.get('recorded_at'):
        raise spool_sync.SpoolError(
            'that spool question is out of date — a newer spool question is open now, '
            'so nothing was changed. Answer the current question instead.')
    clicked_name = str(clicked.get('filename') or '')
    current_name = str(current.get('filename') or '')
    if clicked_name and current_name and clicked_name != current_name:
        raise spool_sync.SpoolError(
            'that spool question is for a different print than the one paused now, '
            'so nothing was changed. Answer the current question instead.')


def _cancel_spool_question_print(listener, spool_sync, context):
    """Cancel the paused print behind a pending spool question, then close the question.

    The cancel itself is print_stop.cancel — the same path as /print-stop and
    the paused alert's Cancel button: state re-read first, the file must still
    match, one cancel, no retries, Bambu LAN printers get their LAN stop.
    The question is closed only once the print is actually over (cancel
    confirmed, or the printer had already left printing/paused on its own):
    the pending record is cleared, which stops the reminders, and filament
    usage is finalised exactly as a cancelled print leaves it — Moonraker
    charged spools in real time up to the suspension, so nothing is resumed,
    re-charged, archived or rebound here. When the cancel does not end the
    print (refused, or sent but the printer still reports printing/paused),
    this raises and the question stays open and answerable, so the operator
    is never deadlocked by a failed cancel.
    """
    from print_handy.runtime import print_stop
    current = listener.load_runout_pause()
    if current is None:
        raise spool_sync.SpoolError('that spool question was already answered; nothing changed')
    _require_clicked_question_current(spool_sync, context, current)
    context = current
    filename = context.get('filename') or ''
    label = filename or 'the paused print'
    from print_handy import printer_control
    before = print_stop.status()
    if before.get('state') in print_stop.ACTIVE:
        _sent, detail = printer_control.for_printer().cancel(filename)
        after = print_stop.status()
        if after.get('state') in print_stop.ACTIVE:
            raise spool_sync.SpoolError(
                f'the print is still {after.get("state")} — {detail}')
        outcome = f'Print cancelled: {detail}'
    else:
        outcome = (f'The print was already over (printer reports {before.get("state")}), '
                   'so there was nothing to cancel.')
    listener.clear_runout_pause()
    return (f'{outcome} The spool question for {label} is closed and its reminders are off. '
            'Filament usage stays paused for every spool: nothing was charged after the '
            'question opened, and no spool binding changed.')


# A multi-slot print keeps Moonraker's active spool cleared (#1362): the
# answer never points usage at one spool, and the print's end charges each
# slot's spool its share.
_MULTISLOT_TRACKING = ('This is a multi-slot print, so no single spool is charged while it runs; '
                       "each slot's spool is charged its share when the print ends.")


def _answer_spool_question(listener, spool_sync, context, choice):
    """Apply one answer. Spoolman first, then hardware map + screen, then usage resumes. No motion."""
    current = listener.load_runout_pause()
    if current is None:
        raise spool_sync.SpoolError('that spool question was already answered; nothing changed')
    _require_clicked_question_current(spool_sync, context, current)
    context = current
    slot = context.get('slot')
    prior = context.get('prior_spool_id')
    created = ''
    if choice['status'] == 'refeed':
        # The same physical spool stays in the slot (#1063, incl. the jam
        # follow-up): the pause signal said runout, but the roll is not
        # exhausted — its feed is stuck, jammed, or falsely tripped, and
        # the operator clears it at the printer. Nothing is created,
        # bound or depleted and the weight record is untouched — usage
        # accounting simply resumes on that spool and the question
        # closes. This is the answer the second pause after a replacement
        # needs; every other route would mint another record, mark a
        # good roll depleted, or refuse the spool outright.
        keep = prior or context.get('suspended_spool_id')
        if not keep:
            raise spool_sync.SpoolError('I do not know which spool was in the slot; answer with its number instead')
        resumed = spool_sync.resume_usage(int(keep))
        listener.clear_runout_pause()
        tracking = f'Usage tracking is back on #{keep}.' if resumed is not False else _MULTISLOT_TRACKING
        # The three concerns stay separate (#1063 jam follow-up): this
        # settles the inventory record only. It does not say the feed
        # path is clear, it does not resume the print, and Print Handy
        # names plainly that it cannot drive a paused-print load/unload
        # itself instead of funneling the operator into a replacement.
        return (f'Got it — the same physical spool #{keep} stays in the slot; this is a feed '
                f'fix, not a replacement. No new spool was created, spool #{keep} was not '
                f'marked depleted, and its remaining weight record is unchanged. {tracking}\n'
                f'That settles the spool records only: it does not mean the feed path is '
                f'clear, and it does not resume the print. Clear the stuck filament at the '
                f'printer and resume from the print controls when the path is actually '
                f'clear. Print Handy cannot load or unload filament for you while the print '
                f'is paused — no supported paused-print load/unload command is available to '
                f'it — so the physical fix happens at the printer.')
    if choice['status'] == 'same':
        keep = prior or context.get('suspended_spool_id')
        if not keep:
            raise spool_sync.SpoolError('I do not know which spool was there before; reply with its number')
        if context.get('cause') != 'runout':
            resumed = spool_sync.resume_usage(int(keep))
            listener.clear_runout_pause()
            return (f'Got it, same spool #{keep}. '
                    + (f'Usage tracking is back on #{keep}.' if resumed is not False else _MULTISLOT_TRACKING))
        # A runout's "same" is a new roll of the same product: the old spool is
        # empty, so charging it on would leave Spoolman wrong. Clone, then bind
        # below exactly like a New spool answer (the old one goes depleted).
        if slot not in (1, 2, 3, 4):
            raise spool_sync.SpoolError('I could not tell which slot changed; use `/spool change <slot> <spool#>`')
        made = spool_sync.fresh_spool_like(int(keep))
        choice = {'status': 'existing', 'spool_id': int(made['spool_id'])}
        created = f"Created spool #{made['spool_id']}, a new {made['grams']:g} g roll like #{keep}. "
    if slot not in (1, 2, 3, 4):
        raise spool_sync.SpoolError('I could not tell which slot changed; use `/spool change <slot> <spool#>`')
    if choice['status'] == 'new':
        # Parsed-color sanity (#792 part 2): a text answer that parses to
        # multiple colors or a fragment is not minted unconfirmed. The
        # walkthrough's own answers carry color_confirmed (its confirmation
        # step named the color); a bare text answer is refused with what
        # was understood, so the operator corrects it or uses the New spool
        # button, whose walkthrough asks the confirm-or-correct step.
        if not choice.get('color_confirmed'):
            from print_handy.core import filament_color
            sanity_text = filament_color.color_sanity_text(choice.get('color'), choice.get('material'))
            if sanity_text is not None:
                error = spool_sync.SpoolError(
                    f'{sanity_text} Nothing was created or changed, and usage is still paused. '
                    'Answer again with the color exactly as labeled, or tap New spool and confirm '
                    'the color there.')
                error.color_sanity = True  # its text is written for the operator; show it as-is
                raise error
        state = spool_sync.load_state()
        previous_slot = (state.get('filament_slots') or {}).get(str(slot)) or {}
        # The color code: a picked swatch, else the color name read against
        # the named-color table, else (only then) the slot's previous swatch.
        from print_handy.core import filament_color
        picked_hex = spool_sync.screen_hex(choice.get('hex'))
        derived_hex = None if picked_hex else filament_color.derive_hex(
            choice.get('color'), choice.get('material'))
        existing_hex = (picked_hex or derived_hex
                        or spool_sync.screen_hex(previous_slot.get('hex')))
        spec = {'brand': choice.get('brand'), 'material': choice['material'],
                'color': choice['color'], 'grams': choice['grams'],
                'nozzle_temp': choice.get('nozzle_temp'),
                'bed_temp': choice.get('bed_temp')}
        # A name-derived swatch is NOT passed as an explicit hex (P2-234):
        # spec_color returns an empty palette whenever a hex is given, so a
        # multi-color name ("rose red blue green") would lose its
        # multi_color_hexes. With no hex key, create_spool derives the same
        # representative swatch AND the palette from the name itself. A
        # picked swatch or the previous slot's inherited swatch stays
        # explicit — neither has a palette to preserve.
        if derived_hex is None and existing_hex:
            spec['hex'] = existing_hex
        made = spool_sync.create_spool(spec)
        spool_id = int(made['spool_id'])
        created = f'Created spool #{spool_id}. '
    else:
        spool_id = int(choice['spool_id'])
    runout = context.get('cause') == 'runout'
    if runout and prior and int(prior) == spool_id:
        raise spool_sync.SpoolError(f'spool #{spool_id} is the one that just ran out; pick another')
    # The old spool is depleted by the bind itself, never before it:
    # bind_slot validates the replacement first (live, not archived, not
    # mapped to another slot or printer) and only then runs its 'depleted'
    # disposition on the spool it replaced. Patching the old spool depleted
    # ahead of the bind destroyed it whenever the bind then refused the
    # replacement, while the failed answer reported nothing changed (P1-8).
    try:
        result = spool_sync.bind_slot(slot, spool_id, prior_disposition='depleted' if runout else 'shelf',
                                      note=f"Bound from Discord after mid-print {context.get('cause')}")
    except Exception as exc:
        if not created:
            raise
        # The new spool exists now (#787, the typed twin of P2-204): never
        # let the answer report "nothing was changed", or a retry creates a
        # duplicate. Name the spool so the operator answers with its number.
        # Only a SpoolError's own operator-phrased reason reaches chat; any
        # other failure is logged, never shown raw.
        reason = f' ({exc})' if isinstance(exc, spool_sync.SpoolError) else ''
        error = spool_sync.SpoolError(
            f'Spool #{spool_id} was created and is on the shelf, but loading it into slot {slot} '
            f'did not work{reason}. The slot was not changed and usage is still paused; answer with '
            f'spool #{spool_id} rather than adding another.')
        error.spool_created = spool_id
        raise error from exc
    resumed = spool_sync.resume_usage(spool_id)
    listener.clear_runout_pause()
    # bind_text already names the 'depleted' disposition ("Spool #N was
    # marked as depleted"), so the old spool's fate is stated once (#787).
    tracking = f'Usage tracking is back on, charged to #{spool_id}.' if resumed is not False else _MULTISLOT_TRACKING
    return f'{created}{spool_sync.bind_text(result)} {tracking}'


def _spool_timeout():
    return (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800)


def _prompt_brand():
    """Whether new-spool intake asks for a brand (#512): spool.prompt_brand,
    off by default — an unreadable config also means off, never a surprise step."""
    try:
        return bool((_config.cfg().get('spool') or {}).get('prompt_brand'))
    except Exception:
        return False


def _photo_intake_ready():
    """Whether the photo label step can be offered (#514): any extraction
    route is available — the label's barcode/QR code (zxing-cpp wheel),
    local OCR (a tesseract binary), or a vision endpoint (the
    visual_watch.vision_ready rule: PRINT_VISION_URL, or
    services.vision_chat_url with its key). No route, no offer, and the
    walkthrough is exactly as it was without one."""
    try:
        from print_handy.runtime import moonraker_event_listener as listener
        if listener._vision_ready():
            return True
    except Exception:
        pass
    try:
        from print_handy.runtime import spool_codes, spool_label
        return bool(spool_codes.codes_available() or spool_label.tesseract_available())
    except Exception:
        return False


def _spool_label_vision_call():
    """The shared vision call for label extraction (#514), carrying the
    listener's configured values — the same endpoint the camera watch uses."""
    from print_handy.runtime import moonraker_event_listener as listener
    from print_handy.runtime import visual_watch

    def call(image, prompt):
        return visual_watch.visual_extract(
            image, prompt, chat_url=listener._vision_chat_url, model=listener._VISION_MODEL,
            api_key=lambda: listener._profile_env_value("CLIPROXY_API_KEY"),
            context="Filament spool label photo. Reply with JSON only.")

    return call


def _spool_label_routes(state):
    """The extraction routes for one label photo (#514), in precedence order.

    The label's own barcode/QR code first (deterministic data the
    manufacturer printed, readable offline), then the vision endpoint
    (the richest reader, when configured), then local OCR via tesseract
    (the offline fallback, matched against the known filament types the
    walkthrough already loaded). Only available routes are included; the
    chain (spool_label.extract_spool_spec_any) takes the first valid spec
    and never merges partial results across routes.
    """
    from print_handy.runtime import spool_codes, spool_label

    known = state.get('types') or []
    routes = []
    if spool_codes.codes_available():
        routes.append(('code', lambda image: spool_codes.extract_via_codes(image, known)))
    try:
        from print_handy.runtime import moonraker_event_listener as listener
        if listener._vision_ready():
            call = _spool_label_vision_call()
            routes.append(('vision', lambda image: spool_label.extract_spool_spec(image, call)))
    except Exception:
        pass
    if spool_label.tesseract_available():
        routes.append(('ocr', lambda image: spool_label.extract_via_ocr(image, known)))
    return routes


def _picker_label(entry):
    """The one spool picker label for a shelf entry (#792 part 2)."""
    from print_handy.runtime.discord_print_summary import spool_picker_label
    return spool_picker_label(entry.get('name'), entry.get('material'),
                              entry.get('spool_id'), entry.get('remaining_grams'))


_LIVE_SESSION = None


def _live_session():
    global _LIVE_SESSION
    if _LIVE_SESSION is None:
        from print_handy import live_stream
        _LIVE_SESSION = live_stream.LiveStreamSession()
    return _LIVE_SESSION


async def live_dispatch(event, gateway, **kwargs):
    """/print-live [start|stop|status]: the live camera viewing session (issue #311, stage 3).

    Discord only, and deliberately not a voice connection: the Stage 0
    audit found Discord's bot API cannot send video (the custom-sender
    lineage is selfbot-only, which Print Handy will never use), and the
    chosen transport is the Embedded App (Activity) in the voice
    channel. So start/stop drive the LiveStreamSession state machine in
    print_handy.live_stream and answer with the Activity pointer for
    the one configured voice channel; other platforms get no command
    surface at all — the dispatch returns None for their sources, like
    an unknown command. The [live_stream] activity page and its OAuth
    allowlist live in print_handy.live_activity; stage 4 serves that
    page from the supervised Activity server (print_handy.live_gate)
    behind the named tunnel, shares this session through the on-disk
    record (auto-start on print, viewer presence, the empty timeout),
    and is documented in docs/DISCORD-BOT-SETUP.md section 7.
    """
    source, adapter = _control_preamble(event, gateway, '/print-live', with_args=True)
    if source is None:
        return None
    if _platform_value(source) != 'discord':
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'live UI delivery unavailable'}
    from print_handy import live_stream
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    section = _config.cfg().get('live_stream') or {}
    args = str(getattr(event, 'text', '')).strip()[len('/print-live'):].split()
    verb = args[0].casefold() if args else 'status'
    # Stage 4: the session record is shared on disk with the listener
    # (auto-start on print) and the Activity server (viewer presence),
    # so adopt it when a state dir is resolvable; the module-global is
    # the fallback for contexts without one (tests install a config
    # override with no paths, which lands here). The leave-when-empty
    # timeout is enforced on entry: a session nobody has watched for
    # the configured timeout is already over before the verb runs.
    shared_path = None
    try:
        shared_path = live_stream.session_file_path(_config.cfg())
    except Exception:
        shared_path = None
    # P2-143: the read-modify-write below is one transaction over a
    # record the listener and the Activity server also write; it runs
    # under the shared cross-process session lock so a presence tick
    # cannot interleave and resurrect a session this command stops
    # (or overwrite this command's start with a stale snapshot).
    _session_guard = (live_stream.session_file_lock(shared_path)
                      if shared_path is not None else contextlib.nullcontext())
    with _session_guard:
        shared_existed = shared_path is not None and shared_path.exists()
        if shared_existed:
            session = live_stream.read_session_file(shared_path)
            globals()['_LIVE_SESSION'] = session
        else:
            session = _live_session()
        now = time.time()
        before = (session.state, session.started_by, session.started_at,
                  session.viewers, session.empty_since)
        live_stream.expire_empty(session, section, now=now)
        if verb == 'start':
            problem = live_stream.start_problem(section)
            if problem is not None:
                message, reason = problem, 'print live not configured'
            elif session.is_live:
                message = live_stream.session_text(session, section, now=now)
                reason = 'print live already live'
            else:
                session.start(str(source.user_id), now=now)
                message = live_stream.pointer_text(session, section)
                reason = 'print live started'
        elif verb == 'stop':
            if not session.is_live:
                message = 'The live camera view is not running. `/print-live start` starts it.'
                reason = 'print live not running'
            else:
                session.stop(now=now)
                message = ('Live camera view stopped. The feed is off and nothing was recorded. '
                           '`/print-live start` starts it again.')
                reason = 'print live stopped'
        elif verb == 'status':
            message = live_stream.session_text(session, section, now=now)
            reason = 'print live status shown'
        else:
            message = ('Use `/print-live start`, `/print-live stop` or `/print-live status`. '
                       'Nothing changed.')
            reason = 'print live usage shown'
        if shared_path is not None:
            after = (session.state, session.started_by, session.started_at,
                     session.viewers, session.empty_since)
            if shared_existed or after != before:
                try:
                    live_stream.write_session_file(session, shared_path)
                except Exception:
                    pass  # the record is bookkeeping; the reply still stands
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': reason}


def _command_matches(text, command, with_args=False):
    """The text half of _control_preamble: ``text`` (already stripped) is ``command``, or with_args its argument form."""
    return text == command or with_args and text.startswith(command + ' ')


def _control_preamble(event, gateway, command, with_args=False):
    """Shared gate for the /print-resume, /print-again and /print-reboot commands."""
    text = str(getattr(event, 'text', '')).strip()
    if not _command_matches(text, command, with_args):
        return None, None
    return _control_gate(event, gateway)


def _control_gate(event, gateway):
    """The source and authorization half of _control_preamble, for handlers
    whose text form is not the command itself (the plain 'print 57' form)."""
    source = getattr(event, 'source', None)
    if (source is None or not _is_bot_platform(source)
            or not _is_hosting_profile(getattr(source, 'profile', None))
            or getattr(event, 'internal', False) or getattr(source, 'is_bot', False)
            or getattr(source, 'profile_route_rejected', False)):
        return None, None
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None, None
    return source, _adapter_for(gateway, source)


async def _confirm(adapter, source, metadata, prefix, question, choices, timeout, live_key=None,
                   approval=False):
    import uuid
    from print_handy.core import prompts as _prompt_marks
    from print_handy.core.prompts import gateway as _prompts
    cm = _prompts()
    cid = uuid.uuid4().hex
    session_key = f'{prefix}:{source.chat_id}:{source.thread_id}'
    # Registration key vs delivery key (the link_intake.ask pattern): a typed
    # reply reaches a prompt only when the prompt is registered under the
    # live chat session key, because the bot core routes typed answers by
    # that key exactly. Button platforms resolve by clarify_id either way,
    # but Matrix has no buttons — its numbered replies are typed answers, so
    # a flow-key registration there can never be answered and the prompt
    # times out. send_clarify keeps the flow key for delivery.
    cm.register(cid, live_key or session_key, question, choices)
    if approval:
        # A print approval (P1-93): the mark lives in this package's own
        # registry, whose is_approval the standalone adapters consult,
        # whichever registry the prompt itself was registered with.
        _prompt_marks.mark_approval(cid)
    try:
        sent = await adapter.send_clarify(chat_id=str(source.chat_id), question=question, choices=choices,
                                          clarify_id=cid, session_key=session_key, metadata=metadata)
        if not getattr(sent, 'success', False):
            raise RuntimeError('button delivery failed')
        return await _prompt_marks.await_answer(cid, timeout, registry=cm)
    finally:
        cm.resolve_gateway_clarify(cid, cm.CANCELLED)
        _prompt_marks.forget_approval(cid)


# "print 57", "print #57", and (#979 review) "reprint 57", "re-print #57",
# "print 57 again": a reprint phrase that names a print number is that
# exact numbered print, never "the last print".
_PLAIN_PRINT_NUMBER_RE = re.compile(r"(?:re-?print|print)\s+#?(\d+)(?:\s+again)?\s*[.!]?", re.I)


def _plain_print_number(text):
    """'print 57' / 'print #57' / 'reprint 57' / 'print 57 again' -> 57 (#436, #979), else None.

    The plain-text twin of '/print-again <N>': deterministic, exact number
    only. A bare number with no 'print' is not a print request, and no other
    wording matches here — anything looser is the LLM fallback's business,
    on the platforms and configs that have one.
    """
    match = _PLAIN_PRINT_NUMBER_RE.fullmatch(str(text or '').strip())
    return int(match.group(1)) if match else None


def _start_refused_needs_restart(detail):
    text = str(detail or '').casefold()
    if not text.startswith('refused:'):
        return False
    return any(word in text for word in ('paused', 'cancelled', 'canceled', 'shutdown', 'klippy',
                                         'stale', 'retained job', 'not idle', 'state is error'))


async def _offer_force_restart(adapter, source, metadata, pc, reason, printer_id=None, live_key=None):
    """Offer the existing confirmation flow after a safe-to-restart start refusal."""
    answer = await _confirm(adapter, source, metadata, 'print-reboot',
        f"{reason} Force restart clears a stuck screen or heaters and wipes any Resume the printer screen is offering. Retry the print manually afterward.",
        [pc.REBOOT_LABEL, pc.CANCEL_REBOOT_LABEL], (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600),
        live_key=live_key)
    if answer != pc.REBOOT_LABEL:
        return 'Not restarted. Nothing sent; retry the print only after checking the printer.'
    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                       content='Restarting the printer now. The print will not start automatically; retry it after restart.')
    from print_handy import printer_control
    _ok, message = await asyncio.to_thread(printer_control.for_printer(printer_id).force_reboot)
    return message + (' Retry the print manually.' if _ok else '')


async def _delete_secret_message(event, source, adapter, label='Token'):
    """Best effort: delete the operator's message that carried a pasted secret.

    Prefers the adapter's own delete_message; otherwise falls back to its
    discord.py client. Any failure is
    ignored and answered with a plain note, so the operator knows to delete it.
    """
    message_id = getattr(event, 'message_id', None)
    deleted = False
    try:
        host = getattr(adapter, '_host', None) or adapter
        if message_id and hasattr(host, 'delete_message'):
            deleted = bool(await host.delete_message(source.chat_id, message_id,
                                                      {'thread_id': source.thread_id} if source.thread_id else None))
        elif message_id and getattr(host, '_client', None) is not None:
            client = host._client
            channel = client.get_channel(int(source.thread_id or source.chat_id)) or                 await client.fetch_channel(int(source.thread_id or source.chat_id))
            await channel.get_partial_message(int(message_id)).delete()
            deleted = True
    except Exception:
        deleted = False
    note = (f'{label} saved. I deleted your message.' if deleted else
            f"{label} saved. I couldn't delete your message, so please delete it yourself.")
    if adapter is not None:
        try:
            await adapter.send(chat_id=source.chat_id, content=note,
                               metadata={'thread_id': source.thread_id} if source.thread_id else None)
        except Exception:
            pass


def _recent_model_link(source):
    """The last model link in this channel, from Print Handy's own recent-links state (#524)."""
    from print_handy.bot import recent_links
    return recent_links.recall(getattr(source, 'chat_id', None), getattr(source, 'thread_id', None))


_INTAKE_UNAVAILABLE_REASON = 'new model intake unavailable; no start authorized'


def alert_button_handlers(bot, adapter):
    """Handle listener buttons, including IFS inventory choices, for authorized Discord users."""
    from print_handy.runtime import print_alert_buttons as pab

    def allowed(interaction):
        user = getattr(interaction, 'user', None)
        uid = str(getattr(user, 'id', ''))
        checker = getattr(adapter, '_is_allowed_user', None)
        if getattr(user, 'bot', False):
            return False
        if callable(checker):
            try:
                return checker(uid, user, guild=getattr(interaction, 'guild', None),
                               channel_ids={str(getattr(interaction, 'channel_id', ''))})
            except Exception:
                return False
        return True

    async def reject(interaction):
        await interaction.response.send_message('Not allowed. Nothing changed.', ephemeral=True)

    from print_handy.core import prepared_intake as _pi
    STOP_PREFIX = _pi.STOP_CUSTOM_ID

    async def send_stop(source, text, clarify_id):
        """Status step: one Stop button, no 'Other (type answer)'. Returns the sent message."""
        import discord
        target = getattr(source, 'thread_id', None) or getattr(source, 'chat_id', None)
        channel = bot.get_channel(int(target)) if target else None
        if channel is None and target:
            channel = await bot.fetch_channel(int(target))
        if channel is None:
            return None
        view = discord.ui.View(timeout=3600)
        view.add_item(discord.ui.Button(label=_pi.STOP_LABEL, style=discord.ButtonStyle.danger,
                                        custom_id=f'{STOP_PREFIX}{clarify_id}'))
        return await channel.send(text, view=view)

    async def edit_refusal(source, message, text, context, raw):
        """Retire Stop and replace it with plain guidance plus a single Retry button."""
        import discord
        from print_handy.runtime import print_alert_buttons as pab
        receipt = (context or {}).get('receipt') or {}
        receipt_id = str(receipt.get('intake_id') or '')
        if not re.fullmatch(r'[a-f0-9]{32}', receipt_id):
            await message.edit(content=text, view=None)
            return False
        view = discord.ui.View(timeout=(globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
        view.add_item(discord.ui.Button(label='Retry', style=discord.ButtonStyle.primary,
                                        custom_id=pab.retry_custom_id(receipt_id)))
        await message.edit(content=text, view=view)
        return True

    _pi.REFUSAL_EDITOR = edit_refusal

    async def handle_stop(interaction, clarify_id):
        from print_handy.core.prompts import gateway as _prompts
        cm = _prompts()
        resolved = cm.resolve_gateway_clarify(clarify_id, _pi.STOP_LABEL)
        note = 'Stopping.' if resolved else 'That step already finished. Nothing to stop.'
        try:
            await interaction.response.edit_message(view=None)
            await interaction.followup.send(note)
        except Exception:
            logger.debug('could not acknowledge Stop', exc_info=True)

    _pi.STOP_SENDER = send_stop
    async def insertion_select(interaction, aid, alert):
        import discord
        from print_handy.runtime import spool_sync
        slot = alert.get('slot')
        if slot not in (1, 2, 3, 4):
            await interaction.response.send_message('That insertion prompt is invalid. Nothing changed.', ephemeral=True)
            return
        # Acknowledge before the Spoolman reads (3 s window).
        await interaction.response.defer()
        state = await asyncio.to_thread(spool_sync.load_state)
        mapped = (state.get('filament_slots') or {}).get(str(slot)) or {}
        loaded_ids = {int(record.get('spoolman_id')) for key, record in (state.get('filament_slots') or {}).items()
                      if str(key) != str(slot) and isinstance(record, dict) and record.get('spoolman_id') is not None}
        try:
            spools = await asyncio.to_thread(spool_sync.spoolman, 'GET', '/api/v1/spool?allow_archived=false', None)
        except Exception as exc:
            await interaction.followup.send('The shelf spools could not be loaded, so nothing was changed. Check that Spoolman is running, then try again.', ephemeral=True)
            return
        options = pab.insertion_select_options(spools, mapped, loaded_ids)
        if not options:
            await interaction.followup.send('No live shelf spools are available. Nothing changed.', ephemeral=True)
            return
        select_aid = await asyncio.to_thread(pab.record, 'insertion-select', '', slot=slot)
        select = discord.ui.Select(placeholder='Choose a shelf spool', min_values=1, max_values=1,
                                   options=[discord.SelectOption(**option) for option in options],
                                   custom_id=f'pbinsertselect:{select_aid}')
        view = discord.ui.View(timeout=(globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
        view.add_item(select)
        await interaction.edit_original_response(content='Choose the shelf spool to map to this IFS slot. Nothing changes until you confirm the bind.', view=view)

    def intake_view(components):
        import discord
        view = discord.ui.View(timeout=(globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
        for row in components:
            for item in row.get('components', []):
                if item.get('type') == 3:
                    view.add_item(discord.ui.Select(
                        placeholder=item.get('placeholder') or 'Choose one', min_values=1, max_values=1,
                        options=[discord.SelectOption(**option) for option in item.get('options', [])],
                        custom_id=item['custom_id']))
                else:
                    view.add_item(discord.ui.Button(label=item['label'], style=discord.ButtonStyle(item['style']),
                                                    custom_id=item['custom_id']))
        return view

    async def intake_show_step(interaction, state, *, new_message=False, deferred=False):
        """Record the walkthrough's next step and show it (#512)."""
        aid = await asyncio.to_thread(pab.record, 'intake', '', **state)
        state = {**state}
        text = pab.intake_step_text(state)
        view = intake_view(pab.intake_step_components(aid, state))
        if deferred:
            await interaction.edit_original_response(content=text, view=view)
        elif new_message:
            await interaction.response.send_message(text, view=view)
        else:
            await interaction.response.edit_message(content=text, view=view)

    async def intake_advance(interaction, state, *, deferred=False):
        state = {**state, 'step': pab.intake_next_step(state)}
        await intake_show_step(interaction, state, deferred=deferred)

    async def intake_start(interaction, origin, slot, context=None):
        """Open the conversational new-spool walkthrough (#512), replacing the modal.

        No spool number is named at any step before creation (#1063):
        the earlier predicted id (max existing id + 1) could be wrong by
        the time the spool was created, and a roll labeled with it was
        labeled wrong. The actual number is announced the moment the
        creation assigns it, before the load question.
        """
        from print_handy.runtime import spool_sync
        if slot not in (1, 2, 3, 4):
            await interaction.response.send_message('That prompt has no usable slot. Nothing changed.', ephemeral=True)
            return
        # Acknowledge before the Spoolman reads (3 s window).
        await interaction.response.defer()
        try:
            types = await asyncio.to_thread(spool_sync.filament_types)
        except Exception:
            logger.exception('new spool walkthrough could not read Spoolman')
            await interaction.followup.send('Spoolman could not be reached, so nothing was changed. Check that Spoolman is running, then try again.', ephemeral=True)
            return
        old_spool_id = old_remaining = None
        if origin == 'insertion':
            try:
                state_now = await asyncio.to_thread(spool_sync.load_state)
                record = (state_now.get('filament_slots') or {}).get(str(slot)) or {}
                old_spool_id = record.get('spoolman_id')
                old_remaining = record.get('remaining_grams')
            except Exception:
                logger.debug('intake could not read slot state', exc_info=True)
        state = {'origin': origin, 'slot': slot, 'context': context or {}, 'step': 'start',
                 'predicted_id': None, 'prompt_brand': _prompt_brand(),
                 'types': types[:100], 'brand': None, 'material': None, 'color': None,
                 'grams': None, 'old_spool_id': old_spool_id, 'old_remaining': old_remaining,
                 'disposition': None, 'photo_offered': _photo_intake_ready(),
                 'from_photo': False, 'photo_route': None,
                 'nozzle_temp': None, 'bed_temp': None, 'hex': None, 'color_confirmed': False}
        await intake_advance(interaction, state, deferred=True)

    async def intake_post_step(channel, state):
        """Post the walkthrough's current step as a new channel message.

        The photo path (#514) continues outside an interaction — the photo
        arrives as a plain message — so its next step is a fresh post
        carrying fresh buttons, in the modal-submit idiom.
        """
        aid = await asyncio.to_thread(pab.record, 'intake', '', **state)
        await channel.send(pab.intake_step_text(state),
                           view=intake_view(pab.intake_step_components(aid, state)))

    _photo_tasks = set()

    async def intake_photo_wait(interaction, state):
        """Wait for the operator's label photo and pre-fill the walkthrough (#514).

        Runs as a background task after the photo button's click (the click
        itself only swaps the prompt text). The photo is the operator's next
        message in this channel with an image attached; its bytes go only
        to the extraction routes (_spool_label_routes) — nothing is saved
        or recorded. A readable label lands the walkthrough on its
        disposition or confirmation step with every extracted value shown;
        an unusable one refuses with the named reason and re-posts the
        manual steps.
        """
        from print_handy.runtime import spool_label

        def is_image(attachment):
            if str(getattr(attachment, 'content_type', '') or '').startswith('image/'):
                return True
            return str(getattr(attachment, 'filename', '') or '').lower().endswith(
                ('.jpg', '.jpeg', '.png', '.webp', '.gif'))

        channel = getattr(interaction, 'channel', None)
        user_id = str(getattr(getattr(interaction, 'user', None), 'id', ''))
        channel_id = str(getattr(interaction, 'channel_id', '') or '')

        def check(message):
            if str(getattr(getattr(message, 'author', None), 'id', '')) != user_id:
                return False
            if str(getattr(getattr(message, 'channel', None), 'id', '') or '') != channel_id:
                return False
            return any(is_image(a) for a in (getattr(message, 'attachments', None) or []))

        timeout = (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800)
        try:
            message = await bot.wait_for('message', check=check, timeout=timeout)
        except Exception:
            logger.debug('label photo wait ended without a photo', exc_info=True)
            with contextlib.suppress(Exception):
                if channel is not None:
                    await channel.send('No photo arrived, so nothing was changed. '
                                       'Start again from the prompt whenever you are ready.')
            return
        if channel is None:
            channel = getattr(message, 'channel', None)
        if channel is None:
            return
        attachment = next(a for a in (getattr(message, 'attachments', None) or []) if is_image(a))
        try:
            image = await attachment.read()
            spec, route = await asyncio.to_thread(
                spool_label.extract_spool_spec_any, image, _spool_label_routes(state))
        except spool_label.SpoolLabelError as exc:
            refusal = str(exc)
        except Exception:
            logger.exception('label photo extraction failed')
            refusal = spool_label.FAILURE_MESSAGES['unreadable']
        else:
            refusal = None
        if refusal is not None:
            await channel.send(refusal)
            fresh = {**state, 'step': 'start', 'from_photo': False, 'photo_route': None,
                     'brand': None, 'material': None, 'color': None, 'grams': None,
                     'nozzle_temp': None, 'bed_temp': None}
            await intake_post_step(channel, {**fresh, 'step': pab.intake_next_step(fresh)})
            return
        filled = {**state, 'brand': spec['brand'], 'material': spec['material'],
                  'color': spec['color'], 'grams': spec['grams'],
                  'nozzle_temp': spec['nozzle_temp'], 'bed_temp': spec['bed_temp'],
                  'from_photo': True, 'photo_route': route, 'step': 'weight'}
        await intake_post_step(channel, {**filled, 'step': pab.intake_next_step(filled)})

    async def intake_field_modal(interaction, state, field, label, max_length=100):
        """The walkthrough's only typed inputs: one field, for Other/Custom choices."""
        import discord
        modal_aid = await asyncio.to_thread(pab.record, 'intake-modal', '', state=state, field=field)

        class IntakeFieldModal(discord.ui.Modal, title=label):
            def __init__(self):
                super().__init__(custom_id=f'pbintakemodal:{modal_aid}')
                self.item = discord.ui.TextInput(label=label, custom_id=field,
                                                 required=True, max_length=max_length)
                self.add_item(self.item)

            async def on_submit(self, modal_interaction):
                pending = await asyncio.to_thread(pab.take, modal_aid)
                if not pending:
                    await modal_interaction.response.send_message('That form is old or was already submitted. Nothing changed.', ephemeral=True)
                    return
                new_state = dict(pending['state'])
                value = str(self.item.value or '').strip()
                if pending['field'] == 'grams':
                    try:
                        grams = float(re.sub(r'[^0-9.]', '', value))
                    except ValueError:
                        grams = 0
                    if grams <= 0:
                        await modal_interaction.response.send_message('That weight is not usable. Nothing changed — start again from the prompt.', ephemeral=True)
                        return
                    new_state['grams'] = round(grams, 1)
                else:
                    if not value:
                        await modal_interaction.response.send_message('That answer was empty. Nothing changed — start again from the prompt.', ephemeral=True)
                        return
                    try:
                        value = pab.intake_clean_typed(pending['field'], value, new_state)
                    except ValueError as exc:
                        await modal_interaction.response.send_message(f'{exc} Nothing changed — start again from the prompt.', ephemeral=True)
                        return
                    if pab.intake_typed_is_new(pending['field'], value, new_state):
                        # Not in the picker: ask explicitly before Spoolman
                        # gets a brand or color it has never seen.
                        new_state = {**new_state, 'step': 'newcheck', 'new_field': pending['field'],
                                     'new_value': value}
                        aid = await asyncio.to_thread(pab.record, 'intake', '', **new_state)
                        await modal_interaction.response.send_message(
                            pab.intake_step_text(new_state),
                            view=intake_view(pab.intake_step_components(aid, new_state)))
                        return
                    new_state[pending['field']] = value
                    if pending['field'] == 'color':
                        # A typed color supersedes any swatch picked
                        # earlier (P2-233): create_spool records the hex
                        # over the name, so a stale swatch would silently
                        # become the new color's code. Clearing it lets the
                        # name derive its own — the select path's color
                        # branch already does exactly this.
                        new_state['hex'] = None
                        new_state['color_confirmed'] = False
                # A modal submission answers with a fresh message carrying the next step.
                new_state = {**new_state, 'step': pab.intake_next_step(new_state)}
                aid = await asyncio.to_thread(pab.record, 'intake', '', **new_state)
                await modal_interaction.response.send_message(
                    pab.intake_step_text(new_state),
                    view=intake_view(pab.intake_step_components(aid, new_state)))

        await interaction.response.send_modal(IntakeFieldModal())

    async def intake_finish(interaction, state):
        """The walkthrough's confirmation: create + bind, once, inventory-only."""
        # Acknowledge before the bind (P2-191): the Spoolman/Moonraker calls
        # inside can outlast Discord's 3 s initial-response window, after
        # which edit_message would fail and leave the consumed prompt's
        # buttons standing over a completed bind.
        await interaction.response.defer()
        try:
            from print_handy.runtime import spool_sync
            values = {'brand': state.get('brand') or '', 'material': state.get('material') or '',
                      'color': state.get('color') or '', 'grams': str(state.get('grams') or ''), 'lot': '',
                      'nozzle_temp': state.get('nozzle_temp'), 'bed_temp': state.get('bed_temp'),
                      'hex': state.get('hex')}
            if state.get('origin') == 'spool-question':
                from print_handy.runtime import moonraker_event_listener as listener
                choice = {'status': 'new', 'brand': values['brand'] or None, 'material': values['material'],
                          'color': values['color'], 'grams': values['grams'], 'lot': None,
                          'nozzle_temp': values['nozzle_temp'], 'bed_temp': values['bed_temp'],
                          'hex': values['hex'],
                          # The walkthrough's confirmation step named this
                          # color (and its colorcheck step, when the parse
                          # was suspicious, confirmed it explicitly).
                          'color_confirmed': True}
                result = await asyncio.to_thread(_answer_spool_question, listener, spool_sync,
                                                 state.get('context') or {}, choice)
            else:
                result = await asyncio.to_thread(pab.create_and_bind_insertion, state['slot'], values,
                                                 spool_sync, state.get('disposition') or 'shelf',
                                                 expected_prior_spool_id=state.get('old_spool_id'))
        except Exception as exc:
            logger.exception('new spool walkthrough create failed')
            # A SpoolError is written for the operator and states what
            # actually happened — including a spool that WAS created before
            # a bind refusal (P2-204); the blanket line would deny it.
            detail = str(exc) if isinstance(exc, spool_sync.SpoolError) else None
            await interaction.edit_original_response(
                content=detail or 'Nothing was changed. Check what you entered and try again.', view=None)
            return
        await interaction.edit_original_response(content=result, view=None)

    async def handle_intake(interaction, action, aid):
        alert = await asyncio.to_thread(pab.peek, aid)
        if not alert or alert.get('kind') != 'intake':
            await interaction.response.send_message('That new-spool walkthrough is old or was already answered. Nothing changed.', ephemeral=True)
            return
        state = {key: alert.get(key) for key in pab.INTAKE_STATE_KEYS}
        # Claim the step before acting on it: two clicks can both peek the
        # same alert, but only one take() succeeds. Without this check a
        # double confirmation would run intake_finish twice and create/bind
        # two spools (#515) — the plate picker's claim idiom.
        if not await asyncio.to_thread(pab.take, aid):  # a parallel click got there first
            await interaction.response.send_message('That new-spool walkthrough is old or was already answered. Nothing changed.', ephemeral=True)
            return
        if action == 'cancel':
            await interaction.response.edit_message(content='New spool cancelled. Nothing changed.', view=None)
            return
        if action == 'mat_other':
            await intake_field_modal(interaction, state, 'material', 'Material')
            return
        if action == 'w_custom':
            await intake_field_modal(interaction, state, 'grams', 'Full starting grams', max_length=12)
            return
        if action == 'brand_type':
            await intake_field_modal(interaction, state, 'brand', 'Brand')
            return
        if action == 'photo':
            await interaction.response.edit_message(
                content=('Send one photo of the spool label — the box, or the sticker on the '
                         'spool — as your next message in this channel. I will read it (the '
                         "label's own barcode/QR code first, then the vision endpoint or local "
                         'OCR) and show you exactly what I found before anything is created. '
                         'The photo is not saved.'), view=None)
            task = asyncio.create_task(intake_photo_wait(interaction, state))
            _photo_tasks.add(task)
            task.add_done_callback(_photo_tasks.discard)
            return
        if action == 'new_ok':
            # The explicit yes for a brand or color that is not in the picker.
            field, value = state.get('new_field'), state.get('new_value')
            state = {**state, field: value, 'new_field': None, 'new_value': None,
                     'step': 'brand' if field == 'brand' else 'color'}
            if field == 'color':
                state.update(hex=None, color_confirmed=False)
            await intake_advance(interaction, state)
            return
        if action == 'new_fix':
            field = state.get('new_field')
            await intake_field_modal(interaction, {**state, 'step': 'brand' if field == 'brand' else 'color',
                                                   'new_field': None, 'new_value': None},
                                     field, 'Brand' if field == 'brand' else 'Color / finish')
            return
        if action == 'color_ok':
            # The confirm-or-correct step (#792 part 2): the operator
            # confirmed the parsed color exactly as labeled.
            state['color_confirmed'] = True
            await intake_advance(interaction, state)
            return
        if action == 'color_fix':
            # Correct it instead: back to the color step, color cleared so
            # the new pick/typing starts the sanity check afresh.
            await intake_show_step(interaction, {**state, 'step': 'color', 'color': None,
                                                 'hex': None, 'color_confirmed': False})
            return
        if action == 'edit':
            # Switching to manual entry discards the photo-extracted
            # temperatures (P2-198): the walkthrough has no step that can
            # edit them, so keeping them would save a possibly misread
            # label's values on the new filament, overriding the defaults
            # of the material the operator is about to pick. (The photo
            # refusal path already restarts with them cleared.)
            await intake_advance(interaction, {**state, 'step': 'start',
                                               'nozzle_temp': None, 'bed_temp': None})
            return
        material = pab.intake_material_for_action(action)
        if material is not None:
            state['material'] = material
        weight = pab.intake_weight_for_action(action)
        if weight is not None:
            state['grams'] = weight
        disposition = pab.intake_disposition_for_action(action)
        if disposition is not None:
            state['disposition'] = disposition
        if action == 'brand_skip':
            state['brand'] = None
        if action == 'confirm':
            await intake_finish(interaction, state)
            return
        await intake_advance(interaction, state)

    async def handle_intake_select(interaction, aid):
        alert = await asyncio.to_thread(pab.take, aid)
        if not alert or alert.get('kind') != 'intake':
            await interaction.response.send_message('That choice is old or was already used. Nothing changed.', ephemeral=True)
            return
        state = {key: alert.get(key) for key in pab.INTAKE_STATE_KEYS}
        value = str((getattr(interaction, 'data', {}) or {}).get('values', [''])[0] or '')
        step = state.get('step')
        if step == 'material' and value == pab.INTAKE_OTHER:
            await intake_field_modal(interaction, state, 'material', 'Material')
            return
        if value == pab.INTAKE_OTHER:
            field = 'brand' if step == 'brand' else 'color'
            await intake_field_modal(interaction, state, field, 'Brand' if field == 'brand' else 'Color / finish')
            return
        if step == 'material':
            subtypes = pab.intake_subtype_options(value)
            if len(subtypes) > 1:
                # PLA has kinds: ask which before anything else.
                await intake_show_step(interaction, {**state, 'primary': value, 'step': 'subtype'})
                return
            state['material'] = subtypes[0]['value'] if subtypes else value
        elif step == 'subtype':
            state['material'] = value
        elif step == 'brand':
            state['brand'] = None if value == pab.INTAKE_SKIP else value
        elif state.get('step') == 'swatch':
            state['hex'] = value  # the closest color, picked on a button; never typed hex
        else:
            state['color'] = value
            state['hex'] = None  # a new color name is read afresh
            state['color_confirmed'] = False  # and its parse is sanity-checked afresh
        await intake_advance(interaction, state)

    async def spool_question_select(interaction, alert):
        import discord
        from print_handy.runtime import spool_sync
        context = alert.get('context') or {}
        slot = context.get('slot')
        if slot not in (1, 2, 3, 4):
            await interaction.response.send_message('That spool question has no usable slot. Nothing changed.', ephemeral=True)
            return
        # Acknowledge before the Spoolman reads (3 s window).
        await interaction.response.defer()
        state = await asyncio.to_thread(spool_sync.load_state)
        mapped = (state.get('filament_slots') or {}).get(str(slot)) or {}
        loaded_ids = {int(record.get('spoolman_id')) for key, record in (state.get('filament_slots') or {}).items()
                      if str(key) != str(slot) and isinstance(record, dict) and record.get('spoolman_id') is not None}
        try:
            spools = await asyncio.to_thread(spool_sync.spoolman, 'GET', '/api/v1/spool?allow_archived=false', None)
        except Exception as exc:
            await interaction.followup.send('The shelf spools could not be loaded, so nothing was changed. Check that Spoolman is running, then try again.', ephemeral=True)
            return
        options = pab.insertion_select_options(spools, mapped, loaded_ids)
        if not options:
            await interaction.followup.send('No live shelf spools are available. Nothing changed.', ephemeral=True)
            return
        select_aid = await asyncio.to_thread(pab.record, 'spool-question-select', '', context=context)
        select = discord.ui.Select(placeholder='Choose the replacement shelf spool', min_values=1, max_values=1,
                                   options=[discord.SelectOption(**option) for option in options],
                                   custom_id=f'pbspoolselect:{select_aid}')
        view = discord.ui.View(timeout=(globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
        view.add_item(select)
        await interaction.edit_original_response(content='Choose the shelf spool to charge for the rest of this print. Nothing changes until you confirm the bind.', view=view)

    async def spool_question_modal(interaction, alert):
        """'New spool' on a paused spool question opens the same walkthrough (#512)."""
        context = alert.get('context') or {}
        if context.get('slot') not in (1, 2, 3, 4):
            await interaction.response.send_message('That spool question has no usable slot. Nothing changed.', ephemeral=True)
            return
        await intake_start(interaction, 'spool-question', context['slot'], context)

    async def handle_spool_question(interaction, action, aid):
        alert = await asyncio.to_thread(pab.take, aid)
        if not alert or alert.get('kind') != 'spool-question':
            await interaction.response.send_message('That spool question is old or was already answered. Nothing changed.', ephemeral=True)
            return
        try:
            await interaction.message.edit(view=None)
        except Exception:
            logger.debug('could not strip spool-question buttons', exc_info=True)
        if action == 'cancel':
            # Acknowledge before waiting on the printer (P2-164):
            # print_stop.cancel can poll for ~20 s, far past Discord's 3 s
            # initial-response window, so the outcome goes by follow-up —
            # the same shape as the generic alert handler.
            await interaction.response.send_message(
                f"{getattr(interaction.user, 'display_name', 'Someone')} tapped Cancel print. "
                'Checking the printer first.')
            try:
                from print_handy.runtime import moonraker_event_listener as listener
                from print_handy.runtime import spool_sync
                result = await asyncio.to_thread(_cancel_spool_question_print, listener, spool_sync,
                                                 alert.get('context') or {})
            except Exception as exc:
                logger.exception('spool-question cancel failed')
                await interaction.followup.send(
                    'The print was not cancelled. The spool question is still open — answer it '
                    'in text (a spool number, `same`, or `cancel the print`); the next reminder '
                    're-posts its buttons.', ephemeral=True)
                return
            await interaction.followup.send(result)
            return
        if action == 'existing':
            await spool_question_select(interaction, alert)
            return
        if action == 'new':
            await spool_question_modal(interaction, alert)
            return
        # Same ack-first shape as cancel: the Spoolman answer can outlast
        # the 3 s window too (the #594 phase 2 gap recorded for both). The
        # ack names the button that was actually tapped (#1063): after a
        # runout, "same" mints a NEW roll of the same filament, and the
        # old hard-coded "Same spool" ack said the opposite of the action
        # it acknowledged. "Refeed" is the true same-physical-spool answer.
        context = alert.get('context') or {}
        if action == 'refeed':
            tapped, status = 'Same spool — fix feed or jam', 'refeed'
        elif context.get('cause') == 'runout':
            tapped, status = 'New roll, same filament', 'same'
        else:
            tapped, status = 'Same spool', 'same'
        await interaction.response.send_message(
            f"{getattr(interaction.user, 'display_name', 'Someone')} tapped {tapped}. "
            'Checking the printer first.')
        try:
            from print_handy.runtime import moonraker_event_listener as listener
            from print_handy.runtime import spool_sync
            result = await asyncio.to_thread(_answer_spool_question, listener, spool_sync,
                                             context, {'status': status})
        except Exception as exc:
            logger.exception('same spool-question answer failed')
            # A Same answer creates the fresh roll before binding it: when the
            # bind fails, name the created spool instead of "nothing changed",
            # or the retry it invites adds another roll (review of #878).
            await interaction.followup.send(
                str(exc) if getattr(exc, 'spool_created', None)
                else 'Nothing was changed. Check what you entered and try again.', ephemeral=True)
            return
        await interaction.followup.send(result)

    async def bind_confirm_show(interaction, *, origin, slot, spool_id, context=None):
        """The confirm step after a spool picker selection (#792 part 2).

        Selecting a spool never binds it: this message names the exact
        spool (color · material · #id · grams left, re-read from Spoolman)
        and the displaced spool's fate — shelf by default, depleted only
        for a runout question's spent spool — and only its Confirm bind
        button writes anything.
        """
        from print_handy.runtime import spool_sync
        from print_handy.runtime.discord_print_summary import spool_picker_label
        # Acknowledge before the Spoolman re-read (3 s window).
        await interaction.response.defer()
        label = spool_picker_label(None, None, spool_id, None)
        try:
            spool = await asyncio.to_thread(spool_sync.spoolman, 'GET', f'/api/v1/spool/{int(spool_id)}', None)
            filament = (spool or {}).get('filament') or {}
            label = spool_picker_label(filament.get('name'), filament.get('material'),
                                       spool_id, (spool or {}).get('remaining_weight'))
        except Exception:
            logger.debug('bind confirmation could not re-read the spool', exc_info=True)
        old_id = None
        if origin == 'insertion':
            try:
                state = await asyncio.to_thread(spool_sync.load_state)
                old_id = ((state.get('filament_slots') or {}).get(str(slot)) or {}).get('spoolman_id')
            except Exception:
                logger.debug('bind confirmation could not read slot state', exc_info=True)
        else:
            old_id = (context or {}).get('prior_spool_id')
        fate = ''
        if old_id and int(old_id) != int(spool_id):
            fate = (f" Spool #{old_id} will be marked as depleted (0 g left)."
                    if origin == 'spool-question' and (context or {}).get('cause') == 'runout'
                    else f" Spool #{old_id} goes back on the shelf.")
        confirm_aid = await asyncio.to_thread(
            pab.record, 'bind-confirm', '', origin=origin, slot=int(slot), spool_id=int(spool_id),
            label=label, context=context or {},
            old_spool_id=int(old_id) if old_id else None)
        if origin == 'insertion':
            content = f'Bind spool {label} to slot {slot}?{fate} Nothing changes until you confirm.'
        else:
            content = (f'Charge the rest of this print to spool {label} in slot {slot}?{fate} '
                       'Nothing changes until you confirm.')
        await interaction.edit_original_response(
            content=content, view=intake_view(pab.bind_confirm_components(confirm_aid)))

    async def handle_bind_confirm(interaction, action, aid):
        pending = await asyncio.to_thread(pab.take, aid)
        if not pending or pending.get('kind') != 'bind-confirm':
            await interaction.response.send_message('That bind confirmation is old or was already used. Nothing changed.', ephemeral=True)
            return
        if action == 'cancel':
            await interaction.response.edit_message(content='Bind cancelled. Nothing changed.', view=None)
            return
        # Acknowledge before the bind (P2-191): the Spoolman/Moonraker
        # calls inside can outlast Discord's 3 s initial-response window.
        await interaction.response.defer()
        try:
            from print_handy.runtime import spool_sync
            if pending.get('origin') == 'insertion':
                result = await asyncio.to_thread(
                    pab.bind_insertion_existing, pending['slot'], pending['spool_id'], spool_sync,
                    expected_prior_spool_id=(int(pending['old_spool_id'])
                                             if pending.get('old_spool_id') else None))
            else:
                from print_handy.runtime import moonraker_event_listener as listener
                result = await asyncio.to_thread(_answer_spool_question, listener, spool_sync,
                                                 pending['context'],
                                                 {'status': 'existing', 'spool_id': pending['spool_id']})
        except Exception as exc:
            logger.exception('bind confirmation failed')
            await interaction.followup.send('Nothing was changed. Check what you entered and try again.', ephemeral=True)
            return
        await interaction.edit_original_response(content=result, view=None)

    async def handle_spool_question_select(interaction, aid):
        pending = await asyncio.to_thread(pab.take, aid)
        if not pending or pending.get('kind') != 'spool-question-select':
            await interaction.response.send_message('That shelf choice is old or was already used. Nothing changed.', ephemeral=True)
            return
        try:
            spool_id = int((getattr(interaction, 'data', {}) or {}).get('values', [None])[0])
            context = pending['context'] or {}
            slot = context.get('slot')
            if slot not in (1, 2, 3, 4):
                raise ValueError('spool question has no usable slot')
        except Exception as exc:
            logger.exception('existing spool-question spool failed')
            await interaction.response.send_message('Nothing was changed. Check what you entered and try again.', ephemeral=True)
            return
        await bind_confirm_show(interaction, origin='spool-question', slot=slot,
                                spool_id=spool_id, context=context)

    async def handle_insertion(interaction, action, aid):
        alert = await asyncio.to_thread(pab.take, aid)
        if not alert or alert.get('kind') != 'insertion':
            await interaction.response.send_message('That insertion prompt is old or was already answered. Nothing changed.', ephemeral=True)
            return
        try:
            await interaction.message.edit(view=None)
        except Exception:
            logger.debug('could not strip insertion buttons', exc_info=True)
        slot = alert.get('slot')
        if action == 'same':
            await interaction.response.send_message(f'Slot {slot}: same spool confirmed. Nothing changed.')
        elif action == 'existing':
            await insertion_select(interaction, aid, alert)
        else:
            await intake_start(interaction, 'insertion', slot)

    async def handle_insertion_select(interaction, aid):
        pending = await asyncio.to_thread(pab.take, aid)
        if not pending or pending.get('kind') != 'insertion-select':
            await interaction.response.send_message('That shelf choice is old or was already used. Nothing changed.', ephemeral=True)
            return
        try:
            spool_id = int((getattr(interaction, 'data', {}) or {}).get('values', [None])[0])
        except Exception as exc:
            logger.exception('existing insertion spool failed')
            await interaction.response.send_message('Nothing was changed. Check what you entered and try again.', ephemeral=True)
            return
        await bind_confirm_show(interaction, origin='insertion', slot=pending['slot'],
                                spool_id=spool_id)

    async def new_spool_slot_select(interaction, spool_id, label):
        """The load step of the new-spool prompt (#483): pick a slot, preselected spool.

        Mirrors the guided /spool change flow's steps (slot → the old spool's
        fate → one confirmation → bind) with the replacement already chosen,
        in the click-layer idiom the insertion and spool-question prompts use:
        a gateway dispatch flow needs a live message source and session key,
        which a button press does not carry in either runtime.
        """
        import discord
        from print_handy.runtime import spool_sync
        state = await asyncio.to_thread(spool_sync.load_state)
        select_aid = await asyncio.to_thread(pab.record, 'spoolman-new-slot', '',
                                             spool_id=int(spool_id), label=label)
        select = discord.ui.Select(placeholder='Choose the slot for the new spool', min_values=1, max_values=1,
                                   options=[discord.SelectOption(**option)
                                            for option in pab.new_spool_slot_options(state)],
                                   custom_id=f'{pab.NEW_SPOOL_SELECT_PREFIX}:{select_aid}')
        view = discord.ui.View(timeout=(globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
        view.add_item(select)
        await interaction.response.edit_message(
            content=f'Which slot should spool {label} go into? Nothing changes until you confirm the load.',
            view=view)

    async def new_spool_confirm(interaction, spool_id, label, slot, disposition, fate='',
                                old_spool_id=None):
        import discord
        confirm_aid = await asyncio.to_thread(pab.record, 'spoolman-new-confirm', '',
                                              spool_id=int(spool_id), label=label, slot=int(slot),
                                              disposition=disposition,
                                              old_spool_id=int(old_spool_id) if old_spool_id else None)
        view = discord.ui.View(timeout=(globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
        view.add_item(discord.ui.Button(label='Confirm load', style=discord.ButtonStyle.success,
                                        custom_id=f'{pab.NEW_SPOOL_PREFIX}:confirm:{confirm_aid}'))
        view.add_item(discord.ui.Button(label='Cancel', style=discord.ButtonStyle.secondary,
                                        custom_id=f'{pab.NEW_SPOOL_PREFIX}:cancel:{confirm_aid}'))
        await interaction.response.edit_message(content=f'Load spool {label} into slot {slot}?{fate}', view=view)

    async def handle_new_spool(interaction, action, aid):
        alert = await asyncio.to_thread(pab.take, aid)
        if not alert or alert.get('kind') not in ('spoolman-new', 'spoolman-new-disposition',
                                                  'spoolman-new-confirm'):
            await interaction.response.send_message('That new-spool prompt is old or was already answered. Nothing changed.', ephemeral=True)
            return
        kind = alert.get('kind')
        spool_id = alert.get('spool_id')
        label = alert.get('label') or f'#{spool_id}'
        if kind == 'spoolman-new':
            if action == 'load':
                await new_spool_slot_select(interaction, spool_id, label)
                return
            # Shelf and Dismiss both just close the prompt: the watcher
            # recorded the spool as known when it prompted, so neither
            # answer — and no answer — is ever asked about twice.
            text = (f'Spool {label} stays on the shelf. Load it any time with '
                    f'`/spool change <slot> {spool_id}`.' if action == 'shelf' else
                    f'Dismissed — spool {label} stays on the shelf, and you will not be asked about it again.')
            await interaction.response.edit_message(content=text, view=None)
            return
        if action == 'cancel':
            await interaction.response.edit_message(
                content=f'Load cancelled. Spool {label} stays on the shelf; nothing changed.', view=None)
            return
        if kind == 'spoolman-new-disposition':
            # The action names the disposition itself (the #475 guided labels).
            old_id = alert.get('old_spool_id')
            fate = {'shelf': f' Spool #{old_id} goes back on the shelf.',
                    'archive': f' Spool #{old_id} will be archived.',
                    'depleted': f' Spool #{old_id} will be marked as depleted (0 g left).',
                    'delete': f' Spool #{old_id} will be DELETED from Spoolman.'}[action]
            await new_spool_confirm(interaction, spool_id, label, alert.get('slot'), action, fate,
                                    old_spool_id=old_id)
            return
        # spoolman-new-confirm + confirm: the one bind, through the same
        # spool_sync.bind_slot the guided /spool change flow ends in.
        # Acknowledge before the bind (P2-191), as in intake_finish.
        await interaction.response.defer()
        try:
            from print_handy.runtime import spool_sync
            result = await asyncio.to_thread(spool_sync.bind_slot, int(alert['slot']), int(spool_id),
                                             prior_disposition=alert.get('disposition') or 'shelf',
                                             expected_prior_spool_id=(int(alert['old_spool_id'])
                                                                      if alert.get('old_spool_id') else None),
                                             note='Bound from Discord new-spool prompt')
            message = spool_sync.bind_text(result)
        except Exception as exc:
            logger.exception('new spool load failed')
            await interaction.followup.send('Nothing was changed. Check the slot and spool number and try again.', ephemeral=True)
            return
        await interaction.edit_original_response(content=message, view=None)

    async def handle_new_spool_select(interaction, aid):
        pending = await asyncio.to_thread(pab.take, aid)
        if not pending or pending.get('kind') != 'spoolman-new-slot':
            await interaction.response.send_message('That slot choice is old or was already used. Nothing changed.', ephemeral=True)
            return
        import discord
        from print_handy.runtime import spool_sync
        spool_id = pending.get('spool_id')
        label = pending.get('label') or f'#{spool_id}'
        try:
            slot = int((getattr(interaction, 'data', {}) or {}).get('values', [None])[0])
        except (TypeError, ValueError):
            slot = None
        if slot not in (1, 2, 3, 4):
            await interaction.response.send_message('That slot choice is not usable. Nothing changed.', ephemeral=True)
            return
        state = await asyncio.to_thread(spool_sync.load_state)
        old_id = ((state.get('filament_slots') or {}).get(str(slot)) or {}).get('spoolman_id')
        if old_id and int(old_id) == int(spool_id):
            await interaction.response.edit_message(
                content=f'Slot {slot} already holds spool {label}. Nothing changed.', view=None)
            return
        if old_id:
            disp_aid = await asyncio.to_thread(pab.record, 'spoolman-new-disposition', '',
                                               spool_id=int(spool_id), label=label, slot=slot,
                                               old_spool_id=int(old_id))
            view = discord.ui.View(timeout=(globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
            for text, key in (('Back on the shelf', 'shelf'), ('Archive it', 'archive'),
                              ('Mark it as depleted', 'depleted'), ('Delete it', 'delete'),
                              ('Cancel', 'cancel')):
                view.add_item(discord.ui.Button(label=text, style=discord.ButtonStyle.secondary,
                                                custom_id=f'{pab.NEW_SPOOL_PREFIX}:{key}:{disp_aid}'))
            await interaction.response.edit_message(
                content=f'Slot {slot} still holds spool #{old_id}. What happens to spool #{old_id}?', view=view)
            return
        await new_spool_confirm(interaction, spool_id, label, slot, 'shelf')

    async def handle_plate_pick(interaction, aid):
        """Choosing a plate only prepares one Confirm button; nothing starts here."""
        import discord
        from print_handy.runtime import print_next
        alert = await asyncio.to_thread(pab.peek, aid)
        if not alert or alert.get('kind') != 'plate-picker':
            await interaction.response.send_message('That plate picker is old or was already used. Nothing started.', ephemeral=True)
            return
        try:
            selected = str((getattr(interaction, 'data', {}) or {}).get('values', [None])[0])
            part_id = pab.plate_part_id(alert, selected)
            target = print_next.resolve_next(print_next.load_plan(alert['plan_id']), part_id=part_id)
        except Exception as exc:
            await interaction.response.send_message(f'Cannot start that plate: {exc}. Nothing started.', ephemeral=True)
            return
        if not await asyncio.to_thread(pab.take, aid):  # a parallel pick got there first
            await interaction.response.send_message('That plate picker is old or was already used. Nothing started.', ephemeral=True)
            return
        confirm_aid = await asyncio.to_thread(pab.record, 'plate-confirm', '', plan_id=alert['plan_id'], part_id=part_id)
        view = discord.ui.View(timeout=(globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
        view.add_item(discord.ui.Button(label=pab.CONFIRM_LABEL, style=discord.ButtonStyle.success,
                                        custom_id=f'{pab.PLATE_PREFIX}:go:{confirm_aid}'))
        await interaction.response.edit_message(content=print_next.question_text(target), view=view)

    async def handle_plate_drop(interaction, aid):
        """Dismiss an empty-bed offer. Does not start a plate."""
        alert = await asyncio.to_thread(pab.take, aid)
        if not alert:
            await interaction.response.send_message('That offer is old or was already used. Nothing started.', ephemeral=True)
            return
        from print_handy.runtime import next_plate_offer
        with contextlib.suppress(Exception):
            await asyncio.to_thread(next_plate_offer.clear)
        await interaction.response.edit_message(content='Left the queue alone. Nothing started.', view=None)

    async def handle_plate_go(interaction, aid):
        """The one Confirm tap. The alert is single use and print_next.start re-checks the printer and claim."""
        from print_handy.runtime import print_next
        alert = await asyncio.to_thread(pab.take, aid)
        if not alert or alert.get('kind') != 'plate-confirm':
            await interaction.response.send_message('That confirmation is old or was already used. Nothing started.', ephemeral=True)
            return
        try:
            target = print_next.resolve_next(print_next.load_plan(alert['plan_id']), part_id=alert['part_id'])
        except Exception as exc:
            await interaction.response.edit_message(content=f'Cannot start that plate: {exc}. Nothing started.', view=None)
            return
        await interaction.response.edit_message(content=f"Preparing to start {target['label']}.", view=None)
        await run_plate_start(interaction, target, alert)

    async def run_plate_start(interaction, target, alert, accept_spool_change=False):
        """Start the plate once. A spool swapped since slicing gets one extra single-use button, not a retry."""
        import discord
        from print_handy.runtime import print_next
        view = None
        ok, detail = False, ''
        try:
            ok, detail = await print_next.start({**target, 'accept_spool_change': True} if accept_spool_change else target)
            message = None if ok else (f"Did not start {target['label']}. Not retrying. "
                                       f"`/print-next {alert['plan_id']} {alert['part_id']}` offers a fresh button.\n"
                                       + intake_plain_start_failure(detail))
            if not ok and not accept_spool_change and SPOOL_CHANGED in str(detail):
                spool_aid = await asyncio.to_thread(pab.record, 'plate-spool', '',
                                                    plan_id=alert['plan_id'], part_id=alert['part_id'])
                view = discord.ui.View(timeout=(globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
                view.add_item(discord.ui.Button(label=USE_LOADED_SPOOL, style=discord.ButtonStyle.success,
                                                custom_id=f'{pab.PLATE_PREFIX}:spool:{spool_aid}'))
                message = f"Did not start {target['label']}.\n{_spool_change_text(target, detail)}"
            elif not ok and _material_pending(detail):
                view, message = await _material_view(target, detail, f"Did not start {target['label']}.")
        except Exception as exc:
            logger.exception('plate picker start failed')
            message = _failure_post('Next plate', exc, 'Check the printer screen before retrying.', subsystem='printer')
        if ok:  # start accepted: the claim suppresses the listener's own post,
            # so announce and register the start here (P2-56).
            await _announce_command_start(lambda content: interaction.followup.send(content),
                                          target, detail, chat_id=str(getattr(interaction, 'channel_id', '') or '') or None)
        if message:
            await (interaction.followup.send(message, view=view) if view else interaction.followup.send(message))

    def _material_pending(detail):
        from print_handy.core import material_accept
        return material_accept.pending(detail)

    async def _material_view(target, detail, lead, view=None):
        """A single-use Print as sliced button for this exact G-code, and the warning text (#1336)."""
        import discord
        from print_handy.core import material_accept
        warnings = material_accept.pending(detail)
        keys = sorted(set(target.get('accept_material') or ()) | {w['key'] for w in warnings})
        stored = json.loads(json.dumps({k: v for k, v in target.items() if k != 'accept_material'}, default=str))
        material_aid = await asyncio.to_thread(pab.record, 'start-material', '', target=stored, keys=keys)
        view = view or discord.ui.View(timeout=(globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
        view.add_item(discord.ui.Button(label=material_accept.print_label(warnings), style=discord.ButtonStyle.danger,
                                        custom_id=f'{pab.PLATE_PREFIX}:material:{material_aid}'))
        return view, (f"{lead}\n{material_accept.question(warnings)}\n"
                      f"{material_accept.reslice_message(warnings)}")

    async def handle_plate_material(interaction, aid):
        """Single-use Print as sliced tap: the exact stored G-code, these warnings only; start() re-checks all."""
        from print_handy.core import material_accept
        from print_handy.runtime import print_next
        alert = await asyncio.to_thread(pab.take, aid)
        if not alert or alert.get('kind') != 'start-material' or not isinstance(alert.get('target'), dict):
            await interaction.response.send_message('That answer is old or was already used. Nothing started.', ephemeral=True)
            return
        target = {**alert['target'], 'accept_material': list(alert.get('keys') or [])}
        await interaction.response.edit_message(content='Preparing to start as sliced.', view=None)
        try:
            ok, detail = await print_next.start(target)
        except Exception as exc:
            logger.exception('print as sliced start failed')
            await interaction.followup.send(_failure_post('Print as sliced', exc, 'Check the printer screen before retrying.',
                                                          subsystem='printer'))
            return
        if ok:
            await _announce_command_start(lambda content: interaction.followup.send(content), target, detail,
                                          chat_id=str(getattr(interaction, 'channel_id', '') or '') or None)
            return
        if material_accept.pending(detail):
            view, text = await _material_view(target, detail, 'Did not start: another material warning.')
            await interaction.followup.send(text, view=view)
            return
        await interaction.followup.send('Did not start. Not retrying.\n' + intake_plain_start_failure(detail))

    async def handle_plate_spool(interaction, aid):
        """Single-use 'use the loaded spool' tap. start() still re-checks the printer, claim and live slot."""
        from print_handy.runtime import print_next
        alert = await asyncio.to_thread(pab.take, aid)
        if not alert or alert.get('kind') != 'plate-spool':
            await interaction.response.send_message('That spool confirmation is old or was already used. Nothing started.', ephemeral=True)
            return
        try:
            target = print_next.resolve_next(print_next.load_plan(alert['plan_id']), part_id=alert['part_id'])
        except Exception as exc:
            await interaction.response.edit_message(content=f'Cannot start that plate: {exc}. Nothing started.', view=None)
            return
        await interaction.response.edit_message(content=f"Preparing to start {target['label']} with the loaded spool.", view=None)
        await run_plate_start(interaction, target, alert, accept_spool_change=True)

    async def handle_intake_retry(interaction, receipt_id):
        """Retry this exact refused plate through print_next.start and all of its guards."""
        import discord
        from print_handy.runtime import print_alert_buttons as pab, print_next
        from print_handy.core import prepared_intake as intake
        receipt_path = _state_dir() / 'model-link-intakes' / f'{receipt_id}.json'

        def retry_view():
            view = discord.ui.View(timeout=(globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
            view.add_item(discord.ui.Button(label='Retry', style=discord.ButtonStyle.primary,
                                            custom_id=pab.retry_custom_id(receipt_id)))
            return view

        try:
            receipt = json.loads(receipt_path.read_text())
            target = receipt.get('retry_target')
            if (receipt.get('intake_id') != receipt_id or receipt.get('status') != 'START_REFUSED'
                    or receipt.get('uploaded') is True or receipt.get('started') is True
                    or not intake._safe_preupload_refusal(receipt.get('start_refusal_detail'))
                    or not isinstance(target, dict) or not target.get('gcode')
                    or not target.get('gcode_sha256')):
                raise ValueError('this is not a confirmed pre-upload refusal')
            if str(receipt.get('start_prompt_message_id') or '') != str(getattr(interaction.message, 'id', '') or ''):
                raise ValueError('this Retry button no longer matches the saved prompt')
        except Exception as exc:
            await interaction.response.edit_message(content=f'Retry unavailable: {exc}. Nothing started.', view=None)
            return

        await interaction.response.edit_message(content='Checking the printer again before retrying this prepared print.', view=None)
        try:
            ok, detail = await print_next.start(target)
        except Exception as exc:
            # An exception out of the start is not a refusal: print-now may already have uploaded or
            # started the plate, so it is uncertain, never a reinstalled Retry (Codex on #182).
            logger.exception('pre-upload retry failed')
            detail = f'UNCERTAIN: the start ended with an unexpected error: {exc}. The plate may already have uploaded or started.'
            ok = False
        receipt.update(status='STARTED' if ok else 'START_REFUSED', started=bool(ok),
                       uploaded=bool(ok), start_refusal_detail='' if ok else str(detail))
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        if ok:
            logger.info('retry start accepted plan=%s part=%s', target.get('plan_id'), target.get('part_id'))
            await interaction.message.edit(content='Print accepted by the printer.', view=None)
            return
        raw = intake._raw_preupload_refusal(detail)
        logger.warning('print retry refused detail=%s', intake.logs.redact(raw or detail))
        if intake._safe_preupload_refusal(detail):
            if _material_pending(detail):
                # Material warnings: Print as sliced beside Retry, never a hard block (#1336).
                view, text = await _material_view(target, detail, 'Did not start.', view=retry_view())
                await interaction.message.edit(content=text, view=view)
                return
            await interaction.message.edit(content=intake._launch_refusal_text(detail), view=retry_view())
        else:
            receipt.update(status='START_UNCERTAIN', uploaded=None, started=None,
                           start_refusal_detail=str(detail))
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
            await interaction.message.edit(content=(
                'The printer did not confirm the start result. Check its screen before taking action. '
                'Do not retry until the printer state is known.'), view=None)

    async def handle_exclude(interaction, action, aid):
        """One object: ask, then send. The last object uses the stop confirm. Nothing is sent on the ask."""
        import discord
        from print_handy.runtime import exclude_chat, print_stop
        from print_handy.runtime import print_control as pc
        timeout = (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600)
        if action == 'pick':
            alert = await asyncio.to_thread(pab.peek, aid)
            if not isinstance(alert, dict):
                await interaction.response.send_message('That object list is old or was already used. Nothing sent.', ephemeral=True)
                return
            try:
                index = (getattr(interaction, 'data', {}) or {}).get('values', [None])[0]
            except (TypeError, AttributeError):
                index = None
            # The live read is two printer calls: acknowledge first, inside Discord's 3 s window.
            await interaction.response.defer()
            step = await asyncio.to_thread(
                exclude_chat.prepare, alert, index, functools.partial(exclude_chat.read_live, alert.get('printer_id') or None))
            if step['step'] in ('stale', 'refuse'):
                await interaction.followup.send(step['text'], ephemeral=True)
                return
            if not await asyncio.to_thread(pab.take, aid):
                await interaction.followup.send('That object list is old or was already used. Nothing sent.', ephemeral=True)
                return
            if step['step'] == 'cancel_print':
                new_id = await asyncio.to_thread(
                    pab.record, 'exclude-cancel', step['filename'], printer_id=step.get('printer_id') or '')
                labels = ((print_stop.STOP_LABEL, 'stop', discord.ButtonStyle.danger),
                          (print_stop.KEEP_LABEL, 'keep', discord.ButtonStyle.secondary))
            else:
                new_id = await asyncio.to_thread(
                    pab.record, 'exclude-go', step['filename'], name=step['name'], names=step['names'],
                    printer_id=step.get('printer_id') or '')
                labels = (('Cancel this object', 'go', discord.ButtonStyle.danger),
                          ('Keep it', 'keep', discord.ButtonStyle.secondary))
            view = discord.ui.View(timeout=timeout)
            for label, act, style in labels:
                view.add_item(discord.ui.Button(
                    label=label[:80], style=style, custom_id=f'{exclude_chat.PREFIX}:{act}:{new_id}'))
            await interaction.edit_original_response(content=step['text'], view=view)
            return
        alert = await asyncio.to_thread(pab.take, aid)
        if not alert:
            await interaction.response.send_message('That confirm is old or was already used. Nothing sent.', ephemeral=True)
            return
        if action == 'keep':
            await interaction.response.edit_message(content='Kept printing. Nothing sent.', view=None)
            return
        printer_id = alert.get('printer_id') or None
        if action == 'stop':
            if alert.get('kind') != 'exclude-cancel':
                await interaction.response.edit_message(content='That confirm is not a stop. Nothing sent.', view=None)
                return
            await interaction.response.defer()
            try:
                from print_handy import printer_control
                _sent, message = await asyncio.to_thread(printer_control.for_printer(printer_id).cancel,
                                                         alert.get('filename') or '')
            except Exception as exc:
                logger.exception('exclude stop failed')
                message = f'Stop was not sent. The print was not cancelled. {exc}'
            await interaction.edit_original_response(content=message, view=None)
            return
        if action != 'go' or alert.get('kind') != 'exclude-go':
            await interaction.response.edit_message(content='That confirm is not an exclude. Nothing sent.', view=None)
            return

        def sender(script):
            pc._send('/printer/gcode/script', method='POST', data={'script': script},
                     timeout=30, printer_id=printer_id)
        await interaction.response.defer()
        result = await asyncio.to_thread(
            exclude_chat.confirm_send, alert, sender, functools.partial(exclude_chat.read_live, printer_id))
        await interaction.edit_original_response(content=result['text'], view=None)

    async def on_interaction(interaction):
        data = getattr(interaction, 'data', None) or {}
        custom_id = data.get('custom_id', '')
        if str(custom_id).startswith(_pi.RETRY_CUSTOM_ID):
            if not allowed(interaction):
                await reject(interaction)
                return
            receipt_id = pab.parse_retry_custom_id(custom_id)
            if not receipt_id:
                await interaction.response.send_message('That Retry button is invalid. Nothing started.', ephemeral=True)
                return
            await handle_intake_retry(interaction, receipt_id)
            return
        if str(custom_id).startswith(STOP_PREFIX):
            if not allowed(interaction):
                await reject(interaction)
                return
            await handle_stop(interaction, str(custom_id)[len(STOP_PREFIX):])
            return
        from print_handy.runtime import exclude_chat
        parsed_exclude = exclude_chat.parse_custom_id(custom_id)
        if parsed_exclude:
            if not allowed(interaction):
                await reject(interaction)
                return
            await handle_exclude(interaction, *parsed_exclude)
            return
        plate = pab.parse_plate_custom_id(custom_id)
        if plate:
            if not allowed(interaction):
                await reject(interaction)
                return
            await {'pick': handle_plate_pick, 'go': handle_plate_go,
                   'spool': handle_plate_spool, 'drop': handle_plate_drop,
                   'material': handle_plate_material}[plate[0]](interaction, plate[1])
            return
        bind_confirm = pab.parse_bind_confirm_custom_id(custom_id)
        if bind_confirm:
            if not allowed(interaction):
                await reject(interaction)
                return
            await handle_bind_confirm(interaction, *bind_confirm)
            return
        parsed = pab.parse_custom_id(custom_id)
        insertion = pab.parse_insertion_custom_id(custom_id)
        spool_question = pab.parse_spool_question_custom_id(custom_id)
        new_spool = pab.parse_new_spool_custom_id(custom_id)
        intake = pab.parse_intake_custom_id(custom_id)
        is_select = str(custom_id).startswith('pbinsertselect:')
        is_spool_select = str(custom_id).startswith('pbspoolselect:')
        is_new_spool_select = str(custom_id).startswith(f'{pab.NEW_SPOOL_SELECT_PREFIX}:')
        is_intake_select = str(custom_id).startswith(f'{pab.INTAKE_SELECT_PREFIX}:')
        if not parsed and not insertion and not spool_question and not new_spool and not intake \
                and not is_select and not is_spool_select and not is_new_spool_select \
                and not is_intake_select:
            return
        if not allowed(interaction):
            await reject(interaction)
            return
        if intake:
            await handle_intake(interaction, *intake)
            return
        if is_intake_select:
            _, aid = str(custom_id).split(':', 1)
            await handle_intake_select(interaction, aid)
            return
        if insertion:
            await handle_insertion(interaction, *insertion)
            return
        if spool_question:
            await handle_spool_question(interaction, *spool_question)
            return
        if new_spool:
            await handle_new_spool(interaction, *new_spool)
            return
        if is_select:
            _, aid = str(custom_id).split(':', 1)
            await handle_insertion_select(interaction, aid)
            return
        if is_spool_select:
            _, aid = str(custom_id).split(':', 1)
            await handle_spool_question_select(interaction, aid)
            return
        if is_new_spool_select:
            _, aid = str(custom_id).split(':', 1)
            await handle_new_spool_select(interaction, aid)
            return
        action, aid = parsed
        alert = await asyncio.to_thread(pab.take, aid)
        label = {'resume': 'Resume', 'pause': 'Pause', 'acknowledge': "It's fine",
                 'mute': "Stop asking for this print",
                 'recover': pab.RECOVER_LABEL}.get(action, 'Cancel')
        await interaction.response.send_message(
            f"{getattr(interaction.user, 'display_name', 'Someone')} tapped {label}. Checking the printer first.")
        try:
            await interaction.message.edit(view=None)
        except Exception:
            logger.debug('could not strip alert buttons', exc_info=True)
        try:
            result = await asyncio.to_thread(pab.handle_click, action, alert)
        except Exception as exc:
            logger.exception('alert button %s failed', action)
            result = _failure_post(label, exc, 'Check the printer screen. Not retrying.', subsystem='printer')
        await interaction.followup.send(result)

    bot.add_listener(on_interaction, 'on_interaction')


class _EditHost:
    def __getattr__(self, name):
        try:
            return globals()[name]
        except KeyError:
            raise AttributeError(name) from None
