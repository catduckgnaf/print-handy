"""#1779: a source whose own settings already enable supports.

The first slice then IS the with-supports slice (elf shorts, intake 11e56d166af9). Slicing "with
supports" again repeated 118 s of identical work, and the Ready card could not give the
without-supports numbers because the first slice carried supports. The without-supports variant must
be a real run with enable_support=0, and it may run beside the first slice.
"""
# ruff: noqa: ASYNC240  (test slicer stubs write tiny files; blocking pathlib is fine here)
import asyncio
import json
import zipfile
from pathlib import Path

import pytest

from print_handy.core import preview_parallel
from print_handy.core.background_slice import BackgroundSlice, supports_variant_equals_first

RISKY = {'risky': True, 'regions': [], 'findings': []}
PROCESS = {'enable_support': '1', 'support_type': 'tree(auto)', 'support_style': 'tree_slim', 'layer_height': '0.16'}


def _source(folder, supports=True):
    path = folder / 'm.3mf'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('Metadata/project_settings.config', json.dumps({'enable_support': '1' if supports else '0'}))
    return path


def _setup(tmp_path, supports=True):
    folder = tmp_path / 'intake'
    folder.mkdir()
    src = _source(folder, supports)
    job = {'color_manifest': {'source_sha256': 'src-a'}, 'downloaded_file': str(src), 'support_clearance': RISKY}
    (folder / 'JOB.json').write_text(json.dumps(job))
    return folder, folder / 'JOB.json'


def _slice_func(launches, process=None, hold=None):
    """A slicer stand-in that resolves enable_support the way slice_model does: the decision, else the author's."""
    base = dict(process or PROCESS)

    async def slice_func(path, work, _runner, **kwargs):
        data = json.loads(Path(path).read_text())
        decided = (data.get('support_decision') or {}).get('supports')
        resolved = dict(base)
        if decided is not None:
            resolved['enable_support'] = '1' if decided else '0'
        launch = {'folder': str(Path(path).parent), 'decided': decided, 'process': resolved,
                  'machine': {'printer': 'ad5x'}, 'filament': {'type': 'PLA'}}
        launches.append(launch)
        if hold is not None:
            await hold(launch)
        run = Path(path).parent / 'sliced' / 'orca' / f'run{len(launches)}'
        run.mkdir(parents=True, exist_ok=True)
        (run / 'process.json').write_text(json.dumps(resolved))
        gcode = run / 'plate_1.gcode'
        gcode.write_text(f"G1 X1\n; CONFIG_BLOCK_START\n; enable_support = {resolved['enable_support']}\n")
        data['gcode'] = str(gcode)
        data['slice_config'] = [str(run / 'process.json')]
        data['provenance'] = {'slice_warnings': {'support_needed': False, 'objects': [], 'support_texts': []},
                              'supports_on': resolved['enable_support'] == '1', 'support_decided': decided is not None}
        Path(path).write_text(json.dumps(data))
        return [gcode]
    return slice_func


def _fingerprint(launch):
    return json.dumps({k: launch[k] for k in ('process', 'machine', 'filament')}, sort_keys=True)


@pytest.mark.parametrize('parallel', [False, True])
def test_authored_supports_source_never_launches_two_identical_slices(tmp_path, monkeypatch, parallel):
    monkeypatch.setattr(preview_parallel, 'enabled', lambda *a, **k: parallel)

    async def run():
        folder, job_path = _setup(tmp_path)
        launches = []
        runner = BackgroundSlice()
        await runner.start(job_path, 'b', _slice_func(launches))
        await runner.settle()
        prints = [_fingerprint(launch) for launch in launches]
        assert len(prints) == len(set(prints)), f'two launches resolved identical settings: {launches}'
        nosup = [launch for launch in launches if launch['process']['enable_support'] == '0']
        assert len(nosup) == 1 and nosup[0]['decided'] is False  # the without-supports variant is real
        assert len(launches) == 2  # the first slice (authored supports) and the real no-supports slice
        assert runner.variant_reused == 'first_slice'
    asyncio.run(run())


def test_nosupports_variant_config_has_supports_disabled_and_gives_real_numbers(tmp_path, monkeypatch):
    monkeypatch.setattr(preview_parallel, 'enabled', lambda *a, **k: False)

    async def run():
        folder, job_path = _setup(tmp_path)
        launches = []
        runner = BackgroundSlice()
        await runner.start(job_path, 'b', _slice_func(launches))
        await runner.settle()
        # the Ready card reads these two for its numbers
        assert runner.job_with_supports is not None and runner.supports_result is not None
        assert runner.job_no_supports is not None and runner.result is not None
        nosup_process = json.loads(Path(runner.job_no_supports['slice_config'][0]).read_text())
        assert nosup_process['enable_support'] == '0'
        assert 'enable_support = 0' in Path(runner.result[0]).read_text()
        sup_process = json.loads(Path(runner.job_with_supports['slice_config'][0]).read_text())
        assert sup_process['enable_support'] == '1'
        # choosing without supports takes the real variant; choosing supports takes the first slice
        nosup = await runner.take_no_supports()
        assert nosup and 'enable_support = 0' in Path(nosup[0]).read_text()
        assert not (folder / 'variant_nosupports').exists()
        sup = await runner.take_supports()
        assert sup and 'enable_support = 1' in Path(sup[0]).read_text()
    asyncio.run(run())


def test_nosupports_variant_runs_beside_the_first_slice_when_parallel(tmp_path, monkeypatch):
    monkeypatch.setattr(preview_parallel, 'enabled', lambda *a, **k: True)

    async def run():
        folder, job_path = _setup(tmp_path)
        launches = []
        gate = asyncio.Event()

        async def hold(launch):
            if len(launches) == 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), 2)  # each waits for the other: only concurrent runs finish
        runner = BackgroundSlice()
        await runner.start(job_path, 'b', _slice_func(launches, hold=hold))
        await runner.settle()
        assert sorted(launch['process']['enable_support'] for launch in launches) == ['0', '1']
        assert runner.error is None and runner.supports_error is None
    asyncio.run(run())


def test_without_authored_supports_the_supports_variant_still_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(preview_parallel, 'enabled', lambda *a, **k: False)

    async def run():
        folder, job_path = _setup(tmp_path, supports=False)
        launches = []
        runner = BackgroundSlice()
        await runner.start(job_path, 'b', _slice_func(launches, process={**PROCESS, 'enable_support': '0'}))
        await runner.settle()
        assert [launch['process']['enable_support'] for launch in launches] == ['0', '1']
        assert runner.variant_reused is None
    asyncio.run(run())


def test_a_setting_the_decision_would_change_is_not_called_equal(tmp_path):
    folder, job_path = _setup(tmp_path)
    run = folder / 'sliced' / 'orca' / 'r'
    run.mkdir(parents=True)

    def job_with(process):
        (run / 'process.json').write_text(json.dumps(process))
        return {'downloaded_file': str(folder / 'm.3mf'), 'slice_config': [str(run / 'process.json')]}
    assert supports_variant_equals_first(job_with(PROCESS)) is True
    # manual placement with nothing painted: "with supports" would switch it to automatic
    assert supports_variant_equals_first(job_with({**PROCESS, 'support_type': 'tree(manual)'})) is False
    assert supports_variant_equals_first(job_with({**PROCESS, 'enable_support': '0'})) is False
    assert supports_variant_equals_first({'downloaded_file': str(folder / 'm.3mf')}) is False  # no config: unknown


def test_reused_first_slice_rebinds_recipe_on_artifact_and_every_derived_plate(tmp_path, monkeypatch):
    """The reuse rewrites the recorded recipe to the supports decision it now stands for, and every
    binding that pointed at the old recipe follows it, so the launch-time recipe check binds the
    G-code to the decision that was actually sliced (#1936 review)."""
    from print_handy.core import background_slice, slice_recipe
    monkeypatch.setattr(background_slice, 'slice_binding', lambda job, sha, state=None: 'binding')
    folder, job_path = _setup(tmp_path)
    recipe = {'supports_brim': {'brim': 'auto'}, 'layer_height': '0.16'}
    stored = slice_recipe.record(recipe)
    other = 'f' * 64
    job = {'color_manifest': {'source_sha256': 'src-a'}, 'downloaded_file': str(folder / 'm.3mf'),
           'slice_recipe': stored,
           'artifact_binding': {'recipe_sha256': stored['sha256'], 'gcode_sha256': 'g0'},
           'provenance': {'support_decided': False},
           'derived_plates': [
               {'artifact_binding': {'recipe_sha256': stored['sha256']}, 'provenance': {'support_decided': False}},
               {'artifact_binding': {'recipe_sha256': stored['sha256']}},
               {'artifact_binding': {'recipe_sha256': other}},  # bound to another recipe: left alone
           ]}
    runner = BackgroundSlice()
    runner.job_path = job_path
    runner.result = ['first.gcode']
    runner._reuse_first_as_supports(job)

    sup = runner.job_with_supports
    expected = slice_recipe.record({**recipe, 'supports_brim': {'brim': 'auto', 'support_decision': True}})
    assert sup['support_decision']['supports'] is True
    assert sup['slice_recipe'] == expected and expected['sha256'] != stored['sha256']
    assert sup['artifact_binding']['recipe_sha256'] == expected['sha256']
    assert [p['artifact_binding']['recipe_sha256'] for p in sup['derived_plates']] == [
        expected['sha256'], expected['sha256'], other]
    assert sup['provenance']['support_decided'] is True
    assert sup['derived_plates'][0]['provenance']['support_decided'] is True
    assert runner.supports_result == ['first.gcode'] and runner.variant_reused == 'first_slice'
    # the first slice's own record is not touched
    assert job['slice_recipe'] == stored and job['artifact_binding']['recipe_sha256'] == stored['sha256']


def test_taking_supports_stops_a_running_nosupports_slice_before_removing_its_folder(tmp_path, monkeypatch):
    """take_supports after a failed with-supports slice must not delete the without-supports folder
    while that slicer is still running (#1936 review)."""
    monkeypatch.setattr(preview_parallel, 'enabled', lambda *a, **k: False)

    async def run():
        folder, job_path = _setup(tmp_path)
        runner = BackgroundSlice()
        started, release = asyncio.Event(), asyncio.Event()
        seen = {}

        async def slow_nosupports():
            started.set()
            try:
                await release.wait()
            finally:
                seen['folder_at_exit'] = variant.is_dir()
        variant = folder / 'variant_nosupports'
        variant.mkdir()
        runner.nosup_variant = variant
        runner.nosup_task = asyncio.ensure_future(slow_nosupports())
        await started.wait()

        async def failed_supports():  # the real task records its failure, it does not raise
            runner.supports_error = RuntimeError('with-supports slice failed')
        runner.supports_task = asyncio.ensure_future(failed_supports())
        await runner.take_supports()
        assert runner.nosup_task is None or runner.nosup_task.done()
        assert seen['folder_at_exit'] is True  # the slicer ended before its folder was removed
        assert not variant.exists()
    asyncio.run(run())
