"""Bot check's design row.

The row must name the lane the transport selection would actually use:
the [design]/[llm] HTTP endpoint — the only lane left.
"""
import shutil

from print_handy.bot import __main__ as bot_main


def _rows(cfg, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x" * 20)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/openscad")
    return {label: (ok, detail) for label, ok, detail in bot_main.checks(cfg)}


def test_design_row_reports_the_http_endpoint(cfg, monkeypatch):
    cfg["design"].update(chat_url="http://design.invalid/v1", model="sol")
    ok, detail = _rows(cfg, monkeypatch)["design"]
    assert ok is True
    assert "sol at http://design.invalid/v1" in detail
