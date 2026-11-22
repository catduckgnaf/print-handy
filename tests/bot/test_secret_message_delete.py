"""Deleting the chat message that carried a pasted login token (best effort)."""
import asyncio
import json
from types import SimpleNamespace as S

from print_handy.bot import adapter as discord_adapter
from print_handy.bot import matrix, platform
from print_handy.bot.router import load_plugin


def run(coro):
    return asyncio.run(coro)


class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_base_adapter_deletes_nothing():
    adapter = platform.BasePlatformAdapter([])
    assert run(adapter.delete_message('c', '1')) is False


def test_discord_deletes_through_a_partial_message():
    deleted = []

    class Partial:
        def __init__(self, mid):
            self.mid = mid

        async def delete(self):
            deleted.append(self.mid)

    class Channel:
        def get_partial_message(self, mid):
            return Partial(mid)

    client = S(get_channel=lambda cid: Channel(), fetch_channel=None)
    adapter = discord_adapter.StandaloneAdapter(client, [])
    assert run(adapter.delete_message('5', '77')) is True and deleted == [77]


def test_discord_delete_failure_is_false_not_an_error():
    def boom(cid):
        raise RuntimeError('missing permissions')
    adapter = discord_adapter.StandaloneAdapter(S(get_channel=boom), [])
    assert run(adapter.delete_message('5', '77')) is False


def test_matrix_redacts_the_event():
    calls = []

    def opener(req, timeout=None):
        calls.append((req.get_method(), req.full_url))
        return _Resp(json.dumps({'event_id': '$r'}).encode())
    adapter = matrix.StandaloneAdapter(None, [], homeserver='https://hs', token='t', opener=opener)
    assert run(adapter.delete_message('!room:hs', '$evt:hs')) is True
    method, url = calls[0]
    assert method == 'PUT' and '/rooms/%21room%3Ahs/redact/%24evt%3Ahs/' in url


def test_plugin_helper_uses_the_adapter_and_says_what_happened():
    plugin = load_plugin()
    sent = []

    class Good:
        async def delete_message(self, chat_id, message_id, metadata=None):
            return True

        async def send(self, chat_id, content, metadata=None):
            sent.append(content)

    class Bad(Good):
        async def delete_message(self, chat_id, message_id, metadata=None):
            return False
    source = S(chat_id='c', thread_id=None)
    event = S(message_id='9')
    run(plugin._delete_secret_message(event, source, Good()))
    run(plugin._delete_secret_message(event, source, Bad()))
    assert sent[0] == 'Token saved. I deleted your message.'
    assert "couldn't delete your message" in sent[1]


def test_plugin_helper_uses_only_the_adapters_delete_message():
    # #1817 L7: no fallback to a Hermes adapter's _host or discord.py _client.
    plugin = load_plugin()
    sent, deleted = [], []

    class Partial:
        async def delete(self):
            deleted.append(1)

    class Channel:
        def get_partial_message(self, mid):
            return Partial()

    class NoDelete:
        _client = S(get_channel=lambda cid: Channel())

        async def send(self, chat_id, content, metadata=None):
            sent.append(content)
    run(plugin._delete_secret_message(S(message_id='9'), S(chat_id='5', thread_id=None), NoDelete()))
    assert deleted == []
    assert "couldn't delete your message" in sent[0]


def test_the_answered_note_never_echoes_a_long_unbroken_string():
    assert platform.answered_note('3 copies') == 'Answered by message: 3 copies'
    assert platform.answered_note('1234567890:' + 'a' * 30) == 'Answered by message.'
