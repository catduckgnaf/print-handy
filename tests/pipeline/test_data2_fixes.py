"""DATA-2: intake and color-decision crashes (Codex backlog burn-down).

Each test pins one finding: it fails on the pre-fix code for the exact
scenario the audit named, and passes once the fix lands.
"""
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy.core import model_job, orca_adapter, prepared_intake
from .test_color_intake import state  # noqa: F401
from .test_prepared_intake import Adapter, env, run_flow  # noqa: F401
from .test_source_colors import source as painted_source


def _receipt(env, mid):
    name = hashlib.sha256(json.dumps(['3dprinting', 'c1', '', mid]).encode()).hexdigest() + '.json'
    return json.loads((env.root / name).read_text())


def test_resume_reuses_saved_color_decisions(env):
    """P1-38: a resume whose JOB.json already holds color decisions must reuse
    them, not rebuild them from an unassigned ``mode`` (UnboundLocalError)."""
    run_flow(env, [1, 0, 0], mid='first')
    prev = prepared_intake.newest_archived_job()
    saved = prev['color_decisions']
    assert saved['mode'] == 'single_color'
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='resume1', text='https://www.printables.com/model/1-ghost')
    adapter = Adapter([0, 0])  # slice-and-print choice, then the start button
    result = asyncio.run(prepared_intake.prepare_link(
        event, adapter, state(), env.root, timeout=1,
        reuse=prepared_intake.resume_job(prev)))
    receipt = _receipt(env, 'resume1')
    assert receipt['status'] != 'FAILED', receipt
    assert result['action'] != 'skip' or 'UnboundLocalError' not in str(result)
    # The saved decisions were reused: no color question was asked again, and
    # the new JOB.json carries the same decisions the operator already made.
    assert not any('Choose the color' in q['question'] or 'Choose colors' in q['question']
                   for q in adapter.questions)
    job = json.loads(Path(receipt['job_json']).read_text())
    assert job['color_decisions']['mode'] == saved['mode']
    assert job['color_decisions']['source_sha256'] == saved['source_sha256']
    assert job['color_decisions']['selection'] == saved['selection']
    assert len(env.starts) == 2  # the resumed intake reached the start


def test_painted_oversize_fit_accept_does_not_crash(env, monkeypatch):
    """P1-27: accepting a painted oversize fit must not read ``options`` —
    at that point it is still the color-mode label list (or unbound), never
    the fit-options dict, so the accept branch crashed with AttributeError."""
    painted = painted_source(env.tmp, painted=True)

    def copy_painted(job, chosen, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / 'ghost.3mf'
        out.write_bytes(painted.read_bytes())
        return out

    monkeypatch.setattr(model_job, 'printables_download', copy_painted)
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 0)
    monkeypatch.setattr(model_job, 'painted_fit_for_job',
                        lambda path, decisions, state=None: {'scale': 0.8, 'plates': 1, 'blocking_plate': None})
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='painted1', text='https://www.printables.com/model/1-ghost')
    adapter = Adapter([0] * 20)  # per-part colors, slot picks, accept the fit, slice and print
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    receipt = _receipt(env, 'painted1')
    assert receipt['status'] != 'FAILED', receipt
    assert receipt['painted_fit']['scale'] == 0.8
    job = json.loads(Path(receipt['job_json']).read_text())
    assert job['scale'] == 0.8 and job['plate_mode'] == 'separate'
    assert not any('has no attribute' in post for post in adapter.sent)
    assert result['action'] != 'skip' or result.get('reason') != 'intake failed'


def test_single_color_rearranged_slice_binds_decisions_to_derivative(tmp_path, monkeypatch):
    """P1-50: with a rearrangement plan, slicing runs on the arranged
    derivative, whose SHA differs from the original source the color
    decisions bind — the slicer's validate_decisions refused every such job.
    The decisions must be validated against the original, then rebound to
    the derivative's bytes for the slice; JOB.json keeps the original."""
    from print_handy.core import source_colors
    from .test_plate_merge import STATE, _source

    source = tmp_path / 'source.3mf'
    _source(source, {'1': (100, 20, 10), '2': (100, 20, 10)}, [['1', '2']])
    manifest = source_colors.inventory(source)
    st = json.loads(STATE.read_text())
    selection = model_job.resolve_filament_selection(st, dict(st['filament_slots']['1'], slot=1))
    decisions = {'schema': 'part_slots_v1', 'mode': 'single_color',
                 'source_sha256': manifest['source_sha256'], 'selection': selection}
    job_path = tmp_path / 'JOB.json'
    job_path.write_text(json.dumps({
        'downloaded_file': str(source),
        'audit': {'plates': 1, 'size_x': 130, 'size_y': 50, 'size_z': 10},
        'color_decisions': decisions, 'filament_selection': selection,
        'full_size_rearrange_plan': {'rearrange_plate': True}}))
    seen = {}

    def fake_slice(src_arg, folder, state_arg, brim=None, **kw):
        # The real adapter's first step with decisions: validate them against
        # the exact file being sliced (orca_adapter.slice_model).
        if kw.get('color_decisions'):
            source_colors.validate_decisions(source_colors.inventory(src_arg),
                                             kw['color_decisions'], state_arg,
                                             model_job.resolve_filament_selection)
        seen['src'] = Path(src_arg)
        seen['decisions'] = kw.get('color_decisions')
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / 'plate_1.gcode'
        slot = state_arg['filament_slots']['1']
        out.write_text(f'; filament_colour = {slot["hex"]}\n; nozzle_diameter = 0.4\n'
                       f'M190 S{state_arg["temps_c"]["bed"]}\nM104 S{state_arg["temps_c"]["nozzle"]} T0\n'
                       f'START_PRINT BED_TEMP={state_arg["temps_c"]["bed"]} '
                       f'EXTRUDER_TEMP={state_arg["temps_c"]["nozzle"]}\n'
                       'G1 X10 Y10 Z0.2\nEND_PRINT\n')
        return out, {'offline_test': True}, []

    def fake_arrange(src_arg, outdir, **kw):
        # A derivative with different bytes (hence a different SHA) but the
        # same inventory: the binding question, not the arranging, is tested.
        import zipfile
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        out = outdir / 'arranged.3mf'
        with zipfile.ZipFile(src_arg) as zin, zipfile.ZipFile(out, 'w') as zout:
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename))
            zout.writestr('Metadata/arranged.txt', 'rearranged at 100%')
        return {'exported_3mf_geometry_only': 'arranged.3mf'}

    monkeypatch.setattr('print_handy.core.geometry_layout.arrange_and_export', fake_arrange)
    monkeypatch.setattr('print_handy.core.orca_adapter.slice_model', fake_slice)
    monkeypatch.setattr(model_job, 'finalize_multicolor', lambda *a, **k: None)
    monkeypatch.setattr(model_job, 'verify_gcode', lambda _path: None)
    model_job.slice_job(job_path)
    assert seen['src'] != source  # the arranged derivative was sliced
    assert seen['decisions']['source_sha256'] == model_job.file_sha256(seen['src'])
    saved = json.loads(job_path.read_text())
    assert saved['color_decisions']['source_sha256'] == manifest['source_sha256']
    assert saved['gcode_verified'] is True


REFUSED = [{'slot': 2, 'plate_id': 2, 'binding_key': '2:2', 'part_keys': ['2:1'],
            'reason': 'derived plate does not fit the bed'}]


def test_partial_job_refusal_names_refused_plates():
    """P1-20 (unit): the launch gate names every refused plate, and an
    explicitly accepted partial job is not gated."""
    assert model_job.partial_job_refusal({}) is None
    assert model_job.partial_job_refusal({'refused_plates': []}) is None
    accepted = model_job.refused_plates_digest({'refused_plates': REFUSED})
    assert model_job.partial_job_refusal(
        {'refused_plates': REFUSED, 'partial_job_accepted': accepted}) is None
    # An acceptance of another plate set (or the old bare True) does not cover these plates.
    other = [{**REFUSED[0], 'plate_id': 3, 'binding_key': '3:3'}]
    assert model_job.partial_job_refusal(
        {'refused_plates': other, 'partial_job_accepted': accepted}) is not None
    assert model_job.partial_job_refusal(
        {'refused_plates': REFUSED, 'partial_job_accepted': True}) is not None
    message = model_job.partial_job_refusal({'refused_plates': REFUSED})
    assert 'plate 2' in message and 'does not fit the bed' in message
    assert 'incomplete' in message


def test_print_now_refuses_an_unaccepted_partial_job(tmp_path, monkeypatch):
    """P1-20: print-now must not start a job whose slicing refused a plate
    until the operator explicitly accepted the partial job."""
    from .test_artifact_binding import setup_job
    jp, _st, _gcode = setup_job(tmp_path, monkeypatch)
    job = json.loads(jp.read_text())
    job['refused_plates'] = REFUSED
    jp.write_text(json.dumps(job))
    with pytest.raises(SystemExit, match='incomplete'):
        model_job.print_now(str(jp))
    job['partial_job_accepted'] = model_job.refused_plates_digest(job)
    jp.write_text(json.dumps(job))
    with pytest.raises(Exception) as excinfo:
        model_job.print_now(str(jp))  # proceeds past the partial gate; no live printer here
    assert 'incomplete' not in str(excinfo.value)
    assert 'were refused during slicing' not in str(excinfo.value)


def test_intake_discloses_refused_plates_and_records_acceptance(env, monkeypatch):
    """P1-20: the intake names refused plates before the start question, and
    the operator's confirmed start records the partial-job acceptance."""
    def fake_slice(job_path, recipe_sha256=None):
        gcode = Path(job_path).parent / 'sliced' / 'plate_1.gcode'
        gcode.parent.mkdir(parents=True, exist_ok=True)
        gcode.write_text('; estimated printing time (normal mode) = 1h 0m 0s\nG1 X1\n')
        data = json.loads(Path(job_path).read_text())
        data['derived_plates'] = [{'gcode': str(gcode), 'plate_id': 1, 'slot': 1}]
        data['refused_plates'] = REFUSED
        model_job.write_job(Path(job_path).parent, data)
        return [gcode]

    monkeypatch.setattr(model_job, 'slice_job', fake_slice)
    adapter, _result, receipt = run_flow(env, [1, 0, 0], mid='partial1')
    assert receipt['status'] != 'FAILED', receipt
    posts = [post for post in adapter.sent if 'will NOT print' in post]
    assert posts and 'Plate 2' in posts[0] and 'does not fit the bed' in posts[0]
    job = json.loads(Path(receipt['job_json']).read_text())
    assert job['partial_job_accepted'] == model_job.refused_plates_digest(job)
    assert len(env.starts) == 1  # the disclosed, accepted partial job started


def test_dropbox_link_without_filename_survives_the_extension_filter(env, monkeypatch):
    """P2-241: a Dropbox share URL with no filename in its path inventories
    as the suffix-less name "shared-file"; the file-list extension filter
    must not remove it before the download, whose response carries the real
    filename (validated by direct_download). Serving HTML proves the flow
    reached the download: it refuses as a web page, not as empty inventory."""
    from .test_direct_links import FakeResponse
    monkeypatch.setattr(model_job, '_resolve_host_ips', lambda host: {'93.184.216.34'})
    monkeypatch.setattr(model_job, '_open_once', lambda url: FakeResponse(
        b'<html><body>share page</body></html>', {'Content-Type': 'text/html'}))

    async def fake_preview(*args, **kw):
        pass

    monkeypatch.setattr(prepared_intake, 'send_preview', fake_preview)
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='dropbox1', text='https://www.dropbox.com/s/abc123?dl=0')
    adapter = Adapter([])
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    receipt = _receipt(env, 'dropbox1')
    assert receipt['status'] == 'FAILED', receipt
    assert any('web page instead of a print file' in post for post in adapter.sent)
    assert not any('no selectable geometry' in post for post in adapter.sent)


def test_painted_fit_crash_offers_retry_not_a_size_verdict(env, monkeypatch):
    """#839: a painted fit check that throws is not "does not fit" — the post
    says the check failed and offers Retry/Cancel; a retry that succeeds
    continues the intake normally."""
    painted = painted_source(env.tmp, painted=True)

    def copy_painted(job, chosen, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / 'ghost.3mf'
        out.write_bytes(painted.read_bytes())
        return out

    monkeypatch.setattr(model_job, 'printables_download', copy_painted)
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 0)
    calls = []

    def flaky_fit(path, decisions, state=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('mesh parse exploded')
        return {'scale': 0.8, 'plates': 1, 'blocking_plate': None}
    monkeypatch.setattr(model_job, 'painted_fit_for_job', flaky_fit)
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='painted-err', text='https://www.printables.com/model/1-ghost')
    adapter = Adapter([0] * 30)  # every first choice; Retry is the first fit-error choice
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    receipt = _receipt(env, 'painted-err')
    asked = [q for q in adapter.questions if q['choices'] == ['Retry fit check', 'Cancel']]
    assert len(asked) == 1 and 'could not check whether this fits' in asked[0]['question']
    assert 'PH_INTAKE' in asked[0]['question']
    assert not any('does not fit' in q['question'] for q in adapter.questions)
    assert not any('does not fit' in post for post in adapter.sent)
    assert len(calls) == 2
    assert receipt['painted_fit']['scale'] == 0.8 and receipt['status'] != 'FAILED', receipt


def test_painted_fit_error_cancel_cancels_without_a_size_verdict(env, monkeypatch):
    """#839: cancelling the fit-error question cancels plainly; no fit verdict is recorded."""
    painted = painted_source(env.tmp, painted=True)

    def copy_painted(job, chosen, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / 'ghost.3mf'
        out.write_bytes(painted.read_bytes())
        return out

    monkeypatch.setattr(model_job, 'printables_download', copy_painted)
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 0)

    def broken_fit(path, decisions, state=None):
        raise RuntimeError('worker died')
    monkeypatch.setattr(model_job, 'painted_fit_for_job', broken_fit)

    class CancelRetry(Adapter):
        async def send_clarify(self, **kw):
            if kw.get('choices') == ['Retry fit check', 'Cancel']:
                self.questions.append(kw)
                import tools.clarify_gateway as cm
                cm.resolve_gateway_clarify(kw['clarify_id'], 'Cancel')
                return S(success=True)
            return await super().send_clarify(**kw)

    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='painted-err-cancel', text='https://www.printables.com/model/1-ghost')
    adapter = CancelRetry([0] * 30)
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    receipt = _receipt(env, 'painted-err-cancel')
    assert result['reason'] == 'painted fit check failed; operator cancelled'
    assert receipt['status'] == 'CANCELLED'
    assert receipt['painted_fit'].get('error') == 'worker died'
    assert not any('does not fit' in post for post in adapter.sent)


def test_painted_no_fit_names_the_measured_layout(env, monkeypatch):
    """#839: a real no-fit keeps its text and adds the measured painted
    extent at the 50% floor, the tower footprint and the usable area."""
    painted = painted_source(env.tmp, painted=True)

    def copy_painted(job, chosen, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / 'ghost.3mf'
        out.write_bytes(painted.read_bytes())
        return out

    monkeypatch.setattr(model_job, 'printables_download', copy_painted)
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 0)
    monkeypatch.setattr(model_job, 'painted_fit_for_job',
                        lambda path, decisions, state=None: {
                            'scale': None, 'blocking_plate': 1, 'plates': 1,
                            'blocking_layout': {'extent_mm': [231.0, 198.0],
                                                'tower_mm': 35.0, 'scale': 0.5}})
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='painted-nofit', text='https://www.printables.com/model/1-ghost')
    adapter = Adapter([0] * 30)  # the only fit choice offered is Cancel
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    receipt = _receipt(env, 'painted-nofit')
    asked = [q['question'] for q in adapter.questions if 'does not fit' in q['question']]
    assert len(asked) == 1
    assert '231 x 198 mm' in asked[0] and '35 x 35 mm prime tower' in asked[0]
    assert 'usable area is 220 x 220 mm' in asked[0]
    assert receipt['status'] == 'CANCELLED_OVERSIZED'
