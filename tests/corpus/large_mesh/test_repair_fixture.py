"""The print repair skill's scans on the real 2026-11-21 fixture (#1777).

Runs the skill's tools through ``Session.run`` (the entry point the heavy
tier's tool calls use) on the couch organizer: the original must show the
four floating regions near Z 92.3, the 26 mm cantilever at Z 27 and the
53 degree flare behind the 0.562 mm low-clearance finding; the fixed file
must show no islands. The files are the owner's upload and are not in the
repository: without them every test here skips (see case.toml).
"""
import time

import pytest

from print_handy.core import mesh_native
from print_handy.skills import print_repair

from . import fetch
from .test_large_mesh import CASES, _case_files, _record

REPAIR = sorted(name for name, case in CASES.items() if case.get('kind') == 'repair')


def _by_role(case, paths):
    return {row['role']: path for row, path in zip(case['file'], paths, strict=True)}


def _timed(timings, stage, fn):
    start = time.perf_counter()
    result = fn()
    timings[stage] = round(time.perf_counter() - start, 3)
    return result


def _ok(reply):
    assert reply['ok'], reply
    return reply['result']


@pytest.mark.timeout(900)
@pytest.mark.parametrize('name', REPAIR)
def test_repair_fixture(name):
    case, paths = _case_files(name)
    files = _by_role(case, paths)
    folder = files['original'].parent
    session = print_repair.Session(folder)
    expect = case['expect']
    layer = expect['layer_mm']
    timings = {}
    original, fixed = files['original'].name, files['fixed'].name

    mesh = _timed(timings, 'load:original', lambda: session.mesh(original))
    assert len(mesh.faces) == expect['original']['faces']
    assert _ok(session.run('bodies', {'mesh': original}))['count'] == expect['original']['bodies']

    islands = _timed(timings, 'island_scan:original',
                     lambda: _ok(session.run('island_scan', {'mesh': original, 'layer_mm': layer})))
    lo, hi = expect['original']['island_z']
    assert islands['island_count'] == len(expect['original']['island_areas_mm2']), islands
    assert all(lo <= island['z'] <= hi for island in islands['islands']), islands
    areas = sorted(island['area_mm2'] for island in islands['islands'])
    assert areas == pytest.approx(expect['original']['island_areas_mm2'], rel=0.03)
    assert islands['open_layers'] == 0

    overhangs = _timed(timings, 'overhang_scan:original',
                       lambda: _ok(session.run('overhang_scan', {'mesh': original, 'layer_mm': layer})))
    cantilever = [o for o in overhangs['overhangs']
                  if o['z_min'] == pytest.approx(expect['original']['cantilever_z'], abs=layer)
                  and o['area_mm2'] > 500]
    assert len(cantilever) == 1, overhangs
    assert cantilever[0]['area_mm2'] == pytest.approx(expect['original']['cantilever_area_mm2'], rel=0.03)
    assert not cantilever[0]['floating']
    floating = [o for o in overhangs['overhangs'] if o['floating']]
    assert len(floating) == islands['island_count']
    shelf = _ok(session.run('underside_profile', {
        'mesh': original, 'region': expect['original']['cantilever_region'],
        'z_range': [expect['original']['cantilever_z'] - 1, expect['original']['cantilever_z'] + 1]}))
    assert shelf['verdict'] == 'flat_underside', shelf

    clearance = _ok(session.run('clearance_report', {'mesh': original, 'first_layer_mm': 0.24}))
    assert clearance['risky']
    assert any(f['clearance_mm']['min'] == pytest.approx(expect['original']['flare_clearance_mm'], abs=0.01)
               for f in clearance['findings']), clearance['findings']
    flare = _ok(session.run('underside_profile', {
        'mesh': original, 'region': expect['original']['flare_region'],
        'z_range': expect['original']['flare_z_range']}))
    assert flare['verdict'] == 'sloped_flare', flare
    assert flare['mean_slope_deg'] == pytest.approx(expect['original']['flare_slope_deg'], abs=1.0)

    fixed_mesh = _timed(timings, 'load:fixed', lambda: session.mesh(fixed))
    assert len(fixed_mesh.faces) == expect['fixed']['faces']
    assert _ok(session.run('bodies', {'mesh': fixed}))['count'] == expect['fixed']['bodies']
    fixed_islands = _timed(timings, 'island_scan:fixed',
                           lambda: _ok(session.run('island_scan', {'mesh': fixed, 'layer_mm': layer})))
    assert fixed_islands['island_count'] == expect['fixed']['islands'], fixed_islands
    fixed_overhangs = _ok(session.run('overhang_scan', {'mesh': fixed, 'layer_mm': layer}))
    assert not [o for o in fixed_overhangs['overhangs']
                if o['z_min'] == pytest.approx(expect['original']['cantilever_z'], abs=1.0)
                and o['area_mm2'] > 100], fixed_overhangs
    assert not [o for o in fixed_overhangs['overhangs'] if o['floating']]

    _record(name, 'auto', timings)
    budget = case['budget']
    over = {stage: seconds for stage, seconds in timings.items()
            if seconds > budget.get(stage.split(':', 1)[0], float('inf'))}
    assert not over, f'scans over budget {budget} (native kernel: {mesh_native.available()}): {over}'


def test_repair_cases_are_local_only():
    """The repair fixture is the owner's upload: never fetched or redistributed."""
    for name in REPAIR:
        assert fetch.local_only(CASES[name]), name
        assert CASES[name]['redistribute'] is False, name
