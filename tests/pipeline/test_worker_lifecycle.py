"""Offline lifecycle tests for intake worker processes. Fake children only: no slicer, no printer.

A fake executable stands in for `python -m print_handy.core.heavy_worker`. It can hang, ignore
SIGTERM, start a grandchild in its own process group, flood stdout or write late into the intake
folder, so each test checks what the parent actually does with the child it started.
"""
import asyncio
import importlib
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from print_handy.core import model_job, orca_adapter, prepared_intake, source_colors
from print_handy.core.background_slice import BackgroundSlice


def test_heavy_worker_configures_logging_and_binds_the_forwarded_context(monkeypatch):
    # P2-55: the worker entrypoint installs the shared logging (its lines go
    # to stderr; stdout stays the JSON reply channel) and binds the log
    # context the parent forwarded in the request.
    import io
    import logging as _logging

    from print_handy import logs
    from print_handy.core import heavy_worker
    source = Path(__file__).parents[1] / 'corpus/cases/single-part/model.3mf'
    request = {'module': 'source_colors', 'func': 'inventory', 'args': [str(source)],
               'log_context': {'intake': 't-55'}}
    out = io.StringIO()
    root = _logging.getLogger()
    old_level = root.level
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(request)))
    monkeypatch.setattr(sys, 'stdout', out)
    try:
        heavy_worker.main()
        assert logs.bound_context() == {'intake': 't-55'}
        assert any(getattr(h, '_print_handy_handler', False) for h in root.handlers)
    finally:
        logs.clear_context()
        for handler in list(root.handlers):
            if getattr(handler, '_print_handy_handler', False):
                root.removeHandler(handler)
                handler.close()
        root.setLevel(old_level)
    reply = json.loads(out.getvalue().rstrip().rsplit('\n', 1)[-1])
    assert reply['ok'] is True

FAKE = r'''
import json, os, signal, subprocess, sys, time
mode, box = os.environ['FAKE_MODE'], os.environ['FAKE_DIR']
sys.stdin.read()
def note(name, text):
    with open(os.path.join(box, name), 'a') as f:
        f.write(text)
note('log', f'start {os.getpid()} {time.monotonic()}\n')
if mode == 'ok':
    time.sleep(float(os.environ.get('FAKE_SLEEP', '0')))
    note('log', f'end {os.getpid()} {time.monotonic()}\n')
    sys.stdout.write('\n' + json.dumps({'ok': True, 'path': False, 'result': 1}))
    sys.exit(0)
if mode == 'flood':
    note('pids', f'{os.getpid()}\n')
    for _ in range(64):
        sys.stdout.write('x' * 65536)
    sys.stdout.flush()
    time.sleep(30)
    sys.exit(0)
if mode == 'late_write':
    note('pids', f'{os.getpid()}\n')
    time.sleep(float(os.environ.get('FAKE_SLEEP', '2')))
    target = os.environ['FAKE_TARGET']
    os.makedirs(os.path.dirname(target), exist_ok=True)
    open(target, 'w').write('G1 X1\n')
    sys.stdout.write('\n' + json.dumps({'ok': True, 'path': False, 'result': [target]}))
    sys.exit(0)
ignore = mode == 'hang_ignore_term'
grandchild = subprocess.Popen([sys.executable, '-c',
    'import signal, time\n'
    + ('signal.signal(signal.SIGTERM, signal.SIG_IGN)\n' if ignore else '')
    + 'time.sleep(30)'])
if ignore:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    def on_term(*_):
        note('term', f'{os.getpid()}\n')
        grandchild.terminate()
        os._exit(0)
    signal.signal(signal.SIGTERM, on_term)
note('pids', f'{os.getpid()}\n{grandchild.pid}\n')
time.sleep(30)
'''


def _alive(pid):
    """True while pid is a live (not zombie) process."""
    try:
        state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False
    return state not in ('Z', 'X')


class Fake:
    def __init__(self, tmp_path, monkeypatch):
        self.box = tmp_path / 'fake-box'
        self.box.mkdir()
        script = tmp_path / 'fake-worker'
        script.write_text(f'#!{sys.executable}\n' + FAKE)
        script.chmod(0o755)
        self.monkeypatch = monkeypatch
        # _heavy launches `sys.executable -m print_handy.core.heavy_worker`; the fake ignores the args.
        monkeypatch.setattr(sys, 'executable', str(script))
        monkeypatch.setenv('FAKE_DIR', str(self.box))

    def mode(self, mode, **env):
        self.monkeypatch.setenv('FAKE_MODE', mode)
        for key, value in env.items():
            self.monkeypatch.setenv(key, str(value))

    def limits(self, **values):
        """Shorten worker limits. Before the owned worker exists this sets nothing, so the test fails on behavior."""
        try:
            ow = importlib.import_module('print_handy.core.owned_worker')
        except ImportError:
            return
        for name, value in values.items():
            target, _, attr = name.rpartition('.')
            self.monkeypatch.setattr(getattr(ow, target) if target else ow, attr, value)

    def pids(self):
        path = self.box / 'pids'
        return [int(p) for p in path.read_text().split()] if path.exists() else []

    def intervals(self):
        rows = {}
        for line in (self.box / 'log').read_text().splitlines():
            kind, pid, when = line.split()
            rows.setdefault(pid, {})[kind] = float(when)
        return sorted((r['start'], r.get('end')) for r in rows.values())

    async def started(self, count=1):
        for _ in range(400):
            if len(self.pids()) >= count:
                return
            await asyncio.sleep(0.025)
        raise AssertionError('fake worker never started')


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = Fake(tmp_path, monkeypatch)
    yield f
    for pid in f.pids():  # never leave a fake behind, whatever the code under test did
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _intake(tmp_path):
    folder = tmp_path / 'archive' / 'ghost-intake'
    folder.mkdir(parents=True)
    job_path = folder / 'JOB.json'
    job_path.write_text('{}')
    return folder, job_path


def _wait_gone(pids, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end and any(_alive(p) for p in pids):
        time.sleep(0.02)
    return [p for p in pids if _alive(p)]


def test_deadline_terminates_then_kills_and_reaps_the_whole_process_group(fake):
    fake.mode('hang_ignore_term')
    fake.limits(DEADLINE_S=0.5, GRACE_S=0.3)

    async def run():
        return await asyncio.wait_for(prepared_intake._heavy(source_colors.inventory, 'model.3mf'), 8)

    began = time.monotonic()
    with pytest.raises(RuntimeError, match='did not finish within') as caught:
        asyncio.run(run())
    pids = fake.pids()
    assert len(pids) == 2  # the worker and the grandchild it started
    assert _wait_gone(pids, 0.5) == []
    # SIGTERM was ignored, so the message says the group was force-stopped, not just "stopped".
    assert 'force-stopped' in str(caught.value)
    assert time.monotonic() - began < 5


def test_cancel_terminates_the_group_and_waits_for_it(fake):
    fake.mode('hang_obey_term')
    fake.limits(GRACE_S=2.0)

    async def run():
        task = asyncio.ensure_future(prepared_intake._heavy(source_colors.inventory, 'model.3mf'))
        await fake.started()
        began = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.monotonic() - began

    elapsed = asyncio.run(asyncio.wait_for(run(), 8))
    assert (fake.box / 'term').exists()  # asked to stop first, and it did within the grace period
    assert elapsed < 2.0
    assert _wait_gone(fake.pids(), 0.5) == []


def test_shutdown_stops_every_running_worker(fake):
    fake.mode('hang_obey_term')
    fake.limits(GRACE_S=2.0)
    from print_handy.core import owned_worker

    async def run():
        task = asyncio.ensure_future(prepared_intake._heavy(source_colors.inventory, 'model.3mf'))
        await fake.started()
        await owned_worker.shutdown()
        with pytest.raises(RuntimeError, match='shutting down'):
            await task

    asyncio.run(asyncio.wait_for(run(), 8))
    assert (fake.box / 'term').exists()
    assert _wait_gone(fake.pids(), 0.5) == []


def test_worker_output_is_size_limited(fake):
    fake.mode('flood')
    fake.limits(OUTPUT_LIMIT=1 << 20, GRACE_S=0.3)

    async def run():
        return await asyncio.wait_for(prepared_intake._heavy(source_colors.inventory, 'model.3mf'), 8)

    with pytest.raises(RuntimeError, match='output limit'):
        asyncio.run(run())
    assert _wait_gone(fake.pids(), 0.5) == []


def test_discarded_background_slice_is_stopped_and_cannot_write_after_cleanup(fake, tmp_path):
    folder, job_path = _intake(tmp_path)
    late = folder / 'sliced' / 'orca' / 'late' / 'plate_1.gcode'
    fake.mode('late_write', FAKE_SLEEP=2.0, FAKE_TARGET=late)
    fake.limits(GRACE_S=0.5)

    async def run():
        runner = BackgroundSlice()
        await runner.start(job_path, 'decisions', lambda jp, work, _runner:
                           prepared_intake._heavy(model_job.slice_job, str(jp)))
        await fake.started()
        began = time.monotonic()
        await runner.discard()
        elapsed = time.monotonic() - began
        await asyncio.sleep(2.5)  # past the moment the fake would have written
        return elapsed

    elapsed = asyncio.run(asyncio.wait_for(run(), 10))
    assert elapsed < 1.5  # discard stops the worker; it does not wait for the slice to finish
    assert not late.exists()
    assert not (folder / 'sliced').exists()
    assert _wait_gone(fake.pids(), 0.5) == []
    assert not (folder / 'WORKER.json').exists()  # a confirmed stop leaves no interrupted marker


def test_discard_removes_only_what_the_cancelled_run_created(tmp_path):
    folder, job_path = _intake(tmp_path)
    keep = folder / 'sliced' / 'keep.gcode'
    older = folder / 'sliced' / 'orca' / 'older' / 'plate_1.gcode'
    for path in (keep, older):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('G1 X0\n')

    async def run():
        runner = BackgroundSlice()
        wrote = asyncio.Event()

        async def slice_func(jp, work, _runner):
            out = work / 'orca' / 'run-1' / 'plate_1.gcode'
            out.parent.mkdir(parents=True)
            out.write_text('G1 X1\n')
            wrote.set()
            await asyncio.sleep(30)

        await runner.start(job_path, 'decisions', slice_func)
        await wrote.wait()
        await runner.discard()

    asyncio.run(asyncio.wait_for(run(), 8))
    assert not (folder / 'sliced' / 'orca' / 'run-1').exists()
    assert keep.exists() and older.exists()


def test_slicer_steps_run_one_at_a_time(fake, tmp_path):
    folder, job_path = _intake(tmp_path)
    fake.mode('ok', FAKE_SLEEP=0.4)

    async def run():
        await asyncio.gather(prepared_intake._heavy(model_job.slice_job, str(job_path)),
                             prepared_intake._heavy(orca_adapter.fit_options, folder / 'model.3mf', {}),
                             prepared_intake._heavy(orca_adapter.orca_plate_capacity, folder / 'model.3mf', {}))

    asyncio.run(asyncio.wait_for(run(), 10))
    runs = fake.intervals()
    assert len(runs) == 3
    for (_, end), (start, _) in zip(runs, runs[1:]):
        assert end <= start  # concurrent Flatpak Orca runs collide (orca_adapter, exit 243)


def test_slicer_queue_is_bounded(fake, tmp_path):
    folder, job_path = _intake(tmp_path)
    fake.mode('ok', FAKE_SLEEP=0.6)
    fake.limits(**{'SLICER.waiting': 1})

    async def run():
        steps = [prepared_intake._heavy(model_job.slice_job, str(job_path)) for _ in range(4)]
        return await asyncio.gather(*steps, return_exceptions=True)

    results = asyncio.run(asyncio.wait_for(run(), 10))
    busy = [r for r in results if isinstance(r, RuntimeError) and 'busy' in str(r)]
    assert len(busy) == 2, results  # one running, one waiting, the rest refused at once
    assert sum(1 for r in results if r == 1) == 2


def test_running_slice_records_only_what_explains_an_interruption(fake, tmp_path):
    folder, job_path = _intake(tmp_path)
    fake.mode('hang_obey_term')
    fake.limits(GRACE_S=2.0)

    async def run():
        task = asyncio.ensure_future(prepared_intake._heavy(model_job.slice_job, str(job_path)))
        await fake.started()
        record = json.loads((folder / 'WORKER.json').read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return record

    record = asyncio.run(asyncio.wait_for(run(), 8))
    assert record['state'] == 'running' and record['step'] == 'slicing'
    assert set(record) <= {'state', 'step', 'started_at', 'worker_pid', 'owner'}
    assert not (folder / 'WORKER.json').exists()


def test_interrupted_preparation_is_explained_and_its_approval_is_not_reused(tmp_path):
    folder, job_path = _intake(tmp_path)
    saved = {'intake_id': 'old', 'title': 'Ghost Stand', 'downloaded_file': str(folder / 'source' / 'ghost.3mf'),
             'selected_source': {'name': 'ghost.3mf'}, 'color_decisions': {'mode': 'single_color'},
             'slice_approval': {'attempt': 1}, 'bed_clear_approval': {'gcode': 'x'}}
    (folder / 'source').mkdir()
    (folder / 'source' / 'ghost.3mf').write_bytes(b'3mf')
    job_path.write_text(json.dumps(saved))
    # Written by a Print Handy process that is no longer running.
    (folder / 'WORKER.json').write_text(json.dumps({'state': 'running', 'step': 'slicing',
                                                     'started_at': 1.0, 'worker_pid': 1, 'owner': 'previous-run'}))
    note = prepared_intake.interrupted_note(saved)
    assert note and 'interrupted' in note and 'slicing' in note
    for fresh in (prepared_intake.reuse_job(saved), prepared_intake.resume_job(saved)):
        assert not {'slice_approval', 'bed_clear_approval'} & set(fresh)
