"""Status-only progress has no Stop view; final pre-start work keeps cancellation."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace as S

from print_handy.core import prepared_intake
from tests.hermes.test_print_next import Interaction, load_plugin


def _fake_discord(monkeypatch):
    class Button:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class View:
        def __init__(self, timeout=None):
            self.items = []

        def add_item(self, item):
            self.items.append(item)
    monkeypatch.setitem(sys.modules, 'discord', S(ui=S(View=View, Button=Button),
                                                  ButtonStyle=S(success=3, danger=4, primary=1)))


def _install(monkeypatch):
    _fake_discord(monkeypatch)
    sent, handlers = [], {}

    class Msg:
        def __init__(self, text, view):
            self.id = str(len(sent) + 1)
            self.text, self.view, self.edits = text, view, []

        async def edit(self, **kw):
            self.edits.append(kw)
            self.text = kw.get('content', self.text)
            self.view = kw.get('view', self.view)

    class Channel:
        async def send(self, text, view=None):
            msg = Msg(text, view)
            sent.append(msg)
            return msg
    bot = S(add_listener=lambda fn, name: handlers.update({name: fn}), get_channel=lambda cid: Channel())
    monkeypatch.setattr(prepared_intake, 'STOP_SENDER', None)
    plugin = load_plugin()
    plugin.alert_button_handlers(bot, S())
    return sent, handlers['on_interaction'], plugin


class _NoClarify:
    def __init__(self):
        self.status = []

    async def send_clarify(self, **kw):
        raise AssertionError('status-only progress must not create an interactive prompt')

    async def send(self, **kw):
        self.status.append(kw['content'])
        return S(success=True)


def test_status_step_sends_plain_progress_without_a_stop_view(monkeypatch):
    sent, _, _ = _install(monkeypatch)
    adapter = _NoClarify()

    async def work():
        await asyncio.sleep(0.01)
        return 'fits'

    async def run():
        return await asyncio.wait_for(prepared_intake._with_stop(
            adapter, S(chat_id='123', thread_id=None), 'k-view', None,
            'Checking whether it fits the bed. Press Stop to cancel.', work()), 5)

    assert asyncio.run(run()) == 'fits'
    assert adapter.status == ['Checking whether it fits the bed.']
    assert sent == []


def test_refused_start_retires_stop_and_installs_retry_on_same_prompt(monkeypatch):
    sent, _, plugin = _install(monkeypatch)
    source = S(chat_id='123', thread_id=None)
    receipt_id = 'a' * 32
    context = {'target': {'gcode': '/safe/job.gcode', 'gcode_sha256': 'sha'},
               'receipt': {'intake_id': receipt_id}}
    detail = ('REFUSED: idle guard failed: native rightTargetTemp == 0 (observed 210.0); '
              'native platTargetTemp == 0 (observed 60.0). Nothing uploaded or started.')

    async def refused():
        return False, detail

    async def run():
        return await prepared_intake._with_stop(
            _NoClarify(), source, 'k-refusal', None,
            'Uploading to the printer. Press Stop to request cancellation.', refused(),
            allow_stop=True, refusal_context=context)

    assert asyncio.run(run()) == (False, detail)
    assert len(sent) == 1
    assert len(sent[0].edits) == 1
    assert "Didn't start:" in sent[0].text and "rightTargetTemp" not in sent[0].text
    buttons = sent[0].view.items
    assert [button.label for button in buttons] == ['Retry']
    assert buttons[0].custom_id == f'pbretry:{receipt_id}'
    assert context['start_prompt_message_id'] == sent[0].id
    assert context['prompt_handled'] is True


def test_retry_click_reuses_saved_target_and_calls_guarded_start(monkeypatch, tmp_path):
    sent, on_interaction, plugin = _install(monkeypatch)
    from print_handy.runtime import print_next
    receipt_id = 'b' * 32
    receipt_root = tmp_path / 'model-link-intakes'
    receipt_root.mkdir()
    target = {'plan_id': 'plan-7', 'part_id': 'plate2', 'copy': 1,
              'gcode': str(tmp_path / 'plate_2.gcode'), 'gcode_sha256': 'digest', 'reprint': False}
    Path(target['gcode']).write_text('; exact prepared bytes\n')
    receipt = {'intake_id': receipt_id, 'status': 'START_REFUSED', 'uploaded': False,
               'started': False, 'job_json': str(tmp_path / 'JOB.json'),
               'start_refusal_detail': 'REFUSED: printer was busy. Nothing uploaded or started.',
               'start_prompt_message_id': '77', 'retry_target': target}
    (receipt_root / f'{receipt_id}.json').write_text(__import__('json').dumps(receipt))
    monkeypatch.setattr(plugin, '_state_dir', lambda: tmp_path)
    called = []

    async def guarded_start(actual):
        called.append(actual)
        return False, 'REFUSED: printer is still busy. Nothing uploaded or started.'

    monkeypatch.setattr(print_next, 'start', guarded_start)
    click = Interaction(f'pbretry:{receipt_id}')
    click.message = S(id='77', edits=[])
    async def edit(**kwargs):
        click.message.edits.append(kwargs)
        click.message.content = kwargs.get('content')
        click.message.view = kwargs.get('view')
    click.message.edit = edit
    asyncio.run(on_interaction(click))

    assert called == [target]
    assert click.message.content.startswith("Didn't start:")
    assert click.message.view is not None
    saved = __import__('json').loads((receipt_root / f'{receipt_id}.json').read_text())
    assert saved['status'] == 'START_REFUSED'
    assert 'still busy' in saved['start_refusal_detail']


def test_pressing_plugin_stop_cancels_the_final_start_step(monkeypatch):
    sent, on_interaction, _ = _install(monkeypatch)

    async def slow():
        await asyncio.sleep(30)
        return 'never'

    async def run():
        step = asyncio.ensure_future(prepared_intake._with_stop(
            _NoClarify(), S(chat_id='123', thread_id=None), 'k-press', None,
            'Uploading to the printer. Press Stop to cancel before it starts.', slow(), allow_stop=True))
        while not sent:
            await asyncio.sleep(0.01)
        press = Interaction(sent[0].view.items[0].custom_id)
        await on_interaction(press)
        try:
            await asyncio.wait_for(step, 5)
        except prepared_intake.Stopped:
            return 'stopped', press.out
        return 'not stopped', press.out

    outcome, out = asyncio.run(run())
    assert outcome == 'stopped'
    assert ('followup', 'Stopping.') in out
