"""One job card per print job on Discord (#1472).

A simple job used to post 9+ bot messages (preview, each question, each
progress phase, each summary repost, the start post, then completion plus
a separate roast). The intake now runs through :class:`JobCardSession`
when the platform adapter supports job cards (Discord does; Telegram,
Matrix and web chat do not, and keep today's behavior untouched):

* The session wraps the adapter for one ``prepare_link`` /
  ``print_gcode_file`` run. The first thing the intake shows posts the
  card — a Discord embed built from :class:`JobCard` — and everything
  after that edits the card in place instead of posting again.
* Questions attach to the card as its buttons (the adapter's
  ``attach_card_prompt``); an answered question collapses into a line on
  the card, the generalization of ``close_prompt``'s single-prompt edit.
* Progress phases edit the card's status line. A summary or warning that
  is shown again replaces its earlier copy: notes are keyed, and a repeat
  of a keyed note edits the card (or its thread copy) instead of posting.
* With ``[discord] job_card_thread`` on, long detail (support analysis,
  shortage details, camera checks) goes to a thread opened on the card;
  the main channel keeps the card and the completion.

The session is presentation only: no intake decision changes, and every
registry resolution, initiator rule and approver rule stays exactly
where it was — card buttons use the same ``pbq:`` custom ids and the
same adapter ``handle_click`` as standalone prompts.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from .platform import SendResult

logger = logging.getLogger(__name__)

# Embed colors per card state (Discord color integers).
STATE_COLORS = {
    "asking": 0xF5C542,    # amber: waiting on a person
    "working": 0x3498DB,   # blue: busy, nothing needed
    "slicing": 0x3498DB,
    "ready": 0x2ECC71,     # green: sliced, ready when you are
    "printing": 0x1ABC9C,  # teal: on the printer
    "done": 0x2ECC71,
    "failed": 0xE74C3C,    # red: needs attention
    "cancelled": 0x95A5A6,  # grey: stopped on purpose
}

STATE_LABELS = {
    "asking": "Waiting for an answer",
    "working": "Working",
    "slicing": "Slicing",
    "ready": "Ready",
    "printing": "Printing",
    "done": "Print complete",
    "failed": "Failed",
    "cancelled": "Cancelled",
}

# A note at least this long is "long detail": with the thread setting on
# it lives in the card's thread instead of on the card.
DETAIL_MIN_CHARS = 300

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

_PROGRESS_HEADS = (
    "checking", "still checking", "still slicing", "slicing", "preparing to start",
    "preparing", "uploading", "uploaded to the printer", "downloading", "fetching",
    "waiting for the background slice", "starting", "working", "fit check",
)

_FAILURE_MARKS = (
    "could not", "couldn't", "stopped before", "failed", "refused", "refusal",
    "nothing was sliced", "nothing was uploaded", "not be prepared",
    "could not be", "unable to",
)

# Notes whose repeats must replace, keyed by a stable marker in the text.
_NOTE_KEYS = (
    ("ready to slice", "ready"),
    ("support toolpaths do not cover", "support-warning"),
    ("heads up on the source profile", "profile-warning"),
    ("sliced with supports", "support-note"),
    ("short on filament", "shortage"),
    ("will run out", "shortage"),
    ("camera check", "camera-check"),
)

_DETAIL_MARKS = (
    "support toolpaths", "support analysis", "short on filament", "will run out",
    "camera check", "not enough filament", "shortage",
)

_MEDIA_LINE = re.compile(r"^MEDIA:(\S+)\s*$", re.M)


def _first_line(text):
    return str(text or "").strip().splitlines()[0].strip() if str(text or "").strip() else ""


def _note_key(text):
    low = str(text).casefold()
    for mark, key in _NOTE_KEYS:
        if mark in low:
            return key
    return "note-" + hashlib.sha1(str(text).strip().encode("utf-8", "replace")).hexdigest()[:12]


def _is_detail(text):
    if len(str(text)) >= DETAIL_MIN_CHARS:
        return True
    low = str(text).casefold()
    return any(mark in low for mark in _DETAIL_MARKS)


def _state_for_progress(text):
    low = str(text).casefold()
    if "slice" in low or "slicing" in low:
        return "slicing"
    return "working"


class JobCard:
    """The card's content: one job's name, facts, state and lines.

    Platform-neutral data; the Discord adapter turns it into an embed
    (:meth:`embed_spec`). The adapter mutates the same object when a
    button is pressed, so the session's next render never resurrects a
    question that was already answered.
    """

    def __init__(self, title=None):
        self.title = str(title or "").strip() or "Print job"
        self.thumbnail_url = None
        self.thumbnail_path = None
        self.material = None      # material / slot line, e.g. "White PLA · Slot 1"
        self.estimate = None      # e.g. "about 1h 0m"
        self.state = "working"
        self.status = None        # the current phase line
        self.question = None      # the open question's text, while one is open
        self.question_cid = None
        self.answered = []        # collapsed "question -> answer" lines, in order
        self.notes = {}           # key -> text, insertion ordered; a repeat replaces
        self.message_id = None

    # -------------------------------------------------------------- facts

    def set_title(self, title):
        title = re.sub(r"\s+", " ", str(title or "")).strip()
        if title:
            self.title = title

    def add_answered(self, question, answer_line, cid=None):
        """Collapse an answered question into one line on the card.

        ``cid`` names the answered prompt: the open question is cleared
        only when it is that prompt (or none is named), so an answer
        recorded after the next question went up never erases it."""
        short = _first_line(question)
        if len(short) > 90:
            short = short[:87] + "..."
        line = f"{short} → {answer_line}" if short else str(answer_line)
        if line not in self.answered:
            self.answered.append(line)
        if cid is not None and self.question_cid not in (None, str(cid)):
            return
        if self.question_cid is not None or self.question is not None:
            self.question = None
            self.question_cid = None

    def add_note(self, key, text):
        self.notes[str(key)] = str(text)

    # -------------------------------------------------------------- render

    def embed_spec(self):
        """The embed as plain data (the adapter builds the discord.Embed)."""
        parts = []
        if self.status:
            parts.append(str(self.status))
        if self.question:
            question = str(self.question)
            if len(question) > 1600:
                # A question naming hundreds of parts cannot fit an embed
                # whole: keep its head and its tail — the tail is where
                # the actual ask ("choose ... or ...?") lives.
                question = question[:800] + "\n...\n" + question[-800:]
            parts.append(f"**Waiting:** {question}")
        for line in self.answered[-6:]:
            parts.append(f"> {line}")
        for key, text in self.notes.items():
            shown = str(text)
            if len(shown) > 1000:
                shown = shown[:997] + "..."
            parts.append(shown)
        description = "\n\n".join(parts)[:4000]
        fields = []
        if self.material:
            fields.append({"name": "Material", "value": str(self.material)[:1024], "inline": True})
        if self.estimate:
            fields.append({"name": "Estimate", "value": str(self.estimate)[:1024], "inline": True})
        spec = {
            "title": self.title[:256],
            "description": description,
            "color": STATE_COLORS.get(self.state, STATE_COLORS["working"]),
            "fields": fields,
            "footer": STATE_LABELS.get(self.state, self.state),
        }
        if self.thumbnail_url:
            spec["thumbnail"] = self.thumbnail_url
        elif self.thumbnail_path:
            spec["thumbnail"] = f"attachment://{Path(str(self.thumbnail_path)).name}"
        return spec

    def plain_text(self):
        """The card as text (used by tests and non-embed renderings)."""
        spec = self.embed_spec()
        lines = [spec["title"]]
        if spec["description"]:
            lines.append(spec["description"])
        for field in spec["fields"]:
            lines.append(f"{field['name']}: {field['value']}")
        lines.append(spec["footer"])
        return "\n".join(lines)


class CardStopHandle:
    """A message-shaped handle for the card's Stop prompt (#1472).

    ``prepared_intake._with_stop`` and the refusal editor treat the Stop
    prompt as a message they ``edit`` when the step ends or turns into a
    refusal. On the card there is no separate message: the handle routes
    those edits back onto the card, so the refusal text lands on the card
    and the Stop button retires, exactly the end state the standalone
    prompt reaches.
    """

    is_job_card_handle = True

    def __init__(self, session):
        self._session = session
        self.id = session.card.message_id

    async def edit(self, content=None, view=None, **kwargs):
        session = self._session
        if content:
            session.card.state = "failed"
            session.card.add_note("refusal", str(content))
            session.card.status = _first_line(content)
        if view is not None:
            await session._update_card(view=view)
        else:
            await session._update_card(clear_view=True)


_sessions = {}


def active_session_for(source):
    """The live card session for ``source``'s chat/thread, or None.

    The Stop sender in the bot handlers posts outside the adapter, so it
    finds the job's card through this registry instead.
    """
    thread = getattr(source, "thread_id", None)
    for key in (thread, getattr(source, "chat_id", None)):
        if key and str(key) in _sessions:
            return _sessions[str(key)]
    return None


class JobCardSession:
    """An adapter wrapper that presents one intake as one job card.

    Only wraps adapters with ``supports_job_card``; anything else passes
    through unchanged (:meth:`maybe`). Every method the intake uses is
    either card-aware here or delegated to the wrapped adapter untouched
    (``__getattr__``), so access checks, the prompt registry and typed
    answers behave exactly as without a card.
    """

    def __init__(self, adapter, source=None):
        self._adapter = adapter
        self._source = source
        self.card = JobCard()
        self._card_posted = False
        self._thread_id = None
        self._thread_notes = {}
        self._registered_keys = []
        self.thread_enabled = bool(getattr(adapter, "job_card_thread", False))
        self._register(source)

    @classmethod
    def maybe(cls, adapter, source=None):
        """``adapter`` wrapped in a session when it supports job cards, else itself."""
        if isinstance(adapter, cls):
            return adapter
        if not getattr(adapter, "supports_job_card", False):
            return adapter
        try:
            return cls(adapter, source)
        except Exception:
            logger.debug("job card session unavailable; intake continues uncarded", exc_info=True)
            return adapter

    # ---------------------------------------------------------- registry

    def _register(self, source):
        if source is None:
            return
        for key in (getattr(source, "thread_id", None), getattr(source, "chat_id", None)):
            if key:
                _sessions[str(key)] = self
                self._registered_keys.append(str(key))

    async def close(self):
        """End the session: leave the registry. The card stays as posted."""
        for key in self._registered_keys:
            if _sessions.get(key) is self:
                _sessions.pop(key, None)
        self._registered_keys = []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._adapter, name)

    # ---------------------------------------------------------- card plumbing

    async def _ensure_card(self, chat_id, metadata=None):
        if self._card_posted:
            return True
        post = getattr(self._adapter, "post_card", None)
        if post is None:
            return False
        try:
            result = await post(chat_id, self.card, metadata=metadata)
        except Exception:
            logger.debug("job card post failed", exc_info=True)
            return False
        if result is not None and getattr(result, "success", False):
            self._card_posted = True
            if getattr(result, "message_id", None):
                self.card.message_id = str(result.message_id)
            return True
        return False

    async def _update_card(self, view=None, clear_view=False):
        update = getattr(self._adapter, "update_card", None)
        if update is None or not self._card_posted:
            return False
        try:
            result = await update(self.card, view=view, clear_view=clear_view)
        except Exception:
            logger.debug("job card update failed", exc_info=True)
            return False
        return bool(result is None or getattr(result, "success", True))

    async def _ensure_thread(self, chat_id, metadata=None):
        if self._thread_id or not self.thread_enabled:
            return self._thread_id
        opener = getattr(self._adapter, "open_card_thread", None)
        if opener is None:
            return None
        try:
            self._thread_id = await opener(chat_id, self.card, metadata=metadata)
        except Exception:
            logger.debug("job card thread open failed", exc_info=True)
            self._thread_id = None
        return self._thread_id

    async def _post_detail(self, chat_id, metadata, key, text):
        """Long detail: the card's thread when enabled, else a card note."""
        thread_id = await self._ensure_thread(chat_id, metadata)
        if thread_id:
            known = self._thread_notes.get(key)
            if known:
                edited = await self._adapter.edit_message(chat_id=str(thread_id), message_id=known,
                                                          content=str(text)[:2000])
                if edited is not None and getattr(edited, "success", False):
                    return
            sent = await self._adapter.send(chat_id=str(thread_id), content=str(text),
                                            metadata=metadata)
            if sent is not None and getattr(sent, "message_id", None):
                self._thread_notes[key] = str(sent.message_id)
            return
        self.card.add_note(key, text)

    # ---------------------------------------------------------- prompt collapse

    def _sync_closed_prompt(self):
        """Drop a stale open question (expired/cancelled) from the card."""
        cid = self.card.question_cid
        if not cid:
            return
        try:
            from ..core import prompts
            if prompts.is_open(cid):
                return
        except Exception:
            logger.debug("job card could not check prompt %s; leaving it open", cid, exc_info=True)
            return
        short = _first_line(self.card.question)
        if short:
            self.card.add_answered(short, "no answer (closed)")
        self.card.question = None
        self.card.question_cid = None

    # ---------------------------------------------------------- adapter surface

    async def send(self, chat_id, content, reply_to=None, metadata=None, files=None):
        text = "" if content is None else str(content)
        media_paths = [Path(p) for p in _MEDIA_LINE.findall(text)]
        explicit = [Path(f) for f in (files or []) if f]
        attachments = explicit + [p for p in media_paths if p not in explicit]
        images = [p for p in attachments if p.suffix.lower() in _IMAGE_SUFFIXES]
        others = [p for p in attachments if p.suffix.lower() not in _IMAGE_SUFFIXES]
        body = _MEDIA_LINE.sub("", text).strip()
        if others:
            # A real deliverable (G-code, a bundle): posted as its own
            # message, as today. The card never swallows a file hand-off.
            return await self._adapter.send(chat_id, content, reply_to=reply_to,
                                            metadata=metadata, files=files)
        if images and not self.card.thumbnail_path:
            self.card.thumbnail_path = str(images[0])
        if not body and not images:
            return await self._adapter.send(chat_id, content, reply_to=reply_to,
                                            metadata=metadata, files=files)
        if not body and images:
            # A preview/thumbnail image on its own: it becomes the card's
            # thumbnail instead of its own message.
            if await self._ensure_card(chat_id, metadata):
                await self._update_card()
                return SendResult(True, message_id=self.card.message_id)
            return await self._adapter.send(chat_id, content, reply_to=reply_to,
                                            metadata=metadata, files=files)
        await self._absorb_send(chat_id, metadata, body)
        if await self._ensure_card(chat_id, metadata):
            await self._update_card()
            return SendResult(True, message_id=self.card.message_id)
        return await self._adapter.send(chat_id, content, reply_to=reply_to,
                                        metadata=metadata, files=files)

    async def _absorb_send(self, chat_id, metadata, text):
        if not text:
            return
        self._sync_closed_prompt()
        card = self.card
        low = text.casefold()
        first = _first_line(text)
        if text.startswith("That looks like "):
            match = re.match(r"That looks like ([^,.]+)", text)
            if match:
                card.set_title(match.group(1))
            est = re.search(r"estimated print time about ([^,]+)", text)
            if est:
                card.estimate = f"about {est.group(1).strip()}"
            fil = re.search(r"Filament: ([^.]+)\.", text)
            if fil:
                card.material = fil.group(1).strip()
            card.add_note("preview", text)
            card.status = "Taking a look at the model."
            return
        if first.startswith("Print started"):
            title = first.split(":", 1)[1].strip() if ":" in first else ""
            if title:
                card.set_title(title)
            slot = re.search(r"^Slot \d+: (.+)$", text, re.M)
            if slot:
                card.material = slot.group(1).strip()
            est = re.search(r"^Estimated total: (.+)$", text, re.M)
            if est:
                card.estimate = est.group(1).strip()
            card.state = "printing"
            card.status = first
            card.question = None
            card.question_cid = None
            card.add_note("started", text)
            return
        if first.startswith("Print complete"):
            card.state = "done"
            card.status = first
            card.add_note("complete", text)
            return
        if first.startswith(("Cancelled", "Canceled")):
            card.state = "cancelled"
            card.status = first
            card.add_note(_note_key(text), text)
            return
        if any(mark in low for mark in _FAILURE_MARKS) and not low.startswith(_PROGRESS_HEADS):
            card.state = "failed"
            card.status = first
            if _is_detail(text):
                await self._post_detail(chat_id, metadata, _note_key(text), text)
            else:
                card.add_note(_note_key(text), text)
            return
        if low.startswith(_PROGRESS_HEADS):
            card.state = _state_for_progress(text)
            card.status = first
            return
        # A summary or a warning: keyed, so a repeat replaces its copy.
        key = _note_key(text)
        if _is_detail(text):
            await self._post_detail(chat_id, metadata, key, text)
        else:
            card.add_note(key, text)

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        labels = [str(c) for c in (choices or [])]
        self._sync_closed_prompt()
        if labels == ["Stop"]:
            # A progress prompt: its text is the card's status line and
            # Stop rides on the card, not on a message of its own.
            attach_stop = getattr(self._adapter, "attach_card_stop", None)
            if attach_stop is not None and await self._ensure_card(chat_id, metadata):
                self.card.status = _first_line(question)
                try:
                    result = await attach_stop(self.card, question, clarify_id, metadata=metadata)
                    if result is not None and getattr(result, "success", False):
                        return SendResult(True, message_id=self.card.message_id)
                except Exception:
                    logger.debug("card Stop attach failed; falling back to a prompt", exc_info=True)
            return await self._adapter.send_clarify(chat_id, question, choices, clarify_id,
                                                    session_key, metadata=metadata)
        attach = getattr(self._adapter, "attach_card_prompt", None)
        if attach is None or not await self._ensure_card(chat_id, metadata):
            return await self._adapter.send_clarify(chat_id, question, choices, clarify_id,
                                                    session_key, metadata=metadata)
        card = self.card
        card.state = "ready" if str(question).startswith("Ready to slice") else "asking"
        if str(question).startswith("Ready to slice"):
            rest = str(question).split("·", 1)
            if len(rest) == 2 and rest[1].strip():
                card.material = rest[1].strip()
        card.question = str(question)
        card.question_cid = str(clarify_id)
        try:
            result = await attach(chat_id, card, question, labels, clarify_id, session_key,
                                  metadata=metadata)
        except Exception:
            logger.debug("card prompt attach failed; falling back to a prompt", exc_info=True)
            result = None
        if result is not None and getattr(result, "success", False):
            return result
        card.question = None
        card.question_cid = None
        return await self._adapter.send_clarify(chat_id, question, choices, clarify_id,
                                                session_key, metadata=metadata)

    async def edit_message(self, chat_id, message_id, content, metadata=None):
        if self._card_posted and self.card.message_id and str(message_id) == str(self.card.message_id):
            self.card.status = _first_line(content)
            if await self._update_card():
                return SendResult(True, message_id=str(message_id))
        return await self._adapter.edit_message(chat_id, message_id, content, metadata=metadata)

    async def close_prompt(self, clarify_id, note):
        await self._adapter.close_prompt(clarify_id, note)
        if self.card.question_cid == str(clarify_id):
            self.card.question = None
            self.card.question_cid = None

    # ---------------------------------------------------------- Stop sender support

    async def attach_stop(self, text, clarify_id):
        """Attach a Stop button to the card for the handlers' STOP_SENDER.

        Returns a :class:`CardStopHandle`, or None when the card cannot
        take the button (the caller then posts its standalone message).
        """
        source = self._source
        chat_id = str(getattr(source, "chat_id", "") or "")
        if not chat_id or not await self._ensure_card(chat_id):
            return None
        attach_stop = getattr(self._adapter, "attach_card_stop", None)
        if attach_stop is None:
            return None
        self.card.status = _first_line(text)
        try:
            result = await attach_stop(self.card, text, clarify_id, metadata=None)
        except Exception:
            logger.debug("card Stop attach failed", exc_info=True)
            return None
        if result is not None and getattr(result, "success", False):
            return CardStopHandle(self)
        return None
