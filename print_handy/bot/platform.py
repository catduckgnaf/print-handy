"""The chat-platform interface the standalone bot core runs against (#164).

Handlers and ``BotCore`` talk to a platform only through this surface, so a new
chat service is one new implementation, not a fork of the intake, slicing or
print-control code:

* ``send(chat_id, content, reply_to=None, metadata=None)`` — post text. A
  ``MEDIA:/path`` marker in ``content`` becomes a file attachment, the
  convention every caller already uses.
* ``send_clarify(chat_id, question, choices, clarify_id, session_key,
  metadata=None)`` — ask a question with choices. Platforms with buttons
  render them; platforms without fall back to a numbered list that the
  typed-answer path resolves.
* ``answer_typed(session_key, text, user_id=None)`` — a typed reply to the open question
  of that session.
* ``close_prompt(clarify_id, note)`` — edit/close a prompt once answered.
* ``is_allowed_id(user_id)`` — may this user act at all?

Prompt bookkeeping is shared: choices resolve through the platform-neutral
registry in ``print_handy.core.prompts`` under ``pbq:<clarify_id>:<index>``
ids, so no platform code touches intake state directly.

Per-platform limits live on the adapter as class attributes: ``name``,
``message_limit``, ``upload_max_bytes``, ``supports_buttons``,
``max_buttons`` and ``supports_live_stream``. Callers that chunk text or
size an upload read them from the platform instead of hard-coding one
service's numbers.

Discord is the first implementation (``print_handy.bot.adapter``),
Telegram the second (``print_handy.bot.telegram``) and Matrix the third
(``print_handy.bot.matrix``: numbered-list prompts, no buttons, pure bot).

Adding a platform: implement the methods above in a new module under
``print_handy/bot/`` (its adapter class is named ``StandaloneAdapter``,
like the Discord one), give it a client that turns service events into
``runtime.make_source`` / ``runtime.make_event`` values and button presses
(or typed answers) into registry resolutions, register its name in
``KNOWN_PLATFORMS`` and its module in ``platform_module``, add its
settings section, and drive it with a fake in ``tests/bot/`` — no handler
or core change should be needed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..core import prompts

logger = logging.getLogger(__name__)

CUSTOM_PREFIX = "pbq:"

# Every platform name a config may name. Implementations register below as
# their stage of #164 lands; a known-but-unimplemented name is a clear
# settings/startup error, never a silent Discord fallback.
KNOWN_PLATFORMS = ("discord", "telegram", "matrix")


@dataclass
class SendResult:
    success: bool
    message_id: str | None = None
    error: str | None = None


def custom_id(clarify_id, index):
    return f"{CUSTOM_PREFIX}{clarify_id}:{index}"


def parse_custom_id(value):
    """(clarify_id, index) for a prompt button, else None."""
    value = str(value or "")
    if not value.startswith(CUSTOM_PREFIX):
        return None
    cid, _, index = value[len(CUSTOM_PREFIX):].rpartition(":")
    if not cid or not index.isdigit():
        return None
    return cid, int(index)


def answered_note(text):
    """The "Answered by message" line left on a prompt a typed reply closed.

    A long unbroken string may be a pasted login token, so it is never echoed back.
    """
    shown = str(text)[:100]
    if len(shown) >= 20 and not any(c.isspace() for c in shown):
        return "Answered by message."
    return f"Answered by message: {shown}"


def describe_access(allowed_count, denied_count, channel_open):
    """The one-line access mode every surface reports (#386).

    ``channel_open`` means an empty allow list lets everyone in the print
    channel act (the channel is the boundary); otherwise an empty list
    refuses everyone. A deny list subtracts in every mode and is named so
    a startup log never hides that someone is excluded.
    """
    if channel_open:
        # Latched open wins over the count: the allowed set may still hold
        # the approval user, who is unioned in for action in every mode.
        text = "channel (everyone)"
    elif allowed_count:
        text = f"whitelist ({allowed_count} user{'s' if allowed_count != 1 else ''})"
    else:
        text = "none (no users allowed)"
    if denied_count:
        text += f", {denied_count} denied"
    return text


def platform_module(name):
    """The module implementing platform ``name``, or None when its stage has not landed."""
    if name == "discord":
        from . import adapter
        return adapter
    if name == "telegram":
        from . import telegram
        return telegram
    if name == "matrix":
        from . import matrix
        return matrix
    return None


def library_status(name):
    """(label, available, version-or-detail) for the platform's client library.

    Reported by the offline ``check`` command so the check itself never has
    to import a platform library directly. Stdlib-only platforms report
    themselves available with no library row needed.
    """
    if name == "discord":
        return platform_module(name).library_status()
    module = platform_module(name)
    if module is None:
        return (str(name), False, f"platform {name!r} is not implemented yet (#164)")
    return (str(name), True, "standard library client")


@runtime_checkable
class ChatPlatform(Protocol):
    """The operations the bot core needs from a chat service. See the module docstring."""

    name: str
    message_limit: int
    upload_max_bytes: int | None
    supports_buttons: bool
    max_buttons: int
    supports_live_stream: bool
    polls_serially: bool

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult: ...
    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key,
                           metadata=None) -> SendResult: ...
    async def answer_typed(self, session_key, text, user_id=None) -> bool: ...
    async def close_prompt(self, clarify_id, note) -> None: ...
    def is_allowed_id(self, user_id) -> bool: ...


class BasePlatformAdapter:
    """Shared prompt and allow-list behaviour for platform adapters.

    A concrete adapter only delivers: ``send``, ``send_clarify`` (recording
    the prompt row with ``_record_prompt``) and ``close_prompt`` (editing
    the service-side message so an answered question stops offering its
    choices). Who may act, which prompt is open for a session, typed
    answers and choice resolution behave the same on every platform.
    """

    name = "platform"
    message_limit = 2000
    upload_max_bytes: int | None = None
    supports_buttons = False
    max_buttons = 0
    # Pure-bot platforms set this False: BotCore then never calls the LLM
    # fallback for them, even when [llm] is configured (#164 stage 2, #73).
    uses_llm = True
    # Live camera viewing (#311) is a Discord Activity: only the Discord
    # adapter reports True. Other platforms expose no /print-live command,
    # no stub and no config surface for it.
    supports_live_stream = False
    # The client drives messages from a serial polling loop (Telegram
    # getUpdates, Matrix /sync): each update is awaited before the next is
    # fetched, so a long wait inside message handling starves the loop that
    # must deliver the answer to an open prompt. BotCore reads this to run
    # the did-you-mean prompt's wait in a background task (P1-109).
    # Event-driven clients (Discord) leave it False.
    polls_serially = False
    # Access model (#386): on a platform whose adapter sets this, the print
    # channel is the access boundary — with no allow list configured, anyone
    # who can post in the channel may use the bot, and the allow/deny lists
    # are opt-in refinements. Platforms that leave it False keep the strict
    # list-only rule (an empty list refuses everyone).
    channel_is_boundary = False

    def __init__(self, allowed, *, denied=(), approver=None, approve_all=False, registry=prompts):
        # The mode latches on the raw allow list BEFORE the approver is
        # unioned in: an operator who sets only an approval user has not
        # opted into a whitelist, so channel mode must still apply (#386).
        # Latched at construction, too: a runtime mutation of ``allowed``
        # (tests do this) must not silently flip the mode either way.
        self.channel_open = bool(self.channel_is_boundary and not {str(u) for u in allowed})
        # The approver is ALSO kept apart from the allowed set (P1-93): being
        # allowed to act and being allowed to approve a print are separate
        # checks, and an open channel must not blur them.
        # ``approver`` is one ID or several (an iterable); ``approve_all``
        # lifts the approver restriction so anyone allowed to act may approve.
        if isinstance(approver, (str, int)):
            approver = [approver]
        self.approvers = {str(a) for a in (approver or ()) if str(a)}
        self.approve_all = bool(approve_all)
        self.allowed = {str(u) for u in allowed}
        self.allowed.update(self.approvers)
        self.denied = {str(u) for u in denied}
        self.registry = registry
        self._prompts: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ who may act

    def is_allowed_id(self, user_id):
        """May this user act? Deny beats everything; an open channel admits
        everyone else; a closed one admits only the allowed set (which
        includes the approver) — an empty set there refuses everyone."""
        uid = str(user_id or "")
        if not uid or uid in self.denied:
            return False
        if self.channel_open:
            return True
        return uid in self.allowed

    def refusal_reason(self, user_id):
        """Why ``user_id`` may not act, in log words, or None when they may."""
        uid = str(user_id or "")
        if uid and uid in self.denied:
            return "user is on the denied list"
        if not self.is_allowed_id(uid):
            return "user is not on the allowed list"
        return None

    def may_approve_id(self, user_id):
        """May this user approve a print (P1-93)? Separate from acting: when
        approval users are configured, only they approve — however open the
        channel is for intake — unless ``approve_all`` is on, which lets anyone
        allowed to act approve. With none configured, approval follows the
        general check, as before. Deny beats everything, as everywhere."""
        uid = str(user_id or "")
        if not uid or uid in self.denied:
            return False
        if self.approve_all or not self.approvers:
            return self.is_allowed_id(uid)
        return uid in self.approvers

    def approval_refusal_reason(self, user_id):
        """Why ``user_id`` may not approve a print, in log words, or None."""
        uid = str(user_id or "")
        if uid and uid in self.denied:
            return "user is on the denied list"
        if not self.is_allowed_id(uid):
            return "user is not on the allowed list"
        if not self.may_approve_id(uid):
            return "only an approval user may approve prints"
        return None

    def access_description(self):
        """The startup-log line for this adapter's access mode (#386)."""
        return describe_access(len(self.allowed), len(self.denied), self.channel_open)

    def _is_allowed_user(self, uid, user=None, guild=None, channel_ids=None):
        """The check the listener alert buttons call (same name as the Hermes adapter's)."""
        if getattr(user, "bot", False):
            return False
        return self.is_allowed_id(uid)

    # ------------------------------------------------------------------ prompts

    def _record_prompt(self, clarify_id, row):
        self._prompts[str(clarify_id)] = row
        self._forget_closed()

    def _forget_closed(self):
        for cid in [c for c in self._prompts if not self.registry.is_open(c)]:
            self._prompts.pop(cid, None)

    def open_prompt(self, session_key):
        """The newest open button question asked under ``session_key``, else None."""
        # The registry key is the live chat session (who asked, where); send_clarify's key names the flow.
        hits = [cid for cid in self._prompts if self.registry.session_key(cid) == str(session_key)]
        if hits:
            return hits[-1]
        # Shared channels (#536): a decided intake registers its questions
        # under '<session key>:printer:<id>'. A typed reply may follow it
        # only when exactly one printer has open questions there — with two
        # printers waiting, the reply is ambiguous and resolves nothing.
        prefix = f"{session_key}{prompts.PRINTER_KEY_SEP}"
        scoped = [(cid, str(self.registry.session_key(cid))) for cid in self._prompts
                  if str(self.registry.session_key(cid)).startswith(prefix)]
        printers = {key[len(prefix):] for _cid, key in scoped}
        if len(printers) != 1:
            return None
        return scoped[-1][0] if scoped else None

    def typed_approval_blocked(self, session_key, user_id):
        """True when ``user_id`` typing into this session would answer a print
        approval they may not give (2.2.5 review of P1-93): a typed "yes" is
        the same decision as the button, so it takes the same check."""
        if user_id is None:
            return False
        cid = self.open_prompt(session_key)
        if cid is None or not prompts.is_approval(cid) or self.may_approve_id(user_id):
            return False
        logger.info("refused typed approval from %s user %s: %s",
                    self.name, user_id, self.approval_refusal_reason(user_id))
        return True

    async def answer_typed(self, session_key, text, user_id=None):
        """A typed reply to the open question of this session.

        ``user_id`` is the sender; a print approval answers only to the
        approval user, and a refused reply leaves the prompt open.
        """
        if self.typed_approval_blocked(session_key, user_id):
            return False
        cid = self.open_prompt(session_key)
        if cid is None or not self.registry.resolve_gateway_clarify(cid, str(text)):
            return False
        await self.close_prompt(cid, answered_note(text))
        return True

    async def answer_choice(self, clarify_id, index, *, user_id=None, user=None):
        """Resolve prompt ``clarify_id`` with choice ``index``. Returns the label, else None.

        The platform-neutral half of a button press: allow check, label
        lookup and registry resolution. A concrete platform wraps this with
        its own acknowledgement (an interaction reply, a callback answer).
        """
        if user_id is not None and not self._is_allowed_user(str(user_id), user):
            return None
        if user_id is not None and prompts.is_approval(clarify_id) \
                and not self.may_approve_id(user_id):
            # A print approval is not ordinary channel access (P1-93): the
            # press resolves nothing and the prompt stays open for the
            # approval user.
            logger.info("refused approval button press from %s user %s: %s",
                        self.name, user_id, self.approval_refusal_reason(user_id))
            return None
        row = self._prompts.get(str(clarify_id))
        labels = row["choices"] if row else (self.registry.choices(clarify_id) or [])
        if not 0 <= index < len(labels):
            return None
        if not self.registry.resolve_gateway_clarify(clarify_id, labels[index]):
            return None
        return labels[index]

    async def delete_message(self, chat_id, message_id, metadata=None):
        """Delete one chat message (best effort). True when the service accepted it.

        Used to remove a message that carried a pasted login token. The default
        deletes nothing; a platform that can, overrides it. Never raises.
        """
        return False

    async def close_prompt(self, clarify_id, note):  # pragma: no cover - overridden per platform
        """Edit or close the service-side prompt message. Default: nothing to edit."""
        self._prompts.pop(str(clarify_id), None)
