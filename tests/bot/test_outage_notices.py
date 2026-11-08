"""The bot-side outage callback (#1020): delivery results and routing.

``outage_alert.tick`` trusts the callback it is given: True means the
notice was delivered and its state change may be persisted, False means
retry next tick. These tests pin the callback in ``BotCore`` to the
adapter contract — a failed platform send comes back as
``SendResult(success=False)``, not an exception — and to the shared
channel routing (#536): a printer whose channel is shared with another
printer still gets its notices in that channel, not the home channel.
"""
import asyncio
import time

from corpus import fake_discord
from print_handy import settings
from print_handy.bot import outage_alert, runtime
from print_handy.bot.platform import SendResult

HOME = "100000000000000001"
SHARED = "555000111222333444"
DOWN = {"p1": {"required": True, "ok": False, "reason": "listener heartbeat stale or invalid"}}
UP = {"p1": {"required": True, "ok": True, "reason": "listener event loop alive"}}


class StubAdapter:
    """Records sends and answers with a scripted SendResult."""

    name = "discord"
    uses_llm = False

    def __init__(self, result):
        self.result = result
        self.calls = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls.append((str(chat_id), content))
        return self.result


def make_core(cfg, tmp_path, result, printers):
    cfg["printers"] = printers
    cfg["paths"]["hermes_home"] = tmp_path / "hermes-home"
    discord = fake_discord.FakeDiscord(auto=True, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, cfg)
    core.adapter = StubAdapter(result)
    return core


def seed_state(core, entry):
    outage_alert.save(settings.state_dir(core.cfg), {"p1": entry})


def old_outage():
    return {"since": time.time() - 10000, "notified_at": None}


def run(coro):
    return asyncio.run(coro)


def test_a_failed_send_is_not_recorded_and_is_retried(cfg, tmp_path):
    core = make_core(cfg, tmp_path, SendResult(False, error="platform down"),
                     [{"id": "p1", "chat_id": "424242"}])
    core._listener_statuses = lambda: DOWN
    seed_state(core, old_outage())
    messages = run(core.check_listener_outages())
    assert len(messages) == 1
    assert len(core.adapter.calls) == 1
    state = outage_alert.load(settings.state_dir(core.cfg))
    assert state["p1"]["notified_at"] is None, "a failed down notice was persisted as delivered"
    run(core.check_listener_outages())
    assert len(core.adapter.calls) == 2, "the failed notice was not retried on the next tick"
    core.adapter.result = SendResult(True, message_id="1")
    run(core.check_listener_outages())
    assert len(core.adapter.calls) == 3
    state = outage_alert.load(settings.state_dir(core.cfg))
    assert state["p1"]["notified_at"] is not None
    run(core.check_listener_outages())
    assert len(core.adapter.calls) == 3, "a delivered notice was repeated inside the reminder window"


def test_a_failed_recovery_send_keeps_the_outage_state(cfg, tmp_path):
    core = make_core(cfg, tmp_path, SendResult(False, error="platform down"),
                     [{"id": "p1", "chat_id": "424242"}])
    core._listener_statuses = lambda: UP
    since = time.time() - 10000
    seed_state(core, {"since": since, "notified_at": since + 200})
    messages = run(core.check_listener_outages())
    assert len(messages) == 1
    state = outage_alert.load(settings.state_dir(core.cfg))
    assert state.get("p1", {}).get("notified_at") is not None, \
        "a failed recovery notice cleared the outage for good"
    core.adapter.result = SendResult(True, message_id="1")
    run(core.check_listener_outages())
    assert len(core.adapter.calls) == 2, "the failed recovery notice was not retried"
    state = outage_alert.load(settings.state_dir(core.cfg))
    assert "p1" not in state


def test_outage_notices_use_the_shared_channel_not_the_home_channel(cfg, tmp_path):
    printers = [{"id": "p1", "chat_id": SHARED}, {"id": "p2", "chat_id": SHARED}]
    core = make_core(cfg, tmp_path, SendResult(True, message_id="1"), printers)
    assert SHARED not in core.channel_printers, "fixture channel must be shared, not exclusive"
    core._listener_statuses = lambda: DOWN
    seed_state(core, old_outage())
    run(core.check_listener_outages())
    assert [chat for chat, _text in core.adapter.calls] == [SHARED]


def test_outage_notices_fall_back_to_the_home_channel_without_a_roster(cfg, tmp_path):
    core = make_core(cfg, tmp_path, SendResult(True, message_id="1"), [])
    core._listener_statuses = lambda: DOWN
    seed_state(core, old_outage())
    run(core.check_listener_outages())
    assert [chat for chat, _text in core.adapter.calls] == [HOME]


class PinningAdapter(StubAdapter):
    """A StubAdapter that also supports pinning (Discord does)."""

    def __init__(self, result):
        super().__init__(result)
        self.pinned = []

    async def pin_message(self, chat_id, message_id):
        self.pinned.append((str(chat_id), message_id))
        return True


def test_exhausted_budget_escalates_to_a_pinned_critical_with_the_bundle(cfg, tmp_path):
    # #1020 B3 + C4: supervisor record says the restart budget is spent,
    # so the outage notice is the critical one, carries the bundle as a
    # MEDIA attachment, and gets pinned through the adapter.
    from print_handy import supervisor_state

    core = make_core(cfg, tmp_path, SendResult(True, message_id="77"),
                     [{"id": "p1", "chat_id": "424242"}])
    core.adapter = PinningAdapter(SendResult(True, message_id="77"))
    supervisor_state.write("p1", restarts=6, budget=5, window_s=600,
                           exhausted=True, data=core.cfg)
    core._listener_statuses = lambda: DOWN
    seed_state(core, old_outage())
    messages = run(core.check_listener_outages())
    assert [kind for _p, kind, _t in messages] == ["critical"]
    assert len(core.adapter.calls) == 1
    chat, content = core.adapter.calls[0]
    assert chat == "424242"
    assert "CRITICAL" in content and "exhausted its restart budget" in content
    assert "MEDIA:" in content and "diagnostics-bundle.md" in content
    assert core.adapter.pinned == [("424242", "77")]
    state = outage_alert.load(settings.state_dir(core.cfg))
    assert "critical_at" in state["p1"]


def test_degraded_notice_from_a_direct_poll_posts_once(cfg, tmp_path, monkeypatch):
    # #1020 B4: the listener is down (inside its grace period, so no
    # outage notice), and the printer itself reports a pause — the bot
    # posts the degraded notice from its own poll, once.
    core = make_core(cfg, tmp_path, SendResult(True, message_id="5"),
                     [{"id": "p1", "chat_id": "424242"}])
    core._listener_statuses = lambda: {"p1": {"required": True, "ok": False,
                                              "reason": "listener heartbeat stale or invalid"}}
    monkeypatch.setattr(runtime, "_poll_moonraker_state",
                        lambda entry: ("paused", "benchy.gcode"))
    run(core.check_listener_outages())
    degraded = [c for _chat, c in core.adapter.calls if "Degraded alert" in c]
    assert len(degraded) == 1
    assert "**paused**" in degraded[0] and "benchy.gcode" in degraded[0]
    run(core.check_listener_outages())
    degraded = [c for _chat, c in core.adapter.calls if "Degraded alert" in c]
    assert len(degraded) == 1, "the same reported state was noticed twice"
