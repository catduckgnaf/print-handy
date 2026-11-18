"""#1672 Part 2: each printer's last roast also goes to MQTT, with HA discovery.

The fake broker is a socket that records its bytes; the tests decode the
MQTT packets themselves, retain bit included. The Moonraker sink and the
MQTT sink are independent: either can fail or be off without the other.
"""
from __future__ import annotations

import json
import threading

import pytest

from print_handy import notify, settings
from print_handy.runtime import screen_tip

LINE = "Benchy has seen things you people would not believe."
FACTS = {"duration_s": 4500, "material": "PLA", "grams": 12.4}


def _drain():
    for t in list(threading.enumerate()):
        if t.name.startswith("screen-tip"):
            t.join(5)


class _Sock:
    def __init__(self):
        self.sent = b""

    def sendall(self, data):
        self.sent += data

    def recv(self, _n):
        return b"\x20\x02\x00\x00"  # CONNACK, accepted

    def close(self):
        pass


def _packets(raw):
    out, i = [], 0
    while i < len(raw):
        first, i = raw[i], i + 1
        mult, rem = 1, 0
        while True:
            byte, i = raw[i], i + 1
            rem += (byte & 127) * mult
            if not byte & 128:
                break
            mult *= 128
        out.append((first, raw[i:i + rem]))
        i += rem
    return out


def _publishes(sockets):
    """Every PUBLISH across the fake connections: (first byte, topic, payload)."""
    found = []
    for sock in sockets:
        for first, body in _packets(sock.sent):
            if first & 0xF0 == 0x30:
                tlen = int.from_bytes(body[:2], "big")
                found.append((first, body[2:2 + tlen].decode(),
                              body[2 + tlen:].decode()))
    return found


def _cfg(**notify_over):
    cfg = {"notify": {"mqtt_enabled": True, "mqtt_host": "broker.local",
                      "mqtt_port": 1883, "mqtt_topic": "print-handy",
                      "mqtt_username": "", "mqtt_roast": True},
           "vision": {"roast_enabled": True},
           "services": {"moonraker_url": ""}}
    cfg["notify"].update(notify_over)
    return cfg


@pytest.fixture
def broker(monkeypatch):
    """Settings with MQTT on, a recording fake broker, discovery state reset."""
    sockets = []

    def factory(host, port):
        sock = _Sock()
        sockets.append(sock)
        return sock

    real = notify.publish_retained
    monkeypatch.setattr(notify, "publish_retained",
                        lambda topic, payload: real(topic, payload, connector=factory))
    monkeypatch.setattr(settings, "current", lambda: _cfg())
    monkeypatch.setattr(screen_tip, "_moonraker_url", lambda printer_id: "")
    screen_tip._DISCOVERY_SENT.clear()
    yield sockets
    _drain()
    screen_tip._DISCOVERY_SENT.clear()


def _publish(**kw):
    return screen_tip.publish_roast(
        LINE, title="Benchy", status="completed", key="completed:benchy.gcode",
        facts=FACTS, printer_id="bambu1", **kw)


def test_one_roast_publishes_a_retained_state_with_the_exact_line(broker):
    assert _publish() is True
    _drain()
    states = [p for p in _publishes(broker) if p[1] == "print-handy/bambu1/last_roast"]
    (first, _topic, payload), = states
    assert first == 0x31  # PUBLISH with the retain flag set
    record = json.loads(payload)
    assert record["title"] == LINE
    assert record["printer_id"] == "bambu1"
    assert record["status"] == "completed"
    assert record["details"] == "Your last print, Benchy, finished. 1h 15m, PLA, 12 g."
    assert record["source"] == "Print Handy"
    assert record["id"] and isinstance(record["posted_at"], float)


def test_discovery_goes_out_once_per_printer_then_only_state(broker):
    _publish()
    _publish()
    _drain()
    pubs = _publishes(broker)
    discovery = [p for p in pubs if p[1].endswith("/config")]
    states = [p for p in pubs if p[1] == "print-handy/bambu1/last_roast"]
    assert len(discovery) == 1
    assert discovery[0][0] == 0x31  # retained, like the state
    assert discovery[0][1] == "homeassistant/sensor/print_handy_bambu1/last_roast/config"
    assert len(states) == 2


def test_discovery_config_matches_the_documented_shape():
    topic, config = screen_tip.discovery_config(_cfg()["notify"], "bambu1")
    assert topic == "homeassistant/sensor/print_handy_bambu1/last_roast/config"
    assert config["unique_id"] == "print_handy_bambu1_last_roast"
    assert config["state_topic"] == "print-handy/bambu1/last_roast"
    assert config["json_attributes_topic"] == "print-handy/bambu1/last_roast"
    # The 255-character state limit: the template truncates, the full
    # line stays in the title attribute.
    assert config["value_template"] == "{{ value_json.title[:255] }}"
    assert config["device"]["identifiers"] == ["print_handy_bambu1"]
    assert config["device"]["name"]


@pytest.mark.parametrize("over", [{"mqtt_enabled": False}, {"mqtt_roast": False}])
def test_off_switches_publish_nothing(broker, monkeypatch, over):
    monkeypatch.setattr(settings, "current", lambda: _cfg(**over))
    assert _publish() is False
    _drain()
    assert broker == []


def test_roasts_off_publishes_nothing(broker, monkeypatch):
    cfg = _cfg()
    cfg["vision"]["roast_enabled"] = False
    monkeypatch.setattr(settings, "current", lambda: cfg)
    assert _publish() is False
    _drain()
    assert broker == []


def test_a_refusing_broker_never_reaches_the_post(monkeypatch):
    monkeypatch.setattr(settings, "current", lambda: _cfg())

    def refuse(host, port):
        raise OSError("refused")

    real = notify.publish_retained
    monkeypatch.setattr(notify, "publish_retained",
                        lambda topic, payload: real(topic, payload, connector=refuse))
    sent = []
    monkeypatch.setattr(screen_tip, "_moonraker_url", lambda printer_id: "http://p:7125")
    monkeypatch.setattr(screen_tip, "_post", lambda url, value: sent.append((url, value)))
    screen_tip._DISCOVERY_SENT.clear()
    # The Moonraker sink still queues and delivers; the MQTT failure is
    # contained on its own thread.
    assert _publish() is True
    _drain()
    assert len(sent) == 1 and sent[0][1]["title"] == LINE
    screen_tip._DISCOVERY_SENT.clear()


def test_a_moonraker_printer_gets_both_sinks(broker, monkeypatch):
    sent = []
    monkeypatch.setattr(screen_tip, "_moonraker_url", lambda printer_id: "http://voron:7125")
    monkeypatch.setattr(screen_tip, "_post", lambda url, value: sent.append((url, value)))
    assert _publish() is True
    _drain()
    (url, value), = sent
    assert url == "http://voron:7125"
    # The HelixScreen record shape is unchanged: no MQTT-only keys leak in.
    assert set(value) == {"id", "title", "details", "source", "posted_at"}
    assert value["title"] == LINE
    states = [p for p in _publishes(broker) if p[1] == "print-handy/bambu1/last_roast"]
    assert len(states) == 1


def test_event_topics_stay_non_retained(monkeypatch):
    sock = _Sock()
    monkeypatch.setattr(notify, "_settings", lambda: _cfg()["notify"])
    assert notify.emit("complete", "plate 1 finished", printer_id="ad5x",
                       connector=lambda host, port: sock) is True
    pubs = _publishes([sock])
    (first, topic, payload), = pubs
    assert first == 0x30  # the event publish keeps its no-retain byte
    assert topic == "print-handy/complete"
    assert payload == "ad5x: plate 1 finished"
