"""A resized or turned multicolor model stays one multicolor assembly.

Regression for the 2.3.1 report: "Reorient (lay flat)" on a one-plate
multicolor plaque (bronze letters resting on a black plaque, separate build
objects) recorded an identity rotation, and any recorded rotation or scale
made the slicer skip the one-bed merge and print one single-color plate per
slot. Plate 1 printed only the letters, detached from the plaque, and the
split reason read "Resized per-part job: one fitted plate per slot".
"""
import json
import zipfile
from pathlib import Path

import pytest

from print_handy.core import color_geometry, edit_plan, model_job, slice_refusal, source_colors
from print_handy.core.orca_adapter import _rotate, _yaw
from print_handy.jobs import slice as jobs_slice

STATE = Path(__file__).parent / 'fixtures' / 'printer_hardware.json'
IDENTITY = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]

# Plaque 80 x 50 x 4 mm; letters 40 x 10 x 2 mm resting on its top face.
PARTS = {'1': ('PLAQUE', (80, 50, 4), (40, 60, 0)),
         '2': ('LETTERS', (40, 10, 2), (60, 80, 4))}


def _box(w, d, h):
    pts = [(x, y, z) for z in (0, h) for y in (0, d) for x in (0, w)]
    body = ''.join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in pts)
    faces = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4),
             (2, 6, 3), (3, 6, 7), (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]
    tris = ''.join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces)
    return (f'<model unit="millimeter"><resources><object id="1"><mesh><vertices>{body}</vertices>'
            f'<triangles>{tris}</triangles></mesh></object></resources></model>')


def _plaque(path):
    """One authored plate: a plaque (extruder 1) with letters (extruder 2) resting on it."""
    resources, items, objects = [], [], []
    for oid, (name, _size, (x, y, z)) in PARTS.items():
        resources.append(f'<object id="{oid}" name="{name}"><components><component '
                         f'p:path="/3D/Objects/object_{oid}.model" objectid="1" '
                         'transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>')
        items.append(f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {x} {y} {z}"/>')
        objects.append(f'<object id="{oid}"><metadata key="name" value="{name}"/>'
                       f'<metadata key="extruder" value="{oid}"/><part id="1">'
                       f'<metadata key="name" value="{name}"/><metadata key="extruder" value="{oid}"/>'
                       '</part></object>')
    plate = ('<plate><metadata key="plater_id" value="1"/>'
             + ''.join(f'<model_instance><metadata key="object_id" value="{o}"/></model_instance>'
                       for o in PARTS) + '</plate>')
    with zipfile.ZipFile(path, 'w') as archive:
        for oid, (_name, size, _at) in PARTS.items():
            archive.writestr(f'3D/Objects/object_{oid}.model', _box(*size))
        archive.writestr('3D/3dmodel.model',
                         '<model xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" '
                         f'unit="millimeter"><resources>{"".join(resources)}</resources>'
                         f'<build>{"".join(items)}</build></model>')
        archive.writestr('Metadata/model_settings.config', f'<config>{"".join(objects)}{plate}</config>')
        archive.writestr('Metadata/project_settings.config',
                         '{\n  "filament_colour": [\n    "#111111",\n    "#BC8743"\n  ],\n'
                         '  "filament_type": [\n    "PLA",\n    "PLA"\n  ]\n}')
        archive.writestr('3D/_rels/3dmodel.model.rels', '<Relationships>' + ''.join(
            f'<Relationship Target="/3D/Objects/object_{o}.model" Id="r{o}"/>' for o in PARTS)
            + '</Relationships>')
        archive.writestr('Metadata/plate_1.png', b'png')
    return path


def _decisions(manifest):
    """The plaque prints in slot 1, the letters in slot 2."""
    state = json.loads(STATE.read_text())
    sel = {s: model_job.resolve_filament_selection(state, dict(state['filament_slots'][s], slot=int(s)))
           for s in ('1', '2')}
    choices = {}
    for part in manifest['parts']:
        choices['part:' + part['key']] = sel['2'] if part['object_id'] == '2' else sel['1']
    for row in source_colors.canonical_prompts(manifest):
        choices.setdefault(row['key'], choices.get(row['key']) or sel['1'])
    return {'schema': 'part_slots_v1', 'mode': 'per_part', 'source_sha256': manifest['source_sha256'],
            'choices': choices}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(model_job.printer_state, 'check',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('slice contacted the printer')))


def _fake_orca(manifest, calls):
    """Only Orca is faked: the real multicolor export runs with the slice's own arguments."""
    def fake_slice(source_arg, folder, state, brim=None, first_layer=False, color_decisions=None,
                   preserve_layout=False, filament_slot=None, item_shifts=None, fit_by_object=None,
                   scale=None, rotation=None, **kw):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        geometry = {}
        if color_decisions and color_decisions.get('mode') == 'per_part' and filament_slot is None:
            _mapping, geometry = color_geometry.export_color_geometry(
                source_arg, folder / 'geometry.3mf', color_decisions, item_shifts=item_shifts,
                fit_by_object=fit_by_object, scale=scale, rotation=rotation)
        calls.append({'filament_slot': filament_slot, 'scale': scale, 'rotation': rotation,
                      'item_shifts': item_shifts, 'geometry': geometry, 'source': str(source_arg)})
        out = folder / 'plate_1.gcode'
        labels = ''.join(f'; printing object {p["name"]} id:{i} copy 0\n' for i, p in enumerate(manifest['parts']))
        slot = state['filament_slots']['1']
        out.write_text(f'; filament_colour = {slot["hex"]}\n; nozzle_diameter = 0.4\n'
                       '; estimated printing time (normal mode) = 1h 0m 0s\n'
                       f'M190 S{state["temps_c"]["bed"]}\nM104 S{state["temps_c"]["nozzle"]} T0\n'
                       f'START_PRINT BED_TEMP={state["temps_c"]["bed"]} EXTRUDER_TEMP={state["temps_c"]["nozzle"]}\n'
                       f'{labels}G1 X10 Y10 Z0.2\nEND_PRINT\n')
        return out, {'geometry': geometry}, []
    return fake_slice


def _job(tmp_path, monkeypatch, **fields):
    source = _plaque(tmp_path / 'plaque.3mf')
    manifest = source_colors.inventory(source)
    job = {'downloaded_file': str(source), 'audit': {'plates': 1}, 'color_decisions': _decisions(manifest),
           **fields}
    job_path = tmp_path / 'JOB.json'
    job_path.write_text(json.dumps(job))
    calls = []
    monkeypatch.setattr('print_handy.core.orca_adapter.slice_model', _fake_orca(manifest, calls))
    return job_path, manifest, calls


def _relative_offset(geometry, manifest):
    """Letters' placed min corner minus the plaque's, per axis."""
    object_of_key = {p['key']: str(p['object_id']) for p in manifest['parts']}
    lows = {object_of_key[row['part_key']]: [row['bounds_mm'][a][0] for a in range(3)]
            for row in geometry['parts']}
    return [lows['2'][a] - lows['1'][a] for a in range(3)]


@pytest.mark.parametrize('fields', [
    pytest.param({'orientation': {'rotation': IDENTITY}}, id='identity-reorient'),
    pytest.param({'orientation': {'rotation': _yaw(90.0)}}, id='turned'),
    pytest.param({'scale': 0.8}, id='resized'),
])
def test_resized_or_turned_multicolor_plaque_slices_as_one_multicolor_bed(tmp_path, monkeypatch, fields):
    job_path, manifest, calls = _job(tmp_path, monkeypatch, **fields)
    out = model_job.slice_job(job_path)
    saved = json.loads(job_path.read_text())
    # One multicolor G-code: never a per-slot plate that prints the letters alone.
    assert isinstance(out, (str, Path)), f'split into plates: {out}'
    assert len(calls) == 1 and calls[0]['filament_slot'] is None and calls[0]['item_shifts'] is None
    assert 'derived_plates' not in saved
    assert saved['plate_merge']['mode'] == 'merge' and saved['plate_merge']['reason'] is None
    assert saved['plate_merge']['slots'] == [1, 2]
    geometry = calls[0]['geometry']
    assert sorted(row['part_key'] for row in geometry['parts']) == sorted(p['key'] for p in manifest['parts'])
    # The letters keep their place on the plaque, under the same turn and size.
    scale = float(fields.get('scale') or 1.0)
    rotation = (fields.get('orientation') or {}).get('rotation') or IDENTITY
    authored = [(60 - 40) * scale, (80 - 60) * scale, 4 * scale]
    if rotation is not IDENTITY:
        # Turning both bodies by one matrix moves each min corner; compare
        # against the export's own rotation of the authored points.
        with zipfile.ZipFile(job_path.parent / 'plaque.3mf') as archive:
            points = {oid: [_rotate(p, rotation) for p in color_geometry._world_points(archive, {oid})]
                      for oid in ('1', '2')}
        authored = [min(p[a] for p in points['2']) - min(p[a] for p in points['1']) for a in range(3)]
    assert _relative_offset(geometry, manifest) == pytest.approx(authored, abs=1e-3)
    model_job.require_current_artifact(saved, out)


def test_whole_assembly_rotation_refuses_per_object_placement(tmp_path):
    source = _plaque(tmp_path / 'plaque.3mf')
    decisions = _decisions(source_colors.inventory(source))
    with pytest.raises(RuntimeError, match='whole-assembly rotation'):
        color_geometry.export_color_geometry(source, tmp_path / 'x.3mf', decisions,
                                             item_shifts={'1': [0, 0, 0], '2': [0, 0, 0]},
                                             rotation=_yaw(90.0))


def test_a_fitted_assembly_that_cannot_stay_one_plate_says_so_plainly(tmp_path, monkeypatch):
    # At 290% the plaque is 232 mm wide: it cannot stay one plate on the
    # 220 mm bed, and the split reason says so in plain words.
    job_path, manifest, calls = _job(tmp_path, monkeypatch, scale=2.9)
    job = json.loads(job_path.read_text())
    with pytest.raises(model_job.ImplicitPlateSplit) as caught:
        jobs_slice.slice_multi_plate(job, job_path, Path(job['downloaded_file']), manifest,
                                     job['color_decisions'], json.loads(STATE.read_text()), None)
    assert calls == []  # measured before slicing; no slice was attempted
    reason = caught.value.verdict['reason']
    assert 'Resized per-part job' not in reason and 'fitted plate per slot' not in reason
    assert 'resized or turned' in reason and 'does not fit one plate' in reason
    assert 'print apart' in reason
    assert slice_refusal.split_cause(reason) == 'resized'
    assert slice_refusal.split_cause(jobs_slice.FITTED_SPLIT_REASON) == 'resized'


def test_lies_flat_already_detects_an_identity_lay_flat_turn():
    assert edit_plan.lies_flat_already(IDENTITY)
    assert edit_plan.lies_flat_already(_yaw(30.0))  # a spin on the bed is still flat
    assert not edit_plan.lies_flat_already(edit_plan.rotation_matrix('x', 90))
    assert not edit_plan.lies_flat_already(edit_plan.rotation_matrix('x', 180))
    assert not edit_plan.lies_flat_already(None)
    # A wedge whose largest face is already on the plate needs no turn.
    base = [((0, 0, 0), (60, 40, 0), (60, 0, 0)), ((0, 0, 0), (0, 40, 0), (60, 40, 0))]
    top = [((0, 0, 3), (10, 0, 3), (10, 10, 3))]
    matrix, _dims = edit_plan.lay_flat_rotation(base + top)
    assert edit_plan.lies_flat_already(matrix)
