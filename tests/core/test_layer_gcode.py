"""Per-layer custom G-code parsing and rewriting (#870).

The dialects disagree on the numeric type codes (Bambu/Orca: color change 0,
pause 1; PrusaSlicer: pause 1, color change 2, tool change 4), so the gcode
attribute names the action when it carries one, and the number is a fallback.
"""
import zipfile

import pytest

from print_handy.core import layer_gcode

XML = ('<?xml version="1.0" encoding="utf-8"?>\n<custom_gcodes_per_layer>\n<plate>\n<plate_info id="1"/>\n'
       '<layer top_z="4" type="2" extruder="2" color="#101010" extra="" gcode="tool_change"/>\n'
       '<layer top_z="6" type="1" extruder="1" color="" extra="" gcode=""/>\n'
       '<layer top_z="8" type="0" extruder="3" color="#FF0000" extra="" gcode=""/>\n'
       '<layer top_z="10" type="5" extruder="1" color="" extra="" gcode="M104 S200"/>\n'
       '<mode value="MultiAsSingle"/>\n</plate>\n</custom_gcodes_per_layer>\n')


def test_parse_classifies_each_dialect():
    rows = layer_gcode.parse(XML.encode())
    assert [(row["top_z"], row["kind"], row["source_extruder"]) for row in rows] == [
        (4.0, "tool_change", 2),   # Prusa color change, named by its gcode
        (6.0, "pause", 1),         # pause in both dialects
        (8.0, "tool_change", 3),   # Bambu color change (type 0)
        (10.0, "custom", 1),       # a G-code payload that is neither
    ]
    assert layer_gcode.tool_change_extruders(rows) == [2, 3]


def test_parse_names_tool_change_types():
    xml = ('<custom_gcodes_per_layer><plate><plate_info id="1"/>'
           '<layer top_z="1" type="ToolChange" extruder="2"/>'
           '<layer top_z="2" type="PausePrint" extruder="1"/>'
           '</plate></custom_gcodes_per_layer>')
    rows = layer_gcode.parse(xml.encode())
    assert [row["kind"] for row in rows] == ["tool_change", "pause"]


def test_malformed_xml_is_an_error_not_silence():
    with pytest.raises(Exception):
        layer_gcode.parse(b"<custom_gcodes_per_layer><plate>")


def test_remap_rewrites_only_tool_changes_and_drops_the_rest():
    data, report = layer_gcode.remap(XML.encode(), {2: 3, 3: 1})
    rows = layer_gcode.parse(data)
    assert [(row["top_z"], row["source_extruder"]) for row in rows] == [(4.0, 3), (8.0, 1)]
    assert report["kept"] == 2 and report["dropped"] == 2


def test_remap_drops_a_change_to_an_unmapped_extruder():
    data, report = layer_gcode.remap(XML.encode(), {2: 1})
    rows = layer_gcode.parse(data)
    assert [(row["top_z"], row["source_extruder"]) for row in rows] == [(4.0, 1)]
    assert report["kept"] == 1


def test_as_pauses_keeps_heights_and_clears_the_payload():
    data, report = layer_gcode.as_pauses(XML.encode())
    rows = layer_gcode.parse(data)
    assert [(row["top_z"], row["kind"]) for row in rows] == [(4.0, "pause"), (8.0, "pause")]
    assert report == {"kept": 2, "dropped": 2, "as_pauses": True}


def test_read_from_a_3mf(tmp_path):
    path = tmp_path / "x.3mf"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(layer_gcode.MEMBER, XML)
    rows = layer_gcode.read(path)
    assert len(rows) == 4
    plain = tmp_path / "y.3mf"
    with zipfile.ZipFile(plain, "w") as archive:
        archive.writestr("3D/3dmodel.model", "<model/>")
    assert layer_gcode.read(plain) == []


def test_plate_id_for_objects_reads_model_settings():
    settings = ('<config><plate><metadata key="plater_id" value="1"/>'
                '<model_instance><metadata key="object_id" value="2"/></model_instance></plate>'
                '<plate><metadata key="plater_id" value="2"/>'
                '<model_instance><metadata key="object_id" value="5"/></model_instance></plate></config>')
    assert layer_gcode.plate_id_for_objects(settings.encode(), ["5"]) == "2"
    assert layer_gcode.plate_id_for_objects(settings.encode(), ["9"]) is None


def test_for_plate_keeps_only_the_named_plate():
    xml = ('<custom_gcodes_per_layer>'
           '<plate><plate_info id="1"/><layer top_z="4" type="0" extruder="2"/></plate>'
           '<plate><plate_info id="2"/><layer top_z="9" type="0" extruder="3"/></plate>'
           '</custom_gcodes_per_layer>')
    rows = layer_gcode.parse(layer_gcode.for_plate(xml.encode(), "2"))
    assert [(row["plate"], row["top_z"]) for row in rows] == [("2", 9.0)]
