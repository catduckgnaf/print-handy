"""Read-only thermal probe for a connected printer (#741, Stage 3 of #740).

Every transport is faked: no test opens a socket. The tests pin the
Moonraker object shapes named in the issue (chamber temperature_sensor,
the Sovol Zero temperature_fan, the Creator 5 Pro heater_generic, extra fan
objects, a door button, an exhaustive list with no match), both Bambu report
generations, an SDCP status frame, the PrusaLink schema check, the OctoPrint
cannot-probe path, and the per-printer record: declined or failed changes
nothing, confirmed lands as operator-confirmed and shows in the slice summary.
"""
import json
import urllib.parse

from print_handy.bot import guided
from print_handy.core import printer_state, thermal_probe
from print_handy.knowledge import ThermalCapabilities
from print_handy.knowledge import thermal as thermal_surface


def facts(findings):
    return {f.field: f.value for f in findings}


# ---------------------------------------------------------------------- Moonraker object shapes

def test_chamber_temperature_sensor_proposes_sensor_and_fresh_reading():
    found, note = thermal_probe.moonraker_findings(
        ["fan", "temperature_sensor chamber", "temperature_sensor mcu_temp", "print_stats"],
        {"temperature_sensor chamber": {"temperature": 24.3}})
    got = facts(found)
    assert got["chamber_sensor_present"] == "present"
    assert got["chamber_sensor_fresh_reading"] == "yes"
    assert "24.3 °C" in next(f.evidence for f in found if f.field == "chamber_sensor_present")
    assert got["fan_part_cooling_present"] == "present" and got["fan_part_cooling_mapping"] == "M106"
    assert "chamber_heater_present" not in got
    assert note == ""


def test_sovol_zero_shape_reads_the_chamber_from_a_temperature_fan():
    found, _note = thermal_probe.moonraker_findings(
        ["fan", "temperature_fan exhaust_fan"],
        {"temperature_fan exhaust_fan": {"temperature": 31.0, "speed": 0.0}})
    got = facts(found)
    assert got["chamber_sensor_present"] == "present"
    assert got["chamber_sensor_fresh_reading"] == "yes"
    assert got["fan_exhaust_present"] == "present"
    assert got["fan_exhaust_mapping"] == "temperature_fan exhaust_fan"


def test_creator5_pro_shape_reads_the_chamber_from_a_heater_generic():
    found, _note = thermal_probe.moonraker_findings(
        ["heater_generic chamber_heater"], {"heater_generic chamber_heater": {"temperature": 28.5, "target": 0}})
    got = facts(found)
    assert got["chamber_sensor_present"] == "present"
    assert got["chamber_sensor_fresh_reading"] == "yes"
    assert got["chamber_heater_present"] == "present"
    # Presence is never control: the probe has no opinion on chamber_heater_control.
    assert "chamber_heater_control" not in got


def test_extra_fan_objects_propose_presence_and_object_name_mappings():
    found, note = thermal_probe.moonraker_findings(
        ["fan", "fan_generic exhaust_fan", "fan_generic filter_fan", "fan_generic aux_fan",
         "heater_fan hotend_fan", "controller_fan board_fan"], {})
    got = facts(found)
    assert got["fan_exhaust_mapping"] == "fan_generic exhaust_fan"
    assert got["fan_filtration_mapping"] == "fan_generic filter_fan"
    assert got["fan_auxiliary_mapping"] == "fan_generic aux_fan"
    assert got["fan_auxiliary_present"] == "present"
    assert note == ""


def test_a_fan_with_an_unclear_name_blocks_absent_proposals_and_is_named():
    found, note = thermal_probe.moonraker_findings(["fan", "fan_generic chamber_fan"], {})
    got = facts(found)
    for role in ("auxiliary", "exhaust", "filtration"):
        assert f"fan_{role}_present" not in got
    assert "`fan_generic chamber_fan`" in note


def test_door_button_makes_door_state_knowable():
    found, _note = thermal_probe.moonraker_findings(
        ["gcode_button door_sensor"], {"gcode_button door_sensor": {"state": "RELEASED"}})
    assert facts(found)["door_lid_state_knowable"] == "yes"


def test_exhaustive_list_with_no_match_proposes_absent_and_no():
    found, note = thermal_probe.moonraker_findings(
        ["fan", "heater_bed", "extruder", "temperature_sensor raspberry_pi", "print_stats"], {})
    got = facts(found)
    assert got["chamber_sensor_present"] == "absent"
    assert got["chamber_sensor_fresh_reading"] == "no"
    assert got["door_lid_state_knowable"] == "no"
    for role in ("auxiliary", "exhaust", "filtration"):
        assert got[f"fan_{role}_present"] == "absent"
        assert f"fan_{role}_mapping" not in got
    assert note == ""


class _Response:
    def __init__(self, payload):
        self.status = 200
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _moonraker_opener(objects, status, seen):
    def opener(req, timeout=None):
        seen.append((req.get_method(), req.full_url))
        if req.full_url.endswith("/printer/objects/list"):
            return _Response({"result": {"objects": objects}})
        return _Response({"result": {"status": status}})
    return opener


def test_moonraker_probe_is_two_gets_on_the_object_endpoints():
    seen = []
    objects = ["fan", "temperature_sensor chamber", "extruder", "print_stats"]
    result = thermal_probe.probe(
        "moonraker", {"moonraker_url": "http://zero.local:7125/"},
        opener=_moonraker_opener(objects, {"print_stats": {"state": "standby"},
                                           "temperature_sensor chamber": {"temperature": 22}}, seen))
    assert result.probed
    assert facts(result.findings)["chamber_sensor_fresh_reading"] == "yes"
    assert [method for method, _url in seen] == ["GET", "GET"]
    assert seen[0][1] == "http://zero.local:7125/printer/objects/list"
    query = urllib.parse.unquote(seen[1][1])
    assert "/printer/objects/query?" in query and "temperature_sensor chamber" in query
    assert "extruder" not in query  # only the thermal objects and print_stats are queried


def test_moonraker_probe_skips_a_printing_printer_and_fails_soft():
    seen = []
    busy = thermal_probe.probe(
        "moonraker", {"moonraker_url": "http://zero.local:7125"},
        opener=_moonraker_opener(["print_stats"], {"print_stats": {"state": "printing"}}, seen))
    assert not busy.probed and busy.findings == () and "idle" in busy.note

    def broken(req, timeout=None):
        raise OSError("connection reset")
    failed = thermal_probe.probe("moonraker", {"moonraker_url": "http://zero.local:7125"}, opener=broken)
    assert not failed.probed and failed.findings == () and "nothing was proposed" in failed.note


# ---------------------------------------------------------------------- Bambu, SDCP, PrusaLink, OctoPrint

def test_bambu_x1_generation_report():
    found = thermal_probe.bambu_findings({"print": {
        "chamber_temper": 33, "cooling_fan_speed": "15", "big_fan1_speed": "0", "big_fan2_speed": "7"}})
    got = facts(found)
    assert got["chamber_sensor_present"] == "present" and got["chamber_sensor_fresh_reading"] == "yes"
    assert got["fan_part_cooling_mapping"] == "M106 P1"
    assert got["fan_auxiliary_mapping"] == "M106 P2"
    assert got["fan_exhaust_mapping"] == "M106 P3"
    assert "door_lid_state_knowable" not in got  # never probed on Bambu


def test_bambu_p2s_generation_report_reads_ctc_temp_low_bits():
    found = thermal_probe.bambu_findings({"print": {
        "device": {"ctc": {"info": {"temp": (5 << 16) | 29}}}, "cooling_fan_speed": "0"}})
    got = facts(found)
    assert got["chamber_sensor_fresh_reading"] == "yes"
    assert "29 °C" in next(f.evidence for f in found if f.field == "chamber_sensor_present")
    assert got["fan_auxiliary_present"] == "absent" and got["fan_exhaust_present"] == "absent"


def test_bambu_report_without_chamber_fields_proposes_absent():
    got = facts(thermal_probe.bambu_findings({"print": {"cooling_fan_speed": "0"}}))
    assert got["chamber_sensor_present"] == "absent" and got["chamber_sensor_fresh_reading"] == "no"


class _FakeBambu:
    def __init__(self, report):
        self.report = report

    def __call__(self, host, serial, code, timeout_s=None):
        return self

    def snapshot(self):
        return self.report


def test_bambu_probe_through_the_client_and_busy_refusal():
    ok = thermal_probe.probe("bambulab_lan", {"lan_host": "10.0.0.5", "lan_serial": "01P00A"},
                             {"access_code": "12345678"},
                             bambu_factory=_FakeBambu({"print": {"gcode_state": "IDLE", "chamber_temper": 25}}))
    assert ok.probed and "Door state is not probed" in ok.note
    busy = thermal_probe.probe("bambulab_lan", {"lan_host": "10.0.0.5", "lan_serial": "01P00A"},
                               {"access_code": "12345678"},
                               bambu_factory=_FakeBambu({"print": {"gcode_state": "RUNNING"}}))
    assert not busy.probed and busy.findings == ()


class _FakeSdcp:
    def __init__(self, frame):
        self.frame, self.closed = frame, False

    def __call__(self, host, request_timeout=None):
        return self

    def status(self):
        return self.frame

    def close(self):
        self.closed = True


def test_sdcp_status_frame_proposes_box_sensor_and_fan_channels():
    fake = _FakeSdcp({"Status": {"CurrentStatus": [0], "TempOfBox": 27.4,
                                 "CurrentFanSpeed": {"ModelFan": 0, "AuxiliaryFan": 0, "BoxFan": 0}}})
    result = thermal_probe.probe("elegoo_sdcp", {"host": "10.0.0.9"}, sdcp_factory=fake)
    got = facts(result.findings)
    assert result.probed and fake.closed
    assert got["chamber_sensor_present"] == "present" and got["chamber_sensor_fresh_reading"] == "yes"
    assert got["fan_part_cooling_present"] == "present"
    assert got["fan_auxiliary_present"] == "present"
    assert got["fan_exhaust_present"] == "present"


class _FakePrusa:
    def __init__(self, printer, firmware="6.2.0"):
        self.printer, self.firmware = printer, firmware

    def __call__(self, host, api_key):
        return self

    def info(self):
        return {"firmware": self.firmware}

    def status(self):
        return {"printer": self.printer}


def test_prusalink_schema_check_confirms_no_and_picks_up_a_future_field():
    today = thermal_probe.probe("prusalink", {"link_host": "10.0.0.7"}, {"api_key": "k"},
                                prusa_factory=_FakePrusa({"state": "IDLE", "temp_nozzle": 24, "temp_bed": 23}))
    got = facts(today.findings)
    assert got == {"chamber_sensor_fresh_reading": "no", "door_lid_state_knowable": "no"}
    assert "6.2.0" in today.findings[0].evidence
    future = thermal_probe.probe("prusalink", {"link_host": "10.0.0.7"}, {"api_key": "k"},
                                 prusa_factory=_FakePrusa({"state": "IDLE", "temp_chamber": 35.5,
                                                           "door_open": False}))
    got = facts(future.findings)
    assert got["chamber_sensor_present"] == "present" and got["chamber_sensor_fresh_reading"] == "yes"
    assert got["door_lid_state_knowable"] == "yes"


def test_octoprint_cannot_be_probed_and_makes_no_call():
    def no_network(req, timeout=None):
        raise AssertionError("OctoPrint must not be contacted by the thermal probe")
    result = thermal_probe.probe("octoprint", {"octoprint_url": "http://octopi.local"},
                                 {"api_key": "k"}, opener=no_network)
    assert not result.probed and result.findings == ()
    assert "cannot be probed" in result.note


# ---------------------------------------------------------------------- proposals and the per-printer record

def test_proposals_drop_agreeing_facts_and_name_a_conflicting_model_value():
    found, _note = thermal_probe.moonraker_findings(["fan"], {})
    model = ThermalCapabilities(fan_part_cooling_present="present", chamber_sensor_present="present")
    items = thermal_probe.proposals(thermal_probe.ThermalProbe(True, tuple(found)), model)
    fields = {p["field"]: p for p in items}
    assert "fan_part_cooling_present" not in fields  # agrees with the model: nothing to confirm
    assert fields["chamber_sensor_present"]["model_value"] == "present"
    assert fields["chamber_sensor_present"]["value"] == "absent"
    text = "\n".join(thermal_probe.proposal_lines(items))
    assert "The model record says present" in text


def _seed(tmp_path, monkeypatch, printer_id="zero"):
    monkeypatch.setattr(printer_state, "STATE", str(tmp_path / "printer_hardware.json"))
    record = {"printer": "Zero", "spool": {}, "filament_slots": {}}
    printer_state.save(record, printer_id)
    return printer_state.state_path(printer_id)


def test_declined_or_failed_probe_leaves_the_record_unchanged(tmp_path, monkeypatch):
    path = _seed(tmp_path, monkeypatch)
    before = path.read_bytes()
    assert thermal_probe.record_confirmed("zero", "sovol.zero", []) == 0
    assert thermal_probe.record_confirmed("zero", "sovol.zero", None) == 0
    failed = thermal_probe.probe("moonraker", {"moonraker_url": ""})
    assert thermal_probe.record_confirmed("zero", "sovol.zero",
                                          thermal_probe.proposals(failed, ThermalCapabilities())) == 0
    # A malformed proposal is not a fact either.
    assert thermal_probe.record_confirmed("zero", "sovol.zero", [{"field": "enclosure", "value": "open"}]) == 0
    assert path.read_bytes() == before


def test_accepted_proposal_is_stored_operator_confirmed_and_shows_in_the_slice_summary(tmp_path, monkeypatch):
    import datetime
    path = _seed(tmp_path, monkeypatch)
    accepted = [{"field": "fan_exhaust_present", "value": "present", "evidence": "`temperature_fan exhaust_fan`"},
                {"field": "fan_exhaust_mapping", "value": "temperature_fan exhaust_fan", "evidence": "object"}]
    assert thermal_probe.record_confirmed("zero", "sovol.zero", accepted,
                                          today=datetime.date(2026, 11, 1)) == 2
    stored = json.loads(path.read_text())["thermal_install"]
    assert stored["model"] == "sovol.zero"
    fact = stored["facts"]["fan_exhaust_mapping"]
    assert fact["provenance"] == "operator-confirmed"
    assert fact["source"] == "probed from the connected printer on 2026-11-01"
    assert json.loads(path.read_text())["printer"] == "Zero"  # the rest of the record is kept

    lines = "\n".join(thermal_surface.thermal_lines_for_model("sovol.zero", printer_id="zero"))
    assert "exhaust present (mapping temperature_fan exhaust_fan)" in lines
    assert "this printer: operator-confirmed, probed from the connected printer on 2026-11-01" in lines
    # Another model on the same record id, or no printer id, sees the model record only.
    assert thermal_probe.install_facts("zero", "sovol.sv08") == {}
    assert "operator-confirmed, probed" not in "\n".join(thermal_surface.thermal_lines_for_model("sovol.zero"))
    # Confirmed facts are no longer asked about in setup.
    entry = thermal_surface.thermal_entry_for_model("sovol.zero")
    asked = {q["field"] for q in thermal_surface.thermal_setup_questions(
        entry, thermal_probe.install_facts("zero", "sovol.zero"))}
    assert "fan_exhaust_mapping" not in asked

    from print_handy.runtime import discord_print_summary as summary
    gcode = tmp_path / "plate_1.gcode"
    gcode.write_text("; estimated printing time (normal mode) = 1h 0m 0s\n; total filament used [g] = 5\nG1 X1\n")
    # ABS: thermal lines ride only on a material that needs an enclosure.
    text = summary.format_slice_summary({"printer_model": "sovol.zero", "printer_id": "zero",
                                         "title": "Widget", "gcode": str(gcode),
                                         "filament_selection": {"slot": 1, "material": "ABS"}})
    assert "this printer: operator-confirmed" in text


def test_an_absent_fan_confirmation_clears_the_model_mapping():
    model = ThermalCapabilities(fan_auxiliary_present="present", fan_auxiliary_mapping="M106 P2")
    lines = "\n".join(thermal_surface.thermal_assumptions_lines(
        model, {"fan_auxiliary_present": {"value": "absent", "source": "probed from the connected printer on 2026-11-01"}}))
    assert "auxiliary absent (mapping unknown)" in lines


# ---------------------------------------------------------------------- guided onboarding

_PROPOSALS = [
    {"field": "chamber_sensor_fresh_reading", "value": "yes",
     "evidence": "`temperature_fan exhaust_fan` returned a live temperature (31 °C)", "model_value": "unknown"},
    {"field": "door_lid_state_knowable", "value": "no",
     "evidence": "the printer's complete object list has no door or lid switch", "model_value": "unknown"},
]


def _runner(proposals, note=""):
    def run(answers):
        return guided.ProbeOutcome(True, "the printer answered",
                                   extra={"thermal": {"proposals": proposals, "note": note}})
    return run


def _walk_to_test(tmp_path, runner):
    st = guided.SetupStore(tmp_path / "guided-setup.json")
    loader = lambda: {"printers": [{"id": "ad5x", "model": "flashforge.ad5x", "name": "Workshop AD5X",  # noqa: E731
                                    "moonraker_url": "http://ad5x.local:7125"}]}
    clock = [1000]

    def say(text, **kw):
        clock[0] += 10
        kw.setdefault("report", lambda: "REPORT")
        return guided.handle_text("k", text, store=st, now=clock[0], config_loader=loader,
                                  probe_runner=runner, **kw)
    for text in ("/print-handy add-printer", "sovol.zero", "Zero Bench", "http://zero.local:7125"):
        say(text)
    return st, say


def test_onboarding_proposes_thermal_facts_and_carries_only_confirmed_ones(tmp_path):
    st, say = _walk_to_test(tmp_path, _runner(_PROPOSALS))
    reply = say("test")
    assert "Thermal facts from the printer" in reply
    assert "1. Can the connected printer report a fresh chamber temperature reading" in reply
    assert "Proposed: **yes**" in reply
    reply = say("7")
    assert "does not work" in reply
    reply = say("1")
    assert "Ready to add this printer" in reply
    assert "1 of 2 confirmed (chamber_sensor_fresh_reading yes)" in reply
    applied = []
    reply = say("apply", onboard_apply=lambda answers: applied.append(answers) or "written.")
    assert "Printer onboarding complete" in reply
    assert applied[0]["thermal"] == [_PROPOSALS[0]]


def test_onboarding_none_records_nothing_and_no_proposals_skips_the_step(tmp_path):
    st, say = _walk_to_test(tmp_path, _runner(_PROPOSALS))
    say("test")
    reply = say("none")
    assert "0 of 2 confirmed (none)" in reply
    applied = []
    say("apply", onboard_apply=lambda answers: applied.append(answers) or "written.")
    assert applied[0]["thermal"] == []

    st2, say2 = _walk_to_test(tmp_path / "b", _runner([], note="OctoPrint has no chamber channel"))
    reply = say2("test")
    assert "Ready to add this printer" in reply
    assert "Thermal probe: OctoPrint has no chamber channel" in reply
