"""What the slicer receives per material, and the G-code guards that check it.

Reads the filament JSON slice_model hands Orca (the approach of test_slice_temperatures) for every in-repo
profile family and material: before this change every non-Bambu printer sliced every material with the
template's PLA profile (100 % fan, 12-25 mm3/s, PLA retraction). The values below are written out from
OrcaSlicer 2.4.2's "Generic <family> @System" profiles, not derived from print_handy/data/filament_families.toml,
so a wrong table fails here too.
"""
import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy import printers, settings
from print_handy.core import color_geometry, model_job, orca_adapter, printer_state, slice_guard, source_colors, spoolman_client

from tests.corpus import make_models as mm

from .test_slice_temperatures import (FAMILIES, MODEL, ROOT, STATE, adapter_for, nozzle_effective,  # noqa: F401
                                      slice_inputs, stale_state)

# (fan_min, fan_max, overhang_fan, overhang_threshold, close_fan_layers, max_volumetric_speed, range_low, range_high,
#  vitrification) per family, from Orca's Generic profiles.
TABLE = {
    "PETG": ("20", "100", "100", "95%", "3", "10", "220", "260", "70"),
    "ABS": ("10", "80", "80", "25%", "3", "12", "240", "280", "110"),
    "ASA": ("10", "80", "80", "25%", "3", "12", "240", "280", "110"),
    "TPU": ("100", "100", "100", "95%", "1", "3.2", "200", "250", "30"),
    "PA": ("0", "60", "30", "95%", "3", "8", "260", "300", "108"),
    "PC": ("10", "60", "60", "25%", "3", "12", "260", "290", "120"),
    "PLA-CF": ("100", "100", "100", "50%", "1", "12", "190", "240", "45"),
    "PETG-CF": ("5", "40", "100", "95%", "3", "11.5", "220", "260", "70"),
    "PA-CF": ("0", "60", "30", "95%", "3", "8", "260", "300", "108"),
    "ABS-CF": ("10", "80", "80", "25%", "3", "12", "240", "280", "110"),  # no Orca Generic ABS-CF: base row
    "PA-GF": ("0", "60", "30", "95%", "3", "8", "260", "300", "108"),     # no Orca Generic PA-GF: base row
}
KEYS = ("fan_min_speed", "fan_max_speed", "overhang_fan_speed", "overhang_fan_threshold",
        "close_fan_the_first_x_layers", "filament_max_volumetric_speed", "nozzle_temperature_range_low",
        "nozzle_temperature_range_high", "temperature_vitrification")
# Nozzle/bed targets Print Handy resolves for these labels (defaults + the #512 variant deltas).
TARGETS = {"PETG": (240, 85), "ABS": (245, 100), "ASA": (260, 105), "TPU": (230, 50), "PA": (260, 90),
           "PC": (275, 110), "PLA-CF": (215, 60), "PETG-CF": (245, 85), "PA-CF": (270, 90), "ABS-CF": (250, 100),
           "PA-GF": (265, 90)}


# ---------------------------------------------------------------- 1. the family table reaches Orca

@pytest.mark.parametrize("model", FAMILIES)
@pytest.mark.parametrize("material", sorted(TABLE))
def test_each_family_reaches_orca_instead_of_the_pla_profile(slice_inputs, model, material):
    nozzle, bed, filament = slice_inputs.slice(model, stale_state(material))
    assert tuple(filament[key][0] for key in KEYS) == TABLE[material]
    assert filament["filament_type"] == [material]
    # Print Handy's own values still win over the family row.
    assert (nozzle, bed) == (nozzle_effective(model, TARGETS[material][0]), TARGETS[material][1])
    assert int(filament["nozzle_temperature_range_low"][0]) <= nozzle <= int(filament["nozzle_temperature_range_high"][0])


@pytest.mark.parametrize("model", FAMILIES)
def test_tpu_gets_orcas_filament_retraction_and_other_families_leave_it_to_the_printer(slice_inputs, model):
    template = json.loads((adapter_for(model).templates_dir() / "filament.json").read_text())
    assert slice_inputs.slice(model, stale_state("TPU"))[2]["filament_retraction_length"] == ["0.4"]
    petg = slice_inputs.slice(model, stale_state("PETG"))[2]
    assert petg["filament_retraction_length"] == template["filament_retraction_length"]


@pytest.mark.parametrize("model", FAMILIES)
def test_abs_is_not_cooled_like_pla(slice_inputs, model):
    filament = slice_inputs.slice(model, stale_state("ABS"))[2]
    assert filament["additional_cooling_fan_speed"] == ["0"]
    assert filament["support_material_interface_fan_speed"] == ["-1"]


@pytest.mark.parametrize("model", FAMILIES)
@pytest.mark.parametrize("material", ["PLA", "Silk PLA", "PLA+"])
def test_a_pla_job_keeps_the_printers_tuned_pla_template(slice_inputs, model, material):
    template = json.loads((adapter_for(model).templates_dir() / "filament.json").read_text())
    filament = slice_inputs.slice(model, stale_state(material))[2]
    for key in KEYS:
        if key in template and key != "filament_max_volumetric_speed":
            assert filament[key] == template[key], key
    expected = template["filament_max_volumetric_speed"]
    if material == "Silk PLA" and float(expected[0]) > 12:  # Silk caps flow at 12 mm³/s; it never raises it
        expected = ["12"]
    assert filament["filament_max_volumetric_speed"] == expected


@pytest.mark.parametrize("model", ["flashforge.ad5x", "sovol.sv06"])
def test_explicit_temperatures_win_over_the_family_range(slice_inputs, model):
    state = stale_state("PETG")
    slice_inputs.spools[17] = {"id": 17, "filament": {"material": "PETG", "settings_extruder_temp": 205,
                                                      "settings_bed_temp": 55}}
    nozzle, bed, filament = slice_inputs.slice(model, state)
    assert (nozzle, bed) == (nozzle_effective(model, 205), 55)
    assert int(filament["nozzle_temperature_range_low"][0]) <= nozzle  # the range follows the spool, not the reverse
    assert filament["fan_max_speed"] == ["100"] and filament["filament_max_volumetric_speed"] == ["10"]
    for key in ("cool_plate_temp", "eng_plate_temp", "hot_plate_temp", "textured_plate_temp"):
        assert filament[key] == filament[key + "_initial_layer"] == ["55"]


def test_bambu_lab_keeps_its_checked_vendor_profiles_and_everyone_else_uses_the_table():
    assert printers.get("bambulab.p1s", settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": "bambulab.p1s"})
                        ).vendor_material_profiles is True
    assert all(adapter_for(model).vendor_material_profiles is False for model in FAMILIES)


@pytest.mark.parametrize("material,nozzle_type,base,expected", [
    ("PLA", "brass", 210, 210), ("Silk PLA", "hardened_steel", 220, 225),
    ("PETG", "stainless_steel", 240, 245), ("ABS", "tungsten_carbide", 245, 245),
    ("TPU", "ruby", 230, 230), ("ASA", "diamond", 260, 260),
])
def test_material_and_nozzle_type_compose_in_slice(slice_inputs, monkeypatch, material, nozzle_type, base, expected):
    # The shared legacy fixtures deliberately zero steel offsets. Exercise the
    # shipped defaults here without inheriting that test-only override.
    config = settings.current()
    monkeypatch.setattr(settings, "current", lambda: {**config, "nozzle_temp_offsets": {}})
    assert expected - base == (5 if nozzle_type in {"hardened_steel", "stainless_steel"} else 0)
    state = stale_state(material)
    state["nozzle"] = {**state.get("nozzle", {}), "type": nozzle_type, "diameter_mm": 0.4}
    assert slice_inputs.slice("flashforge.ad5x", state)[0] == expected


@pytest.mark.parametrize("material,target,low,high", [
    ("PLA", 180, 180, 240), ("PETG", 270, 190, 270),
])
def test_vendor_profile_range_includes_explicit_spool_target(slice_inputs, monkeypatch, material, target, low, high):
    # Exercise the vendor branch with an isolated checked-profile-shaped fixture, not a real vendor file.
    from . import test_slice_temperatures as temperature_tests
    adapter = adapter_for("flashforge.ad5x")
    template = json.loads((adapter.templates_dir() / "filament.json").read_text())
    template.update({"filament_type": [material], "nozzle_temperature_range_low": ["190"],
                     "nozzle_temperature_range_high": ["240"]})
    vendor = slice_inputs.tmp / "vendor_filament.json"
    vendor.write_text(json.dumps(template))
    monkeypatch.setattr(adapter, "vendor_material_profiles", True)
    monkeypatch.setattr(adapter, "filament_template", lambda *args: vendor)
    monkeypatch.setattr(temperature_tests, "adapter_for", lambda model: adapter)
    state = stale_state(material)
    state["nozzle"] = {**state.get("nozzle", {}), "type": "brass", "diameter_mm": 0.4}
    slice_inputs.spools[17] = {"id": 17, "filament": {"material": material,
                                 "settings_extruder_temp": target, "settings_bed_temp": 60}}
    nozzle, _, filament = slice_inputs.slice("flashforge.ad5x", state)
    assert nozzle == target
    assert filament["nozzle_temperature_range_low"] == [str(low)]
    assert filament["nozzle_temperature_range_high"] == [str(high)]
    assert json.loads(vendor.read_text()) == template


def test_family_overrides_do_not_touch_the_template_and_report_unknown_families():
    from print_handy.core import filament_families
    template = json.loads((ROOT / "print_handy/templates/orca/flashforge_ad5x/filament.json").read_text())
    before = copy.deepcopy(template)
    data, note = filament_families.apply_family_overrides(template, "PETG", nozzle_temp=265)
    assert template == before and data["fan_max_speed"] == ["100"] and "PETG settings applied" in note
    assert data["nozzle_temperature_range_high"] == ["265"]  # widened to the job's own temperature
    unchanged, note = filament_families.apply_family_overrides(template, "PVA")
    assert unchanged == template and "no filament family row" in note
    assert filament_families.family_row("TPU-CF")["filament_max_volumetric_speed"] == ["3.2"]  # base row for a CF label


# ---------------------------------------------------------------- 4. the Silk factor never silently does nothing

def test_exactly_one_shipped_process_template_sets_no_outer_wall_speed():
    missing = sorted(p.parent.name for p in (ROOT / "print_handy/templates/orca").glob("*/process.json")
                     if "outer_wall_speed" not in json.loads(p.read_text()))
    assert missing == ["snapmaker_u1"]


def test_silk_slows_the_authored_outer_wall(slice_inputs, capsys):
    # The template's 200 x 0.7 is 140 mm/s, which is what a Silk PLA cup holder actually printed at; Orca's own
    # Silk profile asks for 40 to 60 mm/s, so the cap decides.
    state = stale_state("Silk PLA")
    slice_inputs.slice("flashforge.ad5x", state)
    process = json.loads((slice_inputs.tmp / "flashforge_ad5x" / "process.json").read_text())
    assert process["outer_wall_speed"] == "60.0"
    assert "140 mm/s capped at 60 mm/s" in capsys.readouterr().out


def test_silk_raises_the_minimum_layer_time(slice_inputs):
    for material, expected in (("Silk PLA", "8"), ("PLA", None)):
        slice_inputs.slice("flashforge.ad5x", stale_state(material))
        filament = json.loads((slice_inputs.tmp / "flashforge_ad5x" / "filament.json").read_text())
        template = json.loads((ROOT / "print_handy/templates/orca/flashforge_ad5x/filament.json").read_text())
        assert filament["slow_down_layer_time"] == ([expected] if expected else template["slow_down_layer_time"])


def test_silk_lowers_the_max_volumetric_speed(slice_inputs):
    template = json.loads((ROOT / "print_handy/templates/orca/flashforge_ad5x/filament.json").read_text())
    for material, expected in (("Silk PLA", ["12"]), ("PLA", template["filament_max_volumetric_speed"])):
        slice_inputs.slice("flashforge.ad5x", stale_state(material))
        filament = json.loads((slice_inputs.tmp / "flashforge_ad5x" / "filament.json").read_text())
        assert filament["filament_max_volumetric_speed"] == expected


def test_plain_pla_keeps_the_authored_outer_wall(slice_inputs):
    slice_inputs.slice("flashforge.ad5x", stale_state("PLA"))
    process = json.loads((slice_inputs.tmp / "flashforge_ad5x" / "process.json").read_text())
    assert process["outer_wall_speed"] == "200"


def test_silk_scales_orcas_default_when_the_template_sets_no_outer_wall_speed(slice_inputs, tmp_path, capsys):
    src = ROOT / "print_handy/templates/orca/flashforge_ad5x"
    templates = tmp_path / "templates"
    templates.mkdir()
    for name in ("machine", "process", "filament"):
        data = json.loads((src / f"{name}.json").read_text())
        data.pop("outer_wall_speed", None)
        (templates / f"{name}.json").write_text(json.dumps(data))
    adapter = adapter_for("flashforge.ad5x")
    adapter.templates_dir = lambda: templates
    for material, expected in (("Silk PLA", "42.0"), ("PLA", None)):
        folder = tmp_path / material.replace(" ", "_")
        orca_adapter.slice_model(MODEL, folder, stale_state(material), adapter=adapter)
        process = json.loads((folder / "process.json").read_text())
        assert process.get("outer_wall_speed") == expected
    assert "scaling Orca's default 60 mm/s by 0.7" in capsys.readouterr().out


# ---------------------------------------------------------------- 2. AD5X multicolor: one profile per slot

AD5X = "flashforge.ad5x"


def ad5x_state(slot_materials):
    state = json.loads(STATE.read_text())
    for number, material in zip("12", slot_materials):
        state["filament_slots"][number]["material"] = material
    return state


@pytest.fixture
def multicolor(tmp_path, monkeypatch):
    """Slice a two-part, two-slot job on the AD5X with the slicer mocked the way Orca writes its tool changes."""
    monkeypatch.setattr(orca_adapter, "ORCA_COMMAND", list(settings.load(local=False, environ={})["orca"]["command"]))
    monkeypatch.setattr(orca_adapter, "pre_slice", lambda **k: slice_guard.Result())
    monkeypatch.setattr(orca_adapter, "post_slice", lambda *a, **k: slice_guard.Result())
    monkeypatch.setattr(orca_adapter.sg, "enforce_approved_baseline", lambda out, **k: slice_guard.Result())
    monkeypatch.setattr(color_geometry, "prime_tower_candidates", lambda *a, **k: [(170, 170)])
    monkeypatch.setattr(spoolman_client, "note_unavailable_once", lambda *a, **k: None)
    spools = {}
    monkeypatch.setattr(spoolman_client, "get_spool", lambda sid, **k: spools.get(int(sid)))
    adapter = adapter_for(AD5X)
    monkeypatch.setattr(printers, "current", lambda config=None: adapter)

    def run(command, **_kwargs):
        folder = Path(command[command.index("--outputdir") + 1])
        files = [Path(p) for p in command[command.index("--load-filaments") + 1].split(";")]
        profiles = [json.loads(p.read_text()) for p in files]
        types = ";".join(f'"{p["filament_type"][0]}"' if " " in p["filament_type"][0] else p["filament_type"][0]
                         for p in profiles)
        temps = ",".join(p["nozzle_temperature"][0] for p in profiles)
        bed = profiles[0]["hot_plate_temp_initial_layer"][0]
        text = (f"; HEADER_BLOCK_START\n; generated by OrcaSlicer 2.4.2\n; HEADER_BLOCK_END\nM190 S{bed}\n"
                f"M104 S{profiles[0]['nozzle_temperature'][0]} T0\n"
                f"START_PRINT BED_TEMP={bed} EXTRUDER_TEMP={profiles[0]['nozzle_temperature'][0]} MESH=auto\nT0\nG90\n"
                f"G1 X60 Y60 Z0.2 E1\nM104 S{profiles[1]['nozzle_temperature'][0]}\nT1\nG1 X110 Y110 Z0.4 E2\n"
                f"M104 S{profiles[0]['nozzle_temperature'][0]}\nT0\nG1 X61 Y61 Z0.6 E3\nEND_PRINT\n"
                f"; filament_type = {types}\n; nozzle_diameter = 0.4\n; nozzle_temperature = {temps}\n"
                f"; nozzle_temperature_initial_layer = {temps}\n")
        (folder / "plate_1.gcode").write_text(text)
        return S(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(orca_adapter.subprocess, "run", run)

    def slice_job(state, name="job"):
        src = tmp_path / f"{name}.3mf"
        mm.write_3mf(src, [("Part 1", 1, mm.box(30, 30, 10), (60, 60)), ("Part 2", 1, mm.box(20, 20, 10), (110, 110))],
                     [("Plate 1", [0, 1])], [("#FFFFFF", "PLA"), ("#FF00FF", "PLA")])
        manifest = source_colors.inventory(src)
        picks = {s: model_job.resolve_filament_selection(state, dict(state["filament_slots"][str(s)], slot=s))
                 for s in (1, 2)}
        choices = {"part:" + part["key"]: picks[2 if str(part["object_id"]) == "3" else 1] for part in manifest["parts"]}
        for row in source_colors.canonical_prompts(manifest):
            choices.setdefault(row["key"], picks[1])
        decisions = {"schema": "part_slots_v1", "mode": "per_part", "source_sha256": manifest["source_sha256"],
                     "choices": choices}
        folder = tmp_path / f"out_{name}"
        out, _prov, _cfg = orca_adapter.slice_model(
            src, folder, state, None, color_decisions=decisions,
            item_shifts={str(p["object_id"]): [0.0, 0.0, 0.0] for p in manifest["parts"]})
        profiles = [json.loads((folder / f"filament_tool_{i}.json").read_text()) for i in (0, 1)]
        return S(out=out, profiles=profiles, process=json.loads((folder / "process.json").read_text()), state=state)

    return S(slice=slice_job, spools=spools)


def test_ad5x_pla_and_silk_slots_each_get_their_own_type_and_temperature(multicolor):
    job = multicolor.slice(ad5x_state(["PLA", "Silk PLA"]))
    first, second = job.profiles
    assert (first["filament_type"], second["filament_type"]) == (["PLA"], ["Silk PLA"])
    nozzle = lambda p: int(p["nozzle_temperature"][0]) == int(p["nozzle_temperature_initial_layer"][0]) and int(p["nozzle_temperature"][0])  # noqa: E731
    assert (nozzle(first), nozzle(second)) == (nozzle_effective(AD5X, 210), nozzle_effective(AD5X, 220))
    assert first["filament_settings_id"] == ["Hermes slot 1 PLA"] and second["filament_settings_id"] == ["Hermes slot 2 Silk PLA"]
    # One bed for the whole plate on every slot; the slowest finish in the job sets the outer-wall speed.
    assert {tuple(p["hot_plate_temp"]) for p in job.profiles} == {("60",)}
    assert job.process["outer_wall_speed"] == "60.0"  # Silk's cap, not 200 x 0.7
    # Minimum layer time is per filament: only the Silk slot is raised to Orca's 8 s.
    assert (first["slow_down_layer_time"], second["slow_down_layer_time"]) == (["6"], ["8"])


def test_ad5x_pla_and_pla_is_unchanged(multicolor):
    job = multicolor.slice(ad5x_state(["PLA", "PLA"]))
    first, second = job.profiles
    for key in ("filament_type", "nozzle_temperature", "nozzle_temperature_initial_layer", "filament_density",
                "filament_max_volumetric_speed", "fan_max_speed"):
        assert first[key] == second[key], key
    assert first["filament_type"] == ["PLA"] and first["nozzle_temperature"] == [str(nozzle_effective(AD5X, 210))]
    assert job.process["outer_wall_speed"] == "200"


def test_ad5x_slots_use_their_own_spoolman_temperatures(multicolor):
    multicolor.spools[11] = {"id": 11, "filament": {"material": "Silk PLA", "settings_extruder_temp": 215,
                                                    "settings_bed_temp": 60}}
    first, second = multicolor.slice(ad5x_state(["PLA", "Silk PLA"])).profiles
    assert second["nozzle_temperature"] == [str(nozzle_effective(AD5X, 215))]  # recorded value, not the Silk +10 default
    assert first["nozzle_temperature"] == [str(nozzle_effective(AD5X, 210))]


def test_ad5x_refuses_nozzle_targets_further_apart_than_one_heater_can_follow(multicolor):
    # Slot 1 is the active spool: the record's stored 210 describes it. Slot 2 resolves from its own Spoolman filament.
    multicolor.spools[11] = {"id": 11, "filament": {"material": "PLA", "settings_extruder_temp": 225}}
    with pytest.raises(RuntimeError, match=r"nozzle temperatures of this job's spools differ by more than 10 C.*slot 1 PLA 210 C"):
        multicolor.slice(ad5x_state(["PLA", "PLA"]))
    multicolor.spools[11]["filament"]["settings_extruder_temp"] = 220  # 10 C apart is within the tolerance
    assert multicolor.slice(ad5x_state(["PLA", "PLA"]), "within").profiles[1]["nozzle_temperature"] == [
        str(nozzle_effective(AD5X, 220))]


def test_ad5x_refuses_a_different_material_family(multicolor):
    with pytest.raises(RuntimeError, match="mixed materials in one job: PETG, PLA|mixed materials in one job: PLA, PETG"):
        multicolor.slice(ad5x_state(["PLA", "PETG"]))


def test_verification_accepts_the_per_slot_result_and_checks_each_tool(multicolor):
    job = multicolor.slice(ad5x_state(["PLA", "Silk PLA"]))
    assert model_job.verify_gcode_offline(job.out, job.state)["offline"] is True
    text = Path(job.out).read_text()
    wrong = Path(job.out).with_name("wrong.gcode")
    wrong.write_text(text.replace("nozzle_temperature_initial_layer = 215,225", "nozzle_temperature_initial_layer = 215,215")
                     if "215,225" in text else re.sub(r"(nozzle_temperature_initial_layer = )(\d+),\d+", r"\g<1>\2,\2", text))
    with pytest.raises(RuntimeError, match=r"tool T1 \(Silk PLA\) is set to \d+ C but its spool resolves to"):
        model_job.verify_gcode_offline(wrong, job.state)


def test_gcode_temperatures_start_tool_sets_the_nozzle_and_a_wrong_header_refuses():
    state = ad5x_state(["PLA", "Silk PLA"])
    materials = ["PLA", "Silk PLA"]
    body = '; filament_type = PLA;"Silk PLA"\n; nozzle_temperature_initial_layer = 210,220\n'
    assert printer_state.gcode_temperatures(state, materials, None, "T0\n" + body) == {"nozzle": 210, "bed": 60}
    assert printer_state.gcode_temperatures(state, materials, None, "T1\nT0\n" + body) == {"nozzle": 220, "bed": 60}
    with pytest.raises(RuntimeError, match=r"tool T1 \(Silk PLA\) is set to 210 C but its spool resolves to 220 C"):
        printer_state.gcode_temperatures(state, materials, None, "T0\nT1\n; nozzle_temperature_initial_layer = 210,210\n")
    # Not Orca output (no per-filament header): the old behavior, the active spool's targets.
    assert printer_state.gcode_temperatures(state, materials, None, "T0\n") == {"nozzle": 210, "bed": 60}


# ---------------------------------------------------------------- 2b. one multicolor rule for every multi-filament printer

@pytest.mark.parametrize("shared_heater", [True, False], ids=["one-heater (AD5X)", "heater-per-tool (U1)"])
def test_one_material_rule_for_single_extruder_and_multi_tool(shared_heater):
    state = ad5x_state(["PLA", "PLA+"])
    slots = list(state["filament_slots"].values())[:2]
    temps = printer_state.multicolor_slot_temperatures(state, slots, None, shared_heater=shared_heater)
    assert [t["nozzle"] for t, _ in temps] == [210, 215]  # PLA with PLA+ is one family everywhere (no casefold rule)
    slots[1] = {**slots[1], "material": "PETG"}
    with pytest.raises(RuntimeError, match="mixed materials in one job: PLA, PETG"):
        printer_state.multicolor_slot_temperatures(state, slots, None, shared_heater=shared_heater)
    far = [{**slots[0], "material": "PLA"}, {**slots[1], "material": "Silk PLA", "spoolman_id": 17}]
    fetched = {17: {"material": "Silk PLA", "settings_extruder_temp": 235}}
    if shared_heater:
        with pytest.raises(RuntimeError, match="differ by more than 10 C"):
            printer_state.multicolor_slot_temperatures(state, far, fetched.get, shared_heater=True)
    else:  # every toolhead has its own heater
        assert printer_state.multicolor_slot_temperatures(state, far, fetched.get, shared_heater=False)[1][0]["nozzle"] == 235


def test_u1_is_a_multi_tool_printer_and_takes_the_same_branch_as_the_creator5():
    assert adapter_for("snapmaker.u1").multi_tool and adapter_for("flashforge.creator5").multi_tool


def test_multi_tool_branch_accepts_pla_with_pla_plus_and_sets_each_toolheads_type(tmp_path, monkeypatch):
    """The U1 / Creator 5 branch refused PLA with PLA+ by casefold while the IFS branch allowed it."""
    from tests.pipeline.test_multi_tool_intake import four_colour_decisions, slice_capture, tool_state
    state = tool_state()
    state["filament_slots"]["2"]["material"] = "PLA+"
    monkeypatch.setattr(spoolman_client, "get_spool", lambda sid, **k: None)
    monkeypatch.setattr(spoolman_client, "note_unavailable_once", lambda *a, **k: None)
    monkeypatch.setattr("tests.pipeline.test_multi_tool_intake.tool_state", lambda: state)
    decisions = four_colour_decisions(tmp_path, [1, 2, 1, 2])
    for choice in decisions["choices"].values():
        choice["material"] = state["filament_slots"][str(choice["slot"])]["material"]
    _out, _prov, _cfg, _calls, folder = slice_capture("flashforge.creator5", decisions, tmp_path, monkeypatch)
    first, second = (json.loads((folder / f"filament_tool_{i}.json").read_text()) for i in (0, 1))
    assert (first["filament_type"], second["filament_type"]) == (["PLA"], ["PLA+"])
    assert first["nozzle_temperature"] != second["nozzle_temperature"]  # PLA+ prints 5 C hotter, on its own heater


# ---------------------------------------------------------------- 3. guards check the record, not just limits

GUARD_MODELS = ["prusa.mk4s", "qidi.xplus4", "elegoo_sdcp.centauricarbon", "snapmaker.u1"]


def guard_adapter(model):
    return printers.get(model, settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": model}))


def guard_state(**over):
    state = {"nozzle": {"diameter_mm": 0.4, "type": "brass"}, "spool": {"spoolman_id": 7, "material": "PLA"},
             "temps_c": {"nozzle": 215, "bed": 60}}
    state.update(over)
    return state


def guard_gcode(adapter, material="PLA", nozzle="0.4", hotend=215, bed=60, diameter=True):
    model = adapter.fact("printer_model") if "printer_model" in adapter.facts else None
    lines = ([f"; printer_model = {model}"] if model else [])
    lines += [f"; filament_type = {material}"] + ([f"; nozzle_diameter = {nozzle}"] if diameter else [])
    lines += [f"; nozzle_temperature = {hotend}", f"; nozzle_temperature_initial_layer = {hotend}",
              f"; hot_plate_temp_initial_layer = {bed}", f"M140 S{bed}", f"M104 S{hotend}", f"M190 S{bed}",
              f"M109 S{hotend}", "G1 X10 Y10 E1", "M104 S0", "M140 S0"]
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("model", GUARD_MODELS)
class TestGuardsCheckTheRecord:
    def test_the_right_job_passes(self, model):
        adapter = guard_adapter(model)
        assert adapter.gcode_problems(guard_gcode(adapter), guard_state()) == []

    def test_wrong_material_is_reported(self, model):
        adapter = guard_adapter(model)
        problems = adapter.gcode_problems(guard_gcode(adapter, material="PETG"), guard_state())
        assert any("filament_type 'PETG' != loaded 'PLA'" in p for p in problems), problems
        assert adapter.gcode_problems(guard_gcode(adapter, material="Silk PLA"),
                                      guard_state(spool={"spoolman_id": 7, "material": "PLA+"})) == []  # one family

    def test_wrong_nozzle_is_reported(self, model):
        adapter = guard_adapter(model)
        problems = adapter.gcode_problems(guard_gcode(adapter, nozzle="0.6"), guard_state())
        assert any("G-code nozzle 0.6 mm != installed 0.4 mm" in p for p in problems), problems

    def test_a_missing_nozzle_diameter_is_reported_once_the_record_names_a_nozzle(self, model):
        adapter = guard_adapter(model)
        problems = adapter.gcode_problems(guard_gcode(adapter, diameter=False), guard_state())
        assert any("carries no nozzle_diameter" in p for p in problems), problems

    def test_wrong_heater_temperatures_are_reported(self, model):
        adapter = guard_adapter(model)
        hot = adapter.gcode_problems(guard_gcode(adapter, hotend=235), guard_state())
        assert any("nozzle temperature 235 C != recorded 215 C" in p for p in hot), hot
        cold_bed = adapter.gcode_problems(guard_gcode(adapter, bed=40), guard_state())
        assert any("bed temperature 40 C != recorded 60 C" in p for p in cold_bed), cold_bed
        # Only the header says 215: a heater command at another temperature is not a match either.
        text = guard_gcode(adapter).replace("M104 S215", "M104 S230").replace("M109 S215", "M109 S230")
        assert any("no heater command sets the recorded nozzle temperature 215 C" in p
                   for p in adapter.gcode_problems(text, guard_state()))

    def test_the_nozzle_type_offset_is_part_of_the_recorded_target(self, model, monkeypatch):
        adapter = guard_adapter(model)
        config = {**settings.current(), "nozzle_temp_offsets": {"hardened_steel": 5.0}}  # not the host's local offsets
        monkeypatch.setattr(settings, "current", lambda: config)
        state = guard_state(nozzle={"diameter_mm": 0.4, "type": "hardened_steel"})
        assert adapter.gcode_problems(guard_gcode(adapter, hotend=220), state) == []  # 215 + 5 C
        assert adapter.gcode_problems(guard_gcode(adapter, hotend=215), state) != []

    def test_without_a_record_the_file_alone_is_checked_as_before(self, model):
        adapter = guard_adapter(model)
        assert adapter.gcode_problems(guard_gcode(adapter, material="PETG", nozzle="0.4", hotend=235)) == []


def test_prusalink_and_sdcp_verification_goes_through_the_record_checks(tmp_path, monkeypatch):
    """verify_gcode on the offline-verified transports gates only on gcode_problems (model_job._verify_offline)."""
    for model in ("prusa.mk4s", "elegoo_sdcp.centauricarbon"):
        adapter = guard_adapter(model)
        monkeypatch.setattr(model_job.printers, "current", lambda config=None, a=adapter: a)
        monkeypatch.setattr(spoolman_client, "get_spool", lambda sid, **k: None)
        state = guard_state(temps_c={"nozzle": 210, "bed": 60})  # PLA's default, which the G-code's spool resolves to
        good = tmp_path / f"{model}.gcode"
        good.write_text(guard_gcode(adapter, hotend=210))
        assert model_job._verify_offline(good, state)["offline"] is True
        bad = tmp_path / f"{model}_bad.gcode"
        bad.write_text(guard_gcode(adapter, material="PETG", nozzle="0.6", hotend=235))
        with pytest.raises(RuntimeError) as refused:
            model_job._verify_offline(bad, state)
        assert "G-code filament_type 'PETG' != loaded 'PLA'" in str(refused.value)
        assert "G-code nozzle 0.6 mm != installed 0.4 mm" in str(refused.value)
        assert "nozzle temperature 235 C != recorded 210 C" in str(refused.value)  # no slot holds PETG: the active spool's


@pytest.mark.parametrize("model", GUARD_MODELS)
def test_a_multi_filament_file_is_checked_against_its_config_block(model):
    adapter = guard_adapter(model)
    text = guard_gcode(adapter, material="PLA;PLA+").replace("= 215", "= 215,220")
    text = text.replace("hot_plate_temp_initial_layer = 60", "hot_plate_temp_initial_layer = 60,60")
    # Heater commands follow each tool's own target, so only the config block must carry the recorded one.
    assert adapter.gcode_problems(text.replace("M104 S215", "M104 S220"), guard_state()) == []
    wrong = text.replace("= 215,220", "= 225,230")
    assert any("nozzle temperature 225 C != recorded 215 C" in p for p in adapter.gcode_problems(wrong, guard_state()))


# ---------------------------------------------------------------- the operator record: no temperature_resolution

def test_operator_record_resolves_each_slot_from_its_own_spool():
    """Stored temps_c (210) describes no slot here: the active spool has no id, so every slot spool is resolved."""
    from .test_slice_temperatures import operator_record
    state = operator_record()
    slots = [state["filament_slots"]["2"], state["filament_slots"]["4"]]  # Silk PLA #17, PLA #12
    temps = printer_state.multicolor_slot_temperatures(state, slots, None, shared_heater=True)
    assert [t["nozzle"] for t, _ in temps] == [220, 210]


def test_operator_record_per_tool_check_accepts_220_for_the_silk_tool_and_refuses_210():
    from .test_slice_temperatures import operator_record
    state = operator_record()
    materials = ["Silk PLA", "PLA"]
    def header(first, second):
        return f"T0\nT1\n; nozzle_temperature_initial_layer = {first},{second}\n"
    assert printer_state.gcode_temperatures(state, materials, None, header(220, 210)) == {"nozzle": 220, "bed": 60}
    with pytest.raises(RuntimeError, match=r"tool T0 \(Silk PLA\) is set to 210 C but its spool resolves to 220 C"):
        printer_state.gcode_temperatures(state, materials, None, header(210, 210))
    # Exact label first: the PLA slots (210) do not make 210 acceptable for the Silk PLA tool.
    with pytest.raises(RuntimeError, match=r"tool T1 \(PLA\)"):
        printer_state.gcode_temperatures(state, materials, None, header(220, 220))
