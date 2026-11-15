"""Link and G-code intakes honor discord.question_timeout_s.

The three chat callers pass no timeout, so before this an intake question
waited forever and the expiry handler never ran. Found on a live install:
no receipt was ever EXPIRED, and an unanswered color question stayed open.
"""
import asyncio
from types import SimpleNamespace

from print_handy.core import prepared_intake
from print_handy.runtime import _config


def _run(monkeypatch, name, call):
    seen = {}

    async def fake(*args, **kwargs):
        seen.update(kwargs)
        return {"action": "skip"}

    monkeypatch.setattr(prepared_intake, name, fake)
    monkeypatch.setattr(prepared_intake, "_card_wrap", lambda adapter, source: adapter)
    monkeypatch.setattr(_config, "question_timeout_s", lambda: 1800)
    asyncio.run(call())
    return seen["timeout"]


def test_a_link_intake_with_no_timeout_uses_the_configured_one(monkeypatch):
    event, adapter = SimpleNamespace(source=None), object()
    assert _run(monkeypatch, "_prepare_link_intake",
                lambda: prepared_intake.prepare_link(event, adapter, {}, "root")) == 1800


def test_a_gcode_intake_with_no_timeout_uses_the_configured_one(monkeypatch):
    event, adapter = SimpleNamespace(source=None), object()
    assert _run(monkeypatch, "_print_gcode_file_intake",
                lambda: prepared_intake.print_gcode_file(event, adapter, "root", "file.gcode")) == 1800


def test_a_caller_timeout_still_wins(monkeypatch):
    event, adapter = SimpleNamespace(source=None), object()
    assert _run(monkeypatch, "_prepare_link_intake",
                lambda: prepared_intake.prepare_link(event, adapter, {}, "root", timeout=5)) == 5
