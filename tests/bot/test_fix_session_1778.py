"""Repair tools must change geometry or say so, and a fix session accepts only a real new file (#1778)."""
import json
from pathlib import Path

import pytest
import numpy as np

from print_handy.bot import fix_session
from print_handy.core import mesh_repair, support_clearance
from print_handy.skills.print_repair import tools
from tests.core.test_layer_scan import box


@pytest.fixture(autouse=True)
def _clear_sessions():
    fix_session._SESSIONS.clear()
    yield
    fix_session._SESSIONS.clear()


def _files(folder):
    return sorted(path.relative_to(folder).as_posix() for path in folder.rglob('*') if path.is_file())


def _workspace(tmp_path):
    folder = tmp_path / 'job'
    (folder / 'source').mkdir(parents=True)
    stl = folder / 'source' / 'part.stl'
    support_clearance._write_binary_stl(stl, box(0, 0, 0, 10, 10, 10))
    job_path = folder / 'JOB.json'
    job_path.write_text(json.dumps({
        'slug': 'part',
        'downloaded_file': str(stl.resolve()),
    }) + '\n', encoding='utf-8')
    return folder, job_path, stl


def _session(tmp_path):
    folder, job_path, stl = _workspace(tmp_path)
    session = fix_session.start_session('user', 'chat', 'thread', job_path, stl)
    assert session.skill_session is not None
    return folder, job_path, stl, session


def test_unimplemented_repair_does_not_report_success_for_an_unchanged_copy(tmp_path):
    """A stub repair returns ok false and does not leave a copy of the source."""
    folder, _job, stl, _session_obj = _session(tmp_path)
    before = stl.read_bytes()
    names = _files(folder)
    reply = tools.Session(folder).run('gusset_45', {
        'mesh': 'source/part.stl', 'overhang_region': [0, 0, 10, 10], 'wall_x': 0})
    assert reply['ok'] is False
    assert reply['result']['ok'] is False
    assert 'new_mesh' not in reply['result']
    assert 'nothing was changed' in reply['error']
    assert stl.read_bytes() == before
    assert _files(folder) == names

    repair = tools.Session(folder).run('repair_mesh', {'mesh': 'source/part.stl'})
    assert repair['ok'] is False
    assert 'nothing' in repair['error']
    assert 'repair_mesh' not in repair['error']  # the missing attribute must not be what we call
    assert stl.read_bytes() == before
    assert _files(folder) == names


def test_extend_with_nothing_to_move_does_not_leave_a_copy(tmp_path):
    folder, _job, stl, _session_obj = _session(tmp_path)
    before = stl.read_bytes()
    names = _files(folder)
    reply = tools.Session(folder).run('extend_underside', {'mesh': 'source/part.stl', 'mm': 0.4})
    assert reply['ok'] is False
    assert 'nothing was changed' in reply['error']
    assert 'new_mesh' not in reply.get('result', {})
    assert stl.read_bytes() == before
    assert _files(folder) == names


def test_sink_and_trim_change_geometry_and_keep_the_original(tmp_path):
    folder, _job, stl, _session_obj = _session(tmp_path)
    before = stl.read_bytes()
    sink = tools.Session(folder).run('sink', {'mesh': 'source/part.stl', 'mm': 1})
    assert sink['ok'] is True, sink
    result = sink['result']
    assert result['ok'] is True
    assert result['volume_removed_mm3'] == pytest.approx(100, abs=1)
    sunk = folder / result['new_mesh']
    assert sunk.is_file() and sunk.resolve() != stl.resolve()
    assert sunk.read_bytes() != before
    assert mesh_repair.load_triangles(sunk)[:, :, 2].min() == pytest.approx(-1, abs=0.05)
    assert stl.read_bytes() == before

    trim = tools.Session(folder).run('trim_below_plane', {
        'mesh': 'source/part.stl', 'region': [0, 0, 10, 10], 'z': 2})
    assert trim['ok'] is True, trim
    assert trim['result']['volume_removed_mm3'] == pytest.approx(200, abs=1)
    trimmed = folder / trim['result']['new_mesh']
    assert trimmed.is_file() and trimmed.resolve() != stl.resolve()
    assert mesh_repair.load_triangles(trimmed)[:, :, 2].min() == pytest.approx(2, abs=0.05)
    assert stl.read_bytes() == before

    partial = tools.Session(folder).run('trim_below_plane', {
        'mesh': 'source/part.stl', 'region': [0, 0, 1, 1], 'z': 2})
    assert partial['ok'] is False
    assert 'nothing was changed' in partial['error']
    assert stl.read_bytes() == before
    assert not list(folder.rglob('*partial*'))


def test_trim_refuses_triangle_crossing_the_region_at_the_cut_plane(tmp_path):
    folder, _job, stl, _session_obj = _session(tmp_path)
    # Two vertices are under z=2 and inside [0,10] but the clipped edge
    # intersects z=2 at x=11, outside the requested local region.
    support_clearance._write_binary_stl(stl, np.array([[
        [0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [15.0, 5.0, 10.0]]]))
    before = stl.read_bytes()
    result = tools.Session(folder).run('trim_below_plane', {
        'mesh': 'source/part.stl', 'region': [0, 0, 10, 10], 'z': 2})
    assert result['ok'] is False
    assert 'outside the requested region' in result['error']
    assert stl.read_bytes() == before
    assert not list(stl.parent.glob('part-trimmed*'))


def test_process_turn_not_ok_tool_does_not_claim_the_file_changed(tmp_path):
    _folder, _job, stl, session = _session(tmp_path)
    before = stl.read_bytes()

    def ai_complete(bound, message, tool_schemas):
        assert bound is session
        assert message == 'please fix the sag'
        assert any(item['function']['name'] == 'sink' for item in tool_schemas)

        def run(name, arguments=None):
            assert name == 'sink'
            return {'ok': False, 'error': 'sink did not change the mesh',
                    'result': {'ok': False, 'error': 'sink did not change the mesh'}}

        bound.skill_session.run = run
        return {'content': 'Done, the file is fixed.',
                'tool_calls': [{'name': 'sink', 'arguments': {'mesh': 'source/part.stl', 'mm': 1}}]}

    out = fix_session.process_turn(session, 'please fix the sag', ai_complete=ai_complete)
    assert out['accepted'] is False and out['cancelled'] is False
    assert 'did not change the file' in out['reply'].lower()
    assert 'was accepted' not in out['reply'].lower()
    assert 'is fixed' not in out['reply'].lower()
    assert fix_session._same_path(session.current_file, stl)
    assert session.accepted is False and session.active()
    assert stl.read_bytes() == before


def test_process_turn_accept_with_no_new_file_does_not_accept(tmp_path):
    _folder, job_path, stl, session = _session(tmp_path)
    before = stl.read_bytes()
    job_before = job_path.read_text(encoding='utf-8')

    def boom(*_args, **_kwargs):
        raise AssertionError('accept with no fixed file must not call the model or a tool')

    out = fix_session.process_turn(session, 'looks good', ai_complete=boom)
    assert out['accepted'] is False and out['cancelled'] is False
    assert 'no fixed file' in out['reply'].lower()
    assert 'unchanged' in out['reply'].lower()
    assert fix_session.SESSION_END_ACCEPT not in out['reply']
    assert 'was accepted' not in out['reply'].lower()
    assert session.accepted is False and session.active()
    assert stl.read_bytes() == before
    assert job_path.read_text(encoding='utf-8') == job_before


def test_process_turn_accept_after_a_successful_tool_updates_the_job(tmp_path):
    _folder, job_path, stl, session = _session(tmp_path)
    before = stl.read_bytes()

    def ai_complete(_bound, _message, tool_schemas):
        assert any(item['function']['name'] == 'sink' for item in tool_schemas)
        return {'content': 'Sinking the model onto the plate.',
                'tool_calls': [{'name': 'sink', 'arguments': {'mesh': 'source/part.stl', 'mm': 1}}]}

    turn = fix_session.process_turn(session, 'sink it', ai_complete=ai_complete)
    assert turn['accepted'] is False
    assert 'source/part-sunk.stl' in turn['reply']
    assert not fix_session._same_path(session.current_file, stl)
    assert session.current_file.is_file()
    assert stl.read_bytes() == before

    def boom(*_args, **_kwargs):
        raise AssertionError('accept must not call the model')

    # No direct acceptance before the shared model intake has verified the copy.
    premature = fix_session.process_turn(session, 'accept', ai_complete=boom)
    assert premature['accepted'] is False and session.active()
    assert Path(json.loads(job_path.read_text())['downloaded_file']).resolve() == stl.resolve()
    done = fix_session.process_turn(session, 'accept', ai_complete=boom,
                                    resume_intake=lambda _session: True)
    assert done['accepted'] is True and done['cancelled'] is False
    assert done['reply'] == fix_session.SESSION_END_ACCEPT
    assert session.accepted is True and not session.active()
    job = json.loads(job_path.read_text(encoding='utf-8'))
    assert Path(job['downloaded_file']).resolve() == session.current_file.resolve()
    assert Path(job['downloaded_file']).resolve() != stl.resolve()
    assert stl.read_bytes() == before


def test_process_turn_ai_failure_leaves_the_original_unchanged(tmp_path, caplog):
    _folder, _job, stl, session = _session(tmp_path)
    before = stl.read_bytes()

    def ai_complete(_bound, _message, _tools):
        raise RuntimeError('model down')

    with caplog.at_level('ERROR'):
        out = fix_session.process_turn(session, 'fix the sag', ai_complete=ai_complete)
    assert out['accepted'] is False
    assert 'repair step failed' in out['reply'].lower()
    assert 'unchanged' in out['reply'].lower()
    assert 'was accepted' not in out['reply'].lower()
    assert fix_session._same_path(session.current_file, stl)
    assert stl.read_bytes() == before
    assert 'AI fix step failed' in caplog.text


def test_process_turn_accept_does_not_claim_success_when_the_job_write_fails(tmp_path, monkeypatch):
    _folder, job_path, stl, session = _session(tmp_path)
    before = stl.read_bytes()

    def ai_complete(_bound, _message, _tools):
        return {'content': '', 'tool_calls': [
            {'name': 'sink', 'arguments': {'mesh': 'source/part.stl', 'mm': 1}}]}

    turn = fix_session.process_turn(session, 'sink it', ai_complete=ai_complete)
    assert 'source/part-sunk.stl' in turn['reply']

    def fail(_folder, _job):
        raise OSError('read only')

    monkeypatch.setattr('print_handy.jobs.store.write_job', fail)
    out = fix_session.process_turn(session, 'yes', ai_complete=ai_complete,
                                   resume_intake=lambda _session: True)
    assert out['accepted'] is False
    assert session.accepted is False and session.active()
    assert 'was not changed' in out['reply'].lower()
    assert fix_session.SESSION_END_ACCEPT not in out['reply']
    assert 'was accepted' not in out['reply'].lower()
    job = json.loads(job_path.read_text(encoding='utf-8'))
    assert Path(job['downloaded_file']).resolve() == stl.resolve()
    assert stl.read_bytes() == before


def test_tools_unavailable_does_not_claim_a_repair(tmp_path):
    _folder, _job, stl, session = _session(tmp_path)
    session.skill_session = None

    def boom(*_args, **_kwargs):
        raise AssertionError('no skill session must not call the model')

    out = fix_session.process_turn(session, 'fix this', ai_complete=boom)
    assert out['accepted'] is False
    assert 'unavailable' in out['reply'].lower()
    assert 'repaired file' not in out['reply'].lower()
    assert 'was accepted' not in out['reply'].lower()
    assert fix_session._same_path(session.current_file, stl)
