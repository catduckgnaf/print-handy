"""#1204 item 2: collecting one project's files across several chat messages.

The acceptance shapes from #1008/#1204: a collection ignores another
user's file, another thread, another printer and unrelated uploads; equal
file names are kept; duplicate bytes never become a second required part;
the collection survives a restart. ``handle`` is driven with a scripted
``ask`` so the flow is the one every transport gets through the shared
choice mechanism (Discord buttons; numbered choices on Telegram, Matrix
and web chat).
"""
import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy.core import upload_collection as uc


@pytest.fixture
def store(tmp_path, monkeypatch):
    state = tmp_path / 'state' / uc.STATE_NAME
    monkeypatch.setattr(uc, '_state_path', lambda: state)
    return state


def src(user='u1', chat='c1', thread=None, platform='discord'):
    return S(platform=S(value=platform), chat_id=chat, thread_id=thread, user_id=user, profile='3dprinting')


def upload(tmp_path, name, data, tag):
    path = tmp_path / 'cache' / f"doc_{tag:012x}_{name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class Chat:
    def __init__(self, answers=()):
        self.answers = list(answers)
        self.questions = []
        self.sent = []
        self.intakes = []

    async def ask(self, question, choices):
        self.questions.append((question, list(choices)))
        label = self.answers.pop(0)
        return list(choices).index(label)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)

    async def run_intake(self, paths, row):
        self.intakes.append(([Path(p) for p in paths], row))
        return {'action': 'skip', 'reason': 'intake ran'}


def run(chat, source, text='', files=(), printer='ad5x', message_id='m'):
    event = S(source=source, text=text, message_id=message_id)
    return asyncio.run(uc.handle(event, chat, printer_id=printer, files=list(files),
                                 run_intake=chat.run_intake, ask=chat.ask))


def test_intent_phrases():
    assert uc.intent('add to project') == 'start'
    assert uc.intent('Add these to the project please') == 'start'
    assert uc.intent('start a project') == 'start'
    assert uc.intent('done adding') == 'done' and uc.intent('Done adding.') == 'done'
    assert uc.intent('cancel project') == 'cancel'
    for ordinary in ('', 'print this', 'here is the lid', 'is this project done?', 'adding a brim',
                     'done', 'cancel'):
        assert uc.intent(ordinary) is None, ordinary


def test_an_ordinary_upload_never_touches_a_collection(store, tmp_path):
    chat = Chat()
    a = upload(tmp_path, 'lid.stl', b'lid', 1)
    assert run(chat, src(), files=[a]) is None  # the normal single-upload intake takes it
    assert not store.exists() and chat.questions == [] and chat.sent == []


def test_collection_over_three_messages_runs_one_intake(store, tmp_path):
    chat = Chat(['Done adding'])
    a = upload(tmp_path, 'arm_left.stl', b'left', 1)
    b = upload(tmp_path, 'arm_right.stl', b'right', 2)
    c = upload(tmp_path, 'tray.stl', b'tray', 3)
    # Start: a status question with Done adding / Cancel project, left unanswered.
    async def keep_open(question, choices):  # the person does not press anything yet
        chat.questions.append((question, list(choices)))
        from print_handy.core.link_intake import PromptExpired
        raise PromptExpired('expired')
    first = S(source=src(), text='add to project', message_id='m1')
    result = asyncio.run(uc.handle(first, chat, printer_id='ad5x', files=[a], run_intake=chat.run_intake,
                                   ask=keep_open))
    assert result['reason'] == 'upload collection open'
    assert chat.questions[-1][1] == [uc.DONE_LABEL, uc.CANCEL_PROJECT_LABEL]
    # A later upload asks Add to project / Print it on its own; the text "add to project" skips the ask.
    chat.answers = ['Add to project', 'Done adding']
    run(chat, src(), files=[b], message_id='m2')
    assert chat.questions[1][1] == [uc.ADD_LABEL, uc.SOLO_LABEL, uc.NOT_NOW_LABEL]
    assert len(chat.intakes) == 1
    paths, row = chat.intakes[0]
    assert [p.name for p in paths] == ['arm_left.stl', 'arm_right.stl']
    assert [f['message_id'] for f in row['files']] == ['m1', 'm2']
    assert uc.current(uc.scope_key(src(), 'ad5x')) is None  # taken once
    # c was never part of it, and a second Done finds nothing.
    assert run(Chat(), src(), text='done adding')['reason'] == 'no upload collection to finish'
    assert all(p.is_file() for p in paths) and c.is_file()


def test_collection_ignores_other_user_thread_printer_and_unrelated_uploads(store, tmp_path):
    chat = Chat(['Cancel project'])
    a = upload(tmp_path, 'base.stl', b'base', 1)
    # Open (then keep) a collection: answer the status question by expiring it.
    from print_handy.core.link_intake import PromptExpired

    async def expire(question, choices):
        raise PromptExpired('x')
    event = S(source=src(), text='add to project', message_id='m1')
    asyncio.run(uc.handle(event, chat, printer_id='ad5x', files=[a], run_intake=chat.run_intake, ask=expire))
    scope = uc.scope_key(src(), 'ad5x')
    assert [f['name'] for f in uc.current(scope)['files']] == ['base.stl']

    other = upload(tmp_path, 'other.stl', b'other', 2)
    quiet = Chat()
    assert run(quiet, src(user='u2'), files=[other]) is None            # another user
    assert run(quiet, src(thread='t9'), files=[other]) is None          # another thread
    assert run(quiet, src(chat='c2'), files=[other]) is None            # another channel
    assert run(quiet, src(), files=[other], printer='k1') is None       # another printer
    assert run(quiet, src(platform='telegram'), files=[other]) is None  # another transport
    assert run(quiet, src(user='u2'), text='done adding')['reason'] == 'no upload collection to finish'
    assert quiet.questions == [] and quiet.intakes == []
    # The owner's unrelated upload: "Print it on its own" leaves it to the normal intake.
    solo = Chat(['Print it on its own'])
    assert run(solo, src(), files=[other]) is None
    # ...and Cancel adds nothing and prints nothing.
    no = Chat(['Cancel'])
    assert run(no, src(), files=[other])['reason'] == 'upload not added to the collection'
    assert [f['name'] for f in uc.current(scope)['files']] == ['base.stl']


def test_equal_names_are_kept_and_duplicate_bytes_are_not_doubled(store, tmp_path):
    from print_handy.core.link_intake import PromptExpired

    async def expire(question, choices):
        chat.questions.append((question, list(choices)))
        raise PromptExpired('x')
    chat = Chat()
    one = upload(tmp_path, 'arm.stl', b'left arm', 1)
    two = upload(tmp_path, 'arm.stl', b'right arm', 2)
    same = upload(tmp_path, 'arm_copy.stl', b'left arm', 3)
    for n, path in enumerate([one, two, same], 1):
        event = S(source=src(), text='add to project', message_id=f'm{n}')
        asyncio.run(uc.handle(event, chat, printer_id=None, files=[path], run_intake=chat.run_intake,
                              ask=expire))
    files = uc.current(uc.scope_key(src()))['files']
    assert [f['name'] for f in files] == ['arm.stl', 'arm.stl']
    assert len({f['path'] for f in files}) == 2
    assert {Path(f['path']).read_bytes() for f in files} == {b'left arm', b'right arm'}
    assert 'exactly the same bytes as arm.stl' in chat.questions[-1][0]
    assert 'not added as a second part' in chat.questions[-1][0]


def test_collection_survives_a_restart(store, tmp_path):
    from print_handy.core.link_intake import PromptExpired

    async def expire(question, choices):
        raise PromptExpired('x')
    a = upload(tmp_path, 'left.stl', b'l', 1)
    b = upload(tmp_path, 'right.stl', b'r', 2)
    chat = Chat()
    for n, path in enumerate([a, b], 1):
        event = S(source=src(), text='add these to the project', message_id=f'm{n}')
        asyncio.run(uc.handle(event, chat, printer_id='ad5x', files=[path], run_intake=chat.run_intake,
                              ask=expire))
    # A restart: the module state is gone, the cached uploads are pruned, the store file remains.
    a.unlink()
    b.unlink()
    fresh = importlib.reload(uc)
    try:
        fresh._state_path = lambda: store
        after = Chat()
        event = S(source=src(), text='done adding', message_id='m3')
        result = asyncio.run(fresh.handle(event, after, printer_id='ad5x', files=[],
                                          run_intake=after.run_intake, ask=after.ask))
        assert result == {'action': 'skip', 'reason': 'intake ran'}
        paths, row = after.intakes[0]
        assert [p.name for p in paths] == ['left.stl', 'right.stl'] and all(p.is_file() for p in paths)
    finally:
        importlib.reload(uc)


def test_cancel_discards_the_collection_and_its_files(store, tmp_path):
    from print_handy.core.link_intake import PromptExpired

    async def expire(question, choices):
        raise PromptExpired('x')
    a = upload(tmp_path, 'left.stl', b'l', 1)
    chat = Chat()
    asyncio.run(uc.handle(S(source=src(), text='add to project', message_id='m1'), chat, printer_id=None,
                          files=[a], run_intake=chat.run_intake, ask=expire))
    kept = Path(uc.current(uc.scope_key(src()))['files'][0]['path'])
    assert kept.is_file()
    assert run(chat, src(), text='cancel project', printer=None)['reason'] == 'upload collection cancelled'
    assert not kept.exists() and uc.current(uc.scope_key(src())) is None
    assert chat.intakes == []


def test_expired_collections_are_dropped(store, tmp_path):
    a = upload(tmp_path, 'left.stl', b'l', 1)
    scope = uc.scope_key(src())
    uc.add(scope, [a], now=1000.0)
    assert uc.current(scope, now=1000.0 + uc.TTL_S - 1) is not None
    assert uc.current(scope, now=1000.0 + uc.TTL_S + 1) is None


def test_a_zip_is_never_collected(store, tmp_path):
    chat = Chat()
    z = upload(tmp_path, 'rack.zip', b'PK', 1)
    event = S(source=src(), text='add to project', message_id='m1')
    result = asyncio.run(uc.handle(event, chat, printer_id=None, files=[], run_intake=chat.run_intake,
                                   ask=chat.ask, archives=[z]))
    assert result['reason'] == 'archive not collected' and 'not collected' in chat.sent[0]
    assert uc.current(uc.scope_key(src())) is None and chat.questions == []
