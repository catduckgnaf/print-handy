"""Status voice wiring (#1326): one quip after the factual text a stage already sends.

Each test drives the real intake path (or the narrowest seam the suite
already uses for that message) with a seeded quip, and checks three things:
the quip follows the factual text, the factual text is unchanged, and an
empty quip leaves the message byte-identical. tests/conftest.py silences the
voice everywhere else, so this file seeds it explicitly.
"""
import ast
import asyncio
import inspect
from types import SimpleNamespace as S

import pytest

from print_handy.core import model_job, prepared_intake, status_voice
from .test_prepared_intake import Adapter, env, run_flow, run_preview  # noqa: F401  (pytest fixture)
from .test_start_progress import ACCEPTED, UPLOADED, log as log_stage, run as run_start

LARGE = 'This is a large model (90 MB of mesh). Checking it takes about 2 minutes, so it is not stuck.'


def seed(monkeypatch, speak=True):
    """Replace the selector: '[<stage> quip]' when speaking, '' (a failed quip) when not."""
    calls = []

    def fake(stage, **kwargs):
        calls.append((stage, kwargs))
        return f'[{stage} quip]' if speak else ''
    monkeypatch.setattr(status_voice, 'quip', fake)
    return calls


def record_progress(monkeypatch):
    """Every progress text the intake hands _with_stop, and every FitStatus phase it runs."""
    texts, phases = [], []
    with_stop = prepared_intake._with_stop

    async def recorded(adapter, source, key, metadata, text, work, **kw):
        texts.append(text)
        return await with_stop(adapter, source, key, metadata, text, work, **kw)
    monkeypatch.setattr(prepared_intake, '_with_stop', recorded)
    run = prepared_intake.FitStatus.run

    async def recorded_run(self, phase, work, first_text=None, voice=None):
        phases.append((phase, first_text, voice() if voice else None))
        return await run(self, phase, work, first_text=first_text, voice=voice)
    monkeypatch.setattr(prepared_intake.FitStatus, 'run', recorded_run)
    return texts, phases


# link_intake: the "That looks like ..." preview after the link lookup.

PREVIEW = ('That looks like Ghost Stand, A sturdy stand. I will use the selected file: ghost. '
           'The model page gives no time or filament estimate; I will report both after slicing.')


@pytest.mark.parametrize('speak', [True, False])
def test_link_preview_carries_one_quip_after_the_facts(env, monkeypatch, speak):
    calls = seed(monkeypatch, speak)
    log, _ = run_preview(env, monkeypatch, 'A sturdy stand.', {'name': 'ghost.3mf', 'instance_id': 1})
    assert log[0] == ('send', PREVIEW + (' [link_intake quip]' if speak else ''))
    stage, kwargs = next(call for call in calls if call[0] == 'link_intake')
    assert kwargs['identity'] == 'discord:user:u' and kwargs['event_key'].endswith(':link_intake')


# model_audit, color_detection, plate_capacity and slice on one ordinary intake.

@pytest.mark.parametrize('speak', [True, False])
def test_intake_progress_lines_carry_their_stage_quip(env, monkeypatch, speak):
    seed(monkeypatch, speak)
    monkeypatch.setattr(prepared_intake, 'large_model_notice', lambda path: LARGE)
    texts, phases = record_progress(monkeypatch)
    adapter, _result, _receipt = run_flow(env, [1, 0, 0])

    def said(text, stage):
        if not speak:
            return text
        for tail in status_voice.ACTION_TAILS:
            if text.endswith(tail):
                return f'{text[:-len(tail)]} [{stage} quip]{tail}'
        return f'{text} [{stage} quip]'
    assert said(LARGE, 'model_audit') in texts
    assert said('Checking the model. Press Stop to cancel.', 'color_detection') in texts
    assert phases[0][1] == said('Checking fit and calculating plate capacity.', 'plate_capacity')
    wait = said('Waiting for the background slice to finish. Press Stop to cancel.', 'slice')
    assert wait in texts and wait in adapter.log  # the Stop status prompt the operator sees


def test_progress_quip_keys_are_stable_per_intake_and_stage(env, monkeypatch):
    calls = seed(monkeypatch)
    monkeypatch.setattr(prepared_intake, 'large_model_notice', lambda path: LARGE)
    run_flow(env, [1, 0, 0])
    keys = {(stage, kwargs['event_key']) for stage, kwargs in calls}
    iid = next(key for stage, key in keys if stage == 'slice').rsplit(':', 1)[0]
    assert keys == {(stage, f'{iid}:{stage}') for stage, _key in keys}  # one key per stage, replay-stable
    assert {'model_audit', 'color_detection', 'plate_capacity', 'slice'} <= {stage for stage, _key in keys}


# FitStatus phases: bed_fit, plate_arrangement and split share this seam.

class _Editing:
    def __init__(self):
        self.sent, self.edits = [], []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return S(success=True, message_id=str(len(self.sent)))

    async def edit_message(self, chat_id, message_id, content):
        self.edits.append(content)
        return S(success=True, message_id=message_id)


def _fit_texts(monkeypatch, voice):
    monkeypatch.setattr(prepared_intake, 'FIT_HEARTBEAT_S', 0.01)
    monkeypatch.setattr(prepared_intake, 'FIT_QUIET_S', 0)
    adapter = _Editing()
    status = prepared_intake.FitStatus(adapter, S(chat_id='c1', thread_id=None, user_id='u'), {})

    async def work():
        await asyncio.sleep(0.05)
        return 1
    asyncio.run(status.run('trying rotations and smaller sizes', work(), voice=voice))
    return adapter.sent + adapter.edits


@pytest.mark.parametrize('stage', ['bed_fit', 'plate_arrangement', 'split'])
def test_fit_phase_status_carries_its_quip_on_every_refresh(monkeypatch, stage):
    calls = seed(monkeypatch)
    source = S(chat_id='c1', thread_id=None, user_id='u', platform=None)
    texts = _fit_texts(monkeypatch, prepared_intake._voice(stage, source, 'iid1'))
    plain = [t.removesuffix(f' [{stage} quip]') for t in texts]
    assert texts and all(t.endswith(f' [{stage} quip]') for t in texts)
    assert plain[0].startswith('Checking the fit: trying rotations and smaller sizes (')
    assert [c[1]['event_key'] for c in calls] == [f'iid1:{stage}']  # chosen once for the phase


def test_fit_phase_without_a_quip_is_byte_identical(monkeypatch):
    seed(monkeypatch, speak=False)
    source = S(chat_id='c1', thread_id=None, user_id='u', platform=None)
    voiced = _fit_texts(monkeypatch, prepared_intake._voice('bed_fit', source, 'iid1'))
    plain = _fit_texts(monkeypatch, None)
    assert voiced[0] == plain[0] == 'Checking the fit: trying rotations and smaller sizes (0 s).'
    assert all('quip' not in t for t in voiced)


def test_each_fit_phase_call_site_names_its_stage():
    """The intake's own FitStatus.run calls pass the matching stage (static, like test_slice_warnings)."""
    tree = ast.parse(inspect.getsource(prepared_intake))
    stages = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and getattr(node.func, 'attr', None) == 'run' and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            voice = next((k.value for k in node.keywords if k.arg == 'voice'), None)
            if isinstance(voice, ast.Call) and getattr(voice.func, 'id', None) == '_voice':
                stages.setdefault(node.args[0].value, set()).add(voice.args[0].value)
    assert stages == {
        'trying the parts rearranged on one plate at 100%': {'plate_arrangement'},
        'trying rotations and smaller sizes': {'bed_fit'},
        'trying a split onto separate plates at 100%': {'split'},
    }


def test_slice_and_support_progress_call_sites_name_their_stage():
    """Every Slicing/Retrying/Waiting prompt is voiced as slice, every with-supports one as supports."""
    tree = ast.parse(inspect.getsource(prepared_intake))
    seen = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, 'id', None) == '_voiced':
            text, stage = node.args[0], node.args[1]
            if isinstance(text, ast.Constant) and isinstance(stage, ast.Constant):
                seen.setdefault(stage.value, set()).add(text.value)
    assert seen['slice'] == {'Slicing. Press Stop to cancel.', 'Retrying the slice. Press Stop to cancel.',
                             'Waiting for the background slice to finish. Press Stop to cancel.'}
    assert seen['supports'] == {'Slicing with supports. Press Stop to cancel.',
                                'Re-slicing with supports. Press Stop to cancel.'}
    # Never on a filament-warning re-slice or an AI fix re-slice: those follow a decision.
    voiced = {text for texts in seen.values() for text in texts}
    assert not any('filament warnings' in t or 'AI fix' in t for t in voiced)


# print_start: the first START_PROGRESS note ("Uploaded to the printer. Sending
# the start now") reports the start being sent, not the upload, which is over.
# Upload lines ("This is a transfer, not a launch") contradicted it. The second
# note carries no quip, so one start never repeats its line.

@pytest.fixture
def fast_start(monkeypatch):
    monkeypatch.setattr(prepared_intake, 'START_PROGRESS_POLL_S', 0.01)
    monkeypatch.setattr(prepared_intake, 'START_PROGRESS', tuple(
        (stage, 0.05, text) for stage, _grace, text in prepared_intake.START_PROGRESS))


class _Sent:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, metadata=None):
        self.sent.append(content)
        return S(success=True)


@pytest.mark.parametrize('speak', [True, False])
def test_start_notes_carry_one_print_start_quip(tmp_path, monkeypatch, fast_start, speak):
    calls = seed(monkeypatch, speak)
    adapter = _Sent()

    async def script():
        log_stage(tmp_path, 'upload_verified')
        await asyncio.sleep(0.2)
        log_stage(tmp_path, 'start_submitted')
        await asyncio.sleep(0.2)
        return True, 'preparing'
    run_start(tmp_path, adapter, script)
    if speak:
        assert adapter.sent == [UPLOADED + ' [print_start quip]', ACCEPTED]
    else:
        assert adapter.sent == [UPLOADED, ACCEPTED]
    assert [stage for stage, _kwargs in calls] == ['print_start']
    assert [kwargs['event_key'] for _stage, kwargs in calls] == [f'{tmp_path.name}:print_start']
