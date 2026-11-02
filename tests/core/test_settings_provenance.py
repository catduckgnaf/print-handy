"""Tests for Phase 1 settings provenance and slice guard protections.

Verifies:
(a) The 3MF settings sanitizer forces Normal Lift for every filament slot in a synthetic
    project_settings carrying Spiral Lift.
(b) slice_guard refuses a synthetic G-code footer with filament_z_hop_types = Spiral Lift.
(c) slice_guard passes a Normal Lift footer.
(d) The AD5X filament template resolves to Normal Lift.
"""
import json
import zipfile
from pathlib import Path

import pytest

from print_handy import settings
from print_handy.core import orca_adapter, slice_guard
from print_handy.printers.flashforge.ad5x import MODEL

GOOD_GCODE = (
    "; estimated printing time = 1h 2m\n"
    "G28\n"
    "T0\n"
    "G1 X10 Y10 E1\n"
    "G1 X200 Y200 E2\n"
)


def test_sanitizer_forces_normal_lift_for_all_filament_slots(tmp_path: Path):
    """(a) Sanitizer forces Normal Lift for every filament slot in project_settings carrying Spiral Lift."""
    t3mf = tmp_path / "spiral.3mf"
    slot_count = 4
    project_settings = {
        "filament_z_hop_types": ["Spiral Lift"] * slot_count,
        "filament_type": ["PLA"] * slot_count,
        "filament_colour": ["#123456"] * slot_count,
    }
    with zipfile.ZipFile(t3mf, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps(project_settings))

    changed = orca_adapter.sanitize_project_settings(t3mf)

    assert "filament_z_hop_types" in changed
    with zipfile.ZipFile(t3mf, "r") as z:
        after = json.loads(z.read("Metadata/project_settings.config"))

    assert after["filament_z_hop_types"] == ["Normal Lift"] * slot_count
    # All slots are Normal Lift
    assert all(val == "Normal Lift" for val in after["filament_z_hop_types"])


def test_sanitizer_forces_normal_lift_even_with_scalar_value(tmp_path: Path):
    """Sanitizer handles string or mixed values gracefully, forcing Normal Lift."""
    t3mf = tmp_path / "spiral_scalar.3mf"
    project_settings = {
        "filament_z_hop_types": "Spiral Lift",
        "filament_type": ["PLA"],
        "filament_colour": ["#123456"],
    }
    with zipfile.ZipFile(t3mf, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps(project_settings))

    orca_adapter.sanitize_project_settings(t3mf)

    with zipfile.ZipFile(t3mf, "r") as z:
        after = json.loads(z.read("Metadata/project_settings.config"))

    assert after["filament_z_hop_types"] == ["Normal Lift"]


def test_slice_guard_refuses_spiral_lift_footer():
    """(b) slice_guard refuses a synthetic G-code footer with filament_z_hop_types = Spiral Lift."""
    gcode = GOOD_GCODE + "; filament_z_hop_types = Spiral Lift,Normal Lift\n"
    result = slice_guard.post_slice(gcode, tool_slots={0: 1})
    assert result.status == "blocked"
    assert any("Spiral Lift" in msg and "filament_z_hop_types" in msg for msg in result.blocked)


def test_slice_guard_refuses_spiral_lift_on_used_slot():
    """slice_guard refuses Spiral Lift if the used slot has Spiral Lift."""
    gcode = GOOD_GCODE + "; filament_z_hop_types = Normal Lift,Spiral Lift\n"
    # Slot 2 (1-based index) is mapped to tool 0
    result = slice_guard.post_slice(gcode, tool_slots={0: 2})
    assert result.status == "blocked"
    assert any("Spiral Lift" in msg for msg in result.blocked)


def test_slice_guard_passes_normal_lift_footer():
    """(c) slice_guard passes a Normal Lift footer."""
    gcode = GOOD_GCODE + "; filament_z_hop_types = Normal Lift,Normal Lift\n"
    result = slice_guard.post_slice(gcode, tool_slots={0: 1})
    assert result.status == "ok"
    assert not result.blocked


def test_ad5x_filament_template_resolves_to_normal_lift():
    """(d) The AD5X filament template resolves to Normal Lift."""
    template_dir = settings.ROOT / "print_handy" / "templates" / "orca" / MODEL.templates
    filament_path = template_dir / "filament.json"
    assert filament_path.is_file(), f"Expected template file at {filament_path}"

    data = json.loads(filament_path.read_text())
    assert "filament_z_hop_types" in data
    assert data["filament_z_hop_types"] == ["Normal Lift"]


def test_is_allowed_filament_id_allowlist():
    """Verify that Print Handy generated Hermes slot IDs and own template IDs pass the allowlist."""
    own = {"Flashforge Generic PLA"}
    assert slice_guard.is_allowed_filament_id("Flashforge Generic PLA", own)
    assert slice_guard.is_allowed_filament_id('"Flashforge Generic PLA"', own)
    assert slice_guard.is_allowed_filament_id("Hermes slot 1 PLA", own, slot_count=4)
    assert slice_guard.is_allowed_filament_id('"Hermes slot 2 PETG"', own, slot_count=4)
    assert slice_guard.is_allowed_filament_id("Hermes slot 4 TPU-95A", own, slot_count=4)

    # Foreign or arbitrary IDs are rejected
    assert not slice_guard.is_allowed_filament_id("Bambu Generic PLA", own)
    assert not slice_guard.is_allowed_filament_id("Prusament PLA", own)
    assert not slice_guard.is_allowed_filament_id("Custom Profile", own)
    assert not slice_guard.is_allowed_filament_id("", own)
    assert not slice_guard.is_allowed_filament_id(None, own)


def test_is_allowed_filament_id_rejects_spoofed_hermes_ids():
    """P2-27: the Hermes pattern alone must never accept an ID. A Hermes slot ID is
    accepted only as an exact generated profile ID, or for a slot inside the
    configured slot count the caller supplies."""
    own = {"Flashforge Generic PLA"}
    # The audit's spoof: a foreign project naming itself a Hermes slot.
    assert not slice_guard.is_allowed_filament_id("Hermes slot 999 Totally Foreign", own, slot_count=4)
    assert not slice_guard.is_allowed_filament_id("Hermes slot 999 Totally Foreign", own)
    # No slot bound supplied: the pattern by itself proves nothing.
    assert not slice_guard.is_allowed_filament_id("Hermes slot 1 PLA", own)
    assert not slice_guard.is_allowed_filament_id("Hermes slot 1 PLA")
    # Outside the configured slots, even with a plausible material.
    assert not slice_guard.is_allowed_filament_id("Hermes slot 5 PLA", own, slot_count=4)
    assert not slice_guard.is_allowed_filament_id("Hermes slot 0 PLA", own, slot_count=4)
    # A generated ID carried in the active profile IDs passes by exact membership,
    # whatever slot bound the caller has.
    assert slice_guard.is_allowed_filament_id("Hermes slot 2 PETG", {"Hermes slot 2 PETG"})


def test_slice_guard_blocks_a_spoofed_hermes_filament_id():
    """P2-27 at the post-slice gate: a spoofed Hermes ID in the G-code is foreign."""
    gcode = GOOD_GCODE + '; filament_settings_id = "Hermes slot 999 Totally Foreign"\n'
    result = slice_guard.post_slice(gcode, tool_slots={0: 1})
    assert result.status == "blocked"
    assert any("Hermes slot 999" in problem for problem in result.blocked)


def test_slice_guard_accepts_hermes_slot_filament_id():
    """slice_guard permits synthesized Hermes slot IDs in filament_settings_id header/footer."""
    gcode = GOOD_GCODE + '; filament_settings_id = "Hermes slot 1 PLA";"Hermes slot 2 PLA"\n'
    result = slice_guard.post_slice(gcode, tool_slots={0: 1, 1: 2})
    assert result.status == "ok"
    assert not result.blocked


def test_slice_guard_blocks_foreign_filament_id():
    """slice_guard blocks unrecognized third-party filament profiles in filament_settings_id."""
    gcode = GOOD_GCODE + '; filament_settings_id = "Bambu Lab Generic PLA"\n'
    result = slice_guard.post_slice(gcode, tool_slots={0: 1})
    assert result.status == "blocked"
    assert any("Foreign effective setting filament_settings_id" in msg for msg in result.blocked)


def test_sanitizer_preserves_hermes_slot_filament_ids(tmp_path: Path):
    """Sanitizer retains generated Hermes slot names while replacing unknown foreign IDs with template default."""
    t3mf = tmp_path / "mixed_filaments.3mf"
    project_settings = {
        "filament_settings_id": ["Hermes slot 1 PLA", "Alien Profiler Extraordinaire", "Hermes slot 3 PETG"],
        "filament_type": ["PLA", "PLA", "PETG"],
        "filament_colour": ["#FFFFFF", "#000000", "#FF0000"],
    }
    with zipfile.ZipFile(t3mf, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps(project_settings))

    orca_adapter.sanitize_project_settings(t3mf)

    with zipfile.ZipFile(t3mf, "r") as z:
        after = json.loads(z.read("Metadata/project_settings.config"))

    # Slot 1 and Slot 3 are preserved, Slot 2 is replaced by template default ("Flashforge Generic PLA")
    assert after["filament_settings_id"][0] == "Hermes slot 1 PLA"
    assert after["filament_settings_id"][1] == "Flashforge Generic PLA"
    assert after["filament_settings_id"][2] == "Hermes slot 3 PETG"


def test_sanitizer_slot_limit_is_physical_not_filaments_used(tmp_path: Path):
    """2.2.5 review: a 2-filament mapping must not strip Hermes slot 3/4 names or a 5th toolhead's slot."""
    t3mf = tmp_path / "physical_slots.3mf"
    project_settings = {"filament_settings_id": ["Hermes slot 3 PLA", "Hermes slot 5 PETG"]}
    with zipfile.ZipFile(t3mf, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps(project_settings))

    orca_adapter.sanitize_project_settings(t3mf, slot_count=2, slot_limit=5)

    with zipfile.ZipFile(t3mf, "r") as z:
        after = json.loads(z.read("Metadata/project_settings.config"))
    assert after["filament_settings_id"] == ["Hermes slot 3 PLA", "Hermes slot 5 PETG"]
