"""Bambu Lab LAN telemetry (#187 stage b): offline tests only — a fake MQTT client, never a socket.

The fake implements the small paho-shaped surface client.py uses (on_connect / on_message
callbacks, connect / subscribe / publish / loop / disconnect). Payloads follow the shapes the
maintained integrations record (docs/BAMBU-CONNECTIVITY.md): delta `print` pushes, a complete
pushall marked msg == 0, and the get_version reply.
"""
import io
import json
import ssl

import pytest

from print_handy import printers, settings
from print_handy.core import printer_state, spoolman_client
from print_handy.printers.bambulab import adapter as bambu_adapter
from print_handy.printers.bambulab.client import (BambuLanClient, BambuLanError, lan_ssl_context,
                                                  merge_print, signature_required)
from print_handy.setup_wizard import Wizard, build_plan

SERIAL = "00M00A000000000"
CODE = "12345678"


class FakeMessage:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()


class FakeMqtt:
    """Scripted stand-in for the paho client. `script` items are delivered one per loop() call."""

    def __init__(self, script=(), connect_rc=0, connect_error=None):
        self.script = list(script)
        self.connect_rc = connect_rc
        self.connect_error = connect_error
        self.published = []
        self.subscribed = []
        self.connected_to = None
        self.disconnected = False
        self.on_connect = None
        self.on_message = None

    def connect(self, host, port, keepalive=0):
        if self.connect_error is not None:
            raise self.connect_error
        self.connected_to = (host, port, keepalive)
        self.on_connect(self, None, None, self.connect_rc)

    def subscribe(self, topic):
        self.subscribed.append(topic)

    def publish(self, topic, payload):
        self.published.append((topic, json.loads(payload)))
        return type("Result", (), {"rc": 0})()

    def loop(self, timeout=0):
        if self.script:
            self.on_message(self, None, FakeMessage(self.script.pop(0)))
        return 0

    def disconnect(self):
        self.disconnected = True


def factory_for(fake):
    return lambda host, serial, access_code: fake


def complete_snapshot_parts():
    version = {"info": {"command": "get_version", "module": [
        {"name": "ota", "sw_ver": "01.05.00.00"}, {"name": "mc", "hw_ver": "N2S"}]}}
    delta = {"print": {"command": "push_status", "msg": 1, "gcode_state": "RUNNING", "mc_percent": 41}}
    full = {"print": {"command": "push_status", "msg": 0, "gcode_state": "RUNNING", "mc_percent": 42,
                      "mc_remaining_time": 87, "nozzle_temper": 210.5, "nozzle_target_temper": 210,
                      "bed_temper": 59.5, "bed_target_temper": 60, "subtask_name": "benchy.gcode.3mf",
                      "layer_num": 12, "total_layer_num": 40, "fun": "3EC18FFF9CFF", "print_error": 0}}
    return [delta, version, full]


# --------------------------------------------------------------------- client

def test_snapshot_merges_deltas_and_requests_version_then_pushall():
    fake = FakeMqtt(complete_snapshot_parts())
    client = BambuLanClient("192.168.1.50", SERIAL.lower(), CODE, mqtt_factory=factory_for(fake), timeout_s=5)
    snap = client.snapshot()
    assert fake.connected_to == ("192.168.1.50", 8883, 30)
    assert fake.subscribed == [f"device/{SERIAL}/report"]
    assert [body for _topic, body in fake.published] == [
        {"info": {"sequence_id": "0", "command": "get_version"}},
        {"pushing": {"sequence_id": "0", "command": "pushall"}}]
    assert all(topic == f"device/{SERIAL}/request" for topic, _body in fake.published)
    assert snap["print"]["mc_percent"] == 42 and snap["print"]["gcode_state"] == "RUNNING"  # delta merged, full wins
    assert snap["info"]["module"][0]["sw_ver"] == "01.05.00.00"
    assert snap["online"] is True and fake.disconnected


def test_snapshot_access_denied_names_the_remedy_and_never_the_code():
    fake = FakeMqtt(connect_rc=5)
    client = BambuLanClient("192.168.1.50", SERIAL, CODE, mqtt_factory=factory_for(fake), timeout_s=5)
    with pytest.raises(BambuLanError, match="rejected the LAN access code"):
        client.snapshot()
    try:
        client.snapshot()
    except BambuLanError as exc:
        assert CODE not in str(exc)


def test_snapshot_unreachable_and_timeout_are_plain_errors():
    fake = FakeMqtt(connect_error=OSError("no route to host"))
    client = BambuLanClient("192.168.1.50", SERIAL, CODE, mqtt_factory=factory_for(fake), timeout_s=5)
    with pytest.raises(BambuLanError, match="could not be reached"):
        client.snapshot()
    silent = FakeMqtt()  # connects, never answers
    client = BambuLanClient("192.168.1.50", SERIAL, CODE, mqtt_factory=factory_for(silent), timeout_s=0.2)
    with pytest.raises(BambuLanError, match="no complete status snapshot"):
        client.snapshot()


def test_snapshot_reads_use_distinct_client_ids_never_the_listener_id(monkeypatch):
    # P2-265: a status poll must not connect with the listener's MQTT client
    # ID (print-handy-<serial>) — MQTT disconnects the existing client when a
    # new connection reuses its ID, so every poll would kick the listener,
    # and two polls sharing an ID would kick each other. Each snapshot read
    # on the built-in factory path asks for a fresh, distinct ID.
    from print_handy.printers.bambulab import client as client_mod

    seen = []
    fakes = [FakeMqtt(complete_snapshot_parts()), FakeMqtt(complete_snapshot_parts())]

    def recording_factory(host, serial, access_code, client_id=None):
        seen.append(client_id)
        return fakes.pop(0)

    monkeypatch.setattr(client_mod, "_paho_factory", recording_factory)
    client = BambuLanClient("192.168.1.50", SERIAL, CODE, timeout_s=5)  # built-in factory path
    client.snapshot()
    client.snapshot()
    listener_id = f"print-handy-{SERIAL.lower()}"
    assert len(seen) == 2 and all(seen), seen
    assert seen[0] != seen[1], seen  # concurrent polls cannot disconnect each other
    assert all(cid != listener_id for cid in seen), seen  # never the listener's session ID
    assert all(cid.startswith(listener_id) for cid in seen), seen


def test_paho_factory_defaults_to_the_session_id_and_honors_an_explicit_one(monkeypatch):
    # The listener's persistent session keeps the fixed print-handy-<serial>
    # ID (the factory's default); only a caller asking for a distinct ID —
    # a snapshot read — gets one. paho is stubbed: no broker, no socket.
    import sys
    import types

    built = []

    class StubClient:
        def __init__(self, *args, **kwargs):
            built.append(kwargs)

        def username_pw_set(self, *args, **kwargs):
            pass

        def tls_set_context(self, *args, **kwargs):
            pass

        def tls_insecure_set(self, *args, **kwargs):
            pass

        def reconnect_delay_set(self, *args, **kwargs):
            pass

    stub = types.SimpleNamespace(
        Client=StubClient, MQTTv311=5,
        CallbackAPIVersion=types.SimpleNamespace(VERSION2=2))
    paho = types.ModuleType("paho")
    mqtt_pkg = types.ModuleType("paho.mqtt")
    client_mod_stub = types.ModuleType("paho.mqtt.client")
    client_mod_stub.Client = StubClient
    client_mod_stub.MQTTv311 = 5
    client_mod_stub.CallbackAPIVersion = stub.CallbackAPIVersion
    monkeypatch.setitem(sys.modules, "paho", paho)
    monkeypatch.setitem(sys.modules, "paho.mqtt", mqtt_pkg)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", client_mod_stub)

    from print_handy.printers.bambulab.client import _paho_factory
    _paho_factory("192.168.1.50", SERIAL, CODE)
    assert built[-1]["client_id"] == f"print-handy-{SERIAL.lower()}"
    _paho_factory("192.168.1.50", SERIAL, CODE, client_id="print-handy-x-status-1")
    assert built[-1]["client_id"] == "print-handy-x-status-1"


@pytest.mark.parametrize("kwargs", [
    {"host": "https://192.168.1.50"}, {"host": "192.168.1.50:8883"}, {"host": ""},
    {"serial": ""}, {"access_code": ""}, {"timeout_s": 0}, {"timeout_s": True},
])
def test_client_refuses_bad_configuration_before_any_connection(kwargs):
    args = {"host": "192.168.1.50", "serial": SERIAL, "access_code": CODE, "timeout_s": 5}
    args.update(kwargs)
    with pytest.raises(BambuLanError):
        BambuLanClient(mqtt_factory=factory_for(FakeMqtt()), **args)


def test_chamber_light_payload_shape():
    from print_handy.printers.bambulab.client import chamber_light_payload
    assert chamber_light_payload(True) == {
        "system": {"sequence_id": "0", "command": "ledctrl",
                   "led_node": "chamber_light", "led_mode": "on",
                   "led_on_time": 500, "led_off_time": 500,
                   "loop_times": 0, "interval_time": 0}}
    assert chamber_light_payload(False)["system"]["led_mode"] == "off"


def test_chamber_light_commands_use_distinct_client_ids_never_the_listener_id(monkeypatch):
    # P2-206: the auto light-off driver sends a chamber-light command at
    # every print start/end and again at idle-off, and each command built
    # its MQTT client with the listener's session ID — disconnecting the
    # listener's session every time (MQTT reuses an ID by kicking the
    # existing client). One-shot light commands get a fresh distinct ID,
    # the same treatment snapshot reads got in P2-265.
    from print_handy.printers.bambulab import client as client_mod

    seen = []
    fakes = [FakeMqtt(), FakeMqtt()]

    def recording_factory(host, serial, access_code, client_id=None):
        seen.append(client_id)
        return fakes.pop(0)

    monkeypatch.setattr(client_mod, "_paho_factory", recording_factory)
    client = BambuLanClient("192.168.1.50", SERIAL, CODE, timeout_s=5)  # built-in factory path
    client.set_chamber_light(True)
    client.set_chamber_light(False)
    listener_id = f"print-handy-{SERIAL.lower()}"
    assert len(seen) == 2 and all(seen), seen
    assert seen[0] != seen[1], seen  # repeated toggles cannot disconnect each other
    assert all(cid != listener_id for cid in seen), seen  # never the listener's session ID
    assert all("-light-" in cid for cid in seen), seen


def test_set_chamber_light_publishes_ledctrl_to_the_request_topic():
    fake = FakeMqtt()
    client = BambuLanClient("192.168.1.50", SERIAL, CODE, mqtt_factory=factory_for(fake), timeout_s=5)
    client.set_chamber_light(True)
    assert fake.published == [(f"device/{SERIAL}/request",
                               {"system": {"sequence_id": "0", "command": "ledctrl",
                                           "led_node": "chamber_light", "led_mode": "on",
                                           "led_on_time": 500, "led_off_time": 500,
                                           "loop_times": 0, "interval_time": 0}})]
    assert fake.disconnected
    client.set_chamber_light(False)
    assert fake.published[-1][1]["system"]["led_mode"] == "off"


def test_tls_context_verifies_the_bambu_ca_chain_without_hostname_matching():
    context = lan_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is False  # the certificate CN is the serial, not the IP
    assert context.maximum_version == ssl.TLSVersion.TLSv1_2


def test_signature_required_reads_the_fun_bit():
    assert signature_required({"fun": "3EC1AFFF9CFF"}) is True   # Developer Mode off (pybambu example)
    assert signature_required({"fun": "3EC18FFF9CFF"}) is False  # Developer Mode on
    assert signature_required({}) is False and signature_required({"fun": "junk"}) is False


def test_merge_print_merges_nested_dicts_and_replaces_leaves():
    merged = merge_print({"ams": {"ams": [{"id": 0}], "tray_now": 1}, "mc_percent": 1},
                         {"ams": {"tray_now": 2}, "mc_percent": 5})
    assert merged == {"ams": {"ams": [{"id": 0}], "tray_now": 2}, "mc_percent": 5}


# --------------------------------------------------------------------- adapter

def config_for(**environ):
    env = {"PRINT_HANDY__PRINTER__MODEL": "bambulab.a1", **environ}
    return settings.load(local=False, environ=env)


def lan_adapter(monkeypatch, **environ):
    monkeypatch.setenv("BAMBU_LAN_PASSWORD", CODE)
    cfg = config_for(PRINT_HANDY__BAMBU__LAN_HOST="192.168.1.50", PRINT_HANDY__BAMBU__LAN_SERIAL=SERIAL.lower(),
                     **environ)
    return printers.get("bambulab.a1", cfg)


def _adapter(cfg):
    return printers.get("bambulab.a1", cfg)


def test_adapter_lan_configuration_and_secret_indirection(monkeypatch):
    a = lan_adapter(monkeypatch)
    assert a.transport == "bambulab_lan" and a.lan_configured() is True
    client = a.client()
    assert client.host == "192.168.1.50" and client.serial == SERIAL  # serial normalized to upper case
    unconfigured = _adapter(config_for())
    assert unconfigured.lan_configured() is False
    with pytest.raises(BambuLanError, match="not configured"):
        unconfigured.client()
    monkeypatch.delenv("BAMBU_LAN_PASSWORD")
    with pytest.raises(BambuLanError, match="access code is not set"):
        a.client().snapshot()


def test_normalize_status_maps_the_snapshot_to_the_common_shape():
    a = _adapter(config_for())
    snap = {"print": {"gcode_state": "RUNNING", "mc_percent": 42, "mc_remaining_time": 87,
                      "nozzle_temper": 210.5, "bed_temper": 59.5, "subtask_name": "benchy.gcode.3mf",
                      "fun": "3EC1AFFF9CFF"}, "info": None, "online": True}
    out = a.normalize_status(snap)
    assert (out["state"], out["progress"], out["hotend_c"], out["bed_c"]) == ("printing", 0.42, 210.5, 59.5)
    assert out["filename"] == "benchy.gcode.3mf" and out["remaining_min"] == 87 and out["writes_blocked"] is True
    assert out["gcode_state"] == "RUNNING" and out["stage"] is None  # raw state and mc_print_stage kept (#271)
    prepare = a.normalize_status({"print": {"gcode_state": "prepare", "mc_print_stage": "2"}})
    assert (prepare["state"], prepare["gcode_state"], prepare["stage"]) == ("printing", "PREPARE", "2")
    assert a.normalize_status({"print": {"gcode_state": "IDLE"}})["state"] == "standby"
    assert a.normalize_status({"print_stats": {"state": "printing"}})["state"] == "unknown"  # Moonraker shape


def test_readiness_adds_live_problems_only_with_a_snapshot():
    a = _adapter(config_for())
    record = {"bed_mm": [256, 256], "max_print_height_mm": 256, "nozzle": {"diameter_mm": 0.4, "type": "stainless_steel"}}
    assert a.readiness_problems(record, {}) == []  # the original offline behaviour
    busy = {"print": {"gcode_state": "RUNNING", "print_error": 0, "fun": "3EC18FFF9CFF"}}
    assert a.readiness_problems(record, busy) == ["printer is busy (running)"]
    blocked = {"print": {"gcode_state": "IDLE", "print_error": 503, "hms": [{"attr": 1}], "fun": "3EC1AFFF9CFF"}}
    problems = a.readiness_problems(record, blocked)
    assert any("print error" in p for p in problems) and any("HMS" in p for p in problems)
    assert any("Developer Mode is off" in p for p in problems)


# --------------------------------------------------------------------- settings + state

def test_settings_validate_the_lan_pair():
    with pytest.raises(settings.SettingsError, match="must be set together"):
        config_for(PRINT_HANDY__BAMBU__LAN_HOST="192.168.1.50")
    with pytest.raises(settings.SettingsError, match="only apply to Bambu Lab"):
        settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": "flashforge.ad5x",
                                            "PRINT_HANDY__BAMBU__LAN_HOST": "192.168.1.50",
                                            "PRINT_HANDY__BAMBU__LAN_SERIAL": SERIAL})
    with pytest.raises(settings.SettingsError, match="environment variable name"):
        config_for(PRINT_HANDY__BAMBU__LAN_ACCESS_CODE_ENV="not a name")
    cfg = config_for(PRINT_HANDY__BAMBU__LAN_HOST="192.168.1.50", PRINT_HANDY__BAMBU__LAN_SERIAL=SERIAL.lower())
    assert cfg["bambu"]["lan_serial"] == SERIAL  # stored upper case


def test_printer_state_check_uses_telemetry_and_spoolman_but_never_moonraker(monkeypatch):
    a = lan_adapter(monkeypatch)
    snap = {"print": {"gcode_state": "IDLE", "mc_percent": 0, "fun": "3EC18FFF9CFF"}, "info": None, "online": True}
    monkeypatch.setattr(a, "client", lambda **kw: type("C", (), {"snapshot": lambda self: snap})())
    monkeypatch.setattr(printers, "current", lambda config=None: a)
    contacted = []

    def get(url, data=None, method=None, timeout=6):
        contacted.append(url)
        return {"id": 10, "archived": False, "filament": {"name": "white", "material": "PLA"}, "remaining_weight": 900}
    monkeypatch.setattr(printer_state, "_get", get)

    def spoolman_request(method, path, body=None, *, base_url=None, timeout=None):
        # #387: the check's Spoolman leg travels through the one client now.
        contacted.append(f"{base_url}{path}")
        return {"id": 10, "archived": False, "filament": {"name": "white", "material": "PLA"}, "remaining_weight": 900}
    monkeypatch.setattr(spoolman_client, "request", spoolman_request)
    record = {"bed_mm": [256, 256], "max_print_height_mm": 256,
              "nozzle": {"diameter_mm": 0.4, "type": "stainless_steel"},
              "spool": {"spoolman_id": 10, "name": "white"}, "spoolman_url": "http://spoolman.test:7912"}
    ok, probs, facts = printer_state.check(record)
    assert (ok, probs) == (True, [])
    assert facts["connection"] == "bambulab_lan" and facts["lan_configured"] is True
    assert facts["bambu"]["state"] == "standby"
    assert contacted == ["http://spoolman.test:7912/api/v1/spool/10"]


# --------------------------------------------------------------------- wizard

def test_wizard_collects_lan_status_as_config_plus_secret():
    answers = {"brand": "bambulab", "model": "bambulab.a1", "bambu_lan": "yes",
               "lan_host": "192.168.1.50", "lan_serial": SERIAL, "bambu_lan_password": CODE,
               "spoolman": "skip", "camera": "no", "have_bot": "yes", "hermes": "no"}
    plan = build_plan(Wizard(answers=answers, output=io.StringIO()), hermes_found=False)
    assert plan["config"]["bambu"] == {"lan_host": "192.168.1.50", "lan_serial": SERIAL}
    assert plan["secrets"]["BAMBU_LAN_PASSWORD"] == CODE
    assert CODE not in json.dumps(plan["config"])  # the access code never lands in config


def test_wizard_without_lan_keeps_the_offline_plan():
    answers = {"brand": "bambulab", "model": "bambulab.a1", "bambu_lan": "no",
               "spoolman": "skip", "camera": "no", "have_bot": "yes", "hermes": "no"}
    plan = build_plan(Wizard(answers=answers, output=io.StringIO()), hermes_found=False)
    assert "bambu" not in plan["config"] and plan["secrets"]["BAMBU_LAN_PASSWORD"] == ""
