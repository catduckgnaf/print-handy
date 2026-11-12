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
import io
import json
import time
import urllib.error
import urllib.parse

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
    cfg["paths"]["state_home"] = tmp_path / "hermes-home"
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


class _Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class BotApiRecorder:
    """Telegram Bot API stand-in: records (method, JSON body), answers ok."""

    def __init__(self):
        self.calls = []

    def __call__(self, req, timeout=None):
        method = req.full_url.rsplit("/", 1)[-1]
        is_json = "json" in (req.headers.get("Content-type") or "")
        self.calls.append((method, json.loads(req.data or b"{}") if is_json else {}))
        return _Reply(json.dumps({"ok": True, "result": {"message_id": 88}}).encode())


class MatrixStateRecorder:
    """Matrix Client-Server stand-in for the pinned-events state call."""

    def __init__(self, pinned=None):
        self.pinned = pinned  # None: the room has no pins yet (404)
        self.calls = []

    def __call__(self, req, timeout=None):
        method = req.get_method()
        path = urllib.parse.unquote(urllib.parse.urlsplit(req.full_url).path)
        is_json = "json" in (req.headers.get("Content-type") or "")
        body = json.loads(req.data) if req.data and is_json else None
        self.calls.append((method, path, body))
        if "/upload" in path:  # the attached diagnostics bundle
            return _Reply(b'{"content_uri": "mxc://example.org/bundle"}')
        if method == "GET":
            if self.pinned is None:
                raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)
            return _Reply(json.dumps({"pinned": self.pinned}).encode())
        if path.endswith("/state/m.room.pinned_events"):
            self.pinned = list(body["pinned"])
            return _Reply(b'{"event_id": "$state"}')
        # The notice text is the first event sent; the bundle file follows it.
        sends = [c for c in self.calls if "/send/" in c[1]]
        return _Reply(b'{"event_id": "$sent"}' if len(sends) == 1 else b'{"event_id": "$file"}')


def test_1332_telegram_pins_the_critical_outage_notice(cfg, tmp_path):
    # #1332: before, only Discord had pin_message, so a spent restart budget
    # on a Telegram install posted the CRITICAL notice unpinned.
    from print_handy import supervisor_state
    from print_handy.bot import telegram

    core = make_core(cfg, tmp_path, SendResult(True, message_id="1"), [{"id": "p1", "chat_id": "-100777"}])
    api = BotApiRecorder()
    core.adapter = telegram.StandaloneAdapter(None, ["424242"], token="123456789:AA" + "x" * 33, opener=api)
    supervisor_state.write("p1", restarts=6, budget=5, window_s=600, exhausted=True, data=core.cfg)
    core._listener_statuses = lambda: DOWN
    seed_state(core, old_outage())
    messages = run(core.check_listener_outages())
    assert [kind for _p, kind, _t in messages] == ["critical"]
    pins = [body for method, body in api.calls if method == "pinChatMessage"]
    assert pins == [{"chat_id": "-100777", "message_id": 88, "disable_notification": False}]


def test_1332_matrix_pins_the_critical_outage_notice(cfg, tmp_path):
    from print_handy import supervisor_state
    from print_handy.bot import matrix

    core = make_core(cfg, tmp_path, SendResult(True, message_id="1"), [{"id": "p1", "chat_id": "!room:example.org"}])
    hs = MatrixStateRecorder(pinned=["$older"])
    core.adapter = matrix.StandaloneAdapter(None, ["@op:example.org"], homeserver="https://matrix.example.org",
                                            token="syt_test", opener=hs)
    supervisor_state.write("p1", restarts=6, budget=5, window_s=600, exhausted=True, data=core.cfg)
    core._listener_statuses = lambda: DOWN
    seed_state(core, old_outage())
    messages = run(core.check_listener_outages())
    assert [kind for _p, kind, _t in messages] == ["critical"]
    # The existing pin is kept and the notice's event is appended.
    assert hs.pinned == ["$older", "$sent"]


def test_1332_matrix_pin_event_handles_a_room_without_pins_and_repeats():
    from print_handy.bot import matrix_rest

    hs = MatrixStateRecorder(pinned=None)
    matrix_rest.pin_event("https://matrix.example.org", "syt_test", "!r:example.org", "$e1", opener=hs)
    assert hs.pinned == ["$e1"]
    puts = len([c for c in hs.calls if c[0] == "PUT"])
    matrix_rest.pin_event("https://matrix.example.org", "syt_test", "!r:example.org", "$e1", opener=hs)
    assert len([c for c in hs.calls if c[0] == "PUT"]) == puts, "an already pinned event was written again"


def test_1332_a_refused_pin_keeps_the_delivered_critical(cfg, tmp_path):
    # A group bot without the pin right: the Bot API refuses; the notice
    # stays delivered and recorded, and nothing raises into the heartbeat.
    from print_handy import supervisor_state
    from print_handy.bot import telegram

    class Refusing(BotApiRecorder):
        def __call__(self, req, timeout=None):
            if req.full_url.endswith("/pinChatMessage"):
                self.calls.append(("pinChatMessage", {}))
                return _Reply(json.dumps({"ok": False, "description": "not enough rights"}).encode())
            return super().__call__(req, timeout)

    core = make_core(cfg, tmp_path, SendResult(True, message_id="1"), [{"id": "p1", "chat_id": "-100777"}])
    api = Refusing()
    core.adapter = telegram.StandaloneAdapter(None, ["424242"], token="123456789:AA" + "x" * 33, opener=api)
    supervisor_state.write("p1", restarts=6, budget=5, window_s=600, exhausted=True, data=core.cfg)
    core._listener_statuses = lambda: DOWN
    seed_state(core, old_outage())
    messages = run(core.check_listener_outages())
    assert [kind for _p, kind, _t in messages] == ["critical"]
    assert "pinChatMessage" in [m for m, _b in api.calls]
    assert "critical_at" in outage_alert.load(settings.state_dir(core.cfg))["p1"]


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
    assert "**paused**" in degraded[0] and "benchy" in degraded[0]
    run(core.check_listener_outages())
    degraded = [c for _chat, c in core.adapter.calls if "Degraded alert" in c]
    assert len(degraded) == 1, "the same reported state was noticed twice"


def test_a_restarting_listener_posts_the_degraded_notice_once(cfg, tmp_path, monkeypatch):
    # Seen live: a listener restarting every few minutes was healthy for a
    # moment between failures, the bot forgot the notice it had sent, and
    # the same "listener is down" notice went out five times, naming the
    # raw remote path with its content hash.
    core = make_core(cfg, tmp_path, SendResult(True, message_id="5"),
                     [{"id": "p1", "chat_id": "424242"}])
    remote = ("3mf_print/Remote-Holder_plate_1_"
              "5869735638d19cbd24e19e141476903f94d919f2b4f401dfe5e3b72af8b18965.gcode")
    monkeypatch.setattr(runtime, "_poll_moonraker_state", lambda entry: ("paused", remote))
    for statuses in (DOWN, UP, DOWN, UP, DOWN):
        core._listener_statuses = lambda statuses=statuses: statuses
        run(core.check_listener_outages())
    degraded = [c for _chat, c in core.adapter.calls if "Degraded alert" in c]
    assert len(degraded) == 1, degraded
    assert "on Remote-Holder plate 1." in degraded[0]
    assert "3mf_print/" not in degraded[0] and ".gcode" not in degraded[0]


def test_degraded_notices_are_forgotten_after_the_recovery_hold():
    state = {}
    assert outage_alert.degraded_notice(state, "p1", "paused", filename="a.gcode", now=0.0)
    assert outage_alert.degraded_recovered(state, "p1", now=100.0)
    assert outage_alert.degraded_notice(state, "p1", "paused", filename="a.gcode", now=150.0) is None
    outage_alert.degraded_recovered(state, "p1", now=100.0 + outage_alert.DEGRADED_RECOVERY_HOLD_S)
    assert "p1" not in state
    assert outage_alert.degraded_notice(state, "p1", "paused", filename="a.gcode", now=900.0)
