"""This install's stable instance id (#1400, ha-print-handy #14).

A random id minted once, on the first start that asks for it, and kept
in the state folder (``<state>/instance_id``). External consumers (the
Home Assistant integration) key their entities on it, so a change of
the address they reach the server by (hostname to IP address, http to
https) does not orphan anything.

The id is ``uuid.uuid4().hex``: 32 random hex characters and nothing
else. It is never derived from the host name, a network address, a
token or any other local fact, so it reveals nothing about the install.
Deleting the file mints a new id on the next read, which a consumer
sees as a new instance.
"""
from __future__ import annotations

import os
import re
import tempfile
import threading
import uuid
from pathlib import Path

from .. import settings

INSTANCE_FILE = "instance_id"
_VALID = re.compile(r"[0-9a-f]{32}")
_lock = threading.Lock()


def instance_path(data) -> Path:
    return Path(settings.state_dir(data)) / INSTANCE_FILE


def _read(path):
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text if _VALID.fullmatch(text) else None


def _mint(path):
    """Write a new id without ever replacing a valid one another process wrote first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    value = uuid.uuid4().hex
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".instance_id.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists() and _read(path) is None:
            # An unreadable or malformed file holds no identity to keep.
            os.replace(tmp, path)
            return value
        try:
            os.link(tmp, path)  # create-if-absent, atomically
        except FileExistsError:
            return _read(path)
        except OSError:  # a filesystem without hard links: exclusive create instead
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                return _read(path)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(value + "\n")
        return value
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def instance_id(data):
    """This install's id (32 hex characters), minted on first use; None when
    the state folder cannot be read or written. Never raises."""
    try:
        path = instance_path(data)
        with _lock:
            return _read(path) or _mint(path)
    except Exception:  # a state folder problem never breaks a status payload
        return None
