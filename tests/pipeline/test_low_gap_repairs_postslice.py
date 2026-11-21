"""A measured low gap is offered its repair at Ready and again after the slice.

Incident shape (synthetic, no private model): a 131 mm bridge whose underside
sits 0.562 mm over the plate, so Orca also reports floating regions. Supports
cannot reach the gap. At Ready the repair leads; at the post-slice gate the
same repairs are buttons in the same intake (no AI, issue #1373), the repaired
copy is re-sliced, and every gate runs again: the repair waives nothing, grants
no approval and starts nothing.
"""
import json
from pathlib import Path

import pytest

from print_handy.core import ai_fix, model_job, orca_adapter, prepared_intake, support_clearance
from print_handy.intake import support_flow

from .test_intake_support_precheck import _copy, _warning_slice, env, run_flow  # noqa: F401
from .test_support_clearance_phase2 import box, write_3mf

print_next = prepared_intake.print_next
GAP_MM = 0.562


def bridge_file(tmp_path, gap=GAP_MM, name='bridge.3mf'):
    """Two 4 mm feet and a 131 mm deck whose underside is ``gap`` mm over the plate."""
    tris = box(0, 0, gap, 131, 10, 6) + box(0, 0, 0, 4, 10, gap) + box(127, 0, 0, 131, 10, gap)
    return write_3mf(tmp_path / name, [('Body', tris, '', None)])


@pytest.fixture
def bridge(env, tmp_path, monkeypatch):  # noqa: F811
    src = bridge_file(tmp_path / 'src_dir' if (tmp_path / 'src_dir').mkdir() is None else tmp_path)
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy(src, dest))
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 1)
    monkeypatch.setattr(model_job, 'slice_job', _warning_slice(env))
    # No AI anywhere: a call would be the disabled intake fix coming back (#1373).
    def _no_ai(*a, **k):
        raise AssertionError('the AI fix must not be used for a deterministic repair')
    monkeypatch.setattr(ai_fix, 'propose_fix', _no_ai)
    env.src_bytes = Path(src).read_bytes()
    return env


def _ready(adapter):
    return next(q for q in adapter.questions if q['question'].startswith('Ready to slice'))


def test_the_synthetic_fixture_is_the_real_shape():
    import tempfile
    path = bridge_file(Path(tempfile.mkdtemp()))
    rep = support_clearance.analyze(str(path), first_layer_mm=0.2)
    f = next(f for f in rep['findings'] if f['code'] == support_clearance.CODE_CLEARANCE)
    assert f['over'] == 'plate' and f['clearance_mm']['min'] == pytest.approx(GAP_MM, abs=1e-3)
    assert f['span_mm'] > 100 and {x['fix'] for x in f['fixes']} >= {'extend_underside', 'sink'}


def test_ready_leads_with_the_repair_and_does_not_say_supports_fix_both(bridge):
    # color White PLA (0), then Cancel (last of the 10 buttons)
    adapter, _result, _receipt = run_flow(bridge, [0, 9])
    ready = _ready(adapter)
    choices, text = ready['choices'], ready['question']
    assert choices[0] == 'Extend underside to plate' and choices[1] == 'Flatten (sink) to plate'
    assert 'Slice and print with supports (recommended)' not in choices
    # Two separate findings, each named for what it is.
    assert 'the slicer found 1 floating region that needs support' in text
    assert 'Measured low gap: an underside sits 0.562 mm above the plate' in text
    assert '- Recommended: Extend underside to plate or Flatten (sink) to plate' in text
    assert 'they cannot close the measured gap' in text
    assert '(recommended: the slicer found' not in text
    assert text.index('Recommended: Extend') < text.index('- Slice and print without supports')


def test_post_slice_gate_offers_the_repairs_and_the_repair_changes_nothing_else(bridge):
    # color (0), with supports (3, after the two repairs and "without"), then at the
    # gate Extend (0); the repaired slice's own Ready/gate follow, answered Cancel.
    adapter, _result, receipt = run_flow(bridge, [0, 3, 0, 2])
    gate = next(q for q in adapter.questions if 'Nothing has been sent to the printer yet.' in q['question'])
    assert gate['choices'] == ['Extend underside to plate', 'Flatten (sink) to plate',
                               prepared_intake.PRINT_ANYWAY_LABEL, print_next.CANCEL_LABEL]
    assert 'post the model again' not in gate['question']
    assert 'Measured low gap' in gate['question']
    # The repair re-sliced a derived copy and nothing started.
    assert any('Extended underside to touch build plate' in m or 'Re-slicing the repaired model' in m
               for m in adapter.log)
    job = json.loads(Path(receipt['job_json']).read_text())
    assert job['downloaded_file'] != str(Path(env_source(bridge, job)))  # a derived copy
    assert model_job.file_sha256(job['downloaded_file']) != bridge_sha(bridge)
    assert Path(env_source(bridge, job)).read_bytes() == bridge.src_bytes  # original untouched
    assert job['color_decisions']['source_sha256'] == model_job.file_sha256(job['downloaded_file'])
    assert job['color_decisions']['selection']['slot'] == 1  # selected filament kept
    assert not job.get('clearance_acknowledgement', {}).get('acknowledged')
    assert receipt['status'] == 'SLICED_NOT_STARTED' and receipt.get('started') is not True
    assert receipt['geometry_repair'] == {**receipt['geometry_repair'], 'kind': 'extend', 'deterministic': True}
    assert 'support_override' not in receipt
    assert job['ai_fixes'][-1]['lane'] == 'gap_repair'


def env_source(env_, job):
    originals = [e for e in (job.get('ai_fixes') or [])]
    return originals[0]['source_file_before'] if originals else job['downloaded_file']


def bridge_sha(env_):
    return model_job.sha256_bytes(env_.src_bytes) if hasattr(model_job, 'sha256_bytes') \
        else __import__('hashlib').sha256(env_.src_bytes).hexdigest()


def test_the_repair_does_not_waive_the_separate_coverage_failure(bridge, monkeypatch):
    calls = []

    def verify(regions, plates):
        calls.append(1)
        return {'checked': True, 'ok': False, 'unverified': [], 'plate_mode': 'single', 'plates': [],
                'regions': [{'coverage': 0.0, 'part': 'Body', 'z_mm': {'min': 6.0}}]}
    monkeypatch.setattr(support_clearance, 'verify_coverage_by_plate', verify)
    # with supports (3), Extend (0); the re-sliced gate is graded again and asks again:
    # only coverage is left (the gap is closed), so its buttons are Print anyway / Cancel.
    adapter, _result, receipt = run_flow(bridge, [0, 3, 0, 1])
    assert len(calls) == 2, 'coverage must be graded again on the repaired slice'
    last = adapter.questions[-1]
    assert last['choices'] == [prepared_intake.PRINT_ANYWAY_LABEL, print_next.CANCEL_LABEL]
    assert 'found no support of an overhang on Body' in last['question']
    assert 'under 1.0 mm' not in last['question'] and 'Measured low gap' not in last['question']
    assert receipt['status'] == 'SLICED_NOT_STARTED' and receipt.get('started') is not True
    assert 'support_override' not in receipt


def test_repaired_slice_still_needs_the_ordinary_start_approval(bridge, monkeypatch):
    monkeypatch.setattr(support_clearance, 'verify_coverage_by_plate',
                        lambda regions, plates: {'checked': True, 'ok': True, 'unverified': []})
    # with supports (3), Extend (0) at the gate (the repaired slice passes every
    # check), then Cancel at the ordinary start question.
    adapter, _result, receipt = run_flow(bridge, [0, 3, 0, 2])
    assert any('The repaired slice passes the support checks. Nothing was waived' in m for m in adapter.log)
    start_q = adapter.questions[-1]
    # The ordinary start approval, asked again for the repaired slice.
    assert start_q['choices'][0].startswith('Start next') and "Done, don't print" in start_q['choices']
    assert 'Start uploads and prints it immediately' in start_q['question'] or 'Start this exact plate' in start_q['question']
    assert receipt.get('started') is not True and receipt['status'] == 'SLICED_NOT_STARTED'
    assert 'support_override' not in receipt and 'bed_clear_approval' not in receipt
    job = json.loads(Path(receipt['job_json']).read_text())
    assert not job.get('clearance_acknowledgement', {}).get('acknowledged')


def test_a_gap_over_another_part_has_no_repair_and_keeps_the_old_gate(env, tmp_path, monkeypatch):  # noqa: F811
    fixture = Path(__file__).parent.parent / 'fixtures' / 'support_clearance' / '06_part_gap_040_large.3mf'
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy(fixture, dest))
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 1)
    monkeypatch.setattr(model_job, 'slice_job', _warning_slice(env))
    monkeypatch.setattr(support_clearance, 'verify_coverage_by_plate',
                        lambda regions, plates: {'checked': True, 'ok': True, 'unverified': []})
    rep = support_clearance.analyze(str(fixture), first_layer_mm=0.2)
    actions, _ = support_flow.repair_actions(rep, turned_job=False)
    assert not support_flow.gap_repairs(actions)
    # Two-part model: color each part (0, 0, 0), then Cancel at Ready (last of 8).
    adapter, _result, _receipt = run_flow(env, [0, 0, 0, 7])
    ready = _ready(adapter)
    # No plate under it, so no repair: the Ready menu is the one it always was.
    assert 'Extend underside to plate' not in ready['choices'] and 'Flatten (sink) to plate' not in ready['choices']
    assert ready['choices'][0] == 'Slice and print with supports (recommended)'
    assert 'Measured low gap' not in ready['question'] and '- Recommended:' not in ready['question']


def test_a_turned_job_gets_no_extend_or_sink():
    rep = support_clearance.analyze(str(bridge_file(Path(__import__('tempfile').mkdtemp()))),
                                    first_layer_mm=0.2)
    assert support_flow.gap_repairs(support_flow.repair_actions(rep, turned_job=True)[0]) == []
    assert [a['kind'] for a in support_flow.repair_actions(rep, turned_job=False)[0]] == ['extend', 'sink']
    assert support_flow.repair_actions({'risky': False, 'findings': []}, turned_job=False) == ([], [])
