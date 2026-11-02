"""Discord delivery for the standalone bot: the Discord implementation of the platform interface.

``send`` and ``send_clarify`` have the signatures of Hermes' Discord adapter, so the print-feedback
handlers run unchanged. A ``MEDIA:/path`` marker becomes an attachment. A button press resolves the
core prompt registry (print_handy.core.prompts); the click handler lives here too. The shared
interface and prompt bookkeeping live in ``print_handy.bot.platform`` (#164); this module is the
only adapter module that imports ``discord``.
"""
from __future__ import annotations

import logging
from typing import Any

from ..core import prompts
from . import rest
from .platform import (CUSTOM_PREFIX, BasePlatformAdapter, SendResult, custom_id, parse_custom_id)

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
        return ("discord.py", False, "not installed: pip install -r requirements.txt")
    return ("discord.py", True, discord.__version__)


class StandaloneAdapter(BasePlatformAdapter):
    """Sends through a discord.py client. ``allowed`` holds the Discord user IDs that may act.

    Discord is a channel-boundary platform (#386): with no allow list
    configured, anyone who can post in the print channel may use the bot;
    ``allowed``/``denied`` are opt-in lists on top of that default.

    Discord keeps ``uses_llm = True`` (it is the one platform whose chat
    may use the LLM); the operator's per-install choice is the
    ``[discord] chat_llm_enabled`` setting BotCore applies (#466).
    """

    name = "discord"
    message_limit = rest.LIMIT
    supports_buttons = True
    max_buttons = MAX_BUTTONS
    supports_live_stream = True
    channel_is_boundary = True

    def __init__(self, client, allowed, *, denied=(), approver=None, registry=prompts,
                 upload_max_bytes=None):
        super().__init__(allowed, denied=denied, approver=approver, registry=registry)
        self.client = client
        # [discord] upload_max_bytes when BotCore builds this adapter; None
        # means the caller sizes uploads from settings itself.
        self.upload_max_bytes = upload_max_bytes
        self._prompts: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ delivery

    async def _channel(self, chat_id, metadata=None):
        target = (metadata or {}).get("thread_id") or chat_id
        channel = self.client.get_channel(int(target))
        if channel is None:
            channel = await self.client.fetch_channel(int(target))
        return channel

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        import discord
        body, paths = rest.split_media(content)
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

    async def delete_message(self, chat_id, message_id, metadata=None):
        try:
            channel = await self._channel(chat_id, metadata)
            await channel.get_partial_message(int(message_id)).delete()
            return True
        except Exception as exc:
            logger.info("Discord delete failed: %r", exc)
            return False

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

    # ------------------------------------------------------------------ answers

    async def close_prompt(self, cid, note):
        row = self._prompts.pop(str(cid), None)
        if not row:
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
        self._prompts.pop(cid, None)
        try:
            await interaction.edit_original_response(
                content=f"{question}\n> {who} chose **{labels[index]}**"[:rest.LIMIT], view=None)
        except Exception:
            logger.debug("could not record the choice on the prompt", exc_info=True)
        return True


def _is_cancel(label):
    low = label.casefold()
    return any(word in low for word in ("cancel", "don't", "keep", "skip", "not "))
