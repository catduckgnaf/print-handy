"""Synthetic, public color-selection regression; no source downloads or printer."""
from print_handy.core import source_colors
from print_handy.webui import transcript


def test_painted_designer_color_survives_chat_prompt_and_web_render():
    manifest = {'painted': True, 'parts': [], 'filaments': [
        {'source_extruder': 1, 'color': '#F9E2B7FF', 'material': 'PLA'},
    ]}
    row, = source_colors.canonical_prompts(manifest)
    assert 'Light Cream' in row['question']
    assert '#F9E2B7FF' in row['question']
    assert 'Unknown Color' not in row['question']
    prompt = {'clarify_id': 'p1', 'question': row['question'], 'choices': ['Slot 1: black PLA']}
    page = transcript.render_prompt(prompt, action='/choice', form_token='token')
    assert 'background: #F9E2B7FF' in page
    assert 'Slot 1: black PLA' in page


def test_alpha_last_and_unreadable_designer_colors_stay_distinct():
    label = source_colors.author_color_label
    assert label('#FF000000') == 'Red, transparent (`#FF000000`)'
    assert label('#0F08') == 'Green, 53% opacity (`#0F08`)'
    assert label('#12XY34') == 'Unreadable designer color (`#12XY34`)'
    assert label(None) == 'No designer color specified'
    assert label('Blue') == 'Blue'


def test_approximate_names_do_not_merge_distinct_designer_colors():
    manifest = {'painted': True, 'parts': [], 'filaments': [
        {'source_extruder': index, 'color': color, 'material': 'PLA'}
        for index, color in enumerate(('#F9E2B7FF', '#F8E1B6FF'), 1)
    ]}
    groups = source_colors.color_groups(manifest)
    assert [g['keys'] for g in groups] == [['filament:1'], ['filament:2']]
    assert all('Light Cream' in g['author_label'] for g in groups)
