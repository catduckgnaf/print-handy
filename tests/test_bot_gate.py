"""print_handy.bot_gate: the container starts the bot for the configured platform.

Before the gate, docker/supervise.sh started the bot only when
DISCORD_BOT_TOKEN was set, so a Telegram- or Matrix-only container never
ran its bot while the healthcheck reported it fine.
"""
import copy
from pathlib import Path

import pytest

from print_handy import bot_gate
from print_handy.hermes import _config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def platform(monkeypatch):
    def use(name, **env):
        data = copy.deepcopy(_config.cfg())
        data["bot"]["platform"] = name
        monkeypatch.setattr(_config, "cfg", lambda: data)
        for var in ("DISCORD_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "MATRIX_ACCESS_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(_config, "profile_env_value", lambda n: env.get(n, ""))
    return use


@pytest.mark.parametrize("name,var", [("telegram", "TELEGRAM_BOT_TOKEN"),
                                      ("matrix", "MATRIX_ACCESS_TOKEN"),
                                      ("discord", "DISCORD_BOT_TOKEN")])
def test_each_platform_starts_with_its_own_token(platform, capsys, name, var):
    platform(name, **{var: "t"})
    assert bot_gate.main() == 0
    platform(name)  # no token at all
    assert bot_gate.main() == 3
    assert f"{var} is empty" in capsys.readouterr().out


def test_telegram_container_ignores_an_empty_discord_token(platform):
    platform("telegram", TELEGRAM_BOT_TOKEN="t")
    start, name, _ = bot_gate.decide()
    assert start and name == "telegram"


def test_unresolved_configuration_exits_2(monkeypatch):
    def broken():
        raise RuntimeError("bad settings")
    monkeypatch.setattr(_config, "cfg", broken)
    assert bot_gate.main() == 2


def test_unknown_settings_warn_at_the_gate_not_exit_2(platform, capsys, monkeypatch):
    # #1058: settings the running version does not know are named on stderr
    # and the gate still decides normally — they never make it "unresolved".
    from print_handy import settings
    monkeypatch.setattr(settings, "unknown_settings_warning",
                        lambda: "unknown setting(s) ignored: [listener] ticks")
    platform("discord", DISCORD_BOT_TOKEN="t")
    assert bot_gate.main() == 0
    assert "bot gate: WARNING unknown setting(s) ignored: [listener] ticks" in capsys.readouterr().err


def test_supervisor_and_healthcheck_use_the_gate():
    supervisor = (ROOT / "docker" / "supervise.sh").read_text()
    assert "python3 -m print_handy.bot_gate" in supervisor
    assert '${DISCORD_BOT_TOKEN:-}' not in supervisor
    probe = (ROOT / "docker" / "healthcheck.py").read_text()
    assert "from print_handy.bot_gate import decide" in probe
    assert 'os.environ.get("DISCORD_BOT_TOKEN")' not in probe
