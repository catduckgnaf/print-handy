"""Discord REST posts with the bot token: no gateway connection, no Hermes, no LLM.

The listener posts through here; since #524 it replaces the ``hermes send`` subprocess.
Attachments: callers pass ``files`` explicitly. For handler code that still returns
marker strings, a ``MEDIA:/path`` line alone on its own line becomes a file attachment —
only for a Print Handy output file inside the bot's own folders (``attachable``), so a
marker smuggled in through a model title or filename can never upload a secret.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
LIMIT = 2000
# A marker is a whole line: ``MEDIA:`` at the start of a line, the path to the
# end of it. The path itself may contain spaces (P2-40), so the capture stops
# only at the line ending. Some callers join it with a literal backslash-n,
# which is turned into a real line break before matching. A ``MEDIA:`` in the
# middle of a line (a model title, a printer filename, an echoed argument) is
# never a marker.
MEDIA = re.compile(r"^MEDIA:([^\r\n]+)$", re.M)
# What a marker may attach: the media and print files Print Handy itself
# produces (camera frames, previews, timelapses, sliced and packaged prints,
# delivery zips) and the diagnostics bundle by name. Anything else — an .env,
# a settings or state .json, a database, a key — is never attached, wherever
# it lives.
ATTACHABLE_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".mp4", ".mov", ".webm", ".mkv",
    ".gcode", ".3mf", ".stl", ".obj", ".step", ".stp", ".zip",
})
# The sanitized text reports Print Handy writes: the diagnostics bundle and the
# debug-mode log export (print_handy.debug_mode.export_name).
ATTACHABLE_NAMES = re.compile(r"^(?:diagnostics-bundle|print-handy-debug-[\w.-]+)\.md$")


def inert_media(text):
    """Untrusted text (an agent or LLM reply) with every marker made inert, so it can never attach
    a local file: a reply saying MEDIA:/proc/self/environ would otherwise upload it. A zero-width
    space after the word keeps the text as it reads while split_media no longer matches it."""
    return str(text or "").replace("MEDIA:", "MEDIA​:")


def _output_roots():
    """The bot's own output folders, resolved: the data folders from settings
    (state home, archive, cache, records) and the system temp folder the
    camera snapshot and preview helpers write to. Settings that cannot be read
    leave only the temp folder."""
    import tempfile
    roots = [tempfile.gettempdir()]
    try:
        from print_handy import settings
        paths = settings.current().get("paths") or {}
        roots += [paths.get(key) for key in ("state_home", "archive", "cache", "records_vault")]
    except Exception:
        pass
    out = []
    for root in roots:
        if root:
            try:
                out.append(Path(os.path.expanduser(str(root))).resolve())
            except (OSError, RuntimeError):
                continue
    return out


def attachable(path):
    """The resolved path when a marker may attach it, else None.

    The file must exist, be one of the kinds Print Handy produces
    (``ATTACHABLE_SUFFIXES`` / ``ATTACHABLE_NAMES``), and resolve — symlinks
    followed — inside one of the bot's own output folders. Containment is
    checked after resolving, so ``..`` segments and links cannot step out.
    """
    try:
        resolved = Path(os.path.expanduser(str(path).strip())).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved.is_file():
        return None
    if resolved.suffix.lower() not in ATTACHABLE_SUFFIXES and not ATTACHABLE_NAMES.match(resolved.name):
        return None
    if not any(resolved.is_relative_to(root) for root in _output_roots()):
        return None
    return resolved


def split_media(text):
    """(text without markers, [attachable file paths]) — the compatibility path.

    Handler code that still returns marker strings keeps working: a marker
    alone on its own line names a file to attach. Only a file ``attachable``
    accepts is attached; a missing, foreign or out-of-folder file is dropped
    with a log line, never sent as text and never uploaded. Internal callers
    that attach a file should pass it explicitly (``outgoing``'s ``files``).
    """
    files = []

    def take(match):
        raw = match.group(1).strip()
        path = attachable(raw)
        if path is not None:
            files.append(path)
        elif raw:
            logger.warning("MEDIA marker dropped: %r is not an attachable output file", raw[:200])
        return ""

    text = str(text or "").replace("\\nMEDIA:", "\nMEDIA:")
    body = MEDIA.sub(take, text)
    body = "\n".join(line.rstrip() for line in body.splitlines()).strip()
    return body, files


def outgoing(text, files=None):
    """(body, [paths]) for one outgoing message on any platform.

    ``files`` are attachments an internal caller passes explicitly; each must
    exist (a missing one is dropped). Markers in ``text`` go through the
    restricted compatibility path (``split_media``).
    """
    body, found = split_media(text)
    explicit = [Path(f) for f in (files or ()) if f and Path(f).is_file()]
    return body, explicit + found


def starter_mention(content, mention_user_id):
    """(content, allowed_mentions) for one Discord post that may ping one user.

    Discord's ``allowed_mentions`` only filters the mentions already in the
    content: a user listed there and not mentioned in the text is never
    notified. So a post meant to ping the print's starter carries a real
    ``<@id>`` at the front, and ``allowed_mentions`` lets exactly that user
    through (``parse`` stays empty), so an ``@everyone``, a role or another
    user's mention inside a model title or filename still pings nobody.
    Anything but a plain ASCII digit string names nobody.
    """
    user_id = "" if mention_user_id is None else str(mention_user_id)
    if user_id.isascii() and user_id.isdigit():
        return f"<@{user_id}> {content}", {"parse": [], "users": [user_id]}
    return content, {"parse": []}


def chunks(text, limit=LIMIT):
    """Discord's 2000 character limit: split on line breaks where possible."""
    text = str(text or "")
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text or not out:
        out.append(text)
    return out


def _multipart(payload, files):
    boundary = uuid.uuid4().hex
    parts = [(f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
              f'Content-Type: application/json\r\n\r\n{json.dumps(payload)}\r\n').encode()]
    for index, path in enumerate(files):
        name = Path(path).name.replace('"', "")
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="files[{index}]"; '
                      f'filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode())
        parts.append(Path(path).read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _request(token, method, path, payload=None, files=(), timeout=30, opener=None):
    headers = {"Authorization": f"Bot {token}", "User-Agent": "PrintHandy (standalone, 1.0)"}
    data = None
    if files:
        data, headers["Content-Type"] = _multipart(payload or {}, files)
    elif payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body) if body else {}


def send(token, channel_id, text, *, opener=None, timeout=30, mention_user_id=None, files=None):
    """Post text (and ``files``, plus any attachable MEDIA line) to a channel.

    Returns (ok, first message id or error text).

    Mentions never ping by default (``allowed_mentions`` parses nothing).
    ``mention_user_id`` is the one exception: that single user — the person
    who started the print, decided by the starter pings classifier — is
    mentioned at the front of the post and allowed to be pinged, and nobody
    else is (``starter_mention``).
    """
    if not token:
        return False, "no Discord bot token"
    if not channel_id:
        return False, "discord.chat_id is not configured"
    body, files = outgoing(text, files)
    body, allowed = starter_mention(body, mention_user_id)
    pieces = chunks(body)
    first = None
    try:
        for index, piece in enumerate(pieces):
            last = index == len(pieces) - 1
            payload = {"content": piece, "allowed_mentions": allowed}
            sent = _request(token, "POST", f"/channels/{channel_id}/messages", payload,
                            files if last else (), timeout=timeout, opener=opener)
            first = first or str(sent.get("id") or "")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        detail = exc.read().decode(errors="replace")[:200] if isinstance(exc, urllib.error.HTTPError) else ""
        return False, f"{exc}{': ' + detail if detail else ''}"
    return True, first


def whoami(token, *, opener=None, timeout=10):
    """The bot user for a token: {'id', 'username'}. Raises on a bad token or no network."""
    return _request(token, "GET", "/users/@me", timeout=timeout, opener=opener)
