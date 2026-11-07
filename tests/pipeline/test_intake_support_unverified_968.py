"""Issue #968: an unverified support-coverage result must ask, not proceed.

#966 turns a coverage check that cannot run into a named ``unverified``
result, recorded in the receipt and logged -- but the job still reached
the printer with no question. These tests pin the policy change: an
unverified result routes to the same approval-gated Print anyway /
Cancel question #964 added for coverage failures, the question names
why coverage could not be verified, and a waiver is recorded in
``support_override`` the same way (``via: yolo`` under yolo, per #965).
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy.core import model_job, prepared_intake, support_clearance, yolo
from .helpers import external
from .test_color_intake import state
from .test_intake_support_precheck import Adapter, env, run_flow, _warning_slice, _coverage_only_report  # noqa: F401

print_next = external("print_next")

UNREADABLE_REASON = 'plate 1 G-code unreadable (plate_1_supports.gcode): gone'


def _unverified_result(reason=UNREADABLE_REASON):
    return {'checked': False, 'ok': True, 'regions': [],
            'unverified': [{'plate': 1, 'reason': reason}],
            'plate_mode': 'single', 'plates': []}


def _stub_unverified(monkeypatch, reason=UNREADABLE_REASON):
    monkeypatch.setattr(support_clearance, 'verify_coverage_by_plate',
                        lambda regions, plates: _unverified_result(reason))


def _coverage_only_flow(env, monkeypatch):
    monkeypatch.setattr(support_clearance, 'safe_analyze', _coverage_only_report)
    monkeypatch.setattr(model_job, 'slice_job', _warning_slice(env))


@pytest.mark.parametrize('pick, status', [(0, 'STARTED'), (1, 'SLICED_NOT_STARTED')])
def test_unverified_with_no_clearance_finding_asks(env, monkeypatch, pick, status):
    """Unverified alone (no coverage failure, no clearance finding) asks:
    Print anyway proceeds and records the waiver; Cancel starts nothing."""
    _coverage_only_flow(env, monkeypatch)
    _stub_unverified(monkeypatch)
    # one color (1), slot 1 (0), with supports (0), then the override question
    adapter, result, receipt = run_flow(env, [1, 0, 0, pick])
    q = adapter.questions[-1]
    assert q['choices'] == [prepared_intake.PRINT_ANYWAY_LABEL, print_next.CANCEL_LABEL]
    assert "Support coverage couldn't be verified:" in q['question']
    assert UNREADABLE_REASON in q['question']
    assert 'under 1.0 mm' not in q['question']
    assert receipt['support_coverage']['unverified'][0]['reason'] == UNREADABLE_REASON
    assert receipt['status'] == status
    if pick == 0:
        override = receipt['support_override']
        assert override['via'] == 'print_anyway'
        assert override['support_coverage'] is True
        assert override['low_clearance'] is False
        assert override['gcode_sha256'] == receipt['gcode_sha256']
    else:
        assert 'support_override' not in receipt
        assert env.starts == []
        assert result['reason'] == 'support coverage unverified'


def test_unverified_on_the_resliced_slice_asks(env, monkeypatch):
    """The re-slice-with-supports path asks too, not just the first slice."""
    _coverage_only_flow(env, monkeypatch)
    _stub_unverified(monkeypatch)
    # one color (1), slot 1 (0), slice only (2), re-slice with supports (0),
    # Print anyway at the unverified question (0), start (0)
    adapter, result, receipt = run_flow(env, [1, 0, 2, 0, 0, 0])
    asked = [q for q in adapter.questions
             if q['choices'] == [prepared_intake.PRINT_ANYWAY_LABEL, print_next.CANCEL_LABEL]]
    assert len(asked) == 1
    assert "Support coverage couldn't be verified:" in asked[0]['question']
    assert receipt['status'] == 'STARTED'
    assert receipt['support_override']['support_coverage'] is True


def test_unverified_source_bounds_unreadable_asks(env, monkeypatch):
    """A source whose bounds cannot be read carries an unverified_reason
    per plate (#966); that path asks and names the bounds failure."""
    _coverage_only_flow(env, monkeypatch)

    def _no_leaves(path):
        raise OSError('cannot read source')
    monkeypatch.setattr(support_clearance, 'leaves', _no_leaves)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 0])
    q = adapter.questions[-1]
    assert q['choices'] == [prepared_intake.PRINT_ANYWAY_LABEL, print_next.CANCEL_LABEL]
    assert "Support coverage couldn't be verified:" in q['question']
    assert 'source bounds unreadable' in q['question']
    assert receipt['status'] == 'STARTED'
    assert receipt['support_override']['support_coverage'] is True


def test_unverified_coverage_check_raising_asks(env, monkeypatch):
    """A coverage check that raises comes back as a named unverified
    entry from verify_coverage_by_plate (#966); that path asks."""
    _coverage_only_flow(env, monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError('kaboom')
    monkeypatch.setattr(support_clearance, 'verify_support_coverage', _boom)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 1])
    q = adapter.questions[-1]
    assert q['choices'] == [prepared_intake.PRINT_ANYWAY_LABEL, print_next.CANCEL_LABEL]
    assert "Support coverage couldn't be verified:" in q['question']
    assert 'coverage check failed: kaboom' in q['question']
    assert receipt['status'] == 'SLICED_NOT_STARTED'
    assert 'support_override' not in receipt
    assert env.starts == []


def test_unverified_unreadable_plate_file_asks_and_cancel_stops(env, monkeypatch):
    """A plate file that cannot be read at coverage time is unverified;
    cancelling the question leaves the job unstarted."""
    _coverage_only_flow(env, monkeypatch)
    real_sha256 = prepared_intake.delivery.sha256

    def _sha256_then_delete(path):
        digest = real_sha256(path)
        if str(path).endswith('_supports.gcode'):
            Path(path).unlink()
        return digest
    monkeypatch.setattr(prepared_intake.delivery, 'sha256', _sha256_then_delete)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 1])
    q = adapter.questions[-1]
    assert q['choices'] == [prepared_intake.PRINT_ANYWAY_LABEL, print_next.CANCEL_LABEL]
    assert "Support coverage couldn't be verified:" in q['question']
    assert 'G-code unreadable' in q['question']
    assert receipt['status'] == 'SLICED_NOT_STARTED'
    assert 'support_override' not in receipt
    assert env.starts == []


def _plate_text(name, bounds, support_points):
    (x0, y0), (x1, y1) = bounds
    lines = [f'; printing object {name}', ';TYPE:Outer wall',
             f'G1 X{x0} Y{y0} E1', f'G1 X{x1} Y{y1} E1', f'; stop printing object {name}',
             ';TYPE:Support', 'G1 Z4.8']
    lines += [f'G1 X{x:.1f} Y{y:.1f} E1.0' for x, y in support_points]
    return '\n'.join(lines) + '\n'


def test_multi_plate_part_matching_no_object_instance_is_unverified():
    """Producer half of the multi-plate path: every plate carries object
    markers, and a target part matches no instance on any plate."""
    gamma = {'kind': 'supportable_overhang', 'object': 'Gamma', 'part': 'Gamma',
             'bbox_mm': [[0.0, 0.0], [9.0, 9.0]], 'z_mm': {'min': 5.0, 'max': 5.0}}
    pts = [(1.5 + 3 * i, 1.5 + 3 * j) for i in range(3) for j in range(3)]
    plates = [{'gcode': _plate_text('Alpha', ((0, 0), (9, 9)), pts), 'source_bounds': (0.0, 0.0, 9.0, 9.0)},
              {'gcode': _plate_text('Beta', ((100, 100), (109, 109)), pts), 'source_bounds': (0.0, 0.0, 9.0, 9.0)}]
    result = support_clearance.verify_coverage_by_plate([gamma], plates)
    assert result['plate_mode'] == 'per_plate'
    assert len(result['unverified']) == 1
    assert 'region Gamma matched no object instance on any plate' in result['unverified'][0]['reason']


def test_unverified_multi_plate_no_match_asks(env, monkeypatch):
    """Intake half: that multi-plate no-match unverified result asks."""
    _coverage_only_flow(env, monkeypatch)
    reason = 'region Gamma matched no object instance on any plate; its coverage is unverified'
    _stub_unverified(monkeypatch, reason=reason)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 0])
    q = adapter.questions[-1]
    assert q['choices'] == [prepared_intake.PRINT_ANYWAY_LABEL, print_next.CANCEL_LABEL]
    assert "Support coverage couldn't be verified:" in q['question']
    assert reason in q['question']
    assert receipt['status'] == 'STARTED'
    assert receipt['support_override']['support_coverage'] is True


def test_unverified_under_yolo_records_via_yolo(env, monkeypatch):
    """Under yolo the question is auto-answered with Print anyway and
    the waiver is recorded apart from a person's, as #965 records it."""
    _coverage_only_flow(env, monkeypatch)
    _stub_unverified(monkeypatch)
    monkeypatch.setattr(yolo, 'may_yolo', lambda user_id, platform='discord', cfg=None: True)
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u',
                       platform=S(value='discord')),
              message_id='m-968-yolo', text='https://www.printables.com/model/1-ghost yolo')
    adapter = Adapter([])
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    assert receipt['status'] == 'STARTED'
    override = receipt['support_override']
    assert override['via'] == 'yolo'
    assert override['support_coverage'] is True
    assert any(m.startswith('Yolo: ') and m.endswith('-> Print anyway') for m in adapter.sent)
