"""The listener's print posts, for web chat: a feed file in the state folder.

The listener (Moonraker/Klipper, and every family monitor and event
listener, which all post through ``listener_common.post``) runs
in its own process and posts to the chat through the platform REST
senders, never through BotCore, so web chat (bot/web.py, in the Web UI
process) cannot see those posts by itself. This module is the bridge,
and it is deliberately the plainest one: the listener appends each post
it delivered as one JSON line to ``<state>/webui-chat-feed.jsonl``, the
state folder both processes already share (the Web UI's status page
reads the listener's state from the same folder), and web chat reads
the new lines on its own loop and posts them as shared posts every
session sees. No socket, no route, no new auth.

What a line carries: the same rendered text the chat got (the shared
``format_event`` text and its follow-ups), with the attachment markers
taken out and the attached files' names kept, the chat platform's name,
and, for an alert that asks for a decision (a pause, an error, an
emergency stop, a camera check), the alert's kind, file and routing
fields, so web chat can offer the same actions as buttons through the
alert store's claim-then-act path (bot/web.py). The text never carries
a starter mention: the ping is a Discord send-path decision (#1095) and
the rendered text has none.

Fail soft, both ends. ``publish`` never raises and is called after the
chat delivery, so a full disk or a bad folder can never delay or break
a chat post or a print; the reader skips a line it cannot parse.

Bounded: past ``MAX_BYTES`` the file is moved aside to ``.1`` (one
generation kept) before the next append, and the reader reads both, so
a line is never lost between two reads. Lines are matched by their own
id, so a reader never posts a line twice, and lines older than the
reader's start are never posted (a Web UI restart does not replay old
prints; the status page shows the current state).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

FEED_NAME = "webui-chat-feed.jsonl"
MAX_BYTES = 256 * 1024
TEXT_LIMIT = 8000
# The kinds an alert line may name: the print_alert_buttons kinds whose
# buttons run print_alert_buttons.handle_click.
ALERT_KINDS = frozenset({"paused", "error", "shutdown", "visual", "visual-dark"})
_MARKER = re.compile(r"^MEDIA:([^\r\n]+)$", re.M)
PLATFORM_NAMES = {"discord": "Discord", "telegram": "Telegram", "matrix": "Matrix"}


def feed_path(state_dir) -> Path:
    return Path(state_dir) / FEED_NAME


def _old_path(path) -> Path:
    path = Path(path)
    return path.with_name(path.name + ".1")


def enabled(cfg) -> bool:
    """Whether web chat wants the feed: the chat page's switch, on by default."""
    return bool(((cfg or {}).get("webui") or {}).get("chat_enabled", True))


def entry_for(message, *, platform="", alert=None, now=None) -> dict:
    """One feed line for one delivered post.

    The attachment markers come out of the text (split_media, the same
    restricted path every sender uses: only a Print Handy output file is
    named), and so does a bare line repeating a marker's local path (the
    camera alerts put one above the marker): a server path is not the
    reader's business, as in web chat's own sends.
    """
    from . import rest
    raw = str(message or "").replace("\\nMEDIA:", "\nMEDIA:")
    marked = {m.group(1).strip() for m in _MARKER.finditer(raw)}
    body, files = rest.split_media(raw)
    if marked:
        body = "\n".join(line for line in body.splitlines()
                         if line.strip() not in marked).strip()
    entry = {"id": uuid.uuid4().hex, "ts": time.time() if now is None else float(now),
             "text": body[:TEXT_LIMIT], "media": [Path(f).name for f in files],
             "platform": str(platform or "")}
    if alert:
        entry["alert"] = dict(alert)
    return entry


def alert_spec(kind, filename, fields=None) -> dict | None:
    """The alert part of a line: what web chat records its own alert from."""
    if kind not in ALERT_KINDS:
        return None
    return {"kind": str(kind), "filename": str(filename or ""),
            "fields": {k: v for k, v in (fields or {}).items() if v}}


def publish(message, *, state_dir, platform="", alert=None, now=None) -> bool:
    """Append one delivered post to the feed. True when a line was written. Never raises."""
    try:
        entry = entry_for(message, platform=platform, alert=alert, now=now)
        if not entry["text"] and not entry["media"]:
            return False
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        path = feed_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size and size + len(line) > MAX_BYTES:
            try:
                os.replace(path, _old_path(path))
            except OSError:
                pass  # another listener rotated it first
        # One write of one whole line in append mode: listeners for
        # several printers can append at once without interleaving.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        return True
    except Exception as exc:
        logger.warning("web chat feed append failed: %s: %s", type(exc).__name__, exc)
        return False


def render(entry) -> str:
    """The web chat text for one line: the post, and one line for any attachment.

    The chat page cannot show attachments (a timelapse video, a
    completion snapshot, a camera check photo), so it says where they
    went instead of dropping them silently.
    """
    text = str(entry.get("text") or "")
    media = [str(name) for name in entry.get("media") or [] if name]
    if media:
        where = PLATFORM_NAMES.get(str(entry.get("platform") or ""), "the chat")
        note = (f"📎 {', '.join(media)} went to {where} with this post; "
                "the chat page cannot show attachments.")
        text = f"{text}\n{note}" if text else note
    return text


class FeedReader:
    """The new lines since this reader started, each once."""

    def __init__(self, state_dir, since=None):
        self.path = feed_path(state_dir)
        self.since = time.time() if since is None else float(since)
        self._seen = set()
        self._signature = None
        self._lock = threading.Lock()

    def _stat(self, path):
        try:
            st = path.stat()
            return (st.st_size, st.st_mtime_ns)
        except OSError:
            return None

    def poll(self) -> list:
        """New lines, oldest first; [] when nothing changed. Never raises."""
        with self._lock:
            return self._poll()

    def _poll(self) -> list:
        try:
            old = _old_path(self.path)
            signature = (self._stat(old), self._stat(self.path))
            if signature == self._signature:
                return []
            self._signature = signature
            rows = []
            for path in (old, self.path):
                try:
                    raw = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for line in raw.splitlines():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue  # a line being written is read again next time
                    if isinstance(row, dict) and row.get("id"):
                        rows.append(row)
            fresh = []
            for row in rows:
                if row["id"] in self._seen:
                    continue
                try:
                    ts = float(row.get("ts") or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                if ts >= self.since:
                    fresh.append(row)
            # Remember exactly the ids still in the files: bounded with them.
            self._seen = {row["id"] for row in rows}
            return fresh
        except Exception:
            logger.exception("web chat feed read failed")
            return []


__all__ = ["ALERT_KINDS", "FEED_NAME", "FeedReader", "alert_spec", "enabled", "entry_for",
           "feed_path", "publish", "render"]
