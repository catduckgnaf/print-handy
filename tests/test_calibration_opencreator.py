"""OpenCreator calibration routes fail closed until hardware verified."""
import pytest

from print_handy import calibration as cal
from test_calibration import FakeTransport


MODELS = ["flashforge_opencreator.creator5", "flashforge_opencreator.creator5_pro"]
OBJECTS = [
    "print_stats", "configfile", "bed_mesh", "input_shaper", "extruder", "heater_bed",
    "gcode_macro BED_MESH_CALIBRATE", "gcode_macro C5_SCREWS_TILT",
    "gcode_macro C5_PID_CALIBRATE", "gcode_macro C5_MPC_CALIBRATE",
    "gcode_macro CALIBRATE_INPUT_SHAPER", "gcode_macro C5_CALIBRATE_OFFSETS",
    "gcode_macro C5_CALIBRATE_TOOL_POSITION",
]
SETTINGS = {
    "extruder": {"control": "pid", "pid_kp": 20, "pid_ki": 1, "pid_kd": 100},
    "heater_bed": {"control": "pid", "pid_kp": 60, "pid_ki": 2, "pid_kd": 300},
    "bed_mesh": {"mesh_min": "0,0"},
    "input_shaper": {"shaper_type_x": "mzv", "shaper_freq_x": 40,
                     "shaper_type_y": "mzv", "shaper_freq_y": 42},
}


class OpenCreatorTransport(FakeTransport):
    def __init__(self, **kwargs):
        kwargs.setdefault("objects", OBJECTS)
        kwargs.setdefault("settings_map", SETTINGS)
        super().__init__(**kwargs)


@pytest.mark.parametrize("key", MODELS)
def test_opencreator_is_eligible_separate_vendor(key):
    entry = cal.printer_entry(key)
    assert entry.vendor == cal.OPENCREATOR_VENDOR
    assert cal.block_reason(entry) is None
    assert cal.is_opencreator(entry)
    assert not cal.is_forgex(entry)


def test_discovered_opencreator_options_are_all_guidance_only():
    transport = OpenCreatorTransport()
    entry = cal.printer_entry(MODELS[0])
    facts = cal.fetch_facts(transport)
    options = {option.key: option for option in cal.available_options(entry, facts)}
    assert set(options) == {"mesh", "screws", "pid_extruder", "pid_bed", "shaper", "zoffset"}
    assert all(option.mode == "guide" for option in options.values())
    assert "wraps BED_MESH_CALIBRATE" in options["mesh"].note
    assert "C5_SCREWS_TILT" in options["screws"].note
    assert "C5_PID_CALIBRATE" in options["pid_bed"].note
    assert "CALIBRATE_INPUT_SHAPER" in options["shaper"].note


def test_mpc_controller_is_not_misrepresented_as_pid_option():
    transport = OpenCreatorTransport(settings_map={
        **SETTINGS, "extruder": {"control": "mpc"}})
    entry = cal.printer_entry(MODELS[0])
    options = {option.key: option for option in cal.available_options(entry, cal.fetch_facts(transport))}
    assert "pid_extruder" not in options
    assert "mpc_extruder" in options
    assert "C5_MPC_CALIBRATE" in options["mpc_extruder"].note


@pytest.mark.parametrize("calibration,args,primitive", [
    ("mesh", (), "BED_MESH_CALIBRATE"),
    ("screws", (), "C5_SCREWS_TILT"),
    ("pid", ("extruder", 220), "C5_PID_CALIBRATE"),
    ("shaper", (), "CALIBRATE_INPUT_SHAPER"),
])
def test_unverified_flows_refuse_before_any_gcode(calibration, args, primitive):
    transport = OpenCreatorTransport()
    entry = cal.printer_entry(MODELS[0])
    actions = {
        "mesh": lambda: cal.prepare_mesh(transport, entry),
        "screws": lambda: cal.prepare_screws(transport, entry),
        "pid": lambda: cal.prepare_pid(transport, entry, *args),
        "shaper": lambda: cal.prepare_shaper(transport, entry),
    }
    with pytest.raises(cal.CalibrationRefusal, match=primitive):
        actions[calibration]()
    assert transport.posts == []


def test_mpc_refusal_names_firmware_path_and_sends_nothing():
    transport = OpenCreatorTransport(settings_map={
        **SETTINGS, "extruder": {"control": "mpc", "pid_kp": 20, "pid_ki": 1, "pid_kd": 100}})
    with pytest.raises(cal.CalibrationRefusal, match="C5_MPC_CALIBRATE"):
        cal.prepare_pid(transport, cal.printer_entry(MODELS[0]), "extruder", 220)
    assert transport.posts == []


def test_offset_guide_names_hands_on_primitives_and_is_read_only():
    transport = OpenCreatorTransport()
    text = cal.zoffset_report(transport, cal.printer_entry(MODELS[0]))
    assert "C5_CALIBRATE_OFFSETS" in text
    assert "C5_CALIBRATE_TOOL_POSITION" in text
    assert "hardware verified" in text
    assert transport.posts == []


def test_screw_guide_explicitly_does_not_actuate():
    guide = cal.screws_guide(cal.printer_entry(MODELS[0]))
    assert "C5_SCREWS_TILT" in guide and "hardware not verified" in guide
