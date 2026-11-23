import json
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from print_handy.core import model_job as mj
from print_handy.gcode import artifact as gcode_artifact
from print_handy.gcode import verify as gcode_verify
from print_handy.jobs import common as jobs_common
from print_handy.jobs import filament as jobs_filament
from print_handy.jobs import prompts as jobs_prompts
from print_handy.jobs import slice as jobs_slice
from print_handy.jobs import store as jobs_store
from print_handy.launch import gates as launch_gates
from print_handy.launch import print_now as launch_print_now
from print_handy.core import orca_adapter
from .test_color_intake import source, state
from .test_discord_color_intake import Transport
from print_handy.core.discord_color_intake import collect
from tests.bound_names import setattr_bound


def setup_job(tmp_path, monkeypatch):
    st=state(); st.update(bed_mm=[220,220],max_print_height_mm=220,moonraker_url='offline://fake')
    for slot in st['filament_slots'].values():
        slot['physical_color_verified']=True
    p=source(tmp_path); jp=tmp_path/'JOB.json'
    jp.write_text(json.dumps({'downloaded_file':str(p),'audit':{'plates':1,'size_x':40,'size_y':10,'size_z':1}}))
    jobs_prompts.intake_color_choices(jp,st,'single_color',[1])
    monkeypatch.setattr(mj.printer_state,'load',lambda *_a, **_k: st)
    monkeypatch.setattr(mj.printer_state,'check',lambda _: (True,[],{}))
    def slice_fake(src,folder,st,brim,color_decisions,filament_slot=None, copies=1):
        folder.mkdir(parents=True); out=folder/'plate_1.gcode'
        out.write_text(json.dumps(color_decisions,sort_keys=True))
        return out,{'offline_test':True},[]
    monkeypatch.setattr(orca_adapter,'slice_model',slice_fake)
    setattr_bound(monkeypatch, gcode_verify,'verify_gcode',lambda _:None)
    from print_handy.printers.flashforge.zmod import multicolor_contract
    monkeypatch.setattr(multicolor_contract,'finalize',lambda *a,**k:None)
    old=jobs_slice.slice_job(jp)
    return jp,st,Path(old)


def test_unbound_job_refused_even_without_color_decisions(tmp_path):
    gcode = tmp_path / 'plain.gcode'
    gcode.write_text('G1 X1\n')
    job = {'gcode': str(gcode), 'gcode_verified': True}
    with pytest.raises(RuntimeError, match='stale color artifact: reslice current JOB'):
        launch_gates.require_current_artifact(job, gcode)
    job['artifact_binding'] = {}
    with pytest.raises(RuntimeError, match='stale color artifact: reslice current JOB'):
        launch_gates.require_current_artifact(job, gcode)
    # A loose G-code path has no JOB.json to bind. That separate route is unchanged.
    assert launch_gates.require_current_artifact(None, gcode) is None


def test_normal_single_color_slice_persists_binding(tmp_path, monkeypatch):
    jp, _state, gcode = setup_job(tmp_path, monkeypatch)
    job = json.loads(jp.read_text())
    assert job['artifact_binding']['gcode_sha256'] == gcode_artifact.file_sha256(gcode)
    launch_gates.require_current_artifact(job, gcode)


@pytest.mark.asyncio
async def test_persisted_derived_binding_hash_keys_roundtrip_and_tamper_refusal(tmp_path, monkeypatch):
    jp, st, gcode = setup_job(tmp_path, monkeypatch)
    job = json.loads(jp.read_text())
    job['derived_plates'] = [{'slot': 4, 'gcode': str(gcode)}]
    job['artifact_binding'] = {'source_sha256': gcode_artifact.file_sha256(job['downloaded_file']),
        'decisions_sha256': jobs_store.decisions_digest(job['color_decisions']),
        'gcode_sha256': {'4': gcode_artifact.file_sha256(gcode)},
        'recipe_version': job['slice_recipe']['version'], 'recipe_sha256': job['slice_recipe']['sha256']}
    job['gcode'] = str(gcode)
    job['gcode_verified'] = True
    jp.write_text(json.dumps(job))
    persisted = json.loads(jp.read_text())
    launch_gates.require_current_artifact(persisted, gcode)
    altered = json.loads(json.dumps(persisted))
    altered['artifact_binding']['gcode_sha256']['4'] = '0' * 64
    with pytest.raises(RuntimeError, match='stale color artifact'):
        launch_gates.require_current_artifact(altered, gcode)




# Restored coverage for the removed test_callback_swap_stale_refusal_and_reslice_launch.
# Contract (t_c5e33cc5): an identical Discord replay is a byte-for-byte no-op; a changed
# answer is an authorized correction that invalidates the bound artifact (old bytes kept,
# history appended) and launch refuses until reslice; the reslice launches through
# print_mapped_native with exactly one SET_ZCOLOR, and replays cannot resubmit.
def test_callback_swap_stale_refusal_and_reslice_launch(tmp_path, monkeypatch):
    import asyncio, copy, subprocess
    from print_handy.core import color_geometry, source_colors
    from .test_multicolor_contract import artifact, detail, transport
    st = copy.deepcopy(mj.printer_state.load())
    monkeypatch.setattr(mj.printer_state, 'load', lambda *_a, **_k: st)
    monkeypatch.setattr(mj.printer_state, 'check', lambda _: (True, [], {}))
    p = source(tmp_path)
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps({'downloaded_file': str(p), 'color_schema': 'part_slots_v1', 'audit': {'plates': 1},
                              'color_manifest': source_colors.inventory(p)}))
    def plate_fake(src, target, decisions, part_keys, bed=None, slot_colors=None):
        Path(target).write_text(json.dumps(part_keys)); return {'offline_test': True}
    def slice_fake(plate, folder, st, brim, preserve_layout=True, filament_slot=None):
        folder.mkdir(parents=True); out = folder / 'plate_1.gcode'
        out.write_text(artifact(1) + f'; test_plate = {Path(plate).read_text()}\n')
        return out, {'offline_test': True}, []
    monkeypatch.setattr(color_geometry, 'export_single_color_plate', plate_fake)
    # The fixture's parts share build objects across slots and the exporter is
    # faked; this test is about artifact binding, not the #820 assembly guard.
    monkeypatch.setattr(jobs_slice, 'refuse_assembly_split', lambda *a, **k: None)
    monkeypatch.setattr(orca_adapter, 'slice_model', slice_fake)
    # #1473: decline the proposed mapping (1), then the authored per-part picks.
    asyncio.run(collect(jp, st, Transport([1, 0, 3]), 'offline', 'first', timeout=1))
    first = json.loads(jp.read_text())['color_decisions']
    assert sorted(c['slot'] for c in first['choices'].values()) == [1, 4]
    jobs_slice.slice_job(jp)
    bound = json.loads(jp.read_text())
    old = {row['slot']: (Path(row['gcode']), Path(row['gcode']).read_bytes()) for row in bound['derived_plates']}
    assert set(old) == {1, 4}
    # Identical replay: no side effects at all.
    before = jp.read_bytes()
    replay = asyncio.run(collect(jp, st, Transport([0, 3]), 'offline', 'replay', timeout=1))
    assert replay['status'] == 'COLOR_CHOICES_UNCHANGED' and replay['started'] is False
    assert jp.read_bytes() == before
    # Changed answer: authorized correction invalidates the artifact without touching bytes.
    swap = asyncio.run(collect(jp, st, Transport([3, 0]), 'offline', 'swap', timeout=1))
    assert swap['status'] == 'COLOR_CHOICES_SAVED' and swap['uploaded'] is False and swap['started'] is False
    stale = json.loads(jp.read_text())
    assert stale['gcode_verified'] is False and 'gcode' not in stale and 'artifact_binding' not in stale
    assert stale['artifact_history']
    assert all(path.read_bytes() == raw for path, raw in old.values())
    submissions = []
    def no_upload(*a, **k):
        raise AssertionError('stale artifact reached upload')
    setattr_bound(monkeypatch, jobs_common, '_mr', transport(st, submissions))
    from .test_multicolor_contract import spoolman
    setattr_bound(monkeypatch, jobs_common, 'request_json', spoolman)
    monkeypatch.setattr(mj.subprocess, 'run', no_upload)
    with pytest.raises(RuntimeError, match='stale color artifact'):
        launch_print_now.print_now(str(jp))
    for path, _ in old.values():
        with pytest.raises(RuntimeError, match='stale color artifact'):
            launch_print_now.print_now(str(path))
    assert submissions == []
    # Reslice under the corrected decisions, then launch exactly once.
    jobs_slice.slice_job(jp)
    fresh = json.loads(jp.read_text())
    assert fresh['gcode_verified'] is True and set(fresh['artifact_binding']['gcode_sha256']) == {'1', '4'}
    g = Path(fresh['gcode'])
    assert g not in {path for path, _ in old.values()}
    text = g.read_text()
    assert '; print_handy_tool_slots = {"0":{"physical_slot":1,' in text and gcode_verify.zmod_md5_valid(g)
    assert first != fresh['color_decisions']
    def upload(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, json.dumps({'result': {'item': {'root': 'gcodes', 'path': cmd[3]}}}), '')
    monkeypatch.setattr(mj.subprocess, 'run', upload)
    setattr_bound(monkeypatch, launch_gates, '_printer_phase', lambda _: ('standby', '', 'Ready'))
    setattr_bound(monkeypatch, launch_gates, 'mesh_readiness', lambda _: {'display_off': 0, 'profile': 'MESH_DATA', 'problem': None})
    setattr_bound(monkeypatch, launch_gates, 'screen_z_offset', lambda _: -0.05)
    setattr_bound(monkeypatch, jobs_filament, 'read_stock_detail', lambda _: detail(st))
    monkeypatch.setattr(mj, '_fetch_remote_gcode_sha256', lambda *a: gcode_artifact.file_sha256(g))
    monkeypatch.setattr(launch_print_now, '_wait_printing', lambda *a, **k: 'printing')
    setattr_bound(monkeypatch, launch_print_now, 'record_start', lambda *a, **k: tmp_path / 'record.md')
    routed = []
    real_mapped = launch_print_now.print_mapped_native
    monkeypatch.setattr(launch_print_now, 'print_mapped_native', lambda *a, **k: routed.append(a[3]) or real_mapped(*a, **k))
    out = launch_print_now.print_now(str(jp))
    assert routed == [{'0': 1}] and out['submission_count'] == 1
    assert len(submissions) == 1
    command = submissions[0]['script']
    assert command.startswith('SET_ZCOLOR ') and command.count('SET_ZCOLOR') == 1
    assert 'T0=1' in command and 'T0=4' not in command and 'AUTO_ASSIGN=0' in command
    # A replay cannot resubmit: the approval is single use.
    with pytest.raises(SystemExit, match='already used'):
        launch_print_now.print_now(str(jp))
    assert len(submissions) == 1
    # The checkpoint records one launch attempt (print-again reads it), so it stays; but it must not
    # outlive its attempt as a block: an explicit reprint (a new bed-clear approval, multi-copy plates)
    # is a new attempt and replaces it. Only an unapproved replay is refused (test_launch_state).
    first_attempt = json.loads(g.with_suffix('.launch.json').read_text())
    assert first_attempt['accepted'] is True and first_attempt['lease_token']
    out = launch_print_now.print_now(str(jp), reprint=True)
    assert len(submissions) == 2 and out['submission_count'] == 1
    second_attempt = json.loads(g.with_suffix('.launch.json').read_text())
    assert second_attempt['lease_token'] != first_attempt['lease_token']
