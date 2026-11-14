"""A fake discord.py client for the standalone bot: channels, messages, buttons, clicks, attachments.

The standalone adapter builds real discord.py View, Button and File objects and hands them to a channel.
Here the channel records what was sent, and a FakeDiscord can press buttons the way a person would:
by dispatching an interaction to the client's on_interaction listeners. Nothing contacts Discord.
"""
from __future__ import annotations

import asyncio
import itertools
import shutil
from pathlib import Path
from types import SimpleNamespace as S

_ids = itertools.count(900000000000000001)


def next_id():
    return next(_ids)


class FakeUser:
    def __init__(self, user_id, name="operator", bot=False):
        self.id = int(user_id)
        self.name = self.display_name = name
        self.bot = bot

    def __str__(self):
        return self.name


class FakeMessage:
    def __init__(self, channel, author, content=None, view=None, files=(), attachments=(), reference=None):
        self.id = next_id()
        self.channel = channel
        self.author = author
        self.content = content or ""
        self.view = view
        self.files = list(files)
        self.attachments = list(attachments)
        self.reference = reference
        self.edits = []

    def buttons(self):
        return [(item.label, item.custom_id) for item in (self.view.children if self.view else [])
                if getattr(item, "label", None) is not None]

    def selects(self):
        """(placeholder, custom_id, [option labels]) per select menu in the view (#1473)."""
        found = []
        for item in (self.view.children if self.view else []):
            options = getattr(item, "options", None)
            if options:
                found.append((getattr(item, "placeholder", None) or "", item.custom_id,
                              [str(option.label) for option in options]))
        return found

    async def edit(self, content=None, view=None, **kwargs):
        self.edits.append({"content": content, "view": view})
        if content is not None:
            self.content = content
        self.view = view


class FakeChannel:
    def __init__(self, discord, channel_id, parent_id=None):
        self.discord = discord
        self.id = int(channel_id)
        if parent_id is not None:
            self.parent_id = int(parent_id)

    async def send(self, content=None, *, view=None, files=None, file=None, reference=None,
                   allowed_mentions=None, **kwargs):
        names = [f.filename for f in (files or ([file] if file else []))]
        message = FakeMessage(self, self.discord.client.user, content, view, names, reference=reference)
        self.discord.on_send(message)
        return message


class FakeAttachment:
    def __init__(self, path, filename=None):
        self.path = Path(path)
        self.filename = filename or self.path.name
        self.size = self.path.stat().st_size

    async def save(self, target, **kwargs):
        shutil.copyfile(self.path, target)
        return self.size


class _Response:
    def __init__(self, interaction):
        self.interaction = interaction
        self.done = False

    def is_done(self):
        return self.done

    async def send_message(self, content=None, *, ephemeral=False, **kwargs):
        self.done = True
        self.interaction.replies.append({"content": content, "ephemeral": ephemeral})

    async def edit_message(self, *, content=None, view=None, **kwargs):
        self.done = True
        if self.interaction.message is not None:
            await self.interaction.message.edit(content=content, view=view)

    async def send_modal(self, modal):
        self.done = True
        self.interaction.replies.append({"modal": modal})

    async def defer(self, **kwargs):
        self.done = True
        self.interaction.acks.append(("defer", kwargs))


class FakeInteraction:
    def __init__(self, discord, user, *, custom_id=None, message=None, channel=None, values=None):
        self.id = next_id()
        self.user = user
        self.message = message
        self.channel = channel or (message.channel if message else None)
        self.channel_id = getattr(self.channel, "id", None)
        self.guild = None
        self.data = {"custom_id": custom_id} if custom_id else {}
        if values is not None:
            self.data["values"] = list(values)
        self.replies = []
        self.acks = []  # deferred acknowledgements, in order
        self.response = _Response(self)
        self.followup = S(send=self._followup)

    async def _followup(self, content=None, **kwargs):
        self.replies.append({"content": content, "followup": True})

    async def edit_original_response(self, *, content=None, view=None, **kwargs):
        if self.message is not None:
            await self.message.edit(content=content, view=view)


class FakeClient:
    def __init__(self, discord, user):
        self.discord = discord
        self.user = user
        self.channels = {}
        self.listeners = {}

    def get_channel(self, channel_id):
        return self.channels.get(int(channel_id))

    async def fetch_channel(self, channel_id):
        channel = self.get_channel(channel_id)
        if channel is None:
            raise LookupError(f"unknown channel {channel_id}")
        return channel

    def add_listener(self, func, name=None):
        self.listeners.setdefault(name or func.__name__, []).append(func)

    async def dispatch_interaction(self, interaction):
        for listener in list(self.listeners.get("on_interaction", [])):
            await listener(interaction)


class UnansweredQuestion(AssertionError):
    """A button question no click rule answers."""


class FakeDiscord:
    """One guild: a print channel, a bot user, an operator, and an automatic button presser.

    ``clicks`` are corpus rules: {"question": text in the question, "button": text in a label}. A question
    with prompt buttons and no matching rule is recorded in ``unanswered`` and cancelled, so the flow ends
    instead of waiting out its timeout. Stop-only progress buttons are never pressed. ``auto=False``
    presses nothing. A batch message's select menus (#1473) are answered by the same rules, matched
    against each menu's placeholder question; an unmatched menu is recorded in ``unanswered`` and left
    unanswered, since a select has no Cancel.
    """

    def __init__(self, clicks=(), *, chat_id=100000000000000001, operator_id=200000000000000002,
                 events=None, auto=True):
        self.bot_user = FakeUser(300000000000000003, "Print Handy", bot=True)
        self.operator = FakeUser(operator_id, "Mrs catduck")
        self.client = FakeClient(self, self.bot_user)
        self.channel = self.add_channel(chat_id)
        self.clicks = list(clicks)
        self.events = events if events is not None else []
        self.auto = auto
        self.messages = []
        self.questions = []   # {'question', 'choices', 'picked'}, like the Hermes FakeAdapter
        self.sent = []        # plain message texts
        self.errors = []
        self.unanswered = []
        self.pending = set()

    def add_channel(self, channel_id, parent_id=None):
        channel = FakeChannel(self, channel_id, parent_id)
        self.client.channels[channel.id] = channel
        return channel

    def message(self, content="", attachments=(), author=None, channel=None):
        return FakeMessage(channel or self.channel, author or self.operator, content, attachments=attachments)

    def prompt_buttons(self, message):
        return [(label, cid) for label, cid in message.buttons() if str(cid).startswith("pbq:")]

    def on_send(self, message):
        self.messages.append(message)
        buttons = self.prompt_buttons(message)
        if not buttons:
            selects = message.selects()
            if not selects:
                if not message.view:
                    self.sent.append(message.content)
                return
            # A batch question (#1473): one select menu per color group in a
            # single message. Each menu is answered by the same click rules,
            # matched against the menu's own question (its placeholder).
            self.events.append(("prompt", message.content))
            for placeholder, custom_id, labels in selects:
                if not self.auto:
                    self.questions.append({"question": placeholder, "choices": labels, "picked": None})
                    continue
                try:
                    index = self.pick(placeholder, labels)
                except UnansweredQuestion as exc:
                    # No Cancel exists on a select; leave it unanswered so the
                    # flow expires and the test reports the recorded question.
                    self.unanswered.append(str(exc))
                    continue
                self.questions.append({"question": placeholder, "choices": labels,
                                       "picked": labels[index]})
                self.press_soon(message, custom_id, values=[labels[index]])
            return
        labels = [label for label, _ in buttons]
        self.events.append(("prompt", message.content))
        if not self.auto:
            self.questions.append({"question": message.content, "choices": labels, "picked": None})
            return
        try:
            index = self.pick(message.content, labels)
        except UnansweredQuestion as exc:
            # Press Cancel so the flow ends instead of waiting out the question timeout; the test reports it.
            self.unanswered.append(str(exc))
            index = next((i for i, label in enumerate(labels) if "cancel" in label.casefold()), None)
            if index is None:
                raise
        self.questions.append({"question": message.content, "choices": labels, "picked": labels[index]})
        self.press_soon(message, buttons[index][1])

    def pick(self, question, labels):
        for rule in self.clicks:
            if str(rule["question"]).casefold() not in str(question).casefold():
                continue
            want = str(rule["button"]).casefold()
            for index, label in enumerate(labels):
                if want in label.casefold():
                    return index
        raise UnansweredQuestion("no [[click]] rule answers this question; add one to case.toml\n"
                                 f"question: {question}\nbuttons: {labels}")

    def press_soon(self, message, custom_id, user=None, values=None):
        """Click after the send returns, as Discord would: the adapter must already be waiting."""
        task = asyncio.get_running_loop().create_task(self.press(message, custom_id, user, values=values))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def press(self, message, custom_id, user=None, values=None):
        interaction = FakeInteraction(self, user or self.operator, custom_id=custom_id,
                                      message=message, values=values)
        try:
            await self.client.dispatch_interaction(interaction)
        except Exception as exc:  # surfaced by the test, never swallowed silently
            self.errors.append(exc)
        return interaction

    def last_question(self):
        return next((m for m in reversed(self.messages) if self.prompt_buttons(m)), None)
