"""Telegram delivery and polling client for the standalone bot (#164 stage 2).

The Telegram implementation of the platform interface in
``print_handy.bot.platform``, standard library only (the Bot API calls live
in ``print_handy.bot.telegram_rest``). Telegram is a pure bot: this module
never imports or calls the LLM fallback, the adapter reports
``uses_llm = False``, and ``BotCore`` therefore answers unmatched text with
the short unmatched reply only — the roll-test of the no-LLM standalone mode from
#73, even when ``[llm]`` is configured for another platform.

Prompts are inline keyboards. Callback data is the platform-neutral
``pbq:<clarify_id>:<index>`` id; a clarify id is 32 hex characters, so the
whole value is about 38 bytes, inside Telegram's 64-byte callback limit.
Forum chats map ``message_thread_id`` to the source ``thread_id``, the same
scoping Discord threads get.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace as S

from ..core import prompts
from . import commands, health, telegram_rest as tg
from .platform import BasePlatformAdapter, SendResult, _prompts_guard, custom_id, parse_custom_id
from .rest import chunks, split_media

logger = logging.getLogger(__name__)

MAX_BUTTONS = 25
LABEL_LIMIT = 64
HEARTBEAT_S = 15
HANDLER_DRAIN_S = 5
POLL_TIMEOUT_S = 25

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def library_status():
    """(label, available, detail) for the offline check: stdlib only, always available."""
    return ("telegram", True, "standard library client")


def telegram_token(cfg=None):
    """The Telegram bot token: the [telegram] token_env variable, then the profile .env files."""
    from ..hermes import _config
    cfg = cfg if cfg is not None else _config.cfg()
    name = str((cfg.get("telegram") or {}).get("token_env") or "TELEGRAM_BOT_TOKEN")
    return _config.profile_env_value(name)


def clean_command_text(text):
    """Strip a trailing @BotName from a command token ('/print-next@PrintHandy x' -> '/print-next x')."""
    text = str(text or "")
    head, _, rest = text.partition(" ")
    if head.startswith("/") and "@" in head:
        head = head.split("@", 1)[0]
    return head + ((" " + rest) if rest else "")


def parse_message(message):
    """A Bot API message dict as the namespace BotCore.handle_message consumes, or None."""
    if not isinstance(message, dict):
        return None
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    if not sender or not chat:
        return None
    thread = message.get("message_thread_id")
    attachments = []
    document = message.get("document")
    if isinstance(document, dict) and document.get("file_id"):
        attachments.append(_attachment(document))
    for photo in (message.get("photo") or [])[-1:]:
        if isinstance(photo, dict) and photo.get("file_id"):
            attachments.append(_attachment(photo, fallback_name="photo.jpg"))
    text = clean_command_text(message.get("text") or message.get("caption") or "")
    return S(author=S(id=sender.get("id"), bot=bool(sender.get("is_bot"))),
             channel=S(id=thread if thread else chat.get("id"),
                       parent_id=chat.get("id") if thread else None),
             content=text, id=message.get("message_id"), attachments=attachments,
             _chat_id=chat.get("id"), _thread_id=thread)


def _attachment(file_obj, fallback_name="file"):
    file_id = str(file_obj.get("file_id"))
    name = str(file_obj.get("file_name") or fallback_name)

    async def save(target, _file_id=file_id):
        raise RuntimeError("attachment is not bound to a Telegram client")

    return S(filename=name, size=int(file_obj.get("file_size") or 0), save=save, file_id=file_id)


class StandaloneAdapter(BasePlatformAdapter):
    """Sends through the Telegram Bot API. ``allowed`` holds the Telegram user IDs that may act."""

    name = "telegram"
    message_limit = tg.LIMIT
    supports_buttons = True
    max_buttons = MAX_BUTTONS
    uses_llm = False
    supports_live_stream = False
    polls_serially = True  # getUpdates loop awaits each update (P1-109)

    def __init__(self, client, allowed, *, denied=(), approver=None, approve_all=False, registry=prompts,
                 upload_max_bytes=None, token=None, opener=None):
        super().__init__(allowed, denied=denied, approver=approver, approve_all=approve_all, registry=registry)
        self.client = client
        self.upload_max_bytes = int(upload_max_bytes) if upload_max_bytes else 50_000_000
        self.token = token if token is not None else getattr(client, "token", "")
        self.opener = opener if opener is not None else getattr(client, "opener", None)

    # ------------------------------------------------------------------ delivery

    def _call(self, method, payload=None, files=()):
        return tg.call(self.token, method, payload, files, opener=self.opener)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        body, paths = split_media(content)
        pieces = chunks(body, self.message_limit)
        thread = (metadata or {}).get("thread_id")
        first = None
        try:
            for index, piece in enumerate(pieces):
                if not piece and paths:
                    continue
                payload = {"chat_id": str(chat_id), "text": piece}
                if thread:
                    payload["message_thread_id"] = str(thread)
                if index == 0 and reply_to and str(reply_to).lstrip("-").isdigit():
                    payload["reply_to_message_id"] = int(reply_to)
                sent = await asyncio.to_thread(self._call, "sendMessage", payload)
                first = first or sent
            for path in paths:
                sent = await asyncio.to_thread(self._send_file, chat_id, path, thread)
                first = first or sent
        except Exception as exc:
            logger.warning("Telegram send failed: %r", exc)
            return SendResult(False, error=str(exc))
        return SendResult(True, message_id=str((first or {}).get("message_id") or "") or None)

    async def signal_working(self, chat_id, metadata=None):
        """sendChatAction "typing" (shown about 5 s, or until the reply)."""
        payload = {"chat_id": str(chat_id), "action": "typing"}
        thread = (metadata or {}).get("thread_id")
        if thread:
            payload["message_thread_id"] = str(thread)
        await asyncio.to_thread(self._call, "sendChatAction", payload)

    def _send_file(self, chat_id, path, thread):
        suffix = Path(path).suffix.lower()
        if suffix in tg.PHOTO_EXTS:
            method, field = "sendPhoto", "photo"
        elif suffix in tg.VIDEO_EXTS:
            method, field = "sendVideo", "video"
        else:
            method, field = "sendDocument", "document"
        payload = {"chat_id": str(chat_id)}
        if thread:
            payload["message_thread_id"] = str(thread)
        return self._call(method, payload, files=[(field, path)])

    async def delete_message(self, chat_id, message_id, metadata=None):
        try:
            await asyncio.to_thread(self._call, "deleteMessage",
                                    {"chat_id": str(chat_id), "message_id": int(message_id)})
            return True
        except Exception as exc:
            logger.info("Telegram delete failed: %r", exc)
            return False

    async def edit_message(self, chat_id, message_id, content, metadata=None):
        """Replace a posted message's text in place (progress updates, P2-236)."""
        try:
            await asyncio.to_thread(self._call, "editMessageText",
                                    {"chat_id": str(chat_id), "message_id": int(message_id),
                                     "text": str(content)[:self.message_limit]})
        except Exception as exc:
            logger.info("Telegram edit failed: %r", exc)
            return SendResult(False, error=str(exc))
        return SendResult(True, message_id=str(message_id))

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        """One message with an inline keyboard, one button per choice."""
        labels = [str(c) for c in choices][:MAX_BUTTONS]
        pieces = chunks(question, self.message_limit)
        thread = (metadata or {}).get("thread_id")
        try:
            for piece in pieces[:-1]:
                payload = {"chat_id": str(chat_id), "text": piece}
                if thread:
                    payload["message_thread_id"] = str(thread)
                await asyncio.to_thread(self._call, "sendMessage", payload)
            keyboard = [[{"text": label[:LABEL_LIMIT], "callback_data": custom_id(clarify_id, index)}]
                        for index, label in enumerate(labels)]
            payload = {"chat_id": str(chat_id), "text": pieces[-1],
                       "reply_markup": {"inline_keyboard": keyboard}}
            if thread:
                payload["message_thread_id"] = str(thread)
            message = await asyncio.to_thread(self._call, "sendMessage", payload)
        except Exception as exc:
            logger.warning("Telegram button question failed: %r", exc)
            return SendResult(False, error=str(exc))
        self._record_prompt(clarify_id, {"message_id": (message or {}).get("message_id"),
                                         "chat_id": str(chat_id), "choices": labels,
                                         "question": pieces[-1]})
        return SendResult(True, message_id=str((message or {}).get("message_id") or "") or None)

    # ------------------------------------------------------------------ answers

    async def close_prompt(self, cid, note):
        with _prompts_guard(self):
            row = self._prompts.pop(str(cid), None)
        if not row or not row.get("message_id"):
            return
        text = f"{row['question']}\n> {note}"[:self.message_limit]
        try:
            await asyncio.to_thread(self._call, "editMessageText",
                                    {"chat_id": row["chat_id"], "message_id": row["message_id"],
                                     "text": text})
        except Exception:
            logger.debug("could not strip answered keyboard", exc_info=True)

    async def handle_callback_query(self, query):
        """Resolve one inline-keyboard press. Always answers the callback query."""
        if not isinstance(query, dict):
            return False
        parsed = parse_custom_id(query.get("data"))
        query_id = query.get("id")
        sender = query.get("from") or {}
        user_id = str(sender.get("id") or "")
        if parsed is None:
            await self._answer_callback(query_id, "")
            return False
        cid, index = parsed
        async def answer(text):
            await self._answer_callback(query_id, text)
        if not self._is_allowed_user(user_id, S(bot=bool(sender.get("is_bot")))):
            await answer("Not allowed. Nothing changed.")
            return True
        if prompts.is_approval(cid) and not self.may_approve_id(user_id):
            # Print approvals stay separate from channel access (P1-93), as
            # on Discord: only the approval user resolves a start prompt.
            await answer("Not allowed. Nothing changed.")
            return True
        # A prompt is answered in the chat it was asked in (#335): the chat
        # is the approval's target, so a callback from another chat never
        # resolves this chat's question.
        with _prompts_guard(self):
            row = self._prompts.get(cid)
        asked_chat = str((row or {}).get("chat_id") or "")
        press_chat = str(((query.get("message") or {}).get("chat") or {}).get("id") or "")
        if asked_chat and press_chat and press_chat != asked_chat:
            await answer("Not allowed. Nothing changed.")
            return True
        label = await self.answer_choice(cid, index, user_id=user_id)
        if label is None:
            await answer("That question already closed. Nothing changed.")
            return True
        await answer("")
        who = str(sender.get("first_name") or sender.get("username") or "Someone")
        await self.close_prompt(cid, f"{who} chose {label}")
        return True

    async def _answer_callback(self, query_id, text):
        if not query_id:
            return
        try:
            await asyncio.to_thread(self._call, "answerCallbackQuery",
                                    {"callback_query_id": str(query_id), "text": str(text or "")})
        except Exception:
            logger.debug("answerCallbackQuery failed", exc_info=True)


class TelegramClient:
    """Long-polling client shim with the surface BotCore and the health code need.

    ``user`` is the bot's own Telegram user (from getMe), ``core`` the
    BotCore once started. Updates are dispatched as managed tasks (see
    poll_once): messages become BotCore messages, callback queries go to
    the adapter.
    """

    def __init__(self, cfg, token, *, opener=None):
        self.cfg = cfg
        self.token = token
        self.opener = opener
        self.core = None
        self.user = None
        self.connected = False
        self._offset = 0
        self._background = []
        self._handlers = set()

    # -------------------------------------------------------------- attachments

    def _bind(self, message_ns, raw):
        """Point a parsed message's attachments at this client's downloader."""
        token, opener = self.token, self.opener
        for attachment in message_ns.attachments:
            file_id = attachment.file_id

            async def save(target, _file_id=file_id, _token=token, _opener=opener):
                data = await asyncio.to_thread(tg.download, _token, _file_id, opener=_opener)
                Path(target).write_bytes(data)

            attachment.save = save
        return message_ns

    # -------------------------------------------------------------- update routing

    async def handle_update(self, update):
        if not isinstance(update, dict):
            return None
        if isinstance(update.get("update_id"), int):
            self._offset = max(self._offset, update["update_id"] + 1)
        query = update.get("callback_query")
        if isinstance(query, dict):
            if self.core is not None:
                return await self.core.adapter.handle_callback_query(query)
            return None
        raw = update.get("message") or update.get("channel_post")
        parsed = parse_message(raw) if raw else None
        if parsed is None or self.core is None:
            return None
        return await self.core.handle_message(self._bind(parsed, raw))

    async def _run_update(self, update):
        """One dispatched update's handling; failures are logged, never raised
        into the poll loop that spawned the task."""
        try:
            await self.handle_update(update)
        except Exception:
            logger.exception("Telegram update handling failed")

    async def drain_handlers(self, timeout=HANDLER_DRAIN_S):
        """Wait (up to ``timeout`` seconds) for in-flight update handlers.

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
        """One getUpdates round; returns the number of updates received.

        Updates are dispatched as managed tasks, NOT awaited inline (bonus,
        found during DATA-7 — the P1-65 stall in the same shape): a handler
        can park awaiting a prompt answer, and that answer only arrives as
        an update in a later getUpdates round — awaiting the handler here
        starved the loop, so the answer could never be received and the
        flow stalled until its prompt timed out. The offset advances as
        each update is received (handle_update advances it the same way),
        so a later round never re-delivers an update whose handler is
        still running. Tasks are tracked in ``_handlers`` and drained by
        close(), so shutdown never drops one silently.
        """
        updates = await asyncio.to_thread(tg.get_updates, self.token, self._offset,
                                          poll_timeout=POLL_TIMEOUT_S, opener=self.opener)
        for update in updates:
            if isinstance(update, dict) and isinstance(update.get("update_id"), int):
                self._offset = max(self._offset, update["update_id"] + 1)
            task = asyncio.create_task(self._run_update(update))
            self._handlers.add(task)
            task.add_done_callback(self._handlers.discard)
        return len(updates)

    # -------------------------------------------------------------- lifecycle

    async def start(self):
        from .runtime import BotCore
        me = await asyncio.to_thread(tg.whoami, self.token, opener=self.opener)
        self.user = S(id=(me or {}).get("id"), username=(me or {}).get("username"))
        self.core = BotCore(self, self.cfg)
        try:  # best effort: typed commands work without the menu registration
            await asyncio.to_thread(self._register_commands)
        except Exception as exc:
            logger.warning("Telegram setMyCommands failed (typed commands still work): %r", exc)
        self.connected = True
        logger.info("logged in as @%s; print chat %s; %d allowed user(s); handlers: %s",
                    getattr(self.user, "username", None), self.core.chat_id,
                    len(self.core.adapter.allowed), ", ".join(self.core.router.handler_names()))
        health.write(self.core.status(True))
        self._background.append(asyncio.create_task(self._heartbeat()))
        port = int(self.cfg["bot"].get("health_port") or 0)
        if port:
            server = await health.serve(port, lambda: self.core.status(self.connected))
            self._background.append(server)
            logger.info("health check on :%d/healthz", port)

    def _register_commands(self):
        # The menu is filtered by this platform's capabilities (P2-123):
        # Telegram registers no /print_live, which only Discord can run.
        live = bool(self.core is not None
                    and getattr(self.core.adapter, "supports_live_stream", False))
        payload = {"commands": [{"command": name.replace("-", "_"), "description": c.description[:256]}
                                for name, c in commands.registrations(supports_live_stream=live)]}
        tg.call(self.token, "setMyCommands", payload, opener=self.opener)

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
            await asyncio.sleep(HEARTBEAT_S)

    async def run(self):
        await self.start()
        try:
            while True:
                await self.poll_once()
        finally:
            await self.close()

    async def close(self):
        self.connected = False
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
        if self._handlers:
            # Update handlers dispatched by poll_once (bonus, found during
            # DATA-7): a graceful shutdown gives in-flight ones a brief
            # chance to finish, then cancels what is left — a flow parked
            # on a prompt answer would otherwise hold shutdown open for its
            # whole prompt timeout. Only this loop's tasks are awaited;
            # foreign-loop tasks are cancelled and left to their own loop.
            await self.drain_handlers()
            loop = asyncio.get_running_loop()
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
    """Start the Telegram bot for ``cfg``; the entry point ``print_handy.bot.run`` delegates here."""
    token = telegram_token(cfg)
    if not token:
        name = str((cfg.get("telegram") or {}).get("token_env") or "TELEGRAM_BOT_TOKEN")
        print(f"Telegram bot token is empty: put it in the ${name} environment variable.", file=sys.stderr)
        return 3
    client = TelegramClient(cfg, token)
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        return 0
    return 0
