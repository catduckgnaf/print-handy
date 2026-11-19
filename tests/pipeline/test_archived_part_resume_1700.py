import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy.bot.handlers import model_link
from print_handy.core import model_job as mj, prepared_intake, printer_state
from . import test_multi_tool_intake as harness
from tests.core.test_plate_usage import write_source

pytest_plugins = ('tests.pipeline.test_multi_tool_intake',)


def test_natural_part_two_reopens_checkpoint_and_slices_only_that_plate(flow_env, monkeypatch, tmp_path):
    source_file = write_source(tmp_path / 'authored.3mf', [('Body', 1, None), ('Lid', 2, 3)], [[0], [1]],
                               project_extra={'filament_colour': ['#FFFFFF', '#000000', '#FF0000', '#0000FF'],
                                              'filament_type': ['PLA'] * 4})
    flow_env.holder['bytes'] = source_file.read_bytes()
    from print_handy import settings
    printer_id = settings.default_printer_id(settings.current())

    def event(message_id, text):
        return S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u',
                          platform=S(value='discord'), printer_id=printer_id),
                 message_id=message_id, text=text, metadata={})

    class Deferred(harness.FlowAdapter):
        async def send_clarify(self, **kwargs):
            raise prepared_intake.IntakeCancelled('interrupt before the first decision')

    asyncio.run(prepared_intake.prepare_link(
        event('first', 'https://www.printables.com/model/1-ghost'), Deferred([]),
        harness.tool_state(), flow_env.root, timeout=1, printer_id=printer_id))
    first_job, = mj.ARCHIVE.glob('*/JOB.json')
    original = json.loads(first_job.read_text())
    checkpoint = original['source_checkpoint']
    assert checkpoint['launch_approval'] is None
    assert not flow_env.sliced

    def no_download(*args, **kwargs):
        pytest.fail('The resumed intake must use the local archive')

    monkeypatch.setattr(mj, 'printables_download', no_download)
    snapshot = []
    original_slice = mj.slice_job

    def capture_slice(job_path, **kwargs):
        snapshot.append(json.loads(Path(job_path).read_text()))
        return original_slice(job_path, **kwargs)

    monkeypatch.setattr(mj, 'slice_job', capture_slice)
    adapter = harness.FlowAdapter(['Resume plate', 'Change colors', 'Black', 'Red', 'Slice and Download'])
    gateway = S(_is_user_authorized_for_source=lambda source: True,
                _session_key_for_source=lambda source: 'resume-session')
    monkeypatch.setattr(model_link, '_adapter_for', lambda *_args: adapter)
    monkeypatch.setattr(model_link, '_state_dir', lambda: flow_env.root)
    monkeypatch.setattr(model_link, '_undecided_printer_ids', lambda *_args: [])
    monkeypatch.setattr(printer_state, 'load', lambda *_args, **_kwargs: harness.tool_state())
    asyncio.run(model_link.model_link_dispatch(event('second', 'print part 2'), gateway))
    assert snapshot, (adapter.questions, adapter.sent)
    assert [job['selected_plates'] for job in snapshot] == [['2']]
    resumed, = snapshot
    assert resumed['color_manifest']['source_sha256'] == checkpoint['source_sha256']
    assert {v['slot'] for v in resumed['color_decisions']['choices'].values()} == {2, 3}
    assert 'Resume' in adapter.questions[0]['question']
    receipts = [json.loads(path.read_text()) for path in (flow_env.root / 'model-link-intakes').glob('*.json')]
    receipt, = receipts
    assert receipt['sliced'] is True
    assert receipt['started'] is False
    assert receipt['uploaded'] is False


def test_single_candidate_is_revalidated_before_resume(flow_env, monkeypatch, tmp_path):
    from print_handy.core import projects

    printer_id = 'printer-1'
    archive = tmp_path / 'archive'
    folder = archive / 'job-1'
    folder.mkdir(parents=True)
    source = folder / 'source.3mf'
    source.write_bytes(b'original source')
    checkpoint = {
        'schema': projects.SOURCE_CHECKPOINT_SCHEMA,
        'status': 'available',
        'request_scope': {'platform': 'discord', 'requester_id': 'u',
                          'channel_id': 'c1', 'printer_id': printer_id},
        'source_path': str(source),
        'source_sha256': mj.file_sha256(source),
        'title': 'Only archived project',
        'authored_plates': [{'id': '1', 'name': 'Only plate'}],
    }
    job_path = folder / 'JOB.json'
    job_path.write_text(json.dumps({'downloaded_file': str(source),
                                    'source_checkpoint': checkpoint}))
    original_discover = projects.discover_source_checkpoints

    def discover_then_mutate(**kwargs):
        rows = original_discover(**kwargs)
        source.write_bytes(b'mutated source')
        return rows

    monkeypatch.setattr(projects, 'discover_source_checkpoints', discover_then_mutate)
    monkeypatch.setattr(model_link, '_adapter_for', lambda *_args: harness.FlowAdapter([]))
    monkeypatch.setattr(model_link, '_channel_printer_id', lambda *_args: printer_id)
    monkeypatch.setattr(model_link, '_routed_printer_id', lambda *_args: printer_id)
    monkeypatch.setattr(model_link, '_state_dir', lambda: flow_env.root)
    monkeypatch.setattr(model_link, '_undecided_printer_ids', lambda *_args: [])
    monkeypatch.setattr(printer_state, 'load', lambda *_args, **_kwargs: harness.tool_state())

    prepared = []
    async def capture_prepare(*_args, **kwargs):
        prepared.append(kwargs)
        return {'action': 'skip', 'reason': 'unexpected resume'}
    monkeypatch.setattr(prepared_intake, 'prepare_link', capture_prepare)

    gateway = S(_is_user_authorized_for_source=lambda _source: True,
                _session_key_for_source=lambda _source: 'resume-session')
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u',
                       platform=S(value='discord'), printer_id=printer_id),
              message_id='resume-single', text='/resume-model', metadata={})
    result = asyncio.run(model_link.model_link_dispatch(event, gateway))

    assert result['action'] == 'skip'
    assert not prepared


@pytest.mark.parametrize('utterance,outcome', [
    ('print part 2', 'select'), ('/resume-model', 'select'),
    ('print part 2', 'cancel'), ('print part 2', 'changed'),
    ('print part 2', 'no adapter'), ('print part 99', 'no match'),
])
def test_ambiguous_archived_projects_need_project_and_fresh_confirmation(
        flow_env, monkeypatch, tmp_path, utterance, outcome):
    from print_handy.core import projects

    printer_id = 'printer-1'
    candidates = []
    for index, title in enumerate(('Alpha assembly', 'Beta assembly'), start=1):
        source = tmp_path / f'project-{index}.3mf'
        source.write_bytes(f'project-{index}'.encode())
        checkpoint = {
            'schema': projects.SOURCE_CHECKPOINT_SCHEMA,
            'status': 'available',
            'request_scope': {'platform': 'discord', 'requester_id': 'u',
                              'channel_id': 'c1', 'printer_id': printer_id},
            'source_path': str(source),
            'source_sha256': mj.file_sha256(source),
            'title': title,
            'authored_plates': [{'id': '1', 'name': 'First'}, {'id': '2', 'name': 'Second'}],
            'authored_parts': [{'key': 'part-1', 'name': 'Body'},
                               {'key': 'part-2', 'name': 'Lid'}],
            'color_manifest': {'plate_usage': {'2': {'part_keys': ['part-2']}}},
        }
        candidates.append({'job_json': str(tmp_path / f'job-{index}.json'),
                           'job': {'selected_source': {'path': str(source)},
                                   'downloaded_file': str(source),
                                   'source_checkpoint': checkpoint},
                           'checkpoint': checkpoint})

    monkeypatch.setattr(model_link, '_adapter_for', lambda *_args: harness.FlowAdapter([]))
    monkeypatch.setattr(model_link, '_channel_printer_id', lambda *_args: printer_id)
    monkeypatch.setattr(model_link, '_routed_printer_id', lambda *_args: printer_id)
    monkeypatch.setattr(model_link, '_state_dir', lambda: flow_env.root)
    monkeypatch.setattr(model_link, '_undecided_printer_ids', lambda *_args: [])
    monkeypatch.setattr(printer_state, 'load', lambda *_args, **_kwargs: harness.tool_state())
    monkeypatch.setattr(projects, 'discover_source_checkpoints', lambda **_kwargs: candidates)
    monkeypatch.setattr(projects, 'load_source_checkpoint', lambda job_json, **_kwargs:
                        next((row for row in candidates if row['job_json'] == str(job_json)), None))

    questions = []
    async def confirm(_adapter, _source, _metadata, _prefix, question, choices, _timeout,
                      live_key=None, approval=False):
        questions.append((question, choices, live_key, approval))
        if outcome == 'cancel':
            return 'Cancel'
        if outcome == 'changed':
            monkeypatch.setattr(projects, 'load_source_checkpoint', lambda job_json, **_kwargs:
                                next((row for row in candidates[:1]
                                      if row['job_json'] == str(job_json)), None))
        return next(choice for choice in choices if choice.startswith('Beta assembly · plate 2'))

    monkeypatch.setattr(model_link, '_confirm', confirm)
    prepared = []
    async def capture_prepare(event, adapter, state, root, **kwargs):
        prepared.append(kwargs['reuse'])
        return {'action': 'skip', 'reason': 'captured'}

    monkeypatch.setattr(prepared_intake, 'prepare_link', capture_prepare)
    gateway = S(_is_user_authorized_for_source=lambda _source: True,
                _session_key_for_source=lambda _source: 'resume-session')
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u',
                       platform=S(value='discord'), printer_id=printer_id),
              message_id='resume', text=utterance, metadata={})
    if outcome == 'no adapter':
        candidates[0]['checkpoint']['color_manifest']['plate_usage'].clear()
        monkeypatch.setattr(model_link, '_adapter_for', lambda *_args: None)
    result = asyncio.run(model_link.model_link_dispatch(event, gateway))

    if outcome != 'select':
        assert result['action'] == 'skip'
        assert not prepared
        assert len(questions) == (1 if outcome in {'cancel', 'changed'} else 0)
        if outcome == 'no adapter':
            assert result['reason'] == 'archived project selection requires a chat adapter'
        return
    assert result['reason'] == 'captured'
    assert len(questions) == 1
    assert questions[0][0] == 'Which archived project and plate do you mean?'
    expected_choices = 3 if utterance == 'print part 2' else 5
    assert len(questions[0][1]) == expected_choices
    assert len(prepared) == 1
    resume = prepared[0]['source_checkpoint_resume']
    assert resume['plate_id'] == '2'
    assert resume['source_sha256'] == candidates[1]['checkpoint']['source_sha256']
    assert prepared[0].get('start_approval') is None
