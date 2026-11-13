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
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .. import credential_binding
from .rest import chunks, outgoing

CLIENT = "/_matrix/client/v3"
MEDIA_CLIENT = "/_matrix/client/v1"
# Matrix events are capped at 65536 bytes on the wire. 16000 characters of
# plain text stays far under that even for multi-byte text, and matches the
# chunking style of the other platforms.
LIMIT = 16000
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".webm"}


def _headers(homeserver, token):
    """The request headers; the token only when the homeserver is the host it is bound to (#1192)."""
    headers = {"User-Agent": "PrintHandy (standalone, 1.0)"}
    token = credential_binding.key_for("matrix.homeserver_url", homeserver, token)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request(homeserver, token, method, path, payload=None, *, raw=None, content_type=None,
             timeout=30, opener=None):
    """One Client-Server API call. Returns parsed JSON ({} for an empty body)."""
    url = str(homeserver).rstrip("/") + path
    headers = _headers(homeserver, token)
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


def display_name(homeserver, token, user_id, *, opener=None, timeout=10):
    """A user's profile display name, or None when it has none."""
    user = urllib.parse.quote(str(user_id), safe="")
    data = _request(homeserver, token, "GET", f"{CLIENT}/profile/{user}/displayname",
                    timeout=timeout, opener=opener)
    name = str((data or {}).get("displayname") or "").strip()
    return name or None


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


def send_reaction(homeserver, token, room_id, event_id, key, *, txn_id=None, opener=None, timeout=30):
    """One m.reaction annotation on ``event_id`` with reaction ``key``. Returns the event id, or ''."""
    content = {"m.relates_to": {"rel_type": "m.annotation", "event_id": str(event_id), "key": str(key)}}
    sent = send_event(homeserver, token, room_id, "m.reaction", content, txn_id,
                      opener=opener, timeout=timeout)
    return str((sent or {}).get("event_id") or "")


def redact(homeserver, token, room_id, event_id, *, txn_id=None, opener=None, timeout=30):
    """Redact (delete) one event. Returns the response dict."""
    room = urllib.parse.quote(str(room_id), safe="")
    event = urllib.parse.quote(str(event_id), safe="")
    txn = urllib.parse.quote(str(txn_id or uuid.uuid4().hex), safe="")
    return _request(homeserver, token, "PUT", f"{CLIENT}/rooms/{room}/redact/{event}/{txn}",
                    {}, timeout=timeout, opener=opener)


def pin_event(homeserver, token, room_id, event_id, *, opener=None, timeout=30):
    """Add one event to the room's pinned list (``m.room.pinned_events``, #1332).

    The pinned list is room state, so the current list is read first and
    the event appended (a room with no pins yet answers 404). Already
    pinned is a no-op. A bot without the power level to change room state
    gets the homeserver's error raised to the caller.
    """
    room = urllib.parse.quote(str(room_id), safe="")
    path = f"{CLIENT}/rooms/{room}/state/m.room.pinned_events"
    try:
        current = _request(homeserver, token, "GET", path, timeout=timeout, opener=opener)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        current = {}
    pinned = [str(e) for e in (current or {}).get("pinned") or [] if e]
    if str(event_id) in pinned:
        return {}
    return _request(homeserver, token, "PUT", path, {"pinned": pinned + [str(event_id)]},
                    timeout=timeout, opener=opener)


def typing(homeserver, token, room_id, user_id, *, typing_ms=10000, opener=None, timeout=10):
    """PUT /rooms/{roomId}/typing/{userId}: show the bot as typing for ``typing_ms``."""
    room = urllib.parse.quote(str(room_id), safe="")
    user = urllib.parse.quote(str(user_id), safe="")
    return _request(homeserver, token, "PUT", f"{CLIENT}/rooms/{room}/typing/{user}",
                    {"typing": True, "timeout": int(typing_ms)}, timeout=timeout, opener=opener)


_MATRIX_USER = re.compile(r"@[a-z0-9._=/+-]+:[A-Za-z0-9.-]+(?::\d{1,5})?")


def mention_content(text, mention_user_id=None):
    """(body, m.mentions) for one text event (intentional mentions, Matrix 1.7).

    An event that carries ``m.mentions`` is the only signal a
    mentions-aware homeserver uses to notify: the legacy rules that match
    ``@room`` or a member's display name anywhere in the body are skipped.
    So every post sends an explicit empty object, and an ``@room`` or a
    display name inside a model title or filename notifies nobody. A post
    meant to ping one person names a well-formed Matrix user ID: it leads
    the body and is the only ID listed.
    """
    user_id = "" if mention_user_id is None else str(mention_user_id)
    if _MATRIX_USER.fullmatch(user_id):
        return f"{user_id} {text}", {"user_ids": [user_id]}
    return str(text), {}


def send_message(homeserver, token, room_id, text, *, txn_id=None, opener=None, timeout=30,
                 mention_user_id=None):
    """One m.room.message text event. Returns the event id, or ''.

    Mentions nobody unless ``mention_user_id`` names one Matrix user
    (``mention_content``)."""
    body, mentions = mention_content(text, mention_user_id)
    sent = send_event(homeserver, token, room_id, "m.room.message",
                      {"msgtype": "m.text", "body": body, "m.mentions": mentions},
                      txn_id, opener=opener, timeout=timeout)
    return str((sent or {}).get("event_id") or "")


def edit_message(homeserver, token, room_id, event_id, text, *, txn_id=None, opener=None, timeout=30):
    """Edit one posted message: an m.replace relation carrying the new text.

    Returns the edit event's id, or ''. Clients render the replacement in
    place of the original event; the fallback body keeps the '* ' prefix
    convention for clients that do not understand edits.
    """
    # Empty m.mentions on both halves: an edit never notifies anyone.
    content = {"msgtype": "m.text", "body": f"* {text}", "m.mentions": {},
               "m.new_content": {"msgtype": "m.text", "body": str(text), "m.mentions": {}},
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
    # The body is the file name: an @room in it must not notify the room.
    content = {"msgtype": msgtype, "body": path.name, "url": uri, "m.mentions": {},
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
    req = urllib.request.Request(url, method="GET", headers=_headers(homeserver, token))
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
        return resp.read()


def send(homeserver, token, room_id, text, *, opener=None, timeout=120, mention_user_id=None,
         files=None):
    """Post text (and ``files``, plus any attachable MEDIA line) to a room.

    Returns (ok, first event id or error text).

    Every event carries an explicit ``m.mentions``: empty, or on the first
    text event only, the one user ``mention_user_id`` names."""
    if not token:
        return False, "no Matrix access token"
    if not homeserver:
        return False, "matrix.homeserver_url is not configured"
    if not room_id:
        return False, "matrix.room_id is not configured"
    body, files = outgoing(text, files)
    pieces = chunks(body, LIMIT)
    first = None
    mention = mention_user_id
    try:
        for piece in pieces:
            if not piece and files:
                continue
            sent = send_message(homeserver, token, room_id, piece, opener=opener, timeout=timeout,
                                mention_user_id=mention)
            mention = None
            first = first or sent
        for path in files:
            sent = send_file(homeserver, token, room_id, path, opener=opener, timeout=timeout)
            first = first or sent
    except (urllib.error.URLError, OSError, ValueError) as exc:
        detail = exc.read().decode(errors="replace")[:200] if isinstance(exc, urllib.error.HTTPError) else ""
        return False, f"{exc}{': ' + detail if detail else ''}"
    return True, first
