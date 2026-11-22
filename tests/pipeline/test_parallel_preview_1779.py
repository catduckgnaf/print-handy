"""Parallel preview slices (#1779): scheduling, config and CPU fallback, the two-run cap, isolation, cancel."""
# ruff: noqa: ASYNC240  (test slicer stubs write tiny files; blocking pathlib is fine here)
import asyncio
import json
from pathlib import Path

import pytest

from print_handy.core import owned_worker, preview_parallel
from print_handy.core.background_slice import BackgroundSlice

RISKY = {'risky': True, 'regions': [], 'findings': []}


def _cfg(parallel=True, command=('/usr/local/bin/orca-slicer',)):
    return {'slicer': {'parallel_preview': parallel}, 'orca': {'command': list(command)}}


@pytest.mark.parametrize('cpus,cfg,budget,expected', [
    (4, _cfg(), None, True),
    (8, _cfg(), None, True),
    (3, _cfg(), None, False),           # below 4: serial
    (None, _cfg(), None, False),        # unknown CPU count: serial
    (0, _cfg(), None, False),
    (8, _cfg(parallel=False), None, False),   # configured off
    (8, _cfg(), 2, False),              # container quota below 4
    (8, _cfg(), 4, True),
    (8, _cfg(command=('flatpak', 'run', 'com.orcaslicer.OrcaSlicer')), None, False),  # exit 243 collisions
])
def test_enabled_rule(cpus, cfg, budget, expected, monkeypatch):
    monkeypatch.delenv('PRINT_HANDY_ORCA_CMD', raising=False)
    assert preview_parallel.enabled(cpu_count=cpus, cfg=cfg, budget=budget) is expected


def test_default_is_on_for_an_eligible_host(monkeypatch):
    monkeypatch.delenv('PRINT_HANDY_ORCA_CMD', raising=False)
    assert preview_parallel.enabled(cpu_count=4, cfg={'orca': {'command': ['/usr/local/bin/orca-slicer']}},
                                    budget=None) is True


def test_slicer_slots_follow_the_rule_and_never_exceed_two(monkeypatch):
    monkeypatch.setattr(preview_parallel, 'enabled', lambda *a, **k: True)
    assert preview_parallel.slicer_slots() == 2 == preview_parallel.MAX_SLICER_RUNS
    monkeypatch.setattr(preview_parallel, 'enabled', lambda *a, **k: False)
    assert preview_parallel.slicer_slots() == 1

    def boom(*a, **k):
        raise RuntimeError('unreadable')
    monkeypatch.setattr(preview_parallel, 'enabled', boom)
    assert preview_parallel.slicer_slots() == 1


def test_slicer_lane_admits_at_most_two_at_once(monkeypatch):
    monkeypatch.setattr(preview_parallel, 'enabled', lambda *a, **k: True)
    lane = owned_worker.Lane('slicing', slots=preview_parallel.slicer_slots, waiting=8)

    async def run():
        live = peak = 0

        async def one():
            nonlocal live, peak
            async with lane.slot():
                live += 1
                peak = max(peak, live)
                await asyncio.sleep(0.02)
                live -= 1
        await asyncio.gather(*(one() for _ in range(6)))
        return peak
    assert asyncio.run(run()) == 2


def _setup(tmp_path, clearance=RISKY):
    folder = tmp_path / 'intake'
    folder.mkdir()
    src = folder / 'm.stl'
    src.write_text('solid')
    job = {'color_manifest': {'source_sha256': 'src-a'}, 'downloaded_file': str(src)}
    if clearance is not None:
        job['support_clearance'] = clearance
    (folder / 'JOB.json').write_text(json.dumps(job))
    return folder, folder / 'JOB.json'


def _slice_func(log, gate=None):
    async def slice_func(path, work, _runner, **kwargs):
        data = json.loads(Path(path).read_text())
        sup = bool((data.get('support_decision') or {}).get('supports'))
        log.append(('start', sup, str(Path(path).parent)))
        if gate is not None:
            await gate(sup)
        out_dir = Path(path).parent / 'sliced'
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / ('plate_sup.gcode' if sup else 'plate_nosup.gcode')
        out.write_text('G1 X1\n')
        data['gcode'] = str(out)
        data['provenance'] = {'slice_warnings': {'support_needed': not sup, 'objects': ['B'],
                                                 'support_texts': [] if sup else ['floating']},
                              'supports_on': sup, 'support_decided': sup}
        Path(path).write_text(json.dumps(data))
        log.append(('end', sup))
        return [out]
    return slice_func


def test_flagged_geometry_runs_both_slices_at_once_in_isolated_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(preview_parallel, 'enabled', lambda *a, **k: True)

    async def run():
        folder, job_path = _setup(tmp_path)
        log = []
        both_started = asyncio.Event()

        async def gate(sup):
            if sum(1 for e in log if e[0] == 'start') == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 2)  # each waits for the other: only overlap passes
        runner = BackgroundSlice()
        await runner.start(job_path, 'b', _slice_func(log, gate))
        await runner.settle()
        starts = {e[1]: e[2] for e in log if e[0] == 'start'}
        assert starts[False] == str(folder) and starts[True] == str(folder / 'variant_supports')
        # the speculative decision never reached the intake JOB.json while the other slice ran
        assert 'support_decision' not in json.loads(job_path.read_text()) \
            or json.loads(job_path.read_text())['support_decision'].get('supports') is not True
        sup = await runner.take_supports()
        assert [Path(p).name for p in sup] == ['plate_sup.gcode']
        assert Path(sup[0]).parent == folder / 'sliced'          # adopted into the intake folder
        assert not (folder / 'variant_supports').exists()
        assert Path(sup[0]).read_text() == 'G1 X1\n'
        job = json.loads(job_path.read_text())
        assert Path(job['gcode']).parent == folder / 'sliced'
        assert (job['support_decision'] or {}).get('supports') is True
        nosup = await runner.take_no_supports()
        assert Path(nosup[0]).name == 'plate_nosup.gcode' and Path(nosup[0]).is_file()
    asyncio.run(run())


def test_serial_when_off_or_unflagged_or_small_host(tmp_path, monkeypatch):
    for enabled, clearance in ((False, RISKY), (True, None), (True, {'risky': False})):
        monkeypatch.setattr(preview_parallel, 'enabled', lambda *a, enabled=enabled, **k: enabled)

        async def run(clearance=clearance, enabled=enabled):
            sub = tmp_path / f'{enabled}-{bool(clearance)}'
            sub.mkdir()
            folder, job_path = _setup(sub, clearance)
            log = []
            runner = BackgroundSlice()
            await runner.start(job_path, 'b', _slice_func(log))
            await runner.settle()
            assert not (folder / 'variant_supports').exists()
            starts = [e for e in log if e[0] == 'start']
            assert all(s[2] == str(folder) for s in starts)
            # strictly one after the other: every start follows the previous end
            order = [e[0] for e in log]
            assert order in (['start', 'end'], ['start', 'end', 'start', 'end'])
        asyncio.run(run())


def test_concurrent_failure_falls_back_to_slicing_in_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(preview_parallel, 'enabled', lambda *a, **k: True)

    async def run():
        folder, job_path = _setup(tmp_path)
        log = []
        real = _slice_func(log)
        calls = {'sup': 0}

        async def flaky(path, work, runner, **kw):
            if 'variant_supports' in path:
                calls['sup'] += 1
                if calls['sup'] == 1:
                    raise RuntimeError('orca exit 243')
            return await real(path, work, runner, **kw)
        runner = BackgroundSlice()
        await runner.start(job_path, 'b', flaky)
        await runner.settle()
        assert runner.supports_error is None
        sup = await runner.take_supports()
        assert sup and Path(sup[0]).name == 'plate_sup.gcode'
    asyncio.run(run())


def test_cancel_stops_both_and_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(preview_parallel, 'enabled', lambda *a, **k: True)

    async def run():
        folder, job_path = _setup(tmp_path)
        before = json.loads(job_path.read_text())
        log = []
        hold = asyncio.Event()

        async def gate(sup):
            await hold.wait()
        runner = BackgroundSlice()
        await runner.start(job_path, 'b', _slice_func(log, gate))
        for _ in range(100):
            if len([e for e in log if e[0] == 'start']) == 2:
                break
            await asyncio.sleep(0.01)
        assert len([e for e in log if e[0] == 'start']) == 2
        await runner.discard()
        assert not runner.running()
        assert not (folder / 'variant_supports').exists()
        assert not (folder / 'sliced').exists()
        assert [e for e in log if e[0] == 'end'] == []
        assert json.loads(job_path.read_text()) == before
    asyncio.run(run())


def test_parallel_output_matches_serial_output(tmp_path, monkeypatch):
    results = {}
    for mode in (True, False):
        monkeypatch.setattr(preview_parallel, 'enabled', lambda *a, mode=mode, **k: mode)

        async def run(mode=mode):
            sub = tmp_path / str(mode)
            sub.mkdir()
            folder, job_path = _setup(sub)
            runner = BackgroundSlice()
            await runner.start(job_path, 'b', _slice_func([]))
            await runner.settle()
            nosup = await runner.take_no_supports()
            nosup_job = json.loads(job_path.read_text())
            sup = await runner.take_supports()
            sup_job = json.loads(job_path.read_text())
            norm = lambda v: json.loads(json.dumps(v).replace(str(folder), '<F>'))
            return {'nosup': [Path(p).read_text() for p in nosup], 'sup': [Path(p).read_text() for p in sup],
                    'nosup_job': norm(nosup_job), 'sup_job': norm(sup_job),
                    'sup_path': str(sup[0]).replace(str(folder), '<F>')}
        results[mode] = asyncio.run(run())
    assert results[True] == results[False]
