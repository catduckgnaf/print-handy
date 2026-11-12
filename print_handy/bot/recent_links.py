"""The last model link posted in each channel, for "print it" follow-ups.

Replaces the Hermes state.db messages lookup: Print Handy keeps its own small state file.
"""
from __future__ import annotations

import json
import os
import threading
import time

from ..runtime import _config

LIMIT = 50
TTL_S = 7 * 24 * 3600
_lock = threading.Lock()


def _path():
    return _config.state_file(None, "recent_model_links.json")


def _load():
    try:
        data = json.loads(_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _timestamp(value):
    """A stored row's timestamp as a float, or None when it cannot be read as one (#404)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _scope(chat_id, thread_id=None):
    return f"{chat_id}:{thread_id or ''}"


def remember(chat_id, url, thread_id=None, now=None, user_id=None, printer_id=None):
    """Record the newest model link for a channel (or thread). Failures are ignored.

    The row also records who posted the link and the printer the post's
    channel routes to, when known (#979): the store is still scoped per
    channel, but a future incident — or a future scoping change — should
    never again have to guess a link's provenance. Rows written before
    provenance existed ({"url", "at"} only) load and recall unchanged.
    """
    if not url or not chat_id:
        return
    now = time.time() if now is None else now
    path = _path()
    with _lock:
        data = {}
        for k, v in _load().items():
            if not isinstance(v, dict):
                continue
            at = _timestamp(v.get("at", 0))
            if at is not None and now - at < TTL_S:
                data[k] = v
        row = {"url": str(url), "at": now}
        if user_id is not None:
            row["user_id"] = str(user_id)
        if printer_id is not None:
            row["printer_id"] = str(printer_id)
        data[_scope(chat_id, thread_id)] = row
        if len(data) > LIMIT:
            data = dict(sorted(data.items(), key=lambda kv: _timestamp(kv[1].get("at", 0)) or 0.0)[-LIMIT:])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            os.replace(tmp, path)
        except OSError:
            pass


def recall(chat_id, thread_id=None, now=None):
    """The newest unexpired model link for this channel (or thread), else None."""
    now = time.time() if now is None else now
    row = _load().get(_scope(chat_id, thread_id))
    if not isinstance(row, dict):
        return None
    at = _timestamp(row.get("at", 0))
    if at is None or now - at >= TTL_S:
        return None
    return row.get("url") or None
