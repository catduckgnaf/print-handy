"""Fit-probe and slice-envelope parity for representative footprints."""
import json
from types import SimpleNamespace
import zipfile

import pytest

from print_handy.core import orca_adapter as oa
from print_handy.jobs import slice as slice_jobs


@pytest.mark.parametrize('dims,bed,height,margin,expected', [
    ((231, 74, 85), (220, 220), 220, 1, False),
    ((180, 150, 40), (220, 220), 220, 1, True),
    ((20, 20, 221), (220, 220), 220, 1, False),
    ((200, 200, 20), (220, 220), 220, 1, True),
    ((219, 100, 20), (220, 220), 220, 1, False),
])
def test_fit_envelope_precheck(dims, bed, height, margin, expected):
    assert oa.fit_envelope_fits(dims, bed, height, margin_mm=margin) is expected


def test_fit_envelope_uses_scale_and_optional_xy_check():
    assert oa.fit_envelope_fits((150, 74, 85), (220, 220), 220, margin_mm=1)
    assert not oa.fit_envelope_fits((150, 74, 85), (220, 220), 220, margin_mm=1, scale=1.5)
    assert oa.fit_envelope_fits((231, 74, 85), (220, 220), 220, check_xy=False)


@pytest.fixture
def fake_orca(monkeypatch, tmp_path):
    templates = tmp_path / 'templates'
    templates.mkdir()
    for name in ('machine', 'process', 'filament'):
        (templates / f'{name}.json').write_text('{}')
    monkeypatch.setattr(oa, 'TEMPLATES', templates)
    monkeypatch.setattr(oa, '_fit_exclusion_polygon', lambda state=None: ())
    shape = {'dims': (20, 20, 10), 'copies': 1}
    monkeypatch.setattr(oa, '_footprint', lambda *a: (
        [(0, 0), (shape['dims'][0], 0), shape['dims'][:2], (0, shape['dims'][1])],
        0, shape['dims'][2]))

    def spawn(cmd, timeout, cwd):
        with zipfile.ZipFile(cwd / 'probe.3mf', 'w') as archive:
            archive.writestr('Metadata/model_settings.config',
                             '<config><plate><metadata key="plater_id" value="1"/></plate></config>')
        return SimpleNamespace(returncode=0, stderr='', stdout='')

    monkeypatch.setattr(oa, '_spawn_slicer', spawn)
    def export_geometry(*args, **kwargs):
        x, y, z = shape['dims']
        return {'build_bounds_mm': [(110-x/2, 110+x/2), (110-y/2, 110+y/2), (0, z)]}
    monkeypatch.setattr(oa, 'export_geometry', export_geometry)
    return tmp_path / 'part.stl', shape


@pytest.mark.parametrize('dims,rotation,copy_limit', [
    ((231, 74, 85), None, 1),
    ((208.5, 208.5, 85), [0.707106781, -0.707106781, 0, 0.707106781, 0.707106781, 0, 0, 0, 1], 1),
    ((219.5, 100, 20), None, 1),
    ((20, 20, 221), None, 1),
    ((180, 150, 40), None, 1),  # centered probe ignores authored offset
    ((100, 100, 20), None, 2),  # multi-copy arrangement
])
def test_probe_and_slice_precheck_parity(fake_orca, dims, rotation, copy_limit):
    source, shape = fake_orca
    shape['dims'] = dims
    state = {'bed_mm': [220, 220], 'max_print_height_mm': 220}
    probe_accepts = oa.orca_plate_capacity(source, state, copy_limit=copy_limit,
                                           rotation=rotation) > 0
    slice_accepts = slice_jobs._volume_precheck(*dims, 1.0, state)
    assert not probe_accepts or slice_accepts
    assert slice_accepts or not probe_accepts
