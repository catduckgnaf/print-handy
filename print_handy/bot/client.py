"""The discord.py client for the standalone bot: gateway events, slash commands, heartbeat."""
import asyncio
import errno
import logging
import random
import socket
import sys
import time
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands as ext_commands

from . import commands, health
from .runtime import BotCore

logger = logging.getLogger(__name__)

HEARTBEAT_S = 15
STARTUP_ATTEMPTS = 5
_STARTUP_RETRY_BASE_S = 10.0
_STARTUP_RETRY_MAX_S = 80.0
_STARTUP_TRANSIENT_ERRNOS = frozenset({
    getattr(socket, "EAI_AGAIN", -3),  # temporary DNS failure: "Temporary failure in name resolution"
    errno.EAGAIN,
    errno.EWOULDBLOCK,
    errno.ECONNABORTED,
    errno.ECONNREFUSED,
    errno.ECONNRESET,
    errno.EHOSTUNREACH,
    errno.ENETUNREACH,
    errno.ETIMEDOUT,
})
_startup_sleep = time.sleep

# aiohttp 3.10 added ClientConnectorDNSError; older releases — still
# permitted, since requirements floors discord.py, not aiohttp — do not
# define it, and an unguarded lookup inside the classifier raised
# AttributeError over the real startup failure (P2-136). On those versions
# a DNS failure arrives as a plain ClientConnectorError and classifies
# through its OS error below.
_DNS_ERROR = getattr(aiohttp, "ClientConnectorDNSError", None)


def _is_transient_startup_error(exc):
    """Whether ``exc`` is a bounded-retry candidate during Discord startup (#364).

    Only connection establishment failures are retried: DNS and connector errors,
    connection timeouts, and Discord gateway/HTTP failures with a transient
    status. Certificate failures are configuration/trust failures, not network
    weather, and connector errors are retried only when the underlying OS error
    is itself transient. Login, privileged-intent, configuration and ordinary
    programming errors are never retried.
    """
    if isinstance(exc, (discord.LoginFailure, discord.PrivilegedIntentsRequired,
                        aiohttp.ClientConnectorCertificateError)):
        return False
    if _DNS_ERROR is not None and isinstance(exc, _DNS_ERROR):
        return True
    if isinstance(exc, aiohttp.ClientConnectorError):
        cause = exc.os_error
        return cause is not None and _is_transient_startup_error(cause)
    if isinstance(exc, discord.HTTPException):
        status = exc.status or 0
        return status == 429 or status >= 500
    if isinstance(exc, discord.GatewayNotFound):
        return True
    if isinstance(exc, aiohttp.WSServerHandshakeError):
        return exc.status == 429 or exc.status >= 500
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    if isinstance(exc, aiohttp.ClientConnectionError):
        return True
    if isinstance(exc, OSError):
        return exc.errno in _STARTUP_TRANSIENT_ERRNOS
    return False


def _startup_retry_delay(attempt):
    """Exponential backoff with jitter for the failed 1-based ``attempt``.

    The nominal delays after attempts 1-4 are 10, 20, 40 and 80 seconds
    (150 seconds total), jittered by +/-20%, so five attempts span roughly
    two to three minutes when each connection attempt itself is fast.
    """
    base = min(_STARTUP_RETRY_MAX_S, _STARTUP_RETRY_BASE_S * (2 ** (attempt - 1)))
    return random.uniform(base * 0.8, base * 1.2)


def run_bot(cfg):
    """Start the Discord bot for ``cfg``; the entry point ``print_handy.bot.run`` delegates here.

    Kept in the Discord client module so platform-specific login failures are
    translated where ``discord`` is imported (#164).

    Transient startup failures get up to ``STARTUP_ATTEMPTS`` fresh bot
    instances with bounded backoff (#364). Auth and configuration failures
    still exit immediately with code 3. If the final attempt fails, its
    exception is re-raised exactly as before, leaving the process supervisor
    and container restart policy as the outer backstop.
    """
    from ..runtime import _config
    token = _config.discord_token()
    for attempt in range(1, STARTUP_ATTEMPTS + 1):
        try:
            bot = PrintHandyBot(cfg)
            bot.warm_worker = True  # the production loop keeps a warm heavy worker (#1332)
            bot.run(token, log_handler=None)
        except discord.LoginFailure as exc:
            from .. import diagnostics
            print(f"Discord refused the bot token: {exc}. Check that the token in the environment "
                  "variable named by discord.token_env is the current bot token. "
                  # #924: stderr is a plain-text surface, so the tail is the
                  # bare code plus its docs URL, not Markdown link syntax.
                  f"{diagnostics.plain_reference_tail('PH_DISCORD_CONFIGURATION')}",
                  file=sys.stderr)
            return 3
        except discord.PrivilegedIntentsRequired:
            from .. import diagnostics
            print("Turn on the Message Content intent for this bot in the Discord developer portal "
                  "(Bot > Privileged Gateway Intents). "
                  f"{diagnostics.plain_reference_tail('PH_DISCORD_CONFIGURATION')}",
                  file=sys.stderr)
            return 3
        except Exception as exc:
            if not _is_transient_startup_error(exc):
                raise
            error_errno = getattr(exc, "errno", None)
            if attempt >= STARTUP_ATTEMPTS:
                from .. import diagnostics
                logger.warning(
                    "DISCORD_STARTUP_RETRY attempt %d/%d failed: %s (errno=%s); no retries left [%s]",
                    attempt, STARTUP_ATTEMPTS, type(exc).__name__, error_errno,
                    diagnostics.marker("discord", diagnostics.classify(exc)))
                raise
            delay = _startup_retry_delay(attempt)
            logger.warning(
                "DISCORD_STARTUP_RETRY attempt %d/%d failed: %s (errno=%s); retrying in %.1f seconds",
                attempt, STARTUP_ATTEMPTS, type(exc).__name__, error_errno, delay)
            _startup_sleep(delay)
        else:
            return 0


# ------------------------------------------------------- autocomplete (#1475)
# Providers answer from the lists the bot already holds. Every one is
# fail-soft: a printer or Spoolman that cannot be read offers no choices
# instead of failing the keystroke, and the typed form still works.

async def _ac_jobs(interaction, current):
    from ..runtime import print_control
    rows = await asyncio.to_thread(print_control.printed_jobs)
    return [app_commands.Choice(name=f"#{row['index']} {row['title']}"[:100], value=str(row["index"]))
            for row in rows if not current or current.casefold() in str(row["title"]).casefold()
            or current == str(row["index"])][:25]


async def _ac_plans(interaction, current):
    from ..runtime.print_records import PLANS_DIR
    try:
        names = sorted(p.stem for p in PLANS_DIR.glob("*.json"))
    except OSError:
        names = []
    return [app_commands.Choice(name=n[:100], value=n) for n in names
            if not current or current.casefold() in n.casefold()][:25]


async def _ac_plates(interaction, current):
    plan_id = getattr(getattr(interaction, "namespace", None), "plan_id", None)
    if not plan_id:
        return []
    try:
        from ..runtime import print_next
        plan = await asyncio.to_thread(print_next.load_plan, str(plan_id))
        parts = print_next.remaining_parts(plan)
    except Exception:
        logger.debug("plate autocomplete could not load plan %s", plan_id, exc_info=True)
        return []
    out = []
    for part, _index in parts:
        pid = str(part.get("id") or "")
        if pid and (not current or current.casefold() in pid.casefold()):
            out.append(app_commands.Choice(name=pid[:100], value=pid))
    return out[:25]


async def _ac_spools(interaction, current):
    from ..runtime import spool_sync
    rows = await asyncio.to_thread(spool_sync.shelf_spools)
    return [app_commands.Choice(name=f"#{row['spool_id']} {row['name']}"[:100], value=str(row["spool_id"]))
            for row in rows if not current or current in str(row["spool_id"])
            or current.casefold() in str(row["name"]).casefold()][:25]


async def _ac_slots(interaction, current):
    from ..runtime import spool_sync
    return [app_commands.Choice(name=f"Slot {slot}", value=str(slot)) for slot in spool_sync.SLOTS
            if not current or current in str(slot)][:25]


async def _ac_macros(interaction, current):
    from .. import macros as macro_lib
    from ..runtime import spool_sync
    try:
        listing = await asyncio.to_thread(macro_lib.discover, spool_sync.moonraker_for(None))
        names = [m.name for m in listing.macros]
    except Exception:
        logger.debug("macro autocomplete could not list macros", exc_info=True)
        names = []
    return [app_commands.Choice(name=n[:100], value=n) for n in names
            if not current or current.casefold() in n.casefold()][:25]


async def _ac_printers(interaction, current):
    from .. import settings
    try:
        entries = settings.current().get("printers") or []
    except Exception:
        logger.debug("printer autocomplete could not read the roster", exc_info=True)
        entries = []
    out = []
    for entry in entries:
        pid = str(entry.get("id") or "")
        if pid and (not current or current.casefold() in pid.casefold()
                    or current.casefold() in str(entry.get("name") or "").casefold()):
            out.append(app_commands.Choice(name=str(entry.get("name") or pid)[:100], value=pid))
    return out[:25]


AUTOCOMPLETE_PROVIDERS = {
    "job": _ac_jobs, "plan": _ac_plans, "plate": _ac_plates, "spool": _ac_spools,
    "slot": _ac_slots, "macro": _ac_macros, "printer": _ac_printers,
}


def _make_callback(core_ref, registered, options, extra=None, view_factory=None):
    """An app-command callback with one real parameter per table option.

    discord.py reads the option set from the callback's signature, so the
    function is built with the exact parameter names and types the table
    declares (``Optional[int]`` for integer options, else ``Optional[str]``).
    The callback hands the picked values to ``BotCore.run_slash``, which
    maps them back to the typed text form — handlers never see Discord
    shapes. ``extra`` carries fixed values (a picked subcommand's name).
    """
    namespace = {"Optional": Optional, "Interaction": discord.Interaction}
    # A required option is a plain typed parameter (discord.py reads
    # Optional[...] as never required); an optional one defaults to None.
    params = ", ".join(
        f"{o.name}: {'int' if o.kind == 'integer' else 'str'}" if o.required else
        f"{o.name}: Optional[{'int' if o.kind == 'integer' else 'str'}] = None" for o in options)
    source = (f"async def callback(interaction: Interaction"
              + (f", {params}" if params else "") + "):\n"
              "    values = dict(extra or {})\n"
              + "".join(f"    values[{o.name!r}] = {o.name}\n" for o in options)
              + "    view = view_factory() if view_factory is not None else None\n"
              "    await core_ref().run_slash(interaction, registered, values, view=view)\n")
    namespace.update({"core_ref": core_ref, "registered": registered,
                      "extra": dict(extra or {}), "view_factory": view_factory})
    exec(source, namespace)
    callback = namespace["callback"]
    if options:
        callback = app_commands.describe(**{o.name: o.description[:100] for o in options})(callback)
        for option in options:
            if option.choices:
                callback = app_commands.choices(**{option.name: [app_commands.Choice(name=c, value=c)
                                                                 for c in option.choices]})(callback)
            if option.autocomplete:
                provider = AUTOCOMPLETE_PROVIDERS[option.autocomplete]
                callback = app_commands.autocomplete(**{option.name: provider})(callback)
    return callback


class _HelpSelect(discord.ui.Select):
    """The /print-help menu (#1475): picking a command runs it as a slash command."""

    def __init__(self, core_ref):
        options = [discord.SelectOption(label="/" + c.name, value=c.name,
                                        description=c.description[:100])
                   for c in commands.help_menu_commands()]
        super().__init__(placeholder="More commands…", min_values=1, max_values=1, options=options)
        self._core_ref = core_ref

    async def callback(self, interaction: discord.Interaction):
        await self._core_ref().run_slash(interaction, self.values[0], {})


def _help_view(core_ref):
    view = discord.ui.View(timeout=300)
    view.add_item(_HelpSelect(core_ref))
    return view


def _slash(core_ref, command, name=None):
    """One app command (or subcommand group) handing its options to BotCore.run_slash.

    ``name`` is the registered name (the canonical name, or an alias from
    the commands table): run_slash is called with it, so an alias produces
    its own typed form (``/design …`` for ``/print-design``'s alias).
    Commands with subcommands in the table register as a Discord group of
    real subcommands with typed options (#1475); the picked subcommand and
    values map back to the one typed form in ``BotCore.run_slash``.
    """
    registered = name or command.name
    if command.subcommands:
        group = app_commands.Group(name=registered, description=command.description[:100])
        for sub in command.subcommands:
            callback = _make_callback(core_ref, registered, sub.options,
                                      extra={"subcommand": sub.name})
            group.add_command(app_commands.Command(
                name=sub.name, description=sub.description[:100], callback=callback))
        return group
    view_factory = (lambda: _help_view(core_ref)) if command.name == "print-help" else None
    callback = _make_callback(core_ref, registered, command.options, view_factory=view_factory)
    return app_commands.Command(name=registered, description=command.description[:100], callback=callback)


class PrintHandyBot(ext_commands.Bot):
    def __init__(self, cfg):
        intents = discord.Intents.default()
        intents.message_content = True  # typed replies, links and "print it" follow-ups
        super().__init__(command_prefix=ext_commands.when_mentioned, intents=intents, help_command=None)
        self.cfg = cfg
        self.core = None
        self._background = []
        self._interrupted_checked = False
        self.connected = False  # gateway session up: set on ready/resume, cleared on disconnect

    async def setup_hook(self):
        from ..core import resources
        resources.cap_default_executor(asyncio.get_running_loop())  # asyncio.to_thread pool follows the CPU quota
        self.core = BotCore(self, self.cfg)
        if getattr(self, "warm_worker", False):
            from .runtime import enable_warm_worker
            enable_warm_worker()
        for name, command in commands.registrations():
            self.tree.add_command(_slash(lambda: self.core, command, name))
        if self.cfg["bot"].get("sync_commands", True):
            try:
                synced = await self.tree.sync()
                logger.info("registered %d slash commands", len(synced))
            except discord.HTTPException as exc:
                logger.warning("slash command registration failed (typed commands still work): %r", exc)
        self._background.append(asyncio.create_task(self._heartbeat()))
        port = int(self.cfg["bot"].get("health_port") or 0)
        if port:
            server = await health.serve(port, lambda: self.core.status(self.connected))
            self._background.append(server)
            logger.info("health check on :%d/healthz", port)

    async def _heartbeat(self):
        while not self.is_closed():
            try:
                health.write(self.core.status(self.connected))
            except Exception:
                logger.exception("heartbeat write failed")
            try:
                # The listener posts every alert; tell the channel when it is down (#1020).
                await self.core.check_listener_outages()
            except Exception:
                logger.exception("listener outage check failed")
            try:
                # Opt-out ZMod update alerts ([firmware] update_alerts): schedules only.
                await self.core.check_firmware_updates()
            except Exception:
                logger.exception("firmware update alert check failed")
            try:
                # Debug log mode (#478) is shared state; observe it here so
                # a session started from the CLI or Web UI reaches the bot.
                from .. import debug_mode
                await asyncio.to_thread(debug_mode.poll)
            except Exception:
                logger.debug("bot heartbeat or health-report operation failed", exc_info=True)
            await asyncio.sleep(HEARTBEAT_S)

    async def on_disconnect(self):
        self.connected = False

    async def on_resumed(self):
        self.connected = True

    async def on_ready(self):
        if self.connected:
            # A second READY on a live session means Discord invalidated
            # the session under us. One cause is another process logging
            # in with the same bot token (#386): two instances serving
            # the SAME channel each receive every message and both act.
            # (Sharing one token across instances is supported only with
            # one instance per channel — docs/MULTI-INSTANCE.md.)
            logger.warning("gateway READY arrived on an already-connected session; "
                           "if another process logged in with this bot token, check "
                           "that no two instances serve the same print channel")
        self.connected = True
        logger.info("logged in as %s; print channel %s; access: %s; handlers: %s",
                    self.user, self.core.chat_id, self.core.adapter.access_description(),
                    ", ".join(self.core.router.handler_names()))
        health.write(self.core.status(True))
        if self.cfg["bot"].get("setup_only"):
            logger.warning("setup-only mode: this install is not configured yet — "
                           "run /print-handy setup in any channel to set it up; "
                           "nothing else is answered until setup is applied")
        if not self._interrupted_checked:
            self._interrupted_checked = True  # once per process; READY can arrive again on a reconnect
            try:
                from ..core import prepared_intake
                from ..runtime import _config
                await prepared_intake.announce_interrupted(_config.state_dir() / "model-link-intakes",
                                                           self.core.adapter)
            except Exception:
                logger.exception("could not announce interrupted intakes")
            try:
                await self.core.announce_pending_launch()
            except Exception:
                logger.exception("could not announce a pending launch")

    async def on_message(self, message):
        try:
            await self.core.handle_message(message)
        except Exception:
            logger.exception("message handling failed")

    async def close(self):
        for item in self._background:
            if isinstance(item, asyncio.Task):
                item.cancel()
            else:
                item.close()
        tasks = [item for item in self._background if isinstance(item, asyncio.Task)]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for item in self._background:
            if not isinstance(item, asyncio.Task):
                await item.wait_closed()
        self._background.clear()
        try:  # a slicer or model-check worker must not outlive the bot
            from print_handy.core import owned_worker
            await owned_worker.shutdown()
        except Exception:
            logger.exception("stopping intake workers failed")
        self.connected = False
        try:
            if self.core is not None:
                health.write(self.core.status(False))
        except Exception:
            logger.debug("bot heartbeat or health-report operation failed", exc_info=True)
        await super().close()
