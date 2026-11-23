"""Deleting the chat message that carried a pasted login token (best effort)."""
import asyncio
import json
import logging
from types import SimpleNamespace as S

from print_handy.bot import adapter as discord_adapter
from print_handy.bot import matrix, platform


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
    from tests.handler_namespace import plugin
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


def test_plugin_helper_falls_back_to_the_hermes_discord_client():
    from tests.handler_namespace import plugin
    deleted = []

    class Partial:
        async def delete(self):
            deleted.append(1)

    class Channel:
        def get_partial_message(self, mid):
            return Partial()

    class Hermes:
        _client = S(get_channel=lambda cid: Channel())

        async def send(self, chat_id, content, metadata=None):
            pass
    run(plugin._delete_secret_message(S(message_id='9'), S(chat_id='5', thread_id=None), Hermes()))
    assert deleted == [1]


def test_plugin_helper_deletes_when_delete_message_lives_only_on_host():
    """A wrapper with no delete_message of its own still removes the secret via _host."""
    from tests.handler_namespace import plugin
    deleted, sent = [], []

    class Host:
        async def delete_message(self, chat_id, message_id, metadata=None):
            deleted.append((chat_id, message_id, metadata))
            return True

    class Wrapper:
        def __init__(self):
            self._host = Host()

        async def send(self, chat_id, content, metadata=None):
            sent.append(content)

    run(plugin._delete_secret_message(
        S(message_id='9'), S(chat_id='5', thread_id='3'), Wrapper()))
    assert deleted == [('5', '9', {'thread_id': '3'})]
    assert sent == ['Token saved. I deleted your message.']


def test_a_failed_host_delete_keeps_the_note_and_does_not_log_the_secret():
    from tests.handler_namespace import plugin
    sent, records = [], []
    secret = "pasted-login-secret"

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())
            if record.exc_info and record.exc_info[1] is not None:
                records.append(str(record.exc_info[1]))

    class Host:
        async def delete_message(self, chat_id, message_id, metadata=None):
            raise RuntimeError(secret)

    class Wrapper:
        def __init__(self):
            self._host = Host()

        async def send(self, chat_id, content, metadata=None):
            sent.append(content)

    log = logging.getLogger("print_handy.bot.handlers.common")
    handler = Capture()
    log.addHandler(handler)
    try:
        run(plugin._delete_secret_message(
            S(message_id='9'), S(chat_id='5', thread_id=None), Wrapper()))
    finally:
        log.removeHandler(handler)
    assert "couldn't delete your message" in sent[0]
    assert secret not in sent[0]
    assert all(secret not in text for text in records)


def test_the_answered_note_never_echoes_a_long_unbroken_string():
    assert platform.answered_note('3 copies') == 'Answered by message: 3 copies'
    assert platform.answered_note('1234567890:' + 'a' * 30) == 'Answered by message.'
