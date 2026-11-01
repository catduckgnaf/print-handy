"""Offline unit tests for the #721 per-Z G-code analyzer (synthetic Orca-style G-code)."""
from tests.slicer.per_z_analysis import (FIXTURES, analyze_gcode, band, fan_max,
                                         footer_config, outer_wall_feed_range, write_fixture)

SAMPLE = """; header
M106 S255
;LAYER_CHANGE
;Z:0.2
;TYPE:Outer wall
G1 X10 Y10 Z0.2 F3000 E1
G1 X30 Y30 F30000
M106 S100
;LAYER_CHANGE
;Z:0.4
;TYPE:Bridge
G1 X20 Y10 F2400 E2
;LAYER_CHANGE
;Z:0.6
;TYPE:Outer wall
G1 X5 Y5 F6000 E3
M107
; outer_wall_speed = 200
; outer_wall_acceleration = 3200
"""


def test_analyzer_reads_layers_fans_and_features():
    layers = analyze_gcode(SAMPLE)
    assert [layer["z"] for layer in layers] == [0.2, 0.4, 0.6]
    # Fan state set in the header carries into layer 1; the in-layer change is recorded too.
    assert layers[0]["fan_states"] == [255.0, 100.0]
    # The bridge layer inherits the active fan state (100) — no new M106 is needed.
    assert layers[1]["bridge_fan_max"] == 100.0
    assert layers[2]["fan_states"] == [100.0, 0.0]
    assert fan_max(layers) == 255.0
    # Only the extruding outer-wall move counts; the F30000 travel move does not.
    assert outer_wall_feed_range(layers) == (3000.0, 6000.0)
    assert band(layers, 0.3, 0.7)[0]["z"] == 0.4


def test_footer_config_reads_single_values():
    config = footer_config(SAMPLE)
    assert config["outer_wall_speed"] == "200"
    assert config["outer_wall_acceleration"] == "3200"


def test_auxiliary_fan_is_not_counted_as_part_fan():
    layers = analyze_gcode("M106 P2 S255\n;LAYER_CHANGE\n;Z:0.2\nM106 S80\n")
    assert layers[0]["fan_states"] == [80.0]


def test_fixtures_have_the_controlled_shapes(tmp_path):
    for name, boxes in FIXTURES.items():
        path = write_fixture(tmp_path / f"{name}.stl", boxes)
        text = path.read_text()
        assert text.startswith("solid fixture")
        assert text.count("facet normal") == 12 * len(boxes)
    tall = FIXTURES["tall_thin"][0]
    assert tall[5] == 140  # the #715 tall-part height
    assert max(box[5] for box in FIXTURES["tall_bridge_top"]) == 128
    assert max(box[5] for box in FIXTURES["short_control"]) == 10
