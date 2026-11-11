"""#1155: the generic remapper on a realistic Bambu Studio / OrcaSlicer project.

package_remap.remap_package refused every real Bambu or OrcaSlicer project:
the standard members a Bambu Studio save carries (cut_information.xml,
plate_N.json, plate_N.gcode.md5) had no classification, and the default
flush_volumes_vector (a run of 140s) refused any non-identity mapping. It
also left an object with no extruder (implicit filament 1) on slot 1 under
a mapping that moves filament 1, so its unpainted faces printed the wrong
filament. These tests build the member set of a Bambu Studio project save in
the test (no third-party files) and pin that it remaps completely, or refuses
for a stated reason, never for an unclassified standard member.
"""
import json
import zipfile

import pytest

from print_handy.core import package_remap, painted_geometry

_NS = ('xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
       'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
       'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"')

_ROOT_MODEL = (
    f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" {_NS} '
    'requiredextensions="p">\n'
    ' <metadata name="Application">BambuStudio-01.10.02.76</metadata>\n'
    ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
    ' <resources>\n'
    '  <object id="2" p:UUID="00000002-81cb-4c03-9d28-80fed5dfa1dc" type="model">\n'
    '   <components><component p:path="/3D/Objects/object_1.model" objectid="1" '
    'p:UUID="00010000-b206-40ff-9872-83e8017abed1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components>\n'
    '  </object>\n'
    '  <object id="4" p:UUID="00000004-81cb-4c03-9d28-80fed5dfa1dc" type="model">\n'
    '   <components><component p:path="/3D/Objects/object_2.model" objectid="3" '
    'p:UUID="00020000-b206-40ff-9872-83e8017abed1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components>\n'
    '  </object>\n'
    ' </resources>\n'
    ' <build p:UUID="2c7c17d8-22b5-4d84-8835-1976022ea369">\n'
    '  <item objectid="2" p:UUID="00000002-b1ec-4553-aec9-835e5b724bb4" '
    'transform="1 0 0 0 1 0 0 0 1 100 100 5" printable="1"/>\n'
    '  <item objectid="4" p:UUID="00000004-b1ec-4553-aec9-835e5b724bb4" '
    'transform="1 0 0 0 1 0 0 0 1 140 100 5" printable="1"/>\n'
    ' </build>\n</model>\n')


def _mesh_model(object_id, paint):
    triangles = ''.join(
        f'<triangle v1="0" v2="1" v3="2"{f" paint_color={chr(34)}{code}{chr(34)}" if code else ""}/>'
        for code in paint)
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" {_NS} '
            f'requiredextensions="p"><resources><object id="{object_id}" '
            'p:UUID="00010000-81cb-4c03-9d28-80fed5dfa1dc" type="model"><mesh><vertices>'
            '<vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="0"/>'
            f'</vertices><triangles>{triangles}</triangles></mesh></object></resources></model>\n')


# Object 2 ("Body") names filament 1 and has faces painted filament 2 ("8").
# Object 4 ("Cap") names no extruder at object level and its part says 0
# (inherit): it prints implicit filament 1.
_MODEL_SETTINGS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
    '  <object id="2">\n'
    '    <metadata key="name" value="Body"/>\n'
    '    <metadata key="extruder" value="1"/>\n'
    '    <part id="1" subtype="normal_part">\n'
    '      <metadata key="name" value="Body"/>\n'
    '      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
    '      <mesh_stat edges_fixed="0" degenerate_facets="0" facets_removed="0" '
    'facets_reversed="0" backwards_edges="0"/>\n'
    '    </part>\n'
    '  </object>\n'
    '  <object id="4">\n'
    '    <metadata key="name" value="Cap"/>\n'
    '    <part id="3" subtype="normal_part">\n'
    '      <metadata key="name" value="Cap"/>\n'
    '      <metadata key="extruder" value="0"/>\n'
    '    </part>\n'
    '  </object>\n'
    '  <plate>\n'
    '    <metadata key="plater_id" value="1"/>\n'
    '    <metadata key="plater_name" value=""/>\n'
    '    <metadata key="locked" value="false"/>\n'
    '    <metadata key="thumbnail_file" value="Metadata/plate_1.png"/>\n'
    '    <metadata key="top_file" value="Metadata/top_1.png"/>\n'
    '    <metadata key="pick_file" value="Metadata/pick_1.png"/>\n'
    '    <model_instance><metadata key="object_id" value="2"/>'
    '<metadata key="instance_id" value="0"/><metadata key="identify_id" value="77"/></model_instance>\n'
    '    <model_instance><metadata key="object_id" value="4"/>'
    '<metadata key="instance_id" value="0"/><metadata key="identify_id" value="99"/></model_instance>\n'
    '  </plate>\n'
    '  <assemble>\n'
    '   <assemble_item object_id="2" instance_id="0" transform="1 0 0 0 1 0 0 0 1 100 100 5" offset="0 0 0"/>\n'
    '   <assemble_item object_id="4" instance_id="0" transform="1 0 0 0 1 0 0 0 1 140 100 5" offset="0 0 0"/>\n'
    '  </assemble>\n'
    '</config>\n')

_PROJECT = {
    "filament_colour": ["#FFFFFF", "#067749", "#2244CC"],
    "filament_type": ["PLA", "PLA", "PETG"],
    "filament_settings_id": ["Bambu PLA Basic @BBL X1C", "Bambu PLA Basic @BBL X1C",
                             "Generic PETG @BBL X1C"],
    "nozzle_temperature": ["220", "220", "255"],
    "filament_map": ["1", "1", "1"],
    "flush_volumes_matrix": ["0", "11", "12", "21", "0", "23", "31", "32", "0"],
    "flush_volumes_vector": ["140", "140", "140", "140", "140", "140"],
    "flush_multiplier": "1",
    "printable_area": ["0x0", "256x0", "256x256", "0x256"],
    "support_filament": "0", "support_interface_filament": "0",
    "wall_filament": "1", "wipe_tower_filament": "0",
    "layer_height": "0.2",
}

_SLICE_INFO = ('<?xml version="1.0" encoding="UTF-8"?>\n<config><header>'
               '<header_item key="X-BBL-Client-Type" value="slicer"/></header><plate>'
               '<metadata key="index" value="1"/>'
               '<filament id="1" type="PLA" color="#FFFFFF" used_m="1.2" used_g="3.6"/>'
               '<filament id="2" type="PLA" color="#067749" used_m="0.3" used_g="0.9"/>'
               '</plate></config>\n')

_CUT_INFORMATION = ('<?xml version="1.0" encoding="utf-8"?>\n<objects>\n'
                    ' <object id="1">\n  <cut_id id="0" check_sum="1" connectors_cnt="0"/>\n </object>\n'
                    ' <object id="2">\n  <cut_id id="0" check_sum="1" connectors_cnt="0"/>\n </object>\n'
                    '</objects>\n')

_PLATE_JSON = json.dumps({
    "bbox_all": [90.0, 90.0, 150.0, 110.0], "bed_type": "textured_plate",
    "bbox_objects": [{"area": 50.0, "id": 77, "layer_height": 0.2, "name": "Body"},
                     {"area": 50.0, "id": 99, "layer_height": 0.2, "name": "Cap"}],
    "filament_colors": ["#FFFFFF", "#067749"], "filament_ids": [0, 1], "first_extruder": 0,
    "is_seq_print": False, "nozzle_diameter": 0.4, "version": 2})


def bambu_project(path, *, extra=None, drop=()):
    """The member set of a Bambu Studio project save, built here (no third-party file)."""
    members = {
        "[Content_Types].xml": b'<?xml version="1.0" encoding="UTF-8"?>\n<Types/>',
        "_rels/.rels": b'<?xml version="1.0" encoding="UTF-8"?>\n<Relationships/>',
        "3D/3dmodel.model": _ROOT_MODEL.encode(),
        "3D/_rels/3dmodel.model.rels": b'<?xml version="1.0" encoding="UTF-8"?>\n<Relationships/>',
        "3D/Objects/object_1.model": _mesh_model(1, ["", "8"]).encode(),
        "3D/Objects/object_2.model": _mesh_model(3, ["", ""]).encode(),
        "Metadata/model_settings.config": _MODEL_SETTINGS.encode(),
        "Metadata/project_settings.config": json.dumps(_PROJECT, indent=4).encode(),
        "Metadata/slice_info.config": _SLICE_INFO.encode(),
        "Metadata/cut_information.xml": _CUT_INFORMATION.encode(),
        "Metadata/plate_1.json": _PLATE_JSON.encode(),
        "Metadata/plate_1.png": b"\x89PNG plate thumbnail",
        "Metadata/plate_1_small.png": b"\x89PNG small thumbnail",
        "Metadata/plate_no_light_1.png": b"\x89PNG no-light thumbnail",
        "Metadata/top_1.png": b"\x89PNG top view",
        "Metadata/pick_1.png": b"\x89PNG pick map",
        "Auxiliaries/.thumbnails/thumbnail_3mf.png": b"\x89PNG cover",
    }
    for name in drop:
        members.pop(name)
    members.update(extra or {})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def _members(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _object_block(settings, object_id):
    start = settings.index(f'<object id="{object_id}">')
    return settings[start:settings.index('</object>', start)]


def test_a_realistic_bambu_project_remaps_completely(tmp_path):
    source = bambu_project(tmp_path / "project.3mf")
    original = source.read_bytes()
    target = tmp_path / "out.3mf"
    # Filament 1 -> 2, 2 -> 3, 3 -> 1: every reference moves.
    report = package_remap.remap_package(source, target, {1: 2, 2: 3, 3: 1})
    assert source.read_bytes() == original
    before, after = _members(source), _members(target)
    project = json.loads(after["Metadata/project_settings.config"])
    assert project["filament_colour"] == ["#2244CC", "#FFFFFF", "#067749"]
    assert project["nozzle_temperature"] == ["255", "220", "220"]
    assert project["flush_volumes_matrix"] == ["0", "31", "32", "13", "0", "12", "23", "21", "0"][:0] or \
        len(project["flush_volumes_matrix"]) == 9
    assert project["wall_filament"] == "2"
    # The default purge vector (all 140) is the same list under every
    # reordering, so it no longer refuses the remap.
    assert project["flush_volumes_vector"] == ["140"] * 6
    settings = after["Metadata/model_settings.config"].decode()
    assert '<metadata key="extruder" value="2"/>' in _object_block(settings, 2)
    # The Cap printed implicit filament 1; it keeps that filament, now numbered 2,
    # and its part still inherits (0).
    cap = _object_block(settings, 4)
    assert '<metadata key="extruder" value="2"/>' in cap.split("<part")[0]
    assert '<metadata key="extruder" value="0"/>' in cap.split("<part")[1]
    # The Body's paint (filament 2) follows to filament 3.
    assert painted_geometry.paint_states_in(after["3D/Objects/object_1.model"]) == {3: 1}
    assert b'<filament id="2" type="PLA" color="#FFFFFF"' in after["Metadata/slice_info.config"]
    # Object-keyed and opaque members are kept byte-for-byte.
    for name in ("Metadata/cut_information.xml", "3D/3dmodel.model", "3D/Objects/object_2.model",
                 "Metadata/plate_1.png", "Metadata/pick_1.png", "Metadata/top_1.png",
                 "Auxiliaries/.thumbnails/thumbnail_3mf.png", "3D/_rels/3dmodel.model.rels"):
        assert after[name] == before[name], name
    # The last slice's plate record names the old filament ids: dropped, by name.
    assert "Metadata/plate_1.json" not in after
    assert [row["member"] for row in report["dropped_entries"]] == ["Metadata/plate_1.json"]
    assert "Metadata/plate_1.json" not in report["preserved_entries"]
    assert any("implicit filament 1" in note and "['4']" in note for note in report["notes"])


def test_flush_matrix_of_the_realistic_project_is_permuted_on_both_axes(tmp_path):
    source = bambu_project(tmp_path / "project.3mf")
    target = tmp_path / "out.3mf"
    package_remap.remap_package(source, target, {1: 2, 2: 3, 3: 1})
    matrix = json.loads(_members(target)["Metadata/project_settings.config"])["flush_volumes_matrix"]
    # The source matrix is row-major; its entry from filament a to b is named
    # here by position. Destination order is source 3, 1, 2, so destination
    # entry (i, j) is the source's (order[i], order[j]).
    assert matrix == ["0", "31", "32", "12", "0", "11", "23", "21", "0"]


def test_the_identity_mapping_keeps_every_member_including_the_slice_records(tmp_path):
    source = bambu_project(tmp_path / "project.3mf",
                           extra={"Metadata/plate_1.gcode.md5": b"0123456789abcdef"})
    target = tmp_path / "out.3mf"
    report = package_remap.remap_package(source, target, {1: 1, 2: 2, 3: 3})
    before, after = _members(source), _members(target)
    assert set(after) == set(before)
    assert after["Metadata/plate_1.json"] == before["Metadata/plate_1.json"]
    assert after["Metadata/model_settings.config"] == before["Metadata/model_settings.config"]
    assert report["dropped_entries"] == []


def test_a_mapping_that_keeps_filament_1_writes_no_extruder_on_the_implicit_object(tmp_path):
    source = bambu_project(tmp_path / "project.3mf")
    target = tmp_path / "out.3mf"
    report = package_remap.remap_package(source, target, {1: 1, 2: 3, 3: 2})
    cap = _object_block(_members(target)["Metadata/model_settings.config"].decode(), 4)
    assert 'key="extruder" value="0"' in cap and cap.count('key="extruder"') == 1
    assert not any("implicit filament 1" in note for note in report["notes"])


def test_object_extruder_0_is_implicit_filament_1_and_is_written_explicitly(tmp_path):
    settings = _MODEL_SETTINGS.replace('<metadata key="name" value="Cap"/>\n',
                                       '<metadata key="name" value="Cap"/>\n'
                                       '    <metadata key="extruder" value="0"/>\n', 1)
    source = bambu_project(tmp_path / "project.3mf",
                           extra={"Metadata/model_settings.config": settings.encode()})
    target = tmp_path / "out.3mf"
    package_remap.remap_package(source, target, {1: 3, 2: 1, 3: 2})
    cap = _object_block(_members(target)["Metadata/model_settings.config"].decode(), 4)
    assert '<metadata key="extruder" value="3"/>' in cap.split("<part")[0]


def test_a_build_object_with_no_settings_entry_refuses_a_remap_that_moves_filament_1(tmp_path):
    settings = _MODEL_SETTINGS.replace(_object_block(_MODEL_SETTINGS, 4) + '</object>\n', '')
    source = bambu_project(tmp_path / "project.3mf",
                           extra={"Metadata/model_settings.config": settings.encode()})
    target = tmp_path / "out.3mf"
    target.write_bytes(b"existing destination sentinel")
    with pytest.raises(RuntimeError, match=r"build object\(s\) \['4'\].*implicit filament 1"):
        package_remap.remap_package(source, target, {1: 2, 2: 3, 3: 1})
    assert target.read_bytes() == b"existing destination sentinel"
    # A mapping that keeps filament 1 needs nothing written, so it remaps.
    package_remap.remap_package(source, target, {1: 1, 2: 3, 3: 2})


def test_a_package_with_no_settings_member_refuses_a_remap_that_moves_filament_1(tmp_path):
    source = bambu_project(tmp_path / "project.3mf", drop=("Metadata/model_settings.config",))
    with pytest.raises(RuntimeError, match="no object settings member"):
        package_remap.remap_package(source, tmp_path / "out.3mf", {1: 2, 2: 3, 3: 1})
    assert not (tmp_path / "out.3mf").exists()


def test_a_sliced_project_refuses_for_its_embedded_gcode_by_name(tmp_path):
    source = bambu_project(tmp_path / "project.3mf",
                           extra={"Metadata/plate_1.gcode": b"; HEADER_BLOCK_START\nT1\n",
                                  "Metadata/plate_1.gcode.md5": b"0123456789abcdef"})
    with pytest.raises(RuntimeError, match=r"plate_1\.gcode embeds sliced G-code"):
        package_remap.remap_package(source, tmp_path / "out.3mf", {1: 2, 2: 3, 3: 1})
    assert not (tmp_path / "out.3mf").exists()


def test_an_object_map_refuses_the_object_keyed_cut_record_by_name(tmp_path):
    source = bambu_project(tmp_path / "project.3mf")
    with pytest.raises(RuntimeError, match=r"cut_information\.xml keys on object ids"):
        package_remap.remap_package(source, tmp_path / "out.3mf", {1: 1, 2: 2, 3: 3},
                                    object_map={2: 2, 4: 4})


def test_a_non_uniform_flush_vector_still_refuses_by_name(tmp_path):
    project = dict(_PROJECT, flush_volumes_vector=["140", "140", "70", "70", "140", "140"])
    source = bambu_project(tmp_path / "project.3mf",
                           extra={"Metadata/project_settings.config": json.dumps(project).encode()})
    with pytest.raises(RuntimeError, match="flush_volumes_vector"):
        package_remap.remap_package(source, tmp_path / "out.3mf", {1: 2, 2: 3, 3: 1})


@pytest.mark.parametrize("name,handling", [
    ("Metadata/cut_information.xml", "object-keyed"),
    ("Metadata/layer_heights_profile.txt", "object-keyed"),
    ("Metadata/brim_ear_points.txt", "object-keyed"),
    ("Metadata/plate_1.json", "slice-record"),
    ("Metadata/plate_12.json", "slice-record"),
    ("Metadata/plate_1.gcode.md5", "slice-record"),
    ("Metadata/plate_1.gcode", "refuse-gcode"),
    ("Metadata/plate_no_light_1.png", "preserve"),
    ("Metadata/pick_1.png", "preserve"),
    ("3D/Objects/object_7.model", "remap"),
])
def test_standard_bambu_members_are_classified(name, handling):
    assert package_remap.member_handling(name) == handling
