"""Offline part-name translation. No printer, no network."""
import unittest

from print_handy.core import part_translator as tr
from print_handy.core import source_colors


class PartTranslatorTests(unittest.TestCase):
    def test_cuerpo_becomes_body_and_keeps_the_original(self):
        assert tr.translate_name('Cuerpo') == 'Body (Cuerpo)'


    def test_spanish_comb_name_translates(self):
        assert tr.translate_name('peine') == 'Comb (peine)'


    def test_body_is_already_english(self):
        assert tr.translate_name('Body') == 'Body'


    def test_component_and_sketch_pass_through(self):
        assert tr.translate_name('Component') == 'Component'
        assert tr.translate_name('Sketch') == 'Sketch'


    def test_camel_case_and_glued_numbers_split(self):
        assert tr.translate_name('Cuerpo1') == 'Body 1 (Cuerpo1)'
        assert tr.translate_name('Cuerpo_Base') == 'Body Base (Cuerpo_Base)'
        assert tr.translate_name('CuboAsiento') == 'Cube Seat (CuboAsiento)'


    def test_unknown_token_passes_through_and_known_token_translates(self):
        assert tr.translate_name('CuerpoWidget') == 'Body Widget (CuerpoWidget)'
        assert tr.translate_name('Widget') == 'Widget'


    def test_size_hint_uses_bounds_when_present(self):
        part = {'name': 'Cuerpo', 'bounds_mm': [(0, 40), (0, 12.5), (0, 8)]}
        assert tr.size_hint(part) == '40 x 12.5 x 8 mm'
        assert tr.display_name(part) == 'Body (Cuerpo), 40 x 12.5 x 8 mm'
        assert tr.size_hint({'name': 'Body'}) == ''


    def test_display_name_does_not_invent_geometry(self):
        assert tr.display_name({'name': 'Tapa'}) == 'Lid (Tapa)'
        assert tr.display_name('Base') == 'Base'


    def test_painted_hello_kitty_manifest_uses_three_english_palette_questions(self):
        manifest = {'source_sha256': 'b' * 64, 'painted': True,
                    'filaments': [{'source_extruder': 1, 'color': '#FF80C0', 'material': 'PLA'},
                                  {'source_extruder': 2, 'color': '#FF0000', 'material': 'PLA'},
                                  {'source_extruder': 3, 'color': '#FFFFFF', 'material': 'PLA'}],
                    'parts': [{'key': '2:1', 'name': 'Cuerpo3', 'source_extruder': 1},
                              {'key': '2:2', 'name': 'Cuerpo1', 'source_extruder': 1},
                              {'key': '2:3', 'name': 'Cubo', 'source_extruder': 1}]}
        rows = source_colors.grouped_prompts(manifest)
        assert [row['key'] for row in rows] == ['filament:1', 'filament:2', 'filament:3']
        assert [row['question'] for row in rows] == [
            'Choose the loaded color/material for Pink regions (#FF80C0, PLA)',
            'Choose the loaded color/material for Red regions (#FF0000, PLA)',
            'Choose the loaded color/material for White regions (#FFFFFF, PLA)']
        assert all('Cuerpo' not in row['question'] and 'Cubo' not in row['question'] for row in rows)


    def test_named_color_is_offline_and_nearest_to_known_palette_colors(self):
        assert tr.named_color('#FF80C0') == 'pink'
        assert tr.named_color('#FF0000') == 'red'
        assert tr.named_color('#FFFFFF') == 'white'
        assert tr.named_color('not-a-color') == 'unknown color'

    def test_prompt_keys_stay_put_when_the_question_is_translated(self):
        manifest = {'source_sha256': 'a' * 64, 'painted': False, 'filaments': [],
                    'parts': [{'key': 'build:0/0', 'name': 'Cuerpo1', 'source_extruder': 1,
                               'bounds_mm': [(0, 10), (0, 20), (0, 5)]}]}
        rows = source_colors.canonical_prompts(manifest)
        assert rows[0]['key'] == 'part:build:0/0'
        assert rows[0]['question'] == 'Choose the color/material for Body 1 (Cuerpo1), 10 x 20 x 5 mm'
        legacy = source_colors.prompts(manifest)
        assert legacy[0]['key'] == 'part:build:0/0'
        assert 'Body 1 (Cuerpo1)' in legacy[0]['question']


def test_named_color_matches_the_witch_ghost_palette():
    """#711: RGB nearest-point naming called gold pink and dark slate brown."""
    want = {'#FAF9F7': 'white', '#61506F': 'purple', '#C47CE7': 'purple', '#FFE57C': 'yellow',
            '#383845': 'black', '#FFB3C4': 'pink', '#067749': 'green', '#C0392B': 'red',
            '#FF00FF': 'pink', '#808080': 'gray', '#8B4513': 'brown', '#FFA500': 'orange',
            '#0000FF': 'blue', '#000000': 'black', '#FFFFFF': 'white'}
    assert {c: tr.named_color(c) for c in want} == want
