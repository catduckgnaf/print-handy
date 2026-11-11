"""Resolved temperatures are preserved with the sliced artifact (P2-275).

Slicing resolves the spool's temperatures (Spoolman filament record wins);
verification used to re-resolve them live, so a Spoolman outage at verify
time fell back to material defaults (220/60 against a file sliced at the
spool's 225/65) and refused a sound artifact. The slice provenance now
carries the resolved temperatures and the verify paths check against them.
"""
import pytest

from print_handy.core import model_job, printer_state

STATE = {'temps_c': {'nozzle': 210, 'bed': 60},
         'spool': {'material': 'PLA', 'spoolman_id': 7},
         'nozzle': {'diameter_mm': 0.4, 'type': 'hardened_steel'}}
SLICED_AT = {'nozzle': 225, 'bed': 65}
DEFAULTS = {'nozzle': 220, 'bed': 60}
GCODE = '; filament_type = PLA\nM190 S65\nM104 S225\nG1 X10 Y10\n'


class _OfflineAdapter:
    """Judges the file against the temperatures it was sliced at."""

    connection = 'offline'
    transport = None
    label = 'Offline test printer'

    def gcode_problems(self, text, state):
        temps = state['temps_c']
        if float(temps['nozzle']) == 225 and float(temps['bed']) == 65:
            return []
        return [f"nozzle {temps['nozzle']} != sliced 225"]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    path = tmp_path / 'plate_1.gcode'
    path.write_text(GCODE)
    monkeypatch.setattr(model_job.printer_state, 'check', lambda *a, **k: (True, [], {}))
    # Verify-time re-resolution sees Spoolman down: material defaults.
    monkeypatch.setattr(printer_state, 'gcode_temperatures',
                        lambda state, materials, fetch=None, text=None, **_: dict(DEFAULTS))
    return path


def test_verify_uses_the_recorded_slice_temperatures(rig):
    result = model_job.verify_gcode(rig, adapter=_OfflineAdapter(), state=dict(STATE),
                                    printer_id='p1', recorded_temps=dict(SLICED_AT))
    assert result['offline'] is True


def test_verify_without_recorded_temperatures_still_reresolves(rig):
    with pytest.raises(RuntimeError, match='nozzle 220'):
        model_job.verify_gcode(rig, adapter=_OfflineAdapter(), state=dict(STATE), printer_id='p1')


def test_routed_verify_reads_temperatures_from_the_job_provenance(rig):
    job = {'gcode': str(rig), 'provenance': {'temperatures': dict(SLICED_AT)}}
    result = model_job._verify_routed(rig, _OfflineAdapter(), dict(STATE), 'p1', job=job)
    assert result['offline'] is True


def test_routed_verify_reads_temperatures_from_a_split_jobs_plate_row(rig):
    job = {'gcode': '/elsewhere/other.gcode',
           'derived_plates': [{'gcode': str(rig), 'provenance': {'temperatures': dict(SLICED_AT)}}]}
    result = model_job._verify_routed(rig, _OfflineAdapter(), dict(STATE), 'p1', job=job)
    assert result['offline'] is True


def test_recorded_temperatures_lookup_edges(tmp_path):
    path = tmp_path / 'a.gcode'
    assert model_job._recorded_temperatures(None, path) is None
    assert model_job._recorded_temperatures({'gcode': str(path)}, path) is None
    assert model_job._recorded_temperatures(
        {'gcode': str(path), 'provenance': {'temperatures': 'hot'}}, path) is None
    assert model_job._recorded_temperatures(
        {'gcode': str(path), 'provenance': {'temperatures': dict(SLICED_AT)}}, path) == SLICED_AT


def test_with_gcode_temperatures_prefers_recorded_without_resolving(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError('recorded temperatures must not trigger a re-resolution')

    monkeypatch.setattr(printer_state, 'gcode_temperatures', forbidden)
    out = model_job._with_gcode_temperatures(dict(STATE), GCODE, recorded={'nozzle': '225', 'bed': 65})
    assert out['temps_c'] == {'nozzle': 225.0, 'bed': 65.0}


def test_with_gcode_temperatures_ignores_a_malformed_record(monkeypatch):
    monkeypatch.setattr(printer_state, 'gcode_temperatures',
                        lambda state, materials, fetch=None, text=None, **_: dict(DEFAULTS))
    out = model_job._with_gcode_temperatures(dict(STATE), GCODE, recorded={'nozzle': 'hot'})
    assert out['temps_c'] == DEFAULTS
