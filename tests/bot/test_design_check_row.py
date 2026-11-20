"""Bot check's design row.

The row must name the lane the transport selection would actually use:
the one [llm] endpoint (#1731) — design.model is only a model-name
override on it, and the retired design.chat_url is inert.
"""
import shutil

from print_handy.bot import __main__ as bot_main


def _rows(cfg, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x" * 20)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/openscad")
    return {label: (ok, detail) for label, ok, detail in bot_main.checks(cfg)}


def test_design_row_reports_the_one_endpoint(cfg, monkeypatch):
    cfg["llm"].update(chat_url="http://llm.invalid/v1", model="luna")
    cfg["design"].update(model="sol")
    ok, detail = _rows(cfg, monkeypatch)["design"]
    assert ok is True
    assert "sol at http://llm.invalid/v1" in detail


def test_design_row_ignores_the_retired_design_endpoint(cfg, monkeypatch):
    cfg["llm"].update(chat_url="", model="")
    cfg["design"].update(chat_url="http://design.invalid/v1", model="sol")
    ok, detail = _rows(cfg, monkeypatch)["design"]
    assert ok is True
    assert detail.startswith("auto-off (the AI endpoint is not configured")
