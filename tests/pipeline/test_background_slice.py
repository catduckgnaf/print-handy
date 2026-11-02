"""Offline lifecycle tests for the decisions-bound early slicer."""
import asyncio
import json
from pathlib import Path

import pytest

from print_handy.core import model_job
from print_handy.core.background_slice import BackgroundSlice, slice_binding


def _job(decisions=None, **extra):
    return {
        'color_decisions': decisions or {'mode': 'single_color', 'selection': {'slot': 1, 'color': 'white'}},
        'scale': 1.0,
        'orientation': {'label': 'upright'},
        'copies': 1,
        'plate_mode': 'single',
        **extra,
    }


def test_binding_includes_source_sha_and_every_gcode_decision():
    base = _job()
    digest = slice_binding(base, 'source-a')
    assert json.loads(digest)['source_sha256'] == 'source-a'
    assert slice_binding(dict(reversed(list(base.items()))), 'source-a') == digest
    assert slice_binding(base, 'source-b') != digest
    for change in (
        {'scale': 0.8},
        {'orientation': {'label': 'rotated'}},
        {'copies': 2},
        {'plate_mode': 'multi'},
        {'full_size_split_plan': {'plates': [1, 2]}},
        {'color_decisions': {'mode': 'single_color', 'selection': {'slot': 2, 'color': 'black'}}},
    ):
        assert slice_binding({**base, **change}, 'source-a') != digest


def test_changed_binding_discards_old_slice_before_starting_new_one(tmp_path):
    async def run():
        folder = tmp_path / 'intake'
        folder.mkdir()
        job_path = folder / 'JOB.json'
        job_path.write_text('{}')
        runner = BackgroundSlice()
        entered = asyncio.Event()
        release = asyncio.Event()
        finished = []
        invocation = 0

        async def slice_func(path, work, _runner):
            nonlocal invocation
            invocation += 1
            current = invocation
            entered.set()
            if current == 1:
                await release.wait()
            work.mkdir(parents=True, exist_ok=True)
            output = work / f'plate-{current}.gcode'
            output.write_text(path)
            finished.append(output)
            return [output]

        await runner.start(job_path, 'old decisions', slice_func)
        await entered.wait()
        old_work = runner.work
        async def unblock():
            await asyncio.sleep(0.02)
            release.set()
        asyncio.create_task(unblock())
        await runner.start(job_path, 'new decisions', slice_func)
        assert runner.binding == 'new decisions'
        assert old_work is not None and list(old_work.glob('*.gcode')) == []
        assert runner.running()
        await runner.discard()
        assert not runner.running()
        assert not (folder / 'sliced').exists()
        assert finished == []  # cancellation stops the old writer before it writes; nothing is left

    asyncio.run(run())


def test_confirm_takes_completed_slice_without_starting_another(tmp_path):
    async def run():
        folder = tmp_path / 'intake'
        folder.mkdir()
        job_path = folder / 'JOB.json'
        job_path.write_text('{}')
        runner = BackgroundSlice()
        calls = []

        async def slice_func(path, work, _runner):
            calls.append(path)
            work.mkdir(parents=True, exist_ok=True)
            output = work / 'plate.gcode'
            output.write_text('G1 X1\n')
            return [output]

        await runner.start(job_path, 'same decisions', slice_func)
        await runner.started.wait()
        while runner.running():
            await asyncio.sleep(0)
        result = await runner.take('same decisions')
        assert result == [folder / 'sliced' / 'plate.gcode']
        assert calls == [str(job_path)]
        assert (folder / 'sliced' / 'plate.gcode').exists()

    asyncio.run(run())


def test_changed_decision_digest_and_source_hash_never_match_prior_slice(tmp_path):
    first = _job()
    source = tmp_path / 'source.3mf'
    source.write_bytes(b'original source')
    original = slice_binding(first, model_job.file_sha256(source))
    changed_colors = {**first, 'color_decisions': {'mode': 'single_color', 'selection': {'slot': 2}}}
    assert slice_binding(changed_colors, model_job.file_sha256(source)) != original
    source.write_bytes(b'changed source')
    assert slice_binding(first, model_job.file_sha256(source)) != original


def _slice_writes_metadata(path, work, marker):
    """What slice_job does mid-run: output files plus committed JOB.json metadata."""
    job = json.loads(Path(path).read_text())
    job.update({'gcode': str(work / f'{marker}.gcode'), 'gcode_verified': True,
                'sliced_at': f'at-{marker}', 'artifact_binding': {'gcode_sha256': marker},
                'derived_plates': [{'gcode': f'{marker}.gcode'}]})
    Path(path).write_text(json.dumps(job))
    output = work / f'{marker}.gcode'
    output.write_text('G1 X1\n')
    return [output]


def test_discard_clears_slice_metadata_the_slice_committed(tmp_path):
    """P2-24: discard removed the output files but left JOB.json claiming the
    discarded G-code — verified, bound and timestamped — for a later resume
    or print-next to trust."""
    async def run():
        folder = tmp_path / 'intake'
        folder.mkdir()
        job_path = folder / 'JOB.json'
        job_path.write_text(json.dumps({'title': 'keep me'}))
        runner = BackgroundSlice()

        async def slice_func(path, work, _runner):
            return _slice_writes_metadata(path, work, 'early')

        await runner.start(job_path, 'binding', slice_func)
        assert await runner.take('binding') is not None
        job = json.loads(job_path.read_text())
        assert job['gcode'].endswith('early.gcode') and job['gcode_verified'] is True
        await runner.discard()
        job = json.loads(job_path.read_text())
        assert job == {'title': 'keep me'}

    asyncio.run(run())


def test_discard_restores_the_slice_metadata_that_predates_the_run(tmp_path):
    """P2-24: restoring is a snapshot, not a blanket clear — a job that already
    held a completed slice keeps that slice's metadata when only the newer
    background slice is discarded."""
    async def run():
        folder = tmp_path / 'intake'
        folder.mkdir()
        job_path = folder / 'JOB.json'
        prior = {'title': 'keep me', 'gcode': '/old/plate.gcode', 'gcode_verified': True,
                 'sliced_at': 'at-old', 'artifact_binding': {'gcode_sha256': 'old'}}
        job_path.write_text(json.dumps(prior))
        runner = BackgroundSlice()

        async def slice_func(path, work, _runner):
            return _slice_writes_metadata(path, work, 'new')

        await runner.start(job_path, 'binding', slice_func)
        assert await runner.take('binding') is not None
        await runner.discard()
        assert json.loads(job_path.read_text()) == prior

    asyncio.run(run())


def test_await_background_slice_reraises_cancellation(tmp_path):
    """P2-58: take() absorbs CancelledError while it waits out its slice
    task; the intake's wrapper must surface a cancellation again, or a Stop
    pressed during the background-slice wait is silently lost."""
    from print_handy.core import prepared_intake

    async def run():
        folder = tmp_path / 'intake'
        folder.mkdir()
        job_path = folder / 'JOB.json'
        job_path.write_text('{}')
        runner = BackgroundSlice()

        async def never(path, work, _runner):
            await asyncio.sleep(30)
            return []

        await runner.start(job_path, 'binding', never)
        await runner.started.wait()
        task = asyncio.ensure_future(prepared_intake._await_background_slice(runner, 'binding'))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await runner.discard()

    asyncio.run(run())
