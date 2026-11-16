"""Invocation provenance for the conversational AI gate (#1524).

A loose ``mentioned`` boolean cannot tell a real platform mention from a
typed name, a quoted mention, a mention of a different bot, or a Web UI
Ask AI click. Platform adapters therefore stamp every message with one
provenance value, set where the platform evidence exists and carried
unchanged through ``BotCore.handle_message``, ``WebChatService``,
``make_event``, ``process`` and ``fallback``. One policy table (data, in
this module) decides which provenance values unlock the conversational
lane on which platform. A mention never overrides a deterministic match
and never authorizes a mutation: it only makes the model lane eligible
as a fallback, subject to the operator's chat-LLM configuration.
"""
from __future__ import annotations

from dataclasses import dataclass

# The provenance values. ``none`` is the default for every message that
# carries no trusted invocation evidence.
PLATFORM_ENTITY = "platform_entity"    # structured mention of this bot (Discord markup, Matrix pill / m.mentions)
COMMAND_SUFFIX = "command_suffix"      # Telegram /cmd@thisbot
REPLY_TO_BOT = "reply_to_bot"          # a reply whose parent is one of the bot's messages
TYPED_AT_NAME = "typed_at_name"        # "@Print Handy ..." typed as text at the start
TYPED_NAME = "typed_name"              # "Print Handy, ..." typed as text at the start
TYPED_AI_PHRASE = "typed_ai_phrase"    # "ask ai: ...", "ai: ...", "hey ai ..."
UI_FLAG = "ui_flag"                    # Web UI Ask AI control, set server-side
NONE = "none"

ALL = (PLATFORM_ENTITY, COMMAND_SUFFIX, REPLY_TO_BOT, TYPED_AT_NAME,
       TYPED_NAME, TYPED_AI_PHRASE, UI_FLAG, NONE)

# Owner decisions (catduck, 2026-11-16):
# * a reply to one of the bot's messages does NOT by itself count as
#   addressing it, so REPLY_TO_BOT unlocks nothing anywhere;
# * typed "ask ai:" stays an explicit invocation only in direct messages
#   and the Web UI, never in shared channels.
# The table is keyed by (platform, channel kind): "shared" is a guild /
# group / room channel, "dm" is a one-to-one conversation, "webui" is the
# authenticated Web UI chat. Unknown platforms get the shared table.
POLICY = {
    ("discord", "shared"): frozenset({PLATFORM_ENTITY}),
    ("discord", "dm"): frozenset({PLATFORM_ENTITY, TYPED_AT_NAME, TYPED_AI_PHRASE}),
    ("telegram", "shared"): frozenset({PLATFORM_ENTITY, COMMAND_SUFFIX}),
    ("telegram", "dm"): frozenset({PLATFORM_ENTITY, COMMAND_SUFFIX, TYPED_AT_NAME, TYPED_AI_PHRASE}),
    ("matrix", "shared"): frozenset({PLATFORM_ENTITY}),
    ("matrix", "dm"): frozenset({PLATFORM_ENTITY, TYPED_AT_NAME, TYPED_AI_PHRASE}),
    ("webui", "webui"): frozenset({UI_FLAG, TYPED_AI_PHRASE, TYPED_AT_NAME, PLATFORM_ENTITY}),
}
_DEFAULT_POLICY = frozenset({PLATFORM_ENTITY, COMMAND_SUFFIX})


@dataclass(frozen=True)
class Provenance:
    """One message's trusted invocation provenance, carried unchanged."""

    value: str = NONE
    platform: str = ""
    channel_kind: str = "shared"  # "shared" | "dm" | "webui"
    edited: bool = False

    def __post_init__(self):
        if self.value not in ALL:
            object.__setattr__(self, "value", NONE)

    def unlocks_conversation(self, *, ai_disabled_channels=()):
        """Whether this provenance may open the conversational lane.

        Edited messages never re-trigger the lane, and a per-channel off
        switch (channel ids in ``ai_disabled_channels`` are checked by the
        caller through :meth:`unlocks_for_channel`) wins over the table.
        """
        if self.edited or self.value == NONE:
            return False
        allowed = POLICY.get((self.platform, self.channel_kind), _DEFAULT_POLICY)
        return self.value in allowed

    def unlocks_for_channel(self, channel_id, *, ai_disabled_channels=()):
        if str(channel_id) in {str(c) for c in (ai_disabled_channels or ())}:
            return False
        return self.unlocks_conversation()


def for_platform(platform, value, *, channel_kind="shared", edited=False):
    return Provenance(value=value, platform=str(platform or ""),
                      channel_kind=str(channel_kind or "shared"), edited=bool(edited))


def from_event(event, platform=""):
    """The provenance stamped on an event, or NONE when it carries none.

    Events built before this gate existed (direct ``process`` calls in
    older flows) carry no stamp; they read as ``none`` so no model lane
    opens for them by accident.
    """
    metadata = getattr(event, "metadata", None) or {}
    raw = metadata.get("provenance") if isinstance(metadata, dict) else None
    if isinstance(raw, Provenance):
        return raw
    if isinstance(raw, dict):
        return Provenance(value=str(raw.get("value") or NONE),
                          platform=str(raw.get("platform") or platform or ""),
                          channel_kind=str(raw.get("channel_kind") or "shared"),
                          edited=bool(raw.get("edited")))
    if isinstance(raw, str) and raw in ALL:
        return Provenance(value=raw, platform=str(platform or ""))
    return Provenance(value=NONE, platform=str(platform or ""))


def stamp(metadata, provenance):
    """A metadata dict carrying ``provenance`` unchanged."""
    out = dict(metadata or {})
    out["provenance"] = provenance
    return out
