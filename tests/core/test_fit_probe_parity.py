"""Residual #1152 fit parity through the production probe, with a fake Orca."""
import json
from types import SimpleNamespace
import zipfile

import pytest

from print_handy.core import color_geometry, model_job, orca_adapter as oa


@pytest.fixture
def probe(tmp_path, monkeypatch):
    templates = tmp_path / 'templates'
    templates.mkdir()
    for name in ('machine', 'process', 'filament'):
        (templates / f'{name}.json').write_text('{}')
    monkeypatch.setattr(oa, 'TEMPLATES', templates)
    monkeypatch.setattr(oa, '_fit_exclusion_polygon', lambda: ())
    monkeypatch.setattr(oa, '_footprint', lambda *a: ([(0, 0), (20, 0), (20, 20), (0, 20)], 0, 10))
    machines = []

    def spawn(cmd, timeout, cwd):
        machines.append(json.loads(open(cmd[cmd.index('--load-settings') + 1].split(';')[0]).read()))
        with zipfile.ZipFile(cwd / 'probe.3mf', 'w') as archive:
            archive.writestr('Metadata/model_settings.config',
                             '<config><plate><metadata key="plater_id" value="1"/></plate></config>')
        return SimpleNamespace(returncode=0, stderr='', stdout='')

    monkeypatch.setattr(oa, '_spawn_slicer', spawn)
    monkeypatch.setattr(oa, 'export_geometry', lambda *a: {'build_bounds_mm': [(100, 120), (100, 120), (0, 10)]})
    return tmp_path / 'part.stl', machines


@pytest.mark.parametrize('kind', ['outer_only', 'brim_ears', 'outer_and_inner', 'inner_only'])
def test_probe_machine_reserves_every_explicit_brim(probe, kind):
    source, machines = probe
    state = {'bed_mm': [220, 220], 'max_print_height_mm': 220,
             'resolved_retraction_mm': [0], 'resolved_hop_types': ['Normal Lift']}
    assert oa.orca_plate_capacity(source, state, copy_limit=1,
                                  brim_policy={'type': kind, 'width_mm': 5}) == 1
    assert machines[0]['printable_area'] == ['6.0x6.0', '214.0x6.0', '214.0x214.0', '6.0x214.0']


def test_probe_preserves_native_polygon_and_refuses_overlap(probe, monkeypatch):
    source, machines = probe
    polygon = ((0, 0), (110, 0), (110, 220), (0, 220))
    monkeypatch.setattr(oa, '_fit_exclusion_polygon', lambda: polygon)
    assert oa.orca_plate_capacity(source, {'bed_mm': [220, 220]}, copy_limit=1) == 0
    assert machines[0]['bed_exclude_area'] == ['0x0', '110x0', '110x220', '0x220']


def test_probe_accepts_nonoverlapping_exclusion(probe, monkeypatch):
    source, _ = probe
    monkeypatch.setattr(oa, '_fit_exclusion_polygon', lambda: ((0, 0), (18, 0), (18, 28), (0, 28)))
    assert oa.orca_plate_capacity(source, {'bed_mm': [220, 220]}, copy_limit=1) == 1


@pytest.mark.parametrize('height, expected', [(220.0, 1), (220.001, 0), (220.049, 0)])
def test_single_probe_uses_strict_slice_height(probe, monkeypatch, height, expected):
    source, machines = probe
    monkeypatch.setattr(oa, '_footprint', lambda *a: ([(0, 0), (20, 0), (20, 20), (0, 20)], 0, height))
    assert oa.orca_plate_capacity(source, {'bed_mm': [220, 220], 'max_print_height_mm': 220}, copy_limit=1) == expected
    if not expected:
        assert machines == []


@pytest.mark.parametrize('height, expected', [(220.0, 1), (220.001, 0), (220.049, 0)])
def test_multi_probe_uses_strict_slice_height(probe, monkeypatch, tmp_path, height, expected):
    source = tmp_path / 'multi.3mf'
    with zipfile.ZipFile(source, 'w'):
        pass
    monkeypatch.setattr(model_job, 'plate_manifest', lambda *a: {'plates': [{'object_ids': ['1']}]})
    monkeypatch.setattr(color_geometry, '_world_bounds', lambda *a: [[(0, 20), (0, 20), (0, height)]])
    assert oa._multi_plate_capacity(source, {'bed_mm': [220, 220], 'max_print_height_mm': 220}) == expected


def test_multi_probe_uses_polygon_rectangle_cover_once(probe, monkeypatch, tmp_path):
    source = tmp_path / 'multi.3mf'
    with zipfile.ZipFile(source, 'w'):
        pass
    monkeypatch.setattr(oa, '_fit_exclusion_polygon', lambda: ((0, 0), (110, 0), (110, 220), (0, 220)))
    monkeypatch.setattr(model_job, 'plate_manifest', lambda *a: {'plates': [{'object_ids': ['1']}]})
    monkeypatch.setattr(color_geometry, '_world_bounds', lambda *a: [[(0, 150), (0, 150), (0, 10)]])
    assert oa._multi_plate_capacity(source, {'bed_mm': [220, 220]}) == 0


def test_multi_probe_reserves_feature_clearance_at_exclusions(probe, monkeypatch, tmp_path):
    source = tmp_path / 'multi.3mf'
    with zipfile.ZipFile(source, 'w'):
        pass
    monkeypatch.setattr(oa, '_fit_exclusion_polygon', lambda: ((0, 0), (110, 0), (110, 220), (0, 220)))
    monkeypatch.setattr(model_job, 'plate_manifest', lambda *a: {'plates': [{'object_ids': ['1']}]})
    monkeypatch.setattr(color_geometry, '_world_bounds', lambda *a: [[(0, 100), (0, 100), (0, 10)]])
    # 100 mm model + 5 mm feature on both sides needs 110 mm of the
    # 110 mm remaining strip, but still owes the 1 mm bed edge margin.
    assert oa._multi_plate_capacity(source, {'bed_mm': [220, 220]}, single_margin=6) == 0


def test_active_printer_exclusion_reader_returns_points_not_rectangles(monkeypatch):
    polygon = ((0, 0), (18, 0), (18, 28), (0, 28))
    model = SimpleNamespace(adapter=SimpleNamespace(facts={'bed_exclude_area': SimpleNamespace(value=polygon)}))
    monkeypatch.setattr(oa.settings, 'current', lambda: {'printer': {'model': 'fixture'}})
    monkeypatch.setitem(oa.printers.MODELS, 'fixture', model)
    assert oa._fit_exclusion_polygon() == polygon


def test_active_printer_lookup_failure_is_unknown(monkeypatch):
    monkeypatch.setattr(oa.settings, 'current', lambda: {'printer': {'model': 'not-a-model'}})
    with pytest.raises(RuntimeError, match='cannot resolve printer bed exclusions'):
        oa._fit_exclusion_polygon()
