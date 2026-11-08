"""Behavioral parity for the stock-Klipper families: klipper.generic and sovol.* (Klipper).

Offline, recorded-response tests at the depth the sibling family suites use:
Moonraker status shapes are the ones tests/docker/fake_moonraker.py serves and
the ones print_handy/printers/base.py's basic_status is written against;
command construction is asserted as the exact requests the shared Moonraker
paths emit to a recording transport; the camera still is the listener's
_fetch_snapshot bounded-JPEG fetch plus Moonraker webcam-registry resolution
(print_handy/live_stream.py). No network, no printer.
"""
import importlib.util
import io
import json
import urllib.request
from pathlib import Path

import pytest

from print_handy import printers, settings
from print_handy.printers.klipper.adapter import KlipperAdapter

GENERIC = "klipper.generic"
KLIPPER_KEYS = tuple(sorted(k for k in printers.MODELS
                            if k == GENERIC or k.startswith("sovol.")))

# The fake Moonraker the docker acceptance runs against: its served payloads
# are the recorded Moonraker shapes for this family.
_FAKE_PATH = Path(__file__).parent.parent / "docker" / "fake_moonraker.py"
_spec = importlib.util.spec_from_file_location("fake_moonraker_fixture", _FAKE_PATH)
fake_moonraker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_moonraker)


def config_for(key, environ=None):
    env = {"PRINT_HANDY__PRINTER__MODEL": key, **(environ or {})}
    return settings.load(local=False, environ=env)


def adapter_for(key, cfg=None):
    return printers.get(key, cfg or config_for(key))


def status_raw(state, **overrides):
    raw = {"print_stats": {"state": state, "filename": "benchy.gcode",
                           "print_duration": 1200.0,
                           "info": {"current_layer": 12, "total_layer": 80}},
           "virtual_sdcard": {"progress": 0.25},
           "extruder": {"temperature": 214.8, "target": 215.0},
           "heater_bed": {"temperature": 59.9, "target": 60.0},
           "webhooks": {"state": "ready"}}
    raw.update(overrides)
    return raw


# --------------------------------------------------------------------------- resolution

@pytest.mark.parametrize("key", KLIPPER_KEYS)
def test_every_klipper_family_model_uses_the_stock_klipper_adapter(key):
    adapter = adapter_for(key)
    # Exact class: not the OctoPrint subclass, not a vendor fork.
    assert type(adapter) is KlipperAdapter
    assert adapter.connection == "moonraker"
    assert (adapter.slot_count, adapter.multi_slot) == (1, False)


def test_klipper_family_key_set_is_the_generic_and_sovol_klipper_models():
    assert GENERIC in KLIPPER_KEYS
    assert "sovol.sv06" in KLIPPER_KEYS and "sovol.sv08" in KLIPPER_KEYS
    assert not any(k.startswith("sovol_marlin.") for k in KLIPPER_KEYS)


# --------------------------------------------------------------------------- status parsing

def test_fake_moonraker_idle_status_normalizes_to_standby():
    out = adapter_for(GENERIC).normalize_status(fake_moonraker.IDLE)
    assert out["state"] == "standby"
    assert out["filename"] == "" and out["progress"] == 0.0
    assert out["hotend_c"] is None and out["bed_c"] is None


def test_fake_moonraker_served_query_payload_has_the_idle_status_shape():
    served = fake_moonraker.ROUTES["/printer/objects/query"]["status"]
    out = adapter_for("sovol.sv06").normalize_status(served)
    assert out["state"] == "standby"
    assert set(adapter_for("sovol.sv06").moonraker_objects()) >= {"print_stats", "virtual_sdcard"}


@pytest.mark.parametrize("state", ["printing", "paused", "complete", "cancelled", "error", "standby"])
def test_print_lifecycle_states_pass_through_normalize_status(state):
    out = adapter_for(GENERIC).normalize_status(status_raw(state))
    assert out["state"] == state
    assert out["filename"] == "benchy.gcode"
    assert out["progress"] == 0.25


@pytest.mark.parametrize("key", [GENERIC, "sovol.sv06", "sovol.sv08"])
def test_temperature_and_layer_report(key):
    out = adapter_for(key).normalize_status(status_raw("printing"))
    assert (out["hotend_c"], out["hotend_target_c"]) == (214.8, 215.0)
    assert (out["bed_c"], out["bed_target_c"]) == (59.9, 60.0)
    assert (out["layer"], out["total_layers"]) == (12, 80)
    # 1200 s elapsed at 25 % progress -> 3600 s remaining.
    assert out["remaining_min"] == 60


def test_remaining_estimate_only_while_printing_with_progress():
    adapter = adapter_for(GENERIC)
    assert adapter.normalize_status(status_raw("paused"))["remaining_min"] is None
    zero = status_raw("printing", virtual_sdcard={"progress": 0.0})
    assert adapter.normalize_status(zero)["remaining_min"] is None


def test_non_numeric_targets_and_layers_read_as_none_not_numbers():
    raw = status_raw("printing",
                     extruder={"temperature": 200.0, "target": True},
                     heater_bed={"temperature": 55.0, "target": "60"})
    raw["print_stats"]["info"] = {"current_layer": True, "total_layer": "80"}
    out = adapter_for(GENERIC).normalize_status(raw)
    assert out["hotend_target_c"] is None and out["bed_target_c"] is None
    assert out["layer"] is None and out["total_layers"] is None


def test_second_extruder_report_becomes_toolheads():
    raw = status_raw("printing", extruder1={"temperature": 199.5})
    out = adapter_for(GENERIC).normalize_status(raw)
    assert out["toolheads"] == [
        {"tool": 0, "name": "extruder", "temperature": 214.8},
        {"tool": 1, "name": "extruder1", "temperature": 199.5},
    ]


def test_exclude_object_lists_are_parsed_and_deduplicated():
    raw = status_raw("printing", exclude_object={
        "current_object": "gear.stl",
        "objects": [{"name": "gear.stl"}, {"name": "knob.stl"}, "gear.stl", {"no_name": 1}, 7],
        "excluded_objects": ["knob.stl", {"name": "knob.stl"}]})
    out = adapter_for(GENERIC).normalize_status(raw)
    assert out["current_object"] == "gear.stl"
    assert out["objects"] == ["gear.stl", "knob.stl"]
    assert out["excluded_objects"] == ["knob.stl"]


def test_exclude_object_defaults_are_present_and_empty_when_unreported():
    out = adapter_for(GENERIC).normalize_status(status_raw("standby"))
    assert out["current_object"] is None
    assert out["objects"] == [] and out["excluded_objects"] == []


def test_print_control_status_parses_the_fake_moonraker_query_shape(monkeypatch):
    from print_handy.hermes import print_control
    payload = {"result": {"status": {
        "print_stats": {"state": "printing", "filename": "a.gcode", "print_duration": 90.0,
                        "info": {"current_layer": 3, "total_layer": 40}},
        "virtual_sdcard": {"progress": 0.5}, "webhooks": {"state": "ready"}}}}
    monkeypatch.setattr(print_control, "_send", lambda path, **kw: payload)
    st = print_control.status()
    assert (st["state"], st["klippy"], st["filename"]) == ("printing", "ready", "a.gcode")
    assert (st["layer"], st["total_layer"], st["progress"]) == (3, 40, 0.5)


def test_print_control_status_falls_back_to_server_info_when_the_query_fails(monkeypatch):
    from print_handy.hermes import print_control

    def send(path, **kw):
        if path.startswith("/server/info"):
            return {"result": {"klippy_state": "shutdown"}}
        raise OSError("moonraker unreachable")

    monkeypatch.setattr(print_control, "_send", send)
    st = print_control.status()
    assert st["state"] == "unknown" and st["klippy"] == "shutdown" and st["filename"] == ""


# --------------------------------------------------------------------------- readiness

@pytest.mark.parametrize("key", KLIPPER_KEYS)
def test_readiness_accepts_ready_and_refuses_busy_or_unready(key):
    adapter = adapter_for(key)
    assert adapter.readiness_problems({}, status_raw("standby")) == []
    assert adapter.readiness_problems({}, status_raw("complete")) == []
    for busy in ("printing", "paused"):
        problems = adapter.readiness_problems({}, status_raw(busy))
        assert any("busy" in p for p in problems), (key, busy, problems)
    problems = adapter.readiness_problems({}, status_raw("standby", webhooks={"state": "shutdown"}))
    assert any("not ready" in p for p in problems)
    assert adapter.readiness_problems({}, {}), "missing print_stats must refuse"


# --------------------------------------------------------------------------- commands

@pytest.mark.parametrize("key", KLIPPER_KEYS)
def test_moonraker_objects_are_the_stock_query_set(key):
    assert adapter_for(key).moonraker_objects() == [
        "webhooks", "print_stats", "virtual_sdcard", "extruder", "heater_bed", "exclude_object"]


@pytest.mark.parametrize("key", [GENERIC, "sovol.sv06", "sovol.zero"])
def test_start_command_is_the_exact_moonraker_print_start_request(key):
    assert adapter_for(key).moonraker_start_commands("plate_1.gcode") == [
        {"method": "POST", "path": "/printer/print/start", "body": {"filename": "plate_1.gcode"}}]


def test_fake_moonraker_object_list_names_the_adapters_start_and_end_macros():
    listed = fake_moonraker.ROUTES["/printer/objects/list"]["objects"]
    adapter = adapter_for(GENERIC)
    start_macro = adapter.start_plan(60, 215)[0].split()[0]
    assert f"gcode_macro {start_macro}" in listed
    assert f"gcode_macro {adapter.machine_end_gcode('ignored')}" in listed


@pytest.mark.parametrize("key", [GENERIC, "sovol.sv08"])
def test_macro_names_come_from_settings_for_klipper_families(key):
    cfg = config_for(key)
    cfg["printer"].update(start_macro="START_MY_PRINT", end_macro="END_MY_PRINT")
    adapter = printers.get(key, cfg)
    assert adapter.start_plan(70, 240)[0] == "START_MY_PRINT BED=70 HOTEND=240"
    assert adapter.machine_start_gcode("ignored").startswith(
        "START_MY_PRINT BED=[bed_temperature_initial_layer_single]")
    assert adapter.machine_end_gcode("") == "END_MY_PRINT"


def _record_moonraker_posts(monkeypatch):
    from print_handy.webui import controls
    sent = []
    monkeypatch.setattr(controls, "_moonraker_post",
                        lambda view, path, payload=None: sent.append((path, payload)) or {})
    return controls, sent


@pytest.mark.parametrize("action,path", [("pause", "/printer/print/pause"),
                                         ("resume", "/printer/print/resume"),
                                         ("cancel", "/printer/print/cancel")])
def test_pause_resume_cancel_post_to_the_moonraker_print_paths(monkeypatch, action, path):
    controls, sent = _record_moonraker_posts(monkeypatch)
    view = {"services": {"moonraker_url": "http://printer.test:7125"}}
    controls.default_send(adapter_for(GENERIC), view, ("control", action))
    assert sent == [(path, None)]


def test_temperature_command_is_a_gcode_script_post(monkeypatch):
    controls, sent = _record_moonraker_posts(monkeypatch)
    view = {"services": {"moonraker_url": "http://printer.test:7125"}}
    controls.default_send(adapter_for("sovol.sv06"), view, ("temperature", "nozzle", 215.0))
    controls.default_send(adapter_for("sovol.sv06"), view, ("temperature", "bed", 60.0))
    assert sent == [("/printer/gcode/script", {"script": "M104 S215"}),
                    ("/printer/gcode/script", {"script": "M140 S60"})]


def test_multicolor_mapping_is_refused_for_a_sovol_klipper_model():
    from print_handy.printers.base import MultiColorUnsupported
    adapter = adapter_for("sovol.sv08")
    assert adapter.normalize_tool_mapping({"0": 1}, {}) == {"0": 1}
    with pytest.raises(MultiColorUnsupported):
        adapter.normalize_tool_mapping({"0": 1, "1": 2}, {})


# --------------------------------------------------------------------------- camera

# The Moonraker webcam-registry shape live_stream resolves (the same recorded
# shape tests/test_live_stream.py pins), tied here to this family.
WEBCAM_PAYLOAD = {"webcams": [{
    "name": "bedcam", "location": "printer", "service": "mjpegstreamer",
    "stream_url": "/webcam/?action=stream", "snapshot_url": "/webcam/?action=snapshot",
    "enabled": True}]}


def test_moonraker_webcam_registry_resolves_a_snapshot_url():
    from print_handy import live_stream
    source = live_stream.camera_source_from_moonraker(
        WEBCAM_PAYLOAD, base_url="http://printer.test:7125")
    assert source.snapshot_url == "http://printer.test:7125/webcam/?action=snapshot"
    assert source.stream_url == "http://printer.test:7125/webcam/?action=stream"
    assert source.kind == "mjpeg"


def test_moonraker_webcam_registry_without_a_usable_webcam_refuses():
    from print_handy import live_stream
    with pytest.raises(live_stream.LiveStreamError):
        live_stream.camera_source_from_moonraker({"webcams": []}, base_url="http://printer.test:7125")
    with pytest.raises(live_stream.LiveStreamError):
        live_stream.camera_source_from_moonraker(
            {"webcams": [{"name": "off", "snapshot_url": "http://cam.test/snap", "enabled": False}]},
            base_url="http://printer.test:7125")


JPEG = b"\xff\xd8\xff\xe0" + b"frame-body" + b"\xff\xd9"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _fetch_with(monkeypatch, payload_or_error):
    from print_handy.hermes import moonraker_event_listener as listener

    def opener(request, timeout=None):
        if isinstance(payload_or_error, Exception):
            raise payload_or_error
        return _Response(payload_or_error)

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    return listener._fetch_snapshot("http://printer.test/webcam/?action=snapshot")


def test_camera_snapshot_success_returns_the_jpeg_bytes(monkeypatch):
    assert _fetch_with(monkeypatch, JPEG) == JPEG


def test_camera_snapshot_non_jpeg_payload_is_a_named_refusal(monkeypatch):
    with pytest.raises(ValueError, match="not a bounded JPEG"):
        _fetch_with(monkeypatch, b"<html>camera offline</html>")


def test_camera_snapshot_oversized_payload_is_a_named_refusal(monkeypatch):
    oversized = b"\xff\xd8" + b"x" * (8 * 1024 * 1024)
    with pytest.raises(ValueError, match="not a bounded JPEG"):
        _fetch_with(monkeypatch, oversized)


def test_camera_snapshot_unreachable_camera_raises_instead_of_faking_a_frame(monkeypatch):
    with pytest.raises(OSError):
        _fetch_with(monkeypatch, OSError("connection refused"))


def test_recorded_command_bodies_are_json_shaped():
    # The start command dict is what model_job serializes onto the wire;
    # pin that it round-trips as JSON with no extra keys.
    cmd = adapter_for(GENERIC).moonraker_start_commands("a.gcode")[0]
    assert json.loads(json.dumps(cmd)) == cmd
    assert set(cmd) == {"method", "path", "body"}
