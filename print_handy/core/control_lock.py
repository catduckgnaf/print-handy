"""Interprocess printer-control lock (P1-33).

One physical printer must never take two control actions at once: a purge,
a slot load, a nozzle change and a print start overlapping can heat, feed
or move against each other. The guard used to be a per-process
``threading.Lock``, but the commands that need it run in separate
processes — the listener, the bot, the ``print-now`` subprocess and the
standalone ``ph-purge`` / ``ph-load-slot`` CLIs each held their own lock
and could interleave freely.

This lock layers an OS file lock (``flock``) under the threading lock. The
file lives in the printer's own state dir, keyed by printer, so every
process acting on the same printer contends for the same lock, while two
printers never block each other. ``flock`` is released by the kernel when
the holding process dies, so a crashed CLI can never wedge the printer.
Acquisition is fail-closed: a held lock refuses, it never queues behind a
possibly-dead holder beyond the caller's own timeout.

``flock`` is POSIX; Print Handy ships on Linux/Docker (the codebase's
platform pattern is ``os.name == "posix"``, see core/design.py). Where
``fcntl`` is unavailable the lock degrades to the in-process threading
lock alone. The threading lock stays the inner layer on purpose: it is
what makes a same-process re-entry refuse immediately instead of
deadlocking against the process's own flock.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable

try:
    import fcntl
except ImportError:  # non-POSIX platform: in-process lock only
    fcntl = None  # type: ignore[assignment]

from .. import settings


def _printer_key(printer_id: str | None) -> str:
    """The lock's printer identity: the given roster id, else the default printer's."""
    if printer_id:
        return str(printer_id)
    try:
        return str(settings.default_printer_id())
    except Exception:
        return "default"


def _path_segment(key: str) -> str:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in key)
    return safe or "default"


class PrinterControlLock:
    """A ``threading.Lock``-shaped guard shared across processes via flock.

    ``acquire`` / ``release`` / ``locked`` mirror the threading.Lock calls
    the control paths already make, so existing users swap in unchanged.
    """

    def __init__(self, path_for: Callable[[], Path]):
        self._path_for = path_for
        self._inner = threading.Lock()
        self._fd: int | None = None

    def path(self) -> Path:
        """The lock file this guard contends for (diagnostics and tests)."""
        return self._path_for()

    def locked(self) -> bool:
        return self._inner.locked()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if not blocking and timeout != -1:
            raise ValueError("can't specify a timeout for a non-blocking acquire")
        deadline = None if (not blocking or timeout is None or timeout < 0) \
            else time.monotonic() + timeout
        if blocking and deadline is None:
            self._inner.acquire()
        elif blocking:
            if not self._inner.acquire(timeout=max(0.0, deadline - time.monotonic())):
                return False
        elif not self._inner.acquire(blocking=False):
            return False
        try:
            if self._flock(blocking, deadline):
                return True
        except BaseException:
            self._inner.release()
            raise
        self._inner.release()
        return False

    def _flock(self, blocking: bool, deadline: float | None) -> bool:
        """Take the file lock; False means another process holds it."""
        if fcntl is None:
            return True
        path = self._path_for()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._fd = fd
                    return True
                except BlockingIOError:
                    if not blocking or (deadline is not None and time.monotonic() >= deadline):
                        os.close(fd)
                        return False
                    time.sleep(0.01)
        except BaseException:
            os.close(fd)
            raise

    def release(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        self._inner.release()

    def __enter__(self) -> "PrinterControlLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


_LOCKS: dict[tuple[str, str | None], PrinterControlLock] = {}
_LOCKS_GUARD = threading.Lock()


def control_lock(printer_id: str | None = None, *, state_dir: Path | None = None) -> PrinterControlLock:
    """The shared control lock for one printer (one instance per printer and state dir).

    The lock file is ``<state_dir>/printers/<printer id>/control.lock``;
    ``state_dir`` defaults to the configured state dir, and callers with
    their own settings view (the Hermes helpers) pass theirs so every
    process resolves the same file for the same install.
    """
    key = _path_segment(_printer_key(printer_id))
    if state_dir is not None:
        base = Path(state_dir)
        path_for = lambda: base / "printers" / key / "control.lock"  # noqa: E731
        registry_key = (key, str(base))
    else:
        path_for = lambda: settings.printer_state_dir(key) / "control.lock"  # noqa: E731
        registry_key = (key, None)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(registry_key)
        if lock is None:
            lock = _LOCKS[registry_key] = PrinterControlLock(path_for)
        return lock
