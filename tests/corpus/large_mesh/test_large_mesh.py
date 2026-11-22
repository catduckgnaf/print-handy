"""Large-mesh intake tier (#1790): real multi-MB sources, fetched by SHA-256.

Each case folder holds a case.toml with the source, license record, file
hashes, the measured intake result and stage budgets; the model files are
never committed. Without the fetched files every test here skips and says
how to fetch them, so the normal PR lane only collects them. The
large-mesh job of the model-corpus workflow fetches the files
(``python -m tests.corpus.large_mesh.fetch``), builds the native kernel and
runs this module three times: with the kernel (budgets enforced), with
``PRINT_HANDY_LARGE_MESH_ENGINE=numpy`` (the kernel switched off, so the
numpy twin serves, as on an install without the wheel; its own budgets) and
with ``PRINT_HANDY_BOUNDS_IMPL=python`` (the Python loops, timed only).
Every run must give the measured parts and the same combined 3MF model
bytes, and the audit of the combined file reads the combine step's recorded
mesh tables instead of its XML (#1790).

Stage timings go to stdout and, when PRINT_HANDY_LARGE_MESH_TIMING names a
file, are appended to it as JSON lines. PRINT_HANDY_LARGE_MESH_REQUIRE=1 (the
workflow job) turns a missing file into a failure instead of a skip.
"""
import hashlib
import json
import os
import time
import zipfile
from pathlib import Path

import pytest

from print_handy.core import geometry_layout, mesh_fast, mesh_native, mesh_sidecar, model_parts
from print_handy.core import model_job as mj
from print_handy.jobs import prompts

from . import fetch

CASES = fetch.cases()
# Intake cases time the unit question, load_parts and combine; repair cases
# (``kind = "repair"``, #1777) pin the print repair skill's scans instead
# (test_repair_fixture.py).
INTAKE = {name: case for name, case in CASES.items() if case.get('kind', 'intake') == 'intake'}


def _case_files(name):
    case = CASES[name]
    root = fetch.default_root() / name
    paths = []
    for row in case['file']:
        path = root / row['name']
        if not path.is_file():
            message = (f'large-mesh case {name!r} is not fetched ({path} is missing); '
                       'run: python -m tests.corpus.large_mesh.fetch')
            if fetch.local_only(case):
                message = (f'large-mesh case {name!r} has no public source and is not in place '
                           f'({path} is missing); put its files there by hand')
            elif os.environ.get('PRINT_HANDY_LARGE_MESH_REQUIRE'):
                pytest.fail(message)
            pytest.skip(message)
        if path.stat().st_size != row['bytes']:
            pytest.fail(f'{path}: {path.stat().st_size} bytes, the manifest pins {row["bytes"]}')
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1 << 22), b''):
                digest.update(chunk)
        if digest.hexdigest() != row['sha256']:
            pytest.fail(f'{path}: SHA-256 differs from the manifest; refetch it')
        paths.append(path)
    return case, paths


def _record(name, impl, timings):
    row = {'case': name, 'impl': impl, 'native': mesh_native.available(), **timings}
    print('LARGE-MESH TIMING', json.dumps(row))
    target = os.environ.get('PRINT_HANDY_LARGE_MESH_TIMING')
    if target:
        with open(target, 'a', encoding='utf-8') as out:
            out.write(json.dumps(row) + '\n')


def test_manifests_are_complete():
    assert CASES, 'no large-mesh cases'
    for name, case in CASES.items():
        for key in ('title', 'source_page', 'site', 'license', 'attribution', 'redistribute'):
            assert case.get(key) not in (None, ''), (name, key)
        assert case['file'], name
        for row in case['file']:
            assert len(row['sha256']) == 64 and row['bytes'] > 0, (name, row['name'])
        if name in INTAKE:
            assert len(case['expect']['part']) == len(case['file']), name
        else:
            assert case['kind'] == 'repair', name
            assert {row.get('role') for row in case['file']} == {'original', 'fixed'}, name


@pytest.mark.timeout(3600)
@pytest.mark.parametrize('name', sorted(INTAKE))
def test_large_mesh_intake(name, tmp_path, monkeypatch):
    case, paths = _case_files(name)
    impl = os.environ.get('PRINT_HANDY_BOUNDS_IMPL', 'auto').strip().lower()
    engine = os.environ.get('PRINT_HANDY_LARGE_MESH_ENGINE', '').strip().lower()
    if engine == 'numpy':
        # An install without the wheel: the kernel is unavailable, numpy serves.
        monkeypatch.setattr(mesh_native, '_load', lambda: None)
        impl = 'numpy'
    native = mesh_native.available()
    if os.environ.get('PRINT_HANDY_MESH_REQUIRE_NATIVE') and impl != 'python':
        assert native, 'native mesh kernel required but unavailable'
    timings = {}

    def timed(stage, fn):
        start = time.perf_counter()
        result = fn()
        timings[stage] = round(time.perf_counter() - start, 3)
        return result

    expect = case['expect']
    timed('parts_unit_question', lambda: prompts.parts_unit_question({'scale': None}, paths))
    for path, want in zip(paths, expect['part'], strict=True):
        assert path.name == want['file']
        parts, _, _, _ = timed(f'load_parts:{path.name}', lambda path=path: geometry_layout.load_parts(path))
        assert len(parts) == 1
        triangles = parts[0]['triangles']
        info = timed(f'describe:{path.name}', lambda t=triangles: geometry_layout.describe_triangles(t))
        for key in ('faces', 'dimensions_mm', 'shell_count', 'watertight', 'manifold', 'abs_volume_mm3', 'area_mm2'):
            assert info[key] == want[key], (path.name, key, info[key])
    target = tmp_path / f'{name}-parts.3mf'
    result = timed('combine', lambda: model_parts.combine(target, [str(p) for p in paths], expect['bed_mm']))
    assert mesh_sidecar.path_for(target).is_file()

    def no_parse(*args, **kwargs):
        raise AssertionError('the audit parsed the combined XML instead of the recorded tables')
    with monkeypatch.context() as patch:
        patch.setattr(mesh_fast, '_parse_member', no_parse)
        audit = timed('audit', lambda: mj.audit(target))
    _record(name, impl, timings)
    assert result['objects'] == expect['objects']
    assert result['on_one_bed'] == expect['on_one_bed']
    assert 'mesh_audit_error' not in audit, audit.get('mesh_audit_error')
    assert audit['mesh_audit']['triangle_count'] == sum(p['faces'] for p in expect['part'])
    with zipfile.ZipFile(target) as archive:
        model = archive.read('3D/3dmodel.model')
    assert hashlib.sha256(model).hexdigest() == expect['model_sha256']
    budget = None
    if native and impl != 'python':
        budget = case['budget']
    elif impl == 'numpy':
        budget = case['budget_numpy']
    if budget is not None:
        over = {stage: seconds for stage, seconds in timings.items()
                if seconds > budget.get(stage.split(':', 1)[0], float('inf'))}
        assert not over, f'stages over budget {budget}: {over}'


def test_fetch_refuses_a_changed_file(tmp_path, monkeypatch):
    """A download whose SHA-256 differs is deleted and fails the run (the CI
    fetch step), never kept or skipped."""
    import subprocess
    import sys

    from tests.corpus import fetch_complex_corpus as fetcher
    case = {'site': 'printables', 'site_model_id': '1',
            'file': [{'name': 'part.stl', 'site_file_id': '2', 'bytes': 5, 'sha256': '0' * 64}]}
    monkeypatch.setattr(fetcher, 'printables_link', lambda file_id, model_id: 'https://example.invalid/x')
    monkeypatch.setattr(fetcher, 'download', lambda url, dest: Path(dest).write_bytes(b'12345'))
    with pytest.raises(RuntimeError, match='different SHA-256'):
        fetch.fetch_case('changed', case, tmp_path)
    assert not (tmp_path / 'changed' / 'part.stl').exists()
    # The module entry point exits non-zero on the same refusal.
    script = ("import sys; from pathlib import Path; from tests.corpus import fetch_complex_corpus as f;"
              "from tests.corpus.large_mesh import fetch;"
              "f.printables_link = lambda *a: 'https://example.invalid/x';"
              "f.download = lambda url, dest: Path(dest).write_bytes(b'changed');"
              "sys.exit(fetch.main(['--root', sys.argv[1]]))")
    done = subprocess.run([sys.executable, '-c', script, str(tmp_path)], capture_output=True, text=True,
                          cwd=Path(__file__).resolve().parents[3])
    assert done.returncode != 0
    assert 'refusing a changed file' in done.stderr
