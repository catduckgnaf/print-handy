import sys
from pathlib import Path

from print_handy.core import discord_color_intake
from print_handy.core import model_job
from print_handy.hermes import print_records


def test_sanitized_summary_contract_for_discord_and_homeassistant():
    plan = {
        'parts': [
            {'id': 'frame', 'label': 'Frame', 'quantity': 1, 'completed_quantity': 1,
             'status': 'completed', 'run_id': 1, 'plate_id': 1, 'gcode_file': 'frame.gcode'},
            {'id': 'clip', 'label': 'Clip', 'quantity': 2, 'completed_quantity': 0,
             'run_id': 2, 'plate_id': 3, 'plate_estimate_seconds': 1800, 'gcode_file': 'clip.gcode',
             'filament': {'slot': 1, 'color': 'white', 'material': 'PLA'}},
            {'id': 'lid', 'label': 'Lid', 'quantity': 1, 'completed_quantity': 0,
             'run_id': 3, 'plate_id': 4, 'gcode_file': 'private/path/lid-deadbeef.gcode'},
            {'id': 'blue', 'label': 'Blue alternative', 'alternative_group': 'color', 'selected': False},
        ]
    }
    summary = print_records.format_multipart_summary(plan, 'frame.gcode')
    assert summary is not None
    assert 'Current plate/run finished: Run 1, Plate 1' in summary
    assert 'Next required part: Clip' in summary
    assert 'Clip: 2 remaining' in summary
    assert 'Lid: 1 remaining' in summary
    assert 'Start next plate · bed is clear' in summary
    assert 'require a fresh confirmation for this exact artifact' in summary
    assert 'No automatic advance' in summary
    assert 'deadbeef' not in summary


def test_filament_label_does_not_name_the_material_twice():
    from print_handy.hermes.discord_print_summary import filament_label
    assert filament_label("Coffee PLA+", "PLA+") == "Coffee PLA+"
    assert filament_label("coffee pla+", "PLA+") == "coffee pla+"
    assert filament_label("PLA+", "PLA+") == "PLA+"
    assert filament_label("Coffee", "PLA+") == "Coffee PLA+"
    assert filament_label("Coffee PLA", "PLA+") == "Coffee PLA PLA+"  # a different material is kept
    assert filament_label("Silk PLA", "LA") == "Silk PLA LA"  # whole-word match only
    assert filament_label(None, "PETG", "?") == "? PETG"
    assert filament_label("", "") == ""


def test_alert_slot_line_names_the_material_once():
    from print_handy.hermes.discord_print_summary import format_confirmed_print
    text = format_confirmed_print(kind="paused", model="clip.gcode", slot=2, color="Coffee PLA+", material="PLA+")
    assert "Slot 2: Coffee PLA+" in text and "PLA+ PLA+" not in text
