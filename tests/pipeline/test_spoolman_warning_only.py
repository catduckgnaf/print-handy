"""#235: Spoolman is advisory only — it must warn, never block slicing or printing.

Regression context: during the 2.1.2 cutover, printer_hardware.json still
pointed at a decommissioned Spoolman host while the bundled Spoolman lived on
http://127.0.0.1:8000. printer_state.check() turned the connection-refused
lookup into a hardware problem, verify_gcode() raised, and the slice was
discarded. These tests pin the replacement behaviour:

- check() reports every Spoolman outcome (unreachable, archived, renamed,
  missing) as a warning in facts, never as a problem; ok is unaffected.
- The Spoolman URL comes from settings (services.spoolman_url) when set, and
  only falls back to the hardware record's cached URL otherwise.
- verify_gcode() proceeds on the recorded state and prints the warning.
- orca_adapter's derived-plate slot check warns on any Spoolman outcome and
  slices on the slot's operator-verified record.
"""
import copy
import io
import json
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import pytest

from print_handy import printers, settings
from print_handy.core import model_job, orca_adapter, printer_state, spoolman_client

FIXTURE = Path(__file__).parent / "fixtures" / "printer_hardware.json"

GENERIC_STATE = {"moonraker_url": "http://printer.test:7125", "printer": "Test Klipper",
                 "nozzle": {"diameter_mm": 0.4, "type": "brass"}, "temps_c": {"nozzle": 215, "bed": 60},
                 "spool": {"spoolman_id": 7, "name": "Black PLA", "material": "PLA"},
                 "spoolman_url": "http://stale.test:7912"}

GCODE = """; nozzle_diameter = 0.4
; filament_type = PLA
; first_layer_height = 0.2
PRINT_START BED=60 HOTEND=215
G90
;LAYER_CHANGE
;TYPE:Outer wall
;WIDTH:0.42
G1 X10 Y10 Z0.2 E1
;LAYER_CHANGE
;WIDTH:0.42
G1 X20 Y20 Z0.4 E2
PRINT_END
"""


def use_generic(monkeypatch):
    data = settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": "klipper.generic"})
    adapter = printers.get("klipper.generic", data)
    monkeypatch.setattr(printers, "current", lambda config=None: adapter)
    return adapter


def moonraker_get(spool_reply):
    """printer_state._get stand-in for the Moonraker legs only (#387).

    Spoolman no longer travels over _get: it goes through
    print_handy.core.spoolman_client, faked by spoolman_request below.
    """
    def get(url, data=None, method=None, timeout=6):
        if "/api/v1/spool/" in url:
            raise AssertionError("Spoolman must go through spoolman_client, not _get (#387)")
        if "configfile" in url:
            return {"result": {"status": {"configfile": {"settings": {"extruder": {"nozzle_diameter": "0.4"}}}}}}
        if "spool_id" in url:
            return {"result": {"spool_id": 7}}
        raise AssertionError(f"unexpected url {url}")
    return get


def spoolman_request(spool_reply, contacted=None):
    """spoolman_client.request stand-in: returns spool_reply, or raises it as an outage.

    A transport/HTTP exception becomes SpoolmanUnavailable carrying the
    original message, exactly as the real client wraps it.
    """
    def request(method, path, body=None, *, base_url=None, timeout=5.0):
        if contacted is not None:
            contacted.append((method, path, base_url))
        if isinstance(spool_reply, Exception):
            raise spoolman_client.SpoolmanUnavailable(str(spool_reply))
        return spool_reply
    return request


def test_spoolman_unreachable_is_a_warning_not_a_problem(monkeypatch):
    use_generic(monkeypatch)
    down = urllib.error.URLError("Connection refused")
    with mock.patch.object(printer_state, "_get", moonraker_get(down)), \
         mock.patch.object(spoolman_client, "request", spoolman_request(down)):
        ok, probs, facts = printer_state.check(dict(GENERIC_STATE))
    assert ok is True
    assert probs == []
    assert any("Spoolman could not be reached" in w and "recorded spool was used" in w for w in facts["warnings"])
    assert all("Connection refused" not in w for w in facts["warnings"])  # raw detail demoted (#467)
    assert facts["spoolman_warnings"] is facts["warnings"]
    assert "spool_remaining_g" not in facts


def test_spoolman_archived_and_renamed_are_warnings(monkeypatch):
    use_generic(monkeypatch)
    reply = {"id": 7, "archived": True, "remaining_weight": 120,
             "filament": {"name": "Renamed PLA", "material": "PLA"}}
    with mock.patch.object(printer_state, "_get", moonraker_get(reply)), \
         mock.patch.object(spoolman_client, "request", spoolman_request(reply)):
        ok, probs, facts = printer_state.check(dict(GENERIC_STATE))
    assert ok is True and probs == []
    assert any("archived" in w for w in facts["warnings"])
    assert any("Renamed PLA" in w for w in facts["warnings"])
    assert facts["spool_remaining_g"] == 120


def test_spoolman_missing_record_is_a_warning(monkeypatch):
    use_generic(monkeypatch)
    missing = urllib.error.HTTPError("u", 404, "not found", {}, None)
    with mock.patch.object(printer_state, "_get", moonraker_get(missing)), \
         mock.patch.object(spoolman_client, "request", spoolman_request(missing)):
        ok, probs, facts = printer_state.check(dict(GENERIC_STATE))
    assert ok is True and probs == []
    assert any("Spoolman could not be reached" in w for w in facts["warnings"])


def test_offline_check_spoolman_down_still_ok(monkeypatch):
    stub = mock.Mock(connection="offline", transport=None, has_native_screen=False)
    stub.readiness_problems = lambda st, snapshot: []
    monkeypatch.setattr(printers, "current", lambda config=None: stub)
    with mock.patch.object(printer_state, "_get", side_effect=urllib.error.URLError("Connection refused")), \
         mock.patch.object(spoolman_client, "request",
                           spoolman_request(urllib.error.URLError("Connection refused"))):
        ok, probs, facts = printer_state.check(dict(GENERIC_STATE))
    assert ok is True and probs == []
    assert facts["connection"] == "offline"
    assert any("Spoolman could not be reached" in w for w in facts["warnings"])


def test_real_hardware_problem_still_blocks_despite_spoolman_warning(monkeypatch):
    """Warnings-only applies to Spoolman, not to the real hardware gates."""
    use_generic(monkeypatch)

    def get(url, data=None, method=None, timeout=6):
        if "configfile" in url:
            return {"result": {"status": {"configfile": {"settings": {"extruder": {"nozzle_diameter": "0.6"}}}}}}
        if "spool_id" in url:
            return {"result": {"spool_id": 7}}
        raise AssertionError(url)

    with mock.patch.object(printer_state, "_get", get), \
         mock.patch.object(spoolman_client, "request",
                           spoolman_request(urllib.error.URLError("Connection refused"))):
        ok, probs, facts = printer_state.check(dict(GENERIC_STATE))
    assert ok is False
    assert any("nozzle_diameter" in p for p in probs)
    assert any("Spoolman could not be reached" in w for w in facts["warnings"])


# --------------------------------------------------------------------- URL resolution

def test_spoolman_url_prefers_settings_over_stale_record(monkeypatch):
    monkeypatch.setattr(printer_state.settings, "current",
                        lambda: {"services": {"spoolman_url": "http://configured.test:8000/"}})
    assert printer_state.spoolman_url(dict(GENERIC_STATE)) == "http://configured.test:8000"
    contacted = []
    reply = {"id": 7, "filament": {"name": "Black PLA", "material": "PLA"}}

    use_generic(monkeypatch)
    with mock.patch.object(printer_state, "_get", moonraker_get(reply)), \
         mock.patch.object(spoolman_client, "request", spoolman_request(reply, contacted)):
        ok, probs, facts = printer_state.check(dict(GENERIC_STATE))
    assert ok is True
    # The one Spoolman client was aimed at the settings URL, never the stale record.
    assert ("GET", "/api/v1/spool/7", "http://configured.test:8000") in contacted
    assert not any("stale.test" in str(base) for _m, _p, base in contacted)


def test_spoolman_url_falls_back_to_record_when_settings_empty(monkeypatch):
    monkeypatch.setattr(printer_state.settings, "current", lambda: {"services": {"spoolman_url": ""}})
    assert printer_state.spoolman_url(dict(GENERIC_STATE)) == "http://stale.test:7912"
    assert printer_state.spoolman_url({}) == ""
    assert printer_state.spoolman_url(None) == ""


def test_spoolman_url_survives_broken_settings(monkeypatch):
    def broken():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(printer_state.settings, "current", broken)
    assert printer_state.spoolman_url(dict(GENERIC_STATE)) == "http://stale.test:7912"


# --------------------------------------------------------------------- verify_gcode

def test_verify_gcode_proceeds_and_warns_when_spoolman_down(monkeypatch, tmp_path, capsys):
    use_generic(monkeypatch)
    monkeypatch.setattr(printer_state, "load", lambda: dict(GENERIC_STATE))
    down = urllib.error.URLError("Connection refused")
    with mock.patch.object(printer_state, "_get", moonraker_get(down)), \
         mock.patch.object(spoolman_client, "request", spoolman_request(down)):
        gcode = tmp_path / "plate_1.gcode"
        gcode.write_text(GCODE)
        assert model_job.verify_gcode(gcode) is True
    captured = capsys.readouterr()
    # Warning channel unified with #240 during the merge of origin/main:
    # check warnings are reported on stderr, the G-code verdict stays on stdout.
    assert "WARNING: Spoolman could not be reached" in captured.err
    assert "G-CODE OK" in captured.out


# --------------------------------------------------------------------- orca slot check

def _derived_state():
    st = copy.deepcopy(model_job.printer_state.load())
    st["spool"] = {"spoolman_id": 12, "name": "Dark Green PLA", "material": "PLA",
                   "diameter_mm": 1.75, "density": 1.24}
    return st


def _fake_slice(cmd, **kwargs):
    folder = Path(cmd[cmd.index("--outputdir") + 1])
    (folder / "plate_1.gcode").write_text(
        "; HEADER_BLOCK_START\n; generated by OrcaSlicer 2.4.2\n"
        "; filament_type = PLA+\n; HEADER_BLOCK_END\nT0\nG90\n")
    return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()


def _derived_source(tmp_path):
    source = tmp_path / "part.3mf"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("Metadata/project_settings.config", '{\n  "filament_colour": [\n    "#FFFFFF"\n  ]\n}\n')
        archive.writestr("3D/3dmodel.model", "<model/>")
    return source


def test_derived_plate_slices_when_spoolman_unreachable(tmp_path, capsys):
    st = _derived_state()
    with patch.object(orca_adapter.subprocess, "run", side_effect=_fake_slice), \
         patch.object(urllib.request, "urlopen", side_effect=urllib.error.URLError("Connection refused")):
        output, _, _ = orca_adapter.slice_model(_derived_source(tmp_path), tmp_path / "out", st,
                                                preserve_layout=True, filament_slot=2)
    assert output.exists()
    filament = json.loads((tmp_path / "out/filament.json").read_text())
    assert filament["filament_type"] == ["PLA"]  # the slot record's material, not a Spoolman answer
    # #387: the detail is in the WARNING log; the user gets the one line, once.
    assert capsys.readouterr().out.count(spoolman_client.UNAVAILABLE_NOTICE) == 1


def test_derived_plate_slices_when_spoolman_says_archived(tmp_path, capsys):
    st = _derived_state()
    spool = io.BytesIO(json.dumps({"archived": True, "filament": {"material": "PLA"}}).encode())
    with patch.object(orca_adapter.subprocess, "run", side_effect=_fake_slice), \
         patch.object(urllib.request, "urlopen", return_value=spool):
        output, _, _ = orca_adapter.slice_model(_derived_source(tmp_path), tmp_path / "out", st,
                                                preserve_layout=True, filament_slot=2)
    assert output.exists()
    assert "archived" in capsys.readouterr().out


def _moonraker_with_active_spool(active):
    def get(url, data=None, method=None, timeout=6):
        if "configfile" in url:
            return {"result": {"status": {"configfile": {"settings": {"extruder": {"nozzle_diameter": "0.4"}}}}}}
        if "spool_id" in url:
            return {"result": {"spool_id": active}}
        raise AssertionError(f"unexpected url {url}")
    return get


def test_live_moonraker_spool_wins_over_stale_record(monkeypatch):
    """A stale record naming #7 must not describe the wrong spool when Moonraker says #15."""
    use_generic(monkeypatch)
    reply = {"id": 15, "remaining_weight": 800, "filament": {"name": "Black", "material": "PLA+"}}
    contacted = []
    with mock.patch.object(printer_state, "_get", _moonraker_with_active_spool(15)), \
         mock.patch.object(spoolman_client, "request", spoolman_request(reply, contacted)):
        ok, probs, facts = printer_state.check(copy.deepcopy(GENERIC_STATE))
    assert ok is True and probs == []
    assert [p for _, p, _ in contacted] == ["/api/v1/spool/15"]
    assert facts["effective_spool_id"] == 15
    assert facts["effective_spool_source"] == "moonraker"
    assert facts["spool_name"] == "Black"
    assert "active_spool_mismatch" in facts
    assert any("live active spool is #15" in w and "recorded spool is #7" in w for w in facts["warnings"])
    # No bogus rename/material warnings comparing spool 15 against spool 7's cached label.
    assert not any("state says" in w or "material is" in w for w in facts["warnings"])


def test_matching_live_spool_keeps_recorded_checks(monkeypatch):
    use_generic(monkeypatch)
    reply = {"id": 7, "remaining_weight": 50, "filament": {"name": "Renamed PLA", "material": "PLA"}}
    contacted = []
    with mock.patch.object(printer_state, "_get", _moonraker_with_active_spool(7)), \
         mock.patch.object(spoolman_client, "request", spoolman_request(reply, contacted)):
        ok, probs, facts = printer_state.check(copy.deepcopy(GENERIC_STATE))
    assert [p for _, p, _ in contacted] == ["/api/v1/spool/7"]
    assert "effective_spool_id" not in facts
    assert any("Renamed PLA" in w for w in facts["warnings"])


def test_no_live_spool_falls_back_to_record(monkeypatch):
    use_generic(monkeypatch)
    reply = {"id": 7, "remaining_weight": 50, "filament": {"name": "Black PLA", "material": "PLA"}}
    contacted = []
    with mock.patch.object(printer_state, "_get", _moonraker_with_active_spool(None)), \
         mock.patch.object(spoolman_client, "request", spoolman_request(reply, contacted)):
        ok, probs, facts = printer_state.check(copy.deepcopy(GENERIC_STATE))
    assert [p for _, p, _ in contacted] == ["/api/v1/spool/7"]
    assert "effective_spool_id" not in facts
