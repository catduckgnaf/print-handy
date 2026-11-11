"""#1155 remainder: feature filament keys, unknown per-filament arrays, the
filament sequence member and the one-read-per-call paint decode.

OrcaSlicer per-feature filaments (wall, sparse and solid infill, wipe tower),
per-object wall and support filament metadata and plate filament maps are
filament references. Every production export that renumbers filaments remaps
them with the same mapping as the parts. A value the mapping does not cover,
one past the palette, or one that is not a filament number is unknown usage:
never a used filament and never a refusal (owner ruling, #1189/#1190/#1213),
and it is named in the export report.

Offline: Orca is faked where a slice runs.
"""
import io
import json
import re
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

from print_handy import printers
from print_handy.core import model_job, orca_adapter, package_remap, painted_geometry, source_colors
from print_handy.core.color_geometry import export_color_geometry, export_single_color_plate
from tests.corpus.make_models import box, write_3mf

STATE = Path(__file__).parent / "fixtures" / "printer_hardware.json"
PALETTE = [("#E74C3C", "PLA"), ("#2980B9", "PLA")]
SEQUENCE = json.dumps({"print": [{"setting": {"wall_loops": "4"}}]}).encode()


def _rewrite(path, edit):
    with zipfile.ZipFile(path) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    edit(entries)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def _object_meta(settings, name, meta):
    tags = "".join(f'<metadata key="{k}" value="{v}"/>' for k, v in meta.items())
    marker = f'<metadata key="name" value="{name}"/>'
    assert marker in settings
    return settings.replace(marker, marker + tags, 1)


def feature_source(path, project=None, base_meta=None, cap_meta=None, plate_map="1 2", extra=None):
    """Base prints filament 1, Cap filament 2; Orca feature keys at every level."""
    keys = {"wall_filament": "2", "sparse_infill_filament": "1", "solid_infill_filament": "9",
            "wipe_tower_filament": "1", "enable_prime_tower": "1",
            "made_up_per_slot_list": ["left", "right"]}
    keys.update(project or {})
    write_3mf(path, [("Base", 1, box(30, 30, 10), (60, 60)), ("Cap", 2, box(20, 20, 8), (110, 110))],
              [("Plate 1", [0, 1])], PALETTE, project_extra=keys)

    def edit(entries):
        settings = entries["Metadata/model_settings.config"].decode()
        settings = _object_meta(settings, "Base", base_meta if base_meta is not None else
                                {"wall_filament": "1"})
        settings = _object_meta(settings, "Cap", cap_meta if cap_meta is not None else
                                {"wall_filament": "2", "support_filament": "1"})
        if plate_map is not None:
            settings = settings.replace('<metadata key="plater_id" value="1"/>',
                                        '<metadata key="plater_id" value="1"/>'
                                        f'<metadata key="filament_maps" value="{plate_map}"/>', 1)
        entries["Metadata/model_settings.config"] = settings.encode()
        entries.update(extra or {})
    return _rewrite(path, edit)


def _decisions(source, picks):
    """part_slots_v1 decisions; picks maps source filament -> physical slot."""
    state = json.loads(STATE.read_text())
    manifest = source_colors.inventory(source)
    choices = {}
    for row in source_colors.canonical_prompts(manifest):
        match = re.fullmatch(r"filament:(\d+)", row["key"])
        part = next((p for p in manifest["parts"] if "part:" + p["key"] == row["key"]), None)
        filament = int(match.group(1)) if match else part["source_extruder"]
        slot = picks[filament]
        choices[row["key"]] = model_job.resolve_filament_selection(
            state, dict(state["filament_slots"][str(slot)], slot=slot))
    return manifest, {"schema": "part_slots_v1", "mode": "per_part",
                      "source_sha256": manifest["source_sha256"], "choices": choices}


def _meta(settings, name):
    """{key: value} metadata of the model_settings object named `name`."""
    root = re.search(rf'<object id="\d+">\s*<metadata key="name" value="{name}"/>(.*?)</object>',
                     settings, re.S)
    assert root, name
    head = root.group(1).split("<part", 1)[0]
    return dict(re.findall(r'<metadata key="([^"]+)" value="([^"]*)"/>', head))


# --- the single-color copy (_declare_one_filament and its model settings) -----

def test_single_color_plate_remaps_every_feature_key_with_the_parts_mapping(tmp_path):
    source = feature_source(tmp_path / "source.3mf", project={
        "flush_volumes_matrix": ["0", "100", "200", "0"], "flush_volumes_vector": ["1", "2", "3", "4"]})
    # Filament 2 (the Cap) prints from slot 1; filament 1 from slot 2. The plate
    # keeps only the Cap: source filament 2 becomes the plate's filament 1, a
    # non-identity mapping, and filament 1 is not on this plate.
    manifest, decisions = _decisions(source, {1: 2, 2: 1})
    keys = [p["key"] for p in manifest["parts"] if p["source_extruder"] == 2]
    target = tmp_path / "plate.3mf"
    report = export_single_color_plate(source, target, decisions, keys,
                                       slot_colors=json.loads(STATE.read_text())["filament_slots"])
    with zipfile.ZipFile(target) as archive:
        project = json.loads(archive.read("Metadata/project_settings.config"))
        settings = archive.read("Metadata/model_settings.config").decode()
    # Print-wide: the mapped filament follows the parts (2 -> 1); a filament the
    # plate does not print, and one past the palette, are unknown usage: the
    # wall/infill keys are removed (the slicer default prints them with the
    # plate's one filament) and the tower names no specific filament (0).
    assert project["wall_filament"] == "1"
    assert "sparse_infill_filament" not in project
    assert "solid_infill_filament" not in project
    assert project["wipe_tower_filament"] == "0"
    # Per object: the Cap's wall override follows its filament; its support
    # override names filament 1, which this plate does not print, so it falls
    # back to the object's own filament (0). The unselected Base is kept in the
    # copy, and its override no longer names a stale filament either.
    assert _meta(settings, "Cap")["wall_filament"] == "1"
    assert _meta(settings, "Cap")["support_filament"] == "0"
    assert _meta(settings, "Base")["wall_filament"] == "0"
    # Purge data follows the same mapping: the kept filament's diagonal entry
    # and its own pair of the flush vector (two entries per filament).
    assert project["flush_volumes_matrix"] == ["0"]
    assert project["flush_volumes_vector"] == ["3", "4"]
    # Plate filament map: positional per palette filament, cut to the kept one.
    assert '<metadata key="filament_maps" value="2"/>' in settings
    # Every unknown is named in the report, never silent and never a refusal.
    unknown = {(row["where"], row["key"]) for row in report["feature_filaments"]
               if row["status"] == "unknown"}
    assert {("project", "sparse_infill_filament"), ("project", "solid_infill_filament"),
            ("project", "wipe_tower_filament")} <= unknown
    assert ("Metadata/model_settings.config", "support_filament") in unknown
    # No filament reference in the derived plate names a filament past its one.
    for key in package_remap.OBJECT_FILAMENT_KEYS:
        for value in re.findall(rf'<metadata key="{key}" value="(\d+)"/>', settings):
            assert int(value) <= 1


def test_single_color_plate_maps_a_feature_filament_picked_for_the_same_slot(tmp_path):
    source = feature_source(tmp_path / "source.3mf")
    # Both filaments print from slot 1: the plate's one filament is the slot,
    # so a feature naming filament 1 on the Cap-only plate is mapped, not unknown.
    manifest, decisions = _decisions(source, {1: 1, 2: 1})
    keys = [p["key"] for p in manifest["parts"] if p["source_extruder"] == 2]
    target = tmp_path / "plate.3mf"
    report = export_single_color_plate(source, target, decisions, keys,
                                       slot_colors=json.loads(STATE.read_text())["filament_slots"])
    with zipfile.ZipFile(target) as archive:
        project = json.loads(archive.read("Metadata/project_settings.config"))
        settings = archive.read("Metadata/model_settings.config").decode()
    assert project["sparse_infill_filament"] == "1"
    assert _meta(settings, "Cap")["support_filament"] == "1"
    # The out-of-palette value is still unknown, and still no refusal.
    assert "solid_infill_filament" not in project
    assert any(row["key"] == "solid_infill_filament" and row["status"] == "unknown"
               for row in report["feature_filaments"])


def test_single_color_plate_names_an_unknown_palette_length_array_and_keeps_it(tmp_path):
    source = feature_source(tmp_path / "source.3mf")
    manifest, decisions = _decisions(source, {1: 2, 2: 1})
    keys = [p["key"] for p in manifest["parts"] if p["source_extruder"] == 2]
    target = tmp_path / "plate.3mf"
    report = export_single_color_plate(source, target, decisions, keys,
                                       slot_colors=json.loads(STATE.read_text())["filament_slots"])
    with zipfile.ZipFile(target) as archive:
        project = json.loads(archive.read("Metadata/project_settings.config"))
    # Not cut by guesswork and not refused: kept as authored, named.
    assert project["made_up_per_slot_list"] == ["left", "right"]
    assert {"key": "made_up_per_slot_list", "where": "project", "status": "unknown_passthrough",
            "reason": "made_up_per_slot_list has one entry per palette filament but is not a "
                      "known per-filament array; kept as authored"} in report["feature_filaments"]
    # A known per-filament array is still cut to the kept filament's entry.
    assert project["filament_colour"] == [project["filament_colour"][0]]
    assert len(project["filament_type"]) == 1


def test_single_color_plate_bytes_stay_as_authored_without_feature_keys(tmp_path):
    # A one-filament project with no feature key is copied as authored: the
    # new remap adds no rewrite of its own.
    source = tmp_path / "plain.3mf"
    write_3mf(source, [("Base", 1, box(30, 30, 10), (60, 60))], [("Plate 1", [0])], PALETTE[:1])
    manifest, decisions = _decisions(source, {1: 1})
    target = tmp_path / "plate.3mf"
    report = export_single_color_plate(source, target, decisions, [p["key"] for p in manifest["parts"]],
                                       slot_colors=json.loads(STATE.read_text())["filament_slots"])
    assert report["feature_filaments"] == []
    assert "Metadata/project_settings.config" not in report["changed_entries"] or \
        json.loads(zipfile.ZipFile(target).read("Metadata/project_settings.config")).get("wall_filament") is None


# --- the multicolor rebuilt export -----------------------------------------------

def test_rebuilt_export_writes_feature_overrides_as_the_tools_the_parts_use(tmp_path):
    source = feature_source(tmp_path / "source.3mf",
                            base_meta={"wall_filament": "2", "sparse_infill_filament": "4"},
                            cap_meta={"support_filament": "1", "solid_infill_filament": "x"})
    # Non-identity: filament 1 -> slot 3, filament 2 -> slot 1. The job's tools
    # are the sorted slots [1, 3], so filament 2 is tool filament 1 and
    # filament 1 is tool filament 2.
    _manifest, decisions = _decisions(source, {1: 3, 2: 1})
    target = tmp_path / "out.3mf"
    mapping, report = export_color_geometry(source, target, decisions)
    assert mapping == {"0": 1, "1": 3}
    with zipfile.ZipFile(target) as archive:
        settings = archive.read("Metadata/model_settings.config").decode()
    parts = {m.group(1): dict(re.findall(r'<metadata key="([^"]+)" value="([^"]*)"\s*/>', m.group(2)))
             for m in re.finditer(r'<part id="\d+" subtype="normal_part">\s*'
                                  r'<metadata key="name" value="([^"]+)"\s*/>(.*?)</part>', settings, re.S)}
    # Base prints filament 1 (tool filament 2); its wall override names
    # filament 2, written as the tool filament 2 got: 1.
    assert parts["Base"]["extruder"] == "2" and parts["Base"]["wall_filament"] == "1"
    # Cap prints filament 2 (tool filament 1); its support override names
    # filament 1, written as tool filament 2.
    assert parts["Cap"]["extruder"] == "1" and parts["Cap"]["support_filament"] == "2"
    # Out of palette (4) and not a number (x): unknown usage, not written, the
    # part's own tool prints the feature, and no refusal.
    assert "sparse_infill_filament" not in parts["Base"]
    assert "solid_infill_filament" not in parts["Cap"]
    unknown = {(row["where"], row["key"]) for row in report["feature_filaments"] if row["status"] == "unknown"}
    assert any(key == "sparse_infill_filament" for _where, key in unknown)
    assert any(key == "solid_infill_filament" for _where, key in unknown)
    # A carried override is not reported as a dropped per-part setting.
    dropped = {(row["part_key"], row["key"]) for row in report["dropped_part_settings"]}
    assert not any(key in ("wall_filament", "support_filament") for _part, key in dropped)
    # The print-wide keys are named with the tool each maps to, not dropped silently.
    project_rows = {row["key"]: row for row in report["feature_filaments"] if row["where"] == "project"}
    assert project_rows["wall_filament"]["status"] == "not_carried"
    assert project_rows["wall_filament"]["tool_filament"] == 1
    assert project_rows["solid_infill_filament"]["status"] == "unknown"


def test_rebuilt_export_drops_the_filament_sequence_member_by_name_under_a_remap(tmp_path):
    source = feature_source(tmp_path / "source.3mf",
                            extra={"Metadata/filament_sequence.json": SEQUENCE})
    _manifest, decisions = _decisions(source, {1: 3, 2: 1})
    mapping, report = export_color_geometry(source, tmp_path / "out.3mf", decisions)
    assert mapping == {"0": 1, "1": 3}
    with zipfile.ZipFile(tmp_path / "out.3mf") as archive:
        assert "Metadata/filament_sequence.json" not in archive.namelist()
    assert "Metadata/filament_sequence.json" in report["dropped_members"]


def test_single_color_plate_keeps_the_filament_sequence_byte_for_byte_under_a_remap(tmp_path):
    source = feature_source(tmp_path / "source.3mf",
                            extra={"Metadata/filament_sequence.json": SEQUENCE})
    manifest, decisions = _decisions(source, {1: 2, 2: 1})
    keys = [p["key"] for p in manifest["parts"] if p["source_extruder"] == 2]
    target = tmp_path / "plate.3mf"
    report = export_single_color_plate(source, target, decisions, keys,
                                       slot_colors=json.loads(STATE.read_text())["filament_slots"])
    # Source filament 2 is the plate's filament 1 (a non-identity mapping). The
    # sequence member is provenance evidence about the source: preserved byte
    # for byte and named as preserved-unremapped, never rewritten or dropped.
    with zipfile.ZipFile(target) as archive:
        assert archive.read("Metadata/filament_sequence.json") == SEQUENCE
    assert "Metadata/filament_sequence.json" in report["preserved_unremapped_entries"]
    assert "Metadata/filament_sequence.json" not in report["changed_entries"]


# --- the generic remapper (experimental; no production caller) -------------------

def test_generic_remap_permutes_feature_keys_plate_maps_and_names_unknown_arrays(tmp_path):
    source = feature_source(tmp_path / "source.3mf", project={"solid_infill_filament": "2"})
    target = tmp_path / "out.3mf"
    report = package_remap.remap_package(source, target, {1: 2, 2: 1})
    with zipfile.ZipFile(target) as archive:
        project = json.loads(archive.read("Metadata/project_settings.config"))
        settings = archive.read("Metadata/model_settings.config").decode()
    assert project["wall_filament"] == "1"
    assert project["sparse_infill_filament"] == "2"
    assert project["solid_infill_filament"] == "1"
    assert project["wipe_tower_filament"] == "2"
    assert _meta(settings, "Base")["wall_filament"] == "2"
    assert _meta(settings, "Cap")["wall_filament"] == "1"
    assert _meta(settings, "Cap")["support_filament"] == "2"
    assert '<metadata key="filament_maps" value="2 1"/>' in settings
    # The unknown palette-length array is passed through unpermuted and named.
    assert project["made_up_per_slot_list"] == ["left", "right"]
    assert any("made_up_per_slot_list" in note and "unknown passthrough" in note
               for note in report["notes"])


def test_generic_remap_records_an_out_of_palette_feature_value_as_unknown(tmp_path):
    source = feature_source(tmp_path / "source.3mf", cap_meta={"wall_filament": "7"})
    target = tmp_path / "out.3mf"
    report = package_remap.remap_package(source, target, {1: 2, 2: 1})
    with zipfile.ZipFile(target) as archive:
        project = json.loads(archive.read("Metadata/project_settings.config"))
        settings = archive.read("Metadata/model_settings.config").decode()
    assert "solid_infill_filament" not in project  # "9": past the palette
    assert _meta(settings, "Cap")["wall_filament"] == "0"
    assert any("solid_infill_filament" in note and "unknown usage" in note for note in report["notes"])
    assert any("wall_filament" in note and "outside the 2-filament palette" in note
               for note in report["notes"])


def test_generic_remap_refuses_the_filament_sequence_only_under_a_non_identity_mapping(tmp_path):
    source = feature_source(tmp_path / "source.3mf",
                            extra={"Metadata/filament_sequence.json": SEQUENCE})
    import pytest
    with pytest.raises(RuntimeError, match="filament_sequence.json"):
        package_remap.remap_package(source, tmp_path / "swap.3mf", {1: 2, 2: 1})
    assert not (tmp_path / "swap.3mf").exists()
    package_remap.remap_package(source, tmp_path / "same.3mf", {1: 1, 2: 2})
    with zipfile.ZipFile(tmp_path / "same.3mf") as archive:
        assert archive.read("Metadata/filament_sequence.json") == SEQUENCE


# --- AD5X round trip: a non-identity mapping still passes tool verification ------

def _fake_orca(captured):
    """Emit the tools the exported package asks for, as Orca would select them."""
    def run(cmd, **kwargs):
        folder = Path(cmd[cmd.index("--outputdir") + 1])
        package = Path(cmd[-1])
        with zipfile.ZipFile(package) as archive:
            settings = archive.read("Metadata/model_settings.config").decode()
        keys = ("extruder",) + package_remap.OBJECT_FILAMENT_KEYS
        filaments = sorted({int(v) for k, v in re.findall(r'<metadata key="([^"]+)" value="(\d+)"', settings)
                            if k in keys and int(v) > 0})
        captured["filaments"] = filaments
        body = "".join(f"T{f - 1}\nG1 X{10 * f} Y10 E1\n" for f in filaments)
        (folder / "plate_1.gcode").write_text(
            "; HEADER_BLOCK_START\n; generated by OrcaSlicer 2.4.2\n; HEADER_BLOCK_END\n" + body +
            "; filament_type = " + ";".join("PLA" for _ in range(max(filaments))) + "\n")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    return run


def test_non_identity_feature_remap_slices_to_gcode_that_passes_ad5x_tool_verification(tmp_path):
    source = feature_source(tmp_path / "source.3mf",
                            base_meta={"wall_filament": "2"}, cap_meta={"support_filament": "1"})
    state = model_job.printer_state.load()
    adapter = printers.current()
    assert "AD5X" in adapter.label  # the owner's printer
    manifest = source_colors.inventory(source)
    choices = {}
    for row in source_colors.canonical_prompts(manifest):
        match = re.fullmatch(r"filament:(\d+)", row["key"])
        part = next((p for p in manifest["parts"] if "part:" + p["key"] == row["key"]), None)
        slot = {1: 3, 2: 1}[int(match.group(1)) if match else part["source_extruder"]]
        choices[row["key"]] = model_job.resolve_filament_selection(
            state, dict(state["filament_slots"][str(slot)], slot=slot))
    decisions = {"schema": "part_slots_v1", "mode": "per_part",
                 "source_sha256": manifest["source_sha256"], "choices": choices}
    captured = {}
    spool = io.BytesIO(json.dumps({"archived": False, "filament": {"material": "PLA"}}).encode())
    with patch.object(orca_adapter.subprocess, "run", side_effect=_fake_orca(captured)), \
            patch.object(urllib.request, "urlopen", return_value=spool):
        out, provenance, _configs = orca_adapter.slice_model(source, tmp_path / "out", state,
                                                             color_decisions=decisions)
    assert provenance["tool_mapping"] == {"0": 1, "1": 3}
    # The feature overrides reached the slicer as job tools, both inside the mapping.
    assert captured["filaments"] == [1, 2]
    text = Path(out).read_text()
    # AD5X tool verification: the embedded tool map is the job's mapping, the
    # used tools agree with it and every tool's material matches its slot.
    embedded = adapter.native.embedded_mapping(text, state)
    assert embedded == {"0": 1, "1": 3}
    material = re.search(r"^; filament_type = (.+)$", text, re.M).group(1)
    assert adapter.native.validate_materials(text, material, state) == embedded
    assert model_job._gcode_tools(text) <= {int(tool) for tool in embedded}
    adapter.normalize_tool_mapping({str(t): t + 1 for t in sorted(model_job._gcode_tools(text) | {0})}, state)


# --- one read per call: .model members and paint decoded once --------------------

def _painted_source(path):
    write_3mf(path, [("Base", 1, box(30, 30, 10), (60, 60)), ("Cap", 1, box(20, 20, 8), (110, 110))],
              [("Plate 1", [0, 1])], PALETTE)

    def edit(entries):
        model = entries["3D/3dmodel.model"]
        entries["3D/3dmodel.model"] = re.sub(rb'(<triangle v1="\d+" v2="\d+" v3="\d+")',
                                             rb'\1 paint_color="8"', model, count=4)
    return _rewrite(path, edit)


def test_one_slice_reads_each_model_member_once_and_decodes_paint_once(tmp_path):
    source = _painted_source(tmp_path / "painted.3mf")
    state = model_job.printer_state.load()
    manifest = source_colors.inventory(source)
    assert manifest["painted"]
    _manifest, decisions = _decisions(source, {1: 1, 2: 3})
    reads, decodes = [], []
    real_read, real_blocks = zipfile.ZipFile.read, painted_geometry._object_blocks

    def read(self, name, *args, **kwargs):
        member = name.filename if isinstance(name, zipfile.ZipInfo) else name
        if str(self.filename) == str(source) and member.lower().endswith(".model"):
            reads.append(member)
        return real_read(self, name, *args, **kwargs)

    def blocks(model_bytes):
        decodes.append(len(model_bytes))
        return real_blocks(model_bytes)
    captured = {}
    spool = io.BytesIO(json.dumps({"archived": False, "filament": {"material": "PLA"}}).encode())
    with patch.object(zipfile.ZipFile, "read", read), \
            patch.object(painted_geometry, "_object_blocks", blocks), \
            patch.object(orca_adapter.subprocess, "run", side_effect=_fake_orca(captured)), \
            patch.object(urllib.request, "urlopen", return_value=spool):
        _out, provenance, _configs = orca_adapter.slice_model(source, tmp_path / "out", state,
                                                              color_decisions=decisions)
    # inventory (twice), the export's parse and per-part paint, and the
    # governing-rule scan all share one decompression and one block index.
    assert reads == ["3D/3dmodel.model"]
    assert len(decodes) == 1
    assert provenance["tool_mapping"] == {"0": 1, "1": 3}


def test_decode_scope_never_changes_a_verdict(tmp_path):
    source = _painted_source(tmp_path / "painted.3mf")
    plain = source_colors.inventory.__wrapped__(source)  # outside any scope
    with painted_geometry.decode_scope():
        scoped = source_colors.inventory(source)
        again = source_colors.inventory(source)
    assert plain == scoped == again
    part = plain["parts"][0]
    with painted_geometry.decode_scope():
        first = painted_geometry.leaf_states(source, part)
        first["states"][99] = 1  # a caller mutating its row never leaks into the scope
        second = painted_geometry.leaf_states(source, part)
    assert second == painted_geometry.leaf_states(source, part)
    assert 99 not in second["states"]
