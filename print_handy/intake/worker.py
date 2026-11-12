"""Intake work that runs off the chat loop: the heavy-step child process, its log forwarding,
the background slice wait and the prefetch.

Moved verbatim from ``print_handy.core.prepared_intake`` (#1323). The old names are still
importable from there for one release.
"""
import asyncio, hashlib, json, logging, re, sys
from pathlib import Path

from .. import logs, settings
from ..core import owned_worker

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy.core.prepared_intake")


_OFFLOAD = {('model_job', 'audit'), ('model_job', 'slice_job'), ('source_colors', 'inventory'),
            ('orca_adapter', 'orca_plate_capacity'), ('orca_adapter', 'fit_options'),
            ('orca_adapter', 'largest_fitting_fraction'),
            ('model_job', 'per_part_fit_options'), ('plate_merge', 'full_size_split_possible'),
            ('geometry_layout', 'inspect_geometry'), ('geometry_layout', 'arrange_and_export'),
            ('geometry_layout', 'probe_single_plate_rearrange'), ('model_job', 'full_size_one_plate'),
            ('cad_split', 'check_source_against_split_plan'), ('cad_split', 'prepare_cad_split'),
            ('model_parts', 'combine'),
            # The painted-plate fit (#1153 item 8): pure-Python 3MF geometry export at each ladder
            # scale. In a thread it held the GIL for whole stretches of a chat interaction.
            ('model_job', 'painted_fit_for_job')}
# Steps that moved out of their offload module (#1323) keep its name: the worker
# imports print_handy.core.<module>, which still re-exports them.
_OFFLOAD_HOMES = {'print_handy.jobs.slice': 'model_job', 'print_handy.jobs.audit': 'model_job'}
# Steps that run the slicer share one lane; the geometry-only steps share a small general lane.
_SLICER_STEPS = {('model_job', 'slice_job'), ('orca_adapter', 'orca_plate_capacity'), ('orca_adapter', 'fit_options'),
                 ('orca_adapter', 'largest_fitting_fraction')}
_STEP_LABELS = {'slice_job': 'slicing', 'audit': 'the model check', 'inventory': 'the color check',
                'orca_plate_capacity': 'the bed fit check', 'fit_options': 'the bed fit check',
                'largest_fitting_fraction': 'the bed fit check',
                'per_part_fit_options': 'the per-part fit check', 'full_size_split_possible': 'the split check',
                'inspect_geometry': 'the geometry check', 'arrange_and_export': 'the layout',
                'probe_single_plate_rearrange': 'the layout check', 'combine': 'combining the files',
                'check_source_against_split_plan': 'the split check', 'prepare_cad_split': 'the split',
                'painted_fit_for_job': 'the bed fit check'}


async def _await_background_slice(runner, binding):
    """Await the background slice without losing a cancellation (P2-58).

    BackgroundSlice.take() absorbs CancelledError — it waits out its own
    slice task — so a Stop delivered as a cancellation during the wait
    would be swallowed and the intake would carry on as if the slice had
    simply found nothing. Re-raise it, so _with_stop's race turns the
    press back into the operator's Stop.
    """
    result = await runner.take(binding)
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError
    return result


def _prefetch(work):
    """Start ``work`` now and return its task, to be awaited (or abandoned) later.

    An intake that ends before awaiting it (cancel, timeout, a refusal) must not log
    "Task exception was never retrieved", so the outcome is always marked as read.
    """
    task = asyncio.ensure_future(work)
    task.add_done_callback(lambda done: done.cancelled() or done.exception())
    return task


_WORKER_LOG_LINE = re.compile(r'^\S+\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(.*)$')
_LOG_LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')


def _forward_worker_log_lines(err):
    """Re-emit a heavy worker's stderr through this process's logging (P2-55).

    The worker configures the shared logging, so its lines are already
    formatted, timestamped and redacted; forwarding keeps them in the
    operator's log instead of dropping them whenever the JSON reply parses.
    A line that does not start a formatted record (a traceback continuation)
    follows the previous line's level, as does unformatted noise. The worker's
    timestamp and level are dropped from the message: this logger adds its own
    (2.2.5 review).
    """
    if not err:
        return
    level = logging.INFO
    for raw in err.decode(errors='replace').splitlines():
        line = raw.strip()
        if not line:
            continue
        message = line
        match = _WORKER_LOG_LINE.match(line)
        if match:
            level = getattr(logging, match.group(1))
            message = match.group(2)
        elif line.startswith('{'):
            try:
                record = json.loads(line)
            except ValueError:
                record = None
            if isinstance(record, dict) and str(record.get('level', '')).upper() in _LOG_LEVELS:
                level = getattr(logging, str(record['level']).upper())
                if isinstance(record.get('message'), str):
                    message = record['message']
        logger.log(level, '%s', message)


_WORKER_ARGV = ('-m', 'print_handy.core.heavy_worker')


def _worker_argv(warm=False):
    """The heavy worker command; ``warm`` is the pre-started spare form (#1332)."""
    return [sys.executable, *_WORKER_ARGV, *(['--warm'] if warm else [])]


def _settings_stamp():
    """What a worker reads at import: the parent's merged settings (#1332).

    A warm spare started before a settings change would run on the old
    values, so a spare is used only while this stamp is unchanged.
    """
    return hashlib.sha256(json.dumps(settings.current(), sort_keys=True, default=str).encode()).hexdigest()


def enable_warm_worker():
    """Keep one warm heavy worker ready on the running loop (#1332).

    The bot runtimes call this once their loop is up; the first spare starts
    now. Every step still runs in its own single-use process (see
    owned_worker's warm spare notes), so isolation is unchanged; only the
    0.6 to 1.1 s of start and imports leaves the user's wait.
    """
    owned_worker.enable_warm_spare(_settings_stamp, _worker_argv(warm=True), str(settings.ROOT))


async def _heavy(func, *args, **kwargs):
    """Run CPU-heavy intake work in a child process (own GIL) so Discord buttons answer in time.

    Threads are not enough: large 3MF XML parsing holds the GIL for ~10 s stretches and starves the
    gateway loop past Discord's 3 s interaction deadline. Substituted callables (tests) run in a thread.
    The child is owned: it has a deadline and an output limit, and a cancel stops its process group.
    """
    import importlib
    mod, name = getattr(func, '__module__', None), getattr(func, '__name__', None)
    short = _OFFLOAD_HOMES.get(mod, (mod or '').rsplit('.', 1)[-1])
    real = (short, name) in _OFFLOAD and getattr(importlib.import_module(mod), name, None) is func
    if not real:
        return await owned_worker.in_thread(func, *args, **kwargs)
    def plain(v):
        return str(v) if isinstance(v, Path) else v
    payload = json.dumps({'module': short, 'func': name, 'args': [plain(a) for a in args],
                          'kwargs': {k: plain(v) for k, v in kwargs.items()},
                          'log_context': logs.bound_context()}).encode()
    slicer = (short, name) in _SLICER_STEPS
    # A slice worker leaves a record in its intake folder, so a restart mid-slice can be explained.
    record = Path(args[0]).parent / owned_worker.RECORD if (short, name) == ('model_job', 'slice_job') else None
    out, err = await owned_worker.run(
        _worker_argv(), payload, label=_STEP_LABELS.get(name, name),
        lane=owned_worker.SLICER if slicer else owned_worker.GENERAL, cwd=str(settings.ROOT), record=record,
        warm_argv=_worker_argv(warm=True))
    _forward_worker_log_lines(err)
    try:
        reply = json.loads(out.decode().rstrip().rsplit('\n', 1)[-1])
    except (ValueError, IndexError):
        detail = err.decode(errors="replace")[-400:].strip() or 'it stopped without reporting why (killed or crashed)'
        raise RuntimeError(f'{name} worker failed: {detail}') from None
    if not reply.get('ok'):
        from ..core.worker_errors import decode
        if reply.get('traceback'):
            # The parent's own traceback ends at this raise; log where the child failed.
            logger.error('%s worker raised %s; worker traceback:\n%s',
                         name, reply.get('type') or 'an error', reply['traceback'].rstrip())
        raise decode(reply)
    return Path(reply['result']) if reply.get('path') else reply['result']
