"""Bambu Lab listener (#299 Stage B / #298 Stage 3): offline tests only — a fake MQTT
client and a fake clock, never a socket, never a real printer.

The disciplines pinned here are the #298 Stage 0 audit's corrected facts:
pushall on (re)connect with no periodic cadence, the ack-echo guard, merged
partial frames, 60 s staleness with a hard-reset reconnect (fresh client,
30 s cooldown), and a read-only session (only get_version / pushall are
ever published).
"""
import json

import pytest

from print_handy import healthcheck, printers, settings
from print_handy.printers.bambulab.client import BambuLanError
from print_handy.printers.bambulab.listener import (RECONNECT_COOLDOWN_S,
                                                    STALE_AFTER_S,
                                                    BambuListener)

SERIAL = "00M00A000000000"
CODE = "12345678"
KNOWN_HMS = {"attr": 0x03000200, "code": 0x00010008}  # as in test_bambu_hms.py


class FakeMessage:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()


class FakeMqtt:
    """Scripted paho-shaped stand-in. `script` items are delivered one per loop() call;
    `tick` (optional) runs at the top of each loop() so tests can advance a fake clock."""

    def __init__(self, script=(), connect_rc=0, tick=None, stop_on_empty=False):
        self.script = list(script)
        self.connect_rc = connect_rc
        self.tick = tick
        self.stop_on_empty = stop_on_empty
        self.published = []
        self.subscribed = []
        self.connected_to = None
        self.connect_at = None
        self.disconnected = False
        self.on_connect = None
        self.on_message = None

    def connect(self, host, port, keepalive=0):
        self.connected_to = (host, port, keepalive)
        self.on_connect(self, None, None, self.connect_rc)

    def subscribe(self, topic):
        self.subscribed.append(topic)

    def publish(self, topic, payload):
        self.published.append((topic, json.loads(payload)))
        return type("Result", (), {"rc": 0})()

    def loop(self, timeout=0):
        if self.tick:
            self.tick()
        if self.script:
            self.on_message(self, None, FakeMessage(self.script.pop(0)))
        elif self.stop_on_empty and self.on_empty is not None:
            self.on_empty()
        return 0

    def disconnect(self):
        self.disconnected = True

    on_empty = None


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def full(state="IDLE", **overrides):
    frame = {"command": "push_status", "msg": 0, "gcode_state": state,
             "mc_percent": 0, "mc_remaining_time": 0, "nozzle_temper": 24.0,
             "nozzle_target_temper": 0, "bed_temper": 23.0, "bed_target_temper": 0,
             "subtask_name": "", "layer_num": 0, "total_layer_num": 0,
             "fun": "3EC18FFF9CFF", "print_error": 0}
    frame.update(overrides)
    return {"print": frame}


def delta(**overrides):
    frame = {"command": "push_status", "msg": 1}
    frame.update(overrides)
    return {"print": frame}


VERSION = {"info": {"command": "get_version", "module": [{"name": "mc", "hw_ver": "N2S"}]}}


def lan_adapter(monkeypatch):
    monkeypatch.setenv("BAMBU_LAN_PASSWORD", CODE)
    cfg = settings.load(local=False, environ={
        "PRINT_HANDY__PRINTER__MODEL": "bambulab.a1",
        "PRINT_HANDY__BAMBU__LAN_HOST": "192.168.1.50",
        "PRINT_HANDY__BAMBU__LAN_SERIAL": SERIAL.lower()})
    return printers.get("bambulab.a1", cfg)


def make_listener(monkeypatch, fakes):
    clock = FakeClock()
    queue = list(fakes)
    beats = []
    listener = BambuListener(
        lan_adapter(monkeypatch),
        mqtt_factory=lambda host, serial, code: queue.pop(0),
        clock=clock, sleep=clock.sleep,
        beat=lambda **details: beats.append(details))
    for fake in fakes:
        fake.on_empty = listener.stop
    return listener, clock, beats


def kinds(listener):
    return [(event["kind"], event.get("transition")) for event in listener.events]


# --------------------------------------------------------------------- session discipline

def test_connect_requests_version_then_pushall_and_merges(monkeypatch):
    fake = FakeMqtt([VERSION, full("RUNNING", mc_percent=42, subtask_name="benchy.gcode.3mf",
                                   layer_num=12, total_layer_num=40)], stop_on_empty=True)
    listener, _clock, _beats = make_listener(monkeypatch, [fake])
    assert listener.run_forever() == 0
    assert fake.connected_to == ("192.168.1.50", 8883, 30)
    assert fake.subscribed == [f"device/{SERIAL}/report"]
    assert [body for _topic, body in fake.published] == [
        {"info": {"sequence_id": "0", "command": "get_version"}},
        {"pushing": {"sequence_id": "0", "command": "pushall"}}]
    assert listener._print["mc_percent"] == 42 and listener._complete is True
    assert fake.disconnected


def test_startup_line_names_the_version(monkeypatch, capsys):
    from print_handy import __version__
    fake = FakeMqtt([VERSION, full("IDLE")], stop_on_empty=True)
    listener, _clock, _beats = make_listener(monkeypatch, [fake])
    assert listener.run_forever() == 0
    out = capsys.readouterr().out
    assert f"bambu listener: listener starting (Print Handy {__version__})" in out


def test_ack_frames_are_not_telemetry(monkeypatch):
    fake = FakeMqtt([
        full("RUNNING", mc_percent=10, subtask_name="a.gcode.3mf", layer_num=1, total_layer_num=9),
        {"print": {"command": "project_file", "result": "success", "gcode_state": "IDLE"}},
        {"print": {"command": "print_option", "gcode_state": "PAUSE"}},
    ], stop_on_empty=True)
    listener, _clock, _beats = make_listener(monkeypatch, [fake])
    listener.run_forever()
    assert listener._print["gcode_state"] == "RUNNING"  # acks did not move state
    assert ("status", "paused") not in kinds(listener)
    assert listener._status["state"] == "printing"


def test_partial_frames_merge_into_persistent_state(monkeypatch):
    fake = FakeMqtt([
        delta(gcode_state="RUNNING", mc_percent=10, subtask_name="a.gcode.3mf",
              nozzle_temper=200.0, layer_num=3, total_layer_num=9),
        delta(mc_percent=25),
        delta(mc_percent=30),
    ], stop_on_empty=True)
    listener, _clock, _beats = make_listener(monkeypatch, [fake])
    listener.run_forever()
    status = listener._status
    assert status["progress"] == 0.30 and status["filename"] == "a.gcode.3mf"
    assert status["hotend_c"] == 200.0 and status["layer"] == 3


def test_access_denied_is_a_plain_error_and_nothing_is_published(monkeypatch):
    fake = FakeMqtt(connect_rc=5)
    listener, _clock, _beats = make_listener(monkeypatch, [fake])
    with pytest.raises(BambuLanError, match="rejected the LAN access code"):
        listener._session()
    assert fake.published == [] and CODE not in "rejected the LAN access code"


def test_stale_session_hard_resets_with_cooldown_and_a_fresh_client(monkeypatch):
    clock_holder = {}

    def tick():
        clock_holder["clock"].t += 10.0

    fake1 = FakeMqtt([full("IDLE")], tick=tick)
    fake2 = FakeMqtt([full("IDLE")], stop_on_empty=True)
    listener, clock, _beats = make_listener(monkeypatch, [fake1, fake2])
    clock_holder["clock"] = clock
    connect_times = []
    for fake in (fake1, fake2):
        original = fake.connect

        def recording_connect(host, port, keepalive=0, _fake=fake, _orig=original):
            connect_times.append(clock.t)
            return _orig(host, port, keepalive)

        fake.connect = recording_connect
    listener.run_forever()
    assert fake1.disconnected and fake1 is not fake2
    assert len(connect_times) == 2
    assert connect_times[1] - connect_times[0] >= STALE_AFTER_S + RECONNECT_COOLDOWN_S - 10
    # The fresh client re-runs the full connect discipline: version, then pushall.
    assert [body for _t, body in fake2.published] == [
        {"info": {"sequence_id": "0", "command": "get_version"}},
        {"pushing": {"sequence_id": "0", "command": "pushall"}}]


def test_layer1_pushall_once_when_total_unknown_and_never_periodically(monkeypatch):
    frame = full("RUNNING", mc_percent=1, subtask_name="a.gcode.3mf", layer_num=1)
    del frame["print"]["total_layer_num"]
    fake = FakeMqtt([frame, dict(frame), dict(frame)], stop_on_empty=True)
    listener, _clock, _beats = make_listener(monkeypatch, [fake])
    listener.run_forever()
    pushalls = [b for _t, b in fake.published if b.get("pushing", {}).get("command") == "pushall"]
    assert len(pushalls) == 2  # the connect pushall + the single layer-1 refresh
    assert all(set(body) <= {"info", "pushing"} for _t, body in fake.published)  # read-only


# --------------------------------------------------------------------- events

def test_transitions_emit_contract_events_deduped(monkeypatch):
    fake = FakeMqtt([
        full("RUNNING", subtask_name="a.gcode.3mf", layer_num=1, total_layer_num=9),
        full("RUNNING", subtask_name="a.gcode.3mf", layer_num=1, total_layer_num=9),
        delta(gcode_state="PAUSE"),
        delta(gcode_state="RUNNING"),
        delta(gcode_state="FINISH", mc_percent=100),
        delta(gcode_state="FINISH", mc_percent=100),
        full("RUNNING", subtask_name="b.gcode.3mf", layer_num=1, total_layer_num=4),
        delta(gcode_state="IDLE"),
    ], stop_on_empty=True)
    listener, _clock, _beats = make_listener(monkeypatch, [fake])
    listener.run_forever()
    assert kinds(listener) == [
        ("status", "started"), ("snapshot_available", None),
        ("status", "paused"), ("status", "resumed"),
        ("status", None), ("completion", "finished"),
        ("status", "started"), ("status", "cancelled"),
    ]


def test_error_event_carries_decoded_hms(monkeypatch):
    fake = FakeMqtt([
        full("RUNNING", subtask_name="a.gcode.3mf", layer_num=1, total_layer_num=9),
        delta(gcode_state="FAILED", hms=[KNOWN_HMS]),
        delta(gcode_state="FAILED", hms=[KNOWN_HMS]),
    ], stop_on_empty=True)
    listener, _clock, _beats = make_listener(monkeypatch, [fake])
    listener.run_forever()
    errors = [event for event in listener.events if event["kind"] == "error"]
    assert len(errors) == 1  # repeated FAILED frame does not re-emit
    assert errors[0]["hms_errors"] and errors[0]["hms_errors"][0]["code"]


def test_status_events_validate_against_the_contract(monkeypatch):
    from print_handy import listeners
    fake = FakeMqtt([full("RUNNING", subtask_name="a.gcode.3mf", mc_percent=5,
                          layer_num=2, total_layer_num=9)], stop_on_empty=True)
    listener, _clock, _beats = make_listener(monkeypatch, [fake])
    listener.run_forever()
    assert listener.events  # at least the started status event
    for event in listener.events:
        assert listeners.validate_status(event["status"]) == []


def test_telemetry_only_changes_emit_updated_status(monkeypatch):
    # P2-117: a delta that changes only progress / temperatures on the same
    # layer must still reach the sink; deduping on (state, filename, layer)
    # alone left the sink holding the first frame's 10% and 20 C forever.
    fake = FakeMqtt([
        full("RUNNING", subtask_name="a.gcode.3mf", layer_num=1, total_layer_num=9,
             mc_percent=10, nozzle_temper=20.0),
        delta(mc_percent=20, nozzle_temper=200.0),
        delta(mc_percent=20, nozzle_temper=200.0),
    ], stop_on_empty=True)
    listener, _clock, _beats = make_listener(monkeypatch, [fake])
    listener.run_forever()
    statuses = [event for event in listener.events if event["kind"] == "status"]
    assert [event["status"]["progress"] for event in statuses] == [0.1, 0.2]
    assert statuses[-1]["status"]["hotend_c"] == 200.0


def test_session_with_no_report_at_all_goes_stale(monkeypatch):
    # P2-116: the staleness deadline starts at connect, not at the first
    # report — a session the printer never talks to must still be recycled.
    clock_holder = {}
    holder = {}

    def tick():
        clock = clock_holder["clock"]
        clock.t += 10.0
        if clock.t > 400 and "listener" in holder:
            holder["listener"].stop()

    fake1 = FakeMqtt([], tick=tick)  # connects and stays silent forever
    fake2 = FakeMqtt([full("IDLE")], stop_on_empty=True)
    listener, clock, _beats = make_listener(monkeypatch, [fake1, fake2])
    clock_holder["clock"] = clock
    holder["listener"] = listener
    listener.run_forever()
    assert fake1.disconnected  # the silent session was hard-reset...
    assert fake2.disconnected  # ...and the listener moved on to a fresh client


def test_debug_mode_is_polled_during_an_active_session(monkeypatch):
    # P2-187: debug-mode start/stop must be observed while a healthy session
    # runs, not only between sessions.
    from print_handy import debug_mode
    clock_holder = {}
    polls = []

    def tick():
        clock_holder["clock"].t += 10.0

    fake = FakeMqtt([full("IDLE"), full("IDLE"), full("IDLE"), full("IDLE")],
                    tick=tick, stop_on_empty=True)
    listener, clock, _beats = make_listener(monkeypatch, [fake])
    clock_holder["clock"] = clock
    monkeypatch.setattr(debug_mode, "poll",
                        lambda: polls.append(fake.disconnected))
    listener.run_forever()
    assert polls and any(seen is False for seen in polls)  # polled mid-session


def test_idle_observations_are_forwarded_while_terminal(monkeypatch):
    # P1-107: after a print finishes, unchanged idle reports are deduped
    # away, so the sink's light idle_tick starved. The listener forwards a
    # periodic idle observation while the state stays terminal.
    clock_holder = {}
    holder = {}

    def tick():
        clock = clock_holder["clock"]
        clock.t += 10.0
        if clock.t >= 150 and "listener" in holder:
            holder["listener"].stop()

    fake = FakeMqtt([
        full("RUNNING", subtask_name="a.gcode.3mf", layer_num=1, total_layer_num=9),
        delta(gcode_state="FINISH", mc_percent=100),
    ], tick=tick)
    fresh = FakeMqtt([full("IDLE")], stop_on_empty=True)  # the post-staleness session
    listener, clock, _beats = make_listener(monkeypatch, [fake, fresh])
    clock_holder["clock"] = clock
    holder["listener"] = listener
    listener.run_forever()
    idle = [event for event in listener.events
            if event["kind"] == "status" and event["status"]["state"] == "complete"
            and event.get("transition") is None]
    assert len(idle) >= 3  # the finish observation plus periodic re-forwards


# --------------------------------------------------------------------- liveness / configuration

def test_unconfigured_listener_idles_beats_and_never_touches_the_network(monkeypatch):
    cfg = settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": "bambulab.a1"})
    adapter = printers.get("bambulab.a1", cfg)
    beats = []
    calls = {"sleeps": 0}

    def factory(*_args):
        raise AssertionError("an unconfigured listener must not build an MQTT client")

    def sleep(seconds):
        calls["sleeps"] += 1
        if calls["sleeps"] >= 3:
            listener.stop()

    listener = BambuListener(adapter, mqtt_factory=factory, sleep=sleep,
                             beat=lambda **details: beats.append(details))
    assert listener.active is False
    assert listener.run_forever() == 0
    assert beats and all(b["printer_connected"] is False for b in beats)


def test_heartbeat_uses_the_bambu_runtime_and_satisfies_the_healthcheck(monkeypatch, tmp_path):
    path = tmp_path / "listener_health.json"
    monkeypatch.setattr(healthcheck, "heartbeat_path",
                        lambda data=None, printer_id=None: path)
    listener, _clock, _beats = make_listener(monkeypatch, [FakeMqtt()])
    listener._beat_fn = lambda **details: healthcheck.beat(runtime="bambu_event_listener",
                                                            **details)
    listener._beat_now()
    record = json.loads(path.read_text())
    assert record["runtime"] == "bambu_event_listener"
    ok, _reason = healthcheck.check({"printer": {"model": "bambulab.a1"}})
    assert ok


# --------------------------------------------------------------------- main() entry point (#409)

class _FakeMainListener:
    """Stand-in for BambuListener in main() tests: records, never connects."""
    built = []

    def __init__(self, adapter, printer_id=None, **kwargs):
        type(self).built.append((adapter, printer_id))
        self.adapter = adapter

    def run_forever(self):
        return 0


def _call_main(monkeypatch, adapter):
    import types

    from print_handy import logs
    from print_handy.printers.bambulab import listener as listener_module

    _FakeMainListener.built = []
    monkeypatch.setattr(logs, "configure", lambda *args, **kwargs: None)
    monkeypatch.setattr(printers, "current", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(listener_module, "BambuListener", _FakeMainListener)
    return listener_module.main()


def test_main_returns_3_for_a_non_bambu_printer(monkeypatch):
    # The shape main() used to crash on in #409: no usable vendor at all.
    # It must report the connection and return 3, not raise ImportError
    # (the old ``from .. import``) or AttributeError (``adapter.vendor``).
    import types
    adapter = types.SimpleNamespace(vendor=None, connection="moonraker", transport=None)
    assert _call_main(monkeypatch, adapter) == 3
    assert _FakeMainListener.built == []


def test_main_returns_3_for_a_real_non_bambu_adapter(monkeypatch):
    cfg = settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": "flashforge.ad5x"})
    adapter = printers.get("flashforge.ad5x", cfg)
    assert _call_main(monkeypatch, adapter) == 3
    assert _FakeMainListener.built == []


def test_main_starts_when_vendor_is_none_but_transport_is_bambulab_lan(monkeypatch):
    # The exact 2.1.5 container state in #409: vendor is None, the
    # adapter still reports transport="bambulab_lan", and the verified
    # fix starts the listener instead of dying on ``.vendor.connection``.
    import types
    adapter = types.SimpleNamespace(vendor=None, connection=None, transport="bambulab_lan")
    assert _call_main(monkeypatch, adapter) == 0
    assert _FakeMainListener.built and _FakeMainListener.built[0][0] is adapter


def test_main_starts_for_a_real_bambu_adapter(monkeypatch):
    adapter = lan_adapter(monkeypatch)
    assert _call_main(monkeypatch, adapter) == 0
    assert _FakeMainListener.built and _FakeMainListener.built[0][0] is adapter


# --- main(): the entry point `python -m print_handy.listeners --printer <id>` runs through runpy (#409, from #420) ---

class _Adapter:
    def __init__(self, transport, vendor=None):
        self.transport = transport
        self.vendor = vendor


def test_main_refuses_a_non_bambu_printer_through_the_adapter_transport(monkeypatch):
    # #409: `from .. import logs, printers` raised ImportError (one package too low) and the
    # vendor check read adapter.vendor.connection on an adapter whose vendor is None.
    from print_handy.printers.bambulab import listener as listener_mod
    monkeypatch.setattr(printers, "current", lambda: _Adapter("moonraker", vendor=None))
    assert listener_mod.main() == 3


def test_main_builds_the_listener_for_a_bambu_lan_adapter(monkeypatch):
    from print_handy.printers.bambulab import listener as listener_mod
    seen = {}

    class FakeListener:
        def __init__(self, adapter, *, printer_id=None, **kw):
            seen["adapter"] = adapter
            seen["printer_id"] = printer_id

        def run_forever(self):
            return 7

    adapter = _Adapter("bambulab_lan", vendor=None)
    monkeypatch.setattr(printers, "current", lambda: adapter)
    monkeypatch.setattr(listener_mod, "BambuListener", FakeListener)
    assert listener_mod.main() == 7
    assert seen["adapter"] is adapter and seen["printer_id"] is None
