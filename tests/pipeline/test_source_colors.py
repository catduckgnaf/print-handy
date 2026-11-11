import io
import json
from pathlib import Path
import sys
import zipfile
from unittest.mock import patch

import pytest
from print_handy.core import model_job as job
from print_handy.core import source_colors as colors


def source(tmp_path, painted=False):
    p = tmp_path / 'source.3mf'
    with zipfile.ZipFile(p, 'w') as z:
        z.writestr('Metadata/project_settings.config', json.dumps({'filament_colour': ['#FFFFFF', '#000000'], 'filament_type': ['PLA', 'PLA']}))
        z.writestr('Metadata/model_settings.config', '<config><object id="1"><metadata value="Body" key="name"/><part id="2"><metadata value="Shell" key="name"/><metadata value="1" key="extruder"/></part><part id="3"><metadata key="name" value="Eyes"/><metadata key="extruder" value="2"/></part></object><plate><metadata key="plater_id" value="1"/><model_instance><metadata key="object_id" value="1"/></model_instance></plate></config>')
        paint = ' paint_color="1"' if painted else ''
        z.writestr('3D/3dmodel.model', '<model><resources><object id="1"><components><component objectid="2"/><component objectid="3"/></components></object><object id="2"><mesh><triangles><triangle' + paint + '/></triangles></mesh></object><object id="3"><mesh/></object></resources><build><item objectid="1"/></build></model>')
    return p


def test_parts_palette_and_paint_are_retained(tmp_path):
    m = colors.inventory(source(tmp_path, True))
    assert [p['name'] for p in m['parts']] == ['Shell', 'Eyes']
    assert [p['source_extruder'] for p in m['parts']] == [1, 2]
    assert m['painted'] and m['requires_choices']
    assert len(colors.prompts(m)) == 4
    assert all(p['status'] == 'planned' for p in m['parts'])


def test_choices_must_cover_parts_and_palette_and_bind_hash(tmp_path):
    m = colors.inventory(source(tmp_path))
    selection = {'slot': 1, 'color': 'white', 'material': 'PLA', 'spoolman_id': 10}
    d = {'source_sha256': m['source_sha256'], 'mode': 'per_part', 'choices': {p['key']: selection for p in colors.prompts(m)}}
    assert len(colors.validate_decisions(m, d, {}, job.resolve_filament_selection)['choices']) == 4
    with pytest.raises(RuntimeError, match='cannot preserve colors'):
        colors.require_single_color(m, d)
    d['choices'].pop('filament:2')
    with pytest.raises(ValueError, match='every part'):
        colors.validate_decisions(m, d, {}, job.resolve_filament_selection)
    with pytest.raises(ValueError, match='SHA256'):
        colors.validate_decisions(m, dict(d, source_sha256='stale'), {}, job.resolve_filament_selection)


def test_single_color_is_explicit_not_global_selection_inference(tmp_path):
    m = colors.inventory(source(tmp_path))
    with pytest.raises(RuntimeError, match='choices required'):
        colors.require_single_color(m, None)
    d = {'source_sha256': m['source_sha256'], 'mode': 'single_color', 'selection': {'slot': 1, 'color': 'white', 'material': 'PLA', 'spoolman_id': 10}}
    colors.require_single_color(m, colors.validate_decisions(m, d, {}, job.resolve_filament_selection))


@pytest.mark.parametrize('phase', ['standby', 'complete', 'cancelled', 'printing', 'paused', 'error', None])
def test_staged_sensor_exemption_requires_idle_and_present_feeder(phase):
    s = {'print_stats': {'state': phase}, 'filament_switch_sensor head_switch_sensor': {'enabled': True, 'filament_detected': False}, 'zmod_color': {'slots': [{'ID': '1', 'Color': 'white', 'Material': 'PLA', 'hasFilament': True}]}}
    selection = {'slot': 1, 'color': 'white', 'material': 'PLA', 'spoolman_id': 10}
    problems = job.check_filament_readiness({}, s, selection, observed_at=100, now=100)
    assert bool(problems) == (phase not in ('standby', 'complete', 'cancelled'))
    s['zmod_color']['slots'][0]['hasFilament'] = None
    assert job.check_filament_readiness({}, s, selection, observed_at=100, now=100)
    s['filament_switch_sensor head_switch_sensor']['enabled'] = False
    assert any('enabled state is false' in p for p in job.check_filament_readiness({}, s, selection, observed_at=100, now=100))


def test_color_plan_and_select_cli_fake_transport(tmp_path):
    p = source(tmp_path)
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps({'model_id': '1', 'downloaded_file': str(p)}))
    with patch.object(sys, 'argv', ['model_job.py', 'color-plan', str(jp)]), patch('sys.stdout', new_callable=io.StringIO) as out, patch.object(job, 'request_json') as network:
        job.main()
        assert len(json.loads(out.getvalue())['questions']) == 4
        network.assert_not_called()
    m = colors.inventory(p)
    dp = tmp_path / 'decisions.json'
    dp.write_text(json.dumps({'source_sha256': m['source_sha256'], 'mode': 'single_color', 'selection': {'slot': 1, 'color': 'white', 'material': 'PLA', 'spoolman_id': 10}}))
    with patch.object(sys, 'argv', ['model_job.py', 'color-select', str(jp), str(dp)]), patch.object(job.printer_state, 'load', return_value={}), patch('sys.stdout', new_callable=io.StringIO), patch.object(job, 'request_json') as network:
        job.main()
        network.assert_not_called()
    assert json.loads(jp.read_text())['filament_selection']['slot'] == 1


def test_slice_stops_before_adapter_when_multicolor_is_unresolved(tmp_path):
    p = source(tmp_path)
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps({'model_id': '1', 'downloaded_file': str(p), 'audit': {'plates': 1}}))
    from print_handy.core import orca_adapter
    with patch.object(job.printer_state, 'load', return_value={}), patch.object(job.printer_state, 'check', return_value=(True, [], {})), patch.object(orca_adapter, 'slice_model') as slicer:
        # Behavior change (#1189, owner decision 2026-11-10): unresolved
        # colors no longer refuse with "choices required"; they stop in the
        # filament warning round naming the one-color fallback (slot 1 here,
        # said out loud, because the record has no loaded-slot information).
        with pytest.raises(job.FilamentWarningPending) as caught:
            job.slice_job(jp)
        assert caught.value.warnings == [
            'color choices were never made for this print; every part prints single-color from '
            'slot 1, because the printer record has no loaded-slot information']
        slicer.assert_not_called()
    assert len(json.loads(jp.read_text())['color_prompts']) == 4


def test_verify_readiness_pipeline_idle_with_native_presence_fake_transport():
    selection = {'slot': 1, 'color': 'white', 'material': 'PLA', 'spoolman_id': 10}
    # The active spool differs on purpose: the selected slot owns the job.
    st = {'moonraker_url': 'http://fake', 'spool': {'spoolman_id': 12},
          'filament_slots': {'1': {'spoolman_id': 10, 'material': 'PLA', 'physical_color_verified': True}}}
    status = {'print_stats': {'state': 'standby'},
              'filament_switch_sensor head_switch_sensor': {'enabled': True, 'filament_detected': False},
              'zmod_color': {'slots': [{'ID': '1', 'Material': 'PLA', 'Color': 'white', 'hasFilament': False}]}}
    detail = {'status': 'ready', 'hasMatlStation': True,
              'matlStationInfo': {'slotInfos': [{'slotId': i, 'hasFilament': i == 1} for i in (1, 2, 3, 4)]}}
    with patch.object(job, '_mr', return_value={'result': {'status': {'save_variables': {'variables': {'display_off': 0}}}}}), patch.object(job, 'request_json', return_value={'result': {'status': status}}) as request, patch.object(job, 'read_stock_detail', return_value=detail), patch.object(job, '_printer_phase', return_value=('standby', '', 'Ready')):
        job._verify_filament_readiness(st, selection)
        assert all(call.kwargs.get('data') is None for call in request.call_args_list)
        status['print_stats']['state'] = 'printing'
        with pytest.raises(RuntimeError, match='head filament sensor reports no filament'):
            job._verify_filament_readiness(st, selection)


def test_makerworld_inventory_keeps_palette_and_objects():
    payload = {'instances': [{'id': 2, 'title': 'Colored model', 'extention': {'modelInfo': {'plates': [{'objects': [{'name': 'Eyes'}]}]}}, 'instanceFilaments': [{'type': 'PLA', 'color': '#FFFFFF'}, {'type': 'PLA', 'color': '#000000'}]}]}
    with patch.object(job, 'maker_headers', return_value={}), patch.object(job, 'request_json', return_value=payload):
        m = job.maker_inventory('https://makerworld.com/en/models/1')['sources'][0]
    assert m['filaments'][1]['color'] == '#000000'
    assert m['source_parts'][0]['objects'][0]['name'] == 'Eyes'


def test_slice_job_stl_binds_source_hash_before_decision_validation(tmp_path, monkeypatch):
    # Regression: an STL inventory has no source_sha256, which made every
    # single-color STL slice crash with KeyError('source_sha256').
    stl = tmp_path / 'clip.stl'
    stl.write_text('solid x\nendsolid x\n')
    st = {'spool': {'spoolman_id': 12, 'material': 'PLA'}, 'nozzle': {'diameter_mm': 0.4},
          'bed_mm': [220, 220], 'max_print_height_mm': 220,
          'filament_slots': {'3': {'spoolman_id': 8, 'color': 'black', 'material': 'PLA', 'physical_color_verified': True}}}
    selection = {'slot': 3, 'color': 'black', 'material': 'PLA', 'spoolman_id': 8}
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps({'downloaded_file': str(stl), 'audit': {'plates': 1, 'size_x': 40, 'size_y': 10, 'size_z': 5},
                              'filament_selection': selection,
                              'color_decisions': {'source_sha256': job.file_sha256(stl), 'schema': 'part_slots_v1',
                                                  'mode': 'single_color', 'selection': selection}}))
    seen = {}
    def stop(src, folder, state, brim, **kw):
        seen.update(kw)
        raise RuntimeError('reached slicer')
    monkeypatch.setattr(job.printer_state, 'load', lambda: st)
    monkeypatch.setattr(job.printer_state, 'check', lambda _: (True, [], {}))
    monkeypatch.setattr(job, 'slice_model', stop, raising=False)
    from print_handy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'slice_model', stop)
    with pytest.raises(RuntimeError, match='reached slicer'):
        job.slice_job(jp)
    assert seen.get('filament_slot') == 3


def test_inventory_of_stl_binds_source_hash(tmp_path):
    # Regression: STL inventory lacked source_sha256, crashing validate_decisions.
    stl = tmp_path / 'clip.stl'
    stl.write_text('solid x\nendsolid x\n')
    assert colors.inventory(stl)["source_sha256"] == job.file_sha256(stl)
