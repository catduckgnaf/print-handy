"""Health check, listener posts without Hermes, settings, doctor, and a clean no-Hermes process."""
import asyncio
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

from print_handy import doctor, settings
from print_handy.bot import __main__ as bot_main
from print_handy.bot import health
from print_handy.hermes import _config
from print_handy.hermes import moonraker_discord_announcements as announcer
from print_handy.hermes import moonraker_event_listener as mel


def beat(**extra):
    return {"mode": "standalone", "pid": os.getpid(), "heartbeat_at": time.time(), "connected": True,
            "user": "Print Handy#0001", "handlers": 13, **extra}


@pytest.mark.parametrize(("status", "ok", "reason"), [
    (None, False, "no heartbeat"),
    (beat(), True, "connected as Print Handy#0001"),
    (beat(heartbeat_at=time.time() - 3600), False, "old"),
    (beat(connected=False), False, "not connected"),
    # A heartbeat written by an older build still carries the retired field; it is ignored.
    (beat(hermes_modules=["tools.clarify_gateway"]), True, "connected as"),
    (beat(mode="hermes"), False, "mode"),
    (beat(pid=2 ** 22 + 12345), False, "is gone"),
])
def test_health_verdicts(status, ok, reason):
    got, why = health.evaluate(status, stale_s=120)
    assert got is ok and reason in why


def test_health_command_reads_the_heartbeat_file(capsys):
    health.write(beat())
    assert health.check() == 0
    health.write(beat(connected=False))
    assert health.check() == 1
    assert "not connected" in capsys.readouterr().out


def test_health_http_endpoint():
    async def flow():
        server = await health.serve(0, lambda: beat(), host="127.0.0.1")
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
            await writer.drain()
            data = await reader.read()
            writer.close()
            return data
        finally:
            server.close()
            await server.wait_closed()

    data = asyncio.run(flow())
    head, body = data.split(b"\r\n\r\n", 1)
    assert head.startswith(b"HTTP/1.1 200") and json.loads(body)["ok"] is True


def test_listener_posts_over_rest_in_standalone_mode(monkeypatch):
    sent = []
    monkeypatch.setitem(_config.cfg()["bot"], "mode", "standalone")
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: "tok" if name == "DISCORD_BOT_TOKEN" else "")
    monkeypatch.setattr("print_handy.bot.rest.send", lambda token, chat, text: sent.append((token, chat, text)) or (True, "1"))
    with mock.patch.object(mel.subprocess, "run", side_effect=AssertionError("hermes send was called")):
        assert mel.post("Print started: cube") is True
    assert sent == [("tok", announcer.DISCORD_CHAT_ID, "Print started: cube")]


def test_listener_posts_over_rest_in_hermes_mode(monkeypatch):
    sent = []
    monkeypatch.setitem(_config.cfg()["bot"], "mode", "hermes")
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: "tok" if name == "DISCORD_BOT_TOKEN" else "")
    monkeypatch.setattr("print_handy.bot.rest.send", lambda token, chat, text: sent.append((token, chat, text)) or (True, "1"))
    with mock.patch.object(mel.subprocess, "run", side_effect=AssertionError("hermes send was called")):
        assert mel.post("Print started: cube") is True
    assert sent == [("tok", announcer.DISCORD_CHAT_ID, "Print started: cube")]


def test_button_posts_use_the_configured_token_variable(monkeypatch):
    monkeypatch.setitem(_config.cfg()["discord"], "token_env", "MY_BOT_TOKEN")
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: {"MY_BOT_TOKEN": "mine"}.get(name, ""))
    assert mel._bot_token() == "mine"


def _load(text, tmp_path, environ=None):
    path = tmp_path / "local.yaml"
    path.write_text(text)
    return settings.load(local=path, environ=environ or {})


def test_bot_settings_defaults_and_validation(tmp_path):
    data = _load("", tmp_path)
    assert data["bot"] == {"mode": "standalone", "platform": "discord", "health_port": 0, "health_stale_s": 120, "sync_commands": True}
    assert data["discord"]["token_env"] == "DISCORD_BOT_TOKEN" and data["discord"]["allowed_user_ids"] == ""
    assert data["llm"]["chat_url"] == "" and data["llm"]["api_key_env"] == "LLM_API_KEY"
    data = _load("bot:\n  mode: standalone\ndiscord:\n  allowed_user_ids: '1, 2 3'\n", tmp_path,
                 {"PRINT_HANDY__BOT__HEALTH_PORT": "8080"})
    assert data["bot"]["mode"] == "standalone" and data["bot"]["health_port"] == 8080
    assert settings.user_ids(data["discord"]["allowed_user_ids"]) == ["1", "2", "3"]
    for text, error in (("bot:\n  mode: chatty\n", "mode must be one of"),
                        ("bot:\n  platform: smoke-signals\n", "platform must be one of"),
                        ("discord:\n  allowed_user_ids: 'me'\n", "numeric Discord user IDs"),
                        ("llm:\n  chat_url: http://x\n", "model is required"),
                        ("discord:\n  token_env: 'not a name'\n", "environment variable name"),
                        ("bot:\n  health_port: 70000\n", "health_port")):
        with pytest.raises(settings.SettingsError, match=error):
            _load(text, tmp_path)


def test_check_command_names_every_problem(monkeypatch, capsys, tmp_path):
    cfg = settings.load(local=Path(os.devnull), environ={})
    cfg["bot"]["mode"] = "hermes"  # this check run is the Hermes-mode one (#524: no longer the default)
    cfg["paths"]["hermes_home"] = tmp_path / "profile"
    monkeypatch.setattr(settings, "current", lambda: cfg)
    monkeypatch.setattr(_config, "cfg", lambda: cfg)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr(_config, "profile_env_value", lambda name, **kw: "")
    assert bot_main.check() == 1
    out = capsys.readouterr().out
    for line in ("FAIL  mode: hermes", "FAIL  token", "FAIL  print channel",
                 "ok    access: channel (everyone)",
                 "ok    handlers: 31", f"ok    state dir: {tmp_path}"):
        assert line in out, out


def test_check_design_row_states_off_on_and_missing_openscad(monkeypatch, capsys, tmp_path):
    """#388: the design row — the tri-state mode is named (off / auto-off /
    on); on names the model source; enabled without openscad is a FAIL."""
    cfg = settings.load(local=Path(os.devnull), environ={})
    cfg["paths"]["hermes_home"] = tmp_path / "profile"
    cfg["bot"]["mode"] = "standalone"
    monkeypatch.setattr(settings, "current", lambda: cfg)
    monkeypatch.setattr(_config, "cfg", lambda: cfg)
    bot_main.check()
    assert "ok    design: auto-off (no design/LLM endpoint configured" in capsys.readouterr().out
    cfg["design"]["enabled"] = False
    bot_main.check()
    assert "ok    design: off (design.enabled is false" in capsys.readouterr().out
    cfg["design"]["enabled"] = None
    cfg["llm"].update(chat_url="http://llm.invalid/v1", model="sol")
    real_which0 = shutil.which
    monkeypatch.setattr(shutil, "which",
                        lambda name: "/usr/bin/openscad" if name == "openscad" else real_which0(name))
    bot_main.check()
    assert "ok    design: auto-on (sol at http://llm.invalid/v1)" in capsys.readouterr().out
    cfg["design"]["enabled"] = True
    cfg["bot"]["mode"] = "standalone"
    cfg["llm"].update(chat_url="http://llm.invalid/v1", model="sol")
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which",
                        lambda name: "/usr/bin/openscad" if name == "openscad" else real_which(name))
    bot_main.check()
    assert "ok    design: on (sol at http://llm.invalid/v1)" in capsys.readouterr().out
    monkeypatch.setattr(shutil, "which",
                        lambda name: None if name == "openscad" else real_which(name))
    bot_main.check()
    out = capsys.readouterr().out
    assert "FAIL  design: enabled, but openscad is not on PATH" in out


def test_run_refuses_to_start_when_checks_fail(monkeypatch):
    monkeypatch.setattr(bot_main, "check", lambda out=None: 1)
    with mock.patch("print_handy.bot.client.PrintHandyBot") as bot:
        assert bot_main.run() == 2
    bot.assert_not_called()


def test_run_logs_the_version_at_startup(monkeypatch, caplog):
    from print_handy import __version__
    monkeypatch.setattr(bot_main, "check", lambda out=None: 1)
    with caplog.at_level(logging.INFO, logger="print_handy.bot.__main__"):
        assert bot_main.run() == 2
    assert f"standalone bot starting (Print Handy {__version__})" in caplog.text


def test_doctor_reports_mode_and_users(monkeypatch, tmp_path):
    cfg = settings.load(local=Path(os.devnull), environ={})
    cfg["bot"]["mode"] = "standalone"
    cfg["discord"].update(chat_id="1", allowed_user_ids="5, 6")
    cfg["paths"]["hermes_home"] = tmp_path / "data"
    assert doctor.check_bot(cfg) == "standalone, print channel 1, access: whitelist (2 users)"


def test_doctor_login_check_uses_the_token(monkeypatch):
    cfg = settings.load(local=Path(os.devnull), environ={})
    cfg["bot"]["mode"] = "standalone"
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    with mock.patch("print_handy.bot.rest.whoami", return_value={"id": "9", "username": "printhandy"}) as who:
        assert doctor.check_discord_login(cfg) == "printhandy (9)"
    assert who.call_args[0][0] == "tok"
    monkeypatch.delenv("DISCORD_BOT_TOKEN")
    monkeypatch.setattr(_config, "profile_env_value", lambda name, **kw: "")
    with pytest.raises(RuntimeError, match="DISCORD_BOT_TOKEN is empty"):
        doctor.check_discord_login(cfg)


BLOCK_HERMES = """
import importlib.abc, sys
class NoHermes(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in ('tools', 'gateway', 'hermes_state', 'hermes_cli'):
            raise ImportError('Hermes is not installed: ' + name)
        return None
sys.meta_path.insert(0, NoHermes())
"""

CHILD = BLOCK_HERMES + """
import json, os, sys, tempfile
sys.path.insert(0, os.path.join(os.getcwd(), 'tests'))
import conftest  # the offline test settings: temp state, fake hosts
from unittest import mock
from print_handy.core import printer_state
from corpus import harness
from print_handy.bot import health
import print_handy.bot.client  # the discord.py client imports without Hermes too
case = harness.load_case(harness.CASES / 'parts-one-plate')
with mock.patch.object(printer_state, 'save', lambda state: None):
    run = harness.run_case(case, tempfile.mkdtemp(), transport='standalone')
harness.check_expectations(case, run)
print('REPORT ' + json.dumps({'status': run.receipt.get('status'), 'starts': len(run.starts)}))
"""


def test_a_clean_process_with_hermes_blocked_runs_the_whole_intake():
    """Acceptance (#73): core and bot import and run with no Hermes module; any Hermes import fails here."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PRINT_HANDY", "HERMES_"))}
    env.update(PYTHONPATH=str(settings.ROOT), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-c", CHILD], cwd=str(settings.ROOT), env=env,
                          capture_output=True, text=True, timeout=300)
    lines = [line for line in proc.stdout.splitlines() if line.startswith("REPORT ")]
    assert lines, f"child failed ({proc.returncode}):\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    report = json.loads(lines[-1][len("REPORT "):])
    assert report == {"status": "STARTED", "starts": 1}


def test_install_standalone_renders_units_and_data_folder(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PRINT_HANDY", "HERMES_"))}
    env.update(SYSTEMD_USER_DIR=str(tmp_path / "units"), NO_SYSTEMCTL="1",
               PRINT_HANDY__PATHS__HERMES_HOME=str(tmp_path / "data"), PYTHON=sys.executable,
               PRINT_HANDY_CONFIG=str(tmp_path / "empty.yaml"))
    (tmp_path / "empty.yaml").write_text("")
    script = settings.ROOT / "scripts" / "install-standalone.sh"
    first = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=120)
    assert first.returncode == 0, first.stderr
    for name in ("print-handy-bot", "print-handy-listener"):
        unit = (tmp_path / "units" / f"{name}.service").read_text()
        assert "{{" not in unit and "PRINT_HANDY__BOT__MODE=standalone" in unit
        assert f"EnvironmentFile=-{tmp_path / 'data'}/.env" in unit
        assert "HERMES_HOME" not in unit and "PartOf=" not in unit  # not tied to a Hermes gateway
        assert (tmp_path / "units" / f"{name}.service").stat().st_mode & 0o777 != 0o600
    assert "-m print_handy.bot run" in (tmp_path / "units" / "print-handy-bot.service").read_text()
    assert (tmp_path / "data" / ".env").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "data" / "state").is_dir()
    again = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=120)
    assert again.stdout.count("unchanged") == 2


@pytest.mark.parametrize("changes", [
    {"host": "other-container"}, {"process_start": "reused"},
    {"pid": 0}, {"pid": -1}, {"pid": "invalid"}, {"pid": 10 ** 100},
    {"heartbeat_at": float("nan")}, {"heartbeat_at": time.time() + 3600},
])
def test_bot_health_command_rejects_foreign_or_invalid_identity(changes):
    health.write(beat())
    status = health.read()
    health.path().write_text(json.dumps({**status, **changes}))
    assert health.check(out=io.StringIO()) == 1


def test_bot_shutdown_awaits_owned_resources(cfg):
    from print_handy.bot.client import PrintHandyBot

    async def flow():
        bot = PrintHandyBot(cfg)
        stopped = asyncio.Event()
        started = asyncio.Event()

        async def heartbeat():
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        task = asyncio.create_task(heartbeat())
        await started.wait()
        server = mock.Mock()
        server.wait_closed = mock.AsyncMock()
        bot._background = [task, server]
        with mock.patch("discord.ext.commands.Bot.close", mock.AsyncMock()), \
                mock.patch("print_handy.core.owned_worker.shutdown", mock.AsyncMock()) as workers:
            await bot.close()
        workers.assert_awaited_once()  # no slicer or model-check worker outlives the bot
        assert stopped.is_set() and task.done()
        server.close.assert_called_once()
        server.wait_closed.assert_awaited_once()
        assert bot._background == [] and not bot.connected

    asyncio.run(flow())
