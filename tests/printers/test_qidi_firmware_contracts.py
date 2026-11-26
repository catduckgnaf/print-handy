"""Offline contract tests for QIDI firmware releases audited in #1906."""
import json
from pathlib import Path

import pytest

from print_handy import printers
from print_handy.runtime import spool_sync

CONTRACTS = json.loads(Path(__file__).with_name("qidi_firmware_contracts.json").read_text())
MODELS = {
    "QIDI Q1 Pro 6.1.1": "qidi.q1pro",
    "QIDI X-Plus 4 2.1.1": "qidi.xplus4",
}


@pytest.mark.parametrize("release,model_key", MODELS.items())
def test_qidi_release_documents_fluidd_filament_management_restriction(release, model_key):
    contract = CONTRACTS[release]
    assert contract["fluidd_filament_management_supported"] is False
    assert contract["fluidd_box_settings_writes_allowed"] is False
    assert contract["box_settings_interface"] == "QIDI Studio"
    assert contract["config_replaced"] is True
    assert contract["recalibration_required"] is True
    if model_key == "qidi.xplus4":
        assert "New BOX firmware supported" in contract["notes"]


@pytest.mark.parametrize("release,model_key", MODELS.items())
def test_qidi_adapter_refuses_fluidd_slot_writes(release, model_key, monkeypatch):
    adapter = printers.get(model_key)
    refusal = adapter.lane_data_write_refusal
    assert "does not support Fluidd filament management" in refusal
    assert "QIDI Studio" in refusal
    assert adapter.screen_command(1, "PLA", "FFFFFF") is None

    calls = []
    monkeypatch.setattr(spool_sync, "_lane_write_adapter", lambda printer_id: adapter)
    result = spool_sync.write_lane_side(
        1, {"id": 12, "material": "PLA", "name": "test spool"},
        printer_id="offline-qidi-test",
        mr=lambda *args, **kwargs: calls.append((args, kwargs)) or {"result": "ok"},
    )
    assert result == refusal
    assert calls == []


@pytest.mark.parametrize("release", CONTRACTS)
def test_qidi_release_contract_has_distinct_upstream_identity(release):
    contract = CONTRACTS[release]
    assert contract["tag"]
    assert len(contract["commit"]) == 40
    assert contract["released"] == "2026-11-18"
    assert contract["notes"]
