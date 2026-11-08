"""Behavioral parity for the Marlin-over-OctoPrint families: sovol_marlin.* and octoprint.marlin.

Registry fact these tests pin: every sovol_marlin.* model and octoprint.marlin
resolve to print_handy.printers.octoprint.adapter.OctoPrintAdapter (transport
"octoprint") — never to the Klipper/Moonraker adapter the same-named sovol.*
models use. Recorded responses are the OctoPrint shapes the adapter and client
code are written against (printer state flags, temperature block, job progress;
the classic single-webcam settings payload for the camera), replayed through a
recording opener so the exact requests are asserted. No network, no printer.
"""
import json

import pytest

from print_handy import printers, settings
from print_handy.printers.klipper.adapter import KlipperAdapter
from print_handy.printers.octoprint.adapter import OctoPrintAdapter
from print_handy.printers.octoprint.client import OctoPrintClient, OctoPrintError

MARLIN_KEYS = tuple(sorted(k for k in printers.MODELS
                           if k.startswith("sovol_marlin.") or k == "octoprint.marlin"))
BASE = "http://octo.test:5000"
KEY = "TESTKEY"


def config_for(key):
    return settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": key})


def adapter_for(key):
    return printers.get(key, config_for(key))


def octo_status(flags, completion=50.0, left=1800):
    return {"printer": {"state": {"flags": flags},
                        "temperature": {"tool0": {"actual": 199.6, "target": 215.0},
                                        "bed": {"actual": 59.8, "target": 60.0}}},
            "job": {"job": {"file": {"path": "benchy.gcode"}},
                    "progress": {"completion": completion, "printTimeLeft": left}}}


# --------------------------------------------------------------------------- resolution

@pytest.mark.parametrize("key", MARLIN_KEYS)
def test_every_marlin_model_uses_the_octoprint_adapter(key):
    adapter = adapter_for(key)
    assert type(adapter) is OctoPrintAdapter
    assert adapter.transport == "octoprint"
    assert adapter.connection == "octoprint"


def test_marlin_key_set_is_the_sovol_marlin_models_plus_the_generic_marlin():
    assert "octoprint.marlin" in MARLIN_KEYS
    assert "sovol_marlin.sv06" in MARLIN_KEYS and "sovol_marlin.sv01" in MARLIN_KEYS
    assert len(MARLIN_KEYS) == 9  # eight sovol_marlin.* models + octoprint.marlin


def test_same_named_sovol_models_split_by_firmware():
    klipper_sv06 = adapter_for("sovol.sv06")
    marlin_sv06 = adapter_for("sovol_marlin.sv06")
    assert type(klipper_sv06) is KlipperAdapter
    assert type(marlin_sv06) is OctoPrintAdapter
    assert klipper_sv06.connection == "moonraker"
    assert marlin_sv06.connection == "octoprint"
    # Same iron, same build volume; different transports and start sequences.
    assert klipper_sv06.build_volume() == marlin_sv06.build_volume()
    assert klipper_sv06.start_plan(60, 215) != marlin_sv06.start_plan(60, 215)


# --------------------------------------------------------------------------- start / slicer code

@pytest.mark.parametrize("key", MARLIN_KEYS)
def test_start_plan_is_the_marlin_temperature_waits(key):
    assert adapter_for(key).start_plan(60, 215) == ["M190 S60", "M109 S215"]
    assert adapter_for(key).start_plan(65.5, 240) == ["M190 S65.5", "M109 S240"]


@pytest.mark.parametrize("key", MARLIN_KEYS)
def test_slicer_owns_the_start_and_end_gcode(key):
    adapter = adapter_for(key)
    assert adapter.machine_start_gcode("SLICER START GCODE") == "SLICER START GCODE"
    assert adapter.machine_end_gcode("SLICER END GCODE") == "SLICER END GCODE"


@pytest.mark.parametrize("key", MARLIN_KEYS)
def test_moonraker_paths_are_refused_by_name(key):
    adapter = adapter_for(key)
    with pytest.raises(OctoPrintError, match="unsupported for OctoPrint"):
        adapter.moonraker_start_commands("part.gcode")
    with pytest.raises(OctoPrintError, match="unsupported for OctoPrint"):
        adapter.moonraker_objects()


# --------------------------------------------------------------------------- status parsing

@pytest.mark.parametrize("flags,state", [
    ({"printing": True}, "printing"),
    ({"paused": True}, "paused"),
    ({"paused": True, "printing": True}, "paused"),  # paused wins, as coded
    ({"ready": True, "operational": True}, "standby"),
    ({"ready": True, "operational": False}, "unknown"),
    ({"error": True}, "unknown"),
    ({}, "unknown"),
])
def test_octoprint_state_flags_normalize(flags, state):
    out = adapter_for("sovol_marlin.sv06").normalize_status(octo_status(flags))
    assert out["state"] == state


@pytest.mark.parametrize("key", ["sovol_marlin.sv01", "sovol_marlin.sv06_plus", "octoprint.marlin"])
def test_octoprint_temperature_progress_and_remaining_report(key):
    out = adapter_for(key).normalize_status(octo_status({"printing": True}))
    assert out["filename"] == "benchy.gcode"
    assert out["progress"] == 0.5  # completion percent -> fraction
    assert (out["hotend_c"], out["hotend_target_c"]) == (199.6, 215.0)
    assert (out["bed_c"], out["bed_target_c"]) == (59.8, 60.0)
    assert out["remaining_min"] == 30  # 1800 s, floored to whole minutes
    # Plain OctoPrint reports no layer numbers, so none are invented.
    assert "layer" not in out and "total_layers" not in out


@pytest.mark.parametrize("raw", [
    {},
    {"printer": {"temperature": {"tool0": {"target": None}, "bed": {}}},
     "job": {"progress": {"printTimeLeft": None, "completion": None}}},
    {"printer": {"temperature": {"tool0": {"target": True}, "bed": {"target": "60"}}},
     "job": {"progress": {"printTimeLeft": False, "completion": "50"}}},
])
def test_octoprint_absent_and_non_numeric_fields_read_as_none(raw):
    out = adapter_for("octoprint.marlin").normalize_status(raw)
    assert out["hotend_target_c"] is None and out["bed_target_c"] is None
    assert out["remaining_min"] is None and out["progress"] is None
    assert out["state"] == "unknown"


# --------------------------------------------------------------------------- readiness

def test_readiness_accepts_an_idle_ready_snapshot_and_names_each_refusal():
    adapter = adapter_for("sovol_marlin.sv06")
    assert adapter.readiness_problems({}, octo_status({"operational": True, "ready": True})) == []
    for flags in ({"operational": True, "ready": True, "printing": True},
                  {"operational": True, "ready": True, "paused": True},
                  {"operational": False, "ready": True},
                  {"operational": True, "ready": False},
                  {"operational": True, "ready": True, "error": True}):
        problems = adapter.readiness_problems({}, octo_status(flags))
        assert len(problems) == 1 and "not idle and ready" in problems[0], flags


# --------------------------------------------------------------------------- recorded transport

class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def recorded_adapter(key="sovol_marlin.sv06", payloads=()):
    """An adapter whose client talks to a recording opener, never a socket."""
    seen = []
    queue = list(payloads)

    def opener(request, timeout=None):
        seen.append(request)
        return _Response(queue.pop(0) if queue else b"{}")

    client = OctoPrintClient(BASE, KEY, opener=opener)
    adapter = adapter_for(key)
    adapter.client = lambda: client
    return adapter, seen


def request_shapes(seen):
    return [(r.get_method(), r.full_url, json.loads(r.data) if r.data else None) for r in seen]


@pytest.mark.parametrize("action,body", [
    ("pause", {"command": "pause", "action": "pause"}),
    ("resume", {"command": "pause", "action": "resume"}),
    ("stop", {"command": "cancel"}),  # the adapter translates stop -> OctoPrint's cancel
])
def test_control_actions_emit_the_exact_job_requests(action, body):
    adapter, seen = recorded_adapter()
    adapter.control(action)
    assert request_shapes(seen) == [("POST", f"{BASE}/api/job", body)]
    assert seen[0].get_header("X-api-key") == KEY


@pytest.mark.parametrize("action", ["cancel", "emergency_stop", "restart"])
def test_unsupported_control_words_are_refused_before_any_request(action):
    adapter, seen = recorded_adapter()
    with pytest.raises(OctoPrintError, match="unsupported"):
        adapter.control(action)
    assert seen == []


def test_send_gcode_posts_one_command_entry_per_nonblank_line():
    adapter, seen = recorded_adapter()
    adapter.send_gcode(["G28", "", "  M104 S200  ", "G1 X10"])
    assert request_shapes(seen) == [
        ("POST", f"{BASE}/api/printer/command", {"commands": ["G28", "M104 S200", "G1 X10"]})]


def test_send_gcode_with_no_commands_sends_nothing():
    adapter, seen = recorded_adapter()
    with pytest.raises(OctoPrintError, match="no G-code commands"):
        adapter.send_gcode(["", "   "])
    assert seen == []


def test_start_flow_heats_then_selects_and_starts_the_file():
    ready = json.dumps({"state": {"flags": {"operational": True, "ready": True}}}).encode()
    adapter, seen = recorded_adapter(payloads=[ready, b"{}", b"{}", b"{}"])
    bed, nozzle = adapter.start_plan(60, 215)
    adapter.client().start("part.gcode", 60, 215)
    assert request_shapes(seen) == [
        ("GET", f"{BASE}/api/printer", None),
        ("GET", f"{BASE}/api/job", None),
        ("POST", f"{BASE}/api/printer/command", {"commands": [bed, nozzle]}),
        ("POST", f"{BASE}/api/files/local/part.gcode", {"command": "select", "print": True}),
    ]


def test_start_flow_refuses_a_busy_printer_before_any_mutation():
    busy = json.dumps({"state": {"flags": {"operational": True, "ready": True,
                                           "printing": True}}}).encode()
    adapter, seen = recorded_adapter(payloads=[busy, b"{}"])
    with pytest.raises(OctoPrintError, match="not idle"):
        adapter.client().start("part.gcode", 60, 215)
    assert all(r.get_method() == "GET" for r in seen)


def test_recorded_status_round_trips_through_the_adapter():
    payloads = [json.dumps({"state": {"flags": {"printing": True}},
                            "temperature": {"tool0": {"actual": 210.0, "target": 215.0},
                                            "bed": {"actual": 60.0, "target": 60.0}}}).encode(),
                json.dumps({"job": {"file": {"path": "part.gcode"}},
                            "progress": {"completion": 25.0, "printTimeLeft": 3600}}).encode()]
    adapter, seen = recorded_adapter(payloads=payloads)
    out = adapter.normalize_status(adapter.client().status())
    assert [r.full_url for r in seen] == [f"{BASE}/api/printer", f"{BASE}/api/job"]
    assert out["state"] == "printing" and out["filename"] == "part.gcode"
    assert out["progress"] == 0.25 and out["remaining_min"] == 60


def test_failed_request_is_not_retried_and_does_not_leak_the_key():
    calls = []

    def opener(request, timeout=None):
        calls.append(request)
        raise OSError(f"connection refused for {KEY}")

    adapter = adapter_for("sovol_marlin.sv06")
    adapter.client = lambda: OctoPrintClient(BASE, KEY, opener=opener)
    with pytest.raises(OctoPrintError) as excinfo:
        adapter.control("pause")
    assert "not retried" in str(excinfo.value) and KEY not in str(excinfo.value)
    assert len(calls) == 1


# --------------------------------------------------------------------------- camera

def test_octoprint_webcam_settings_resolve_stream_and_snapshot():
    from print_handy import live_stream
    payload = {"webcam": {"streamUrl": "/?action=stream", "snapshotUrl": "/?action=snapshot",
                          "webcamEnabled": True}}
    source = live_stream.camera_source_from_octoprint(payload, base_url="http://octo.test")
    assert source.snapshot_url == "http://octo.test/?action=snapshot"
    assert source.stream_url == "http://octo.test/?action=stream"
    assert source.kind == "mjpeg"


def test_octoprint_snapshot_only_payload_derives_the_stream():
    from print_handy import live_stream
    payload = {"webcam": {"snapshotUrl": "http://octo.test/?action=snapshot", "streamUrl": ""}}
    source = live_stream.camera_source_from_octoprint(payload, base_url="http://octo.test")
    assert source.stream_url == "http://octo.test/?action=stream"


def test_octoprint_disabled_or_absent_webcam_refuses_without_a_fallback():
    from print_handy import live_stream
    with pytest.raises(live_stream.LiveStreamError):
        live_stream.camera_source_from_octoprint(
            {"webcam": {"streamUrl": "http://cam.test/stream", "webcamEnabled": False}},
            base_url="http://octo.test")
    with pytest.raises(live_stream.LiveStreamError):
        live_stream.camera_source_from_octoprint({}, base_url="http://octo.test")
