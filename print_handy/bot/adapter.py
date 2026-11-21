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
from . import prompt_ledger, rest
from .prompt_ledger import saved_job_state, stale_notice
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

    Discord is a pure bot (#1731): ``uses_llm = False``, so no Discord
    message ever reaches a model call — commands, handlers and the
    deterministic plain-language layer answer, and unmatched text gets
    the short not-understood reply. The conversational model lane lives
    only in Web Chat, behind [webui] chat_llm_enabled.
    """

    name = "discord"
    uses_llm = False
    message_limit = rest.LIMIT
    supports_buttons = True
    max_buttons = MAX_BUTTONS
    supports_live_stream = True
    channel_is_boundary = True

    def __init__(self, client, allowed, *, denied=(), approver=None, approve_all=False, registry=prompts,
                 upload_max_bytes=None):
        super().__init__(allowed, denied=denied, approver=approver, approve_all=approve_all, registry=registry)
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

    async def send_followup(self, interaction, content, files=None, view=None):
        """Deliver one handler answer as a slash interaction's own response (#1475).

        The interaction was deferred by ``BotCore.run_slash``; its followup
        is the one visible response, so the answer lands there instead of as
        a separate channel post. ``view`` attaches only to the first piece
        (the /print-help command menu). A ``MEDIA:/path`` marker becomes an
        attachment, exactly as in ``send``.
        """
        import discord
        body, paths = rest.outgoing(content, files)
        pieces = rest.chunks(body)
        first = None
        opened = []
        try:
            for index, piece in enumerate(pieces):
                kwargs = {"allowed_mentions": discord.AllowedMentions.none()}
                if index == 0 and view is not None:
                    kwargs["view"] = view
                if index == len(pieces) - 1 and paths:
                    opened = [discord.File(str(path)) for path in paths]
                    kwargs["files"] = opened
                sent = await interaction.followup.send(piece or None, **kwargs)
                first = first or sent
        except Exception as exc:
            logger.warning("Discord slash followup failed: %r", exc)
            return SendResult(False, error=str(exc))
        finally:
            for handle in opened:
                handle.close()
        return SendResult(True, message_id=str(first.id) if getattr(first, "id", None) else None)

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
        self._ledger_prompt(clarify_id, message, session_key)
        return SendResult(True, message_id=str(message.id))

    def _ledger_prompt(self, clarify_id, message, session_key=None):
        """Note the posted question on disk so a button that outlives a restart is recognized."""
        key = str(session_key or "")
        prompt_ledger.record(
            clarify_id, channel_id=getattr(getattr(message, "channel", None), "id", None),
            message_id=getattr(message, "id", None), approval=prompts.is_approval(clarify_id),
            initiator=self._prompt_initiator(clarify_id),
            intake_id=key[len("model-link:"):] if key.startswith("model-link:") else None)

    async def send_clarify_batch(self, chat_id, question, groups, session_key, metadata=None):
        """One message with one select menu per group (#1473), five rows to a message.

        ``groups`` is [{label, choices, clarify_id}]; each select resolves its
        own clarify_id in the registry with the chosen label, so the waiting
        flow collects one answer per group from a single message instead of
        one message per part. Groups past the fifth row spill to a following
        message. A message's selects stay live until every one is answered;
        only then is the message edited to show the picks."""
        import discord
        labels_by_cid = {}
        try:
            channel = await self._channel(chat_id, metadata)
            pieces = rest.chunks(question)
            for piece in pieces[:-1]:
                await channel.send(piece, allowed_mentions=discord.AllowedMentions.none())
            first_id = None
            for start in range(0, len(groups), 5):
                chunk = groups[start:start + 5]
                view = discord.ui.View(timeout=None)
                batch = {"pending": set(), "answers": {}, "labels": {}}
                for group in chunk:
                    labels = [str(c)[:100] for c in group["choices"]][:25]
                    labels_by_cid[group["clarify_id"]] = labels
                    batch["pending"].add(group["clarify_id"])
                    batch["labels"][group["clarify_id"]] = str(group["label"])
                    view.add_item(discord.ui.Select(
                        placeholder=str(group.get("question") or group["label"])[:150],
                        options=[discord.SelectOption(label=label, value=label) for label in labels],
                        custom_id=custom_id(group["clarify_id"], 0)))
                message = await channel.send(pieces[-1], view=view,
                                             allowed_mentions=discord.AllowedMentions.none())
                first_id = first_id or str(message.id)
                for group in chunk:
                    self._record_prompt(group["clarify_id"],
                                        {"message": message, "choices": labels_by_cid[group["clarify_id"]],
                                         "question": pieces[-1], "batch": batch,
                                         "group_label": str(group["label"])})
                    self._ledger_prompt(group["clarify_id"], message, session_key)
            return SendResult(True, message_id=first_id)
        except Exception as exc:
            logger.warning("Discord batch question failed: %r", exc)
            return SendResult(False, error=str(exc))

    async def retire_prompt(self, clarify_id):
        """Retire a prompt's buttons without an answer (prepared_intake's
        Stop flow): the message loses its view."""
        prompt_ledger.forget(clarify_id)
        with _prompts_guard(self):
            row = self._prompts.pop(str(clarify_id), None)
        if not row:
            return
        message = row.get("message")
        if message is None:
            return
        try:
            await message.edit(view=None)
        except Exception:
            logger.debug("could not retire prompt buttons", exc_info=True)

    # ------------------------------------------------------------------ answers

    async def close_prompt(self, cid, note):
        prompt_ledger.forget(cid)
        with _prompts_guard(self):
            row = self._prompts.pop(str(cid), None)
        if not row:
            return
        batch = row.get("batch")
        if batch is not None and batch["pending"] - {str(cid)}:
            # One select of a batch message closed; the others stay live.
            batch["pending"].discard(str(cid))
            return
        try:
            await row["message"].edit(content=f"{row['question']}\n> {note}"[:rest.LIMIT], view=None)
        except Exception:
            logger.debug("could not strip answered buttons", exc_info=True)

    # The pre-interface name for closing a prompt's buttons.
    _close_buttons = close_prompt

    async def _report_stale(self, interaction, cid):
        """A press on a button whose question is no longer open (answered, timed out, or posted
        before a restart). Nothing is resolved, restored, replayed or started.

        The ledger (``bot.prompt_ledger``) holds only the identity this needs. A message this
        install posted gets its dead buttons removed and a plain explanation; anything else
        keeps the short generic reply. The same checks as a live press apply first: the
        channel the question was asked in, the approver for a print approval, and the
        person who was asked for an ordinary question.
        """
        async def say(text):
            if interaction.response.is_done():  # the press was already acknowledged (a lost race)
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)

        user = getattr(interaction, "user", None)
        uid = str(getattr(user, "id", "") or "")
        entry = prompt_ledger.lookup(cid)
        message = getattr(interaction, "message", None)
        known = (entry is not None
                 and (not entry.get("message_id") or message is None
                      or str(getattr(message, "id", "")) == entry["message_id"]))
        if not known:
            await say("That question already closed. Nothing changed.")
            return
        press_channel = getattr(interaction, "channel_id", None)
        if entry.get("channel_id") and press_channel is not None \
                and str(press_channel) != entry["channel_id"]:
            await say("Not allowed. Nothing changed.")
            return
        if entry.get("approval"):
            if not self.authorize_press(interaction, "approver"):
                logger.info("refused stale approval button press from Discord user %s: %s", uid,
                            self.approval_refusal_reason(uid))
                await say("Not allowed. Nothing changed.")
                return
        elif entry.get("initiator") and uid != entry["initiator"] \
                and not (uid and uid not in self.denied and uid in self.approvers):
            await say("That question was asked of someone else. Nothing changed.")
            return
        restarted = entry.get("boot") != prompt_ledger.BOOT_ID
        with _prompts_guard(self):
            row = self._prompts.get(cid)
            batch = row.get("batch") if row else None
            if batch is not None:
                batch["pending"].discard(cid)
                preserve_siblings = bool(batch["pending"])
            else:
                preserve_siblings = False
            self._prompts.pop(cid, None)
        prompt_ledger.forget(cid)
        try:
            # A batched select message is shared: removing its view while another
            # select is live would strand that sibling question.
            if message is not None and not preserve_siblings:
                await message.edit(view=None)
        except Exception:
            logger.debug("could not retire the stale buttons", exc_info=True)
        await say(stale_notice(restarted, entry.get("approval"),
                               saved_job_state(entry.get("intake_id"))))

    async def handle_click(self, interaction):
        """Resolve a prompt button. Returns False when the interaction is not one of ours."""
        data = getattr(interaction, "data", None) or {}
        parsed = parse_custom_id(data.get("custom_id"))
        if parsed is None:
            return False
        cid, index = parsed
        # A select menu (#1473 batch questions) answers with the chosen value,
        # not a button index: one message can carry several selects, each with
        # its own clarify_id.
        values = data.get("values") if isinstance(data, dict) else None
        is_select = isinstance(values, list) and bool(values)
        select_answer = str(values[0]) if is_select else None
        user = getattr(interaction, "user", None)
        if not self.authorize_press(interaction, "allowed"):
            # Never silent (#386): the presser gets the ephemeral reply and
            # the log names who was refused and why.
            logger.info("refused button press from Discord user %s: %s",
                        getattr(user, "id", ""), self.refusal_reason(getattr(user, "id", "")))
            await interaction.response.send_message("Not allowed. Nothing changed.", ephemeral=True)
            return True
        if not self.registry.is_open(cid):
            # Nothing live behind this button: answered, timed out, or posted before a
            # restart. Report which, retire the dead buttons when this install posted
            # them, and never resolve, restore or replay anything.
            await self._report_stale(interaction, cid)
            return True
        if prompts.is_approval(cid) and not self.authorize_press(interaction, "approver"):
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
        if is_select:
            if select_answer not in labels:
                await interaction.response.send_message("That question already closed. Nothing changed.",
                                                        ephemeral=True)
                return True
        elif not 0 <= index < len(labels):
            await interaction.response.send_message("That question already closed. Nothing changed.",
                                                    ephemeral=True)
            return True
        answer = select_answer if is_select else labels[index]
        # Acknowledge before resolving: resolving wakes the waiting flow (slice start, file reads),
        # which can hold the event loop past Discord's 3 s interaction window, and the presser then
        # sees "This interaction failed" with nothing logged. A deferred update claims nothing yet.
        try:
            await interaction.response.defer()
        except Exception:
            logger.warning("could not acknowledge prompt click", exc_info=True)
        if not self.registry.resolve_gateway_clarify(cid, answer):
            # Lost a race: another press answered, or the question timed out, between the
            # open check and now. The same stale report applies; nothing was resolved.
            try:
                await self._report_stale(interaction, cid)
            except Exception:
                logger.debug("could not report a closed question", exc_info=True)
            return True
        prompt_ledger.forget(cid)
        who = getattr(user, "display_name", None) or getattr(user, "name", None) or "Someone"
        question = row["question"] if row else str(getattr(getattr(interaction, "message", None), "content", "") or "")
        with _prompts_guard(self):
            self._prompts.pop(cid, None)
        batch = row.get("batch") if row else None
        if is_select and batch is not None:
            batch["answers"][cid] = answer
            batch["pending"].discard(cid)
            if batch["pending"]:
                # Other selects on this message are still open: leave the
                # message and its remaining menus untouched.
                return True
            picked = "\n".join(f"> {batch['labels'][c]}: **{batch['answers'][c]}**"
                               for c in batch["labels"] if c in batch["answers"])
            try:
                await interaction.edit_original_response(
                    content=f"{question}\n{picked}"[:rest.LIMIT], view=None)
            except Exception:
                logger.debug("could not record the batch choices on the prompt", exc_info=True)
            return True
        try:
            await interaction.edit_original_response(
                content=f"{question}\n> {who} chose **{answer}**"[:rest.LIMIT], view=None)
        except Exception:
            logger.debug("could not record the choice on the prompt", exc_info=True)
        return True


def _is_cancel(label):
    low = label.casefold()
    return any(word in low for word in ("cancel", "don't", "keep", "skip", "not "))
