"""Unit tests for classify_start_readback and start readback integration (Issue #84)."""
import pytest
from print_handy.core.model_job import classify_start_readback, StartReadback


def test_classify_failed_error():
    snap = {"print_stats": {"state": "error"}}
    rb = classify_start_readback(snap)
    assert isinstance(rb, StartReadback)
    assert rb.outcome == "failed"
    assert "state" in rb.evidence
    assert "failed" in rb.message


def test_classify_failed_cancelled():
    snap = {"result": {"status": {"print_stats": {"state": "cancelled"}}}}
    rb = classify_start_readback(snap)
    assert rb.outcome == "failed"
    assert rb.evidence["state"] == "cancelled"


def test_classify_failed_webhooks_shutdown():
    snap = {"webhooks": {"state": "shutdown"}}
    rb = classify_start_readback(snap)
    assert rb.outcome == "failed"
    assert rb.evidence["state"] == "shutdown"


def test_classify_printing_with_filament_used():
    snap = {
        "print_stats": {
            "state": "printing",
            "filament_used": 12.5,
            "info": {"current_layer": 2, "total_layer": 100},
        }
    }
    rb = classify_start_readback(snap)
    assert rb.outcome == "printing"
    assert rb.message == "printing"
    assert rb.evidence["filament_used"] == 12.5
    assert rb.evidence["extrusion_confirmed"] is True


def test_classify_printing_with_extruder_delta():
    snap = {
        "print_stats": {"state": "printing", "filament_used": 0.0},
        "extruder_position_delta": 1.5,
    }
    rb = classify_start_readback(snap)
    assert rb.outcome == "printing"
    assert rb.message == "printing"
    assert rb.evidence["extrusion_confirmed"] is True


def test_classify_printing_zero_extrusion_layer_1():
    """NEW evidence from tonight: layer 1 reported while still heating/purging.
    filament_used is 0, so extrusion is not confirmed."""
    snap = {
        "print_stats": {
            "state": "printing",
            "filament_used": 0.0,
            "info": {"current_layer": 1, "total_layer": 141},
        }
    }
    rb = classify_start_readback(snap)
    assert rb.outcome == "printing"
    assert rb.message == "printing (extrusion not yet confirmed)"
    assert rb.evidence["extrusion_confirmed"] is False
    assert rb.evidence["filament_used"] == 0.0


def test_classify_printing_no_extrusion_keys():
    snap = {
        "print_stats": {
            "state": "printing",
        }
    }
    rb = classify_start_readback(snap)
    assert rb.outcome == "printing"
    assert rb.message == "printing (extrusion not yet confirmed)"
    assert rb.evidence["extrusion_confirmed"] is False


def test_classify_preparing_heating_extruder():
    snap = {
        "extruder": {"target": 210.0},
        "heater_bed": {"target": 0.0},
    }
    rb = classify_start_readback(snap)
    assert rb.outcome == "preparing"
    assert "heating" in rb.evidence
    assert rb.evidence["heating"]["extruder"] == 210.0
    assert "not a failure; do not resubmit" in rb.message


def test_classify_preparing_heating_bed():
    snap = {
        "extruder": {"target": 0.0},
        "heater_bed": {"target": 60.0},
    }
    rb = classify_start_readback(snap)
    assert rb.outcome == "preparing"
    assert "heating" in rb.evidence
    assert rb.evidence["heating"]["heater_bed"] == 60.0


def test_classify_homed_axes_alone_are_not_preparing():
    """Klipper keeps the axes homed between prints (the recorded idle AD5X reports homed_axes xyz),
    so homed axes alone say nothing about a new start."""
    rb = classify_start_readback({"toolhead": {"homed_axes": "xyz"}})
    assert rb.outcome == "unconfirmed"


def test_classify_axes_homed_since_the_baseline_are_preparing():
    rb = classify_start_readback({"toolhead": {"homed_axes": "xyz"}, "print_stats": {"state": "standby"}},
                                 baseline={"toolhead": {"homed_axes": ""}, "print_stats": {"state": "standby"}})
    assert rb.outcome == "preparing" and rb.evidence["homing"] == "xyz"


# A reprint: remote names come from the artifact hash, so the previous run left exactly this name behind.
LEFTOVER = {"print_stats": {"state": "complete", "filename": "X.gcode", "total_duration": 612.0},
            "toolhead": {"homed_axes": "xyz"}, "extruder": {"target": 0.0}, "heater_bed": {"target": 0.0},
            "idle_timeout": {"state": "Idle"}, "virtual_sdcard": {"is_active": False, "file_path": None}}


def test_a_reprints_leftover_complete_state_is_not_preparing():
    """REPRODUCED before the fix: a leftover state=complete with the same filename read as preparing."""
    assert classify_start_readback({"print_stats": {"state": "complete", "filename": "X.gcode"}}).outcome == "unconfirmed"
    assert classify_start_readback(LEFTOVER).outcome == "unconfirmed"
    assert classify_start_readback(LEFTOVER, baseline=LEFTOVER).outcome == "unconfirmed"


@pytest.mark.parametrize("leftover", ["complete", "cancelled", "standby", "error"])
def test_an_unchanged_leftover_state_is_unconfirmed_never_preparing_or_failed(leftover):
    snap = {**LEFTOVER, "print_stats": {**LEFTOVER["print_stats"], "state": leftover}}
    rb = classify_start_readback(snap, baseline=snap)
    assert rb.outcome == "unconfirmed", rb


def test_an_unreadable_baseline_proves_no_change():
    snap = {**LEFTOVER, "extruder": {"target": 220.0}, "print_stats": {"state": "cancelled", "filename": "X.gcode"}}
    assert classify_start_readback(snap, baseline={}).outcome == "unconfirmed"


def test_changes_from_the_baseline_are_this_starts_evidence():
    heating = {**LEFTOVER, "extruder": {"target": 220.0}, "idle_timeout": {"state": "Printing"}}
    rb = classify_start_readback(heating, baseline=LEFTOVER)
    assert rb.outcome == "preparing" and rb.evidence["heating"] == {"extruder": 220.0}
    reloaded = {**LEFTOVER, "print_stats": {"state": "standby", "filename": "X.gcode", "total_duration": 0.0}}
    assert classify_start_readback(reloaded, baseline=LEFTOVER).evidence == {"new_job": "X.gcode"}
    failed = {**LEFTOVER, "print_stats": {"state": "error", "filename": "X.gcode", "total_duration": 0.0}}
    assert classify_start_readback(failed, baseline=LEFTOVER).outcome == "failed"


def test_heaters_already_on_in_the_baseline_are_not_this_starts_evidence():
    warm = {**LEFTOVER, "extruder": {"target": 220.0}}
    assert classify_start_readback(warm, baseline=warm).outcome == "unconfirmed"


def test_classify_preparing_virtual_sdcard():
    snap = {
        "virtual_sdcard": {"is_active": True, "file_path": "test.gcode"},
    }
    rb = classify_start_readback(snap)
    assert rb.outcome == "preparing"
    assert "virtual_sdcard" in rb.evidence


def test_classify_preparing_idle_printing():
    snap = {
        "idle_timeout": {"state": "Printing"},
    }
    rb = classify_start_readback(snap)
    assert rb.outcome == "preparing"
    assert rb.evidence["idle_timeout"] == "Printing"


def test_classify_preparing_ifs_loading():
    snap = {
        "zmod_ifs": {"state": "loading"},
    }
    rb = classify_start_readback(snap)
    assert rb.outcome == "preparing"
    assert "zmod_ifs" in rb.evidence


def test_classify_unconfirmed_empty():
    snap = {}
    rb = classify_start_readback(snap)
    assert rb.outcome == "unconfirmed"
    assert rb.evidence is None
    assert rb.message == "submitted, awaiting confirmation"


def test_classify_unconfirmed_idle_ready():
    snap = {
        "print_stats": {"state": "standby"},
        "idle_timeout": {"state": "Ready"},
        "extruder": {"target": 0},
        "heater_bed": {"target": 0},
    }
    rb = classify_start_readback(snap)
    assert rb.outcome == "unconfirmed"
    assert rb.evidence is None
    assert rb.message == "submitted, awaiting confirmation"
