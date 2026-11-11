"""Deliver ZMod update alerts on the platform the bot runs on ([firmware] update_alerts).

The bot heartbeat (Discord, Telegram and Matrix clients alike) calls
:meth:`FirmwareAlerter.tick`. A due printer is checked in a background
task, so the heartbeat never waits on a printer or on GitHub:

- only ZMod printers Print Handy may apply updates on (the registry's
  ``zmod`` family and firmware_update.is_apply_eligible); every other
  family gets no alert (see docs/FIRMWARE-UPDATE.md);
- each printer once per ``update_check_hours`` (default 24), jittered
  so a restart or a roster of printers does not check in lockstep, and
  never while it prints: a printing, paused or unreadable printer is
  checked again later;
- a failed check is logged once (until a check succeeds again) and
  never alerts;
- one alert per release, remembered in the printer's state folder, so
  a restart does not repeat it (print_handy.firmware_alerts).

An applicable alert carries **Update** and **Skip this version**
buttons where the platform has buttons (Discord, Telegram); on Matrix
it names the typed command instead. Update runs
firmware_update.confirmed_update, the same flow as ``/print-firmware
update zmod``: prepare (eligibility, the live component, idle), the same
confirmation prompt, then send (idle again, one upgrade POST, verified
re-read). Nothing is applied without both presses.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid

from .. import firmware_alerts as fa
from .. import firmware_update as fw
from .. import settings
from ..core import prompts
from . import routing

logger = logging.getLogger(__name__)

UPDATE_LABEL = "Update"
SKIP_LABEL = "Skip this version"
# The first check after a start waits a little (the bot is still settling,
# and a crash-looping bot must not hit printers and GitHub on every start).
FIRST_CHECK_S = 120.0
FIRST_JITTER_S = 480.0
# A printer that is printing, paused or unreadable is asked again later.
BUSY_RETRY_S = 1800.0
JITTER_FRACTION = 0.1


def enabled(cfg) -> bool:
    return bool((cfg.get("firmware") or {}).get("update_alerts", True))


def interval_s(cfg) -> float:
    hours = (cfg.get("firmware") or {}).get("update_check_hours", 24)
    try:
        return max(1.0, float(hours)) * 3600.0
    except (TypeError, ValueError):
        return 24 * 3600.0


def alert_printers(cfg):
    """(printer_id, registry entry) for every roster printer that gets alerts.

    ZMod-family printers whose profile is apply-eligible: the AD5X, AD5M,
    AD5M Pro, Creator 5 and Creator 5 Pro under ZMod. A roster entry whose
    model is not registered, or is any other family, is left out.
    """
    out = []
    for row in settings.printers(cfg):
        entry = fw.printer_entry(row.get("model"))
        if entry is not None and entry.firmware_family == "zmod" and fw.is_apply_eligible(entry):
            out.append((str(row["id"]), entry))
    return out


def _moonraker_for(cfg, printer_id):
    from ..hermes import spool_sync
    default = settings.default_printer_id(cfg)
    return spool_sync.moonraker_for(None if printer_id == default else printer_id)


class FirmwareAlerter:
    """The bot's update-alert scheduler and delivery for one BotCore."""

    def __init__(self, core, *, transport_for=None, releases=None, clock=time.time,
                 rng=None, confirm_timeout_s=None):
        self.core = core
        self.transport_for = transport_for or (lambda pid: _moonraker_for(core.cfg, pid))
        self.releases = releases or fa.ReleaseCache()
        self.clock = clock
        self.rng = rng or random.Random()
        self.confirm_timeout_s = confirm_timeout_s
        # The Web UI keeps its own alert state (firmware_alerts.surface_for).
        self.surface = fa.surface_for(getattr(core, "platform_name", ""))
        self._first_due: dict[str, float] = {}
        self._checking: set[str] = set()
        self._applying: set[str] = set()
        self._failing: set[str] = set()
        self.tasks: set[asyncio.Task] = set()

    # -------------------------------------------------------------- scheduling

    def _state_dir(self, printer_id):
        return settings.printer_state_dir(printer_id, self.core.cfg)

    def _due(self, printer_id, state, now):
        stored = state.get("next_check_at")
        if isinstance(stored, (int, float)) and not isinstance(stored, bool):
            return float(stored)
        if printer_id not in self._first_due:
            self._first_due[printer_id] = now + FIRST_CHECK_S + self.rng.uniform(0, FIRST_JITTER_S)
        return self._first_due[printer_id]

    def _next(self, now):
        base = interval_s(self.core.cfg)
        return now + base * (1 + self.rng.uniform(-JITTER_FRACTION, JITTER_FRACTION))

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("firmware update alert task failed: %r", task.exception())

    def tick(self):
        """Start a background check for each due printer. Never raises, never waits."""
        try:
            cfg = self.core.cfg
            if not enabled(cfg) or getattr(getattr(self.core, "gateway", None), "setup_only", False):
                return []
            now = self.clock()
            started = []
            for printer_id, entry in alert_printers(cfg):
                if printer_id in self._checking:
                    continue
                state = fa.load(self._state_dir(printer_id), self.surface)
                if now < self._due(printer_id, state, now):
                    continue
                self._checking.add(printer_id)
                started.append(self._spawn(self.check(printer_id, entry)))
            return started
        except Exception:
            logger.exception("firmware update alert scheduling failed")
            return []

    # -------------------------------------------------------------- one check

    def _fail(self, printer_id, detail):
        if printer_id not in self._failing:
            self._failing.add(printer_id)
            logger.warning("firmware update check for %s failed (no alert sent; "
                           "retrying at the next check): %s", printer_id, detail)
        else:
            logger.debug("firmware update check for %s still failing: %s", printer_id, detail)

    async def check(self, printer_id, entry):
        """One check of one printer: read, decide, announce what is new."""
        state_dir = self._state_dir(printer_id)
        now = self.clock()
        state = fa.load(state_dir, self.surface)
        try:
            transport = self.transport_for(printer_id)
            try:
                printer_state = await asyncio.to_thread(fw.print_state, transport)
            except fw.FirmwareError as exc:
                self._fail(printer_id, exc)
                fa.save(state_dir, {**state, "next_check_at": now + BUSY_RETRY_S}, self.surface)
                return []
            if printer_state not in fw.IDLE_STATES:
                # Never during a print: ask again later, alert nothing now.
                fa.save(state_dir, {**state, "next_check_at": now + BUSY_RETRY_S}, self.surface)
                return []
            release = await asyncio.to_thread(self.releases.get)
            try:
                observation = await asyncio.to_thread(fa.observe, transport, release)
            except fw.FirmwareError as exc:
                self._fail(printer_id, exc)
                fa.save(state_dir, {**state, "next_check_at": self._next(now)}, self.surface)
                return []
            self._failing.discard(printer_id)
            fresh = fa.to_announce(fa.offers(observation), state,
                                   skipped=fa.skipped_everywhere(state_dir))
            for offer in fresh:
                if await self.announce(printer_id, entry, offer):
                    state = fa.record_announced(state, offer, self.clock())
            fa.save(state_dir, {**state, "next_check_at": self._next(now)}, self.surface)
            return fresh
        except Exception as exc:
            self._fail(printer_id, repr(exc))
            return []
        finally:
            self._checking.discard(printer_id)

    # -------------------------------------------------------------- delivery

    def _chat(self, printer_id):
        core = self.core
        return routing.chat_for_printer(core.cfg, core.platform_name, printer_id) or core.chat_id

    def _label(self, printer_id, entry):
        name = routing.printer_name(self.core.cfg, printer_id)
        return name if name and name != printer_id else entry.label

    async def announce(self, printer_id, entry, offer) -> bool:
        """Post one alert; True only when the platform delivered it."""
        adapter = self.core.adapter
        chat = self._chat(printer_id)
        if not chat:
            logger.info("firmware update alert for %s not posted: no chat configured", printer_id)
            return False
        label = self._label(printer_id, entry)
        buttons = offer.applicable and bool(getattr(adapter, "supports_buttons", False))
        text = offer.text(label, buttons=buttons)
        if not buttons:
            return _delivered(await adapter.send(chat_id=chat, content=text))
        registry = prompts.gateway()
        clarify_id = f"fwalert-{uuid.uuid4().hex[:12]}"
        session = f"firmware-alert:{chat}:{printer_id}"
        registry.register(clarify_id, session, text, [UPDATE_LABEL, SKIP_LABEL])
        try:
            sent = await adapter.send_clarify(chat_id=chat, question=text,
                                              choices=[UPDATE_LABEL, SKIP_LABEL],
                                              clarify_id=clarify_id, session_key=session)
        except Exception:
            sent = None
        if not _delivered(sent):
            _close(registry, clarify_id)
            return False
        self._spawn(self._answer(printer_id, entry, offer, chat, clarify_id))
        return True

    async def _answer(self, printer_id, entry, offer, chat, clarify_id):
        """Wait for the alert's press; Update enters the confirmed apply, Skip remembers."""
        from .runtime import await_prompt
        registry = prompts.gateway()
        adapter = self.core.adapter
        try:
            answer = await await_prompt(registry, clarify_id, interval_s(self.core.cfg))
        finally:
            _close(registry, clarify_id)
        if answer == SKIP_LABEL:
            state_dir = self._state_dir(printer_id)
            fa.save(state_dir, fa.record_skipped(fa.load(state_dir, self.surface), offer.key,
                                                 self.clock()), self.surface)
            await adapter.close_prompt(clarify_id, "Skipped. Print Handy will tell you about "
                                                   "the next ZMod release.")
            return "skipped"
        if answer != UPDATE_LABEL:
            await adapter.close_prompt(clarify_id, "This alert expired. "
                                                   f"`/print-firmware update {offer.component}` "
                                                   "still works.")
            return "expired"
        await adapter.close_prompt(clarify_id, "Checking the printer first.")
        message = await self.apply(printer_id, entry, offer.component, chat)
        await adapter.send(chat_id=chat, content=message)
        return message

    async def apply(self, printer_id, entry, component, chat) -> str:
        """The confirmed apply for one printer: firmware_update.confirmed_update."""
        if printer_id in self._applying:
            return "An update for this printer is already waiting for its answer. Nothing sent."
        self._applying.add(printer_id)
        adapter = self.core.adapter
        registry = prompts.gateway()
        timeout = self.confirm_timeout_s or _safety_timeout(self.core.cfg)

        async def ask(question, choices):
            clarify_id = f"fwapply-{uuid.uuid4().hex[:12]}"
            session = f"firmware-apply:{chat}:{printer_id}"
            registry.register(clarify_id, session, question, choices)
            try:
                sent = await adapter.send_clarify(chat_id=chat, question=question,
                                                  choices=choices, clarify_id=clarify_id,
                                                  session_key=session)
                if not _delivered(sent):
                    return None
                from .runtime import await_prompt
                return await await_prompt(registry, clarify_id, timeout)
            finally:
                _close(registry, clarify_id)

        try:
            transport = self.transport_for(printer_id)
            return await fw.confirmed_update(transport, entry, component, ask)
        except Exception:
            logger.exception("firmware update from an alert failed")
            return ("The firmware update stopped on an error. No confirmation was completed, "
                    "so nothing was sent.")
        finally:
            self._applying.discard(printer_id)


def _delivered(result):
    return result is not None and bool(getattr(result, "success", False))


def _close(registry, clarify_id):
    registry.resolve_gateway_clarify(clarify_id, registry.CANCELLED)
    registry.wait_for_response(clarify_id, 0.001)


def _safety_timeout(cfg):
    try:
        return int((cfg.get("discord") or {}).get("safety_confirm_timeout_s") or 600)
    except (TypeError, ValueError):
        return 600
