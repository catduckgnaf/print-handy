import pytest

from print_handy import printers, settings
from print_handy.core import model_job, printer_state, spoolman_client


def test_live_check_spoolman_offline_is_warning_not_fatal(monkeypatch):
    config = settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": "klipper.generic"})
    adapter = printers.get("klipper.generic", config)
    monkeypatch.setattr(printers, "current", lambda config=None: adapter)
    monkeypatch.setattr(settings, "current", lambda: {**config, "services": {"spoolman_url": "http://configured-spoolman"}})
    urls = []

    def get(url, data=None, method=None, timeout=6):
        urls.append(url)
        if "configfile" in url:
            return {"result": {"status": {"configfile": {"settings": {
                "extruder": {"nozzle_diameter": "0.4", "pressure_advance": {"value": 0.04}}
            }}}}}
        if "spool_id" in url:
            return {"result": {"spool_id": 7}}
        raise AssertionError(f"Spoolman must go through spoolman_client, not _get (#387): {url}")

    def spoolman_down(method, path, body=None, *, base_url=None, timeout=5.0):
        urls.append(f"{base_url}{path}")
        raise spoolman_client.SpoolmanUnavailable("connection refused")

    monkeypatch.setattr(printer_state, "_get", get)
    monkeypatch.setattr(spoolman_client, "request", spoolman_down)
    monkeypatch.setattr(printer_state, "STATE", "/nonexistent-for-check")
    state = {
        "moonraker_url": "http://printer.test", "spoolman_url": "http://configured-spoolman",
        "nozzle": {"diameter_mm": 0.4},
        "spool": {"spoolman_id": 7, "name": "Black PLA", "material": "PLA"},
        "temps_c": {"nozzle": 210, "bed": 60},
    }
    ok, problems, facts = printer_state.check(state)
    assert ok, problems
    assert problems == []
    assert facts["warnings"] == ["Spoolman could not be reached, so the recorded spool was used instead; "
                                 "printing can continue (#235)"]
    assert "connection refused" not in facts["warnings"][0]  # raw detail demoted (#467)
    assert any(url.startswith("http://configured-spoolman/") for url in urls)


def test_verify_gcode_continues_when_spoolman_offline(monkeypatch, tmp_path, capsys):
    config = settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": "klipper.generic"})
    adapter = printers.get("klipper.generic", config)
    monkeypatch.setattr(printers, "current", lambda config=None: adapter)
    state = {
        "moonraker_url": "http://printer.test", "spoolman_url": "http://spoolman.test",
        "printer": "Test Klipper", "nozzle": {"diameter_mm": 0.4, "type": "brass"},
        "spool": {"spoolman_id": 7, "name": "Black PLA", "material": "PLA", "density": 1.24},
        "temps_c": {"nozzle": 210, "bed": 60},
        "axis_limits_mm": {"x": [0, 223], "y": [0, 223], "z": [0, 250]},
    }
    monkeypatch.setattr(printer_state, "load", lambda: state)
    monkeypatch.setattr(printer_state, "check", lambda st, **_kw: (True, [], {
        "warnings": ["Spoolman lookup failed: timed out"]
    }))
    monkeypatch.setattr(model_job, "_mr", lambda *a, **k: {"result": {"status": {"save_variables": {"variables": {}}}}})
    monkeypatch.setattr(model_job, "_native", lambda: False)
    monkeypatch.setattr(model_job, "cached_motion_bounds", lambda *a: {"x": (10, 20), "y": (10, 20), "z": (0.2, 0.4)})
    gcode = tmp_path / "plate.gcode"
    gcode.write_text("""; nozzle_diameter = 0.4
; filament_type = PLA
; first_layer_height = 0.2
PRINT_START BED=60 HOTEND=210
G90
;LAYER_CHANGE
;TYPE:Outer wall
;WIDTH:0.42
G1 X10 Y10 Z0.2 E1
;LAYER_CHANGE
;TYPE:Outer wall
;WIDTH:0.42
G1 X20 Y20 Z0.4 E2
PRINT_END
""")
    assert model_job.verify_gcode(gcode)
    assert "WARNING: Spoolman lookup failed: timed out" in capsys.readouterr().err
