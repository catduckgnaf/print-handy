"""ForgeBridge changelog edge cases, as Print Handy tests (#868, from the #867 audit).

Each case is a failure ForgeBridge (https://forgebridge.app/changelog) fixed in its own converter. Print Handy
converts differently (it slices from the printer's template and only carries an allowlist of author
settings), so each test states what the case means here. Edge case 10 (a dedicated support filament) is in
test_foreign_project_adoption.py, edge case 17 (towers inside the bed on every plate) in
test_author_settings.py, and edge case 13's real-Orca half in tests/slicer/test_creator_heads_real.py.
"""
import json
import re
import zipfile
from pathlib import Path

import pytest

from print_handy.core import orca_adapter, source_colors
from print_handy.core.color_geometry import export_color_geometry, export_single_color_plate
from print_handy.core.geometry_layout import _bounds_from_triangles
from tests.corpus.make_models import box, write_3mf, write_stl

from .test_multi_tool_intake import MODELS, four_colour_decisions, slice_capture

STATE = Path(__file__).parent / "fixtures" / "printer_hardware.json"
PALETTE = [("#FFFFFF", "PLA"), ("#101010", "PLA"), ("#FF0000", "PLA")]


def painted(path, code, palette=PALETTE):
    """One Bambu part on extruder 1 whose every triangle carries paint_color=code."""
    write_3mf(path, [("Body", 1, box(20, 20, 10), (60, 60))], [("Plate 1", [0])], palette)
    with zipfile.ZipFile(path) as archive:
        entries = {info.filename: archive.read(info) for info in archive.infolist()}
    entries["3D/3dmodel.model"] = entries["3D/3dmodel.model"].replace(
        b"<triangle ", f'<triangle paint_color="{code}" '.encode())
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def slot(n, color):
    return {"slot": n, "color": color, "material": "PLA", "spoolman_id": 10 + n}


# --- 11. painting that cannot be read keeps all filaments (ForgeBridge 1.1.3) -------------------------

def test_unreadable_paint_keeps_every_filament_as_a_choice(tmp_path):
    """A truncated paint code is still paint: Print Handy never trims the palette from paint it could not
    read. Every source filament stays a question, so no colour is dropped or guessed."""
    manifest = source_colors.inventory(painted(tmp_path / "bad.3mf", "C"))  # state >= 3 with its nibble missing
    assert manifest["painted"]
    assert [row["key"] for row in source_colors.canonical_prompts(manifest)] == ["filament:1", "filament:2", "filament:3"]
    assert len(source_colors.distinct_source_colours(manifest)) == 3


def test_unreadable_paint_is_refused_by_name_never_sliced_with_a_guess(tmp_path):
    source = painted(tmp_path / "bad.3mf", "C")
    manifest = source_colors.inventory(source)
    choices = {row["key"]: slot(i + 1, c) for i, (row, (c, _)) in
               enumerate(zip(source_colors.canonical_prompts(manifest), PALETTE))}
    decisions = {"schema": "part_slots_v1", "mode": "per_part", "source_sha256": manifest["source_sha256"],
                 "choices": choices}
    with pytest.raises((RuntimeError, ValueError), match="paint"):
        export_color_geometry(source, tmp_path / "geometry.3mf", decisions)


# --- 12. an object painted entirely in one filament prints in that filament (ForgeBridge 1.1.5) -------

def test_object_painted_entirely_in_one_filament_uses_only_that_filament(tmp_path):
    source = painted(tmp_path / "all2.3mf", "8")  # every triangle is state 2 (source filament 2)
    manifest = source_colors.inventory(source)
    choices = {"filament:1": slot(1, "white"), "filament:2": slot(3, "black"), "filament:3": slot(4, "red")}
    decisions = {"schema": "part_slots_v1", "mode": "per_part", "source_sha256": manifest["source_sha256"],
                 "choices": choices}
    mapping, _geometry = export_color_geometry(source, tmp_path / "geometry.3mf", decisions)
    # One tool, on filament 2's slot: no swap to the base extruder's slot, so no tower or purge.
    assert mapping == {"0": 3}
    with zipfile.ZipFile(tmp_path / "geometry.3mf") as archive:
        extruders = set(re.findall(rb'key="extruder" value="(\d+)"', archive.read("Metadata/model_settings.config")))
    assert extruders == {b"1"}


# --- 13. Creator 5 / 5 Pro: 2-3 filaments, heads 3 and 4 (ForgeBridge 1.5.3, 1.5.5) -------------------

@pytest.mark.parametrize("model_key", MODELS)
@pytest.mark.parametrize("picks,mapping", [([3, 4, 3, 4], {"2": 3, "3": 4}), ([1, 3, 4, 1], {"0": 1, "2": 3, "3": 4})])
def test_creator_two_or_three_filaments_load_four_matching_profiles(model_key, picks, mapping, tmp_path, monkeypatch):
    """Orca's flush matrix is sized by the loaded filament count. Print Handy loads one profile per toolhead
    (four) whatever the job uses, so a 2- or 3-colour job never has a matrix for fewer filaments than the
    machine's extruders, and a colour on head 3 or 4 is tool T2/T3 with its own profile."""
    _out, provenance, configs, calls, folder = slice_capture(
        model_key, four_colour_decisions(tmp_path, picks), tmp_path, monkeypatch)
    assert provenance["tool_mapping"] == mapping
    filaments = [p for p in configs if p.name.startswith("filament_tool_")]
    assert len(filaments) == 4
    argv = calls[0]
    assert len(argv[argv.index("--load-filaments") + 1].split(";")) == 4
    for path in configs:
        data = json.loads(Path(path).read_text())
        matrix = data.get("flush_volumes_matrix")
        assert matrix is None or len(matrix) == 16, (path.name, matrix)
    machine = json.loads((folder / "machine.json").read_text())
    assert len(machine["nozzle_diameter"]) == 4
    for tool, head in mapping.items():
        profile = json.loads((folder / f"filament_tool_{tool}.json").read_text())
        assert profile["name"] == f"Hermes toolhead {head} PLA"


@pytest.mark.parametrize("model_key", MODELS)
def test_creator_unused_toolhead_profiles_carry_filament_is_support(model_key, tmp_path, monkeypatch):
    """#871: a job whose picks leave toolhead 4 unused loads the base template profile in the last
    --load-filaments position, and Orca rejects the set (-5, "The input preset file is invalid") because
    that profile carries no filament_is_support — the per-toolhead profiles all set it. Every loaded
    profile must carry the key, used toolhead or not."""
    _out, _provenance, _configs, _calls, folder = slice_capture(
        model_key, four_colour_decisions(tmp_path, [1, 3, 2, 1]), tmp_path, monkeypatch)
    for tool in range(4):
        profile = json.loads((folder / f"filament_tool_{tool}.json").read_text())
        assert profile.get("filament_is_support") == ["0"], f"filament_tool_{tool}.json"


# --- 14. height-based colour changes (ChromaCanvas-style layer changes) --------------------------------

LAYER_CHANGES = ('<?xml version="1.0" encoding="utf-8"?>\n<custom_gcodes_per_layer>\n<plate>\n<plate_info id="1"/>\n'
                 '<layer top_z="4" type="2" extruder="2" color="#101010" extra="" gcode="tool_change"/>\n'
                 '<layer top_z="8" type="2" extruder="3" color="#FF0000" extra="" gcode="tool_change"/>\n'
                 '<mode value="MultiAsSingle"/>\n</plate>\n</custom_gcodes_per_layer>\n')


def layered(path):
    write_3mf(path, [("Tower", 1, box(20, 20, 12), (60, 60))], [("Plate 1", [0])], PALETTE)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("Metadata/custom_gcode_per_layer.xml", LAYER_CHANGES)
    return path


def test_height_based_colour_changes_are_kept(tmp_path):
    manifest = source_colors.inventory(layered(tmp_path / "chroma.3mf"))
    # The filaments the layer changes switch to are asked for, like painted regions.
    asked = {row["key"] for row in source_colors.canonical_prompts(manifest)}
    assert {"filament:2", "filament:3"} <= asked
    # The inventory names the changes themselves, and counts their colors as
    # colors the source prints with.
    assert [(row["top_z"], row["source_extruder"]) for row in manifest["layer_changes"]] == [(4.0, 2), (8.0, 3)]
    assert len(source_colors.distinct_source_colours(manifest)) == 3


def _layered_decisions(source, picks):
    manifest = source_colors.inventory(source)
    choices = {row["key"]: slot(n, PALETTE[row["source_extruder"] - 1][0])
               for row, n in zip(source_colors.canonical_prompts(manifest), picks)}
    return {"schema": "part_slots_v1", "mode": "per_part", "source_sha256": manifest["source_sha256"],
            "choices": choices}


def test_height_based_colour_changes_are_exported_remapped_to_the_job_tools(tmp_path):
    """The geometry Print Handy hands Orca carries the changes, with the
    source extruders remapped through the operator's slot picks: filament 2
    was picked for slot 3 and filament 3 for slot 2, so the change at 4 mm
    (source filament 2) must name tool 3 and the one at 8 mm tool 2."""
    source = layered(tmp_path / "chroma.3mf")
    target = tmp_path / "geometry.3mf"
    mapping, report = export_color_geometry(source, target, _layered_decisions(source, [1, 3, 2]))
    assert mapping == {"0": 1, "1": 2, "2": 3}
    assert report["layer_changes"]["kept"] == 2
    with zipfile.ZipFile(target) as archive:
        xml = archive.read("Metadata/custom_gcode_per_layer.xml").decode()
    changes = dict(re.findall(r'top_z="(\d+)"[^>]*extruder="(\d+)"', xml))
    assert changes == {"4": "3", "8": "2"}


def test_height_based_colour_changes_scale_with_the_geometry(tmp_path):
    """A resized print keeps its color boundaries at the same relative
    heights: exporting the 12 mm tower at 50% moves the changes from
    4/8 mm to 2/4 mm instead of leaving them at the authored heights."""
    source = layered(tmp_path / "chroma.3mf")
    target = tmp_path / "geometry.3mf"
    export_color_geometry(source, target, _layered_decisions(source, [1, 3, 2]), scale=0.5)
    with zipfile.ZipFile(target) as archive:
        xml = archive.read("Metadata/custom_gcode_per_layer.xml").decode()
    changes = dict(re.findall(r'top_z="([\d.]+)"[^>]*extruder="(\d+)"', xml))
    assert changes == {"2": "3", "4": "2"}


def test_height_based_colour_changes_become_pauses_for_a_manual_swap(tmp_path):
    """layer_mode='pause' (a single-extruder job whose operator chose manual
    swaps): every change is written as a pause at the same height."""
    source = layered(tmp_path / "chroma.3mf")
    target = tmp_path / "geometry.3mf"
    _mapping, report = export_color_geometry(source, target, _layered_decisions(source, [1, 1, 1]),
                                             layer_mode="pause")
    assert report["layer_changes"]["as_pauses"] is True
    with zipfile.ZipFile(target) as archive:
        xml = archive.read("Metadata/custom_gcode_per_layer.xml").decode()
    entries = re.findall(r"<layer [^>]*>", xml)
    assert len(entries) == 2
    assert all('type="1"' in entry and 'gcode=""' in entry for entry in entries)
    assert [re.search(r'top_z="(\d+)"', entry).group(1) for entry in entries] == ["4", "8"]


def test_layer_pauses_and_custom_gcode_are_reported_not_carried(tmp_path):
    """Pauses and custom G-code in the same XML are out of scope for carrying
    (#870), but the inventory reports them instead of dropping them quietly."""
    source = layered(tmp_path / "chroma.3mf")
    extra = ('<layer top_z="2" type="1" extruder="1" color="" extra="" gcode=""/>\n'
             '<layer top_z="6" type="5" extruder="1" color="" extra="" gcode="M104 S200"/>\n')
    with zipfile.ZipFile(source) as archive:
        entries = {info.filename: archive.read(info) for info in archive.infolist()}
    entries["Metadata/custom_gcode_per_layer.xml"] = entries[
        "Metadata/custom_gcode_per_layer.xml"].replace(b'<mode value=', extra.encode() + b'<mode value=')
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    manifest = source_colors.inventory(source)
    assert [row["kind"] for row in manifest["layer_extras"]] == ["pause", "custom"]
    assert len(manifest["layer_changes"]) == 2


def test_height_based_colour_changes_never_reach_a_one_filament_plate_as_extra_tools(tmp_path):
    """What happens today on a derived plate (route (c)): the changes ride the byte copy, and the plate has one
    filament, so a change to source filament 2 would be a tool the plate does not have. The post-slice guard
    refuses any such G-code (tests/core/test_slice_guard.py); here we pin that the XML is copied unchanged
    rather than silently remapped onto filament 1."""
    source = layered(tmp_path / "chroma.3mf")
    state = json.loads(STATE.read_text())
    manifest = source_colors.inventory(source)
    white = {"slot": 1, "color": state["filament_slots"]["1"]["color"], "material": "PLA",
             "spoolman_id": state["filament_slots"]["1"]["spoolman_id"]}
    decisions = {"schema": "part_slots_v1", "mode": "per_part", "source_sha256": manifest["source_sha256"],
                 "choices": {row["key"]: white for row in source_colors.canonical_prompts(manifest)}}
    plate = tmp_path / "plate.3mf"
    export_single_color_plate(source, plate, decisions, [p["key"] for p in manifest["parts"]])
    with zipfile.ZipFile(plate) as archive:
        assert archive.read("Metadata/custom_gcode_per_layer.xml").decode() == LAYER_CHANGES


# --- 15. the fit check measures models wider at mid-height (ForgeBridge) -------------------------------

def bicone(path, base=10.0, middle=100.0, height=50.0):
    """Narrow at the bed and the top, widest at mid-height (a spinning top)."""
    def ring(size, z):
        h = size / 2
        return [(-h, -h, z), (h, -h, z), (h, h, z), (-h, h, z)]
    vertices = ring(base, 0.0) + ring(middle, height / 2) + ring(base, height)
    faces = [(0, 2, 1), (0, 3, 2), (8, 9, 10), (8, 10, 11)]
    for low, high in ((0, 4), (4, 8)):
        for i in range(4):
            a, b = low + i, low + (i + 1) % 4
            faces += [(a, b, high + (i + 1) % 4), (a, high + (i + 1) % 4, high + i)]
    write_stl(path, (vertices, faces))
    return path


def test_fit_check_measures_the_widest_section_not_the_base(tmp_path):
    source = bicone(tmp_path / "top.stl")
    hull, z_min, z_max = orca_adapter._footprint(source)
    xs, ys = [p[0] for p in hull], [p[1] for p in hull]
    assert max(xs) - min(xs) == pytest.approx(100.0) and max(ys) - min(ys) == pytest.approx(100.0)
    assert (z_min, z_max) == (pytest.approx(0.0), pytest.approx(50.0))
    _lo, _hi, extent = _bounds_from_triangles(orca_adapter.source_triangles(source))
    assert extent[:2] == [pytest.approx(100.0), pytest.approx(100.0)]


# --- 16. multi-plate parts stay on their plates (ForgeBridge 1.3.0) ------------------------------------

def test_each_source_plate_keeps_only_its_own_parts(tmp_path):
    source = tmp_path / "plates.3mf"
    write_3mf(source, [("A", 1, box(20, 20, 5), (20, 20)), ("B", 1, box(20, 20, 5), (60, 20)),
                       ("C", 1, box(20, 20, 5), (20, 20))], [("Plate 1", [0, 1]), ("Plate 2", [2])],
              [("#FFFFFF", "PLA")], project_extra={"curr_bed_type": "Supertack Plate", "outer_wall_speed": "300"})
    state = json.loads(STATE.read_text())
    manifest = source_colors.inventory(source)
    white = {"slot": 1, "color": state["filament_slots"]["1"]["color"], "material": "PLA",
             "spoolman_id": state["filament_slots"]["1"]["spoolman_id"]}
    decisions = {"schema": "part_slots_v1", "mode": "per_part", "source_sha256": manifest["source_sha256"],
                 "choices": {row["key"]: white for row in source_colors.canonical_prompts(manifest)}}
    second = [p["key"] for p in manifest["parts"] if p["name"] == "C"]
    plate = tmp_path / "plate2.3mf"
    report = export_single_color_plate(source, plate, decisions, second)
    assert report["object_ids"] == ["5"] and report["nonprintable_object_ids"] == ["1", "3"]
    # Rewriting the plate's settings for the job (#868) keeps its plate membership exactly.
    orca_adapter.adopt_job_settings(plate, {"curr_bed_type": "Cool Plate", "outer_wall_speed": "200"})
    with zipfile.ZipFile(plate) as archive:
        settings = archive.read("Metadata/model_settings.config").decode()
        model = archive.read("3D/3dmodel.model").decode()
    plates = re.findall(r"<plate>.*?</plate>", settings, re.S)
    assert len(plates) == 1
    assert re.findall(r'key="object_id" value="(\d+)"', plates[0]) == ["5"]
    printable = re.findall(r'<item objectid="(\d+)"[^>]*printable="1"', model)
    assert printable == ["5"]
