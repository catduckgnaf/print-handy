"""Telegram Bot API posts with the bot token: standard library only, no gateway, no LLM.

The listener posts through here in standalone mode when ``[bot] platform``
is ``telegram``, mirroring ``print_handy.bot.rest`` for Discord. A
``MEDIA:/path`` marker in the text becomes a document attachment, the same
marker convention the Discord sender reads. Messages are plain text: no
``parse_mode`` is set, so model names, file paths and G-code fragments are
never reinterpreted as Telegram Markdown.

Every call takes an injectable ``opener`` (the ``urllib.request.urlopen``
shape) so tests drive the whole module against a stubbed Bot API with no
network. The token appears only in request URLs (that is how the Bot API
works); ``print_handy.logs`` redacts its shape from log lines.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .rest import chunks, outgoing

API = "https://api.telegram.org"
LIMIT = 4096  # Telegram's sendMessage text limit, in characters.
# Extensions sent as photos/videos instead of documents, when small enough
# to matter: Telegram compresses photos, so anything not in these sets (and
# every model or G-code file) goes as a document, byte for byte.
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov"}


def _encode(payload, files):
    """(body, content type): JSON, or multipart when files are attached."""
    if not files:
        return json.dumps(payload).encode(), "application/json"
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in payload.items():
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
                      f"{value}\r\n").encode())
    for field, path in files:
        name = Path(path).name.replace('"', "")
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
                      f"filename=\"{name}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode())
        parts.append(Path(path).read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def call(token, method, payload=None, files=(), *, timeout=30, opener=None):
    """One Bot API call. Returns the ``result`` value; raises on HTTP/API errors."""
    payload = dict(payload or {})
    data, content_type = _encode(payload, files)
    req = urllib.request.Request(f"{API}/bot{token}/{method}", data=data, method="POST",
                                 headers={"Content-Type": content_type,
                                          "User-Agent": "PrintHandy (standalone, 1.0)"})
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
        body = resp.read()
    data = json.loads(body) if body else {}
    if not data.get("ok", True):
        raise ValueError(f"Telegram {method} failed: {data.get('description') or data}")
    return data.get("result")


def get_updates(token, offset, *, poll_timeout=25, timeout=35, opener=None):
    """Long-poll getUpdates. Returns the list of update dicts (may be empty)."""
    result = call(token, "getUpdates",
                  {"offset": int(offset), "timeout": int(poll_timeout),
                   "allowed_updates": json.dumps(["message", "callback_query"])},
                  timeout=timeout, opener=opener)
    return result if isinstance(result, list) else []


def download(token, file_id, *, timeout=60, opener=None):
    """The bytes of one uploaded file: getFile, then the file download URL."""
    info = call(token, "getFile", {"file_id": str(file_id)}, timeout=timeout, opener=opener)
    path = str((info or {}).get("file_path") or "")
    if not path:
        raise ValueError(f"Telegram getFile returned no file_path for {file_id!r}")
    req = urllib.request.Request(f"{API}/file/bot{token}/{path}",
                                 headers={"User-Agent": "PrintHandy (standalone, 1.0)"})
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
        return resp.read()


def whoami(token, *, opener=None, timeout=10):
    """The bot user for a token: {'id', 'username', ...}. Raises on a bad token or no network."""
    return call(token, "getMe", timeout=timeout, opener=opener)


def _file_call(token, chat_id, path, *, thread_id=None, opener=None, timeout=60):
    """Send one file with the Bot API method matching its extension."""
    suffix = Path(path).suffix.lower()
    if suffix in PHOTO_EXTS:
        method, field = "sendPhoto", "photo"
    elif suffix in VIDEO_EXTS:
        method, field = "sendVideo", "video"
    else:
        method, field = "sendDocument", "document"
    payload = {"chat_id": str(chat_id)}
    if thread_id:
        payload["message_thread_id"] = str(thread_id)
    return call(token, method, payload, files=[(field, path)], timeout=timeout, opener=opener)


def send(token, chat_id, text, *, thread_id=None, opener=None, timeout=60, files=None,
         reply_markup=None):
    """Post text (and ``files``, plus any attachable MEDIA line) to a chat.

    ``reply_markup`` (an inline keyboard) rides the last text piece, the
    same place the adapter's prompt keyboards sit. Returns (ok, first
    message id or error text)."""
    if not token:
        return False, "no Telegram bot token"
    if not chat_id:
        return False, "telegram.chat_id is not configured"
    body, files = outgoing(text, files)
    pieces = [piece for piece in chunks(body, LIMIT) if piece]
    first = None
    try:
        for index, piece in enumerate(pieces):
            payload = {"chat_id": str(chat_id), "text": piece}
            if thread_id:
                payload["message_thread_id"] = str(thread_id)
            if reply_markup is not None and index == len(pieces) - 1:
                payload["reply_markup"] = reply_markup
            sent = call(token, "sendMessage", payload, timeout=timeout, opener=opener)
            first = first or str((sent or {}).get("message_id") or "")
        for path in files:
            sent = _file_call(token, chat_id, path, thread_id=thread_id, opener=opener, timeout=timeout)
            first = first or str((sent or {}).get("message_id") or "")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        detail = exc.read().decode(errors="replace")[:200] if isinstance(exc, urllib.error.HTTPError) else ""
        return False, f"{exc}{': ' + detail if detail else ''}"
    return True, first
