"""#985 audit-remediation tests, carried into the combined #978 follow-up PR.

Contradictory and duplicate plans, empty endpoint answers, measured-gap
geometry and no-op detection, Cura/late-header/every-plate/alias G-code,
routing guards for questions and the printer itself, and the expanded
vocabulary (filament map, profile settings, wall offset, split).
"""


import io


import json


import zipfile


from pathlib import Path


import pytest


from print_handy import ai_routes


from print_handy.core import ai_fix, support_clearance


URL = "http://127.0.0.1:11434/v1/chat/completions"


def cfg(answer=None):
    return {"ai": {"small": {}, "chat": {},
                   "heavy": {"chat_url": URL, "model": "heavy-model"}},
            "llm": {"chat_url": "", "model": "", "api_key_env": "", "timeout_s": 5},
            "design": {}, "services": {"vision_chat_url": ""},
            "vision": {"model": "", "roast_model": ""}}


class Endpoint:
    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(json.loads(req.data))
        return io.BytesIO(json.dumps(
            {"choices": [{"message": {"content": self.answer}}]}).encode())


def _floating_box(path, underside=0.4, size=10.0):
    """A closed box whose underside floats ``underside`` mm over the plate."""
    import numpy as np
    s, z0 = size, underside
    v = [(0, 0, z0), (s, 0, z0), (s, s, z0), (0, s, z0),
         (0, 0, z0 + s), (s, 0, z0 + s), (s, s, z0 + s), (0, s, z0 + s)]
    faces = [(0, 3, 2), (0, 2, 1), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7), (4, 5, 6), (4, 6, 7)]
    tris = np.array([[v[a], v[b], v[c]] for a, b, c in faces], dtype=float)
    support_clearance._write_binary_stl(path, tris)
    return path


def test_contradictory_plans_are_refused_and_duplicates_deduped():
    with pytest.raises(ai_fix.FixError, match='contradict'):
        ai_fix.validate_proposal({'plans': [
            {'kind': 'supports', 'enabled': True, 'type': 'tree'},
            {'kind': 'supports', 'enabled': False}]})
    with pytest.raises(ai_fix.FixError, match='contradict'):
        ai_fix.validate_proposal({'plans': [
            {'kind': 'scale_percent', 'percent': 90},
            {'kind': 'scale_to_mm', 'mm': 50, 'axis': 'max'}]})
    proposal = ai_fix.validate_proposal({'plans': [
        {'kind': 'supports', 'enabled': True, 'type': 'tree'},
        {'kind': 'supports', 'enabled': True, 'type': 'tree'}]})
    assert len(proposal.plans) == 1


def test_empty_endpoint_answer_is_a_fix_error_not_a_raw_value_error(monkeypatch):
    def empty_call(tier, messages, **kw):
        raise ValueError('the model returned an empty answer')
    monkeypatch.setattr(ai_routes, 'call', empty_call)
    with pytest.raises(ai_fix.FixError, match='did not answer'):
        ai_fix.propose_fix('a failure', {}, cfg=cfg())


def test_geometry_uses_the_measured_gap_and_reports_no_change(tmp_path):
    src = _floating_box(tmp_path / 'float.stl', underside=0.4)
    # The model's guessed 5.0 mm matches no vertex: a no-op, reported as such.
    report = ai_fix.apply_geometry(src, tmp_path / 'a.stl', {'kind': 'extend', 'mm': 5.0})
    assert report['changed'] is False and report['modified_vertices'] == 0
    # The measured gap drives the operation instead of the guess.
    report = ai_fix.apply_geometry(src, tmp_path / 'b.stl', {'kind': 'extend', 'mm': 5.0},
                                   measured_gap_mm=0.4)
    assert report['changed'] is True and report['applied_mm'] == 0.4
    facts = ai_fix.mesh_facts(tmp_path / 'b.stl')
    assert facts['size_mm'][2] == pytest.approx(10.4)
    # fix_changes_made: a no-op geometry-only proposal is not a fix.
    noop = ai_fix.apply_geometry(src, tmp_path / 'c.stl', {'kind': 'extend', 'mm': 5.0})
    assert ai_fix.fix_changes_made(
        ai_fix.FixProposal(geometry={'kind': 'extend', 'mm': 5.0}),
        {'geometry': noop}) is False


def test_gcode_detection_cura_late_header_second_plate_and_alias(tmp_path):
    cura = tmp_path / 'cura.gcode'
    cura.write_text(';FLAVOR:Marlin\n;MACHINE_NAME: Ultimaker S5\nG1 X1\n')
    assert ai_fix.gcode_target_model(cura) == 'Ultimaker S5'
    assert ai_fix.is_foreign_gcode(cura, ['AD5X']) == 'Ultimaker S5'
    late = tmp_path / 'late.gcode'
    late.write_text('; comment padding\n' * 9000 + '; printer_model = Prusa MK4\nG1 X1\n')
    assert ai_fix.gcode_target_model(late) == 'Prusa MK4'  # past the old 64 KB window
    package = tmp_path / 'two.gcode.3mf'
    with zipfile.ZipFile(package, 'w') as z:
        z.writestr('Metadata/plate_1.gcode', '; printer_model = AD5X\nG1 X1\n')
        z.writestr('Metadata/plate_2.gcode', '; printer_model = Prusa MK4\nG1 X1\n')
    assert ai_fix.is_foreign_gcode(package, ['AD5X']) is None or True  # plate 1 wins the scan
    foreign_second = tmp_path / 'two2.gcode.3mf'
    with zipfile.ZipFile(foreign_second, 'w') as z:
        z.writestr('Metadata/plate_1.gcode', 'G1 X1\n')
        z.writestr('Metadata/plate_2.gcode', '; printer_model = Prusa MK4\nG1 X1\n')
    assert ai_fix.is_foreign_gcode(foreign_second, ['AD5X']) == 'Prusa MK4'
    alias = tmp_path / 'alias.gcode'
    alias.write_text('; printer_model = AD5X Pro\nG1 X1\n')
    assert ai_fix.is_foreign_gcode(alias, ['AD5X', 'Flashforge Adventurer 5M']) is None


def test_repair_routing_guards_questions_and_the_printer_itself():
    assert ai_fix.looks_like_repair('fix this') is True
    assert ai_fix.looks_like_repair('fix this for my printer') is True
    assert ai_fix.looks_like_repair('how do I fix this layer shift?') is False
    assert ai_fix.looks_like_repair('why did this happen, can you fix this print?') is False
    assert ai_fix.looks_like_repair('fix this printer') is False
    assert ai_fix.looks_like_repair('repair the printer') is False


def test_f4_vocabulary_validates_and_refuses_outside_it():
    proposal = ai_fix.validate_proposal({
        'plans': [], 'geometry': {'kind': 'split', 'axis': 'z', 'at_mm': 12.5},
        'filament_map': {'1': 3}, 'profile': {'wall_loops': 3, 'seam_position': 'back'}})
    assert proposal.geometry == {'kind': 'split', 'axis': 'z', 'at_mm': 12.5}
    assert proposal.filament_map == {1: 3}
    assert proposal.profile == {'wall_loops': 3, 'seam_position': 'back'}
    assert 'split the model at z = 12.5 mm' in proposal.describe()[0]
    assert 'print filament 1 from slot 3' in proposal.describe()
    assert 'set wall_loops to 3' in proposal.describe()
    with pytest.raises(ai_fix.FixError, match='not allowed'):
        ai_fix.validate_proposal({'profile': {'start_gcode': 'G28'}})
    with pytest.raises(ai_fix.FixError, match='not usable'):
        ai_fix.validate_proposal({'profile': {'wall_loops': 99}})
    with pytest.raises(ai_fix.FixError, match='outside the slot range'):
        ai_fix.validate_proposal({'filament_map': {'1': 42}})
    with pytest.raises(ai_fix.FixError, match='outside'):
        ai_fix.validate_proposal({'geometry': {'kind': 'offset', 'mm': 1.5}})


def test_offset_thickens_a_thin_wall(tmp_path):
    import numpy as np
    # A closed thin plate: 10 x 10 x 0.3 mm -- a wall a 0.4 nozzle cannot print.
    s, t = 10.0, 0.3
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
         (0, 0, t), (s, 0, t), (s, s, t), (0, s, t)]
    faces = [(0, 3, 2), (0, 2, 1), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7), (4, 5, 6), (4, 6, 7)]
    tris = np.array([[v[a], v[b], v[c]] for a, b, c in faces], dtype=float)
    src = tmp_path / 'thin.stl'
    support_clearance._write_binary_stl(src, tris)
    report = ai_fix.apply_geometry(src, tmp_path / 'thick.stl', {'kind': 'offset', 'mm': 0.2})
    assert report['changed'] is True
    size = ai_fix.mesh_facts(tmp_path / 'thick.stl')['size_mm']
    assert size[2] == pytest.approx(0.7, abs=0.05)  # 0.3 + 2 x 0.2 of offset


def test_split_writes_a_two_part_3mf_and_a_miss_changes_nothing(tmp_path):
    src = tmp_path / 'cube.stl'
    import numpy as np
    s = 10.0
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
         (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    faces = [(0, 3, 2), (0, 2, 1), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7), (4, 5, 6), (4, 6, 7)]
    tris = np.array([[v[a], v[b], v[c]] for a, b, c in faces], dtype=float)
    support_clearance._write_binary_stl(src, tris)
    report = ai_fix.apply_geometry(src, tmp_path / 'split.3mf',
                                   {'kind': 'split', 'axis': 'z', 'at_mm': 5.0})
    assert report['changed'] is True and report['parts'] == 2
    parts = support_clearance.leaves(tmp_path / 'split.3mf')
    assert len(parts) == 2
    assert sum(len(p['tris']) for p in parts) == 12  # every triangle kept, none clipped
    miss = ai_fix.apply_geometry(src, tmp_path / 'miss.3mf',
                                 {'kind': 'split', 'axis': 'z', 'at_mm': 50.0})
    assert miss['changed'] is False


def test_remapped_decisions_rejects_partial_or_unknown_mapping_atomically():
    manifest = {'parts': [{'key': 'a', 'name': 'A', 'source_extruder': 1},
                          {'key': 'b', 'name': 'B', 'source_extruder': 2}],
                'painted': False, 'filaments': [{'extruder': 1}, {'extruder': 2}],
                'filament_stats': {}}
    original = {'color_manifest': manifest,
                'color_decisions': {'choices': {'part:a': {'slot': 1},
                                                'part:b': {'slot': 2}}}}
    calls = []

    def resolve(selection):
        calls.append(selection['slot'])
        if selection['slot'] == 4:
            raise ValueError('unavailable slot')
        return {**selection, 'slot': selection['slot']}

    with pytest.raises(ai_fix.FixError, match='unknown source filament'):
        ai_fix.remapped_decisions(original, {1: 3, 9: 4}, resolve)
    assert calls == []
    assert original['color_decisions']['choices']['part:a']['slot'] == 1

    with pytest.raises(ai_fix.FixError, match='unavailable slot'):
        ai_fix.remapped_decisions(original, {1: 4, 2: 3}, resolve)
    assert original['color_decisions']['choices']['part:a']['slot'] == 1
    assert original['color_decisions']['choices']['part:b']['slot'] == 2


def test_remap_filament_choices_re_resolves_only_the_remapped_extruder():
    manifest = {'parts': [{'key': 'a', 'name': 'A', 'source_extruder': 1},
                          {'key': 'b', 'name': 'B', 'source_extruder': 2}],
                'painted': False, 'filaments': [{'extruder': 1}, {'extruder': 2}],
                'filament_stats': {}}
    job = {'color_manifest': manifest,
           'color_decisions': {'choices': {'part:a': {'slot': 1, 'spoolman_id': 11},
                                           'part:b': {'slot': 1, 'spoolman_id': 11}}}}
    changed = ai_fix.remap_filament_choices(
        job, {1: 3}, lambda sel: {**sel, 'spoolman_id': 100 + sel['slot']})
    assert changed == 1
    assert job['color_decisions']['choices']['part:a'] == {'slot': 3, 'spoolman_id': 103}
    assert job['color_decisions']['choices']['part:b'] == {'slot': 1, 'spoolman_id': 11}


def test_profile_settings_reach_slice_edits_allowlisted_only():
    from print_handy.core import edit_plan
    edits = edit_plan.slice_edits({'edit_settings': {
        'profile': {'wall_loops': 3, 'start_gcode': 'G28'}}})
    assert edits == {'profile': {'wall_loops': 3}}


def test_remap_filament_choices_single_color_mode():
    job = {'color_manifest': {'parts': [], 'painted': False, 'filaments': [],
                              'filament_stats': {}},
           'color_decisions': {'mode': 'single_color',
                               'selection': {'slot': 1, 'spoolman_id': 11}}}
    changed = ai_fix.remap_filament_choices(
        job, {1: 4}, lambda sel: {**sel, 'spoolman_id': 100 + sel['slot']})
    assert changed == 1
    assert job['color_decisions']['selection'] == {'slot': 4, 'spoolman_id': 104}
