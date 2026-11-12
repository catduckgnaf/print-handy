"""The guided chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio, time

from .common import _adapter_for, _delete_secret_message, _is_print_discord_event, _platform_value, _state_dir
from ...hermes import _config

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy_print_feedback")


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


# --- handler interface (#1323) ---
from ._gates import free_text  # noqa: E402

match = free_text
handle = guided_dispatch
