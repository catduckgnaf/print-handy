"""Discord REST posts with the bot token: no gateway connection, no Hermes, no LLM.

The listener posts through here; since #524 it replaces the ``hermes send`` subprocess.
A ``MEDIA:/path`` marker in the text becomes a file attachment, the same marker convention
``hermes send`` read.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API = "https://discord.com/api/v10"
LIMIT = 2000
# A marker runs to the end of its line — the path itself may contain spaces
# (P2-40), so the capture stops only at the line ending. Some callers join it
# with a literal backslash-n.
MEDIA = re.compile(r"(?:\\n)?MEDIA:([^\r\n]+)")


def inert_media(text):
    """Untrusted text (an agent or LLM reply) with every marker made inert, so it can never attach
    a local file: a reply saying MEDIA:/proc/self/environ would otherwise upload it. A zero-width
    space after the word keeps the text as it reads while split_media no longer matches it."""
    return str(text or "").replace("MEDIA:", "MEDIA​:")


def split_media(text):
    """(text without markers, [existing file paths]). A missing file is dropped, never sent as text."""
    files = []

    def take(match):
        path = Path(match.group(1).strip())
        if path.is_file():
            files.append(path)
        return ""

    body = MEDIA.sub(take, str(text or ""))
    body = "\n".join(line.rstrip() for line in body.splitlines()).strip()
    return body, files


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


def send(token, channel_id, text, *, opener=None, timeout=30, mention_user_id=None):
    """Post text (and any MEDIA files) to a channel. Returns (ok, first message id or error text).

    Mentions never ping by default (``allowed_mentions`` parses nothing).
    ``mention_user_id`` is the one exception: that single user — the person
    who started a finished print, named by the listener from the post's own
    starter-mention line — is allowed to be pinged, and nobody else is.
    """
    if not token:
        return False, "no Discord bot token"
    if not channel_id:
        return False, "discord.chat_id is not configured"
    allowed = {"parse": []}
    if mention_user_id is not None and str(mention_user_id).isdigit():
        allowed = {"parse": [], "users": [str(mention_user_id)]}
    body, files = split_media(text)
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
