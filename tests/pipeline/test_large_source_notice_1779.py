"""#1779: a big source gets one heads-up line before its slow steps.

On 2026-11-21 two STLs of 56 MB (about 1.1 million triangles) spent four
minutes before the first question with no word to the operator. Now the
intake says once, after the download and before the checks and the combine,
that the files are big and take a few minutes.
"""
import asyncio
import struct
import zipfile
from pathlib import Path
from types import SimpleNamespace as S

import pytest


from print_handy.core import prepared_intake


def _sized_stl(path, triangles):
    """A binary STL whose header and length agree on ``triangles`` (zero-filled)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as out:
        out.write(b'\0' * 80 + struct.pack('<I', triangles))
        out.truncate(84 + 50 * triangles)
    return path


def test_small_sources_get_no_heads_up(tmp_path):
    assert prepared_intake.large_source_notice([_sized_stl(tmp_path / 'a.stl', 80_000)]) is None


def test_the_2026_11_21_pair_names_its_size_and_triangles(tmp_path):
    paths = [_sized_stl(tmp_path / 'SnackTongs.stl', 606_000),
             _sized_stl(tmp_path / 'SnackTongs_FlatProfile.stl', 516_000)]
    assert prepared_intake.large_source_notice(paths) == (
        'These files are big (56 MB of mesh, about 1.1 million triangles); '
        'preparing them takes a few minutes.')


def test_one_file_over_the_triangle_limit(tmp_path):
    path = _sized_stl(tmp_path / 'a.stl', prepared_intake.LARGE_SOURCE_TRIANGLES)
    assert prepared_intake.large_source_notice([path]) == (
        'This file is big (20 MB of mesh, about 400,000 triangles); preparing it takes a few minutes.')


def test_a_3mf_is_measured_by_its_unpacked_mesh(tmp_path):
    path = tmp_path / 'big.3mf'
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('3D/3dmodel.model', b'0' * 25_000_000)
    assert path.stat().st_size < 1_000_000  # the download itself is small
    assert prepared_intake.large_source_notice([path]) == (
        'This file is big (25 MB of mesh); preparing it takes a few minutes.')


def test_unreadable_sources_get_no_heads_up(tmp_path):
    bad = tmp_path / 'bad.3mf'
    bad.write_bytes(b'not a zip')
    assert prepared_intake.large_source_notice([bad, tmp_path / 'missing.stl']) is None


def _intake(tmp_path, monkeypatch, names):
    from print_handy.core import model_job, model_parts
    from print_handy.runtime import _config as runtime_config
    from .test_running_job_cancel import _IntakeAdapter, _grid_stl

    monkeypatch.setattr(model_job, 'ARCHIVE', tmp_path / 'archive')
    monkeypatch.setattr(runtime_config, 'cache_dir', lambda: tmp_path / 'cache')
    monkeypatch.setattr(prepared_intake, 'LARGE_SOURCE_TRIANGLES', 500)  # the 800-triangle grids are "big"
    monkeypatch.setattr(model_job, 'printables_inventory', lambda url: {
        'site': 'printables', 'slug': 'big-model', 'title': 'Big Model', 'model_id': 1,
        'sources': [{'id': n, 'name': name, 'kind': 'stls'} for n, name in enumerate(names, 1)]})
    monkeypatch.setattr(model_job, 'printables_download',
                        lambda job, chosen, dest: str(_grid_stl(Path(dest) / chosen['name'], 20, 20)))
    adapter = _IntakeAdapter([0])
    seen = {}

    def stop_at(name):
        def stop(*_a, **_k):
            seen[name] = list(adapter.sent)
            raise RuntimeError('stop the intake here')
        return stop

    from print_handy.core import intake_resources
    if len(names) > 1:
        monkeypatch.setattr(model_parts, 'combine', stop_at('combine'))
        monkeypatch.setattr(prepared_intake, '_heavy', lambda fn, *a, **k: asyncio.to_thread(fn, *a, **k))
    else:
        monkeypatch.setattr(intake_resources, 'check_source', stop_at('check'))
    event = S(source=S(chat_id='c1', thread_id=None, user_id='u'),
              message_id='big-' + str(len(names)), text='https://www.printables.com/model/1-big-model')
    from .test_color_intake import state
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), tmp_path / 'receipts', timeout=30))
    return adapter, seen


def test_a_big_multi_file_intake_posts_one_heads_up_before_the_combine(tmp_path, monkeypatch):
    adapter, seen = _intake(tmp_path, monkeypatch, ['part_a.stl', 'part_b.stl'])
    notes = [m for m in seen['combine'] if 'take' in m and 'big' in m]
    assert notes == ['These files are big (0 MB of mesh, about 2,000 triangles); '
                     'preparing them takes a few minutes.']
    assert sum('are big' in m for m in adapter.sent) == 1


def test_a_big_single_file_intake_posts_the_heads_up_before_the_checks(tmp_path, monkeypatch):
    _adapter, seen = _intake(tmp_path, monkeypatch, ['part_a.stl'])
    assert [m for m in seen['check'] if 'is big' in m] == [
        'This file is big (0 MB of mesh, about 1,000 triangles); preparing it takes a few minutes.']


def _3mf_with_mesh(path, size):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('3D/3dmodel.model', b'0' * size)
    return path


def test_the_limits_are_inclusive_as_documented(tmp_path, monkeypatch):
    """docs/INTAKE-LIMITS.md: 20 MB of mesh or more, or 400,000 triangles or more."""
    notice = prepared_intake.large_source_notice
    # Size limit alone (a 3MF carries no triangle count).
    assert notice([_3mf_with_mesh(tmp_path / 'at.3mf', 20_000_000)]) is not None
    assert notice([_3mf_with_mesh(tmp_path / 'under.3mf', 19_999_999)]) is None
    # Triangle limit alone: the size limit is lifted so it cannot decide.
    monkeypatch.setattr(prepared_intake, 'LARGE_SOURCE_MB', 10_000)
    limit = prepared_intake.LARGE_SOURCE_TRIANGLES
    assert notice([_sized_stl(tmp_path / 'at.stl', limit)]) is not None
    assert notice([_sized_stl(tmp_path / 'under.stl', limit - 1)]) is None


class _Sender:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.sent = result, error, []

    async def send(self, chat_id, content, metadata=None):
        self.sent.append(content)
        if self.error:
            raise self.error
        return self.result


@pytest.mark.parametrize('adapter, delivered', [
    (_Sender(S(success=True)), True),
    (_Sender(S(success=False, error='rate limited')), False),
    (_Sender(error=RuntimeError('gateway down')), False),
])
def test_only_a_delivered_heads_up_counts_as_posted(tmp_path, adapter, delivered):
    """#1800 review: a failed send returns None, so the audit keeps its own large-model prompt."""
    path = _sized_stl(tmp_path / 'a.stl', prepared_intake.LARGE_SOURCE_TRIANGLES)
    posted = asyncio.run(prepared_intake._post_large_source_notice(
        adapter, S(chat_id='c1'), None, [path]))
    assert adapter.sent  # the send was attempted either way
    assert bool(posted) is delivered


def test_a_failed_heads_up_leaves_the_audit_its_large_model_prompt(tmp_path, monkeypatch):
    from print_handy.core import model_job, intake_resources  # noqa: F401
    from print_handy.runtime import _config as runtime_config
    from .test_color_intake import state
    from .test_running_job_cancel import _IntakeAdapter, _grid_stl

    monkeypatch.setattr(model_job, 'ARCHIVE', tmp_path / 'archive')
    monkeypatch.setattr(runtime_config, 'cache_dir', lambda: tmp_path / 'cache')
    monkeypatch.setattr(prepared_intake, 'LARGE_SOURCE_TRIANGLES', 500)
    monkeypatch.setattr(prepared_intake, 'large_model_notice', lambda path: 'LARGE MODEL PROMPT')
    monkeypatch.setattr(model_job, 'printables_inventory', lambda url: {
        'site': 'printables', 'slug': 'big-model', 'title': 'Big Model', 'model_id': 1,
        'sources': [{'id': 1, 'name': 'part_a.stl', 'kind': 'stls'}]})
    monkeypatch.setattr(model_job, 'printables_download',
                        lambda job, chosen, dest: str(_grid_stl(Path(dest) / chosen['name'], 20, 20)))
    audit_texts = []
    real_voiced = prepared_intake._voiced

    def voiced(text, stage, source, key):
        if stage == 'model_audit':
            audit_texts.append(text)
        return real_voiced(text, stage, source, key)

    def stop(*_a, **_k):
        raise RuntimeError('stop the intake here')

    monkeypatch.setattr(prepared_intake, '_voiced', voiced)
    monkeypatch.setattr(model_job, 'audit', stop)
    monkeypatch.setattr(prepared_intake, '_heavy', lambda fn, *a, **k: asyncio.to_thread(fn, *a, **k))

    class FailingHeadsUp(_IntakeAdapter):
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            if 'is big' in content:
                raise RuntimeError('gateway down')
            return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)

    adapter = FailingHeadsUp([0])
    event = S(source=S(chat_id='c1', thread_id=None, user_id='u'),
              message_id='big-failed-send', text='https://www.printables.com/model/1-big-model')
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), tmp_path / 'receipts', timeout=30))
    assert not any('is big' in m for m in adapter.sent)
    assert audit_texts == ['LARGE MODEL PROMPT']
