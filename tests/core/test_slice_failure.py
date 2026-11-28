"""Failures must retain their cause without putting raw worker output in chat."""
import asyncio
import json
import subprocess
import sys

import pytest

from print_handy import diagnostics
from print_handy.core import owned_worker, slice_failure, worker_errors
from print_handy.intake import worker as intake_worker
from print_handy.core.resources import ResourceLimitError
from print_handy.core.slice_refusal import unconfirmed_slot_color


@pytest.mark.parametrize('error,category,phrase', [
    (subprocess.TimeoutExpired(['secret-command'], 600), 'transient', 'time limit'),
    (ResourceLimitError('container is at its process limit SECRET'), 'configuration', 'process limit'),
    (TypeError('SECRET implementation detail'), 'programming', 'internal error'),
    (slice_failure.SliceFailure('Pre-slice guard blocked: nozzle too hot', kind='guard'), 'configuration', 'safety check'),
    (slice_failure.SliceFailure('SECRET stderr', kind='engine', slicer='OrcaSlicer'), 'unknown', 'OrcaSlicer'),
    (RuntimeError('SECRET unknown'), 'unknown', 'could not identify the cause'),
])
def test_error_protocol_preserves_category_and_plain_cause(error, category, phrase):
    reply = json.loads(json.dumps(worker_errors.encode(error)))
    back = worker_errors.decode(reply)
    assert str(back) == str(error)
    assert diagnostics.classify(back).value == category
    post = slice_failure.post(back)
    assert phrase in post
    assert 'SECRET' not in post and 'secret-command' not in post
    assert 'nothing was uploaded or started' in post
    assert 'Reference: [PH_SLICING_' in post


def test_protocol_preserves_refusal_and_safe_fix():
    original = unconfirmed_slot_color(2, {'spoolman_id': 3, 'material': 'PLA', 'name': 'Blue PLA'}, 'raw')
    back = worker_errors.decode(json.loads(json.dumps(worker_errors.encode(original))))
    assert back.fix == original.fix and back.plain == original.plain
    assert diagnostics.classify(back) == diagnostics.Category.CONFIGURATION


def test_worker_protocol_never_imports_a_reported_type():
    back = worker_errors.decode({'type': 'os.system', 'error': 'SECRET'})
    assert isinstance(back, RuntimeError)
    assert diagnostics.classify(back) == diagnostics.Category.UNKNOWN
    assert 'SECRET' not in slice_failure.post(back)


@pytest.mark.parametrize('error_code,phrase,category', [
    ("subprocess.TimeoutExpired(['SECRET-command'], 600)", 'time limit', 'transient'),
    ("ResourceLimitError('container is at its process limit SECRET')", 'process limit', 'configuration'),
    ("SliceFailure('SECRET-stderr', kind='engine', slicer='OrcaSlicer')", 'OrcaSlicer', 'unknown'),
])
def test_real_child_process_error_reaches_parent_heavy_boundary(monkeypatch, tmp_path, error_code, phrase, category):
    # Run the production worker __main__ and its error encoder in a real child.
    # Only the slice function is replaced; the transport, JSON and parent decoder
    # are real. No printer, external download or installed slicer is needed.
    script = f'''import runpy, subprocess
from print_handy.jobs import slice as jobs_slice
from print_handy.core.resources import ResourceLimitError
from print_handy.core.slice_failure import SliceFailure
def fail(*args, **kwargs):
    raise {error_code}
jobs_slice.slice_job = fail
runpy.run_module('print_handy.core.heavy_worker', run_name='__main__')
'''
    real_run = owned_worker.run

    async def child(command, payload, **kwargs):
        return await real_run([sys.executable, '-c', script], payload, **kwargs)

    monkeypatch.setattr(owned_worker, 'run', child)
    from print_handy.jobs import slice as jobs_slice
    with pytest.raises(RuntimeError) as info:
        asyncio.run(intake_worker._heavy(jobs_slice.slice_job, str(tmp_path / 'JOB.json')))
    assert diagnostics.classify(info.value).value == category
    post = slice_failure.post(info.value)
    assert phrase in post and 'SECRET' not in post


def test_slicer_timeout_keeps_partial_output_before_cleanup(monkeypatch, tmp_path):
    from print_handy.core import orca_adapter
    work = tmp_path / 'work'
    work.mkdir()
    kept = tmp_path / 'kept'
    from print_handy.runtime import _config
    monkeypatch.setattr(_config, 'state_home', lambda: str(kept))

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(['secret'], 600, output=b'partial output', stderr=b'partial error')

    monkeypatch.setattr(orca_adapter, '_spawn_slicer', timeout)
    with pytest.raises(slice_failure.SliceFailure) as info:
        orca_adapter._run_slicer(['secret'], 'OrcaSlicer', work)
    assert info.value.kind == 'timeout'
    retained = kept / 'state' / 'slice-failures' / work.name / 'orca.log'
    assert retained.read_text() == 'partial outputpartial error'
    assert str(retained.parent) in str(info.value)


def test_invalid_wire_kind_and_slicer_cannot_inject_chat_text():
    back = slice_failure.SliceFailure.from_dict({'message': 'SECRET', 'kind': 'fake', 'slicer': 'SECRET'})
    assert 'SECRET' not in slice_failure.post(back)
    assert 'could not identify the cause' in slice_failure.post(back)


@pytest.mark.parametrize('kind,phrase', [
    ('launch', 'could not be opened'), ('output', 'generator could not be verified'),
    ('conflict', 'conflicting toolpaths'),
])
def test_other_identified_failures_survive_serialization_without_blind_retry(kind, phrase):
    error = slice_failure.SliceFailure('SECRET', kind=kind, slicer='OrcaSlicer')
    back = worker_errors.decode(json.loads(json.dumps(worker_errors.encode(error))))
    assert phrase in slice_failure.post(back)
    assert 'SECRET' not in slice_failure.post(back)
    assert not slice_failure.explain(back).retryable


def test_configuration_error_remains_nonretryable_after_worker_roundtrip():
    from print_handy.settings import SettingsError
    back = worker_errors.decode(worker_errors.encode(SettingsError('SECRET configuration')))
    assert diagnostics.classify(back) == diagnostics.Category.CONFIGURATION
    assert 'configuration check failed' in slice_failure.post(back)
    assert not slice_failure.explain(back).retryable


def test_missing_executable_preserves_exception_api_and_helpful_worker_message():
    back = worker_errors.decode(json.loads(json.dumps(worker_errors.encode(FileNotFoundError('SECRET path')))))
    assert isinstance(back, FileNotFoundError)
    assert 'could not be opened' in slice_failure.post(back)
    assert '/print-handy debugging' in slice_failure.post(back)
    assert 'SECRET' not in slice_failure.post(back)
    assert not slice_failure.explain(back).retryable


def test_failed_retry_keeps_new_known_cause_remedy():
    post = slice_failure.post(ResourceLimitError('container is at its process limit SECRET'), retry_failed=True)
    assert 'The one retry also failed' in post and '--pids-limit' in post
    assert 'PH_SLICING_CONFIGURATION' in post and 'SECRET' not in post


def test_timeout_log_write_failure_does_not_replace_original_cause(monkeypatch, tmp_path):
    from pathlib import Path
    from print_handy.core import orca_adapter

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(['secret'], 600, output=b'partial')

    monkeypatch.setattr(orca_adapter, '_spawn_slicer', timeout)
    monkeypatch.setattr(Path, 'write_text', lambda *a, **k: (_ for _ in ()).throw(PermissionError('read-only')))
    with pytest.raises(slice_failure.SliceFailure) as info:
        orca_adapter._run_slicer(['secret'], 'OrcaSlicer', tmp_path)
    assert info.value.kind == 'timeout'


def test_guard_failure_shows_its_reason_in_chat():
    exc = slice_failure.SliceFailure('Pre-slice guard blocked: bed temperature 400 exceeds the 300 limit',
                                     kind='guard')
    post = slice_failure.post(exc)
    assert 'bed temperature 400 exceeds the 300 limit' in post
    assert 'Pre-slice guard blocked' not in post


def test_guard_reason_is_one_line_redacted_and_capped():
    secret = 'Bearer abcdefghijklmnopqrstuvwxyz123456'
    exc = slice_failure.SliceFailure(
        f'Pre-slice guard blocked: first {secret}\nsecond line; ' + 'x' * 500, kind='guard')
    reason = slice_failure.guard_reason(exc)
    assert '\n' not in reason and 'abcdefghijklmnopqrstuvwxyz123456' not in reason
    assert len(reason) <= slice_failure.GUARD_REASON_LIMIT and reason.endswith('...')
    assert '\n' not in slice_failure.explain(exc).plain


def test_guard_failure_without_a_reason_keeps_the_generic_text():
    exc = slice_failure.SliceFailure('Pre-slice guard blocked:', kind='guard')
    assert slice_failure.explain(exc).plain == 'A safety check rejected the slicing settings or generated G-code.'


def test_unexplained_worker_error_logs_child_traceback_but_not_in_chat(monkeypatch, tmp_path, caplog):
    # A real child raises a plain bug (the P2-282 shape). The parent log must
    # name the child's failing frame, or the operator sees only the message.
    script = '''import runpy
from print_handy.jobs import slice as jobs_slice
def failing_build_item_edit(*args, **kwargs):
    None.group(1)
jobs_slice.slice_job = failing_build_item_edit
runpy.run_module('print_handy.core.heavy_worker', run_name='__main__')
'''
    real_run = owned_worker.run

    async def child(command, payload, **kwargs):
        return await real_run([sys.executable, '-c', script], payload, **kwargs)

    monkeypatch.setattr(owned_worker, 'run', child)
    from print_handy.jobs import slice as jobs_slice
    with caplog.at_level('ERROR', logger='print_handy.intake.worker'):
        with pytest.raises(worker_errors.RemoteError) as info:
            asyncio.run(intake_worker._heavy(jobs_slice.slice_job, str(tmp_path / 'JOB.json')))
    assert "'NoneType' object has no attribute 'group'" in str(info.value)
    assert 'failing_build_item_edit' in info.value.worker_traceback
    assert 'failing_build_item_edit' in caplog.text and 'worker traceback' in caplog.text
    post = slice_failure.post(info.value)
    assert 'Traceback' not in post and 'failing_build_item_edit' not in post


def test_worker_traceback_is_bounded_and_skipped_for_explained_failures():
    try:
        raise RuntimeError('x' * (worker_errors.TRACEBACK_LIMIT * 2))
    except RuntimeError as exc:
        reply = worker_errors.encode(exc)
    assert len(reply['traceback']) == worker_errors.TRACEBACK_LIMIT
    explained = worker_errors.encode(slice_failure.SliceFailure('guard', kind='guard'))
    assert 'traceback' not in explained


@pytest.mark.parametrize('message,plain,recovery,retryable', [
    ('only STL/3MF can be sliced; selected source is not geometry', 'not a model Print Handy can slice', 'Send the model as an .stl or .3mf file.', False),
    ('3MF plate assignment has no plater_id value', '3MF file has a plate with no valid plate assignment', 'Re-export the model as a fresh 3MF', False),
    ('unable to inspect 3MF plate assignments: malformed value', '3MF file has invalid or unreadable plate assignments', 'Re-export the model as a fresh 3MF', False),
    ('multi-plate 3MF source has no plate thumbnails; no slicing attempted', 'multi-plate 3MF is missing its plate thumbnails', 'Re-export the model as a fresh 3MF', False),
    ('multi-plate 3MF cannot be merged or split: invalid inventory; no slicing attempted', "multi-plate 3MF's parts could not be assigned to plates", 'Re-export the model as a fresh 3MF', False),
    ('part/color choices required; run color-plan and color-select before slicing', 'needs color or part choices', 'color-plan', False),
    ('per-part decision did not resolve any single-color plate', 'did not resolve to any printable plate', 'color-plan', False),
    ('derived plate physical IFS slot 2 has no verified material', 'slot 2 has no verified material', 'Record the loaded spool', False),
    ('source, decisions or slice recipe changed during slicing; artifacts not made ready', 'saved job or printer settings changed after this slice was requested', 'Prepare the saved model again', False),
    ('source, decisions or slice recipe changed during slicing; artifact not made ready', 'saved job or printer settings changed after this slice was requested', 'Prepare the saved model again', False),
])
def test_slice_refusals_survive_worker_wire_and_explain_without_retry(message, plain, recovery, retryable):
    from print_handy.core.slice_refusal import SliceRefusal
    original = SliceRefusal(message, plain=plain, next_step=recovery)
    back = worker_errors.decode(json.loads(json.dumps(worker_errors.encode(original))))
    post = slice_failure.post(back)
    info = slice_failure.explain(back)
    assert plain in post
    assert 'could not identify the cause' not in post
    assert info.retryable is retryable
    assert recovery in post


def test_recipe_change_slice_failure_survives_worker_wire_without_retry():
    original = slice_failure.SliceFailure('source, decisions or slice recipe changed during slicing', kind='recipe')
    back = worker_errors.decode(json.loads(json.dumps(worker_errors.encode(original))))
    post = slice_failure.post(back)
    assert 'saved job or printer settings changed after this slice was requested' in post
    assert 'could not identify the cause' not in post
    assert not slice_failure.explain(back).retryable
    assert 'Prepare the saved model again' in post


def test_slice_boundaries_bare_runtime_errors_are_allowlisted():
    import ast
    from pathlib import Path
    source = Path('print_handy/jobs/slice.py').read_text()
    tree = ast.parse(source)
    # Internal invariants are deliberately left as RuntimeError; keep a reason beside each.
    allowlist = {
        'multi-plate 3MF objects ': 'an audited source object missing a plate assignment is an internal inventory invariant',
        "plate_mode must be 'merge' or 'separate'": 'caller mode is constrained by the internal slice planner',
        'every derived plate was refused: ': 'all per-plate slicer refusals are reported as an aggregated pipeline failure',
        'per-plate output does not account for every source part': 'artifact/source accounting invariant',
    }
    offenders = []
    for name in ('_slice_job', 'slice_multi_plate', '_slice_per_plate'):
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        for node in ast.walk(fn):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            if not (isinstance(node.exc.func, ast.Name) and node.exc.func.id == 'RuntimeError'):
                continue
            arg = node.exc.args[0] if node.exc.args else None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                text = arg.value
            elif isinstance(arg, ast.JoinedStr):
                text = ''.join(v.value for v in arg.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
            elif isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add):
                text = ''.join(v.value for v in ast.walk(arg) if isinstance(v, ast.Constant) and isinstance(v.value, str))
            else:
                text = ''
            if text.startswith('every derived plate was refused:'):
                continue
            if any(text.startswith(allowed) for allowed in allowlist):
                continue
            offenders.append((name, node.lineno, text))
    assert not offenders, f'Unallowlisted bare RuntimeError: {offenders}'

