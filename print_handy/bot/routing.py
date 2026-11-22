"""Channel → printer routing for the standalone bot (#335; shared channels #536).

One bot serves every configured printer. Each roster entry claims one chat
on the platform the bot runs: the entry's own ``chat_id`` when it sets one,
else the platform's home chat (``[discord] chat_id``, ``[telegram] chat_id``
or ``[matrix] room_id``). The claim model:

- A chat claimed by exactly one entry routes to that printer: a message or
  interaction in it is handled for that printer, decided before any
  handler runs.
- A chat claimed by two or more entries is a *shared* channel (#536). It
  is served, but the channel decides nothing: printer-bound handlers ask
  which printer (or resolve the printer from the job they act on), and a
  shared channel never falls through to the default printer.
- A chat nobody claimed is dropped silently, exactly as a foreign channel
  is dropped today; with no roster at all, the legacy single configured
  chat routes to the default printer.
"""
from __future__ import annotations


def home_chat_id(cfg, platform_name):
    """The platform's global chat: where the default printer is reached when its entry sets no chat_id."""
    if not isinstance(cfg, dict):
        return ""
    if platform_name == "matrix":
        return str((cfg.get("matrix") or {}).get("room_id") or "")
    section = cfg.get(platform_name)
    if not isinstance(section, dict):
        return ""
    return str(section.get("chat_id") or "")


def roster(cfg):
    """The [[printers]] entries of a settings-shaped dict (empty when it carries none)."""
    if not isinstance(cfg, dict):
        return []
    raw = cfg.get("printers")
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict) and entry.get("id")]


def channel_claims(cfg, platform_name):
    """{chat_id: [printer ids in roster order]} for one platform (#536).

    An entry with no claimable chat (no chat_id of its own and no platform
    home chat configured) is simply unreachable by chat and claims nothing.
    Claims by several entries on the same chat are legal: they make the
    chat shared.
    """
    entries = roster(cfg)
    if not entries:
        return {}
    home = home_chat_id(cfg, platform_name)
    claims: dict[str, list[str]] = {}
    for entry in entries:
        claim = str(entry.get("chat_id") or "") or home
        if not claim:
            continue
        claims.setdefault(claim, []).append(str(entry["id"]))
    return claims


def channel_map(cfg, platform_name):
    """{chat_id: printer_id} for chats claimed by exactly one entry.

    {} when the config names no roster. Shared chats (2+ claims) are
    absent — see :func:`shared_channel_map`; a chat claimed once routes
    to its printer exactly as it always has.
    """
    return {chat: ids[0] for chat, ids in channel_claims(cfg, platform_name).items() if len(ids) == 1}


def shared_channel_map(cfg, platform_name):
    """{chat_id: [printer ids in roster order]} for shared chats (#536).

    A shared chat is claimed by two or more roster entries. Nothing in it
    is decided by the channel; handlers resolve the printer by asking or
    from the job they act on.
    """
    return {chat: ids for chat, ids in channel_claims(cfg, platform_name).items() if len(ids) > 1}


def printer_for_chat(cfg, platform_name, chat_id):
    """The roster printer id a chat routes to, or None.

    None means either "unmapped" or "shared chat — undecided"; callers
    that must tell those apart use :func:`shared_channel_map`.
    """
    return channel_map(cfg, platform_name).get(str(chat_id))


def chat_for_printer(cfg, platform_name, printer_id):
    """The chat a printer is reached in on this platform (its claim, shared or not), or None."""
    for chat, ids in channel_claims(cfg, platform_name).items():
        if str(printer_id) in ids:
            return chat
    return None


def default_printer_id(cfg):
    """The roster's first entry id (the default printer), or None without a roster."""
    entries = roster(cfg)
    return str(entries[0]["id"]) if entries else None


def printer_name(cfg, printer_id):
    """An entry's display name for user-facing text; the id itself when unknown."""
    for entry in roster(cfg):
        if str(entry["id"]) == str(printer_id):
            return str(entry.get("name") or entry["id"])
    return str(printer_id)
