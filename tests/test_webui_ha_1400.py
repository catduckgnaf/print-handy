"""#1400 (ha-print-handy #14): what the Home Assistant integration needs.

Per-printer temperature limits, the controls-enabled flag, a stable
instance id, and the cheap ``GET /api/printers`` roster. Fake reads and
senders only: nothing here opens a socket to a printer or a service.
"""
import http.client
import json
import re
import socket
import threading
import urllib.request
from pathlib import Path
from unittest import mock

import pytest

from print_handy import doctor, printers, settings
from print_handy.webui import controls, instance, server, status

TOKEN = "ha-1400-test-token"

ROSTER = """\
printers:
  - id: garage-a1
    name: Garage A1
    model: bambulab.a1
    lan_host: 192.0.2.60
    lan_serial: "00M00A000000000"
  - id: workshop-ad5x
    name: Workshop AD5X
    model: flashforge.ad5x
    moonraker_url: http://192.0.2.61:7125
    camera_snapshot_url: http://192.0.2.61/webcam/?action=snapshot
  - id: bench-klipper
    name: Bench Klipper
    model: klipper.generic
    moonraker_url: http://192.0.2.62:7125
"""
IDS = ["garage-a1", "workshop-ad5x", "bench-klipper"]


def load(tmp_path, extra="", environ=None):
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    local = Path(tmp_path) / "local.yaml"
    local.write_text(f"paths:\n  state_home: {tmp_path}/state-home\n{ROSTER}{extra}")
    return settings.load(local=local, environ={} if environ is None else environ)


def enabled(tmp_path, extra=""):
    return load(tmp_path, "webui:\n  controls_enabled: true\n" + extra)


def snap(data):
    return status.snapshot(data, environ={}, heartbeat=None,
                           probe=lambda url: (True, "HTTP 200"), checks=[])


def _boom(*_args, **_kwargs):
    raise AssertionError("the roster must not run the doctor or touch the network")


# -- temperature limits ------------------------------------------------------

def test_limits_are_the_ceilings_the_control_routes_enforce(tmp_path):
    data = enabled(tmp_path)
    rows = {row["id"]: row for row in status.roster(data)["printers"]}
    for printer_id in IDS:
        view = settings.view_for(printer_id, data)
        adapter = printers.get(settings.by_id(printer_id, data)["model"], view)
        expected = {heater: controls.temperature_limit(adapter, view, heater, printer_id)
                    for heater in ("nozzle", "bed", "chamber")}
        assert rows[printer_id]["temperature_limits_c"] == expected
        assert all(isinstance(v, float) and v > 0 for v in expected.values())


def test_limits_match_what_set_temperature_accepts_and_refuses(tmp_path):
    data = enabled(tmp_path)
    for printer_id in IDS:
        limits = status.roster(data)["printers"][IDS.index(printer_id)]["temperature_limits_c"]
        for heater in ("nozzle", "bed"):
            sent = []
            code, body = controls.set_temperature(
                data, printer_id, {"heater": heater, "target": limits[heater]},
                read=lambda a, v: {"state": "idle"},
                send=lambda a, v, command: sent.append(command))
            assert code == 200, body
            assert body["limit"] == limits[heater]
            code, body = controls.set_temperature(
                data, printer_id, {"heater": heater, "target": limits[heater] + 1},
                read=lambda a, v: {"state": "idle"},
                send=lambda a, v, command: sent.append(command))
            assert code == 400 and "Nothing sent" in body["reason"]
            assert len(sent) == 1


def test_limits_on_every_payload_ha_reads(tmp_path):
    data = load(tmp_path)
    snapshot_rows = {row["id"]: row for row in snap(data)["printers"]}
    roster_rows = {row["id"]: row for row in status.roster(data)["printers"]}
    for printer_id in IDS:
        code, body = status.printer_status(data, printer_id,
                                           read=lambda a, v: {"state": "idle"})
        assert code == 200
        limits = body["temperature_limits_c"]
        assert set(limits) == {"nozzle", "bed", "chamber"}
        assert snapshot_rows[printer_id]["temperature_limits_c"] == limits
        assert roster_rows[printer_id]["temperature_limits_c"] == limits


def test_limits_follow_the_fitted_nozzle(tmp_path):
    def single(name, nozzle):
        local = Path(tmp_path) / f"{name}.yaml"
        local.write_text(f"paths:\n  state_home: {tmp_path}/{name}\n"
                         f"printer:\n  model: klipper.generic\n  nozzle_type: {nozzle}\n")
        return settings.load(local=local, environ={})

    brass, steel = single("brass", "brass"), single("steel", "hardened_steel")
    pid = settings.default_printer_id(brass)
    view = settings.view_for(pid, brass)
    adapter = printers.get("klipper.generic", view)
    assert controls.temperature_limits(brass, pid)["nozzle"] == controls.temperature_limit(
        adapter, view, "nozzle", pid)
    assert (controls.temperature_limits(brass, pid)["nozzle"]
            < controls.temperature_limits(steel, pid)["nozzle"])


def test_limits_for_an_unknown_printer_are_none():
    assert controls.temperature_limits({}, "nope") is None


# -- controls-enabled flag ---------------------------------------------------

@pytest.mark.parametrize("flag", [False, True])
def test_controls_flag_follows_the_setting(tmp_path, flag):
    data = enabled(tmp_path) if flag else load(tmp_path)
    assert controls.controls_enabled(data) is flag
    snapshot = snap(data)
    assert snapshot["controls_enabled"] is flag
    assert all(row["controls_enabled"] is flag for row in snapshot["printers"])
    roster = status.roster(data)
    assert roster["controls_enabled"] is flag
    assert all(row["controls_enabled"] is flag for row in roster["printers"])
    for printer_id in IDS:
        _code, body = status.printer_status(data, printer_id,
                                            read=lambda a, v: {"state": "idle"})
        assert body["controls_enabled"] is flag


def test_existing_fields_are_unchanged(tmp_path):
    data = load(tmp_path)
    _code, body = status.printer_status(data, "workshop-ad5x",
                                        read=lambda a, v: {"state": "idle"})
    for key in ("ok", "id", "name", "model", "vendor", "connection", "status"):
        assert key in body
    row = next(r for r in snap(data)["printers"] if r["id"] == "workshop-ad5x")
    for key in ("id", "name", "model", "vendor", "connection", "chat_id", "default",
                "moonraker_url", "camera_snapshot_url"):
        assert key in row


# -- instance id -------------------------------------------------------------

def test_instance_id_is_minted_once_and_stable(tmp_path):
    data = load(tmp_path)
    path = instance.instance_path(data)
    assert not path.exists()
    first = instance.instance_id(data)
    assert re.fullmatch(r"[0-9a-f]{32}", first)
    assert path.read_text().strip() == first
    mtime = path.stat().st_mtime_ns
    # A "restart": a fresh settings load over the same state folder.
    again = load(tmp_path)
    assert instance.instance_id(again) == first
    assert path.stat().st_mtime_ns == mtime
    assert snap(again)["instance_id"] == first
    assert status.roster(again)["instance_id"] == first


def test_instance_id_is_random_not_host_derived(tmp_path):
    with mock.patch.object(socket, "gethostname", _boom), \
            mock.patch("uuid.getnode", _boom):
        one = instance.instance_id(load(tmp_path / "a"))
        two = instance.instance_id(load(tmp_path / "b"))
    assert one and two and one != two
    assert socket.gethostname().lower() not in one


def test_a_malformed_instance_file_is_replaced(tmp_path):
    data = load(tmp_path)
    path = instance.instance_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-an-id\n")
    value = instance.instance_id(data)
    assert re.fullmatch(r"[0-9a-f]{32}", value)
    assert instance.instance_id(data) == value


def test_an_unwritable_state_folder_reads_as_none(tmp_path):
    data = load(tmp_path)
    with mock.patch.object(instance, "_mint", side_effect=PermissionError("read-only")):
        assert instance.instance_id(data) is None
        assert status.roster(data)["instance_id"] is None


# -- the roster endpoint -----------------------------------------------------

def start(tmp_path, extra=""):
    environ = {"WEBUI_ACCESS_TOKEN": TOKEN}
    data = load(tmp_path, "webui:\n  enabled: true\n  port: 8765\n" + extra, environ=environ)
    httpd = server.make_server(data, environ=environ, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def get(port, path, token=TOKEN):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {"Accept": "application/json"}
        if token:
            headers[server.TOKEN_HEADER] = token
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        return response.status, response.read().decode()
    finally:
        conn.close()


@pytest.fixture
def no_doctor_no_network():
    """Every doctor, probe and printer-read path raises."""
    with mock.patch.object(status, "doctor_checks", _boom), \
            mock.patch.object(status, "probe_spoolman", _boom), \
            mock.patch.object(status, "read_live_status", _boom), \
            mock.patch.object(status, "_moonraker_status", _boom), \
            mock.patch.object(status, "printer_reachability", _boom), \
            mock.patch.object(status, "slicer_status", _boom), \
            mock.patch.object(doctor, "check_orca", _boom), \
            mock.patch.object(doctor, "check_service", _boom), \
            mock.patch.object(urllib.request, "urlopen", _boom), \
            mock.patch("subprocess.run", _boom), \
            mock.patch("subprocess.Popen", _boom):
        yield


def test_roster_endpoint_lists_every_printer_without_doctor_or_network(
        tmp_path, no_doctor_no_network):
    """Regression for #1400: on main there is no GET /api/printers (it is a 404)."""
    httpd = start(tmp_path, "  controls_enabled: true\n")
    try:
        code, text = get(httpd.server_address[1], "/api/printers")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert code == 200, text
    body = json.loads(text)
    assert body["ok"] is True
    assert re.fullmatch(r"[0-9a-f]{32}", body["instance_id"])
    assert body["controls_enabled"] is True
    assert [row["id"] for row in body["printers"]] == IDS
    by_id = {row["id"]: row for row in body["printers"]}
    assert by_id["garage-a1"]["model"] == "bambulab.a1"
    assert by_id["garage-a1"]["connection"] == "bambulab_lan"
    assert by_id["workshop-ad5x"]["connection"] == "moonraker"
    assert by_id["garage-a1"]["default"] is True
    assert by_id["workshop-ad5x"]["camera_snapshot_url"]
    for row in body["printers"]:
        assert row["controls_enabled"] is True
        assert set(row["temperature_limits_c"]) == {"nozzle", "bed", "chamber"}
        assert row["name"] and row["vendor"]
    # No secret material in the cheap roster either.
    assert "00M00A000000000" not in text and TOKEN not in text


def test_roster_endpoint_needs_the_same_auth_as_api_status(tmp_path, no_doctor_no_network):
    httpd = start(tmp_path)
    try:
        port = httpd.server_address[1]
        for path in ("/api/printers", "/api/status"):
            assert get(port, path, token=None)[0] == 401
            assert get(port, path, token="wrong")[0] == 401
        assert get(port, "/api/printers")[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_roster_endpoint_is_read_only(tmp_path):
    httpd = start(tmp_path)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("POST", "/api/printers", body=b"{}",
                     headers={"Content-Type": "application/json", server.TOKEN_HEADER: TOKEN})
        response = conn.getresponse()
        response.read()
        conn.close()
        assert response.status != 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_roster_reads_no_socket(tmp_path, no_doctor_no_network):
    data = enabled(tmp_path)
    with mock.patch.object(socket.socket, "connect", _boom), \
            mock.patch.object(socket, "create_connection", _boom), \
            mock.patch.object(socket, "getaddrinfo", _boom):
        body = status.roster(data)
    assert [row["id"] for row in body["printers"]] == IDS


def test_roster_route_declares_the_read_policy():
    from print_handy.webui import routes
    route, _ = routes.resolve("GET", "/api/printers")
    assert route.handler == "_api_printers" and route.policy == routes.SESSION
    status_route, _ = routes.resolve("GET", "/api/status")
    assert route.policy == status_route.policy
