"""Offline tests for printer calibration with the before/after accept gate (issue #565).

No printer or network: Moonraker responses are recorded shapes replayed
through a fake transport that records every call, and the dispatch tests
use a fake adapter in the style of tests/test_firmware_update.py. The
pins that matter (Stage 0 evidence on #565): before-values are
presented; results carry the new values AND the correct signed
differences; no save is sent before an accept in any flow; accept sends
exactly the documented save once; a PID discard sends FIRMWARE_RESTART
and no save; busy printers are refused before any command; the
auto-saving ZMod PID macros are never sent; an absent macro means the
calibration is not offered; Z offset follows the Stage 0 mode verdict
(report + guide, native screen owns its store); and a record is written
on accept, on discard and on failure.
"""
import asyncio
import importlib.util
import json
import socket
import threading
import urllib.error
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy import calibration as cal
from print_handy import printers, settings
from print_handy.bot import commands as bot_commands
from print_handy.bot import llm
from print_handy.core import model_job
from print_handy.runtime import spool_sync

ROOT = Path(__file__).parents[1]
PLUGIN_PATH = ROOT / "plugins/print-feedback/__init__.py"
spec = importlib.util.spec_from_file_location("print_feedback_calibration", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def entry(key):
    found = cal.printer_entry(key)
    assert found is not None, key
    return found


AD5X = entry("flashforge.ad5x")
GENERIC = entry("klipper.generic")
NEPTUNE4 = entry("elegoo.neptune4")
BAMBU_A1 = entry("bambulab.a1")

NO_SLEEP = lambda _seconds: None  # noqa: E731

MESH_BEFORE = [[0.0, 0.21], [0.42, 0.1]]            # range 0.42
MESH_AFTER = [[0.0, 0.09], [0.18, 0.05]]            # range 0.18

ZMOD_OBJECTS = [
    "print_stats", "bed_mesh", "input_shaper", "save_variables", "configfile",
    "gcode_macro AUTO_FULL_BED_LEVEL", "gcode_macro ZSHAPER",
    "gcode_macro BELTS_SHAPER_CALIBRATION", "gcode_macro BED_LEVEL_SCREWS_TUNE",
    "gcode_macro LOAD_ZOFFSET_NATIVE", "extruder", "heater_bed",
]
ZMOD_SETTINGS = {
    # Klipper's configfile status lowercases every option name, so the PID
    # terms arrive as pid_kp / pid_ki / pid_kd whatever the file's casing.
    "extruder": {"pid_kp": "22.2", "pid_ki": "1.5", "pid_kd": "105.0"},
    "heater_bed": {"pid_kp": "70.0", "pid_ki": "3.0", "pid_kd": "380.0"},
    "input_shaper": {"shaper_type_x": "mzv", "shaper_freq_x": 36.8,
                     "shaper_type_y": "mzv", "shaper_freq_y": 40.1},
    "bed_mesh": {"mesh_min": "10,10"},
}
SCREENLESS_VARIABLES = {"display_off": 1, "load_zoffset": 1,
                        "gcode_offsets": {"z": -1.185}}
NATIVE_VARIABLES = {"display_off": 0, "load_zoffset": 0,
                    "gcode_offsets": {"z": -1.185}}
NATIVE_CONFIG = {"leftExtruderOffset": {"zProbeOffset": -1.24}}

KLIPPER_OBJECTS = ["print_stats", "bed_mesh", "input_shaper", "save_variables",
                   "configfile", "resonance_tester", "extruder", "heater_bed"]
KLIPPER_SETTINGS = {
    "extruder": {"pid_kp": 21.0, "pid_ki": 1.2, "pid_kd": 90.0},
    "heater_bed": {"pid_kp": 65.0, "pid_ki": 2.5, "pid_kd": 300.0},
    "input_shaper": {"shaper_type": "mzv", "shaper_freq_x": 40.0, "shaper_freq_y": 44.0},
    "bed_mesh": {"mesh_min": "10,10"},
    "screws_tilt_adjust": {"screw1": "-5,30"},
    "resonance_tester": {"accel_chip": "adxl345"},
    "probe": {"z_offset": 1.85},
}

PID_REPORT = "// PID parameters: pid_Kp=24.850 pid_Ki=1.720 pid_Kd=98.400"
SHAPER_REPORT_KLIPPER = [
    "// Calculating the best input shaper parameters for x axis",
    "// Recommended shaper_type_x = ei, shaper_freq_x = 41.5 Hz",
    "// Calculating the best input shaper parameters for y axis",
    "// Recommended shaper_type_y = mzv, shaper_freq_y = 38.0 Hz",
]
SHAPER_REPORT_ZMOD = [
    "// Calculating the best input shaper parameters for x axis",
    "// Recommended shaper is zv @ 53.2 Hz",
    "// Calculating the best input shaper parameters for y axis",
    "// Recommended shaper is mzv @ 54.2 Hz",
]
SCREWS_FIRST = [
    "// front left screw (base) : x=-5.0, y=30.0, z=2.48750",
    "// front right screw : x=155.0, y=30.0, z=2.36000 : adjust CW 01:15",
    "// rear right screw : x=155.0, y=190.0, z=2.71500 : adjust CCW 00:50",
]
SCREWS_SECOND = [
    "// front left screw (base) : x=-5.0, y=30.0, z=2.48750",
    "// front right screw : x=155.0, y=30.0, z=2.47500 : adjust CW 00:02",
    "// rear right screw : x=155.0, y=190.0, z=2.50000 : adjust CCW 00:03",
]
BELTS_REPORT = ["// Belt frequency analysis", "// X belt resonance: 62 Hz, Y belt resonance: 58 Hz"]


def bed_mesh_status(matrix=None, profiles=None, profile_name="auto"):
    return {"profile_name": profile_name,
            "probed_matrix": MESH_BEFORE if matrix is None else matrix,
            "mesh_matrix": [],
            "profiles": ({"auto": {"points": MESH_BEFORE, "mesh_params": {}},
                          "print_handy": {"points": [[0.1, 0.2], [0.3, 0.15]],
                                          "mesh_params": {}}}
                         if profiles is None else profiles)}


def facts_status(settings_map, variables, mesh=None):
    return {"configfile": {"settings": settings_map},
            "bed_mesh": bed_mesh_status() if mesh is None else mesh,
            "input_shaper": {},
            "save_variables": {"variables": variables}}


class FakeTransport:
    """The spool_sync.moonraker call shape, recording every call.

    Routes by path. `store_appends` maps an exact script line to console
    messages that appear once that script is POSTed; `mesh_after` is the
    bed_mesh object served once a mesh measurement script was sent. Like
    Moonraker, the console store stamps each entry with an increasing time
    and keeps only the newest `store_cap` entries. `send_error` is raised
    by a gcode-script POST after it has been recorded (the printer got it).
    """

    def __init__(self, objects=ZMOD_OBJECTS, settings_map=ZMOD_SETTINGS,
                 variables=SCREENLESS_VARIABLES, states=("standby",),
                 store=(), store_appends=None, mesh_after=None,
                 native_config=NATIVE_CONFIG, error=None, store_cap=1000,
                 send_error=None, heaters_stuck=False):
        self.heaters_stuck = heaters_stuck
        self.objects = objects
        self.settings_map = settings_map
        self.variables = variables
        self.states = list(states)
        self.store_cap = store_cap
        self.clock = 1_793_000_000.0
        self.store = []
        self._append(store)
        self.store_appends = store_appends or {}
        self.send_error = send_error
        self.mesh_after = mesh_after
        self.native_config = native_config
        self.error = error
        self.calls = []
        self.mesh_measured = False

    def _append(self, messages):
        for message in messages:
            self.clock += 0.25
            self.store.append((self.clock, message))
        del self.store[:-self.store_cap]

    @property
    def posts(self):
        return [(path, body) for method, path, body in self.calls if method == "POST"]

    @property
    def scripts(self):
        return [body["script"] for _path, body in self.posts
                if _path == cal.GCODE_SCRIPT_PATH]

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.error is not None:
            raise self.error
        if path == cal.GCODE_SCRIPT_PATH:
            script = (body or {}).get("script", "")
            self._append(self.store_appends.get(script, []))
            if script.startswith(("AUTO_FULL_BED_LEVEL", "BED_MESH_CALIBRATE")):
                self.mesh_measured = True
            if self.send_error is not None:
                raise self.send_error
            return {"result": "ok"}
        if path == cal.OBJECTS_LIST_PATH:
            return {"result": {"objects": self.objects}}
        if path == cal.FACTS_PATH:
            mesh = bed_mesh_status(matrix=MESH_AFTER) if self.mesh_measured \
                and self.mesh_after is None else \
                (self.mesh_after if self.mesh_measured else bed_mesh_status())
            return {"result": {"status": facts_status(self.settings_map,
                                                      self.variables, mesh)}}
        if path == cal.SETTINGS_PATH:
            return {"result": {"status": {"configfile": {"settings": self.settings_map}}}}
        if path == "/printer/objects/query?bed_mesh":
            mesh = bed_mesh_status(matrix=MESH_AFTER) if self.mesh_measured else bed_mesh_status()
            return {"result": {"status": {"bed_mesh": mesh}}}
        if path == cal.PRINT_STATE_PATH:
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {"result": {"status": {"print_stats": {"state": state}}}}
        if path == cal.GCODE_STORE_PATH:
            return {"result": {"gcode_store": [
                {"message": message, "type": "response", "time": when}
                for when, message in self.store]}}
        if path == cal.HEATER_TARGETS_PATH:
            off = "TURN_OFF_HEATERS" in self.scripts and not self.heaters_stuck
            return {"result": {"status": {"extruder": {"target": 0.0 if off else 220.0},
                                          "heater_bed": {"target": 0.0 if off else 60.0}}}}
        if path == cal.PRINTER_INFO_PATH:
            return {"result": {"state": "ready"}}
        if path == cal.NATIVE_CONFIG_PATH:
            if self.native_config is None:
                raise OSError("no native config")
            return self.native_config
        raise AssertionError(f"unexpected transport call: {method} {path}")


def zmod_facts(transport=None, **kwargs):
    transport = transport or FakeTransport(**kwargs)
    return transport, cal.fetch_facts(transport)


# ------------------------------------------------------------------ eligibility

def test_eligibility_mirrors_the_firmware_rule():
    assert cal.is_eligible(AD5X) and cal.is_eligible(GENERIC)
    assert not cal.is_eligible(NEPTUNE4)
    assert "vendor fork" in cal.block_reason(NEPTUNE4)
    assert not cal.is_eligible(BAMBU_A1)
    assert "Moonraker" in cal.block_reason(BAMBU_A1)


def test_options_zmod_screenless_offers_the_full_menu():
    _t, facts = zmod_facts()
    options = cal.available_options(AD5X, facts)
    modes = {option.key: option.mode for option in options}
    assert modes == {"mesh": "run", "screws": "guide", "pid_extruder": "run",
                     "pid_bed": "run", "shaper": "run", "belts": "run-report",
                     "zoffset": "report"}


def test_options_zmod_native_mesh_and_zoffset_are_report_only():
    _t, facts = zmod_facts(variables=NATIVE_VARIABLES)
    modes = {option.key: option.mode for option in cal.available_options(AD5X, facts)}
    assert modes["mesh"] == "report" and modes["zoffset"] == "report"


def test_absent_macro_means_not_offered():
    objects = [name for name in ZMOD_OBJECTS
               if "AUTO_FULL_BED_LEVEL" not in name and "ZSHAPER" not in name
               and "BELTS" not in name]
    _t, facts = zmod_facts(objects=objects)
    keys = {option.key for option in cal.available_options(AD5X, facts)}
    assert "mesh" not in keys and "shaper" not in keys and "belts" not in keys
    assert "pid_extruder" in keys  # PID comes from configfile settings, still present


def test_options_generic_klipper_uses_klipper_primitives():
    transport = FakeTransport(objects=KLIPPER_OBJECTS, settings_map=KLIPPER_SETTINGS,
                              variables={})
    facts = cal.fetch_facts(transport)
    modes = {option.key: option.mode for option in cal.available_options(GENERIC, facts)}
    assert modes == {"mesh": "run", "screws": "run", "pid_extruder": "run",
                     "pid_bed": "run", "shaper": "run", "zoffset": "guide"}


def test_menu_text_lists_forms_and_the_pattern():
    _t, facts = zmod_facts()
    text = cal.menu_text(AD5X, cal.available_options(AD5X, facts))
    for form in ("`/print-calibration mesh`", "`/print-calibration pid extruder [temp]`",
                 "`/print-calibration shaper`", "`/print-calibration records`"):
        assert form in text, form
    assert "nothing is saved" in text or "before anything is saved" in text


# ------------------------------------------------------------------ PID

def pid_transport(**kwargs):
    kwargs.setdefault("store_appends", {
        "PID_CALIBRATE HEATER=extruder TARGET=240": [PID_REPORT],
        "PID_CALIBRATE HEATER=heater_bed TARGET=60": [PID_REPORT]})
    return FakeTransport(**kwargs)


def test_pid_prepare_presents_before_values_and_never_the_auto_save_macros():
    transport = pid_transport()
    prepared = cal.prepare_pid(transport, AD5X, "extruder", 240)
    assert prepared.before == cal.PidValues(22.2, 1.5, 105.0)
    prompt = cal.pid_prompt_text(prepared)
    assert "Kp 22.200" in prompt and "Ki 1.500" in prompt and "Kd 105.000" in prompt
    assert "`PID_CALIBRATE HEATER=extruder TARGET=240`" in prompt
    assert "PID_TUNE" not in prepared.script
    assert transport.posts == []  # prepare sends nothing


def test_pid_result_shows_new_values_and_signed_differences():
    transport = pid_transport()
    prepared = cal.prepare_pid(transport, AD5X, "extruder", 240)
    result = cal.execute_pid(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    assert result.after == cal.PidValues(24.85, 1.72, 98.4)
    text = cal.pid_result_text(result)
    assert "Kp: 22.200 → 24.850 (+2.650)" in text
    assert "Ki: 1.500 → 1.720 (+0.220)" in text
    assert "Kd: 105.000 → 98.400 (-6.600)" in text
    # Nothing saved before an accept: the only POST so far is the calibration itself.
    assert transport.scripts == ["PID_CALIBRATE HEATER=extruder TARGET=240"]


def test_pid_accept_sends_exactly_one_save_config():
    transport = pid_transport()
    prepared = cal.prepare_pid(transport, AD5X, "extruder", 240)
    result = cal.execute_pid(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    message = cal.accept_pid(transport, result)
    assert transport.scripts == ["PID_CALIBRATE HEATER=extruder TARGET=240", "SAVE_CONFIG"]
    assert "Saved" in message


def test_pid_discard_sends_firmware_restart_and_no_save():
    transport = pid_transport()
    prepared = cal.prepare_pid(transport, AD5X, "heater_bed", 60)
    result = cal.execute_pid(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    message = cal.discard_pid(transport, result)
    assert transport.scripts == ["PID_CALIBRATE HEATER=heater_bed TARGET=60",
                                 "FIRMWARE_RESTART"]
    assert "SAVE_CONFIG" not in " ".join(transport.scripts)
    assert "Discarded" in message


def test_pid_flows_never_send_the_auto_saving_zmod_macros():
    transport = pid_transport()
    for heater, target in (("extruder", 240), ("heater_bed", 60)):
        prepared = cal.prepare_pid(transport, AD5X, heater, target)
        result = cal.execute_pid(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
        cal.accept_pid(transport, result)
    for script in transport.scripts:
        for banned in cal.NEVER_SEND:
            assert banned not in script


@pytest.mark.parametrize("state", ["printing", "paused"])
def test_pid_refused_while_busy_before_any_command(state):
    transport = pid_transport(states=(state,))
    with pytest.raises(cal.CalibrationRefusal) as excinfo:
        cal.prepare_pid(transport, AD5X, "extruder", 240)
    assert state in str(excinfo.value)
    assert transport.posts == []


def test_pid_execute_rechecks_idle_and_sends_nothing_when_a_print_started():
    transport = pid_transport(states=("standby", "printing"))
    prepared = cal.prepare_pid(transport, AD5X, "extruder", 240)
    with pytest.raises(cal.CalibrationRefusal):
        cal.execute_pid(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    assert transport.posts == []


def test_pid_timeout_is_an_honest_error_not_a_result():
    transport = FakeTransport()  # the store never reports PID parameters
    prepared = cal.prepare_pid(transport, AD5X, "extruder", 240)
    with pytest.raises(cal.CalibrationError) as excinfo:
        cal.execute_pid(transport, prepared, poll_attempts=2, poll_interval_s=0,
                        sleep=NO_SLEEP)
    assert "did not report" in str(excinfo.value)


# ------------------------------------------------------------------ long sends

def test_long_calibration_send_survives_a_read_timeout_and_polls_for_the_result():
    # Moonraker holds /printer/gcode/script until the gcode finishes; the
    # transport's read timeout fires while the printer keeps running it.
    transport = pid_transport(send_error=TimeoutError("timed out"))
    prepared = cal.prepare_pid(transport, AD5X, "extruder", 240)
    result = cal.execute_pid(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    assert result.after.kp == pytest.approx(24.85)
    assert transport.scripts == ["PID_CALIBRATE HEATER=extruder TARGET=240"]


def test_long_mesh_send_survives_a_read_timeout():
    transport = FakeTransport(send_error=TimeoutError("timed out"))
    _t, facts = zmod_facts(transport)
    prepared = cal.prepare_mesh(transport, AD5X, facts)
    result = cal.execute_mesh(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    assert result.after.range == pytest.approx(0.18)


def test_long_send_refused_connection_is_still_a_failure():
    refused = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    transport = pid_transport(send_error=refused)
    prepared = cal.prepare_pid(transport, AD5X, "extruder", 240)
    with pytest.raises(cal.CalibrationError) as excinfo:
        cal.execute_pid(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    assert "The send did not succeed" in str(excinfo.value)


def test_short_send_timeout_stays_an_uncertain_failure():
    transport = FakeTransport(send_error=TimeoutError("timed out"))
    with pytest.raises(cal.CalibrationError) as excinfo:
        cal._send_script(transport, "SAVE_CONFIG")
    assert "The send did not succeed" in str(excinfo.value)


def test_urllib_signals_sent_then_timed_out_differently_from_never_connected():
    # The real transport (spool_sync._http) against a server that accepts
    # the request and never answers, like Moonraker during a long gcode:
    # a bare TimeoutError, which a long calibration send treats as sent.
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    received = []

    def accept_and_hold():
        conn, _addr = server.accept()
        received.append(conn.recv(65536))
        threading.Event().wait(2)
        conn.close()

    threading.Thread(target=accept_and_hold, daemon=True).start()

    def held(method, path, body=None):
        return spool_sync._http(f"http://127.0.0.1:{port}", method, path, body, timeout=0.3)

    try:
        cal._send_script(held, "AUTO_FULL_BED_LEVEL", long_running=True)
        assert received and received[0].startswith(b"POST /printer/gcode/script")
    finally:
        server.close()

    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()

    def refused(method, path, body=None):
        return spool_sync._http(f"http://127.0.0.1:{closed_port}", method, path, body,
                                timeout=0.3)

    with pytest.raises(cal.CalibrationError):
        cal._send_script(refused, "AUTO_FULL_BED_LEVEL", long_running=True)


# ------------------------------------------------------------------ full console store

FULL_STORE = [f"// earlier line {n}" for n in range(1000)]


def test_pid_result_found_when_moonraker_console_store_is_full():
    # Moonraker keeps the newest 1000 entries: the count stays at 1000
    # after the send, so a count baseline would never see the report.
    transport = pid_transport(store=FULL_STORE)
    prepared = cal.prepare_pid(transport, AD5X, "extruder", 240)
    result = cal.execute_pid(transport, prepared, poll_attempts=2, poll_interval_s=0,
                             sleep=NO_SLEEP)
    assert len(transport.store) == 1000
    assert result.after.kp == pytest.approx(24.85)


def test_shaper_and_belts_results_found_when_the_console_store_is_full():
    transport = FakeTransport(store=FULL_STORE,
                              store_appends={"ZSHAPER": SHAPER_REPORT_ZMOD,
                                             "BELTS_SHAPER_CALIBRATION": BELTS_REPORT})
    facts = cal.fetch_facts(transport)
    shaper = cal.execute_shaper(transport, cal.prepare_shaper(transport, AD5X, facts),
                                poll_attempts=2, poll_interval_s=0, sleep=NO_SLEEP)
    assert shaper.after == cal.ShaperValues("zv", 53.2, "mzv", 54.2)
    text = cal.execute_belts(transport, cal.prepare_belts(transport, AD5X, facts),
                             poll_interval_s=0, sleep=NO_SLEEP, quiet_polls=2)
    assert "62 Hz" in text and "earlier line" not in text


# ------------------------------------------------------------------ mesh

def test_mesh_flow_zmod_screenless_measures_without_saving_then_accepts_once():
    transport = FakeTransport()
    _t, facts = zmod_facts(transport)
    prepared = cal.prepare_mesh(transport, AD5X, facts)
    prompt = cal.mesh_prompt_text(prepared)
    assert "0.420" in prompt  # before range presented
    assert "SAVE=0" in prepared.script and "AUTO_FULL_BED_LEVEL" in prepared.script
    result = cal.execute_mesh(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    text = cal.mesh_result_text(result)
    assert "0.420 mm → 0.180 mm (-0.240 mm)" in text
    # No save of any kind before the accept; the heaters go off right after measuring.
    assert transport.scripts == [prepared.script, "TURN_OFF_HEATERS"]
    assert result.cooled and "Heaters turned off after measuring (checked)." in text
    message = cal.accept_mesh(transport, result)
    assert transport.scripts == [prepared.script, "TURN_OFF_HEATERS",
                                 "BED_MESH_PROFILE LOAD=print_handy",
                                 "BED_MESH_PROFILE SAVE=auto",
                                 "BED_MESH_PROFILE REMOVE=print_handy", "SAVE_CONFIG"]
    assert transport.scripts.count("SAVE_CONFIG") == 1
    assert "Saved" in message


def test_mesh_cooldown_leaves_heaters_alone_when_a_print_started_meanwhile():
    # A print started from the screen while the mesh was read back keeps its heaters.
    transport = FakeTransport(states=("standby", "standby", "printing"))
    _t, facts = zmod_facts(transport)
    prepared = cal.prepare_mesh(transport, AD5X, facts)
    result = cal.execute_mesh(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    assert "TURN_OFF_HEATERS" not in transport.scripts
    assert result.cooled is None
    assert "Heaters left as they are" in cal.mesh_result_text(result)


def test_mesh_result_warns_when_the_heaters_cannot_be_confirmed_off():
    transport = FakeTransport(heaters_stuck=True)
    _t, facts = zmod_facts(transport)
    prepared = cal.prepare_mesh(transport, AD5X, facts)
    result = cal.execute_mesh(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    assert result.cooled is False and result.after.range == pytest.approx(0.18)
    assert "could not confirm the heaters turned off" in cal.mesh_result_text(result)


def test_mesh_discard_reloads_the_previous_profile_and_never_saves():
    transport = FakeTransport()
    _t, facts = zmod_facts(transport)
    prepared = cal.prepare_mesh(transport, AD5X, facts)
    result = cal.execute_mesh(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    cal.discard_mesh(transport, result)
    assert transport.scripts[-1] == "BED_MESH_PROFILE LOAD=auto"
    assert "BED_MESH_PROFILE REMOVE=print_handy" in transport.scripts
    assert "SAVE_CONFIG" not in transport.scripts
    assert not any(s.startswith("BED_MESH_PROFILE SAVE") for s in transport.scripts)


def test_mesh_native_install_is_refused_for_a_run_and_reported_instead():
    transport = FakeTransport(variables=NATIVE_VARIABLES)
    facts = cal.fetch_facts(transport)
    with pytest.raises(cal.CalibrationRefusal) as excinfo:
        cal.prepare_mesh(transport, AD5X, facts)
    assert "native screen owns the bed mesh" in str(excinfo.value)
    assert transport.posts == []
    report = cal.mesh_report_text(AD5X, facts)
    assert "0.420" in report and "stock screen" in report


def test_mesh_generic_klipper_uses_bed_mesh_calibrate():
    transport = FakeTransport(objects=KLIPPER_OBJECTS, settings_map=KLIPPER_SETTINGS,
                              variables={})
    facts = cal.fetch_facts(transport)
    prepared = cal.prepare_mesh(transport, GENERIC, facts)
    assert prepared.script == "BED_MESH_CALIBRATE PROFILE=print_handy"
    result = cal.execute_mesh(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    assert result.after.range == pytest.approx(0.18)
    cal.accept_mesh(transport, result)
    assert "BED_MESH_PROFILE SAVE=default" in transport.scripts


def test_mesh_zmod_saves_to_and_compares_against_the_profile_prints_load():
    # ZMod START_PRINT loads MESH=auto; Print Handy's own saved-mesh
    # readiness check uses the same name.
    assert cal.MESH_PROFILES["zmod"] == model_job.MESH_PROFILE == "auto"
    # Nothing active: the before value is the saved 'auto' profile (range
    # 0.42), not the scratch print_handy profile (range 0.20).
    transport = FakeTransport()
    facts = cal.fetch_facts(FakeTransport(variables=SCREENLESS_VARIABLES))
    facts = cal.PrinterFacts(objects=facts.objects, macros=facts.macros,
                             settings=facts.settings, variables=facts.variables,
                             bed_mesh=bed_mesh_status(matrix=[], profile_name=""))
    prepared = cal.prepare_mesh(transport, AD5X, facts)
    assert prepared.profile == "auto"
    assert prepared.before.range == pytest.approx(0.42)
    assert "profile 'auto'" in cal.mesh_result_text(
        cal.MeshResult(prepared=prepared, after=prepared.before))


def test_mesh_zmod_uses_the_screenless_contract_temperatures():
    contract = json.loads((ROOT / "config/mesh-interface.json").read_text())
    transport = FakeTransport()
    _t, facts = zmod_facts(transport)
    script = cal.prepare_mesh(transport, AD5X, facts).script
    assert f"EXTRUDER_TEMP={contract['hotend_target']}" in script
    assert f"BED_TEMP={contract['bed_target']}" in script
    assert "SAVE=0" in script and "SAVE=0" in contract["probe"]


# ------------------------------------------------------------------ accept/discard idle guard

def test_pid_accept_and_discard_refuse_when_a_print_started_meanwhile():
    for action in (cal.accept_pid, cal.discard_pid):
        transport = pid_transport(states=("standby", "standby", "printing"))
        prepared = cal.prepare_pid(transport, AD5X, "extruder", 240)
        result = cal.execute_pid(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
        with pytest.raises(cal.CalibrationRefusal):
            action(transport, result)
        assert transport.scripts == ["PID_CALIBRATE HEATER=extruder TARGET=240"]


def test_mesh_and_shaper_accept_refuse_when_a_print_started_meanwhile():
    transport = FakeTransport(states=("standby", "standby", "printing"))
    _t, facts = zmod_facts(transport)
    prepared = cal.prepare_mesh(transport, AD5X, facts)
    result = cal.execute_mesh(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    for action in (cal.accept_mesh, cal.discard_mesh):
        with pytest.raises(cal.CalibrationRefusal):
            action(transport, result)
    # The print that started meanwhile also keeps its heaters (no cooldown sent).
    assert transport.scripts == [prepared.script]
    transport = FakeTransport(states=("standby", "standby", "printing"),
                              store_appends={"ZSHAPER": SHAPER_REPORT_ZMOD})
    facts = cal.fetch_facts(transport)
    result = cal.execute_shaper(transport, cal.prepare_shaper(transport, AD5X, facts),
                                poll_interval_s=0, sleep=NO_SLEEP)
    with pytest.raises(cal.CalibrationRefusal):
        cal.accept_shaper(transport, result)
    assert transport.scripts == ["ZSHAPER"]


# ------------------------------------------------------------------ shaper

def test_shaper_zmod_parses_recommendations_and_gates_the_save():
    transport = FakeTransport(store_appends={"ZSHAPER": SHAPER_REPORT_ZMOD})
    _t, facts = zmod_facts(transport)
    prepared = cal.prepare_shaper(transport, AD5X, facts)
    prompt = cal.shaper_prompt_text(prepared)
    assert "mzv @ 36.8 Hz" in prompt  # before values presented
    result = cal.execute_shaper(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    assert result.after == cal.ShaperValues("zv", 53.2, "mzv", 54.2)
    text = cal.shaper_result_text(result)
    assert "X axis: mzv @ 36.8 Hz → zv @ 53.2 Hz (+16.4 Hz)" in text
    assert "Y axis: mzv @ 40.1 Hz → mzv @ 54.2 Hz (+14.1 Hz)" in text
    assert transport.scripts == ["ZSHAPER"]  # nothing saved before accept
    cal.accept_shaper(transport, result)
    assert transport.scripts == ["ZSHAPER", "SAVE_CONFIG"]


def test_shaper_discard_sends_nothing_at_all():
    transport = FakeTransport(store_appends={"ZSHAPER": SHAPER_REPORT_ZMOD})
    _t, facts = zmod_facts(transport)
    prepared = cal.prepare_shaper(transport, AD5X, facts)
    result = cal.execute_shaper(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    before_posts = list(transport.scripts)
    message = cal.discard_shaper(transport, result)
    assert transport.scripts == before_posts  # discard is not a send
    assert "nothing was sent" in message


def test_shaper_generic_klipper_uses_shaper_calibrate_format():
    transport = FakeTransport(objects=KLIPPER_OBJECTS, settings_map=KLIPPER_SETTINGS,
                              variables={},
                              store_appends={"SHAPER_CALIBRATE": SHAPER_REPORT_KLIPPER})
    facts = cal.fetch_facts(transport)
    prepared = cal.prepare_shaper(transport, GENERIC, facts)
    result = cal.execute_shaper(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    assert result.after.type_x == "ei" and result.after.freq_x == 41.5
    assert result.after.type_y == "mzv" and result.after.freq_y == 38.0


# ------------------------------------------------------------------ screws

def test_screws_parse_the_documented_klipper_output():
    reading = cal.parse_screw_lines(SCREWS_FIRST)
    assert [s.name for s in reading.screws] == ["front left screw", "front right screw",
                                                "rear right screw"]
    assert reading.screws[0].is_base and reading.screws[1].adjust == "CW 01:15"
    errors = reading.errors()
    assert errors["front right screw"] == pytest.approx(-0.1275, abs=0.001)
    assert errors["rear right screw"] == pytest.approx(0.2275, abs=0.001)


def test_screws_measure_and_remeasure_show_the_deltas():
    transport = FakeTransport(objects=KLIPPER_OBJECTS, settings_map=KLIPPER_SETTINGS,
                              variables={},
                              store_appends={"SCREWS_TILT_CALCULATE": SCREWS_FIRST})
    facts = cal.fetch_facts(transport)
    cal.prepare_screws(transport, GENERIC, facts)
    first = cal.measure_screws(transport, poll_interval_s=0, sleep=NO_SLEEP)
    text = cal.screws_text(first)
    assert "turn CW 01:15" in text
    assert f"{first.errors()['front right screw']:.3f} mm" in text
    transport.store_appends["SCREWS_TILT_CALCULATE"] = SCREWS_SECOND
    second = cal.measure_screws(transport, poll_interval_s=0, sleep=NO_SLEEP)
    text = cal.screws_text(second, first)
    assert "was" in text and "front right screw" in text  # delta vs the first reading shown
    assert second.errors()["front right screw"] > first.errors()["front right screw"]
    differences = cal.screws_differences(first, second)
    assert differences["front right screw"]["delta"] == pytest.approx(0.115, abs=0.001)
    assert "SAVE_CONFIG" not in " ".join(transport.scripts)


def test_screws_on_zmod_is_a_guide_not_a_run():
    transport = FakeTransport()
    facts = cal.fetch_facts(transport)
    with pytest.raises(cal.CalibrationRefusal):
        cal.prepare_screws(transport, AD5X, facts)
    assert "BED_LEVEL_SCREWS_TUNE" in cal.SCREWS_ZMOD_GUIDE
    assert "does NOT reset the temperatures" in cal.SCREWS_ZMOD_GUIDE


# ------------------------------------------------------------------ belts

def test_belts_report_only_run_applies_and_saves_nothing():
    transport = FakeTransport(
        store_appends={"BELTS_SHAPER_CALIBRATION": BELTS_REPORT})
    facts = cal.fetch_facts(transport)
    prepared = cal.prepare_belts(transport, AD5X, facts)
    text = cal.execute_belts(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP,
                             quiet_polls=2)
    assert "62 Hz" in text
    assert transport.scripts == ["BELTS_SHAPER_CALIBRATION"]
    assert "SAVE_CONFIG" not in " ".join(transport.scripts)


# ------------------------------------------------------------------ z offset (Stage 0 verdict)

def test_zoffset_screenless_reports_the_saved_store_and_its_arming():
    transport = FakeTransport()
    facts = cal.fetch_facts(transport)
    report = cal.zoffset_report(transport, AD5X, facts)
    assert "-1.185" in report and "LOAD_ZOFFSET=1 is armed" in report
    # The other store is shown as a separate value with its difference.
    assert "-1.240" in report and "(-0.055 mm" in report
    assert transport.posts == []
    # LOAD_ZOFFSET_NATIVE has no reverse direction.
    assert "the other way" not in report
    assert "START=0" in report and "START=1" in report


def test_zoffset_guidance_never_offers_a_reverse_load_zoffset_native():
    for variables in (SCREENLESS_VARIABLES, NATIVE_VARIABLES):
        transport = FakeTransport(variables=variables)
        report = cal.zoffset_report(transport, AD5X, cal.fetch_facts(transport))
        assert "LOAD_ZOFFSET_NATIVE the other way" not in report


def test_zoffset_native_reports_the_screen_store_as_the_owner():
    transport = FakeTransport(variables=NATIVE_VARIABLES)
    facts = cal.fetch_facts(transport)
    report = cal.zoffset_report(transport, AD5X, facts)
    assert "zProbeOffset): -1.240" in report
    assert "loads at every print start" in report
    assert "only be adjusted **during a print**" in report
    assert transport.posts == []


def test_zoffset_generic_klipper_is_the_probe_calibrate_guide():
    transport = FakeTransport(objects=KLIPPER_OBJECTS, settings_map=KLIPPER_SETTINGS,
                              variables={})
    facts = cal.fetch_facts(transport)
    report = cal.zoffset_report(transport, GENERIC, facts)
    assert "TESTZ" in report and "PROBE_CALIBRATE" in report
    assert "1.850" in report


# ------------------------------------------------------------------ records

def test_records_round_trip_and_listing(tmp_path):
    path = tmp_path / "printers" / "p1" / cal.RECORDS_NAME
    record = cal.make_record(
        printer="Flashforge AD5X (ZMod)", calibration="pid_extruder", outcome="accepted",
        before={"kp": 22.2, "ki": 1.5, "kd": 105.0},
        after={"kp": 24.85, "ki": 1.72, "kd": 98.4},
        differences=cal.pid_differences(cal.PidValues(22.2, 1.5, 105.0),
                                        cal.PidValues(24.85, 1.72, 98.4)),
        detail="target 240 °C")
    cal.append_record(path, record)
    cal.append_record(path, cal.make_record(printer="Flashforge AD5X (ZMod)",
                                            calibration="mesh", outcome="discarded"))
    records = cal.read_records(path)
    assert [r["outcome"] for r in records] == ["discarded", "accepted"]  # newest first
    assert records[1]["differences"]["kp"]["delta"] == pytest.approx(2.65)
    text = cal.records_text(records, "Flashforge AD5X (ZMod)")
    assert "accepted and saved" in text and "discarded" in text
    assert "Kp 22.200 → 24.850 (+2.650)" in text
    stored = json.loads(path.read_text())
    assert stored[0]["calibration"] == "pid_extruder"
    assert stored[0]["timestamp"] and stored[0]["printer"] == "Flashforge AD5X (ZMod)"


def test_records_missing_file_is_an_empty_listing(tmp_path):
    assert cal.read_records(tmp_path / "nope.json") == []
    assert "No calibration records" in cal.records_text([], "your printer")


# ------------------------------------------------------------------ bot wiring

def test_bot_command_registered_with_help_and_llm_parity():
    assert bot_commands.by_name("print-calibration") is not None
    assert bot_commands.as_text("print-calibration", {}) == "/print-calibration"
    assert "/print-calibration" in bot_commands.help_text()
    assert len(bot_commands.help_text()) <= 2000
    # Menu, records and the Z-offset report are read-only, so the chat model
    # may name them; a run or an accept may never come from the model.
    assert llm.allowed_command("/print-calibration") == "/print-calibration"
    assert llm.allowed_command("/print-calibration records") == "/print-calibration records"
    assert llm.allowed_command("/print-calibration zoffset") == "/print-calibration zoffset"
    assert llm.allowed_command("/print-calibration mesh") is None
    assert llm.allowed_command("/print-calibration pid extruder") is None
    assert llm.allowed_command("/print-calibration shaper") is None


def test_records_filename_is_registered_as_a_state_file():
    from print_handy import state_files
    assert cal.RECORDS_NAME in state_files.KNOWN


class FakeAdapter:
    """Records everything the dispatch tries to show the user."""

    def __init__(self):
        self.sent = []
        self.clarified = []

    async def send(self, chat_id=None, content=None, metadata=None):
        self.sent.append(content)

    async def send_clarify(self, **kwargs):
        self.clarified.append(kwargs["question"])
        return S(success=True)


def _event(text):
    source = S(platform=S(value="discord"), profile="3dprinting", is_bot=False,
               profile_route_rejected=False, user_id="u", chat_id="c", thread_id=None)
    return S(source=source, text=text, internal=False)


def _gateway(adapter):
    return S(_is_user_authorized_for_source=lambda s: True,
             _delivery_adapter_for=lambda s: adapter,
             _session_key_for_source=lambda s: "session-key")


def _configured(key, connection):
    return mock.patch.object(printers, "current",
                             return_value=S(connection=connection, model=S(key=key)))


def _records_in(tmp_path):
    return mock.patch.object(settings, "printer_state_dir",
                             lambda printer_id=None, data=None: tmp_path)


def _scripted_confirm(answers):
    queue = list(answers)
    questions = []

    async def confirm(adapter, source, metadata, prefix, question, choices, timeout,
                      live_key=None):
        questions.append(question)
        assert queue, f"unexpected extra prompt: {question}"
        answer = queue.pop(0)
        assert answer in choices, f"{answer!r} not offered in {choices!r}"
        return answer

    confirm.questions = questions
    return confirm


def _fast_mesh_execute(real):
    def run(transport, prepared):
        return real(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    return run


def test_dispatch_menu_is_read_only():
    adapter = FakeAdapter()
    transport = FakeTransport()
    with _configured("flashforge.ad5x", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport):
        out = asyncio.run(plugin.calibration_dispatch(
            _event("/print-calibration"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "print calibration menu shown"}
    assert "Printer calibration — Flashforge AD5X (ZMod)" in adapter.sent[-1]
    assert transport.posts == []


def test_dispatch_refuses_a_vendor_fork_before_any_run():
    adapter = FakeAdapter()
    transport = FakeTransport()
    with _configured("elegoo.neptune4", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport):
        out = asyncio.run(plugin.calibration_dispatch(
            _event("/print-calibration pid extruder 240"), _gateway(adapter)))
    assert out["reason"] == "print calibration refused for this printer"
    assert "vendor fork" in adapter.sent[-1]
    assert transport.posts == []


def test_dispatch_pid_accept_flow_saves_once_and_writes_the_record(tmp_path):
    adapter = FakeAdapter()
    transport = pid_transport()
    confirm = _scripted_confirm(["240 °C (default)", cal.RUN_LABEL, cal.ACCEPT_LABEL])
    with _configured("flashforge.ad5x", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport), \
            mock.patch.object(plugin, "_confirm", confirm), \
            _records_in(tmp_path):
        out = asyncio.run(plugin.calibration_dispatch(
            _event("/print-calibration pid extruder"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "print calibration handled"}
    assert transport.scripts == ["PID_CALIBRATE HEATER=extruder TARGET=240", "SAVE_CONFIG"]
    assert "Kp: 22.200 → 24.850 (+2.650)" in " ".join(confirm.questions)
    records = cal.read_records(tmp_path / cal.RECORDS_NAME)
    assert len(records) == 1 and records[0]["outcome"] == "accepted"
    assert records[0]["calibration"] == "pid_extruder"
    assert records[0]["differences"]["kp"]["delta"] == pytest.approx(2.65)


def test_dispatch_pid_discard_flow_restarts_and_records_discarded(tmp_path):
    adapter = FakeAdapter()
    transport = pid_transport()
    confirm = _scripted_confirm([cal.RUN_LABEL, cal.DISCARD_LABEL])
    with _configured("flashforge.ad5x", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport), \
            mock.patch.object(plugin, "_confirm", confirm), \
            _records_in(tmp_path):
        out = asyncio.run(plugin.calibration_dispatch(
            _event("/print-calibration pid bed 60"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "print calibration handled"}
    assert transport.scripts == ["PID_CALIBRATE HEATER=heater_bed TARGET=60",
                                 "FIRMWARE_RESTART"]
    assert "SAVE_CONFIG" not in transport.scripts
    records = cal.read_records(tmp_path / cal.RECORDS_NAME)
    assert len(records) == 1 and records[0]["outcome"] == "discarded"


def test_dispatch_unanswered_result_sends_nothing_and_says_so(tmp_path):
    # A timed-out result prompt is not a Discard: a PID discard is
    # FIRMWARE_RESTART, which would kill a print started meanwhile.
    adapter = FakeAdapter()
    transport = pid_transport()
    answers = [cal.RUN_LABEL, None]

    async def confirm(adapter_, source, metadata, prefix, question, choices, timeout,
                      live_key=None):
        return answers.pop(0)

    with _configured("flashforge.ad5x", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport), \
            mock.patch.object(plugin, "_confirm", confirm), \
            _records_in(tmp_path):
        out = asyncio.run(plugin.calibration_dispatch(
            _event("/print-calibration pid extruder 240"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "print calibration result unanswered"}
    assert transport.scripts == ["PID_CALIBRATE HEATER=extruder TARGET=240"]
    assert adapter.sent[-1] == cal.UNANSWERED_TEXT
    assert "until Klipper restarts" in cal.UNANSWERED_TEXT
    records = cal.read_records(tmp_path / cal.RECORDS_NAME)
    assert len(records) == 1 and records[0]["outcome"] == "unanswered"


def test_dispatch_pid_failure_writes_a_failed_record(tmp_path):
    adapter = FakeAdapter()
    transport = pid_transport()
    confirm = _scripted_confirm([cal.RUN_LABEL])

    def failing_execute(transport_, prepared):
        raise cal.CalibrationError("The extruder PID calibration did not report its "
                                   "new values in the time Print Handy waited.")

    with _configured("flashforge.ad5x", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport), \
            mock.patch.object(plugin, "_confirm", confirm), \
            mock.patch.object(cal, "execute_pid", failing_execute), \
            _records_in(tmp_path):
        out = asyncio.run(plugin.calibration_dispatch(
            _event("/print-calibration pid extruder 240"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "print calibration failed"}
    assert transport.scripts == []  # the failing execute sent nothing in this stub
    records = cal.read_records(tmp_path / cal.RECORDS_NAME)
    assert len(records) == 1 and records[0]["outcome"] == "failed"


def test_dispatch_refuses_while_printing_before_any_prompt():
    adapter = FakeAdapter()
    transport = pid_transport(states=("printing",))
    confirm = _scripted_confirm([])
    with _configured("flashforge.ad5x", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport), \
            mock.patch.object(plugin, "_confirm", confirm):
        out = asyncio.run(plugin.calibration_dispatch(
            _event("/print-calibration pid extruder 240"), _gateway(adapter)))
    assert out["reason"] == "print calibration refused"
    assert "printing" in adapter.sent[-1]
    assert transport.posts == [] and confirm.questions == []


def test_dispatch_mesh_native_reports_instead_of_running(tmp_path):
    adapter = FakeAdapter()
    transport = FakeTransport(variables=NATIVE_VARIABLES)
    with _configured("flashforge.ad5x", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport), \
            _records_in(tmp_path):
        out = asyncio.run(plugin.calibration_dispatch(
            _event("/print-calibration mesh"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "print calibration mesh reported"}
    assert "native screen owns the bed mesh" in adapter.sent[-1]
    assert transport.posts == []
    records = cal.read_records(tmp_path / cal.RECORDS_NAME)
    assert records and records[0]["outcome"] == "reported"


def test_dispatch_mesh_accept_flow(tmp_path):
    adapter = FakeAdapter()
    transport = FakeTransport()
    confirm = _scripted_confirm([cal.RUN_LABEL, cal.ACCEPT_LABEL])
    with _configured("flashforge.ad5x", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport), \
            mock.patch.object(plugin, "_confirm", confirm), \
            mock.patch.object(cal, "execute_mesh", _fast_mesh_execute(cal.execute_mesh)), \
            _records_in(tmp_path):
        out = asyncio.run(plugin.calibration_dispatch(
            _event("/print-calibration mesh"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "print calibration handled"}
    assert transport.scripts == [
        "AUTO_FULL_BED_LEVEL PROFILE=print_handy SAVE=0 EXTRUDER_TEMP=220 BED_TEMP=60",
        "TURN_OFF_HEATERS",
        "BED_MESH_PROFILE LOAD=print_handy", "BED_MESH_PROFILE SAVE=auto",
        "BED_MESH_PROFILE REMOVE=print_handy", "SAVE_CONFIG"]
    records = cal.read_records(tmp_path / cal.RECORDS_NAME)
    assert records[0]["outcome"] == "accepted"
    assert records[0]["differences"]["range_mm"]["delta"] == pytest.approx(-0.24)


def test_dispatch_zoffset_report_and_records_listing(tmp_path):
    adapter = FakeAdapter()
    transport = FakeTransport()
    with _configured("flashforge.ad5x", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport), \
            _records_in(tmp_path):
        out = asyncio.run(plugin.calibration_dispatch(
            _event("/print-calibration zoffset"), _gateway(adapter)))
        assert out["reason"] == "print calibration zoffset shown"
        assert "-1.185" in adapter.sent[-1]
        out = asyncio.run(plugin.calibration_dispatch(
            _event("/print-calibration records"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "print calibration records shown"}
    assert "zoffset" in adapter.sent[-1] and "report/guide only" in adapter.sent[-1]
    assert transport.posts == []


def test_dispatch_screws_guide_on_zmod(tmp_path):
    adapter = FakeAdapter()
    transport = FakeTransport()
    with _configured("flashforge.ad5x", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport), \
            _records_in(tmp_path):
        out = asyncio.run(plugin.calibration_dispatch(
            _event("/print-calibration screws"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "print calibration screws guide shown"}
    assert "BED_LEVEL_SCREWS_TUNE" in adapter.sent[-1]
    assert transport.posts == []


def test_dispatch_ignores_other_text():
    assert asyncio.run(plugin.calibration_dispatch(
        _event("/print-calibrationx"), _gateway(FakeAdapter()))) is None
    assert asyncio.run(plugin.calibration_dispatch(
        _event("print-calibration"), _gateway(FakeAdapter()))) is None



# ------------------------------------------------- SAVE_CONFIG restarts Klipper

class RestartingTransport(FakeTransport):
    """SAVE_CONFIG drops the request like a live printer does, then Klipper
    is unreachable for `startup_reads` info reads before it reports ready."""

    def __init__(self, save_error, startup_reads=2, **kwargs):
        super().__init__(**kwargs)
        self.save_error = save_error
        self.startup_reads = startup_reads

    def __call__(self, method, path, body=None):
        if path == cal.GCODE_SCRIPT_PATH and (body or {}).get("script") == "SAVE_CONFIG":
            self.calls.append((method, path, body))
            raise self.save_error
        if path == cal.PRINTER_INFO_PATH:
            self.calls.append((method, path, body))
            if self.startup_reads > 0:
                self.startup_reads -= 1
                raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
            return {"result": {"state": "ready"}}
        return super().__call__(method, path, body)


def _http_error(code):
    return urllib.error.HTTPError(cal.GCODE_SCRIPT_PATH, code, "Klippy Disconnected", {}, None)


@pytest.mark.parametrize("error", [
    _http_error(503),
    ConnectionResetError(104, "Connection reset by peer"),
    urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer")),
])
def test_save_config_disconnect_is_the_expected_restart_not_a_failure(error):
    transport = RestartingTransport(error)
    cal._save_config(transport, sleep=NO_SLEEP)
    assert transport.scripts == ["SAVE_CONFIG"]
    reads = [path for method, path, _b in transport.calls if method == "GET"]
    assert reads.count(cal.PRINTER_INFO_PATH) == 3


def test_pid_before_reads_the_lowercase_keys_configfile_returns():
    settings_map = {"extruder": {"pid_kp": "22.2", "pid_ki": "1.5", "pid_kd": "105.0"}}
    assert cal._pid_before(settings_map, "extruder") == cal.PidValues(22.2, 1.5, 105.0)
    assert cal._pid_before(settings_map, "heater_bed") is None


def test_is_restart_disconnect_separates_presend_from_postsend_failures():
    # Post-send drops are the SAVE_CONFIG restart signature; a refused
    # connection or a wrapped connect timeout means the request never
    # reached Moonraker, so nothing was saved (P2-230).
    assert cal._is_restart_disconnect(TimeoutError("read timed out"))
    assert cal._is_restart_disconnect(ConnectionResetError(104, "Connection reset by peer"))
    assert cal._is_restart_disconnect(
        urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer")))
    assert not cal._is_restart_disconnect(ConnectionRefusedError(111, "Connection refused"))
    assert not cal._is_restart_disconnect(
        urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")))
    assert not cal._is_restart_disconnect(urllib.error.URLError(TimeoutError("timed out")))


@pytest.mark.parametrize("error", [
    ConnectionRefusedError(111, "Connection refused"),
    urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
    urllib.error.URLError(TimeoutError("timed out")),
])
def test_save_config_presend_failures_stay_uncertain_and_are_not_resent(error):
    transport = RestartingTransport(error, startup_reads=0)
    with pytest.raises(cal.CalibrationError) as excinfo:
        cal._save_config(transport, sleep=NO_SLEEP)
    assert "The send did not succeed" in str(excinfo.value)
    assert transport.scripts == ["SAVE_CONFIG"]


def test_save_config_other_errors_stay_uncertain_and_are_not_resent():
    transport = RestartingTransport(_http_error(400))
    with pytest.raises(cal.CalibrationError) as excinfo:
        cal._save_config(transport, sleep=NO_SLEEP)
    assert "The send did not succeed" in str(excinfo.value)
    assert transport.scripts == ["SAVE_CONFIG"]


def test_save_config_that_never_comes_back_ready_is_unconfirmed():
    transport = RestartingTransport(_http_error(503), startup_reads=10_000)
    with pytest.raises(cal.CalibrationError) as excinfo:
        cal._save_config(transport, attempts=3, sleep=NO_SLEEP)
    assert "not confirmed" in str(excinfo.value)
    assert transport.scripts == ["SAVE_CONFIG"]


def test_accept_mesh_reports_saved_after_the_live_503_restart():
    transport = RestartingTransport(_http_error(503))
    prepared = cal.prepare_mesh(transport, AD5X)
    result = cal.execute_mesh(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    text = cal.accept_mesh(transport, result, sleep=NO_SLEEP)
    assert "restarted and is ready" in text
    assert transport.scripts.count("SAVE_CONFIG") == 1
