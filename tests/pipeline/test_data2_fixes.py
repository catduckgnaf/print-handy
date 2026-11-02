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
    prev = prepared_intake.last_job()
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
                        lambda path, decisions: {'scale': 0.8, 'plates': 1, 'blocking_plate': None})
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
