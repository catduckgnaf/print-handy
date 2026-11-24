"""Chat text seen live on a 9-part MakerWorld model (2026-10-29): summaries, colour questions, TIMING."""
import io
import logging
from contextlib import redirect_stdout

from print_handy.core import model_job, prepared_intake, printer_state, source_colors
from print_handy.jobs import sources as jobs_sources
from print_handy.jobs import store as jobs_store


def _part(key, name, size, color='#FFC0CB', ext=1):
    return {'key': key, 'object_id': key.split(':')[0], 'name': name, 'source_extruder': ext,
            'bounds_mm': [(0, size[0]), (0, size[1]), (0, size[2])], 'author_color': color}


SWING = {'source_sha256': 's' * 64, 'painted': False, 'filaments': [],
         'parts': [_part(f'{i}:1', 'chain', (15, 87.7, 6)) for i in range(1, 5)]
         + [_part('5:1', 'frameLeg', (95.4, 103.6, 16.6)), _part('6:1', 'frameLeg', (95.4, 103.6, 16.6)),
            _part('7:1', 'seat-brick', (31.8, 63.8, 19.2)), _part('8:1', 'double-frame', (190, 20, 16.6))]}


def test_identical_siblings_get_one_short_question():
    rows = {tuple(r['keys']): r['question'] for r in source_colors.grouped_prompts(SWING)}
    chains = rows[tuple(f'part:{i}:1' for i in range(1, 5))]
    assert chains == 'Choose one color/material for the 4 chains (15 x 87.7 x 6 mm, author made them Pink (`#FFC0CB`))'
    legs = rows[('part:5:1', 'part:6:1')]
    assert legs.startswith('Choose one color/material for both framelegs (')
    assert legs.count('95.4 x 103.6 x 16.6 mm') == 1


def test_siblings_that_differ_are_listed_once_each():
    manifest = {**SWING, 'parts': [_part('1:1', 'Stencil 1', (10, 10, 1)), _part('2:1', 'Stencil 2', (20, 20, 1)),
                                   _part('3:1', 'Stencil 2', (20, 20, 1))]}
    fams = [r for r in source_colors.grouped_prompts(manifest) if len(r['keys']) > 1]
    assert len(fams) == 1
    question = fams[0]['question']
    assert question.startswith('Choose one color/material for the 3 stencils: ')
    assert '2× Stencil 2, 20 x 20 x 1 mm' in question and question.count('Stencil 2') == 1


def test_family_option_label_does_not_carry_the_part_list():
    families = [r for r in source_colors.grouped_prompts(SWING) if len(r['keys']) > 1]
    assert [r['label'] for r in families] == ['all 4 chains', 'both framelegs']


def test_long_summary_is_cut_at_a_word_with_an_ellipsis():
    summary = ('Two seats, I created a Duplo swing for my daughter and thought it might be nice to have two '
               'swings side by side, so I extended the frame and added a second seat for her little brother.')
    text = prepared_intake.preview_sentence({'title': 'Duplo swing - Two seats', 'summary': summary},
                                            {'title': 'Duplo swing - Two seats'}, 'White PLA')
    assert 'extended the.' not in text and 'the…' not in text
    assert '…' in text and '….' not in text
    desc = prepared_intake._preview_description(summary)
    assert len(desc) <= 121 and desc.endswith('…')
    assert desc.split()[-1].rstrip('…').lower() not in prepared_intake._DANGLING_WORDS


def test_short_summary_is_unchanged():
    assert prepared_intake.preview_sentence({'title': 'T', 'summary': 'Nice.'}, {'title': 'T'},
                                            'White PLA').startswith('That looks like T, Nice. Filament')


def test_pre_slice_summary_groups_identical_parts_and_normalises_material():
    questions = source_colors.canonical_prompts(SWING)
    white = {'color': 'Matte white', 'material': 'Pla', 'slot': 1}
    pink = {'color': 'pink', 'material': 'silk pla', 'slot': 2}
    choices = {f'part:{p["key"]}': (pink if p['name'] == 'chain' else white) for p in SWING['parts']}

    def color_of(sel):
        color = sel['color']
        return f"{color[:1].upper() + color[1:]} {printer_state.display_material(sel['material'])}"

    text = prepared_intake._choice_summary(questions, choices, color_of)
    assert text == ('chain, chain, chain, chain: designer color: Pink (`#FFC0CB`) → final print color: Pink Silk PLA'
                    ' · frameLeg, frameLeg, seat-brick, double-frame: designer color: Pink (`#FFC0CB`)'
                    ' → final print color: Matte white PLA')


def test_same_name_parts_keep_their_distinct_labels():
    manifest = {**SWING, 'parts': [_part('1:1', 'leg', (10, 10, 50)), _part('2:1', 'leg', (10, 10, 80))]}
    choices = {'part:1:1': 'A', 'part:2:1': 'B'}
    manifest['parts'][0]['display_label'] = 'leg (part 1)'
    manifest['parts'][1]['display_label'] = 'leg (part 2)'
    text = prepared_intake._choice_summary(source_colors.canonical_prompts(manifest), choices, str)
    assert 'leg (part 1): designer color: Pink (`#FFC0CB`) → final print color: A' in text
    assert 'leg (part 2): designer color: Pink (`#FFC0CB`) → final print color: B' in text


def test_display_material_is_display_only():
    assert printer_state.display_material('Pla') == 'PLA'
    assert printer_state.display_material('silk pla') == 'Silk PLA'
    assert printer_state.display_material('petg-cf') == 'PETG-CF'
    assert printer_state.display_material('PLA+') == 'PLA+'
    assert printer_state.display_material('') == ''
    assert printer_state.material_family('Pla') == 'PLA'  # comparisons never needed the display form


def test_slot_labels_show_normalised_material():
    labels = model_job.build_slot_labels([{'slot': 1, 'color': 'white', 'material': 'Pla'},
                                          {'slot': 2, 'color': 'black', 'material': 'petg'}])
    assert labels == ['White PLA', 'Black PETG']


def test_timing_goes_to_debug_logging_not_stdout(tmp_path, caplog):
    job = tmp_path / 'job.json'
    out = io.StringIO()
    with caplog.at_level(logging.DEBUG, logger=jobs_store.logger.name), redirect_stdout(out):
        jobs_store._timing(job, 'print_now', extra=1)
    assert out.getvalue() == ''
    assert any(r.levelno == logging.DEBUG and r.getMessage().startswith('TIMING {') for r in caplog.records)
    assert '"stage": "print_now"' in (tmp_path / 'TIMING.jsonl').read_text()


def test_reused_source_is_linked_not_copied(tmp_path):
    import os
    first = tmp_path / 'job1' / 'model.3mf'
    first.parent.mkdir()
    first.write_bytes(b'3mf bytes')
    out = jobs_sources._reuse_cached(first, tmp_path / 'job2', 'MakerWorld')
    assert out.read_bytes() == b'3mf bytes'
    assert os.path.samefile(out, first) or os.name == 'nt'  # copy fallback where links are unsupported
    again = jobs_sources._reuse_cached(first, tmp_path / 'job2', 'MakerWorld')  # re-run onto itself
    assert again.read_bytes() == b'3mf bytes' and first.read_bytes() == b'3mf bytes'
