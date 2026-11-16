import asyncio
from types import SimpleNamespace as S

from print_handy.core.prepared_intake import _choose_files


class Adapter:
    def __init__(self):
        self.questions = []
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return S(success=True)

    async def send_clarify(self, **kwargs):
        self.questions.append(kwargs)
        return S(success=True)


def test_distinct_parts_are_announced_and_returned_without_print_all_question(monkeypatch):
    adapter = Adapter()
    questions = []

    async def ask(_adapter, _source, _key, question, choices, _timeout):
        questions.append({'question': question, 'choices': choices})
        return 0

    monkeypatch.setattr('print_handy.core.prepared_intake._ask', ask)
    rows = [{'name': name} for name in ('shell.stl', 'plunger.stl', 'spring.stl')]
    result = asyncio.run(_choose_files(adapter, S(chat_id='test', thread_id=None), 'key', rows, 1))
    assert result == rows
    assert not any('Print all' in str(question.get('choices')) for question in questions)
    assert not questions
    assert any('This model has 3 files that print together' in post['content'] for post in adapter.sent)


def test_version_groups_still_ask_which_version(monkeypatch):
    adapter = Adapter()
    questions = []

    async def ask(_adapter, _source, _key, question, choices, _timeout):
        questions.append({'question': question, 'choices': choices})
        return 0

    monkeypatch.setattr('print_handy.core.prepared_intake._ask', ask)
    rows = [{'name': name} for name in ('cap-sign.stl', 'cap-no-sign.stl', 'shell.stl')]
    asyncio.run(_choose_files(adapter, S(chat_id='test', thread_id=None), 'key', rows, 1))
    assert questions
    assert 'Which cap?' in questions[0]['question']
    assert not adapter.sent
