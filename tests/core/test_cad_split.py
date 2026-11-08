"""Deterministic synthetic tests for opt-in reviewed CAD split plans and dowels (#153)."""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import pytest

from print_handy.core import cad_split, heavy_worker, prepared_intake
from print_handy.core.geometry_layout import describe_triangles, write_binary_stl
from print_handy.core.orca_adapter import read_stl_triangles


def _box_triangles(xmin: float, ymin: float, zmin: float, xmax: float, ymax: float, zmax: float):
    """Create a watertight, outward-wound axis-aligned box mesh (12 triangles)."""
    v000 = (xmin, ymin, zmin)
    v100 = (xmax, ymin, zmin)
    v110 = (xmax, ymax, zmin)
    v010 = (xmin, ymax, zmin)
    v001 = (xmin, ymin, zmax)
    v101 = (xmax, ymin, zmax)
    v111 = (xmax, ymax, zmax)
    v011 = (xmin, ymax, zmax)
    return [
        # Bottom (-Z)
        [v000, v010, v110],
        [v000, v110, v100],
        # Top (+Z)
        [v001, v101, v111],
        [v001, v111, v011],
        # Front (-Y)
        [v000, v100, v101],
        [v000, v101, v001],
        # Back (+Y)
        [v010, v011, v111],
        [v010, v111, v110],
        # Left (-X)
        [v000, v001, v011],
        [v000, v011, v010],
        # Right (+X)
        [v100, v110, v111],
        [v100, v111, v101],
    ]


def test_preset_plans_load_and_validate():
    rack = cad_split.load_split_plan('rack_adapter_10to19')
    assert rack['plan_id'] == 'rack_adapter_10to19'
    assert len(rack['bores']) == 4
    expected_rack_bore_vol = 4 * math.pi * ((5.4 / 2.0) ** 2) * 33.0
    assert abs(cad_split.expected_bore_removal_volume_mm3(rack) - expected_rack_bore_vol) < 1e-6

    mac = cad_split.load_split_plan('macmini_mount_a1347')
    assert mac['plan_id'] == 'macmini_mount_a1347'
    assert mac['expected_shells'] == 2
    assert len(mac['bores']) == 3
    expected_mac_bore_vol = (
        2 * math.pi * ((3.35 / 2.0) ** 2) * 23.0
        + math.pi * ((2.15 / 2.0) ** 2) * 23.0
    )
    assert abs(cad_split.expected_bore_removal_volume_mm3(mac) - expected_mac_bore_vol) < 1e-6


def test_schema_rejects_zero_clearance_or_overlong_pin():
    bad_clearance = {
        'plan_id': 'bad_pin',
        'cut_axis': 'x',
        'cut_coord_mm': 0.0,
        'bores': [
            {
                'y_mm': 0.0,
                'z_mm': 5.0,
                'bore_diameter_mm': 5.0,
                'depth_each_side_mm': 15.0,
                'pin_diameter_mm': 5.0,
                'pin_length_mm': 28.0,
            }
        ],
    }
    with pytest.raises(ValueError, match='smaller than bore_diameter_mm'):
        cad_split.validate_split_plan_schema(bad_clearance)

    bad_length = {
        'plan_id': 'bad_len',
        'cut_axis': 'x',
        'cut_coord_mm': 0.0,
        'bores': [
            {
                'y_mm': 0.0,
                'z_mm': 5.0,
                'bore_diameter_mm': 5.4,
                'depth_each_side_mm': 15.0,
                'pin_diameter_mm': 5.0,
                'pin_length_mm': 31.0,
            }
        ],
    }
    with pytest.raises(ValueError, match='exceeds total bore length'):
        cad_split.validate_split_plan_schema(bad_length)


def test_chamfered_dowel_row_is_watertight_and_exact():
    rack = cad_split.load_split_plan('rack_adapter_10to19')
    tris = cad_split.generate_dowel_row_triangles(rack, segments=32)
    desc = describe_triangles(tris)
    assert desc['watertight'] is True
    assert desc['winding_consistent'] is True
    assert desc['shell_count'] == 4
    assert abs(desc['dimensions_mm'][2] - 30.5) < 1e-4


def test_prepare_cad_split_rack_adapter_synthetic(tmp_path: Path):
    # Synthetic 245 x 44.5 x 20 mm bar centered on X=0
    stl_path = tmp_path / '10to19_adapter_with_ears.stl'
    write_binary_stl(_box_triangles(-122.5, -14.75, 0.0, 122.5, 29.75, 20.0), stl_path)

    outdir = tmp_path / 'out_rack'
    report = cad_split.prepare_cad_split(stl_path, outdir, 'rack_adapter_10to19', execute_openscad=False)

    assert report['plan_id'] == 'rack_adapter_10to19'
    assert report['preflight']['one_piece_fits_nominal_bed'] is True
    assert report['preflight']['prefer_intact_one_piece'] is True
    diag_dims = report['preflight']['one_piece_diagonal_dimensions_mm']
    assert abs(diag_dims[0] - 204.7074) < 0.05
    assert abs(diag_dims[1] - 204.7074) < 0.05
    assert abs(diag_dims[2] - 20.0) < 0.01

    one_piece_file = outdir / report['one_piece_intact_stl']
    dowels_file = outdir / report['printable_dowels_stl']
    scad_file = outdir / report['openscad_script']
    val_file = outdir / 'split_validation.json'

    assert one_piece_file.is_file()
    assert dowels_file.is_file()
    assert scad_file.is_file()
    assert val_file.is_file()

    scad_text = scad_file.read_text(encoding='utf-8')
    assert 'cylinder(h=33.0000, d=5.4000' in scad_text
    assert 'DOWEL_D = 5.0000;' in scad_text
    assert 'DOWEL_LENGTH = 30.5000;' in scad_text

    ladder = report['status_ladder']
    assert ladder['geometry_inspected'] == 'passed'
    assert ladder['geometry_nominal_fit'] == 'passed'
    assert ladder['slicer_capacity_verified'] == 'not_run'
    assert ladder['gcode_sliced_and_guarded'] == 'not_run'
    assert ladder['physical_print_tested'] == 'not_run'


def test_plan_without_pin_fields_generates_the_validated_dowel_defaults():
    # Validation derives the pin defaults from the bore (diameter - 0.4,
    # 2*depth - 2.5); generation must produce those dimensions, not the fixed
    # 5.0 x 30.5 fallbacks, or the dowel cannot fit the joint it passed for (P2-48).
    plan = cad_split.load_split_plan({
        'plan_id': 'small_bore', 'cut_axis': 'x', 'cut_coord_mm': 0.0,
        'bores': [{'y_mm': 0.0, 'z_mm': 5.0, 'bore_diameter_mm': 3.0,
                   'depth_each_side_mm': 5.0, 'pin_chamfer_mm': 0.3,
                   'pin_tip_diameter_mm': 2.2}],
    })
    assert plan['bores'][0]['pin_diameter_mm'] == pytest.approx(2.6)
    assert plan['bores'][0]['pin_length_mm'] == pytest.approx(7.5)
    desc = describe_triangles(cad_split.generate_dowel_row_triangles(plan, segments=24))
    assert abs(desc['dimensions_mm'][0] - 2.6) < 1e-4
    assert abs(desc['dimensions_mm'][2] - 7.5) < 1e-4
    scad = cad_split.render_openscad_script('small.stl', plan)
    assert 'DOWEL_D = 2.6000;' in scad
    assert 'DOWEL_LENGTH = 7.5000;' in scad


@pytest.mark.skipif(not shutil.which('openscad'), reason='openscad not installed')
def test_run_openscad_real_binary_exports_and_refuses_bad_source(tmp_path: Path):
    """The cad_split execution seam against the real OpenSCAD binary.

    Every other cad_split execution test fakes _run_openscad; this one runs
    it (added in the 2026-11-07 test-suite cleanup, when the offline CI legs
    started installing openscad, so the gate executes in CI). A good source
    exports a binary STL the repo's own reader can parse with the expected
    dimensions; a syntax-broken source raises RuntimeError and leaves no
    output, matching the seam's fail-closed contract.
    """
    good = tmp_path / 'good.scad'
    good.write_text('cube([10, 20, 30]);\n', encoding='utf-8')
    out = tmp_path / 'good.stl'
    cad_split._run_openscad(good, out)
    assert out.is_file() and out.stat().st_size >= 84
    tris = read_stl_triangles(out)
    assert tris, 'exported STL must contain triangles'
    xs = [v[0] for tri in tris for v in tri]
    ys = [v[1] for tri in tris for v in tri]
    zs = [v[2] for tri in tris for v in tri]
    assert (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) == (10.0, 20.0, 30.0)

    bad = tmp_path / 'bad.scad'
    bad.write_text('cube([10, 10, 10);\n', encoding='utf-8')
    bad_out = tmp_path / 'bad.stl'
    with pytest.raises(RuntimeError, match='OpenSCAD failed'):
        cad_split._run_openscad(bad, bad_out)
    assert not bad_out.is_file()


def test_prepare_cad_split_resolves_a_relative_outdir_before_openscad(tmp_path: Path, monkeypatch):
    # OpenSCAD runs with cwd=<tmp scad dir>; a relative output path would land
    # there instead of the caller's outdir, so prepare must resolve it first (P1-51).
    stl_path = tmp_path / '10to19_adapter_with_ears.stl'
    write_binary_stl(_box_triangles(-122.5, -14.75, 0.0, 122.5, 29.75, 20.0), stl_path)
    seen = []

    def fake_run(scad_path, out_path, defines=None, timeout=210):
        seen.append(Path(out_path))
        raise RuntimeError('stop after recording')

    monkeypatch.setattr(cad_split, '_run_openscad', fake_run)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match='stop after recording'):
        cad_split.prepare_cad_split(stl_path, Path('relative_out'), 'rack_adapter_10to19',
                                    execute_openscad=True)
    assert seen and all(p.is_absolute() for p in seen)


def test_prepare_cad_split_copies_the_source_next_to_the_exported_scad(tmp_path: Path):
    # The exported script imports the source by bare name; without a copy next
    # to it, the advertised reproducible artifact cannot load its model (P2-47).
    stl_path = tmp_path / '10to19_adapter_with_ears.stl'
    write_binary_stl(_box_triangles(-122.5, -14.75, 0.0, 122.5, 29.75, 20.0), stl_path)
    outdir = tmp_path / 'elsewhere'
    report = cad_split.prepare_cad_split(stl_path, outdir, 'rack_adapter_10to19')
    scad_text = (outdir / report['openscad_script']).read_text(encoding='utf-8')
    assert f'import("{stl_path.name}"' in scad_text
    copied = outdir / stl_path.name
    assert copied.is_file()
    assert copied.read_bytes() == stl_path.read_bytes()


def test_check_source_rejects_wrong_dimensions_or_shell_count(tmp_path: Path):
    wrong_stl = tmp_path / 'wrong.stl'
    write_binary_stl(_box_triangles(-50.0, -10.0, 0.0, 50.0, 10.0, 20.0), wrong_stl)
    with pytest.raises(ValueError, match='extent on axis 0'):
        cad_split.prepare_cad_split(wrong_stl, tmp_path / 'out', 'rack_adapter_10to19')

    # 1-shell STL rejected by 2-shell Mac Mini plan
    one_shell_mac = tmp_path / 'mac_1shell.stl'
    write_binary_stl(_box_triangles(-127.0, -22.3125, 0.0, 127.0, 22.3125, 183.4078), one_shell_mac)
    with pytest.raises(ValueError, match='connected shells'):
        cad_split.prepare_cad_split(one_shell_mac, tmp_path / 'out_mac', 'macmini_mount_a1347')


def test_verify_split_artifacts_enforces_volume_conservation():
    rack = cad_split.load_split_plan('rack_adapter_10to19')
    whole = _box_triangles(-122.5, -14.75, 0.0, 122.5, 29.75, 20.0)
    # Left and right halves with zero bore volume removed -> should fail volume conservation check
    left_no_bores = _box_triangles(-122.5, -14.75, 0.0, 0.0, 29.75, 20.0)
    right_no_bores = _box_triangles(0.0, -14.75, 0.0, 122.5, 29.75, 20.0)

    with pytest.raises(ValueError, match='Split modified unexpected material'):
        cad_split.verify_split_artifacts(
            whole_triangles=whole,
            left_triangles=left_no_bores,
            right_triangles=right_no_bores,
            plan=rack,
        )


def test_heavy_worker_registration():
    for entry in (
        ('cad_split', 'check_source_against_split_plan'),
        ('cad_split', 'prepare_cad_split'),
    ):
        assert entry in heavy_worker.ALLOWED
        assert entry in prepared_intake._OFFLOAD


# ------------------------------------------- real OpenSCAD binary (expansion)
#
# The seam test above proves _run_openscad can export one cube. The tests
# below run the whole cad_split execution path against the real binary:
# -D defines choosing geometry, and prepare_cad_split end to end (both
# halves, the combined plate, the dowels, and the boolean verification),
# asserting on the geometry of the files that come back — never merely
# that openscad was invoked. Gated exactly like the seam test: they skip
# where openscad is absent and execute in the offline CI legs, which
# install it.

needs_openscad = pytest.mark.skipif(not shutil.which('openscad'),
                                    reason='openscad not installed')


@needs_openscad
def test_run_openscad_defines_choose_the_part_and_scale_the_geometry(tmp_path: Path):
    scad = tmp_path / 'parts.scad'
    scad.write_text(
        'part = "small";\n'
        'w = 4;\n'
        'if (part == "small") cube([w, w, w]);\n'
        'if (part == "big") cube([3 * w, 2 * w, w]);\n',
        encoding='utf-8')

    def dims(name, defines=None):
        out = tmp_path / name
        cad_split._run_openscad(scad, out, defines=defines)
        return describe_triangles(read_stl_triangles(out))['dimensions_mm']

    assert dims('default.stl') == [4.0, 4.0, 4.0]
    assert dims('big.stl', {'part': 'big'}) == [12.0, 8.0, 4.0]
    assert dims('wide.stl', {'w': 7}) == [7.0, 7.0, 7.0]
    assert dims('bigwide.stl', {'part': 'big', 'w': 7}) == [21.0, 14.0, 7.0]


@needs_openscad
def test_prepare_cad_split_real_openscad_end_to_end(tmp_path: Path):
    # A 60 x 20 x 10 bar cut at x=0 with one 4 mm bore through the joint:
    # small enough for a fast real boolean, exact enough to assert on.
    stl_path = tmp_path / 'bar.stl'
    write_binary_stl(_box_triangles(-30.0, -10.0, 0.0, 30.0, 10.0, 10.0), stl_path)
    plan = {
        'plan_id': 'openscad_e2e_bar',
        'cut_axis': 'x',
        'cut_coord_mm': 0.0,
        'bed_mm': [220, 220, 250],
        'bores': [{'y_mm': 0.0, 'z_mm': 5.0, 'bore_diameter_mm': 4.0,
                   'depth_each_side_mm': 8.0, 'pin_chamfer_mm': 0.3,
                   'pin_tip_diameter_mm': 2.8}],
    }
    outdir = tmp_path / 'out_e2e'
    report = cad_split.prepare_cad_split(stl_path, outdir, plan, execute_openscad=True)

    assert report['openscad_executed'] is True
    assert set(report['generated_split_files']) == {'left', 'right', 'both_halves'}
    for name in report['generated_split_files'].values():
        assert (outdir / name).is_file()

    left = describe_triangles(read_stl_triangles(
        outdir / report['generated_split_files']['left']))
    right = describe_triangles(read_stl_triangles(
        outdir / report['generated_split_files']['right']))
    for half in (left, right):
        assert half['watertight'] is True and half['winding_consistent'] is True
        assert half['shell_count'] == 1
        assert half['dimensions_mm'] == pytest.approx([30.0, 20.0, 10.0], abs=0.05)
    # The cut interface sits exactly on the plan's cut plane.
    assert left['bounds_max_mm'][0] == pytest.approx(0.0, abs=0.02)
    assert right['bounds_min_mm'][0] == pytest.approx(0.0, abs=0.02)

    verification = report['boolean_verification']
    assert verification is not None
    assert verification['whole_volume_mm3'] == pytest.approx(12000.0, abs=1.0)
    # Each half is the 6000 mm3 block minus its half of the bore cylinder.
    expected_removed = math.pi * 2.0 ** 2 * 16.0
    assert verification['expected_bore_volume_mm3'] == pytest.approx(expected_removed, abs=0.01)
    removed = verification['whole_volume_mm3'] - (
        verification['left_volume_mm3'] + verification['right_volume_mm3'])
    assert removed == pytest.approx(expected_removed, abs=35.0)

    # The combined one-plate render holds both halves at once.
    both = describe_triangles(read_stl_triangles(
        outdir / report['generated_split_files']['both_halves']))
    assert both['shell_count'] == 2
    assert both['dimensions_mm'][0] > 60.0  # the halves sit apart on the plate

    # The dowel STL matches the pin the validated plan derived (3.6 x 13.5).
    dowels = describe_triangles(read_stl_triangles(outdir / report['printable_dowels_stl']))
    assert dowels['dimensions_mm'][0] == pytest.approx(3.6, abs=0.05)
    assert dowels['dimensions_mm'][2] == pytest.approx(13.5, abs=0.05)

    on_disk = json.loads((outdir / 'split_validation.json').read_text(encoding='utf-8'))
    assert on_disk['openscad_executed'] is True
    assert on_disk['boolean_verification']['whole_volume_mm3'] == \
        verification['whole_volume_mm3']


@needs_openscad
def test_prepare_cad_split_real_openscad_rack_preset_end_to_end(tmp_path: Path):
    # The shipped rack preset (4 bores, 245 mm bar) through the real binary.
    # The source is centered on Y, unlike the fake-run test's offset box:
    # the preset's half boxes span y=-25..25, so an offset source would be
    # clipped by the real boolean (the fake run cannot see that).
    stl_path = tmp_path / '10to19_adapter_with_ears.stl'
    write_binary_stl(_box_triangles(-122.5, -22.25, 0.0, 122.5, 22.25, 20.0), stl_path)
    outdir = tmp_path / 'out_rack_real'
    report = cad_split.prepare_cad_split(
        stl_path, outdir, 'rack_adapter_10to19', execute_openscad=True)

    assert report['openscad_executed'] is True
    verification = report['boolean_verification']
    assert verification is not None
    assert verification['whole_volume_mm3'] == pytest.approx(245 * 44.5 * 20, abs=1.0)

    left = describe_triangles(read_stl_triangles(
        outdir / report['generated_split_files']['left']))
    right = describe_triangles(read_stl_triangles(
        outdir / report['generated_split_files']['right']))
    for half in (left, right):
        assert half['watertight'] is True and half['shell_count'] == 1
        assert half['dimensions_mm'][0] == pytest.approx(122.5, abs=0.05)
        assert half['dimensions_mm'][1] == pytest.approx(44.5, abs=0.05)
        assert half['dimensions_mm'][2] == pytest.approx(20.0, abs=0.05)
    total = (left['abs_volume_mm3'] + right['abs_volume_mm3']
             + report['expected_bore_removal_volume_mm3'])
    assert total == pytest.approx(245 * 44.5 * 20, abs=40.0)
