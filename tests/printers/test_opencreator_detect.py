"""Tests for #1490 Tranche 1: OpenCreator vs ZMod firmware identity and mismatch messages."""
from __future__ import annotations

import pytest

from print_handy import doctor, printers, settings
from print_handy.launch import gates
from print_handy.printers.flashforge.opencreator_detect import (
    detect_creator_firmware,
    zmod_opencreator_mismatch_message,
)


def _creator_adapter(model: str = "flashforge.creator5"):
    cfg = settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": model})
    return printers.get(model, cfg)


def _base_status(**overrides):
    status = {
        "print_stats": {"state": "standby"},
        "webhooks": {"state": "ready"},
        "extruder": {"temperature": 25.0},
        "extruder1": {"temperature": 25.0},
        "extruder2": {"temperature": 25.0},
        "extruder3": {"temperature": 25.0},
        "zmod_color": {"active_tool_id": 0},
    }
    status.update(overrides)
    return status


# -- 1. Pure object-list detection --------------------------------------------

def test_detect_creator_firmware_cases():
    oc = detect_creator_firmware(["webhooks", "print_stats", "creator5_toolchanger", "AFC"])
    assert oc.firmware == "opencreator"
    assert oc.has_opencreator and not oc.has_zmod
    assert "AFC present" in oc.secondary_evidence

    zmod = detect_creator_firmware(["webhooks", "print_stats", "zmod_color"])
    assert zmod.firmware == "zmod"
    assert zmod.has_zmod and not zmod.has_opencreator

    neither = detect_creator_firmware(["webhooks", "print_stats", "extruder"])
    assert neither.firmware == "unknown"
    assert not neither.has_opencreator and not neither.has_zmod

    both = detect_creator_firmware(["webhooks", "creator5_toolchanger", "zmod_color"])
    assert both.firmware == "both"
    assert both.has_opencreator and both.has_zmod


def test_secondary_evidence_only_affects_wording_never_verdict():
    unknown = detect_creator_firmware(
        ["webhooks", "print_stats"],
        update_components=["opencreator-updater"],
        server_components=["afc"],
    )
    assert unknown.firmware == "unknown"
    assert "update manager lists opencreator-updater" in unknown.evidence_note
    assert zmod_opencreator_mismatch_message(unknown) is None

    oc = detect_creator_firmware(
        ["creator5_toolchanger"],
        update_components=["opencreator"],
    )
    msg = zmod_opencreator_mismatch_message(oc)
    assert msg is not None
    assert "OpenCreator" in msg
    assert "update manager lists opencreator" in msg


# -- 2. ZmodMultiToolAdapter.readiness_problems -------------------------------

@pytest.mark.parametrize("model", ["flashforge.creator5", "flashforge.creator5_pro"])
def test_readiness_mismatch_message_for_all_object_states(model):
    adapter = _creator_adapter(model)
    no_zmod = _base_status()
    del no_zmod["zmod_color"]

    oc_probs = adapter.readiness_problems(
        {}, no_zmod, objects_list=["webhooks", "print_stats", "creator5_toolchanger"]
    )
    assert len(oc_probs) == 1
    assert "OpenCreator" in oc_probs[0]
    assert "creator5_toolchanger" in oc_probs[0]
    assert "zmod_color" in oc_probs[0]
    assert "[printer] model" in oc_probs[0]

    zmod_probs = adapter.readiness_problems(
        {}, no_zmod, objects_list=["webhooks", "print_stats", "zmod_color"]
    )
    assert zmod_probs == ["zmod_color is not reporting (the active tool is unknown)"]

    neither_probs = adapter.readiness_problems(
        {}, no_zmod, objects_list=["webhooks", "print_stats"]
    )
    assert neither_probs == ["zmod_color is not reporting (the active tool is unknown)"]

    both_probs = adapter.readiness_problems(
        {}, no_zmod, objects_list=["creator5_toolchanger", "zmod_color"]
    )
    assert len(both_probs) == 1
    assert "creator5_toolchanger" in both_probs[0] and "zmod_color" in both_probs[0]


# -- 3. gates._verify_readiness_adapter: zero extra requests when healthy -----

def test_gates_makes_no_extra_moonraker_request_on_healthy_zmod(monkeypatch):
    adapter = _creator_adapter("flashforge.creator5")
    calls = []

    def fake_request_json(url, *args, **kwargs):
        calls.append(url)
        if "/printer/objects/query" in url:
            return {"result": {"status": _base_status()}}
        raise AssertionError(f"unexpected extra Moonraker request: {url}")

    monkeypatch.setattr(gates, "request_json", fake_request_json)
    gates._verify_readiness_adapter(adapter, {"moonraker_url": "http://printer.test:7125"})
    assert len(calls) == 1
    assert "/printer/objects/query" in calls[0]


def test_gates_probes_objects_list_on_missing_zmod_color_and_names_opencreator(monkeypatch):
    adapter = _creator_adapter("flashforge.creator5_pro")
    no_zmod = _base_status()
    del no_zmod["zmod_color"]
    calls = []

    def fake_request_json(url, *args, **kwargs):
        calls.append(url)
        if "/printer/objects/query" in url:
            return {"result": {"status": no_zmod}}
        if "/printer/objects/list" in url:
            return {"result": {"objects": ["webhooks", "print_stats", "creator5_toolchanger"]}}
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(gates, "request_json", fake_request_json)
    with pytest.raises(RuntimeError, match="OpenCreator"):
        gates._verify_readiness_adapter(adapter, {"moonraker_url": "http://printer.test:7125"})
    assert any("/printer/objects/list" in c for c in calls)


# -- 4. doctor.check_firmware_identity ----------------------------------------

def _creator_roster_settings(model="flashforge.creator5"):
    return {
        "printer": {"model": model, "name": "Creator", "bed_mm": [256, 256],
                    "max_print_height_mm": 256, "nozzle_diameter_mm": 0.4,
                    "tool_count": 4, "filament_slots": 1},
        "services": {"moonraker_url": "http://creator.test:7125"},
        "printers": [{"id": "c5", "name": "Creator", "model": model,
                      "services": {"moonraker_url": "http://creator.test:7125"}}],
    }


def test_doctor_check_firmware_identity_skips_non_creator():
    data = {
        "printer": {"model": "flashforge.ad5x"},
        "services": {"moonraker_url": "http://ad5x.test:7125"},
        "printers": [{"id": "ad5x", "name": "AD5X", "model": "flashforge.ad5x"}],
    }
    assert doctor.check_firmware_identity(data) is None


def test_doctor_check_firmware_identity_reports_zmod_and_opencreator_mismatch(monkeypatch):
    data = _creator_roster_settings()

    monkeypatch.setattr(
        doctor.moonraker,
        "moonraker_json",
        lambda base, path, **kw: {"result": {"objects": ["webhooks", "zmod_color"]}},
    )
    ok_line = doctor.check_firmware_identity(data)
    assert "c5: flashforge.creator5 -> zmod" in ok_line

    monkeypatch.setattr(
        doctor.moonraker,
        "moonraker_json",
        lambda base, path, **kw: {"result": {"objects": ["webhooks", "creator5_toolchanger"]}},
    )
    with pytest.raises(RuntimeError, match="OpenCreator"):
        doctor.check_firmware_identity(data)

    def _unreachable(base, path, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(doctor.moonraker, "moonraker_json", _unreachable)
    offline_line = doctor.check_firmware_identity(data)
    assert "Moonraker unreachable" in offline_line
