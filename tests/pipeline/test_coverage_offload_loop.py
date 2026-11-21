"""The post-slice support coverage gate runs off the chat loop (Discord heartbeat stalls).

Synthetic data only. A controlled slow coverage worker (a blocking sleep inside the
patched ``verify_coverage_by_plate``) stands in for a long G-code walk. Run inline it would
freeze the loop; offloaded, a Discord-like ticker keeps beating, and the gate's real result
still decides whether the job starts.
"""
import asyncio
import json
import time
from types import SimpleNamespace as S

import pytest

from print_handy.core import owned_worker, prepared_intake, support_clearance
from .helpers import external
from .test_color_intake import state
from .test_intake_support_precheck import Adapter, env, _warning_slice, _coverage_only_report  # noqa: F401
from .test_intake_support_unverified_968 import _in_process_heavy

print_next = external("print_next")
SLOW_S = 0.6
MAX_GAP_S = 0.25


def _slow_verify(result):
    def verify(regions, plates):
        time.sleep(SLOW_S)  # a blocking G-code walk
        return result
    return verify


def _run_with_ticker(env, picks):
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'), message_id='m1',
              text='https://www.printables.com/model/1-ghost')
    adapter = Adapter(picks)
    gaps = []

    async def main():
        async def ticker():
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.02)
                now = time.monotonic()
                gaps.append(now - last)
                last = now
        beat = asyncio.ensure_future(ticker())
        try:
            return await prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1)
        finally:
            beat.cancel()
    result = asyncio.run(main())
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    return adapter, result, receipt, gaps


def _setup(env, monkeypatch, verify):
    monkeypatch.setattr(support_clearance, 'safe_analyze', _coverage_only_report)
    from print_handy.core import model_job
    monkeypatch.setattr(model_job, 'slice_job', _warning_slice(env))
    monkeypatch.setattr(support_clearance, 'verify_coverage_by_plate', verify)
    _in_process_heavy(monkeypatch)


@pytest.mark.parametrize('ok, pick, status, asked', [
    (True, None, 'STARTED', False),        # a passing gate starts without a question
    (False, 1, 'SLICED_NOT_STARTED', True),  # a failing gate asks, and Cancel starts nothing
])
def test_slow_coverage_keeps_the_loop_responsive_and_the_gate_still_decides(env, monkeypatch, ok, pick, status, asked):
    result = {'checked': True, 'ok': ok, 'unverified': [], 'plate_mode': 'single', 'plates': [],
              'regions': [{'coverage': 1.0 if ok else 0.0, 'part': 'Body', 'z_mm': {'min': 5.0, 'max': 5.0}}]}
    _setup(env, monkeypatch, _slow_verify(result))
    picks = [2, 0, 0] + ([pick] if pick is not None else [])
    adapter, _res, receipt, gaps = _run_with_ticker(env, picks)
    assert max(gaps) < MAX_GAP_S, f'the loop stalled for {max(gaps):.2f}s while coverage ran'
    assert sum(gaps) > SLOW_S  # the slow worker really ran under the ticker
    assert receipt['status'] == status
    gate_q = [q for q in adapter.questions if q['choices'] and prepared_intake.PRINT_ANYWAY_LABEL in q['choices']]
    assert bool(gate_q) is asked
    assert bool(env.starts) is (status == 'STARTED')


def test_coverage_worker_failure_is_unverified_and_asks_never_a_pass(env, monkeypatch):
    def dead(regions, plates):
        raise owned_worker.WorkerError('the support coverage check exceeded its deadline')
    _setup(env, monkeypatch, dead)

    async def heavy(func, *args, **kwargs):
        if func is support_clearance.verify_coverage_by_plate:
            raise owned_worker.WorkerError('Print Handy is busy with 10 other model check steps')
        return await asyncio.to_thread(func, *args, **kwargs)
    monkeypatch.setattr(prepared_intake, '_heavy', heavy)
    adapter, _res, receipt, _gaps = _run_with_ticker(env, [2, 0, 0, 1])
    q = adapter.questions[-1]
    assert q['choices'] == [prepared_intake.PRINT_ANYWAY_LABEL, print_next.CANCEL_LABEL]
    assert "Support coverage couldn't be verified:" in q['question']
    assert 'could not run' in q['question']
    assert receipt['status'] == 'SLICED_NOT_STARTED'
    assert env.starts == []


def test_coverage_steps_are_bounded_general_lane_worker_steps():
    from print_handy.core import heavy_worker
    for name in ('verify_coverage_by_plate', 'source_extent'):
        entry = ('support_clearance', name)
        assert entry in heavy_worker.ALLOWED
        assert entry in prepared_intake._OFFLOAD
        assert entry not in prepared_intake._SLICER_STEPS  # general lane (2 slots, 8 queued), never the slicer's


def test_source_extent_matches_the_inline_computation():
    from pathlib import Path
    import numpy as np
    path = Path(__file__).parents[1] / 'corpus/cases/single-part/model.3mf'
    got = support_clearance.source_extent(path)
    found = support_clearance.leaves(path)
    tris = np.concatenate([p['tris'].reshape(-1, 3) for p in found])
    assert got['bounds'] == [float(tris[:, 0].min()), float(tris[:, 1].min()),
                             float(tris[:, 0].max()), float(tris[:, 1].max())]
    assert got['height'] == (float(tris[:, 2].max()) or None)
    assert got['part_count'] == (len({p.get('item', i) for i, p in enumerate(found) if not p.get('support')}) or None)
    json.dumps(got)  # plain JSON for the worker's reply
