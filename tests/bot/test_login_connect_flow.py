"""The connect-account flow end to end through the real bot (#464).

A Nexprint link arrives as a real Telegram update and runs through BotCore,
the print-feedback plugin handlers and the real prepared intake. The
Nexprint API and the file host are faked (no network), and no token is
saved, so the download stops with SiteLoginNeeded. The bot asks once, with
the whole walkthrough in that one message. The test then does what an
operator does, two ways: saves the token file on disk and types `saved`, or
pastes the token itself as a plain chat message (which the bot saves and
then deletes). The same download must then be retried with the saved token. The intake is stopped right after the download by a
sentinel, so this covers exactly the connect loop on the real path.

The typed `saved` matters: typed text reaches the prompt through
link_intake.typed_reply, which resolves it with the parser's value, and
ask() must read that value back as the button. This test is what caught
the parser returning an index that ask() then re-asked forever.
"""
import asyncio
from pathlib import Path
from unittest import mock

from print_handy.core import intake_resources, model_job, site_accounts
from .test_telegram import CHAT, USER, BotApiStub, _callback, cfg, make_core, run  # noqa: F401

LINK = 'https://www.nexprint.com/en/models/2076455756517335040'
MODEL = {'code': 0, 'data': {
    'id': '2076455756517335040', 'modelName': 'Penguin keychain', 'licenseType': 0,
    'modelFileList': [{'fileId': '2076455174108495872', 'fileName': 'Penguin+keychain.stl',
                       'fileSize': 584084.0, 'fileExt': 'stl', 'isCreator': True}],
    'settingInfoList': []}}
SIGNED = 'https://nexprint.oss-eu-central-1.aliyuncs.com/models/1/penguin.stl?X-Amz-Signature=1'


class Stopped(RuntimeError):
    pass


def _message(update_id, text):
    return {'update_id': update_id, 'message': {
        'message_id': 40 + update_id, 'from': {'id': int(USER), 'is_bot': False},
        'chat': {'id': int(CHAT)}, 'text': text}}


async def _wait_prompt(core, starts):
    for _ in range(400):
        cid = next((c for c, row in core.adapter._prompts.items()
                    if row['question'].startswith(starts)), None)
        if cid is not None:
            return cid
        await asyncio.sleep(0.01)
    raise AssertionError(f'no prompt {starts!r}: {core.adapter._prompts}')


def _rig(tmp_path, monkeypatch, cfg, token):
    home = tmp_path/'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('NEXPRINT_TOKEN', raising=False)
    monkeypatch.setattr(model_job, 'ARCHIVE', tmp_path/'archive')
    api_calls = []
    fetched = []

    def fake_api(method, url, *, body=None, headers=None, timeout=25):
        api_calls.append((url, dict(headers or {})))
        if 'model-base-info/get' in url:
            return 200, MODEL
        if 'presigned-download-url' in url:
            if (headers or {}).get('Authorization') != f'Bearer {token}':
                return 200, {'code': 401, 'data': None, 'msg': '账号未登录'}
            return 200, {'code': 0, 'data': {'fileInfoList': [
                {'fileId': '2076455174108495872', 'type': 2, 'resultUrl': SIGNED}]}}
        raise AssertionError(url)

    def fake_download(url, target, headers=None):
        fetched.append(url)
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b'solid penguin\n')

    def stop_after_download(path, *a, **kw):
        raise Stopped(f'downloaded {Path(path).name}')
    monkeypatch.setattr(model_job, '_web_api', fake_api)
    monkeypatch.setattr(model_job, 'download', fake_download)
    monkeypatch.setattr(intake_resources, 'check_source', stop_after_download)
    stub = BotApiStub()
    client, core = make_core(cfg, stub)
    return client, core, stub, api_calls, fetched


def _check(stub, tmp_path, api_calls, fetched, token):
    texts = [p.get('text', '') for p in stub.payloads('sendMessage')]
    first = next(t for t in texts if t.startswith('**Nexprint needs you to be signed in'))
    assert 'auth_token' in first and 'Paste it here as your next message' in first
    assert '~/.config/nexprint/token' in first
    assert str(site_accounts.token_path('nexprint')) not in first
    assert 'may keep a copy' in first
    # One walkthrough message: no second "press Saved" prompt, no Connect button step.
    assert not [t for t in texts if 'Press Saved once' in t or 'Connect your Nexprint' in t]
    downloads = [h for url, h in api_calls if 'presigned-download-url' in url]
    # First attempt never reached the network (no token); the retry sent the saved one.
    assert [h.get('Authorization') for h in downloads] == [f'Bearer {token}']
    assert fetched == [SIGNED]
    assert any('Penguin+keychain.stl' in p.name for p in (tmp_path/'archive').rglob('*.stl'))
    assert token not in '\n'.join(texts)  # the token never appears in chat


def test_link_connect_saved_retry_through_the_real_bot(cfg, tmp_path, monkeypatch):
    client, core, stub, api_calls, fetched = _rig(tmp_path, monkeypatch, cfg, 'saved-in-file')

    async def flow():
        task = asyncio.create_task(client.handle_update(_message(1, LINK)))
        await _wait_prompt(core, '**Nexprint needs you to be signed in')
        # The operator saves the token on the Print Handy machine and says so.
        path = site_accounts.token_path('nexprint')
        path.parent.mkdir(parents=True)
        path.write_text('saved-in-file\n')
        await client.handle_update(_message(3, 'saved'))
        return await asyncio.wait_for(task, 10)

    with mock.patch('print_handy.core.printer_state.load', return_value={}):
        run(flow())
    _check(stub, tmp_path, api_calls, fetched, 'saved-in-file')
    assert not stub.payloads('deleteMessage')  # nothing secret was posted


def test_link_connect_pasted_token_is_saved_deleted_and_retried(cfg, tmp_path, monkeypatch):
    token = 'pasted-' + 'p' * 30
    client, core, stub, api_calls, fetched = _rig(tmp_path, monkeypatch, cfg, token)

    async def flow():
        task = asyncio.create_task(client.handle_update(_message(11, LINK)))
        await _wait_prompt(core, '**Nexprint needs you to be signed in')
        await client.handle_update(_message(13, 'what is this?'))  # ordinary chat is not a token
        await client.handle_update(_message(14, token))
        return await asyncio.wait_for(task, 10)

    with mock.patch('print_handy.core.printer_state.load', return_value={}):
        run(flow())
    _check(stub, tmp_path, api_calls, fetched, token)
    assert site_accounts.token_path('nexprint').read_text() == token + '\n'
    texts = [p.get('text', '') for p in stub.payloads('sendMessage')]
    assert any("doesn't look like a Nexprint token" in t for t in texts)
    # The operator's message (id 40 + update id) was deleted, and they were told.
    assert [p.get('message_id') for p in stub.payloads('deleteMessage')] == [54]
    assert any(t.startswith('Token saved. I deleted your message.') for t in texts)


def test_link_connect_says_so_when_it_cannot_delete_the_message(cfg, tmp_path, monkeypatch):
    token = 'pasted-' + 'p' * 30
    client, core, stub, api_calls, fetched = _rig(tmp_path, monkeypatch, cfg, token)
    real = stub.__call__

    def refusing(req, timeout=None):
        if req.full_url.endswith('/deleteMessage'):
            raise OSError('cannot delete')
        return real(req, timeout)
    client.opener = refusing
    core.adapter.opener = refusing

    async def flow():
        task = asyncio.create_task(client.handle_update(_message(21, LINK)))
        await _wait_prompt(core, '**Nexprint needs you to be signed in')
        await client.handle_update(_message(24, token))
        return await asyncio.wait_for(task, 10)

    with mock.patch('print_handy.core.printer_state.load', return_value={}):
        run(flow())
    texts = [p.get('text', '') for p in stub.payloads('sendMessage')]
    assert any("couldn't delete your message" in t for t in texts)
    assert fetched == [SIGNED]
