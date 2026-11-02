"""Which spool's temperatures win, in slicing and in G-code verification.

LOW-12: verification and slicing must resolve the same spool. Three ways
they used to disagree, each failing a correctly sliced job or silently
re-slicing at another spool's targets:

- P2-272: verification resolved the *active* spool whenever it held the
  job's material, even when the artifact's hermes_tool_slots map names
  the different slot spool the job was actually sliced for.
- P2-273: an operator override attested for the exact spool+material was
  honored by verification (filament=None) but bypassed by slicing
  whenever the caller had prefetched the spool's Spoolman filament.
- P2-277: the per-tool check took the bed from the first candidate spool
  (lowest id), not from the spool whose nozzle target the header carries.
"""
from print_handy.core import printer_state

SPOOLS = {
    10: {"material": "PLA", "settings_extruder_temp": 210, "settings_bed_temp": 60},
    11: {"material": "PLA", "settings_extruder_temp": 220, "settings_bed_temp": 65},
    17: {"material": "PLA", "settings_extruder_temp": 225, "settings_bed_temp": 65},
}


def fetch(spool_id):
    return SPOOLS.get(spool_id)


def two_slot_state():
    """Active spool #10 (PLA, 210/60 by operator record); slot 2 holds #17 (PLA, 225/65)."""
    return {
        "spool": {"spoolman_id": 10, "material": "PLA", "name": "Active PLA"},
        "temps_c": {"nozzle": 210, "bed": 60},
        "temperature_resolution": {"material_family": "PLA", "material": "PLA",
                                   "spoolman_id": 10, "source": "operator setting"},
        "filament_slots": {"1": {"spoolman_id": 10, "material": "PLA"},
                           "2": {"spoolman_id": 17, "material": "PLA"}},
    }


# ---------------------------------------------------------------- P2-272

def test_gcode_temperatures_resolves_the_mapped_job_spool_not_the_active_one():
    text = ('; hermes_tool_slots = {"0": {"physical_slot": 2, "spoolman_id": 17}}\n'
            "; filament_type = PLA\n")
    assert printer_state.gcode_temperatures(two_slot_state(), ["PLA"], fetch, text) == \
        {"nozzle": 225, "bed": 65}


def test_gcode_temperatures_resolves_a_plain_slot_map_through_the_slot_record():
    # The older map spelling names physical slots only; the slot record
    # supplies the spool bound to the named slot.
    text = '; hermes_tool_slots = {"0": 2}\n; filament_type = PLA\n'
    assert printer_state.gcode_temperatures(two_slot_state(), ["PLA"], fetch, text) == \
        {"nozzle": 225, "bed": 65}


def test_gcode_temperatures_without_a_map_keeps_the_active_spool_resolution():
    assert printer_state.gcode_temperatures(
        two_slot_state(), ["PLA"], fetch, "; filament_type = PLA\n") == {"nozzle": 210, "bed": 60}


# ---------------------------------------------------------------- P2-273

def operator_override_state():
    return {
        "spool": {"spoolman_id": 17, "material": "PLA", "name": "Calibrated PLA"},
        "temps_c": {"nozzle": 225, "bed": 65},
        "temperature_resolution": {"material_family": "PLA", "material": "PLA",
                                   "spoolman_id": 17, "source": "operator setting"},
    }


def test_prefetched_filament_does_not_bypass_a_matching_operator_override():
    # orca_adapter.slice_model prefetches the job spool's filament and
    # passes it in; the override attested for this exact spool+material
    # must still win, exactly as it does for verification's filament=None.
    filament = {"material": "PLA", "settings_extruder_temp": 210, "settings_bed_temp": 60}
    temps, _resolution = printer_state.slice_temperatures(operator_override_state(), filament)
    assert temps == {"nozzle": 225, "bed": 65}


def test_prefetched_filament_still_wins_when_the_override_names_another_spool():
    # The spool changed since the override was recorded: the stored temps
    # belong to spool #10, so the selected spool's own filament decides.
    state = operator_override_state()
    state["temperature_resolution"] = {**state["temperature_resolution"], "spoolman_id": 10}
    state["temps_c"] = {"nozzle": 210, "bed": 60}
    filament = {"material": "PLA", "settings_extruder_temp": 225, "settings_bed_temp": 65}
    temps, _resolution = printer_state.slice_temperatures(state, filament)
    assert temps == {"nozzle": 225, "bed": 65}


# ---------------------------------------------------------------- P2-277

def test_gcode_tool_bed_comes_from_the_spool_whose_nozzle_matched():
    # Both slot records say PLA; the spools' own targets differ. T0's
    # header value (220) is spool #11's, so the job's bed is #11's 65 —
    # not options[0]'s (#10's) 60 just because 10 sorts first.
    state = {
        "spool": {"spoolman_id": 10, "material": "PLA", "name": "PLA"},
        "filament_slots": {"1": {"spoolman_id": 10, "material": "PLA"},
                           "2": {"spoolman_id": 11, "material": "PLA"}},
        "nozzle": {"type": "brass"},  # zero temperature offset: header == resolved targets
    }
    text = "T0\nT1\n; nozzle_temperature = 220,210\n"
    assert printer_state.gcode_temperatures(state, ["PLA", "PLA"], fetch, text) == \
        {"nozzle": 220, "bed": 65}
