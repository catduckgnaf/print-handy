"""Issue #978 intake tests: "Fix this for my printer" on the post-slice
support gates and on slice failure/refusal, plus the AI-disabled shape.

The heavy tier is faked at the ai_fix module seam (heavy_ready / propose_fix);
the gate and slice harnesses are the existing #964/#968 ones. What is pinned:
a fix offer appears only when the tier is ready; an applied fix re-slices and
the gates run again on the new slice; a passing fix records NO support
waiver (a fix is not a waiver, and never a yolo waiver); the fix confirmation
is not an approval question, while the gate and the start keep theirs.
"""
import json
from pathlib import Path

import pytest

from print_handy.core import ai_fix, model_job, prepared_intake, slice_failure, support_clearance
from .helpers import external
from .test_color_intake import state  # noqa: F401  (re-exported fixture state)
from .test_intake_support_precheck import Adapter, env, run_flow, _warning_slice, _coverage_only_report  # noqa: F401

print_next = external("print_next")


def _ai_on(monkeypatch, proposal):
    monkeypatch.setattr(ai_fix, 'heavy_ready', lambda cfg=None: True)
    monkeypatch.setattr(ai_fix, 'propose_fix', lambda problem, job=None, **kw: proposal)


def _coverage_sequence(monkeypatch):
    """Coverage verification that fails the first slice and passes the fixed one."""
    calls = []

    def verify(regions, plates):
        calls.append(plates)
        if len(calls) == 1:
            return {'checked': True, 'ok': False, 'unverified': [], 'plate_mode': 'single',
                    'plates': [], 'regions': [{'coverage': 0.4, 'part': 'Body'}]}
        return {'checked': True, 'ok': True, 'unverified': [], 'plate_mode': 'single',
                'plates': [], 'regions': [{'coverage': 1.0, 'part': 'Body'}]}
    monkeypatch.setattr(support_clearance, 'verify_coverage_by_plate', verify)
    return calls


def test_support_gate_failure_offers_fix_and_the_fix_reslices_with_gates_rerun(env, monkeypatch):
    monkeypatch.setattr(support_clearance, 'safe_analyze', _coverage_only_report)
    monkeypatch.setattr(model_job, 'slice_job', _warning_slice(env))
    calls = _coverage_sequence(monkeypatch)
    _ai_on(monkeypatch, ai_fix.FixProposal(
        plans=[{'kind': 'supports', 'enabled': True, 'type': 'tree'}], summary='use tree supports'))
    # one color (1), slot 1 (0), with supports (0), Fix (1) at the gate,
    # Apply the fix (0); the fixed slice passes and the print starts.
    adapter, result, receipt = run_flow(env, [1, 0, 0, 1, 0])
    gate = [q for q in adapter.questions if ai_fix.FIX_LABEL in (q.get('choices') or [])]
    assert len(gate) == 1
    assert gate[0]['choices'] == [prepared_intake.PRINT_ANYWAY_LABEL, ai_fix.FIX_LABEL,
                                  print_next.CANCEL_LABEL]
    assert 'do not cover every flagged region' in gate[0]['question']
    apply_q = [q for q in adapter.questions if ai_fix.APPLY_LABEL in (q.get('choices') or [])]
    assert len(apply_q) == 1
    assert 're-slice with tree supports' in apply_q[0]['question']  # every change shown first
    assert len(calls) >= 2  # the gates graded the fixed slice again
    assert receipt['status'] == 'STARTED'
    assert receipt['ai_fix']['changes'] == ['re-slice with tree supports']
    assert 'support_override' not in receipt  # a fix is not a waiver
    job = json.loads(Path(receipt['job_json']).read_text())
    assert [e['lane'] for e in job['edits']] == ['ai_fix']
    assert job['support_decision']['supports'] is True


def test_fix_confirmation_is_not_an_approval_question_but_gate_and_start_are(env, monkeypatch):
    """A non-approver may ask for a fix but can't start the print: the fix
    confirmation carries no approval mark, while the waiver gate and the
    start question keep theirs (the adapters refuse non-approver answers
    to approval-marked prompts)."""
    from print_handy.core import prompts
    marked = []
    real_mark = prompts.mark_approval
    monkeypatch.setattr(prompts, 'mark_approval',
                        lambda cid: (marked.append(str(cid)), real_mark(cid))[1])
    monkeypatch.setattr(support_clearance, 'safe_analyze', _coverage_only_report)
    monkeypatch.setattr(model_job, 'slice_job', _warning_slice(env))
    _coverage_sequence(monkeypatch)
    _ai_on(monkeypatch, ai_fix.FixProposal(plans=[{'kind': 'supports', 'enabled': True,
                                                   'type': 'tree'}]))
    adapter, result, receipt = run_flow(env, [1, 0, 0, 1, 0])
    by_label = {}
    for q in adapter.questions:
        for label in (ai_fix.FIX_LABEL, ai_fix.APPLY_LABEL, prepared_intake.PRINT_ANYWAY_LABEL,
                      print_next.START_LABEL):
            if label in (q.get('choices') or []):
                by_label.setdefault(label, q)
    assert by_label[ai_fix.FIX_LABEL]['clarify_id'] in marked      # the gate (a waiver question)
    assert by_label[ai_fix.APPLY_LABEL]['clarify_id'] not in marked  # the fix ask: anyone may answer
    assert receipt['status'] == 'STARTED'


def test_ai_disabled_gate_has_no_fix_button_and_the_note_instead(env, monkeypatch):
    """AI not configured: today's question, plus the one-line note; no fix."""
    monkeypatch.setattr(support_clearance, 'safe_analyze', _coverage_only_report)
    monkeypatch.setattr(model_job, 'slice_job', _warning_slice(env))
    _coverage_sequence(monkeypatch)
    # heavy_ready is NOT patched: the test config configures no heavy tier.
    assert ai_fix.heavy_ready() is False
    adapter, result, receipt = run_flow(env, [1, 0, 0, 1])
    q = adapter.questions[-1]
    assert q['choices'] == [prepared_intake.PRINT_ANYWAY_LABEL, print_next.CANCEL_LABEL]
    assert ai_fix.UNCONFIGURED_NOTE in q['question']
    assert receipt['status'] == 'SLICED_NOT_STARTED'
    assert 'ai_fix' not in receipt


# ------------------------------------------------------- slice failure / refusal

def test_wrong_nozzle_slice_refusal_offers_fix_and_reslices_on_the_job_profile(
        env, monkeypatch):
    """A slice refused for the wrong nozzle/bed offers the fix; the applied
    fix re-slices the same job (its own printer profile -- the harness job
    is the AD5X-profile intake job) and the print can then start through
    the normal fresh start question."""
    real_slice = model_job.slice_job
    calls = []

    def fail_once(job_path, *args, **kwargs):
        calls.append(str(job_path))
        if len(calls) == 1:
            raise slice_failure.SliceFailure(
                'Pre-slice guard blocked: the job targets a 0.4 mm nozzle on a '
                'different bed; no tuned profile matches', kind='guard')
        return real_slice(job_path, *args, **kwargs)

    monkeypatch.setattr(model_job, 'slice_job', fail_once)
    _ai_on(monkeypatch, ai_fix.FixProposal(plans=[{'kind': 'scale_percent', 'percent': 90.0}],
                                           summary='scale to fit the target bed'))
    # one color (1), slot 1 (0), slice and print (0), Fix (0), Apply (0), start (0)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 0, 0, 0], mid='fix-refusal')
    offered = [q for q in adapter.questions if ai_fix.FIX_LABEL in (q.get('choices') or [])]
    assert len(offered) == 1
    assert 'safety check rejected' in offered[0]['question']
    assert len(calls) == 2 and calls[0] == calls[1]  # re-sliced the same job/profile
    assert receipt['status'] == 'STARTED'
    assert receipt['ai_fix']['changes'] == ['scale the model to 90% and re-slice']
    job = json.loads(Path(receipt['job_json']).read_text())
    assert job['scale'] == pytest.approx(0.9)


# ------------------------------------------------- #978 review: acknowledgements

def _v12(monkeypatch):
    from .test_intake_support_precheck import V12_FIXTURE, _copy
    monkeypatch.setattr(model_job, 'printables_download',
                        lambda job, chosen, dest: _copy(V12_FIXTURE, dest))
    from print_handy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 1)
    return V12_FIXTURE


def test_fix_that_clears_low_clearance_records_no_acknowledgement(env, monkeypatch):
    """A fix that makes every gate pass waived nothing: no support_override
    and no clearance acknowledgement -- in particular never a print_anyway
    the operator did not give (#978 review, finding 1). The fixed pose's
    clearance report replaces the stale one, as the Reorient button does."""
    _v12(monkeypatch)
    _coverage_sequence(monkeypatch)
    clean = {'version': 1, 'risky': False, 'findings': [], 'objects': [], 'limitations': [],
             'regions': []}
    monkeypatch.setattr(support_clearance, 'rotated_report', lambda source, matrix: clean)
    _ai_on(monkeypatch, ai_fix.FixProposal(plans=[{'kind': 'lay_flat'}], summary='lay it flat'))
    # one color (1), slot 1 (0), with supports (0), Fix (1), Apply (0)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 1, 0])
    assert receipt['status'] == 'STARTED'
    assert 'support_override' not in receipt
    job = json.loads(Path(receipt['job_json']).read_text())
    assert 'clearance_acknowledgement' not in job
    assert job['support_clearance'] == clean
    assert any('passes the support checks. Nothing was waived.' in m for m in adapter.sent)


def test_print_anyway_after_an_unsuccessful_geometry_fix_is_a_print_anyway(env, monkeypatch):
    """A geometry fix that leaves the low clearance in place repaired
    nothing: the approver's Print anyway that follows is recorded as
    print_anyway, never as geometry_repair (#978 review, finding 2)."""
    fixture = _v12(monkeypatch)
    _coverage_sequence(monkeypatch)
    real_analyze = support_clearance.safe_analyze
    # The sunk copy still has its low underside (the fix was too small).
    monkeypatch.setattr(support_clearance, 'safe_analyze',
                        lambda path: real_analyze(fixture if '.fixed-' in str(path) else path))
    _ai_on(monkeypatch, ai_fix.FixProposal(geometry={'kind': 'sink', 'mm': 0.05},
                                           summary='sink a little'))
    # one color (1), slot 1 (0), with supports (0), Fix (1), Apply (0), Print anyway (0)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 1, 0, 0])
    gate = [q for q in adapter.questions if prepared_intake.PRINT_ANYWAY_LABEL in (q.get('choices') or [])]
    assert len(gate) == 2 and 'under 1.0 mm' in gate[1]['question']
    assert receipt['status'] == 'STARTED'
    assert receipt['support_override']['via'] == 'print_anyway'
    assert receipt['support_override']['low_clearance'] is True
    job = json.loads(Path(receipt['job_json']).read_text())
    ack = job['clearance_acknowledgement']
    assert ack['acknowledged'] is True and ack['via'] == 'print_anyway'
    assert job['ai_fixes'][0]['geometry'] == {'kind': 'sink', 'mm': 0.05}


# ------------------------------------------------- #978 follow-ups: budget, staging

def test_gate_offers_no_fix_once_the_job_budget_is_spent(env, monkeypatch):
    monkeypatch.setattr(support_clearance, 'safe_analyze', _coverage_only_report)
    monkeypatch.setattr(model_job, 'slice_job', _warning_slice(env))
    _coverage_sequence(monkeypatch)
    _ai_on(monkeypatch, ai_fix.FixProposal(plans=[{'kind': 'supports', 'enabled': True,
                                                   'type': 'tree'}]))
    monkeypatch.setattr(ai_fix, 'MAX_JOB_FIX_ATTEMPTS', 0)
    # one color (1), slot 1 (0), with supports (0), Cancel (1) at the gate
    adapter, result, receipt = run_flow(env, [1, 0, 0, 1])
    gate = adapter.questions[-1]
    assert gate['choices'] == [prepared_intake.PRINT_ANYWAY_LABEL, print_next.CANCEL_LABEL]
    assert ai_fix.out_of_attempts_message() in gate['question']
    assert ai_fix.UNCONFIGURED_NOTE not in gate['question']  # AI is set up; the budget is spent


def test_intake_fix_requests_are_counted_on_the_job_and_attributed(env, monkeypatch):
    monkeypatch.setattr(support_clearance, 'safe_analyze', _coverage_only_report)
    monkeypatch.setattr(model_job, 'slice_job', _warning_slice(env))
    _coverage_sequence(monkeypatch)
    _ai_on(monkeypatch, ai_fix.FixProposal(plans=[{'kind': 'supports', 'enabled': True,
                                                   'type': 'tree'}]))
    adapter, result, receipt = run_flow(env, [1, 0, 0, 1, 0])
    job = json.loads(Path(receipt['job_json']).read_text())
    assert [a['surface'] for a in job['ai_fix_attempts']] == ['intake']
    platform, _, user = job['ai_fix_attempts'][0]['actor'].partition(':')
    assert platform == 'discord' and user  # who asked, not just that someone did
    assert receipt['ai_fix']['actor'] == job['ai_fix_attempts'][0]['actor']


def test_a_declined_geometry_fix_leaves_no_copy_in_the_job_folder(env, monkeypatch):
    _v12(monkeypatch)
    _coverage_sequence(monkeypatch)
    _ai_on(monkeypatch, ai_fix.FixProposal(geometry={'kind': 'sink', 'mm': 0.05}))
    # one color (1), slot 1 (0), with supports (0), Fix (1), Cancel the fix (1), Cancel (2)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 1, 1, 2])
    folder = Path(receipt['job_json']).parent
    assert not list(folder.glob(ai_fix.STAGING_PREFIX + '*'))
    assert not list(folder.glob('*.fixed-*'))
    job = json.loads(Path(receipt['job_json']).read_text())
    assert 'ai_fixes' not in job
