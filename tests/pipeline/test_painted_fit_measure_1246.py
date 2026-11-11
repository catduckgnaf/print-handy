"""#1246: the painted-plate fit measures each scale step instead of writing a 3MF.

``export_color_geometry(source, None, ...)`` runs every check of a written
export and returns the same part records, build bounds and prime tower
placement, with no package built or written. These tests pin:

* report equivalence: over the repo's painted, multi-part, height-change,
  cutter and multi-plate sources (and the painted corpus-v1 models when
  ``PRINT_HANDY_PRIVATE_CORPUS`` is set), at scales from 3x down to the 50%
  floor, with and without a prime tower, the measured report equals the
  written one, or both refuse with the same error;
* verdict equivalence: ``painted_fit`` (scale, blocking plate, blocking
  layout) equals a frozen copy of the pre-#1246 fit, which wrote a full
  export per step, for every source, ceiling and tower setting;
* the cost: a fit writes nothing and parses the source once however many
  steps and plates it tries, and a paired timing gate.
"""
from __future__ import annotations

import os
import re
import tempfile
import time
import zipfile
from pathlib import Path

import pytest

from print_handy.core import color_geometry, model_job, package_remap, source_colors
from print_handy.core.color_geometry import export_color_geometry

from .test_color_geometry import (_cut_recess_and_plug_source, _cutter_source, _painted_two_plate_source,
                                  _project_settings)

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / 'tests' / 'corpus' / 'cases'
REPO_SOURCES = ['prusa-mmu-project', 'height-color-changes', 'assembly-2c-tight', 'prime-tower-clearance',
                'must-shrink', 'islands-144-four-color', 'plates-multicolor', 'witch-ghost-cults3d',
                'core-basematerials-parts']
CORPUS = os.environ.get('PRINT_HANDY_PRIVATE_CORPUS')
# The painted 3MFs of corpus-v1 (every model whose inventory reads painted).
CORPUS_PAINTED = [
    '075-ghost-face-slim-caddy--1a89caecc10fbf05bd064437c0bcc1754f73f1c95c37e7262044dca8a053deb6/GhostFaceSlim.3mf',
    '104-mini-dragon/MiniDragon_2Colors_80cm_Red-2.3mf',
    '105-mini-dragon/MiniDragon_2Colors_80cm_Red.3mf',
    '106-mini-dragon/MiniDragon_2Colors_90cm-2.3mf',
    '107-mini-dragon/MiniDragon_2Colors_90cm.3mf',
]
SCALES = (3.0, 1.5, 1.0, 0.87, 0.5)
REPORT_KEYS = ('parts', 'build_bounds_mm', 'translation_mm', 'merged_placement', 'prime_tower_xy_mm',
               'prime_tower_candidates_mm', 'cutter_bounds_by_object', 'layer_changes', 'source_sha256')


def _decisions(path):
    """Exact-source decisions: every part on its own filament's slot (1-4)."""
    manifest = source_colors.inventory(path)
    slot = lambda n: (int(n) - 1) % 4 + 1  # noqa: E731
    own = {p['key']: p['source_extruder'] for p in manifest['parts']}
    choices = {}
    for row in source_colors.canonical_prompts(manifest):
        kind, _, rest = row['key'].partition(':')
        choices[row['key']] = {'slot': slot(rest if kind == 'filament' else own.get(rest, 1) or 1)}
    return manifest, {'schema': 'part_slots_v1', 'mode': 'per_part',
                      'source_sha256': manifest['source_sha256'], 'choices': choices}


def _plates(path, manifest):
    try:
        plates = model_job.plate_manifest(path)['plates']
    except RuntimeError:
        plates = [{'id': 1, 'object_ids': sorted({str(p['object_id']) for p in manifest['parts']})}]
    known = {str(p['object_id']) for p in manifest['parts']}
    return [p for p in plates if any(str(o) in known for o in p['object_ids'])]


def _generated(tmp_path):
    out = []
    for name, build in (('painted-two-plates', _painted_two_plate_source), ('cutter', _cutter_source),
                        ('recess-and-plug', _cut_recess_and_plug_source)):
        path = tmp_path / f'{name}.3mf'
        build(path)
        out.append(path)
    return out


def _sources(tmp_path):
    return [CASES / name / 'model.3mf' for name in REPO_SOURCES] + _generated(tmp_path)


def _outcome(call):
    try:
        mapping, report = call()
    except Exception as exc:  # noqa: BLE001 - the comparison is the point
        return ('raised', type(exc).__name__, str(exc))
    return ('ok', mapping, {key: report.get(key) for key in REPORT_KEYS})


def _compare_reports(path, scales):
    compared = refused = 0
    manifest, decisions = _decisions(path)
    selections = [None] + [set(p['object_ids']) for p in _plates(path, manifest)]
    for object_ids in selections:
        for scale in scales:
            for tower in (None, 0.0):
                def written():
                    with tempfile.TemporaryDirectory() as tmp:
                        return export_color_geometry(path, Path(tmp) / 'g.3mf', decisions,
                                                     object_ids=object_ids, scale=scale, tower_mm=tower)
                full = _outcome(written)
                measured = _outcome(lambda: export_color_geometry(
                    path, None, decisions, object_ids=object_ids, scale=scale, tower_mm=tower))
                assert measured == full, (path.name, object_ids, scale, tower)
                compared += 1
                refused += full[0] == 'raised'
    return compared, refused


def test_measured_reports_equal_written_ones(tmp_path):
    compared = refused = 0
    for path in _sources(tmp_path):
        done, failed = _compare_reports(path, SCALES)
        compared, refused = compared + done, refused + failed
    # The matrix really covers both outcomes, including the tower refusal.
    assert compared > 100 and 0 < refused < compared


@pytest.mark.skipif(not CORPUS, reason='corpus-v1 not downloaded here; the model-corpus workflow '
                                       'sets PRINT_HANDY_PRIVATE_CORPUS')
@pytest.mark.parametrize('rel', CORPUS_PAINTED)
@pytest.mark.timeout(900)  # a written export of a 4 MB painted model takes seconds per step
def test_corpus_painted_models_measure_and_fit_like_the_written_export(rel):
    path = Path(CORPUS) / rel
    manifest, decisions = _decisions(path)
    assert manifest['painted']
    _compare_reports(path, (3.0, 1.0))
    view = model_job._per_part_view(manifest, decisions)
    plates = _plates(path, manifest)
    for ceiling, tower in ((3.0, None), (1.0, 0.0)):
        assert model_job.painted_fit(path, view, plates, ceiling, tower) ==             _old_painted_fit(path, view, plates, ceiling, tower)


def _old_painted_fit(src, view, plates, ceiling=1.0, tower_mm=None):
    """The pre-#1246 fit, frozen: a full written export at every step."""
    def plate_scale(object_ids, ceiling):
        steps = [ceiling] + [s for s in model_job.PAINTED_SCALE_STEPS if s < ceiling - 1e-9]
        with tempfile.TemporaryDirectory(prefix='painted-fit-') as tmp:
            for scale in steps:
                if scale > ceiling + 1e-9:
                    continue
                try:
                    _, geometry = export_color_geometry(src, Path(tmp) / 'g.3mf', view, object_ids=object_ids,
                                                        scale=scale, tower_mm=tower_mm)
                except RuntimeError:
                    continue
                if all(0 <= ax[0] and ax[1] <= model_job.BED_LIMIT_MM
                       for row in geometry['parts'] for ax in row['bounds_mm'][:2]):
                    return scale
        return None

    def layout(object_ids, scale):
        try:
            with tempfile.TemporaryDirectory(prefix='painted-layout-') as tmp:
                _, geometry = export_color_geometry(src, Path(tmp) / 'g.3mf', view,
                                                    object_ids=object_ids, scale=scale, tower_mm=0.0)
        except Exception:  # noqa: BLE001
            return None
        bounds = geometry.get('build_bounds_mm') or []
        extent = [round(float(bounds[a][1]) - float(bounds[a][0]), 1) for a in range(2)]
        tower = (float(color_geometry.effective_tower_mm()) if tower_mm is None
                 else float(tower_mm) if float(tower_mm) > 0 else None)
        return {'extent_mm': extent, 'tower_mm': tower, 'scale': float(scale)}

    scale, blocking = ceiling, None
    for plate in plates:
        ids = {str(o) for o in plate['object_ids']}
        fitted = plate_scale(ids, scale)
        if fitted is None:
            result = {'scale': None, 'blocking_plate': plate['id'], 'plates': len(plates)}
            measured = layout(ids, model_job.PAINTED_SCALE_STEPS[-1])
            if measured is not None:
                result['blocking_layout'] = measured
            return result
        if fitted < scale:
            scale, blocking = fitted, plate['id']
    return {'scale': scale, 'blocking_plate': blocking, 'plates': len(plates)}


def test_fit_verdicts_equal_the_written_export_fit(tmp_path):
    verdicts = set()
    for path in _sources(tmp_path):
        manifest, decisions = _decisions(path)
        view = model_job._per_part_view(manifest, decisions)
        plates = _plates(path, manifest)
        for ceiling in (3.0, 1.2, 1.0, 0.99, 0.6):
            for tower in (None, 0.0):
                old = _old_painted_fit(path, view, plates, ceiling, tower)
                new = model_job.painted_fit(path, view, plates, ceiling, tower)
                assert new == old, (path.name, ceiling, tower)
                verdicts.add((new['scale'] is None, new['scale'] == ceiling))
    # fits at the ceiling, fits only lower on the ladder, and no fit at all
    assert verdicts >= {(False, True), (False, False), (True, False)}


def test_a_fit_writes_nothing_and_parses_the_source_once(tmp_path, monkeypatch):
    path = tmp_path / 'painted-two-plates.3mf'
    _painted_two_plate_source(path)
    manifest, decisions = _decisions(path)
    view = model_job._per_part_view(manifest, decisions)
    plates = _plates(path, manifest)
    assert len(plates) == 2

    def no_write(*args, **kwargs):
        raise AssertionError('a painted fit wrote a package')
    monkeypatch.setattr(package_remap, 'write_package_atomically', no_write)
    parsed, exports = [], []
    real_require, real_export = color_geometry.require_millimeter, color_geometry.export_color_geometry
    monkeypatch.setattr(color_geometry, 'require_millimeter', lambda root: (parsed.append(root), real_require(root)))
    monkeypatch.setattr(color_geometry, 'export_color_geometry',
                        lambda *a, **k: (exports.append(k.get('scale')), real_export(*a, **k))[1])
    # 8x does not fit either plate: each plate steps down the ladder.
    fit = model_job.painted_fit(path, view, plates, 8.0, None)
    assert fit['scale'] == 1.0 and exports == [8.0, 1.0, 1.0]
    # Three model members, each parsed once per selection (two plates).
    assert len(parsed) == 3 * len(plates)


def _dense_painted_source(path, n=120, size=90.0):
    """One painted part: an n x n vertex surface, every other triangle painted filament 2."""
    step = size / (n - 1)
    vertices = ''.join(f'<vertex x="{i * step}" y="{j * step}" z="{(i * j) % 5 * 0.5}"/>'
                       for j in range(n) for i in range(n))
    triangles = []
    for j in range(n - 1):
        for i in range(n - 1):
            a, b, c, d = j * n + i, j * n + i + 1, (j + 1) * n + i, (j + 1) * n + i + 1
            triangles.append(f'<triangle v1="{a}" v2="{b}" v3="{c}" paint_color="8"/>')
            triangles.append(f'<triangle v1="{b}" v2="{d}" v3="{c}"/>')
    mesh = (f'<model unit="millimeter"><resources><object id="1"><mesh><vertices>{vertices}</vertices>'
            f'<triangles>{"".join(triangles)}</triangles></mesh></object></resources></model>')
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('3D/Objects/object_1.model', mesh)
        archive.writestr('3D/3dmodel.model', '''<model xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" unit="millimeter"><resources>
          <object id="1" name="Surface"><components><component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>
        </resources><build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 20 30 0"/></build></model>''')
        archive.writestr('Metadata/model_settings.config', '''<config>
          <object id="1"><metadata key="name" value="Surface"/><metadata key="extruder" value="1"/><part id="1"><metadata key="name" value="Surface"/><metadata key="extruder" value="1"/></part></object>
          <plate><metadata key="plater_id" value="1"/><model_instance><metadata key="object_id" value="1"/></model_instance></plate>
        </config>''')
        archive.writestr('Metadata/project_settings.config', _project_settings(['#FF00FF', '#067749']))
        archive.writestr('3D/_rels/3dmodel.model.rels',
                         '<Relationships><Relationship Target="/3D/Objects/object_1.model" Id="rel-1"/></Relationships>')
    return path


def test_measuring_a_fit_is_faster_than_writing_each_step(tmp_path):
    path = _dense_painted_source(tmp_path / 'dense.3mf')
    manifest, decisions = _decisions(path)
    assert manifest['painted'] and manifest['parts']
    view = model_job._per_part_view(manifest, decisions)
    plates = _plates(path, manifest)
    # The measured verdict is the written one before anything is timed.
    assert model_job.painted_fit(path, view, plates, 3.0) == _old_painted_fit(path, view, plates, 3.0)

    def best(fn, runs=5):
        times = []
        for _ in range(runs):
            start = time.perf_counter()
            fn()
            times.append(time.perf_counter() - start)
        return min(times)
    old = best(lambda: _old_painted_fit(path, view, plates, 3.0))
    new = best(lambda: model_job.painted_fit(path, view, plates, 3.0))
    # Measured about 0.33 on this source (one parse, nothing serialized);
    # the gate leaves room for a loaded runner.
    assert new / old < 0.6, (new, old)


@pytest.mark.parametrize('bad', ['8G', '', '1', '80'])
def test_a_paint_code_the_written_export_refuses_is_refused_when_measuring(tmp_path, bad):
    # duckcatgnaf on #1264: the measure path must run the paint remap too.
    # The census regex skips a non-hex code ('8G') and an empty one, so only
    # the remap sees them; '1' is truncated and '80' has trailing data.
    path = tmp_path / 'bad-paint.3mf'
    _painted_two_plate_source(path)
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    holder = members['3D/Objects/object_1.model'].decode()
    holder = holder.replace('<triangle v1="0" v2="2" v3="1"/>',
                            '<triangle v1="0" v2="2" v3="1" paint_color="8"/>'
                            f'<triangle v1="1" v2="2" v3="0" slic3rpe:mmu_segmentation="{bad}" '
                            'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06"/>')
    members['3D/Objects/object_1.model'] = holder.encode()
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    manifest, decisions = _decisions(path)
    assert manifest['painted']

    def written():
        with tempfile.TemporaryDirectory() as tmp:
            return export_color_geometry(path, Path(tmp) / 'g.3mf', decisions, scale=1.0)
    full = _outcome(written)
    assert full[0] == 'raised' and full[1] == 'ValueError'
    assert _outcome(lambda: export_color_geometry(path, None, decisions, scale=1.0)) == full
    view = model_job._per_part_view(manifest, decisions)
    plates = _plates(path, manifest)
    with pytest.raises(ValueError, match=re.escape(full[2])):
        _old_painted_fit(path, view, plates, 1.0)
    with pytest.raises(ValueError, match=re.escape(full[2])):
        model_job.painted_fit(path, view, plates, 1.0)
