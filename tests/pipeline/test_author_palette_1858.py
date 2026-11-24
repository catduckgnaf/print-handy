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
