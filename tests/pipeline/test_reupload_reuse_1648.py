"""#1648 item 7: re-sending a file already sliced offers last time's decisions and verified slice.

Every case drives the real prepare_link intake twice with the same model link. The first
intake slices through the production ``slice_job`` entry, whose inner slicer is a stand-in
that records a real recipe, artifact binding and verified G-code, exactly the fields the
real slice paths write. The second intake is the re-upload. Starts go through the real
print_next.start gates with the stub print-now from the shared ``env`` fixture.
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy.core import model_job, orca_adapter, prepared_intake, printer_state
from print_handy.core import slice_recipe as sr
from print_handy.jobs import slice as slicing
from .test_color_intake import state
from .test_prepared_intake import Adapter, env, print_next  # noqa: F401  (pytest fixture)

# Taken at import, before the env fixture swaps model_job.slice_job (an alias of this name).
PRODUCTION_SLICE_JOB = slicing.slice_job
REUSE = 'Print it like last time'
START_OVER = 'Start over'
# One color for everything (#1473 partial offer), slot 1 (white), then "Slice and print now".
FRESH_PICKS = [2, 0, 0]


@pytest.fixture
def slicer(env, monkeypatch):  # noqa: F811
    """The production slice_job with a recording stand-in for the Orca run behind it."""
    runs = []

    def orca_stand_in(job_path, brim_override=None, copies=None, scale=None, recipe_sha256=None):
        job = json.loads(Path(job_path).read_text())
        st = printer_state.load(job['printer_id']) if job.get('printer_id') else printer_state.load()
        source_sha = model_job.file_sha256(job['downloaded_file'])
        recipe = sr.record(sr.slice_recipe(job, source_sha, st, copies=copies, scale=scale,
                                           brim_override=brim_override))
        assert recipe_sha256 in (None, recipe['sha256'])
        # A per-part job slices as two plates (the painted and split paths' shape:
        # derived_plates and one hash per plate); a one-color job as one G-code.
        plates = 2 if (job.get('color_decisions') or {}).get('mode') == 'per_part' else 1
        gcodes = []
        for number in range(1, plates + 1):
            gcode = Path(job_path).parent / 'sliced' / f'plate_{number}.gcode'
            gcode.parent.mkdir(parents=True, exist_ok=True)
            gcode.write_text('; estimated printing time (normal mode) = 1h 0m 0s\n'
                             f'; run {len(runs)} plate {number}\nG1 X1\n')
            gcodes.append(gcode)
        job['slice_recipe'] = recipe
        job['artifact_binding'] = {'recipe_version': recipe['version'], 'recipe_sha256': recipe['sha256'],
                                   'source_sha256': source_sha,
                                   'decisions_sha256': model_job.decisions_digest(job.get('color_decisions')),
                                   'gcode_sha256': model_job.file_sha256(gcodes[0])}
        if plates > 1:
            job['derived_plates'] = [{'gcode': str(g), 'plate_id': n, 'slot': n, 'binding_key': f'plate:{n}'}
                                     for n, g in enumerate(gcodes, 1)]
            job['artifact_binding']['gcode_sha256'] = {f'plate:{n}': model_job.file_sha256(g)
                                                       for n, g in enumerate(gcodes, 1)}
        job.update(gcode=str(gcodes[0]), gcode_verified=True, sliced_at='2026-11-17T22:30:00-0500')
        model_job.write_job(Path(job_path).parent, job)
        runs.append(str(job_path))
        return gcodes if plates > 1 else gcodes[0]

    monkeypatch.setattr(slicing, '_slice_job', orca_stand_in)
    # Carried decisions probe the fit at their own pose (scale, turn, copy limit).
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st, **pose: 1)
    # A plain function (not slice_job itself) keeps the call in this process, so the
    # stand-in above is the slicer the production entry falls through to.
    monkeypatch.setattr(model_job, 'slice_job', lambda *args, **kwargs: PRODUCTION_SLICE_JOB(*args, **kwargs))
    return runs


def intake(env, picks, mid):  # noqa: F811
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'), message_id=mid,
              text='https://www.printables.com/model/1-ghost')
    adapter = Adapter(picks)
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    iid = model_job.intake_id_from_message(event.source, mid)
    receipt = json.loads((env.root / f'{iid}.json').read_text())
    return adapter, result, receipt


def job_of(receipt):
    return json.loads(Path(receipt['job_json']).read_text())


def reuse_question(adapter):
    return next((q for q in adapter.questions if REUSE in q['choices']), None)


def color_questions(adapter):
    """The mode, mapping and slot questions a fresh intake asks before any slice."""
    return [q for q in adapter.questions
            if 'One color for everything' in q['choices'] or q['question'].startswith('Choose the color')]


def first_print(env):  # noqa: F811
    adapter, _, receipt = intake(env, FRESH_PICKS, 'm1')
    assert receipt['status'] == 'STARTED' and reuse_question(adapter) is None
    return receipt


def test_resend_reuses_the_decisions_and_the_verified_gcode_without_slicing(env, slicer):  # noqa: F811
    first = first_print(env)
    assert len(slicer) == 1
    adapter, _, receipt = intake(env, [0, 0], 'm2')
    offer = reuse_question(adapter)
    assert offer is not None and offer is adapter.questions[0]
    assert offer['choices'] == [REUSE, START_OVER, 'Cancel']
    assert 'colors White PLA (slot 1), scale 100%, no supports' in offer['question']
    assert 'no new slice is needed' in offer['question']
    assert color_questions(adapter) == []  # no color, mode or slot question again
    assert len(slicer) == 1  # the earlier verified G-code served; nothing was sliced
    job = job_of(receipt)
    earlier = job_of(first)
    assert job['reused_decisions']['intake_id'] == earlier['intake_id']
    assert job['reused_slice']['from_intake'] == earlier['intake_id']
    assert job['color_decisions'] == earlier['color_decisions']
    assert Path(job['gcode']).parent.parent == Path(receipt['job_json']).parent  # copied into the new job
    assert Path(job['gcode']).read_bytes() == Path(earlier['gcode']).read_bytes()
    assert job['artifact_binding'] == earlier['artifact_binding']
    assert 'gcode_full_verified_sha256' not in job  # the launch verifies the copy itself
    # The start still went through the bed-clear button and the print_next gates for the copy.
    assert receipt['status'] == 'STARTED' and len(env.starts) == 2
    assert env.starts[-1]['gcode'] == job['gcode']
    assert env.starts[-1]['gcode_sha256'] == model_job.file_sha256(job['gcode'])
    assert receipt['bed_clear_approval']['gcode'] == job['gcode']


def test_changed_recipe_reuses_the_decisions_and_slices_again(env, slicer, monkeypatch):  # noqa: F811
    first_print(env)
    # A slicer update changes the recipe: the old G-code no longer describes this print.
    monkeypatch.setattr(orca_adapter, 'ORCA_COMMAND', ['orca-slicer-updated'])
    adapter, _, receipt = intake(env, [0, 0], 'm2')
    offer = reuse_question(adapter)
    assert offer is not None and 'setup changed since then' in offer['question']
    assert color_questions(adapter) == []
    assert len(slicer) == 2  # sliced again, with last time's decisions
    job = job_of(receipt)
    assert 'reused_slice' not in job and job['reused_decisions']['reuse_slice'] is False
    assert job['color_decisions']['selection']['slot'] == 1
    assert receipt['status'] == 'STARTED' and env.starts[-1]['gcode'] == job['gcode']


def reused_once(env):  # noqa: F811
    """The first print, then a resend that is offered and reuses it: every earlier job on disk."""
    first = first_print(env)
    adapter, _, second = intake(env, [0, 0], 'm2')
    assert reuse_question(adapter) is not None and job_of(second).get('reused_slice')
    return [first, second]


def break_all(receipts, damage):
    for receipt in receipts:
        job = job_of(receipt)
        damage(job)
        model_job.write_job(Path(receipt['job_json']).parent, job)


def test_missing_gcode_is_never_offered(env, slicer):  # noqa: F811
    break_all(reused_once(env), lambda job: Path(job['gcode']).unlink())
    adapter, _, receipt = intake(env, FRESH_PICKS, 'm3')
    assert reuse_question(adapter) is None
    assert color_questions(adapter)  # asked from the start
    assert len(slicer) == 2 and receipt['status'] == 'STARTED'


def test_failed_verification_is_never_offered(env, slicer):  # noqa: F811
    break_all(reused_once(env), lambda job: job.update(gcode_verified=False))
    adapter, _, _ = intake(env, FRESH_PICKS, 'm3')
    assert reuse_question(adapter) is None and len(slicer) == 2


def test_tampered_gcode_is_never_offered(env, slicer):  # noqa: F811
    break_all(reused_once(env), lambda job: Path(job['gcode']).write_text('G1 X999\n'))
    adapter, _, _ = intake(env, FRESH_PICKS, 'm3')
    assert reuse_question(adapter) is None and len(slicer) == 2


def cancel_at_first_question(env, st, mid):  # noqa: F811
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'), message_id=mid,
              text='https://www.printables.com/model/1-ghost')
    adapter = Adapter([-1])
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, st, env.root, timeout=1))
    assert result['action'] == 'skip' and adapter.questions
    return adapter


def test_other_source_bytes_are_never_offered(env, slicer, monkeypatch):  # noqa: F811
    reused_once(env)
    from .test_source_colors import source
    (env.tmp / 'other').mkdir()
    other = source(env.tmp / 'other', painted=True)

    def other_download(job, chosen, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / 'ghost.3mf'
        out.write_bytes(other.read_bytes())
        return out
    monkeypatch.setattr(model_job, 'printables_download', other_download)
    assert reuse_question(cancel_at_first_question(env, state(), 'm3')) is None


def test_changed_slot_filament_is_never_offered(env, slicer):  # noqa: F811
    reused_once(env)
    changed = state()
    changed['filament_slots']['1'] = {'color': 'red', 'material': 'PETG', 'spoolman_id': 20}
    assert reuse_question(cancel_at_first_question(env, changed, 'm3')) is None


def test_start_over_asks_everything_again(env, slicer):  # noqa: F811
    first_print(env)
    adapter, _, receipt = intake(env, [1] + FRESH_PICKS, 'm2')
    assert reuse_question(adapter) is adapter.questions[0]
    assert color_questions(adapter)
    job = job_of(receipt)
    assert 'reused_decisions' not in job and 'reused_slice' not in job
    assert len(slicer) == 2 and receipt['status'] == 'STARTED'


def test_reused_gcode_still_meets_the_start_gates(env, slicer, monkeypatch):  # noqa: F811
    first_print(env)
    # The one-start-per-printer claim (the start lease) is held elsewhere: a reused
    # artifact is refused exactly like a fresh one, nothing uploaded or started.
    refused = 'REFUSED: another start already holds this printer. Nothing uploaded or started.'
    taken = []
    monkeypatch.setattr(print_next, '_take_claim', lambda claim, target: taken.append(target) or refused)
    adapter, _, receipt = intake(env, [0, 0], 'm2')
    assert reuse_question(adapter) is not None and len(slicer) == 1
    job = job_of(receipt)
    assert job.get('reused_slice') and taken and taken[-1]['gcode'] == job['gcode']
    assert receipt['status'] != 'STARTED' and receipt['started'] is False


def test_mapped_multicolor_plates_are_reused_with_their_mapping(env, slicer):  # noqa: F811
    # "Use these colors" (Shell white, Eyes black), "Slice and print now", plate 1 first.
    first_adapter, _, first = intake(env, [0, 0, 0], 'm1')
    assert first['status'] == 'STARTED' and reuse_question(first_adapter) is None
    earlier = job_of(first)
    assert earlier['color_decisions']['mode'] == 'per_part' and len(earlier['derived_plates']) == 2
    adapter, _, receipt = intake(env, [0, 0, 0], 'm2')  # like last time, print, plate 1 first
    offer = reuse_question(adapter)
    assert offer is adapter.questions[0]
    assert 'colors White PLA (slot 1), Black PLA (slot 3), scale 100%, no supports, 2 plates' in offer['question']
    assert color_questions(adapter) == [] and len(slicer) == 1
    job = job_of(receipt)
    assert job['color_decisions'] == earlier['color_decisions']
    folder = Path(receipt['job_json']).parent
    assert [Path(row['gcode']).parent.parent for row in job['derived_plates']] == [folder, folder]
    assert ([Path(row['gcode']).read_bytes() for row in job['derived_plates']]
            == [Path(row['gcode']).read_bytes() for row in earlier['derived_plates']])
    assert receipt['status'] == 'STARTED'
    assert env.starts[-1]['gcode'] in [row['gcode'] for row in job['derived_plates']]


def test_painted_multi_plate_keeps_last_times_size_without_asking(env, slicer, monkeypatch):  # noqa: F811
    from .test_source_colors import source
    painted = source(env.tmp, painted=True)

    def copy_painted(job, chosen, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / 'ghost.3mf'
        out.write_bytes(painted.read_bytes())
        return out
    monkeypatch.setattr(model_job, 'printables_download', copy_painted)
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st, **pose: 0)
    monkeypatch.setattr(model_job, 'painted_fit_for_job',
                        lambda path, decisions, state=None: {'scale': 0.95, 'plates': 2, 'blocking_plate': None})
    # Keep the painted regions with the proposed colors, accept 95% as 2 plates, print plate 1.
    first_adapter, _, first = intake(env, [0] * 20, 'm1')
    assert first['status'] == 'STARTED'
    assert any(q['choices'][0] == '95% · 2 plates' for q in first_adapter.questions)
    earlier = job_of(first)
    assert earlier['scale'] == 0.95 and earlier['plate_mode'] == 'separate'
    adapter, _, receipt = intake(env, [0] * 20, 'm2')
    offer = reuse_question(adapter)
    assert offer is adapter.questions[0] and 'scale 95%' in offer['question'] and '2 plates' in offer['question']
    assert not any(q['choices'][0] == '95% · 2 plates' for q in adapter.questions)  # the size is not asked again
    assert 'Same size as last time: 95% · 2 plates.' in adapter.sent
    assert color_questions(adapter) == [] and len(slicer) == 1
    job = job_of(receipt)
    assert job['scale'] == 0.95 and job['plate_mode'] == 'separate' and job.get('reused_slice')
    assert receipt['status'] == 'STARTED'
