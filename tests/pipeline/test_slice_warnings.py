"""Orca support warnings: parsed on success, gate the launch until the operator decides."""
import ast
import json
import shutil
from pathlib import Path

import pytest

from print_handy.core import model_job as mj
from print_handy.core import orca_adapter, slice_warnings

from .test_artifact_binding import setup_job

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'orca_result_support_warnings.json'


def test_real_result_json_is_a_support_finding_naming_both_objects():
    found = slice_warnings.parse(FIXTURE)
    assert found['support_needed'] is True
    assert found['objects'] == ['BLACK BODY', 'BRONZE DETAILS']
    # The plate-level warning_message repeats the second warning: de-duplicated, not doubled.
    assert len(found['support_texts']) == 2
    assert found['info'] == []


def test_other_warnings_are_information_deduplicated_and_ignorable(tmp_path):
    payload = {'return_code': 0, 'warnings': [
        {'class': 'slicing_warning_non_critical', 'text': 'Thin wall   detected.'},
        {'class': 'slicing_warning_non_critical', 'text': 'Thin wall detected.'},
        {'class': 'slicing_warning_non_critical', 'text': 'Known harmless noise 7'}],
        'sliced_plates': [{'warning_message': 'Thin wall detected.'}]}
    ignore = tmp_path / 'ignore.json'
    ignore.write_text(json.dumps({'ignore': [{'pattern': 'harmless noise'}, {'oops': 1}]}))
    found = slice_warnings.parse(payload, ignore_path=ignore)
    assert found['support_needed'] is False
    assert found['info'] == ['Thin wall detected.']
    assert found['ignored'] == ['Known harmless noise 7']


def test_tolerant_of_odd_shapes_and_failed_slices(tmp_path):
    assert slice_warnings.parse(tmp_path / 'missing.json') is None
    assert slice_warnings.parse({'return_code': -100, 'warnings': []}) is None
    odd = slice_warnings.parse({'warnings': ['floating regions on object X has none', 7, None],
                                'sliced_plates': ['x', {'warning_message': ['a', 'b']}]})
    assert odd['support_needed'] is True and odd['info'] == ['a', 'b']


def test_compact_info_is_one_line_with_a_count():
    job = {'provenance': {'slice_warnings': {'info': ['first', 'second', 'third']}}}
    line = slice_warnings.compact_info(job)
    assert line.count('\n') == 0 and '(3)' in line and 'first' in line and 'second' not in line
    assert slice_warnings.compact_info({}) == ''


def test_provenance_fields_read_the_slice_folder(tmp_path):
    shutil.copy(FIXTURE, tmp_path / 'result.json')
    fields = slice_warnings.provenance_fields(tmp_path, {'enable_support': '0'}, None)
    assert fields['slice_warnings']['support_needed'] and not fields['supports_on']
    assert not fields['support_decided']
    assert slice_warnings.provenance_fields(tmp_path, {'enable_support': '1'}, None)['supports_on']


def _warned(supports_on=False, decided=False):
    return {'provenance': {'slice_warnings': slice_warnings.parse(FIXTURE),
                           'supports_on': supports_on, 'support_decided': decided}}


def test_finding_rules():
    assert slice_warnings.support_finding(_warned())['objects'] == ['BLACK BODY', 'BRONZE DETAILS']
    assert slice_warnings.support_finding(_warned(supports_on=True)) is None
    assert slice_warnings.support_finding(_warned(decided=True)) is None
    # The author's enable_support=0 is not a decision; an explicit one on the job is.
    assert slice_warnings.support_finding({**_warned(), 'support_decision': {'supports': False}}) is None
    # Explicit operator "no supports" is never overridden, even if the slicer complains.
    assert slice_warnings.support_finding({**_warned(), 'orientation': {'rotation': [[1]], 'supports': False}}) is None


def _warned_slice(tmp_path, monkeypatch):
    def slice_fake(src, folder, st, brim, color_decisions, filament_slot=None, copies=1, **kw):
        folder.mkdir(parents=True)
        out = folder / 'plate_1.gcode'
        out.write_text(json.dumps(color_decisions, sort_keys=True))
        shutil.copy(FIXTURE, folder / 'result.json')
        return out, {'offline_test': True,
                     **slice_warnings.provenance_fields(folder, {'enable_support': '0'}, kw.get('supports'))}, []
    jp, st, gcode = setup_job(tmp_path, monkeypatch)
    monkeypatch.setattr(orca_adapter, 'slice_model', slice_fake)
    return jp, Path(mj.slice_job(jp))


def test_warned_slice_cannot_reach_start_without_a_decision(tmp_path, monkeypatch):
    jp, gcode = _warned_slice(tmp_path, monkeypatch)
    job = json.loads(jp.read_text())
    with pytest.raises(RuntimeError, match='supports decision needed'):
        mj.require_current_artifact(job, gcode)


def test_print_without_supports_proceeds_and_is_remembered(tmp_path, monkeypatch):
    jp, gcode = _warned_slice(tmp_path, monkeypatch)
    mj.record_support_decision(jp.parent, False)
    job = json.loads(jp.read_text())
    assert job['support_decision']['supports'] is False
    mj.require_current_artifact(job, gcode)  # no refusal, and nothing asks again on a reprint
    assert slice_warnings.support_finding(job) is None


def test_reslice_with_supports_passes_the_decision_to_the_slicer(tmp_path, monkeypatch):
    jp, gcode = _warned_slice(tmp_path, monkeypatch)
    seen = {}
    real = orca_adapter.slice_model

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)
    monkeypatch.setattr(orca_adapter, 'slice_model', spy)
    mj.record_support_decision(jp.parent, True)
    mj.slice_job(jp)
    assert seen.get('supports') is True
    job = json.loads(jp.read_text())
    assert slice_warnings.support_finding(job) is None
    assert job['provenance']['support_decided'] is True


def _warned_reslice(tmp_path, monkeypatch, orientation):
    """Re-slice the warned fixture job with an orientation on the job,
    capturing the exact kwargs the slicer received."""
    seen = {}

    def slice_fake(src, folder, st, brim, color_decisions, filament_slot=None, copies=1, **kw):
        seen.update(kw)
        folder.mkdir(parents=True)
        out = folder / 'plate_1.gcode'
        out.write_text(json.dumps(color_decisions, sort_keys=True))
        shutil.copy(FIXTURE, folder / 'result.json')
        return out, {'offline_test': True,
                     **slice_warnings.provenance_fields(folder, {'enable_support': '0'}, kw.get('supports'))}, []
    jp, st, gcode = setup_job(tmp_path, monkeypatch)
    job = json.loads(jp.read_text())
    job['orientation'] = orientation
    jp.write_text(json.dumps(job))
    monkeypatch.setattr(orca_adapter, 'slice_model', slice_fake)
    return jp, Path(mj.slice_job(jp)), seen


_IDENTITY_ROTATION = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]


def test_rotation_without_a_support_choice_leaves_the_question_open(tmp_path, monkeypatch):
    # Regression (#916): a rotate edit adds a rotation without a supports
    # value; slicing must not turn that absence into supports=False with
    # support_decided=True, which suppressed the support question.
    jp, gcode, seen = _warned_reslice(tmp_path, monkeypatch,
                                      {'rotation': _IDENTITY_ROTATION})
    assert seen.get('supports') is None
    job = json.loads(jp.read_text())
    assert job['provenance']['support_decided'] is False
    assert slice_warnings.support_finding(job) is not None
    with pytest.raises(RuntimeError, match='supports decision needed'):
        mj.require_current_artifact(job, gcode)


def test_rotation_keeps_an_existing_support_choice(tmp_path, monkeypatch):
    jp, gcode, seen = _warned_reslice(
        tmp_path, monkeypatch, {'rotation': _IDENTITY_ROTATION, 'supports': False})
    assert seen.get('supports') is False
    job = json.loads(jp.read_text())
    assert job['provenance']['support_decided'] is True
    assert slice_warnings.support_finding(job) is None
    jp, gcode, seen = _warned_reslice(
        tmp_path, monkeypatch, {'rotation': _IDENTITY_ROTATION, 'supports': True})
    assert seen.get('supports') is True


def test_job_fit_leaves_supports_unresolved_after_a_plain_rotation():
    # The per-plate fit channel carried the same forced False (#916).
    _fit, supports, _brim = mj._job_fit(
        {'scale': 1.0, 'orientation': {'rotation': _IDENTITY_ROTATION}}, None)
    assert supports is None
    _fit, supports, _brim = mj._job_fit(
        {'scale': 1.0, 'orientation': {'rotation': _IDENTITY_ROTATION, 'supports': False}}, None)
    assert supports is False
    _fit, supports, _brim = mj._job_fit(
        {'scale': 1.0, 'orientation': {'rotation': _IDENTITY_ROTATION},
         'support_decision': {'supports': True}}, None)
    assert supports is True


def test_support_kw_carries_the_operator_choice_into_every_branch():
    assert mj._support_kw({}) == {}
    assert mj._support_kw({}, False) == {'supports': False}
    assert mj._support_kw({'support_decision': {'supports': True}}) == {'supports': True}
    # "Re-slice with supports" wins over a path default of off (it came after the warning).
    assert mj._support_kw({'support_decision': {'supports': True}}, False) == {'supports': True}
    assert mj._support_kw({'support_decision': {'supports': False}}) == {'supports': False}
    # A path's own recorded choice (reorientation offer) is kept when the operator said no supports.
    assert mj._support_kw({'support_decision': {'supports': False}}, True) == {'supports': True}


def _slice_model_calls(module):
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'slice_model':
            yield node


def test_every_job_slicing_path_passes_the_support_decision():
    """Merged, multi-tool, painted, per-plate, split and single paths all reach Orca with it."""
    import ast
    missing = []
    for call in _slice_model_calls(mj):
        if any(k.arg == 'first_layer' for k in call.keywords):
            continue  # first-layer probe, not a printed job artifact
        carried = any(k.arg is None and isinstance(k.value, ast.Call)
                      and getattr(k.value.func, 'id', None) == '_support_kw' for k in call.keywords)
        carried = carried or any(k.arg is None and getattr(k.value, 'id', None) == 'scale_kw'
                                 for k in call.keywords)
        if not carried:
            missing.append(call.lineno)
    assert not missing, f'slice_model calls without the support decision at model_job.py lines {missing}'


def test_chosen_supports_never_print_an_unsupported_artifact(tmp_path, monkeypatch):
    jp, gcode = _warned_slice(tmp_path, monkeypatch)
    job = mj.record_support_decision(jp.parent, True)
    job['provenance']['supports_on'] = False  # a branch that dropped the decision
    with pytest.raises(RuntimeError, match='sliced without them'):
        mj.require_current_artifact(job, gcode)
    assert slice_warnings.support_mismatch({'support_decision': {'supports': False},
                                            'provenance': {'supports_on': False}}) == []
    assert slice_warnings.support_mismatch({'support_decision': {'supports': True},
                                            'provenance': {}}) == []  # pre-warning rows


def test_support_question_is_answerable_only_by_print_approvers():
    import ast
    import inspect
    from print_handy.core import prepared_intake
    tree = ast.parse(inspect.getsource(prepared_intake))
    asks = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, 'id', None) == '_ask'
            and any(isinstance(a, ast.Call) and getattr(a.func, 'attr', None) == 'question'
                    and getattr(a.func.value, 'id', None) == 'slice_warnings' for a in n.args)]
    assert asks, 'support question not found'
    for call in asks:
        assert any(k.arg == 'approval' and getattr(k.value, 'value', None) is True for k in call.keywords)


def _prepare_link_tree():
    import ast
    import inspect
    from print_handy.core import prepared_intake
    return ast.parse(inspect.getsource(prepared_intake))


def test_support_reslice_can_be_stopped():
    """The re-slice is pre-upload, so its progress offers Stop like the first slice."""
    import ast
    calls = [n for n in ast.walk(_prepare_link_tree()) if isinstance(n, ast.Call)
             and getattr(n.func, 'id', None) == '_with_stop'
             and any(isinstance(a, ast.Constant) and str(a.value).startswith('Re-slicing with supports')
                     for a in n.args)]
    assert calls, 'support re-slice not found'
    for call in calls:
        assert any(k.arg == 'allow_stop' and getattr(k.value, 'value', None) is True for k in call.keywords)


def test_refused_plates_are_read_from_the_final_slice():
    """A re-slice with supports can refuse different plates; disclose and accept those, not the first slice's."""
    import ast
    tree = _prepare_link_tree()
    # prepare_link is now a thin wrapper (the yolo dispatch); the intake
    # body it delegates to is _prepare_link, which is where the re-slice
    # and the refused-plate read live.
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == '_prepare_link')
    reads = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Assign)
             and any(getattr(t, 'id', None) == 'refused_plates' for t in n.targets)]
    reslices = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
                and getattr(n.func, 'id', None) == '_with_stop'
                and any(isinstance(a, ast.Constant) and str(a.value).startswith('Re-slicing with supports')
                        for a in n.args)]
    assert reads and reslices
    assert min(reads) > max(reslices), 'refused_plates is read before the support re-slice'


def test_every_job_slicing_path_passes_the_chat_edit_settings():
    """Codex P2 on #908: layer height, infill and support type edits reach
    Orca on the merged, painted, multi-tool and per-plate paths too."""
    missing = []
    for call in _slice_model_calls(mj):
        if any(k.arg == 'first_layer' for k in call.keywords):
            continue
        carried = any(k.arg is None and isinstance(k.value, ast.Call)
                      and getattr(k.value.func, 'id', None) == '_edit_kw' for k in call.keywords)
        carried = carried or any(k.arg is None and getattr(k.value, 'id', None) == 'scale_kw'
                                 for k in call.keywords)
        if not carried:
            missing.append(call.lineno)
    assert not missing, f'slice_model calls without the edit settings at model_job.py lines {missing}'


def test_edit_kw_and_support_kw_carry_the_chat_edit():
    from print_handy.core import edit_plan as ep
    job = {'scale': 1.0, 'orientation': {}}
    assert mj._edit_kw(job) == {}
    ep.apply_plan(job, {'kind': 'supports', 'enabled': True, 'type': 'tree'})
    assert mj._edit_kw(job) == {'edits': {'support_type': 'tree'}}
    assert mj._support_kw(job) == {'supports': True}
    # The operator's own support decision still wins over the edit.
    job['support_decision'] = {'supports': False}
    assert mj._support_kw(job) == {'supports': False}
