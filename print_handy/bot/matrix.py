"""Matrix delivery and sync client for the standalone bot (#164 stage 3).

The Matrix implementation of the platform interface in
``print_handy.bot.platform``, standard library only (the Client-Server API
calls live in ``print_handy.bot.matrix_rest``). Matrix is a pure bot, like
Telegram: this module never imports or calls the LLM fallback, the adapter
reports ``uses_llm = False``, and ``BotCore`` therefore answers unmatched
text with the short unmatched reply only, even when ``[llm]`` is configured (#467).

One configured room is the print chat; threads are out of scope, and
encrypted rooms are out of scope — ``m.room.encrypted`` events are ignored,
so the room must be unencrypted (see docs/MATRIX-BOT-SETUP.md).

Matrix has no native buttons, so prompts are a numbered choice list
("Reply with the number, or the choice text") resolved through the typed
answer path. The number-to-choice translation happens in this adapter, so
the core prompt registry only ever sees real choice text.

Listener pause/stop alerts are the one button-shaped exception (#1291):
Matrix's button equivalent is an ``m.reaction`` annotation, so the alert
names one reaction per action (▶️ Resume, ⏹️ Cancel, …), the bot seeds
those reactions on its own alert, and a reaction from an allowed user
runs the same claim-then-act path as a Discord button click.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace as S

from .. import settings
from ..core import prompts
from . import health
from . import matrix_rest as mx
from . import routing
from .platform import BasePlatformAdapter, SendResult, _prompts_guard, answered_note
from .rest import chunks, outgoing

logger = logging.getLogger(__name__)

HEARTBEAT_S = 15
HANDLER_DRAIN_S = 5
SYNC_TIMEOUT_MS = 30_000
SINCE_FILE = "matrix-next-batch"
# A reaction older than this, or older than this process, is a replay
# (e.g. pressed while the bot was down) and never acts (#1291).
REACTION_MAX_AGE_S = 300
# At most one "Not allowed" post per refused sender per this many seconds.
REFUSAL_POST_INTERVAL_S = 60
_NUMBER = re.compile(r"(\d{1,3})\s*[.)]?")


def library_status():
    """(label, available, detail) for the offline check: stdlib only, always available."""
    return ("matrix", True, "standard library client")


def matrix_token(cfg=None):
    """The Matrix access token: the [matrix] access_token_env variable, then the profile .env files."""
    from ..runtime import _config
    cfg = cfg if cfg is not None else _config.cfg()
    name = str((cfg.get("matrix") or {}).get("access_token_env") or "MATRIX_ACCESS_TOKEN")
    return _config.profile_env_value(name)


def homeserver_url(cfg=None):
    from ..runtime import _config
    cfg = cfg if cfg is not None else _config.cfg()
    return str((cfg.get("matrix") or {}).get("homeserver_url") or "").rstrip("/")


def _attachment(mxc_url, name, size=0):
    async def save(target, _mxc=mxc_url):
        raise RuntimeError("attachment is not bound to a Matrix client")

    return S(filename=str(name), size=int(size or 0), save=save, mxc=str(mxc_url))


def parse_event(event, room_id, bot_user_id=""):
    """A room timeline event as the namespace BotCore.handle_message consumes, or None.

    Only plain ``m.room.message`` events from the configured room count:
    encrypted events, edits (``m.replace`` relations) and the bot's own
    messages are not new messages.
    """
    if not isinstance(event, dict) or event.get("type") != "m.room.message":
        return None
    sender = str(event.get("sender") or "")
    if not sender or sender in _own_ids(bot_user_id):
        return None
    content = event.get("content") or {}
    if not isinstance(content, dict):
        return None
    relates = content.get("m.relates_to") or {}
    if isinstance(relates, dict) and relates.get("rel_type") == "m.replace":
        return None
    msgtype = content.get("msgtype")
    attachments = []
    if msgtype in ("m.file", "m.image", "m.video", "m.audio") and content.get("url"):
        info = content.get("info") or {}
        attachments.append(_attachment(content["url"],
                                       content.get("filename") or content.get("body") or "file",
                                       info.get("size") if isinstance(info, dict) else 0))
        text = ""
    elif msgtype == "m.text":
        text = str(content.get("body") or "")
    else:
        return None
    return S(author=S(id=sender, bot=False), channel=S(id=str(room_id), parent_id=None),
             content=text, id=event.get("event_id"), attachments=attachments)


def _own_ids(bot_user_id):
    """The bot's own user ids, from one id or a collection of them."""
    if isinstance(bot_user_id, str):
        return {bot_user_id} if bot_user_id else set()
    return {str(uid) for uid in (bot_user_id or ()) if uid}


def parse_reaction_event(event, room_id, bot_user_id=""):
    """An m.reaction annotation as its routing fields, or None.

    Only an ``m.annotation`` relation from the configured room counts,
    and never the bot's own: the bot seeds one reaction per action on
    its own alerts (#1291), and those seeds must not act. Own means a
    sender among ``bot_user_id`` (one id or several), or any event
    carrying ``unsigned.transaction_id``: the homeserver adds that only
    to events this access token sent, so the seeds are dropped even if
    the configured bot id is wrong. Everything else (a reaction with no
    target or key, another event type) is not an alert press. ``ts`` is
    the event's ``origin_server_ts`` in ms, 0 when absent.
    """
    if not isinstance(event, dict) or event.get("type") != "m.reaction":
        return None
    sender = str(event.get("sender") or "")
    if not sender or sender in _own_ids(bot_user_id):
        return None
    unsigned = event.get("unsigned")
    if isinstance(unsigned, dict) and unsigned.get("transaction_id"):
        return None
    content = event.get("content") or {}
    if not isinstance(content, dict):
        return None
    relates = content.get("m.relates_to") or {}
    if not isinstance(relates, dict) or relates.get("rel_type") != "m.annotation":
        return None
    target = str(relates.get("event_id") or "")
    key = str(relates.get("key") or "")
    if not target or not key:
        return None
    try:
        ts = int(event.get("origin_server_ts") or 0)
    except (TypeError, ValueError):
        ts = 0
    return S(sender_id=sender, room_id=str(room_id), target_event_id=target, key=key,
             id=event.get("event_id"), ts=ts)


class StandaloneAdapter(BasePlatformAdapter):
    """Sends through the Matrix Client-Server API. ``allowed`` holds the @user:server IDs that may act."""

    name = "matrix"
    message_limit = mx.LIMIT
    supports_buttons = False
    max_buttons = 0
    uses_llm = False
    supports_live_stream = False
    polls_serially = True  # /sync loop awaits each event (P1-109)

    def __init__(self, client, allowed, *, denied=(), approver=None, approve_all=False, registry=prompts,
                 upload_max_bytes=None, homeserver=None, token=None, opener=None):
        super().__init__(allowed, denied=denied, approver=approver, approve_all=approve_all, registry=registry)
        self.client = client
        self.upload_max_bytes = int(upload_max_bytes) if upload_max_bytes else 50_000_000
        self.homeserver = homeserver if homeserver is not None else getattr(client, "homeserver", "")
        self.token = token if token is not None else getattr(client, "token", "")
        self.opener = opener if opener is not None else getattr(client, "opener", None)
        self._refusal_posted_at = {}  # sender id -> monotonic time of the last refusal post

    # ------------------------------------------------------------------ delivery

    async def send(self, chat_id, content, reply_to=None, metadata=None, files=None):
        body, paths = outgoing(content, files)
        pieces = chunks(body, self.message_limit)
        first = None
        try:
            for piece in pieces:
                if not piece and paths:
                    continue
                sent = await asyncio.to_thread(mx.send_message, self.homeserver, self.token,
                                               str(chat_id), piece, opener=self.opener)
                first = first or sent
            for path in paths:
                sent = await asyncio.to_thread(mx.send_file, self.homeserver, self.token,
                                               str(chat_id), path, opener=self.opener)
                first = first or sent
        except Exception as exc:
            logger.warning("Matrix send failed: %r", exc)
            return SendResult(False, error=str(exc))
        return SendResult(True, message_id=first or None)

    async def signal_working(self, chat_id, metadata=None):
        """A Matrix typing notification for the bot's own account (10 s, or until the reply)."""
        client = self.client
        user_id = str(getattr(client, "bot_user_id", "") or "") or str(
            getattr(getattr(client, "user", None), "id", "") or "")
        if not user_id:
            return None
        await asyncio.to_thread(mx.typing, self.homeserver, self.token, str(chat_id), user_id,
                                opener=self.opener)

    async def delete_message(self, chat_id, message_id, metadata=None):
        try:
            await asyncio.to_thread(mx.redact, self.homeserver, self.token, str(chat_id),
                                    str(message_id), opener=self.opener)
            return True
        except Exception as exc:
            logger.info("Matrix redact failed: %r", exc)
            return False

    async def pin_message(self, chat_id, message_id):
        """Pin a message this adapter sent: the budget-exhausted outage
        escalation (#1020 B3, beyond Discord in #1332). Needs the power
        level to change room state; without it the homeserver refuses and
        the outage path keeps the unpinned CRITICAL notice."""
        await asyncio.to_thread(mx.pin_event, self.homeserver, self.token, str(chat_id),
                                str(message_id), opener=self.opener)
        return True

    async def edit_message(self, chat_id, message_id, content, metadata=None):
        """Replace a posted message's text in place (progress updates, P2-236).

        Matrix edits are new events pointing at the original, so the result
        keeps the original message id: later edits target it again.
        """
        try:
            await asyncio.to_thread(mx.edit_message, self.homeserver, self.token, str(chat_id),
                                    str(message_id), str(content)[:mx.LIMIT], opener=self.opener)
        except Exception as exc:
            logger.info("Matrix edit failed: %r", exc)
            return SendResult(False, error=str(exc))
        return SendResult(True, message_id=str(message_id))

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        """The question plus a numbered choice list; answers come back as typed replies."""
        labels = [str(c) for c in choices]
        pieces = chunks(question, self.message_limit)
        try:
            for piece in pieces[:-1]:
                await asyncio.to_thread(mx.send_message, self.homeserver, self.token,
                                        str(chat_id), piece, opener=self.opener)
            listing = "\n".join(f"{index}. {label}" for index, label in enumerate(labels, 1))
            text = f"{pieces[-1]}\n\n{listing}\nReply with the number, or the choice text."
            event_id = await asyncio.to_thread(mx.send_message, self.homeserver, self.token,
                                               str(chat_id), text, opener=self.opener)
        except Exception as exc:
            logger.warning("Matrix prompt failed: %r", exc)
            return SendResult(False, error=str(exc))
        self._record_prompt(clarify_id, {"event_id": event_id, "room_id": str(chat_id),
                                         "choices": labels, "question": pieces[-1]})
        return SendResult(True, message_id=event_id or None)

    # ------------------------------------------------------------------ answers

    async def answer_typed(self, session_key, text, user_id=None):
        """A typed reply, with a bare number ('2', '2.') translated to that choice's text first.

        Ordering (#416): the answer note is posted BEFORE the prompt is
        resolved. Resolving wakes the waiting flow, which sends its next
        message immediately; on Matrix the close-out is a new room message
        (there is no edit), so the shared resolve-then-close sequence let
        the flow's next message overtake the note whenever the note's send
        lost the thread-pool race — the intermittent CI failure where a
        room saw "Started." and only then "> Answered by message: ...".
        Discord/Telegram close-outs are edits, so only Matrix reorders.
        Answers are dispatched by the sync loop as their own event tasks
        (P1-65), so an answer is processed while the flow it answers is
        still running; the resolve stays first-wins (the registry decides):
        its result is what this call returns.

        The answer is RESERVED before the note posts (P2-166): the note is
        a network send that can take up to 30 seconds, and an answer that
        arrived just before the prompt's timeout used to be discarded as
        expired while the note was still sending. Reserving records the
        answer at arrival — the wait holds for the commit — while the
        resolve itself still happens after the note, so the flow's
        continuation cannot overtake the note in the room.
        """
        if self.typed_approval_blocked(session_key, user_id):
            return False
        text = str(text or "").strip()
        match = _NUMBER.fullmatch(text)
        if match:
            cid = self.open_prompt(session_key)
            if cid is not None:
                with _prompts_guard(self):
                    row = self._prompts.get(cid)
                labels = self.registry.choices(cid) or (row or {}).get("choices") or []
                index = int(match.group(1)) - 1
                if 0 <= index < len(labels):
                    text = labels[index]
        cid = self.open_prompt(session_key)
        if cid is None or not self.registry.is_open(cid):
            return False
        if not self.registry.reserve_gateway_clarify(cid, str(text)):
            return False
        try:
            await self.close_prompt(cid, answered_note(text))
        finally:
            resolved = self.registry.resolve_gateway_clarify(cid, str(text))
        return bool(resolved)

    async def handle_alert_reaction(self, sender_id, room_id, target_event_id, key):
        """Resolve one reaction press on a listener alert (#1291).

        The listener recorded the alert (``print_alert_buttons``) when it
        posted the alert with its reactions; a press claims it exactly
        like a Discord click or a Telegram callback — find, check, take
        once — and ``handle_click`` re-reads the printer before acting.
        The gate is the allow-list every typed answer passes; an alert
        press is not a print-start approval, so the separate approval
        gate does not apply. The press counts only in the room the alert
        was posted to. Matrix has no ephemeral answer, so refusals and
        the outcome are short follow-up messages in the room (a refusal
        at most once a minute per sender) — except
        for reactions that are not alert actions at all (an unrelated
        emoji, or any reaction on a message that is not a live alert),
        which are ignored silently so ordinary room reactions never
        draw bot noise.
        """
        from print_handy.runtime import print_alert_buttons as pab

        action = pab.matrix_reaction_action(key)
        if action is None:
            return False
        aid, alert = await asyncio.to_thread(pab.matrix_alert, str(target_event_id))
        if not aid or not alert:
            return False
        kind = str(alert.get("kind") or "")
        if action not in {choice for _key, choice, _label in pab.matrix_reaction_choices(kind)}:
            # An action emoji this alert never offered (▶️ on a cancel-only
            # alert): ignored before any claim, so the alert stays live.
            logger.info("ignored %s reaction from %s: not offered on a %s alert", action, sender_id, kind)
            return False

        async def post(text):
            await self.send(chat_id=str(room_id), content=text)

        posted_room = str(alert.get("matrix_room_id") or "")
        if (not self._is_allowed_user(str(sender_id), S(bot=False))
                or (posted_room and posted_room != str(room_id))):
            logger.info("refused %s reaction from %s in %s", action, sender_id, room_id)
            if self._refusal_due(str(sender_id)):
                await post("Not allowed. Nothing changed.")
            return True
        if not await asyncio.to_thread(pab.take, aid):
            await post("That alert is old or was already answered. Nothing sent.")
            return True
        outcome = await asyncio.to_thread(pab.handle_click, action, alert)
        await post(str(outcome))
        return True

    def _refusal_due(self, sender_id):
        """True at most once per sender per REFUSAL_POST_INTERVAL_S, so repeated
        presses by a non-allowed user cannot flood the room with refusals."""
        now = time.monotonic()
        last = self._refusal_posted_at.get(sender_id)
        if last is not None and now - last < REFUSAL_POST_INTERVAL_S:
            return False
        self._refusal_posted_at[sender_id] = now
        return True

    async def close_prompt(self, clarify_id, note):
        """No message edit on Matrix: a short follow-up message records the answer."""
        with _prompts_guard(self):
            row = self._prompts.pop(str(clarify_id), None)
        if not row:
            return
        try:
            await asyncio.to_thread(mx.send_message, self.homeserver, self.token,
                                    row["room_id"], f"> {note}", opener=self.opener)
        except Exception:
            logger.debug("could not post the prompt answer note", exc_info=True)


class MatrixClient:
    """Long-polling /sync client shim with the surface BotCore and the health code need.

    ``user`` is the bot's own Matrix account, ``core`` the BotCore once
    started. The sync ``next_batch`` token is persisted in the state dir,
    so a restart resumes where the last run stopped instead of replaying
    room history; the very first run starts from "now".
    """

    def __init__(self, cfg, homeserver, token, *, opener=None, state_path=None):
        self.cfg = cfg
        self.homeserver = str(homeserver or "").rstrip("/")
        self.token = token
        self.opener = opener
        self.core = None
        self.user = None
        self.connected = False
        self.room_id = str((cfg.get("matrix") or {}).get("room_id") or "")
        self.bot_user_id = str((cfg.get("matrix") or {}).get("bot_user_id") or "")
        self._started_ms = int(time.time() * 1000)
        self._since = None
        self._background = []
        self._handlers = set()
        self._state_path = Path(state_path) if state_path else None

    # -------------------------------------------------------------- sync state

    def _since_path(self):
        if self._state_path is not None:
            return self._state_path
        return settings.state_dir(self.cfg) / SINCE_FILE

    def _load_since(self):
        try:
            return self._since_path().read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _save_since(self):
        if not self._since:
            return
        try:
            path = self._since_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self._since, encoding="utf-8")
        except OSError:
            logger.debug("could not persist the Matrix sync token", exc_info=True)

    # -------------------------------------------------------------- attachments

    def _bind(self, message_ns):
        """Point a parsed message's attachments at this client's downloader."""
        homeserver, token, opener = self.homeserver, self.token, self.opener
        for attachment in message_ns.attachments:
            mxc_url = attachment.mxc

            async def save(target, _mxc=mxc_url, _hs=homeserver, _token=token, _opener=opener):
                data = await asyncio.to_thread(mx.download_media, _hs, _token, _mxc, opener=_opener)
                Path(target).write_bytes(data)

            attachment.save = save
        return message_ns

    # -------------------------------------------------------------- event routing

    def _served_rooms(self):
        """The rooms this bot answers in (#335): every room the roster maps to a
        printer, plus the configured home room. Without a roster map that is
        the home room alone, exactly as before."""
        rooms = set()
        core_map = getattr(self.core, "channel_printers", None) if self.core is not None else None
        core_shared = getattr(self.core, "shared_printers", None) if self.core is not None else None
        if core_map or core_shared:
            rooms.update(core_map or {})
            rooms.update(core_shared or {})  # shared rooms are served too (#536)
        else:
            try:
                rooms.update(routing.channel_map(self.cfg, "matrix"))
                rooms.update(routing.shared_channel_map(self.cfg, "matrix"))
            except Exception:
                pass  # BotCore's own map build reports a bad roster loudly at startup
        if self.room_id:
            rooms.add(self.room_id)
        return rooms

    def _own_ids(self):
        """Every id that is this bot: the configured one and the whoami-resolved one."""
        ids = {self.bot_user_id, str(self.user.id) if self.user is not None else ""}
        return {uid for uid in ids if uid}

    def parse(self, event, room_id=None):
        return parse_event(event, room_id or self.room_id, self._own_ids())

    def parse_reaction(self, event, room_id=None):
        return parse_reaction_event(event, room_id or self.room_id, self._own_ids())

    def _reaction_cutoff_ms(self):
        """Reactions older than this never act: older than REACTION_MAX_AGE_S, or
        sent before this process started. /sync resumes from the saved token, so
        a press made while the bot was down arrives on startup (#1291)."""
        return max(self._started_ms, int(time.time() * 1000) - REACTION_MAX_AGE_S * 1000)

    async def handle_event(self, event, room_id=None):
        reaction = self.parse_reaction(event, room_id)
        if reaction is not None:
            if self.core is None:
                return None
            cutoff = self._reaction_cutoff_ms()
            if reaction.ts < cutoff:
                logger.info("ignored stale %s reaction from %s on %s (%d s before cutoff)",
                            reaction.key, reaction.sender_id, reaction.target_event_id,
                            (cutoff - reaction.ts) // 1000)
                return None
            handler = getattr(self.core.adapter, "handle_alert_reaction", None)
            if handler is None:
                return None
            return await handler(reaction.sender_id, reaction.room_id,
                                 reaction.target_event_id, reaction.key)
        parsed = self.parse(event, room_id)
        if parsed is None or self.core is None:
            return None
        return await self.core.handle_message(self._bind(parsed))

    def _room_events(self, data):
        rooms = (data or {}).get("rooms") or {}
        joined = rooms.get("join") or {}
        served = self._served_rooms()
        out = []
        for room_id, room in joined.items():
            if room_id not in served:
                continue
            timeline = (room or {}).get("timeline") or {}
            events = timeline.get("events")
            if isinstance(events, list):
                out.extend((room_id, event) for event in events)
        return out

    async def _maybe_join(self, data):
        """Accept invitations to served rooms when they show up in /sync."""
        invited = ((data or {}).get("rooms") or {}).get("invite") or {}
        for room_id in invited:
            if room_id not in self._served_rooms():
                continue
            try:
                await asyncio.to_thread(mx.join_room, self.homeserver, self.token, room_id,
                                        opener=self.opener)
                logger.info("joined the print room %s", room_id)
            except Exception:
                logger.warning("could not join the print room %s", room_id, exc_info=True)

    async def _run_event(self, event, room_id):
        """One dispatched event's handling; failures are logged, never raised
        into the sync loop that spawned the task."""
        try:
            await self.handle_event(event, room_id)
        except Exception:
            logger.exception("Matrix event handling failed")

    async def drain_handlers(self, timeout=HANDLER_DRAIN_S):
        """Wait (up to ``timeout`` seconds) for in-flight event handlers.

        True when every handler on this loop finished. Handlers still
        running after the timeout — typically a flow parked on a prompt
        answer — are left running; close() cancels what remains.
        """
        loop = asyncio.get_running_loop()
        tasks = [task for task in self._handlers if task.get_loop() is loop and not task.done()]
        if not tasks:
            return True
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        return not pending

    async def poll_once(self):
        """One /sync round; returns the number of events dispatched.

        Events are dispatched as managed tasks, NOT awaited inline (P1-65):
        a handler can park awaiting a prompt answer, and that answer only
        arrives as an event in a later /sync — awaiting the handler here
        starved the loop, so the answer could never be received and the
        flow stalled until its prompt timed out. Tasks are tracked in
        ``_handlers`` and drained by close(), so shutdown never drops one
        silently. Concurrent handling is the posture BotCore already runs
        under on Discord, where the gateway dispatches every message as
        its own task.
        """
        data = await asyncio.to_thread(mx.sync, self.homeserver, self.token, self._since,
                                       timeout_ms=SYNC_TIMEOUT_MS, opener=self.opener)
        await self._maybe_join(data)
        dispatched = 0
        for room_id, event in self._room_events(data):
            if self.parse(event, room_id) is None and self.parse_reaction(event, room_id) is None:
                continue
            task = asyncio.create_task(self._run_event(event, room_id))
            self._handlers.add(task)
            task.add_done_callback(self._handlers.discard)
            dispatched += 1
        if (data or {}).get("next_batch"):
            self._since = str(data["next_batch"])
            self._save_since()
        return dispatched

    # -------------------------------------------------------------- lifecycle

    async def start(self):
        from .runtime import BotCore
        # whoami is asked always: own-event filtering must use the id this
        # token really is, not a configured one that may be mistyped.
        configured = self.bot_user_id
        try:
            me = await asyncio.to_thread(mx.whoami, self.homeserver, self.token, opener=self.opener)
            bot_id = str((me or {}).get("user_id") or "") or configured
        except Exception:
            if not configured:
                raise
            logger.warning("whoami failed; using the configured bot id %s", configured, exc_info=True)
            bot_id = configured
        if configured and bot_id != configured:
            logger.warning("matrix.bot_user_id is %s but the token belongs to %s; using %s",
                           configured, bot_id, bot_id)
        self.bot_user_id = bot_id
        self.user = S(id=bot_id)
        self.core = BotCore(self, self.cfg, chat_id=self.room_id)
        if getattr(self, "warm_worker", False):
            from .runtime import enable_warm_worker
            enable_warm_worker()
        limit = await asyncio.to_thread(mx.media_config, self.homeserver, self.token, opener=self.opener)
        if limit:  # the server's media cap wins when it is lower (best effort)
            self.core.adapter.upload_max_bytes = min(self.core.adapter.upload_max_bytes or limit, limit)
        saved = self._load_since()
        if saved:
            self._since = saved
        else:  # first run: start from now, never replay room history
            data = await asyncio.to_thread(mx.sync, self.homeserver, self.token, None,
                                           timeout_ms=0, opener=self.opener)
            await self._maybe_join(data)
            self._since = str((data or {}).get("next_batch") or "") or None
            self._save_since()
        self.connected = True
        logger.info("logged in as %s; print room %s; %d allowed user(s); handlers: %s",
                    bot_id, self.core.chat_id, len(self.core.adapter.allowed),
                    ", ".join(self.core.router.handler_names()))
        health.write(self.core.status(True))
        self._background.append(asyncio.create_task(self._heartbeat()))
        port = int(self.cfg["bot"].get("health_port") or 0)
        if port:
            server = await health.serve(port, lambda: self.core.status(self.connected))
            self._background.append(server)
            logger.info("health check on :%d/healthz", port)

    async def _heartbeat(self):
        while self.connected:
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
            await asyncio.sleep(HEARTBEAT_S)

    async def run(self):
        await self.start()
        try:
            while True:
                try:
                    await self.poll_once()
                except Exception:
                    logger.exception("Matrix sync failed; retrying")
                    await asyncio.sleep(2)
        finally:
            await self.close()

    async def close(self):
        self.connected = False
        loop = asyncio.get_running_loop()
        for item in self._background:
            if isinstance(item, asyncio.Task):
                item.cancel()
            else:
                item.close()
        # Only tasks owned by this loop can be awaited here; a task from a
        # previous loop (close called after a restart, or from a test's
        # second asyncio.run) is cancelled above and left to its own loop.
        tasks = [item for item in self._background
                 if isinstance(item, asyncio.Task) and item.get_loop() is loop]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for item in self._background:
            if not isinstance(item, asyncio.Task):
                try:
                    await item.wait_closed()
                except RuntimeError:
                    pass  # the health server belonged to a previous loop
        self._background.clear()
        if self._handlers:
            # Event handlers dispatched by poll_once (P1-65): a graceful
            # shutdown gives in-flight ones a brief chance to finish, then
            # cancels what is left — a flow parked on a prompt answer would
            # otherwise hold shutdown open for its whole prompt timeout.
            # Same loop-affinity rule as the background tasks above: only
            # this loop's tasks are awaited; foreign-loop tasks are
            # cancelled and left to their own loop.
            await self.drain_handlers()
            for task in self._handlers:
                task.cancel()
            mine = [task for task in self._handlers if task.get_loop() is loop]
            if mine:
                await asyncio.gather(*mine, return_exceptions=True)
            self._handlers.clear()
        try:
            from print_handy.core import owned_worker
            await owned_worker.shutdown()
        except Exception:
            logger.exception("stopping intake workers failed")
        try:
            if self.core is not None:
                health.write(self.core.status(False))
        except Exception:
            pass


def run_bot(cfg):
    """Start the Matrix bot for ``cfg``; the entry point ``print_handy.bot.run`` delegates here."""
    token = matrix_token(cfg)
    if not token:
        name = str((cfg.get("matrix") or {}).get("access_token_env") or "MATRIX_ACCESS_TOKEN")
        print(f"Matrix access token is empty: put it in the ${name} environment variable.", file=sys.stderr)
        return 3
    homeserver = homeserver_url(cfg)
    if not homeserver:
        print("Matrix homeserver is empty: set [matrix] homeserver_url.", file=sys.stderr)
        return 3
    client = MatrixClient(cfg, homeserver, token)
    client.warm_worker = True  # the production loop keeps a warm heavy worker (#1332)
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        return 0
    return 0
