"""#1648 item 7: re-sending a file already sliced offers last time's decisions and verified slice.

Every case drives the real prepare_link intake twice with the same model link. The first
intake slices through the production ``slice_job`` entry, whose inner slicer is a stand-in
that records a real recipe, artifact binding and verified G-code, exactly the fields the
real slice paths write. The second intake is the re-upload. Starts go through the real
print_next.start gates with the stub print-now from the shared ``env`` fixture.
"""
import asyncio
import json
import zipfile
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
        job['artifact_binding'] = {**model_job._recipe_binding(job),
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


def intake_as(env, picks, mid, user_id):  # noqa: F811
    """intake() with a chosen requester, for the same-requester checks."""
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id=user_id),
              message_id=mid, text='https://www.printables.com/model/1-ghost')
    adapter = Adapter(picks)
    result = asyncio.run(prepared_intake.prepare_link(
        event, adapter, state(), env.root, timeout=1))
    iid = model_job.intake_id_from_message(event.source, mid)
    return adapter, result, json.loads((env.root / f'{iid}.json').read_text())


# Fix 1: a slice built by another Print Handy version, or from a different
# resolved author-settings digest, is never a reuse source (#1665's case).


def test_other_print_handy_version_is_never_offered(env, slicer):  # noqa: F811
    break_all(reused_once(env), lambda job: job['artifact_binding'].update(
        print_handy_version='0.0.0-older'))
    adapter, _, receipt = intake(env, FRESH_PICKS, 'm3')
    assert reuse_question(adapter) is None
    assert color_questions(adapter) and len(slicer) == 2
    assert receipt['status'] == 'STARTED'


def test_changed_author_settings_digest_is_never_offered(env, slicer):  # noqa: F811
    break_all(reused_once(env), lambda job: job['artifact_binding'].update(
        author_settings_sha256='0' * 64))
    adapter, _, receipt = intake(env, FRESH_PICKS, 'm3')
    assert reuse_question(adapter) is None
    assert color_questions(adapter) and len(slicer) == 2
    assert receipt['status'] == 'STARTED'


def _cura_cfg(kind, values):
    rows = '\n'.join(f'{key} = {value}' for key, value in values.items())
    return f'[general]\nversion = 4\n\n[metadata]\ntype = {kind}\n\n[values]\n{rows}\n'


def _cura_project(tmp_path, name, *, quality_changes, quality):
    stack = ('[general]\nversion = 6\nid = M\n\n[metadata]\ntype = machine\n\n'
             '[containers]\n0 = M_user\n1 = M_qc\n2 = M_quality\n')
    members = {'Cura/M.global.cfg': stack,
               'Cura/M_user.inst.cfg': _cura_cfg('user', {}),
               'Cura/M_qc.inst.cfg': _cura_cfg('quality_changes', quality_changes),
               'Cura/M_quality.inst.cfg': _cura_cfg('quality', quality)}
    target = tmp_path / name
    with zipfile.ZipFile(target, 'w') as archive:
        for member, text in members.items():
            archive.writestr(member, text)
    return target


def test_resolved_digest_tracks_the_resolved_settings_not_the_bytes(tmp_path):
    """The digest follows what #1665's resolver reads out of a Cura project:
    changing only the vendor quality layer leaves the digest alone (those
    defaults are not the designer's), changing the designer's own layer
    moves it."""
    from print_handy.core import author_settings
    designer = {'layer_height': '0.12', 'infill_pattern': 'gyroid'}
    first = _cura_project(
        tmp_path, 'a.3mf', quality_changes=designer,
        quality={'layer_height': '0.2', 'infill_pattern': 'grid',
                 'support_structure': 'tree'})
    vendor_changed = _cura_project(
        tmp_path, 'b.3mf', quality_changes=designer,
        quality={'layer_height': '0.28', 'infill_pattern': 'lines',
                 'support_structure': 'normal', 'brim_width': '7'})
    designer_changed = _cura_project(
        tmp_path, 'c.3mf', quality_changes={'layer_height': '0.2',
                                            'infill_pattern': 'gyroid'},
        quality={'layer_height': '0.2', 'infill_pattern': 'grid',
                 'support_structure': 'tree'})
    resolved = author_settings.read_project(first)['author']
    assert resolved.get('layer_height') == '0.12'
    assert author_settings.resolved_digest(first) == \
        author_settings.resolved_digest(vendor_changed)
    assert author_settings.resolved_digest(first) != \
        author_settings.resolved_digest(designer_changed)
    assert author_settings.resolved_digest(tmp_path / 'missing.3mf') is None


# Fix 2: the offer is only ever made to the requester of the earlier job.


def test_other_requester_is_never_offered(env, slicer):  # noqa: F811
    first_print(env)
    adapter, _, receipt = intake_as(env, FRESH_PICKS, 'm2', 'someone-else')
    assert reuse_question(adapter) is None
    assert color_questions(adapter) and len(slicer) == 2
    assert receipt['status'] == 'STARTED'


def test_unstamped_earlier_job_is_never_offered(env, slicer):  # noqa: F811
    break_all(reused_once(env), lambda job: job.pop('started_by', None))
    adapter, _, receipt = intake(env, FRESH_PICKS, 'm3')
    assert reuse_question(adapter) is None
    assert color_questions(adapter) and len(slicer) == 2
    assert receipt['status'] == 'STARTED'


# Fix 3: a job that was sliced but never approved and started is not a
# reuse source, however good its slice is.


def test_sliced_but_never_started_is_never_offered(env, slicer):  # noqa: F811
    # "Slice and Download", then "Done, don't print" at the plate menu: a
    # complete verified slice, never approved for a start, never started.
    adapter, _, first = intake(env, [2, 0, 1, 2], 'm1')
    assert first['status'] == 'SLICED_NOT_STARTED' and first['started'] is False
    assert job_of(first).get('gcode_verified')
    adapter, _, receipt = intake(env, FRESH_PICKS, 'm2')
    assert reuse_question(adapter) is None
    assert color_questions(adapter) and len(slicer) == 2
    assert receipt['status'] == 'STARTED'


def test_approved_but_start_refused_is_never_offered(env, slicer, monkeypatch):  # noqa: F811
    from print_handy.runtime import print_next
    refused = ('REFUSED: another start already holds this printer. '
               'Nothing uploaded or started.')
    gate = {'refuse': True}
    real_take = print_next._take_claim

    def take(claim, target):
        return refused if gate['refuse'] else real_take(claim, target)

    monkeypatch.setattr(print_next, '_take_claim', take)
    adapter, _, first = intake(env, FRESH_PICKS, 'm1')
    # The operator approved the start; the start itself was refused.
    assert first['bed_clear_approval'] and first['started'] is False
    assert first['status'] != 'STARTED'
    gate['refuse'] = False
    adapter, _, receipt = intake(env, FRESH_PICKS, 'm2')
    assert reuse_question(adapter) is None
    assert color_questions(adapter) and len(slicer) == 2
    assert receipt['status'] == 'STARTED'


# Fix 4: races between the offer's checks and the slice-time copy fall back
# to a fresh slice — never a partial copy, never an exception.


def test_copy_failure_falls_back_to_a_fresh_slice(env, slicer, monkeypatch):  # noqa: F811
    import shutil
    first = first_print(env)
    earlier = job_of(first)
    earlier_folder = Path(first['job_json']).parent
    real_copy2 = shutil.copy2

    def flaky_copy2(src, dst, *args, **kwargs):
        if earlier_folder in Path(src).parents:
            raise OSError('the artifact vanished mid-copy')
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, 'copy2', flaky_copy2)
    adapter, _, receipt = intake(env, [0, 0], 'm2')
    assert reuse_question(adapter) is not None  # the offer itself was sound
    job = job_of(receipt)
    assert 'reused_slice' not in job  # nothing was adopted
    # A fresh slice served the print instead of the copied bytes.
    assert Path(job['gcode']).read_bytes() != Path(earlier['gcode']).read_bytes()
    assert receipt['status'] == 'STARTED'


def test_failed_cleanup_of_a_partial_copy_does_not_escape(tmp_path, caplog):
    # duckcatgnaf on #1671 (slice.py copy fallback): a cleanup unlink that
    # raises must not escape, or intake aborts instead of slicing fresh.
    from print_handy.jobs.slice import _discard_copies

    class Stuck:
        def unlink(self, missing_ok=False):
            raise OSError('read-only file system')

        def __str__(self):
            return 'stuck.gcode'

    removable = tmp_path / 'copied.gcode'
    removable.write_text('G28')
    _discard_copies([Stuck(), removable])  # must not raise
    assert not removable.exists()  # later copies are still removed
    assert 'could not remove the partial copy stuck.gcode' in caplog.text


def test_a_copy_that_fails_midway_leaves_no_partial_file(env, slicer, monkeypatch):  # noqa: F811
    # duckcatgnaf on #1671: copy2 can create or truncate dest and then raise;
    # that partial file must be removed before the fresh slice runs.
    import shutil
    from print_handy.jobs import slice as slice_mod
    first = first_print(env)
    earlier_folder = Path(first['job_json']).parent
    real_copy2, real_discard = shutil.copy2, slice_mod._discard_copies
    partials, removed = [], []

    def half_copy2(src, dst, *args, **kwargs):
        if earlier_folder in Path(src).parents:
            Path(dst).write_bytes(b'G28\n; truncated')
            partials.append(Path(dst))
            raise OSError('disk full mid-copy')
        return real_copy2(src, dst, *args, **kwargs)

    def spy_discard(copied):
        real_discard(copied)
        removed.extend((Path(p), Path(p).exists()) for p in copied)

    monkeypatch.setattr(shutil, 'copy2', half_copy2)
    monkeypatch.setattr(slice_mod, '_discard_copies', spy_discard)
    adapter, _, receipt = intake(env, [0, 0], 'm2')
    assert reuse_question(adapter) is not None
    assert partials, 'the reuse copy was never attempted'
    # The partial file was handed to cleanup and was gone before slicing fresh.
    assert [(p, False) for p in partials] == [(p, e) for p, e in removed if p in partials]
    job = job_of(receipt)
    assert 'reused_slice' not in job
    assert receipt['status'] == 'STARTED'


def test_gcode_vanishing_during_the_scan_is_not_an_error(env, slicer, monkeypatch):  # noqa: F811
    from print_handy.gcode import artifact as gcode_artifact
    first = first_print(env)
    earlier_folder = Path(first['job_json']).parent
    real_sha = gcode_artifact.file_sha256

    def vanishing(path):
        if earlier_folder in Path(path).parents:
            raise OSError('vanished mid-scan')
        return real_sha(path)

    monkeypatch.setattr(gcode_artifact, 'file_sha256', vanishing)
    adapter, _, receipt = intake(env, FRESH_PICKS, 'm2')
    assert reuse_question(adapter) is None
    assert color_questions(adapter) and len(slicer) == 2
    assert receipt['status'] == 'STARTED'


def test_adopt_rechecks_the_slots_against_fresh_state(env, slicer, monkeypatch):  # noqa: F811
    """The offer checks the loaded slots; the copy at slice time checks them
    again, so a spool swapped in between refuses the reuse."""
    from print_handy.jobs import slice as slicing
    first = first_print(env)
    earlier = job_of(first)
    folder = Path(env.tmp) / 'archive' / 'resend-job'
    folder.mkdir(parents=True)
    new_job = dict(earlier)
    new_job.pop('reused_slice', None)
    new_job['reused_decisions'] = {'intake_id': earlier['intake_id'],
                                   'job_json': first['job_json'],
                                   'reuse_slice': True}
    job_path = folder / 'JOB.json'
    job_path.write_text(json.dumps(new_job))
    fresh = []

    def sentinel(path, **kwargs):
        fresh.append(str(path))
        return Path(path).parent / 'sliced' / 'fresh.gcode'

    monkeypatch.setattr(slicing, '_slice_job', sentinel)
    # Slots as offered: the verified slice is adopted, no fresh slice runs.
    result = slicing.slice_job(job_path)
    assert fresh == [] and Path(result).is_relative_to(folder)
    # A slot changed since the offer: the copy is refused, a fresh slice runs.
    monkeypatch.setattr(prepared_intake, 'loaded_slot_change',
                        lambda decisions, st: 'slot 1 held white PLA and now '
                                              'holds red PETG')
    result = slicing.slice_job(job_path)
    assert fresh == [str(job_path)]
    assert result == folder / 'sliced' / 'fresh.gcode'


# Fix 5: the earlier job's supports answer must not silence a supports flag
# on a fresh slice — only on the slice that is actually reused.


def _force_supports_flag(monkeypatch):
    """Give every job a risky support-clearance report: the one supports
    flag a fresh slice can raise even when the job already carries an
    operator's earlier answer (a slicer finding is answered by that answer
    and never re-fires, in production too; a clearance report is recomputed
    from the mesh every time). The harness model analyzes to no report, so
    the report is supplied at the analyzer seam."""
    from print_handy.core import support_clearance
    report = {'risky': True, 'findings': []}
    monkeypatch.setattr(support_clearance, 'safe_analyze',
                        lambda source, first_layer_mm=None: report)
    # The shared env holds every background slice until the delivery step;
    # the supports question is composed before that, so let this slice run
    # (the pattern the prepared-intake lifecycle tests use).
    from print_handy.core import background_slice
    held_init = background_slice.BackgroundSlice.__init__

    def released_init(runner):
        held_init(runner)
        runner.release.set()

    monkeypatch.setattr(background_slice.BackgroundSlice, '__init__', released_init)


def _restore_verified_artifact(first):
    """Re-slice the first job through the production entry, leaving the
    verified artifact production's delivery slice leaves: in this harness
    the speculative supports slice shares the first slice's file name and
    overwrites it, while the real delivery slice re-checks the artifact
    against its binding and restores matching bytes."""
    slicing.slice_job(Path(first['job_json']))


class PickByLabel(Adapter):
    """Adapter whose picks may be choice-label substrings instead of indices
    (the flagged Ready menu orders its supports choices by recommendation)."""

    async def send_clarify(self, **kw):
        choices = kw.get('choices') or []
        pick = next(self.picks)
        if isinstance(pick, str):
            pick = next(i for i, choice in enumerate(choices) if pick in str(choice))
        self.questions.append(kw)
        if len(choices) == 1:
            self.log.append(kw['question'])
        answer = choices[pick]
        import tools.clarify_gateway as cm
        cm.resolve_gateway_clarify(kw['clarify_id'], answer)
        return S(success=True)


def intake_labeled(env, picks, mid):  # noqa: F811
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id=mid, text='https://www.printables.com/model/1-ghost')
    adapter = PickByLabel(picks)
    result = asyncio.run(prepared_intake.prepare_link(
        event, adapter, state(), env.root, timeout=1))
    iid = model_job.intake_id_from_message(event.source, mid)
    return adapter, result, json.loads((env.root / f'{iid}.json').read_text())


def test_fresh_slice_after_a_setup_change_asks_supports_again(env, slicer, monkeypatch):  # noqa: F811
    _force_supports_flag(monkeypatch)
    # First print: the flag is raised and the operator answers "without
    # supports" in the Ready menu.
    adapter, _, first = intake_labeled(env, [2, 0, 'without supports', 0, 0, 0, 0, 0], 'm1')
    for q in adapter.questions:
        print('Q1:', q['question'][:70].replace('\n', ' '), '|', [str(c)[:40] for c in q.get('choices') or []])
    print('FIRST:', first['status'])
    assert first['status'] == 'STARTED'
    _restore_verified_artifact(first)
    decision = job_of(first).get('support_decision') or {}
    assert decision.get('by') == 'operator' and decision.get('supports') is False
    # A slicer update changes the recipe: the decisions are reused, the
    # slice is not — and the fresh slice's own flag is asked, not silenced.
    monkeypatch.setattr(orca_adapter, 'ORCA_COMMAND', ['orca-slicer-updated'])
    adapter, _, receipt = intake_labeled(env, [0, 'without supports'], 'm2')
    asked = [q for q in adapter.questions
             if any('without supports' in choice for choice in q['choices'])]
    assert asked, 'the fresh slice never asked about supports'
    job = job_of(receipt)
    assert 'reused_slice' not in job
    assert (job.get('support_decision') or {}).get('by') == 'operator'
    assert receipt['status'] == 'STARTED'


def test_reused_slice_keeps_last_times_supports_answer(env, slicer, monkeypatch):  # noqa: F811
    _force_supports_flag(monkeypatch)
    adapter, _, first = intake_labeled(env, [2, 0, 'without supports', 0, 0, 0, 0, 0], 'm1')
    for q in adapter.questions:
        print('Q1:', q['question'][:70].replace('\n', ' '), '|', [str(c)[:40] for c in q.get('choices') or []])
    print('FIRST:', first['status'])
    assert first['status'] == 'STARTED'
    _restore_verified_artifact(first)
    # Nothing changed: the verified slice itself is reused, so its supports
    # answer stands and the question is not asked again.
    adapter, _, receipt = intake(env, [0, 0], 'm2')
    asked = [q for q in adapter.questions
             if any('without supports' in choice for choice in q['choices'])]
    assert asked == []
    job = job_of(receipt)
    assert job.get('reused_slice') and receipt['status'] == 'STARTED'
    assert (job.get('support_decision') or {}).get('by') == 'operator'


# Fix 6: "no new slice is needed" is computed for the NEW job (its own
# parameters with the carried decisions applied), not for the old one.


def _offer_for(env, earlier, **extra):  # noqa: F811
    job = {'color_manifest': {'source_sha256':
                              earlier['color_manifest']['source_sha256']},
           'printer_id': earlier.get('printer_id'),
           'downloaded_file': earlier['downloaded_file'],
           'started_by': {'platform': 'discord', 'user_id': 'u'}, **extra}
    adapter = Adapter([0])
    source = S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u')
    reused = asyncio.run(prepared_intake._offer_previous_decisions(
        adapter, source, 'k', 1, job, state(), Path(env.tmp) / 'new-job',
        receipts_root=env.root))
    return adapter, reused


def test_offer_recipe_verdict_is_computed_for_the_new_job(env, slicer):  # noqa: F811
    earlier = job_of(first_print(env))
    # The new job carries a full-size split plan the earlier job never had:
    # its recipe differs, so the offer must say a fresh slice will run.
    adapter, reused = _offer_for(env, earlier,
                               full_size_split_plan={'kind': 'test-plan'})
    assert reused is not None and reused['reuse_slice'] is False
    assert 'slice it again' in adapter.questions[0]['question']
    # Control: the same job with nothing new really does need no new slice.
    adapter, reused = _offer_for(env, earlier)
    assert reused is not None and reused['reuse_slice'] is True
    assert 'no new slice is needed' in adapter.questions[0]['question']


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




