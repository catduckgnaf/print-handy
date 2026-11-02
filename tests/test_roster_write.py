"""Roster writer (#537): append to a roster, convert a legacy single-printer
config, refuse safely. Everything runs on temp files through the real
settings loader; no printer is contacted."""
import json
from pathlib import Path

import pytest
import yaml

from print_handy import roster_write, settings
from print_handy.core import printer_state

HOME = settings.HOME

ROSTER_ONE = """
discord:
  chat_id: "111"
printers:
  - id: garage-a1
    name: Garage A1
    model: bambulab.a1
    lan_host: 192.0.2.60
    lan_serial: "00M00A000000000"
    lan_access_code_env: BAMBU_LAN_PASSWORD_GARAGE_A1
"""

LEGACY_MOONRAKER = """
discord:
  chat_id: "111"
printer:
  model: flashforge.ad5x
  name: Garage AD5X
services:
  moonraker_url: http://192.0.2.10:7125
  spoolman_url: http://spoolman:8000
"""

LEGACY_BAMBU = """
printer:
  model: bambulab.a1
bambu:
  lan_host: 192.0.2.60
  lan_serial: "00M00A000000000"
  lan_access_code_env: BAMBU_LAN_PASSWORD_A1
"""

NEW_BAMBU = {"id": "bench-p1", "model": "bambulab.p1s", "name": "Bench P1",
             "lan_host": "192.0.2.61", "lan_serial": "01P00A000000000",
             "lan_access_code_env": "BAMBU_LAN_PASSWORD_BENCH_P1"}


def _write(tmp_path, text, name="local.yaml"):
    path = tmp_path / name
    path.write_text(text)
    return path


def _load(path, environ=None):
    return settings.load(home=HOME, local=path, environ=environ or {})


def _add(path, entry, **kw):
    kw.setdefault("environ", {})
    kw.setdefault("home", HOME)
    kw.setdefault("timestamp", "20261028-120000")
    return roster_write.add_printer(entry, config_path=path, **kw)


# ---------------------------------------------------------------------- append

def test_append_to_existing_roster(tmp_path):
    path = _write(tmp_path, ROSTER_ONE)
    result = _add(path, NEW_BAMBU)
    assert result["converted"] is False
    assert result["printer_ids"] == ["garage-a1", "bench-p1"]
    assert result["backup"].is_file()
    assert result["backup"].read_text() == ROSTER_ONE
    data = _load(path)
    assert [e["id"] for e in data["printers"]] == ["garage-a1", "bench-p1"]
    first = data["printers"][0]
    assert first["lan_access_code_env"] == "BAMBU_LAN_PASSWORD_GARAGE_A1"
    parsed = yaml.safe_load(path.read_text())
    assert parsed["printers"][1]["lan_access_code_env"] == "BAMBU_LAN_PASSWORD_BENCH_P1"
    assert "chat_id" not in parsed["printers"][1]  # no channel of its own: shared channel (#536)


# ---------------------------------------------------------------------- conversion

def test_legacy_moonraker_config_converts(tmp_path):
    path = _write(tmp_path, LEGACY_MOONRAKER)
    result = _add(path, {"id": "bench-ender", "model": "creality.ender3v3ke",
                         "name": "Bench Ender", "moonraker_url": "http://192.0.2.11:7125"})
    assert result["converted"] is True
    assert result["printer_ids"] == ["garage-ad5x", "bench-ender"]
    parsed = yaml.safe_load(path.read_text())
    first, second = parsed["printers"]
    assert first == {"id": "garage-ad5x", "model": "flashforge.ad5x", "name": "Garage AD5X",
                     "moonraker_url": "http://192.0.2.10:7125"}
    assert second["moonraker_url"] == "http://192.0.2.11:7125"
    # The legacy per-printer keys are gone; the global Spoolman URL stays.
    assert "printer" not in parsed
    assert parsed["services"] == {"spoolman_url": "http://spoolman:8000"}
    data = _load(path)
    assert data["printers"][0]["moonraker_url"] == "http://192.0.2.10:7125"
    assert data["services"]["spoolman_url"] == "http://spoolman:8000"


def test_legacy_bambu_conversion_carries_lan_and_custom_secret_name(tmp_path):
    path = _write(tmp_path, LEGACY_BAMBU)
    result = _add(path, {"id": "bench-ad5x", "model": "flashforge.ad5x", "name": "Bench AD5X",
                         "moonraker_url": "http://192.0.2.12:7125"})
    assert result["converted"] is True
    first = yaml.safe_load(path.read_text())["printers"][0]
    assert first["lan_host"] == "192.0.2.60" and first["lan_serial"] == "00M00A000000000"
    assert first["lan_access_code_env"] == "BAMBU_LAN_PASSWORD_A1"
    assert "bambu" not in yaml.safe_load(path.read_text())
    data = _load(path)
    assert data["printers"][0]["lan_access_code_env"] == "BAMBU_LAN_PASSWORD_A1"


def test_conversion_keeps_a_nondefault_derived_url(tmp_path):
    text = LEGACY_MOONRAKER.replace(
        "  moonraker_url: http://192.0.2.10:7125\n",
        "  moonraker_url: http://192.0.2.10:7125\n  moonraker_ws_url: ws://192.0.2.10:9999/websocket\n")
    path = _write(tmp_path, text)
    _add(path, {"id": "bench-ender", "model": "creality.ender3v3ke", "name": "Bench Ender",
                "moonraker_url": "http://192.0.2.11:7125"})
    first = yaml.safe_load(path.read_text())["printers"][0]
    assert first["moonraker_ws_url"] == "ws://192.0.2.10:9999/websocket"


def test_conversion_keeps_an_explicit_camera_url_without_moonraker(tmp_path):
    # An explicit camera URL with no Moonraker URL to derive one from must
    # survive conversion: it was only copied inside the Moonraker branch,
    # and _strip_legacy then removed the original (P2-216).
    text = """
discord:
  chat_id: "111"
printer:
  model: flashforge.ad5x
  name: Garage AD5X
services:
  camera_snapshot_url: http://192.0.2.20/snap.jpg
"""
    path = _write(tmp_path, text)
    _add(path, {"id": "bench-ender", "model": "creality.ender3v3ke", "name": "Bench Ender",
                "moonraker_url": "http://192.0.2.11:7125"})
    first = yaml.safe_load(path.read_text())["printers"][0]
    assert first["camera_snapshot_url"] == "http://192.0.2.20/snap.jpg"
    data = _load(path)
    assert data["printers"][0]["camera_snapshot_url"] == "http://192.0.2.20/snap.jpg"


def _home_with_roster(tmp_path):
    """The shipped home layer plus a two-printer roster: an install whose
    roster lives in the home layer (the mixed-shape refusal checks only
    the local layer and the environment, never the defaults tree)."""
    raw = yaml.safe_load(Path(HOME).read_text())
    raw["printers"] = [
        {"id": "garage-a1", "name": "Garage A1", "model": "bambulab.a1",
         "lan_host": "192.0.2.60", "lan_serial": "00M00A000000000"},
        {"id": "workshop-ad5x", "name": "Workshop AD5X", "model": "flashforge.ad5x",
         "chat_id": "222", "moonraker_url": "http://192.0.2.10:7125"},
    ]
    path = tmp_path / "home.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def test_add_with_roster_inherited_from_home_keeps_every_printer(tmp_path):
    # The target file declares no roster of its own; the resolved roster
    # comes from the home layer. Converting only the first entry would
    # shadow the inherited roster and silently drop the rest (P2-217).
    home = _home_with_roster(tmp_path)
    path = _write(tmp_path, 'discord:\n  chat_id: "111"\n')
    result = _add(path, NEW_BAMBU, home=home)
    assert result["printer_ids"] == ["garage-a1", "workshop-ad5x", "bench-p1"]
    parsed = yaml.safe_load(path.read_text())
    assert [e["id"] for e in parsed["printers"]] == ["garage-a1", "workshop-ad5x", "bench-p1"]
    ad5x = parsed["printers"][1]
    assert ad5x["moonraker_url"] == "http://192.0.2.10:7125"
    assert ad5x["chat_id"] == "222"
    data = settings.load(home=home, local=path, environ={})
    assert [e["id"] for e in data["printers"]] == ["garage-a1", "workshop-ad5x", "bench-p1"]


def test_conversion_keeps_measured_geometry_and_does_not_pin_model_defaults(tmp_path):
    # A converted entry keeps its measured build volume, and the model's own
    # nozzle_type / tool_count defaults are not written into the file
    # (2.2.5 review of the per-entry geometry keys).
    home = _home_with_roster(tmp_path)
    raw = yaml.safe_load(Path(home).read_text())
    raw["printers"][1].update(bed_mm=[200, 190], max_print_height_mm=180)
    home.write_text(yaml.safe_dump(raw))
    path = _write(tmp_path, 'discord:\n  chat_id: "111"\n')
    _add(path, NEW_BAMBU, home=home)
    written = yaml.safe_load(path.read_text())["printers"]
    assert written[1]["bed_mm"] == [200, 190] and written[1]["max_print_height_mm"] == 180
    for entry in written[:1] + written[2:]:
        assert not {"bed_mm", "max_print_height_mm"} & set(entry)
    for entry in written:
        assert not {"nozzle_type", "tool_count"} & set(entry)
    data = settings.load(home=home, local=path, environ={})
    assert data["printers"][1]["bed_mm"] == [200.0, 190.0]
    assert data["printers"][1]["max_print_height_mm"] == 180.0


# ---------------------------------------------------------------------- refusals

def test_duplicate_id_and_name_refused_without_writing(tmp_path):
    path = _write(tmp_path, ROSTER_ONE)
    with pytest.raises(roster_write.RosterWriteError, match="already exists"):
        _add(path, {"id": "garage-a1", "model": "bambulab.p1s", "name": "Other"})
    with pytest.raises(roster_write.RosterWriteError, match="already exists"):
        _add(path, {"id": "other", "model": "bambulab.p1s", "name": "garage a1"})
    assert path.read_text() == ROSTER_ONE


def test_toml_target_refused(tmp_path):
    path = _write(tmp_path, 'title = "x"\n', name="local.toml")
    with pytest.raises(roster_write.RosterWriteError, match="TOML"):
        _add(path, NEW_BAMBU)
    assert path.read_text() == 'title = "x"\n'


def test_missing_file_refused(tmp_path):
    with pytest.raises(roster_write.RosterWriteError, match="no settings file"):
        _add(tmp_path / "local.yaml", NEW_BAMBU)


def test_docker_image_settings_file_refused_up_front(tmp_path, monkeypatch):
    # The image's /app/config/docker.yaml is root-owned and not on a volume.
    path = _write(tmp_path, ROSTER_ONE, name="docker.yaml")
    monkeypatch.setattr(roster_write, "_IMAGE_SETTINGS", path.resolve())
    with pytest.raises(roster_write.RosterWriteError, match="baked into the Docker image") as err:
        _add(path, NEW_BAMBU)
    assert "PRINT_HANDY_CONFIG" in str(err.value)
    assert path.read_text() == ROSTER_ONE
    assert list(tmp_path.iterdir()) == [path]


def test_unwritable_settings_file_refused_up_front(tmp_path, monkeypatch):
    path = _write(tmp_path, ROSTER_ONE)
    monkeypatch.setattr(roster_write.os, "access", lambda p, mode: False)
    with pytest.raises(roster_write.RosterWriteError, match="not writable") as err:
        _add(path, NEW_BAMBU)
    assert "PRINT_HANDY_CONFIG" in str(err.value)
    assert path.read_text() == ROSTER_ONE


def test_oserror_on_write_maps_to_the_refusal(tmp_path, monkeypatch):
    path = _write(tmp_path, ROSTER_ONE)

    def denied(*_a, **_k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(roster_write.tempfile, "mkstemp", denied)
    with pytest.raises(roster_write.RosterWriteError, match="Permission denied") as err:
        _add(path, NEW_BAMBU)
    assert "PRINT_HANDY_CONFIG" in str(err.value)
    assert path.read_text() == ROSTER_ONE


def test_write_refusal_names_the_image_file_and_passes_a_writable_one(tmp_path, monkeypatch):
    baked = _write(tmp_path, ROSTER_ONE, name="docker.yaml")
    monkeypatch.setattr(roster_write, "_IMAGE_SETTINGS", baked.resolve())
    refusal = roster_write.write_refusal({"PRINT_HANDY_CONFIG": str(baked)})
    assert refusal and "baked into the Docker image" in refusal and "PRINT_HANDY_CONFIG" in refusal
    mounted = _write(tmp_path, ROSTER_ONE, name="local.yaml")
    assert roster_write.write_refusal({"PRINT_HANDY_CONFIG": str(mounted)}) is None
    assert roster_write.write_refusal({"PRINT_HANDY_CONFIG": str(tmp_path / "absent.yaml")}) is None


def test_unroundtrippable_file_refused_byte_identical(tmp_path):
    text = "printer:\n  model: flashforge.ad5x\nprinter:\n  name: Dup\n"
    path = _write(tmp_path, text)
    with pytest.raises(roster_write.RosterWriteError, match="cannot be safely rewritten"):
        _add(path, NEW_BAMBU)
    assert path.read_text() == text


def test_invalid_new_entry_refused_by_real_validation(tmp_path):
    path = _write(tmp_path, ROSTER_ONE)
    bad = {"id": "bench-x", "model": "flashforge.ad5x", "name": "Bench X",
           "host": "192.0.2.5"}  # host only applies to SDCP / stock models
    with pytest.raises(roster_write.RosterWriteError, match="do not validate"):
        _add(path, bad)
    assert path.read_text() == ROSTER_ONE


def test_environment_legacy_offender_surfaces_on_conversion(tmp_path):
    path = _write(tmp_path, LEGACY_MOONRAKER)
    env = {"PRINT_HANDY__SERVICES__MOONRAKER_URL": "http://192.0.2.99:7125"}
    with pytest.raises(roster_write.RosterWriteError, match="PRINT_HANDY__SERVICES__MOONRAKER_URL"):
        _add(path, {"id": "bench-ender", "model": "creality.ender3v3ke", "name": "Bench Ender",
                    "moonraker_url": "http://192.0.2.11:7125"}, environ=env)
    assert path.read_text() == LEGACY_MOONRAKER


# ---------------------------------------------------------------------- hardware seed

def test_seed_hardware_record_creates_a_minimal_truthful_record(tmp_path, monkeypatch):
    state = tmp_path / "printer_hardware.json"
    monkeypatch.setattr(printer_state, "STATE", str(state))
    view = {"printer": {"name": "Bench P1", "bed_mm": [256, 256], "max_print_height_mm": 256,
                        "nozzle_diameter_mm": 0.4},
            "services": {"moonraker_url": ""},
            "material_temps": {"PLA": {"nozzle": 210, "bed": 60}}}
    assert printer_state.seed_hardware_record("bench-p1", view) is True
    record = printer_state.load("bench-p1")
    assert record["printer"] == "Bench P1"
    assert record["nozzle"] == {"diameter_mm": 0.4, "type": "stock",
                                "installed": record["nozzle"]["installed"]}
    assert record["spool"] == {} and record["filament_slots"] == {}
    assert record["temps_c"] == {"nozzle": 210, "bed": 60}
    assert (tmp_path / "printers" / "bench-p1" / "printer_hardware.json").is_file()
    # Never overwritten once it exists.
    assert printer_state.seed_hardware_record("bench-p1", view) is False
