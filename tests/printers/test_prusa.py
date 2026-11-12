"""Prusa vendor (#242): models, settings, generated profiles, PrusaLink client, adapter, guard.

Everything is offline: the client runs against a fake opener, profiles against invented
resources (prusa_fixture), and no test touches a network or a printer.
"""
import io
import json
import re
from pathlib import Path

import pytest

from print_handy import printers, settings
from print_handy.printers.prusa import VENDOR, facts, profiles
from print_handy.printers.prusa.adapter import PrusaAdapter
from print_handy.printers.prusa.client import PrusaLinkClient, PrusaLinkError, _Response

from . import prusa_fixture as fixture

MODELS = {
    "prusa.mk4s": "MK4S",
    "prusa.coreone": "COREONE",
    "prusa.xl": "XL",
    "prusa.miniplus": "MINIPLUS",
}


def model_facts(key):
    return getattr(facts, MODELS[key])


def config_for(key, hermes=fixture.HERMES, environ=None, **printer):
    env = {"PRINT_HANDY__PRINTER__MODEL": key, "PRINT_HANDY__PATHS__STATE_HOME": str(hermes),
           **(environ or {}),
           **{f"PRINT_HANDY__PRINTER__{k.upper()}": str(v) for k, v in printer.items()}}
    return settings.load(local=False, environ=env)


def adapter_for(key, cfg=None):
    return printers.get(key, cfg or config_for(key))


# --------------------------------------------------------------------------- models + settings

@pytest.mark.parametrize("key", sorted(MODELS))
def test_model_registration_and_defaults(key):
    model = printers.MODELS[key]
    assert model.vendor is VENDOR is printers.VENDORS["prusa"]
    assert model.generated_templates is True
    a = adapter_for(key)
    assert isinstance(a, PrusaAdapter)
    assert (a.connection, a.transport, a.multi_slot) == ("prusalink", "prusalink", False)
    # The XL's positions are its five toolheads (#670); the other Prusa models have one path.
    assert a.slot_count == (5 if key == "prusa.xl" else 1)
    assert a.multi_tool == (key == "prusa.xl")
    f = model_facts(key)
    volume = a.build_volume()
    assert volume.bed_mm == tuple(float(v) for v in f["bed_mm"].value)
    assert volume.max_height_mm == float(f["max_print_height_mm"].value)
    cfg = config_for(key)
    assert cfg["printer"]["slicer"] == "orca" and cfg["printer"]["firmware"] == "Prusa Buddy (PrusaLink)"
    assert cfg["delivery"]["methods"] == ["discord"]  # printer delivery is opt-in, never a default
    with pytest.raises(settings.SettingsError, match="not supported by model"):
        config_for(key, nozzle_diameter_mm=0.6)


def test_prusa_settings_validation():
    cfg = config_for("prusa.mk4s", environ={"PRINT_HANDY__PRUSA__LINK_HOST": "192.168.1.70"})
    assert cfg["prusa"] == {"link_host": "192.168.1.70", "api_key_env": "PRUSA_LINK_API_KEY"}
    with pytest.raises(settings.SettingsError, match="only applies to Prusa"):
        config_for("sovol.sv06", environ={"PRINT_HANDY__PRUSA__LINK_HOST": "192.168.1.70"})
    with pytest.raises(settings.SettingsError, match="plain IP address or hostname"):
        config_for("prusa.mk4s", environ={"PRINT_HANDY__PRUSA__LINK_HOST": "http://192.168.1.70"})
    with pytest.raises(settings.SettingsError, match="moonraker_url and octoprint_url must be empty"):
        config_for("prusa.mk4s", environ={"PRINT_HANDY__SERVICES__MOONRAKER_URL": "http://x:7125"})


# --------------------------------------------------------------------------- profiles

@pytest.mark.parametrize("key", sorted(MODELS))
def test_recorded_identity_is_a_real_pin(key):
    f = model_facts(key)
    entries = f["orca_identity"].value
    # One pin per checked packaging (#356): 2.4.2's tree, then 2.5.0-alpha's and 2.5.0-beta's tree and packed cache (beta kept the vendor version), then the 2.5.0 final's tree and packed cache (the final moved the vendor version; the release candidate matched the beta's entries, so it records none of its own).
    assert [known["vendor_version"] for known in entries] == ["02.04.00.03", "02.04.00.17", "02.04.00.17", "02.04.00.17", "02.04.00.17", "02.04.00.18", "02.04.00.18"]
    for known in entries:
        assert all(re.fullmatch(r"[0-9a-f]{64}", d) for d in known["sha256"].values())
    assert all(fixture.identity(f)["sha256"] != known["sha256"] for known in entries)


@pytest.mark.parametrize("key", sorted(MODELS))
def test_fixture_profile_install_joins_gcode_lists(key, monkeypatch):
    f = model_facts(key)
    a = adapter_for(key)
    fixture.trust(type(a), f, monkeypatch.setattr)
    folder = fixture.install(a, f)
    assert profiles.problems(a) == []
    machine = json.loads((folder / "machine.json").read_text())
    # The real Prusa profiles store start G-code as a list; templates carry the joined string.
    assert isinstance(machine["machine_start_gcode"], str) and "G28" in machine["machine_start_gcode"]
    upstream = json.loads((folder / "upstream" / "machine.json").read_text())
    assert isinstance(upstream["machine_start_gcode"], list)  # upstream copy stays the vendor's form
    assert a.machine_start_gcode(machine["machine_start_gcode"]) == machine["machine_start_gcode"]
    assert a.start_plan(60, 215)[0] == "G90"


# --------------------------------------------------------------------------- client (fake transport)

class FakePrusaLink:
    """An in-memory PrusaLink: records requests, serves canned status, stores uploads."""

    def __init__(self, state="IDLE", job=None):
        self.requests = []
        self.state = state
        self.job = job or {}
        self.files = {}

    def __call__(self, req, timeout=None):
        url, method = req.full_url, req.get_method()
        body = req.data or b""
        headers = {k.lower(): v for k, v in req.headers.items()}
        self.requests.append((method, url, headers, body))
        path = url.split(".local", 1)[-1] if ".local" in url else url[url.index("/api"):]
        if path == "/api/v1/info":
            return _Raw(200, {"name": "Fake", "serial": "FAKE1"})
        if path == "/api/v1/status":
            printer = {"state": self.state, "temp_nozzle": 25.0, "target_nozzle": 0.0,
                       "temp_bed": 24.0, "target_bed": 0.0}
            job = {"id": self.job.get("id"), "progress": self.job.get("progress", 0.0),
                   "time_remaining": self.job.get("time_remaining", 0)}
            return _Raw(200, {"printer": printer, "job": job, "storage": "usb"})
        if path == "/api/v1/job" and method == "GET":
            if not self.job:
                return _Raw(204, None)
            return _Raw(200, self.job)
        if path == "/api/v1/storage":
            return _Raw(200, {"storage_list": [
                {"name": "local", "path": "/local", "available": True, "read_only": True},
                {"name": "usb", "path": "/usb", "available": True, "read_only": False}]})
        if path.startswith("/api/v1/files/usb"):
            name = path.rsplit("/", 1)[-1]
            if method == "PUT":
                self.files[name] = body
                return _Raw(201, {})
            if method == "GET" and path == "/api/v1/files/usb":
                return _Raw(200, {"children": [{"name": n, "display_name": n, "size": len(b), "type": "file"}
                                               for n, b in self.files.items()]})
            if method == "POST":
                if name not in self.files:
                    return _Raw(404, {})
                self.state, self.job = "PRINTING", {"id": 7, "progress": 0.0,
                                                    "file": {"name": name, "display_name": name}}
                return _Raw(204, None)
        if path.startswith("/api/v1/job/") and method == "PUT":
            return _Raw(204, None)
        if path.startswith("/api/v1/job/") and method == "DELETE":
            self.state, self.job = "IDLE", {}
            return _Raw(204, None)
        return _Raw(404, {})


class _Raw:
    def __init__(self, status, payload):
        self.status = status
        self.headers = {}
        self._body = b"" if payload is None else json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def client_for(fake):
    return PrusaLinkClient("printer.local", "test-key", opener=fake)


def test_client_malformed_json_is_a_prusalink_error():
    # A non-JSON answer (a captive portal, a different service on the
    # host) must surface as a PrusaLinkError, not a raw JSONDecodeError
    # escaping the client (P2-214).
    class _RawBody:
        def __init__(self, status, body):
            self.status, self.headers, self._body = status, {}, body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    client = PrusaLinkClient("printer.local", "test-key",
                             opener=lambda req, timeout=None: _RawBody(200, b"<html>not json</html>"))
    with pytest.raises(PrusaLinkError, match="malformed JSON"):
        client.info()


def test_client_status_snapshot_and_storage_choice():
    fake = FakePrusaLink()
    client = client_for(fake)
    snap = client.snapshot()
    assert snap["status"]["printer"]["state"] == "IDLE" and snap["job"] == {}
    assert client.storage() == "usb"  # USB preferred over the read-only local storage
    auth = fake.requests[0][2].get("x-api-key")
    assert auth == "test-key"


def test_client_upload_verifies_size_then_start_and_control(tmp_path):
    fake = FakePrusaLink()
    client = client_for(fake)
    gcode = tmp_path / "benchy.gcode"
    gcode.write_bytes(b"G28\nG1 X10 Y10 E1\n")
    assert client.upload(gcode) == "benchy.gcode"
    put = [r for r in fake.requests if r[0] == "PUT"][0]
    assert put[2].get("print-after-upload") == "?0"  # upload alone never starts
    assert client.start_print("benchy.gcode") == "benchy.gcode"
    assert fake.state == "PRINTING"
    assert client.control("pause", job_id=7) is True
    assert client.control("stop", job_id=7) is True
    with pytest.raises(PrusaLinkError, match="unsupported"):
        client.control("explode", job_id=7)


def test_client_upload_size_mismatch_refuses(tmp_path):
    fake = FakePrusaLink()
    client = client_for(fake)
    gcode = tmp_path / "x.gcode"
    gcode.write_bytes(b"G28\n")
    # Corrupt the stored copy so the readback size disagrees.
    original = fake.__call__

    def corrupting(req, timeout=None):
        resp = original(req, timeout=timeout)
        if req.get_method() == "PUT":
            for name in list(fake.files):
                fake.files[name] = fake.files[name] + b"junk"
        return resp

    client.opener = corrupting
    with pytest.raises(PrusaLinkError, match="could not be verified"):
        client.upload(gcode)


def test_client_rejects_bad_inputs(tmp_path):
    with pytest.raises(PrusaLinkError, match="plain printer IP"):
        PrusaLinkClient("http://printer.local", "k")
    with pytest.raises(PrusaLinkError, match="API key is required"):
        PrusaLinkClient("printer.local", "")
    client = client_for(FakePrusaLink())
    bad = tmp_path / "model.stl"
    bad.write_bytes(b"x")
    with pytest.raises(PrusaLinkError, match=r"\.gcode or \.bgcode"):
        client.upload(bad)
    with pytest.raises(PrusaLinkError, match="invalid PrusaLink remote file name"):
        client.upload(tmp_path / "ok.gcode", "../escape.gcode") if (tmp_path / "ok.gcode").write_bytes(b"G28\n") else None


# --------------------------------------------------------------------------- adapter

def test_adapter_unconfigured_and_status_normalization(monkeypatch):
    a = adapter_for("prusa.mk4s")
    assert a.link_configured() is False
    with pytest.raises(PrusaLinkError, match="PrusaLink is not configured"):
        a.client()
    snap = {"status": {"printer": {"state": "PRINTING", "temp_nozzle": 215.0, "target_nozzle": 215.0,
                                    "temp_bed": 60.0, "target_bed": 60.0},
                       "job": {"id": 3, "progress": 0.5, "time_remaining": 600}},
            "job": {"id": 3, "progress": 0.5, "time_remaining": 600,
                    "file": {"name": "benchy.gcode", "display_name": "benchy.gcode"}}}
    out = a.normalize_status(snap)
    assert out["state"] == "printing" and out["progress"] == 0.5 and out["filename"] == "benchy.gcode"
    assert out["hotend_c"] == 215.0 and out["remaining_min"] == 10 and out["job_id"] == 3
    assert a.normalize_status({})["state"] == "unknown"
    record = {"bed_mm": [250.0, 210.0], "max_print_height_mm": 220.0, "nozzle": {"diameter_mm": 0.4}}
    busy = {"status": {"printer": {"state": "PRINTING"}, "job": {}}}
    assert any("busy" in p for p in a.readiness_problems(record, busy))
    assert a.readiness_problems(record, {}) == []


def test_adapter_env_api_key(monkeypatch):
    monkeypatch.setenv("PRUSA_LINK_API_KEY", "from-env")
    cfg = config_for("prusa.coreone", environ={"PRINT_HANDY__PRUSA__LINK_HOST": "192.168.1.70"})
    a = adapter_for("prusa.coreone", cfg)
    assert a.link_configured() is True and a.api_key() == "from-env"
    client = a.client()
    assert client.base == "http://192.168.1.70"


# --------------------------------------------------------------------------- guard

def test_guard_refusals_and_acceptance():
    a = adapter_for("prusa.mk4s")
    good = ("; printer_model = Prusa MK4S\n; nozzle_diameter = 0.4\n"
            "M140 S60\nM104 S215\nG90\nM83\nG28\n; CHANGE_LAYER\nG1 Z0.2\n"
            "G1 X10 Y10 E1\n; FEATURE: Custom\nM104 S0\n")
    assert a.gcode_problems(good) == []
    assert any("another vendor" in p for p in a.gcode_problems("PRINT_START BED=60\n" + good))
    assert any("sliced for" in p for p in a.gcode_problems(good.replace("Prusa MK4S", "Prusa XL 5T")))
    assert any("single-tool" in p for p in a.gcode_problems(good + "T1\n"))
    assert any("exceeds" in p for p in a.gcode_problems(good + "M104 S300\n"))
    assert any("nozzle" in p for p in a.gcode_problems(good.replace("= 0.4", "= 0.6")))


def test_guard_checks_motion_bounds_for_standalone_gcode():
    # P1-63: standalone or legacy G-code reaches this guard without passing
    # through slicing, so the guard itself must hold the toolpath to the
    # model's bed, motion envelope and height before accepting it.
    a = adapter_for("prusa.mk4s")
    good = ("; printer_model = Prusa MK4S\n; nozzle_diameter = 0.4\n"
            "M140 S60\nM104 S215\nG90\nM83\nG28\n; CHANGE_LAYER\nG1 Z0.2\n"
            "G1 X10 Y10 E1\n; FEATURE: Custom\nM104 S0\n")
    assert a.gcode_problems(good) == []
    # The Codex case: a raw far-off-bed move in an otherwise bare file.
    raw = ("; printer_model = Prusa MK4S\n; nozzle_diameter = 0.4\n"
           "G1 X500 Y500 E1\n")
    assert a.gcode_problems(raw) != []
    # Off-bed and over-height toolpath moves in a well-formed file are refused.
    off_bed = good.replace("G1 X10 Y10 E1", "G1 X500 Y500 E1")
    assert any("envelope" in p or "bed" in p for p in a.gcode_problems(off_bed))
    too_tall = good.replace("G1 Z0.2", "G1 Z300")
    assert any("Z300" in p for p in a.gcode_problems(too_tall))


# --------------------------------------------------------------------------- #670: the XL as multi_tool

def test_xl_tool_mapping_is_identity_only():
    from print_handy.printers.base import MultiColorUnsupported
    a = adapter_for("prusa.xl")
    assert a.tool_count == 5 and a.slot_count == 5 and a.multi_tool is True
    assert a.normalize_tool_mapping({}, None) == {"0": 1}
    assert a.normalize_tool_mapping({"0": 1, "2": 3, "4": 5}, None) == {"0": 1, "2": 3, "4": 5}
    with pytest.raises(MultiColorUnsupported, match="always prints with toolhead 2"):
        a.normalize_tool_mapping({"1": 1}, None)  # a remap cannot be represented
    with pytest.raises(MultiColorUnsupported, match="has 5 toolheads"):
        a.normalize_tool_mapping({"5": 6}, None)  # a sixth tool does not exist
    with pytest.raises(MultiColorUnsupported, match="not a tool number"):
        a.normalize_tool_mapping({"x": 1}, None)
    # The single-tool models keep the refusal they have always had.
    mk4s = adapter_for("prusa.mk4s")
    assert mk4s.normalize_tool_mapping({"0": 1}, None) == {"0": 1}
    with pytest.raises(MultiColorUnsupported, match="multi-tool jobs are not supported"):
        mk4s.normalize_tool_mapping({"1": 2}, None)


def test_xl_guard_accepts_five_tools_and_refuses_a_sixth():
    a = adapter_for("prusa.xl")
    # A multi-tool XL file as Orca writes it: per-toolhead nozzle list in the
    # footer, per-tool temperatures, bare T picks with the changer's parameters.
    good = ("; printer_model = Prusa XL 5T\n; nozzle_diameter = 0.4,0.4,0.4,0.4,0.4\n"
            "M140 S60\nM104 S215 T0\nM104 S220 T3\nG90\nM83\nG28\nT0 S1 L0 D0\n; CHANGE_LAYER\n"
            "G1 Z0.2\nG1 X10 Y10 E1\n"
            "T3 S1 L0 D0\nG1 X20 Y20 E1\n; FEATURE: Custom\nM104 S0 T0\nM104 S0 T3\n")
    assert a.gcode_problems(good) == []
    assert any("has 5 toolheads" in p for p in a.gcode_problems(good + "T5\n"))
    assert any("nozzle" in p for p in a.gcode_problems(good.replace("0.4,0.4,0.4,0.4,0.4", "0.4,0.6,0.4,0.4,0.4")))
    # A single-tool XL job still verifies, and the MK4S still refuses any T1+.
    assert a.gcode_problems(good.replace("0.4,0.4,0.4,0.4,0.4", "0.4").replace("T3 S1 L0 D0\n", "")) == []
    mk4s = adapter_for("prusa.mk4s")
    assert any("single-tool" in p for p in mk4s.gcode_problems(
        "; printer_model = Prusa MK4S\n; nozzle_diameter = 0.4\nT4\n"))


def test_xl_tool_count_flows_through_settings():
    cfg = config_for("prusa.xl")
    assert cfg["printer"]["tool_count"] == 5 and cfg["printer"]["filament_slots"] == 1
    # tool_count is a real setting on the multi_tool class (the Creator surface):
    # an operator value within the class loads; the same key on a single-tool
    # Prusa model is refused by the class validation.
    assert config_for("prusa.xl", tool_count=5)["printer"]["tool_count"] == 5
    with pytest.raises(settings.SettingsError):
        config_for("prusa.mk4s", tool_count=5)


# --- #391 stage 2: timeout mid-upload ---------------------------------------


def test_client_upload_timeout_is_not_retried_and_starts_nothing(tmp_path):
    fake = FakePrusaLink()
    gcode = tmp_path / "benchy.gcode"
    gcode.write_bytes(b"G28\n")
    calls = []

    def timing_out(req, timeout=None):
        calls.append(req.get_method())
        raise TimeoutError("timed out for test-key")

    client = client_for(fake)
    client.opener = timing_out
    with pytest.raises(PrusaLinkError) as err:
        client.upload(gcode)
    assert "not retried" in str(err.value)
    assert "test-key" not in str(err.value)
    # The storage lookup GET fails soft to the default storage first; the
    # PUT itself is attempted exactly once — no retry, no verify, no start.
    assert calls == ["GET", "PUT"]
    assert fake.state == "IDLE" and fake.files == {}
