"""#817: every model failure names its stage, cause and recovery, and says
whether anything was submitted. Raw slicer text never reaches chat."""
import json

import pytest

from print_handy.core import failure_report, orca_adapter, prepared_intake, slice_failure, worker_errors

ORCA_LOG = ("[error] can not find object id 1 to builditem\n"
            "Error (Invalid 3MF format) while parsing '3D/3dmodel.model' at line 2\n")


@pytest.mark.parametrize('text,code,expected', [
    ('can not find object id 1 to builditem', None, 'model_unreadable'),
    ("Error (Invalid 3MF format) while parsing '3D/3dmodel.model' at line 2", None, 'model_unreadable'),
    ('Archive does not contain a valid model', None, 'model_unreadable'),
    ('The input model file to the slicer can not be parsed.', None, 'model_unreadable'),
    ('anything at all', -6, 'model_unreadable'),
    ('Object Cube is outside the print volume', None, 'outside_bed'),
    ('Nothing to be sliced, either the print is empty', None, 'empty_plate'),
    ('Failed slicing the model', -100, 'thin_features'),
    ('std::bad_alloc', None, 'out_of_memory'),
    ('everything went fine', None, None),
    ('', None, None),
])
def test_known_slicer_signatures(text, code, expected):
    assert failure_report.match_signature(text, code) == expected


@pytest.mark.parametrize('text,safe', [
    ('The input model file to the slicer can not be parsed.', True),
    ('Failed slicing the model', True),
    ('Traceback (most recent call last):', False),
    ('failed to open /home/user/models/secret.3mf', False),
    ('C:\\Users\\andre\\model.stl is broken', False),
    ('x' * 400, False),
    ('', False),
    ('line with a control char \x07', False),
])
def test_safe_detail_quotes_only_a_short_pathless_sentence(text, safe):
    assert bool(failure_report.safe_detail(text)) is safe


def test_safe_detail_keeps_only_the_first_line_and_redacts():
    detail = failure_report.safe_detail('Bad value token=sekrit123abc\nsecond line')
    assert 'second line' not in detail and 'sekrit123abc' not in detail


def test_unknown_stage_is_a_programming_error():
    with pytest.raises(ValueError):
        failure_report.describe(RuntimeError('x'), 'nowhere')


def test_every_stage_has_a_lead_sentence():
    assert set(failure_report.STAGES) == {'intake', 'parse', 'repair', 'color plan', 'plate plan',
                                          'slice', 'upload', 'launch'}
    for stage in failure_report.STAGES:
        report = failure_report.Report(stage, 'Cause.', 'Do this.')
        assert failure_report.lead(report).startswith(failure_report._HEADLINES[stage])


def test_recognized_text_names_its_stage_cause_and_recovery_without_raw_text():
    post = failure_report.post(RuntimeError('Invalid 3MF format at /data/secret/path.3mf'), 'intake',
                               subsystem='intake', headline='Preparing that model stopped before it was ready.')
    assert 'Stage: parse.' in post and 'could not read this 3MF' in post
    assert 'Re-export the model' in post and 'Nothing was uploaded or started.' in post
    assert 'stopped before it was ready' not in post
    assert '/data/secret' not in post and 'RuntimeError' not in post


def test_known_cause_gets_the_stage_lead_not_the_generic_one():
    exc = RuntimeError('MakerWorld links need an account token in /data/.config/bambu/token; '
                       'see docs/PROJECT-REFERENCE.md (MakerWorld account token)')
    post = failure_report.post(exc, 'intake', subsystem='intake',
                               headline='Preparing that model stopped before it was ready.')
    assert 'stopped before it was ready' not in post
    assert 'Stage: intake.' in post and 'MakerWorld' in post and '/data/.config' not in post


def test_unrecognized_failure_keeps_caller_headline_and_still_names_stage_and_recovery():
    post = failure_report.post(RuntimeError('SECRET mystery'), 'plate plan', subsystem='intake',
                               headline='I could not finish the check.')
    assert post.startswith('I could not finish the check. Stage: plate plan.')
    assert '/print-handy bundle' in post and 'SECRET' not in post
    assert 'Reference: PH_INTAKE_UNKNOWN' in post


def test_signature_on_the_exception_beats_its_text():
    exc = slice_failure.SliceFailure('SECRET', kind='engine', signature='outside_bed')
    report = failure_report.describe(exc, 'slice')
    assert report.signature == 'outside_bed' and report.stage == 'plate plan'


# --- the slicer boundary ---------------------------------------------------------------------

def test_failure_evidence_reads_the_orca_logs_and_result_json(tmp_path):
    (tmp_path / 'orca.log').write_text(ORCA_LOG)
    (tmp_path / 'result.json').write_text(json.dumps(
        {'return_code': -6, 'error_string': 'The input model file to the slicer can not be parsed.'}))
    evidence = orca_adapter._failure_evidence(tmp_path)
    assert evidence['signature'] == 'model_unreadable'
    assert evidence['detail'] == 'The input model file to the slicer can not be parsed.'


def test_failure_evidence_can_be_limited_to_the_orca_log(tmp_path):
    # A missing Bambu binary must not hide why Orca failed, nor invent a cause of its own.
    (tmp_path / 'orca.log').write_text(ORCA_LOG)
    (tmp_path / 'bambu.log').write_text('taskset: failed to execute /usr/bin/bambu-studio: No such file')
    assert orca_adapter._failure_evidence(tmp_path, 'orca.log')['signature'] == 'model_unreadable'
    assert orca_adapter._failure_evidence(tmp_path, 'bambu.log')['signature'] is None


def test_failure_evidence_never_raises(tmp_path):
    assert orca_adapter._failure_evidence(tmp_path / 'missing') == {'signature': None, 'detail': ''}
    (tmp_path / 'result.json').write_text('not json')
    assert orca_adapter._failure_evidence(tmp_path)['signature'] is None


def test_unreadable_3mf_failure_reaches_chat_as_parse_stage_across_the_worker_wire():
    original = slice_failure.SliceFailure(
        'Orca slicing failed (250) SECRET; see /data/state/slice-failures/x/orca.log', kind='engine',
        slicer='OrcaSlicer and Bambu Studio', returncode=250,
        **failure_report.slicer_evidence(ORCA_LOG, 'The input model file to the slicer can not be parsed.', -6))
    back = worker_errors.decode(json.loads(json.dumps(worker_errors.encode(original))))
    post = slice_failure.post(back)
    assert 'Stage: parse.' in post and 'could not read this 3MF' in post
    assert 'Re-export the model' in post
    assert 'nothing was uploaded or started' in post and 'PH_SLICING_' in post
    assert 'SECRET' not in post and '/data/state' not in post
    assert 'could not identify the cause' not in post


def test_unmatched_engine_failure_quotes_only_the_safe_sentence():
    quoted = slice_failure.SliceFailure('SECRET', kind='engine', slicer='OrcaSlicer', returncode=3,
                                        detail='Some brand new slicer complaint')
    post = slice_failure.post(quoted)
    assert 'OrcaSlicer said: "Some brand new slicer complaint"' in post
    assert 'Stage: slice.' in post and 'SECRET' not in post
    assert '/print-handy bundle' in post
    leaky = slice_failure.SliceFailure('SECRET', kind='engine', slicer='OrcaSlicer',
                                       detail='Traceback (most recent call last): File "/app/x.py"')
    assert 'Traceback' not in slice_failure.post(leaky) and '/app/x.py' not in slice_failure.post(leaky)


def test_wire_cannot_inject_a_signature_or_unsafe_detail():
    back = slice_failure.SliceFailure.from_dict(
        {'message': 'm', 'kind': 'engine', 'signature': 'made_up', 'detail': 'see /etc/passwd'})
    assert back.signature is None and back.detail == ''


def test_slice_failure_report_feeds_receipts():
    exc = slice_failure.SliceFailure('x', kind='engine', signature='model_unreadable')
    record = slice_failure.report(exc).as_dict()
    assert record['stage'] == 'parse' and record['signature'] == 'model_unreadable'
    assert record['submitted'] == 'nothing' and record['recovery']


# --- upload and launch -----------------------------------------------------------------------

def test_refused_start_says_nothing_was_submitted():
    report = prepared_intake._start_report(
        'REFUSED: internal probe 7 failed. Nothing uploaded or started.')
    assert report.stage == 'launch' and report.submitted is failure_report.Submitted.NOTHING
    assert failure_report.state_line(report) == 'Nothing was uploaded or started.'


def test_printer_rejected_start_says_the_file_is_there():
    report = prepared_intake._start_report(
        'REFUSED: the printer rejected the start request (busy). Check it.')
    assert report.submitted is failure_report.Submitted.UPLOADED
    assert 'file is on the printer' in failure_report.state_line(report)


@pytest.mark.parametrize('tail,stage', [
    ('The file could not be uploaded to the printer, so nothing started.\nDetail: boom', 'upload'),
    ('UPLOAD FAILED: connection reset', 'upload'),
    ('UNCERTAIN: print-now timed out. The plate may already have uploaded.', 'launch'),
    ('print-now exited 1 with no output', 'launch'),
])
def test_unconfirmed_start_is_submitted_with_unknown_status(tail, stage):
    report = prepared_intake._start_report(tail)
    assert report.stage == stage and report.submitted is failure_report.Submitted.UNKNOWN
    post = prepared_intake.plain_start_failure(tail)
    assert f'Stage: {stage}.' in post and 'may already have the file' in post
    assert 'did not confirm whether the print started' in post and 'do not press Start again' in post


def test_confirmed_refusal_post_names_the_launch_stage():
    post = prepared_intake.plain_start_failure(
        'REFUSED: internal probe 7 failed. Nothing uploaded or started.')
    assert post.startswith("Didn't start:") and post.endswith('Stage: launch.')


# --- the archived failure folder -------------------------------------------------------------

def test_failure_folders_list_newest_first(tmp_path):
    root = tmp_path / 'state' / 'slice-failures'
    older, newer = root / 'old', root / 'new'
    older.mkdir(parents=True)
    newer.mkdir()
    import os
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))
    rows = failure_report.failure_folders(tmp_path)
    assert [row['name'] for row in rows] == ['new', 'old']
    assert failure_report.failure_folders(tmp_path / 'nothing') == []
