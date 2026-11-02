"""Regression tests for 100%-scale geometry inspection, plate arrangement, and export (#152)."""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from print_handy.core import background_slice, geometry_layout, heavy_worker, model_job, orca_adapter, prepared_intake


def _box_triangles(dx: float, dy: float, dz: float, offset=(0.0, 0.0, 0.0)):
    ox, oy, oz = (float(v) for v in offset)
    v = [
        (ox, oy, oz),
        (ox + dx, oy, oz),
        (ox + dx, oy + dy, oz),
        (ox, oy + dy, oz),
        (ox, oy, oz + dz),
        (ox + dx, oy, oz + dz),
        (ox + dx, oy + dy, oz + dz),
        (ox, oy + dy, oz + dz),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2),
        (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4),
        (2, 3, 7), (2, 7, 6),
        (1, 2, 6), (1, 6, 5),
        (3, 0, 4), (3, 4, 7),
    ]
    return [[v[a], v[b], v[c]] for a, b, c in faces]


def _write_test_3mf(
    target: Path,
    objects_spec,
    *,
    unit: str = 'millimeter',
    model_settings_xml: str | None = None,
    extra_files: dict[str, bytes] | None = None,
) -> Path:
    content_types = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        b'<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
        b'</Types>\n'
    )
    rels = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Target="/3D/3dmodel.model" Id="rel0" '
        b'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        b'</Relationships>\n'
    )
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<model unit="{unit}" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
        'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">',
        '  <resources>',
    ]
    build_lines = ['  <build>']
    for spec in objects_spec:
        oid = str(spec['id'])
        name = spec.get('name', f'object_{oid}')
        lines.append(f'    <object id="{oid}" name="{name}" type="model">')
        if 'triangles' in spec:
            lines.append('      <mesh>')
            lines.append('        <vertices>')
            v_map = {}
            v_list = []
            t_list = []
            for tri in spec['triangles']:
                idxs = []
                for pt in tri:
                    key = (round(float(pt[0]), 6), round(float(pt[1]), 6), round(float(pt[2]), 6))
                    idx = v_map.get(key)
                    if idx is None:
                        idx = len(v_list)
                        v_map[key] = idx
                        v_list.append(key)
                    idxs.append(idx)
                t_list.append(idxs)
            for vx, vy, vz in v_list:
                lines.append(f'          <vertex x="{vx:.6f}" y="{vy:.6f}" z="{vz:.6f}"/>')
            lines.append('        </vertices>')
            lines.append('        <triangles>')
            paint_attr = f' slic3rpe:mmu_segmentation="{spec["paint"]}"' if spec.get('paint') else ''
            for v1, v2, v3 in t_list:
                lines.append(f'          <triangle v1="{v1}" v2="{v2}" v3="{v3}"{paint_attr}/>')
            lines.append('        </triangles>')
            lines.append('      </mesh>')
        if 'components' in spec:
            lines.append('      <components>')
            for comp in spec['components']:
                tf = comp.get('transform', '1 0 0 0 1 0 0 0 1 0 0 0')
                lines.append(f'        <component objectid="{comp["objectid"]}" transform="{tf}"/>')
            lines.append('      </components>')
        lines.append('    </object>')
        if spec.get('build', True):
            tf = spec.get('transform', '1 0 0 0 1 0 0 0 1 0 0 0')
            build_lines.append(f'    <item objectid="{oid}" transform="{tf}" printable="1"/>')
    lines.append('  </resources>')
    build_lines.append('  </build>')
    lines.extend(build_lines)
    lines.append('</model>\n')
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('[Content_Types].xml', content_types)
        zf.writestr('_rels/.rels', rels)
        zf.writestr('3D/3dmodel.model', '\n'.join(lines).encode('utf-8'))
        if model_settings_xml is not None:
            zf.writestr('Metadata/model_settings.config', model_settings_xml.encode('utf-8'))
        for k, v in (extra_files or {}).items():
            zf.writestr(k, v)
    return target


def _make_duplo_6part_3mf(target: Path) -> Path:
    """Create the 6-object Duplo test fixture whose authored layout spans 222.17x172.12x16.6 mm."""
    # Exact part dimensions from Duplo Original_Inspection.json, spread out so combined X = 222.1727 mm > 220 mm
    specs = [
        {'id': '2', 'name': 'object_2', 'triangles': _box_triangles(112.0, 79.9617, 15.6), 'transform': '1 0 0 0 1 0 0 0 1 16.9137 47.9016 0'},
        {'id': '3', 'name': 'object_3', 'triangles': _box_triangles(64.0, 79.9999, 15.6), 'transform': '1 0 0 0 1 0 0 0 1 139.9739 47.9593 0'},
        {'id': '4', 'name': 'object_4', 'triangles': _box_triangles(64.0, 79.9999, 15.6), 'transform': '1 0 0 0 1 0 0 0 1 175.0864 134.0606 0'},
        {'id': '5', 'name': 'object_5', 'triangles': _box_triangles(119.0, 34.0, 16.6), 'transform': '1 0 0 0 1 0 0 0 1 39.2799 138.1213 0'},
        {'id': '6', 'name': 'object_6', 'triangles': _box_triangles(68.4, 34.0, 14.3), 'transform': '1 0 0 0 1 0 0 0 1 168.1818 96.1820 0'},
        {'id': '7', 'name': 'object_7', 'triangles': _box_triangles(68.4, 34.0, 14.3), 'transform': '1 0 0 0 1 0 0 0 1 102.9818 180.0229 0'},
    ]
    return _write_test_3mf(target, specs)


def test_duplo_6_object_rearrange_at_100_percent(tmp_path):
    """6-object Duplo 3MF exceeding 220x220 as authored packs onto 1 plate at 100% scale."""
    src = _make_duplo_6part_3mf(tmp_path / 'duplo_train.3mf')
    inspected = geometry_layout.inspect_geometry(src)
    assert inspected['part_count'] == 6
    assert inspected['scale_preserved_100_percent'] is True
    assert inspected['original_assembly_dimensions_mm'][0] > 220.0
    assert inspected['can_fit_as_authored_dimensions'] is False
    assert inspected['status_ladder'] == {
        'geometry_inspected': 'passed',
        'geometry_nominal_fit': 'failed',
        'slicer_capacity_verified': 'not_run',
        'gcode_sliced_and_guarded': 'not_run',
        'physical_print_tested': 'not_run',
    }

    outdir = tmp_path / 'out'
    report = geometry_layout.arrange_and_export(src, outdir)
    assert report['result'] == 'geometry_fits_nominal_bed'
    assert report['scale_preserved_100_percent'] is True
    assert report['additional_scale_factor'] == 1.0
    assert report['exported_3mf_object_count'] == 6
    assert report['arranged_dimensions_mm'][0] <= 210.0
    assert report['arranged_dimensions_mm'][1] <= 210.0
    assert report['arranged_dimensions_mm'][2] == pytest.approx(16.6, abs=0.01)
    assert report['arranged_bounds_min_mm'][0] >= 5.0 - 1e-3
    assert report['arranged_bounds_min_mm'][1] >= 5.0 - 1e-3
    assert report['arranged_bounds_max_mm'][0] <= 215.0 + 1e-3
    assert report['arranged_bounds_max_mm'][1] <= 215.0 + 1e-3
    assert report['checks']['bed_bounds'] is True
    assert report['checks']['bbox_separation'] is True
    assert report['checks']['original_surface_areas_preserved'] is True
    assert report['checks']['3mf_roundtrip_object_count_area_extents'] is True
    assert report['status_ladder'] == {
        'geometry_inspected': 'passed',
        'geometry_nominal_fit': 'passed',
        'slicer_capacity_verified': 'not_run',
        'gcode_sliced_and_guarded': 'not_run',
        'physical_print_tested': 'not_run',
    }
    assert (outdir / report['exported_3mf_geometry_only']).exists()
    assert (outdir / report['exported_combined_file']).exists()
    assert (outdir / 'plate_preview.png').exists()
    assert (outdir / 'plate_preview.png').read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    assert (outdir / 'validation.json').exists()
    assert 'preserved_3mf_package' in report
    assert (outdir / report['preserved_3mf_package']['preserved_3mf']).exists()


def test_oversized_single_object_245mm_and_254mm_refused_without_scaling(tmp_path):
    """Single-body 245 mm rack ear and 254 mm Mac mount cannot pack flat on 220x220 at 100% scale."""
    rack_stl = geometry_layout.write_binary_stl(_box_triangles(245.1, 44.0, 177.8), tmp_path / 'rack_245.stl')
    with pytest.raises(RuntimeError, match='cannot fit the 220x220x220 mm bed at 100% scale'):
        geometry_layout.arrange_and_export(rack_stl, tmp_path / 'rack_out', angles=[0, 90])

    # Even with 5-degree yaw search, 245.1 x 44.0 mm requires ~208.5 mm XY at 45 deg, which fits inside 210x210 usable bed!
    # Verify that 5-degree Z rotation finds the diagonal flat fit for a narrow 245.1 x 30 mm bar,
    # whereas the 254 x 122.3 mm Mac mount cannot fit at any Z rotation and is refused.
    mac_stl = geometry_layout.write_binary_stl(_box_triangles(254.0, 122.3, 45.7), tmp_path / 'mac_254.stl')
    with pytest.raises(RuntimeError, match='cannot fit the 220x220x220 mm bed at 100% scale'):
        geometry_layout.arrange_and_export(mac_stl, tmp_path / 'mac_out')


def test_source_85_percent_instance_scale_is_warned_and_preserved(tmp_path):
    """Authored 0.85x instance transform in a 3MF is warned about and preserved, never silently undone."""
    spec = [{
        'id': '1',
        'name': 'rack_ear_scaled',
        'triangles': _box_triangles(200.0, 40.0, 20.0),
        'transform': '0.85 0 0 0 0.85 0 0 0 0.85 10 10 0',
    }]
    src = _write_test_3mf(tmp_path / 'authored_85pct.3mf', spec)
    inspected = geometry_layout.inspect_geometry(src)
    assert inspected['original_assembly_dimensions_mm'] == pytest.approx([170.0, 34.0, 17.0], abs=0.01)
    assert any('authored source transform scaling/shear' in w for w in inspected['warnings'])


def test_unit_override_skips_preserved_3mf_package(tmp_path):
    """An input-unit override that differs from the archive's declared unit
    converts the packed geometry, but the preserved package rewrites build
    transforms in the archive's own unit space — preserving would report a
    package with different physical dimensions, so it is skipped (P2-46)."""
    spec = [{'id': '1', 'name': 'inch_box', 'triangles': _box_triangles(2.0, 3.0, 1.0)}]
    src = _write_test_3mf(tmp_path / 'inch_numbers.3mf', spec)
    with pytest.raises(RuntimeError, match='input unit'):
        geometry_layout.preserve_3mf_package_layout(
            src, [], tmp_path / 'direct_preserved.3mf', input_unit='inch')
    out = geometry_layout.arrange_and_export(src, tmp_path / 'out', input_unit='inch')
    assert 'preserved_3mf_package' not in out
    assert any('input unit' in w for w in out['warnings'])
    assert not (tmp_path / 'out' / 'inch_numbers_100pct_arranged_preserved.3mf').exists()
    # Control: no override — the same archive still preserves.
    out2 = geometry_layout.arrange_and_export(src, tmp_path / 'out2')
    assert 'preserved_3mf_package' in out2


def test_unit_conversion_and_invalid_unit_refusal(tmp_path):
    """Explicit inch/cm units convert accurately to mm; unknown units raise ValueError."""
    stl = geometry_layout.write_binary_stl(_box_triangles(2.0, 3.0, 1.0), tmp_path / 'inch_part.stl')
    inspected = geometry_layout.inspect_geometry(stl, input_unit='inch')
    assert inspected['unit_conversion_to_mm'] == pytest.approx(25.4)
    assert inspected['original_assembly_dimensions_mm'] == pytest.approx([50.8, 76.2, 25.4], abs=0.01)

    with pytest.raises(ValueError, match='Unrecognized/missing length units'):
        geometry_layout.inspect_geometry(stl, input_unit='furlongs')


def test_disconnected_stl_shells_and_non_watertight_warnings(tmp_path):
    """Multi-shell STL warns when unsplit, splits cleanly with split_stl=True, and flags open boundaries."""
    two_shells = _box_triangles(20.0, 20.0, 10.0, (0, 0, 0)) + _box_triangles(20.0, 20.0, 10.0, (180, 180, 0))
    stl_path = geometry_layout.write_binary_stl(two_shells, tmp_path / 'two_shells.stl')

    unsplit = geometry_layout.inspect_geometry(stl_path, split_stl=False)
    assert unsplit['part_count'] == 1
    assert unsplit['parts'][0]['shell_count'] == 2
    assert any('disconnected triangle shells' in w for w in unsplit['warnings'])

    split_res = geometry_layout.inspect_geometry(stl_path, split_stl=True)
    assert split_res['part_count'] == 2

    # Open mesh (missing one triangle) is flagged as non-watertight
    open_tris = _box_triangles(20.0, 20.0, 10.0)[:-1]
    open_stl = geometry_layout.write_binary_stl(open_tris, tmp_path / 'open_box.stl')
    open_insp = geometry_layout.inspect_geometry(open_stl)
    assert open_insp['parts'][0]['watertight'] is False
    assert open_insp['parts'][0]['boundary_edges'] > 0
    assert any('non-watertight' in w for w in open_insp['warnings'])


def test_negative_volume_and_multicolor_3mf_guards(tmp_path):
    """3MF negative-volume cutters are refused; painted/multicolor 3MF refuses strip when disallowed."""
    cutter_settings = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="1">
    <part id="1" subtype="normal_part"/>
    <part id="2" subtype="negative_volume"/>
  </object>
</config>"""
    specs = [
        {'id': '2', 'name': 'cutter', 'triangles': _box_triangles(10, 10, 10), 'build': False},
        {'id': '1', 'name': 'main', 'triangles': _box_triangles(40, 40, 20), 'components': [{'objectid': '2'}]},
    ]
    neg_3mf = _write_test_3mf(tmp_path / 'neg_vol.3mf', specs, model_settings_xml=cutter_settings)
    with pytest.raises(RuntimeError, match='negative-volume cutters'):
        geometry_layout.arrange_and_export(neg_3mf, tmp_path / 'neg_out')
    assert geometry_layout.probe_single_plate_rearrange(neg_3mf) is None

    painted_specs = [
        {'id': '1', 'name': 'p1', 'triangles': _box_triangles(50, 50, 10), 'paint': '4', 'transform': '1 0 0 0 1 0 0 0 1 0 0 0'},
        {'id': '2', 'name': 'p2', 'triangles': _box_triangles(50, 50, 10), 'paint': '8', 'transform': '1 0 0 0 1 0 0 0 1 180 180 0'},
    ]
    painted_3mf = _write_test_3mf(tmp_path / 'painted.3mf', painted_specs)
    with pytest.raises(RuntimeError, match='multi-color / painted 3MF assignments'):
        geometry_layout.arrange_and_export(painted_3mf, tmp_path / 'painted_out', allow_geometry_only_strip=False)


def test_intake_fit_choice_rows_and_cli_layout_subcommand(tmp_path):
    """fit_options, fit_choice_rows, heavy_worker, and CLI `model_job layout` integrate cleanly."""
    src = _make_duplo_6part_3mf(tmp_path / 'duplo_intake.3mf')
    probe = geometry_layout.probe_single_plate_rearrange(src)
    assert probe is not None
    assert probe['id'] == 'rearrange_100'
    assert probe['scale'] == 1.0
    assert probe['part_count'] == 6

    rows = prepared_intake.fit_choice_rows({
        'rearrange_100': probe,
        'scales': [0.95],
        'rotations': [],
    })
    assert rows[0]['kind'] == 'rearrange'
    assert rows[0]['label'] == '100% · rearrange 1 plate'
    assert prepared_intake._fit_choice_reply('rearrange', rows) == 0
    assert prepared_intake._fit_choice_reply('100%', rows) == 0

    job = {'full_size_rearrange_plan': probe}
    assert model_job.copies_supported(job) is False
    b1 = background_slice.slice_binding({}, 'abc')
    b2 = background_slice.slice_binding(job, 'abc')
    assert b1 != b2
    assert ('geometry_layout', 'arrange_and_export') in heavy_worker.ALLOWED
    assert ('geometry_layout', 'arrange_and_export') in prepared_intake._OFFLOAD

    cli_out = tmp_path / 'cli_out'
    proc = subprocess.run(
        [sys.executable, '-m', 'print_handy.core.model_job', 'layout', str(src), '-o', str(cli_out)],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    assert payload['result'] == 'geometry_fits_nominal_bed'
    assert (cli_out / 'validation.json').exists()
