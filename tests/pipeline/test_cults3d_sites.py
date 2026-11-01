"""Cults3D provider (#462 Stage 3): metadata-only inventory + named gap.

Verified live 2026-10-29 with an operator key: `creation(slug:)` answers
{id, name, url, license{name}} for a real slug (id is UNPADDED base64 of
"Creation/<slug>") and creation: null for an unknown one. The fixtures
below use that live shape. The first build's guessed node(id:) lookup with
a padded id never matched; this file pins the working query. The
credential is a token file holding `nickname:key`.
"""
import asyncio
import base64
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.core import link_intake, model_job, prepared_intake
from .test_color_intake import state  # noqa: F401  (used by the e2e flow)
from .test_prepared_intake import Adapter, env  # noqa: F401  (env is the shared harness fixture)

CULTS_URL = 'https://cults3d.com/en/3d-model/home/benchy-boat'
SLUG = 'benchy-boat'
# Cults' own ids are unpadded base64 (as recorded live).
GLOBAL_ID = base64.b64encode(f'Creation/{SLUG}'.encode()).decode().rstrip('=')
NICKNAME, KEY = 'CatDuck', 's3cret-key'
EXPECTED_AUTH = 'Basic ' + base64.b64encode(f'{NICKNAME}:{KEY}'.encode()).decode()


def write_token(home: Path, content: str) -> Path:
    folder = home / '.config' / 'cults3d'
    folder.mkdir(parents=True)
    path = folder / 'token'
    path.write_text(content, encoding='utf-8')
    return path


@pytest.fixture
def key_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    write_token(home, f'{NICKNAME}:{KEY}\n')
    monkeypatch.setenv('HOME', str(home))
    return home


def graphql_ok(url, *, data=None, headers=None, timeout=25):
    return {'data': {'creation': {'id': GLOBAL_ID, 'name': 'Benchy Boat', 'url': CULTS_URL,
                                  'license': {'name': 'CC BY - Attribution'}}}}


@pytest.mark.parametrize('text', [
    CULTS_URL,
    'https://cults3d.com/3d-model/home/benchy-boat',
    'https://www.cults3d.com/en/3d-model/benchy-boat',
    'check this https://cults3d.com/fr/3d-model/art/cool-thing please',
])
def test_model_link_accepts_cults3d(text):
    assert link_intake.model_link(text)


@pytest.mark.parametrize('text', [
    'https://cults3d.com/en/3d-model',
    'https://cults3d.com/en/collections/staff-picks',
    'https://cults3d.com/en/users/CatDuck/creations',
    'https://example.com/en/3d-model/home/benchy-boat',
    'no link at all',
])
def test_model_link_rejects_non_model_cults3d(text):
    assert link_intake.model_link(text) is None


@pytest.mark.parametrize('url, slug', [
    (CULTS_URL, SLUG),
    ('https://cults3d.com/3d-model/benchy-boat', 'benchy-boat'),
    ('https://cults3d.com/en/3d-model/gadget/phone-stand-v2/', 'phone-stand-v2'),
])
def test_cults_id_extracts_slug(url, slug):
    assert model_job.cults_id(url) == slug


def test_cults_id_rejects_non_model_url():
    with pytest.raises(ValueError, match='Cults3D URL needs'):
        model_job.cults_id('https://cults3d.com/en/users/CatDuck')


def test_cults_credentials_builds_basic_header(key_home):
    assert model_job.cults_credentials() == EXPECTED_AUTH


def test_cults_credentials_missing_file_names_the_fix(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    with pytest.raises(RuntimeError) as caught:
        model_job.cults_credentials()
    message = str(caught.value)
    assert '.config/cults3d/token' in message.replace('\\', '/')
    assert 'https://cults3d.com/en/api/keys' in message
    assert 'nickname:key' in message


@pytest.mark.parametrize('content', ['', 'no-colon-here', ':only-a-key', 'nickname-only:'])
def test_cults_credentials_malformed_file_refuses(tmp_path, monkeypatch, content):
    home = tmp_path / 'home'
    home.mkdir()
    write_token(home, content)
    monkeypatch.setenv('HOME', str(home))
    with pytest.raises(RuntimeError, match='must contain exactly nickname:key'):
        model_job.cults_credentials()


def test_cults_inventory_happy_path(key_home, monkeypatch):
    seen = {}

    def fake(url, *, data=None, headers=None, timeout=25):
        seen.update(url=url, data=data, headers=headers)
        return graphql_ok(url, data=data, headers=headers)

    monkeypatch.setattr(model_job, 'request_json', fake)
    job = model_job.cults_inventory(CULTS_URL)
    assert seen['url'] == 'https://cults3d.com/graphql'
    assert seen['headers']['Authorization'] == EXPECTED_AUTH
    assert seen['data']['variables'] == {'slug': SLUG}
    assert 'creation(slug: $slug)' in seen['data']['query']
    assert job['site'] == 'cults3d'
    assert job['model_id'] == SLUG
    assert job['title'] == 'Benchy Boat'
    assert job['license'] == 'CC BY - Attribution'
    assert job['sources'] == []
    assert 'cannot download files from Cults3D' in job['download_gap']
    assert 'Benchy Boat' in job['no_geometry_reason']
    assert 'does not expose' in job['no_geometry_reason']


def test_cults_inventory_without_token_never_networks(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setattr(model_job, 'request_json',
                        mock.Mock(side_effect=AssertionError('network attempted')))
    with pytest.raises(RuntimeError, match='needs an API key first'):
        model_job.cults_inventory(CULTS_URL)


def test_cults_inventory_rejects_a_different_creation(key_home, monkeypatch):
    # The API is known to answer an unfiltered result for an unknown id; a node
    # whose decoded id is not the requested creation must not be followed.
    other = base64.b64encode(b'Creation/someone-elses-model').decode()
    monkeypatch.setattr(model_job, 'request_json',
                        lambda *a, **k: {'data': {'creation': {'id': other, 'name': 'Nope'}}})
    with pytest.raises(RuntimeError, match='different creation'):
        model_job.cults_inventory(CULTS_URL)


def test_cults_inventory_not_found(key_home, monkeypatch):
    monkeypatch.setattr(model_job, 'request_json',
                        lambda *a, **k: {'data': {'creation': None}})  # the live answer
    with pytest.raises(RuntimeError, match='creation not found'):
        model_job.cults_inventory(CULTS_URL)


def test_cults_inventory_401_is_plain_and_leaks_nothing(key_home, monkeypatch):
    def refuse(*a, **k):
        raise urllib.error.HTTPError(CULTS_URL, 401, 'Unauthorized', None, None)

    monkeypatch.setattr(model_job, 'request_json', refuse)
    with pytest.raises(RuntimeError) as caught:
        model_job.cults_inventory(CULTS_URL)
    message = str(caught.value)
    assert '401' in message and 'nickname' in message
    assert KEY not in message and NICKNAME not in message


def test_cults_inventory_403_is_plain(key_home, monkeypatch):
    def refuse(*a, **k):
        raise urllib.error.HTTPError(CULTS_URL, 403, 'Forbidden', None, None)

    monkeypatch.setattr(model_job, 'request_json', refuse)
    with pytest.raises(RuntimeError, match='never tries to solve or bypass'):
        model_job.cults_inventory(CULTS_URL)


def test_cults_download_refuses_without_network(tmp_path):
    job = {'site': 'cults3d', 'model_id': SLUG}
    with mock.patch.object(model_job, 'request_json',
                           side_effect=AssertionError('network attempted')), \
         mock.patch.object(model_job, 'download',
                           side_effect=AssertionError('download attempted')):
        with pytest.raises(RuntimeError, match='cannot download files from Cults3D'):
            model_job.cults_download(job, {'name': 'x.stl'}, tmp_path / 'x.stl')


def test_registry_routes_cults3d(key_home, monkeypatch):
    monkeypatch.setattr(model_job, 'request_json', graphql_ok)
    job = model_job.inventory_for_url(CULTS_URL)
    assert job['site'] == 'cults3d'
    with pytest.raises(RuntimeError, match='cannot download files from Cults3D'):
        model_job.download_for_site(job, {'name': 'x.stl'}, Path('/tmp/x.stl'))
    with pytest.raises(ValueError) as caught:
        model_job.inventory_for_url('https://unknown.example/model/1')
    assert 'Cults3D' in str(caught.value)


def test_design_fetch_cults3d_link_names_the_gap(tmp_path, monkeypatch):
    from print_handy.core import design
    monkeypatch.setattr(model_job, 'printables_inventory',
                        mock.Mock(side_effect=AssertionError('printables lookup attempted')))
    with pytest.raises(design.DesignError, match='cannot download files from Cults3D'):
        design.fetch_link_source(CULTS_URL, tmp_path / 'src')


def test_prepared_intake_cults3d_refuses_with_title_and_gap(env, monkeypatch):
    job = {'site': 'cults3d', 'model_id': SLUG, 'title': 'Benchy Boat',
           'slug': SLUG, 'license': None, 'sources': [],
           'download_gap': model_job.CULTS_DOWNLOAD_GAP,
           'no_geometry_reason': (
               'Cults3D listing "Benchy Boat" was found through the Cults API, but '
               'the API does not expose a creation\'s files.')}
    monkeypatch.setattr(model_job, 'cults_inventory', lambda url: job)
    monkeypatch.setattr(model_job, 'cults_download',
                        mock.Mock(side_effect=AssertionError('download attempted')))
    previews = []

    async def fake_preview(*args, **kw):
        previews.append(args)

    monkeypatch.setattr(prepared_intake, 'send_preview', fake_preview)
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='cults1', text=CULTS_URL)
    adapter = Adapter([])
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert receipt['status'] == 'FAILED'
    assert receipt['error'] == 'Cults3D does not allow app downloads'
    assert adapter.sent[-1] == model_job.CULTS_REFUSAL
    # Nothing reached a choice, a preview, a download or a start.
    assert adapter.questions == [] and previews == []


def test_cults_link_without_a_key_is_refused_before_any_connect_prompt(env, tmp_path, monkeypatch):
    # #712: no token saved. The listing lookup raises SiteLoginNeeded, but a
    # Cults key can never unlock a download, so there is no connect prompt,
    # no API-key walkthrough and no path: one plain refusal.
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('CULTS3D_TOKEN', raising=False)
    monkeypatch.setattr(model_job, 'request_json',
                        mock.Mock(side_effect=AssertionError('network attempted')))
    monkeypatch.setattr(model_job, 'cults_download',
                        mock.Mock(side_effect=AssertionError('download attempted')))
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='cults2', text=CULTS_URL)
    adapter = Adapter([])
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    assert adapter.questions == []
    assert adapter.sent[-1] == model_job.CULTS_REFUSAL
    chat = '\n'.join(adapter.sent)
    assert '/data/' not in chat and '.config' not in chat and 'API key' not in chat
    assert 'Saved' not in chat
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert receipt['status'] == 'FAILED'
    assert receipt['error'] == 'Cults3D does not allow app downloads'
