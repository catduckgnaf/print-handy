"""One JSON state-file helper for every runtime state file (#1585).

Before this module there were four implementations of "write JSON
state atomically, read it tolerantly" (the listener, print_records,
the announcer and the alert store) plus two hand-rolled lock files of
the same shape (the alert store and the print-number registry). Each
had different atomicity and corruption behavior, which is where the
fixed-temp-name and narrow-except bugs came from.

This module is the one implementation:

- ``write_atomic(path, value)`` serializes a JSON value to a unique
  temp file in the target folder, flushes and fsyncs it, renames it
  over the target, then fsyncs the folder so the rename itself is
  durable. A crash leaves the old file or the whole new one.
- ``write_text_atomic(path, text)`` is the same write for callers
  that already serialized their text (print records).
- ``read_object(path)`` returns the stored JSON object, or ``{}``
  when the file is missing. A file that exists but cannot be read as
  a JSON object is corrupt: it is renamed to ``<name>.corrupt`` so
  the evidence survives, a warning is logged once per path, and the
  read returns ``{}`` instead of silently treating the file as empty
  forever.
- ``locked(path)`` serializes a read-modify-write across threads and
  processes with an O_EXCL lock file. A lock file older than
  ``stale_s`` belongs to a crashed holder and is broken. Callers
  choose whether a lock that cannot be had in ``wait_s`` raises
  (``raise_on_timeout=True``, the registry) or proceeds unlocked with
  a warning (the historical alert-store behavior).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_warned_corrupt: set[str] = set()
_warned_lock = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}
_thread_locks_guard = threading.Lock()
_held = threading.local()


def _thread_lock_for(key: str) -> threading.RLock:
    with _thread_locks_guard:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = _thread_locks[key] = threading.RLock()
        return lock


def _held_counts() -> dict[str, int]:
    counts = getattr(_held, "counts", None)
    if counts is None:
        counts = _held.counts = {}
    return counts


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        logger.debug("state file folder fsync failed", exc_info=True)
    finally:
        os.close(fd)


def write_text_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically, with fsync before the rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_dir(path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_atomic(path: Path, value: Any, *, indent: int | None = 2,
                 sort_keys: bool = False) -> None:
    """Serialize ``value`` as JSON and write it to ``path`` atomically."""
    text = json.dumps(value, indent=indent, sort_keys=sort_keys) + "\n"
    write_text_atomic(Path(path), text)


def _quarantine_corrupt(path: Path) -> None:
    """Rename an unreadable state file to ``<name>.corrupt``; warn once per path."""
    key = str(path)
    with _warned_lock:
        first = key not in _warned_corrupt
        _warned_corrupt.add(key)
    corrupt = path.with_name(path.name + ".corrupt")
    try:
        os.replace(path, corrupt)
        moved = True
    except OSError:
        moved = False
        logger.debug("could not rename corrupt state file %s", path, exc_info=True)
    if first:
        if moved:
            logger.warning("state file %s is unreadable; moved it to %s and starting empty",
                           path, corrupt)
        else:
            logger.warning("state file %s is unreadable; starting empty", path)


def read_object(path: Path) -> dict[str, Any]:
    """The JSON object stored at ``path``, or ``{}``.

    A missing file reads as ``{}`` silently. A file that exists but is
    not a readable JSON object is quarantined to ``<name>.corrupt``
    (see the module docstring) and reads as ``{}``.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError:
        # A directory, a permission error, ...: not parseable content we
        # can quarantine, and callers must not raise on state reads.
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        _quarantine_corrupt(path)
        return {}
    if not isinstance(value, dict):
        _quarantine_corrupt(path)
        return {}
    return value


class _Locked:
    """Cross-process lock file plus a per-path thread lock; re-entrant per thread."""

    def __init__(self, path: Path, *, lock_path: Path | None = None,
                 wait_s: float = 5.0, stale_s: float = 30.0,
                 raise_on_timeout: bool = False):
        target = Path(path)
        self.lock = Path(lock_path) if lock_path is not None else Path(str(target) + ".lock")
        try:
            self.key = str(self.lock.resolve())
        except OSError:
            self.key = str(self.lock)
        self.wait_s, self.stale_s = float(wait_s), float(stale_s)
        self.raise_on_timeout = raise_on_timeout
        self._file_held = False

    def __enter__(self) -> "_Locked":
        counts = _held_counts()
        if counts.get(self.key):
            counts[self.key] += 1
            return self
        thread_lock = _thread_lock_for(self.key)
        thread_lock.acquire()
        try:
            self.lock.parent.mkdir(parents=True, exist_ok=True)
            deadline = time.monotonic() + self.wait_s
            while True:
                try:
                    os.close(os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                    self._file_held = True
                    break
                except FileExistsError:
                    try:
                        if time.time() - self.lock.stat().st_mtime > self.stale_s:
                            self.lock.unlink(missing_ok=True)
                            continue
                    except OSError:
                        continue
                    if time.monotonic() >= deadline:
                        if self.raise_on_timeout:
                            raise TimeoutError(f"state file is locked: {self.lock}")
                        logger.warning("state file lock %s busy for %.0fs; proceeding without it",
                                       self.lock, self.wait_s)
                        break
                    time.sleep(0.02)
        except BaseException:
            thread_lock.release()
            raise
        counts[self.key] = 1
        return self

    def __exit__(self, *exc: object) -> bool:
        counts = _held_counts()
        counts[self.key] -= 1
        if not counts[self.key]:
            del counts[self.key]
            try:
                if self._file_held:
                    self._file_held = False
                    self.lock.unlink(missing_ok=True)
            except OSError:
                logger.debug("state file lock not removed", exc_info=True)
            finally:
                _thread_lock_for(self.key).release()
        return False


def locked(path: Path, *, lock_path: Path | None = None,
           wait_s: float = 5.0, stale_s: float = 30.0,
           raise_on_timeout: bool = False) -> _Locked:
    """Context manager serializing a read-modify-write of the file at ``path``."""
    return _Locked(path, lock_path=lock_path, wait_s=wait_s, stale_s=stale_s,
                   raise_on_timeout=raise_on_timeout)
