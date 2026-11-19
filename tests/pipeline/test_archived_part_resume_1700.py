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
