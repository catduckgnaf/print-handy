"""Mapped native start: slow AD5X preparation is reported as accepted/preparing, not a failure."""
import json
from tests.bound_names import setattr_bound
from print_handy.jobs import common as jobs_common
from print_handy.launch import print_now as launch_print_now


def _status(ext, bed):
    return {'result': {'status': {'extruder': {'target': ext}, 'heater_bed': {'target': bed}}}}


def test_preparation_evidence_reads_heater_targets(monkeypatch):
    setattr_bound(monkeypatch, jobs_common, '_mr', lambda st, path, **k: _status(0, 60))
    assert launch_print_now._preparation_evidence({}) is True
    setattr_bound(monkeypatch, jobs_common, '_mr', lambda st, path, **k: _status(0, 0))
    assert launch_print_now._preparation_evidence({}) is False
    def boom(*a, **k): raise OSError('offline')
    setattr_bound(monkeypatch, jobs_common, '_mr', boom)
    assert launch_print_now._preparation_evidence({}) is None
