"""Issue #1151 remainder: a clearance acknowledgement given for one artifact
must not be reused for the artifact a reslice-with-supports produces.

Experiment (run on the code before this fix): at the Ready menu the operator
chose "Print anyway (accept the risk)" for a low-clearance model, which only
records a *pending* acknowledgement. At the post-slice support prompt the
operator then chose "Re-slice with supports". The reslice produced a new
artifact, coverage verified, and the pending acknowledgement was silently
recorded against the NEW artifact's G-code hash -- the print started with no
question about the low-clearance finding for the artifact actually printing.
"""
import json
from pathlib import Path

import pytest

from print_handy.core import model_job, orca_adapter, prepared_intake, support_clearance
from .test_intake_support_precheck import env, run_flow, _warning_slice, V12_FIXTURE, _copy  # noqa: F401
print_next = prepared_intake.print_next


def _v12_warning_env(env, monkeypatch):
    monkeypatch.setattr(model_job, 'printables_download',
                        lambda job, chosen, dest: _copy(V12_FIXTURE, dest))
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 1)
    monkeypatch.setattr(model_job, 'slice_job', _warning_slice(env))
    # Coverage of the resliced artifact verifies cleanly, so the only gate
    # left for the new artifact is the low-clearance finding itself.
    monkeypatch.setattr(support_clearance, 'verify_coverage_by_plate',
                        lambda regions, plates: {'checked': True, 'ok': True, 'unverified': []})


def test_reslice_with_supports_requires_a_new_clearance_acknowledgement(env, monkeypatch):
    """Print-anyway waiver at Ready, then reslice-with-supports: the waiver
    was given for the first artifact, so the new artifact's low-clearance
    finding must be asked again (with buttons), not silently waived."""
    _v12_warning_env(env, monkeypatch)
    # color(1), slot(0), Print anyway at Ready(5), Re-slice with supports(0),
    # then Print anyway at the re-asked gate question(0).
    adapter, result, receipt = run_flow(env, [1, 0, 5, 0, 0])
    gate_q = adapter.questions[-1]
    assert gate_q['choices'] == [prepared_intake.PRINT_ANYWAY_LABEL, print_next.CANCEL_LABEL]
    assert 'under 1.0 mm' in gate_q['question']
    assert receipt['status'] == 'STARTED'
    # The acknowledgement that starts the print is the fresh one, bound to
    # the with-supports artifact the operator just waived for.
    job = json.loads(Path(receipt['job_json']).read_text())
    ack = job['clearance_acknowledgement']
    assert ack['acknowledged'] is True and ack['via'] == 'print_anyway'
    assert 'plate_1_supports.gcode' in job['gcode']
    assert ack['gcode_sha256'] == model_job.file_sha256(job['gcode'])


def test_reslice_with_supports_cancel_at_the_reasked_gate_starts_nothing(env, monkeypatch):
    """Cancelling the re-asked low-clearance question after a reslice leaves
    the slice on disk, starts nothing, and records no acknowledgement."""
    _v12_warning_env(env, monkeypatch)
    # color(1), slot(0), Print anyway at Ready(5), Re-slice with supports(0),
    # then Cancel at the re-asked gate question(1).
    adapter, result, receipt = run_flow(env, [1, 0, 5, 0, 1])
    gate_q = adapter.questions[-1]
    assert gate_q['choices'] == [prepared_intake.PRINT_ANYWAY_LABEL, print_next.CANCEL_LABEL]
    assert 'under 1.0 mm' in gate_q['question']
    assert receipt['status'] == 'SLICED_NOT_STARTED'
    assert env.starts == []
    job = json.loads(Path(receipt['job_json']).read_text())
    assert not job.get('clearance_acknowledgement', {}).get('acknowledged')
