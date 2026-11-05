import json,os,re,zipfile,xml.etree.ElementTree as ET
from pathlib import Path
import pytest
from print_handy.core import source_colors, model_job
from print_handy.core.color_geometry import export_color_geometry, export_single_color_plate

ROOT=Path(os.environ.get('PRINT_HANDY_COUPONS','/nonexistent'))
STATE=Path(__file__).parent/'fixtures'/'printer_hardware.json'
needs_coupon=pytest.mark.skipif(not (ROOT/'three/three_core_geometry.3mf').is_file(),
 reason='recorded coupon 3MF is not shipped; set PRINT_HANDY_COUPONS to the sol_recovery folder')

def decisions(src,st):
 m=source_colors.inventory(src)
 slots=[1,2,4]
 return {'mode':'per_part','source_sha256':m['source_sha256'],'choices':{
  q['key']:model_job.resolve_filament_selection(st,dict(st['filament_slots'][str(slots[q['source_extruder']-1])],slot=slots[q['source_extruder']-1])) for q in source_colors.prompts(m)}}

@needs_coupon
def test_preserves_instances_and_transforms(tmp_path):
 src=ROOT/'three/three_core_geometry.3mf';target=tmp_path/'instances.3mf'
 ns={'c':'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}
 with zipfile.ZipFile(src) as z:
  entries={n:z.read(n) for n in z.namelist()}
 model=ET.fromstring(entries['3D/3dmodel.model']);build=model.find('c:build',ns)
 item=build[0];item.set('transform','1 0 0 0 1 0 0 0 1 10 20 0')
 extra=ET.SubElement(build,item.tag,objectid=item.get('objectid'),transform='1 0 0 0 1 0 0 0 1 50 20 0')
 entries['3D/3dmodel.model']=ET.tostring(model)
 with zipfile.ZipFile(target,'w') as z:
  for n,b in entries.items():z.writestr(n,b)
 st=json.loads(STATE.read_text());d=decisions(target,st)
 mapping,report=export_color_geometry(target,tmp_path/'out.3mf',d)
 assert len(report['parts'])==6
 assert len({p['build_key'] for p in report['parts']})==6
 with zipfile.ZipFile(tmp_path/'out.3mf') as z:
  out=ET.fromstring(z.read('3D/3dmodel.model'))
 objects=out.findall('c:resources/c:object',ns)[:-1]
 # Repeated build instance differs by exactly 40 mm, not merged or rearranged.
 for a,b in zip(objects[:3],objects[3:]):
  pa=a.findall('c:mesh/c:vertices/c:vertex',ns);pb=b.findall('c:mesh/c:vertices/c:vertex',ns)
  assert len(pa)==len(pb)
  for va,vb in zip(pa,pb):
   assert float(vb.get('x'))-float(va.get('x'))==pytest.approx(40)
   assert va.get('y')==vb.get('y') and va.get('z')==vb.get('z')

@needs_coupon
@pytest.mark.parametrize('failure',['hash','incomplete','painted','contradiction'])
def test_invalid_geometry_mapping_refused(tmp_path,failure):
 src=ROOT/'three/three_core_geometry.3mf';st=json.loads(STATE.read_text());d=decisions(src,st)
 if failure=='hash':d['source_sha256']='wrong'
 if failure=='incomplete':d['choices'].pop(next(iter(d['choices'])))
 if failure=='contradiction':
  m=source_colors.inventory(src);d['choices']['filament:1']=d['choices']['part:'+m['parts'][1]['key']]
 if failure=='painted':
  from unittest.mock import patch
  m=source_colors.inventory(src);m['painted']=True
  # Painted sources are supported now; a painted flag whose decisions do not
  # cover the painted states must still fail closed with no output.
  with patch('print_handy.core.color_geometry.inventory',return_value=m),pytest.raises(RuntimeError):
   export_color_geometry(src,tmp_path/'out.3mf',d)
  assert not (tmp_path/'out.3mf').exists()
  return
 with pytest.raises(RuntimeError):export_color_geometry(src,tmp_path/'out.3mf',d)
 assert not (tmp_path/'out.3mf').exists()

def _mesh(path, vertices):
    body = ''.join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in vertices)
    return f'<model unit="millimeter"><resources><object id="1"><mesh><vertices>{body}</vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources></model>'


def test_single_color_plate_shifts_without_cropping_or_rewriting_mesh(tmp_path):
    source = tmp_path / 'source.3mf'
    holder = [(0, 0, 0), (40, 0, 0), (0, 20, 10)]
    stencil = [(0, 0, 0), (30, 0, 0), (0, 20, 5)]
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('3D/Objects/object_1.model', _mesh('object_1', holder))
        archive.writestr('3D/Objects/object_2.model', _mesh('object_2', stencil))
        archive.writestr('3D/3dmodel.model', '''<model xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" unit="millimeter"><resources>
          <object id="1" name="Holder"><components><component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>
          <object id="2" name="Stencil"><components><component p:path="/3D/Objects/object_2.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>
        </resources><build>
          <item objectid="1" transform="1 0 0 0 1 0 0 0 1 20 30 0"/>
          <item objectid="2" transform="1 0 0 0 1 0 0 0 1 40 225 0"/>
        </build></model>''')
        archive.writestr('Metadata/model_settings.config', '''<config>
          <object id="1"><metadata key="name" value="Holder"/><metadata key="extruder" value="1"/><part id="1"><metadata key="name" value="Holder"/><metadata key="extruder" value="1"/></part></object>
          <object id="2"><metadata key="name" value="Stencil"/><metadata key="extruder" value="1"/><part id="1"><metadata key="name" value="Stencil"/><metadata key="extruder" value="1"/></part></object>
          <plate><metadata key="plater_id" value="1"/><model_instance><metadata key="object_id" value="1"/></model_instance><model_instance><metadata key="object_id" value="2"/></model_instance></plate>
        </config>''')
        archive.writestr('Metadata/project_settings.config', json.dumps({'filament_colour': ['#FFFFFF'], 'filament_type': ['PLA']}))
        archive.writestr('3D/_rels/3dmodel.model.rels', '''<Relationships>
 <Relationship Target="/3D/Objects/object_1.model" Id="rel-1"/>
 <Relationship Target="/3D/Objects/object_2.model" Id="rel-2"/>
</Relationships>''')
    manifest = source_colors.inventory(source)
    state = json.loads(STATE.read_text())
    slot = dict(state['filament_slots']['4'], slot=4)
    selection = model_job.resolve_filament_selection(state, slot)
    decisions = {'schema': 'part_slots_v1', 'mode': 'per_part', 'source_sha256': manifest['source_sha256'],
                 'choices': {row['key']: selection for row in source_colors.canonical_prompts(manifest)}}
    before = source.read_bytes()
    report = export_single_color_plate(source, tmp_path / 'stencil.3mf', decisions, ['2:1'])
    assert source.read_bytes() == before
    assert report['slot'] == 4
    assert report['part_keys'] == ['2:1']
    assert report['placed_bounds_mm'][1][1] <= 215
    with zipfile.ZipFile(tmp_path / 'stencil.3mf') as archive:
        root = ET.fromstring(archive.read('3D/3dmodel.model'))
        items = [node for node in root.iter() if node.tag.split('}')[-1] == 'item']
        # Orca refuses packages with removed objects, so every build item stays
        # and only the selected one remains printable.
        assert [(item.get('objectid'), item.get('printable')) for item in items] == [('1', '0'), ('2', '1')]
        shift = report['translation_mm']
        assert [float(value) for value in items[1].get('transform').split()[-3:]] == [pytest.approx(40 + shift[0]), pytest.approx(225 + shift[1]), pytest.approx(shift[2])]
        # The unselected item stays printable=0 but is parked past the bed in +X: real Orca 2.4.2
        # rejects the plate (-52) when a non-printable item overlaps the bed volume. Y/Z are untouched.
        parked = [float(value) for value in items[0].get('transform').split()]
        assert parked[:9] == [1, 0, 0, 0, 1, 0, 0, 0, 1] and parked[10:] == [30, 0]
        assert parked[9] >= 220 + 50
        with zipfile.ZipFile(source) as original:
            for name in ('3D/Objects/object_1.model', '3D/Objects/object_2.model', '3D/_rels/3dmodel.model.rels'):
                assert archive.read(name) == original.read(name)
            assert sorted(archive.namelist()) == sorted(original.namelist())
        components = [node for node in root.iter() if node.tag.split('}')[-1] == 'component']
        assert len(components) == 2
        assert set(re.findall(rb'key="extruder" value="(\d+)"', archive.read('Metadata/model_settings.config'))) == {b'1'}
    assert report['nonprintable_object_ids'] == ['1']
    assert report['logical_filament'] == 1
    with pytest.raises(RuntimeError, match='mixes physical slots'):
        other = dict(selection, slot=1)
        mixed = dict(decisions, choices=dict(decisions['choices'], **{'part:1:1': other}))
        export_single_color_plate(source, tmp_path / 'mixed.3mf', mixed, ['1:1', '2:1'])
    assert not (tmp_path / 'mixed.3mf').exists()
    # Legacy schema carries palette rows (only for multi-filament sources); a
    # part that contradicts its palette row must not produce a plate.
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(tmp_path / 'two.3mf', 'w') as two:
        for name in original.namelist():
            data = original.read(name)
            if name == 'Metadata/project_settings.config':
                data = json.dumps({'filament_colour': ['#FFFFFF', '#067749'], 'filament_type': ['PLA', 'PLA']}).encode()
            if name == 'Metadata/model_settings.config':
                data = data.replace(b'value="Stencil"/><metadata key="extruder" value="1"', b'value="Stencil"/><metadata key="extruder" value="2"')
            two.writestr(name, data)
    source = tmp_path / 'two.3mf'
    manifest = source_colors.inventory(source)
    legacy = {'mode': 'per_part', 'source_sha256': manifest['source_sha256'],
              'choices': {row['key']: selection for row in source_colors.prompts(manifest)}}
    assert any(key.startswith('filament:') for key in legacy['choices'])
    stencil = next(part for part in manifest['parts'] if part['object_id'] == '2')
    legacy['choices']['part:' + stencil['key']] = dict(selection, slot=1)
    with pytest.raises(RuntimeError, match='contradicts its source filament'):
        export_single_color_plate(source, tmp_path / 'contradiction.3mf', legacy, [stencil['key']])
    assert not (tmp_path / 'contradiction.3mf').exists()


def _project_settings(colours):
    body = ',\n'.join(f'    "{colour}"' for colour in colours)
    return '{\n  "filament_colour": [\n' + body + '\n  ],\n  "filament_type": ["PLA"]\n}\n'


def _two_slot_source(path):
    holder = [(0, 0, 0), (40, 0, 0), (0, 20, 10)]
    stencil = [(0, 0, 0), (30, 0, 0), (0, 20, 5)]
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('3D/Objects/object_1.model', _mesh('object_1', holder))
        archive.writestr('3D/Objects/object_2.model', _mesh('object_2', stencil))
        archive.writestr('3D/3dmodel.model', '''<model xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" unit="millimeter"><resources>
          <object id="1" name="Holder"><components><component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>
          <object id="2" name="Stencil"><components><component p:path="/3D/Objects/object_2.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>
        </resources><build>
          <item objectid="1" transform="1 0 0 0 1 0 0 0 1 20 30 0"/>
          <item objectid="2" transform="1 0 0 0 1 0 0 0 1 40 50 0"/>
        </build></model>''')
        archive.writestr('Metadata/model_settings.config', '''<config>
          <object id="1"><metadata key="name" value="Holder"/><metadata key="extruder" value="1"/><part id="1"><metadata key="name" value="Holder"/><metadata key="extruder" value="1"/></part></object>
          <object id="2"><metadata key="name" value="Stencil"/><metadata key="extruder" value="2"/><part id="1"><metadata key="name" value="Stencil"/><metadata key="extruder" value="2"/></part></object>
          <plate><metadata key="plater_id" value="1"/><model_instance><metadata key="object_id" value="1"/></model_instance><model_instance><metadata key="object_id" value="2"/></model_instance></plate>
        </config>''')
        archive.writestr('Metadata/project_settings.config', _project_settings(['#FF00FF', '#067749']))
        archive.writestr('3D/_rels/3dmodel.model.rels', '''<Relationships>
 <Relationship Target="/3D/Objects/object_1.model" Id="rel-1"/>
 <Relationship Target="/3D/Objects/object_2.model" Id="rel-2"/>
</Relationships>''')


def test_derived_plate_uses_its_own_slot_not_the_loaded_spool(tmp_path):
    source = tmp_path / 'source.3mf'
    _two_slot_source(source)
    manifest = source_colors.inventory(source)
    state = json.loads(STATE.read_text())
    state['spool'] = dict(state['filament_slots']['4'])
    assert state['filament_slots']['4']['hex'].lstrip('#').upper() == '067749'
    assert state['spool']['spoolman_id'] == state['filament_slots']['4']['spoolman_id']
    white = model_job.resolve_filament_selection(state, dict(state['filament_slots']['1'], slot=1))
    decisions = {'schema': 'part_slots_v1', 'mode': 'per_part', 'source_sha256': manifest['source_sha256'],
                 'choices': {row['key']: white for row in source_colors.canonical_prompts(manifest)}}
    report = export_single_color_plate(source, tmp_path / 'white.3mf', decisions, ['1:1'], slot_colors=state['filament_slots'])
    assert report['slot'] == 1
    assert report['filament_colour'] == '#FFFFFF'
    with zipfile.ZipFile(tmp_path / 'white.3mf') as archive:
        colours = json.loads(archive.read('Metadata/project_settings.config'))['filament_colour']
        settings = archive.read('Metadata/model_settings.config')
        model = archive.read('3D/3dmodel.model')
        assert colours[0] == '#FFFFFF'
        assert '#FF00FF' not in colours[0]
        assert '#067749' not in colours[0]
        assert set(re.findall(rb'key="extruder" value="(\d+)"', settings)) == {b'1'}
        # The other-slot stencil is still present but excluded from the plate.
        assert b'object_2.model' in model
        assert '3D/Objects/object_2.model' in archive.namelist()
        assert re.search(rb'<item objectid="2"[^>]*printable="0"', model)
        assert re.search(rb'<item objectid="1"[^>]*printable="1"', model)


def test_slice_job_emits_both_slots_without_calling_the_printer(tmp_path, monkeypatch):
    source = tmp_path / 'source.3mf'
    _two_slot_source(source)
    state = json.loads(STATE.read_text())
    manifest = source_colors.inventory(source)
    white = model_job.resolve_filament_selection(state, dict(state['filament_slots']['1'], slot=1))
    green = model_job.resolve_filament_selection(state, dict(state['filament_slots']['4'], slot=4))
    by_extruder = {1: white, 2: green}
    choices = {}
    for row in source_colors.canonical_prompts(manifest):
        if row['key'].startswith('part:'):
            part = next(part for part in manifest['parts'] if part['key'] == row['key'][5:])
            choices[row['key']] = by_extruder[part['source_extruder']]
        else:
            choices[row['key']] = by_extruder[int(row['key'].split(':')[1])]
    job_path = tmp_path / 'JOB.json'
    job_path.write_text(json.dumps({'downloaded_file': str(source), 'audit': {'plates': 1}, 'plate_mode': 'separate', 'color_decisions': {
        'schema': 'part_slots_v1', 'mode': 'per_part', 'source_sha256': manifest['source_sha256'], 'choices': choices}}))

    def fake_slice(source, folder, state, brim=None, first_layer=False, color_decisions=None, preserve_layout=False, filament_slot=None):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        slot = state['filament_slots'][str(filament_slot)]
        out = folder / f'slot{filament_slot}.gcode'
        out.write_text(f'; filament_colour = {slot["hex"]}\n; nozzle_diameter = 0.4\nM190 S{state["temps_c"]["bed"]}\nM104 S{state["temps_c"]["nozzle"]} T0\nSTART_PRINT BED_TEMP={state["temps_c"]["bed"]} EXTRUDER_TEMP={state["temps_c"]["nozzle"]}\nG1 X10 Y10 Z0.2\nEND_PRINT\n')
        return out, {'slot': filament_slot}, []

    monkeypatch.setattr(model_job.printer_state, 'check', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('slice contacted the printer')))
    monkeypatch.setattr('print_handy.core.orca_adapter.slice_model', fake_slice)
    paths = model_job.slice_job(job_path)
    saved = json.loads(job_path.read_text())
    assert len(paths) == 2
    assert [row['slot'] for row in saved['derived_plates']] == [1, 4]
    assert 'refused_plates' not in saved
    assert 'FFFFFF' in Path(paths[0]).read_text()
    assert '067749' not in Path(paths[0]).read_text()
    assert '067749' in Path(paths[1]).read_text()
    # Guard against a regression that binds every derived plate to the same IFS slot.
    def physical_slots(path):
        line = Path(path).read_text().split('; hermes_tool_slots = ', 1)[1].split('\n', 1)[0]
        return {tool: row['physical_slot'] for tool, row in json.loads(line).items()}
    assert physical_slots(paths[0]) == {'0': 1}
    assert physical_slots(paths[1]) == {'0': 4}
    assert saved['derived_plates'][0]['plate_report']['filament_colour'] == '#FFFFFF'


def test_multiline_objects_keep_their_own_component(tmp_path):
    source = tmp_path / 'source.3mf'
    holder = [(0, 0, 0), (40, 0, 0), (0, 20, 10)]
    stencil = [(0, 0, 0), (30, 0, 0), (0, 20, 5)]
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('3D/Objects/object_1.model', _mesh('object_1', holder))
        archive.writestr('3D/Objects/object_2.model', _mesh('object_2', stencil))
        archive.writestr('3D/3dmodel.model', '''<model xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" unit="millimeter">
 <resources>
  <object id="1" name="Holder">
   <components>
    <component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0" />
   </components>
  </object>
  <object id="2" name="Stencil">
   <components>
    <component p:path="/3D/Objects/object_2.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0" />
   </components>
  </object>
 </resources>
 <build>
  <item objectid="1" transform="1 0 0 0 1 0 0 0 1 20 30 0" />
  <item objectid="2" transform="1 0 0 0 1 0 0 0 1 40 50 0" />
 </build>
</model>''')
        archive.writestr('Metadata/model_settings.config', '''<config>
          <object id="1"><metadata key="name" value="Holder"/><metadata key="extruder" value="1"/><part id="1"><metadata key="name" value="Holder"/><metadata key="extruder" value="1"/></part></object>
          <object id="2"><metadata key="name" value="Stencil"/><metadata key="extruder" value="1"/><part id="1"><metadata key="name" value="Stencil"/><metadata key="extruder" value="1"/></part></object>
          <plate><metadata key="plater_id" value="1"/><model_instance><metadata key="object_id" value="1"/></model_instance><model_instance><metadata key="object_id" value="2"/></model_instance></plate>
        </config>''')
        archive.writestr('Metadata/project_settings.config', json.dumps({'filament_colour': ['#FFFFFF'], 'filament_type': ['PLA']}))
        archive.writestr('3D/_rels/3dmodel.model.rels', '''<Relationships>
 <Relationship Target="/3D/Objects/object_1.model" Id="rel-1"/>
 <Relationship Target="/3D/Objects/object_2.model" Id="rel-2"/>
</Relationships>''')
    manifest = source_colors.inventory(source)
    state = json.loads(STATE.read_text())
    slot = dict(state['filament_slots']['4'], slot=4)
    selection = model_job.resolve_filament_selection(state, slot)
    decisions = {'schema': 'part_slots_v1', 'mode': 'per_part', 'source_sha256': manifest['source_sha256'],
                 'choices': {row['key']: selection for row in source_colors.canonical_prompts(manifest)}}
    report = export_single_color_plate(source, tmp_path / 'stencil.3mf', decisions, ['2:1'])
    with zipfile.ZipFile(tmp_path / 'stencil.3mf') as archive:
        root = ET.fromstring(archive.read('3D/3dmodel.model'))
        objects = [node for node in root.iter() if node.tag.split('}')[-1] == 'object']
        items = [node for node in root.iter() if node.tag.split('}')[-1] == 'item']
        assert [node.get('id') for node in objects] == ['1', '2']
        assert [(item.get('objectid'), item.get('printable')) for item in items] == [('1', '0'), ('2', '1')]
        # Pretty-printed source survives byte for byte outside the build items.
        with zipfile.ZipFile(source) as original:
            strip = lambda data: re.sub(rb'<item\b[^>]*>', b'', data)
            assert strip(archive.read('3D/3dmodel.model')) == strip(original.read('3D/3dmodel.model'))
        assert report['object_ids'] == ['2']


def test_selected_object_with_foreign_part_is_refused(tmp_path):
    source = tmp_path / 'source.3mf'
    _two_slot_source(source)
    manifest = source_colors.inventory(source)
    state = json.loads(STATE.read_text())
    white = model_job.resolve_filament_selection(state, dict(state['filament_slots']['1'], slot=1))
    decisions = {'schema': 'part_slots_v1', 'mode': 'per_part', 'source_sha256': manifest['source_sha256'],
                 'choices': {row['key']: white for row in source_colors.canonical_prompts(manifest)}}
    with pytest.raises(RuntimeError, match='do not match'):
        export_single_color_plate(source, tmp_path / 'bad.3mf', decisions, ['9:9'])
    assert not (tmp_path / 'bad.3mf').exists()


def _two_slot_job(tmp_path, **extra):
    source = tmp_path / 'source.3mf'
    _two_slot_source(source)
    state = json.loads(STATE.read_text())
    manifest = source_colors.inventory(source)
    white = model_job.resolve_filament_selection(state, dict(state['filament_slots']['1'], slot=1))
    green = model_job.resolve_filament_selection(state, dict(state['filament_slots']['4'], slot=4))
    by_extruder = {1: white, 2: green}
    choices = {}
    for row in source_colors.canonical_prompts(manifest):
        if row['key'].startswith('part:'):
            part = next(part for part in manifest['parts'] if part['key'] == row['key'][5:])
            choices[row['key']] = by_extruder[part['source_extruder']]
        else:
            choices[row['key']] = by_extruder[int(row['key'].split(':')[1])]
    decisions = {'schema': 'part_slots_v1', 'mode': 'per_part', 'source_sha256': manifest['source_sha256'], 'choices': choices}
    job_path = tmp_path / 'JOB.json'
    job_path.write_text(json.dumps({'downloaded_file': str(source), 'audit': {'plates': 1}, 'color_decisions': decisions, **extra}))
    return source, state, decisions, job_path


def test_compose_matches_sequential_application():
    from print_handy.core.color_geometry import _apply, _compose
    item = [0, 1, 0, -1, 0, 0, 0, 0, 1, 100, 5, 0]
    component = [2, 0, 0, 0, 1, 0, 0, 0, 1, 10, 0, 3]
    for point in [(1, 2, 3), (0, 0, 0), (-4, 7, 1)]:
        assert _apply(_compose(item, component), point) == pytest.approx(_apply(item, _apply(component, point)))


def test_fit_transform_matches_orca_rotation_and_scale():
    from print_handy.core.color_geometry import _apply, fit_transform
    from print_handy.core.orca_adapter import _rotate, _yaw
    assert fit_transform(None) is None
    assert fit_transform({'scale': 1.0, 'rotation': None}) is None
    rotation = _yaw(90)
    fit = fit_transform({'scale': 0.5, 'rotation': rotation})
    for point in [(1, 2, 3), (10, -4, 0)]:
        assert _apply(fit, point) == pytest.approx(tuple(0.5 * v for v in _rotate(point, rotation)))
    # Above 100% is an operator-chosen size too (the confirm prompt's Scale
    # the model button): the same rigid transform applies, only larger.
    grown = fit_transform({'scale': 1.5, 'rotation': rotation})
    for point in [(1, 2, 3), (10, -4, 0)]:
        assert _apply(grown, point) == pytest.approx(tuple(1.5 * v for v in _rotate(point, rotation)))
    with pytest.raises(RuntimeError, match='scale'):
        fit_transform({'scale': -1})


def test_resized_plate_composes_fit_into_selected_items_only(tmp_path):
    source, state, decisions, _ = _two_slot_job(tmp_path)
    from print_handy.core.orca_adapter import _yaw
    from print_handy.core.color_geometry import _compose, fit_transform
    before = source.read_bytes()
    fit = {'scale': 0.5, 'rotation': _yaw(90)}
    report = export_single_color_plate(source, tmp_path / 'white.3mf', decisions, ['1:1'], slot_colors=state['filament_slots'], fit=fit)
    assert source.read_bytes() == before
    assert report['fit']['scale'] == 0.5
    # Holder is 40 x 20 (XY): yawed 90 degrees and halved it is 10 x 20.
    assert [hi - lo for lo, hi in report['placed_bounds_mm']] == pytest.approx([10, 20])
    with zipfile.ZipFile(tmp_path / 'white.3mf') as archive, zipfile.ZipFile(source) as original:
        model = archive.read('3D/3dmodel.model').decode()
        for name in ('3D/Objects/object_1.model', '3D/Objects/object_2.model'):
            assert archive.read(name) == original.read(name)
    transforms = dict(re.findall(r'<item objectid="(\d)" transform="([^"]+)"', model))
    expected = _compose(fit_transform(fit), [1, 0, 0, 0, 1, 0, 0, 0, 1, 20, 30, 0])
    got = [float(v) for v in transforms['1'].split()]
    assert got[:9] == pytest.approx(expected[:9])
    assert [got[9 + i] - expected[9 + i] for i in range(3)] == pytest.approx(report['translation_mm'])
    # The unselected item is parked, never resized.
    assert [float(v) for v in transforms['2'].split()][:9] == [1, 0, 0, 0, 1, 0, 0, 0, 1]
    with pytest.raises(RuntimeError, match='merge shifts'):
        export_single_color_plate(source, tmp_path / 'x.3mf', decisions, ['1:1'], fit=fit, item_shifts={'1': [0, 0, 0]})


def test_resized_layout_that_lifts_a_part_is_refused(tmp_path):
    source, state, decisions, _ = _two_slot_job(tmp_path)
    for part in source_colors.inventory(source)['parts']:
        decisions['choices']['part:' + part['key']] = decisions['choices']['part:1:1']
    for key in list(decisions['choices']):
        if key.startswith('filament:'):
            decisions['choices'][key] = decisions['choices']['part:1:1']
    # Tipping the authored side-by-side layout 90 degrees about X stands the
    # 20 mm Y spread up on end: the farther part floats above the bed.
    tip = [1, 0, 0, 0, 0, -1, 0, 1, 0]
    with pytest.raises(RuntimeError, match='off the bed'):
        export_single_color_plate(source, tmp_path / 'tip.3mf', decisions, ['1:1', '2:1'], fit={'scale': 1.0, 'rotation': tip})
    assert not (tmp_path / 'tip.3mf').exists()
    # The same tip on one part alone is fine.
    export_single_color_plate(source, tmp_path / 'one.3mf', decisions, ['1:1'], fit={'scale': 1.0, 'rotation': tip})


def test_slice_job_applies_chosen_fit_to_every_slot_plate(tmp_path, monkeypatch):
    from print_handy.core.orca_adapter import _yaw
    rotation = _yaw(90)
    _, _, _, job_path = _two_slot_job(tmp_path, scale=0.6, orientation={'rotation': rotation, 'supports': True, 'brim': True})
    calls = []

    def fake_slice(source, folder, state, brim=None, first_layer=False, color_decisions=None, preserve_layout=False, filament_slot=None, supports=None, **kwargs):
        assert not kwargs, 'derived plates are pre-fitted; Orca must not rescale them again'
        calls.append({'slot': filament_slot, 'supports': supports, 'brim': brim})
        folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
        slot = state['filament_slots'][str(filament_slot)]
        out = folder / f'slot{filament_slot}.gcode'
        out.write_text(f'; filament_colour = {slot["hex"]}\n; nozzle_diameter = 0.4\nM190 S{state["temps_c"]["bed"]}\nM104 S{state["temps_c"]["nozzle"]} T0\nSTART_PRINT BED_TEMP={state["temps_c"]["bed"]} EXTRUDER_TEMP={state["temps_c"]["nozzle"]}\nG1 X10 Y10 Z0.2\nEND_PRINT\n')
        return out, {'slot': filament_slot}, []

    monkeypatch.setattr(model_job.printer_state, 'check', lambda *a, **k: (_ for _ in ()).throw(AssertionError('slice contacted the printer')))
    monkeypatch.setattr('print_handy.core.orca_adapter.slice_model', fake_slice)
    paths = model_job.slice_job(job_path)
    saved = json.loads(job_path.read_text())
    assert len(paths) == 2
    assert [c['slot'] for c in calls] == [1, 4]
    assert all(c['supports'] is True and c['brim'] == 5 for c in calls)
    for row in saved['derived_plates']:
        assert row['plate_report']['fit']['scale'] == 0.6
        assert row['plate_report']['fit']['rotation'] == rotation


def test_per_part_fit_options_drops_choices_a_slot_plate_cannot_honor(tmp_path):
    source, state, decisions, _ = _two_slot_job(tmp_path)
    upright = {'label': 'Upright', 'scale': 0.9, 'rotation': [1, 0, 0, 0, 1, 0, 0, 0, 1]}
    impossible = {'label': 'Mirror', 'scale': 0.9, 'rotation': [-1, 0, 0, 0, 1, 0, 0, 0, 1]}
    options = {'scales': [0.9, 0.5], 'rotations': [upright, impossible]}
    kept = model_job.per_part_fit_options(source, decisions, options, bed=220)
    assert kept['scales'] == [0.9, 0.5]
    assert kept['rotations'] == [upright]


def test_select_build_items_treats_a_missing_transform_as_identity():
    # P2-282: direct-mesh core build items may carry no transform attribute
    # at all (the checked-in core-colorgroup-items case). Parking an
    # unselected one crashed on the missing attribute, and shifting a
    # selected one raised 'selected build item has no transform'; a missing
    # transform is the identity, so both edits write one in.
    from print_handy.core.color_geometry import _select_build_items
    model = b'<model><build><item objectid="2"/><item objectid="3"/></build></model>'
    out, kept = _select_build_items(model, {'3'}, {'3': [5.0, 0.0, 0.0]}, park={'2': 300.0})
    assert kept == ['3']
    items = {m.group(1).decode(): m.group(0) for m in re.finditer(br'<item\b[^>]*objectid="(\d+)"[^>]*>', out)}
    parked = [float(v) for v in re.search(br'transform="([^"]+)"', items['2']).group(1).split()]
    assert parked[:9] == [1, 0, 0, 0, 1, 0, 0, 0, 1] and parked[9:] == [300.0, 0, 0]
    assert b'printable="0"' in items['2']
    shifted = [float(v) for v in re.search(br'transform="([^"]+)"', items['3']).group(1).split()]
    assert shifted[:9] == [1, 0, 0, 0, 1, 0, 0, 0, 1] and shifted[9:] == [5.0, 0, 0]
    assert b'printable="1"' in items['3']


def test_single_color_export_of_a_direct_mesh_core_part_parks_the_rest(tmp_path):
    # P2-282 end to end: exporting one part of the checked-in direct-mesh
    # core file parks the other two; none of its build items has a
    # transform attribute.
    from print_handy.core.color_geometry import export_single_color_plate
    source = Path(__file__).parents[1] / 'corpus/cases/core-colorgroup-items/model.3mf'
    manifest = source_colors.inventory(source)
    state = json.loads(STATE.read_text())
    selection = model_job.resolve_filament_selection(state, dict(state['filament_slots']['1'], slot=1))
    decisions = {'schema': 'part_slots_v1', 'mode': 'per_part', 'source_sha256': manifest['source_sha256'],
                 'choices': {row['key']: selection for row in source_colors.canonical_prompts(manifest)}}
    keys = [part['key'] for part in manifest['parts']]
    report = export_single_color_plate(source, tmp_path / 'one.3mf', decisions, [keys[1]])
    assert report['part_keys'] == [keys[1]]
    with zipfile.ZipFile(tmp_path / 'one.3mf') as archive:
        root = ET.fromstring(archive.read('3D/3dmodel.model'))
        items = [node for node in root.iter() if node.tag.split('}')[-1] == 'item']
        assert [item.get('printable') for item in items] == ['0', '1', '0']
        # The parked items gained an identity-based transform carrying the
        # park move (they had none); the selected item needed no shift, so
        # it keeps its authored (absent) transform.
        for item in (items[0], items[2]):
            values = [float(v) for v in item.get('transform').split()]
            assert len(values) == 12
        parked = [float(v) for v in items[0].get('transform').split()]
        # Left's mesh starts at x=40, so its parked bounds start at 270:
        # past the 220 bed plus the same 50 mm clearance as any other export.
        assert parked[9] + 40 >= 220 + 50
# ---------------------------------------------------------------------- P2-85

def _box(dx, dy, dz, offset=(0.0, 0.0, 0.0)):
    ox, oy, oz = offset
    v = [(ox, oy, oz), (ox + dx, oy, oz), (ox + dx, oy + dy, oz), (ox, oy + dy, oz),
         (ox, oy, oz + dz), (ox + dx, oy, oz + dz), (ox + dx, oy + dy, oz + dz), (ox, oy + dy, oz + dz)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (2, 3, 7), (2, 7, 6), (1, 2, 6), (1, 6, 5), (3, 0, 4), (3, 4, 7)]
    return [[v[a], v[b], v[c]] for a, b, c in faces]


def _mesh_xml(triangles):
    verts, index, tris = [], {}, []
    for tri in triangles:
        row = []
        for pt in tri:
            key = tuple(round(float(c), 6) for c in pt)
            if key not in index:
                index[key] = len(verts)
                verts.append(key)
            row.append(index[key])
        tris.append(row)
    vxml = ''.join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in verts)
    txml = ''.join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    return f'<mesh><vertices>{vxml}</vertices><triangles>{txml}</triangles></mesh>'


def _cutter_source(path):
    """One object: a 40x40x10 body plus a negative-volume cutter poking 2 mm above it."""
    settings_xml = ('<?xml version="1.0" encoding="UTF-8"?><config><object id="1">'
                    '<part id="2" subtype="normal_part"/><part id="3" subtype="negative_volume"/>'
                    '</object></config>')
    model = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
             f'<object id="2" name="main" type="model">{_mesh_xml(_box(40, 40, 10))}</object>'
             f'<object id="3" name="cutter" type="model">{_mesh_xml(_box(10, 10, 12, (5, 5, 0)))}</object>'
             '<object id="1" name="assembly" type="model"><components>'
             '<component objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
             '<component objectid="3" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
             '</components></object>'
             '</resources><build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0" printable="1"/></build></model>')
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr('3D/3dmodel.model', model)
        z.writestr('Metadata/model_settings.config', settings_xml)
    return path


def test_placed_bounds_without_cutters_are_the_part_rows():
    geometry = {'parts': [{'part_key': 'a', 'bounds_mm': [[0, 10], [0, 10], [0, 5]]}]}
    assert model_job._placed_bounds_by_object(geometry, {'a': '1'}) == {'1': [[0, 10], [0, 10], [0, 5]]}


def test_export_records_cutter_bounds_so_the_proof_compares_matching_volumes(tmp_path):
    """The merged-placement proof compares the exporter's landed bounds
    against bounds computed from ALL authored volumes. The report's part
    rows exclude cutters, so a cutter reaching past its body (z 12 over a
    10 mm body) read as a misplaced export and refused a sound slice."""
    from print_handy.core.color_geometry import _world_points
    from .test_plate_merge import _two_slot_decisions

    src = _cutter_source(tmp_path / 'cutter.3mf')
    manifest = source_colors.inventory(src)
    assert manifest['cutters'], 'the fixture must really carry a cutter'
    _mapping, report = export_color_geometry(src, tmp_path / 'out.3mf', _two_slot_decisions(manifest))
    [cutter_box] = report['cutter_bounds_by_object'].values()
    assert cutter_box[2][1] > 10  # the cutter reaches past the 10 mm body
    object_of_key = {p['key']: str(p['object_id']) for p in manifest['parts']}
    placed = model_job._placed_bounds_by_object(report, object_of_key)
    oid = str(manifest['parts'][0]['object_id'])
    with zipfile.ZipFile(src) as archive:
        points = _world_points(archive, {oid})
    delta = report['translation_mm']
    for a in range(3):
        assert placed[oid][a][0] == pytest.approx(min(p[a] for p in points) + delta[a])
        assert placed[oid][a][1] == pytest.approx(max(p[a] for p in points) + delta[a])


# ---------------------------------------------------------------------- #814

CORE_NS = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'


def _prefixed_assembly(path):
    """One ns0-prefixed core 3MF: build item -> object 1 -> component -> mesh object 2 (no default xmlns)."""
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<ns0:model xmlns:ns0="{CORE_NS}" unit="millimeter"><ns0:resources>'
        '<ns0:object id="2" type="model"><ns0:mesh><ns0:vertices>'
        '<ns0:vertex x="0" y="0" z="0"/><ns0:vertex x="10" y="0" z="0"/>'
        '<ns0:vertex x="0" y="10" z="0"/><ns0:vertex x="0" y="0" z="10"/></ns0:vertices>'
        '<ns0:triangles><ns0:triangle v1="0" v2="2" v3="1"/><ns0:triangle v1="0" v2="1" v3="3"/>'
        '<ns0:triangle v1="1" v2="2" v3="3"/><ns0:triangle v1="2" v2="0" v3="3"/></ns0:triangles>'
        '</ns0:mesh></ns0:object>'
        '<ns0:object id="1" type="model"><ns0:components><ns0:component objectid="2"/></ns0:components></ns0:object>'
        '</ns0:resources><ns0:build>'
        '<ns0:item objectid="1" transform="1 0 0 0 1 0 0 0 1 50 60 0"/></ns0:build></ns0:model>')
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr('3D/3dmodel.model', model)
    return path


def test_flattened_prefixed_core_assembly_keeps_build_items_in_the_core_namespace(tmp_path):
    from print_handy.core.color_geometry import _flatten_core_assemblies
    flat = _flatten_core_assemblies(_prefixed_assembly(tmp_path / 'ns0.3mf'))
    with zipfile.ZipFile(flat) as archive:
        root = ET.fromstring(archive.read('3D/3dmodel.model'))
    ns = {'c': CORE_NS}
    items = root.findall('c:build/c:item', ns)
    assert [item.get('objectid') for item in items] == ['2']
    assert [float(v) for v in items[0].get('transform').split()][9:] == [50.0, 60.0, 0.0]
    objects = {node.get('id') for node in root.findall('c:resources/c:object', ns)}
    assert {item.get('objectid') for item in items} <= objects
    # Nothing was left outside the namespace: no un-namespaced item anywhere.
    assert not [node for node in root.iter('item')]


def test_build_item_edits_see_prefixed_core_items(tmp_path):
    from print_handy.core.color_geometry import _build_ids, _select_build_items
    with zipfile.ZipFile(_prefixed_assembly(tmp_path / 'ns0.3mf')) as archive:
        model = archive.read('3D/3dmodel.model')
    assert _build_ids(model) == {'1'}
    out, kept = _select_build_items(model, {'1'}, [5.0, 0.0, 0.0], None, None)
    assert kept == ['1']
    item = ET.fromstring(out).find(f'{{{CORE_NS}}}build/{{{CORE_NS}}}item')
    assert item.get('printable') == '1'
    assert [float(v) for v in item.get('transform').split()][9:] == [55.0, 60.0, 0.0]


def test_default_core_namespace_gives_the_slicer_raw_element_names(tmp_path):
    from print_handy.core.orca_adapter import default_core_namespace, normalize_3mf_core_namespace
    path = _prefixed_assembly(tmp_path / 'ns0.3mf')
    with zipfile.ZipFile(path) as archive:
        original = archive.read('3D/3dmodel.model')
    fixed = default_core_namespace(original)
    assert fixed.startswith(b'<?xml version="1.0" encoding="UTF-8"?><model xmlns="' + CORE_NS.encode() + b'"')
    assert b'ns0' not in fixed
    root = ET.fromstring(fixed)
    ns = {'c': CORE_NS}
    objects = {node.get('id') for node in root.findall('c:resources/c:object', ns)}
    assert objects == {'1', '2'}
    assert [i.get('objectid') for i in root.findall('c:build/c:item', ns)] == ['1']
    # The archive helper rewrites the copy it is given and reports it.
    assert normalize_3mf_core_namespace(path) is True
    with zipfile.ZipFile(path) as archive:
        assert archive.read('3D/3dmodel.model') == fixed
    assert normalize_3mf_core_namespace(path) is False


def test_default_core_namespace_keeps_other_prefixes_and_default_files_byte_identical():
    from print_handy.core.orca_adapter import default_core_namespace
    prefixed = (f'<ns0:model xmlns:ns0="{CORE_NS}" xmlns:p="http://schemas.microsoft.com/3dmanufacturing/'
                'production/2015/06"><ns0:build><ns0:item objectid="1" p:path="/3D/o.model"/></ns0:build></ns0:model>').encode()
    fixed = default_core_namespace(prefixed)
    assert b'<model xmlns="' in fixed and b'xmlns:p="' in fixed and b'p:path="/3D/o.model"' in fixed
    assert b'ns0' not in fixed and b'<build><item objectid="1"' in fixed
    plain = f'<model xmlns="{CORE_NS}"><build><item objectid="1"/></build></model>'.encode()
    assert default_core_namespace(plain) is plain
    # A prefix bound to something else, beside a default core namespace, is not touched.
    other = f'<model xmlns="{CORE_NS}" xmlns:x="urn:x"><x:a/></model>'.encode()
    assert default_core_namespace(other) == other


def _painted_two_plate_source(path):
    """Two objects on two authored plates; the Holder is partly painted filament 2.

    Painted part_slots_v1 decisions are keyed by palette row ('filament:N')
    only, never per part: the shape of the 'Estanteria Hello' corpus model.
    """
    holder = _mesh('object_1', [(0, 0, 0), (40, 0, 0), (0, 20, 10)]).replace(
        '<triangle v1="0" v2="1" v3="2"/>',
        '<triangle v1="0" v2="1" v3="2" paint_color="8"/><triangle v1="0" v2="2" v3="1"/>')
    stencil = _mesh('object_2', [(0, 0, 0), (30, 0, 0), (0, 20, 5)])
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('3D/Objects/object_1.model', holder)
        archive.writestr('3D/Objects/object_2.model', stencil)
        archive.writestr('3D/3dmodel.model', '''<model xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" unit="millimeter"><resources>
          <object id="1" name="Holder"><components><component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>
          <object id="2" name="Stencil"><components><component p:path="/3D/Objects/object_2.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>
        </resources><build>
          <item objectid="1" transform="1 0 0 0 1 0 0 0 1 20 30 0"/>
          <item objectid="2" transform="1 0 0 0 1 0 0 0 1 40 50 0"/>
        </build></model>''')
        archive.writestr('Metadata/model_settings.config', '''<config>
          <object id="1"><metadata key="name" value="Holder"/><metadata key="extruder" value="1"/><part id="1"><metadata key="name" value="Holder"/><metadata key="extruder" value="1"/></part></object>
          <object id="2"><metadata key="name" value="Stencil"/><metadata key="extruder" value="1"/><part id="1"><metadata key="name" value="Stencil"/><metadata key="extruder" value="1"/></part></object>
          <plate><metadata key="plater_id" value="1"/><model_instance><metadata key="object_id" value="1"/></model_instance></plate>
          <plate><metadata key="plater_id" value="2"/><model_instance><metadata key="object_id" value="2"/></model_instance></plate>
        </config>''')
        archive.writestr('Metadata/project_settings.config', _project_settings(['#FF00FF', '#067749']))
        archive.writestr('3D/_rels/3dmodel.model.rels', '''<Relationships>
 <Relationship Target="/3D/Objects/object_1.model" Id="rel-1"/>
 <Relationship Target="/3D/Objects/object_2.model" Id="rel-2"/>
</Relationships>''')


def test_painted_one_color_view_exports_each_part_plate(tmp_path):
    source = tmp_path / 'source.3mf'
    _painted_two_plate_source(source)
    manifest = source_colors.inventory(source)
    assert manifest['painted']
    assert {row['key'] for row in source_colors.canonical_prompts(manifest)} == {'filament:1', 'filament:2'}
    state = json.loads(STATE.read_text())
    white = model_job.resolve_filament_selection(state, dict(state['filament_slots']['1'], slot=1))
    one_color = {'schema': 'part_slots_v1', 'mode': 'single_color', 'source_sha256': manifest['source_sha256'],
                 'selection': white}
    view = model_job._per_part_view(manifest, one_color)
    for key in ('1:1', '2:1'):
        # Before the fix this raised KeyError: 'part:1:1' (painted decisions have no part rows).
        target = tmp_path / f'plate{key[0]}.3mf'
        report = export_single_color_plate(source, target, view, [key], slot_colors=state['filament_slots'])
        assert report['slot'] == 1 and report['part_keys'] == [key]
        with zipfile.ZipFile(target) as archive:
            # One logical filament: no paint code may still name source filament 2.
            assert not any(b'paint_color' in archive.read(n) for n in archive.namelist() if n.endswith('.model'))
            settings = archive.read('Metadata/model_settings.config')
            assert set(re.findall(rb'key="extruder" value="(\d+)"', settings)) == {b'1'}


def test_painted_part_plate_refuses_paint_of_another_slot(tmp_path):
    source = tmp_path / 'source.3mf'
    _painted_two_plate_source(source)
    manifest = source_colors.inventory(source)
    state = json.loads(STATE.read_text())
    white = model_job.resolve_filament_selection(state, dict(state['filament_slots']['1'], slot=1))
    green = model_job.resolve_filament_selection(state, dict(state['filament_slots']['4'], slot=4))
    view = {'schema': 'part_slots_v1', 'mode': 'per_part', 'source_sha256': manifest['source_sha256'],
            'choices': {'filament:1': white, 'filament:2': green}}
    # The Holder's painted filament-2 region is slot 4, its body slot 1.
    with pytest.raises(RuntimeError, match='mixes physical slots'):
        export_single_color_plate(source, tmp_path / 'holder.3mf', view, ['1:1'])
    assert not (tmp_path / 'holder.3mf').exists()
    # The unpainted Stencil prints only its own filament 1 row.
    assert export_single_color_plate(source, tmp_path / 'stencil.3mf', view, ['2:1'])['slot'] == 1


def test_slice_job_splits_a_one_color_painted_model_per_plate(tmp_path, monkeypatch):
    source = tmp_path / 'source.3mf'
    _painted_two_plate_source(source)
    manifest = source_colors.inventory(source)
    state = json.loads(STATE.read_text())
    white = model_job.resolve_filament_selection(state, dict(state['filament_slots']['1'], slot=1))
    job_path = tmp_path / 'JOB.json'
    job_path.write_text(json.dumps({'downloaded_file': str(source), 'audit': {'plates': 2}, 'plate_mode': 'separate',
                                    'color_decisions': {'schema': 'part_slots_v1', 'mode': 'single_color',
                                                        'source_sha256': manifest['source_sha256'],
                                                        'selection': white}}))

    def fake_slice(source, folder, state, brim=None, first_layer=False, color_decisions=None, preserve_layout=False,
                   filament_slot=None, **kwargs):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        slot = state['filament_slots'][str(filament_slot)]
        out = folder / f'slot{filament_slot}.gcode'
        out.write_text(f'; filament_colour = {slot["hex"]}\n; nozzle_diameter = 0.4\nM190 S{state["temps_c"]["bed"]}\nM104 S{state["temps_c"]["nozzle"]} T0\nSTART_PRINT BED_TEMP={state["temps_c"]["bed"]} EXTRUDER_TEMP={state["temps_c"]["nozzle"]}\nG1 X10 Y10 Z0.2\nEND_PRINT\n')
        return out, {'slot': filament_slot}, []

    monkeypatch.setattr(model_job.printer_state, 'check',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('slice contacted the printer')))
    monkeypatch.setattr('print_handy.core.orca_adapter.slice_model', fake_slice)
    paths = model_job.slice_job(job_path)
    saved = json.loads(job_path.read_text())
    assert len(paths) == 2
    assert 'refused_plates' not in saved
    assert [(row['plate_id'], row['slot'], row['part_keys']) for row in saved['derived_plates']] == [
        (1, 1, ['1:1']), (2, 1, ['2:1'])]


# ---------------------------------------------------------------------- #912

def _items_source(path, items, objects=None, settings_config=True):
    """Mesh objects, each placed by its own build item: (object_id, (x, y, z)).

    Default objects: 1 = 40x40x10 body, 2 = inlay seated 0.02 mm above it."""
    objects = objects or {'1': ('BODY', _box(40, 40, 10)), '2': ('INLAY', _box(20, 20, 2, (10, 10, 10.02)))}
    resources = ''.join(f'<object id="{oid}" name="{name}" type="model">{_mesh_xml(tris)}</object>'
                        for oid, (name, tris) in objects.items())
    build = ''.join(f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {x} {y} {z}" printable="1"/>'
                    for oid, x, y, z in items)
    settings = ''.join(f'<object id="{oid}"><metadata key="name" value="{name}"/>'
                       f'<metadata key="extruder" value="{1 if oid == "1" else 2}"/></object>'
                       for oid, (name, _t) in objects.items())
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr('3D/3dmodel.model', '<?xml version="1.0" encoding="UTF-8"?>'
                   f'<model unit="millimeter" xmlns="{CORE_NS}"><resources>{resources}</resources>'
                   f'<build>{build}</build></model>')
        if settings_config:
            z.writestr('Metadata/model_settings.config', f'<config>{settings}</config>')
    return path


def _export_items(tmp_path, items, objects=None, tower_mm=None, settings_config=True):
    from .test_plate_merge import _two_slot_decisions
    src = _items_source(tmp_path / 'items.3mf', items, objects, settings_config)
    manifest = source_colors.inventory(src)
    _mapping, report = export_color_geometry(src, tmp_path / 'out.3mf', _two_slot_decisions(manifest),
                                             tower_mm=tower_mm)
    with zipfile.ZipFile(tmp_path / 'out.3mf') as z:
        model = ET.fromstring(z.read('3D/3dmodel.model'))
        config = ET.fromstring(z.read('Metadata/model_settings.config'))
    return report, model, config


def _plate_objects(model):
    return model.findall(f'.//{{{CORE_NS}}}build/{{{CORE_NS}}}item')


def _tool_of_parts(config):
    return {part.find("metadata[@key='name']").get('value'): part.find("metadata[@key='extruder']").get('value')
            for part in config.iter('part')}


def test_interlocking_separate_build_items_export_as_one_assembly(tmp_path):
    report, model, config = _export_items(tmp_path, [('1', 20, 20, 0), ('2', 20, 20, 0)])
    assert len(_plate_objects(model)) == 1
    assert len(config.findall('object')) == 1
    assert _tool_of_parts(config) == {'BODY': '1', 'INLAY': '2'}  # per-part extruders survive
    assert len(report['parts']) == 2


def test_non_touching_separate_build_items_stay_separate_objects(tmp_path):
    report, model, config = _export_items(tmp_path, [('1', 20, 20, 0), ('2', 120, 120, 0)])
    assert len(_plate_objects(model)) == 2
    assert len(config.findall('object')) == 2


def test_repeated_instances_of_one_object_stay_separate(tmp_path):
    _report, model, config = _export_items(tmp_path, [('1', 20, 20, 0), ('1', 100, 20, 0), ('1', 20, 100, 0)],
                                         {'1': ('BODY', _box(40, 40, 10))}, settings_config=False)
    assert len(_plate_objects(model)) == 3
    assert len(config.findall('object')) == 3


def test_a_touching_chain_of_items_is_one_assembly_and_the_rest_stay_apart(tmp_path):
    # BODY at 20, INLAY on it, a second INLAY touching only the first inlay's footprint edge, and a far body.
    _report, model, config = _export_items(
        tmp_path, [('1', 20, 20, 0), ('2', 20, 20, 0), ('2', 50, 20, 0), ('1', 150, 150, 0)])
    # the third item (x 60..80) touches the body's edge region (20..60) only through the 1 mm tolerance.
    assert len(_plate_objects(model)) == 2


def test_tower_clears_the_whole_merged_group(tmp_path):
    report, _model, _config = _export_items(tmp_path, [('1', 20, 20, 0), ('2', 20, 20, 0)], tower_mm=60)
    tower = report['prime_tower_xy_mm']
    assert tower is not None
    boxes = [p['bounds_mm'] for p in report['parts']]
    x0, x1 = min(b[0][0] for b in boxes), max(b[0][1] for b in boxes)
    y0, y1 = min(b[1][0] for b in boxes), max(b[1][1] for b in boxes)
    clear_x = tower[0] >= x1 or tower[0] + 60 <= x0
    clear_y = tower[1] >= y1 or tower[1] + 60 <= y0
    assert clear_x or clear_y
    for candidate in report['prime_tower_candidates_mm']:
        assert candidate[0] >= x1 or candidate[0] + 60 <= x0 or candidate[1] >= y1 or candidate[1] + 60 <= y0


def test_single_item_multi_part_export_is_unchanged(tmp_path):
    src = _cutter_source(tmp_path / 'cutter.3mf')
    from .test_plate_merge import _two_slot_decisions
    manifest = source_colors.inventory(src)
    export_color_geometry(src, tmp_path / 'out.3mf', _two_slot_decisions(manifest))
    with zipfile.ZipFile(tmp_path / 'out.3mf') as z:
        model = ET.fromstring(z.read('3D/3dmodel.model'))
    assert len(_plate_objects(model)) == 1


def _cut_recess_and_plug_source(path):
    """Item 1: a body with a negative-volume recess. Item 2: a separate plug seated in that recess."""
    settings_xml = ('<?xml version="1.0" encoding="UTF-8"?><config><object id="1">'
                    '<metadata key="name" value="BODY"/><metadata key="extruder" value="1"/>'
                    '<part id="10" subtype="normal_part"/><part id="11" subtype="negative_volume"/>'
                    '</object><object id="2"><metadata key="name" value="PLUG"/>'
                    '<metadata key="extruder" value="2"/></object></config>')
    model = ('<?xml version="1.0" encoding="UTF-8"?>'
             f'<model unit="millimeter" xmlns="{CORE_NS}"><resources>'
             f'<object id="10" name="main" type="model">{_mesh_xml(_box(40, 40, 10))}</object>'
             f'<object id="11" name="recess" type="model">{_mesh_xml(_box(10, 10, 6, (15, 15, 5)))}</object>'
             '<object id="1" name="BODY" type="model"><components>'
             '<component objectid="10" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
             '<component objectid="11" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
             '</components></object>'
             f'<object id="2" name="PLUG" type="model">{_mesh_xml(_box(9.6, 9.6, 5, (15.2, 15.2, 5.02)))}</object>'
             '</resources><build>'
             '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 20 20 0" printable="1"/>'
             '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 20 20 0" printable="1"/>'
             '</build></model>')
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr('3D/3dmodel.model', model)
        z.writestr('Metadata/model_settings.config', settings_xml)
    return path


def test_cutter_bearing_item_is_not_merged_with_an_overlapping_item(tmp_path):
    """A negative volume is object-scoped: merging the plug into the body's object would cut the plug."""
    from .test_plate_merge import _two_slot_decisions
    src = _cut_recess_and_plug_source(tmp_path / 'recess.3mf')
    manifest = source_colors.inventory(src)
    assert manifest['cutters'], 'the fixture must really carry a cutter'
    export_color_geometry(src, tmp_path / 'out.3mf', _two_slot_decisions(manifest))
    with zipfile.ZipFile(tmp_path / 'out.3mf') as z:
        model = ET.fromstring(z.read('3D/3dmodel.model'))
        config = ET.fromstring(z.read('Metadata/model_settings.config'))
    assert len(_plate_objects(model)) == 2
    objects = config.findall('object')
    assert len(objects) == 2
    owner_of_cutter = [o for o in objects
                       if any(p.get('subtype') == 'negative_volume' for p in o.iter('part'))]
    assert len(owner_of_cutter) == 1
    names = {p.find("metadata[@key='name']").get('value') for p in owner_of_cutter[0].iter('part')}
    assert 'PLUG' not in names
