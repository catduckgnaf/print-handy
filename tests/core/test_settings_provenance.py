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
    """Only the job's own profile names pass; the retired "Hermes slot" shape proves nothing (#1816)."""
    own = {"Flashforge Generic PLA", "Flashforge HS PETG @FF AD5X [slot 2]"}
    assert slice_guard.is_allowed_filament_id("Flashforge Generic PLA", own)
    assert slice_guard.is_allowed_filament_id('"Flashforge Generic PLA"', own)
    assert slice_guard.is_allowed_filament_id("Flashforge HS PETG @FF AD5X [slot 2]", own)
    assert not slice_guard.is_allowed_filament_id("Hermes slot 1 PLA", own)
    assert not slice_guard.is_allowed_filament_id("Flashforge HS PETG @FF AD5X [slot 3]", own)

    # Foreign or arbitrary IDs are rejected
    assert not slice_guard.is_allowed_filament_id("Bambu Generic PLA", own)
    assert not slice_guard.is_allowed_filament_id("Prusament PLA", own)
    assert not slice_guard.is_allowed_filament_id("Custom Profile", own)
    assert not slice_guard.is_allowed_filament_id("", own)
    assert not slice_guard.is_allowed_filament_id(None, own)
    assert not slice_guard.is_allowed_filament_id("Flashforge Generic PLA", set())


def test_is_allowed_filament_id_rejects_spoofed_and_retired_hermes_ids():
    """P2-27 and #1816: a name's shape never proves anything; the retired "Hermes slot" names are foreign
    unless the job itself generated exactly that name."""
    own = {"Flashforge Generic PLA"}
    assert not slice_guard.is_allowed_filament_id("Hermes slot 999 Totally Foreign", own)
    assert not slice_guard.is_allowed_filament_id("Hermes slot 1 PLA", own)
    assert not slice_guard.is_allowed_filament_id("Hermes slot 1 PLA")
    assert not slice_guard.is_allowed_filament_id("Generic PLA @Flashforge [slot 1]", own)
    assert slice_guard.is_allowed_filament_id("Generic PLA @Flashforge [slot 1]", {"Generic PLA @Flashforge [slot 1]"})


def test_slice_guard_blocks_a_spoofed_hermes_filament_id():
    """P2-27 at the post-slice gate: a spoofed Hermes ID in the G-code is foreign."""
    gcode = GOOD_GCODE + '; filament_settings_id = "Hermes slot 999 Totally Foreign"\n'
    result = slice_guard.post_slice(gcode, tool_slots={0: 1})
    assert result.status == "blocked"
    assert any("Hermes slot 999" in problem for problem in result.blocked)


def test_slice_guard_rejects_the_retired_hermes_slot_filament_id():
    """#1816: the post-slice gate no longer accepts "Hermes slot N" names it did not generate for this job."""
    gcode = GOOD_GCODE + '; filament_settings_id = "Hermes slot 1 PLA";"Hermes slot 2 PLA"\n'
    result = slice_guard.post_slice(gcode, tool_slots={0: 1, 1: 2})
    assert result.status == "blocked"
    assert any("Hermes slot 1 PLA" in problem for problem in result.blocked)


def test_slice_guard_accepts_the_jobs_own_vendor_profile_names(tmp_path: Path):
    """#1816: the names the job generated (vendor profile, per slot) pass by exact membership."""
    names = ["Flashforge HS PETG @FF AD5X [slot 1]", "Generic PETG @Flashforge [slot 2]"]
    paths = []
    for index, name in enumerate(names):
        path = tmp_path / f"filament_tool_{index}.json"
        path.write_text(json.dumps({"name": name, "filament_settings_id": [name]}))
        paths.append(path)
    gcode = GOOD_GCODE + "; filament_settings_id = " + ";".join(f'"{n}"' for n in names) + "\n"
    result = slice_guard.post_slice(gcode, tool_slots={0: 1, 1: 2}, profile_paths=paths)
    assert result.status == "ok", result.blocked


def test_slice_guard_blocks_foreign_filament_id():
    """slice_guard blocks unrecognized third-party filament profiles in filament_settings_id."""
    gcode = GOOD_GCODE + '; filament_settings_id = "Bambu Lab Generic PLA"\n'
    result = slice_guard.post_slice(gcode, tool_slots={0: 1})
    assert result.status == "blocked"
    assert any("Foreign effective setting filament_settings_id" in msg for msg in result.blocked)


def test_sanitizer_keeps_the_jobs_own_per_slot_ids_and_pins_foreign_ones_per_tool(tmp_path: Path):
    """#1816: a multi-tool job's own per-slot names survive; a foreign entry becomes that tool's own name."""
    names = ["Flashforge HS PETG @FF AD5X [slot 1]", "Generic PETG @Flashforge [slot 2]",
             "Flashforge Generic PLA [slot 3]"]
    profiles = []
    for index, name in enumerate(names):
        path = tmp_path / f"filament_tool_{index}.json"
        path.write_text(json.dumps({"name": name, "filament_settings_id": [name]}), encoding="utf-8")
        profiles.append(path)
    t3mf = tmp_path / "own_slots.3mf"
    project = {"filament_settings_id": [names[0], "Hermes slot 2 PETG", "Alien Profile"],
               "filament_type": ["PETG", "PETG", "PLA"], "filament_colour": ["#000000", "#FFFFFF", "#FF0000"]}
    with zipfile.ZipFile(t3mf, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps(project))

    orca_adapter.sanitize_project_settings(t3mf, slot_count=3, filament_profiles=profiles)

    with zipfile.ZipFile(t3mf, "r") as z:
        after = json.loads(z.read("Metadata/project_settings.config"))
    assert after["filament_settings_id"] == names


@pytest.mark.parametrize("imported", [
    # slot 1's and slot 2's own IDs swapped
    lambda names: [names[1], names[0], names[2]],
    # one tool's valid ID copied onto every slot
    lambda names: [names[0]] * 3,
    # the bare template ID on slots the job has its own profiles for
    lambda names: ["Flashforge Generic PLA"] * 3,
], ids=["swapped", "copied", "bare-template"])
def test_sanitizer_pins_each_slot_to_its_own_generated_id(tmp_path: Path, imported):
    """duckcatgnaf (a53a07df): a generated ID is valid only on the slot it was generated for; on any
    other slot it would run that tool with another filament's settings, so it is repinned."""
    names = ["Flashforge HS PETG @FF AD5X [slot 1]", "Generic PETG @Flashforge [slot 2]",
             "Flashforge Generic PLA [slot 3]"]
    profiles = []
    for index, name in enumerate(names):
        path = tmp_path / f"filament_tool_{index}.json"
        path.write_text(json.dumps({"name": name, "filament_settings_id": [name]}), encoding="utf-8")
        profiles.append(path)
    t3mf = tmp_path / "moved_ids.3mf"
    project = {"filament_settings_id": imported(names),
               "filament_type": ["PETG", "PETG", "PLA"], "filament_colour": ["#000000", "#FFFFFF", "#FF0000"]}
    with zipfile.ZipFile(t3mf, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps(project))

    changed = orca_adapter.sanitize_project_settings(t3mf, slot_count=3, filament_profiles=profiles)

    with zipfile.ZipFile(t3mf, "r") as z:
        after = json.loads(z.read("Metadata/project_settings.config"))
    assert after["filament_settings_id"] == names
    assert changed["filament_settings_id"] == [imported(names), names]


def test_sanitizer_replaces_retired_hermes_slot_filament_ids(tmp_path: Path):
    """#1816: an imported project's "Hermes slot N" names are foreign now, like any other name that is not the
    job's own profile: every one is pinned to the job's profile."""
    t3mf = tmp_path / "mixed_filaments.3mf"
    project_settings = {
        "filament_settings_id": ["Hermes slot 1 PLA", "Alien Profiler Extraordinaire", "Hermes slot 3 PETG"],
        "filament_type": ["PLA", "PLA", "PETG"],
        "filament_colour": ["#FFFFFF", "#000000", "#FF0000"],
    }
    with zipfile.ZipFile(t3mf, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps(project_settings))

    orca_adapter.sanitize_project_settings(t3mf, slot_count=3)

    with zipfile.ZipFile(t3mf, "r") as z:
        after = json.loads(z.read("Metadata/project_settings.config"))
    assert after["filament_settings_id"] == ["Flashforge Generic PLA"] * 3
