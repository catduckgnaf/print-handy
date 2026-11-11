"""#1204 item 2 at the dispatch hook: the collection hook sits directly
before model_link_dispatch, never claims an ordinary upload, and on Done
adding hands the collected files to model_link_dispatch itself.

The prompts go through the real shared choice mechanism (link_intake.ask
and the core prompt registry) that every BotCore transport answers:
Discord buttons, Telegram and web chat choices, Matrix numbered replies.
"""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy.core import prompts, upload_collection

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "upload_collection_plugin_1204", ROOT / "plugins/print-feedback/__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class Adapter:
    def __init__(self, answers):
        self.answers = list(answers)
        self.questions, self.sent = [], []

    async def send_clarify(self, **kw):
        self.questions.append(kw)
        prompts.resolve_gateway_clarify(kw['clarify_id'], self.answers.pop(0))
        return S(success=True)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return S(success=True)


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / 'state' / upload_collection.STATE_NAME
    monkeypatch.setattr(upload_collection, '_state_path', lambda: path)
    return path


def _gateway(adapter):
    return S(_is_user_authorized_for_source=lambda s: True, _delivery_adapter_for=lambda s: adapter,
             _session_key_for_source=lambda s: f'session:{s.chat_id}:{s.user_id}',
             printer_id_for=lambda s: 'k2', shared_printer_ids_for=lambda s: None)


def _event(platform, text, files, message_id):
    source = S(platform=S(value=platform), profile='3dprinting', chat_id='c1', thread_id=None,
               user_id='u1', is_bot=False, profile_route_rejected=False)
    return S(source=source, text=text, media_urls=[str(f) for f in files], message_id=message_id,
             internal=False, metadata={})


def test_hook_sits_directly_before_model_link_dispatch():
    hooks = list(plugin._DISPATCH_HOOKS)
    assert hooks.index(plugin.upload_collection_dispatch) + 1 == hooks.index(plugin.model_link_dispatch)


def test_an_ordinary_upload_passes_through_untouched(store, tmp_path):
    lid = tmp_path / ('doc_' + 'a' * 12 + '_lid.stl')
    lid.write_bytes(b'lid')
    adapter = Adapter([])
    for text in ('', 'print this in red', 'https://www.printables.com/model/1-x add to project'):
        result = asyncio.run(plugin.upload_collection_dispatch(_event('discord', text, [lid], 'm1'),
                                                               _gateway(adapter)))
        assert result is None
    assert adapter.questions == [] and adapter.sent == [] and not store.exists()


@pytest.mark.parametrize('platform', ['discord', 'telegram', 'matrix', 'webui'])
def test_add_then_done_hands_the_collection_to_model_link_dispatch(store, tmp_path, monkeypatch, platform):
    left = tmp_path / ('doc_' + 'a' * 12 + '_arm.stl')
    right = tmp_path / ('doc_' + 'b' * 12 + '_arm.stl')
    left.write_bytes(b'left')
    right.write_bytes(b'right')
    handed = []

    async def fake_model_link(event, gateway, **kw):
        handed.append(event)
        return {'action': 'skip', 'reason': 'intake ran'}
    monkeypatch.setattr(plugin, 'model_link_dispatch', fake_model_link)

    # Message 1 starts the project; its status question is answered later (Cancel here would discard).
    first = Adapter(['__expire__'])

    async def expire_clarify(**kw):
        first.questions.append(kw)
        prompts.resolve_gateway_clarify(kw['clarify_id'], None)
        return S(success=True)
    first.send_clarify = expire_clarify
    asyncio.run(plugin.upload_collection_dispatch(_event(platform, 'add to project', [left], 'm1'),
                                                  _gateway(first)))
    assert first.questions[0]['choices'] == ['Done adding', 'Cancel project']
    # Message 2: same user, chat and printer. Add to project, then Done adding.
    second = Adapter(['Add to project', 'Done adding'])
    result = asyncio.run(plugin.upload_collection_dispatch(_event(platform, '', [right], 'm2'),
                                                           _gateway(second)))
    assert result == {'action': 'skip', 'reason': 'intake ran'}
    assert second.questions[0]['choices'] == ['Add to project', 'Print it on its own', 'Cancel']
    (event,) = handed
    assert [Path(p).name for p in event.media_urls] == ['arm.stl', 'arm.stl']
    assert [Path(p).read_bytes() for p in event.media_urls] == [b'left', b'right']
    assert event.message_id.startswith('collection-') and event.text == ''
    meta = event.metadata['upload_collection']
    assert [f['message_id'] for f in meta['files']] == ['m1', 'm2']
    assert event.source.user_id == 'u1'


def test_print_it_on_its_own_reaches_the_normal_intake(store, tmp_path, monkeypatch):
    part = tmp_path / ('doc_' + 'c' * 12 + '_tray.stl')
    part.write_bytes(b'tray')
    other = tmp_path / ('doc_' + 'd' * 12 + '_lid.stl')
    other.write_bytes(b'lid')
    scope = upload_collection.scope_key(_event('discord', '', [], 'x').source, 'k2')
    upload_collection.add(scope, [part])
    adapter = Adapter(['Print it on its own'])
    result = asyncio.run(plugin.upload_collection_dispatch(_event('discord', '', [other], 'm3'),
                                                           _gateway(adapter)))
    assert result is None  # the router goes on to model_link_dispatch with this message unchanged
    assert [f['name'] for f in upload_collection.current(scope)['files']] == ['tray.stl']
