"""The warm heavy-worker spare (#1332, the #1153 follow-up). Fake children only.

A cold heavy step spends 0.6 to 1.1 s starting Python and importing before
it works. The spare is the same single-use worker started ahead of time:
it imports, waits for one request, serves it and exits. These tests check
that a step is served by the pre-started process, that every step still
gets its own process, that a stale spare is stopped instead of used, that
shutdown leaves nothing behind, and that loops which did not ask for a
spare never get one.
"""
import asyncio
import io
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from print_handy.core import heavy_worker, owned_worker, prepared_intake, source_colors

posix_only = pytest.mark.skipif(os.name == "nt", reason="worker process groups are POSIX")

FAKE = r'''
import json, os, sys, time
box = os.environ['FAKE_DIR']
warm = '--warm' in sys.argv
with open(os.path.join(box, 'born'), 'a') as f:
    f.write(f'{os.getpid()} {int(warm)} {time.monotonic()}\n')
sys.stdin.read()
with open(os.path.join(box, 'served'), 'a') as f:
    f.write(f'{os.getpid()} {time.monotonic()}\n')
sys.stdout.write('\n' + json.dumps({'ok': True, 'path': False, 'result': os.getpid()}))
'''


def _alive(pid):
    try:
        state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False
    return state not in ('Z', 'X')


class Box:
    def __init__(self, tmp_path, monkeypatch):
        self.dir = tmp_path / 'box'
        self.dir.mkdir()
        script = tmp_path / 'fake-worker'
        script.write_text(f'#!{sys.executable}\n' + FAKE)
        script.chmod(0o755)
        monkeypatch.setattr(sys, 'executable', str(script))
        monkeypatch.setenv('FAKE_DIR', str(self.dir))

    def born(self):
        path = self.dir / 'born'
        rows = [line.split() for line in path.read_text().splitlines()] if path.exists() else []
        return [(int(pid), warm == '1', float(t)) for pid, warm, t in rows]

    async def wait_born(self, count):
        for _ in range(400):
            if len(self.born()) >= count:
                return
            await asyncio.sleep(0.025)
        raise AssertionError(f'expected {count} worker starts, saw {self.born()}')


@pytest.fixture
def box(tmp_path, monkeypatch):
    b = Box(tmp_path, monkeypatch)
    yield b
    for pid, _warm, _t in b.born():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _step():
    return prepared_intake._heavy(source_colors.inventory, 'model.3mf')


@posix_only
def test_a_step_is_served_by_the_spare_started_before_it(box):
    async def run():
        prepared_intake.enable_warm_worker()
        await box.wait_born(1)  # the first spare starts when the runtime enables it
        spare_pid, warm, born_at = box.born()[0]
        assert warm
        asked_at = time.monotonic()
        served_by = await asyncio.wait_for(_step(), 10)
        assert served_by == spare_pid and born_at < asked_at
        await box.wait_born(2)  # the next spare starts at once
        second = await asyncio.wait_for(_step(), 10)
        assert second == box.born()[1][0] != spare_pid  # one process per step, never reused
        await box.wait_born(3)
        leftover = box.born()[2][0]
        await owned_worker.shutdown()
        assert not owned_worker._LIVE, 'shutdown left the idle spare registered'
        return leftover

    leftover = asyncio.run(run())
    assert not _alive(leftover), 'shutdown left the idle spare running'


@posix_only
def test_a_spare_from_older_settings_is_stopped_not_used(box, monkeypatch):
    stamps = iter(['before', 'after', 'after', 'after'])

    async def run():
        owned_worker.enable_warm_spare(lambda: next(stamps), prepared_intake._worker_argv(warm=True),
                                       str(prepared_intake.settings.ROOT))
        await box.wait_born(1)
        stale = box.born()[0][0]
        served_by = await asyncio.wait_for(_step(), 10)
        cold = [pid for pid, warm, _t in box.born() if not warm]
        await owned_worker.shutdown()
        assert not owned_worker._LIVE
        return stale, served_by, cold

    stale, served_by, cold = asyncio.run(run())
    assert served_by != stale and served_by in cold
    deadline = time.monotonic() + 3
    while _alive(stale) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _alive(stale), 'the stale spare was left running'


@posix_only
def test_shutdown_stops_and_unregisters_an_idle_spare(box):
    # CI on the first head of #1335: shutdown stopped the idle spare but left
    # it in _LIVE (only run()'s finally unregisters a worker), so the next
    # test's `assert not owned_worker._LIVE` saw a dead spare.
    async def run():
        prepared_intake.enable_warm_worker()
        await box.wait_born(1)
        await owned_worker.shutdown()
        return box.born()[0][0]

    spare = asyncio.run(run())
    assert not owned_worker._LIVE
    assert not _alive(spare)


@posix_only
def test_a_loop_that_did_not_ask_gets_no_spare(box):
    async def run():
        served_by = await asyncio.wait_for(_step(), 10)
        await asyncio.sleep(0.3)
        return served_by

    served_by = asyncio.run(run())
    assert [(pid, warm) for pid, warm, _t in box.born()] == [(served_by, False)]


def test_the_warm_worker_imports_every_step_module_before_reading_its_request(monkeypatch):
    order = []
    monkeypatch.setattr(heavy_worker, 'preload', lambda: order.append('preload'))
    request = {'module': 'source_colors', 'func': 'inventory',
               'args': [str(Path(__file__).parents[1] / 'corpus/cases/single-part/model.3mf')]}

    class Stdin(io.StringIO):
        def read(self, *a):
            order.append('read')
            return super().read(*a)

    out = io.StringIO()
    monkeypatch.setattr(sys, 'stdin', Stdin(json.dumps(request)))
    monkeypatch.setattr(sys, 'stdout', out)
    heavy_worker.main(['--warm'])
    assert order == ['preload', 'read']
    order.clear()
    monkeypatch.setattr(sys, 'stdin', Stdin(json.dumps(request)))
    heavy_worker.main([])  # a cold worker imports only its own step
    assert order == ['read']


def test_preload_imports_the_allowed_modules():
    heavy_worker.preload()
    for module in {m for m, _f in heavy_worker.ALLOWED}:
        assert f'print_handy.core.{module}' in sys.modules


def test_only_run_bot_turns_the_spare_on():
    # Test loops build clients directly and must never get a spare.
    root = Path(__file__).parents[2] / 'print_handy' / 'bot'
    for name in ('client.py', 'telegram.py', 'matrix.py'):
        text = (root / name).read_text(encoding='utf-8')
        run_bot = text[text.index('def run_bot('):]
        assert 'warm_worker = True' in run_bot, name
        assert 'getattr(self, "warm_worker", False)' in text, name
