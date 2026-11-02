"""Per-domain connection probes (#537): the lightest proof call per domain,
run against candidate fields before anything is saved. All transports are
faked — no test here opens a socket or contacts a printer."""
import io
import json
import urllib.error

import pytest

from print_handy.core import connection_probe
from print_handy.core.connection_probe import probe
from print_handy.printers.bambulab.client import BambuLanError
from print_handy.printers.elegoo_sdcp.client import SdcpError
from print_handy.printers.prusa.client import PrusaLinkError


class _Response:
    def __init__(self, payload, status=200):
        self.status = status
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_returning(payload, status=200, seen=None):
    def opener(req, timeout=None):
        if seen is not None:
            seen.append((req.full_url, dict(req.header_items()), timeout))
        return _Response(payload, status)
    return opener


def opener_raising(exc):
    def opener(req, timeout=None):
        raise exc
    return opener


# ---------------------------------------------------------------------- Moonraker

def test_moonraker_probe_ok_reports_what_answered():
    seen = []
    result = probe("moonraker", {"moonraker_url": "http://ad5x.local:7125/"},
                   opener=opener_returning(
                       {"result": {"hostname": "ad5x", "software_version": "v2.3"}}, seen=seen))
    assert result.ok and result.category == ""
    assert "ad5x" in result.summary and "v2.3" in result.summary
    assert seen[0][0] == "http://ad5x.local:7125/printer/info"
    # The geometry read is a second request. This opener has no config, so the
    # probe stays ok and names the miss instead of inventing numbers.
    assert result.detail.get("geometry_error")
    assert "geometry" not in result.detail


def test_moonraker_geometry_is_read_after_info_and_a_failed_read_stays_ok():
    seen = []

    def opener(req, timeout=None):
        seen.append(req.full_url)
        if req.full_url.endswith("/printer/info"):
            return _Response({"result": {"hostname": "ad5x", "software_version": "v2"}})
        return _Response({"result": {"status": {"configfile": {"config": {
            "extruder": {"nozzle_diameter": 0.4},
            "stepper_x": {"position_max": 220},
            "stepper_y": {"position_max": 220},
            "stepper_z": {"position_max": 250},
        }}}}})

    result = probe("moonraker", {"moonraker_url": "http://ad5x.local:7125"}, opener=opener)
    assert result.ok
    assert seen[0].endswith("/printer/info")
    assert "configfile" in seen[1]
    assert result.detail["geometry"] == {"nozzle_mm": 0.4, "bed_mm": [220.0, 220.0], "max_z_mm": 250.0}

    def failing(req, timeout=None):
        if req.full_url.endswith("/printer/info"):
            return _Response({"result": {"hostname": "ad5x"}})
        raise urllib.error.URLError("config unavailable")

    missed = probe("moonraker", {"moonraker_url": "http://ad5x.local:7125"}, opener=failing)
    assert missed.ok and missed.detail.get("geometry_error")
    assert "geometry" not in missed.detail


def test_moonraker_probe_sends_api_key_only_when_stored():
    seen = []
    probe("moonraker", {"moonraker_url": "http://h:7125"}, {"moonraker_api_key": "k3y"},
          opener=opener_returning({"result": {}}, seen=seen))
    assert seen[0][1].get("X-api-key") == "k3y"


def test_moonraker_probe_unreachable_and_auth_categories():
    down = probe("moonraker", {"moonraker_url": "http://h:7125"},
                 opener=opener_raising(urllib.error.URLError("no route")))
    assert not down.ok and down.category == "unreachable"
    refused = probe("moonraker", {"moonraker_url": "http://h:7125"},
                    opener=opener_raising(urllib.error.HTTPError("u", 403, "no", {}, None)))
    assert not refused.ok and refused.category == "auth"
    weird = probe("moonraker", {"moonraker_url": "http://h:7125"},
                  opener=opener_returning({"jsonrpc": "2.0"}))
    assert not weird.ok and weird.category == "protocol"


# ---------------------------------------------------------------------- Bambu

class _Bambu:
    def __init__(self, host, serial, code, *, timeout_s=None, outcome=None):
        self.outcome = outcome

    def snapshot(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _bambu_factory(outcome):
    return lambda host, serial, code, **kw: _Bambu(host, serial, code, outcome=outcome)


def test_bambu_probe_ok():
    outcome = {"print": {}, "info": {"command": "get_version",
                                     "module": [{"name": "ota", "sw_ver": "01.07"}]},
               "online": True}
    result = probe("bambulab_lan", {"lan_host": "192.0.2.5", "lan_serial": "01S00A000000000"},
                   {"access_code": "12345678"}, bambu_factory=_bambu_factory(outcome))
    assert result.ok and "01.07" in result.summary and "192.0.2.5" in result.summary


def test_bambu_probe_missing_secret_never_connects():
    result = probe("bambulab_lan", {"lan_host": "192.0.2.5", "lan_serial": "S"},
                   {}, bambu_factory=_bambu_factory({}))
    assert not result.ok and result.category == "missing_secret"


def test_bambu_probe_access_code_rejected_is_auth():
    outcome = BambuLanError("the printer rejected the LAN access code (MQTT access denied)")
    result = probe("bambulab_lan", {"lan_host": "192.0.2.5", "lan_serial": "S"},
                   {"access_code": "00000000"}, bambu_factory=_bambu_factory(outcome))
    assert not result.ok and result.category == "auth"


def test_bambu_probe_unreachable():
    outcome = BambuLanError("Bambu Lab printer at 192.0.2.5 could not be reached (OSError)")
    result = probe("bambulab_lan", {"lan_host": "192.0.2.5", "lan_serial": "S"},
                   {"access_code": "12345678"}, bambu_factory=_bambu_factory(outcome))
    assert not result.ok and result.category == "unreachable"


# ---------------------------------------------------------------------- PrusaLink

class _Prusa:
    def __init__(self, host, key, *, outcome=None):
        self.outcome = outcome

    def info(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _prusa_factory(outcome):
    return lambda host, key: _Prusa(host, key, outcome=outcome)


def test_prusa_probe_ok():
    info = {"hostname": "mk4", "firmware": "6.1.0",
            "printer": {"name": "Garage MK4", "serial": "CZPX1234"}}
    result = probe("prusalink", {"link_host": "mk4.local"}, {"api_key": "key"},
                   prusa_factory=_prusa_factory(info))
    assert result.ok and "Garage MK4" in result.summary and "CZPX1234" in result.summary


def test_prusa_probe_malformed_json_is_protocol():
    result = probe("prusalink", {"link_host": "mk4.local"}, {"api_key": "k"},
                   prusa_factory=_prusa_factory(
                       PrusaLinkError("PrusaLink GET /api/v1/info answered malformed JSON")))
    assert not result.ok and result.category == "protocol"


def test_prusa_probe_categories():
    missing = probe("prusalink", {"link_host": "mk4.local"}, {}, prusa_factory=_prusa_factory({}))
    assert missing.category == "missing_secret"
    refused = probe("prusalink", {"link_host": "mk4.local"}, {"api_key": "bad"},
                    prusa_factory=_prusa_factory(
                        PrusaLinkError("PrusaLink GET /api/v1/info answered 401")))
    assert refused.category == "auth"
    down = probe("prusalink", {"link_host": "mk4.local"}, {"api_key": "k"},
                 prusa_factory=_prusa_factory(
                     PrusaLinkError("PrusaLink GET /api/v1/info failed; not retried")))
    assert down.category == "unreachable"


# ---------------------------------------------------------------------- SDCP

class _Sdcp:
    def __init__(self, host, *, outcome=None, request_timeout=None):
        self.outcome = outcome
        self.mainboard_id = ""
        self.closed = False

    def status(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        self.mainboard_id = "MID-123"
        return self.outcome

    def close(self):
        self.closed = True


def test_sdcp_probe_ok_reports_discovered_mainboard():
    client = _Sdcp("h", outcome={"MachineName": "Centauri Carbon"})
    result = probe("elegoo_sdcp", {"host": "192.0.2.9"}, {},
                   sdcp_factory=lambda host, **kw: client)
    assert result.ok and "MID-123" in result.summary and client.closed


def test_sdcp_probe_unreachable():
    result = probe("elegoo_sdcp", {"host": "192.0.2.9"}, {},
                   sdcp_factory=lambda host, **kw: _Sdcp(
                       host, outcome=SdcpError("could not open the SDCP WebSocket")))
    assert not result.ok and result.category == "unreachable"


# ---------------------------------------------------------------------- OctoPrint

def test_octoprint_probe_ok():
    seen = []
    result = probe("octoprint", {"octoprint_url": "http://ender.local"}, {"api_key": "KEY"},
                   opener=opener_returning({"api": "1.1", "server": "1.10.2", "text": "t"},
                                           seen=seen))
    assert result.ok and "1.10.2" in result.summary
    assert seen[0][0] == "http://ender.local/api/version"
    assert seen[0][1].get("X-api-key") == "KEY"


def test_octoprint_probe_categories():
    missing = probe("octoprint", {"octoprint_url": "http://e"}, {})
    assert missing.category == "missing_secret"
    refused = probe("octoprint", {"octoprint_url": "http://e"}, {"api_key": "bad"},
                    opener=opener_raising(urllib.error.HTTPError("u", 401, "no", {}, None)))
    assert refused.category == "auth"
    down = probe("octoprint", {"octoprint_url": "http://e"}, {"api_key": "k"},
                 opener=opener_raising(urllib.error.URLError("down")))
    assert down.category == "unreachable"


# ---------------------------------------------------------------------- stock Flashforge

def _stock_post(reply=None, exc=None, seen=None):
    def post(url, body, content_type):
        if seen is not None:
            seen.append((url, body))
        if exc is not None:
            raise exc
        return reply
    return post


def test_stock_probe_ok_validates_host_and_credentials_together():
    seen = []
    reply = {"code": 0, "detail": {"name": "Adventurer 5M Pro", "state": "ready"}}
    result = probe("flashforge_stock", {"host": "192.0.2.7"},
                   {"serial": "SN123", "check_code": "CC9"},
                   stock_post=_stock_post(reply, seen=seen))
    assert result.ok and "Adventurer 5M Pro" in result.summary
    url, body = seen[0]
    assert url == "http://192.0.2.7:8898/detail"
    assert body == {"serialNumber": "SN123", "checkCode": "CC9"}


def test_stock_probe_categories_and_secret_hygiene():
    missing = probe("flashforge_stock", {"host": "h"}, {"serial": "SN123"})
    assert missing.category == "missing_secret"
    refused = probe("flashforge_stock", {"host": "h"}, {"serial": "S", "check_code": "C"},
                    stock_post=_stock_post({"code": 1}))
    assert refused.category == "auth"
    busy = probe("flashforge_stock", {"host": "h"}, {"serial": "S", "check_code": "C"},
                 stock_post=_stock_post({"code": 2}))
    assert busy.category == "protocol" and "busy" in busy.error
    down = probe("flashforge_stock", {"host": "h"}, {"serial": "S", "check_code": "sekrit"},
                 stock_post=_stock_post(exc=OSError("boom sekrit")))
    assert down.category == "unreachable" and "sekrit" not in down.error


def test_probe_unknown_connection_is_a_config_result_not_an_exception():
    result = probe("offline", {})
    assert not result.ok and result.category == "config"


def test_probe_results_never_carry_secret_values():
    secret = "hunter2-hunter2"
    result = probe("octoprint", {"octoprint_url": "http://e"}, {"api_key": secret},
                   opener=opener_raising(urllib.error.URLError(f"down {secret}")))
    assert secret not in result.error and secret not in result.summary
