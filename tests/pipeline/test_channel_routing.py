"""Two channels, two printers, end to end (#335 acceptance): offline.

Reuses the prepared-intake harness (stubbed slicing; the start goes through
the real print_next.start gates with a stub print-now) and drives
prepare_link once per channel with that channel's printer bound, the way
model_link_dispatch binds it from the routing map. Each job must be stamped
with its own printer, every question and post must land in its own channel,
and each start must target its own job. A flow with no printer bound keeps
the legacy behaviour: the job belongs to the default (first) printer.
"""
import asyncio
import json
from types import SimpleNamespace as S

import pytest

from print_handy import settings
from print_handy.core import prepared_intake
from .test_prepared_intake import Adapter, env  # noqa: F401  (env is the shared harness fixture)
from .test_color_intake import state

TWO_PRINTERS = """
discord:
  chat_id: "111"
printers:
  - id: garage-ad5x
    name: Garage AD5X
    model: flashforge.ad5x
    moonraker_url: http://192.168.1.60:7125
  - id: workshop-ad5x
    name: Workshop AD5X
    model: flashforge.ad5x
    chat_id: "222"
    moonraker_url: http://192.168.1.61:7125
"""

PICKS = [2, 0, 0]  # one color for everything (2 on the #1473 offer), slot 1, then "Slice and print now"


@pytest.fixture
def two_printers(tmp_path, monkeypatch):
    local = tmp_path / "local.yaml"
    local.write_text(TWO_PRINTERS)
    data = settings.load(home=settings.HOME, local=local, environ={})
    assert [entry["id"] for entry in data["printers"]] == ["garage-ad5x", "workshop-ad5x"]
    monkeypatch.setattr(settings, "current", lambda: data)
    return data


class ChannelAdapter(Adapter):
    """The harness adapter, recording which channel every post and question went to."""

    def __init__(self, picks):
        super().__init__(picks)
        self.chats = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.chats.append(str(chat_id))
        return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)

    async def send_clarify(self, **kw):
        self.chats.append(str(kw.get("chat_id")))
        return await super().send_clarify(**kw)


def run_flow(env, chat_id, printer_id, mid):
    event = S(source=S(profile="3dprinting", chat_id=chat_id, thread_id=None, user_id="u"),
              message_id=mid, text="https://www.printables.com/model/1-ghost")
    adapter = ChannelAdapter(list(PICKS))
    before = set(env.root.glob("*.json"))
    result = asyncio.run(prepared_intake.prepare_link(
        event, adapter, state(), env.root, timeout=1, printer_id=printer_id))
    fresh = [path for path in env.root.glob("*.json") if path not in before]
    assert len(fresh) == 1
    return adapter, result, json.loads(fresh[0].read_text())


def test_two_channels_drive_two_printers_end_to_end(env, two_printers):
    adapter_a, _result_a, receipt_a = run_flow(env, "111", "garage-ad5x", "m-a")
    adapter_b, _result_b, receipt_b = run_flow(env, "222", "workshop-ad5x", "m-b")

    for receipt, pid in ((receipt_a, "garage-ad5x"), (receipt_b, "workshop-ad5x")):
        assert receipt["status"] == "STARTED" and receipt["started"] is True
        job = json.loads(open(receipt["job_json"]).read())
        assert job["printer_id"] == pid  # the intake channel's printer owns the job
    assert receipt_a["job_json"] != receipt_b["job_json"]

    # Every question and post stayed in the flow's own channel.
    assert adapter_a.chats and set(adapter_a.chats) == {"111"}
    assert adapter_b.chats and set(adapter_b.chats) == {"222"}

    # Each start targeted its own channel's job, in order.
    assert [start["job_json"] for start in env.starts] == [receipt_a["job_json"], receipt_b["job_json"]]


def test_an_unbound_flow_stamps_the_default_printer(env, two_printers):
    # Legacy single-channel behaviour: no routed printer -> the default (first
    # roster entry) owns the job, exactly as before routing existed.
    _adapter, _result, receipt = run_flow(env, "111", None, "m-legacy")
    assert receipt["status"] == "STARTED"
    job = json.loads(open(receipt["job_json"]).read())
    assert job["printer_id"] == "garage-ad5x"


def test_an_unknown_printer_id_fails_closed(env, two_printers):
    _adapter, result, receipt = run_flow(env, "222", "no-such-printer", "m-bad")
    assert result["action"] == "skip"
    assert receipt["status"] == "FAILED"
    assert "no-such-printer" in receipt["error"]  # the bad id is named, never silently re-stamped
    assert not env.starts
