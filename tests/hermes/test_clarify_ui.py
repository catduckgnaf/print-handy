"""HermesClarifyAdapter (#524 stage 2): plugin-owned question rendering by REST.

Fake-opener tests: no network, no Discord. The handler pattern is mirrored
exactly — register in Print Handy's own registry first, then send — so the
registry state these tests resolve is the state production sees with the
S5 switch on ([discord] clarify_ui, wired in test_clarify_wiring.py).
"""
import asyncio
import json
from types import SimpleNamespace as S

from print_handy.core import prompts
from print_handy.hermes import clarify_ui
from print_handy.hermes.clarify_ui import HermesClarifyAdapter

CID = "clarifyui1"
KEY = "live:chan1:user1"
QUESTION = "Pick a color?"
CHOICES = ["Red", "Blue", "Cancel"]


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Records (method, url, payload, authorization); POSTs get fresh message ids."""

    def __init__(self):
        self.requests = []
        self.fail = False
        self._seq = 0

    def __call__(self, req, timeout=None):
        if self.fail:
            raise OSError("no network in tests")
        payload = json.loads(req.data.decode()) if req.data else {}
        self.requests.append((req.method, req.full_url, payload, req.get_header("Authorization")))
        if req.method == "POST":
            self._seq += 1
            return FakeResponse({"id": f"m{self._seq}"})
        return FakeResponse({})

    def patches(self):
        return [r for r in self.requests if r[0] == "PATCH"]


class FakeInteraction:
    def __init__(self, channel_id="chan1"):
        self.channel_id = channel_id
        self.user = S(id="1", bot=False, display_name="op", name="op")
        self.message = S(id="m1", content=QUESTION)
        self.out = []
        interaction = self

        class Response:
            async def send_message(self, content, ephemeral=False, **kw):
                interaction.out.append(("send", content, ephemeral))

            async def defer(self):
                interaction.out.append(("defer",))

        self.response = Response()


def ask(opener, cid=CID, key=KEY, question=QUESTION, choices=CHOICES, chat_id="chan1", metadata=None):
    """The handler pattern: register in the own registry, then render."""
    prompts.register(cid, key, question, list(choices))
    ui = HermesClarifyAdapter(token="tok", opener=opener)
    result = asyncio.run(ui.send_clarify(chat_id, question, list(choices), cid, key, metadata))
    return ui, result


def close(cid=CID):
    prompts.resolve_gateway_clarify(cid, prompts.CANCELLED)
    prompts.wait_for_response(cid, 0.01)


# ---------------------------------------------------------------------- posting


def test_send_clarify_posts_the_pab_payload_shape():
    opener = FakeOpener()
    ui, result = ask(opener)
    try:
        assert result.success and result.message_id == "m1"
        assert len(opener.requests) == 1
        method, url, payload, auth = opener.requests[0]
        assert method == "POST" and url.endswith("/channels/chan1/messages")
        assert auth == "Bot tok"
        assert payload["content"] == QUESTION
        assert payload["allowed_mentions"] == {"parse": []}
        buttons = [b for row in payload["components"] for b in row["components"]]
        assert [b["label"] for b in buttons] == CHOICES
        assert [b["custom_id"] for b in buttons] == [f"pbq:{CID}:0", f"pbq:{CID}:1", f"pbq:{CID}:2"]
        # Standalone styling: ordinary choices primary (1), the cancel-like one secondary (2).
        assert [b["style"] for b in buttons] == [1, 1, 2]
        assert all(row["type"] == 1 and all(b["type"] == 2 for b in row["components"])
                   for row in payload["components"])
        # The returned message id is recorded against the prompt.
        row = ui._prompts[CID]
        assert row["message_id"] == "m1" and row["channel_id"] == "chan1" and row["choices"] == CHOICES
    finally:
        close()


def test_send_clarify_targets_the_thread_from_metadata():
    opener = FakeOpener()
    ui, result = ask(opener, metadata={"thread_id": "thread9"})
    try:
        assert result.success
        assert opener.requests[0][1].endswith("/channels/thread9/messages")
        assert ui._prompts[CID]["channel_id"] == "thread9"
    finally:
        close()


def test_send_clarify_caps_at_25_choices_in_rows_of_five():
    opener = FakeOpener()
    choices = [f"Choice {i}" for i in range(30)]
    ui, result = ask(opener, choices=choices)
    try:
        assert result.success
        rows = opener.requests[0][2]["components"]
        assert len(rows) == 5 and all(len(row["components"]) == 5 for row in rows)
        buttons = [b for row in rows for b in row["components"]]
        assert [b["custom_id"] for b in buttons] == [f"pbq:{CID}:{i}" for i in range(25)]
        assert ui._prompts[CID]["choices"] == choices[:25]
    finally:
        close()


def test_send_clarify_truncates_labels_at_80_chars():
    opener = FakeOpener()
    ui, result = ask(opener, choices=["x" * 120, "Cancel"])
    try:
        assert result.success
        buttons = [b for row in opener.requests[0][2]["components"] for b in row["components"]]
        assert buttons[0]["label"] == "x" * 80
    finally:
        close()


def test_send_clarify_long_question_chunks_plain_then_buttons_ride_the_last():
    opener = FakeOpener()
    question = "a" * 2000 + "\n" + "tail?"
    ui, result = ask(opener, question=question)
    try:
        assert result.success
        assert len(opener.requests) == 2
        first, last = opener.requests[0][2], opener.requests[1][2]
        assert first["content"] == "a" * 2000 and "components" not in first
        assert last["content"] == "tail?" and "components" in last
        assert ui._prompts[CID]["question"] == "tail?"
    finally:
        close()


def test_send_clarify_without_a_token_fails_closed(monkeypatch):
    opener = FakeOpener()
    monkeypatch.setattr(clarify_ui._config, "discord_token", lambda: "")
    prompts.register(CID, KEY, QUESTION, CHOICES)
    ui = HermesClarifyAdapter(token=None, opener=opener)
    result = asyncio.run(ui.send_clarify("chan1", QUESTION, CHOICES, CID, KEY))
    try:
        assert not result.success and "token" in (result.error or "")
        assert opener.requests == []
    finally:
        close()


def test_send_clarify_transport_failure_fails_closed():
    opener = FakeOpener()
    opener.fail = True
    ui, result = ask(opener)
    try:
        assert not result.success and result.error
        assert CID not in ui._prompts
    finally:
        close()


# ---------------------------------------------------------------------- clicks


def test_handle_click_resolves_and_acks_by_rest_patch():
    opener = FakeOpener()
    ui, _ = ask(opener)
    interaction = FakeInteraction()
    assert asyncio.run(ui.handle_click(interaction, CID, 1)) is True
    assert prompts.wait_for_response(CID, 1) == "Blue"
    patches = opener.patches()
    assert len(patches) == 1
    _, url, payload, _ = patches[0]
    assert url.endswith("/channels/chan1/messages/m1")
    assert payload["content"] == f"{QUESTION}\n> op chose **Blue**"
    assert payload["components"] == []
    assert ("defer",) in interaction.out
    assert CID not in ui._prompts


def test_handle_click_in_another_channel_never_resolves():
    opener = FakeOpener()
    ui, _ = ask(opener)
    try:
        interaction = FakeInteraction(channel_id="elsewhere")
        assert asyncio.run(ui.handle_click(interaction, CID, 0)) is True
        assert interaction.out == [("send", "Not allowed. Nothing changed.", True)]
        assert prompts.is_open(CID) and opener.patches() == []
    finally:
        close()


def test_handle_click_on_a_closed_question_says_so():
    opener = FakeOpener()
    ui, _ = ask(opener)
    prompts.resolve_gateway_clarify(CID, "Red")
    prompts.wait_for_response(CID, 0.01)
    interaction = FakeInteraction()
    assert asyncio.run(ui.handle_click(interaction, CID, 0)) is True
    assert interaction.out == [("send", "That question already closed. Nothing changed.", True)]
    assert opener.patches() == []


# ---------------------------------------------------------------------- typed answers


def test_answer_typed_resolves_the_sessions_newest_prompt_and_retires_it():
    opener = FakeOpener()
    ui = HermesClarifyAdapter(token="tok", opener=opener)
    other = "clarifyui-other"
    prompts.register(other, "live:chan2:user2", "Other?", ["Yes"])
    asyncio.run(ui.send_clarify("chan2", "Other?", ["Yes"], other, "live:chan2:user2"))
    prompts.register(CID, KEY, QUESTION, CHOICES)
    asyncio.run(ui.send_clarify("chan1", QUESTION, CHOICES, CID, KEY))
    try:
        assert asyncio.run(ui.answer_typed(KEY, "3 copies")) is True
        assert prompts.wait_for_response(CID, 1) == "3 copies"
        patches = opener.patches()
        assert len(patches) == 1 and patches[0][1].endswith("/channels/chan1/messages/m2")
        assert patches[0][2]["content"] == f"{QUESTION}\n> Answered by message: 3 copies"
        assert patches[0][2]["components"] == []
        # Another session's prompt is never claimed by this session's text.
        assert prompts.is_open(other)
        assert asyncio.run(ui.answer_typed("live:nobody", "x")) is False
    finally:
        close(other)
        close()


def test_close_prompt_edits_once_and_forgets_the_row():
    opener = FakeOpener()
    ui, _ = ask(opener)
    try:
        asyncio.run(ui.close_prompt(CID, "Cancelled or expired."))
        patches = opener.patches()
        assert len(patches) == 1
        assert patches[0][2]["content"] == f"{QUESTION}\n> Cancelled or expired."
        assert patches[0][2]["components"] == []
        assert CID not in ui._prompts
        asyncio.run(ui.close_prompt(CID, "again"))
        assert len(opener.patches()) == 1  # nothing left to edit
    finally:
        close()


def test_retire_prompt_strips_buttons_without_an_answer_line():
    # S6: a superseded question (the Stop step moved on) stops being
    # answerable in place — same edit as a click, minus the answer line.
    opener = FakeOpener()
    ui, _ = ask(opener)
    try:
        assert asyncio.run(ui.retire_prompt(CID)) is True
        patches = opener.patches()
        assert len(patches) == 1
        assert patches[0][2]["content"] == QUESTION  # text unchanged
        assert patches[0][2]["components"] == []
        assert CID not in ui._prompts
        assert asyncio.run(ui.retire_prompt(CID)) is False  # already gone: nothing to edit
        assert len(opener.patches()) == 1
    finally:
        close()
