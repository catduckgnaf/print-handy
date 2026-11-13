"""Talking to the bot by mentioning it: "@Print Handy spool list" instead of "/spool list".

Every chat platform hands a message to ``BotCore.handle_message`` (web chat
to ``BotCore.process``) with the mention still in it: Discord's
``<@id>`` markup, Telegram's ``@username`` or ``/cmd@username`` suffix, a
Matrix pill or ``Display Name:`` prefix, or the bot's name typed as plain
text. Every command gate tests the text with ``startswith('/...')``, so
until now a mentioned command matched nothing.

This module is the one step that recognises a mention of *this* bot and
strips it before routing. Who "this bot" is comes from the platform at run
time (the logged-in account's id, username and display names), plus
``bot_name`` when the operator set one. Nothing is hard-coded: renaming the
bot's account is enough for it to answer to the new name and stop
answering to "Print Handy".

``identity_for`` gathers the names and markup for one platform and message;
``address`` applies them to one message's text and returns what to route.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import commands

# The built-in bot_name. It counts as a name the bot answers to only where
# the platform has no account of its own to read (web chat), or before the
# account is known.
DEFAULT_BOT_NAME = "Print Handy"

# What may follow a name typed at the start of a message: "Print Handy, help",
# "Print Handy: help", "@printhandy help". Anything else (a letter, a digit, a
# hyphen as in "print-handy") means the word is not the name.
_AFTER_NAME = r"(?=$|[\s,:;.!?])[\s,:;.!?]*"
_LEADING_PUNCT = re.compile(r"^[\s,:;.!?]+")
_HEAD = re.compile(r"(\S+)(\s*)(.*)", re.S)
_HANDLE = re.compile(r"@(\w+)[,:;.!?]*")
_HELP = "/print-help"


@dataclass(frozen=True)
class Identity:
    """Everything that names this bot on one platform, for one message.

    ``names`` are plain names: the account's username and display names,
    and ``bot_name`` when set. A leading ``@name`` is a mention; a leading
    bare name is the name typed as text. ``tokens`` are markup that is this
    bot wherever it appears (``<@123>``, ``<@&456>``, ``@bot:example.org``).
    ``suffixes`` is a platform where ``/cmd@handle`` addresses one bot
    (Telegram); ``handles`` are this bot's own usernames there, empty while
    unknown. ``flagged`` is the platform saying the message mentions the
    bot (Matrix ``m.mentions``), whether or not its text shows it.
    """

    names: tuple[str, ...] = ()
    tokens: tuple[str, ...] = ()
    handles: tuple[str, ...] = ()
    suffixes: bool = False
    flagged: bool = False


@dataclass(frozen=True)
class Addressed:
    """One message's text after mention handling.

    ``text`` is what to route. ``mentioned`` is an explicit mention of this
    bot (markup, ``@name``, the platform flag, or this bot's command
    suffix); ``named`` is one of its names typed as plain text (no "@") in
    front of a command. ``elsewhere`` is a message meant for a different
    bot (``/cmd@OtherBot``, or "@OtherBot ..." on Telegram), which this bot
    must not run.
    """

    text: str
    mentioned: bool = False
    named: bool = False
    elsewhere: bool = False


def _unique(values):
    seen, out = set(), []
    for value in values:
        value = " ".join(str(value or "").split())
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            out.append(value)
    return tuple(out)


def configured_name(cfg_value):
    """The operator's own ``bot_name``, or None when it is unset or the built-in default."""
    name = " ".join(str(cfg_value or "").split())
    return name if name and name.casefold() != DEFAULT_BOT_NAME.casefold() else None


def account_names(platform, client):
    """The names of the bot's own account, best first; () when the account is unknown."""
    user = getattr(client, "user", None)
    if user is None:
        return ()
    if platform == "discord":
        return _unique([getattr(user, "display_name", None), getattr(user, "global_name", None),
                        getattr(user, "name", None)])
    if platform == "telegram":
        return _unique([getattr(user, "first_name", None), getattr(user, "username", None)])
    if platform == "matrix":
        mxid = str(getattr(user, "id", "") or "")
        localpart = mxid.lstrip("@").split(":", 1)[0] if mxid else None
        return _unique([getattr(client, "display_name", None), localpart])
    return ()


def identity_for(platform, client, bot_name, message=None):
    """The names and markup that are this bot, from the platform's logged-in account.

    ``client.user`` is the bot's own account on every platform client
    (Discord's ``ClientUser``, Telegram's ``getMe``, Matrix ``whoami``).
    ``message`` adds what only the message knows: a Discord guild's
    nickname and managed role, a Matrix pill's link text and
    ``m.mentions`` flag. ``bot_name`` is the operator's own name for the
    bot, or None when unset: then the default name counts only where no
    account name is known (web chat, or before login). Missing pieces are
    skipped, never guessed.
    """
    user = getattr(client, "user", None)
    uid = getattr(user, "id", None)
    names = list(account_names(platform, client))
    tokens, handles = [], []
    if platform == "discord":
        if uid is not None:
            tokens += [f"<@{uid}>", f"<@!{uid}>"]
        guild = getattr(message, "guild", None)
        me = getattr(guild, "me", None)
        names += [getattr(me, "nick", None), getattr(me, "display_name", None)]
        role = getattr(guild, "self_role", None)  # the role Discord manages for this bot, if any
        if getattr(role, "id", None) is not None:
            tokens.append(f"<@&{role.id}>")
    elif platform == "telegram":
        username = getattr(user, "username", None)
        if username:
            handles.append(username)
    elif platform == "matrix":
        if uid:
            tokens.append(str(uid))
        names += list(getattr(message, "mention_names", None) or ())
    if bot_name:
        names.insert(0, bot_name)
    elif not _unique(names):
        names.append(DEFAULT_BOT_NAME)
    return Identity(names=_unique(names), tokens=_unique(tokens), handles=_unique(handles),
                    suffixes=platform == "telegram",
                    flagged=bool(getattr(message, "mentions_bot", False)))


def _name_pattern(name):
    words = [re.escape(word) for word in name.split()]
    return re.compile(r"^\s*(?P<at>@)?" + r"\s+".join(words) + _AFTER_NAME, re.I)


def _strip_tokens(text, tokens):
    found = False
    for token in sorted(tokens, key=len, reverse=True):
        # "@bot:example.org" must not match inside "@bot:example.org.evil",
        # while "@bot:example.org: help" ends the id at the colon. Markup that
        # closes with ">" needs no guard: "<@123>help" is a mention.
        guard = "" if token.endswith(">") else r"(?![\w-]|[.:][\w-])"
        pattern = re.compile(r"[ \t]*" + re.escape(token) + guard + r"[ \t]*", re.I)
        text, count = pattern.subn(" ", text)
        found = found or bool(count)
    return text, found


def _strip_leading_name(text, names):
    """(rest, how): how is "at" for "@name", "plain" for the bare name, None when absent."""
    for name in sorted(names, key=len, reverse=True):
        match = _name_pattern(name).match(text)
        if match:
            return text[match.end():], "at" if match.group("at") else "plain"
    return text, None


def _split_head(text):
    """(first word, the whitespace after it, the rest), the rest's newlines kept."""
    match = _HEAD.match(text)
    return match.groups() if match else ("", "", "")


def command_text(text):
    """A mentioned message whose first word is a command name, as that command.

    "spool list" -> "/spool list", "stop" -> "/print-stop",
    "handy setup" -> "/print-handy setup". The word must be a registered
    command or alias exactly, with or without the "print-" prefix
    (Telegram-style underscores count as hyphens). Anything else comes back
    unchanged, stripped, for the normal flow.
    """
    clean = str(text or "").strip()
    head, _space, rest = _split_head(clean)
    if not head or head.startswith("/"):
        return clean
    word = head.casefold().replace("_", "-")
    known = {c.name for c in commands.COMMANDS} | {a for c in commands.COMMANDS for a in c.aliases}
    for candidate in (word, "print-" + word):
        if candidate in known:
            return "/" + candidate + (" " + rest.strip() if rest.strip() else "")
    return clean


def _other_bot_handle(head, handles):
    """True when ``head`` is "@somebot", a bot username that is not one of ``handles``.

    Telegram requires every bot username to end in "bot", so a leading
    "@OtherBot" addresses a different bot; "@SomePerson" is a person and
    is left alone.
    """
    match = _HANDLE.fullmatch(head)
    if match is None:
        return False
    name = match.group(1).casefold()
    return name.endswith("bot") and name not in {h.casefold() for h in handles}


def address(text, identity):
    """Recognise and strip a mention of this bot in ``text``.

    No mention: the text comes back unchanged, so a message that does not
    name the bot routes exactly as before. An explicit mention is removed
    wherever it is; what is left runs as a command when its first word is a
    command name (``command_text``), and a bare mention asks for help. The
    name typed as plain text at the start only counts when what follows
    is a command: "Print Handy, help" runs help, while "print handy is
    great" stays the message it was. On Telegram a ``/cmd@handle`` runs
    only when the handle is this bot's known username.
    """
    raw = str(text or "")
    work, mentioned = _strip_tokens(raw, identity.tokens)
    if mentioned:
        work = _LEADING_PUNCT.sub("", work)  # "<@123>, spool list"
    mentioned = mentioned or identity.flagged
    work = work.strip()
    if identity.suffixes:
        head, space, rest = _split_head(work)
        own = {h.casefold() for h in identity.handles}
        if head.startswith("/") and "@" in head:
            command, _, handle = head.partition("@")
            if handle.casefold() not in own:
                # Another bot's command, or ours while our username is unknown:
                # never guess that a suffixed command was meant for this bot.
                return Addressed(raw, elsewhere=True)
            work, mentioned = command + space + rest, True
        elif _other_bot_handle(head, own):
            return Addressed(raw, elsewhere=True)
    work, how = _strip_leading_name(work, identity.names)
    mentioned = mentioned or how == "at"
    rest = work.strip()
    if mentioned:
        return Addressed(command_text(rest) if rest else _HELP, mentioned=True)
    if how == "plain" and rest:
        routed = command_text(rest)
        if routed.startswith("/"):
            return Addressed(routed, named=True)
        return Addressed(raw)  # the name in a sentence, not a command for the bot
    return Addressed(raw)
