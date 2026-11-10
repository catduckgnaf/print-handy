"""A restart mid-intake leaves one line in the chat instead of a status that never changes."""
import asyncio
import json
import os
import time

import pytest

from print_handy.core import owned_worker, prepared_intake


class Adapter:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, chat_id, content, metadata=None, **kwargs):
        if self.fail:
            raise RuntimeError('discord down')
        self.sent.append((chat_id, content, metadata))


def receipt(root, name, **fields):
    data = {'intake_id': name, 'status': 'AWAITING_COLOR', 'chat_id': '100', 'thread_id': None,
            'owner': 'another-process', **fields}
    path = root / f'{name}.json'
    path.write_text(json.dumps(data))
    return path


def announce(root, adapter, **kw):
    return asyncio.run(prepared_intake.announce_interrupted(root, adapter, **kw))


def test_stranded_intake_is_announced_once(tmp_path):
    path = receipt(tmp_path, 'a', thread_id='55', status='INSPECTING_SOURCE')
    adapter = Adapter()
    assert announce(tmp_path, adapter) == ['a']
    chat, text, metadata = adapter.sent[0]
    assert chat == '100' and metadata == {'thread_id': '55'}
    assert 'restarted' in text and 'nothing was uploaded or started' in text and 'again' in text
    saved = json.loads(path.read_text())
    assert saved['status'] == 'INTERRUPTED' and saved['interrupted_from'] == 'INSPECTING_SOURCE'
    assert announce(tmp_path, adapter) == []  # said once
    assert len(adapter.sent) == 1


def test_a_failed_post_is_not_repeated(tmp_path):
    receipt(tmp_path, 'a')
    assert announce(tmp_path, Adapter(fail=True)) == ['a']
    adapter = Adapter()
    assert announce(tmp_path, adapter) == []
    assert adapter.sent == []


def test_only_stranded_receipts_are_announced(tmp_path):
    receipt(tmp_path, 'finished', status='SLICED')
    receipt(tmp_path, 'expired', status='EXPIRED')
    receipt(tmp_path, 'live', owner=owned_worker._OWNER)  # this process is still running it
    receipt(tmp_path, 'legacy', chat_id=None)  # written before receipts carried a chat
    receipt(tmp_path, 'noowner', owner=None)
    old = receipt(tmp_path, 'old')
    stale = time.time() - 3 * 24 * 3600
    os.utime(old, (stale, stale))
    (tmp_path / 'broken.json').write_text('{not json')
    adapter = Adapter()
    assert announce(tmp_path, adapter) == []
    assert adapter.sent == []


def test_missing_folder_is_fine(tmp_path):
    assert announce(tmp_path / 'absent', Adapter()) == []


class WebAdapter(Adapter):
    name = 'webui'


OTHER = 'ab' * 16  # another process's owner id (owned_worker._OWNER shape)


def _hold_owner_lock(root, owner):
    """Stand in for a live process holding its liveness lock: a second open
    file description's flock conflicts exactly as another process's would."""
    import fcntl
    path = root / '.owners' / f'{owner}.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


@pytest.mark.skipif(prepared_intake.fcntl is None, reason='needs POSIX file locks')
def test_opening_web_chat_leaves_a_live_discord_intake_alone(tmp_path):
    """The web chat runs in the Web UI process. Its first use must not mark
    the chat bot's in-progress intake interrupted, post a false restart note,
    or spend the notice a later real restart owes that chat."""
    path = receipt(tmp_path, 'live', platform='discord', owner=OTHER)
    held = _hold_owner_lock(tmp_path, OTHER)
    try:
        web = WebAdapter()
        assert announce(tmp_path, web) == []
        assert web.sent == []
        # The Discord bot's own process is still running it, so even another
        # Discord-platform process leaves it alone while that lock is held.
        assert announce(tmp_path, Adapter()) == []
        assert json.loads(path.read_text())['status'] == 'AWAITING_COLOR'
    finally:
        os.close(held)  # the bot process dies: the kernel drops its lock
    bot = Adapter()
    assert announce(tmp_path, bot) == ['live']  # the real restart announces it
    assert len(bot.sent) == 1 and 'restarted' in bot.sent[0][1]
    assert announce(tmp_path, bot) == [] and len(bot.sent) == 1  # once
    assert not (tmp_path / '.owners' / f'{OTHER}.lock').exists()  # the dead owner's lock is cleaned


def test_web_chat_announces_only_its_own_receipts(tmp_path):
    receipt(tmp_path, 'discord', platform='discord')
    receipt(tmp_path, 'legacy')  # no platform field: belongs to the chat bot, never web chat
    receipt(tmp_path, 'web', platform='webui', chat_id='webui')
    web = WebAdapter()
    assert announce(tmp_path, web) == ['web']
    assert [chat for chat, _, _ in web.sent] == ['webui']
    bot = Adapter()
    assert sorted(announce(tmp_path, bot)) == ['discord', 'legacy']
    assert announce(tmp_path, WebAdapter()) == []


@pytest.mark.skipif(prepared_intake.fcntl is None, reason='needs POSIX file locks')
def test_a_process_that_wrote_receipts_reads_as_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(prepared_intake, '_OWNER_LOCK', None)
    prepared_intake.claim_receipt_owner(tmp_path)
    held = prepared_intake._OWNER_LOCK
    try:
        assert held is not None
        assert prepared_intake._owner_alive(tmp_path, owned_worker._OWNER)
        assert not prepared_intake._owner_alive(tmp_path, OTHER)  # never claimed
        assert not prepared_intake._owner_alive(tmp_path, '../../etc/passwd')
    finally:
        os.close(held)


def test_two_stranded_intakes_in_one_chat_get_one_notice(tmp_path):
    receipt(tmp_path, 'a')
    receipt(tmp_path, 'b')
    receipt(tmp_path, 'c', chat_id='200')
    adapter = Adapter()
    assert sorted(announce(tmp_path, adapter)) == ['a', 'b', 'c']
    by_chat = {chat: text for chat, text, _ in adapter.sent}
    assert len(adapter.sent) == 2 and set(by_chat) == {'100', '200'}
    assert '2 models' in by_chat['100'] and 'that model' in by_chat['200']
    assert announce(tmp_path, adapter) == [] and len(adapter.sent) == 2
