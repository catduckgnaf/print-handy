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

Each Web UI session is its own conversation (#1145), never one shared
outbox: the server passes a per-session scope (:func:`scope_for`, a
digest of the login session id or, with no login, of the browser's
chat cookie), the scope rides as the BotCore source's ``thread_id`` so
BotCore keys its chat session per Web UI session, and every post goes
to the transcript of the scope it was made for. Two sessions never see
or answer each other's messages and questions; a post that belongs to
no session (a startup notice) is shown to all of them.

``[webui] chat_llm_enabled`` mirrors ``[discord] chat_llm_enabled``
and like it is off by default (#1376): with it off, web chat is a pure bot — commands, design's deterministic
parts and every handler still run, but no stray-chat model lane
(``[llm]`` or ``[agent]``) is ever called for web chat text.

``[webui] chat_enabled`` (default true) is the page's own switch
(PR #1106 review): off, the chat page shows a disabled state and this
service processes no message and no button press. It is distinct from
``chat_llm_enabled``, which only governs the model lane. There is
deliberately no claim-based lock on this surface: an install with no
login set up prints and approves from web chat exactly like a claimed
one (catduck's ruling, PR #1106) — access to the page itself is the
Web UI's own auth decision, as before.

Proactive alerts reach web chat the way they reach a chat platform: the
service runs the bot heartbeat's alert checks (``HEARTBEAT_CHECKS``) on
its own loop, through the same BotCore methods the Discord, Telegram
and Matrix heartbeats call. An alert belongs to no session, so it is a
shared post every session sees; its buttons (a ZMod update alert's
Update and Skip this version) are the prompt's own, pressed through
:meth:`WebChatService.answer` like any other. The merge-time guard
(tests/bot/test_command_webui_parity.py) fails a heartbeat check a chat
platform runs that web chat neither runs nor exempts with a reason.

The listener's print posts reach web chat too. The listener runs in its
own process and posts through the platform REST senders, not BotCore,
so it appends every post it delivered to the feed file in the state
folder both processes share (bot/web_feed.py), and this service reads
the new lines every ``FEED_POLL_S`` and posts them as shared posts: the
same ``format_event`` text (started, paused, resumed, complete,
cancelled, error, the roast and timelapse follow-ups) from every printer
family. An attachment the page cannot show is named in one line. An
alert that asks for a decision (a pause, an error, an emergency stop, a
camera check) comes with the same actions as buttons: web chat records
its own alert in the alert store (print_alert_buttons), and a press
claims it the way a chat press does: the operator allow-list
(answer_choice), peek, take once, then ``handle_click``, which re-reads
the printer before it acts. The buttons close when the alert leaves the
store (answered, or retired when the print ended).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import hashlib
import logging
import threading
import time
import uuid

from . import rest, runtime, web_feed
from .platform import BasePlatformAdapter, SendResult, _prompts_guard

logger = logging.getLogger(__name__)

OPERATOR_ID = "webui"
CHAT_ID = "webui"
RATE_LIMIT_PER_MINUTE = 20
# The bot heartbeat's alert checks web chat runs too (BotCore method
# names), and how often. Each schedules only and never raises, exactly
# as on the chat platforms (client.py, telegram.py, matrix.py).
HEARTBEAT_CHECKS = ("check_firmware_updates",)
HEARTBEAT_S = 15
# How often the listener's feed is read (a stat when nothing changed).
FEED_POLL_S = 2
# How often an open listener alert checks that it is still in the store.
ALERT_RECHECK_S = 5
OUTBOX_LIMIT = 200
# Web UI sessions whose transcripts are kept; the least recently used
# one is forgotten past this.
MAX_SCOPES = 64


# The web chat scope the running turn belongs to (a per-session id, see
# scope_for). Set around every BotCore turn and button press the service
# starts; asyncio tasks and asyncio.to_thread copy it, so a reply sent
# from deep inside a flow (or from a task that flow spawned) lands in the
# transcript of the session that started it even when the send carries
# no thread metadata.
_CURRENT_SCOPE = contextvars.ContextVar("webui_chat_scope", default="")


def scope_for(session_secret):
    """The web chat scope for one Web UI session.

    ``session_secret`` is the browser's login-session id, or its chat
    cookie on an install with no login. The scope is a one-way digest
    of it under its own label, so the value that ends up in a BotCore
    session key, a receipt or a log never reveals the cookie (nor the
    remembered-session store's key, a plain SHA-256 of the session id).
    Empty in, empty out: the unscoped default transcript."""
    secret = str(session_secret or "")
    if not secret:
        return ""
    digest = hashlib.sha256(b"print-handy web chat scope\x00" + secret.encode("utf-8"))
    return "s" + digest.hexdigest()[:32]


class WebAdapter(BasePlatformAdapter):
    """Delivers BotCore's posts into per-session outboxes the Web UI polls.

    Each Web UI session is its own conversation: its turns run with the
    session's scope as the source's ``thread_id`` (so BotCore keys its
    sessions, open questions, typed answers and recent links per Web UI
    session), and every post goes to the outbox of the scope it names —
    the ``thread_id`` metadata a handler passes, else the scope of the
    turn that is running. A post with neither (a startup notice) is
    shared: every session sees it, since it belongs to none of them.
    """

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

    @staticmethod
    def _scope(metadata=None):
        thread = (metadata or {}).get("thread_id")
        return str(thread) if thread else _CURRENT_SCOPE.get()

    def prompt_scope(self, clarify_id):
        """The scope a prompt was asked in, or "" for a shared one.

        Read from the registry's session key first ("webui:webui:<scope>:
        webui", the thread segment), else from the recorded row."""
        key = self.registry.session_key(clarify_id)
        parts = str(key or "").split(":")
        if len(parts) >= 3 and parts[0] == self.name and parts[2]:
            return parts[2]
        with _prompts_guard(self):
            row = self._prompts.get(str(clarify_id)) or {}
        return str(row.get("scope") or "")

    async def send(self, chat_id, content, reply_to=None, metadata=None, files=None):
        # The chat page cannot attach files, so an attachment becomes
        # a note naming the file — never the marker itself, whose server
        # path is not the reader's business. Markers go through the same
        # restricted compatibility path as on the other platforms.
        scope = self._scope(metadata)
        body, files = rest.outgoing(str(content or ""), files)
        if body or not files:
            for start in range(0, max(len(body), 1), self.message_limit):
                self._outbox.append({"kind": "message",
                                     "content": body[start:start + self.message_limit]},
                                    scope)
        for path in files:
            self._outbox.append({"kind": "message",
                                 "content": f"📎 {path.name}"}, scope)
        return SendResult(True, message_id="web")

    async def send_clarify(self, chat_id, question, choices, clarify_id,
                           session_key, metadata=None):
        scope = self._scope(metadata)
        labels = [str(choice) for choice in choices]
        self._record_prompt(clarify_id, {"choices": labels,
                                         "question": str(question),
                                         "scope": scope})
        self._outbox.append({"kind": "prompt", "clarify_id": str(clarify_id),
                             "question": str(question), "choices": labels}, scope)
        return SendResult(True, message_id=str(clarify_id))

    async def close_prompt(self, clarify_id, note):
        scope = self.prompt_scope(clarify_id)
        with _prompts_guard(self):
            self._prompts.pop(str(clarify_id), None)
        self._outbox.append({"kind": "prompt_closed",
                             "clarify_id": str(clarify_id),
                             "note": str(note or "")}, scope)


class _PressGate:
    """Claim-or-abandon for one button press (#1145, duckcatgnaf on #1106).

    The press runs on the chat loop while the page's request thread waits.
    Exactly one side wins: the loop claims the press before acting, or the
    waiting thread abandons it after its timeout. A claimed press runs and
    the page waits for its real result; an abandoned press never runs.
    Cancelling the future alone cannot promise that once the coroutine has
    been scheduled.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state = None

    def _take(self, state):
        with self._lock:
            if self._state is None:
                self._state = state
                return True
            return False

    def claim(self):
        return self._take("claimed")

    def abandon(self):
        return self._take("abandoned")


class WebChatService:
    """One BotCore for web chat, on its own event loop thread.

    The transcript is kept per scope (one per Web UI session, see
    scope_for): a session sees its own entries and the shared ones
    (posts that belong to no session), never another session's. Every
    method takes the ``scope``; the default "" is the unscoped
    transcript a caller without sessions uses. :meth:`view` binds one.
    """

    # How long a button press waits to start on the chat loop before it is
    # abandoned (never run) and the page says it did not reach the bot.
    press_wait_s = 10

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._transcripts = {}      # scope -> outbox dicts, oldest first
        self._shared = []           # entries every scope sees
        self._seq = 0
        self._sent_at = []          # submit timestamps for the rate limit
        self._inflight = {}         # scope -> process() turns not yet finished

        outer = self

        class _Outbox:
            def append(self, entry, scope=""):
                outer._append(entry, scope)

        self.adapter = WebAdapter(_Outbox())
        webui = cfg.get("webui") or {}
        self.core = runtime.BotCore(
            None, cfg, platform_name="webui", adapter=self.adapter,
            chat_id=CHAT_ID,
            use_llm=bool(webui.get("chat_llm_enabled", False)))
        self.source = self._source("")
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="webui-chat", daemon=True)
        self._thread.start()
        self._message_count = 0
        self._heartbeat = None
        self._feed_task = None
        self._alert_tasks = set()
        # Lines the listener writes from now on (a restart replays nothing).
        self._feed = web_feed.FeedReader(_state_dir(cfg))
        if chat_enabled(cfg):
            asyncio.run_coroutine_threadsafe(self._announce_startup(), self._loop)
            self._heartbeat = asyncio.run_coroutine_threadsafe(
                self._in_scope("", self._run_heartbeat), self._loop)
            self._feed_task = asyncio.run_coroutine_threadsafe(
                self._in_scope("", self._run_feed), self._loop)

    @staticmethod
    def _source(scope):
        """The BotCore source for one scope: the operator in the web chat,
        with the scope as the thread, so session_key() differs per Web UI
        session while the chat (and so the gateway's routing) is the same."""
        return runtime.make_source(OPERATOR_ID, CHAT_ID, thread_id=scope or None,
                                   platform_name="webui")

    def _append(self, entry, scope=""):
        """Record one outbox entry in its scope's transcript (or the shared
        one). The oldest scopes are forgotten past MAX_SCOPES, and each
        transcript keeps its newest OUTBOX_LIMIT entries."""
        scope = str(scope or "")
        with self._lock:
            self._seq += 1
            row = {"seq": self._seq, **entry}
            if not scope:
                self._shared.append(row)
                del self._shared[:-OUTBOX_LIMIT]
                return
            entries = self._transcripts.pop(scope, None)
            if entries is None:
                entries = []
            # Most recently used last, so the trim drops the stalest.
            self._transcripts[scope] = entries
            entries.append(row)
            del entries[:-OUTBOX_LIMIT]
            while len(self._transcripts) > MAX_SCOPES:
                self._transcripts.pop(next(iter(self._transcripts)))

    def _touch_locked(self, scope):
        """Mark ``scope`` most recently used (caller holds the lock) and
        return its transcript; an unknown scope is not created."""
        entries = self._transcripts.pop(scope, None)
        if entries is None:
            return []
        self._transcripts[scope] = entries
        return entries

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _announce_startup(self):
        """The on_ready notices, for a Web UI process start.

        The Discord client announces intakes a restart stranded and a
        launch a hard kill left pending (client.on_ready); a Web UI
        restart strands the same state, so web chat makes the same two
        announcements, once per service, fail-soft exactly as there.
        Only web chat's own receipts: the chat bot's process shares the
        receipt folder, and its live intakes are not this process's to
        call interrupted. A receipt names the session it came from (its
        thread), so its notice goes to that session's transcript.
        """
        try:
            from ..core import prepared_intake
            from ..runtime import _config
            await prepared_intake.announce_interrupted(
                _config.state_dir() / "model-link-intakes", self.adapter,
                platform=WebAdapter.name)
        except Exception:
            logger.exception("could not announce interrupted intakes")
        try:
            await self.core.announce_pending_launch()
        except Exception:
            logger.exception("could not announce a pending launch")

    async def heartbeat_once(self):
        """One pass of the alert checks (HEARTBEAT_CHECKS); never raises.

        Skipped while the page's switch is off: an alert would land in a
        transcript nobody can see and offer buttons nobody can press.
        Runs in the shared scope, so every alert is a shared post.
        """
        if not self._enabled():
            return []
        results = []
        for name in HEARTBEAT_CHECKS:
            try:
                results.append(await getattr(self.core, name)())
            except Exception:
                logger.exception("web chat %s failed", name)
        return results

    async def _run_heartbeat(self):
        while True:
            try:
                await self.heartbeat_once()
            except Exception:  # the loop must outlive any one bad pass
                logger.exception("web chat heartbeat pass failed")
            await asyncio.sleep(HEARTBEAT_S)

    # -- the listener's print posts ----------------------------------------

    async def feed_once(self):
        """Post the listener's new feed lines (bot/web_feed.py); never raises.

        Skipped while the page's switch is off, like the heartbeat; the
        lines are still read, so turning it back on never floods the page
        with posts from while it was off. Runs in the shared scope: a
        print post belongs to no session. Returns the lines posted.
        """
        rows = await asyncio.to_thread(self._feed.poll)
        if not rows or not self._enabled():
            return []
        for row in rows:
            try:
                text = web_feed.render(row)
                alert = row.get("alert")
                if isinstance(alert, dict) and alert.get("kind") in web_feed.ALERT_KINDS:
                    task = asyncio.ensure_future(self._listener_alert(text, alert))
                    self._alert_tasks.add(task)
                    task.add_done_callback(self._alert_tasks.discard)
                    await asyncio.sleep(0)  # let it post before the next line
                elif text:
                    await self.adapter.send(CHAT_ID, text)
            except Exception:
                logger.exception("web chat could not post a listener line")
        return rows

    async def _run_feed(self):
        while True:
            try:
                await self.feed_once()
            except Exception:
                logger.exception("web chat feed pass failed")
            await asyncio.sleep(FEED_POLL_S)

    async def _listener_alert(self, text, alert):
        """One listener alert as a shared prompt with the alert's own buttons.

        Web chat records its own alert in the alert store (the same kind,
        file and routing fields the chat button's record has), so a press
        here goes through exactly the claim-then-act path a chat press
        does: the operator allow-list (answer_choice), peek, take once,
        then ``print_alert_buttons.handle_click``, which re-reads the
        printer before acting and never retries. A second press, here or
        after the record left the store, sends nothing.
        """
        from ..core import prompts
        from ..runtime import print_alert_buttons as pab
        kind = str(alert["kind"])
        filename = str(alert.get("filename") or "")
        fields = alert.get("fields") if isinstance(alert.get("fields"), dict) else {}
        aid = await asyncio.to_thread(pab.record, kind, filename, **fields)
        buttons = []
        for row in pab.components(aid, kind):
            for button in row["components"]:
                parsed = pab.parse_custom_id(button.get("custom_id"))
                if parsed:
                    buttons.append((button["label"], parsed[0]))
        labels = [label for label, _action in buttons]
        registry = prompts.gateway()
        clarify_id = f"webalert-{uuid.uuid4().hex[:12]}"
        session = f"listener-alert:{CHAT_ID}:{aid}"
        registry.register(clarify_id, session, text, labels)
        try:
            await self.adapter.send_clarify(CHAT_ID, text, labels, clarify_id, session)
        except Exception:
            logger.exception("web chat could not post a listener alert")
            _close_prompt(registry, clarify_id)
            await asyncio.to_thread(pab.take, aid)
            return None
        answer = await self._await_alert(registry, clarify_id, aid)
        _close_prompt(registry, clarify_id)
        action = dict(buttons).get(answer)
        if action is None:
            gone = await asyncio.to_thread(pab.take, aid) is None
            await self.adapter.close_prompt(
                clarify_id, "This alert was answered or ended. Nothing sent."
                if gone else "This alert expired. Nothing sent.")
            return None
        claimed = await asyncio.to_thread(pab.peek, aid)
        if not claimed or not await asyncio.to_thread(pab.take, aid):
            await self.adapter.close_prompt(
                clarify_id, "That alert is old or was already answered. Nothing sent.")
            return None
        await self.adapter.close_prompt(clarify_id, f"You chose {answer}.")
        try:
            outcome = await asyncio.to_thread(pab.handle_click, action, claimed)
        except Exception:
            logger.exception("web chat alert action %s failed", action)
            outcome = "That action failed before it reached the printer. The error was logged."
        await self.adapter.send(CHAT_ID, str(outcome))
        return outcome

    async def _await_alert(self, registry, clarify_id, aid):
        """The pressed label, or None once the alert left the store or its TTL passed."""
        from ..runtime import print_alert_buttons as pab
        deadline = time.time() + pab.TTL
        while registry.is_open(clarify_id) and time.time() < deadline:
            await asyncio.sleep(min(ALERT_RECHECK_S, max(0.0, deadline - time.time())))
            if registry.is_open(clarify_id) and await asyncio.to_thread(pab.peek, aid) is None:
                return None  # retired at print end, or expired in the store
        return registry.wait_for_response(clarify_id, 0)

    async def _in_scope(self, scope, coro_fn):
        """Run one turn (or press) with the scope set for every send it makes."""
        token = _CURRENT_SCOPE.set(str(scope or ""))
        try:
            return await coro_fn()
        finally:
            _CURRENT_SCOPE.reset(token)

    # -- operator side ------------------------------------------------------

    def view(self, scope):
        """This service bound to one scope (what the chat page renders)."""
        return ScopedChat(self, scope)

    def _enabled(self):
        """The page's own switch, read live from the settings mapping."""
        return chat_enabled(self.cfg)

    def submit(self, text, scope=""):
        """Queue one operator message in ``scope``. False when rate-limited or off.

        The rate limit counts every scope together: it guards the bot
        (and the model budget), which the sessions share."""
        if not self._enabled():
            return False
        scope = str(scope or "")
        now = time.time()
        with self._lock:
            self._sent_at[:] = [t for t in self._sent_at if now - t < 60]
            if len(self._sent_at) >= RATE_LIMIT_PER_MINUTE:
                return False
            self._sent_at.append(now)
            self._message_count += 1
            count = self._message_count
            self._inflight[scope] = self._inflight.get(scope, 0) + 1
        self._append({"kind": "operator", "content": str(text or "")}, scope)
        event = runtime.make_event(self._source(scope), text, f"web-{count}")
        future = asyncio.run_coroutine_threadsafe(
            self._in_scope(scope, lambda: self.core.process(event)), self._loop)
        future.add_done_callback(lambda done: self._process_done(done, scope))
        return True

    def _process_done(self, future, scope=""):
        """A finished turn: failures are logged and posted, never silent.

        The future used to be discarded, so a handler exception
        produced no reply and no log — the operator's message simply
        vanished. The failure entry names no internals; the exception
        itself goes to the log.
        """
        with self._lock:
            left = self._inflight.get(scope, 0) - 1
            if left > 0:
                self._inflight[scope] = left
            else:
                self._inflight.pop(scope, None)
        if future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            logger.exception("web chat message handling failed", exc_info=exc)
            self._append({
                "kind": "message",
                "content": "Something went wrong with that message, so it "
                           "wasn't acted on. The error was logged."}, scope)

    def busy(self, scope=""):
        """True while a message this scope submitted is still running."""
        with self._lock:
            return self._inflight.get(str(scope or ""), 0) > 0

    def answer(self, clarify_id, index, scope=""):
        """Press a button on an open prompt, as the signed-in operator.

        Only a prompt this scope can see — its own, or a shared one —
        can be pressed from it; another session's prompt resolves
        nothing. Resolves nothing when the page's switch is off.
        """
        if not self._enabled():
            return None
        scope = str(scope or "")
        owner = self.adapter.prompt_scope(clarify_id)
        if owner and owner != scope:
            logger.info("refused web chat press on prompt %s from another session",
                        clarify_id)
            return None
        gate = _PressGate()

        async def press():
            if not gate.claim():
                return None  # the page already said it did not reach the bot
            return await self._in_scope(owner or scope, lambda: self.adapter.answer_choice(
                clarify_id, index, user_id=OPERATOR_ID))

        future = asyncio.run_coroutine_threadsafe(press(), self._loop)
        try:
            return future.result(timeout=self.press_wait_s)
        except concurrent.futures.TimeoutError:
            if gate.abandon():
                # Never started, and now never will: the page's "didn't
                # reach the bot, the question is still open" is true.
                future.cancel()
                logger.info("web chat press on prompt %s abandoned after %ss",
                            clarify_id, self.press_wait_s)
                raise
            # It started before the timeout: report what it actually did.
            return future.result()

    # -- rendering side -------------------------------------------------------

    def entries(self, since=0, scope=""):
        """This scope's transcript and the shared entries, oldest first.

        Reading a transcript counts as using it: the scope moves to the
        most recently used end, so a session that is only polling is not
        evicted ahead of scopes nobody is reading."""
        with self._lock:
            own = self._touch_locked(str(scope or "")) if scope else []
            rows = [entry for entry in (*self._shared, *own) if entry["seq"] > since]
        return sorted(rows, key=lambda entry: entry["seq"])

    def open_prompt(self, scope=""):
        """{clarify_id, question, choices} for this scope's open prompt, or None.

        The page answers by clarify id (the button-press path every
        platform uses), so the open prompt is the newest recorded one
        the registry still holds open — flows register their questions
        under their own session keys, not the chat session's — among
        the prompts this scope may see: its own and the shared ones.
        """
        scope = str(scope or "")
        for cid, row in reversed(self.adapter.prompts_snapshot()):
            owner = self.adapter.prompt_scope(cid)
            if owner and owner != scope:
                continue
            if self.adapter.registry.is_open(cid):
                return {"clarify_id": str(cid),
                        "question": row.get("question") or "",
                        "choices": list(row.get("choices") or [])}
        return None


class ScopedChat:
    """One Web UI session's view of the web chat service."""

    def __init__(self, service, scope):
        self.service = service
        self.scope = str(scope or "")

    def submit(self, text):
        return self.service.submit(text, self.scope)

    def answer(self, clarify_id, index):
        return self.service.answer(clarify_id, index, self.scope)

    def entries(self, since=0):
        return self.service.entries(since, self.scope)

    def busy(self):
        return self.service.busy(self.scope)

    def open_prompt(self):
        return self.service.open_prompt(self.scope)


def _close_prompt(registry, clarify_id):
    registry.resolve_gateway_clarify(clarify_id, registry.CANCELLED)
    registry.wait_for_response(clarify_id, 0.001)


def _state_dir(cfg):
    """The state folder the listener's feed lives in, from these settings."""
    from .. import settings
    try:
        return settings.state_dir(cfg)
    except Exception:
        return settings.state_dir()


def chat_llm_enabled(cfg):
    """Web chat's stray-chat LLM gate ([webui] chat_llm_enabled)."""
    return bool((cfg.get("webui") or {}).get("chat_llm_enabled", False))


def chat_enabled(cfg):
    """Whether the web chat surface is on at all ([webui] chat_enabled).

    On by default. Off, the chat page shows a disabled state and the
    service processes no message and no button press. This is the
    page's own switch — chat_llm_enabled only governs the model lane.
    (This helper carried the LLM gate's meaning before the switch
    existed; the gate is chat_llm_enabled above.)
    """
    return bool((cfg.get("webui") or {}).get("chat_enabled", True))


__all__ = ["CHAT_ID", "FEED_POLL_S", "HEARTBEAT_CHECKS", "OPERATOR_ID", "ScopedChat", "WebAdapter", "WebChatService",
           "chat_enabled", "chat_llm_enabled", "scope_for"]
