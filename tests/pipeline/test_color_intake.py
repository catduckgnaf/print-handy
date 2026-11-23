"""Offline regression for reviewed assembly and normal intake defects."""
import io
import json
from pathlib import Path
import sys
import zipfile
from unittest.mock import patch

import pytest
from print_handy.core import model_job as job
from print_handy.jobs import audit as jobs_audit
from print_handy.jobs import cli as jobs_cli
from print_handy.jobs import common as jobs_common
from print_handy.jobs import prompts as jobs_prompts
from print_handy.jobs import slice as jobs_slice
from print_handy.jobs import sources as jobs_sources
from print_handy.launch import print_now as launch_print_now
from print_handy.core import source_colors as colors
from .test_source_colors import source
from tests.bound_names import patch_bound


def assembly(tmp_path, component='<component objectid="1"/><component objectid="2"/>', extra=''):
    path = tmp_path / 'assembly.3mf'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('3D/3dmodel.model', '<model><resources><object id="1" name="Shell"><mesh/></object><object id="2" name="Eyes"><mesh/></object><object id="3"><components>' + component + '</components></object>' + extra + '</resources><build><item objectid="3"/></build></model>')
    return path


def test_standard_assembly_leaves_require_explicit_choices(tmp_path):
    manifest = colors.inventory(assembly(tmp_path))
    assert [p['name'] for p in manifest['parts']] == ['Shell', 'Eyes']
    assert manifest['requires_choices']
    with pytest.raises(RuntimeError, match='choices required'):
        colors.require_single_color(manifest, None)


@pytest.mark.parametrize('component,extra,error', [
    ('<component objectid="3"/>', '', 'cyclic'),
    ('<component objectid="99"/>', '', 'unresolved'),
    ('<component objectid="1"/>', '<object id="1"><mesh/></object>', 'duplicate'),
    ('<component objectid="1" path="/3D/missing.model"/>', '', 'unresolved'),
])
def test_invalid_graph_fails_closed(tmp_path, component, extra, error):
    with pytest.raises(RuntimeError, match=error):
        colors.inventory(assembly(tmp_path, component, extra))


def test_external_and_duplicate_instances_are_preserved(tmp_path):
    path = assembly(tmp_path, '<component objectid="9" path="/3D/Objects/a.model"/><component objectid="9" path="Objects/a.model"/>')
    with zipfile.ZipFile(path, 'a') as z:
        z.writestr('3D/Objects/a.model', '<model><resources><object id="9" name="External"><mesh/></object></resources></model>')
    manifest = colors.inventory(path)
    assert [p['name'] for p in manifest['parts']] == ['External', 'External']
    assert len({p['key'] for p in manifest['parts']}) == 2


def metadata_archive(tmp_path, root, metadata, external=None):
    path = tmp_path / 'metadata.3mf'
    with zipfile.ZipFile(path, 'w') as z:
        if root is not None:
            z.writestr('3D/3dmodel.model', root)
        z.writestr('Metadata/model_settings.config', metadata)
        if external:
            z.writestr('3D/Objects/a.model', external)
    return path


ROOT = ('<model><resources><object id="1" name="Shell"><mesh/></object>'
        '<object id="2" name="Eyes"><mesh/></object><object id="3"><components>'
        '<component objectid="1"/><component objectid="2"/></components></object>'
        '</resources><build><item objectid="3"/></build></model>')
VALID_META = ('<config><object id="3"><part id="1"><metadata key="name" value="Shell"/>'
              '</part><part id="2"><metadata key="name" value="Eyes"/></part></object></config>')


@pytest.mark.parametrize('root,metadata,error', [
    (None, '<config><object id="1"/></config>', 'root'),
    ('<model><resources><object id="1"><mesh/></object></resources></model>',
     '<config><object id="1"/></config>', 'build'),
    (ROOT.replace('<build>', '<notbuild>').replace('</build>', '</notbuild>'), VALID_META, 'build'),
    (ROOT.replace('</model>', '<build><item objectid="3"/></build></model>'), VALID_META, 'build'),
    (ROOT, '<config><object id="999"><part id="7"/><part id="8"/></object></config>', 'identities'),
    (ROOT, '<config><object id="3"><part id="1"/><part id="1"/></object></config>', 'duplicate'),
    (ROOT, '<config><object id="1"/><object id="1"/></config>', 'duplicate'),
    (ROOT, '<config><object id="3"><part id="1"/></object></config>', 'inventory'),
    (ROOT, '<config><object id="1"><part id="2"/></object><object id="2"><part id="1"/></object></config>', 'identities'),
])
def test_unresolved_or_contradictory_metadata_cannot_reach_adapter(tmp_path, root, metadata, error):
    path = metadata_archive(tmp_path, root, metadata,
                            '<model><resources><object id="7"><mesh/></object><object id="8"><mesh/></object></resources></model>')
    with pytest.raises(RuntimeError, match=error):
        colors.inventory(path)
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps({'downloaded_file': str(path), 'audit': {'plates': 1}}))
    from print_handy.core import orca_adapter
    with patch.object(job.printer_state, 'load', return_value={}), patch.object(job.printer_state, 'check', return_value=(True, [], {})), patch.object(orca_adapter, 'slice_model') as slicer:
        with pytest.raises(RuntimeError, match=error):
            jobs_slice.slice_job(jp)
        slicer.assert_not_called()


def test_valid_metadata_matches_component_identity(tmp_path):
    manifest = colors.inventory(metadata_archive(tmp_path, ROOT, VALID_META))
    assert [p['key'] for p in manifest['parts']] == ['3:1', '3:2']
    assert [p['name'] for p in manifest['parts']] == ['Shell', 'Eyes']
    assert len({p['key'] for p in colors.prompts(manifest)}) == 2
    with pytest.raises(RuntimeError, match='choices required'):
        colors.require_single_color(manifest, None)


def test_repeated_build_metadata_retains_unique_selection_keys(tmp_path):
    root = ROOT.replace('<item objectid="3"/>', '<item objectid="3"/><item objectid="3"/>')
    manifest = colors.inventory(metadata_archive(tmp_path, root, VALID_META))
    assert len(manifest['parts']) == 4
    assert len({p['key'] for p in colors.prompts(manifest)}) == 4


def test_external_component_metadata_resolves_identity(tmp_path):
    root = ('<model><resources><object id="3"><components>'
            '<component objectid="9" path="/3D/Objects/a.model"/>'
            '</components></object></resources><build><item objectid="3"/></build></model>')
    metadata = '<config><object id="3"><part id="9"><metadata key="name" value="External"/></part></object></config>'
    external = '<model><resources><object id="9"><mesh/></object></resources></model>'
    manifest = colors.inventory(metadata_archive(tmp_path, root, metadata, external))
    assert manifest['parts'][0]['model_path'] == '3D/Objects/a.model'
    assert manifest['parts'][0]['key'] == '3:9'


def test_duplicate_prompt_keys_are_rejected(tmp_path):
    manifest = colors.inventory(source(tmp_path))
    manifest['parts'][1]['key'] = manifest['parts'][0]['key']
    with pytest.raises(ValueError, match='duplicate'):
        colors.prompts(manifest)


def test_external_identity_collision_fails_closed(tmp_path):
    root = ('<model><resources><object id="3"><components>'
            '<component objectid="9" path="/3D/Objects/a.model"/>'
            '<component objectid="9" path="/3D/Objects/b.model"/>'
            '</components></object></resources><build><item objectid="3"/></build></model>')
    metadata = '<config><object id="3"><part id="9"/></object></config>'
    external = '<model><resources><object id="9"><mesh/></object></resources></model>'
    path = metadata_archive(tmp_path, root, metadata, external)
    with zipfile.ZipFile(path, 'a') as z:
        z.writestr('3D/Objects/b.model', external)
    with pytest.raises(RuntimeError, match='identities'):
        colors.inventory(path)


def state():
    return {'nozzle': {'diameter_mm': 0.6, 'type': 'brass'}, 'z_offset_mm': 0.0,
            'temps_c': {'nozzle': 220, 'bed': 60}, 'spool': {'material': 'PLA', 'spoolman_id': 10, 'name': 'white'},
            'filament_slots': {'1': {'color': 'white', 'material': 'PLA', 'spoolman_id': 10},
                          '3': {'color': 'black', 'material': 'PLA', 'spoolman_id': 12}}}


def test_normal_fetch_outputs_part_questions_without_slicing(tmp_path):
    path = source(tmp_path)
    metadata = {'title': 'Model', 'site': 'makerworld', 'model_id': '1', 'slug': 'model',
                'sources': [{'title': 'One plate two parts', 'site_nozzle_mm': 0.6, 'plates': [{}], 'materials': ['PLA']}]}
    with patch.object(sys, 'argv', ['model_job.py', 'fetch', 'https://makerworld.com/en/models/1', '--select', '1', '--approved']), patch_bound(jobs_common, 'ARCHIVE', tmp_path), patch.object(job.printer_state, 'load', return_value=state()), patch.object(job.printer_state, 'check', return_value=(True, [], {})), patch.object(jobs_sources, 'maker_inventory', return_value=metadata), patch.object(jobs_sources, 'maker_download', return_value=path), patch_bound(jobs_audit, 'audit', return_value={'plates': 1}), patch_bound(jobs_slice, 'slice_job') as slicer, patch_bound(launch_print_now, 'print_now') as start, patch.object(sys.stdin, 'isatty', return_value=False), patch('sys.stdout', new_callable=io.StringIO) as out:
        jobs_cli.main()
        assert 'COLOR_CHOICES_REQUIRED' in out.getvalue()
        assert 'Shell' in out.getvalue() and 'Eyes' in out.getvalue()
        slicer.assert_not_called()
        start.assert_not_called()
    saved = json.loads((tmp_path / 'model/JOB.json').read_text())
    assert len(saved['color_questions']) == 4
    assert saved['color_questions'][0]['choices'][0]['selection']['slot'] == 1


def test_interactive_intake_persists_per_part_answers(tmp_path):
    path = source(tmp_path)
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps({'model_id': '1', 'downloaded_file': str(path)}))
    with patch('builtins.input', side_effect=['per_part', '1', '3', '1', '3']), patch('sys.stdout', new_callable=io.StringIO):
        assert jobs_prompts.intake_color_choices(jp, state(), interactive=True)
    saved = json.loads(jp.read_text())
    assert saved['color_decisions']['choices']['part:1:3']['slot'] == 3
    with pytest.raises(RuntimeError, match='cannot preserve colors'):
        colors.require_single_color(saved['color_manifest'], saved['color_decisions'])
    assert jobs_prompts.intake_color_choices(jp, state())


def test_slice_cli_does_not_phone_the_printer(tmp_path):
    path = source(tmp_path)
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps({'model_id': '1', 'downloaded_file': str(path)}))
    with patch.object(sys, 'argv', ['model_job.py', 'slice', str(jp), '--approved', '--color-mode', 'single_color', '--color-slot', '3']), patch.object(job.printer_state, 'load', return_value=state()), patch.object(job.printer_state, 'check', side_effect=AssertionError('slice contacted the printer')), patch_bound(jobs_slice, 'slice_job', return_value='fake.gcode') as slicer, patch_bound(launch_print_now, 'print_now') as start:
        jobs_cli.main()
        slicer.assert_called_once()
        start.assert_not_called()


def test_slice_cli_direct_slots_no_handwritten_json(tmp_path):
    path = source(tmp_path)
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps({'model_id': '1', 'downloaded_file': str(path)}))
    with patch.object(sys, 'argv', ['model_job.py', 'slice', str(jp), '--approved', '--color-mode', 'single_color', '--color-slot', '3']), patch.object(job.printer_state, 'load', return_value=state()), patch.object(job.printer_state, 'check', return_value=(True, [], {})), patch_bound(jobs_slice, 'slice_job', return_value='fake.gcode') as slicer, patch_bound(launch_print_now, 'print_now') as start:
        jobs_cli.main()
        slicer.assert_called_once()
        start.assert_not_called()
    saved = json.loads(jp.read_text())
    assert saved['color_decisions']['mode'] == 'single_color'
    assert saved['filament_selection']['slot'] == 3
    with zipfile.ZipFile(path, 'a') as z:
        z.writestr('changed', 'new bytes')
    with pytest.raises(ValueError, match='SHA256'):
        jobs_prompts.intake_color_choices(jp, state())


def test_bambu_ams_job_still_refuses_source_color_choices(tmp_path):
    # #671: a Bambu AMS model is multi_slot by class, but its filament slots are
    # the sliced file's own — matched to the printer's reported AMS trays at
    # print start — not the record-bound slots the intake questions are built
    # from. A source needing color choices is refused at intake exactly as it
    # was while the model was classified "single"; nothing is asked or sliced.
    src = assembly(tmp_path)
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps({'downloaded_file': str(src), 'audit': {'plates': 1},
                              'printer_model': 'bambulab.p1s'}))
    from print_handy.printers.base import MultiColorUnsupported
    with pytest.raises(MultiColorUnsupported, match='single extruder'):
        jobs_prompts.intake_color_choices(jp, {})
