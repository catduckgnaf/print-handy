"""#622: MQTT is off by default, and a publish carries the event. Failures do not raise."""
from print_handy.notify import emit, event_for


class _Sock:
    def __init__(self):
        self.sent = b""

    def sendall(self, data):
        self.sent += data

    def close(self):
        pass


def test_events_map_and_stay_off_without_a_host(monkeypatch):
    assert event_for("started") == "print_started"
    assert event_for("paused", "filament runout on slot 2") == "runout"
    assert event_for("complete") == "complete"
    assert event_for("slice") == ""
    monkeypatch.setattr("print_handy.notify._settings", lambda: {"mqtt_enabled": False, "mqtt_host": "broker"})
    assert emit("print_started", "duck", connector=lambda *_: (_ for _ in ()).throw(AssertionError("connected"))) is False


def test_enabled_publish_names_the_topic_and_a_refused_broker_does_not_raise(monkeypatch):
    sock = _Sock()
    monkeypatch.setattr("print_handy.notify._settings", lambda: {
        "mqtt_enabled": True, "mqtt_host": "broker.local", "mqtt_port": 1883, "mqtt_topic": "print-handy",
    })
    assert emit("complete", "plate 1 finished", printer_id="ad5x", connector=lambda host, port: sock) is True
    assert b"print-handy/complete" in sock.sent
    assert b"ad5x: plate 1 finished" in sock.sent

    def refuse(host, port):
        raise OSError("refused")

    assert emit("failed", "stopped", connector=refuse) is False


def test_password_is_env_only_and_absent_from_settings_and_diagnostics(monkeypatch, tmp_path):
    """The token is read from the environment. It is not a settings key and not in a bundle."""
    import yaml
    from print_handy.diagnostics_bundle import build_bundle
    from print_handy.settings import OPTIONAL_KEYS, SettingsError, _check_keys

    secret = "mqtt-secret-not-for-yaml-9f3c"
    monkeypatch.setenv("MQTT_PASSWORD", secret)
    assert ("notify", "mqtt_password") not in OPTIONAL_KEYS
    notify = {"mqtt_enabled": True, "mqtt_host": "broker.local", "mqtt_port": 1883,
              "mqtt_topic": "print-handy", "mqtt_username": "ha"}
    written = tmp_path / "home.yaml"
    written.write_text(yaml.safe_dump({"notify": notify}), encoding="utf-8")
    assert secret not in written.read_text(encoding="utf-8")
    try:
        _check_keys({}, {"notify": {**notify, "mqtt_password": secret}}, "home.yaml")
    except SettingsError as exc:
        assert "notify" in str(exc)
    else:
        raise AssertionError("a password key was accepted as a setting")

    sock = _Sock()
    monkeypatch.setattr("print_handy.notify._settings", lambda: dict(notify))
    assert emit("paused", "Print paused: duck", connector=lambda host, port: sock) is True
    assert secret.encode() in sock.sent
    assert secret not in written.read_text(encoding="utf-8")

    bundle = build_bundle({
        "notify": notify,
        "logging": {},
        "printers": [],
        "paths": {"state_home": tmp_path},
    }, include_doctor=False)
    assert secret not in bundle
    leaky = build_bundle({
        "notify": {**notify, "mqtt_password": secret},
        "logging": {},
        "printers": [],
        "paths": {"state_home": tmp_path},
    }, include_doctor=False)
    assert secret not in leaky


def test_recorded_pause_notifies_once_and_the_chat_post_still_returns(monkeypatch):
    import print_handy.runtime.discord_print_summary as summary
    monkeypatch.setattr(summary, "find_job", lambda *args, **kwargs: (None, None))
    calls = []

    def capture(kind, text, **kwargs):
        calls.append((kind, text))
        return True

    # format_event dispatches through the notify queue (P2-249); capturing
    # emit_queued is the seam where the queued event is visible.
    monkeypatch.setattr("print_handy.notify.emit_queued", capture)
    message = summary.format_event("paused", "plate.gcode")
    assert message.startswith("Print paused")
    assert calls == [("paused", message)]

    def refuse(*args, **kwargs):
        raise OSError("refused")

    monkeypatch.setattr("print_handy.notify.emit_queued", refuse)
    again = summary.format_event("paused", "plate.gcode")
    assert again.startswith("Print paused")


def test_name_lookup_never_publishes_and_a_publish_never_blocks_the_caller(monkeypatch):
    import threading
    import time
    import print_handy.runtime.discord_print_summary as summary
    monkeypatch.setattr(summary, "find_job", lambda *args, **kwargs: (None, None))
    gate = threading.Event()
    calls = []

    def slow(kind, text, **kwargs):
        gate.wait(5)
        calls.append(kind)
        return True

    monkeypatch.setattr("print_handy.notify.emit", slow)
    summary.format_event("started", "plate.gcode", emit=False)
    summary.format_event("started", "plate.gcode")  # returns while the publish is still waiting
    assert calls == []
    gate.set()
    # Wait for the publish itself, not for every daemon thread in the process:
    # other tests' long-lived threads would each cost a full join timeout.
    deadline = time.time() + 5
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)  # a second publish (the emit=False call) would land by now
    assert calls == ["print_started"]


def test_emit_queued_delivers_in_order_on_the_worker(monkeypatch):
    import time
    from print_handy import notify
    delivered = []
    monkeypatch.setattr(notify, "emit", lambda kind, text, **kw: delivered.append(kind) or True)
    assert notify.emit_queued("not-an-event", "x") is False
    assert notify.emit_queued("paused", "first") is True
    assert notify.emit_queued("complete", "second") is True
    deadline = time.time() + 5
    while delivered != ["paused", "complete"] and time.time() < deadline:
        time.sleep(0.01)
    assert delivered == ["paused", "complete"]


def test_emit_queued_returns_before_a_slow_delivery_finishes(monkeypatch):
    # The point of the queue (P2-249): broker I/O (a 3 s connect, a 5 s
    # HTTP POST) never runs on the caller's path.
    import threading
    import time
    from print_handy import notify
    gate = threading.Event()
    done = []

    def slow(kind, text, **kwargs):
        gate.wait(5)
        done.append(kind)
        return True

    monkeypatch.setattr(notify, "emit", slow)
    assert notify.emit_queued("failed", "boom") is True
    assert done == []  # the caller returned while delivery is still blocked
    gate.set()
    deadline = time.time() + 5
    while done != ["failed"] and time.time() < deadline:
        time.sleep(0.01)
    assert done == ["failed"]


def test_connack_refusal_is_reported_and_client_ids_differ(monkeypatch):
    class Broker(_Sock):
        def __init__(self, reply):
            super().__init__()
            self.reply = reply

        def recv(self, n):
            return self.reply

    monkeypatch.setattr("print_handy.notify._settings", lambda: {
        "mqtt_enabled": True, "mqtt_host": "broker.local", "mqtt_topic": "print-handy"})
    ok, refused, silent = Broker(b"\x20\x02\x00\x00"), Broker(b"\x20\x02\x00\x05"), Broker(b"")
    assert emit("complete", "done", connector=lambda *_: ok) is True
    assert emit("complete", "done", connector=lambda *_: refused) is False
    assert emit("complete", "done", connector=lambda *_: silent) is False
    other = Broker(b"\x20\x02\x00\x00")
    emit("complete", "done", connector=lambda *_: other)
    assert ok.sent[:30] != other.sent[:30]


# --------------------------------------------------------------------- ntfy (#622, 2.2.2)

class _Response:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Http:
    """The ntfy test seam: records requests, answers with a fixed status."""

    def __init__(self, status=200):
        self.status = status
        self.requests = []

    def __call__(self, request, timeout=0):
        self.requests.append(request)
        return _Response(self.status)


def _ntfy_settings(monkeypatch, **overrides):
    cfg = {"ntfy_enabled": True, "ntfy_url": "https://ntfy.example", "ntfy_topic": "prints"}
    cfg.update(overrides)
    monkeypatch.setattr("print_handy.notify._settings", lambda: cfg)
    return cfg


def test_ntfy_publish_posts_the_event_to_the_topic_url(monkeypatch):
    _ntfy_settings(monkeypatch)
    http = _Http()
    assert emit("complete", "plate 1 finished", printer_id="ad5x", http_opener=http) is True
    (request,) = http.requests
    assert request.full_url == "https://ntfy.example/prints"
    assert request.data == b"ad5x: plate 1 finished"
    assert request.headers["Title"] == "Print Handy: complete"


def test_ntfy_url_may_name_the_topic_itself(monkeypatch):
    _ntfy_settings(monkeypatch, ntfy_url="https://ntfy.example/prints", ntfy_topic="")
    http = _Http()
    assert emit("failed", "stopped", http_opener=http) is True
    assert http.requests[0].full_url == "https://ntfy.example/prints"
    # A topic equal to the URL's last segment is not doubled.
    _ntfy_settings(monkeypatch, ntfy_url="https://ntfy.example/prints", ntfy_topic="prints")
    http = _Http()
    assert emit("failed", "stopped", http_opener=http) is True
    assert http.requests[0].full_url == "https://ntfy.example/prints"


def test_ntfy_is_off_by_default_and_a_refusal_does_not_raise(monkeypatch):
    monkeypatch.setattr("print_handy.notify._settings", lambda: {"ntfy_enabled": False})
    assert emit("complete", "done", http_opener=lambda *_: (_ for _ in ()).throw(
        AssertionError("opened"))) is False
    _ntfy_settings(monkeypatch)
    assert emit("complete", "done", http_opener=_Http(status=403)) is False

    def refuse(request, timeout=0):
        raise OSError("refused")

    assert emit("complete", "done", http_opener=refuse) is False
    # Enabled but nowhere to publish: no topic in the URL or the setting.
    _ntfy_settings(monkeypatch, ntfy_url="", ntfy_topic="")
    assert emit("complete", "done", http_opener=_Http()) is False


def test_ntfy_token_is_env_only(monkeypatch):
    _ntfy_settings(monkeypatch)
    monkeypatch.setenv("NTFY_ACCESS_TOKEN", "tk_test_token")
    http = _Http()
    assert emit("complete", "done", http_opener=http) is True
    assert http.requests[0].headers["Authorization"] == "Bearer tk_test_token"
    from print_handy.settings import OPTIONAL_KEYS
    assert ("notify", "ntfy_token") not in OPTIONAL_KEYS
    assert ("notify", "ntfy_access_token") not in OPTIONAL_KEYS


def test_one_channels_failure_never_blocks_the_other(monkeypatch):
    monkeypatch.setattr("print_handy.notify._settings", lambda: {
        "mqtt_enabled": True, "mqtt_host": "broker.local", "mqtt_topic": "print-handy",
        "ntfy_enabled": True, "ntfy_url": "https://ntfy.example", "ntfy_topic": "prints"})

    def refuse(host, port):
        raise OSError("refused")

    # MQTT down, ntfy up: delivered.
    assert emit("complete", "done", connector=refuse, http_opener=_Http()) is True
    # ntfy down, MQTT up: delivered.
    sock = _Sock()
    assert emit("complete", "done", connector=lambda host, port: sock,
                http_opener=_Http(status=500)) is True
    # Both down: not delivered, and nothing raised either way.
    assert emit("complete", "done", connector=refuse, http_opener=_Http(status=500)) is False
