"""Discord delivery for the standalone bot: the Discord implementation of the platform interface.

``send`` and ``send_clarify`` carry the signatures the print-feedback
handlers call, so the handlers run unchanged. A ``MEDIA:/path`` marker becomes an attachment. A button press resolves the
core prompt registry (print_handy.core.prompts); the click handler lives here too. The shared
interface and prompt bookkeeping live in ``print_handy.bot.platform`` (#164); this module is the
only adapter module that imports ``discord``.
"""
from __future__ import annotations

import logging
from typing import Any

from ..core import prompts
from . import rest
from .platform import (CUSTOM_PREFIX, BasePlatformAdapter, SendResult, _prompts_guard, custom_id,
                       parse_custom_id)

__all__ = ["CUSTOM_PREFIX", "MAX_BUTTONS", "LABEL_LIMIT", "SendResult", "StandaloneAdapter",
           "custom_id", "parse_custom_id"]

logger = logging.getLogger(__name__)

MAX_BUTTONS = 25
LABEL_LIMIT = 80


def library_status():
    """(label, available, version-or-detail) for discord.py, reported to the offline check."""
    try:
        import discord
    except ImportError:
        return ("discord.py", False, "not installed: pip install . (or the print-handy package)")
    return ("discord.py", True, discord.__version__)


class StandaloneAdapter(BasePlatformAdapter):
    """Sends through a discord.py client. ``allowed`` holds the Discord user IDs that may act.

    Discord is a channel-boundary platform (#386): with no allow list
    configured, anyone who can post in the print channel may use the bot;
    ``allowed``/``denied`` are opt-in lists on top of that default.

    Discord keeps ``uses_llm = True`` (it is the one platform whose chat
    may use the LLM); the operator's per-install choice is the
    ``[discord] chat_llm_enabled`` setting BotCore applies (#466), off by
    default since #1376.
    """

    name = "discord"
    message_limit = rest.LIMIT
    supports_buttons = True
    max_buttons = MAX_BUTTONS
    supports_live_stream = True
    channel_is_boundary = True
    # One job card per job, edited in place (#1472): Discord is the one
    # adapter with embeds, message editing and threads, so it is the one
    # that presents a job as a card. The card session lives in
    # print_handy.bot.job_card; the methods below are its rendering arms.
    supports_job_card = True
    supports_embeds = True

    def __init__(self, client, allowed, *, denied=(), approver=None, approve_all=False, registry=prompts,
                 upload_max_bytes=None, job_card_thread=False):
        super().__init__(allowed, denied=denied, approver=approver, approve_all=approve_all, registry=registry)
        self.client = client
        # [discord] upload_max_bytes when BotCore builds this adapter; None
        # means the caller sizes uploads from settings itself.
        self.upload_max_bytes = upload_max_bytes
        # [discord] job_card_thread (#1472): the card session opens a
        # thread on the card for long detail when this is on.
        self.job_card_thread = bool(job_card_thread)
        self._prompts: dict[str, dict[str, Any]] = {}
        self._card_messages: dict[str, Any] = {}
        self._card_views: dict[str, Any] = {}

    # ------------------------------------------------------------------ delivery

    async def _channel(self, chat_id, metadata=None):
        target = (metadata or {}).get("thread_id") or chat_id
        channel = self.client.get_channel(int(target))
        if channel is None:
            channel = await self.client.fetch_channel(int(target))
        return channel

    async def send(self, chat_id, content, reply_to=None, metadata=None, files=None):
        import discord
        body, paths = rest.outgoing(content, files)
        pieces = rest.chunks(body)
        first = None
        opened = []
        try:
            channel = await self._channel(chat_id, metadata)
            for index, piece in enumerate(pieces):
                kwargs = {"allowed_mentions": discord.AllowedMentions.none()}
                if index == 0 and reply_to and str(reply_to).isdigit():
                    kwargs["reference"] = discord.MessageReference(
                        message_id=int(reply_to), channel_id=channel.id, fail_if_not_exists=False)
                if index == len(pieces) - 1 and paths:
                    opened = [discord.File(str(path)) for path in paths]
                    kwargs["files"] = opened
                sent = await channel.send(piece or None, **kwargs)
                first = first or sent
        except Exception as exc:
            logger.warning("Discord send failed: %r", exc)
            return SendResult(False, error=str(exc))
        finally:
            for handle in opened:
                handle.close()
        return SendResult(True, message_id=str(first.id) if first is not None else None)

    async def signal_working(self, chat_id, metadata=None):
        """One Discord typing indicator (shown about 10 s, or until the reply)."""
        channel = await self._channel(chat_id, metadata)
        await channel.typing()

    async def delete_message(self, chat_id, message_id, metadata=None):
        try:
            channel = await self._channel(chat_id, metadata)
            await channel.get_partial_message(int(message_id)).delete()
            return True
        except Exception as exc:
            logger.info("Discord delete failed: %r", exc)
            return False

    async def edit_message(self, chat_id, message_id, content, metadata=None):
        """Replace a posted message's text in place (progress updates, P2-236).

        Without this, intake progress (FitStatus) could only post a new
        message per phase on the production adapters, though the intake
        already knew how to edit when an adapter offers it.
        """
        try:
            channel = await self._channel(chat_id, metadata)
            await channel.get_partial_message(int(message_id)).edit(
                content=str(content)[:rest.LIMIT])
        except Exception as exc:
            logger.info("Discord edit failed: %r", exc)
            return SendResult(False, error=str(exc))
        return SendResult(True, message_id=str(message_id))

    async def pin_message(self, chat_id, message_id):
        """Pin a message this adapter sent (#1020 B3: the budget-exhausted
        outage escalation). Optional capability: the outage path checks
        for it with getattr and carries on without it elsewhere."""
        channel = await self._channel(chat_id)
        message = await channel.fetch_message(int(message_id))
        await message.pin()
        return True

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        """One message with one button per choice. A press resolves ``clarify_id`` in the registry."""
        import discord
        labels = [str(c) for c in choices][:MAX_BUTTONS]
        pieces = rest.chunks(question)
        try:
            channel = await self._channel(chat_id, metadata)
            for piece in pieces[:-1]:
                await channel.send(piece, allowed_mentions=discord.AllowedMentions.none())
            view = discord.ui.View(timeout=None)
            for index, label in enumerate(labels):
                style = (discord.ButtonStyle.secondary if _is_cancel(label)
                         else discord.ButtonStyle.primary)
                view.add_item(discord.ui.Button(label=label[:LABEL_LIMIT], style=style,
                                                custom_id=custom_id(clarify_id, index)))
            message = await channel.send(pieces[-1], view=view, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            logger.warning("Discord button question failed: %r", exc)
            return SendResult(False, error=str(exc))
        self._record_prompt(clarify_id, {"message": message, "choices": labels, "question": pieces[-1]})
        return SendResult(True, message_id=str(message.id))

    # ------------------------------------------------------------------ job card (#1472)

    def _card_embed(self, card):
        """A discord.Embed for a job card's current spec (bot/job_card.py)."""
        import discord
        spec = card.embed_spec()
        embed = discord.Embed(title=spec["title"], description=spec["description"] or None,
                              color=discord.Color(spec["color"]))
        for field in spec["fields"]:
            embed.add_field(name=field["name"], value=field["value"], inline=field["inline"])
        if spec.get("thumbnail"):
            embed.set_thumbnail(url=spec["thumbnail"])
        embed.set_footer(text=spec["footer"])
        return embed

    def _card_files(self, card):
        """[discord.File] carrying the card's thumbnail once, else [].

        The embed's thumbnail points at attachment://<name>, so the file
        must ride the message that shows it; after that first ride the
        attachment stays on the message and later edits send nothing.
        """
        import discord
        path = getattr(card, "thumbnail_path", None)
        if not path or getattr(card, "_thumbnail_attached", False):
            return []
        try:
            handle = discord.File(str(path))
        except Exception:
            logger.debug("job card thumbnail unreadable", exc_info=True)
            return []
        card._thumbnail_attached = True
        return [handle]

    async def post_card(self, chat_id, card, metadata=None):
        """Post the job card: one embed message the whole job edits."""
        import discord
        try:
            channel = await self._channel(chat_id, metadata)
            files = self._card_files(card)
            kwargs = {"embed": self._card_embed(card),
                      "allowed_mentions": discord.AllowedMentions.none()}
            if files:
                kwargs["files"] = files
            message = await channel.send(None, **kwargs)
        except Exception as exc:
            logger.warning("Discord job card post failed: %r", exc)
            return SendResult(False, error=str(exc))
        card.message_id = str(message.id)
        self._card_messages[str(message.id)] = message
        self._card_views[str(message.id)] = None
        return SendResult(True, message_id=str(message.id))

    async def update_card(self, card, view=None, clear_view=False, metadata=None):
        """Re-render a posted card in place, keeping its current buttons
        unless ``view`` replaces them or ``clear_view`` retires them."""
        message = self._card_messages.get(str(getattr(card, "message_id", "") or ""))
        if message is None:
            return SendResult(False, error="job card message is not tracked")
        mid = str(message.id)
        if view is not None:
            self._card_views[mid] = view
        elif clear_view:
            self._card_views[mid] = None
        try:
            kwargs = {"embed": self._card_embed(card), "view": self._card_views.get(mid)}
            files = self._card_files(card)
            if files:
                kwargs["attachments"] = files
            await message.edit(**kwargs)
        except Exception as exc:
            logger.info("Discord job card edit failed: %r", exc)
            return SendResult(False, error=str(exc))
        return SendResult(True, message_id=mid)

    async def attach_card_prompt(self, chat_id, card, question, choices, clarify_id, session_key,
                                 metadata=None):
        """Put a question's buttons on the card itself (#1472).

        The prompt row records the card, so a press (handle_click) and a
        typed answer (close_prompt) collapse the question into a line on
        the card instead of editing a standalone message. The buttons use
        the same pbq: custom ids as send_clarify, so every access rule in
        handle_click applies unchanged.
        """
        import discord
        message = self._card_messages.get(str(getattr(card, "message_id", "") or ""))
        if message is None:
            return SendResult(False, error="job card message is not tracked")
        labels = [str(c) for c in choices][:MAX_BUTTONS]
        view = discord.ui.View(timeout=None)
        for index, label in enumerate(labels):
            style = (discord.ButtonStyle.secondary if _is_cancel(label)
                     else discord.ButtonStyle.primary)
            view.add_item(discord.ui.Button(label=label[:LABEL_LIMIT], style=style,
                                            custom_id=custom_id(clarify_id, index)))
        self._card_views[str(message.id)] = view
        try:
            await message.edit(embed=self._card_embed(card), view=view)
        except Exception as exc:
            logger.warning("Discord card prompt attach failed: %r", exc)
            return SendResult(False, error=str(exc))
        self._record_prompt(clarify_id, {"message": message, "choices": labels,
                                         "question": str(question), "card": card})
        return SendResult(True, message_id=str(message.id))

    async def attach_card_stop(self, card, text, clarify_id, metadata=None):
        """Put the one Stop button on the card for a progress step (#1472).

        Same custom id the standalone Stop prompt uses (prepared_intake's
        STOP_CUSTOM_ID), so the handlers' stop listener resolves it the
        same way; the button retires when the step ends (retire_prompt or
        the stop handle's edit).
        """
        import discord
        from ..core import prepared_intake
        message = self._card_messages.get(str(getattr(card, "message_id", "") or ""))
        if message is None:
            return SendResult(False, error="job card message is not tracked")
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label=prepared_intake.STOP_LABEL,
                                        style=discord.ButtonStyle.danger,
                                        custom_id=f"{prepared_intake.STOP_CUSTOM_ID}{clarify_id}"))
        self._card_views[str(message.id)] = view
        try:
            await message.edit(embed=self._card_embed(card), view=view)
        except Exception as exc:
            logger.warning("Discord card Stop attach failed: %r", exc)
            return SendResult(False, error=str(exc))
        # Recorded like every other prompt, so retire_prompt finds it when the
        # step ends and takes the Stop button off the card (review of #1488:
        # unrecorded, the button stayed on the card after the step).
        self._record_prompt(clarify_id, {"message": message, "choices": [prepared_intake.STOP_LABEL],
                                         "question": str(text), "card": card, "stop": True})
        return SendResult(True, message_id=str(message.id))

    async def open_card_thread(self, chat_id, card, metadata=None):
        """Open a thread on the card for long detail (#1472). Returns its
        id, or None when Discord will not open one here."""
        message = self._card_messages.get(str(getattr(card, "message_id", "") or ""))
        if message is None:
            return None
        try:
            thread = await message.create_thread(name=str(card.title)[:100])
        except Exception:
            logger.debug("job card thread open failed", exc_info=True)
            return None
        return str(getattr(thread, "id", "") or "") or None

    async def retire_prompt(self, clarify_id):
        """Retire a prompt's buttons without an answer (prepared_intake's
        Stop flow): the card re-renders bare, a standalone message just
        loses its view."""
        with _prompts_guard(self):
            row = self._prompts.pop(str(clarify_id), None)
        if not row:
            return
        message = row.get("message")
        if message is None:
            return
        self._card_views[str(getattr(message, "id", ""))] = None
        try:
            if row.get("card") is not None:
                await message.edit(embed=self._card_embed(row["card"]), view=None)
            else:
                await message.edit(view=None)
        except Exception:
            logger.debug("could not retire prompt buttons", exc_info=True)

    # ------------------------------------------------------------------ answers

    async def close_prompt(self, cid, note):
        with _prompts_guard(self):
            row = self._prompts.pop(str(cid), None)
        if not row:
            return
        if row.get("card") is not None:
            # A card prompt collapses into a line on the card (#1472), the
            # generalization of the standalone edit below.
            card = row["card"]
            if not row.get("stop"):  # a progress step's Stop is not a question to record
                card.add_answered(row["question"], note)
            self._card_views[str(getattr(row["message"], "id", ""))] = None
            try:
                await row["message"].edit(embed=self._card_embed(card), view=None)
            except Exception:
                logger.debug("could not collapse answered card prompt", exc_info=True)
            return
        try:
            await row["message"].edit(content=f"{row['question']}\n> {note}"[:rest.LIMIT], view=None)
        except Exception:
            logger.debug("could not strip answered buttons", exc_info=True)

    # The pre-interface name for closing a prompt's buttons.
    _close_buttons = close_prompt

    async def handle_click(self, interaction):
        """Resolve a prompt button. Returns False when the interaction is not one of ours."""
        data = getattr(interaction, "data", None) or {}
        parsed = parse_custom_id(data.get("custom_id"))
        if parsed is None:
            return False
        cid, index = parsed
        user = getattr(interaction, "user", None)
        if not self._is_allowed_user(str(getattr(user, "id", "")), user):
            # Never silent (#386): the presser gets the ephemeral reply and
            # the log names who was refused and why.
            logger.info("refused button press from Discord user %s: %s",
                        getattr(user, "id", ""), self.refusal_reason(getattr(user, "id", "")))
            await interaction.response.send_message("Not allowed. Nothing changed.", ephemeral=True)
            return True
        if prompts.is_approval(cid) and not self.may_approve_id(getattr(user, "id", "")):
            # Print approvals stay separate from open channel access (P1-93):
            # anyone in the channel may chat, but only the approval user
            # resolves a prompt whose answer starts a print.
            logger.info("refused approval button press from Discord user %s: %s",
                        getattr(user, "id", ""), self.approval_refusal_reason(getattr(user, "id", "")))
            await interaction.response.send_message("Not allowed. Nothing changed.", ephemeral=True)
            return True
        if not prompts.is_approval(cid) and not self.may_answer_prompt(getattr(user, "id", ""), cid):
            # The same initiator rule as every other platform (#1146): a prompt
            # belongs to the person who was asked, or an approver (#1373 review:
            # Discord clicks skipped it, so anyone could answer anyone's prompt).
            logger.info("refused button press from Discord user %s: prompt %s belongs to its "
                        "initiator or an approver", getattr(user, "id", ""), cid)
            await interaction.response.send_message(
                "That question was asked of someone else. Nothing changed.", ephemeral=True)
            return True
        with _prompts_guard(self):
            row = self._prompts.get(cid)
        if row is not None:
            # A prompt is answered in the channel it was asked in (#335): the
            # channel is the approval's target, so a press arriving through a
            # different channel never resolves another channel's question.
            asked_channel = getattr(getattr(row.get("message"), "channel", None), "id", None)
            press_channel = getattr(interaction, "channel_id", None)
            if asked_channel is not None and press_channel is not None \
                    and str(asked_channel) != str(press_channel):
                await interaction.response.send_message("Not allowed. Nothing changed.", ephemeral=True)
                return True
        labels = row["choices"] if row else (self.registry.choices(cid) or [])
        if not 0 <= index < len(labels):
            await interaction.response.send_message("That question already closed. Nothing changed.",
                                                    ephemeral=True)
            return True
        # Acknowledge before resolving: resolving wakes the waiting flow (slice start, file reads),
        # which can hold the event loop past Discord's 3 s interaction window, and the presser then
        # sees "This interaction failed" with nothing logged. A deferred update claims nothing yet.
        try:
            await interaction.response.defer()
        except Exception:
            logger.warning("could not acknowledge prompt click", exc_info=True)
        if not self.registry.resolve_gateway_clarify(cid, labels[index]):
            try:
                await interaction.followup.send("That question already closed. Nothing changed.", ephemeral=True)
            except Exception:
                logger.debug("could not report a closed question", exc_info=True)
            return True
        who = getattr(user, "display_name", None) or getattr(user, "name", None) or "Someone"
        question = row["question"] if row else str(getattr(getattr(interaction, "message", None), "content", "") or "")
        with _prompts_guard(self):
            self._prompts.pop(cid, None)
        if row is not None and row.get("card") is not None:
            # A press on the job card (#1472): every check above (allowed,
            # approver, initiator, channel) already ran exactly as for a
            # standalone prompt. The answer collapses into a line on the
            # card and the card re-renders without its buttons.
            card = row["card"]
            card.add_answered(question, f"{who} chose **{labels[index]}**")
            self._card_views[str(getattr(row["message"], "id", ""))] = None
            try:
                await interaction.edit_original_response(embed=self._card_embed(card), view=None)
            except Exception:
                logger.debug("could not record the choice on the job card", exc_info=True)
            return True
        try:
            await interaction.edit_original_response(
                content=f"{question}\n> {who} chose **{labels[index]}**"[:rest.LIMIT], view=None)
        except Exception:
            logger.debug("could not record the choice on the prompt", exc_info=True)
        return True


def _is_cancel(label):
    low = label.casefold()
    return any(word in low for word in ("cancel", "don't", "keep", "skip", "not "))
