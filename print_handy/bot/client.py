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
    from ..hermes import _config
    token = _config.discord_token()
    for attempt in range(1, STARTUP_ATTEMPTS + 1):
        try:
            PrintHandyBot(cfg).run(token, log_handler=None)
        except discord.LoginFailure as exc:
            print(f"Discord refused the bot token: {exc}. Check that the token in the environment "
                  "variable named by discord.token_env is the current bot token. "
                  "Reference: PH_DISCORD_CONFIGURATION", file=sys.stderr)
            return 3
        except discord.PrivilegedIntentsRequired:
            print("Turn on the Message Content intent for this bot in the Discord developer portal "
                  "(Bot > Privileged Gateway Intents). Reference: PH_DISCORD_CONFIGURATION",
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


def _slash(core_ref, command, name=None):
    """One app command whose callback hands its options to BotCore.run_slash.

    ``name`` is the registered name (the canonical name, or an alias from
    the commands table): run_slash is called with it, so an alias produces
    its own typed form (``/design …`` for ``/print-design``'s alias).
    """
    names = [o.name for o in command.options]
    registered = name or command.name

    async def run(interaction, values):
        await core_ref().run_slash(interaction, registered, values)

    if not names:
        async def callback(interaction: discord.Interaction):
            await run(interaction, {})
    elif len(names) == 1:
        async def callback(interaction: discord.Interaction, first: Optional[str] = None):
            await run(interaction, {names[0]: first})
    else:
        async def callback(interaction: discord.Interaction, first: Optional[str] = None,
                           second: Optional[str] = None):
            await run(interaction, {names[0]: first, names[1]: second})
    params = ["first", "second"][:len(names)]
    if params:
        callback = app_commands.rename(**dict(zip(params, names)))(callback)
        callback = app_commands.describe(**{p: o.description[:100]
                                            for p, o in zip(params, command.options)})(callback)
        for param, option in zip(params, command.options):
            if option.choices:
                callback = app_commands.choices(**{param: [app_commands.Choice(name=c, value=c)
                                                           for c in option.choices]})(callback)
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
                # Debug log mode (#478) is shared state; observe it here so
                # a session started from the CLI or Web UI reaches the bot.
                from .. import debug_mode
                await asyncio.to_thread(debug_mode.poll)
            except Exception:
                pass
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
        if not self._interrupted_checked:
            self._interrupted_checked = True  # once per process; READY can arrive again on a reconnect
            try:
                from ..core import prepared_intake
                from ..hermes import _config
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
            pass
        await super().close()
