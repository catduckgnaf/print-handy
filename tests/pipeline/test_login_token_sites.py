"""Thangs, Nexprint and MakerOnline providers plus the connect-account flow (#464).

Each provider uses the JSON API its own web app calls. Verified live on
2026-10-29: the anonymous legs (model detail and file list) of all three;
each download gate refusing a tokenless or bogus-token request; a real
Nexprint and a real Thangs download with operator tokens (the success
answers below have those live shapes); and MakerOnline refusing every
download ("system busy") with a valid token but no browser fingerprint
token, which is why MakerOnline is inventory-only.
"""
import asyncio
import base64
import io
import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.core import link_intake, model_job, prepared_intake, site_accounts
from print_handy.core.site_accounts import SiteLoginNeeded
from .test_color_intake import state  # noqa: F401  (used by the e2e flow)
from .test_prepared_intake import Adapter, env  # noqa: F401  (env is the shared harness fixture)

THANGS_URL = 'https://thangs.com/designer/Hobson318/3d-model/Nimitz%20Benchy-29424'
NEXPRINT_URL = 'https://www.nexprint.com/en/models/1970971018767982592'
MAKERONLINE_URL = 'https://makeronline.com/en/model/Articulated_Pixel_Heart/1000.html'

THANGS_MODEL = {
    'id': '29424', 'name': 'Nimitz Benchy', 'visibility': 'public', 'plans': [],
    'marketplaceInfo': None, 'license': 'f543a901-ecec-435a-8413-cc6ba77a47a2/LICENSE.txt',
    'owner': {'username': 'Hobson318'},
    'parts': [
        {'name': 'Nimitz Benchy', 'size': None,
         'filename': 'f543a901/Nimitz_Class_Benchy_Launching_Benchies.stl',
         'originalFileName': 'Nimitz_Class_Benchy_Launching_Benchies.stl',
         'partIdentifier': '2fff24cf777145de0ed1be806ab95ce3', 'isPrimary': True},
        {'name': 'Nimitz_Class_Benchy_V1.3.stl', 'size': 1234,
         'filename': 'f543a901/Nimitz_Class_Benchy_V1.3.stl',
         'originalFileName': 'Nimitz_Class_Benchy_V1.3.stl',
         'partIdentifier': '17353846e1d348e5d55a49954310d684', 'isPrimary': False},
        {'name': 'notes', 'size': 10, 'filename': 'f543a901/notes.pdf',
         'originalFileName': 'notes.pdf', 'partIdentifier': 'pdf1', 'isPrimary': False}]}

NEXPRINT_MODEL = {'code': 0, 'data': {
    'id': '1970971018767982592', 'modelId': 'G0166578',
    'modelName': 'Articulated Dragon 001', 'licenseType': 7, 'authorName': 'TORUA3D',
    'modelFileList': [
        {'fileId': 2494062, 'fileName': 'ARTICULATED DRAGON 001.stl', 'fileSize': 64311784.0,
         'fileUrl': None, 'fileExt': 'stl', 'isCreator': True},
        {'fileId': 2564408, 'fileName': 'articulated dragon.3mf', 'fileSize': 20176213.0,
         'fileUrl': None, 'fileExt': '3mf', 'isCreator': False},
        {'fileId': 2564408, 'fileName': 'articulated dragon.3mf', 'fileSize': 20176213.0,
         'fileUrl': None, 'fileExt': '3mf', 'isCreator': False},
        {'fileId': 77, 'fileName': 'dragon.step', 'fileSize': 9.0, 'fileExt': 'step'}],
    'modelAttachList': [],
    'settingInfoList': [
        {'id': '1972478188160307200', 'settingName': 'High Detail Fast Print',
         'settingFile': {'fileId': 2564408, 'fileName': 'articulated dragon.3mf'}}]}}

MAKERONLINE_MODEL = {'code': 0, 'message': 'Successful operation', 'data': {
    'id': 1000, 'title': 'Articulated Pixel Heart Keychain', 'license': 6,
    'is_premium': True,
    'files': [{'url': 'https://acop-prod-private.s3.us-east-2.amazonaws.com/asop/x.stl',
               'file_name': 'Articulated Pixel Heart Keychain Frikarte3D.stl',
               'file_size': 317484, 'key': '8816dd006d34f1a02a6ac631dd95bb2d', 'file_id': 3372}],
    'docs': [], 'print_profiles': [{'id': 30634, 'title': 'Articulated pixel heart'}]}}


def web_fake(routes, calls=None):
    """A _web_api stand-in: routes maps a URL substring to (status, body)."""
    def fake(method, url, *, body=None, headers=None, timeout=25):
        if calls is not None:
            calls.append({'method': method, 'url': url, 'body': body, 'headers': headers or {}})
        for part, answer in routes.items():
            if part in url:
                return answer
        raise AssertionError(f'unexpected API call: {url}')
    return fake


def fake_file_download(monkeypatch, payload=b'solid fake\n', seen=None):
    def fake(url, target, headers=None):
        if seen is not None:
            seen.append(url)
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(payload)
    monkeypatch.setattr(model_job, 'download', fake)


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path/'home'
    path.mkdir()
    monkeypatch.setenv('HOME', str(path))
    for acc in site_accounts.LOGIN_SITES.values():
        monkeypatch.delenv(acc.env_name, raising=False)
    return path


def save_token(site, value='test-login-token'):
    path = site_accounts.token_path(site)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + '\n', encoding='utf-8')
    return path


def jwt(exp):
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip('=')
    return f"{part({'alg': 'HS256'})}.{part({'exp': exp})}.sig"


# ---------------------------------------------------------------- links

@pytest.mark.parametrize('text', [
    THANGS_URL,
    'https://thangs.com/m/29424',
    'https://www.thangs.com/designer/LoftedGoods/3d-model/The%20Rail%20-%20Tripod%20Attachment-1463861',
    NEXPRINT_URL,
    'https://www.nexprint.com/models/1970971018767982592',
    'https://www.nexprint.com/de/models/G0166578',
    MAKERONLINE_URL,
    'https://www.makeronline.com/model/Water_Absorbent_Ammo_Tank/341128.html?trackModuleType=1',
    f'look at this {THANGS_URL} please',
])
def test_model_link_accepts_login_sites(text):
    assert link_intake.model_link(text)


@pytest.mark.parametrize('text', [
    'https://thangs.com/designer/Hobson318',
    'https://thangs.com/search/benchy',
    'https://www.nexprint.com/en/models',
    'https://www.nexprint.com/en/user/123',
    'https://makeronline.com/en/',
    'https://makeronline.com/en/model/no-id.html',
    'https://example.com/m/29424',
])
def test_model_link_rejects_non_model_pages(text):
    assert link_intake.model_link(text) is None


def test_ids_from_urls():
    assert model_job.thangs_id(THANGS_URL) == '29424'
    assert model_job.thangs_id('https://thangs.com/m/29424') == '29424'
    assert model_job.nexprint_id(NEXPRINT_URL) == '1970971018767982592'
    assert model_job.makeronline_id(MAKERONLINE_URL) == '1000'
    for parse, url in ((model_job.thangs_id, 'https://thangs.com/designer/x'),
                       (model_job.nexprint_id, 'https://www.nexprint.com/en/'),
                       (model_job.makeronline_id, 'https://makeronline.com/en/')):
        with pytest.raises(ValueError):
            parse(url)


def test_registry_routes_the_new_sites(monkeypatch):
    monkeypatch.setattr(model_job, '_web_api', web_fake({
        '/api/models/v2/29424': (200, THANGS_MODEL),
        'model-base-info/get': (200, NEXPRINT_MODEL),
        '/api/mold/detail': (200, MAKERONLINE_MODEL)}))
    assert model_job.inventory_for_url(THANGS_URL)['site'] == 'thangs'
    assert model_job.inventory_for_url(NEXPRINT_URL)['site'] == 'nexprint'
    assert model_job.inventory_for_url(MAKERONLINE_URL)['site'] == 'makeronline'
    with pytest.raises(ValueError) as caught:
        model_job.inventory_for_url('https://unknown.example/model/1')
    assert 'Thangs, Nexprint, MakerOnline' in str(caught.value)


# ---------------------------------------------------------------- tokens

def test_missing_token_names_the_file_and_cookie(home):
    with pytest.raises(SiteLoginNeeded) as caught:
        site_accounts.read_token('thangs')
    assert caught.value.site == 'thangs' and caught.value.reason == 'missing'
    message = str(caught.value).replace('\\', '/')
    assert '.config/thangs/token' in message and 'accessToken' in message


def test_token_from_environment_variable(home, monkeypatch):
    monkeypatch.setenv('THANGS_TOKEN', 'Bearer env-token')
    assert site_accounts.read_token('thangs') == 'env-token'
    # A saved file (what the chat connect flow writes) wins over the variable.
    save_token('thangs', 'file-token')
    assert site_accounts.read_token('thangs') == 'file-token'


def test_missing_token_names_the_variable_too(home, monkeypatch):
    monkeypatch.delenv('NEXPRINT_TOKEN', raising=False)
    with pytest.raises(SiteLoginNeeded) as caught:
        site_accounts.read_token('nexprint')
    assert 'NEXPRINT_TOKEN' in str(caught.value)
    assert 'NEXPRINT_TOKEN' in str(caught.value)


def test_crealitycloud_reads_its_variable(home, monkeypatch):
    monkeypatch.setenv('CREALITYCLOUD_TOKEN', '1234567890:cc-env')
    assert model_job.crealitycloud_token() == '1234567890:cc-env'


def test_empty_token_refuses(home):
    save_token('nexprint', '   ')
    with pytest.raises(SiteLoginNeeded) as caught:
        site_accounts.read_token('nexprint')
    assert caught.value.reason == 'empty'


def test_expired_jwt_is_named_before_any_request(home):
    save_token('thangs', jwt(time.time() - 60))
    with pytest.raises(SiteLoginNeeded) as caught:
        site_accounts.read_token('thangs')
    assert caught.value.reason == 'expired'
    live = jwt(time.time() + 3600)
    save_token('thangs', live)
    assert site_accounts.read_token('thangs') == live


@pytest.mark.parametrize('pasted', [
    'tok123', '"tok123"', 'Bearer tok123', '  bearer tok123\n',
    '{"access_token": "tok123", "refresh_token": "r", "expires_time": 1}',
])
def test_token_file_accepts_common_paste_shapes(home, pasted):
    save_token('nexprint', pasted)
    assert site_accounts.read_token('nexprint') == 'tok123'


def test_connect_steps_is_the_whole_walkthrough_in_one_message(home):
    for site in site_accounts.LOGIN_SITES:
        text = site_accounts.connect_steps(site, lead='**Lead line.**')
        assert text.startswith('**Lead line.**')
        # Paste it here is an option, with the honest risk note beside it.
        assert 'Paste it here' in text and "Don't paste it here" not in text
        assert site_accounts.RISK_NOTE in text and 'may keep a copy' in text
        # Saving on the machine stays an option, with its home-relative path
        # (never the absolute or container path, #712).
        assert site_accounts.display_path(site) in text and 'Saved' in text
        assert str(site_accounts.token_path(site)) not in text and '/data/' not in text
        assert '1000:1000' not in text
        # Every walkthrough links its own docs section.
        acc = site_accounts.LOGIN_SITES[site]
        assert acc.docs_url in text and acc.docs_url.startswith(site_accounts.DOCS + '#')
        assert len(text) < 2400, (site, len(text))  # one chat message's worth
    assert site_accounts.LOGIN_SITES['thingiverse'].docs_url.endswith('#thingiverse-app-token')
    assert site_accounts.LOGIN_SITES['cults3d'].docs_url.endswith('#cults3d-api-key')
    assert site_accounts.LOGIN_SITES['myminifactory'].docs_url.endswith('#myminifactory-sign-in')
    assert 'Create an App' in site_accounts.connect_steps('thingiverse')


def test_source_report_shows_every_site_and_never_a_credential(home, monkeypatch):
    for name in ('MAKERWORLD_TOKEN', 'THINGIVERSE_TOKEN', 'CULTS3D_TOKEN', 'MYMINIFACTORY_OAUTH_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    save_token('thingiverse', 'tv-secret-123')
    save_token('thangs', jwt(time.time() - 60))      # expired
    monkeypatch.setenv('NEXPRINT_TOKEN', 'np-secret-456')
    report = site_accounts.source_report()
    lines = {line.split('**')[1]: line for line in report.splitlines()[1:]}
    assert set(lines) == {label for _, label, _, _ in site_accounts.SOURCES}
    assert 'connected (token file)' in lines['Thingiverse']
    assert 'connected (variable)' in lines['Nexprint']
    assert 'login expired' in lines['Thangs'] and 'renewing it' in lines['Thangs']
    assert 'not connected' in lines['MakerWorld'] and 'connecting it' in lines['MakerWorld']
    assert 'no login needed' in lines['Printables']
    assert 'listing only' in lines['MakerOnline'] and 'listing only' in lines['Cults3D']
    assert 'not supported' in lines['GrabCAD']
    assert 'tv-secret-123' not in report and 'np-secret-456' not in report


# ---------------------------------------------------------------- Thangs

def test_thangs_inventory(monkeypatch):
    calls = []
    monkeypatch.setattr(model_job, '_web_api',
                        web_fake({'/api/models/v2/29424': (200, THANGS_MODEL)}, calls))
    job = model_job.thangs_inventory(THANGS_URL)
    assert job['site'] == 'thangs' and job['model_id'] == '29424'
    assert job['title'] == 'Nimitz Benchy' and job['slug'] == 'nimitz-benchy'
    assert job['license'] == 'license file on the model page'
    assert [r['name'] for r in job['sources']] == [
        'Nimitz_Class_Benchy_Launching_Benchies.stl', 'Nimitz_Class_Benchy_V1.3.stl']
    assert job['sources'][1]['id'] == '17353846e1d348e5d55a49954310d684'
    assert 'paid' not in job
    assert 'Authorization' not in calls[0]['headers']  # anonymous


def test_thangs_inventory_marks_membership_models(monkeypatch):
    gated = dict(THANGS_MODEL, visibility='paid', plans=[120])
    monkeypatch.setattr(model_job, '_web_api', web_fake({'/api/models/v2/': (200, gated)}))
    assert model_job.thangs_inventory(THANGS_URL)['paid'] is True


def test_thangs_missing_model_answers_403(monkeypatch):
    monkeypatch.setattr(model_job, '_web_api', web_fake({'/api/models/v2/': (403, 'Forbidden')}))
    with pytest.raises(RuntimeError, match='not found'):
        model_job.thangs_inventory('https://thangs.com/m/999999999')


def _thangs_job():
    return {'site': 'thangs', 'model_id': '29424',
            'sources': [{'id': 'a', 'name': 'Nimitz_Class_Benchy_Launching_Benchies.stl'},
                        {'id': 'b', 'name': 'Nimitz_Class_Benchy_V1.3.stl'}]}


def test_thangs_download_unpacks_the_chosen_part(home, monkeypatch, tmp_path):
    save_token('thangs')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('Nimitz Benchy/Nimitz_Class_Benchy_Launching_Benchies.stl', b'solid a\n')
        archive.writestr('Nimitz Benchy/Nimitz_Class_Benchy_V1.3.stl', b'solid b\n')
    calls = []
    monkeypatch.setattr(model_job, '_web_api', web_fake({'/download-url': (200, {
        'fileName': 'Nimitz Benchy', 'modelSource': '0', 'downloadId': '27975304',
        'signedUrl': 'https://storage.googleapis.com/thangs-thumbnails/production/'
                     'f543a901/Nimitz_Benchy.zip?X-Goog-Signature=1'})}, calls))
    fake_file_download(monkeypatch, buffer.getvalue())
    job = _thangs_job()
    dest = model_job.thangs_download(job, job['sources'][1], tmp_path/'src')
    assert dest.name == 'Nimitz_Class_Benchy_V1.3.stl'
    assert dest.read_bytes() == b'solid b\n'
    assert calls[0]['url'].endswith('/api/v2/models/29424/download-url')
    assert calls[0]['headers']['Authorization'] == 'Bearer test-login-token'


def test_thangs_single_file_answer_must_match_when_several_parts(home, monkeypatch, tmp_path):
    save_token('thangs')
    monkeypatch.setattr(model_job, '_web_api', web_fake({'/download-url': (200, {
        'signedUrl': 'https://storage.example/f/Nimitz_Class_Benchy_Launching_Benchies.stl?sig=1'})}))
    fake_file_download(monkeypatch)
    job = _thangs_job()
    with pytest.raises(RuntimeError, match='not the chosen'):
        model_job.thangs_download(job, job['sources'][1], tmp_path/'src')
    dest = model_job.thangs_download(job, job['sources'][0], tmp_path/'src2')
    assert dest.name == 'Nimitz_Class_Benchy_Launching_Benchies.stl'


@pytest.mark.parametrize('answer,expected', [
    ((401, 'Unauthorized'), SiteLoginNeeded),
    ((200, {'userDownloadData': {'downloadLimitReached': True}}), 'daily download limit'),
    ((200, {'fileName': 'Error'}), 'not ready'),
    ((200, {'something': 'else'}), 'could not find a file URL'),
    ((429, ''), 'rate-limiting'),
])
def test_thangs_download_refusals(home, monkeypatch, tmp_path, answer, expected):
    save_token('thangs')
    monkeypatch.setattr(model_job, '_web_api', web_fake({'/download-url': answer}))
    job = _thangs_job()
    if expected is SiteLoginNeeded:
        with pytest.raises(SiteLoginNeeded) as caught:
            model_job.thangs_download(job, job['sources'][0], tmp_path/'src')
        assert caught.value.reason == 'refused'
    else:
        with pytest.raises(RuntimeError, match=expected):
            model_job.thangs_download(job, job['sources'][0], tmp_path/'src')


def test_thangs_paid_403_names_membership(home, monkeypatch, tmp_path):
    save_token('thangs')
    monkeypatch.setattr(model_job, '_web_api', web_fake({'/download-url': (403, 'Forbidden')}))
    job = dict(_thangs_job(), paid=True)
    with pytest.raises(RuntimeError, match='membership or a purchase') as caught:
        model_job.thangs_download(job, job['sources'][0], tmp_path/'src')
    assert not isinstance(caught.value, SiteLoginNeeded)


# ---------------------------------------------------------------- Nexprint

def test_nexprint_inventory(monkeypatch):
    calls = []
    monkeypatch.setattr(model_job, '_web_api',
                        web_fake({'model-base-info/get': (200, NEXPRINT_MODEL)}, calls))
    job = model_job.nexprint_inventory(NEXPRINT_URL)
    assert job['site'] == 'nexprint' and job['model_id'] == '1970971018767982592'
    assert job['license'] == 'Nexprint Standard License'
    # The duplicate setting row is dropped, the .step file filtered, and the
    # designer's own file listed before the print-setting project.
    assert [(r['name'], r['file_type'], r['biz_id']) for r in job['sources']] == [
        ('ARTICULATED DRAGON 001.stl', '2', '1970971018767982592'),
        ('articulated dragon.3mf', '5', '1972478188160307200')]
    assert job['sources'][1]['title'] == 'articulated dragon.3mf (print setting: High Detail Fast Print)'
    assert job['sources'][0]['bytes'] == 64311784
    assert calls[0]['headers'] == {'User-Lang': 'en'}  # anonymous


def test_nexprint_unknown_id_is_not_found(monkeypatch):
    empty = {'code': 0, 'data': {'id': None, 'modelName': None}}
    monkeypatch.setattr(model_job, '_web_api', web_fake({'model-base-info/get': (200, empty)}))
    with pytest.raises(RuntimeError, match='not found'):
        model_job.nexprint_inventory('https://www.nexprint.com/en/models/1')


def test_nexprint_download(home, monkeypatch, tmp_path):
    save_token('nexprint')
    calls = []
    monkeypatch.setattr(model_job, '_web_api', web_fake({'presigned-download-url': (200, {
        'code': 0, 'data': {'from': None, 'fileInfoList': [{
            'fileId': '2564408', 'type': 5,
            'resultUrl': 'https://nexprint.oss-eu-central-1.aliyuncs.com/models/1/a.3mf?X-Amz-Signature=1'}]}})},
        calls))
    seen = []
    fake_file_download(monkeypatch, seen=seen)
    job = {'site': 'nexprint', 'model_id': '1970971018767982592'}
    source = {'id': '2564408', 'name': 'articulated dragon.3mf', 'file_type': '5',
              'biz_id': '1972478188160307200'}
    dest = model_job.nexprint_download(job, source, tmp_path/'src')
    assert dest.name == 'articulated dragon.3mf'
    assert seen == ['https://nexprint.oss-eu-central-1.aliyuncs.com/models/1/a.3mf?X-Amz-Signature=1']
    assert calls[0]['method'] == 'POST'
    assert calls[0]['body'] == {'fileInfoList': [
        {'fileId': '2564408', 'type': '5', 'bizId': '1972478188160307200'}]}
    assert calls[0]['headers']['Authorization'] == 'Bearer test-login-token'


@pytest.mark.parametrize('envelope,expected', [
    ({'code': 401, 'data': None, 'msg': '账号未登录'}, SiteLoginNeeded),
    ({'code': 1003002050, 'data': None}, 'download limit'),
    ({'code': 403, 'data': None}, 'unusual activity'),
    ({'code': 77, 'data': None}, r'\(code 77\)'),
    ({'code': 0, 'data': {'fileInfoList': [{}]}}, 'could not find a file URL'),
])
def test_nexprint_download_refusals(home, monkeypatch, tmp_path, envelope, expected):
    save_token('nexprint')
    monkeypatch.setattr(model_job, '_web_api',
                        web_fake({'presigned-download-url': (200, envelope)}))
    job = {'site': 'nexprint', 'model_id': '1'}
    source = {'id': '2', 'name': 'a.stl'}
    if expected is SiteLoginNeeded:
        with pytest.raises(SiteLoginNeeded):
            model_job.nexprint_download(job, source, tmp_path/'src')
    else:
        with pytest.raises(RuntimeError, match=expected):
            model_job.nexprint_download(job, source, tmp_path/'src')


# ---------------------------------------------------------------- MakerOnline

def test_makeronline_inventory(monkeypatch):
    calls = []
    monkeypatch.setattr(model_job, '_web_api',
                        web_fake({'/api/mold/detail?id=1000': (200, MAKERONLINE_MODEL)}, calls))
    job = model_job.makeronline_inventory(MAKERONLINE_URL)
    assert job['site'] == 'makeronline' and job['model_id'] == '1000'
    assert job['license'] == 'CC BY-NC-ND'
    assert job['sources'] == [{'id': '8816dd006d34f1a02a6ac631dd95bb2d',
                               'name': 'Articulated Pixel Heart Keychain Frikarte3D.stl',
                               'bytes': 317484, 'kind': 'files', 'file_id': 3372}]
    assert 'Authorization' not in calls[0]['headers']  # anonymous


def test_makeronline_missing_model(monkeypatch):
    gone = {'code': 1, 'message': 'The model does not exist or has been deleted.', 'data': {}}
    monkeypatch.setattr(model_job, '_web_api', web_fake({'/api/mold/detail': (200, gone)}))
    with pytest.raises(RuntimeError, match='does not exist'):
        model_job.makeronline_inventory(MAKERONLINE_URL)


def test_makeronline_is_inventory_only(monkeypatch, tmp_path):
    monkeypatch.setattr(model_job, '_web_api',
                        web_fake({'/api/mold/detail': (200, MAKERONLINE_MODEL)}))
    job = model_job.makeronline_inventory(MAKERONLINE_URL)
    assert job['download_gap'] == model_job.MAKERONLINE_DOWNLOAD_GAP
    monkeypatch.setattr(model_job, '_web_api',
                        mock.Mock(side_effect=AssertionError('network attempted')))
    with pytest.raises(RuntimeError, match='cannot download files from MakerOnline'):
        model_job.makeronline_download(job, job['sources'][0], tmp_path/'src')


def test_prepared_intake_makeronline_lists_files_then_names_the_gap(env, monkeypatch):
    monkeypatch.setattr(model_job, '_web_api',
                        web_fake({'/api/mold/detail': (200, MAKERONLINE_MODEL)}))
    monkeypatch.setattr(model_job, 'download_for_site',
                        mock.Mock(side_effect=AssertionError('download attempted')))
    adapter = Adapter([])
    asyncio.run(prepared_intake.prepare_link(
        _event(MAKERONLINE_URL), adapter, state(), env.root, timeout=1))
    assert adapter.questions == []  # no connect offer: no login can fix this
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert receipt['status'] == 'FAILED'
    assert 'Articulated Pixel Heart Keychain Frikarte3D.stl' in receipt['error']
    assert 'browser security check' in receipt['error']
    assert 'Nothing was downloaded' in receipt['error']


@pytest.mark.parametrize('download', [model_job.thangs_download, model_job.nexprint_download])
def test_downloads_without_token_never_network(home, monkeypatch, tmp_path, download):
    monkeypatch.setattr(model_job, '_web_api',
                        mock.Mock(side_effect=AssertionError('network attempted')))
    with pytest.raises(SiteLoginNeeded) as caught:
        download({'site': 'x', 'model_id': '1'}, {'id': 'f', 'name': 'a.stl'}, tmp_path/'src')
    assert caught.value.reason == 'missing'


# ---------------------------------------------------------------- design

def test_design_fetch_refuses_to_pick_among_several_files(monkeypatch):
    from print_handy.core import design
    monkeypatch.setattr(model_job, '_web_api',
                        web_fake({'/api/models/v2/29424': (200, THANGS_MODEL)}))
    with pytest.raises(design.DesignError, match='cannot pick'):
        design.fetch_link_source(THANGS_URL, Path('/tmp/thangs-design'))


def test_design_fetch_keeps_the_token_wording(home, monkeypatch, tmp_path):
    from print_handy.core import design
    one = json.loads(json.dumps(NEXPRINT_MODEL))
    one['data']['modelFileList'] = one['data']['modelFileList'][:1]
    monkeypatch.setattr(model_job, '_web_api', web_fake({'model-base-info/get': (200, one)}))
    with pytest.raises(design.DesignError, match='auth_token'):
        design.fetch_link_source(NEXPRINT_URL, tmp_path/'src')


def test_design_fetch_makeronline_names_the_gap(tmp_path):
    from print_handy.core import design
    with pytest.raises(design.DesignError, match='browser security check'):
        design.fetch_link_source(MAKERONLINE_URL, tmp_path/'src')


# ---------------------------------------------------------------- connect flow

def _event(url):
    return S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
             message_id='lt1', text=url)


def _single_source_job():
    return {'site': 'nexprint', 'model_id': '2076455756517335040', 'title': 'Penguin keychain',
            'slug': 'penguin-keychain', 'license': 'CC BY',
            'sources': [{'id': 'k', 'name': 'Penguin+keychain.stl', 'kind': 'files'}]}


def _quiet_preview(monkeypatch):
    async def fake_preview(*args, **kw):
        pass
    monkeypatch.setattr(prepared_intake, 'send_preview', fake_preview)


def test_connect_flow_retries_the_download_after_saved(env, home, monkeypatch):
    # No token: the bot asks once, with the whole walkthrough in that one
    # message, and after "Saved — try again" retries the same download. The retry is stopped by a sentinel so this test covers
    # exactly the connect loop.
    monkeypatch.setattr(model_job, 'nexprint_inventory', lambda url: _single_source_job())
    _quiet_preview(monkeypatch)
    attempts = []

    def fake_download(job, chosen, dest):
        attempts.append(chosen['id'])
        if len(attempts) == 1:
            site_accounts.read_token('nexprint')  # raises: no file yet
        raise RuntimeError('sentinel: download retried')
    monkeypatch.setattr(model_job, 'download_for_site', fake_download)
    adapter = Adapter([0])
    asyncio.run(prepared_intake.prepare_link(
        _event(NEXPRINT_URL), adapter, state(), env.root, timeout=1))
    assert attempts == ['k', 'k']
    assert len(adapter.questions) == 1
    first = adapter.questions[0]
    assert first['choices'][:2] == ['Saved — try again', 'Cancel']
    assert 'no Nexprint account is connected' in first['question']
    assert 'auth_token' in first['question'] and 'Paste it here' in first['question']
    assert not any('Connect your Nexprint account' in text for text in adapter.sent)
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert 'sentinel' in receipt['error']


def test_connect_flow_refused_token_is_offered_again(env, home, monkeypatch):
    monkeypatch.setattr(model_job, 'nexprint_inventory', lambda url: _single_source_job())
    _quiet_preview(monkeypatch)

    def refused(job, chosen, dest):
        raise site_accounts.refused('nexprint')
    monkeypatch.setattr(model_job, 'download_for_site', refused)
    adapter = Adapter([0, 1])
    asyncio.run(prepared_intake.prepare_link(
        _event(NEXPRINT_URL), adapter, state(), env.root, timeout=1))
    # A refused login is asked about again every time it is refused; the
    # second prompt's Cancel ends it as CANCELLED, not FAILED (#712).
    assert len(adapter.questions) == 2
    assert all('turned down the saved login' in q['question'] for q in adapter.questions)
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert receipt['status'] == 'CANCELLED' and receipt['sliced'] is False


def test_connect_flow_cancel_is_one_terminal_post_and_cancelled(env, home, monkeypatch):
    # #712: Cancel on a connect prompt ends with exactly one terminal post,
    # receipt CANCELLED (not FAILED) and a short cause, never the walkthrough.
    monkeypatch.setattr(model_job, 'nexprint_inventory', lambda url: _single_source_job())
    _quiet_preview(monkeypatch)
    monkeypatch.setattr(model_job, 'download_for_site',
                        lambda job, chosen, dest: site_accounts.read_token('nexprint'))
    adapter = Adapter([1])
    asyncio.run(prepared_intake.prepare_link(
        _event(NEXPRINT_URL), adapter, state(), env.root, timeout=1))
    assert len(adapter.questions) == 1
    assert adapter.sent[-1] == 'Cancelled. Nothing was downloaded.'
    assert sum('Cancelled' in t or 'could not be' in t for t in adapter.sent) == 1
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert receipt['status'] == 'CANCELLED'
    assert receipt['error'] == 'connect cancelled by operator'
    chat = '\n'.join(adapter.sent + [q['question'] for q in adapter.questions])
    assert '/data/' not in chat and str(home) not in chat


@pytest.mark.parametrize('site', sorted(set(site_accounts.LOGIN_SITES) - {'cults3d'}))
def test_every_connect_prompt_cancel_is_cancelled(env, home, monkeypatch, site):
    # The same Cancel contract for every site that has a connect prompt.
    monkeypatch.setattr(model_job, 'nexprint_inventory', lambda url: _single_source_job())
    _quiet_preview(monkeypatch)

    def needs(job, chosen, dest):
        raise site_accounts.SiteLoginNeeded(site, 'missing', f'needs {site_accounts.token_path(site)}')
    monkeypatch.setattr(model_job, 'download_for_site', needs)
    adapter = Adapter([1])
    asyncio.run(prepared_intake.prepare_link(
        _event(NEXPRINT_URL), adapter, state(), env.root, timeout=1))
    assert len(adapter.questions) == 1
    assert adapter.sent[-1] == 'Cancelled. Nothing was downloaded.'
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert receipt['status'] == 'CANCELLED'
    chat = '\n'.join(adapter.sent + [q['question'] for q in adapter.questions])
    assert '/data/' not in chat and str(home) not in chat


def test_gave_up_receipt_error_is_short_and_chat_has_no_home_path(env, home, monkeypatch):
    monkeypatch.setattr(model_job, 'nexprint_inventory', lambda url: _single_source_job())
    _quiet_preview(monkeypatch)
    monkeypatch.setattr(model_job, 'download_for_site',
                        lambda job, chosen, dest: (_ for _ in ()).throw(site_accounts.refused('nexprint')))
    adapter = Adapter([0] * prepared_intake.MAX_CONNECT_ROUNDS)
    asyncio.run(prepared_intake.prepare_link(
        _event(NEXPRINT_URL), adapter, state(), env.root, timeout=1))
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert receipt['status'] == 'FAILED' and receipt['error'] == 'Nexprint login refused'
    assert str(home) not in adapter.sent[-1] and '~/.config/nexprint/token' in adapter.sent[-1]


def test_chat_safe_hides_a_container_home(monkeypatch):
    monkeypatch.setattr(site_accounts.Path, 'home', classmethod(lambda cls: site_accounts.Path('/data')))
    text = site_accounts.chat_safe('save it in /data/.config/cults3d/token please')
    assert text == 'save it in ~/.config/cults3d/token please'
    assert site_accounts.display_path('nexprint') == '~/.config/nexprint/token'
    assert '/data/' not in site_accounts.connect_steps('nexprint')


def test_connect_flow_gives_up_after_three_rounds(env, home, monkeypatch):
    monkeypatch.setattr(model_job, 'nexprint_inventory', lambda url: _single_source_job())
    _quiet_preview(monkeypatch)
    calls = []

    def refused(job, chosen, dest):
        calls.append(1)
        raise site_accounts.refused('nexprint')
    monkeypatch.setattr(model_job, 'download_for_site', refused)
    adapter = Adapter([0] * prepared_intake.MAX_CONNECT_ROUNDS)
    asyncio.run(prepared_intake.prepare_link(
        _event(NEXPRINT_URL), adapter, state(), env.root, timeout=1))
    assert len(calls) == prepared_intake.MAX_CONNECT_ROUNDS + 1
    assert adapter.sent[-1].startswith('That model could not be downloaded:')


def test_saved_reply_parser():
    label = prepared_intake.SAVED_LABEL
    assert prepared_intake._saved_reply('Saved') == label
    assert prepared_intake._saved_reply(' stored. ') == label
    # typed_reply resolves the prompt with the parser's value; that value
    # must read back as the button, or ask() would re-ask forever.
    assert prepared_intake._saved_reply(label) == label
    with pytest.raises(ValueError):
        prepared_intake._saved_reply('eyJhbGciOi.paste.attempt')


def test_connect_flow_at_the_lookup_for_a_key_site(env, home, monkeypatch):
    # Thingiverse needs its App Token for the lookup itself, before any
    # file list exists: the connect offer comes at the lookup, with the
    # Thingiverse-specific steps and docs link, then the lookup is retried.
    monkeypatch.delenv('THINGIVERSE_TOKEN', raising=False)
    lookups = []

    def lookup(url):
        lookups.append(url)
        model_job.thingiverse_headers()  # no token yet on the first call
        raise RuntimeError('sentinel: lookup retried with the token saved')
    monkeypatch.setattr(model_job, 'thingiverse_inventory', lookup)
    url = 'https://www.thingiverse.com/thing:763622'

    class SavingAdapter(Adapter):
        async def send_clarify(self, **kw):
            if kw['question'].startswith('**Thingiverse needs'):
                save_token('thingiverse', 'tv-token')  # the operator saves it, then presses Saved
            return await super().send_clarify(**kw)
    adapter = SavingAdapter([0])
    asyncio.run(prepared_intake.prepare_link(_event(url), adapter, state(), env.root, timeout=1))
    assert lookups == [url, url]
    assert adapter.questions[0]['choices'][0] == 'Saved — try again'
    steps = adapter.questions[0]['question']
    assert 'Create an App' in steps and '#thingiverse-app-token' in steps
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert 'sentinel' in receipt['error']


def test_archive_member_is_capped_as_it_expands(home, monkeypatch, tmp_path):
    # A small, highly compressed zip must not expand past the intake download
    # cap: the cap counts the compressed bytes, so the member is counted too.
    from print_handy.core import intake_resources
    monkeypatch.setattr(intake_resources, 'MAX_DOWNLOAD_BYTES', 64 * 1024)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('a.stl', b'\0' * (1024 * 1024))   # 1 MiB, compresses to ~1 KiB
    save_token('thangs')
    monkeypatch.setattr(model_job, '_web_api', web_fake({'/download-url': (200, {
        'signedUrl': 'https://storage.example/model.zip'})}))
    fake_file_download(monkeypatch, buffer.getvalue())
    job = {'site': 'thangs', 'model_id': '1', 'sources': [{'id': 'a', 'name': 'a.stl'}]}
    assert len(buffer.getvalue()) < 64 * 1024
    with pytest.raises(intake_resources.IntakeLimitExceeded, match='MiB limit'):
        model_job.thangs_download(job, job['sources'][0], tmp_path/'src')
    assert not list((tmp_path/'src').glob('*'))  # nothing partial left behind


def test_makerworld_401_is_a_named_token_refusal(home, monkeypatch):
    # A raw "HTTP Error 401: Unauthorized" names no site, so the #590 known
    # cause could not recognise it; it is now a SiteLoginNeeded the intake
    # turns into the renew-login question.
    import urllib.error
    from print_handy import diagnostics
    save_token('makerworld', 'old-token')

    def refuse(url, **kw):
        raise urllib.error.HTTPError(url, 401, 'Unauthorized', {}, None)
    monkeypatch.setattr(model_job, 'request_json', refuse)
    with pytest.raises(SiteLoginNeeded) as caught:
        model_job.maker_inventory('https://makerworld.com/en/models/931302-holder')
    assert caught.value.site == 'makerworld' and caught.value.reason == 'refused'
    assert diagnostics.known_cause(caught.value) is not None


def test_design_fetch_keeps_a_known_cause(home, monkeypatch, tmp_path):
    # The design flow used to replace every failure with "check the link is
    # public"; a curated cause (here a missing MakerWorld token) is kept.
    from print_handy.core import design
    with pytest.raises(design.DesignError) as caught:
        design.fetch_link_source('https://makerworld.com/en/models/931302-holder', tmp_path/'src')
    assert 'account token' in str(caught.value)
    assert 'check the link is public' not in str(caught.value)
