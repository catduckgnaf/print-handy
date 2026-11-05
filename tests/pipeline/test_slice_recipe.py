"""Slice identity must cover the complete slice recipe, not a label for it.

Every input that changes the G-code (transform, scale, copies, support/brim policy,
split assignments, printer model, material/nozzle, resolved profiles, slicer identity)
must change the binding, so a slice made for one recipe is never reused for another.
"""
import json
from pathlib import Path

import pytest

from print_handy.core import model_job as mj
from print_handy.core import orca_adapter, printer_state
from print_handy.core import slice_recipe as recipe_module
from print_handy.core.background_slice import slice_binding

from .test_artifact_binding import setup_job

UPRIGHT = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
ON_SIDE = [[1, 0, 0], [0, 0, -1], [0, 1, 0]]


def _job(**extra):
    return {
        'color_decisions': {'mode': 'single_color', 'source_sha256': 'source-a',
                            'selection': {'slot': 1, 'color': 'white', 'material': 'PLA'}},
        'scale': 1.0,
        'orientation': {'label': 'best fit', 'rotation': UPRIGHT, 'axis': 'z',
                        'dims_full_mm': [40, 10, 1], 'supports': False, 'brim': False},
        'copies': 1,
        'plate_mode': 'separate',
        'fit_brim_policy': {'width_mm': 0},
        'full_size_split_plan': {'plates': [['a', 'b'], ['c']]},
        **extra,
    }


def _with_orientation(**change):
    base = _job()
    return {**base, 'orientation': {**base['orientation'], **change}}


JOB_CHANGES = {
    'rotation matrix, same label': _with_orientation(rotation=ON_SIDE),
    'rotated envelope, same label': _with_orientation(dims_full_mm=[40, 1, 10]),
    'supports, same label': _with_orientation(supports=True),
    'orientation brim, same label': _with_orientation(brim=True),
    'fit brim policy': _job(fit_brim_policy={'width_mm': 5}),
    'split assignments': _job(full_size_split_plan={'plates': [['a'], ['b', 'c']]}),
    'printer model': _job(printer_model='sovol.sv06'),
    'material choice': _job(color_decisions={'mode': 'single_color', 'source_sha256': 'source-a',
                                             'selection': {'slot': 1, 'color': 'white', 'material': 'PETG'}}),
    'scale': _job(scale=0.8),
    'copies': _job(copies=3),
    'unit decision': _job(unit_decision={'source_sha256': 'source-a', 'units': 'inch',
                                         'original_mm': [2.0, 3.0, 4.0], 'source': 'user_confirmation'}),
}


@pytest.mark.parametrize('name', sorted(JOB_CHANGES))
def test_every_job_recipe_input_changes_the_binding(name):
    assert slice_binding(JOB_CHANGES[name], 'source-a') != slice_binding(_job(), 'source-a'), name


def test_binding_is_a_versioned_recipe_and_ignores_dict_order():
    base = _job()
    binding = slice_binding(base, 'source-a')
    recipe = json.loads(binding)
    assert recipe['recipe_version'] == recipe_module.RECIPE_VERSION == 3
    assert recipe['source_sha256'] == 'source-a'
    assert slice_binding(dict(reversed(list(base.items()))), 'source-a') == binding


def _hardware(**change):
    st = json.loads(Path(printer_state.STATE).read_text())
    for path, value in change.items():
        target = st
        *parents, leaf = path.split('.')
        for key in parents:
            target = target[key]
        target[leaf] = value
    return st


@pytest.mark.parametrize('path,value', [
    ('nozzle.diameter_mm', 0.6),
    ('nozzle.type', 'hardened'),
    ('filament_slots.1.material', 'PETG'),
    ('filament_slots.1.hex', '000000'),
])
def test_printer_material_and_nozzle_state_change_the_binding(monkeypatch, path, value):
    monkeypatch.setattr(printer_state, 'load', _hardware)
    before = slice_binding(_job(), 'source-a')
    monkeypatch.setattr(printer_state, 'load', lambda: _hardware(**{path: value}))
    assert slice_binding(_job(), 'source-a') != before


def test_resolved_profile_bytes_change_the_binding(tmp_path, monkeypatch):
    templates = tmp_path / 'templates'
    templates.mkdir()
    for name in ('machine', 'process', 'filament'):
        (templates / f'{name}.json').write_text((Path(orca_adapter.TEMPLATES) / f'{name}.json').read_text())
    monkeypatch.setattr(orca_adapter, 'TEMPLATES', templates)
    before = slice_binding(_job(), 'source-a')
    process = json.loads((templates / 'process.json').read_text())
    process['print_handy_test_change'] = '1'
    (templates / 'process.json').write_text(json.dumps(process))
    assert slice_binding(_job(), 'source-a') != before


def test_slicer_identity_changes_the_binding(monkeypatch):
    before = slice_binding(_job(), 'source-a')
    monkeypatch.setattr(orca_adapter, 'ORCA_COMMAND', ['/opt/other-orca/orca-slicer'])
    assert slice_binding(_job(), 'source-a') != before


def test_slice_records_versioned_recipe_hash_in_the_artifact(tmp_path, monkeypatch):
    jp, _st, _gcode = setup_job(tmp_path, monkeypatch)
    job = json.loads(jp.read_text())
    record = job['slice_recipe']
    assert record['version'] == recipe_module.RECIPE_VERSION
    assert job['artifact_binding']['recipe_version'] == recipe_module.RECIPE_VERSION
    assert job['artifact_binding']['recipe_sha256'] == record['sha256']
    from print_handy.core import slice_recipe
    expected = slice_binding(job, mj.file_sha256(job['downloaded_file']))
    assert record['sha256'] == slice_recipe.binding_sha256(expected)
    mj.require_current_artifact(job, job['gcode'])


def test_launch_refuses_when_the_nozzle_changes_after_slicing(tmp_path, monkeypatch):
    # P2-52: require_current_artifact must recompute the recipe at launch. The
    # stored record is internally consistent, but the hardware record no longer
    # matches what was sliced, so the old artifact must be refused.
    import copy
    jp, st, gcode = setup_job(tmp_path, monkeypatch)
    persisted = json.loads(jp.read_text())
    mj.require_current_artifact(persisted, gcode)  # unchanged inputs: still accepted
    changed = copy.deepcopy(st)
    changed['nozzle']['diameter_mm'] = 0.4  # the fixture slices with a 0.6 nozzle
    monkeypatch.setattr(mj.printer_state, 'load', lambda: changed)
    with pytest.raises(RuntimeError, match='stale color artifact'):
        mj.require_current_artifact(persisted, gcode)


def test_launch_refuses_when_a_job_transform_changes_after_slicing(tmp_path, monkeypatch):
    # P2-52: a transform edited in JOB.json after slicing changes the current
    # recipe even though the stored recipe still hashes to its stored digest.
    jp, _st, gcode = setup_job(tmp_path, monkeypatch)
    persisted = json.loads(jp.read_text())
    persisted['scale'] = 0.5
    with pytest.raises(RuntimeError, match='stale color artifact'):
        mj.require_current_artifact(persisted, gcode)


def test_launch_refuses_when_slice_settings_change_after_slicing(tmp_path, monkeypatch):
    # P2-52: settings that are recipe inputs (here the prime tower) changing
    # after slicing must refuse the old artifact at launch.
    from print_handy import settings
    jp, _st, gcode = setup_job(tmp_path, monkeypatch)
    persisted = json.loads(jp.read_text())
    changed = dict(settings.current())
    changed['prime_tower'] = {**changed['prime_tower'], 'size_mm': 99}
    monkeypatch.setattr(settings, 'current', lambda: changed)
    with pytest.raises(RuntimeError, match='stale color artifact'):
        mj.require_current_artifact(persisted, gcode)


def test_worker_refuses_a_request_bound_to_another_recipe(tmp_path, monkeypatch):
    jp, _st, _gcode = setup_job(tmp_path, monkeypatch)
    before = jp.read_bytes()
    calls = []
    monkeypatch.setattr(orca_adapter, 'slice_model', lambda *a, **k: calls.append(a))
    with pytest.raises(RuntimeError, match='recipe'):
        mj.slice_job(str(jp), recipe_sha256='0' * 64)
    assert calls == [] and jp.read_bytes() == before


def test_recipe_change_during_slicing_never_marks_the_artifact_ready(tmp_path, monkeypatch):
    jp, _st, _gcode = setup_job(tmp_path, monkeypatch)
    real = orca_adapter.slice_model

    def slice_then_rotate(*args, **kwargs):
        result = real(*args, **kwargs)
        job = json.loads(jp.read_text())
        job['orientation'] = {'label': 'best fit', 'rotation': ON_SIDE}
        jp.write_text(json.dumps(job))
        return result
    monkeypatch.setattr(orca_adapter, 'slice_model', slice_then_rotate)
    previous = json.loads(jp.read_text())['artifact_binding']
    with pytest.raises(RuntimeError, match='changed during slicing'):
        mj.slice_job(str(jp))
    assert json.loads(jp.read_text())['artifact_binding'] == previous


def test_artifact_sliced_before_recipe_identity_is_refused_not_guessed(tmp_path, monkeypatch):
    jp, _st, gcode = setup_job(tmp_path, monkeypatch)
    job = json.loads(jp.read_text())
    legacy = {key: job['artifact_binding'][key] for key in ('source_sha256', 'decisions_sha256', 'gcode_sha256')}
    job['artifact_binding'] = legacy
    job.pop('slice_recipe', None)
    with pytest.raises(RuntimeError, match='slice recipe'):
        mj.require_current_artifact(job, gcode)


def test_tampered_recipe_record_is_refused(tmp_path, monkeypatch):
    jp, _st, gcode = setup_job(tmp_path, monkeypatch)
    job = json.loads(jp.read_text())
    job['slice_recipe']['recipe']['copies'] = 4
    with pytest.raises(RuntimeError, match='slice recipe'):
        mj.require_current_artifact(job, gcode)


def test_worker_accepts_the_recipe_intake_bound_before_the_first_slice(tmp_path, monkeypatch):
    # Intake binds the job as saved before slicing; slice_job then normalizes the job
    # (printer model, resolved slot selection) and must still see the same recipe.
    from .test_color_intake import source, state
    from print_handy.core import slice_recipe
    st = state(); st.update(bed_mm=[220, 220], max_print_height_mm=220, moonraker_url='offline://fake')
    for slot in st['filament_slots'].values():
        slot['physical_color_verified'] = True
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps({'downloaded_file': str(source(tmp_path)),
                              'audit': {'plates': 1, 'size_x': 40, 'size_y': 10, 'size_z': 1}}))
    mj.intake_color_choices(jp, st, 'single_color', [1])
    monkeypatch.setattr(mj.printer_state, 'load', lambda: st)
    job = json.loads(jp.read_text())
    job['copies'] = 2
    jp.write_text(json.dumps(job))
    assert 'printer_model' not in job

    def slice_fake(src, folder, st, brim, color_decisions, filament_slot=None, copies=1):
        folder.mkdir(parents=True)
        out = folder / 'plate_1.gcode'
        out.write_text('G1 X1\n')
        return out, {'offline_test': True}, []
    monkeypatch.setattr(orca_adapter, 'slice_model', slice_fake)
    monkeypatch.setattr(mj, 'verify_gcode', lambda _: None)
    from print_handy.printers.flashforge.zmod import multicolor_contract
    monkeypatch.setattr(multicolor_contract, 'finalize', lambda *a, **k: None)
    requested = slice_recipe.binding_sha256(slice_binding(job, mj.file_sha256(job['downloaded_file'])))
    mj.slice_job(str(jp), copies=2, recipe_sha256=requested)
    sliced = json.loads(jp.read_text())
    assert sliced['printer_model'] and sliced['artifact_binding']['recipe_sha256'] == requested


def test_spacing_and_prime_tower_settings_are_recipe_inputs(monkeypatch):
    # They decide where parts and the tower land, so changing them must
    # change the recipe: an old slice is never reused under new spacing.
    from print_handy import settings
    job = {'title': 't'}
    state = {'nozzle': {'diameter_mm': 0.4}, 'filament_slots': {}}
    before = recipe_module.slice_recipe(job, 'a' * 64, state)
    assert before['layout']['spacing']['layout.merge_edge_margin_mm'] is not None
    assert 'size_mm' in before['layout']['prime_tower']
    changed = dict(settings.current())
    changed['layout'] = {**changed['layout'],
                         'merge_edge_margin_mm': changed['layout']['merge_edge_margin_mm'] + 3.0}
    monkeypatch.setattr(settings, 'current', lambda: changed)
    after = recipe_module.slice_recipe(job, 'a' * 64, state)
    assert recipe_module.recipe_sha256(after) != recipe_module.recipe_sha256(before)


def test_remap_slot_keeps_a_bound_job_launchable_but_still_refuses_other_changes(tmp_path, monkeypatch):
    # 2.2.5 review: the recipe hashes filament_selection.slot, and remap_slot
    # changes it for a same-G-code launch on another spool. The remap must carry
    # the new slot into the recorded recipe and binding, or print-now
    # --remap-slot refuses its own job as a stale color artifact.
    from types import SimpleNamespace
    jp, _st, gcode = setup_job(tmp_path, monkeypatch)
    before = json.loads(jp.read_text())
    assert before['artifact_binding'] and before['color_decisions']
    selection = {**before['filament_selection'], 'slot': 2}
    native = SimpleNamespace(normalize_mapping=lambda mapping, st: mapping, finalize=lambda *a: None)
    routed = SimpleNamespace(native=native)
    monkeypatch.setattr(mj, 'resolve_filament_selection', lambda st, request: selection)
    monkeypatch.setattr(mj, '_verify_routed', lambda *a, **k: None)
    with monkeypatch.context() as scoped:
        # Only the remap itself runs against the stub adapter; the launch-time
        # recipe recompute below needs the job's real adapter.
        scoped.setattr(mj, '_job_adapter', lambda job: routed)
        scoped.setattr(mj.printers, 'current', lambda: routed)
        mj.remap_slot(jp, 2, state=_st)
    after = json.loads(jp.read_text())
    assert after['filament_selection']['slot'] == 2
    assert after['slice_recipe']['recipe']['filament']['selected_slot'] == 2
    assert after['artifact_binding']['recipe_sha256'] == after['slice_recipe']['sha256'] != before['slice_recipe']['sha256']
    mj.require_current_artifact(after, gcode)  # the remapped job still launches
    after['scale'] = 0.5  # a real input change after the remap is still stale
    with pytest.raises(RuntimeError, match='stale color artifact'):
        mj.require_current_artifact(after, gcode)
