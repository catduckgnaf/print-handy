"""#1817 L1: one Discord User-Agent, no Hermes names on the wire."""
from pathlib import Path

import print_handy
from print_handy.bot import rest

ROOT = Path(print_handy.__file__).resolve().parent


def test_the_discord_user_agent_names_the_project_and_version():
    assert rest.USER_AGENT == (
        f"DiscordBot (https://github.com/catduckgnaf/print-handy, {print_handy.__version__})")


def test_no_hermes_user_agent_or_boundary_is_left():
    for rel in ("runtime/print_alert_buttons.py", "runtime/exclude_chat.py",
                "core/spool_lifecycle.py", "bot/rest.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for old in ("hermes-3dprinting", "Hermes-SpoolLifecycle", "----hermes-print-"):
            assert old not in text, (rel, old)
    assert "rest.USER_AGENT" in (ROOT / "runtime/print_alert_buttons.py").read_text(encoding="utf-8")
    assert "----print-handy-" in (ROOT / "runtime/print_alert_buttons.py").read_text(encoding="utf-8")
