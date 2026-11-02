"""#734 start progress: while print-now runs, the intake relays the child's TIMING.jsonl
stages as at most two short notes, without changing how the start is awaited or stopped."""
import asyncio
import json
from types import SimpleNamespace as S

import pytest

from print_handy.core import prepared_intake

UPLOADED, ACCEPTED = (text for _stage, _grace, text in prepared_intake.START_PROGRESS)


class Adapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, metadata=None):
        self.sent.append(content)
        return S(success=True)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(prepared_intake, 'START_PROGRESS_POLL_S', 0.01)
    monkeypatch.setattr(prepared_intake, 'START_PROGRESS', tuple(
        (stage, 0.05, text) for stage, _grace, text in prepared_intake.START_PROGRESS))


def log(job_dir, stage):
    with (job_dir / 'TIMING.jsonl').open('a') as out:
        out.write(json.dumps({'stage': stage, 't': 0}) + '\n')


def run(job_dir, adapter, script):
    async def work():
        return await script()
    return asyncio.run(prepared_intake._with_start_progress(
        adapter, S(chat_id='c1'), {}, job_dir / 'JOB.json', work()))


def test_fast_start_posts_nothing(tmp_path):
    adapter = Adapter()

    async def script():
        log(tmp_path, 'upload_verified')
        log(tmp_path, 'start_submitted')
        return True, 'PRINT STARTED'
    assert run(tmp_path, adapter, script) == (True, 'PRINT STARTED')
    assert adapter.sent == []


def test_slow_upload_then_slow_preparation_posts_both_notes_once(tmp_path):
    adapter = Adapter()

    async def script():
        log(tmp_path, 'upload_verified')
        await asyncio.sleep(0.2)
        log(tmp_path, 'start_submitted')
        await asyncio.sleep(0.2)
        return True, 'preparing'
    run(tmp_path, adapter, script)
    assert adapter.sent == [UPLOADED, ACCEPTED]


def test_quick_acceptance_skips_the_stale_upload_note(tmp_path):
    adapter = Adapter()

    async def script():
        log(tmp_path, 'upload_verified')
        log(tmp_path, 'start_submitted')
        await asyncio.sleep(0.2)
        return True, 'preparing'
    run(tmp_path, adapter, script)
    assert adapter.sent == [ACCEPTED]


def test_stages_from_an_earlier_attempt_are_ignored(tmp_path):
    log(tmp_path, 'upload_verified')
    log(tmp_path, 'start_submitted')
    adapter = Adapter()

    async def script():
        await asyncio.sleep(0.2)
        return False, 'REFUSED: printer busy'
    assert run(tmp_path, adapter, script) == (False, 'REFUSED: printer busy')
    assert adapter.sent == []


def test_cancellation_still_reaches_the_start_and_stops_the_watcher(tmp_path):
    adapter = Adapter()
    seen = []

    async def start():
        try:
            log(tmp_path, 'upload_verified')
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            seen.append('cancelled')  # print_next.start stops and reaps print-now here
            raise

    async def main():
        task = asyncio.ensure_future(prepared_intake._with_start_progress(
            adapter, S(chat_id='c1'), {}, tmp_path / 'JOB.json', start()))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        before = list(adapter.sent)
        await asyncio.sleep(0.2)
        return before
    before = asyncio.run(main())
    assert seen == ['cancelled']
    assert adapter.sent == before == []


def test_failed_note_delivery_never_affects_the_start(tmp_path):
    class Broken(Adapter):
        async def send(self, chat_id, content, metadata=None):
            raise ConnectionError('discord down')

    async def script():
        log(tmp_path, 'start_submitted')
        await asyncio.sleep(0.2)
        return True, 'preparing'
    assert run(tmp_path, Broken(), script) == (True, 'preparing')


def test_scripted_adapters_get_no_notes(tmp_path):
    adapter = Adapter()
    adapter.picks = []

    async def script():
        log(tmp_path, 'start_submitted')
        await asyncio.sleep(0.2)
        return True, 'preparing'
    run(tmp_path, adapter, script)
    assert adapter.sent == []
