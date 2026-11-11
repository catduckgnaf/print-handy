"""#1279: which package a per-part color slice hands the slicer, on the production path.

slice_route.export_per_part (called by orca_adapter.slice_model) slices the
source project remapped by package_remap.remap_package when a job keeps the
project and only renumbers filaments, and rebuilds it with
export_color_geometry otherwise, naming the route either way. A remapper
refusal falls back to the rebuild with a named notice when the rebuild carries
every filament reference, and refuses the slice by name when it does not. The
real-slicer G-code checks are tests/slicer/test_remap_route_real.py.
"""
import io
import json
import re
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from print_handy.core import model_job, orca_adapter, package_remap, slice_route, source_colors
from print_handy.core.color_geometry import export_color_geometry
from tests.corpus.make_models import box, write_3mf

STATE = Path(__file__).parent / "fixtures" / "printer_hardware.json"
PALETTE = [("#E74C3C", "PLA"), ("#2980B9", "PLA")]
SEQUENCE = json.dumps({"print": [{"setting": {"wall_loops": "4"}}]}).encode()


def two_part_source(path, *, palette=PALETTE, plates=None, project=None, extra=None):
    """Base on filament 1 and Cap on filament 2, apart on one plate."""
    write_3mf(path, [("Base", 1, box(30, 30, 10), (60, 60)), ("Cap", 2, box(20, 20, 8), (130, 130))],
              plates or [("Plate 1", [0, 1])], palette, project_extra=project)
    if extra:
        with zipfile.ZipFile(path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        entries.update(extra)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
    return path


def decisions(source, picks):
    """part_slots_v1 decisions; picks maps source filament -> physical slot."""
    state = json.loads(STATE.read_text())
    manifest = source_colors.inventory(source)
    choices = {}
    for row in source_colors.canonical_prompts(manifest):
        match = re.fullmatch(r"filament:(\d+)", row["key"])
        part = next((p for p in manifest["parts"] if "part:" + p["key"] == row["key"]), None)
        slot = picks[int(match.group(1)) if match else part["source_extruder"]]
        choices[row["key"]] = model_job.resolve_filament_selection(
            state, dict(state["filament_slots"][str(slot)], slot=slot))
    return {"schema": "part_slots_v1", "mode": "per_part", "source_sha256": manifest["source_sha256"],
            "choices": choices}


# --- the route each job takes ------------------------------------------------------

def test_a_job_that_only_renumbers_filaments_takes_the_remap_route(tmp_path):
    source = two_part_source(tmp_path / "s.3mf")
    plan = slice_route.plan(source, decisions(source, {1: 3, 2: 1}))
    assert plan["route"] == "remap"
    assert plan["filament_map"] == {1: 2, 2: 1}  # slots 1 and 3, in ascending order


@pytest.mark.parametrize("kwargs, reason", [
    ({"scale": 0.5}, "different scale"),
    ({"object_ids": ["1"]}, "subset of the source objects"),
    ({"item_shifts": {"1": [0, 0, 0], "3": [0, 0, 0]}}, "plate merge"),
    ({"fit_by_object": {"1": {"rotation": [1, 0, 0, 0, 1, 0, 0, 0, 1]}}}, "turns objects"),
    ({"identity_tools": True}, "toolhead (multi_tool)"),
    ({"layer_mode": "pause"}, "pauses"),
    ({"slicer": "bambu_studio"}, "verified with OrcaSlicer only"),
    ({"slicer": "flash_studio"}, "verified with OrcaSlicer only"),
])
def test_a_job_that_changes_geometry_or_numbering_keeps_the_rebuild_and_says_why(tmp_path, kwargs, reason):
    source = two_part_source(tmp_path / "s.3mf")
    plan = slice_route.plan(source, decisions(source, {1: 3, 2: 1}), **kwargs)
    assert plan["route"] == "rebuild" and reason in plan["reason"], plan


def test_two_filaments_on_one_slot_keep_the_rebuild(tmp_path):
    source = two_part_source(tmp_path / "s.3mf")
    plan = slice_route.plan(source, decisions(source, {1: 2, 2: 2}))
    assert plan == {"route": "rebuild", "reason": "the job prints two palette filaments from one slot"}


def test_a_palette_filament_the_job_does_not_load_keeps_the_rebuild(tmp_path):
    source = two_part_source(tmp_path / "s.3mf", palette=PALETTE + [("#00FF00", "PLA")])
    plan = slice_route.plan(source, decisions(source, {1: 3, 2: 1}))
    assert plan == {"route": "rebuild", "reason": "palette filament 3 is not loaded for this job"}


def test_a_multi_plate_source_keeps_the_rebuild(tmp_path):
    source = two_part_source(tmp_path / "s.3mf", plates=[("Plate 1", [0]), ("Plate 2", [1])])
    plan = slice_route.plan(source, decisions(source, {1: 3, 2: 1}))
    assert plan["route"] == "rebuild" and "more than one plate" in plan["reason"]


def test_a_prusa_project_whose_object_roles_differ_keeps_the_rebuild(tmp_path):
    from tests.pipeline.test_remap_package_prusa_1155 import PRUSA_CASE
    source = tmp_path / "prusa.3mf"
    with zipfile.ZipFile(PRUSA_CASE) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    entries["Metadata/Slic3r_PE.config"] += b"; perimeter_extruder = 2\n; infill_extruder = 1\n"
    with zipfile.ZipFile(source, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    plan = slice_route.plan(source, decisions(source, {1: 2, 2: 1}))
    assert plan["route"] == "rebuild" and "perimeter_extruder = 2" in plan["reason"], plan


# --- the production path: slice_model ----------------------------------------------

def _fake_orca(captured):
    """Record the package Orca is handed and emit the tools its objects name."""
    def run(cmd, **kwargs):
        folder = Path(cmd[cmd.index("--outputdir") + 1])
        package = Path(cmd[-1])
        with zipfile.ZipFile(package) as archive:
            captured["members"] = {name: archive.read(name) for name in archive.namelist()}
        settings = captured["members"]["Metadata/model_settings.config"].decode()
        filaments = sorted({int(v) for v in re.findall(r'<metadata key="extruder" value="(\d+)"', settings)
                            if int(v) > 0})
        body = "".join(f"T{f - 1}\nG1 X{10 * f} Y10 E1\n" for f in filaments)
        (folder / "plate_1.gcode").write_text(
            "; HEADER_BLOCK_START\n; generated by OrcaSlicer 2.4.2\n; HEADER_BLOCK_END\n" + body +
            "; filament_type = " + ";".join("PLA" for _ in range(max(filaments))) + "\n")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    return run


def _slice(source, tmp_path, picks):
    state = model_job.printer_state.load()
    captured = {}
    spool = io.BytesIO(json.dumps({"archived": False, "filament": {"material": "PLA"}}).encode())
    with patch.object(orca_adapter.subprocess, "run", side_effect=_fake_orca(captured)), \
            patch.object(urllib.request, "urlopen", return_value=spool), \
            patch.object(package_remap, "remap_package", wraps=package_remap.remap_package) as remap:
        out, provenance, _configs = orca_adapter.slice_model(source, tmp_path / "out", state,
                                                             color_decisions=decisions(source, picks))
    return out, provenance, captured, remap


def _objects(settings):
    """{object name: extruder} from model_settings.config bytes."""
    found = re.findall(rb'<object id="\d+">\s*<metadata key="name" value="([^"]+)"/>\s*'
                       rb'<metadata key="extruder" value="(\d+)"/>', settings)
    return {name.decode(): int(value) for name, value in found}


def test_slice_model_slices_a_renumbering_job_through_remap_package(tmp_path):
    source = two_part_source(tmp_path / "s.3mf", project={"flush_volumes_matrix": ["0", "11", "22", "0"]})
    _out, provenance, captured, remap = _slice(source, tmp_path, {1: 3, 2: 1})
    remap.assert_called_once()
    assert remap.call_args.args[2] == {1: 2, 2: 1}
    route = provenance["geometry"]["slice_route"]
    assert route["route"] == "remap" and route["filament_map"] == {"1": 2, "2": 1}
    # The confirmation's mapping: job filament 1 (tool 0) is slot 1, the Cap's.
    assert provenance["tool_mapping"] == {"0": 1, "1": 3}
    members = captured["members"]
    # The slicer reads the source project, its filaments renumbered: Base
    # (filament 1, slot 3) is job filament 2, Cap (filament 2, slot 1) is 1.
    assert _objects(members["Metadata/model_settings.config"]) == {"Base": 2, "Cap": 1}
    project = json.loads(members["Metadata/project_settings.config"])
    assert project["flush_volumes_matrix"] == ["0", "22", "11", "0"]
    # Placed exactly where the rebuild would place it: same parts and bounds.
    _tools, measured = export_color_geometry(source, None, decisions(source, {1: 3, 2: 1}))
    placed = {row["part_key"]: row["bounds_mm"] for row in provenance["geometry"]["parts"]}
    assert placed == {row["part_key"]: row["bounds_mm"] for row in measured["parts"]}
    assert provenance["geometry"]["prime_tower_xy_mm"] == measured["prime_tower_xy_mm"]


def test_a_rebuild_job_never_calls_the_remapper(tmp_path):
    source = two_part_source(tmp_path / "s.3mf")
    _out, provenance, captured, remap = _slice(source, tmp_path, {1: 2, 2: 2})
    remap.assert_not_called()
    assert provenance["geometry"]["slice_route"] == {
        "route": "rebuild", "reason": "the job prints two palette filaments from one slot"}
    assert "Metadata/project_settings.config" not in captured["members"]  # the rebuilt package
    assert slice_route.route_notice(provenance) is None


def test_a_remap_refusal_falls_back_to_the_equivalent_rebuild_and_names_the_reason(tmp_path):
    source = two_part_source(tmp_path / "s.3mf", extra={"Metadata/filament_sequence.json": SEQUENCE})
    _out, provenance, captured, remap = _slice(source, tmp_path, {1: 3, 2: 1})
    remap.assert_called_once()
    route = provenance["geometry"]["slice_route"]
    assert route["route"] == "rebuild" and "filament_sequence.json" in route["remap_refused"]
    assert "Metadata/project_settings.config" not in captured["members"]
    notice = slice_route.route_notice(provenance)
    assert notice.startswith("Heads up on the slice: ") and "filament_sequence.json" in notice
    assert provenance["tool_mapping"] == {"0": 1, "1": 3}  # the same slots as confirmed


def test_a_remap_refusal_with_a_rebuild_that_drops_a_feature_filament_refuses_by_name(tmp_path):
    source = two_part_source(tmp_path / "s.3mf", project={"outer_wall_filament_id": "2"},
                             extra={"Metadata/filament_sequence.json": SEQUENCE})
    with pytest.raises(slice_route.RemapRefused) as refused:
        _slice(source, tmp_path, {1: 3, 2: 1})
    message = str(refused.value)
    assert "filament_sequence.json" in message and "outer_wall_filament_id = 2" in message
    assert "Nothing was sliced" in message


def test_a_height_range_extruder_is_translated_on_the_remap_route(tmp_path):
    """The rebuild refuses height ranges that name an extruder; the remap route translates them."""
    ranges = (b'<objects><object id="3"><range min_z="2" max_z="4">'
              b'<option opt_key="extruder">1</option></range></object></objects>')
    source = two_part_source(tmp_path / "s.3mf", extra={"Metadata/layer_config_ranges.xml": ranges})
    with pytest.raises(RuntimeError, match="height-range"):
        export_color_geometry(source, tmp_path / "rebuilt.3mf", decisions(source, {1: 3, 2: 1}))
    _out, provenance, captured, _remap = _slice(source, tmp_path, {1: 3, 2: 1})
    assert provenance["geometry"]["slice_route"]["route"] == "remap"
    translated = captured["members"]["Metadata/layer_config_ranges.xml"]
    assert b'<option opt_key="extruder">2</option>' in translated  # filament 1 is job filament 2


def test_a_remap_refusal_with_height_ranges_refuses_rather_than_take_the_rebuild(tmp_path):
    ranges = (b'<objects><object id="3"><range min_z="2" max_z="4">'
              b'<option opt_key="extruder">1</option></range></object></objects>')
    source = two_part_source(tmp_path / "s.3mf", extra={"Metadata/layer_config_ranges.xml": ranges,
                                                        "Metadata/filament_sequence.json": SEQUENCE})
    with pytest.raises(slice_route.RemapRefused, match="filament_sequence.json.*height-range extruder"):
        _slice(source, tmp_path, {1: 3, 2: 1})
