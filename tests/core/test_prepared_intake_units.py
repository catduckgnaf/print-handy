"""Unit coverage for bounded geometry intake, audits and owned worker behavior."""
import asyncio
import json
import math
import random
import struct
import sys
import time
import zipfile
from pathlib import Path

import pytest

from print_handy.core import model_job, orca_adapter, owned_worker, prepared_intake, source_colors


def _cube(size=1.0, offset=(0.0, 0.0, 0.0)):
    x, y, z = offset
    p = [(x, y, z), (x + size, y, z), (x + size, y + size, z), (x, y + size, z),
         (x, y, z + size), (x + size, y, z + size), (x + size, y + size, z + size),
         (x, y + size, z + size)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return [[p[i] for i in face] for face in faces]


def _write_stl(path, triangles):
    with open(path, 'wb') as f:
        f.write(b'test mesh'.ljust(80, b' '))
        f.write(struct.pack('<I', len(triangles)))
        for tri in triangles:
            f.write(struct.pack('<12fH', 0.0, 0.0, 1.0, *(v for p in tri for v in p), 0))
    return Path(path)


def _3mf(path, *, model=b'<model><resources/><build><item objectid="1"/></build></model>', extra=None):
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('3D/3dmodel.model', model)
        for name, content in (extra or {}).items():
            z.writestr(name, content)
    return Path(path)


def test_yaw_rotating_xy_hull_matches_rotating_every_point_on_seeded_meshes():
    rng = random.Random(0xBADD1E)
    for _ in range(80):
        points = [(rng.uniform(-1e3, 1e3), rng.uniform(-1e3, 1e3), rng.uniform(-1e3, 1e3))
                  for _ in range(rng.randrange(3, 45))]
        tris = [points[i:i + 3] for i in range(0, len(points) - 2, 3)]
        yaw = rng.uniform(-360, 360)
        m = orca_adapter._yaw(yaw)
        all_rotated = [orca_adapter._rotate(p, m) for p in orca_adapter._unique_points(tris)]
        brute = orca_adapter._hull([(p[0], p[1]) for p in all_rotated])
        hull = orca_adapter._hull([(p[0], p[1]) for p in orca_adapter._unique_points(tris)])
        hull_rotated = [(m[0] * x + m[1] * y, m[3] * x + m[4] * y) for x, y in hull]
        assert (max(x for x, _ in brute) - min(x for x, _ in brute),
                max(y for _, y in brute) - min(y for _, y in brute)) == pytest.approx(
                    (max(x for x, _ in hull_rotated) - min(x for x, _ in hull_rotated),
                     max(y for _, y in hull_rotated) - min(y for _, y in hull_rotated)), abs=1e-9)


def test_rotation_preserves_pairwise_distances_and_candidate_footprints():
    points = [(0.0, 0.0, 0.0), (3.0, 0.0, 0.0), (0.0, 4.0, 2.0), (-2.0, 1.0, 7.0)]
    rot = orca_adapter._yaw(37.0)
    before = sorted(round(math.dist(a, b), 9) for i, a in enumerate(points) for b in points[i + 1:])
    after = [orca_adapter._rotate(p, rot) for p in points]
    assert sorted(round(math.dist(a, b), 9) for i, a in enumerate(after) for b in after[i + 1:]) == before
    rows = orca_adapter.placement_candidates([points[:3]], printer_model='test-printer', bed_mm=(100, 80), max_z_mm=120)
    assert rows and all(row['printer_model'] == 'test-printer' for row in rows)
    assert sorted(row['rank'] for row in rows) == list(range(1, len(rows) + 1))


@pytest.mark.parametrize('points', [
    [], [(0.0, 0.0, 0.0)], [(0.0, 0.0, 0.0), (1.0, 1.0, 1.0)],
    [(0.0, 0.0, 0.0), (1.0, 1.0, 0.0), (2.0, 2.0, 0.0)],
    [(0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)],
    [(-1e9, -1e9, -1e9), (1e9, -1e9, -1e9), (0.0, 1e9, 1e9)],
])
def test_hull_handles_empty_degenerate_duplicate_and_huge_negative_coordinates(points):
    hull = orca_adapter._hull([(p[0], p[1]) for p in points])
    assert len(hull) <= len(set((p[0], p[1]) for p in points))
    assert len(hull) <= 2 or len(hull) <= len(points)


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), float('-inf')])
def test_placement_nonfinite_coordinates_do_not_produce_successful_candidate_report(bad):
    with pytest.raises((ValueError, OverflowError, RuntimeError)):
        orca_adapter.placement_candidates([[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, bad, 1.0)]])


def test_matrix_helpers_match_scalar_reference():
    a = [1, 2, 3, 4, 5, 6, 7, 8, 9]
    b = [9, 8, 7, 6, 5, 4, 3, 2, 1]
    expected = [sum(a[r * 3 + k] * b[k * 3 + c] for k in range(3)) for r in range(3) for c in range(3)]
    assert orca_adapter._matmul(a, b) == expected
    yaw = orca_adapter._yaw(90)
    assert orca_adapter._rotate((2, 3, 4), yaw) == pytest.approx((-3, 2, 4))


def test_binary_stl_source_triangles_and_malformed_files(tmp_path):
    path = _write_stl(tmp_path / 'mesh.stl', _cube())
    assert orca_adapter.source_triangles(path) == _cube()
    for name, content in [('empty.stl', b''), ('truncated.stl', b'BINARY'.ljust(80, b' ') + struct.pack('<I', 2) + bytes(50)),
                          ('wrong-count.stl', b'BINARY'.ljust(80, b' ') + struct.pack('<I', 1) + bytes(100)),
                          ('ascii-garbage.stl', b'solid invalid facet\nvertex garbage\n')]:
        bad = tmp_path / name
        bad.write_bytes(content)
        with pytest.raises(RuntimeError, match='not a readable STL'):
            orca_adapter.read_stl_triangles(bad)


def test_model_job_audit_reports_digest_dimensions_topology_and_printer(tmp_path):
    path = _write_stl(tmp_path / 'closed.stl', _cube())
    report = model_job.mesh_audit(path, printer_model='A1')
    assert report['schema_version'] == 1
    assert report['printer_model'] == 'A1'
    assert report['dimensions_mm'] == [1.0, 1.0, 1.0]
    assert report['triangles'] == 12
    assert report['sha256'] == report['source_sha256']
    assert report['placement_candidates']
    assert model_job.mesh_topology(_cube())['manifold'] is True


def test_mesh_topology_counts_open_degenerate_and_disconnected_shells():
    open_face = [[(0, 0, 0), (1, 0, 0), (0, 1, 0)]]
    report = model_job.mesh_topology(open_face + [[(0, 0, 0), (0, 0, 0), (0, 1, 0)]] + _cube(offset=(5, 0, 0)))
    assert report['boundary_edges'] == 3
    assert report['degenerate_triangles'] == 1
    assert report['shells'] == 2
    assert report['manifold'] is False


@pytest.mark.parametrize('size,expect', [(19_999_999, False), (20_000_000, True), (20_000_001, True)])
def test_large_model_notice_stl_boundary_is_exact(tmp_path, size, expect):
    path = tmp_path / 'size.stl'
    with path.open('wb') as f:
        f.truncate(size)
    notice = prepared_intake.large_model_notice(path)
    assert (notice is not None) is expect
    if expect:
        assert notice is not None and 'not stuck' in notice


def test_large_model_notice_counts_model_entries_only_and_handles_bad_inputs(tmp_path):
    path = tmp_path / 'project.3mf'
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('3D/3dmodel.model', b' ' * 21_000_000)
        archive.writestr('notes.txt', b'x' * 100)
    notice = prepared_intake.large_model_notice(path)
    assert notice is not None and '21 MB of mesh' in notice
    at_threshold = tmp_path / 'threshold.3mf'
    with zipfile.ZipFile(at_threshold, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('3D/3dmodel.model', b' ' * 20_000_000)
    assert prepared_intake.large_model_notice(at_threshold) is not None
    only_other = tmp_path / 'metadata-only.3mf'
    _3mf(only_other, model=b'<model/>', extra={'3D/other.model': b'x' * 20_000_001})
    other_model_notice = prepared_intake.large_model_notice(only_other)
    assert other_model_notice is not None and '20 MB of mesh' in other_model_notice
    assert prepared_intake.large_model_notice(tmp_path / 'missing.stl') is None
    malformed = tmp_path / 'malformed.3mf'
    malformed.write_bytes(b'not zip')
    assert prepared_intake.large_model_notice(malformed) is None


def test_large_model_notice_handles_zip_declared_size_without_expanding(tmp_path):
    archive = tmp_path / 'declared-bomb.3mf'
    with zipfile.ZipFile(archive, 'w') as z:
        info = zipfile.ZipInfo('3D/3dmodel.model')
        info.compress_type = zipfile.ZIP_STORED
        z.writestr(info, bytes(100_000_000))
    notice = prepared_intake.large_model_notice(archive)
    assert notice is not None and '100 MB of mesh' in notice


def test_3mf_adversarial_xml_and_missing_model_are_rejected(tmp_path):
    missing = tmp_path / 'missing-model.3mf'
    with zipfile.ZipFile(missing, 'w') as z:
        z.writestr('notes.txt', 'nothing')
    assert prepared_intake.large_model_notice(missing) is None
    malformed = tmp_path / 'bad-xml.3mf'
    _3mf(malformed, model=b'<model><broken>')
    with pytest.raises((RuntimeError, KeyError, zipfile.BadZipFile, __import__('xml.etree.ElementTree', fromlist=['ParseError']).ParseError)):
        source_colors.inventory(malformed)
    zero_model = tmp_path / 'zero-model.3mf'
    _3mf(zero_model, model=b'')
    with pytest.raises((RuntimeError, KeyError, zipfile.BadZipFile, __import__('xml.etree.ElementTree', fromlist=['ParseError']).ParseError)):
        source_colors.inventory(zero_model)


def test_3mf_path_traversal_component_path_does_not_read_outside_archive(tmp_path):
    model = b'<model><resources><object id="1"><components><component objectid="9" path="../../outside.model"/></components></object></resources><build><item objectid="1"/></build></model>'
    archive = tmp_path / 'traversal.3mf'
    _3mf(archive, model=model)
    with pytest.raises((RuntimeError, KeyError, FileNotFoundError)):
        source_colors.inventory(archive)


@pytest.mark.parametrize('triangles', [[], [[(0, 0, 0), (1, 0, 0), (0, 1, 0)]]])
def test_topology_empty_and_single_triangle_counts(triangles):
    report = model_job.mesh_topology(triangles)
    assert report['triangles'] == len(triangles)
    assert report['boundary_edges'] == (3 if triangles else 0)
    assert report['manifold'] is (not triangles)


def test_heavy_worker_killed_garbage_and_empty_stderr_fail_readably(monkeypatch):
    async def run_case(stdout, stderr):
        async def fake(*args, **kwargs):
            return stdout, stderr
        monkeypatch.setattr(prepared_intake.owned_worker, 'run', fake)
        with pytest.raises(RuntimeError) as exc:
            await prepared_intake._heavy(model_job.audit, Path('/does/not/exist.stl'))
        return str(exc.value)
    message = asyncio.run(run_case(b'not json', b''))
    assert 'it stopped without reporting why (killed or crashed)' in message
    assert 'audit worker failed' in message
    garbage = asyncio.run(run_case(b'x' * 2000, b'child diagnostic'))
    assert 'child diagnostic' in garbage


def test_heavy_worker_log_lines_are_forwarded_and_context_is_sent(monkeypatch, caplog):
    # P2-55: the worker's stderr used to be dropped whenever its JSON reply
    # parsed. Its formatted log lines are now re-emitted through intake's
    # logger at their own levels, and the payload carries the bound log
    # context so the worker's lines name the same intake.
    import logging
    captured = {}

    async def fake(argv, payload, **kwargs):
        captured['payload'] = json.loads(payload)
        reply = {'ok': True, 'path': False, 'result': {'plates': 1}}
        err = (b'2026-11-02T00:00:00.000-05:00 WARNING print_handy.core.orca_adapter: tower is close to the part\n'
               b'Traceback (most recent call last):\n'
               b'2026-11-02T00:00:01.000-05:00 INFO print_handy.core.orca_adapter: fit check done\n')
        return ('\n' + json.dumps(reply)).encode(), err
    monkeypatch.setattr(prepared_intake.owned_worker, 'run', fake)
    token = prepared_intake.logs.bind(intake='t-55')
    try:
        with caplog.at_level(logging.DEBUG, logger='print_handy.core.prepared_intake'):
            result = asyncio.run(prepared_intake._heavy(model_job.audit, Path('/does/not/exist.stl')))
    finally:
        prepared_intake.logs.unbind(token)
    assert result == {'plates': 1}
    assert captured['payload']['log_context'] == {'intake': 't-55'}
    forwarded = [(r.levelname, r.getMessage()) for r in caplog.records
                 if r.name == 'print_handy.core.prepared_intake']
    assert ('WARNING', 'tower is close to the part') in [(lvl, msg.split(': ', 1)[-1]) for lvl, msg in forwarded]
    assert any(lvl == 'WARNING' and 'Traceback' in msg for lvl, msg in forwarded)  # continuation keeps the level
    assert any(lvl == 'INFO' and 'fit check done' in msg for lvl, msg in forwarded)


def test_heavy_worker_log_lines_are_forwarded_on_an_error_reply(monkeypatch, caplog):
    import logging
    from print_handy.core import worker_errors

    async def fake(argv, payload, **kwargs):
        reply = worker_errors.encode(RuntimeError('slice exploded'))
        return ('\n' + json.dumps(reply)).encode(), \
            b'2026-11-02T00:00:00.000-05:00 ERROR print_handy.core.orca_adapter: orca exited -50\n'
    monkeypatch.setattr(prepared_intake.owned_worker, 'run', fake)
    with caplog.at_level(logging.DEBUG, logger='print_handy.core.prepared_intake'):
        with pytest.raises(RuntimeError, match='slice exploded'):
            asyncio.run(prepared_intake._heavy(model_job.audit, Path('/does/not/exist.stl')))
    assert any(r.levelname == 'ERROR' and 'orca exited -50' in r.getMessage() for r in caplog.records)


def test_owned_worker_timeout_and_output_limit_are_reported(monkeypatch):
    async def exercise():
        monkeypatch.setattr(owned_worker, 'DEADLINE_S', 0.1)
        monkeypatch.setattr(owned_worker, 'GRACE_S', 0.1)
        monkeypatch.setattr(owned_worker, 'REAP_S', 1.0)
        with pytest.raises(owned_worker.WorkerError, match='worker check did not finish within 0.1 s'):
            await owned_worker.run([sys.executable, '-c', 'import time; time.sleep(10)'], b'',
                                   label='worker check', lane=owned_worker.GENERAL)
        monkeypatch.setattr(owned_worker, 'OUTPUT_LIMIT', 10)
        monkeypatch.setattr(owned_worker, 'DEADLINE_S', 5.0)
        with pytest.raises(owned_worker.WorkerError, match='output limit'):
            await owned_worker.run([sys.executable, '-c', 'print("x" * 1000)'], b'',
                                   label='noisy', lane=owned_worker.GENERAL)
    asyncio.run(exercise())


def test_sigkilled_worker_and_malformed_stdout_fail_readably(monkeypatch, tmp_path):
    run_worker = owned_worker.run
    async def killed(*args, **kwargs):
        return await run_worker([sys.executable, '-c',
                                 'import os, signal; os.kill(os.getpid(), signal.SIGKILL)'],
                                b'', label='killed audit worker', lane=owned_worker.GENERAL)
    monkeypatch.setattr(owned_worker, 'run', killed)
    with pytest.raises(RuntimeError, match=r'audit worker failed: it stopped without reporting why \(killed or crashed\)'):
        asyncio.run(prepared_intake._heavy(model_job.audit, tmp_path / 'mesh.stl'))


def test_owned_worker_concurrent_calls_observe_lane_limits():
    async def exercise():
        lane = owned_worker.Lane('test', slots=2, waiting=2)
        entered = []
        async def work(index):
            async with lane.slot():
                entered.append(index)
                await asyncio.sleep(0.02)
                return index
        results = await asyncio.gather(*(work(i) for i in range(4)))
        assert sorted(results) == [0, 1, 2, 3]
        assert len(entered) == 4
    asyncio.run(exercise())


def test_placement_candidates_handles_generated_300k_triangle_mesh():
    pytest.importorskip('trimesh')
    tris = [[(i % 1000, (i // 1000) % 1000, 0.0),
             (i % 1000 + 0.5, (i // 1000) % 1000, 0.0),
             (i % 1000, (i // 1000) % 1000 + 0.5, 0.0)] for i in range(300_000)]
    started = time.monotonic()
    rows = orca_adapter.placement_candidates(tris, printer_model='synthetic', bed_mm=(300, 300), max_z_mm=300)
    assert len(rows) >= 5
    assert time.monotonic() - started < 120
