"""A model whose files are all different parts prints them all (catduck).

By default the intake says which parts it is printing and goes on, with
no "Print all N / Choose files" question. ``[discord] ask_which_parts``
brings the question back for anyone who wants to pick a subset. Versions
of one part are a real choice and are always asked.
"""
import asyncio
from types import SimpleNamespace as S

import pytest

from print_handy import settings
from print_handy.core import prepared_intake
from print_handy.core.prepared_intake import _choose_files
from print_handy.runtime import _config

PARTS = ('shell.stl', 'plunger.stl', 'spring.stl')


class Adapter:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return S(success=True)


@pytest.fixture
def asked(monkeypatch):
    questions = []

    async def ask(_adapter, _source, _key, question, choices, _timeout, **_kw):
        questions.append({'question': question, 'choices': choices})
        return 0

    monkeypatch.setattr(prepared_intake, '_ask', ask)
    return questions


def _run(rows, ask_which_parts=None):
    adapter = Adapter()
    # Restore the session's own override (tests/conftest.py installs one for
    # every test); resetting it to None would leak into every later test.
    saved = _config._override
    data = _config.cfg()
    if ask_which_parts is not None:
        data = {**data, 'discord': {**(data.get('discord') or {}), 'ask_which_parts': ask_which_parts}}
    _config.set_override(data)
    try:
        result = asyncio.run(_choose_files(adapter, S(chat_id='test', thread_id=None), 'key', rows, 1))
    finally:
        _config.set_override(saved)
    return adapter, result


def test_the_setting_exists_and_is_off_by_default():
    assert settings.OPTIONAL_KEYS[("discord", "ask_which_parts")] is bool
    assert settings.current()["discord"]["ask_which_parts"] is False


def test_by_default_every_distinct_part_prints_without_a_question(asked):
    rows = [{'name': name} for name in PARTS]
    adapter, result = _run(rows)
    assert result == rows
    assert not asked
    assert any('This model has 3 files that print together' in post['content'] for post in adapter.sent)


def test_ask_which_parts_on_brings_the_question_back(asked):
    rows = [{'name': name} for name in PARTS]
    adapter, result = _run(rows, True)
    assert result == rows
    assert asked and asked[0]['choices'][:2] == ['Print all 3', 'Choose files']
    assert not adapter.sent


def test_versions_of_a_part_are_always_asked(asked):
    rows = [{'name': name} for name in ('cap-sign.stl', 'cap-no-sign.stl', 'shell.stl')]
    adapter, _result = _run(rows)
    assert asked and 'Which cap?' in asked[0]['question']
    assert not adapter.sent
