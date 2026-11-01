"""MyMiniFactory via its official API v2 (#462 Stage 2, downloads #464).

Inventory uses the operator's API key (or, without a key file, the OAuth
sign-in). Downloads use MyMiniFactory's documented OAuth sign-in: per its
API guidelines an in-app download requires the downloader to be signed in.
Verified live 2026-10-29: the API-key inventory shape below, and that with
an OAuth token GET /files/{id} returns the file's download_url (an API key
returns none), and a real download through it arrived intact after the
302 to signed storage. A Cloudflare challenge is a plain stop. All network
is mocked here."""
import asyncio
import io
import json
import urllib.error
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.core import model_job as model_job
from print_handy.core import prepared_intake
from print_handy.core.link_intake import model_link
from .test_color_intake import source, state  # noqa: F401  (state used by the e2e flow)
from .test_prepared_intake import Adapter, env  # noqa: F401  (env is the shared harness fixture)

MMF_URL = 'https://www.myminifactory.com/object/3d-print-benchy-boat-12345'
OBJ_LIST_FILES = {
    'id': 12345, 'name': 'Benchy Boat',
    'license': {'name': 'Creative Commons - Attribution'},
    'files': [
        {'id': 11, 'filename': 'benchy.stl', 'size': 54321},
        {'id': 12, 'name': 'benchy_hull.3mf', 'size': 999},
        {'id': 13, 'filename': 'notes.txt', 'size': 10},
        {'id': 14, 'filename': 'all_files.zip', 'size': 20000},
    ]}
OBJ_ITEMS_FILES = {
    'id': 777, 'name': 'Gear', 'license': 'CC BY-SA',
    'files': {'items': [{'id': 21, 'name': 'gear.stl', 'size': 42}]}}


def _key_home(tmp_path, monkeypatch, key='test-key'):
    home = tmp_path / 'home'
    cfg = home / '.config' / 'myminifactory'
    cfg.mkdir(parents=True)
    if key is not None:
        (cfg / 'token').write_text(key + '\n')
    monkeypatch.setenv('HOME', str(home))
    return home


def _api(monkeypatch, obj=OBJ_LIST_FILES):
    calls = []

    def fake_request_json(url, **kw):
        calls.append(url)
        return obj

    monkeypatch.setattr(model_job, 'request_json', fake_request_json)
    return calls


@pytest.mark.parametrize('text,hit', [
    ('https://www.myminifactory.com/object/3d-print-benchy-boat-12345', True),
    ('https://myminifactory.com/object/3d-print-benchy-boat-12345', True),
    ('https://www.myminifactory.com/object/12345', True),
    ('print https://www.myminifactory.com/object/3d-print-benchy-boat-12345 please', True),
    ('https://www.myminifactory.com/designer/someone', False),
    ('https://www.myminifactory.com/object/3d-print-benchy-boat', False),
    ('https://www.myminifactory.com/object/', False),
])
def test_model_link_myminifactory(text, hit):
    assert (model_link(text) is not None) is hit


def test_myminifactory_id():
    assert model_job.myminifactory_id(MMF_URL) == '12345'
    assert model_job.myminifactory_id('https://www.myminifactory.com/object/12345') == '12345'
    assert model_job.myminifactory_id(MMF_URL + '?download_show=1') == '12345'
    with pytest.raises(ValueError, match=r'/object/'):
        model_job.myminifactory_id('https://www.myminifactory.com/designer/someone')
    with pytest.raises(ValueError, match=r'/object/'):
        model_job.myminifactory_id('https://www.myminifactory.com/object/3d-print-benchy-boat')


def test_myminifactory_inventory_list_files_shape(tmp_path, monkeypatch):
    _key_home(tmp_path, monkeypatch)
    calls = _api(monkeypatch)
    job = model_job.myminifactory_inventory(MMF_URL)
    assert job['site'] == 'myminifactory'
    assert job['model_id'] == '12345'
    assert job['title'] == 'Benchy Boat'
    # License dict normalised to its name string.
    assert job['license'] == 'Creative Commons - Attribution'
    # Only .stl/.3mf files are sources: the .txt note and the zip are not.
    # "filename" and "name" keys are both honoured.
    assert [(s['id'], s['name'], s['bytes'], s['kind']) for s in job['sources']] == [
        (11, 'benchy.stl', 54321, 'files'), (12, 'benchy_hull.3mf', 999, 'files')]
    assert 'no_geometry_reason' not in job
    assert 'download_gap' not in job  # downloads are a real route now
    # The API key goes as the key= query parameter on the API v2 object URL.
    assert calls == ['https://www.myminifactory.com/api/v2/objects/12345?key=test-key']


def test_myminifactory_inventory_items_files_shape(tmp_path, monkeypatch):
    _key_home(tmp_path, monkeypatch)
    _api(monkeypatch, obj=OBJ_ITEMS_FILES)
    job = model_job.myminifactory_inventory(
        'https://www.myminifactory.com/object/3d-print-gear-777')
    assert job['model_id'] == '777'
    assert job['license'] == 'CC BY-SA'  # license as a plain string passes through
    assert [(s['id'], s['name'], s['bytes']) for s in job['sources']] == [(21, 'gear.stl', 42)]


def test_myminifactory_no_geometry_names_the_reason(tmp_path, monkeypatch):
    _key_home(tmp_path, monkeypatch)
    _api(monkeypatch, obj={'id': 55, 'name': 'Notes Only', 'license': None,
                           'files': [{'id': 1, 'filename': 'readme.txt', 'size': 5},
                                     {'id': 2, 'filename': 'pack.zip', 'size': 9}]})
    job = model_job.myminifactory_inventory('https://www.myminifactory.com/object/3d-print-notes-55')
    assert job['sources'] == []
    assert 'exposes no STL/3MF' in job['no_geometry_reason']


def test_myminifactory_missing_key_error(tmp_path, monkeypatch):
    _key_home(tmp_path, monkeypatch, key=None)
    with pytest.raises(RuntimeError, match=r'\.config/myminifactory/token'):
        model_job.myminifactory_inventory(MMF_URL)
    with pytest.raises(RuntimeError, match=r'\.config/myminifactory/token'):
        model_job.myminifactory_api_key()


def test_myminifactory_empty_key_error(tmp_path, monkeypatch):
    _key_home(tmp_path, monkeypatch, key='')
    with pytest.raises(RuntimeError, match='MyMiniFactory API key empty'):
        model_job.myminifactory_api_key()


def _http_error(code):
    return urllib.error.HTTPError(
        'https://www.myminifactory.com/api/v2/objects/1', code, 'blocked', {}, io.BytesIO(b''))


@pytest.mark.parametrize('code', [401, 403])
def test_myminifactory_refusal_is_plain(tmp_path, monkeypatch, code):
    _key_home(tmp_path, monkeypatch, key='SECRETKEY123')
    monkeypatch.setattr(model_job, 'request_json', mock.Mock(side_effect=_http_error(code)))
    with pytest.raises(RuntimeError, match=r'401/403') as excinfo:
        model_job.myminifactory_inventory(MMF_URL)
    # The key value never appears in any raised message.
    assert 'SECRETKEY123' not in str(excinfo.value)


def _oauth_home(tmp_path, monkeypatch, token='oauth-token'):
    home = tmp_path / 'home'
    cfg = home / '.config' / 'myminifactory'
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / 'oauth_token').write_text(token + '\n')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('MYMINIFACTORY_OAUTH_TOKEN', raising=False)
    return home


# GET /api/v2/files/{id} with an OAuth token, as recorded live 2026-10-29 (trimmed).
FILE_INFO = {'id': 373940, 'filename': 'flying-benchy.stl', 'size': 840784,
             'download_url': 'https://www.myminifactory.com/download/111400?downloadfile=flying-benchy.stl'}
CHOSEN = {'id': 373940, 'name': 'flying-benchy.stl', 'bytes': 840784, 'kind': 'files'}
JOB = {'site': 'myminifactory', 'model_id': '111400', 'title': 'winged benchy'}


def test_myminifactory_download_uses_the_oauth_sign_in(tmp_path, monkeypatch):
    _oauth_home(tmp_path, monkeypatch)
    calls, fetched = [], []
    monkeypatch.setattr(model_job, '_web_api', lambda method, url, **kw: (
        calls.append((url, kw.get('headers'))) or (200, FILE_INFO)))

    def fake_download(url, target, headers=None):
        fetched.append((url, headers))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'solid benchy\n')
    monkeypatch.setattr(model_job, 'download', fake_download)
    dest = model_job.myminifactory_download(JOB, CHOSEN, tmp_path / 'src')
    assert dest.name == 'flying-benchy.stl' and dest.read_bytes() == b'solid benchy\n'
    assert calls == [('https://www.myminifactory.com/api/v2/files/373940',
                      {'Authorization': 'Bearer oauth-token'})]
    assert fetched == [(FILE_INFO['download_url'], {'Authorization': 'Bearer oauth-token'})]


def test_myminifactory_sign_in_accepts_the_pasted_redirect_url(tmp_path, monkeypatch):
    _oauth_home(tmp_path, monkeypatch, token=(
        'https://github.com/catduckgnaf/print-handy#access_token=ABC123'
        '&expires_in=604800&state=print-handy&token_type=Bearer'))
    from print_handy.core import site_accounts
    assert site_accounts.read_token('myminifactory') == 'ABC123'


def test_myminifactory_cloudflare_challenge_is_a_plain_stop(tmp_path, monkeypatch):
    _oauth_home(tmp_path, monkeypatch)
    monkeypatch.setattr(model_job, '_web_api', lambda method, url, **kw: (200, FILE_INFO))

    def challenged(url, target, headers=None):
        raise urllib.error.HTTPError(url, 403, 'Forbidden', {'cf-mitigated': 'challenge'}, None)
    monkeypatch.setattr(model_job, 'download', challenged)
    with pytest.raises(RuntimeError, match='Cloudflare robot check') as caught:
        model_job.myminifactory_download(JOB, CHOSEN, tmp_path / 'src')
    assert 'never tries to get past' in str(caught.value)


def test_myminifactory_challenge_page_saved_as_200_is_refused(tmp_path, monkeypatch):
    _oauth_home(tmp_path, monkeypatch)
    monkeypatch.setattr(model_job, '_web_api', lambda method, url, **kw: (200, FILE_INFO))

    def page(url, target, headers=None):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'<!DOCTYPE html><html><title>Just a moment...</title>')
    monkeypatch.setattr(model_job, 'download', page)
    with pytest.raises(RuntimeError, match='sent a web page instead of the file'):
        model_job.myminifactory_download(JOB, CHOSEN, tmp_path / 'src')
    assert not list((tmp_path / 'src').glob('*'))


def test_myminifactory_no_download_link_and_refused_token(tmp_path, monkeypatch):
    from print_handy.core.site_accounts import SiteLoginNeeded
    _oauth_home(tmp_path, monkeypatch)
    monkeypatch.setattr(model_job, '_web_api', lambda method, url, **kw: (200, dict(FILE_INFO, download_url=None)))
    with pytest.raises(RuntimeError, match='did not give a download link'):
        model_job.myminifactory_download(JOB, CHOSEN, tmp_path / 'src')
    monkeypatch.setattr(model_job, '_web_api', lambda method, url, **kw: (401, {'error': 'access_denied'}))
    with pytest.raises(SiteLoginNeeded) as caught:
        model_job.myminifactory_download(JOB, CHOSEN, tmp_path / 'src')
    assert caught.value.reason == 'refused'


def test_myminifactory_download_without_sign_in_never_networks(tmp_path, monkeypatch):
    from print_handy.core.site_accounts import SiteLoginNeeded
    _key_home(tmp_path, monkeypatch)  # an API key alone cannot download
    monkeypatch.delenv('MYMINIFACTORY_OAUTH_TOKEN', raising=False)
    monkeypatch.setattr(model_job, '_web_api', mock.Mock(side_effect=AssertionError('network contacted')))
    with pytest.raises(SiteLoginNeeded) as caught:
        model_job.download_for_site(JOB, CHOSEN, tmp_path / 'src')
    assert caught.value.site == 'myminifactory' and caught.value.reason == 'missing'
    assert 'oauth_token' in str(caught.value)


def test_myminifactory_inventory_falls_back_to_the_sign_in(tmp_path, monkeypatch):
    _oauth_home(tmp_path, monkeypatch)  # no API key file
    seen = []
    monkeypatch.setattr(model_job, 'request_json',
                        lambda url, headers=None, **kw: seen.append((url, headers)) or OBJ_ITEMS_FILES)
    model_job.myminifactory_inventory('https://www.myminifactory.com/object/3d-print-gear-777')
    assert seen == [('https://www.myminifactory.com/api/v2/objects/777',
                     {'Authorization': 'Bearer oauth-token'})]


def test_inventory_for_url_routes_myminifactory(monkeypatch):
    sentinel = {'site': 'myminifactory'}
    monkeypatch.setattr(model_job, 'myminifactory_inventory', lambda url: sentinel)
    assert model_job.inventory_for_url(MMF_URL) is sentinel
    with pytest.raises(ValueError, match='MyMiniFactory'):
        model_job.inventory_for_url('https://example.com/object/3d-print-x-1')


def test_design_fetch_myminifactory_downloads_a_single_file(tmp_path, monkeypatch):
    # /design goes through the same signed-in download, never the Printables lookup.
    from print_handy.core import design
    monkeypatch.setattr(model_job, 'printables_inventory',
                        mock.Mock(side_effect=AssertionError('printables lookup attempted')))
    monkeypatch.setattr(model_job, 'myminifactory_inventory', lambda url: dict(
        JOB, sources=[CHOSEN]))
    seen = []

    def fake_download(job, chosen, dest):
        seen.append(chosen)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / chosen['name']).write_bytes(b'solid x\n')
        return dest / chosen['name']
    monkeypatch.setattr(model_job, 'myminifactory_download', fake_download)
    assert design.fetch_link_source(MMF_URL, tmp_path / 'src').name == 'flying-benchy.stl'
    assert seen == [CHOSEN]


def test_prepared_intake_myminifactory_offers_the_connect_flow(env, tmp_path, monkeypatch):
    # No sign-in saved: the listing works, the download offers "Connect
    # MyMiniFactory account" with the OAuth walkthrough, and declining stops
    # with a short cancellation cause — nothing sliced or started.
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('MYMINIFACTORY_OAUTH_TOKEN', raising=False)
    monkeypatch.setattr(model_job, 'myminifactory_inventory', lambda url: {
        'site': 'myminifactory', 'model_id': '111400', 'slug': 'winged-benchy',
        'title': 'winged benchy', 'license': 'CC BY-NC-SA', 'sources': [CHOSEN]})

    async def fake_preview(*args, **kw):
        pass
    monkeypatch.setattr(prepared_intake, 'send_preview', fake_preview)
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='mmf1', text=MMF_URL)
    adapter = Adapter([1])  # Cancel at the one connect question
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    assert adapter.questions[0]['choices'][0] == 'Saved — try again'
    steps = adapter.questions[0]['question']
    assert 'auth.myminifactory.com/web/authorize' in steps and 'Developers' in steps
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert receipt['status'] == 'CANCELLED'
    assert receipt['error'] == 'connect cancelled by operator'
    assert adapter.sent.count('Cancelled. Nothing was downloaded.') == 1
    assert adapter.sent[-1] == 'Cancelled. Nothing was downloaded.'
    assert receipt['sliced'] is False and receipt['started'] is False
    assert not env.starts


def test_download_never_carries_the_token_to_another_host():
    # MyMiniFactory redirects a signed-in download to signed storage on
    # another host: the token must not go there (and that host rejects it).
    import urllib.request
    handler = model_job._DropAuthOnHostChange()
    req = urllib.request.Request('https://www.myminifactory.com/download/1?downloadfile=a.stl',
                                 headers={'Authorization': 'Bearer t', 'User-Agent': 'x'})
    away = handler.redirect_request(req, None, 302, 'Found', {},
                                    'https://dl4.myminifactory.com/a.stl?X-Amz-Signature=1')
    assert not away.has_header('Authorization')
    same = handler.redirect_request(req, None, 302, 'Found', {},
                                    'https://www.myminifactory.com/download/2')
    assert same.get_header('Authorization') == 'Bearer t'


def test_myminifactory_refused_sign_in_at_the_lookup_offers_renew(tmp_path, monkeypatch):
    # Inventory through the OAuth sign-in (no API key file): a 401 is the
    # sign-in itself, so it must reach the renew flow, not the API-key remedy.
    from print_handy.core.site_accounts import SiteLoginNeeded
    _oauth_home(tmp_path, monkeypatch)

    def refuse(url, headers=None, **kw):
        raise urllib.error.HTTPError(url, 401, 'Unauthorized', {}, None)
    monkeypatch.setattr(model_job, 'request_json', refuse)
    with pytest.raises(SiteLoginNeeded) as caught:
        model_job.myminifactory_inventory(MMF_URL)
    assert caught.value.reason == 'refused' and caught.value.site == 'myminifactory'
