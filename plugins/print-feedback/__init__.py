"""Truthful receipt feedback and print controls: the standalone bot's handler module.

The bot's Router (print_handy.bot.router) loads this module and calls register() with
itself as the context. Everything it needs lives in the Print Handy package:
print_handy.hermes (listener helpers, print control) and print_handy.core (the slicing
and print pipeline). Hosts, paths and temperatures come from the home configuration.
"""
import asyncio
import contextlib
import copy
import functools
import json
import logging
import os
import re
import sys
import time
import threading
from pathlib import Path
from collections import OrderedDict

logger = logging.getLogger(__name__)


def _make_package_importable():
    """The gateway does not run from the repo. When print_handy is not installed, use the repo
    this plugin symlink points into (plugins/print-feedback/__init__.py -> repo root)."""
    try:
        import print_handy  # noqa: F401
    except ImportError:
        root = str(Path(__file__).resolve().parents[2])
        if root not in sys.path:
            sys.path.insert(0, root)


_make_package_importable()
from print_handy.hermes import _config  # noqa: E402


def _hermes_home():
    return _config.hermes_home()


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
# Upload-wait freeze length. Read at use time so a settings override in tests is honored.
_upload_waits = {}


def _wait_key(source):
    return (getattr(source, 'profile', None), str(getattr(source, 'chat_id', '')),
            str(getattr(source, 'thread_id', '') or ''))


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


def _job_owner_pid(job_json):
    """The roster printer a JOB.json belongs to: its recorded printer_id, else the default printer."""
    try:
        from print_handy.core import model_job
        return model_job._job_printer_id(json.loads(Path(job_json).read_text()))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Shared channels (#536): a chat claimed by 2+ roster entries is served, but
# the channel decides no printer. Printer-bound handlers resolve the printer
# by asking, or from the job they act on — never by falling through to the
# default, which is what pid=None has always meant everywhere else.


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


async def upload_wait_dispatch(event, gateway, **kwargs):
    """Freeze a Discord intake while uploads arrive; only an explicit resume releases it."""
    if not _is_print_discord_event(event):
        return None
    source = event.source
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    text = str(getattr(event, 'text', '') or '').strip().casefold()
    key = _wait_key(source)
    now = time.monotonic()
    until = _upload_waits.get(key, 0)
    if until <= now:
        _upload_waits.pop(key, None)
        until = 0
    waiting = bool(re.search(r'\b(wait|hold|pause)\b.*\b(upload|attach|file)s?\b', text))
    resume = bool(re.search(r'\b(resume|continue|uploads? (?:are )?(?:done|complete|finished))\b', text))
    if waiting:
        _upload_waits[key] = now + (globals().get('_config').upload_wait_s() if globals().get('_config') else 3600)
        return {'action': 'skip', 'reason': 'upload wait active'}
    if until:
        if resume:
            _upload_waits.pop(key, None)
            return None
        return {'action': 'skip', 'reason': 'upload wait active; explicit resume required'}
    return None


# Setup-only mode answers strangers, so stray messages share one notice per chat.
SETUP_ONLY_NOTICE_S = 600
_SETUP_ONLY_NOTICED = {}


async def guided_dispatch(event, gateway, **kwargs):
    """/print-handy setup|debugging|status|cancel, plus plain replies to an open setup checklist (#162).

    One state machine (print_handy.bot.guided) serves both runtimes. Setup never collects a
    token, never writes configuration without the explicit `apply` reply, and never touches a
    printer; debugging is read-only and redacted.
    """
    if not _is_print_discord_event(event):
        return None
    source = event.source
    # Setup-only mode (bootstrap): no chat/user is configured yet, so the
    # normal authorization cannot pass anyone. Admission here is only
    # for the guided setup surface — guided.handle_text(setup_only=True)
    # enforces the bind-at-start rule and refuses every non-setup
    # command, and this dispatcher is the first router hook, so a
    # setup-only event never reaches another handler through it.
    setup_only = bool(getattr(gateway, 'setup_only', False))
    if not getattr(source, 'user_id', None) or \
            (not setup_only and not gateway._is_user_authorized_for_source(source)):
        return None
    text = str(getattr(event, 'text', '') or '').strip()
    from print_handy.bot import guided
    key = f"{source.chat_id}:{source.thread_id or ''}:{source.user_id}"
    store = guided.SetupStore(_state_dir() / guided.STORE_NAME)
    if not text.startswith('/print-handy'):
        session = store.get(key)
        if not setup_only and (not session or session.get('status') not in ('in_progress', 'awaiting_consent',
                                                        'onboarding', 'onboard_review')):
            return None
    adapter = _adapter_for(gateway, source)
    if adapter is None:
        return {'action': 'skip', 'reason': 'guided UI delivery unavailable'}
    if setup_only and not text.startswith('/print-handy'):
        # A stray message from someone outside the setup conversation gets
        # one notice per chat, not a reply to everything said there.
        active = store.get(key)
        if not active or active.get('status') not in ('in_progress', 'awaiting_consent', 'adding_printer'):
            noticed_at = _SETUP_ONLY_NOTICED.get(str(source.chat_id))
            moment = time.monotonic()
            if noticed_at is not None and moment - noticed_at < SETUP_ONLY_NOTICE_S:
                return {'action': 'skip', 'reason': 'setup-only notice throttled'}
            _SETUP_ONLY_NOTICED[str(source.chat_id)] = moment

    def report():
        cfg = _config.cfg()
        token = _config.discord_token()
        return guided.debugging_report(loader=lambda: cfg,
                                       token_getter=lambda _cfg: token,
                                       secrets=[token] if token else [])

    def bundle():
        from print_handy import diagnostics_bundle
        target = diagnostics_bundle.write_bundle(_state_dir() / diagnostics_bundle.BUNDLE_NAME,
                                                 data=_config.cfg())
        # #1020 C1: the bundle is uploaded in the chat (the MEDIA: marker
        # the delivery layer turns into a file attachment), not only
        # written to the state folder.
        return (f"**Diagnostics bundle**: `{target}`\n"
                "It contains versions, the runtime status, a redacted configuration "
                "summary (no secret values), the printer roster, the doctor output "
                "and a bounded recent-log tail. Review it, then attach it to your "
                f"bug report.\nMEDIA:{target}")

    def debug(action):
        """#478 debug log mode: on/off/status over the shared state file.

        ``off`` delivers the session's sanitized export in chat (the
        MEDIA: marker the delivery layer turns into an attachment), with
        the counts-only sanitization report shown first. A failed export
        answers plainly with a PB marker instead of dropping silently,
        and leaves the mode on so the operator can retry.
        """
        from print_handy import debug_mode
        cfg = _config.cfg()
        parts = (action or "").split()
        verb = parts[0] if parts else "status"
        if verb == "on":
            minutes = None
            if len(parts) > 1:
                try:
                    minutes = int(parts[1])
                except ValueError:
                    return (f"`{parts[1]}` is not a number of minutes; debug mode was not "
                            "started. Use `/print-handy debug on` (30 minutes) or "
                            "`/print-handy debug on 60`.")
            state = debug_mode.start(cfg, minutes=minutes)
            return (f"**Debug mode ON** — session `{state['session']}`, off in "
                    f"{state['minutes']} minute(s) [{debug_mode.MARKER}].\n"
                    "Reproduce the problem, then run `/print-handy debug off`: the sanitized "
                    "log export for this session will be posted here with its sanitization "
                    "report. Printing is unaffected — this is logging only.")
        if verb == "off":
            if debug_mode._read_state(cfg) is None and debug_mode.latest_session(cfg) is None:
                return "Debug mode is already off, and there is no debug session to export."
            try:
                target, report, ended = debug_mode.off_and_export(cfg)
            except Exception as exc:
                logger.exception('debug log export failed')
                from print_handy import diagnostics
                return diagnostics.operator_message(
                    "The debug log export failed, so nothing was attached.",
                    subsystem="debug", exc=exc,
                    remedy="Debug mode is still on and the capture is kept. Try "
                           "`/print-handy debug off` again, or on the host run "
                           "`python3 -m print_handy.debug_mode off`.")
            return (f"**Debug mode OFF** (session `{ended.get('session')}`).\n"
                    f"{report}\n"
                    "The sanitized export is attached below — review it, then attach it to "
                    "your bug report. Nothing was uploaded anywhere.\n"
                    f"MEDIA:{target}")
        if verb == "status":
            return debug_mode.status_text(cfg)
        return ("Use `/print-handy debug on [minutes]`, `/print-handy debug off`, "
                "or `/print-handy debug status`.")

    try:
        cfg_now = _config.cfg()
    except Exception:
        cfg_now = None
    try:
        existing = store.get(key)
    except Exception:
        existing = None
    # The token step names the variable of the platform being set up,
    # which is not always the one this chat is running on.
    token_env = guided.setup_token_env(cfg_now, existing, _platform_value(source))
    from print_handy.bot import webui_access
    can_delete_secrets = webui_access.deletion_supported(getattr(adapter, '_host', None) or adapter)
    # Was this message a Web UI password answer? Its session step says
    # so; the message is deleted right after the reply is sent, exactly
    # like a pasted login token.
    secret_step = False
    try:
        if existing and existing.get('status') == 'in_progress':
            flow = guided.steps(token_env)
            if 0 <= existing.get('step', 0) < len(flow):
                secret_step = flow[existing['step']].key in ('webui_password', 'webui_confirm')
    except Exception:
        secret_step = False
    try:
        reply = await asyncio.to_thread(guided.handle_text, key, text, store=store,
                                        token_env=token_env, report=report, bundle=bundle,
                                        debug=debug, setup_only=setup_only,
                                        access_data=cfg_now,
                                        can_delete_secrets=can_delete_secrets)
    except Exception:
        logger.exception('guided setup/debugging failed')
        reply = 'That guided step failed and nothing was changed. Try `/print-handy status`, or `/print-handy cancel` to pause setup.'
    if reply is None:
        return None
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=reply)
    if secret_step:
        await _delete_secret_message(event, source, adapter, label='Password')
    return {'action': 'skip', 'reason': 'guided setup/debugging handled'}


def _adapter_for(gateway, source):
    """The adapter a dispatch handler renders through: the host adapter."""
    return gateway._delivery_adapter_for(source)


def _design_root(cfg):
    from print_handy.core import design
    cache = (cfg.get('paths') or {}).get('cache')
    return design.sessions_root(cache) if cache else design.sessions_root(_hermes_home() / 'cache')


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
        from print_handy.hermes import print_control as _print_control
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


def _fetch_design_link(url, target_dir):
    """Seam over design.fetch_link_source so dispatch tests can fake the network."""
    from print_handy.core import design
    return design.fetch_link_source(url, target_dir)


def _design_file(event):
    """The single dropped .scad in this message (a design source), else None."""
    hits = [Path(p) for p in (getattr(event, 'media_urls', None) or [])
            if Path(str(p)).suffix.lower() == '.scad']
    return hits[0] if len(hits) == 1 and hits[0].is_file() else None


_DESIGN_CONTEXT_PHRASES = ("design this", "design it", "redesign this",
                           "edit this", "modify this", "change this")
_DESIGN_ASK_PHRASES = ("edit this", "modify this", "change this")


def _design_parse(text):
    """Classify a plain message as a design request: (kind, rest) or None.

    Kinds: 'create' (rest is the description), 'context' (a design/edit ask
    that needs a model — attached, linked or recent; rest is the instruction,
    possibly ''), 'ask' (a bare 'design me'/'help me design' with neither a
    description nor an obvious model). Word-boundaried, case-insensitive;
    commands and ordinary chat return None.
    """
    low = text.casefold()

    def rest_of(match):
        return re.sub(r"\s+", " ", text[:match.start()] + " " +
                      text[match.end():]).strip(" \t—–-:,.").strip()

    for phrase in ("help me design", "design me"):
        match = re.search(rf"\b{re.escape(phrase)}\b", low)
        if match:
            rest = rest_of(match)
            return ("create", rest) if rest else ("ask", "")
    for phrase in _DESIGN_CONTEXT_PHRASES:
        match = re.search(rf"\b{re.escape(phrase)}\b", low)
        if match:
            return ("context", rest_of(match))
    return None


def _recent_design_source(source):
    """(path, link) for the channel's recent model, else (None, None).

    The recent link wins (the same machinery "print it" uses); otherwise the
    newest archived job by record mtime whose source file is still on disk —
    a design-context recency hint, deliberately not "the last print" (#979).
    """
    url = _recent_model_link(source)
    if url:
        return None, url
    try:
        from print_handy.core.prepared_intake import newest_archived_job
        job = newest_archived_job()
    except Exception:
        job = None
    if job:
        path = Path(job.get('downloaded_file') or '')
        if path.is_file() and path.suffix.lower() in ('.stl', '.3mf', '.scad'):
            return path, None
    return None, None


_DESIGN_COMMANDS = ('/print-design', '/design')  # canonical + short alias (#388 close-out)


def _design_command_args(text):
    """The argument text when `text` is a design command, else None."""
    for command in _DESIGN_COMMANDS:
        if text == command:
            return ''
        if text.startswith(command + ' '):
            return text[len(command):].strip()
    return None


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


async def design_dispatch(event, gateway, **kwargs):
    """Serialize design turns per owner around _design_dispatch_turns (P2-151).

    A turn awaits the model (and then its Slice/Change/Cancel prompt) with
    the session's files as shared state. A second owner message arriving
    in that window used to load the same session and run a concurrent
    turn: both turns wrote the same SCAD/STL/preview files, and the first
    turn's still-open approval buttons could hand off the second turn's
    geometry. Hold a per-owner claim for the whole dispatch and refuse a
    concurrent one with a busy note; other commands and unrelated chat
    still fall through untouched.

    While the design's own question is open (phase "prompt"), a plain
    message is the answer to it — a typed choice, a Matrix number, or a
    change typed instead of pressing Change it — so it falls through to the
    bot core, which routes typed answers to the open prompt. Refusing it
    here used to swallow every typed answer with the busy note.
    """
    if not _is_print_discord_event(event):
        return None
    source = event.source
    owner = f"{source.chat_id}:{source.thread_id or ''}:{source.user_id}"
    if owner in _DESIGN_BUSY:
        text = str(getattr(event, 'text', '') or '').strip()
        if _design_command_args(text) is None and text.startswith('/'):
            return None  # another command: not design's to refuse
        if _DESIGN_BUSY.get(owner) == 'prompt' and _design_command_args(text) is None:
            # The answer to the open design question. Answer it here, the
            # way the bot core's typed-answer route would: returning None
            # let a later hook (edit_dispatch) claim "make it 5 mm taller"
            # or "undo" first and apply it to the channel's job (#1206).
            adapter = _adapter_for(gateway, source)
            answer_typed = getattr(adapter, 'answer_typed', None)
            if text and answer_typed is not None and await answer_typed(
                    gateway._session_key_for_source(source), text,
                    user_id=getattr(source, 'user_id', None)):
                return {'action': 'skip', 'reason': 'typed answer to the design question'}
            return None
        from print_handy.core import design as _design
        if _design_command_args(text) is None and not _design.find_session_for_owner(
                _design_root(_config.cfg()), owner):
            return None  # no live design session: not a design message
        adapter = _adapter_for(gateway, source)
        if adapter is None:
            return {'action': 'skip', 'reason': 'design turn already running'}
        metadata = {'thread_id': source.thread_id} if source.thread_id else None
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='A design turn is already running — let it finish, '
                                   'then send your change again. Nothing was changed.')
        return {'action': 'skip', 'reason': 'design turn already running'}
    _DESIGN_BUSY[owner] = 'turn'
    try:
        return await _design_dispatch_turns(event, gateway, **kwargs)
    finally:
        _DESIGN_BUSY.pop(owner, None)


async def _design_dispatch_turns(event, gateway, **kwargs):
    """/print-design (alias /design) and natural design requests (#388).

    Create loop (Stage 1): /print-design <what to make>, Slice it / Change
    it / Cancel, Slice it handing the STL to prepared intake. Stage 2 adds
    edit sessions — /print-design (or a design phrase) with a dropped
    .stl/.3mf/.scad or a model link in the same message, the same phrases
    against the channel's recent model, and create phrases ("design me …",
    "help me design …") — seeded through the engine's edit path. Routing is
    deterministic and narrow: no design phrase means this handler returns
    None and plain intake/chat behave exactly as before; inside a session
    awaiting a revision, the owner's next plain message is the revision
    before any phrase detection runs.

    Close-out rulings: the handler serves every bot platform — Discord,
    Telegram and Matrix — through the platform-neutral adapter/prompt path
    (Matrix answers the Slice it / Change it / Cancel choices as a numbered
    list, via the shared prompt machinery). Design is its own configured
    lane: on Telegram/Matrix it runs only through an explicitly configured
    [design] endpoint, and their pure-bot chat posture is untouched —
    ordinary chat there still has no [llm] fallback (BotCore keys that off
    the adapter's uses_llm, which design never changes). The enable switch
    is tri-state (design.effective_enabled): unset is auto-on when an
    endpoint resolves, explicit false is off in all cases.
    """
    if not _is_print_discord_event(event):
        return None
    source = event.source
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    text = str(getattr(event, 'text', '') or '').strip()
    command_args = _design_command_args(text)
    is_command = command_args is not None
    from print_handy.core import design
    from print_handy.core.link_intake import model_link
    from print_handy.core.prepared_intake import model_file
    cfg = _config.cfg()
    root = _design_root(cfg)
    owner = f"{source.chat_id}:{source.thread_id or ''}:{source.user_id}"
    session = design.find_session_for_owner(root, owner)

    # Resolve what this message asks for before any state changes.
    intent = None  # (kind, instruction, file_path, link_url)
    if is_command:
        intent = ("command", command_args, None, None)
    elif text.startswith('/'):
        return None
    elif session and session.get('status') == 'awaiting_revision':
        intent = ("revision", text, None, None)
    else:
        parsed = _design_parse(text)
        if parsed is None:
            return None
        kind, rest = parsed
        attached = model_file(event) or _design_file(event)
        linked = model_link(text)
        if attached or linked:
            intent = ("edit", rest, attached, linked)
        elif kind == "create":
            intent = ("create", rest, None, None)
        elif kind == "ask":
            recent_path, recent_link = _recent_design_source(source)
            if recent_path or recent_link:
                intent = ("edit", "", recent_path, recent_link)
            else:
                intent = ("ask", "", None, None)
        else:  # 'context': only the change-this family stays silent without a model
            recent_path, recent_link = _recent_design_source(source)
            if recent_path or recent_link:
                intent = ("edit", rest, recent_path, recent_link)
            elif any(re.search(rf"\b{re.escape(p)}\b", text.casefold())
                     for p in _DESIGN_ASK_PHRASES):
                return None  # "change this" with no model anywhere is not ours
            else:
                intent = ("ask", "", None, None)
    # Natural-language intents get the same answers as the command when
    # design cannot run (#388 criterion 7, catduck's ruling): the enabled
    # and transport checks below reply "not set up" / "not configured" and
    # consume the message. Messages that matched no trigger returned None
    # during intent resolution above, so non-design chat is untouched
    # whatever the design state.
    adapter = _adapter_for(gateway, source)
    if adapter is None:
        return {'action': 'skip', 'reason': 'design UI delivery unavailable'}
    metadata = {'thread_id': source.thread_id} if source.thread_id else None

    async def say(content):
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=content)

    complete, _kind = _design_transport(cfg)
    design_raw = (cfg.get('design') or {}).get('enabled')
    if design_raw is False:
        await say('Design is not set up: design.enabled is false — set design.enabled = true '
                  '(PRINT_HANDY__DESIGN__ENABLED=true) or remove it to turn design on when a '
                  'model is configured — see docs/DESIGN.md. Nothing was designed.')
        return {'action': 'skip', 'reason': 'design not enabled'}
    if complete is None:
        if design_raw is True:
            await say('Design is enabled but no design model is configured: set '
                      '[design] chat_url/model (or the [llm] values they inherit). '
                      'Nothing was designed.')
            return {'action': 'skip', 'reason': 'design model not configured'}
        # Auto posture with nothing configured anywhere: the same "not set
        # up" answer the command has always given (#422).
        await say('Design is not set up: configure a design model — set [design] chat_url/model '
                  '(or the [llm] values they inherit), or PRINT_HANDY__DESIGN__ENABLED=true with '
                  'a design endpoint — see docs/DESIGN.md. Nothing was designed.')
        return {'action': 'skip', 'reason': 'design not enabled'}
    engine = design.DesignEngine((cfg.get('paths') or {}).get('cache') or (_hermes_home() / 'cache'),
                                 complete, cfg=cfg)
    note = ''
    try:
        kind, instruction, file_path, link_url = intent
        if kind == "revision":
            # After Change it, a typed button word is that button, not a
            # change to send to the model (#1206): "never mind" cancels and
            # "slice it" hands the current version to intake.
            chosen = design.typed_choice(text)
            current_stl = (root / session['id'] / session['stl']) if session.get('stl') else None
            if chosen == design.CANCEL_LABEL:
                engine.cancel(session)
                await say('Design cancelled. The files stay in the session folder for a while; '
                          'nothing was sliced or started.')
                return {'action': 'skip', 'reason': 'design cancelled'}
            if chosen == design.SLICE_LABEL and current_stl is not None and current_stl.is_file():
                engine.mark_handed_off(session)
                return await _design_handoff(event, gateway, adapter, session, current_stl)
            if chosen == design.CHANGE_LABEL:
                await say('Say the change in your next message — e.g. "make it 5 mm taller".')
                return {'action': 'skip', 'reason': 'design awaiting revision'}
            result = await _run_design_turn(engine, session, text, adapter, source, metadata)
        elif kind == "ask":
            await say('Drop the file you want to work on, paste its model link, or tell me '
                      'what to make — e.g. "design me a 40 mm bracket with two holes". '
                      'Nothing was designed.')
            return {'action': 'skip', 'reason': 'design needs a model or a description'}
        else:
            if kind == "command":
                attached = model_file(event) or _design_file(event)
                linked = model_link(instruction)
                if attached or linked:
                    kind, file_path, link_url = "edit", attached, linked
                elif not instruction:
                    if session and session.get('status') == 'awaiting_revision':
                        await say('A design is waiting for your call: reply with the change you want, '
                                  'or /print-design <a new part> to start over.')
                    else:
                        await say('Usage: /print-design <what to make> — e.g. '
                                  '/print-design a 40x20x3 plate with two 4 mm holes 30 mm apart; '
                                  'or drop a model file / paste a link with the change you want.')
                    return {'action': 'skip', 'reason': 'design usage shown'}
                else:
                    kind = "create"
            if kind == "create":
                if session:
                    engine.cancel(session)  # a fresh design supersedes the old session
                session = engine.start_session(owner, source.chat_id, instruction)
                result = await _run_design_turn(engine, session, instruction, adapter, source,
                                              metadata)
            else:  # edit
                if link_url:
                    instruction = instruction.replace(link_url, '').strip(" \t—–-:,.")
                if file_path and link_url:
                    # An attached model wins over a link in the same message
                    # (P2-156): the operator supplied the exact source to
                    # edit, while fetching the link would silently edit the
                    # downloaded version instead (e.g. an edited .scad
                    # attached alongside its original model link).
                    link_url = None
                if session:
                    engine.cancel(session)  # a fresh design supersedes the old session
                description = instruction or f"Edit {(file_path.name if file_path else 'the linked model')}"
                if link_url:
                    session = await asyncio.to_thread(
                        engine.start_edit_session, owner, source.chat_id, description,
                        None, link_url=link_url,
                        fetcher=lambda url, folder: _fetch_design_link(url, folder))
                else:
                    session = await asyncio.to_thread(
                        engine.start_edit_session, owner, source.chat_id, description,
                        file_path)
                note = design.source_note(session)
                if instruction:
                    result = await _run_design_turn(engine, session, instruction, adapter, source,
                                              metadata)
                else:
                    # No change asked for: show the dropped model exactly as
                    # it is, with no model call in between (#841 gap 4).
                    result = await asyncio.to_thread(engine.baseline_turn, session)
    except design.DesignQuestion as asked:
        # The one clarifying question a design may ask, only when the answer
        # changes the part and cannot be assumed. The session waits; the
        # owner's next plain message is the answer and the design runs then.
        await say(f'{asked.question}\n(Answer in your next message — or say "you pick" and '
                  'I will choose a sensible size and tell you what I assumed.)')
        return {'action': 'skip', 'reason': 'design asked a question'}
    except design.DesignError as exc:
        await say(f'That design change did not work: {exc} Nothing was sliced or started.')
        return {'action': 'skip', 'reason': 'design turn failed'}
    except Exception:
        logger.exception('design turn failed unexpectedly')
        await say('That design change did not work, and I could not tell why from here. Nothing was sliced or started. '
                  'If it keeps happening, run `/print-handy bundle` and share the bundle so it can be looked into.')
        return {'action': 'skip', 'reason': 'design turn failed'}
    owner_phase_key = owner
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
            engine.mark_handed_off(session)
            return await _design_handoff(event, gateway, adapter, session, result['stl_path'])
        if answer == design.CHANGE_LABEL:
            engine.awaiting_revision(session)
            await say('Say the change in your next message — e.g. "make it 5 mm taller". '
                      f'({design.MAX_TURNS} turns per design; this one has had {session["turns"]}.)')
            return {'action': 'skip', 'reason': 'design awaiting revision'}
        typed = str(answer or '').strip()
        if answer is None or not typed or typed == design.CANCEL_LABEL:
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


async def color_selection_dispatch(event, gateway, **kwargs):
    """Explicit color command: real Discord UI, no physical or slicing actions."""
    source = getattr(event, 'source', None)
    text = str(getattr(event, 'text', '')).strip()
    if not text.startswith('/print-colors '):
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
        return {'action': 'skip', 'reason': 'color UI delivery unavailable'}
    from print_handy.core import printer_state
    from print_handy.core.discord_color_intake import collect
    from print_handy.core.link_intake import PromptCancelled
    from print_handy.core import model_job
    ARCHIVE = model_job.ARCHIVE
    path = Path(text[len('/print-colors '):]).expanduser().resolve()
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    key = ('color-ui', source.chat_id, source.thread_id)
    if key in _seen:
        return {'action': 'skip', 'reason': 'color selection already active'}
    _seen[key] = time.monotonic()
    try:
        if path.name != 'JOB.json' or not path.is_relative_to(ARCHIVE.resolve()):
            raise ValueError('color command requires an archived JOB.json')
        # Colors bind against the JOB's printer (#335): its recorded
        # printer_id picks the state record, never the channel's or the
        # configured printer's.
        try:
            job_pid = model_job._job_printer_id(json.loads(path.read_text()))
        except Exception:
            job_pid = None
        result = await collect(path, printer_state.load(**_pkw(job_pid)), adapter, source.chat_id,
                               f'print-colors:{source.chat_id}:{source.thread_id}', metadata)
        message = json.dumps(result)
    except PromptCancelled:
        message = 'Cancelled. No colors changed, nothing sliced, nothing started.'
    except Exception as exc:
        message = _failure_post('Color selection', exc, 'No slicing, upload or print started.', subsystem='intake')
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic color selection handled'}


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
        answer = await asyncio.to_thread(cm.wait_for_response, cid, (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
    finally:
        cm.resolve_gateway_clarify(cid, cm.CANCELLED)
        _prompt_marks.forget_approval(cid)
    if answer != USE_LOADED_SPOOL:
        return False, 'Cancelled. Nothing uploaded or started.'
    return await print_next.start({**target, 'accept_spool_change': True})


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
    from print_handy.hermes import discord_print_summary as print_summary
    from print_handy.hermes import print_next
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
    from print_handy.hermes import print_next
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
        from print_handy.hermes import discord_print_summary as print_summary
        job, plate = print_summary.find_job(target['gcode'])
        seconds = (plate or {}).get('seconds')
        # No number here: this plate gets its own new print number when it
        # starts, and the start announcement shows it.
        target['estimate_text'] = (f"Estimated print time ~{print_summary.human(seconds)} "
                                   f"plus warmup ~{print_summary.human(seconds + print_summary.WARMUP_S)} total"
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
            answer = await asyncio.to_thread(cm.wait_for_response, cid, (globals().get('_config').question_timeout_s() if globals().get('_config') else 1800))
        finally:
            cm.resolve_gateway_clarify(cid, cm.CANCELLED)
            _prompt_marks.forget_approval(cid)
        if answer != print_next.START_LABEL:
            message = 'Cancelled or expired. Nothing uploaded or started.'
        else:
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=f"Preparing to start {target['label']}. {target.get('estimate_text', 'Estimated print time unknown')}.")
            ok, detail = await print_next.start(target)
            if not ok and SPOOL_CHANGED in str(detail):
                ok, detail = await _offer_spool_change(adapter, source, metadata, print_next, target, detail,
                                                        live_key=live_key)
            if ok:
                await _announce_command_start(
                    lambda content: adapter.send(chat_id=source.chat_id, content=content, metadata=metadata),
                    target, detail, job=job, plate=plate,
                    chat_id=str(source.chat_id), thread_id=str(source.thread_id) if source.thread_id else None)
            message = (None if ok
                       else f"Did not start {target['label']}. Not retrying.\n"
                            + intake_plain_start_failure(detail))
            if not ok and _start_refused_needs_restart(detail):
                from print_handy.hermes import print_control as pc
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


def _discord_operator(event, gateway):
    """True for an authorized human Discord message in this profile."""
    source = getattr(event, 'source', None)
    if (source is None or not _is_bot_platform(source)
            or not _is_hosting_profile(getattr(source, 'profile', None))
            or getattr(event, 'internal', False) or getattr(source, 'is_bot', False)
            or getattr(source, 'profile_route_rejected', False)):
        return None
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    return source


def _maintenance_hours_now(printer_id=None):
    from print_handy.hermes import print_control
    payload = (print_control._send('/server/history/totals', printer_id=printer_id)
               if printer_id else print_control._call('/server/history/totals'))
    result = payload.get('result') or {}
    seconds = result.get('total_time', result.get('print_time'))
    if seconds is None:
        raise RuntimeError('Moonraker history totals did not include total_time or print_time')
    return float(seconds) / 3600.0


def _cold_pull_temps():
    """(pla_hot, pla_cold, petg_hot, petg_cold, pla_retry_cold) from [cold_pull] in the home configuration."""
    from print_handy.hermes import print_control as pc
    pla_hot, pla_cold = pc.COLD_PULL_PROFILES['pla_trial']
    petg_hot, petg_cold = pc.COLD_PULL_PROFILES['petg']
    return pla_hot, pla_cold, petg_hot, petg_cold, pla_cold + 5


async def _coldpull_workflow(adapter, source, metadata, *, source_tag, initial_cold=None, selected_material=None,
                             printer_id=None, live_key=None):
    """Shared user interaction for the standalone and maintenance cold-pull paths."""
    from print_handy.hermes import maintenance
    from print_handy.hermes import print_control as pc

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


async def coldpull_dispatch(event, gateway, **kwargs):
    source, adapter = _control_preamble(event, gateway, '/print-coldpull')
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'cold-pull UI delivery unavailable'}
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    key = ('coldpull', source.chat_id, source.thread_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content='A cold pull is already in progress. Nothing sent.')
        return {'action': 'skip', 'reason': 'cold-pull prompt already active'}
    _seen[key] = time.monotonic()
    try:
        decided = await _decide_printer(source, gateway, adapter, metadata,
                                        question='Cold pull on which printer?')
        if decided is None:
            return {'action': 'skip', 'reason': 'shared channel: no printer chosen for cold pull'}
        pid, chosen = decided
        _record, message = await _coldpull_workflow(adapter, source, metadata, source_tag='coldpull',
                                                    live_key=_live_key_for(source, gateway, chosen),
                                                    **_pkw(pid))
    except Exception as exc:
        logger.exception('/print-coldpull failed')
        message = 'The cold pull stopped early. Do not retry until the printer state is checked on its screen.'
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'cold pull handled'}


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


async def maintenance_dispatch(event, gateway, **kwargs):
    """Guided preventive maintenance checklist using the shared cold-pull path."""
    source, adapter = _control_preamble(event, gateway, '/print-maintenance', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'maintenance UI delivery unavailable'}
    import json as _json
    import uuid
    from print_handy.hermes import maintenance
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
        from print_handy.hermes import print_control as pc
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
    from print_handy.hermes import print_stop
    current = listener.load_runout_pause()
    if current is None:
        raise spool_sync.SpoolError('that spool question was already answered; nothing changed')
    _require_clicked_question_current(spool_sync, context, current)
    context = current
    filename = context.get('filename') or ''
    label = filename or 'the paused print'
    before = print_stop.status()
    if before.get('state') in print_stop.ACTIVE:
        _sent, detail = print_stop.cancel(filename)
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


async def runout_intake_dispatch(event, gateway, **kwargs):
    """Answer a pending filament-runout pause. Inventory writes only — except cancel.

    A generic pause leaves no pending record, so an ordinary message is ignored.
    Nothing here resumes, heats, or moves the printer. The one exception is an
    unambiguous typed cancel ("cancel the print"), which routes to the same
    print_stop.cancel path as the Cancel print button and /print-stop.
    """
    source = _discord_operator(event, gateway)
    text = str(getattr(event, 'text', '')).strip()
    if source is None or not text or text.startswith('/'):
        return None
    from print_handy.hermes import moonraker_event_listener as listener
    context = listener.load_runout_pause()
    if context is None:
        return None
    if _routed_printer_id(source, gateway):
        # The pending runout belongs to the listener's printer — the default
        # printer until the listener itself is per-printer (Stage 2d). A
        # routed channel must not answer it or bind its printer's slots.
        return None
    adapter = _adapter_for(gateway, source)
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    if listener.parse_cancel_print_reply(text):
        from print_handy.hermes import spool_sync
        try:
            message = await asyncio.to_thread(_cancel_spool_question_print, listener, spool_sync, context)
        except Exception as exc:
            logger.exception('spool question cancel failed')
            await adapter.send(chat_id=source.chat_id, metadata=metadata, content=(
                'The print was not cancelled. The spool question is still open and usage '
                'is still paused — answer it, or try cancelling again.'))
            return {'action': 'skip', 'reason': 'spool question cancel failed, question left open'}
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
        return {'action': 'skip', 'reason': 'spool question print cancelled'}
    choice = listener.parse_runout_reply(text)
    if text.strip().casefold() in ('same', 'same spool', 'no change'):
        choice = {'status': 'same'}
    if choice['status'] in ('need_choice', 'need_spool_number', 'need_new_fields', 'unparsed'):
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=(
            'Spool question is still open and usage is still paused. Reply with a spool number, '
            '`same filament` for a fresh roll of the run-out filament, `refeed` or `fix feed` '
            'if the same physical spool stays in and you cleared its stuck feed or jam, or '
            '`cancel the print` — or tap a button on the question in Discord. '
            'Nothing was changed.'))
        return {'action': 'skip', 'reason': 'spool reply needs a choice'}
    from print_handy.hermes import spool_sync
    try:
        message = await asyncio.to_thread(_answer_spool_question, listener, spool_sync, context, choice)
    except Exception as exc:
        logger.exception('spool question answer failed')
        # Only an answer that created a spool before failing says so; every
        # other failure changed nothing and keeps the pinned fail-closed copy.
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=str(exc) if getattr(exc, 'spool_created', None)
                           or getattr(exc, 'color_sanity', None) else
                           'That answer did not work, so nothing was changed. Usage is still paused; please answer again.')
        return {'action': 'skip', 'reason': 'spool answer failed closed'}
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'spool question answered'}


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
        spool_sync.resume_usage(int(keep))
        listener.clear_runout_pause()
        # The three concerns stay separate (#1063 jam follow-up): this
        # settles the inventory record only. It does not say the feed
        # path is clear, it does not resume the print, and Print Handy
        # names plainly that it cannot drive a paused-print load/unload
        # itself instead of funneling the operator into a replacement.
        return (f'Got it — the same physical spool #{keep} stays in the slot; this is a feed '
                f'fix, not a replacement. No new spool was created, spool #{keep} was not '
                f'marked depleted, and its remaining weight record is unchanged. Usage '
                f'tracking is back on #{keep}.\n'
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
            spool_sync.resume_usage(int(keep))
            listener.clear_runout_pause()
            return f'Got it, same spool #{keep}. Usage tracking is back on #{keep}.'
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
    spool_sync.resume_usage(spool_id)
    listener.clear_runout_pause()
    # bind_text already names the 'depleted' disposition ("Spool #N was
    # marked as depleted"), so the old spool's fate is stated once (#787).
    return f'{created}{spool_sync.bind_text(result)} Usage tracking is back on, charged to #{spool_id}.'


_SPOOL_CANCELLED = 'Cancelled. Nothing changed.'
_SPOOL_UNCLEAR = 'That answer was not clear. Nothing changed.'
_SPOOL_MENU_NEW = 'Add a new spool'
_SPOOL_MENU_CHANGE = 'Change a spool'
_SPOOL_MENU_LIST = 'List spools'
_SPOOL_DISPOSITIONS = {'Back on the shelf': 'shelf', 'Archive it': 'archive',
                       'Mark it as depleted': 'depleted', 'Archive as depleted': 'depleted',
                       'Delete it': 'delete'}

_SPOOL_NL_NEW_RES = (
    re.compile(r"(?:please\s+)?add(?:ing)?\s+(?:a\s+|an\s+|some\s+)?(?:new\s+)?(?:filament|spool|roll)\s*[.!]?", re.I),
    re.compile(r"(?:we|i)\s+(?:need|want)\s+to\s+add\s+(?:a\s+|an\s+|some\s+)?(?:new\s+)?(?:filament|spool|roll)\s*[.!]?", re.I),
    re.compile(r"(?:we|i)\s+(?:need|want)\s+(?:a\s+)?new\s+(?:filament|spool|roll)\s*[.!]?", re.I),
    re.compile(r"new\s+(?:filament|spool|roll)\s*[.!]?", re.I),
)
_SPOOL_NL_CHANGE_RES = (
    re.compile(r"(?:please\s+)?(?:change|swap|replace)\s+(?:the\s+|a\s+|an\s+)?(?:filament|spool|roll)(?:\s+(?:roll|spool))?(?:\s+in\s+slot\s+[1-4])?\s*[.!]?", re.I),
    re.compile(r"(?:we|i)\s+(?:need|want)\s+to\s+(?:change|swap|replace)\s+(?:the\s+|a\s+|an\s+)?(?:filament|spool|roll)(?:\s+(?:roll|spool))?\s*[.!]?", re.I),
    re.compile(r"(?:please\s+)?(?:change|swap|replace)\s+(?:the\s+|a\s+|an\s+)?filament\s+roll\s*[.!]?", re.I),
    re.compile(r"(?:we|i)\s+(?:need|want)\s+to\s+change\s+a\s+filament\s+roll\s*[.!]?", re.I),
)


def _spool_nl_intent(text):
    """Deterministic plain-text spool requests (#475): 'new' or 'change', else None.

    Anchored full-message patterns in the style of 'print 57' (#436), not
    loose substring matching: 'add new filament', 'we need to add new
    filament', 'new spool', 'swap the filament'. Discussion ('the filament
    is tangled', 'add new filament to my shopping list', questions about
    filament) never matches, and this runs with no LLM configured.
    """
    text = ' '.join(str(text or '').split()).strip()
    if not text or text.startswith('/'):
        return None
    if any(pattern.fullmatch(text) for pattern in _SPOOL_NL_NEW_RES):
        return 'new'
    if any(pattern.fullmatch(text) for pattern in _SPOOL_NL_CHANGE_RES):
        return 'change'
    return None


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
        from print_handy.hermes import moonraker_event_listener as listener
        if listener._vision_ready():
            return True
    except Exception:
        pass
    try:
        from print_handy.hermes import spool_codes, spool_label
        return bool(spool_codes.codes_available() or spool_label.tesseract_available())
    except Exception:
        return False


def _spool_label_vision_call():
    """The shared vision call for label extraction (#514), carrying the
    listener's configured values — the same endpoint the camera watch uses."""
    from print_handy.hermes import moonraker_event_listener as listener
    from print_handy.hermes import visual_watch

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
    from print_handy.hermes import spool_codes, spool_label

    known = state.get('types') or []
    routes = []
    if spool_codes.codes_available():
        routes.append(('code', lambda image: spool_codes.extract_via_codes(image, known)))
    try:
        from print_handy.hermes import moonraker_event_listener as listener
        if listener._vision_ready():
            call = _spool_label_vision_call()
            routes.append(('vision', lambda image: spool_label.extract_spool_spec(image, call)))
    except Exception:
        pass
    if spool_label.tesseract_available():
        routes.append(('ocr', lambda image: spool_label.extract_via_ocr(image, known)))
    return routes


def _purchase_ai_call():
    """The AI call for purchase intake (#801), on the #514 vision route.

    One callable with the purchase_intake shape (prompt, plus an image or
    page text): a screenshot goes through the shared vision extraction
    (visual_watch.visual_extract, the listener's configured values, as
    _spool_label_vision_call does); page text goes to the same chat
    endpoint as a text-only message, or to PRINT_VISION_URL with the text
    inside the prompt when that is the only route.
    """
    import urllib.request
    from print_handy.hermes import moonraker_event_listener as listener
    from print_handy.hermes import visual_watch

    def call(*, prompt, image=None, text=None):
        if image is not None:
            return visual_watch.visual_extract(
                image, prompt, chat_url=listener._vision_chat_url, model=listener._VISION_MODEL,
                api_key=lambda: listener._profile_env_value("CLIPROXY_API_KEY"),
                context="Filament purchase screenshot or order. Reply with JSON only.")
        full = prompt if text is None else f"{prompt}\n\nPage text:\n{text}"
        endpoint = os.environ.get("PRINT_VISION_URL")
        if endpoint:
            import base64 as _b64
            payload = json.dumps({"image": _b64.b64encode(b"").decode(), "prompt": full}).encode()
            req = urllib.request.Request(endpoint, data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read())
        url = listener._vision_chat_url()
        key = listener._profile_env_value("CLIPROXY_API_KEY")
        if not key and not visual_watch.keyless_ok(url):
            return None
        body = {"model": listener._VISION_MODEL, "temperature": 0, "max_tokens": 800,
                "messages": [{"role": "user", "content": full}]}
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.loads(response.read())
        content = visual_watch._strip_json_fence(result["choices"][0]["message"]["content"])
        try:
            return json.loads(content)
        except ValueError:
            return content

    return call


def _purchase_event_image_paths(event):
    from print_handy.hermes import purchase_intake
    return [str(p) for p in (getattr(event, 'media_urls', None) or [])
            if purchase_intake.is_image_name(p)]


def _purchase_event_image(event):
    """(has_image, image bytes or None) for a channel message (#801).

    Saved attachments ride the event as media_urls (the model_file shape);
    their bytes are read only after the operator accepts the offer.
    """
    paths = _purchase_event_image_paths(event)
    if not paths:
        return False, None
    for path in paths:
        candidate = Path(path)
        if candidate.is_file():
            try:
                return True, candidate.read_bytes()
            except OSError:
                return True, None
    return True, None


async def _spool_ask(adapter, source, metadata, gateway, question, choices):
    """One guided /spool question through the shared button prompt (_confirm)."""
    return await _confirm(adapter, source, metadata, 'spool', question, list(choices),
                          _spool_timeout(), live_key=gateway._session_key_for_source(source))


def _spool_is_cancel(answer):
    if answer is None or not isinstance(answer, str):
        return True
    return answer.strip().casefold() in ('cancel', '__cancelled__')


def _spool_type_label(filament_type):
    """A filament type in the picker shape (#792 part 2): 'color · material · brand'.

    A type is not a spool, so it has no spool number or grams to show;
    color and material lead, in the same order and separator the spool
    pickers use, with the brand last.
    """
    parts = [str(filament_type.get('color') or '').strip(),
             str(filament_type.get('material') or '').strip(),
             str(filament_type.get('brand') or '').strip()]
    return ' · '.join(x for x in parts if x)


def _picker_label(entry):
    """The one spool picker label for a shelf entry (#792 part 2)."""
    from print_handy.hermes.discord_print_summary import spool_picker_label
    return spool_picker_label(entry.get('name'), entry.get('material'),
                              entry.get('spool_id'), entry.get('remaining_grams'))


def _spool_spec_text(spec):
    head = ' '.join(x for x in (spec.get('brand'), spec.get('material')) if x)
    return f"{head}, {spec['color']}" if head else str(spec['color'])


async def _spool_slot_ask(adapter, source, metadata, gateway, spool_sync, state, lead):
    """Ask which slot, showing what each slot holds. (slot, None) or (None, final message)."""
    slots = (state or {}).get('filament_slots') or {}
    lines = [lead]
    for slot in spool_sync.SLOTS:
        rec = slots.get(str(slot)) or {}
        sid = rec.get('spoolman_id')
        if not sid:
            lines.append(f"Slot {slot}: empty")
        elif rec.get('color') and rec.get('material'):
            # A fully recorded slot names its spool in the one picker shape.
            from print_handy.hermes.discord_print_summary import spool_picker_label
            lines.append(f"Slot {slot}: {spool_picker_label(rec['color'], rec['material'], sid, rec.get('remaining_grams'))}")
        else:
            lines.append(f"Slot {slot}: #{sid} {rec.get('name') or ''}".rstrip())
    answer = await _spool_ask(adapter, source, metadata, gateway, '\n'.join(lines),
                              [f'Slot {slot}' for slot in spool_sync.SLOTS])
    if _spool_is_cancel(answer):
        return None, _SPOOL_CANCELLED
    match = re.fullmatch(r'(?:slot\s*)?([1-4])', str(answer).strip().casefold())
    if not match:
        return None, _SPOOL_UNCLEAR
    return int(match.group(1)), None


async def _spool_disposition_ask(adapter, source, metadata, gateway, slot, old_id, old_remaining=None):
    """The old spool's fate, as buttons. (disposition, None) or (None, final message).

    Delete is offered alongside shelf/archive/depleted (#512 §3); when the
    old spool has nothing left, Archive as depleted leads and says why.
    """
    empty = isinstance(old_remaining, (int, float)) and old_remaining <= 0
    if empty:
        choices = ['Archive as depleted', 'Back on the shelf', 'Archive it', 'Delete it', 'Cancel']
        question = (f"Slot {slot} still holds spool #{old_id} (0 g left). What happens to spool #{old_id}? "
                    f"Archive as depleted is the recommended choice for an empty spool.")
    else:
        choices = ['Back on the shelf', 'Archive it', 'Mark it as depleted', 'Delete it', 'Cancel']
        question = f"Slot {slot} still holds spool #{old_id}. What happens to spool #{old_id}?"
    answer = await _spool_ask(adapter, source, metadata, gateway, question, choices)
    if _spool_is_cancel(answer):
        return None, _SPOOL_CANCELLED
    disposition = _SPOOL_DISPOSITIONS.get(str(answer).strip())
    return (disposition, None) if disposition else (None, _SPOOL_UNCLEAR)


async def _spool_new_type_details(adapter, source, metadata, gateway, lead=''):
    """Brand / material / colour for a filament type the database does not know.

    Returns a spec dict, or a final message string when the flow ends.
    Hex is never asked and never required (#475). Brand is asked only when
    spool.prompt_brand is on (#512): off by default, the flow goes straight
    to material and the spool is created with no brand. ``lead`` prefixes the
    first question (the early spool-number preview, #512 §2).
    """
    brand = None
    if _prompt_brand():
        answer = await _spool_ask(adapter, source, metadata, gateway,
                                  (lead + '\n' if lead else '')
                                  + 'What brand is the new filament? Type the brand name, or choose Skip brand.',
                                  ['Skip brand', 'Cancel'])
        if _spool_is_cancel(answer):
            return _SPOOL_CANCELLED
        brand = None if str(answer).strip().casefold() in ('skip brand', 'skip') else str(answer).strip()
        if brand is not None and not brand:
            return _SPOOL_UNCLEAR
    answer = await _spool_ask(adapter, source, metadata, gateway,
                              (lead + '\n' if lead and not _prompt_brand() else '')
                              + 'What material is it? Pick one, or type another material.',
                              ['PLA', 'Silk PLA', 'PLA+', 'PETG', 'TPU', 'ABS', 'Cancel'])
    if _spool_is_cancel(answer):
        return _SPOOL_CANCELLED
    material = str(answer).strip()
    if not material:
        return _SPOOL_UNCLEAR
    while True:
        answer = await _spool_ask(adapter, source, metadata, gateway,
                                  'What is the color called? Type the color name — no color code is needed.',
                                  ['Cancel'])
        if _spool_is_cancel(answer):
            return _SPOOL_CANCELLED
        color = str(answer).strip()
        if not color:
            return _SPOOL_UNCLEAR
        # Parsed-color sanity (#792 part 2): a color that parses to
        # multiple colors or carries unrecognized fragment words gets a
        # confirm-or-correct step naming what was understood, instead of
        # minting a spool record with a possible garble baked in.
        from print_handy.core import filament_color
        sanity_text = filament_color.color_sanity_text(color, material)
        if sanity_text is None:
            break
        answer = await _spool_ask(adapter, source, metadata, gateway,
                                  sanity_text + ' Is that exactly as labeled, or do you want to type it again?',
                                  ['That is exactly as labeled', 'Type it again', 'Cancel'])
        if _spool_is_cancel(answer):
            return _SPOOL_CANCELLED
        if str(answer).strip() == 'That is exactly as labeled':
            break
        if str(answer).strip() != 'Type it again':
            return _SPOOL_UNCLEAR
    return {'brand': brand, 'material': material, 'color': color, 'hex': None}


async def _spool_grams_ask(adapter, source, metadata, gateway):
    """Full-spool grams: a preset button or a typed positive number. A float, or a final message."""
    answer = await _spool_ask(adapter, source, metadata, gateway,
                              'How many grams is a full spool? Pick one, or type the weight in grams.',
                              ['1000', '500', '250', 'Cancel'])
    if _spool_is_cancel(answer):
        return _SPOOL_CANCELLED
    try:
        grams = float(re.sub(r'[^0-9.]', '', str(answer)))
    except ValueError:
        return _SPOOL_UNCLEAR
    if grams <= 0:
        return _SPOOL_UNCLEAR
    return round(grams, 1)


async def _spool_guided_new(adapter, source, metadata, gateway, spool_sync, pid, mr_kw, *, offer_load=True):
    """Walk through adding a spool (#475). Returns (created spool id or None, final message).

    Database first: a known filament type supplies brand/material/colour/hex,
    so no colour or hex question follows. Nothing is created before the
    confirmation button, and no spool number is named before creation
    (#1063: a predicted id can be wrong by creation time — the actual
    number is announced in the result message the moment it exists).
    """
    types = await asyncio.to_thread(spool_sync.filament_types)
    preview = ("Preparing a new spool — nothing is created until you confirm, and I will "
               "announce its actual number the moment it is created.")
    spec = None
    if types:
        listed = types[:8]
        lines = [preview,
                 'Which filament is the new spool? Pick a type already in the database, '
                 'or choose New filament type.']
        lines += [f"{i}. {_spool_type_label(t)}" for i, t in enumerate(listed, 1)]
        lines.append(f"{len(listed) + 1}. New filament type")
        # The registered choices must be the numbered list itself, in order
        # (P2-182): buttonless adapters translate a typed number through
        # this list, so registering only the first three types made the
        # displayed fourth type open new-type entry instead.
        choices = [_spool_type_label(t) for t in listed] + ['New filament type']
        answer = await _spool_ask(adapter, source, metadata, gateway, '\n'.join(lines), choices)
        if _spool_is_cancel(answer):
            return None, _SPOOL_CANCELLED
        text = str(answer).strip()
        pick = None
        if text.casefold() in ('new', 'new filament type'):
            pick = 'new'
        elif text.isdigit() and 1 <= int(text) <= len(listed):
            pick = listed[int(text) - 1]
        else:
            pick = next((t for t in listed if text.casefold() == _spool_type_label(t).casefold()), None)
        if pick is None:
            return None, _SPOOL_UNCLEAR
        if pick != 'new':
            spec = {'brand': pick.get('brand'), 'material': pick['material'],
                    'color': pick['color'], 'hex': pick.get('hex')}
    if spec is None:
        details = await _spool_new_type_details(adapter, source, metadata, gateway,
                                                lead='' if types else preview)
        if isinstance(details, str):
            return None, details
        spec = details
    grams = await _spool_grams_ask(adapter, source, metadata, gateway)
    if isinstance(grams, str):
        return None, grams
    spec = {**spec, 'grams': grams}
    create_label = 'Create spool'
    answer = await _spool_ask(adapter, source, metadata, gateway,
                              f"This creates a new spool — {_spool_spec_text(spec)}, "
                              f"{grams:g} g, on the shelf. Create it?",
                              [create_label, 'Cancel'])
    if answer != create_label:
        return None, _SPOOL_CANCELLED if _spool_is_cancel(answer) else _SPOOL_UNCLEAR
    made = await asyncio.to_thread(spool_sync.create_spool, spec)
    made_id = int(made['spool_id'])
    message = (f"Created spool #{made_id} on the shelf"
               f"{' (existing filament type reused)' if made.get('reused_filament') else ''}.")
    if not offer_load:
        return made_id, message
    answer = await _spool_ask(adapter, source, metadata, gateway,
                              f"Load spool #{made_id} into a slot now, or leave it on the shelf?",
                              ['Load it into a slot', 'Leave it on the shelf'])
    if answer != 'Load it into a slot':
        return made_id, message
    state = await asyncio.to_thread(spool_sync.load_state, **_pkw(pid))
    slot, problem = await _spool_slot_ask(adapter, source, metadata, gateway, spool_sync, state,
                                          f"Which slot should spool #{made_id} go into?")
    if slot is None:
        return made_id, f"{message} Nothing was loaded; no slot changed."
    old_id = ((state.get('filament_slots') or {}).get(str(slot)) or {}).get('spoolman_id')
    disposition = 'shelf'
    if old_id and int(old_id) != made_id:
        disposition, problem = await _spool_disposition_ask(
            adapter, source, metadata, gateway, slot, old_id,
            ((state.get('filament_slots') or {}).get(str(slot)) or {}).get('remaining_grams'))
        if disposition is None:
            return made_id, f"{message} Nothing was loaded; slot {slot} is unchanged."
    # One confirmation naming the exact spool before the bind (#792 part 2):
    # the load used to happen on the slot answer alone.
    fate = {'shelf': f" Spool #{old_id} goes back on the shelf.",
            'archive': f" Spool #{old_id} will be archived.",
            'depleted': f" Spool #{old_id} will be marked as depleted (0 g left).",
            'delete': f" Spool #{old_id} will be DELETED from Spoolman."}.get(disposition, '') if old_id else ''
    from print_handy.hermes.discord_print_summary import spool_picker_label
    label = spool_picker_label(spec.get('color'), spec.get('material'), made_id, spec.get('grams'))
    load_label = f'Load spool #{made_id}'
    answer = await _spool_ask(adapter, source, metadata, gateway,
                              f"Load spool {label} into slot {slot}?{fate}", [load_label, 'Cancel'])
    if answer != load_label:
        return made_id, (f"{message} Nothing was loaded; slot {slot} is unchanged."
                         if _spool_is_cancel(answer) else f"{message} {_SPOOL_UNCLEAR}")
    result = await asyncio.to_thread(spool_sync.bind_slot, slot, made_id,
                                     prior_disposition=disposition,
                                     expected_prior_spool_id=int(old_id) if old_id else None,
                                     **mr_kw)
    return made_id, f"{message} {spool_sync.bind_text(result)}"


async def _spool_guided_change(adapter, source, metadata, gateway, spool_sync, pid, mr_kw):
    """Walk through changing a slot's spool (#475): slot -> replacement ->
    the old spool's fate -> one confirmation -> bind_slot."""
    state = await asyncio.to_thread(spool_sync.load_state, **_pkw(pid))
    slot, problem = await _spool_slot_ask(adapter, source, metadata, gateway, spool_sync, state,
                                          'Which slot are you changing?')
    if slot is None:
        return problem
    old_id = ((state.get('filament_slots') or {}).get(str(slot)) or {}).get('spoolman_id')
    shelf = await asyncio.to_thread(spool_sync.shelf_spools, **_pkw(pid))
    listed = shelf[:12]
    lines = [f"Which spool goes into slot {slot}? Pick one from the shelf, or choose Add a new spool."]
    if listed:
        for i, entry in enumerate(listed, 1):
            lines.append(f"{i}. {_picker_label(entry)}")
    else:
        lines.append('(nothing on the shelf)')
    if listed:
        lines.append(f"{len(listed) + 1}. Add a new spool")
    # Register every listed spool with its FULL label (P2-182, P2-183):
    # adapters truncate only the displayed button text and resolve a click
    # back to the registered value, so a [:80] truncation here — or offering
    # only the first three — made clicks and typed numbers land on the
    # wrong entry or on no entry at all. The label is the one picker shape
    # (#792 part 2): color · material · #id · grams left (or grams unknown).
    choices = [_picker_label(entry) for entry in listed] + ['Add a new spool']
    if not listed:
        choices = ['Add a new spool', 'Cancel']
    answer = await _spool_ask(adapter, source, metadata, gateway, '\n'.join(lines), choices)
    if _spool_is_cancel(answer):
        return _SPOOL_CANCELLED
    text = str(answer).strip()
    created_note = ''
    new_id = None
    if text.casefold() == 'add a new spool':
        made_id, made_message = await _spool_guided_new(adapter, source, metadata, gateway,
                                                        spool_sync, pid, mr_kw, offer_load=False)
        if made_id is None:
            return made_message
        new_id = made_id
        created_note = made_message + ' '
    else:
        if text.startswith('#') and text[1:].isdigit():
            wanted = int(text[1:])
            new_id = wanted if any(entry['spool_id'] == wanted for entry in shelf) else None
        elif text.isdigit():
            number = int(text)
            if 1 <= number <= len(listed):
                new_id = listed[number - 1]['spool_id']
            elif any(entry['spool_id'] == number for entry in shelf):
                new_id = number
        else:
            # The registered picker label, or the pre-#792 '#id name' form
            # a typed answer may still use.
            pick = next((entry for entry in listed
                         if text.casefold() in (_picker_label(entry).casefold(),
                                                f"#{entry['spool_id']} {entry['name']}".casefold())), None)
            new_id = pick['spool_id'] if pick else None
        if new_id is None:
            return _SPOOL_UNCLEAR
    if old_id and int(old_id) == int(new_id):
        return f"{created_note}Slot {slot} already holds spool #{new_id}. Nothing changed."
    disposition = 'shelf'
    if old_id:
        disposition, problem = await _spool_disposition_ask(
            adapter, source, metadata, gateway, slot, old_id,
            ((state.get('filament_slots') or {}).get(str(slot)) or {}).get('remaining_grams'))
        if disposition is None:
            return (f"{created_note}The change was cancelled; slot {slot} is unchanged."
                    if created_note else problem)
    fate = {'shelf': f" Spool #{old_id} goes back on the shelf.",
            'archive': f" Spool #{old_id} will be archived.",
            'depleted': f" Spool #{old_id} will be marked as depleted (0 g left).",
            'delete': f" Spool #{old_id} will be DELETED from Spoolman."}.get(disposition, '') if old_id else ''
    # The confirmation names the exact spool (#792 part 2): number, color,
    # material and remaining — not just the number the picker carried.
    picked = next((entry for entry in listed if entry['spool_id'] == int(new_id)), None)
    if picked is not None:
        exact = _picker_label(picked)
    elif created_note:
        # A spool created inside this flow is on the shelf now: name it
        # exactly too, when the shelf can be re-read.
        try:
            fresh = await asyncio.to_thread(spool_sync.shelf_spools, **_pkw(pid))
        except Exception:
            fresh = []
        made = next((entry for entry in fresh if entry['spool_id'] == int(new_id)), None)
        exact = _picker_label(made) if made is not None else f"#{new_id}"
    else:
        exact = f"#{new_id}"
    confirm_label = f'Change slot {slot}'
    answer = await _spool_ask(adapter, source, metadata, gateway,
                              f"Change slot {slot} to spool {exact}?{fate}", [confirm_label, 'Cancel'])
    if answer != confirm_label:
        return (f"{created_note}The change was cancelled; slot {slot} is unchanged."
                if created_note else
                _SPOOL_CANCELLED if _spool_is_cancel(answer) else _SPOOL_UNCLEAR)
    result = await asyncio.to_thread(spool_sync.bind_slot, slot, int(new_id),
                                     prior_disposition=disposition,
                                     expected_prior_spool_id=int(old_id) if old_id else None,
                                     **mr_kw)
    return created_note + spool_sync.bind_text(result)


_LOADED_NL_RES = (
    re.compile(r"(?:please\s+)?(?:what|which)(?:'s|\s+is|\s+are)?\s+(?:the\s+)?(?:colou?rs?|filaments?|spools?)\s+(?:is\s+|are\s+)?loaded\s*[.!?]?", re.I),
    re.compile(r"(?:please\s+)?(?:what|which)(?:'s|\s+is)\s+loaded\s*[.!?]?", re.I),
    re.compile(r"(?:current|loaded)\s+(?:spools?|filaments?|colou?rs?)\s*[.!?]?", re.I),
    re.compile(r"(?:please\s+)?(?:show|list)(?:\s+me)?\s+(?:the\s+)?(?:loaded|current)\s+(?:spools?|filaments?|colou?rs?)\s*[.!?]?", re.I),
    re.compile(r"print[-\s]colou?rs?\s*[.!?]?", re.I),
    re.compile(r"(?:please\s+)?how\s+much\s+filament\s+is\s+left(?:\s+on\s+(?:the\s+)?spools?)?\s*[.!?]?", re.I),
)


def _loaded_spools_intent(text):
    """Deterministic plain-text asks for the loaded-filament view (#744), else None.

    Anchored full-message patterns in the style of _spool_nl_intent (#475):
    'what colors are loaded', 'current spools', 'show me the loaded
    filament'. Discussion about loaded filament ('the loaded filament is
    tangled') never matches, and this runs with no LLM configured, so the
    Telegram pure-bot path answers it too.
    """
    text = ' '.join(str(text or '').split()).strip()
    if not text or text.startswith('/'):
        return None
    return 'loaded' if any(pattern.fullmatch(text) for pattern in _LOADED_NL_RES) else None


# Fuzzy phrase targets for the loaded view (#744, fuzzy extension): the
# canonical asks a near miss is measured against ("how much filament is
# left" is the same question with no word "loaded" in it, so it is both
# an exact pattern above and a typo target here). Rivals are the neighbouring
# spool intents a loaded ask must beat clearly before it may even be
# suggested — a fuzzy tie with "change the filament" is not a loaded ask.
_LOADED_FUZZY_PHRASES = (
    "what colors are loaded",
    "what color is loaded",
    "what filament is loaded",
    "what filaments are loaded",
    "which spools are loaded",
    "which filament is loaded",
    "which filaments are loaded",
    "current spools",
    "loaded filament",
    "loaded spools",
    "loaded colors",
    "loaded colours",
    "what is loaded",
    "show the loaded spools",
    "print colors",
    "how much filament is left",
    "how much filament is left on the spools",
    "how much filament is loaded",
)
_LOADED_FUZZY_RIVALS = (
    "add new filament",
    "new spool",
    "change the filament",
    "swap the filament",
    "replace the filament",
    "load the filament",
)

_LOADED_SUGGEST_YES = 'Yes — show it'
_LOADED_SUGGEST_NO = 'No'
_LOADED_SUGGEST_TIMEOUT_S = 60  # the #468 stage-3 suggestion expiry


def _loaded_spools_fuzzy(text):
    """True when a plain message is a clear fuzzy near-miss of a loaded ask.

    Measured with the shared scorer (core.fuzzy) against the canonical
    phrases above, family against family: the shared decide() policy
    must call the loaded family the single direct answer — high
    confidence with a clear margin over the rival spool intents — or
    the message is not a loaded ask at all. Grey-zone and contested
    scores fall through to the usual lanes, exactly as #468 prescribes:
    fuzzy here may only ever earn a did-you-mean prompt, never an
    unasked answer. Deterministic, offline, no LLM involved.
    """
    text = ' '.join(str(text or '').split()).strip()
    if not text or text.startswith('/'):
        return False
    from print_handy.core import fuzzy
    best: dict[str, int] = {}
    for family, phrases in (("loaded", _LOADED_FUZZY_PHRASES), ("other", _LOADED_FUZZY_RIVALS)):
        for phrase in phrases:
            score = fuzzy.fuzzy_score(text, phrase)
            if score > best.get(family, 0):
                best[family] = score
    scored = [(score, family) for family, score in best.items() if score]
    if not scored:
        return False
    decision = fuzzy.decide(scored)
    return decision.kind == "direct" and decision.best == "loaded"


async def loaded_spools_dispatch(event, gateway, **kwargs):
    """/print-spools and its twins (#744): what filament is loaded, where,
    and how much is left — read-only, no confirmation, never the shelf.

    Trigger forms: `/print-spools`, `/spool loaded`, a bare `/print-colors`
    (no JOB.json — the archived-job form `/print-colors <JOB.json>` keeps
    its meaning and is claimed earlier by color_selection_dispatch), and
    the anchored plain-text asks in _loaded_spools_intent. Runs before
    spool_dispatch so '/spool loaded' is not read as a spool change verb.

    A clear fuzzy near-miss of a plain ask (_loaded_spools_fuzzy) earns
    the #468 did-you-mean prompt instead: the view is shown only when
    the user presses Yes — a fuzzy match never answers by itself.
    """
    text = str(getattr(event, 'text', '')).strip()
    folded = ' '.join(text.split())
    exact = (folded in ('/print-spools', '/print-colors', '/spool loaded')
             or folded.startswith('/print-spools ')
             or bool(_loaded_spools_intent(text)))
    if not exact and not _loaded_spools_fuzzy(text):
        return None
    source, adapter = _control_gate(event, gateway)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'loaded spools UI delivery unavailable'}
    from print_handy.hermes import spool_sync
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    if not exact:
        answer = await _confirm(adapter, source, metadata, 'spools-suggest',
                                'Did you mean "what colors are loaded"? I can show the filament '
                                'that is loaded, where it is, and how much is left.',
                                [_LOADED_SUGGEST_YES, _LOADED_SUGGEST_NO],
                                _LOADED_SUGGEST_TIMEOUT_S,
                                live_key=gateway._session_key_for_source(source))
        if not (isinstance(answer, str) and answer.strip().casefold()
                in (_LOADED_SUGGEST_YES.casefold(), 'yes', 'y')):
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content='Okay — nothing was shown.')
            return {'action': 'skip', 'reason': 'loaded spools suggestion declined'}
    pid = _routed_printer_id(source, gateway)
    candidates = _undecided_printer_ids(source, gateway) if not pid else None
    try:
        if candidates:
            # A shared channel (#536) decides no printer: like /spool list,
            # the read-only view names each printer's loaded filament.
            sections = []
            for candidate in candidates:
                body = await asyncio.to_thread(spool_sync.loaded_text, printer_id=candidate)
                sections.append(f'**{_printer_label(candidate)}**\n{body}')
            message = '\n\n'.join(sections)
        else:
            message = await asyncio.to_thread(spool_sync.loaded_text, **_pkw(pid))
    except Exception:
        logger.exception('/print-spools failed')
        message = ('I could not read the spool record just now, so I cannot say what is loaded. '
                   'Nothing was changed.')
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'loaded spools view handled'}


async def purchase_dispatch(event, gateway, **kwargs):
    """/purchase intake (#801): a shop/order link or a purchase screenshot in
    the printer channel earns a confirm-first offer, only when a vision/AI
    route is configured (purchase_intake.ai_route_configured — the #514
    gating values). Never automatic: the offer asks first, the proposed
    spools are shown as one list, and Create all / Edit / Cancel decide.
    Model links keep model_link_dispatch's routing (it runs earlier, and
    recognition here refuses them too). With no AI route, or nothing
    purchase-shaped, this returns None and the message is untouched.
    """
    from print_handy.hermes import purchase_intake
    text = str(getattr(event, 'text', '') or '')
    if not purchase_intake.looks_like_purchase(text, has_image=bool(_purchase_event_image_paths(event))):
        return None
    if not purchase_intake.ai_route_configured():
        return None
    source, adapter = _control_gate(event, gateway)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'purchase UI delivery unavailable'}
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    key = ('purchase', source.chat_id, source.thread_id)
    if key in _seen:
        return {'action': 'skip', 'reason': 'purchase flow already active'}
    _seen[key] = time.monotonic()
    live_key = gateway._session_key_for_source(source)
    try:
        answer = await _confirm(adapter, source, metadata, 'purchase',
                                purchase_intake.OFFER_QUESTION,
                                [purchase_intake.OFFER_YES, purchase_intake.OFFER_NO],
                                _spool_timeout(), live_key=live_key)
        if answer != purchase_intake.OFFER_YES:
            message = 'Okay — nothing was added.'
        else:
            url = purchase_intake.purchase_url(text)
            try:
                if url is not None:
                    lines = await asyncio.to_thread(
                        purchase_intake.propose_from_link, url, ai_call=_purchase_ai_call())
                else:
                    has_image, image = _purchase_event_image(event)
                    if not image:
                        raise purchase_intake.PurchaseError('unreadable')
                    lines = await asyncio.to_thread(
                        purchase_intake.propose_from_image, image, ai_call=_purchase_ai_call())
            except purchase_intake.PurchaseError as exc:
                message = str(exc)
            else:
                choice = await _confirm(adapter, source, metadata, 'purchase',
                                        purchase_intake.confirm_text(lines),
                                        list(purchase_intake.CONFIRM_CHOICES),
                                        _spool_timeout(), live_key=live_key)
                if choice == 'Create all':
                    try:
                        created = await asyncio.to_thread(purchase_intake.create_purchase, lines)
                        message = purchase_intake.created_text(created)
                    except purchase_intake.PartialCreationError as exc:
                        # Some spools landed: say which, never "nothing was created".
                        message = str(exc)
                    except Exception:
                        logger.exception('purchase creation failed before any spool was created')
                        message = ('Creating the spools failed before any were created, so nothing '
                                   'was added. Add the spools by hand with `/spool new`.')
                elif choice == 'Edit':
                    message = purchase_intake.edit_handoff_text(lines)
                else:
                    message = 'Cancelled. Nothing was created.'
    except Exception:
        logger.exception('purchase intake failed')
        message = 'That purchase could not be read, so nothing was created. Add the spools by hand with `/spool new`.'
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'purchase intake handled'}


async def spool_dispatch(event, gateway, **kwargs):
    """/spool: bare, incomplete or natural-language forms open the guided flow (#475);
    the complete typed forms (list | change <slot> <spool#> | new brand, material,
    color, grams[, hex] | sync) keep their one-shot behaviour. Never moves the printer."""
    text = str(getattr(event, 'text', '')).strip()
    nl_intent = None if text.startswith('/spool') else _spool_nl_intent(text)
    if nl_intent is not None:
        # The plain-text twin of /spool (#475, in the style of 'print 57'):
        # same gate, same guided flow — never the LLM and never the help dump.
        source, adapter = _control_gate(event, gateway)
    else:
        source, adapter = _control_preamble(event, gateway, '/spool', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'spool UI delivery unavailable'}
    from print_handy.hermes import spool_sync
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    pid = _routed_printer_id(source, gateway)
    # A routed channel's spool state and screen pushes are that printer's:
    # its hardware record and its own Moonraker (#335). Spoolman itself is shared.
    mr_kw = {'mr': spool_sync.moonraker_for(pid), 'printer_id': pid} if pid else {}
    args = text[len('/spool'):].strip() if nl_intent is None else ''
    verb, _, rest = args.partition(' ')
    verb = verb.casefold()
    # Decide up front whether this entry needs the guided flow, so the
    # already-open guard covers exactly the button conversations.
    guided = nl_intent is not None or verb == ''
    change_parts = rest.replace('#', ' ').split() if verb in ('change', 'set', 'load') else []
    change_complete = len(change_parts) == 2 and all(p.isdigit() for p in change_parts)
    new_spec = None
    if verb in ('new', 'add'):
        try:
            new_spec = spool_sync.parse_new(rest)
        except Exception:
            new_spec = None
        guided = new_spec is None
    if verb in ('change', 'set', 'load'):
        guided = not change_complete
    # Shared channel (#536): slot binds and screen syncs are printer-bound
    # and ask first; making a shelf spool and listing inventory are global.
    candidates = _undecided_printer_ids(source, gateway) if not pid else None
    decided = {'done': False}

    async def _spool_printer():
        """Decide the printer for a printer-bound spool form, once."""
        nonlocal pid, mr_kw
        if decided['done']:
            return True
        decided['done'] = True
        if not candidates:
            return True
        chosen = await _ask_which_printer(source, gateway, adapter, candidates,
                                          question='Which printer are the spools for?',
                                          metadata=metadata)
        if chosen is None:
            return False
        pid = _routed_form(chosen)
        mr_kw = {'mr': spool_sync.moonraker_for(pid), 'printer_id': pid} if pid else {}
        return True

    async def _spool_list_message():
        if candidates:
            # No silent default: a shared channel's list names each
            # printer's loaded slots separately.
            sections = []
            for candidate in candidates:
                body = await asyncio.to_thread(spool_sync.list_text, printer_id=candidate)
                sections.append(f'**{_printer_label(candidate)}**\n{body}')
            return '\n\n'.join(sections)
        return await asyncio.to_thread(spool_sync.list_text, **_pkw(pid))

    key = ('spool', source.chat_id, source.thread_id)
    if guided:
        if key in _seen:
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content='A spool question is already open here. Nothing changed.')
            return {'action': 'skip', 'reason': 'spool flow already active'}
        _seen[key] = time.monotonic()
    try:
        if nl_intent == 'new':
            if await _spool_printer():
                _made, message = await _spool_guided_new(adapter, source, metadata, gateway,
                                                         spool_sync, pid, mr_kw)
            else:
                message = 'No printer chosen, so nothing was changed.'
        elif nl_intent == 'change':
            if await _spool_printer():
                message = await _spool_guided_change(adapter, source, metadata, gateway,
                                                     spool_sync, pid, mr_kw)
            else:
                message = 'No printer chosen, so nothing was changed.'
        elif verb in ('list', 'ls'):
            message = await _spool_list_message()
        elif verb in ('change', 'set', 'load'):
            if not await _spool_printer():
                message = 'No printer chosen, so nothing was changed.'
            elif change_complete:
                result = await asyncio.to_thread(spool_sync.bind_slot, int(change_parts[0]),
                                                 int(change_parts[1]), **mr_kw)
                message = spool_sync.bind_text(result)
            else:
                message = await _spool_guided_change(adapter, source, metadata, gateway,
                                                     spool_sync, pid, mr_kw)
        elif verb in ('new', 'add'):
            if new_spec is not None:
                # Parsed-color sanity (#792 part 2): the one-shot form has
                # no confirmation step, so a color that parses to multiple
                # colors or a fragment is not minted from it — the guided
                # form asks the confirm-or-correct step instead.
                from print_handy.core import filament_color
                sanity_text = filament_color.color_sanity_text(new_spec.get('color'),
                                                               new_spec.get('material'))
                if sanity_text is not None:
                    message = (f"{sanity_text} Nothing was created. Run `/spool new` and confirm "
                               f"the color there, or retype the color exactly as labeled.")
                    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
                    return {'action': 'skip', 'reason': 'spool new color needs confirmation'}
                made = await asyncio.to_thread(spool_sync.create_spool, new_spec)
                message = (f"Created spool #{made['spool_id']} on the shelf"
                           f"{' (existing filament type reused)' if made['reused_filament'] else ''}. "
                           f"Load it with `/spool change <slot> {made['spool_id']}`.")
            elif await _spool_printer():
                _made, message = await _spool_guided_new(adapter, source, metadata, gateway,
                                                         spool_sync, pid, mr_kw)
            else:
                message = 'No printer chosen, so nothing was changed.'
        elif verb == 'sync':
            if await _spool_printer():
                # Reconcile first (#798): Spoolman's ph_slot assignments rebuild
                # the local cache and converge the screens, then the existing
                # screen sync runs as before. The message shows both outcomes.
                try:
                    reconciled = await asyncio.to_thread(spool_sync.reconcile, **mr_kw)
                    reconcile_line = spool_sync.reconcile_text(reconciled)
                except Exception:
                    logger.exception('/spool sync reconcile failed')
                    reconcile_line = ('Spool reconcile: could not run — the local slot record '
                                      'was left unchanged')
                message = (reconcile_line + '\nPrinter sync from Spoolman:\n'
                           + '\n'.join(await asyncio.to_thread(spool_sync.sync_all_screens, **mr_kw)))
            else:
                message = 'No printer chosen, so nothing was changed.'
        elif verb == '':
            answer = await _spool_ask(adapter, source, metadata, gateway,
                                      'What would you like to do with the spools?',
                                      [_SPOOL_MENU_NEW, _SPOOL_MENU_CHANGE, _SPOOL_MENU_LIST, 'Cancel'])
            picked = str(answer).strip().casefold() if isinstance(answer, str) else ''
            if answer == _SPOOL_MENU_NEW or picked in ('new', 'add', 'add a new spool'):
                if await _spool_printer():
                    _made, message = await _spool_guided_new(adapter, source, metadata, gateway,
                                                             spool_sync, pid, mr_kw)
                else:
                    message = 'No printer chosen, so nothing was changed.'
            elif answer == _SPOOL_MENU_CHANGE or picked in ('change', 'change a spool', 'swap', 'replace'):
                if await _spool_printer():
                    message = await _spool_guided_change(adapter, source, metadata, gateway,
                                                         spool_sync, pid, mr_kw)
                else:
                    message = 'No printer chosen, so nothing was changed.'
            elif answer == _SPOOL_MENU_LIST or picked == 'list':
                message = await _spool_list_message()
            elif _spool_is_cancel(answer):
                message = _SPOOL_CANCELLED
            else:
                message = _SPOOL_UNCLEAR
        else:
            message = ('`/spool list` · `/spool change <slot> <spool#>` · '
                       '`/spool new brand, material, color, grams[, hex]` · `/spool sync`')
    except Exception as exc:
        logger.exception('/spool failed')
        message = 'That spool change did not work, so nothing was changed. Check the slot and spool number and try again.'
    finally:
        if guided:
            _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'spool NL intent handled' if nl_intent else 'spool command handled'}


async def lookup_dispatch(event, gateway, **kwargs):
    """/print-lookup <query>: a read-only search of the root registries (issue #169).

    Answers from knowledge/, firmware/ and printers/ data only. There is no printer
    client on this path: a lookup cannot heat, move, extrude or change anything.
    """
    source, adapter = _control_preamble(event, gateway, '/print-lookup', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'lookup UI delivery unavailable'}
    from print_handy import knowledge
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    query = str(getattr(event, 'text', '')).strip()[len('/print-lookup'):].strip()
    try:
        message = await asyncio.to_thread(knowledge.lookup_text, query)
    except Exception:
        logger.exception('/print-lookup failed')
        message = 'Lookup failed; the registries could not be read. Nothing was sent to the printer.'
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'print lookup handled'}


async def source_dispatch(event, gateway, **kwargs):
    """/print-source: which model sites are ready, need a login, or are listing-only (#462, #464).

    Read-only: it checks whether each site's credential is saved (and, for a
    JWT, not expired) without contacting any site, and never shows a value.
    """
    source, adapter = _control_preamble(event, gateway, '/print-source')
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'source UI delivery unavailable'}
    from print_handy.core import site_accounts
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    try:
        message = await asyncio.to_thread(site_accounts.source_report)
    except Exception:
        logger.exception('/print-source failed')
        message = 'Could not read the saved site logins. Nothing was changed.'
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'print source handled'}


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
    from print_handy.hermes import spool_sync
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
    try:
        if pid:
            from print_handy import settings
            model_key = settings.by_id(pid, _config.cfg())["model"]
            connection = printers.get(model_key,
                                      settings.view_for(pid, _config.cfg())).connection
        else:
            current = printers.current()
            connection = current.connection
            model_key = getattr(current, "key", None)
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
                                               args[1], args[2:], printer=model_key)
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


async def firmware_dispatch(event, gateway, **kwargs):
    """/print-firmware: the configured printer's firmware update state (issue #561).

    Bare form (stage 1) reports Moonraker's Update Manager state: one
    user-invoked check (a refresh, then the status read) through the spool
    client, rendered by print_handy.firmware_update with the ZMod pairing
    and Klipper 13 MCU extras where the data exposes them. Printers whose
    connection is not Moonraker have no Update Manager to read and get a
    plain explanation instead of an error. The report path is read-only.

    The update form (stage 2) applies one component: eligibility comes
    from the printer's registry profile (vendor Klipper forks and
    non-Moonraker printers are refused before anything is read), the
    component is resolved against the live status, the printer must be
    idle, and the usual _confirm button shows the exact version change
    before the single upgrade POST; the result is verified by re-reading
    the status, never taken from the POST alone.
    """
    source, adapter = _control_preamble(event, gateway, '/print-firmware', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'firmware UI delivery unavailable'}
    from print_handy import firmware_update, printers
    from print_handy.hermes import spool_sync
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    decided = await _decide_printer(source, gateway, adapter, metadata,
                                    question='Firmware on which printer?')
    if decided is None:
        return {'action': 'skip', 'reason': 'shared channel: no printer chosen for firmware'}
    pid, chosen = decided
    mr = spool_sync.moonraker_for(pid)
    args = str(getattr(event, 'text', '')).strip()[len('/print-firmware'):].split()
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
        entry = firmware_update.printer_entry(model_key)
    except Exception:
        logger.exception('/print-firmware could not resolve the configured printer')
    if args and args[0].casefold() == 'guide':
        if len(args) != 2 or args[1].casefold() not in ('zmod', 'stock'):
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=('Guides exist for the ZMod base '
                                        '(`/print-firmware guide zmod`) and for the stock '
                                        'Flashforge firmware under it (`/print-firmware '
                                        'guide stock`). Nothing was sent to the printer.'))
            return {'action': 'skip', 'reason': 'print firmware usage shown'}
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=firmware_update.guide_text(args[1].casefold(), entry))
        return {'action': 'skip', 'reason': 'print firmware guide shown'}
    if args and args[0].casefold() == 'update':
        if len(args) != 2:
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=('Use `/print-firmware update <component>` with one '
                                        'component name from the `/print-firmware` report '
                                        '(for example `/print-firmware update klipper`). '
                                        'One component at a time. Nothing was sent to '
                                        'the printer.'))
            return {'action': 'skip', 'reason': 'print firmware usage shown'}
        if connection != 'moonraker' or not firmware_update.is_apply_eligible(entry):
            reason = firmware_update.apply_block_reason(entry)
            message = (reason + ' Nothing was sent to the printer.') if reason else \
                firmware_update.not_moonraker_text(connection)
            await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
            return {'action': 'skip', 'reason': 'print firmware update refused for this printer'}
        key = ('print-firmware-update', source.chat_id, source.thread_id)
        if key in _seen:
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content='A firmware update prompt is already open here. Nothing sent.')
            return {'action': 'skip', 'reason': 'firmware update prompt already active'}
        _seen[key] = time.monotonic()
        live_key = _live_key_for(source, gateway, chosen)
        try:
            try:
                prepared = await asyncio.to_thread(firmware_update.prepare_update, mr,
                                                   entry, args[1])
            except (firmware_update.FirmwareRefusal, firmware_update.FirmwareError) as exc:
                message = str(exc)
            else:
                answer = await _confirm(adapter, source, metadata, 'print-firmware',
                                        firmware_update.update_prompt_text(prepared),
                                        [firmware_update.UPDATE_LABEL,
                                         firmware_update.CANCEL_LABEL],
                                        (globals().get('_config').safety_confirm_timeout_s()
                                         if globals().get('_config') else 600),
                                        live_key=live_key)
                if answer != firmware_update.UPDATE_LABEL:
                    message = 'Update not applied. Nothing sent.'
                else:
                    try:
                        message = await asyncio.to_thread(firmware_update.send_update, mr,
                                                          prepared)
                    except (firmware_update.FirmwareRefusal,
                            firmware_update.FirmwareError) as exc:
                        message = str(exc)
        except Exception:
            logger.exception('/print-firmware update failed')
            message = ('The firmware update stopped on an error. No confirmation was '
                       'completed, so nothing was sent.')
        finally:
            _seen.pop(key, None)
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
        return {'action': 'skip', 'reason': 'print firmware update handled'}
    if args:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=('Use `/print-firmware` to see the firmware versions '
                                    'your printer reports and what updates are available, '
                                    '`/print-firmware update <component>` to apply one, or '
                                    '`/print-firmware guide zmod` / `/print-firmware guide '
                                    'stock` for the manual update guides. Nothing was sent '
                                    'to the printer.'))
        return {'action': 'skip', 'reason': 'print firmware usage shown'}
    if connection != 'moonraker':
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=firmware_update.guidance_text(entry))
        return {'action': 'skip', 'reason': 'print firmware not applicable to this printer'}
    try:
        message = await asyncio.to_thread(firmware_update.firmware_report, mr, entry,
                                          refresh=True)
    except Exception:
        logger.exception('/print-firmware failed')
        message = firmware_update.COULD_NOT_READ_TEXT
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'print firmware handled'}


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
    from print_handy.hermes import spool_sync
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


async def plate_calibrate_dispatch(event, gateway, **kwargs):
    """/print-plate-calibrate [capture|status]: calibrate the local empty-plate reference (#455 item 4, Stage 1).

    capture snapshots the camera, stages the frame and shows it back;
    only the operator's "Make this my empty plate" answer promotes it
    to the live reference — Retake (or no answer) discards the staged
    frame and the previous reference keeps judging. The calibration
    logic lives in print_handy.hermes.local_empty so a Web UI surface
    can drive the same flow later; this command is the v1 surface.
    Nothing here moves the printer or starts a print.
    """
    source, adapter = _control_preamble(event, gateway, '/print-plate-calibrate', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'plate calibration UI delivery unavailable'}
    from print_handy.hermes import local_empty
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    cfg = _config.cfg()
    args = str(getattr(event, 'text', '')).strip()[len('/print-plate-calibrate'):].split()
    verb = args[0].casefold() if args else 'status'
    if verb == 'capture':
        if not local_empty.empty_enabled(cfg):
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=('Plate calibration is off: set vision.local_empty_enabled to true '
                                        'first, then run `/print-plate-calibrate capture` with the plate empty.'))
            return {'action': 'skip', 'reason': 'plate calibration not enabled'}
        try:
            frame = await asyncio.to_thread(local_empty.capture_frame, cfg)
            pending = await asyncio.to_thread(local_empty.begin_calibration, cfg, frame)
        except Exception as exc:
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=f'No plate reference captured: {exc}. Nothing changed.')
            return {'action': 'skip', 'reason': 'plate calibration capture failed'}
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=('This frame would become the empty plate reference the idle checks '
                                    'compare against.\nMEDIA:' + str(pending)))
        try:
            timeout = _config.safety_confirm_timeout_s()
        except Exception:
            timeout = 600
        live_key = gateway._session_key_for_source(source)
        answer = await _confirm(adapter, source, metadata, 'plate-calibrate',
                                'Make this frame your empty plate reference?',
                                ['Make this my empty plate', 'Retake'], timeout,
                                live_key=live_key)
        if answer == 'Make this my empty plate':
            stored = await asyncio.to_thread(local_empty.confirm_calibration,
                                             cfg, frame, force=True)
            if stored:
                _state, detail = local_empty.reference_status(cfg)
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content=f'Empty plate reference saved. {detail}')
                return {'action': 'skip', 'reason': 'plate reference captured'}
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=('The staged frame changed under the prompt, so nothing was '
                                        'stored. Run `/print-plate-calibrate capture` again.'))
            return {'action': 'skip', 'reason': 'plate reference capture refused'}
        await asyncio.to_thread(local_empty.discard_calibration, cfg)
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=('Reference not changed. `/print-plate-calibrate capture` '
                                    'takes another frame.'))
        return {'action': 'skip', 'reason': 'plate calibration discarded'}
    if verb == 'status':
        state, detail = local_empty.reference_status(cfg)
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content=f'Plate reference: {state}: {detail}')
        return {'action': 'skip', 'reason': 'plate calibration status shown'}
    await adapter.send(chat_id=source.chat_id, metadata=metadata,
                       content=('Use `/print-plate-calibrate capture` or '
                                '`/print-plate-calibrate status`. Nothing changed.'))
    return {'action': 'skip', 'reason': 'plate calibration usage shown'}


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


async def exclude_dispatch(event, gateway, **kwargs):
    """/exclude <name>: ask once, then one Moonraker exclude. The last object is /print-stop's confirm."""
    source, adapter = _control_preamble(event, gateway, '/exclude', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'exclude UI delivery unavailable'}
    from print_handy.core.exclude_object import exclude_plan, object_names
    from print_handy.hermes import discord_print_summary as summary
    from print_handy.hermes import exclude_chat, print_stop
    from print_handy.hermes import print_control as pc
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    text = str(getattr(event, 'text', '')).strip()
    target = text[len('/exclude'):].strip()
    pid = _routed_printer_id(source, gateway)
    if _undecided_printer_ids(source, gateway):
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='This chat is not tied to one printer. Nothing excluded.')
        return {'action': 'skip', 'reason': 'shared channel: no exclude target'}
    key = ('exclude', source.chat_id, source.thread_id, target)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='An exclude prompt for that object is already open. Nothing sent.')
        return {'action': 'skip', 'reason': 'exclude prompt already active'}
    _seen[key] = time.monotonic()
    live_key = gateway._session_key_for_source(source)
    try:
        live = functools.partial(exclude_chat.read_live, pid)
        try:
            st = await asyncio.to_thread(live)
        except Exception:
            st = None
        if st is None:
            message = 'Could not check the printer, so no exclude was offered. Nothing sent.'
        elif st.get('state') not in print_stop.ACTIVE:
            message = f"Nothing to exclude: printer is {st.get('state')}. Nothing sent."
        else:
            _job, plate = await asyncio.to_thread(summary.find_job, st.get('filename') or '')
            path = (plate or {}).get('gcode') if isinstance(plate, dict) else None
            try:
                gcode = Path(path).read_text(encoding='utf-8', errors='replace') if path else ''
            except OSError:
                gcode = ''
            names = object_names(gcode)
            left = exclude_chat.still_printing(names, st)
            plan = exclude_plan(names, target, confirmed=False, remaining=left)
            if plan['action'] == 'refuse' or not names:
                message = plan['reason'] + ' Nothing sent.'
            elif plan['action'] == 'cancel_print':
                answer = await _confirm(adapter, source, metadata, 'exclude-stop',
                                        plan['reason'] + ' Stop the whole print?',
                                        [print_stop.STOP_LABEL, print_stop.KEEP_LABEL],
                                        (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600),
                                        live_key=live_key)
                if answer != print_stop.STOP_LABEL:
                    message = 'Kept printing. Nothing sent.'
                else:
                    _sent, message = await asyncio.to_thread(print_stop.cancel, st.get('filename') or '', **_pkw(pid))
            else:
                answer = await _confirm(adapter, source, metadata, 'exclude',
                                        plan['reason'],
                                        ['Cancel this object', 'Keep it'],
                                        (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600),
                                        live_key=live_key)
                if answer != 'Cancel this object':
                    message = 'Kept that object. Nothing sent.'
                else:
                    def sender(script):
                        pc._send('/printer/gcode/script', method='POST', data={'script': script},
                                 timeout=30, **_pkw(pid))
                    result = await asyncio.to_thread(
                        exclude_chat.confirm_send,
                        {'kind': 'exclude-go', 'name': target, 'names': names,
                         'filename': st.get('filename') or ''}, sender, live)
                    message = result['text']
    except Exception as exc:
        logger.exception('exclude failed')
        message = _failure_post('Exclude', exc, 'Check the printer screen. Nothing was excluded.', subsystem='printer')
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic exclude handled'}


async def print_stop_dispatch(event, gateway, **kwargs):
    """/print-stop: one confirm button, then at most one Moonraker cancel for the confirmed file."""
    source = getattr(event, 'source', None)
    text = str(getattr(event, 'text', '')).strip()
    if text != '/print-stop':
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
        return {'action': 'skip', 'reason': 'print stop UI delivery unavailable'}
    import uuid
    from print_handy.hermes import print_stop
    from print_handy.hermes import discord_print_summary as summary
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    pid = _routed_printer_id(source, gateway)
    key = ('print-stop', source.chat_id, source.thread_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='A stop prompt is already open. Nothing sent.')
        return {'action': 'skip', 'reason': 'stop prompt already active'}
    _seen[key] = time.monotonic()
    live_key = gateway._session_key_for_source(source)
    decided_status = None
    if not pid:
        candidates = _undecided_printer_ids(source, gateway)
        if candidates:
            # "Stop the current print" in a shared channel (#536): the
            # printer with a print running is the target. Two running
            # prints is a question, never a guess; none running refuses,
            # naming the printers it checked.
            statuses = await _candidate_statuses(candidates, print_stop.status)
            active = {cid: st for cid, st in statuses.items()
                      if isinstance(st, dict) and st.get('state') in print_stop.ACTIVE}
            chosen = None
            if len(active) == 1:
                chosen = next(iter(active))
            elif len(active) > 1:
                chosen = await _ask_which_printer(
                    source, gateway, adapter, list(active),
                    question='More than one printer has a print running. Stop which one?',
                    label_for=lambda cid: _busy_label(active.get(cid)), metadata=metadata)
            if chosen is None:
                _seen.pop(key, None)
                names = ' or '.join(_printer_label(c) for c in candidates)
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content=('No printer chosen. Nothing sent.' if active else
                                            f'Nothing to stop: no print is running on {names}. Nothing sent.'))
                return {'action': 'skip', 'reason': 'shared channel: no stop target decided'}
            pid, decided_status = _routed_form(chosen), active[chosen]
            live_key = _live_key_for(source, gateway, chosen)
    try:
        st = decided_status if decided_status is not None else await asyncio.to_thread(print_stop.status, **_pkw(pid))
        if st.get('camera_image_path'):
            # Bambu camera parity (#294): show the camera image alongside the status.
            try:
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content=f"Camera now — {st['filename'] or 'current print'}:\nMEDIA:{st['camera_image_path']}")
            except Exception:
                pass
        if st['state'] not in print_stop.ACTIVE:
            message = f"Nothing to stop: printer is {st['state']}. Nothing sent."
        else:
            from print_handy.core.prompts import gateway as _prompts
            cm = _prompts()
            name = summary.format_event('started', st['filename'], emit=False).splitlines()[0].split(': ', 1)[-1]
            where = print_stop.where_text(st)
            question = f"Stop {name} ({st['state']}{', ' + where if where else ''})? This cancels the print."
            choices = [print_stop.STOP_LABEL, print_stop.KEEP_LABEL]
            cid = uuid.uuid4().hex
            session_key = f'print-stop:{source.chat_id}:{source.thread_id}'
            # Register under the live chat session key (the _confirm pattern):
            # typed replies route by that key, so a flow-key registration can
            # never be answered on buttonless platforms. send_clarify keeps
            # the flow key for delivery.
            cm.register(cid, live_key or session_key, question, choices)
            try:
                sent = await adapter.send_clarify(chat_id=str(source.chat_id), question=question,
                                                  choices=choices, clarify_id=cid,
                                                  session_key=session_key, metadata=metadata)
                if not getattr(sent, 'success', False):
                    raise RuntimeError('button delivery failed')
                answer = await asyncio.to_thread(cm.wait_for_response, cid, (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600))
            finally:
                cm.resolve_gateway_clarify(cid, cm.CANCELLED)
            if answer != print_stop.STOP_LABEL:
                message = 'Kept printing. Nothing sent.'
            else:
                _sent, message = await asyncio.to_thread(print_stop.cancel, st['filename'], **_pkw(pid))
    except Exception as exc:
        logger.exception('print-stop failed')
        message = _failure_post('Stop', exc, 'Check the printer screen.', subsystem='printer')
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic print stop handled'}


def _control_preamble(event, gateway, command, with_args=False):
    """Shared gate for the /print-resume, /print-again and /print-reboot commands."""
    text = str(getattr(event, 'text', '')).strip()
    if not (text == command or with_args and text.startswith(command + ' ')):
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
        return await asyncio.to_thread(cm.wait_for_response, cid, timeout)
    finally:
        cm.resolve_gateway_clarify(cid, cm.CANCELLED)
        _prompt_marks.forget_approval(cid)


async def print_resume_dispatch(event, gateway, **kwargs):
    """/print-resume: one confirm button, then at most one Moonraker resume. Never restarts anything."""
    source, adapter = _control_preamble(event, gateway, '/print-resume')
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'print resume UI delivery unavailable'}
    from print_handy.hermes import print_control as pc
    from print_handy.hermes import discord_print_summary as summary
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    pid = _routed_printer_id(source, gateway)
    key = ('print-resume', source.chat_id, source.thread_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content='A resume prompt is already open. Nothing sent.')
        return {'action': 'skip', 'reason': 'resume prompt already active'}
    _seen[key] = time.monotonic()
    live_key = gateway._session_key_for_source(source)
    decided_status = None
    if not pid:
        candidates = _undecided_printer_ids(source, gateway)
        if candidates:
            # "Resume the current print" in a shared channel (#536): the
            # printer with a resumable print is the target; two resumable
            # prints is a question; none refuses, naming the printers.
            statuses = await _candidate_statuses(candidates, pc.status)
            resumable = {cid: st for cid, st in statuses.items()
                         if isinstance(st, dict) and pc.resumable(st)}
            chosen = None
            if len(resumable) == 1:
                chosen = next(iter(resumable))
            elif len(resumable) > 1:
                chosen = await _ask_which_printer(
                    source, gateway, adapter, list(resumable),
                    question='More than one printer has a paused print. Resume which one?',
                    label_for=lambda cid: _busy_label(resumable.get(cid)), metadata=metadata)
            if chosen is None:
                _seen.pop(key, None)
                names = ' or '.join(_printer_label(c) for c in candidates)
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content=('No printer chosen. Nothing sent.' if resumable else
                                            f'Nothing to resume: no paused print on {names}. Nothing sent.'))
                return {'action': 'skip', 'reason': 'shared channel: no resume target decided'}
            pid, decided_status = _routed_form(chosen), resumable[chosen]
            live_key = _live_key_for(source, gateway, chosen)
    try:
        st = decided_status if decided_status is not None else await asyncio.to_thread(pc.status, **_pkw(pid))
        if not pc.resumable(st):
            message = pc.not_resumable_text(st)
        else:
            name = summary.format_event('started', st['filename'], emit=False).splitlines()[0].split(': ', 1)[-1]
            where = pc.where_text(st)
            answer = await _confirm(adapter, source, metadata, 'print-resume',
                                    f"Resume {name}{' (' + where + ')' if where else ''}? Make sure nothing is in the way.",
                                    [pc.RESUME_LABEL, pc.KEEP_LABEL], (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600),
                                    live_key=live_key)
            if answer != pc.RESUME_LABEL:
                message = 'Left it paused. Nothing sent.'
            else:
                _sent, message = await asyncio.to_thread(pc.resume, st['filename'], **_pkw(pid))
    except Exception as exc:
        logger.exception('print-resume failed')
        message = _failure_post('Resume', exc, 'Check the printer screen.', subsystem='printer')
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic print resume handled'}


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


async def roast_dispatch(event, gateway, **kwargs):
    """"roast my last print" / "roast print 84" / "roast the benchie" (#1002).

    Plain language, typo tolerant through the shared fuzzy layer, and
    read-only: the print is resolved the way 'print 84' and /print-again
    resolve it, and the line comes from the local roast corpus — no LLM,
    nothing sent to the printer. Off with vision.roast_enabled=false.
    """
    from print_handy.hermes import roast_chat
    text = str(getattr(event, 'text', '')).strip()
    if roast_chat.roast_request(text) is None:
        return None
    source, adapter = _control_gate(event, gateway)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'roast UI delivery unavailable'}
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    vision = _config.cfg().get('vision') or {}
    if not vision.get('roast_enabled', True):
        message = 'Roasts are turned off (vision.roast_enabled is false).'
    else:
        from print_handy.hermes import _config as hermes_config
        state_path = hermes_config.state_file('PRINT_HANDY_ROAST_STATE', 'roast_state.json')
        try:
            message = await asyncio.to_thread(roast_chat.roast_reply, text, state_path=state_path,
                                              tz=hermes_config.tz())
        except Exception:
            logger.exception('roast request failed')
            message = 'No roast this time; the print history could not be read.'
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message or 'No roast this time.')
    return {'action': 'skip', 'reason': 'roast handled'}


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
    from print_handy.hermes import print_control as pc
    from print_handy.hermes import print_next
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
            from print_handy.hermes import discord_print_summary as print_summary
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
            ok, detail = await print_next.start(target)
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


def _print_restart_request(event):
    """Recognize direct natural-language requests, not mere mentions of restarting."""
    text = str(getattr(event, 'text', '') or '').strip().casefold()
    if re.search(r'\b(?:do not|don.t|never|avoid|without)\b.{0,30}\b(?:restart|reboot|power[- ]?cycle)\b', text):
        return False
    if re.search(r'\b(?:should|could|would|can|may|need to|want to)\b.{0,25}\b(?:restart|reboot|power[- ]?cycle)\b', text):
        return True
    return bool(re.fullmatch(r'(?:please\s+)?(?:restart|reboot)(?:\s+(?:the\s+)?(?:printer|machine|firmware))?[.! ]*', text)
                or re.search(r'\b(?:restart|reboot)\s+(?:the\s+)?(?:printer|machine|firmware)\b', text))


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
    _ok, message = await asyncio.to_thread(pc.force_reboot, **_pkw(printer_id))
    return message + (' Retry the print manually.' if _ok else '')


async def print_reboot_dispatch(event, gateway, **kwargs):
    """Natural restart requests use the safe ZMod REBOOT button flow."""
    source, adapter = _control_preamble(event, gateway, '/print-reboot')
    if source is None:
        if not _is_print_discord_event(event):
            return None
        text = str(getattr(event, 'text', '') or '').strip().casefold()
        if not _print_restart_request(event):
            return None
        source = event.source
        if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
            return None
        adapter = _adapter_for(gateway, source)
        if adapter is None:
            return {'action': 'skip', 'reason': 'print reboot UI delivery unavailable'}
    if adapter is None:
        return {'action': 'skip', 'reason': 'print reboot UI delivery unavailable'}
    from print_handy.hermes import print_control as pc
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    # The routing helpers are resolved defensively: an offline regression
    # test execs this function alone in a bare namespace (test_reboot_standby).
    _route = globals().get('_routed_printer_id')
    pid = _route(source, gateway) if _route else None
    pkw = {'printer_id': pid} if pid else {}
    key = ('print-reboot', source.chat_id, source.thread_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content='A restart is already open or running. Nothing sent.')
        return {'action': 'skip', 'reason': 'reboot already active'}
    _seen[key] = time.monotonic()
    try:
        live_key = gateway._session_key_for_source(source)
    except Exception:
        live_key = None  # a bare-namespace host may not offer session keys
    _decide = globals().get('_decide_printer')
    if _decide is not None and not pid:
        decided = await _decide(source, gateway, adapter, metadata,
                                question='Restart which printer?')
        if decided is None:
            _seen.pop(key, None)
            return {'action': 'skip', 'reason': 'shared channel: no printer chosen for reboot'}
        pid, chosen = decided
        pkw = {'printer_id': pid} if pid else {}
        _scoped = globals().get('_live_key_for')
        if _scoped is not None:
            live_key = _scoped(source, gateway, chosen)
    try:
        st = await asyncio.to_thread(pc.status, **pkw)
        if st.get('klippy') == 'shutdown' and st.get('state') != 'printing':
            # Emergency stop: REBOOT is a G-code macro Klipper rejects while shut down.
            # One FIRMWARE_RESTART clears it; no motion, no heat. Still needs the confirm button.
            from print_handy.hermes import print_alert_buttons as pab
            reason = await asyncio.to_thread(pab.shutdown_message, **pkw)
            answer = await _confirm(adapter, source, metadata, 'print-reboot',
                                    "The printer is in an emergency stop. " + pab.explain(reason)
                                    + "\nClear it with one firmware restart? No motion or heat. This ends the "
                                    "stopped job for good. Only do this after the bed and nozzle are clear.",
                                    [pc.REBOOT_LABEL, pc.CANCEL_REBOOT_LABEL], (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600),
                                    live_key=live_key)
            if answer != pc.REBOOT_LABEL:
                message = 'Not restarted. Nothing sent.'
            else:
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content='Clearing the emergency stop now. Up to a minute.')
                _ok, message = await asyncio.to_thread(pab.clear_shutdown, **pkw)
                after = await asyncio.to_thread(pc.status, **pkw)
                message += f" Job state now {after.get('state')!r}."
        elif st.get('state') not in {'idle', 'standby', 'complete', 'cancelled', 'error'} or st.get('klippy') != 'ready':
            message = (f"Refused: printer job state is {st.get('state')!r}, controller state is "
                       f"{st.get('klippy')!r}. Check the printer screen before any restart. Nothing sent.")
        else:
            answer = await _confirm(adapter, source, metadata, 'print-reboot',
                                    "Force restart the AD5X? This clears a stuck screen or heaters, and it wipes "
                                    "any Resume the printer screen is offering. Takes about two minutes.",
                                    [pc.REBOOT_LABEL, pc.CANCEL_REBOOT_LABEL], (globals().get('_config').safety_confirm_timeout_s() if globals().get('_config') else 600),
                                    live_key=live_key)
            if answer != pc.REBOOT_LABEL:
                message = 'Not restarted. Nothing sent.'
            else:
                await adapter.send(chat_id=source.chat_id, metadata=metadata,
                                   content='Restarting the printer now. About two minutes.')
                _ok, message = await asyncio.to_thread(pc.force_reboot, **pkw)
    except Exception as exc:
        logger.exception('print-reboot failed')
        message = _failure_post('Restart', exc, 'Check the printer screen.', subsystem='printer')
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic print reboot handled'}


BED_DOWN_LABEL = 'Separate bed and nozzle'
CANCEL_BED_DOWN_LABEL = 'Cancel'


async def bed_down_dispatch(event, gateway, **kwargs):
    """/print-bed-down: prepare to clean by separating the bed and nozzle.

    The plain-language forms ("reset the bed", "lower the bed", "prepare to
    clean") reach this only through the did-you-mean prompt, so a fuzzy
    match never moves anything by itself. Refused while a print is active;
    asks before moving; and print_handy.hermes.bed_separate moves only when
    Klipper reports Z homed and the bounded target fits, otherwise it posts
    instructions and sends nothing. No homing, no heat.
    """
    source, adapter = _control_preamble(event, gateway, '/print-bed-down')
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'bed down UI delivery unavailable'}
    from print_handy.hermes import bed_separate as bs
    from print_handy.hermes import print_control as pc
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    _route = globals().get('_routed_printer_id')
    pid = _route(source, gateway) if _route else None
    key = ('print-bed-down', source.chat_id, source.thread_id)
    if key in _seen:
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='A bed separation is already open or running. Nothing sent.')
        return {'action': 'skip', 'reason': 'bed down already active'}
    _seen[key] = time.monotonic()
    try:
        live_key = gateway._session_key_for_source(source)
    except Exception:
        live_key = None
    try:
        _decide = globals().get('_decide_printer')
        if _decide is not None and not pid:
            decided = await _decide(source, gateway, adapter, metadata,
                                    question='Separate the bed and nozzle on which printer?')
            if decided is None:
                return {'action': 'skip', 'reason': 'shared channel: no printer chosen for bed down'}
            pid, chosen = decided
            _scoped = globals().get('_live_key_for')
            if _scoped is not None:
                live_key = _scoped(source, gateway, chosen)
        connection = await asyncio.to_thread(bs.printer_connection, pid)
        st = None
        if bs.supports_live_move(connection):
            try:
                st = await asyncio.to_thread(pc.status, **_pkw(pid))
            except Exception:
                logger.warning('print-bed-down: status read failed', exc_info=True)
                st = None
        state = st.get('state') if isinstance(st, dict) else None
        if not bs.supports_live_move(connection):
            message = bs.manual_instructions(
                bs.separate_mm(), 'This printer is not on a Moonraker/Klipper connection.')
        elif st is None:
            message = bs.manual_instructions(
                bs.separate_mm(), "I couldn't read the printer state, so nothing moves.")
        elif state in {'printing', 'paused'}:
            message = (f"Refused: the printer is {state}. Separating the bed and nozzle is only offered "
                       "when the printer is idle or stopped. Nothing sent.")
        else:
            distance = bs.separate_mm()
            motion = await asyncio.to_thread(bs.printer_motion, pid)
            answer = await _confirm(adapter, source, metadata, 'print-bed-down',
                                    bs.confirm_question(distance, motion),
                                    [BED_DOWN_LABEL, CANCEL_BED_DOWN_LABEL],
                                    (globals().get('_config').safety_confirm_timeout_s()
                                     if globals().get('_config') else 600),
                                    live_key=live_key)
            if answer != BED_DOWN_LABEL:
                message = 'Not moved. Nothing sent.'
            else:
                _ok, message = await asyncio.to_thread(
                    bs.separate, confirmed_at=time.time(), distance_mm=distance, printer_id=pid)
    except Exception as exc:
        logger.exception('print-bed-down failed')
        message = _failure_post('Bed separation', exc, 'Check the printer screen.', subsystem='printer')
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, content=message, metadata=metadata)
    return {'action': 'skip', 'reason': 'deterministic bed down handled'}


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


async def typed_quantity_dispatch(event, gateway, **kwargs):
    """A typed reply ("4", "3 copies") while a print quantity prompt is open answers that prompt."""
    if not _is_print_discord_event(event):
        return None
    source = event.source
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    from print_handy.core.link_intake import typed_reply
    error = typed_reply(source, getattr(event, 'text', ''))
    if error is None:
        return None
    from print_handy.core.link_intake import take_secret_reply
    if error == '' and take_secret_reply():  # a pasted login token: remove that message
        await _delete_secret_message(event, source, _adapter_for(gateway, source))
    if error:
        adapter = _adapter_for(gateway, source)
        if adapter is not None:
            await adapter.send(chat_id=source.chat_id, content=error,
                               metadata={'thread_id': source.thread_id} if source.thread_id else None)
    return {'action': 'skip', 'reason': 'typed print quantity handled'}


def _recent_model_link(source):
    """The last model link in this channel, from Print Handy's own recent-links state (#524)."""
    from print_handy.bot import recent_links
    return recent_links.recall(getattr(source, 'chat_id', None), getattr(source, 'thread_id', None))


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
    model_files = getattr(_intake_module, 'model_files', lambda event: [])
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
            from print_handy.hermes import print_control as _print_control
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
        return {'action': 'skip', 'reason': 'new model intake unavailable; no start authorized'}
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




def alert_button_handlers(bot, adapter):
    """Handle listener buttons, including IFS inventory choices, for authorized Discord users."""
    from print_handy.hermes import print_alert_buttons as pab

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
        from print_handy.hermes import print_alert_buttons as pab
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
        from print_handy.hermes import spool_sync
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
        from print_handy.hermes import spool_sync
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
        from print_handy.hermes import spool_label

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
            from print_handy.hermes import spool_sync
            values = {'brand': state.get('brand') or '', 'material': state.get('material') or '',
                      'color': state.get('color') or '', 'grams': str(state.get('grams') or ''), 'lot': '',
                      'nozzle_temp': state.get('nozzle_temp'), 'bed_temp': state.get('bed_temp'),
                      'hex': state.get('hex')}
            if state.get('origin') == 'spool-question':
                from print_handy.hermes import moonraker_event_listener as listener
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
        from print_handy.hermes import spool_sync
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
                from print_handy.hermes import moonraker_event_listener as listener
                from print_handy.hermes import spool_sync
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
            from print_handy.hermes import moonraker_event_listener as listener
            from print_handy.hermes import spool_sync
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
        from print_handy.hermes import spool_sync
        from print_handy.hermes.discord_print_summary import spool_picker_label
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
            from print_handy.hermes import spool_sync
            if pending.get('origin') == 'insertion':
                result = await asyncio.to_thread(
                    pab.bind_insertion_existing, pending['slot'], pending['spool_id'], spool_sync,
                    expected_prior_spool_id=(int(pending['old_spool_id'])
                                             if pending.get('old_spool_id') else None))
            else:
                from print_handy.hermes import moonraker_event_listener as listener
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
        from print_handy.hermes import spool_sync
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
            from print_handy.hermes import spool_sync
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
        from print_handy.hermes import spool_sync
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
        from print_handy.hermes import print_next
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
        from print_handy.hermes import next_plate_offer
        with contextlib.suppress(Exception):
            await asyncio.to_thread(next_plate_offer.clear)
        await interaction.response.edit_message(content='Left the queue alone. Nothing started.', view=None)

    async def handle_plate_go(interaction, aid):
        """The one Confirm tap. The alert is single use and print_next.start re-checks the printer and claim."""
        from print_handy.hermes import print_next
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
        from print_handy.hermes import print_next
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
        except Exception as exc:
            logger.exception('plate picker start failed')
            message = _failure_post('Next plate', exc, 'Check the printer screen before retrying.', subsystem='printer')
        if ok:  # start accepted: the claim suppresses the listener's own post,
            # so announce and register the start here (P2-56).
            await _announce_command_start(lambda content: interaction.followup.send(content),
                                          target, detail, chat_id=str(getattr(interaction, 'channel_id', '') or '') or None)
        if message:
            await (interaction.followup.send(message, view=view) if view else interaction.followup.send(message))

    async def handle_plate_spool(interaction, aid):
        """Single-use 'use the loaded spool' tap. start() still re-checks the printer, claim and live slot."""
        from print_handy.hermes import print_next
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
        from print_handy.hermes import print_alert_buttons as pab, print_next
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
        from print_handy.hermes import exclude_chat, print_stop
        from print_handy.hermes import print_control as pc
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
                _sent, message = await asyncio.to_thread(print_stop.cancel, alert.get('filename') or '', printer_id=printer_id)
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
        from print_handy.hermes import exclude_chat
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
                   'spool': handle_plate_spool, 'drop': handle_plate_drop}[plate[0]](interaction, plate[1])
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


# ------------------------------------------------------------------ chat model edits (#903)
# The edit flow lives in print_handy/bot/edit_chat.py; this is its
# registration. The host gives it this module's helpers at call time, so a
# test that patches a helper here is seen there too.


class _EditHost:
    def __getattr__(self, name):
        try:
            return globals()[name]
        except KeyError:
            raise AttributeError(name) from None


async def edit_dispatch(event, gateway, **kwargs):
    from print_handy.bot import edit_chat
    return await edit_chat.edit_dispatch(event, gateway, host=_EditHost(), **kwargs)


# The pre_gateway_dispatch handlers in registration order — the order the
# standalone Router runs them. register() installs this list.
_DISPATCH_HOOKS = (
    guided_dispatch,
    design_dispatch,
    maintenance_dispatch,
    coldpull_dispatch,
    lookup_dispatch,
    source_dispatch,
    troubleshoot_dispatch,
    macros_dispatch,
    firmware_dispatch,
    calibration_dispatch,
    typed_quantity_dispatch,
    upload_wait_dispatch,
    model_link_dispatch,
    color_selection_dispatch,
    print_next_dispatch,
    runout_intake_dispatch,
    exclude_dispatch,
    print_stop_dispatch,
    print_resume_dispatch,
    roast_dispatch,
    print_again_dispatch,
    edit_dispatch,
    loaded_spools_dispatch,
    purchase_dispatch,
    spool_dispatch,
    print_reboot_dispatch,
    bed_down_dispatch,
    live_dispatch,
    plate_calibrate_dispatch,
    nozzle_dispatch,
)

def register(ctx):
    """Install the handler set on the bot's Router (the only host left)."""
    for hook in _DISPATCH_HOOKS:
        ctx.register_hook('pre_gateway_dispatch', hook)
    if hasattr(ctx, 'register_platform_handler'):
        ctx.register_platform_handler('discord', alert_button_handlers)
