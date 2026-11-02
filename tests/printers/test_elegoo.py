"""Elegoo vendors (#243): Neptune models on Moonraker + Centauri Carbon on SDCP.

Everything is offline: profiles run against invented resources (elegoo_fixture), the SDCP
client against a fake WebSocket/UDP/HTTP transport, and no test touches a network or a
printer. Live support is unclaimed — there is no Elegoo hardware to test against.
"""
import json
import re

import pytest

from print_handy import printers, settings
from print_handy.printers.elegoo import VENDOR as ELEGOO_VENDOR
from print_handy.printers.elegoo import facts as elegoo_facts
from print_handy.printers.elegoo import profiles
from print_handy.printers.elegoo_sdcp import VENDOR as SDCP_VENDOR
from print_handy.printers.elegoo_sdcp import facts as sdcp_facts
from print_handy.printers.elegoo_sdcp.adapter import ElegooSdcpAdapter
from print_handy.printers.elegoo_sdcp.client import SdcpClient, SdcpError

from . import elegoo_fixture as fixture

NEPTUNES = {
    "elegoo.neptune4": "NEPTUNE4",
    "elegoo.neptune4pro": "NEPTUNE4PRO",
    "elegoo.neptune4plus": "NEPTUNE4PLUS",
    "elegoo.neptune4max": "NEPTUNE4MAX",
}
CENTAURI = "elegoo_sdcp.centauricarbon"


def config_for(key, hermes=fixture.HERMES, environ=None):
    env = {"PRINT_HANDY__PRINTER__MODEL": key, "PRINT_HANDY__PATHS__HERMES_HOME": str(hermes),
           **(environ or {})}
    return settings.load(local=False, environ=env)


def adapter_for(key, cfg=None):
    return printers.get(key, cfg or config_for(key))


# --------------------------------------------------------------------------- models + settings

@pytest.mark.parametrize("key", sorted(NEPTUNES))
def test_neptune_registration_and_defaults(key):
    model = printers.MODELS[key]
    assert model.vendor is ELEGOO_VENDOR is printers.VENDORS["elegoo"]
    assert model.generated_templates is True
    a = adapter_for(key)
    f = getattr(elegoo_facts, NEPTUNES[key])
    volume = a.build_volume()
    assert volume.bed_mm == tuple(float(v) for v in f["bed_mm"].value)
    assert volume.max_height_mm == float(f["max_print_height_mm"].value)
    cfg = config_for(key)
    assert cfg["printer"]["slicer"] == "orca" and cfg["printer"]["firmware"] == "Klipper"
    assert cfg["delivery"]["methods"] == ["printer"]  # Moonraker path: printer is the default
    with pytest.raises(settings.SettingsError, match="not supported by model"):
        config_for(key, environ={"PRINT_HANDY__PRINTER__NOZZLE_DIAMETER_MM": "0.6"})


def test_centauri_registration_and_defaults():
    model = printers.MODELS[CENTAURI]
    assert model.vendor is SDCP_VENDOR is printers.VENDORS["elegoo_sdcp"]
    a = adapter_for(CENTAURI)
    assert isinstance(a, ElegooSdcpAdapter)
    assert (a.transport, a.slot_count, a.multi_slot) == ("elegoo_sdcp", 1, False)
    cfg = config_for(CENTAURI)
    assert cfg["printer"]["firmware"] == "Elegoo SDCP"
    assert cfg["delivery"]["methods"] == ["discord"]  # printer delivery is opt-in, never a default


def test_sdcp_settings_validation():
    cfg = config_for(CENTAURI, environ={"PRINT_HANDY__ELEGOO_SDCP__HOST": "192.168.1.80"})
    assert cfg["elegoo_sdcp"] == {"host": "192.168.1.80"}
    with pytest.raises(settings.SettingsError, match="only applies to Elegoo SDCP"):
        config_for("elegoo.neptune4", environ={"PRINT_HANDY__ELEGOO_SDCP__HOST": "192.168.1.80"})
    with pytest.raises(settings.SettingsError, match="plain IP address or hostname"):
        config_for(CENTAURI, environ={"PRINT_HANDY__ELEGOO_SDCP__HOST": "http://192.168.1.80"})
    with pytest.raises(settings.SettingsError, match="moonraker_url and octoprint_url must be empty"):
        config_for(CENTAURI, environ={"PRINT_HANDY__SERVICES__MOONRAKER_URL": "http://x:7125"})


# --------------------------------------------------------------------------- profiles

@pytest.mark.parametrize("key", sorted(NEPTUNES))
def test_neptune_identity_is_a_real_pin(key):
    f = getattr(elegoo_facts, NEPTUNES[key])
    entries = f["orca_identity"].value
    # One pin per checked packaging (#356): 2.4.2's tree, then 2.5.0-alpha's and 2.5.0-beta's tree and packed cache.
    assert [known["vendor_version"] for known in entries] == ["02.04.00.06", "02.04.00.11", "02.04.00.11", "02.04.00.13", "02.04.00.13"]
    for known in entries:
        assert all(re.fullmatch(r"[0-9a-f]{64}", d) for d in known["sha256"].values())
    assert all(fixture.identity(f)["sha256"] != known["sha256"] for known in entries)


def test_centauri_identity_is_a_real_pin():
    entries = sdcp_facts.CENTAURI_CARBON["orca_identity"].value
    assert [known["vendor_version"] for known in entries] == ["02.04.00.06", "02.04.00.11", "02.04.00.11", "02.04.00.13", "02.04.00.13"]
    for known in entries:
        assert all(re.fullmatch(r"[0-9a-f]{64}", d) for d in known["sha256"].values())
    assert all(fixture.identity(sdcp_facts.CENTAURI_CARBON)["sha256"] != known["sha256"]
               for known in entries)


@pytest.mark.parametrize("key", sorted(NEPTUNES))
def test_neptune_fixture_profile_install(key, monkeypatch):
    f = getattr(elegoo_facts, NEPTUNES[key])
    a = adapter_for(key)
    fixture.trust(type(a), f, monkeypatch.setattr)
    folder = fixture.install(a, f)
    assert profiles.problems(a) == []
    assert a.start_plan(60, 215)[0] == "M400"  # the template's inline start, not a PRINT_START macro


def test_centauri_fixture_profile_install(monkeypatch):
    f = sdcp_facts.CENTAURI_CARBON
    a = adapter_for(CENTAURI)
    fixture.trust(type(a), f, monkeypatch.setattr)
    folder = fixture.install(a, f)
    assert profiles.problems(a) == []
    machine = json.loads((folder / "machine.json").read_text())
    assert machine["bed_exclude_area"] == ["246x0", "256x0", "256x20", "246x20"]


# --------------------------------------------------------------------------- Neptune guard

@pytest.mark.parametrize("key", sorted(NEPTUNES))
def test_neptune_guard(key):
    a = adapter_for(key)
    f = getattr(elegoo_facts, NEPTUNES[key])
    model = f["printer_model"].value
    good = f"; printer_model = {model}\n; nozzle_diameter = 0.4\nM140 S60\nM104 S215\nG28\nG1 X10 Y10 E1\n"
    assert a.gcode_problems(good) == []
    assert any("another printer ecosystem" in p for p in a.gcode_problems("PRINT_START BED=60\n" + good))
    assert any("sliced for" in p for p in a.gcode_problems(good.replace(model, "Elegoo Neptune 4 Max")
                                                          if model != "Elegoo Neptune 4 Max"
                                                          else good.replace(model, "Elegoo Neptune 4")))
    assert any("exceeds" in p for p in a.gcode_problems(good + "M104 S320\n"))


def test_centauri_guard():
    a = adapter_for(CENTAURI)
    good = ("; printer_model = Elegoo Centauri Carbon\n; nozzle_diameter = 0.4\n"
            "M140 S60\nM104 S215\nG28\nG1 X10 Y10 E1\n")
    assert a.gcode_problems(good) == []
    assert any("another vendor's start code" in p for p in a.gcode_problems("PRINT_START\n" + good))
    assert any("exceeds" in p for p in a.gcode_problems(good + "M109 S330\n"))


# --------------------------------------------------------------------------- SDCP client (fake transports)

class FakeWs:
    """An in-memory SDCP peer: answers each request envelope with a canned response."""

    def __init__(self, server):
        self.server = server
        self.queue = []
        self.sent = []

    def send(self, raw):
        envelope = json.loads(raw)
        self.sent.append(envelope)
        body = envelope["Data"]
        payload = self.server.answer(body["Cmd"], body.get("Data") or {})
        self.queue.append(json.dumps({"Topic": envelope["Topic"].replace("request", "response"),
                                      "Data": {"Cmd": body["Cmd"], "Data": payload,
                                               "RequestID": body["RequestID"], "Ack": 0}}))

    def recv(self, timeout=None):
        return self.queue.pop(0)

    def close(self):
        pass


class FakeSdcpServer:
    def __init__(self, status_code=0, filename=""):
        self.status_code = status_code
        self.filename = filename
        self.file_size = None
        self.started = []
        self.controls = []

    def answer(self, cmd, data):
        if cmd == 0:
            return {"Status": {"CurrentStatus": [self.status_code], "TempOfNozzle": 25.0,
                               "TempOfHotbed": 24.0},
                    "PrintInfo": {"Filename": self.filename, "CurrentLayer": 3, "TotalLayer": 10}}
        if cmd == 260:
            return {"FileSize": self.file_size}
        if cmd == 128:
            self.started.append(data)
            self.status_code, self.filename = 2, data["Filename"].rsplit("/", 1)[-1]
            return {}
        if cmd in (129, 130, 131):
            self.controls.append(cmd)
            return {}
        return {}


class _Raw:
    def __init__(self, status=200):
        self.status = status

    def read(self):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def sdcp_client(server, **kw):
    posts = []

    def opener(req, timeout=None):
        posts.append(req)
        return _Raw(200)

    client = SdcpClient("192.168.1.80", "MID123", ws_factory=lambda url: FakeWs(server),
                        opener=opener, **kw)
    return client, posts


def _discovery_payload(mid):
    return json.dumps({"Id": "x", "Data": {"MainboardID": mid}}).encode()


def test_sdcp_discovery_via_udp():
    payload = _discovery_payload("MID999")
    seen = {}

    def udp(host, timeout):
        seen["host"] = host
        return payload, ("192.168.1.80", 3000)  # the configured printer answers

    client = SdcpClient("192.168.1.80", udp=udp)
    assert client.discover() == "MID999" and seen["host"] == "255.255.255.255"
    assert client.mainboard_id == "MID999"


def test_sdcp_discovery_ignores_other_printers():
    # Another SDCP printer on the LAN answers the broadcast first; its MainboardID
    # must not be remembered for requests sent to the configured host (P2-91).
    answers = iter([
        (_discovery_payload("OTHER1"), ("192.168.1.99", 3000)),
        (_discovery_payload("MID999"), ("192.168.1.80", 3000)),
    ])

    def udp(host, timeout):
        return next(answers)

    client = SdcpClient("192.168.1.80", udp=udp)
    assert client.discover() == "MID999"
    assert client.mainboard_id == "MID999"


def test_sdcp_discovery_refuses_when_only_other_printers_answer():
    def udp(host, timeout):
        return _discovery_payload("OTHER1"), ("192.168.1.99", 3000)

    client = SdcpClient("192.168.1.80", udp=udp)
    with pytest.raises(SdcpError, match="other printers"):
        client.discover()
    assert client.mainboard_id == ""


def test_sdcp_status_upload_start_control(tmp_path):
    server = FakeSdcpServer()
    client, posts = sdcp_client(server)
    status = client.status()
    assert status["Status"]["CurrentStatus"] == [0]
    gcode = tmp_path / "benchy.gcode"
    gcode.write_bytes(b"G28\n" * 100)
    server.file_size = gcode.stat().st_size
    assert client.upload(gcode) == "benchy.gcode"
    assert posts and b"S-File-MD5" in posts[0].data
    assert client.start_print("benchy.gcode") == "benchy.gcode"
    start = server.started[0]
    assert start["Filename"] == "/local/benchy.gcode" and start["Calibration_switch"] == 0
    assert client.control("pause") is True and server.controls == [129]
    with pytest.raises(SdcpError, match="unsupported"):
        client.control("explode")


def test_sdcp_set_light_sends_cmd_403_with_light_status_only():
    server = FakeSdcpServer()
    client, _posts = sdcp_client(server)
    assert client.set_light(True) is True
    assert client.set_light(False) is True
    sent = [e["Data"] for e in client._ws.sent if e["Data"]["Cmd"] == 403]
    # Cmd 403 is a partial device-control update: LightStatus alone, so
    # the fans and any RGB accent state are left exactly as they are.
    assert [d["Data"] for d in sent] == [{"LightStatus": {"SecondLight": 1}},
                                          {"LightStatus": {"SecondLight": 0}}]


def test_sdcp_upload_size_mismatch_refuses(tmp_path):
    server = FakeSdcpServer()
    client, _posts = sdcp_client(server)
    gcode = tmp_path / "x.gcode"
    gcode.write_bytes(b"G28\n")
    server.file_size = 9999  # the printer's copy disagrees with what was uploaded
    with pytest.raises(SdcpError, match="could not be verified"):
        client.upload(gcode)


def test_sdcp_upload_missing_size_refuses(tmp_path):
    # file_info answers but reports no size: the copy is unverified, so the
    # upload must fail instead of letting start_print run on it (P2-90).
    server = FakeSdcpServer()
    client, _posts = sdcp_client(server)
    gcode = tmp_path / "x.gcode"
    gcode.write_bytes(b"G28\n")
    server.file_size = None  # the FileSize field comes back empty
    with pytest.raises(SdcpError, match="could not be verified"):
        client.upload(gcode)


class EmptyInfoServer(FakeSdcpServer):
    def answer(self, cmd, data):
        if cmd == 260:
            return {}  # an empty file-info response carries no size at all
        return super().answer(cmd, data)


def test_sdcp_upload_empty_file_info_refuses(tmp_path):
    client, _posts = sdcp_client(EmptyInfoServer())
    gcode = tmp_path / "x.gcode"
    gcode.write_bytes(b"G28\n")
    with pytest.raises(SdcpError, match="could not be verified"):
        client.upload(gcode)


def test_sdcp_upload_invalid_size_refuses(tmp_path):
    server = FakeSdcpServer()
    client, _posts = sdcp_client(server)
    gcode = tmp_path / "x.gcode"
    gcode.write_bytes(b"G28\n")
    server.file_size = "not-a-number"  # a size that cannot be read is no verification
    with pytest.raises(SdcpError, match="could not be verified"):
        client.upload(gcode)


class SilentWs:
    """A peer that takes the request but never answers (P2-89).

    Models the real socket: recv() with no timeout would block forever, so it
    raises a distinctive error instead; recv(timeout=...) raises TimeoutError,
    the way the websockets sync client does when the deadline passes.
    """

    def __init__(self):
        self.recv_timeouts = []

    def send(self, raw):
        pass

    def recv(self, timeout=None):
        self.recv_timeouts.append(timeout)
        if timeout is None:
            raise RuntimeError("recv without a timeout would block forever")
        raise TimeoutError("timed out")

    def close(self):
        pass


def test_sdcp_request_recv_is_bounded_by_request_timeout():
    ws = SilentWs()
    client = SdcpClient("192.168.1.80", "MID123", ws_factory=lambda url: ws,
                        request_timeout=0.5)
    with pytest.raises(SdcpError, match="did not answer SDCP command 0 in time"):
        client.status()
    # Every receive was handed a positive slice of the request deadline.
    assert ws.recv_timeouts and all(t is not None and 0 < t <= 0.5 for t in ws.recv_timeouts)


def test_sdcp_bad_inputs(tmp_path):
    with pytest.raises(SdcpError, match="plain printer IP"):
        SdcpClient("http://192.168.1.80", "MID")
    client, _ = sdcp_client(FakeSdcpServer())
    bad = tmp_path / "model.stl"
    bad.write_bytes(b"x")
    with pytest.raises(SdcpError, match="invalid SDCP remote file name"):
        client.upload(bad)
    with pytest.raises(SdcpError, match="invalid SDCP remote file name"):
        client.start_print("../escape.gcode")


# --------------------------------------------------------------------------- SDCP adapter

def test_sdcp_adapter_unconfigured_and_status():
    a = adapter_for(CENTAURI)
    assert a.sdcp_configured() is False
    with pytest.raises(SdcpError, match="SDCP is not configured"):
        a.client()
    raw = {"Status": {"CurrentStatus": [2], "TempOfNozzle": 215.0, "TempOfHotbed": 60.0},
           "PrintInfo": {"Filename": "/local/benchy.gcode", "CurrentLayer": 5, "TotalLayer": 10,
                         "RemainTime": 600}}
    out = a.normalize_status(raw)
    assert out["state"] == "printing" and out["progress"] == 0.5 and out["filename"] == "benchy.gcode"
    assert out["hotend_c"] == 215.0 and out["remaining_min"] == 10 and out["status_code"] == 2
    assert a.normalize_status({})["state"] == "unknown"
    record = {"bed_mm": [256.0, 256.0], "max_print_height_mm": 256.0, "nozzle": {"diameter_mm": 0.4}}
    assert any("busy" in p for p in a.readiness_problems(record, raw))
    idle = {"Status": {"CurrentStatus": [0]}, "PrintInfo": {}}
    assert a.readiness_problems(record, idle) == []
    assert a.readiness_problems(record, {}) == []
