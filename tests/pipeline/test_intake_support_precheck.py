"""Tests for speculative intake support pre-check, with-supports options, and #911 geometry fixes."""
import asyncio
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy.core import model_job, prepared_intake, support_clearance, slice_warnings
from print_handy.core.background_slice import BackgroundSlice, slice_binding
from print_handy.core.slice_recipe import RECIPE_VERSION, binding_sha256
from .helpers import external
print_next = external("print_next")
from .test_color_intake import source, state

V12_FIXTURE = Path(__file__).parent.parent / "fixtures" / "modeled_supports_shelf.3mf"


class Adapter:
    def __init__(self, picks):
        self.picks = iter(picks)
        self.questions = []
        self.sent = []
        self.log = []

    async def send_clarify(self, **kw):
        self.questions.append(kw)
        if len(kw.get("choices") or []) == 1:
            self.log.append(kw["question"])
        answer = kw["choices"][next(self.picks)]
        import tools.clarify_gateway as cm
        cm.resolve_gateway_clarify(kw["clarify_id"], answer)
        return S(success=True)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        self.log.append(content)
        return S(success=True)


class _Kept:
    def __init__(self, raw):
        self.raw = raw

    def _live(self):
        return [p for p in self.raw if Path(p).exists()]

    def __len__(self):
        return len(self._live())

    def __getitem__(self, item):
        return self._live()[item]

    def __iter__(self):
        return iter(self._live())

    def __eq__(self, other):
        return self._live() == other

    def __bool__(self):
        return bool(self._live())

    def append(self, item):
        self.raw.append(item)

    def extend(self, items):
        self.raw.extend(items)


@pytest.fixture
def env(tmp_path, monkeypatch):
    src = source(tmp_path)
    _orig_with_stop = prepared_intake._with_stop
    from print_handy.core import background_slice

    async def _logged_with_stop(adapter, source_, key, metadata, text, work, **kw):
        if kw.get('allow_stop') and text and isinstance(getattr(adapter, 'log', None), list) and hasattr(adapter, 'picks'):
            adapter.log.append(text)
        return await _orig_with_stop(adapter, source_, key, metadata, text, work, **kw)
    monkeypatch.setattr(prepared_intake, '_with_stop', _logged_with_stop)

    folder_root = tmp_path / 'archive'
    monkeypatch.setattr(model_job, 'ARCHIVE', folder_root)
    monkeypatch.setattr(model_job, 'printables_inventory', lambda url: {
        'site': 'printables', 'slug': 'ghost', 'title': 'Ghost Stand',
        'sources': [{'name': 'ghost.3mf', 'instance_id': 1}]})
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy(src, dest))
    monkeypatch.setattr(model_job, 'audit', lambda path: {'plates': 1})

    from print_handy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 1)
    sliced = []
    kept = _Kept(sliced)
    recipes = []
    slice_calls = []

    def fake_slice(job_path, recipe_sha256=None, supports=None, **kwargs):
        slice_calls.append({'job_path': str(job_path), 'recipe_sha256': recipe_sha256, 'supports': supports})
        recipes.append(recipe_sha256)
        tag = 'supports' if supports else 'nosup'
        gcode = Path(job_path).parent / 'sliced' / f'plate_1_{tag}.gcode'
        gcode.parent.mkdir(parents=True, exist_ok=True)
        gcode.write_text(f'; estimated printing time (normal mode) = 1h 0m 0s\n; tag = {tag}\nG1 X1\n')
        data = json.loads(Path(job_path).read_text())
        data['derived_plates'] = [{'gcode': str(gcode), 'plate_id': 1, 'slot': 1}]
        data['gcode'] = str(gcode)
        model_job.write_job(Path(job_path).parent, data)
        try:
            return [gcode]
        finally:
            if gcode.exists():
                sliced.append(str(gcode))

    monkeypatch.setattr(model_job, 'slice_job', fake_slice)
    stub = tmp_path / 'print-now-stub'
    stub.write_text('#!/bin/sh\necho "started $2"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    starts = []
    real = print_next.start

    async def start(target):
        starts.append(target)
        return await real(target, model_job=stub, idle_check=lambda: None, timeout=1,
                          claims_dir=tmp_path / 'claims')
    monkeypatch.setattr(print_next, 'start', start)
    monkeypatch.setattr(prepared_intake.print_summary, 'find_job',
                        lambda filename, *args: (json.loads(Path(filename).parents[1].joinpath('JOB.json').read_text()),
                                          {'gcode': str(Path(filename)), 'plate': 1, 'parts': ['Ghost Stand'],
                                           'slot': 1, 'color': 'white', 'material': 'PLA',
                                           'filament_g': None, 'seconds': 3600}))
    return S(root=tmp_path / 'receipts', sliced=kept, starts=starts, tmp=tmp_path, recipes=recipes,
             slice_calls=slice_calls, src=src)


def _copy(src, dest):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / 'ghost.3mf'
    out.write_bytes(Path(src).read_bytes())
    return out


def run_flow(env, picks, mid='m1'):
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'), message_id=mid,
              text='https://www.printables.com/model/1-ghost')
    adapter = Adapter(picks)
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    receipt = json.loads(next(env.root.glob('*.json')).read_text())
    return adapter, result, receipt


def test_unflagged_model_no_second_slice(env):
    """An unflagged model gets no second slice and standard choice buttons."""
    # picks: one color (1), slot 1 (0), Slice and print now (0)
    adapter, result, receipt = run_flow(env, [1, 0, 0])
    last = adapter.questions[-1]['choices']
    assert last[0] == 'Slice and print now (bed is clear)'
    assert last[1] == 'Slice only'
    assert 'with supports' not in last[0]
    # Only one slice was run
    assert len(env.slice_calls) == 1
    assert env.slice_calls[0]['supports'] is None
    assert receipt['status'] == 'STARTED'


def test_flagged_orca_warning_offers_supports_before_bed_clear(env, monkeypatch):
    """Orca floating warning triggers background with-supports slice and options before bed-clear."""
    # Make fake_slice simulate Orca floating regions warning
    def slice_with_warning(job_path, recipe_sha256=None, supports=None, **kwargs):
        data = json.loads(Path(job_path).read_text())
        is_sup = bool(supports or (data.get('support_decision') or {}).get('supports'))
        env.slice_calls.append({'job_path': str(job_path), 'recipe_sha256': recipe_sha256, 'supports': is_sup})
        tag = 'supports' if is_sup else 'nosup'
        gcode = Path(job_path).parent / 'sliced' / f'plate_1_{tag}.gcode'
        gcode.parent.mkdir(parents=True, exist_ok=True)
        gcode.write_text(f'; estimated printing time (normal mode) = 1h 0m 0s\n; tag = {tag}\nG1 X1\n')
        data['derived_plates'] = [{'gcode': str(gcode), 'plate_id': 1, 'slot': 1}]
        data['gcode'] = str(gcode)
        if not is_sup:
            data['provenance'] = {
                'slice_warnings': {
                    'support_needed': True,
                    'objects': ['Body'],
                    'support_texts': ['object Body has floating regions'],
                },
                'supports_on': False,
                'support_decided': False,
            }
        else:
            data['provenance'] = {
                'slice_warnings': {'support_needed': False},
                'supports_on': True,
                'support_decided': True,
            }
        model_job.write_job(Path(job_path).parent, data)
        return [gcode]

    monkeypatch.setattr(model_job, 'slice_job', slice_with_warning)

    # picks: one color (1), slot 1 (0), pick "Slice and print with supports (bed is clear)" (0)
    adapter, result, receipt = run_flow(env, [1, 0, 0])
    last_q = adapter.questions[-1]
    choices = last_q['choices']
    assert choices[0] == 'Slice and print with supports (bed is clear)'
    assert choices[1] == 'Slice and print without supports (bed is clear)'
    assert 'floating regions found' in last_q['question']
    assert 'Slice and print with supports (recommended: floating regions found)' in last_q['question']

    # Both no-supports and with-supports slices were run in background
    assert len(env.slice_calls) >= 2
    # Chosen variant is the supported one
    assert receipt['status'] == 'STARTED'
    assert 'plate_1_supports.gcode' in receipt['bed_clear_approval']['gcode']
    # No second post-slice question fired (no double gate)
    assert not any('What should I do?' in q['question'] for q in adapter.questions)


def test_flagged_v12_fixture_offers_fixes_and_clearance_warnings(env, monkeypatch):
    """V12 fixture triggers clearance warning, fix buttons, and modeled support disclosure."""
    assert V12_FIXTURE.is_file()
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy(V12_FIXTURE, dest))
    from print_handy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 1)

    # picks: one color (1), slot 1 (0), pick choice 0 (Slice and print with supports)
    adapter, result, receipt = run_flow(env, [1, 0, 0])
    last_q = adapter.questions[-1]
    choices = last_q['choices']

    assert choices[0] == 'Slice and print with supports (bed is clear)'
    assert choices[1] == 'Slice and print without supports (bed is clear)'
    assert 'Extend underside to plate' in choices
    assert 'Flatten (sink) to plate' in choices

    # Disclosure in question text
    q_text = last_q['question']
    assert 'bridges a 0.4 mm gap to the build plate across 9493 mm2' in q_text
    assert 'modeled supports touch only 1.8%' in q_text

    # Approval and receipt
    assert receipt['status'] == 'STARTED'
    assert receipt['started'] is True
    # Clearance acknowledgement was recorded
    job = json.loads(Path(receipt['job_json']).read_text())
    assert job.get('clearance_acknowledgement', {}).get('acknowledged') is True


def test_extend_underside_fix_action_repairs_geometry(env, monkeypatch):
    """Choosing 'Extend underside to plate' modifies the mesh and clears the clearance risk."""
    assert V12_FIXTURE.is_file()
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy(V12_FIXTURE, dest))
    from print_handy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 1)

    # picks:
    # 1: one color
    # 0: slot 1
    # 2: 'Extend underside to plate'
    # Then loop repeats with repaired mesh!
    # On repeat, clearance risk is cleared, so pick 0 (Slice and print now)
    adapter, result, receipt = run_flow(env, [1, 0, 2, 0])
    assert any('Extended underside to touch build plate' in m for m in adapter.sent)
    assert receipt['status'] == 'STARTED'


def test_negative_start_guard_refuses_unacknowledged_risk(tmp_path):
    """require_current_artifact refuses a job with low clearance risk that was not acknowledged."""
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

    # Should raise RuntimeError because clearance is risky and no acknowledgement exists
    with pytest.raises(RuntimeError, match='insufficient support clearance'):
        model_job.require_current_artifact(job, str(gcode))

    # After recording acknowledgement, it passes
    support_clearance.record_clearance_acknowledgement(job, gcode=str(gcode), source_file=str(V12_FIXTURE))
    assert job['clearance_acknowledgement']['acknowledged'] is True
