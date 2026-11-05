"""Phase 2 support-clearance hardening tests (#922).

The Pixel_Patina fixture files named in the issue are not in the repo, so
these tests build synthetic fixtures reproducing the stated geometry
conditions (angles, clearances, areas, ratios) and say so in the PR body.
"""
import json
import math
import struct
import zipfile
from pathlib import Path

import pytest

from print_handy.core import failure_report, model_job, support_clearance
from print_handy.core import slice_warnings
from print_handy.core.slice_recipe import RECIPE_VERSION

V12_FIXTURE = Path(__file__).parent.parent / "fixtures" / "modeled_supports_shelf.3mf"


# ---------------------------------------------------------------- mesh helpers

def _orient(tris, center):
    import numpy as np
    out = []
    c = np.array(center, float)
    for tri in tris:
        t = np.array(tri, float)
        n = np.cross(t[1] - t[0], t[2] - t[0])
        if np.dot(n, t.mean(0) - c) < 0:
            t = t[::-1]
        out.append([tuple(v) for v in t])
    return out


def box(x0, y0, z0, x1, y1, z1):
    corners = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    idx = {c: i for i, c in enumerate(corners)}
    quads = [
        [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)],
        [(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)],
        [(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)],
        [(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)],
        [(x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1)],
        [(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)],
    ]
    tris = []
    for q in quads:
        tris.append([q[0], q[1], q[2]])
        tris.append([q[0], q[2], q[3]])
    return _orient(tris, ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2))


def prism_xz(poly, y0, y1):
    """Extrude an XZ polygon along Y."""
    tris = []
    n = len(poly)
    for i in range(n):
        a = poly[i]
        b = poly[(i + 1) % n]
        quad = [(a[0], y0, a[1]), (b[0], y0, b[1]), (b[0], y1, b[1]), (a[0], y1, a[1])]
        tris.append([quad[0], quad[1], quad[2]])
        tris.append([quad[0], quad[2], quad[3]])
    for y in (y0, y1):
        for i in range(1, n - 1):
            tris.append([(poly[0][0], y, poly[0][1]), (poly[i][0], y, poly[i][1]),
                         (poly[i + 1][0], y, poly[i + 1][1])])
    cx = sum(p[0] for p in poly) / n
    cz = sum(p[1] for p in poly) / n
    return _orient(tris, (cx, (y0 + y1) / 2, cz))


def write_3mf(path, objects):
    """objects: [(name, tris, extra_attrs, item_transform_or_None)]"""
    parts = []
    objs = []
    items = []
    for i, (name, tris, extra, transform) in enumerate(objects):
        oid = str(i + 2)
        verts = []
        seen = {}
        faces = []
        for tri in tris:
            face = []
            for v in tri:
                key = tuple(round(float(c), 6) for c in v)
                if key not in seen:
                    seen[key] = len(verts)
                    verts.append(key)
                face.append(seen[key])
            faces.append(face)
        vxml = ''.join(f'<vertex x="{v[0]}" y="{v[1]}" z="{v[2]}"/>' for v in verts)
        txml = ''.join(f'<triangle v1="{f[0]}" v2="{f[1]}" v3="{f[2]}"/>' for f in faces)
        objs.append(f'<object id="{oid}" type="model" name="{name}"{extra}>'
                    f'<mesh><vertices>{vxml}</vertices><triangles>{txml}</triangles></mesh></object>')
        tr = f' transform="{transform}"' if transform else ''
        items.append(f'<item objectid="{oid}"{tr}/>')
    model = ('<?xml version="1.0" encoding="utf-8"?>'
             '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" unit="millimeter">'
             f'<resources>{"".join(objs)}</resources><build>{"".join(items)}</build></model>')
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('3D/3dmodel.model', model)
    return path


def write_stl(path, tris):
    with open(path, 'wb') as fh:
        fh.write(b'\0' * 80)
        fh.write(struct.pack('<I', len(tris)))
        for tri in tris:
            fh.write(struct.pack('<3f', 0, 0, 0))
            for v in tri:
                fh.write(struct.pack('<3f', *[float(c) for c in v]))
            fh.write(struct.pack('<H', 0))
    return path


def wedge_file(tmp_path, angle_deg=40.0, name='wedge.3mf'):
    """Shelf at Z 6.25 plus a sloped wedge rooting into it at x=0.25.

    The slope's low-clearance strip is two raster columns wide (< 2.0 mm),
    its lowest sampled clearance is ~0.42 mm and the column at the root
    itself is a zero-clearance anchor edge.
    """
    shelf = box(0, 0, 0, 100, 100, 6.25)
    rise = math.tan(math.radians(angle_deg))
    poly = [(0.25, 6.25), (4.25, 6.25 + 4 * rise), (4.25, 13.0), (0.25, 13.0)]
    wedge = prism_xz(poly, 0, 100)
    return write_3mf(tmp_path / name, [('SHELF', shelf, '', None), ('WEDGE', wedge, '', None)])


# ---------------------------------------------------------------- gap 1

def test_sloped_wedge_root_is_not_risky(tmp_path):
    rep = support_clearance.analyze(str(wedge_file(tmp_path)))
    assert rep['risky'] is False
    assert support_clearance.CODE_CLEARANCE not in support_clearance.codes(rep)
    roots = [r for r in rep['regions'] if r.get('kind') == 'sloped_wedge_root']
    assert roots, 'the excluded strip should be recorded as a wedge root, not dropped silently'
    assert roots[0]['width_mm'] < 2.0


def test_shallow_wide_sloped_low_clearance_is_still_flagged(tmp_path):
    # A 10 degree slope is not a self-supporting wedge root in the exempt
    # band and its low-clearance strip is wider than 2.0 mm: still risky.
    rep = support_clearance.analyze(str(wedge_file(tmp_path, angle_deg=10.0, name='shallow.3mf')))
    assert rep['risky'] is True
    assert support_clearance.CODE_CLEARANCE in support_clearance.codes(rep)


# ---------------------------------------------------------------- gap 4

def supportable_file(tmp_path):
    low = box(0, 0, 2.65, 60, 34, 5.0)       # 2040 mm2 underside at Z 2.65
    high = box(0, 0, 13.10, 60, 34, 15.0)    # 2040 mm2 underside at Z 13.10
    return write_3mf(tmp_path / 'supportable.3mf',
                     [('LOW SHELF', low, '', None), ('HIGH SHELF', high, '', None)])


def _support_gcode_for(regions):
    lines = [';TYPE:Support']
    for r in regions:
        (x0, y0), (x1, y1) = r['bbox_mm']
        nx = max(1, math.ceil((x1 - x0) / 3.0))
        ny = max(1, math.ceil((y1 - y0) / 3.0))
        for i in range(nx):
            for j in range(ny):
                x = x0 + (i + 0.5) * 3.0
                y = y0 + (j + 0.5) * 3.0
                lines.append(f'G1 X{x:.2f} Y{y:.2f} E1.0')
    lines.append(';TYPE:Outer wall')
    return '\n'.join(lines) + '\n'


def test_supportable_overhang_regions_emitted_and_verified(tmp_path):
    rep = support_clearance.analyze(str(supportable_file(tmp_path)))
    assert rep['risky'] is False
    regions = [r for r in rep['regions'] if r.get('kind') == 'supportable_overhang']
    assert sorted(round(r['z_mm']['min'], 2) for r in regions) == [2.65, 13.10]
    assert all(r['area_mm2'] >= 500 for r in regions)

    ok = support_clearance.verify_support_coverage(rep['regions'], _support_gcode_for(regions))
    assert ok['checked'] is True and ok['ok'] is True
    assert all(r['coverage'] == 1.0 for r in ok['regions'])

    bad = support_clearance.verify_support_coverage(rep['regions'], 'G1 X1 Y1 E1\n')
    assert bad['checked'] is True and bad['ok'] is False


# ---------------------------------------------------------------- gap 3

def single_mesh_cantilever_tris():
    tris = []
    tris += box(0, 0, 23.0, 100, 100, 25.0)          # 10000 mm2 flat overhang at Z 23
    for x, y in ((2, 2), (93, 2), (2, 93), (93, 93)):  # sparse baked-in pillars
        tris += box(x, y, 0, x + 5, y + 5, 23.0)
    return tris


def test_single_mesh_high_overhang_ratio_detected_3mf_and_stl(tmp_path):
    tris = single_mesh_cantilever_tris()
    for path in (write_3mf(tmp_path / 'one.3mf', [('ONEPIECE', tris, '', None)]),
                 write_stl(tmp_path / 'one.stl', tris)):
        rep = support_clearance.analyze(str(path))
        info = rep.get('single_mesh') or {}
        assert info.get('high_overhang_ratio') is True, path
        assert info['overhang_mm2'] >= 5000
        assert info['ratio'] >= 5
        assert any(f['code'] == support_clearance.CODE_CANTILEVER for f in rep['findings'])
        # A warning, not a clearance risk: it must not feed the low-gap guard.
        assert rep['risky'] is False


def test_cantilever_override_record_is_bound_to_warning_and_hash(tmp_path):
    src = write_stl(tmp_path / 'one.stl', single_mesh_cantilever_tris())
    job = {'downloaded_file': str(src)}
    finding = {'objects': ['ONEPIECE'], 'texts': ['object ONEPIECE has a floating cantilever']}
    slice_warnings.record_unsupported_cantilever_override(job, finding, source_file=str(src))
    rec = job['unsupported_cantilever_override']
    assert rec['code'] == 'UNSUPPORTED_CANTILEVER_OVERRIDE'
    assert rec['source_sha256'] == model_job.file_sha256(src)
    assert rec['warning'] == finding['texts']


# ---------------------------------------------------------------- gap 2

def _guard_job(tmp_path, via):
    folder = tmp_path / 'job'
    folder.mkdir()
    gcode = folder / 'test.gcode'
    gcode.write_text('G1 X1\n')
    job = {
        'downloaded_file': str(V12_FIXTURE),
        'gcode': str(gcode),
        'gcode_verified': True,
        'support_clearance': support_clearance.analyze(str(V12_FIXTURE)),
        'color_decisions': {},
        'artifact_binding': {
            'source_sha256': model_job.file_sha256(V12_FIXTURE),
            'decisions_sha256': model_job.decisions_digest({}),
            'gcode_sha256': model_job.file_sha256(gcode),
            'recipe_version': RECIPE_VERSION,
            'recipe_sha256': 'dummy',
        },
        'slice_recipe': {'version': RECIPE_VERSION, 'sha256': 'dummy'},
    }
    support_clearance.record_clearance_acknowledgement(
        job, gcode=str(gcode), source_file=str(V12_FIXTURE), via=via)
    return job, gcode


def test_with_supports_acknowledgement_does_not_satisfy_low_clearance(tmp_path):
    job, gcode = _guard_job(tmp_path, via='with_supports')
    with pytest.raises(RuntimeError, match='insufficient support clearance'):
        model_job.require_current_artifact(job, str(gcode))


def test_without_supports_acknowledgement_satisfies_low_clearance(tmp_path):
    # The explicit as-authored decision (the witch-ghost corpus case pins
    # this flow behavior): recorded via='without_supports', it satisfies.
    job, gcode = _guard_job(tmp_path, via='without_supports')
    try:
        model_job.require_current_artifact(job, str(gcode))
    except RuntimeError as exc:
        assert 'insufficient support clearance' not in str(exc)


def test_print_anyway_acknowledgement_satisfies_low_clearance(tmp_path):
    job, gcode = _guard_job(tmp_path, via='print_anyway')
    try:
        model_job.require_current_artifact(job, str(gcode))
    except RuntimeError as exc:
        # The clearance guard is satisfied; a later, unrelated guard in
        # this synthetic job (its dummy recipe record) may still refuse.
        assert 'insufficient support clearance' not in str(exc)


# ---------------------------------------------------------------- gap 5

def test_extend_underside_respects_build_item_transform(tmp_path):
    # Local box bottom at Z 0, build transform lifts it to Z 0.4.
    path = write_3mf(tmp_path / 'lifted.3mf',
                     [('PLATE', box(0, 0, 0, 60, 40, 2.0), '', '1 0 0 0 1 0 0 0 1 0 0 0.4')])
    before = support_clearance.analyze(str(path))
    assert before['risky'] is True
    res = support_clearance.extend_underside(path, gap_mm=0.4)
    assert res['modified_vertices'] > 0
    assert res['added_volume_mm3'] == pytest.approx(60 * 40 * 0.4, rel=0.05)
    after = support_clearance.analyze(str(path))
    assert after['risky'] is False


def test_extend_underside_respects_component_transform(tmp_path):
    # Mesh object local Z 0..2 placed through a component transform +0.4 Z
    # inside a wrapper object placed by the build item.
    mesh = box(0, 0, 0, 60, 40, 2.0)
    verts = []
    seen = {}
    faces = []
    for tri in mesh:
        face = []
        for v in tri:
            if v not in seen:
                seen[v] = len(verts)
                verts.append(v)
            face.append(seen[v])
        faces.append(face)
    vxml = ''.join(f'<vertex x="{v[0]}" y="{v[1]}" z="{v[2]}"/>' for v in verts)
    txml = ''.join(f'<triangle v1="{f[0]}" v2="{f[1]}" v3="{f[2]}"/>' for f in faces)
    model = ('<?xml version="1.0" encoding="utf-8"?>'
             '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" unit="millimeter">'
             '<resources>'
             f'<object id="2" type="model" name="PLATE"><mesh><vertices>{vxml}</vertices>'
             f'<triangles>{txml}</triangles></mesh></object>'
             '<object id="3" type="model" name="WRAP"><components>'
             '<component objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 0.4"/></components></object>'
             '</resources><build><item objectid="3"/></build></model>')
    path = tmp_path / 'component.3mf'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('3D/3dmodel.model', model)
    assert support_clearance.analyze(str(path))['risky'] is True
    res = support_clearance.extend_underside(path, gap_mm=0.4)
    assert res['modified_vertices'] > 0
    assert support_clearance.analyze(str(path))['risky'] is False


def test_sink_model_reports_removed_volume(tmp_path):
    path = write_3mf(tmp_path / 'onshelf.3mf', [('PLATE', box(0, 0, 0, 60, 40, 2.0), '', None)])
    res = support_clearance.sink_model(path, dz=0.4)
    assert res['removed_volume_mm3'] == pytest.approx(60 * 40 * 0.4, rel=0.10)


# ---------------------------------------------------------------- gap 8

def test_openscad_3mf_without_pid_matches_single_part_stl(tmp_path):
    from print_handy.core import source_colors
    tris = []
    tris += box(0, 0, 2.65, 60, 34, 5.0)
    mf = write_3mf(tmp_path / 'openscad.3mf', [('OpenSCAD_Model', tris, '', None)])
    stl = write_stl(tmp_path / 'same.stl', tris)

    rep_mf = support_clearance.analyze(str(mf))
    rep_stl = support_clearance.analyze(str(stl))
    assert rep_mf['risky'] == rep_stl['risky']
    kinds_mf = sorted((r['kind'], r['z_mm']['min']) for r in rep_mf['regions'])
    kinds_stl = sorted((r['kind'], r['z_mm']['min']) for r in rep_stl['regions'])
    assert kinds_mf == kinds_stl

    manifest = source_colors.inventory(mf)
    assert len(manifest['parts']) == 1
    assert manifest['parts'][0]['name'] == 'OpenSCAD_Model'
    assert manifest['requires_choices'] is False
    decisions = {'source_sha256': manifest['source_sha256'], 'mode': 'single_color',
                 'schema': 'part_slots_v1',
                 'selection': {'slot': 1, 'color': 'white', 'material': 'PLA', 'spoolman_id': 10}}
    validated = source_colors.validate_decisions(manifest, decisions, {},
                                                 model_job.resolve_filament_selection)
    source_colors.require_single_color(manifest, validated)


# ---------------------------------------------------------------- gap 9

def test_gcode_3mf_detection_and_failure_classification(tmp_path):
    from print_handy.core import prepared_intake
    pkg = tmp_path / 'model.gcode.3mf'
    with zipfile.ZipFile(pkg, 'w') as z:
        z.writestr('3D/3dmodel.model',
                   '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
                   '<resources></resources><build></build></model>')
        z.writestr('Metadata/plate_1.gcode', 'G1 X1 Y1\n')
    assert prepared_intake.is_gcode_3mf(pkg) is True
    assert prepared_intake.is_gcode_3mf(supportable_file(tmp_path)) is False

    rep = failure_report.describe(ValueError(prepared_intake.gcode_3mf_message(pkg.name)), 'intake')
    assert rep.signature == 'gcode_3mf_package'
    assert rep.signature != 'bad_3mf_structure'
    assert 'already-sliced' in rep.cause
