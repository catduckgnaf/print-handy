"""Pasting a site login token into chat (the connect flow's typed-reply path).

Covers, per site in LOGIN_SITES: what a pasted value is normalized to and
which shapes are refused; the atomic, owner-only write; the intake flow (the
first message carries the whole walkthrough, a pasted token is saved and the
download retried, junk re-asks, cancel, `saved` unchanged); and that the
token value never reaches a log line, a receipt, an error text or a reply.
"""
import asyncio
import json
import logging
import os
import stat
import sys
import time
from types import SimpleNamespace as S

import pytest

from print_handy.core import link_intake, model_job, prepared_intake, site_accounts
from print_handy.core.site_accounts import LOGIN_SITES, accept_pasted, save_token, token_path
from .test_color_intake import state  # noqa: F401
from .test_prepared_intake import Adapter, env  # noqa: F401
from .test_login_token_sites import (NEXPRINT_URL, _event, _quiet_preview,  # noqa: F401
                                     _single_source_job, home)

GOOD = {
    'makerworld': 'eyJhbGciOiJIUzI1NiJ9.' + 'a' * 40 + '.sig',
    'thingiverse': 'a1b2c3d4e5f60718293a4b5c6d7e8f90',
    'cults3d': 'Penguin:abcdef0123456789abcdef',
    'crealitycloud': '1234567890:4f2a9c8b7d6e5f40312a',
    'thangs': 'eyJhbGciOiJIUzI1NiJ9.' + 'b' * 30 + '.sig',
    'nexprint': 'tok-' + 'c' * 30,
    'myminifactory': 'mmf-' + 'd' * 30,
}
SECRET = GOOD['crealitycloud']


def test_every_login_site_has_a_sample():
    assert set(GOOD) == set(LOGIN_SITES)


@pytest.mark.parametrize('site', sorted(GOOD))
def test_a_well_formed_value_is_accepted_for_every_site(site):
    token, problem = accept_pasted(site, GOOD[site])
    assert (token, problem) == (GOOD[site], '')


@pytest.mark.parametrize('site', sorted(GOOD))
@pytest.mark.parametrize('wrapped', ['"{}"', "'{}'", 'Bearer {}', '  {}\n', 'bearer "{}"'])
def test_quotes_whitespace_and_bearer_are_stripped(site, wrapped):
    token, problem = accept_pasted(site, wrapped.format(GOOD[site]))
    assert (token, problem) == (GOOD[site], '')


@pytest.mark.parametrize('site', sorted(GOOD))
@pytest.mark.parametrize('junk', ['hello', 'can you print this please', 'ok', '', '   ',
                                  'two words here that are long enough to pass a length check',
                                  'x' * 5000])
def test_ordinary_chat_is_not_a_token(site, junk):
    token, problem = accept_pasted(site, junk)
    assert token == '' and problem
    assert len(junk.strip()) < 20 or junk.strip() not in problem  # the reason never repeats the input
    assert 'Nothing was saved' in problem


def test_crealitycloud_accepts_both_cookies_in_either_order_and_forms():
    want = '1234567890:4f2a9c8b7d6e5f40312a'
    for pasted in ('1234567890:4f2a9c8b7d6e5f40312a',
                   'model_user_id=1234567890; model_token=4f2a9c8b7d6e5f40312a',
                   'model_token=4f2a9c8b7d6e5f40312a; model_user_id=1234567890',
                   'Cookie: model_token=4f2a9c8b7d6e5f40312a;model_user_id=1234567890;',
                   'model_user_id="1234567890"; model_token="4f2a9c8b7d6e5f40312a"',
                   'model_user_id=1234567890; other=1; model_token=4f2a9c8b7d6e5f40312a'):
        assert accept_pasted('crealitycloud', pasted) == (want, ''), pasted


def test_crealitycloud_one_cookie_alone_is_refused():
    for pasted in ('model_token=4f2a9c8b7d6e5f40312a', '4f2a9c8b7d6e5f40312a',
                   'model_user_id=1234567890', 'abc:4f2a9c8b7d6e5f40312a'):
        token, problem = accept_pasted('crealitycloud', pasted)
        assert token == '' and 'model_user_id:model_token' in problem, pasted


def test_single_cookie_sites_accept_a_cookie_string():
    assert accept_pasted('thangs', 'accessToken=' + GOOD['thangs'])[0] == GOOD['thangs']
    assert accept_pasted('nexprint', 'Cookie: auth_token=' + 'y' * 30 + '; theme=dark')[0] == 'y' * 30
    assert accept_pasted('makerworld', 'token=' + GOOD['makerworld'])[0] == GOOD['makerworld']


def test_nexprint_account_info_json_and_mmf_redirect_url():
    info = json.dumps({'access_token': 'n' * 30, 'refresh_token': 'r', 'expires_time': 1})
    assert accept_pasted('nexprint', info) == ('n' * 30, '')
    url = 'https://github.com/catduckgnaf/print-handy#access_token=' + 'm' * 30 + '&token_type=bearer'
    assert accept_pasted('myminifactory', url) == ('m' * 30, '')


@pytest.mark.parametrize('site', sorted(GOOD))
@pytest.mark.parametrize('text', [
    'https://thangs.com/designer/foo/3d-model/bar-12345',
    'www.makerworld.com.example.org',
    '/home/user/models/benchy-final-v2.stl',
    'C:\\Users\\me\\models\\benchy-final-v2.stl',
    'please print the thing from https://makerworld.com/en/models/1234',
])
def test_a_link_or_path_is_never_taken_for_a_token(site, text):
    token, problem = accept_pasted(site, text)
    assert token == '' and 'Nothing was saved' in problem


@pytest.mark.parametrize('site', sorted(GOOD))
def test_the_connect_reply_refuses_a_link_and_writes_nothing(home, site):
    parse = prepared_intake._connect_reply(site)
    for text in ('https://thangs.com/designer/foo/3d-model/bar-12345',
                 'can you print the model I posted earlier please', '/tmp/some/long/path/model.stl'):
        with pytest.raises(ValueError):
            parse(text)
    assert not token_path(site).exists()
    assert link_intake.take_secret_reply() is False


def test_thangs_wants_a_jwt():
    assert accept_pasted('thangs', 'tok-' + 'c' * 30)[0] == ''
    assert accept_pasted('thangs', 'api.thangs.com.example.org.au')[0] == ''


def test_cults_and_thingiverse_shapes():
    assert accept_pasted('cults3d', 'nokey')[0] == ''
    assert accept_pasted('cults3d', 'nick:short')[0] == ''
    assert accept_pasted('cults3d', ':abcdef0123456789abcdef')[0] == ''
    assert accept_pasted('thingiverse', 'tooshort')[0] == ''
    assert accept_pasted('thingiverse', 'has spaces in it but is long enough 12345')[0] == ''


def _jwt(exp):
    import base64
    body = base64.urlsafe_b64encode(json.dumps({'exp': exp}).encode()).rstrip(b'=').decode()
    return f'eyJhbGciOiJIUzI1NiJ9.{body}.signature'


def test_an_expired_jwt_is_refused_and_a_live_one_accepted():
    token, problem = accept_pasted('thangs', _jwt(time.time() - 60))
    assert token == '' and 'already expired' in problem
    live = _jwt(time.time() + 3600)
    assert accept_pasted('thangs', live) == (live, '')


@pytest.mark.parametrize('site', sorted(GOOD))
def test_save_token_writes_then_reads_back(home, site):
    path = save_token(site, GOOD[site])
    assert path == token_path(site)
    assert path.read_text() == GOOD[site] + '\n'
    assert site_accounts.stored(site)[0] == GOOD[site]
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temp file left


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX permission bits')
def test_save_token_is_owner_only(home):
    path = save_token('crealitycloud', SECRET)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX permission bits')
def test_save_token_tightens_a_loose_existing_file(home):
    path = token_path('thangs')
    path.parent.mkdir(parents=True)
    path.write_text('old')
    os.chmod(path, 0o644)
    os.chmod(path.parent, 0o755)
    save_token('thangs', GOOD['thangs'])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_save_token_replaces_atomically(home, monkeypatch):
    save_token('thangs', 'old-' + 'o' * 30)
    seen = []
    real = os.replace

    def spy(src, dst):
        seen.append((os.path.basename(src), open(src).read()))
        real(src, dst)
    monkeypatch.setattr(os, 'replace', spy)
    save_token('thangs', GOOD['thangs'])
    assert seen and seen[0][1] == GOOD['thangs'] + '\n' and seen[0][0] != 'token'
    assert token_path('thangs').read_text() == GOOD['thangs'] + '\n'


def test_a_cookie_string_in_the_file_reads_back_normalized(home):
    path = token_path('crealitycloud')
    path.parent.mkdir(parents=True)
    path.write_text('model_token=4f2a9c8b7d6e5f40312a; model_user_id=1234567890\n')
    assert model_job.crealitycloud_token() == '1234567890:4f2a9c8b7d6e5f40312a'


# ------------------------------------------------------------------ the intake flow

class TypingAdapter(Adapter):
    """Answers each question from a script: an int presses that button, a string is typed.

    Typed text goes through link_intake.typed_reply, exactly like a chat
    message; a refusal (the prompt stays open) moves on to the next item.
    """

    def __init__(self, script, source):
        super().__init__([])
        self.script = iter(script)
        self.source = source
        self.errors = []
        self.deleted_flags = []

    async def send_clarify(self, **kw):
        self.questions.append(kw)
        import tools.clarify_gateway as cm
        while True:
            item = next(self.script)
            if isinstance(item, int):
                cm.resolve_gateway_clarify(kw['clarify_id'], kw['choices'][item])
                return S(success=True)
            error = link_intake.typed_reply(self.source, item)
            self.errors.append(error)
            if error == '':
                self.deleted_flags.append(link_intake.take_secret_reply())
                return S(success=True)


def _run(env, adapter):
    asyncio.run(prepared_intake.prepare_link(
        _event(NEXPRINT_URL), adapter, state(), env.root, timeout=1))


def _nexprint_download(monkeypatch, attempts, tokens_seen):
    def fake_download(job, chosen, dest):
        attempts.append(chosen['id'])
        tokens_seen.append(site_accounts.stored('nexprint')[0])
        site_accounts.read_token('nexprint')  # raises until a token is in place
        raise RuntimeError('sentinel: download retried')
    monkeypatch.setattr(model_job, 'download_for_site', fake_download)


def _setup(env, monkeypatch):
    monkeypatch.delenv('NEXPRINT_TOKEN', raising=False)
    monkeypatch.setattr(model_job, 'nexprint_inventory', lambda url: _single_source_job())
    _quiet_preview(monkeypatch)
    return _event(NEXPRINT_URL).source


def test_first_message_has_the_lead_the_steps_the_risk_note_and_the_docs_link(env, home, monkeypatch):
    source = _setup(env, monkeypatch)
    _nexprint_download(monkeypatch, [], [])
    adapter = TypingAdapter([1], source)
    _run(env, adapter)
    question = adapter.questions[0]['question']
    assert question.startswith('**Nexprint needs you to be signed in for this model, and no '
                               'Nexprint account is connected yet.** Nothing was downloaded.')
    assert '1. Sign in at https://www.nexprint.com' in question and '3. Copy the value' in question
    assert 'Paste it here as your next message' in question
    assert '~/.config/nexprint/token' in question and 'Saved' in question
    assert str(token_path('nexprint')) not in question and '/data/' not in question
    assert site_accounts.RISK_NOTE in question
    assert LOGIN_SITES['nexprint'].docs_url in question
    assert adapter.questions[0]['choices'] == ['Saved — try again', 'Cancel']
    assert adapter.sent[-1] == 'Cancelled. Nothing was downloaded.'  # Cancel ends it (#712)


@pytest.mark.parametrize('reason,text', [('empty', 'empty or incomplete'), ('expired', 'has expired'),
                                         ('refused', 'turned down the saved login')])
def test_lead_wording_follows_the_reason(env, home, monkeypatch, reason, text):
    source = _setup(env, monkeypatch)

    def raiser(job, chosen, dest):
        raise SiteLoginNeededFor(reason)
    monkeypatch.setattr(model_job, 'download_for_site', raiser)
    adapter = TypingAdapter([1], source)
    _run(env, adapter)
    assert text in adapter.questions[0]['question']


def SiteLoginNeededFor(reason):
    return site_accounts.SiteLoginNeeded('nexprint', reason, f'nexprint {reason}')


def test_a_pasted_token_is_saved_and_the_download_retried(env, home, monkeypatch):
    source = _setup(env, monkeypatch)
    attempts, seen = [], []
    _nexprint_download(monkeypatch, attempts, seen)
    token = 'n' * 12 + '-pasted-' + 'x' * 12
    adapter = TypingAdapter([token], source)
    _run(env, adapter)
    assert attempts == ['k', 'k'] and seen == [None, token]
    assert token_path('nexprint').read_text() == token + '\n'
    assert adapter.errors == [''] and adapter.deleted_flags == [True]  # the handler deletes that message
    assert len(adapter.questions) == 1


def test_a_pasted_cookie_string_is_saved_normalized(env, home, monkeypatch):
    source = _setup(env, monkeypatch)
    _nexprint_download(monkeypatch, [], [])
    adapter = TypingAdapter(['Cookie: auth_token=' + 'z' * 30 + '; theme=dark'], source)
    _run(env, adapter)
    assert token_path('nexprint').read_text() == 'z' * 30 + '\n'


def test_junk_reasks_without_saving_then_a_token_works(env, home, monkeypatch):
    source = _setup(env, monkeypatch)
    attempts, seen = [], []
    _nexprint_download(monkeypatch, attempts, seen)
    token = 'good-' + 't' * 30
    adapter = TypingAdapter(['hello there', 'what is this?', token], source)
    _run(env, adapter)
    assert len(adapter.errors) == 3 and all("doesn't look like a Nexprint token" in e for e in adapter.errors[:2])
    assert all('Nothing was saved' in e for e in adapter.errors[:2])
    assert adapter.deleted_flags == [True]  # only the accepted paste is deleted
    assert attempts == ['k', 'k'] and token_path('nexprint').read_text() == token + '\n'


def test_junk_alone_saves_nothing(env, home, monkeypatch):
    source = _setup(env, monkeypatch)
    _nexprint_download(monkeypatch, [], [])
    adapter = TypingAdapter(['hello there', 1], source)
    _run(env, adapter)
    assert not token_path('nexprint').exists()


def test_typed_cancel_stops_cancelled(env, home, monkeypatch):
    source = _setup(env, monkeypatch)
    _nexprint_download(monkeypatch, [], [])
    adapter = TypingAdapter(['cancel'], source)
    _run(env, adapter)
    assert not token_path('nexprint').exists()
    assert adapter.sent[-1] == 'Cancelled. Nothing was downloaded.'
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert receipt['status'] == 'CANCELLED'


def test_typed_saved_still_works_when_the_file_is_in_place(env, home, monkeypatch):
    source = _setup(env, monkeypatch)
    attempts, seen = [], []
    _nexprint_download(monkeypatch, attempts, seen)

    class Saver(TypingAdapter):
        async def send_clarify(self, **kw):
            save_token('nexprint', 'by-hand-' + 'h' * 30)  # the operator saves it on the machine
            return await super().send_clarify(**kw)
    adapter = Saver(['saved'], source)
    _run(env, adapter)
    assert attempts == ['k', 'k'] and adapter.deleted_flags == [False]  # nothing to delete


def test_a_pasted_token_the_site_refuses_goes_round_again(env, home, monkeypatch):
    source = _setup(env, monkeypatch)
    calls = []

    def refused(job, chosen, dest):
        calls.append(1)
        site_accounts.read_token('nexprint')
        raise site_accounts.refused('nexprint')
    monkeypatch.setattr(model_job, 'download_for_site', refused)
    adapter = TypingAdapter(['first-' + 'f' * 30, 'second-' + 's' * 30, 1], source)
    _run(env, adapter)
    assert len(adapter.questions) == 3
    assert 'turned down the saved login' in adapter.questions[1]['question']
    assert len(calls) == 3 and token_path('nexprint').read_text() == 'second-' + 's' * 30 + '\n'


def test_an_unwritable_folder_says_so_and_keeps_asking(env, home, monkeypatch):
    source = _setup(env, monkeypatch)
    _nexprint_download(monkeypatch, [], [])

    def boom(site, token):
        raise PermissionError(13, 'Permission denied')
    monkeypatch.setattr(site_accounts, 'save_token', boom)
    secret = 'secret-' + 'k' * 30
    adapter = TypingAdapter([secret, 1], source)
    _run(env, adapter)
    assert "couldn't save it to" in adapter.errors[0] and secret not in adapter.errors[0]
    assert adapter.deleted_flags == []


@pytest.mark.parametrize('site', sorted(GOOD))
def test_the_connect_reply_parser_saves_for_every_site(home, site):
    parse = prepared_intake._connect_reply(site)
    assert parse(GOOD[site]) == prepared_intake.SAVED_LABEL
    assert token_path(site).read_text() == GOOD[site] + '\n'
    assert link_intake.take_secret_reply() is True
    assert link_intake.take_secret_reply() is False  # once
    with pytest.raises(ValueError):
        parse('definitely not a token')
    assert parse('Saved') == prepared_intake.SAVED_LABEL
    assert link_intake.take_secret_reply() is False


# ------------------------------------------------------------------ the secret never leaks

def test_the_token_is_not_in_logs_receipts_replies_or_errors(env, home, monkeypatch, caplog):
    source = _setup(env, monkeypatch)
    secret = 'LEAKCHECK-' + 'q' * 30
    attempts = []

    def fake_download(job, chosen, dest):
        attempts.append(1)
        site_accounts.read_token('nexprint')
        raise site_accounts.refused('nexprint')  # then fails for good
    monkeypatch.setattr(model_job, 'download_for_site', fake_download)
    caplog.set_level(logging.DEBUG)
    adapter = TypingAdapter(['not a token at all', secret, 1], source)
    _run(env, adapter)
    assert attempts, 'the paste should have triggered a retry'
    haystack = '\n'.join([
        caplog.text,
        *(r.getMessage() for r in caplog.records),
        *(str(r.exc_info[1]) for r in caplog.records if r.exc_info),
        *(q['question'] for q in adapter.questions),
        *adapter.sent,
        *(e for e in adapter.errors if e),
        *(p.read_text() for p in env.root.rglob('*') if p.is_file()),
    ])
    assert secret not in haystack
    assert token_path('nexprint').read_text() == secret + '\n'  # it is saved, just nowhere else
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert secret not in json.dumps(receipt)
