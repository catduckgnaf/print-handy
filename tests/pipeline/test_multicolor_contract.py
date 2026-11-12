"""Offline public CLI finalization/verification/launch tests. No live transport."""
import copy
import hashlib
import json
import re
import sys
from unittest.mock import patch

import pytest
from print_handy.core import model_job as job
from print_handy.printers.flashforge.zmod import multicolor_contract as contract
from print_handy.core import orca_adapter


def state():
    return copy.deepcopy(job.printer_state.load())


def artifact(n):
    return ('; HEADER_BLOCK_END\nM190 S60\nM104 S210 T0\nSTART_PRINT BED_TEMP=60 EXTRUDER_TEMP=210 MESH=MESH_DATA\n'
            'G90\nM83\n' +
            ''.join(f'T{i}\nG1 X{10+i} Y10 Z0.24 E1\n' for i in range(n)) +
            ';LAYER_CHANGE\n;WIDTH:0.5\n;LAYER_CHANGE\n;WIDTH:0.44\nEND_PRINT\n'
            '; nozzle_diameter = 0.4\n; first_layer_height = 0.24\n; filament_type = ' +
            ';'.join(['PLA']*n) + '\n')


def detail(st):
    return {'status': 'ready', 'errorCode': '', 'printProgress': 0, 'printDuration': 0,
            'leftTargetTemp': 0, 'rightTargetTemp': 0, 'platTargetTemp': 0, 'chamberTargetTemp': 0,
            'hasMatlStation': True, 'matlStationInfo': {'slotInfos': [
                {'slotId': i, 'hasFilament': True} for i in range(1,5)]}}


def transport(st, submissions):
    active = {'spool_id': (st.get('spool') or {}).get('spoolman_id')}
    def reply(st, path, data=None, **kwargs):
        if path == '/server/spoolman/spool_id':
            # Accounting pointer, not a printer command: never a submission.
            if data is not None:
                active['spool_id'] = data['spool_id']
            return {'result': {'spool_id': active['spool_id']}}
        if data is not None:
            submissions.append(data)
            return {'result': 'ok'}
        status = {'save_variables': {'variables': {'display_off': 0}},
                  'webhooks': {'state': 'ready'}, 'print_stats': {'state': 'standby'},
                  'extruder': {'target': 0}, 'heater_bed': {'target': 0},
                  'filament_switch_sensor head_switch_sensor': {'enabled': True},
                  'zmod_color': {'slots': [{'ID': str(i), 'Material': slot['material'], 'HEX': slot['hex']}
                                        for i, slot in st['filament_slots'].items()]}}
        return {'result': {'status': status}}
    return reply


def spoolman(url, **kwargs):
    assert '/api/v1/spool/' in url, url
    return {'archived': False, 'filament': {'material': 'PLA', 'name': 'offline'}}


def cli(args):
    with patch.object(sys, 'argv', ['model_job.py'] + args):
        job.main()


def test_t0_single_color_plate_uses_explicit_physical_slot_not_project_material_count():
    st = state()
    text = artifact(1).replace('; filament_type = PLA', '; filament_type = PLA;PLA;PLA')
    # T0 is a slicer tool index; the map selects the physical IFS slot.
    assert contract.validate_materials(text, 'PLA;PLA;PLA', st, {'0': 4}) == {'0': 4}
    assert contract.validate_materials(text, 'PLA;PLA;PLA', st, {'0': 1}) == {'0': 1}
    with pytest.raises(RuntimeError, match='T0 material'):
        contract.validate_materials(text, 'PETG;PLA;PLA', st, {'0': 4})


def test_quoted_material_with_a_space_is_valid():
    """2026-10-30 22:46: a single-color slice on a 'Silk PLA' spool failed with
    'malformed per-tool filament_type metadata' because Orca writes the name as
    `"Silk PLA"`; the quote is slicer syntax, not part of the material."""
    st = state()
    st['filament_slots']['2']['material'] = 'Silk PLA'
    text = artifact(1).replace('; filament_type = PLA', '; filament_type = "Silk PLA"')
    assert contract.validate_materials(text, '"Silk PLA"', st, {'0': 2}) == {'0': 2}
    text = artifact(2)
    assert contract.validate_materials(text, 'PLA;"Silk PLA"', st, {'0': 1, '1': 2}) == {'0': 1, '1': 2}


@pytest.mark.parametrize('material', ['"Silk PLA', 'PLA;;PLA', 'PL$A', '""'])
def test_malformed_material_metadata_is_still_refused(material):
    st = state()
    with pytest.raises(RuntimeError, match='malformed per-tool filament_type metadata'):
        contract.validate_materials(artifact(1), material, st, {'0': 1})


def test_multicolor_explicit_map_still_checks_each_tool_material():
    st = state()
    text = artifact(2)
    assert contract.validate_materials(text, 'PLA;PLA', st, {'0': 1, '1': 2}) == {'0': 1, '1': 2}
    with pytest.raises(RuntimeError, match='T1 material'):
        contract.validate_materials(text, 'PLA;PETG', st, {'0': 1, '1': 2})


# The 'reviewed-orca' variant of this test was removed in the 2026-11-07
# test-suite cleanup: it reran this same flow against recorded Orca coupon
# G-code from the host-only sol_recovery folder (PRINT_HANDY_COUPONS), which
# nothing in the repo or CI provides, so it skipped everywhere. The synthetic
# variant below covers the prepare/verify/launch flow; real Orca G-code is
# covered by the Slicer test jobs. See tests/TEST-SUITE-AUDIT-2026-11-07.md.
@pytest.mark.parametrize('bare_upload', [False, True])
@pytest.mark.parametrize('n,mapping', [(3, {'0':1,'1':2,'2':4}), (4, {'0':1,'1':2,'2':3,'3':4})])
def test_public_prepare_verify_launch(tmp_path, n, mapping, bare_upload):
    st=state(); path=tmp_path/'coupon.gcode'; path.write_text(artifact(n))
    mapfile=tmp_path/'map.json'; mapfile.write_text(json.dumps(mapping))
    original=path.read_text(); submissions=[]
    def upload(cmd, **kwargs):
        result={'item':{'root':'gcodes','path':cmd[3]}}
        return type('R', (), {'returncode':0, 'stdout':json.dumps(result if bare_upload else {'result':result}), 'stderr':''})()
    with patch.object(job.printer_state,'load',return_value=st), \
         patch.object(job.printer_state,'check',return_value=(True,[],{})), \
         patch.object(job,'_mr',side_effect=transport(st,submissions)), \
         patch.object(job,'request_json',side_effect=spoolman), \
         patch.object(job,'_printer_phase',return_value=('standby','','Ready')), \
         patch.object(job,'mesh_readiness',return_value={'display_off':0,'profile':'MESH_DATA','problem':None}), \
         patch.object(job,'read_stock_detail',return_value=detail(st)), \
         patch.object(job.subprocess,'run',side_effect=upload), \
         patch.object(job,'_fetch_remote_gcode_sha256',side_effect=lambda *a: job.file_sha256(path)), \
         patch.object(job,'_wait_printing',return_value='printing'), \
         patch.object(job,'record_start',return_value=tmp_path/'record.md'):
        cli(['finalize-gcode',str(path),'--tool-map',str(mapfile)])
        finalized=path.read_bytes()
        cli(['finalize-gcode',str(path),'--tool-map',str(mapfile)])
        assert path.read_bytes()==finalized
        assert job.zmod_md5_valid(path)
        from print_handy.printers.flashforge.zmod.native_startup import commands
        expected_body = original
        assert commands(expected_body) == commands(path.read_text())
        cli(['verify-gcode',str(path)])
        cli(['print-now',str(path)])
        assert len(submissions)==1
        command=submissions[0]['script']
        assert 'ALLOWED_TOOL_COUNT=' not in command
        for tool,slot in mapping.items():
            assert f'T{tool}={slot}' in command
        assert 'AUTO_ASSIGN=0' in command and 'LEVELING=0' in command
        # A replay is refused before anything is sent (#693): a clean REFUSED line, not a traceback.
        with pytest.raises(SystemExit,match='REFUSED: mapped submission checkpoint exists.*Nothing started'):
            cli(['print-now',str(path)])
        assert len(submissions)==1


@pytest.mark.parametrize('fault', ['checksum','unknown-map','material','malformed','missing-checksum'])
def test_launch_invalid_zero_submission(tmp_path,fault):
    st=state(); path=tmp_path/'coupon.gcode'; path.write_text(artifact(4))
    mapping={'0':1,'1':2,'2':3,'3':4}
    contract.finalize(path,None,st,mapping)
    payload=path.read_bytes().partition(b'\n')[2]
    if fault=='checksum':
        path.write_bytes(path.read_bytes()+b';changed\n')
    elif fault=='missing-checksum':
        path.write_bytes(payload)
    else:
        if fault=='unknown-map':
            payload=b'\n'.join(l for l in payload.split(b'\n') if not l.startswith(contract.MAP_PREFIX.encode()))
        elif fault=='material':
            payload=payload.replace(b'PLA;PLA;PLA;PLA',b'PLA;PETG;PLA;PLA')
        else:
            payload=payload.replace(b'PLA;PLA;PLA;PLA',b'PLA;;PLA;PLA')
        path.write_bytes(b'; MD5:'+hashlib.md5(payload).hexdigest().encode()+b'\n'+payload)
    submissions=[]
    with patch.object(job.printer_state,'load',return_value=st), \
         patch.object(job.printer_state,'check',return_value=(True,[],{})), \
         patch.object(job,'_mr',side_effect=transport(st,submissions)), \
         patch.object(job,'request_json',side_effect=spoolman), \
         patch.object(job,'_printer_phase',return_value=('standby','','Ready')), \
         patch.object(job,'mesh_readiness',return_value={'display_off':0,'profile':'MESH_DATA','problem':None}), \
         patch.object(job.subprocess,'run',side_effect=AssertionError('invalid upload reached')):
        with pytest.raises(SystemExit,match='did not pass its safety checks'):
            cli(['print-now',str(path)])
    assert submissions==[]


def test_idle_guard_refusal_happens_before_approval_upload_and_start(tmp_path):
    st = state()
    path = tmp_path/'mapped.gcode'
    path.write_text(artifact(4))
    mapping = {'0':1, '1':2, '2':3, '3':4}
    contract.finalize(path, None, st, mapping)
    submissions = []
    uploads = []
    def upload(*args, **kwargs):
        uploads.append(args)
        raise AssertionError('non-idle printer must not upload')
    mapped_job = {'approvals': []}
    moon = {'webhooks': {'state':'ready'}, 'print_stats': {'state':'standby'},
            'extruder': {'target':0}, 'heater_bed': {'target':0},
            'filament_switch_sensor head_switch_sensor': {'enabled':True},
            'zmod_color': {'slots': [{'ID': str(i), 'Material': slot['material'], 'HEX': slot['hex']}
                                     for i, slot in st['filament_slots'].items()]}}
    native = detail(st)
    native['rightTargetTemp'] = 210.0
    native['rightTemp'] = 119.74
    native['platTargetTemp'] = 60.0
    native['platTemp'] = 55.62
    mesh = {'display_off':0, 'profile':'MESH_DATA', 'problem':None}
    job_path = tmp_path/'JOB.json'
    job_path.write_text(json.dumps({'gcode': str(path), 'approvals': []}))
    def transport(_st, route, data=None, **kwargs):
        if data is not None:
            submissions.append((route, data))
            return {'result': 'ok'}
        return {'result': {'status': {'save_variables': {'variables': {'display_off': 0}}, **moon}}}
    with patch.object(job.printer_state, 'load', return_value=st), \
         patch.object(job.printer_state, 'check', return_value=(True, [], {})), \
         patch.object(job, '_mr', side_effect=transport), \
         patch.object(job, '_printer_phase', return_value=('standby', '', 'Ready')), \
         patch.object(job, 'mesh_readiness', return_value=mesh), \
         patch.object(job, 'read_stock_detail', return_value=native), \
         patch.object(job.subprocess, 'run', side_effect=upload):
        with patch.object(sys, 'argv', ['model_job.py', 'print-now', str(job_path)]):
            with pytest.raises(SystemExit, match='REFUSED: idle guard failed.*Nothing uploaded or started'):
                job.main()
    assert json.loads(job_path.read_text())['approvals'] == []
    assert mapped_job['approvals'] == []
    assert submissions == []
    assert uploads == []


def test_single_color_shared_finalization(tmp_path):
    st=state(); path=tmp_path/'single.gcode'; path.write_text(artifact(1))
    orca_adapter.finalize_zmod(path,'#067749','PLA')
    raw=path.read_bytes()
    orca_adapter.finalize_zmod(path,'#067749','PLA')
    assert raw==path.read_bytes() and job.zmod_md5_valid(path)
    with patch.object(job.printer_state,'load',return_value=st), \
         patch.object(job.printer_state,'check',return_value=(True,[],{})), \
         patch.object(job,'_mr',side_effect=transport(st,[])):
        assert job.verify_gcode(path)


@pytest.mark.parametrize('mapping',[None,{}, {'0':1,'1':9}, {'0':1,'2':3}, {'0':True}, {'0':1,'1':2}])
def test_invalid_mapping_refused(tmp_path,mapping):
    path=tmp_path/'invalid.gcode'; path.write_text(artifact(4))
    with pytest.raises(RuntimeError):
        contract.finalize(path,None,state(),mapping)


@pytest.mark.parametrize('n,initial', [(1,0), (1,1), (3,0), (3,1), (4,0), (4,1)])
@pytest.mark.parametrize('header', ['prefix', 'post', 'bare', 'orca-prefix', 'orca-post', 'orca-bare'])
def test_startup_controls_normal_cli(tmp_path, n, initial, header):
    st = state()
    path = tmp_path/'control.gcode'
    start = 'START_PRINT BED_TEMP=60 EXTRUDER_TEMP=210 MESH=MESH_DATA\n'
    pair = 'M190 S60\n; comments may separate commands\nM104 S210 T0\n'
    body = artifact(n).split('G90\n', 1)[1]
    if n == 1 and initial == 1:
        # One extruding logical tool T1 still needs indexed T0/T1 material
        # metadata; its map names only T1 (an unused T0 entry is refused, #1305).
        body = body.replace('T0\n', 'T1\n').replace('filament_type = PLA\n', 'filament_type = PLA;PLA\n')
    preamble = 'M73 P0 R20\nM106 S0\nM106 P2 S0\n;TYPE:Custom\n' if header.startswith('orca-') else ''
    variant = header.removeprefix('orca-')
    text = preamble + {'prefix': pair+start, 'post': start+pair, 'bare': start}[variant]
    original = text + f'T{initial}\nG90\n' + body
    path.write_text(original)
    mapping = {'1':2} if n == 1 and initial == 1 else {str(i):i+1 for i in range(n)}
    mapfile = tmp_path/'map.json'
    mapfile.write_text(json.dumps(mapping))
    submissions = []
    def upload(cmd, **kwargs):
        return type('R', (), {'returncode':0, 'stdout':json.dumps({'item':{'root':'gcodes','path':cmd[3]}}), 'stderr':''})()
    with patch.object(job.printer_state,'load',return_value=st), \
         patch.object(job.printer_state,'check',return_value=(True,[],{})), \
         patch.object(job,'_mr',side_effect=transport(st,submissions)), \
         patch.object(job,'request_json',side_effect=spoolman), \
         patch.object(job,'_printer_phase',return_value=('standby','','Ready')), \
         patch.object(job,'mesh_readiness',return_value={'display_off':0,'profile':'MESH_DATA','problem':None}), \
         patch.object(job,'read_stock_detail',return_value=detail(st)), \
         patch.object(job.subprocess,'run',side_effect=upload), \
         patch.object(job,'_fetch_remote_gcode_sha256',side_effect=lambda *a: job.file_sha256(path)), \
         patch.object(job,'_wait_printing',return_value='printing'), \
         patch.object(job,'record_start',return_value=tmp_path/'record.md'):
        cli(['finalize-gcode',str(path),'--tool-map',str(mapfile)])
        finalized = path.read_bytes()
        cli(['finalize-gcode',str(path),'--tool-map',str(mapfile)])
        assert path.read_bytes() == finalized
        from print_handy.printers.flashforge.zmod.native_startup import commands
        expected_body = original.replace(start, 'M190 S60\nM104 S210 T0\n'+start, 1) if variant == 'bare' else original
        assert commands(path.read_text()) == commands(expected_body)
        assert job.zmod_md5_valid(path)
        cli(['verify-gcode',str(path)])
        cli(['print-now',str(path)])
    assert len(submissions) == 1
    assert job.native_heater_lines_valid(path.read_text(),60,210)


@pytest.mark.parametrize('fault', ['post-conflict', 'intervening-motion', 'masked-post-conflict',
                                 'unsafe-motion', 'unsafe-tool', 'unsafe-extrusion',
                                 'unsafe-extra', 'unsafe-fan', 'unsafe-macro',
                                 'split-benign', 'preamble-post-conflict'])
def test_startup_defects_normal_cli_zero_transport(tmp_path, fault):
    st = state()
    path = tmp_path/'invalid-header.gcode'
    start = 'START_PRINT BED_TEMP=60 EXTRUDER_TEMP=210 MESH=MESH_DATA\n'
    pair = 'M190 S60\nM104 S210 T0\n'
    headers = {
        'post-conflict': start+'M190 S70\nM104 S250 T1\n',
        'intervening-motion': pair+'G1 X10 E1\n'+start,
        'masked-post-conflict': pair+start+'M190 S70\nM104 S250 T1\n',
        'unsafe-motion': 'G1 X10\n'+start,
        'unsafe-tool': 'T1\n'+start,
        'unsafe-extrusion': 'G1 E1\n'+start,
        'unsafe-extra': 'M73 P0 R20 E1\n'+start,
        'unsafe-fan': 'M106 S255\n'+start,
        'unsafe-macro': 'CUSTOM_MACRO\n'+start,
        'split-benign': pair+'M73 P0 R20\nM106 S0\n'+start,
        'preamble-post-conflict': 'M73 P0 R20\nM106 S0\n'+start+'M190 S70\nM104 S250 T1\n',
    }
    raw = headers[fault] + artifact(4).split('G90\n',1)[1]
    path.write_text(raw)
    mapfile = tmp_path/'map.json'
    mapfile.write_text(json.dumps({'0':1,'1':2,'2':3,'3':4}))
    submissions = []
    with patch.object(job.printer_state,'load',return_value=st), \
         patch.object(job.printer_state,'check',return_value=(True,[],{})), \
         patch.object(job,'_mr',side_effect=transport(st,submissions)), \
         patch.object(job,'request_json',side_effect=spoolman), \
         patch.object(job,'_printer_phase',return_value=('standby','','Ready')), \
         patch.object(job,'mesh_readiness',return_value={'display_off':0,'profile':'MESH_DATA','problem':None}), \
         patch.object(job.subprocess,'run',side_effect=AssertionError('invalid upload reached')):
        with pytest.raises(RuntimeError,match='native startup'):
            cli(['finalize-gcode',str(path),'--tool-map',str(mapfile)])
        assert path.read_text() == raw
        # Supply checksum-valid mapped metadata without repairing the bad header:
        # launch rejection must be the startup rule, not missing metadata/MD5.
        payload = (contract.MAP_PREFIX+json.dumps({'0':1,'1':2,'2':3,'3':4})+'\n'+raw).encode()
        path.write_bytes(b'; MD5:'+hashlib.md5(payload).hexdigest().encode()+b'\n'+payload)
        with pytest.raises(SystemExit,match='immediate startup'):
            cli(['print-now',str(path)])
    assert submissions == []


def test_corrupt_checksum_not_repaired(tmp_path):
    path=tmp_path/'invalid.gcode'; path.write_text(artifact(1))
    orca_adapter.finalize_zmod(path,'#067749','PLA')
    path.write_bytes(path.read_bytes()+b';corruption\n')
    with pytest.raises(RuntimeError,match='invalid existing ZMod checksum'):
        orca_adapter.finalize_zmod(path,'#067749','PLA')


def test_infinite_spool_requires_same_color_and_material():
    slots = {
        '1': {'hex': '#067749', 'material': 'PLA'},
        '2': {'hex': '067749', 'material': 'PLA'},
        '3': {'hex': '#067749', 'material': 'PETG'},
        '4': {'hex': '#FFFFFF', 'material': 'PLA'},
    }
    groups = contract.infinite_spool_groups(slots)
    assert groups == [{'color': '#067749', 'material': 'PLA', 'slots': [1, 2]}]
    material_only = {'1': {'hex': '#067749', 'material': 'PLA'}, '2': {'hex': '#FFFFFF', 'material': 'PLA'}}
    color_only = {'1': {'hex': '#067749', 'material': 'PLA'}, '2': {'hex': '#067749', 'material': 'PETG'}}
    assert contract.infinite_spool_groups(material_only) == []
    assert contract.infinite_spool_groups(color_only) == []
    assert contract.infinite_spool_groups({'1': slots['1']}) == []


def test_slot_commands_do_not_enable_infinite_spool():
    for action in ('set_extruder_slot', 'set_current_prutok', 'print_menu_remap', 'shared_spool'):
        verdict = contract.classify_ifs_action(action)
        assert verdict['enables_infinite_spool'] is False
    with pytest.raises(RuntimeError, match='unknown IFS action'):
        contract.classify_ifs_action('SAVE_ZMOD_DATA')


# --- #1305: noncontiguous (sparse) tool maps ---------------------------------
# A slicer plate that uses a subset of the project's filaments emits exactly
# those tools: filaments 1 and 3 used, 2 skipped -> T0 and T2; filament 3
# alone -> T2. The operator's explicit map for such a file is the selection
# the launch must preserve; coverage of the used tools, not contiguity of
# the mapped ones, is the invariant (validate_materials/finalize see the
# file, normalize_mapping does not).

def sparse_artifact(tools, materials):
    return ('; HEADER_BLOCK_END\nM190 S60\nM104 S210 T0\nSTART_PRINT BED_TEMP=60 EXTRUDER_TEMP=210 MESH=MESH_DATA\n'
            'G90\nM83\n' +
            ''.join(f'T{t}\nG1 X{10 + t} Y10 Z0.24 E1\n' for t in tools) +
            ';LAYER_CHANGE\n;WIDTH:0.5\n;LAYER_CHANGE\n;WIDTH:0.44\nEND_PRINT\n'
            '; nozzle_diameter = 0.4\n; first_layer_height = 0.24\n; filament_type = ' +
            ';'.join(materials) + '\n')


def test_sparse_tool_map_validates_and_describes():
    st = state()
    text = sparse_artifact([0, 2], ['PLA', 'PLA', 'PLA'])
    assert contract.validate_materials(text, 'PLA;PLA;PLA', st, {'0': 1, '2': 3}) == {'0': 1, '2': 3}
    assert contract.describe_mapping({'0': 1, '2': 3}, st).splitlines() == [
        'T0 -> slot 1: white PLA', 'T2 -> slot 3: black PLA']
    # An explicit map entry for a tool the plate never uses is refused by
    # name: it would reach zmod_color_data and SET_ZCOLOR and make launch
    # require that slot loaded, for nothing.
    with pytest.raises(RuntimeError, match=r'names T3, which the file never uses \(it uses T0, T2\)'):
        contract.validate_materials(text, 'PLA;PLA;PLA', st, {'0': 1, '2': 3, '3': 4})


def test_explicit_map_with_unused_tool_refused_at_finalize(tmp_path):
    st = state()
    path = tmp_path / 'sparse.gcode'
    path.write_text(sparse_artifact([0, 2], ['PLA', 'PLA', 'PLA']))
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match='names T3, which the file never uses'):
        contract.finalize(path, None, st, {'0': 1, '2': 3, '3': 4})
    assert path.read_bytes() == before


def test_slice_path_drops_unused_tool_entries(tmp_path):
    # Print Handy's own slice map can name a tool Orca never selected (a
    # support filament's tool with no support generated, #867); that flow
    # trims the entry instead of refusing.
    st = state()
    path = tmp_path / 'sparse.gcode'
    path.write_text(sparse_artifact([0, 2], ['PLA', 'PLA', 'PLA']))
    contract.finalize(path, None, st, {'0': 1, '2': 3, '3': 4}, drop_unused=True)
    text = path.read_text()
    assert contract.embedded_mapping(text, st) == {'0': 1, '2': 3}
    assert '; zmod_color_data = 3|' not in text


def test_shared_spool_map_is_accepted_and_each_tool_checked(tmp_path):
    # Two tools on one slot (classify_ifs_action's 'shared_spool') is an
    # accepted explicit map: both tools are material-checked against it.
    st = state()
    text = sparse_artifact([0, 2], ['PLA', 'PLA', 'PLA'])
    assert contract.normalize_mapping({'0': 1, '2': 1}, st) == {'0': 1, '2': 1}
    assert contract.validate_materials(text, 'PLA;PLA;PLA', st, {'0': 1, '2': 1}) == {'0': 1, '2': 1}
    with pytest.raises(RuntimeError, match='T2 material'):
        contract.validate_materials(sparse_artifact([0, 2], ['PLA', 'PLA', 'PETG']), 'PLA;PLA;PETG', st,
                                    {'0': 1, '2': 1})
    path = tmp_path / 'shared.gcode'
    path.write_text(text)
    contract.finalize(path, None, st, {'0': 1, '2': 1})
    assert contract.embedded_mapping(path.read_text(), st) == {'0': 1, '2': 1}


def test_lone_high_tool_map_validates():
    st = state()
    text = sparse_artifact([2], ['PLA', 'PLA', 'PLA'])
    assert contract.validate_materials(text, 'PLA;PLA;PLA', st, {'2': 3}) == {'2': 3}


def test_sparse_tool_map_finalizes_and_reads_back(tmp_path):
    st = state()
    path = tmp_path / 'sparse.gcode'
    path.write_text(sparse_artifact([0, 2], ['PLA', 'PLA', 'PLA']))
    contract.finalize(path, None, st, {'0': 1, '2': 3})
    text = path.read_text()
    assert contract.embedded_mapping(text, st) == {'0': 1, '2': 3}
    assert '; zmod_color_data = 0|' in text and '; zmod_color_data = 2|' in text
    assert '; zmod_color_data = 1|' not in text
    assert job.zmod_md5_valid(path)


def test_sparse_map_material_mismatch_names_the_sparse_tool():
    st = state()
    st['filament_slots']['3']['material'] = 'PETG'
    text = sparse_artifact([0, 2], ['PLA', 'PLA', 'PLA'])
    with pytest.raises(RuntimeError, match='T2 material'):
        contract.validate_materials(text, 'PLA;PLA;PLA', st, {'0': 1, '2': 3})
    assert contract.validate_materials(
        sparse_artifact([0, 2], ['PLA', 'PLA', 'PETG']), 'PLA;PLA;PETG', st, {'0': 1, '2': 3}) == {'0': 1, '2': 3}


def test_sparse_map_missing_slot_refused():
    st = state()
    st['filament_slots']['3'] = {'material': '', 'physical_color_verified': False}
    with pytest.raises(RuntimeError, match='unknown or unverified mapping for physical slot 3'):
        contract.normalize_mapping({'0': 1, '2': 3}, st)
    st = state()
    del st['filament_slots']['3']
    with pytest.raises(RuntimeError, match='unknown or unverified mapping for physical slot 3'):
        contract.normalize_mapping({'0': 1, '2': 3}, st)


def test_sparse_map_omitting_a_used_tool_refused():
    st = state()
    text = sparse_artifact([0, 2], ['PLA', 'PLA', 'PLA'])
    with pytest.raises(RuntimeError, match='used tools disagree with explicit mapping'):
        contract.validate_materials(text, 'PLA;PLA;PLA', st, {'0': 1})
    # The pre-#1305 invalid case {'0':1,'2':3} against a four-tool file is
    # still refused — now by omission of T1/T3, not by noncontiguity.
    with pytest.raises(RuntimeError, match='used tools disagree with explicit mapping'):
        contract.validate_materials(artifact(4), 'PLA;PLA;PLA;PLA', st, {'0': 1, '2': 3})


def test_sparse_map_short_material_list_names_the_omission():
    st = state()
    text = sparse_artifact([0, 2], ['PLA', 'PLA'])
    with pytest.raises(RuntimeError, match='per-tool material metadata omits a used logical tool'):
        contract.validate_materials(text, 'PLA;PLA', st, {'0': 1, '2': 3})


def test_sparse_map_conflicting_embedded_metadata_refused(tmp_path):
    st = state()
    path = tmp_path / 'conflict.gcode'
    path.write_text(sparse_artifact([0, 2], ['PLA', 'PLA', 'PLA']))
    contract.finalize(path, None, st, {'0': 1, '2': 3})
    lines = path.read_text().splitlines(keepends=True)
    dup = [line for line in lines if line.startswith(contract.MAP_PREFIX)]
    assert len(dup) == 1
    path.write_text(''.join(lines) + dup[0])
    with pytest.raises(RuntimeError, match='duplicate tool mapping metadata'):
        contract.embedded_mapping(path.read_text(), st)


def test_sparse_map_spool_swap_since_slicing_refused(tmp_path):
    st = state()
    path = tmp_path / 'swapped.gcode'
    path.write_text(sparse_artifact([0, 2], ['PLA', 'PLA', 'PLA']))
    contract.finalize(path, None, st, {'0': 1, '2': 3})
    st['filament_slots']['3']['spoolman_id'] = 99
    with pytest.raises(RuntimeError, match=r'slot 3 spool changed since slicing \(was 13, now 99\)'):
        contract.embedded_mapping(path.read_text(), st)


def test_sparse_public_prepare_verify_launch(tmp_path):
    st = state()
    path = tmp_path / 'sparse_coupon.gcode'
    path.write_text(sparse_artifact([0, 2], ['PLA', 'PLA', 'PLA']))
    mapping = {'0': 1, '2': 4}
    mapfile = tmp_path / 'map.json'
    mapfile.write_text(json.dumps(mapping))
    submissions = []
    def upload(cmd, **kwargs):
        result = {'item': {'root': 'gcodes', 'path': cmd[3]}}
        return type('R', (), {'returncode': 0, 'stdout': json.dumps({'result': result}), 'stderr': ''})()
    with patch.object(job.printer_state, 'load', return_value=st), \
         patch.object(job.printer_state, 'check', return_value=(True, [], {})), \
         patch.object(job, '_mr', side_effect=transport(st, submissions)), \
         patch.object(job, 'request_json', side_effect=spoolman), \
         patch.object(job, '_printer_phase', return_value=('standby', '', 'Ready')), \
         patch.object(job, 'mesh_readiness', return_value={'display_off': 0, 'profile': 'MESH_DATA', 'problem': None}), \
         patch.object(job, 'read_stock_detail', return_value=detail(st)), \
         patch.object(job.subprocess, 'run', side_effect=upload), \
         patch.object(job, '_fetch_remote_gcode_sha256', side_effect=lambda *a: job.file_sha256(path)), \
         patch.object(job, '_wait_printing', return_value='printing'), \
         patch.object(job, 'record_start', return_value=tmp_path / 'record.md'):
        cli(['finalize-gcode', str(path), '--tool-map', str(mapfile)])
        finalized = path.read_bytes()
        cli(['finalize-gcode', str(path), '--tool-map', str(mapfile)])
        assert path.read_bytes() == finalized
        assert job.zmod_md5_valid(path)
        cli(['verify-gcode', str(path)])
        cli(['print-now', str(path)])
    assert len(submissions) == 1
    command = submissions[0]['script']
    assert 'T0=1' in command and 'T2=4' in command
    assert 'T1=' not in command and 'T3=' not in command
    assert 'AUTO_ASSIGN=0' in command


def test_live_slot_label_pla_accepts_pla_plus_spool(tmp_path):
    # zmod_color labels a PLA+ spool's slot as plain PLA. The live check must use the material
    # family rule (same_material), not an exact string, or a correctly loaded PLA+ spool is refused.
    st = state()
    for slot in st['filament_slots'].values():
        slot['material'] = 'PLA+'
    path = tmp_path/'coupon.gcode'; path.write_text(artifact(3))
    mapping = {'0': 1, '1': 2, '2': 4}
    mapfile = tmp_path/'map.json'; mapfile.write_text(json.dumps(mapping))
    submissions = []
    live = transport(st, submissions)
    def coarse(st_, route, data=None, **kwargs):
        reply = live(st_, route, data, **kwargs)
        slots = (reply.get('result') or {}).get('status', {}) if isinstance(reply.get('result'), dict) else {}
        for item in slots.get('zmod_color', {}).get('slots', []):
            item['Material'] = 'PLA'
        return reply
    def upload(cmd, **kwargs):
        return type('R', (), {'returncode': 0, 'stdout': json.dumps({'result': {'item': {'root': 'gcodes', 'path': cmd[3]}}}), 'stderr': ''})()
    with patch.object(job.printer_state, 'load', return_value=st), \
         patch.object(job.printer_state, 'check', return_value=(True, [], {})), \
         patch.object(job, '_mr', side_effect=coarse), \
         patch.object(job, 'request_json', side_effect=spoolman), \
         patch.object(job, '_printer_phase', return_value=('standby', '', 'Ready')), \
         patch.object(job, 'mesh_readiness', return_value={'display_off': 0, 'profile': 'MESH_DATA', 'problem': None}), \
         patch.object(job, 'read_stock_detail', return_value=detail(st)), \
         patch.object(job.subprocess, 'run', side_effect=upload), \
         patch.object(job, '_fetch_remote_gcode_sha256', side_effect=lambda *a: job.file_sha256(path)), \
         patch.object(job, '_wait_printing', return_value='printing'), \
         patch.object(job, 'record_start', return_value=tmp_path/'record.md'):
        cli(['finalize-gcode', str(path), '--tool-map', str(mapfile)])
        cli(['print-now', str(path)])
    assert len(submissions) == 1


def test_a_file_finalized_before_1305_with_an_unused_tool_still_launches():
    # Files finalized under the old contiguity rule can embed a map entry for
    # a tool they never select (an unused support tool). Launch validation
    # reads that embedded map and must keep accepting it: only an operator's
    # explicit map is held to "map only the used tools".
    st = state()
    text = sparse_artifact([0, 2], ['PLA', 'PLA', 'PLA', 'PLA'])
    text = text.rstrip('\n') + '\n' + contract.MAP_PREFIX + json.dumps({'0': 1, '1': 2, '2': 3, '3': 4}) + '\n'
    assert contract.validate_materials(text, 'PLA;PLA;PLA;PLA', st) == {'0': 1, '1': 2, '2': 3, '3': 4}
