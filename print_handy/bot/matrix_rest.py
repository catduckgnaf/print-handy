"""Matrix Client-Server API calls with the bot access token: standard library only.

The listener posts through here in standalone mode when ``[bot] platform``
is ``matrix``, mirroring ``print_handy.bot.telegram_rest`` for Telegram. A
``MEDIA:/path`` marker in the text becomes a media event, the same marker
convention the Discord and Telegram senders read.

Every call takes an injectable ``opener`` (the ``urllib.request.urlopen``
shape) so tests drive the whole module against a stubbed homeserver with
no network. The access token travels in the ``Authorization`` header (the
deprecated ``?access_token=`` query form is never used); ``print_handy.logs``
redacts its ``syt_`` shape from log lines anyway.
"""
from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .rest import chunks, split_media

CLIENT = "/_matrix/client/v3"
MEDIA_CLIENT = "/_matrix/client/v1"
# Matrix events are capped at 65536 bytes on the wire. 16000 characters of
# plain text stays far under that even for multi-byte text, and matches the
# chunking style of the other platforms.
LIMIT = 16000
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".webm"}


def _request(homeserver, token, method, path, payload=None, *, raw=None, content_type=None,
             timeout=30, opener=None):
    """One Client-Server API call. Returns parsed JSON ({} for an empty body)."""
    url = str(homeserver).rstrip("/") + path
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "PrintHandy (standalone, 1.0)"}
    data = raw
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    elif content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
        body = resp.read()
    data = json.loads(body) if body else {}
    if isinstance(data, dict) and data.get("errcode"):
        raise ValueError(f"Matrix {method} {path} failed: {data.get('errcode')}: {data.get('error') or data}")
    return data


def whoami(homeserver, token, *, opener=None, timeout=10):
    """The bot's own account: {'user_id', 'device_id', ...}. Raises on a bad token."""
    return _request(homeserver, token, "GET", f"{CLIENT}/account/whoami", timeout=timeout, opener=opener)


def media_config(homeserver, token, *, opener=None, timeout=10):
    """The server's upload size limit in bytes (m.upload.size), or None when unknown."""
    try:
        data = _request(homeserver, token, "GET", "/_matrix/media/v3/config", timeout=timeout, opener=opener)
    except Exception:
        return None
    size = (data or {}).get("m.upload.size")
    return int(size) if isinstance(size, (int, float)) and size > 0 else None


def sync(homeserver, token, since=None, *, timeout_ms=30000, opener=None):
    """One /sync call. ``since=None`` with a short timeout snapshots 'now' (the next_batch)."""
    query = {"timeout": int(timeout_ms)}
    if since:
        query["since"] = since
    path = f"{CLIENT}/sync?{urllib.parse.urlencode(query)}"
    return _request(homeserver, token, "GET", path, timeout=timeout_ms / 1000 + 15, opener=opener)


def join_room(homeserver, token, room_id, *, opener=None, timeout=30):
    """Accept an invitation: POST /rooms/{roomId}/join. Returns the response dict."""
    room = urllib.parse.quote(str(room_id), safe="")
    return _request(homeserver, token, "POST", f"{CLIENT}/rooms/{room}/join", {},
                    timeout=timeout, opener=opener)


def send_event(homeserver, token, room_id, event_type, content, txn_id=None, *, opener=None, timeout=30):
    """PUT one event into a room. Returns the response dict ({'event_id': ...})."""
    room = urllib.parse.quote(str(room_id), safe="")
    txn = urllib.parse.quote(str(txn_id or uuid.uuid4().hex), safe="")
    return _request(homeserver, token, "PUT", f"{CLIENT}/rooms/{room}/send/{event_type}/{txn}",
                    content, timeout=timeout, opener=opener)


def redact(homeserver, token, room_id, event_id, *, txn_id=None, opener=None, timeout=30):
    """Redact (delete) one event. Returns the response dict."""
    room = urllib.parse.quote(str(room_id), safe="")
    event = urllib.parse.quote(str(event_id), safe="")
    txn = urllib.parse.quote(str(txn_id or uuid.uuid4().hex), safe="")
    return _request(homeserver, token, "PUT", f"{CLIENT}/rooms/{room}/redact/{event}/{txn}",
                    {}, timeout=timeout, opener=opener)


def typing(homeserver, token, room_id, user_id, *, typing_ms=10000, opener=None, timeout=10):
    """PUT /rooms/{roomId}/typing/{userId}: show the bot as typing for ``typing_ms``."""
    room = urllib.parse.quote(str(room_id), safe="")
    user = urllib.parse.quote(str(user_id), safe="")
    return _request(homeserver, token, "PUT", f"{CLIENT}/rooms/{room}/typing/{user}",
                    {"typing": True, "timeout": int(typing_ms)}, timeout=timeout, opener=opener)


def send_message(homeserver, token, room_id, text, *, txn_id=None, opener=None, timeout=30):
    """One m.room.message text event. Returns the event id, or ''."""
    sent = send_event(homeserver, token, room_id, "m.room.message",
                      {"msgtype": "m.text", "body": str(text)}, txn_id, opener=opener, timeout=timeout)
    return str((sent or {}).get("event_id") or "")


def edit_message(homeserver, token, room_id, event_id, text, *, txn_id=None, opener=None, timeout=30):
    """Edit one posted message: an m.replace relation carrying the new text.

    Returns the edit event's id, or ''. Clients render the replacement in
    place of the original event; the fallback body keeps the '* ' prefix
    convention for clients that do not understand edits.
    """
    content = {"msgtype": "m.text", "body": f"* {text}",
               "m.new_content": {"msgtype": "m.text", "body": str(text)},
               "m.relates_to": {"rel_type": "m.replace", "event_id": str(event_id)}}
    sent = send_event(homeserver, token, room_id, "m.room.message", content,
                      txn_id, opener=opener, timeout=timeout)
    return str((sent or {}).get("event_id") or "")


def upload_media(homeserver, token, path, *, opener=None, timeout=120):
    """Upload one file to the media repo. Returns its mxc:// URI."""
    path = Path(path)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    query = urllib.parse.urlencode({"filename": path.name})
    data = _request(homeserver, token, "POST", f"/_matrix/media/v3/upload?{query}",
                    raw=path.read_bytes(), content_type=content_type, timeout=timeout, opener=opener)
    uri = str((data or {}).get("content_uri") or "")
    if not uri.startswith("mxc://"):
        raise ValueError(f"Matrix media upload returned no content_uri for {path.name!r}")
    return uri


def send_file(homeserver, token, room_id, path, *, txn_id=None, opener=None, timeout=120):
    """Upload one file and post it as an m.file / m.image / m.video event. Returns the event id."""
    path = Path(path)
    uri = upload_media(homeserver, token, path, opener=opener, timeout=timeout)
    suffix = path.suffix.lower()
    if suffix in PHOTO_EXTS:
        msgtype = "m.image"
    elif suffix in VIDEO_EXTS:
        msgtype = "m.video"
    else:
        msgtype = "m.file"
    content = {"msgtype": msgtype, "body": path.name, "url": uri,
               "info": {"size": path.stat().st_size,
                        "mimetype": mimetypes.guess_type(path.name)[0] or "application/octet-stream"}}
    sent = send_event(homeserver, token, room_id, "m.room.message", content, txn_id,
                      opener=opener, timeout=timeout)
    return str((sent or {}).get("event_id") or "")


def download_media(homeserver, token, mxc_url, *, opener=None, timeout=60):
    """The bytes behind an mxc:// URI, through the authenticated client media endpoint."""
    value = str(mxc_url or "")
    if not value.startswith("mxc://"):
        raise ValueError(f"not an mxc URI: {mxc_url!r}")
    server, _, media_id = value[len("mxc://"):].partition("/")
    if not server or not media_id:
        raise ValueError(f"malformed mxc URI: {mxc_url!r}")
    path = (f"{MEDIA_CLIENT}/media/download/{urllib.parse.quote(server, safe='')}"
            f"/{urllib.parse.quote(media_id, safe='')}")
    url = str(homeserver).rstrip("/") + path
    req = urllib.request.Request(url, method="GET",
                                 headers={"Authorization": f"Bearer {token}",
                                          "User-Agent": "PrintHandy (standalone, 1.0)"})
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
        return resp.read()


def send(homeserver, token, room_id, text, *, opener=None, timeout=120):
    """Post text (and any MEDIA files) to a room. Returns (ok, first event id or error text)."""
    if not token:
        return False, "no Matrix access token"
    if not homeserver:
        return False, "matrix.homeserver_url is not configured"
    if not room_id:
        return False, "matrix.room_id is not configured"
    body, files = split_media(text)
    pieces = chunks(body, LIMIT)
    first = None
    try:
        for piece in pieces:
            if not piece and files:
                continue
            sent = send_message(homeserver, token, room_id, piece, opener=opener, timeout=timeout)
            first = first or sent
        for path in files:
            sent = send_file(homeserver, token, room_id, path, opener=opener, timeout=timeout)
            first = first or sent
    except (urllib.error.URLError, OSError, ValueError) as exc:
        detail = exc.read().decode(errors="replace")[:200] if isinstance(exc, urllib.error.HTTPError) else ""
        return False, f"{exc}{': ' + detail if detail else ''}"
    return True, first
