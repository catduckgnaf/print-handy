"""Orca support warnings: parsed on success, gate the launch until the operator decides."""
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
