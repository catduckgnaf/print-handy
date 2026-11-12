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
    assert 'the slicer found 1 floating region that needs support' in last_q['question']
    assert 'Slice and print with supports (recommended: the slicer found 1 floating region that needs support)' in last_q['question']

    # Both no-supports and with-supports slices were run in background
    assert len(env.slice_calls) >= 2
    # Chosen variant is the supported one
    assert receipt['status'] == 'STARTED'
    assert 'plate_1_supports.gcode' in receipt['bed_clear_approval']['gcode']
    # No second post-slice question fired (no double gate)
    assert not any('What should I do?' in q['question'] for q in adapter.questions)


def _warning_slice(env, first_delay=0.0, supports_delay=0.0):
    """Fake slice_job: the no-supports slice reports Orca floating regions.

    The job dict is read when the slice starts and written back when it ends,
    exactly like the real slice_job; the delays model a slow real slicer.
    """
    import time

    def slice_with_warning(job_path, recipe_sha256=None, supports=None, **kwargs):
        data = json.loads(Path(job_path).read_text())
        is_sup = bool(supports or (data.get('support_decision') or {}).get('supports'))
        env.slice_calls.append({'job_path': str(job_path), 'recipe_sha256': recipe_sha256,
                                'supports': is_sup})
        time.sleep(supports_delay if is_sup else first_delay)
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

    return slice_with_warning


def test_slow_first_slice_warning_still_offers_supports_before_bed_clear(env, monkeypatch):
    """A first slice slower than a brief wait still flags the Ready question.

    Regression: the Ready question was composed after a 1.5 s wait for the
    first slice, so a real (slow) Orca slice's floating-regions warning
    missed the question entirely and the case's pinned prompt never fired
    (CI: test_discord_flow[witch-ghost-cults3d]).
    """
    monkeypatch.setattr(model_job, 'slice_job', _warning_slice(env, first_delay=2.0))
    # picks: one color (1), slot 1 (0), "Slice and print with supports (bed is clear)" (0)
    adapter, result, receipt = run_flow(env, [1, 0, 0])
    last_q = adapter.questions[-1]
    assert last_q['choices'][0] == 'Slice and print with supports (bed is clear)'
    assert 'the slicer found 1 floating region that needs support' in last_q['question']
    assert receipt['status'] == 'STARTED'


def test_slow_speculative_slice_does_not_break_slice_approval(env, monkeypatch):
    """Choosing without supports while the speculative slice is still running
    must not trip the source/job approval guard.

    Regression: the speculative with-supports slice writes JOB.json (support
    decision on) while it runs and its slice_job writes that job back at the
    end, so an operator answer recorded in that window was clobbered and the
    approval guard refused the handoff (CI: test_discord_flow[stl-open-thin-wall]).
    """
    monkeypatch.setattr(model_job, 'slice_job', _warning_slice(env, supports_delay=1.5))
    # picks: one color (1), slot 1 (0), "Slice and print without supports (bed is clear)" (1)
    adapter, result, receipt = run_flow(env, [1, 0, 1])
    assert receipt['status'] == 'STARTED'
    assert 'plate_1_nosup.gcode' in receipt['bed_clear_approval']['gcode']
    job = json.loads(Path(receipt['job_json']).read_text())
    assert job['support_decision']['supports'] is False


def test_flagged_v12_fixture_offers_fixes_and_clearance_warnings(env, monkeypatch):
    """V12 fixture triggers clearance warning, fix buttons, and modeled support disclosure."""
    assert V12_FIXTURE.is_file()
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy(V12_FIXTURE, dest))
    from print_handy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 1)

    # picks: one color (1), slot 1 (0), pick choice 1 (Slice and print with
    # supports; the only evidence is a low clearance supports cannot reach,
    # so supports are "not recommended" and come second), then Cancel (1)
    # at the post-slice support override question
    adapter, result, receipt = run_flow(env, [1, 0, 1, 1])
    # The Ready question is not the last question in the #922 flow (the
    # blocked start sends no further question, so it still is here), but
    # find it by its choices rather than by position.
    last_q = next(q for q in adapter.questions
                  if q['choices'] and 'Slice and print with supports (bed is clear)' in q['choices'])
    choices = last_q['choices']

    assert choices[0] == 'Slice and print without supports (bed is clear)'
    assert choices[1] == 'Slice and print with supports (bed is clear)'
    assert 'Extend underside to plate' in choices
    assert 'Flatten (sink) to plate' in choices
    assert 'Reorient (lay flat)' in choices
    assert 'Print anyway (accept the risk)' in choices

    # Disclosure in question text, including the measured volume deltas (#922 gap 5)
    q_text = last_q['question']
    # Supports are "not recommended" here (the only evidence is a gap they
    # cannot reach), so the without-supports line leads the question too.
    assert '(not recommended:' in q_text
    assert q_text.index('- Slice and print without supports:') < q_text.index('- Slice and print with supports')
    assert 'bridges a 0.4 mm gap to the build plate across 9493 mm2' in q_text
    assert 'modeled supports touch only 1.8%' in q_text
    assert 'Extend underside to plate adds about' in q_text

    # #922 gap 2: choosing "with supports" does NOT waive the 0.40 mm
    # low-clearance finding. The fake with-supports G-code carries no
    # ;TYPE:Support toolpaths, so coverage verification fails and the
    # print does not start; no clearance acknowledgement is recorded.
    assert receipt['status'] == 'SLICED_NOT_STARTED'
    assert receipt.get('started') is not True
    job = json.loads(Path(receipt['job_json']).read_text())
    assert not job.get('clearance_acknowledgement', {}).get('acknowledged')
    # The refusal is a question with a real override, never a dead end.
    override_q = adapter.questions[-1]
    assert override_q['choices'] == [prepared_intake.PRINT_ANYWAY_LABEL, print_next.CANCEL_LABEL]
    assert 'do not cover every flagged region' in override_q['question']
    assert 'under 1.0 mm' in override_q['question']
    assert 'support_override' not in receipt
    assert any('Cancelled. Nothing uploaded or started.' in m for m in adapter.sent)


def test_flagged_v12_with_supports_print_anyway_waives_both_gates_and_starts(env, monkeypatch):
    """With supports + failed gates + Print anyway at the override question starts (#922 dead end)."""
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy(V12_FIXTURE, dest))
    from print_handy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 1)

    # one color (1), slot 1 (0), with supports (1: not recommended, so
    # second), Print anyway (0)
    adapter, result, receipt = run_flow(env, [1, 0, 1, 0])
    assert receipt['status'] == 'STARTED'
    override = receipt['support_override']
    assert override['via'] == 'print_anyway'
    assert override['support_coverage'] is True and override['low_clearance'] is True
    assert override['gcode_sha256'] == receipt['gcode_sha256']
    # The waiver is the clearance acknowledgement the start guard checks,
    # bound to the artifact that actually starts.
    job = json.loads(Path(receipt['job_json']).read_text())
    ack = job['clearance_acknowledgement']
    assert ack['acknowledged'] is True and ack['via'] == 'print_anyway'
    assert ack['gcode_sha256'] == model_job.file_sha256(job['gcode'])


def _coverage_only_report(path):
    """A report whose only post-slice target is a non-risky supportable overhang."""
    return {'version': 1, 'risky': False, 'findings': [], 'objects': [], 'limitations': [],
            'regions': [{'kind': 'supportable_overhang', 'part': 'Body', 'area_mm2': 600.0,
                         'z_mm': {'min': 5.0, 'max': 5.0}, 'bbox_mm': [[0.0, 0.0], [30.0, 20.0]]}]}


@pytest.mark.parametrize('pick, status', [(0, 'STARTED'), (1, 'SLICED_NOT_STARTED')])
def test_coverage_failure_without_clearance_finding_asks_instead_of_dead_ending(env, monkeypatch, pick, status):
    """The reported dead end: a with-supports slice that fails only coverage
    verification (no low-clearance finding, so no pre-check Print anyway
    button) used to say "choose Print anyway" and return with no buttons."""
    monkeypatch.setattr(support_clearance, 'safe_analyze', _coverage_only_report)
    monkeypatch.setattr(model_job, 'slice_job', _warning_slice(env))
    # one color (1), slot 1 (0), with supports (0), then the override question
    adapter, result, receipt = run_flow(env, [1, 0, 0, pick])
    q = adapter.questions[-1]
    assert q['choices'] == [prepared_intake.PRINT_ANYWAY_LABEL, print_next.CANCEL_LABEL]
    assert 'do not cover every flagged region' in q['question']
    assert 'under 1.0 mm' not in q['question']
    assert receipt['support_coverage']['ok'] is False
    assert receipt['status'] == status
    if pick == 0:
        assert receipt['support_override']['support_coverage'] is True
        assert receipt['support_override']['low_clearance'] is False
        assert 'plate_1_supports.gcode' in receipt['bed_clear_approval']['gcode']
    else:
        assert env.starts == []
        assert all(Path(g).is_file() for g in receipt['gcodes'])


def test_flagged_v12_print_anyway_records_explicit_ack_and_starts(env, monkeypatch):
    """The explicit Print anyway choice is the one waiver for a low-clearance finding (#922 gap 2)."""
    assert V12_FIXTURE.is_file()
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy(V12_FIXTURE, dest))
    from print_handy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 1)

    # Menu: sup(0), nosup(1), extend(2), sink(3), reorient(4), print anyway(5)
    adapter, result, receipt = run_flow(env, [1, 0, 5])
    assert receipt['status'] == 'STARTED'
    job = json.loads(Path(receipt['job_json']).read_text())
    ack = job.get('clearance_acknowledgement') or {}
    assert ack.get('acknowledged') is True
    assert ack.get('via') == 'print_anyway'


def test_flagged_v12_reorient_records_rotation_and_clears_decision(env, monkeypatch):
    """The Reorient button wires the reorient fix and clears the support decision (#922 gap 7)."""
    assert V12_FIXTURE.is_file()
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy(V12_FIXTURE, dest))
    from print_handy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 1)

    # Menu: sup(0), nosup(1), extend(2), sink(3), reorient(4), print anyway(5),
    # The fixture already lies flat on its largest face (its lay-flat turn
    # is identity, which is now answered without recording anything), so
    # the lay-flat search is pinned to a real turn: on its side.
    from print_handy.core import edit_plan as _ep
    on_side = _ep.rotation_matrix('x', 90)
    monkeypatch.setattr(_ep, 'lay_flat_rotation', lambda tris: (on_side, _ep.mesh_envelope(on_side, tris)))
    # slice only(6), scale(7), cancel(8). Pick reorient, then cancel the
    # repeated question, which no longer offers Reorient: pressing it again
    # would record the same turn and change nothing (cancel is now 7).
    adapter, result, receipt = run_flow(env, [1, 0, 4, 7])
    assert any('Reoriented' in m for m in adapter.sent)
    ready = [q['choices'] for q in adapter.questions
             if q['choices'] and q['choices'][0] == 'Slice and print with supports (bed is clear)']
    assert 'Reorient (lay flat)' in ready[0]
    assert all('Reorient (lay flat)' not in choices for choices in ready[1:]) and len(ready) > 1
    job = json.loads(Path(receipt['job_json']).read_text())
    assert (job.get('orientation') or {}).get('rotation') is not None
    # The rotated envelope is recorded with the rotation, so the bed
    # pre-check and size edits measure the pose that prints.
    from print_handy.core import edit_plan, support_clearance
    tris = [tuple(map(tuple, tri)) for p in support_clearance.leaves(str(V12_FIXTURE)) for tri in p['tris']]
    expected = edit_plan.mesh_envelope(job['orientation']['rotation'], tris)
    assert job['orientation']['dims_full_mm'] == pytest.approx(list(expected), abs=1e-3)
    assert edit_plan.job_dims(job) == pytest.approx(expected, abs=1e-3)
    assert 'supports' not in (job.get('orientation') or {})
    assert 'support_decision' not in job
    assert 'clearance_acknowledgement' not in job


def test_reorient_on_a_model_that_already_lies_flat_records_nothing(env, monkeypatch):
    """Reorient on an already flat model says so and stores no orientation (2.3.1).

    Regression: the identity lay-flat turn was recorded as an orientation,
    the reply claimed a reorientation that changed nothing, the button stayed
    on offer, and the recorded rotation made a multicolor plaque slice as
    one single-color plate per slot.
    """
    from print_handy.core import edit_plan
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy(V12_FIXTURE, dest))
    from print_handy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 1)
    # The fixture already lies on its largest face: its real lay-flat turn is identity.
    tris = [tuple(map(tuple, tri)) for p in support_clearance.leaves(str(V12_FIXTURE)) for tri in p['tris']]
    assert edit_plan.lay_flat_rotation(tris)[0] == pytest.approx([1, 0, 0, 0, 1, 0, 0, 0, 1])
    # Menu: sup(0), nosup(1), extend(2), sink(3), reorient(4), ... Pick
    # reorient, then cancel the repeated question, which has no Reorient
    # (sup, nosup, extend, sink, print anyway, slice only, scale, cancel=7).
    adapter, result, receipt = run_flow(env, [1, 0, 4, 7])
    assert any('already lies flat' in m for m in adapter.sent)
    assert not any('Reoriented' in m for m in adapter.sent)
    ready = [q['choices'] for q in adapter.questions
             if q['choices'] and q['choices'][0] == 'Slice and print with supports (bed is clear)']
    assert len(ready) == 2
    assert 'Reorient (lay flat)' in ready[0] and 'Reorient (lay flat)' not in ready[1]
    job = json.loads(Path(receipt['job_json']).read_text())
    assert not (job.get('orientation') or {}).get('rotation')


def test_post_slice_support_prompt_cancel_keeps_the_slice(env, monkeypatch):
    """Cancelling at the post-slice support prompt keeps the archived slice (#922 gap 6)."""
    monkeypatch.setattr(model_job, 'slice_job', _warning_slice(env))
    # Ready menu (flagged, no clearance fixes): sup(0), nosup(1), slice only(2);
    # then the post-slice support prompt: re-slice(0), print without(1), cancel(2).
    adapter, result, receipt = run_flow(env, [1, 0, 2, 2])
    assert receipt['status'] == 'SLICED_NOT_STARTED'
    assert receipt['gcodes'], 'the receipt must still reference the slice'
    for g in receipt['gcodes']:
        assert Path(g).is_file(), f'cancel deleted the archived slice: {g}'


def test_post_slice_print_without_supports_records_cantilever_override(env, monkeypatch):
    """Print without supports after an Orca warning records the explicit override (#922 gap 3)."""
    monkeypatch.setattr(model_job, 'slice_job', _warning_slice(env))
    # Ready: slice only(2); support prompt: print without(1); start button: start(0).
    adapter, result, receipt = run_flow(env, [1, 0, 2, 1, 0])
    assert receipt['status'] == 'STARTED'
    job = json.loads(Path(receipt['job_json']).read_text())
    rec = job.get('unsupported_cantilever_override') or {}
    assert rec.get('code') == 'UNSUPPORTED_CANTILEVER_OVERRIDE'
    assert rec.get('warning') == ['object Body has floating regions']
    assert rec.get('source_sha256') == model_job.file_sha256(job['downloaded_file'])


def test_gcode_3mf_upload_is_named_as_already_sliced_not_corrupt(env, monkeypatch, tmp_path):
    """A .gcode.3mf package is refused as already-sliced, not bad_3mf_structure (#922 gap 9)."""
    import zipfile as _zip
    pkg = tmp_path / 'model.gcode.3mf'
    with _zip.ZipFile(pkg, 'w') as z:
        z.writestr('3D/3dmodel.model',
                   '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
                   '<resources></resources><build></build></model>')
        z.writestr('Metadata/plate_1.gcode', 'G1 X1 Y1\n')

    def _download(job, chosen, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / pkg.name
        out.write_bytes(pkg.read_bytes())
        return out
    monkeypatch.setattr(model_job, 'printables_download', _download)
    adapter, result, receipt = run_flow(env, [])
    assert result['reason'] == 'source-first intake failed closed'
    assert any('already-sliced G-code package' in m for m in adapter.sent)
    assert not any('internal structure is inconsistent' in m for m in adapter.sent)
    assert receipt['failure']['signature'] == 'gcode_3mf_package'


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
    # Then loop repeats with repaired mesh: the low-clearance finding is
    # gone, but the modeled-support findings remain, so the menu is the
    # flagged one again and the operator waives those explicitly with
    # 'Print anyway' (index 2 there: sup, nosup, print anyway, ...).
    adapter, result, receipt = run_flow(env, [1, 0, 2, 2])
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


def test_extend_fix_rebinds_color_decisions_to_repaired_source(env, monkeypatch):
    """After a geometry repair the saved color decisions must bind the new
    source SHA256, or the restarted slice refuses them (Codex P2)."""
    assert V12_FIXTURE.is_file()
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy(V12_FIXTURE, dest))
    from print_handy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 1)

    # After the repair the clearance finding is gone but modeled-support
    # findings remain, so the repeated menu is answered with the explicit
    # Print anyway waiver (index 2 there) -- "with supports" no longer
    # waives findings and its slice owes coverage verification (#922).
    adapter, result, receipt = run_flow(env, [1, 0, 2, 2])
    assert receipt['status'] == 'STARTED'
    job = json.loads(Path(receipt['job_json']).read_text())
    assert job['color_decisions']['source_sha256'] == model_job.file_sha256(job['downloaded_file'])


def test_clearance_acknowledgement_binds_the_selected_artifact(env, monkeypatch):
    """The clearance acknowledgement must carry the selected variant's G-code
    hash, not whichever artifact was on disk when the operator answered
    (Codex P2). Since #922 the acknowledgement comes from the explicit
    Print anyway waiver -- "with supports" records none -- so the binding
    is exercised through that choice."""
    assert V12_FIXTURE.is_file()
    monkeypatch.setattr(model_job, 'printables_download', lambda job, chosen, dest: _copy(V12_FIXTURE, dest))
    from print_handy.core import orca_adapter
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 1)

    # picks: one color (1), slot 1 (0), "Print anyway (accept the risk)" (5)
    adapter, result, receipt = run_flow(env, [1, 0, 5])
    assert receipt['status'] == 'STARTED'
    job = json.loads(Path(receipt['job_json']).read_text())
    ack = job['clearance_acknowledgement']
    assert ack['acknowledged'] is True
    assert ack['via'] == 'print_anyway'
    assert ack['gcode_sha256'] == model_job.file_sha256(job['gcode'])
    assert ack['source_sha256'] == model_job.file_sha256(job['downloaded_file'])


def test_flagged_slice_labels_when_printer_cannot_print(env, monkeypatch):
    """With no print delivery method, the flagged 'Slice with supports' option
    takes the slice-only path: no bed-clear approval, no printer start
    (Codex P2)."""
    monkeypatch.setattr(prepared_intake.delivery, 'methods', lambda: [])
    monkeypatch.setattr(model_job, 'slice_job', _warning_slice(env))

    # picks: one color (1), slot 1 (0), "Slice with supports" (0)
    adapter, result, receipt = run_flow(env, [1, 0, 0])
    assert adapter.questions[-1]['choices'][0] == 'Slice with supports'
    assert receipt['status'] == 'SLICED_NOT_STARTED'
    assert 'bed_clear_approval' not in receipt
    assert env.starts == []
