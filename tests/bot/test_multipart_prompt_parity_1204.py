"""Multi-part prompts render the same on every transport (#1204 item 5).

The multi-part questions (the /print-again project scope question, the
/print-next "which plate is next" question, and the bed-clear approval
that follows either) are built once and sent through each transport's
``send_clarify``: Discord buttons, the Telegram inline keyboard, the
Matrix numbered list and the web chat outbox. The parity contract is the
same question text and the same choice labels, whole, on all four, and
the same answer for the same pick. Offline: fake Discord channel, the
Telegram and Matrix stub openers, and the web chat outbox.
"""
import asyncio

import pytest

from print_handy.bot import matrix, telegram
from print_handy.bot.adapter import StandaloneAdapter as DiscordAdapter
from print_handy.bot.web import WebAdapter
from print_handy.core import projects, prompts
from print_handy.runtime import print_control, print_next

from .test_matrix import HOMESERVER, ROOM, HomeserverStub
from .test_matrix import TOKEN as MX_TOKEN
from .test_matrix import USER as MX_USER
from .test_telegram import CHAT, BotApiStub
from .test_telegram import TOKEN as TG_TOKEN
from .test_telegram import USER as TG_USER

DISCORD_CHANNEL = "123456789012345678"


def run(coro):
    return asyncio.run(coro)


class _Message:
    id = 987654321


class _Channel:
    id = int(DISCORD_CHANNEL)

    def __init__(self):
        self.sent = []

    async def send(self, content=None, view=None, allowed_mentions=None, **_kw):
        self.sent.append({"content": content,
                          "labels": [item.label for item in view.children] if view else None})
        return _Message()


class _Client:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, _chat_id):
        return self.channel


def _render(transport, question, choices, cid):
    """(question text shown, choice labels shown) on one transport."""
    prompts.register(cid, f"{transport}:flow", question, choices)
    if transport == "discord":
        channel = _Channel()
        adapter = DiscordAdapter(_Client(channel), ["1"])
        assert run(adapter.send_clarify(DISCORD_CHANNEL, question, choices, cid, "flow")).success
        return channel.sent[-1]["content"], channel.sent[-1]["labels"]
    if transport == "telegram":
        stub = BotApiStub()
        adapter = telegram.StandaloneAdapter(None, [TG_USER], token=TG_TOKEN, opener=stub)
        assert run(adapter.send_clarify(CHAT, question, choices, cid, "flow")).success
        payload = stub.payloads("sendMessage")[-1]
        return payload["text"], [row[0]["text"] for row in payload["reply_markup"]["inline_keyboard"]]
    if transport == "matrix":
        stub = HomeserverStub()
        adapter = matrix.StandaloneAdapter(None, [MX_USER], homeserver=HOMESERVER,
                                           token=MX_TOKEN, opener=stub)
        assert run(adapter.send_clarify(ROOM, question, choices, cid, "flow")).success
        body = stub.sent_bodies()[-1]
        shown, _, listing = body.rpartition("\n\n")
        lines = [ln for ln in listing.splitlines() if ln[:1].isdigit()]
        return shown, [ln.split(". ", 1)[1] for ln in lines]
    class _Outbox(list):
        # The web adapter's outbox takes the browser session scope (#1145).
        def append(self, entry, scope=""):
            super().append(entry)

    outbox = _Outbox()
    adapter = WebAdapter(outbox)
    assert run(adapter.send_clarify("web", question, choices, cid, "flow")).success
    return outbox[-1]["question"], outbox[-1]["choices"]


TRANSPORTS = ("discord", "telegram", "matrix", "webui")


def _plan(tmp_path, labels):
    parts = []
    for n, label in enumerate(labels, 1):
        g = tmp_path / f"plate_{n}.gcode"
        g.write_text("G1 X1\n")
        parts.append({"id": f"plate{n}", "label": label, "plate": n, "quantity": 1,
                      "completed_quantity": 0, "status": "planned",
                      "job_json": str(tmp_path / "JOB.json"), "gcode_path": str(g)})
    return {"plan_id": "plan-shelf", "title": "Shelf kit", "parts": parts}


def _prompts(tmp_path):
    long = "Left bracket with the engraved logo, the extra brim piece and spacer"
    plate_q, plate_labels, _ids = print_next.plate_question(
        _plan(tmp_path, [long, "Right bracket", "Shelf board"]))
    info = {"project_number": 82, "title": "Shelf kit",
            "plates": [{"ref": "82A", "suffix": "A", "gcode": None},
                       {"ref": "82B", "suffix": "B", "gcode": None}]}
    scope_q = print_control.scope_question({"gcode": ""}, info)
    return {
        "plate": (plate_q, plate_labels),
        "scope": (scope_q, list(projects.scope_choices(True, True))),
        "again": ("Print plate 82B again? Confirm the bed is clear.",
                  [print_control.AGAIN_LABEL, print_control.AGAIN_CANCEL_LABEL]),
        "next": ("Next plate for Shelf kit: Shelf board. Confirm the bed is clear.",
                 [print_next.START_LABEL, print_next.CANCEL_LABEL]),
    }


@pytest.mark.parametrize("prompt", ["plate", "scope", "again", "next"])
def test_multipart_prompts_match_across_transports(tmp_path, prompt):
    question, choices = _prompts(tmp_path)[prompt]
    shown = {t: _render(t, question, choices, f"{prompt[:2]}{i:02d}" + "ab" * 15)
             for i, t in enumerate(TRANSPORTS)}
    for transport, (text, labels) in shown.items():
        assert text == question, transport
        assert labels == choices, transport  # every label whole, none cut by one transport


def test_plate_question_fits_the_smallest_button_label(tmp_path):
    question, labels, ids = print_next.plate_question(
        _plan(tmp_path, ["x" * 120, "Right bracket"]))
    assert ids == ["plate1", "plate2"] and labels[-1] == print_next.PLATE_LATER_LABEL
    assert all(len(label) <= min(telegram.LABEL_LIMIT, 80) for label in labels)
    assert len(set(labels)) == len(labels)
    assert "2 plates are left" in question and "bed is clear" in question


def test_one_remaining_plate_asks_no_plate_question(tmp_path):
    plan = _plan(tmp_path, ["Only plate", "Done plate"])
    plan["parts"][1].update(completed_quantity=1, status="completed")
    assert print_next.plate_question(plan) is None


def test_a_typed_matrix_answer_and_a_telegram_button_pick_the_same_plate(tmp_path):
    question, labels, ids = print_next.plate_question(
        _plan(tmp_path, ["Left bracket", "Right bracket", "Shelf board"]))

    async def matrix_pick():
        stub = HomeserverStub()
        adapter = matrix.StandaloneAdapter(None, [MX_USER], homeserver=HOMESERVER,
                                           token=MX_TOKEN, opener=stub)
        cid, key = "c1" * 16, f"matrix:{ROOM}::{MX_USER}"
        prompts.register(cid, key, question, labels)
        await adapter.send_clarify(ROOM, question, labels, cid, "flow")
        assert await adapter.answer_typed(key, "2") is True
        return await asyncio.to_thread(prompts.wait_for_response, cid, 1)

    async def telegram_pick():
        stub = BotApiStub()
        adapter = telegram.StandaloneAdapter(None, [TG_USER], token=TG_TOKEN, opener=stub)
        cid = "c2" * 16
        prompts.register(cid, "telegram:flow", question, labels)
        await adapter.send_clarify(CHAT, question, labels, cid, "flow")
        await adapter.handle_callback_query({
            "id": "q1", "from": {"id": int(TG_USER), "first_name": "Cat"},
            "data": f"pbq:{cid}:1",
            "message": {"message_id": 101, "chat": {"id": int(CHAT)}}})
        return await asyncio.to_thread(prompts.wait_for_response, cid, 1)

    assert run(matrix_pick()) == run(telegram_pick()) == labels[1]
    assert ids[labels.index(labels[1])] == "plate2"
