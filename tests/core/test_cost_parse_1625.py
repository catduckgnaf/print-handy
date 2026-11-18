"""Golden-fixture parser tests for the cost parsers (#1625).

Each fixture is a minimal but dialect-faithful sample of what the
slicer writes: PrusaSlicer / OrcaSlicer footer, Bambu Studio header,
UltiMaker Cura header, and Bambu / Orca sliced 3MF slice_info. The
not-sliced states are pinned too: an unsliced 3MF and a PrusaSlicer
3MF never produce a confident zero.
"""
import io
import zipfile

from print_handy.core import cost_parse

PRUSA_GCODE = """; prusaslicer gcode
G1 X10 Y10
; filament used [mm] = 1590.68, 220.10
; filament used [cm3] = 3.83, 0.53
; filament used [g] = 4.74, 0.65
; total filament used [g] = 5.39
; total filament used for wipe tower [g] = 0.65
; estimated printing time (normal mode) = 25m 30s
; estimated printing time (silent mode) = 31m 10s
; filament_type = PLA;PETG
; filament_cost = 25
"""

BAMBU_GCODE = """; Bambu Studio gcode
; total filament weight [g] : 12.34
; total estimated time: 1h 2m 3s
G1 X10 Y10
"""

CURA_GCODE = """;FLAVOR:Marlin
;TIME:7036
;Filament used: 1.20047m
;Layer height: 0.2
G1 X10 Y10
"""


def _slice_info_xml():
    return """<?xml version="1.0" encoding="UTF-8"?>
<config>
 <plate>
  <metadata key="index" value="1"/>
  <metadata key="prediction" value="8130"/>
  <metadata key="weight" value="42.75"/>
  <filament id="1" type="PLA" color="#FFFFFF" used_m="14.31" used_g="42.75"/>
 </plate>
</config>
"""


def _zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def test_prusa_footer_dialect():
    usage = cost_parse.parse_gcode_text(PRUSA_GCODE)
    assert usage.grams_per_filament == [4.74, 0.65]
    assert usage.total_grams == 5.39
    assert usage.wipe_tower_grams == 0.65
    # Normal mode wins over silent mode.
    assert usage.time_seconds == 25 * 60 + 30
    assert usage.slicer_price_per_kg == 25.0
    assert usage.filament_types == ["PLA", "PETG"]


def test_bambu_header_dialect():
    usage = cost_parse.parse_gcode_text(BAMBU_GCODE)
    assert usage.total_grams == 12.34
    assert usage.time_seconds == 3600 + 120 + 3


def test_cura_header_length_to_grams():
    usage = cost_parse.parse_gcode_text(CURA_GCODE, density=1.24, diameter=1.75)
    # pi x 0.875^2 mm2 x 1200.47 mm x 1.24 / 1000 = 3.58 g.
    assert usage.total_grams is not None
    assert abs(usage.total_grams - 3.58) < 0.02
    assert usage.time_seconds == 7036


def test_sliced_3mf_slice_info():
    data = _zip_bytes({"Metadata/slice_info.config": _slice_info_xml(),
                       "3D/3dmodel.model": "<model/>"})
    usage = cost_parse.parse_3mf_bytes(data)
    assert usage.sliced is True
    assert usage.total_grams == 42.75
    assert usage.grams_per_filament == [42.75]
    assert usage.time_seconds == 8130
    assert usage.filament_types == ["PLA"]


def test_unsliced_3mf_is_not_sliced_state():
    data = _zip_bytes({"3D/3dmodel.model": "<model/>",
                       "Metadata/model_settings.config": "<config/>"})
    usage = cost_parse.parse_3mf_bytes(data)
    assert usage.sliced is False
    assert "not sliced yet" in usage.not_sliced_reason
    assert usage.total_grams is None


def test_prusa_3mf_is_not_sliced_state():
    data = _zip_bytes({
        "3D/3dmodel.model": '<model><metadata name="Application">PrusaSlicer</metadata></model>',
        "Metadata/project_settings.config": '{"printer_model": "MK4S"}'})
    usage = cost_parse.parse_3mf_bytes(data)
    assert usage.sliced is False
    assert "PrusaSlicer 3MF" in usage.not_sliced_reason
    assert usage.total_grams is None


def test_resin_textual_metadata():
    text = "ChiTuBox print file\nResin Volume: 30.5 mL\nPrint Time: 14400 s\n"
    usage = cost_parse.parse_resin_text(text)
    assert usage is not None
    assert usage.volume_ml == 30.5
    assert usage.time_seconds == 14400


def test_resin_binary_without_metadata_is_none():
    assert cost_parse.parse_resin_text("\x00\x01\x02 binary") is None


def test_usage_from_gcode_file(tmp_path):
    path = tmp_path / "part.gcode"
    path.write_text(PRUSA_GCODE)
    usage = cost_parse.usage_from_file(path)
    assert usage.total_grams == 5.39
    assert usage.source == "slicer G-code"
