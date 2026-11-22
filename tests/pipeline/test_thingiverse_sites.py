"""#462 Stages 0+1: Thingiverse via its official API, and the Printables
empty-geometry refusal. All network is mocked; no live call in tests/CI."""
import asyncio
import io
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.core import model_job as model_job
from print_handy.jobs import common as jobs_common
from print_handy.jobs import sources as jobs_sources
from print_handy.core import prepared_intake
from print_handy.core.link_intake import model_link
from .test_color_intake import source, state  # noqa: F401  (state used by the e2e flow)
from .test_prepared_intake import Adapter, env  # noqa: F401  (env is the shared harness fixture)
from tests.bound_names import setattr_bound

THING_URL = 'https://www.thingiverse.com/thing:763622'
THING = {'id': 763622, 'name': '3DBenchy', 'license': 'Creative Commons - Attribution',
         'public_url': THING_URL}
FILES = [
    {'id': 101, 'name': 'benchy.stl', 'size': 12345, 'download_url': 'https://api.thingiverse.com/files/101/download'},
    {'id': 102, 'name': 'notes.txt', 'size': 10, 'download_url': 'https://api.thingiverse.com/files/102/download'},
    {'id': 103, 'name': 'benchy_hull.3mf', 'size': 999, 'download_url': 'https://api.thingiverse.com/files/103/download'},
    {'id': 104, 'name': 'all_files.zip', 'size': 20000, 'download_url': 'https://api.thingiverse.com/files/104/download'},
]


def _token_home(tmp_path, monkeypatch, token='test-token'):
    home = tmp_path / 'home'
    cfg = home / '.config' / 'thingiverse'
    cfg.mkdir(parents=True)
    if token is not None:
        (cfg / 'token').write_text(token + '\n')
    monkeypatch.setenv('HOME', str(home))
    return home


def _api(monkeypatch, thing=THING, files=FILES):
    calls = []

    def fake_request_json(url, **kw):
        calls.append((url, kw))
        if url.endswith('/files'):
            return files
        return thing

    setattr_bound(monkeypatch, jobs_common, 'request_json', fake_request_json)
    return calls


@pytest.mark.parametrize('text,hit', [
    ('https://www.thingiverse.com/thing:763622', True),
    ('https://thingiverse.com/thing:763622', True),
    ('print https://www.thingiverse.com/thing:763622-3dbenchy please', True),
    ('https://www.thingiverse.com/thing:abc', False),
    ('https://www.thingiverse.com/designer/about', False),
])
def test_model_link_thingiverse(text, hit):
    assert (model_link(text) is not None) is hit


def test_thingiverse_id():
    assert jobs_sources.thingiverse_id(THING_URL) == '763622'
    assert jobs_sources.thingiverse_id(THING_URL + '-3dbenchy') == '763622'
    with pytest.raises(ValueError, match=r'/thing:<number>'):
        jobs_sources.thingiverse_id('https://www.thingiverse.com/designer')


def test_thingiverse_inventory_shape_and_geometry_filter(tmp_path, monkeypatch):
    _token_home(tmp_path, monkeypatch)
    calls = _api(monkeypatch)
    job = jobs_sources.thingiverse_inventory(THING_URL)
    assert job['site'] == 'thingiverse'
    assert job['model_id'] == '763622'
    assert job['title'] == '3DBenchy'
    assert job['license'] == 'Creative Commons - Attribution'
    # Only .stl/.3mf files are sources: the .txt note and the download-all zip are not.
    assert [(s['id'], s['name'], s['bytes'], s['kind']) for s in job['sources']] == [
        (101, 'benchy.stl', 12345, 'files'), (103, 'benchy_hull.3mf', 999, 'files')]
    assert all(c[1]['headers']['Authorization'] == 'Bearer test-token' for c in calls)


def test_thingiverse_missing_token_error(tmp_path, monkeypatch):
    _token_home(tmp_path, monkeypatch, token=None)
    with pytest.raises(RuntimeError, match=r'\.config/thingiverse/token'):
        jobs_sources.thingiverse_inventory(THING_URL)
    with pytest.raises(RuntimeError, match='thingiverse.com/developers'):
        jobs_sources.thingiverse_headers()


def test_thingiverse_empty_token_error(tmp_path, monkeypatch):
    _token_home(tmp_path, monkeypatch, token='')
    with pytest.raises(RuntimeError, match='Thingiverse token empty'):
        jobs_sources.thingiverse_headers()


def _http_error(code, headers=None):
    return urllib.error.HTTPError('https://api.thingiverse.com/things/1', code, 'blocked', headers or {}, io.BytesIO(b''))


def test_thingiverse_challenge_is_reported_plainly(tmp_path, monkeypatch):
    _token_home(tmp_path, monkeypatch)
    setattr_bound(monkeypatch, jobs_common, 'request_json',
                        mock.Mock(side_effect=_http_error(403, {'Cf-Mitigated': 'challenge'})))
    with pytest.raises(RuntimeError, match='Cloudflare robot check'):
        jobs_sources.thingiverse_inventory(THING_URL)


def test_thingiverse_401_is_reported_plainly(tmp_path, monkeypatch):
    _token_home(tmp_path, monkeypatch)
    setattr_bound(monkeypatch, jobs_common, 'request_json', mock.Mock(side_effect=_http_error(401)))
    with pytest.raises(RuntimeError, match='401/403'):
        jobs_sources.thingiverse_inventory(THING_URL)


def test_thingiverse_download_and_cache_reuse(tmp_path, monkeypatch):
    _token_home(tmp_path, monkeypatch)
    _api(monkeypatch)
    job = jobs_sources.thingiverse_inventory(THING_URL)
    chosen = job['sources'][0]
    downloads = []

    def fake_download(url, target, headers=None):
        downloads.append((url, headers))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'solid benchy')
        return target

    monkeypatch.setattr(jobs_sources, 'download', fake_download)
    out = jobs_sources.thingiverse_download(job, chosen, tmp_path / 'src')
    assert out == tmp_path / 'src' / 'benchy.stl' and out.read_bytes() == b'solid benchy'
    assert downloads[0][0] == chosen['download_url']
    assert downloads[0][1]['Authorization'] == 'Bearer test-token'

    # Cache reuse: an archived JOB.json naming the same file id + model is reused
    # and the site is not contacted again.
    archive = tmp_path / 'archive'
    folder = archive / 'old-job'
    folder.mkdir(parents=True)
    cached_file = folder / 'source' / 'benchy.stl'
    cached_file.parent.mkdir()
    cached_file.write_bytes(b'solid benchy')
    (folder / 'JOB.json').write_text(json.dumps({
        'site': 'thingiverse', 'model_id': '763622',
        'selected_source': {'id': 101}, 'downloaded_file': str(cached_file)}))
    setattr_bound(monkeypatch, jobs_common, 'ARCHIVE', archive)
    setattr_bound(monkeypatch, jobs_common, 'request_json', mock.Mock(side_effect=AssertionError('site contacted')))
    monkeypatch.setattr(jobs_sources, 'download', mock.Mock(side_effect=AssertionError('site contacted')))
    reused = jobs_sources.thingiverse_download(job, chosen, tmp_path / 'src2')
    assert reused.read_bytes() == b'solid benchy'


def test_thingiverse_download_challenge_is_reported_plainly(tmp_path, monkeypatch):
    _token_home(tmp_path, monkeypatch)
    setattr_bound(monkeypatch, jobs_common, 'ARCHIVE', tmp_path / 'empty-archive')
    monkeypatch.setattr(jobs_sources, 'download',
                        mock.Mock(side_effect=_http_error(403, {'Cf-Mitigated': 'challenge'})))
    job = {'site': 'thingiverse', 'model_id': '763622'}
    chosen = {'id': 101, 'name': 'benchy.stl', 'download_url': 'https://api.thingiverse.com/files/101/download'}
    with pytest.raises(RuntimeError, match='Cloudflare robot check'):
        jobs_sources.thingiverse_download(job, chosen, tmp_path / 'src')


def test_printables_empty_geometry_names_the_real_reason(monkeypatch):
    # Live-audited shape for model 3111 (#462 Stage 0): stls/otherFiles empty, gcodes only.
    payload = {'data': {'print': {
        'id': '3111', 'name': '3D BENCHY  (5 COLORS)', 'slug': '3d-benchy-5-colors',
        'license': {'name': 'Creative Commons - Attribution - Share Alike'},
        'stls': [], 'otherFiles': [],
        'gcodes': [{'id': '2636', 'name': 'benchy.gcode', 'fileSize': 17007184}]}}}
    monkeypatch.setattr(jobs_sources, 'gql', lambda q, v: payload)
    job = jobs_sources.printables_inventory('https://www.printables.com/model/3111-3d-benchy')
    assert job['sources'] and all(s['kind'] == 'gcodes' for s in job['sources'])
    assert 'exposes no STL/3MF via the Printables API' in job['no_geometry_reason']
    assert 'only pre-sliced G-code' in job['no_geometry_reason']


def test_printables_with_geometry_has_no_refusal_reason(monkeypatch):
    payload = {'data': {'print': {
        'id': '67300', 'name': 'Ghost', 'slug': 'ghost', 'license': {'name': 'CC'},
        'stls': [{'id': '1', 'name': 'ghost.stl', 'fileSize': 5}], 'gcodes': [], 'otherFiles': []}}}
    monkeypatch.setattr(jobs_sources, 'gql', lambda q, v: payload)
    job = jobs_sources.printables_inventory('https://www.printables.com/model/67300-ghost')
    assert 'no_geometry_reason' not in job


def test_prepared_intake_thingiverse_end_to_end(env, monkeypatch):
    src = source(env.tmp)
    monkeypatch.setattr(jobs_sources, 'thingiverse_inventory', lambda url: {
        'site': 'thingiverse', 'model_id': '763622', 'slug': 'benchy', 'title': 'Benchy',
        'license': 'Creative Commons - Attribution',
        'sources': [{'id': 101, 'name': 'benchy.stl', 'bytes': 10, 'kind': 'files'}]})
    monkeypatch.setattr(jobs_sources, 'thingiverse_download',
                        lambda job, chosen, dest: _copy_named(src, dest, 'benchy.stl'))
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='tv1', text=THING_URL)
    adapter = Adapter([1, 0, 0])
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    job = json.loads(Path(receipt['job_json']).read_text())
    assert result['action'] == 'rewrite' or receipt['status'] == 'STARTED'
    assert job['site'] == 'thingiverse' and job['selected_source']['name'] == 'benchy.stl'
    assert len(env.starts) == 1


def test_prepared_intake_printables_gcode_only_refuses_with_specific_reason(env, monkeypatch):
    monkeypatch.setattr(jobs_sources, 'printables_inventory', lambda url: {
        'site': 'printables', 'model_id': '3111', 'slug': 'benchy', 'title': 'Benchy',
        'sources': [{'id': '2636', 'name': 'benchy.gcode', 'kind': 'gcodes'}],
        'no_geometry_reason': 'Printables listing for model 3111 exposes no STL/3MF via the '
                              'Printables API (stls is empty; only pre-sliced G-code is listed)'})
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='p3111', text='https://www.printables.com/model/3111-3d-benchy')
    adapter = Adapter([])
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert receipt['status'] == 'FAILED'
    assert 'exposes no STL/3MF via the Printables API' in receipt['error']
    assert not env.starts


def _copy_named(src, dest, name):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / name
    out.write_bytes(Path(src).read_bytes())
    return out
