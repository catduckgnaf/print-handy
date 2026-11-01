"""Creality Cloud provider (#604): API inventory + token-gated download.

Built against the web app's own JSON API, verified live 2026-10-29 for the
anonymous legs (model detail and the source-file list answered code 0 for
the acceptance model) and for the download gate (memberDownload answered
{"code": 4, "msg": "Invalid Token"} without a login token). No real token
exists on the build machine, so the download success path is tested here
with fake transports shaped like the recorded API envelopes, and the PR
body names that as unverified live. The credential is a token file holding
the model_token cookie value, mirroring the other model sites' token
files. All fixtures below are trimmed from the live responses for
bag-clip-30mm (model group 6aa15cfc305edea496978f0f).
"""
import asyncio
import http.client
import io
import json
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.core import link_intake, model_job, prepared_intake
from .test_color_intake import state  # noqa: F401  (used by the e2e flow)
from .test_prepared_intake import Adapter, env  # noqa: F401  (env is the shared harness fixture)

URL = ('https://www.crealitycloud.com/model-detail/bag-clip-30mm'
       '?profileId=6aa15cfc305edea496978f1d')
PLAIN_URL = 'https://www.crealitycloud.com/model-detail/bag-clip-30mm'
SLUG = 'bag-clip-30mm'
GROUP_ID = '6aa15cfc305edea496978f0f'
PROFILE_ID = '6aa15cfc305edea496978f1d'
UID = '1234567890'
TOKEN = f'{UID}:test-login-token'  # model_user_id:model_token

DETAIL = {'code': 0, 'msg': 'ok', 'result': {'groupItem': {
    'id': GROUP_ID, 'groupName': 'Bag Closure Clips', 'license': 'CXY-SL',
    'isPay': False, 'urlAlias': SLUG, 'modelCount': 5, 'gcodeCount': 7,
    'model3mfCount': 5, 'userInfo': {'nickName': 'TuTu YU'}}}}

FILE_LIST = {'code': 0, 'msg': 'ok', 'result': {'count': 5, 'list': [
    {'id': '6aa15cfc305edea496978f10', 'fileName': 'bag clip 150mm',
     'fileFormat': '.stl', 'fileSize': 702984,
     'fileKey': 'model/ef4a559769a92ed015a9470742bafad3.cxbin'},
    {'id': '6aa15cfc305edea496978f12', 'fileName': 'bag clip 70mm',
     'fileFormat': '.stl', 'fileSize': 686084,
     'fileKey': 'model/e9a5e03a0303955ec2ce05093f187689.cxbin'},
    {'id': '6aa15cfc305edea496978f13', 'fileName': 'bag clip 30mm',
     'fileFormat': '.stl', 'fileSize': 723384,
     'fileKey': 'model/20724d1675475928646bf52029a3c336.cxbin'},
    {'id': '6aa15cfc305edea496978f14', 'fileName': 'bag clip 200mm',
     'fileFormat': '.stl', 'fileSize': 740884,
     'fileKey': 'model/707015314188944d5e5100ea422e720e.cxbin'},
    {'id': '6aa15cfc305edea496978f11', 'fileName': 'bag clip 100mm',
     'fileFormat': '.stl', 'fileSize': 682084,
     'fileKey': 'model/851c0d4f9be083b7fbad89846162d9ec.cxbin'},
    {'id': 'readme1', 'fileName': 'assembly notes', 'fileFormat': '.pdf',
     'fileSize': 12000, 'fileKey': 'model/notes.cxbin'}]}}

PROFILE_LIST = {'code': 0, 'msg': 'ok', 'result': {'count': 5, 'list': [
    {'id': PROFILE_ID, 'secondName': 'Bag Closure Clips - 30mm',
     'printerName': 'K1',
     'preParse3mfResult': {'allObjectInfos': [
         {'object_id': 2, 'object_name': 'bag clip 30mm.stl'}]}},
    {'id': '6aa15f8b47e36c784a4a5802', 'secondName': 'Bag Closure Clips - 100mm',
     'printerName': 'K2 Plus',
     'preParse3mfResult': {'allObjectInfos': [
         {'object_id': 2, 'object_name': 'bag clip 100mm.stl'}]}}]}}


def write_token(home: Path, content: str = TOKEN) -> Path:
    folder = home/'.config'/'crealitycloud'
    folder.mkdir(parents=True)
    path = folder/'token'
    path.write_text(content, encoding='utf-8')
    return path


@pytest.fixture
def token_home(tmp_path, monkeypatch):
    home = tmp_path/'home'
    home.mkdir()
    write_token(home)
    monkeypatch.setenv('HOME', str(home))
    return home


def api_fake(detail=DETAIL, files=FILE_LIST, profiles=PROFILE_LIST,
             download_result=None, calls=None):
    """A _crealitycloud_request stand-in routed by API path, recording calls."""
    def fake(path, body, token=None):
        if calls is not None:
            calls.append((path, body, token))
        if path.endswith('/modelGroupDetail'): return detail
        if path.endswith('/fileListPage'): return files
        if path.endswith('/3mfList'): return profiles
        if path.endswith('/memberDownload'): return download_result
        raise AssertionError(f'unexpected API call: {path}')
    return fake


@pytest.mark.parametrize('text', [
    URL,
    PLAIN_URL,
    'https://crealitycloud.com/model-detail/bag-clip-30mm',
    'https://www.crealitycloud.com/de/model-detail/bag-clip-30mm',
    f'check this {URL} please',
])
def test_model_link_accepts_crealitycloud(text):
    assert link_intake.model_link(text)


@pytest.mark.parametrize('text', [
    'https://www.crealitycloud.com/model-detail/',
    'https://www.crealitycloud.com/models',
    'https://www.crealitycloud.com/user-center/downloads',
    'https://example.com/model-detail/bag-clip-30mm',
    'no link at all',
])
def test_model_link_rejects_non_model_crealitycloud(text):
    assert link_intake.model_link(text) is None


def test_crealitycloud_slug_and_profile_id():
    assert model_job.crealitycloud_slug(URL) == SLUG
    assert model_job.crealitycloud_slug(PLAIN_URL) == SLUG
    assert model_job.crealitycloud_profile_id(URL) == PROFILE_ID
    assert model_job.crealitycloud_profile_id(PLAIN_URL) is None


def test_crealitycloud_slug_rejects_non_model_url():
    with pytest.raises(ValueError, match='Creality Cloud URL needs'):
        model_job.crealitycloud_slug('https://www.crealitycloud.com/user/6087373030')


def test_crealitycloud_token_missing_names_the_fix(tmp_path, monkeypatch):
    home = tmp_path/'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    with pytest.raises(RuntimeError) as caught:
        model_job.crealitycloud_token()
    message = str(caught.value)
    assert '.config/crealitycloud/token' in message.replace('\\', '/')
    assert 'model_token' in message


def test_crealitycloud_token_empty_refuses(token_home):
    (token_home/'.config'/'crealitycloud'/'token').write_text('  \n')
    with pytest.raises(RuntimeError, match='token empty'):
        model_job.crealitycloud_token()


def test_crealitycloud_inventory_happy_path(monkeypatch):
    calls = []
    monkeypatch.setattr(model_job, '_crealitycloud_request', api_fake(calls=calls))
    job = model_job.crealitycloud_inventory(URL)
    assert job['site'] == 'crealitycloud'
    assert job['model_id'] == GROUP_ID
    assert job['title'] == 'Bag Closure Clips'
    assert job['slug'] == SLUG
    assert job['license'] == 'CXY-SL'
    names = [row['name'] for row in job['sources']]
    # The non-geometry .pdf row is filtered out, and the file the link's
    # profileId points at (inside the 30mm print profile) is offered first.
    assert names == ['bag clip 30mm.stl', 'bag clip 150mm.stl',
                     'bag clip 70mm.stl', 'bag clip 200mm.stl',
                     'bag clip 100mm.stl']
    assert job['sources'][0]['suggested_by_link'] is True
    assert job['sources'][0]['bytes'] == 723384
    detail_call = next(c for c in calls if c[0].endswith('/modelGroupDetail'))
    assert detail_call[1] == {'id': SLUG}
    # Inventory is anonymous: no token is passed.
    assert detail_call[2] is None


def test_crealitycloud_inventory_without_profile_keeps_site_order(monkeypatch):
    monkeypatch.setattr(model_job, '_crealitycloud_request', api_fake())
    job = model_job.crealitycloud_inventory(PLAIN_URL)
    assert job['sources'][0]['name'] == 'bag clip 150mm.stl'
    assert not any(row.get('suggested_by_link') for row in job['sources'])


def test_crealitycloud_inventory_no_geometry_names_the_reason(monkeypatch):
    empty = {'code': 0, 'msg': 'ok', 'result': {'count': 0, 'list': []}}
    monkeypatch.setattr(model_job, '_crealitycloud_request',
                        api_fake(files=empty, profiles=empty))
    job = model_job.crealitycloud_inventory(PLAIN_URL)
    assert job['sources'] == []
    assert 'no STL/3MF source' in job['no_geometry_reason']
    assert 'pre-sliced G-code' in job['no_geometry_reason']


def test_crealitycloud_inventory_model_not_found(monkeypatch):
    gone = {'code': 0, 'msg': 'ok', 'result': {'groupItem': None}}
    monkeypatch.setattr(model_job, '_crealitycloud_request', api_fake(detail=gone))
    with pytest.raises(RuntimeError, match='model not found'):
        model_job.crealitycloud_inventory(PLAIN_URL)


def test_crealitycloud_inventory_deleted_model_code(monkeypatch):
    deleted = {'code': 517, 'msg': 'The item does not exist', 'result': None}
    monkeypatch.setattr(model_job, '_crealitycloud_request', api_fake(detail=deleted))
    with pytest.raises(RuntimeError, match='does not exist'):
        model_job.crealitycloud_inventory(PLAIN_URL)


def test_crealitycloud_code4_is_a_token_refusal(monkeypatch):
    refused = {'code': 4, 'msg': 'Invalid Token', 'result': None}
    monkeypatch.setattr(model_job, '_crealitycloud_request', api_fake(detail=refused))
    with pytest.raises(RuntimeError) as caught:
        model_job.crealitycloud_inventory(PLAIN_URL)
    message = str(caught.value)
    assert 'login token' in message and 'model_token' in message


def test_crealitycloud_headers_carry_the_required_fields():
    headers = model_job.crealitycloud_headers(TOKEN)
    # The saved login splits into the two headers the web client sends;
    # the token alone is refused as "Invalid Token" (verified live).
    assert headers['__CXY_TOKEN_'] == 'test-login-token'
    assert headers['__CXY_UID_'] == UID
    assert headers['__CXY_APP_ID_'] == 'cxy-gen2'
    # Without __CXY_TIMEZONE_ the live API refuses with code 1.
    assert headers['__CXY_TIMEZONE_'].lstrip('-').isdigit()


def test_crealitycloud_request_sends_token_and_reads_gzip(monkeypatch):
    import gzip as gz
    seen = {}

    class Resp:
        headers = {'Content-Encoding': 'gzip'}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return gz.compress(json.dumps(DETAIL).encode())

    def fake_urlopen(req, timeout=None):
        seen['url'] = req.full_url
        seen['headers'] = {k.lower(): v for k, v in req.header_items()}
        seen['body'] = json.loads(req.data.decode())
        return Resp()
    monkeypatch.setattr(model_job.urllib.request, 'urlopen', fake_urlopen)
    out = model_job._crealitycloud_request(
        '/api/cxy/v3/model/modelGroupDetail', {'id': SLUG}, token=TOKEN)
    assert out == DETAIL
    assert seen['url'].endswith('/api/cxy/v3/model/modelGroupDetail')
    assert seen['body'] == {'id': SLUG}
    assert seen['headers']['__cxy_token_'] == 'test-login-token'
    assert seen['headers']['__cxy_uid_'] == UID
    assert seen['headers']['__cxy_app_id_'] == 'cxy-gen2'
    assert seen['headers']['accept-encoding'] == 'gzip'
    assert seen['headers']['__cxy_timezone_'].lstrip('-').isdigit()


def test_crealitycloud_result_retries_a_dropped_read(monkeypatch):
    # The API's edge drops some reads mid-response (IncompleteRead); the
    # calls are idempotent, so a retry must recover instead of failing.
    attempts = []

    def flaky(path, body, token=None):
        attempts.append(path)
        if len(attempts) < 3:
            raise http.client.IncompleteRead(b'partial', 100)
        return DETAIL
    monkeypatch.setattr(model_job, '_crealitycloud_request', flaky)
    monkeypatch.setattr(model_job.time, 'sleep', lambda s: None)
    result = model_job._crealitycloud_result(
        '/api/cxy/v3/model/modelGroupDetail', {'id': SLUG})
    assert result['groupItem']['id'] == GROUP_ID
    assert len(attempts) == 3


def test_crealitycloud_http_403_is_plain(monkeypatch):
    def refuse(path, body, token=None):
        raise urllib.error.HTTPError(PLAIN_URL, 403, 'Forbidden', None, None)
    monkeypatch.setattr(model_job, '_crealitycloud_request', refuse)
    with pytest.raises(RuntimeError, match='does not try to bypass'):
        model_job.crealitycloud_inventory(PLAIN_URL)


def _download_setup(monkeypatch, tmp_path, download_result, payload=b'solid fake\n'):
    """Wire a fake memberDownload + file download; returns (job, source, calls)."""
    calls = []
    monkeypatch.setattr(model_job, '_crealitycloud_request',
                        api_fake(download_result=download_result, calls=calls))

    def fake_download(url, target, headers=None):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(payload(url) if callable(payload) else payload)
    monkeypatch.setattr(model_job, 'download', fake_download)
    job = {'site': 'crealitycloud', 'model_id': GROUP_ID}
    source = {'id': '6aa15cfc305edea496978f13', 'name': 'bag clip 30mm.stl'}
    return job, source, calls


def test_crealitycloud_download_string_url(token_home, monkeypatch, tmp_path):
    result = {'code': 0, 'msg': 'ok', 'result': 'https://cdn.example/files/30mm.stl'}
    job, source, calls = _download_setup(monkeypatch, tmp_path, result)
    dest = model_job.crealitycloud_download(job, source, tmp_path/'src')
    assert dest == tmp_path/'src'/'bag clip 30mm.stl'
    assert dest.read_bytes() == b'solid fake\n'
    call = next(c for c in calls if c[0].endswith('/memberDownload'))
    assert call[1] == {'modelId': GROUP_ID, 'trailType': 0,
                       'fileIds': ['6aa15cfc305edea496978f13']}
    assert call[2] == TOKEN


def test_crealitycloud_download_dict_url(token_home, monkeypatch, tmp_path):
    result = {'code': 0, 'msg': 'ok',
              'result': {'downloadUrl': 'https://cdn.example/files/30mm.stl'}}
    job, source, _ = _download_setup(monkeypatch, tmp_path, result)
    dest = model_job.crealitycloud_download(job, source, tmp_path/'src')
    assert dest.read_bytes() == b'solid fake\n'


def test_crealitycloud_download_zip_answer(token_home, monkeypatch, tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('bag clip 30mm.stl', b'solid zipped\n')
        archive.writestr('readme.txt', b'hello')
    result = {'code': 0, 'msg': 'ok',
              'result': {'downloadZip': 'https://cdn.example/files/model.zip'}}
    job, source, _ = _download_setup(monkeypatch, tmp_path, result,
                                     payload=buffer.getvalue())
    dest = model_job.crealitycloud_download(job, source, tmp_path/'src')
    assert dest.name == 'bag clip 30mm.stl'
    assert dest.read_bytes() == b'solid zipped\n'
    assert not (tmp_path/'src'/'bag clip 30mm.stl.download').exists()


def test_crealitycloud_download_answer_without_url_is_a_named_gap(
        token_home, monkeypatch, tmp_path):
    result = {'code': 0, 'msg': 'ok', 'result': {'unexpected': True}}
    job, source, _ = _download_setup(monkeypatch, tmp_path, result)
    with pytest.raises(RuntimeError, match='could not find a file URL'):
        model_job.crealitycloud_download(job, source, tmp_path/'src')


def test_crealitycloud_download_refused_token(token_home, monkeypatch, tmp_path):
    refused = {'code': 4, 'msg': 'Invalid Token', 'result': None}
    job, source, _ = _download_setup(monkeypatch, tmp_path, refused)
    with pytest.raises(RuntimeError, match='refused the login token'):
        model_job.crealitycloud_download(job, source, tmp_path/'src')


def test_crealitycloud_download_without_token_never_networks(
        tmp_path, monkeypatch):
    home = tmp_path/'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setattr(model_job, '_crealitycloud_request',
                        mock.Mock(side_effect=AssertionError('network attempted')))
    job = {'site': 'crealitycloud', 'model_id': GROUP_ID}
    with pytest.raises(RuntimeError, match='need a login token'):
        model_job.crealitycloud_download(
            job, {'id': 'f1', 'name': 'x.stl'}, tmp_path/'src')


def test_registry_routes_crealitycloud(monkeypatch):
    monkeypatch.setattr(model_job, '_crealitycloud_request', api_fake())
    job = model_job.inventory_for_url(URL)
    assert job['site'] == 'crealitycloud'
    with pytest.raises(ValueError) as caught:
        model_job.inventory_for_url('https://unknown.example/model/1')
    assert 'Creality Cloud' in str(caught.value)


def test_design_fetch_crealitycloud_names_multi_file_refusal(monkeypatch):
    from print_handy.core import design
    monkeypatch.setattr(model_job, '_crealitycloud_request',
                        api_fake(profiles={'code': 0, 'result': {'list': []}}))
    with pytest.raises(design.DesignError, match='cannot pick'):
        design.fetch_link_source(PLAIN_URL, Path('/tmp/cc-design'))


def test_design_fetch_crealitycloud_uses_profile_file(
        token_home, monkeypatch, tmp_path):
    from print_handy.core import design
    seen = {}

    def fake_download(job, source, target):
        seen['source'] = source
        dest = Path(target)/source['name']
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b'solid fake\n')
        return dest
    monkeypatch.setattr(model_job, '_crealitycloud_request', api_fake())
    monkeypatch.setattr(model_job, 'crealitycloud_download', fake_download)
    dest = design.fetch_link_source(URL, tmp_path/'src')
    assert dest.name == 'bag clip 30mm.stl'
    assert seen['source']['suggested_by_link'] is True


def test_prepared_intake_crealitycloud_stops_at_token_gap(
        env, token_home, monkeypatch):
    # token_home exists but this test removes the file: inventory succeeds,
    # the single source is chosen, the connect-account offer (#464) is
    # cancelled, and the download stops with a short cancellation cause
    # in the receipt and one terminal post — nothing sliced or started.
    (token_home/'.config'/'crealitycloud'/'token').unlink()
    job = {'site': 'crealitycloud', 'model_id': GROUP_ID,
           'title': 'Bag Closure Clips', 'slug': SLUG, 'license': 'CXY-SL',
           'sources': [{'id': 'f1', 'name': 'bag clip 30mm.stl',
                        'kind': 'files'}]}
    monkeypatch.setattr(model_job, 'crealitycloud_inventory', lambda url: job)
    previews = []

    async def fake_preview(*args, **kw):
        previews.append(args)
    monkeypatch.setattr(prepared_intake, 'send_preview', fake_preview)
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None,
                       user_id='u'),
              message_id='cc1', text=PLAIN_URL)
    adapter = Adapter([1])
    asyncio.run(prepared_intake.prepare_link(
        event, adapter, state(), env.root, timeout=1))
    assert adapter.questions[0]['choices'][:2] == ['Saved — try again', 'Cancel']
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert receipt['status'] == 'CANCELLED'
    assert receipt['error'] == 'connect cancelled by operator'
    assert receipt['sliced'] is False and receipt['started'] is False
    assert adapter.sent.count('Cancelled. Nothing was downloaded.') == 1
    assert adapter.sent[-1] == 'Cancelled. Nothing was downloaded.'


def test_crealitycloud_download_3mf_is_not_unpacked(token_home, monkeypatch, tmp_path):
    # A 3MF is itself a zip; the download must keep it whole rather than
    # treat it as an archive of model files (and refuse for finding none).
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types/>')
        archive.writestr('3D/3dmodel.model', '<model/>')
    result = {'code': 0, 'msg': 'ok', 'result': 'https://cdn.example/files/clip.3mf'}
    job, _, _ = _download_setup(monkeypatch, tmp_path, result, payload=buffer.getvalue())
    dest = model_job.crealitycloud_download(
        job, {'id': 'f3', 'name': 'bag clip.3mf'}, tmp_path/'src')
    assert dest.name == 'bag clip.3mf'
    assert dest.read_bytes() == buffer.getvalue()


def test_crealitycloud_token_without_user_id_is_named(token_home):
    (token_home/'.config'/'crealitycloud'/'token').write_text('only-the-model-token\n')
    with pytest.raises(RuntimeError) as caught:
        model_job.crealitycloud_token()
    assert 'model_user_id:model_token' in str(caught.value)
    assert caught.value.reason == 'missing'


def test_crealitycloud_download_live_answer_shape(token_home, monkeypatch, tmp_path):
    # memberDownload's success answer as recorded live on 2026-10-29 (URL
    # trimmed): downloadUrls lists one entry per requested file id.
    result = {'code': 0, 'msg': 'ok', 'result': {'downloadUrls': [
        {'fileName': 'bag clip 70mm', 'fileId': '6aa15cfc305edea496978f12',
         'url': 'https://oss.example/model%2Fother.stl?Signature=1', 'expire': 1793378685},
        {'fileName': 'bag clip 30mm', 'fileId': '6aa15cfc305edea496978f13',
         'url': 'https://oss.example/model%2F743d4cb8.stl?Signature=1', 'expire': 1793378685}]}}
    seen = []
    job, source, _ = _download_setup(monkeypatch, tmp_path, result,
                                     payload=lambda url: seen.append(url) or b'solid fake\n')
    dest = model_job.crealitycloud_download(job, source, tmp_path/'src')
    assert dest.name == 'bag clip 30mm.stl'
    assert seen == ['https://oss.example/model%2F743d4cb8.stl?Signature=1']
