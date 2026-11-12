"""Status voice wiring (#1326) for the support-clearance fixes: rotation and mesh_repair.

Same contract as test_status_voice_wiring.py, on this suite's own intake
fixture: the quip follows the factual notice, and an empty quip leaves the
notice byte-identical.
"""
import pytest

from print_handy.core import model_job
from .test_intake_support_precheck import V12_FIXTURE, _copy, env, run_flow  # noqa: F401  (pytest fixture)
from .test_status_voice_wiring import seed


# rotation and mesh_repair: the posts after a chosen support-clearance fix.

REORIENTED = ('Reoriented: laid the largest face flat. The support question will be evaluated '
              'again for the new orientation.')


@pytest.mark.parametrize('speak', [True, False])
def test_reorient_notice_carries_the_rotation_quip(env, monkeypatch, speak):
    seed(monkeypatch, speak)
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy(V12_FIXTURE, dest))
    from print_handy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 1)
    adapter, _result, _receipt = run_flow(env, [1, 0, 4, 8])
    assert (REORIENTED + (' [rotation quip]' if speak else '')) in adapter.sent


@pytest.mark.parametrize('speak', [True, False])
def test_extend_underside_notice_carries_the_mesh_repair_quip(env, monkeypatch, speak):
    seed(monkeypatch, speak)
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy(V12_FIXTURE, dest))
    from print_handy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 1)
    adapter, _result, _receipt = run_flow(env, [1, 0, 2, 2])
    notice = [m for m in adapter.sent if m.startswith('Extended underside to touch build plate')]
    assert len(notice) == 1
    assert notice[0].endswith('Re-analyzing... [mesh_repair quip]' if speak else 'Re-analyzing...')
