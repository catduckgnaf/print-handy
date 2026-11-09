"""The Web UI chat platform: BotCore hosted in the Web UI process.

The Web UI is a chat platform like any other. ``WebAdapter`` implements
the same platform contract as the Discord/Telegram/Matrix adapters —
outgoing posts land in an in-process outbox the Web UI renders, button
questions are recorded the same way and answered through the base
adapter's ``answer_choice``, typed answers go through the same
registry. ``WebChatService`` owns one :class:`BotCore` (built with
``client=None`` and this adapter) on its own event-loop thread and
feeds it events built with ``make_source``/``make_event``.

Parity is the point (catduck, 2026-11-08: web chat does everything,
even design): messages enter through ``BotCore.process`` — the router,
the deterministic handlers, the design flow, link and file intake, and
the print flow with its own confirm-before-print discipline. The web
layer never calls a printer, a slicer or an intake function directly;
it only delivers text and button presses, exactly like a chat client.

The operator is the one Web UI user: the page sits behind the Web UI's
existing auth, and inside the bot the operator is the single user
``webui``, also the approval user — a print approval in web chat is
answered by the same person who is signed in, through the same prompt
machinery a Discord approval uses.

``[webui] chat_llm_enabled`` mirrors ``[discord] chat_llm_enabled``:
with it off, web chat is a pure bot — commands, design's deterministic
parts and every handler still run, but no stray-chat model lane
(``[llm]`` or ``[agent]``) is ever called for web chat text.
"""
from __future__ import annotations

import asyncio
import threading
import time

from . import runtime
from .platform import BasePlatformAdapter, SendResult

OPERATOR_ID = "webui"
CHAT_ID = "webui"
RATE_LIMIT_PER_MINUTE = 20
OUTBOX_LIMIT = 200


class WebAdapter(BasePlatformAdapter):
    """Delivers BotCore's posts into an outbox list the Web UI polls."""

    name = "webui"
    message_limit = 4000
    supports_buttons = True
    max_buttons = 25
    uses_llm = True
    supports_live_stream = False

    def __init__(self, outbox, *, registry=None):
        kwargs = {"registry": registry} if registry is not None else {}
        super().__init__([OPERATOR_ID], approver=OPERATOR_ID, **kwargs)
        self._outbox = outbox

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        text = str(content or "")
        for start in range(0, max(len(text), 1), self.message_limit):
            self._outbox.append({"kind": "message",
                                 "content": text[start:start + self.message_limit]})
        return SendResult(True, message_id="web")

    async def send_clarify(self, chat_id, question, choices, clarify_id,
                           session_key, metadata=None):
        labels = [str(choice) for choice in choices]
        self._record_prompt(clarify_id, {"choices": labels,
                                         "question": str(question)})
        self._outbox.append({"kind": "prompt", "clarify_id": str(clarify_id),
                             "question": str(question), "choices": labels})
        return SendResult(True, message_id=str(clarify_id))

    async def close_prompt(self, clarify_id, note):
        self._prompts.pop(str(clarify_id), None)
        self._outbox.append({"kind": "prompt_closed",
                             "clarify_id": str(clarify_id),
                             "note": str(note or "")})


class WebChatService:
    """One BotCore for web chat, on its own event loop thread."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._entries = []          # outbox dicts, oldest first
        self._seq = 0
        self._sent_at = []          # submit timestamps for the rate limit

        outer = self

        class _Outbox:
            def append(self, entry):
                with outer._lock:
                    outer._seq += 1
                    outer._entries.append({"seq": outer._seq, **entry})
                    del outer._entries[:-OUTBOX_LIMIT]

        self.adapter = WebAdapter(_Outbox())
        webui = cfg.get("webui") or {}
        self.core = runtime.BotCore(
            None, cfg, platform_name="webui", adapter=self.adapter,
            chat_id=CHAT_ID,
            use_llm=bool(webui.get("chat_llm_enabled", True)))
        self.source = runtime.make_source(
            OPERATOR_ID, CHAT_ID, platform_name="webui")
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="webui-chat", daemon=True)
        self._thread.start()
        self._message_count = 0

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # -- operator side ------------------------------------------------------

    def submit(self, text):
        """Queue one operator message. False when rate-limited."""
        now = time.time()
        with self._lock:
            self._sent_at[:] = [t for t in self._sent_at if now - t < 60]
            if len(self._sent_at) >= RATE_LIMIT_PER_MINUTE:
                return False
            self._sent_at.append(now)
            self._message_count += 1
            count = self._message_count
            self._seq += 1
            self._entries.append({"seq": self._seq, "kind": "operator",
                                  "content": str(text or "")})
        event = runtime.make_event(self.source, text, f"web-{count}")
        asyncio.run_coroutine_threadsafe(self.core.process(event), self._loop)
        return True

    def answer(self, clarify_id, index):
        """Press a button on the open prompt, as the signed-in operator."""
        future = asyncio.run_coroutine_threadsafe(
            self.adapter.answer_choice(clarify_id, index, user_id=OPERATOR_ID),
            self._loop)
        return future.result(timeout=10)

    # -- rendering side -------------------------------------------------------

    def entries(self, since=0):
        with self._lock:
            return [entry for entry in self._entries if entry["seq"] > since]

    def open_prompt(self):
        """{clarify_id, question, choices} for the open prompt, or None.

        The page answers by clarify id (the button-press path every
        platform uses), so the open prompt is the newest recorded one
        the registry still holds open — flows register their questions
        under their own session keys, not the chat session's.
        """
        for cid, row in reversed(list(self.adapter._prompts.items())):
            if self.adapter.registry.is_open(cid):
                return {"clarify_id": str(cid),
                        "question": row.get("question") or "",
                        "choices": list(row.get("choices") or [])}
        return None


def chat_enabled(cfg):
    """Web chat's stray-chat LLM gate ([webui] chat_llm_enabled)."""
    return bool((cfg.get("webui") or {}).get("chat_llm_enabled", True))


__all__ = ["CHAT_ID", "OPERATOR_ID", "WebAdapter", "WebChatService", "chat_enabled"]
