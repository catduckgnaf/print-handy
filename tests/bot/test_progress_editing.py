"""Progress editing on the production bot adapters (P2-236).

prepared_intake's FitStatus edits one status message in place when the
adapter offers edit_message — but the standalone Discord/Telegram/Matrix
adapters never implemented it, so production progress was a stream of new
messages (and a full re-post at the fallback heartbeat). Each adapter now
edits through its own transport. Offline stubs only: no network, no
accounts.
"""
import asyncio
import io
import json
from types import SimpleNamespace as S

from print_handy.bot import matrix, telegram
from print_handy.bot.adapter import StandaloneAdapter as DiscordAdapter
from print_handy.core import prepared_intake

USER = "424242"
CHAT = "-1001234567890"
ROOM = "!room:example.org"
HOMESERVER = "https://matrix.example.org"
FAKE = "t" * 4


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class TelegramStub:
    """Records Bot API calls by method; answers a canned result per call."""

    def __init__(self):
        self.calls = []
        self._next_id = 100

    def __call__(self, req, timeout=None):
        method = req.full_url.rsplit("/", 1)[-1]
        fields = json.loads(req.data or b"{}") if req.data else {}
        self.calls.append((method, fields))
        self._next_id += 1
        return _Response(json.dumps({"ok": True, "result": {"message_id": self._next_id}}).encode())

    def payloads(self, method):
        return [fields for name, fields in self.calls if name == method]


class MatrixStub:
    """Records room sends; answers a fresh event id per send."""

    def __init__(self):
        self.calls = []
        self._next = 100

    def __call__(self, req, timeout=None):
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.full_url, body))
        self._next += 1
        return _Response(json.dumps({"event_id": f"$evt{self._next}"}).encode())

    def bodies(self):
        return [body for _url, body in self.calls]


def run(coro):
    return asyncio.run(coro)


def test_telegram_edit_message_uses_edit_message_text():
    stub = TelegramStub()
    adapter = telegram.StandaloneAdapter(None, [USER], token=FAKE, opener=stub)
    result = run(adapter.edit_message(chat_id=CHAT, message_id="101", content="Still slicing (30 s)."))
    assert result.success and result.message_id == "101"
    [payload] = stub.payloads("editMessageText")
    assert payload == {"chat_id": CHAT, "message_id": 101, "text": "Still slicing (30 s)."}


def test_matrix_edit_message_posts_an_m_replace_relation():
    stub = MatrixStub()
    adapter = matrix.StandaloneAdapter(None, [USER], homeserver=HOMESERVER, token=FAKE, opener=stub)
    result = run(adapter.edit_message(chat_id=ROOM, message_id="$evt5", content="Still slicing (30 s)."))
    assert result.success and result.message_id == "$evt5"  # edits keep targeting the original event
    [body] = stub.bodies()
    assert body["m.new_content"] == {"msgtype": "m.text", "body": "Still slicing (30 s).",
                                     "m.mentions": {}}
    assert body["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$evt5"}


def test_discord_edit_message_edits_the_partial_message():
    class FakeMessage:
        def __init__(self):
            self.edits = []

        async def edit(self, **kwargs):
            self.edits.append(kwargs)

    class FakeChannel:
        id = 123

        def __init__(self):
            self.message = FakeMessage()
            self.requested = None

        def get_partial_message(self, message_id):
            self.requested = message_id
            return self.message

    class FakeClient:
        def __init__(self, channel):
            self.channel = channel

        def get_channel(self, chat_id):
            return self.channel

    channel = FakeChannel()
    adapter = DiscordAdapter(FakeClient(channel), [USER])
    result = run(adapter.edit_message(chat_id="123", message_id="456", content="Still slicing (30 s)."))
    assert result.success and result.message_id == "456"
    assert channel.requested == 456
    assert channel.message.edits == [{"content": "Still slicing (30 s)."}]


def test_fit_status_edits_its_one_message_on_a_production_adapter():
    """The intake-level point of P2-236: with edit_message on the adapter,
    the second progress phase updates the first message instead of posting
    another one."""
    stub = TelegramStub()
    adapter = telegram.StandaloneAdapter(None, [USER], token=FAKE, opener=stub)
    source = S(chat_id=CHAT, thread_id=None)

    async def flow():
        status = prepared_intake.FitStatus(adapter, source, {})
        assert status.can_edit
        await status.show("Checking the fit: first (0 s).")
        await status.show("Still checking the fit: first (15 s).")

    run(flow())
    assert len(stub.payloads("sendMessage")) == 1
    [edit] = stub.payloads("editMessageText")
    assert edit["text"] == "Still checking the fit: first (15 s)."
