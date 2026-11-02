"""The container's real CPU and process budget, and the limits derived from it.

A container started with ``--cpus 2`` gets a CFS quota, not a smaller CPU set, so
``os.cpu_count()`` and thread-pool defaults (TBB in OrcaSlicer, BLAS and OpenMP in
numpy and onnxruntime, ``ThreadPoolExecutor``) still see every host CPU. On a 72 CPU
host that is hundreds of threads in a container that also has a process limit, and at
the limit new threads and forks fail with EAGAIN and OrcaSlicer stalls until its timeout.

Policy: pools are capped only when a budget is known, either a cgroup CPU quota or the
``PRINT_HANDY_THREADS`` override. With neither, nothing is changed and pools keep their
defaults. The module is standard library only so the container entrypoint can run it
before anything else (``python3 print_handy/core/resources.py --exports``).
"""
import errno
import math
import os
import shutil
import sys
from pathlib import Path

THREADS_ENV = 'PRINT_HANDY_THREADS'
POOL_ENV_VARS = ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
                 'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS')
CGROUP_ROOT = Path('/sys/fs/cgroup')
PID_WARN_FRACTION = 0.8

# One stable phrase: it survives the worker-process boundary as message text, and
# diagnostics.known_cause recognises it.
PID_LIMIT_PHRASE = 'container is at its process limit'


class ResourceLimitError(RuntimeError):
    """A process or thread could not be created because the container's PID limit is reached."""
    diagnostic_category = 'configuration'


def _read(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, ValueError):
        return None


def cgroup_cpu_quota(root=None):
    """CPUs granted by a cgroup CPU quota (v2 ``cpu.max``, else v1 cfs files), or None when unlimited."""
    root = Path(CGROUP_ROOT if root is None else root)
    v2 = _read(root / 'cpu.max')
    if v2:
        parts = v2.split()
        if parts and parts[0] != 'max':
            try:
                quota, period = int(parts[0]), int(parts[1]) if len(parts) > 1 else 100000
            except ValueError:
                return None
            return quota / period if quota > 0 and period > 0 else None
        return None
    for base in (root / 'cpu', root / 'cpu,cpuacct'):
        quota, period = _read(base / 'cpu.cfs_quota_us'), _read(base / 'cpu.cfs_period_us')
        if quota is None or period is None:
            continue
        try:
            quota, period = int(quota), int(period)
        except ValueError:
            return None
        return quota / period if quota > 0 and period > 0 else None  # -1 means no quota
    return None


def thread_budget(environ=None, root=None):
    """Threads a pool may use, or None when no limit applies.

    ``PRINT_HANDY_THREADS`` wins; otherwise the cgroup CPU quota rounded up, never below 1 and
    never above the CPUs this process may actually run on.
    """
    environ = os.environ if environ is None else environ
    override = str(environ.get(THREADS_ENV, '')).strip()
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            pass
    quota = cgroup_cpu_quota(root)
    if quota is None:
        return None
    return max(1, min(math.ceil(quota), len(affinity_cpus())))


def affinity_cpus():
    getter = getattr(os, 'sched_getaffinity', None)
    if getter is not None:
        return sorted(getter(0))
    return list(range(os.cpu_count() or 1))


def pool_env(budget):
    """Environment variables that size numeric thread pools to ``budget`` ({} when unlimited)."""
    if not budget:
        return {}
    return {name: str(budget) for name in POOL_ENV_VARS}


def apply_to_environment(environ=None):
    """Cap thread pools of this process and its children. Call before numpy or onnxruntime is imported.

    A pool variable the operator already set is left alone. Returns the budget (None when unlimited).
    """
    environ = os.environ if environ is None else environ
    budget = thread_budget(environ)
    for name, value in pool_env(budget).items():
        environ.setdefault(name, value)
    return budget


def cap_default_executor(loop, budget=None):
    """Size the loop's default executor (asyncio.to_thread) to the budget plus the stdlib's 4 spare threads.

    Returns the worker count, or None when no budget applies and the stdlib default stays.
    """
    budget = thread_budget() if budget is None else budget
    if not budget:
        return None
    from concurrent.futures import ThreadPoolExecutor
    workers = min(32, budget + 4)
    loop.set_default_executor(ThreadPoolExecutor(max_workers=workers, thread_name_prefix='ph-default'))
    return workers


def child_env(base=None):
    """Environment for a child process: ``base`` (default: ours) with pool variables capped."""
    env = dict(os.environ if base is None else base)
    for name, value in pool_env(thread_budget(env)).items():
        env.setdefault(name, value)
    return env


def affinity_prefix(budget=None, cpus=None):
    """``taskset`` arguments restricting a child to ``budget`` CPUs, or [] when nothing needs limiting.

    TBB and ``hardware_concurrency`` follow the affinity mask, not the CFS quota, so this is what
    actually shrinks OrcaSlicer's worker pool.
    """
    budget = thread_budget() if budget is None else budget
    cpus = affinity_cpus() if cpus is None else list(cpus)
    taskset = shutil.which('taskset')
    if not budget or budget >= len(cpus) or not taskset:
        return []
    return [taskset, '-c', ','.join(str(c) for c in cpus[:budget])]


def slicer_command(command):
    """``command`` wrapped for the CPU budget (affinity mask), unchanged when unlimited."""
    return [*affinity_prefix(), *command]


# --- process (PID) limit ------------------------------------------------------------------

def pid_status(root=None):
    """(current, limit) from the cgroup pids controller; limit is None when unlimited; None if unknown."""
    root = Path(CGROUP_ROOT if root is None else root)
    current, limit = _read(root / 'pids.current'), _read(root / 'pids.max')
    if current is None or limit is None:
        # cgroup v1 keeps the pids controller in its own directory under
        # the root, with the same file names (the cpu quota above reads
        # its v1 directories the same way).
        current, limit = _read(root / 'pids' / 'pids.current'), _read(root / 'pids' / 'pids.max')
    if current is None or limit is None:
        return None
    try:
        return int(current), (None if limit == 'max' else int(limit))
    except ValueError:
        return None


def pid_limit_reached(root=None, margin=0):
    """True when the container cannot create another process."""
    status = pid_status(root)
    return bool(status and status[1] is not None and status[0] >= status[1] - margin)


def pid_limit_message(root=None):
    status = pid_status(root)
    detail = f' ({status[0]} of {status[1]} processes and threads in use)' if status and status[1] else ''
    return (f'Print Handy cannot start new processes: the {PID_LIMIT_PHRASE}{detail}. '
            'Remove the PID limit (--pids-limit / pids_limit) from the container, or raise it to 4096 or more.')


def pid_limit_error(root=None):
    """A ResourceLimitError when the container is at its PID limit right now, else None."""
    return ResourceLimitError(pid_limit_message(root)) if pid_limit_reached(root) else None


def is_spawn_exhaustion(exc):
    """True when ``exc`` is the OS refusing to create a process or thread."""
    if isinstance(exc, OSError):
        return exc.errno == errno.EAGAIN
    return isinstance(exc, RuntimeError) and "can't start new thread" in str(exc)


def explain_spawn_failure(exc, root=None):
    """A ResourceLimitError when ``exc`` is spawn exhaustion at the PID limit (or at EAGAIN), else None."""
    if is_spawn_exhaustion(exc) or pid_limit_reached(root):
        return ResourceLimitError(pid_limit_message(root))
    return None


def pid_report(root=None):
    """(text, warn) for the doctor: usage, warn above PID_WARN_FRACTION; None with no limit or unknown."""
    status = pid_status(root)
    if not status or status[1] is None:
        return None
    current, limit = status
    text = f'{current}/{limit} processes and threads in use'
    if current >= limit * PID_WARN_FRACTION:
        text += ' - near the container PID limit; remove --pids-limit or raise it to 4096 or more'
        return text, True
    return text, False


def exports(environ=None):
    """Shell ``export`` lines for the entrypoint (empty when no budget applies)."""
    environ = os.environ if environ is None else environ
    budget = thread_budget(environ)
    lines = []
    for name, value in pool_env(budget).items():
        if name not in environ:
            lines.append(f'export {name}={value}')
    return '\n'.join(lines)


if __name__ == '__main__':
    if '--exports' in sys.argv:
        text = exports()
        if text:
            print(text)
