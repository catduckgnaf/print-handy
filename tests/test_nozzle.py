"""Tests for nozzle validation, nozzle types, and abrasive safety guards (Issue #59)."""
import pytest

from print_handy import settings
from print_handy.core.nozzle import (
    AbrasiveNozzleError,
    NozzleError,
    NozzleTemperatureError,
    check_nozzle_material_safety,
    check_nozzle_temperature_safety,
    get_nozzle_type_adjustments,
    effective_nozzle_temperature,
    is_abrasive_material,
    normalize_nozzle_type,
    validate_nozzle_setting,
)


def test_normalize_durozzle_installed_label_and_reject_unknown():
    assert normalize_nozzle_type("DUROZZLE tungsten carbide 0.4 (nickel-plated copper)") == "tungsten_carbide"
    with pytest.raises(NozzleError, match="unknown nozzle type"):
        normalize_nozzle_type("unlisted printer nozzle")


def test_normalize_nozzle_type_canonical():
    assert normalize_nozzle_type("brass") == "brass"
    assert normalize_nozzle_type("hardened_steel") == "hardened_steel"
    assert normalize_nozzle_type("hardened steel") == "hardened_steel"
    assert normalize_nozzle_type("Hardened-Steel") == "hardened_steel"
    # "stock" is no longer a fixed alias: it resolves per model (tests/test_material_hardening.py).
    assert normalize_nozzle_type("ruby") == "ruby"
    assert normalize_nozzle_type("ruby tipped") == "ruby"
    assert normalize_nozzle_type("tungsten carbide") == "tungsten_carbide"
    assert normalize_nozzle_type("diamond") == "diamond"
    assert normalize_nozzle_type("diamond_pcd") == "diamond"


def test_normalize_nozzle_type_invalid():
    with pytest.raises(NozzleError, match="unknown nozzle type 'plastic'"):
        normalize_nozzle_type("plastic")


def test_is_abrasive_material():
    assert is_abrasive_material("PLA-CF") is True
    assert is_abrasive_material("PETG Carbon Fiber") is True
    assert is_abrasive_material("PA-GF") is True
    assert is_abrasive_material("Glow in the dark PLA") is True
    assert is_abrasive_material("Wood PLA") is True
    assert is_abrasive_material("Metal-fill Bronze") is True
    assert is_abrasive_material("Standard PLA") is False
    assert is_abrasive_material("PETG") is False
    assert is_abrasive_material("ABS") is False
    assert is_abrasive_material(None) is False


def test_check_nozzle_material_safety_abrasive_on_brass_raises():
    with pytest.raises(AbrasiveNozzleError, match="Refusing to slice abrasive material 'PLA-CF' on a brass nozzle"):
        check_nozzle_material_safety("brass", "PLA-CF")

    with pytest.raises(AbrasiveNozzleError):
        check_nozzle_material_safety("brass", "Wood PLA")


def test_check_nozzle_material_safety_abrasive_on_hardened_passes():
    # Hardened steel, ruby, tungsten carbide, diamond can handle abrasive materials
    for nozzle, material in (("hardened_steel", "PLA-CF"), ("ruby", "PETG-GF"),
                            ("tungsten_carbide", "Glow PLA"), ("diamond", "Metal-fill PLA")):
        assert check_nozzle_material_safety(nozzle, material) is None


def test_check_nozzle_material_safety_non_abrasive_on_brass_passes():
    for material in ("PLA", "PETG", None):
        assert check_nozzle_material_safety("brass", material) is None


def test_validate_nozzle_setting():
    dia, n_type = validate_nozzle_setting(0.4, "brass")
    assert dia == 0.4
    assert n_type == "brass"

    dia, n_type = validate_nozzle_setting("0.6", "hardened steel")
    assert dia == 0.6
    assert n_type == "hardened_steel"

    with pytest.raises(NozzleError, match="not supported"):
        validate_nozzle_setting(0.3, "brass", supported_sizes=(0.2, 0.4, 0.6))

    with pytest.raises(NozzleError, match="not supported"):
        validate_nozzle_setting(0.4, "brass", supported_types=("hardened_steel", "stainless_steel"))


def test_settings_validation_nozzle_size_and_type(monkeypatch):
    # Valid nozzle size & type for ad5x
    monkeypatch.setenv("PRINT_HANDY__PRINTER__MODEL", "flashforge.ad5x")
    monkeypatch.setenv("PRINT_HANDY__PRINTER__NOZZLE_DIAMETER_MM", "0.6")
    monkeypatch.setenv("PRINT_HANDY__PRINTER__NOZZLE_TYPE", "hardened_steel")
    cfg = settings.load(local=False)
    assert cfg["printer"]["nozzle_diameter_mm"] == 0.6
    assert cfg["printer"]["nozzle_type"] == "hardened_steel"

    # Unsupported nozzle size for ad5x (0.2 is not in supported (0.25, 0.4, 0.6, 0.8))
    monkeypatch.setenv("PRINT_HANDY__PRINTER__NOZZLE_DIAMETER_MM", "0.2")
    with pytest.raises(settings.SettingsError, match="nozzle_diameter_mm 0.2 is not supported by model 'flashforge.ad5x'"):
        settings.load(local=False)

    # Unsupported nozzle type for ad5x (brass is not in supported (stainless_steel, hardened_steel))
    monkeypatch.setenv("PRINT_HANDY__PRINTER__NOZZLE_DIAMETER_MM", "0.4")
    monkeypatch.setenv("PRINT_HANDY__PRINTER__NOZZLE_TYPE", "brass")
    with pytest.raises(settings.SettingsError, match="nozzle_type 'brass' is not supported by model 'flashforge.ad5x'"):
        settings.load(local=False)


def test_pre_slice_guard_abrasive_material_on_brass():
    from print_handy.core.slice_guard import pre_slice
    res = pre_slice(nozzle_type="brass", materials=["PLA-CF"], bed=(220, 220), filament_slots=1)
    assert res.status == "blocked"
    assert any("Refusing to slice abrasive material 'PLA-CF' on a brass nozzle" in msg for msg in res.blocked)

    # Multi-material check: abrasive in slot 2 while slot 1 is non-abrasive
    res_multi = pre_slice(nozzle_type="brass", materials=["PLA", "PLA-CF"], bed=(220, 220), filament_slots=2, tool_slots={0: 1, 1: 2})
    assert res_multi.status == "blocked"
    assert any("Refusing to slice abrasive material 'PLA-CF' on a brass nozzle" in msg for msg in res_multi.blocked)

    # Hardened steel should not be blocked
    res_ok = pre_slice(nozzle_type="hardened_steel", materials=["PLA-CF"], bed=(220, 220), filament_slots=1)
    assert res_ok.status != "blocked"


def test_printer_state_set_nozzle_validation():
    from print_handy.core import printer_state
    from unittest import mock

    # Mock printer_state.load
    st = {
        "moonraker_url": "http://127.0.0.1:7125",
        "spoolman_url": "http://127.0.0.1:7912",
        "nozzle": {"diameter_mm": 0.4, "type": "hardened_steel"},
        "spool": {"spoolman_id": 1, "name": "Test Spool", "material": "PLA"},
        "temps_c": {"nozzle": 220, "bed": 60},
    }
    with mock.patch.object(printer_state, "load", return_value=st), \
         mock.patch.object(printer_state, "save"), \
         mock.patch("sys.argv", ["printer_state.py", "set", "--nozzle", "0.6", "--no-sync-klipper"]):
        printer_state.main()
        assert st["nozzle"]["diameter_mm"] == 0.6

    # Test invalid nozzle size
    with mock.patch.object(printer_state, "load", return_value=st), \
         mock.patch("sys.argv", ["printer_state.py", "set", "--nozzle", "0.5", "--no-sync-klipper"]), \
         pytest.raises(SystemExit):
        printer_state.main()

    # Test invalid nozzle type for current model (AD5X does not support brass)
    with mock.patch.object(printer_state, "load", return_value=st), \
         mock.patch("sys.argv", ["printer_state.py", "set", "--nozzle-type", "brass", "--no-sync-klipper"]), \
         pytest.raises(SystemExit):
        printer_state.main()


def test_untuned_nozzle_profile_refusal(tmp_path):
    from print_handy.core import orca_adapter
    from print_handy.core.nozzle import UntunedNozzleError
    # State specifying 0.5mm nozzle when no tuned profile exists for 0.5mm
    mock_state = {
        "nozzle": {"diameter_mm": 0.5, "type": "hardened_steel"},
        "spool": {"material": "PLA", "density": 1.24},
        "temps_c": {"nozzle": 220, "bed": 60},
    }
    dummy_stl = tmp_path / "cube.stl"
    dummy_stl.write_text("solid test\nendsolid test\n")
    with pytest.raises(UntunedNozzleError, match="no tuned profile exists for 0.5 mm"):
        orca_adapter.slice_model(dummy_stl, tmp_path / "out", mock_state)


def test_toml_backed_nozzle_profiles():
    from print_handy.core import nozzle

    # Test loading and lookup from nozzles.toml
    prof_04 = nozzle.get_tuned_nozzle_profile(0.4)
    assert prof_04 is not None
    assert prof_04.diameter_mm == 0.4
    assert prof_04.line_width == 0.42
    assert prof_04.prime_tower_width == 45

    prof_08 = nozzle.get_tuned_nozzle_profile(0.8)
    assert prof_08 is not None
    assert prof_08.diameter_mm == 0.8
    assert prof_08.line_width == 0.84
    assert prof_08.prime_tower_width == 75


def test_tuned_nozzle_profiles_configured(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from print_handy.core import orca_adapter, slice_guard

    monkeypatch.setattr(orca_adapter, "pre_slice", lambda **kwargs: slice_guard.Result())
    monkeypatch.setattr(orca_adapter, "post_slice", lambda *_args, **_kwargs: slice_guard.Result())
    monkeypatch.setattr(orca_adapter.sg, "enforce_approved_baseline", lambda *_args, **_kwargs: slice_guard.Result())
    monkeypatch.setattr(orca_adapter, "finalize_zmod", lambda *_args: None)

    def fake_subprocess_run(cmd, **kwargs):
        out_dir = None
        for i, arg in enumerate(cmd):
            if arg == "--outputdir" and i + 1 < len(cmd):
                out_dir = Path(cmd[i + 1])
                break
        if out_dir:
            (out_dir / "plate_1.gcode").write_text("; generated by OrcaSlicer 2.4.2\n")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(orca_adapter.subprocess, "run", fake_subprocess_run)

    dummy_stl = tmp_path / "part.stl"
    dummy_stl.write_text("solid x\nfacet normal 0 0 0\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid x\n")

    # Test 0.6 mm nozzle generation
    state_06 = {
        "moonraker_url": "http://127.0.0.1:7125",
        "spoolman_url": "http://127.0.0.1:7912",
        "nozzle": {"diameter_mm": 0.6, "type": "hardened_steel"},
        "spool": {"spoolman_id": 1, "material": "PLA", "density": 1.24},
        "temps_c": {"nozzle": 220, "bed": 60},
        "filament_slots": {"1": {"slot": 1, "spoolman_id": 1, "material": "PLA", "physical_color_verified": True, "hex": "FFFFFF"}},
    }
    _out, _prov, configs = orca_adapter.slice_model(dummy_stl, tmp_path / "out_06", state_06)
    machine_cfg = json.loads(configs[0].read_text())
    process_cfg = json.loads(configs[1].read_text())

    assert machine_cfg["nozzle_diameter"] == ["0.6"]
    assert machine_cfg["printer_variant"] == "0.6"
    assert machine_cfg["min_layer_height"] == ["0.15"]
    assert machine_cfg["max_layer_height"] == ["0.42"]
    assert machine_cfg["retraction_length"] == ["1.2"]
    assert process_cfg["layer_height"] == "0.32"
    assert process_cfg["line_width"] == "0.63"


def test_printer_state_cli_persistence(tmp_path, monkeypatch):
    """Verify set --nozzle and set --nozzle-type persist to disk through load/save round-trip."""
    import json
    from print_handy.core import printer_state

    state_file = tmp_path / "printer_hardware.json"
    initial_st = {
        "moonraker_url": "http://127.0.0.1:7125",
        "spoolman_url": "http://127.0.0.1:7912",
        "nozzle": {"diameter_mm": 0.4, "type": "stainless_steel", "installed": "2026-10-01"},
        "spool": {"spoolman_id": 1, "name": "Test Spool", "material": "PLA", "diameter_mm": 1.75, "density": 1.24},
        "temps_c": {"nozzle": 220, "bed": 60},
        "z_offset_verified": True,
        "z_offset_mm": -0.05,
    }
    state_file.write_text(json.dumps(initial_st, indent=2) + "\n")

    monkeypatch.setattr(printer_state, "STATE", str(state_file))

    # 1. Update nozzle diameter via CLI
    monkeypatch.setattr("sys.argv", ["printer_state.py", "set", "--nozzle", "0.6", "--no-sync-klipper"])
    printer_state.main()

    saved_st = json.loads(state_file.read_text())
    assert saved_st["nozzle"]["diameter_mm"] == 0.6
    assert saved_st["nozzle"]["type"] == "stainless_steel"
    assert saved_st["z_offset_verified"] is False
    assert saved_st["z_offset_mm"] is None

    # Verify load() reads exactly what was persisted
    loaded_st = printer_state.load()
    assert loaded_st["nozzle"]["diameter_mm"] == 0.6
    assert loaded_st["nozzle"]["type"] == "stainless_steel"

    # 2. Update nozzle type via CLI
    monkeypatch.setattr("sys.argv", ["printer_state.py", "set", "--nozzle-type", "hardened_steel", "--no-sync-klipper"])
    printer_state.main()

    saved_st2 = json.loads(state_file.read_text())
    assert saved_st2["nozzle"]["diameter_mm"] == 0.6
    assert saved_st2["nozzle"]["type"] == "hardened_steel"

    loaded_st2 = printer_state.load()
    assert loaded_st2["nozzle"]["type"] == "hardened_steel"


def test_nozzle_type_characteristics_and_adjustments(monkeypatch):
    monkeypatch.setattr(settings, "current", lambda: {"nozzle_temp_offsets": {}})
    brass = get_nozzle_type_adjustments("brass")
    assert brass["max_temperature_c"] == 300.0
    assert get_nozzle_type_adjustments("brass")["temp_offset_c"] == 0.0
    # #513 defaults: high-conductivity types stay at the brass baseline;
    # hardened/stainless steel default to +5 C (documented +5..+10 C band).
    for nozzle_type in ("ruby", "tungsten_carbide", "diamond"):
        assert get_nozzle_type_adjustments(nozzle_type)["temp_offset_c"] == 0.0
    for nozzle_type in ("hardened_steel", "stainless_steel"):
        assert get_nozzle_type_adjustments(nozzle_type)["temp_offset_c"] == 5.0
    assert brass["flow_multiplier"] == 1.0

    hardened = get_nozzle_type_adjustments("hardened steel")
    assert hardened["max_temperature_c"] == 450.0
    assert hardened["temp_offset_c"] == 5.0
    assert hardened["flow_multiplier"] == 0.95
    monkeypatch.setattr(settings, "current", lambda: {"nozzle_temp_offsets": {"hardened_steel": 5}})
    assert get_nozzle_type_adjustments("hardened_steel")["temp_offset_c"] == 5.0
    assert effective_nozzle_temperature("hardened_steel", 220) == 225.0
    with pytest.raises(NozzleTemperatureError, match="exceeds maximum safe limit of 450"):
        effective_nozzle_temperature("hardened_steel", 446)
    monkeypatch.setattr(settings, "current", lambda: {"nozzle_temp_offsets": {}})

    stainless = get_nozzle_type_adjustments("stainless steel")
    assert stainless["max_temperature_c"] == 300.0
    assert stainless["temp_offset_c"] == 5.0
    assert stainless["flow_multiplier"] == 0.95

    ruby = get_nozzle_type_adjustments("ruby")
    assert ruby["max_temperature_c"] == 500.0
    assert ruby["flow_multiplier"] == 1.0

    carbide = get_nozzle_type_adjustments("tungsten carbide")
    assert carbide["max_temperature_c"] == 500.0
    assert carbide["flow_multiplier"] == 1.05


def test_nozzle_temperature_safety(monkeypatch):
    base = settings.load(local=__import__("pathlib").Path(__import__("os").devnull), environ={})
    monkeypatch.setattr(settings, "current", lambda: {**base, "nozzle_temp_offsets": {}})
    # Brass nozzle within limit
    check_nozzle_temperature_safety("brass", 220)
    check_nozzle_temperature_safety("brass", 300)
    # Brass nozzle exceeding limit
    with pytest.raises(NozzleTemperatureError, match="exceeds maximum safe limit of 300"):
        check_nozzle_temperature_safety("brass", 305)

    # Hardened steel within limit
    check_nozzle_temperature_safety("hardened_steel", 400)
    # Hardened steel exceeding limit
    base_config = settings.current()
    monkeypatch.setattr(settings, "current", lambda: {**base_config, "nozzle_temp_offsets": {"hardened_steel": 5}})
    with pytest.raises(NozzleTemperatureError, match="exceeds maximum safe limit of 450"):
        effective_nozzle_temperature("hardened_steel", 446)


def test_pre_slice_guard_nozzle_temperature():
    from print_handy.core.slice_guard import pre_slice

    # Brass with temperature > 300 should block
    res = pre_slice(nozzle_type="brass", nozzle_temp=310, bed=(220, 220), filament_slots=1)
    assert res.status == "blocked"
    assert any("exceeds maximum safe limit of 300" in msg for msg in res.blocked)

    # Hardened steel with 310 should pass
    res_ok = pre_slice(nozzle_type="hardened_steel", nozzle_temp=310, bed=(220, 220), filament_slots=1)
    assert res_ok.status != "blocked"


def test_orca_adapter_thermal_and_flow_adjustments(tmp_path, monkeypatch):
    from pathlib import Path
    import json
    from print_handy.core import orca_adapter, slice_guard

    monkeypatch.setattr(orca_adapter, "pre_slice", lambda **kwargs: slice_guard.Result())
    monkeypatch.setattr(orca_adapter, "post_slice", lambda *_args, **_kwargs: slice_guard.Result())
    monkeypatch.setattr(orca_adapter.sg, "enforce_approved_baseline", lambda *_args, **_kwargs: slice_guard.Result())
    monkeypatch.setattr(orca_adapter, "finalize_zmod", lambda *_args: None)

    def fake_run(command, **_kwargs):
        out_dir = Path(command[command.index("--outputdir") + 1])
        output = out_dir / "plate_1.gcode"
        output.write_text("; generated by OrcaSlicer 2.4.2\nG90\nG1 X1 Y1 Z0.2\nEND_PRINT\n")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(orca_adapter.subprocess, "run", fake_run)

    dummy_stl = tmp_path / "part.stl"
    dummy_stl.write_text("solid x\nfacet normal 0 0 0\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid x\n")

    state = {
        "moonraker_url": "http://127.0.0.1:7125",
        "spoolman_url": "http://127.0.0.1:7912",
        "nozzle": {"diameter_mm": 0.4, "type": "hardened_steel"},
        "spool": {"spoolman_id": 1, "material": "PLA", "density": 1.24},
        "temps_c": {"nozzle": 220, "bed": 60},
        "filament_slots": {"1": {"slot": 1, "spoolman_id": 1, "material": "PLA", "physical_color_verified": True, "hex": "FFFFFF"}},
    }

    base_config = settings.current()
    monkeypatch.setattr(settings, "current", lambda: {**base_config, "nozzle_temp_offsets": {"hardened_steel": 5}})
    _output, _provenance, configs = orca_adapter.slice_model(dummy_stl, tmp_path / "out", state)

    machine_cfg = json.loads(configs[0].read_text())
    filament_cfg = json.loads(configs[2].read_text())

    # Machine config checks
    assert machine_cfg["nozzle_type"] == "hardened_steel"
    assert machine_cfg["nozzle_temperature_range_high"] == ["450"]

    # Filament config checks:
    # 220 base + 5 temp offset = 225
    assert filament_cfg["nozzle_temperature"] == ["225"]
    assert filament_cfg["nozzle_temperature_initial_layer"] == ["225"]
    # max nozzle temperature guard must reject an offset that pushes above 450 °C
    monkeypatch.setattr(settings, "current", lambda: {**base_config, "nozzle_temp_offsets": {"hardened_steel": 10}})
    from print_handy.core.nozzle import NozzleTemperatureError
    with pytest.raises(NozzleTemperatureError, match="exceeds maximum safe limit of 450"):
        orca_adapter.slice_model(dummy_stl, tmp_path / "too_hot", {**state, "temps_c": {"nozzle": 445, "bed": 60}})
    # flow ratio 1.0 * 0.95 = 0.95
    assert filament_cfg["filament_flow_ratio"] == ["0.95"]

    # Missing state type falls back to the configured printer setting.
    configured = {**base_config, "printer": {**base_config["printer"], "nozzle_type": "stainless_steel"}}
    monkeypatch.setattr(settings, "current", lambda: configured)
    state_without_type = {**state, "nozzle": {"diameter_mm": 0.4}}
    _output, _provenance, fallback_configs = orca_adapter.slice_model(
        dummy_stl, tmp_path / "settings-nozzle-type", state_without_type)
    fallback_machine = json.loads(fallback_configs[0].read_text())
    assert fallback_machine["nozzle_type"] == "stainless_steel"




def test_shipped_offsets_match_the_nozzle_data_defaults():
    """config/home.yaml and home.toml ship [nozzle_temp_offsets] and override nozzles.toml. They once
    shipped 0.0 for every type, which silently turned off the #513 steel compensation (+5 C)."""
    import tomllib
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "print_handy" / "data" / "nozzles.toml").read_text())["types"]
    expected = {name: float(values["temp_offset_c"]) for name, values in data.items()}
    shipped_yaml = yaml.safe_load((root / "config" / "home.yaml").read_text())["nozzle_temp_offsets"]
    shipped_toml = tomllib.loads((root / "config" / "home.toml").read_text())["nozzle_temp_offsets"]
    assert {k: float(v) for k, v in shipped_yaml.items()} == expected
    assert {k: float(v) for k, v in shipped_toml.items()} == expected
    loaded = settings.load(local=False, environ={})["nozzle_temp_offsets"]
    assert loaded["hardened_steel"] == loaded["stainless_steel"] == 5.0


def test_shipped_offsets_raise_a_steel_nozzle_by_five(monkeypatch):
    """With the shipped settings (no test pin), a steel nozzle slices 5 C above the material baseline."""
    from print_handy.core import nozzle

    shipped = settings.load(local=False, environ={})
    monkeypatch.setattr(settings, "current", lambda: shipped)
    assert nozzle.effective_nozzle_temperature("stainless_steel", 210) == 215
    assert nozzle.effective_nozzle_temperature("hardened_steel", 220) == 225
    assert nozzle.effective_nozzle_temperature("brass", 210) == 210
    assert nozzle.effective_nozzle_temperature("tungsten_carbide", 210) == 210

