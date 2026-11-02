"""P2-252: a "Choose files" selection typed as a reply must survive the
typed-reply round trip.

typed_reply() runs the prompt's parser and resolves the gateway prompt with
str(value); ask() then re-runs the parser on that string. For file numbers
the parser returns a list of indices, whose str form ("[1, 2]") the parser
itself rejects — so typed selections reopened the question forever and
files beyond the four shown buttons could never be picked by typing. The
test drives the real _pick_files question and answers it through
typed_reply(), the way the Hermes plugin route does.
"""
import asyncio
from types import SimpleNamespace as S

import pytest

from print_handy.bot import runtime
from print_handy.core import link_intake, prepared_intake, prompts

USER = "200000000000000002"
CHAT = "100000000000000001"


@pytest.fixture(autouse=True)
def _core_registry(monkeypatch):
    monkeypatch.setattr(prompts, "_active", True)
    monkeypatch.setattr(link_intake, "_typed_prompts", {})


class Adapter:
    def __init__(self):
        self.asked = []

    async def send_clarify(self, **kw):
        self.asked.append(kw)
        return S(success=True)


def test_typed_file_selection_beyond_the_shown_buttons_completes():
    async def flow():
        adapter = Adapter()
        source = runtime.make_source(USER, CHAT, None)
        rows = [{'name': f'part{i}.stl'} for i in range(1, 7)]
        task = asyncio.create_task(
            prepared_intake._pick_files(adapter, source, 'model-link:files', rows, 5))
        for _ in range(200):
            if adapter.asked:
                break
            await asyncio.sleep(0)
        assert adapter.asked, 'the Choose files question was not sent'
        # Files 5 and 6 have no buttons (only the first four are shown).
        assert link_intake.typed_reply(source, '5 6') == ''
        return await asyncio.wait_for(task, 2)

    picked = asyncio.run(flow())
    assert [row['name'] for row in picked] == ['part5.stl', 'part6.stl']


def test_typed_file_selection_within_the_shown_buttons_completes():
    async def flow():
        adapter = Adapter()
        source = runtime.make_source(USER, CHAT, None)
        rows = [{'name': f'part{i}.stl'} for i in range(1, 5)]
        task = asyncio.create_task(
            prepared_intake._pick_files(adapter, source, 'model-link:files', rows, 5))
        for _ in range(200):
            if adapter.asked:
                break
            await asyncio.sleep(0)
        assert adapter.asked
        assert link_intake.typed_reply(source, '2 3') == ''
        return await asyncio.wait_for(task, 2)

    picked = asyncio.run(flow())
    assert [row['name'] for row in picked] == ['part2.stl', 'part3.stl']
