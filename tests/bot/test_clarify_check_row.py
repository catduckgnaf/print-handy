"""bot check's clarify ui row: the bot's own renderer is the only one left."""
from print_handy.bot import __main__ as bot_main


def test_bot_check_clarify_row_reports_the_own_renderer(cfg, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x" * 20)
    rows = {label: detail for label, ok, detail in bot_main.checks(cfg)}
    assert rows["clarify ui"].startswith("own renderer")
