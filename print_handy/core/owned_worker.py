"""Child processes the intake owns: a deadline, an output limit, a bounded queue, and a clean stop.

Each worker runs in its own process group, so a slicer it starts (flatpak, Orca) belongs to the same
group. A deadline, a cancel or a shutdown sends SIGTERM to the whole group, waits GRACE_S, then sends
SIGKILL and waits again until no member is left. Messages say which of those actually happened.

While a slice worker runs, a small WORKER.json in the intake folder says so. If Print Handy restarts
mid-slice the file stays behind and explains the interruption. It never holds an approval: a
preparation that was interrupted is started again only from fresh confirmations.
"""
import asyncio
import contextlib
import json
import os
import signal
import time
import uuid
import weakref
from pathlib import Path

from . import resources

DEADLINE_S = 60 * 60      # upper bound for one heavy step; slice_job may run Orca (600 s limit) per plate
GRACE_S = 10.0            # between SIGTERM and SIGKILL
REAP_S = 10.0             # after SIGKILL, before the stop is reported unconfirmed
OUTPUT_LIMIT = 64 << 20   # stdout bytes; the result is one JSON line
STDERR_TAIL = 64 << 10    # stderr is only kept for the error message
RECORD = 'WORKER.json'
_OWNER = uuid.uuid4().hex  # this Print Handy process; a record from another owner was interrupted


class WorkerError(RuntimeError):
    """A worker did not produce a result. The message says how it was stopped."""


class WorkerBusy(WorkerError):
    pass


class Lane:
    """At most `slots` workers at once and `waiting` more queued; anything beyond is refused at once."""

    def __init__(self, name, slots, waiting):
        self.name, self.slots, self.waiting = name, slots, waiting
        self._state = weakref.WeakKeyDictionary()  # per event loop: [semaphore, queued]

    @contextlib.asynccontextmanager
    async def slot(self):
        state = self._state.setdefault(asyncio.get_running_loop(), [asyncio.Semaphore(self.slots), 0])
        sem = state[0]
        if sem.locked():
            if state[1] >= self.waiting:
                raise WorkerBusy(f'Print Handy is busy with {self.slots + state[1]} other {self.name} '
                                 'steps; try again when one finishes')
            state[1] += 1
            try:
                await sem.acquire()
            finally:
                state[1] -= 1
        else:
            await sem.acquire()
        try:
            yield
        finally:
            sem.release()


# Concurrent Flatpak Orca runs collide (orca_adapter), so steps that can launch it run one at a time.
SLICER = Lane('slicing', slots=1, waiting=4)
GENERAL = Lane('model check', slots=2, waiting=8)

_LIVE = {}  # pid -> process, for shutdown()
_SHUTTING_DOWN = set()
# Event loops on which shutdown() has begun. Admission closes with shutdown:
# stopping the running workers frees lane slots, and a queued run must not
# spend a freed slot on a new process nothing will stop (P2-51, P2-54).
_CLOSED = weakref.WeakKeyDictionary()


def _admission_closed():
    """True once shutdown() has begun on this event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    return bool(_CLOSED.get(loop))


def _members(pgid):
    """Live (non-zombie) processes in the group. Falls back to a signal probe without /proc."""
    proc = Path('/proc')
    if not proc.is_dir():
        try:
            os.killpg(pgid, 0)
            return [pgid]
        except (ProcessLookupError, PermissionError):
            return []
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
        except (OSError, IndexError):
            continue
        if fields[0] not in ('Z', 'X') and int(fields[2]) == pgid:
            found.append(int(entry.name))
    return found


def _signal(pgid, sig):
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


async def _gone(proc, seconds):
    end = time.monotonic() + seconds
    while True:
        if proc.returncode is None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(proc.wait()), 0.05)
        if proc.returncode is not None and not _members(proc.pid):
            return True
        if time.monotonic() >= end:
            return False
        await asyncio.sleep(0.05)


async def stop(proc):
    """Stop the worker's whole process group. Returns 'exited', 'stopped', 'force-stopped' or 'unconfirmed'."""
    if proc.returncode is not None and not _members(proc.pid):
        return 'exited'
    _signal(proc.pid, signal.SIGTERM)
    if await _gone(proc, GRACE_S):
        return 'stopped'
    _signal(proc.pid, signal.SIGKILL)
    return 'force-stopped' if await _gone(proc, REAP_S) else 'unconfirmed'


def _span(seconds):
    return f'{seconds / 60:g} min' if seconds >= 60 else f'{seconds:g} s'


def _described(how):
    return {'exited': 'had already exited', 'stopped': 'was stopped',
            'force-stopped': f'was force-stopped after ignoring a stop request for {_span(GRACE_S)}',
            'unconfirmed': 'could not be confirmed stopped'}[how]


async def _stop_through_cancel(proc):
    """Finish stopping even if the caller is cancelled again meanwhile; the stop itself is bounded."""
    stopping = asyncio.ensure_future(stop(proc))
    cancelled = False
    while not stopping.done():
        try:
            await asyncio.shield(stopping)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return stopping.result()


async def _read(stream, limit, tail=False):
    data = bytearray()
    while chunk := await stream.read(65536):
        data += chunk
        if len(data) > limit:
            if not tail:
                raise WorkerError('output limit')
            del data[:-limit]
    return bytes(data)


async def _exchange(proc, payload):
    async def feed():
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            proc.stdin.write(payload)
            await proc.stdin.drain()
        proc.stdin.close()
    out, err, _ = await asyncio.gather(_read(proc.stdout, OUTPUT_LIMIT), _read(proc.stderr, STDERR_TAIL, tail=True),
                                       feed())
    await proc.wait()
    return out, err


def _write_record(path, data):
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(data) + '\n')
    os.replace(tmp, path)


async def run(argv, payload, *, label, lane, cwd=None, record=None):
    """Run argv with payload on stdin; return (stdout, stderr). Raises WorkerError when stopped early."""
    async with lane.slot():
        if _admission_closed():
            raise WorkerError(f'{label} was not started because Print Handy is shutting down')
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, start_new_session=True, env=resources.child_env(),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as exc:
            # EAGAIN here is the container's PID limit, not a transient hiccup: name it.
            limit = resources.explain_spawn_failure(exc)
            if limit is not None:
                raise WorkerError(f'{label} could not start: {limit}') from exc
            raise
        _LIVE[proc.pid] = proc
        how, started = None, time.time()
        try:
            if _admission_closed():
                # shutdown() began while this process was spawning, after its
                # snapshot was taken: stop the newcomer here instead.
                how = await _stop_through_cancel(proc)
                raise WorkerError(f'{label} was interrupted because Print Handy is shutting down; '
                                  'nothing from it was kept')
            if record is not None:
                _write_record(record, {'state': 'running', 'step': label, 'started_at': started,
                                       'worker_pid': proc.pid, 'owner': _OWNER})
            try:
                out, err = await asyncio.wait_for(_exchange(proc, payload), DEADLINE_S)
            except asyncio.TimeoutError:
                how = await _stop_through_cancel(proc)
                raise WorkerError(f'{label} did not finish within {_span(DEADLINE_S)}; '
                                  f'its worker {_described(how)}') from None
            except WorkerError:
                how = await _stop_through_cancel(proc)
                raise WorkerError(f'{label} exceeded the {OUTPUT_LIMIT >> 20} MiB output limit; '
                                  f'its worker {_described(how)}') from None
            # The worker exited; anything it left in its group is still ours to stop.
            how = await _stop_through_cancel(proc)
            if proc.pid in _SHUTTING_DOWN:
                raise WorkerError(f'{label} was interrupted because Print Handy is shutting down; '
                                  'nothing from it was kept')
            return out, err
        except BaseException:  # cancelled (Stop, discard, shutdown) or anything unexpected
            if how is None:
                how = await _stop_through_cancel(proc)
            raise
        finally:
            _LIVE.pop(proc.pid, None)
            _SHUTTING_DOWN.discard(proc.pid)
            if record is not None:
                with contextlib.suppress(OSError):
                    if how in (None, 'unconfirmed'):
                        _write_record(record, {'state': 'stop_unconfirmed', 'step': label, 'started_at': started,
                                               'worker_pid': proc.pid, 'owner': _OWNER})
                    else:
                        record.unlink(missing_ok=True)


async def shutdown():
    """Stop every running worker and close admission, so a queued run cannot
    start a new worker afterwards. Running workers' callers raise WorkerError
    instead of returning a result."""
    _CLOSED[asyncio.get_running_loop()] = True
    procs = list(_LIVE.values())
    _SHUTTING_DOWN.update(p.pid for p in procs)
    await asyncio.gather(*(stop(p) for p in procs), return_exceptions=True)


def interrupted(folder):
    """The record a previous Print Handy process left in this intake folder, or None."""
    try:
        data = json.loads((Path(folder) / RECORD).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get('owner') != _OWNER else None


def clear(folder):
    with contextlib.suppress(OSError):
        (Path(folder) / RECORD).unlink(missing_ok=True)


async def in_thread(func, *args, **kwargs):
    """Run func in a thread. A thread cannot be stopped, so a cancel waits for it before re-raising."""
    work = asyncio.ensure_future(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        await settle(work)
        raise


async def settle(fut):
    """Wait until fut is done, whatever cancels arrive meanwhile. Its result or error is dropped."""
    while not fut.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait({fut})
    if not fut.cancelled():
        fut.exception()
