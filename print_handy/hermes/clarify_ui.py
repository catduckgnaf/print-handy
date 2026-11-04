"""Plugin-owned Discord clarify rendering (#524, stage 2): the question half of the
plugin's own interaction stack, next to ``print_alert_buttons``.

Under Hermes, handler questions are rendered by Hermes' Discord adapter out of
Hermes' clarify registry; the standalone bot renders the same questions itself
(``print_handy.bot.adapter``). This module is Print Handy's renderer for the
Hermes side, built on the alert buttons' proven shape: the question is posted
by direct Discord REST with the bot token (``print_alert_buttons``' payload
shape), buttons carry the standalone ``pbq:<clarify_id>:<index>`` ids, clicks
resolve Print Handy's own prompt registry (``print_handy.core.prompts``), and
an answered question is edited in place by REST PATCH.

WIRED BEHIND A SWITCH. The S5 wiring is live: the plugin's ``register()``
activates Print Handy's prompt registry, ``alert_button_handlers`` installs
this adapter, and the handlers' ``_adapter_for`` substitution routes
``send_clarify`` here — all exactly when the operator turns on the
``[discord] clarify_ui`` setting on a Hermes install. With the setting off
(the default) nothing constructs this adapter in production and Hermes'
adapter renders questions exactly as before.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import urllib.request

from ..bot import rest
from ..bot.platform import SendResult, answered_note, custom_id, parse_custom_id
from ..core import prompts
from . import _config

__all__ = ["HermesClarifyAdapter", "MAX_BUTTONS", "LABEL_LIMIT", "parse_custom_id"]

logger = logging.getLogger(__name__)

# The standalone adapter's limits (print_handy.bot.adapter): one renderer,
# one set of rules, in both modes.
MAX_BUTTONS = 25
LABEL_LIMIT = 80
# print_alert_buttons' agent string: these posts ride the same REST path.
_USER_AGENT = "DiscordBot (hermes-3dprinting, 1.0)"


def _is_cancel(label):
    """Mirror of the standalone adapter's button-styling rule (bot/adapter.py)."""
    low = str(label).casefold()
    return any(word in low for word in ("cancel", "don't", "keep", "skip", "not "))


def _components(clarify_id, labels):
    """Button rows for the choices: at most 5 per row (Discord's limit),
    standalone styles (cancel-like secondary, the rest primary) and ids."""
    rows = []
    for start in range(0, len(labels), 5):
        rows.append({"type": 1, "components": [
            {"type": 2, "style": 2 if _is_cancel(label) else 1,
             "label": label[:LABEL_LIMIT], "custom_id": custom_id(clarify_id, start + offset)}
            for offset, label in enumerate(labels[start:start + 5])]})
    return rows


class HermesClarifyAdapter:
    """``send_clarify`` by Discord REST, with the standalone adapter's rules.

    The surface is what the print-feedback handlers call on the host adapter
    (``send_clarify``) plus the resolution side the plugin's own listener
    needs (``handle_click``, ``answer_typed``, ``close_prompt``) — the same
    trio the standalone adapter implements. ``send`` is deliberately absent:
    plain posts keep travelling through the host adapter and the listener
    paths; this adapter owns questions only.

    Bookkeeping is in-process: unlike the listener's alerts, the poster and
    the click handler are the same gateway process here. The row shape
    (``message_id``, ``channel_id``, ``question``, ``choices``) is kept
    file-portable in case that ever changes.
    """

    name = "discord"
    supports_buttons = True
    max_buttons = MAX_BUTTONS

    def __init__(self, token=None, *, registry=prompts, opener=None):
        # token=None resolves per request through the profile env, the same
        # source the listener's own posts use (_config.discord_token).
        self._token = token
        self.registry = registry
        self._opener = opener
        self._prompts: dict[str, dict] = {}

    # ------------------------------------------------------------------ REST

    def _request(self, method, path, payload):
        token = self._token if self._token is not None else _config.discord_token()
        if not token:
            raise ValueError("no Discord bot token")
        req = urllib.request.Request(
            rest.API + path, data=json.dumps(payload).encode(), method=method,
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                     "User-Agent": _USER_AGENT})
        with (self._opener or urllib.request.urlopen)(req, timeout=15) as resp:
            body = resp.read()
        return json.loads(body) if body else {}

    def _post(self, channel_id, payload):
        return self._request("POST", f"/channels/{channel_id}/messages", payload)

    def _edit(self, channel_id, message_id, content):
        """Retire one question message in place: new text, buttons stripped. False on failure."""
        try:
            self._request("PATCH", f"/channels/{channel_id}/messages/{message_id}",
                          {"content": str(content)[:rest.LIMIT], "components": [],
                           "allowed_mentions": {"parse": []}})
        except Exception:
            logger.debug("could not edit clarify message", exc_info=True)
            return False
        return True

    # ------------------------------------------------------------------ bookkeeping

    def _record_prompt(self, clarify_id, row):
        self._prompts[str(clarify_id)] = row
        self._forget_closed()

    def _forget_closed(self):
        for cid in [c for c in self._prompts if not self.registry.is_open(c)]:
            self._prompts.pop(cid, None)

    # ------------------------------------------------------------------ delivery

    def _send_clarify_sync(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        labels = [str(c) for c in choices][:MAX_BUTTONS]
        target = str((metadata or {}).get("thread_id") or chat_id)
        pieces = rest.chunks(question)
        try:
            for piece in pieces[:-1]:
                self._post(target, {"content": piece, "allowed_mentions": {"parse": []}})
            sent = self._post(target, {"content": pieces[-1],
                                       "components": _components(clarify_id, labels),
                                       "allowed_mentions": {"parse": []}})
        except Exception as exc:
            logger.warning("Discord button question failed: %r", exc)
            return SendResult(False, error=str(exc))
        message_id = str(sent.get("id") or "") or None
        self._record_prompt(clarify_id, {"message_id": message_id, "channel_id": target,
                                         "choices": labels, "question": pieces[-1]})
        return SendResult(True, message_id=message_id)

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        """One message with one button per choice. A press resolves ``clarify_id`` in the registry."""
        return await asyncio.to_thread(self._send_clarify_sync, chat_id, question, choices,
                                       clarify_id, session_key, metadata)

    # ------------------------------------------------------------------ answers

    async def close_prompt(self, clarify_id, note):
        """Edit the question so it stops offering its choices (typed answer, timeout)."""
        row = self._prompts.pop(str(clarify_id), None)
        if not row or not row.get("message_id"):
            return
        await asyncio.to_thread(self._edit, row["channel_id"], row["message_id"],
                                f"{row['question']}\n> {note}")

    async def retire_prompt(self, clarify_id):
        """Strip a still-open prompt's buttons, question text unchanged. True when retired.

        The completion half of the Stop retirement (#524 stage 2, S6): a
        later step (prepared_intake's Stop) superseded this question, and
        on the clarify path no second message may be posted to say so, so
        the question simply stops being answerable — the same end state a
        click produces, minus the answer line close_prompt adds.
        """
        row = self._prompts.pop(str(clarify_id), None)
        if not row or not row.get("message_id"):
            return False
        await asyncio.to_thread(self._edit, row["channel_id"], row["message_id"],
                                row["question"])
        return True

    def _open_prompt(self, session_key):
        if hasattr(self.registry, "open_prompt_scoped"):
            return self.registry.open_prompt_scoped(session_key)
        return self.registry.open_prompt_for(session_key)

    def typed_approval_blocked(self, session_key, user_id):
        """True when ``user_id`` typing would answer a print approval they may not give.

        The typed twin of the approval button check (P1-93, 2.2.5 review):
        with ``[discord] approval_user_id`` set (one ID or several), only those
        users' replies resolve a prompt marked as an approval; the prompt stays
        open. ``approval_all_users`` lifts the restriction.
        """
        from .. import settings as _settings
        discord = _config.cfg().get("discord") or {}
        approvers = _settings.approver_ids(discord)
        if (user_id is None or not approvers or discord.get("approval_all_users") is True
                or str(user_id) in approvers):
            return False
        cid = self._open_prompt(session_key)
        if cid is None or not getattr(self.registry, "is_approval", prompts.is_approval)(cid):
            return False
        logger.info("refused typed approval from Discord user %s: only an approval user may approve prints",
                    user_id)
        return True

    async def answer_typed(self, session_key, text, user_id=None):
        """A typed reply to this session's newest open question, resolved in the own registry.

        The twin of ``BasePlatformAdapter.answer_typed``: it asks the
        registry for the session's newest live-key prompt
        (``prompts.open_prompt_for``) because the plugin holds the registry,
        not an adapter's session rows. ``user_id`` is the sender: a print
        approval answers only to the approval user (2.2.5 review of P1-93).
        """
        if self.typed_approval_blocked(session_key, user_id):
            return False
        cid = self._open_prompt(session_key)
        if cid is None or not self.registry.resolve_gateway_clarify(cid, str(text)):
            return False
        await self.close_prompt(cid, answered_note(text))
        return True

    async def handle_click(self, interaction, clarify_id, index):
        """Resolve one ``pbq:`` press. Returns True: the press was ours.

        The plugin's listener runs its authorisation closure (the alert
        families' ``allowed()``) before calling this; what remains is the
        standalone ``handle_click`` re-expressed for REST: the channel-match
        guard (#335), registry resolution, then the in-place ack by PATCH —
        the interaction itself is only deferred, so the edited message is
        the acknowledgement, exactly as a typed answer's edit is.
        """
        cid = str(clarify_id)
        row = self._prompts.get(cid)
        if row is not None:
            # A prompt is answered in the channel it was asked in (#335): a
            # press arriving through a different channel never resolves it.
            press_channel = getattr(interaction, "channel_id", None)
            if press_channel is not None and str(row["channel_id"]) != str(press_channel):
                await interaction.response.send_message("Not allowed. Nothing changed.", ephemeral=True)
                return True
        labels = row["choices"] if row else (self.registry.choices(cid) or [])
        if not 0 <= index < len(labels) or not self.registry.resolve_gateway_clarify(cid, labels[index]):
            await interaction.response.send_message("That question already closed. Nothing changed.",
                                                    ephemeral=True)
            return True
        user = getattr(interaction, "user", None)
        who = getattr(user, "display_name", None) or getattr(user, "name", None) or "Someone"
        message = getattr(interaction, "message", None)
        question = row["question"] if row else str(getattr(message, "content", "") or "")
        channel_id = row["channel_id"] if row else str(getattr(interaction, "channel_id", "") or "")
        message_id = (row or {}).get("message_id") or str(getattr(message, "id", "") or "")
        self._prompts.pop(cid, None)
        with contextlib.suppress(Exception):
            # discord.py has no defer_update(); defer() is the supported
            # acknowledgement and answers a component interaction with a
            # deferred update, so the edited message stays the ack (P2-207).
            await interaction.response.defer()
        if channel_id and message_id:
            await asyncio.to_thread(self._edit, channel_id, message_id,
                                    f"{question}\n> {who} chose **{labels[index]}**")
        return True
