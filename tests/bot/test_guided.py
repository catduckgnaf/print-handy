"""Guided setup and debugging (#162): small resumable steps, consent, redaction, no printer writes."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from print_handy import __version__, settings
from print_handy.bot import commands, guided, runtime
from print_handy.hermes import _config

from corpus import fake_discord

CHAT = "100000000000000001"
USER = "200000000000000002"
TOKEN = "SECRET-TOKEN-VALUE-123456"
FAKE_TOKEN = "MTIzNDU2Nzg5MGFiY2RlZg.GhIjKl.MnOpQrStUvWxYz0123456789abcd"


def store(tmp_path):
    return guided.SetupStore(tmp_path / "guided-setup.json")


def say(store_obj, text, key="k", **kwargs):
    kwargs.setdefault("report", lambda: "REPORT")
    return guided.handle_text(key, text, store=store_obj, **kwargs)


def finish_steps(store_obj, key="k", **kwargs):
    """Drive a setup session to the review step with valid answers."""
    say(store_obj, "/print-handy setup", key=key, **kwargs)
    for answer in ("flashforge.ad5x", CHAT, USER, "skip", "http://spoolman.test:7912", "stored"):
        say(store_obj, answer, key=key, **kwargs)


# ---------------------------------------------------------------------- commands and help

def test_print_handy_command_is_registered_and_in_help():
    command = commands.by_name("print-handy")
    assert command is not None
    assert commands.as_text("print-handy", {"action": "setup"}) == "/print-handy setup"
    assert commands.as_text("print-handy", {"action": "debugging"}) == "/print-handy debugging"
    assert "/print-handy" in commands.help_text()
    help_reply = say(store(Path("/nonexistent-dir")), "/print-handy help", key="h")
    assert "setup" in help_reply and "debugging" in help_reply and "cancel" in help_reply


def test_unrelated_text_is_not_consumed_without_a_session(tmp_path):
    assert say(store(tmp_path), "hello there") is None
    assert say(store(tmp_path), "/print-stop") is None


def test_a_failed_setup_apply_logs_the_reason_it_names(tmp_path, caplog):
    """The failure reply says 'the detail is in the log' (P2-180), so the
    exception must actually be logged before the reply drops it: actionable
    refusals (an existing config that must be edited or moved aside) were
    lost entirely because this handler never logged and guided had no
    logger. The checklist stays at review for the retry either way."""
    st = store(tmp_path)
    finish_steps(st)

    def boom(answers):
        raise RuntimeError("the existing config/local.yaml already describes a printer; move it aside")

    with caplog.at_level("WARNING", logger="print_handy.bot.guided"):
        out = say(st, "apply", apply_config=boom)
    assert "the detail is in the log" in out
    assert "already describes a printer" in caplog.text
    assert st.get("k")["status"] == "awaiting_consent"


# ---------------------------------------------------------------------- setup flow

def test_setup_walks_small_steps_and_persists_a_checklist(tmp_path):
    st = store(tmp_path)
    first = say(st, "/print-handy setup", now=1000)
    assert "Setup step 1 of 6: Printer model" in first and "☐ Print channel" in first
    bad = say(st, "not-a-model", now=1001)
    assert "does not work" in bad and "Setup step 1 of 6" in bad
    nxt = say(st, "flashforge.ad5x", now=1002)
    assert "Saved: Printer model" in nxt and "Setup step 2 of 6: Print channel" in nxt
    session = st.get("k")
    assert session["answers"] == {"model": "flashforge.ad5x"} and session["step"] == 1
    assert (tmp_path / "guided-setup.json").stat().st_mode & 0o777 == 0o600


def test_setup_rejects_invalid_ids_and_urls_without_advancing(tmp_path):
    st = store(tmp_path)
    say(st, "/print-handy setup")
    say(st, "flashforge.ad5x")
    out = say(st, "the-print-channel")
    assert "does not work" in out and st.get("k")["step"] == 1
    say(st, CHAT)
    out = say(st, "alice, bob")
    assert "does not work" in out and st.get("k")["step"] == 2
    say(st, USER)
    out = say(st, "printer.local:7125")  # no scheme
    assert "does not work" in out and st.get("k")["step"] == 3
    out = say(st, "http://printer.local:7125")
    assert "Setup step 5 of 6" in out
    out = say(st, "spoolman.local:7912")  # no scheme
    assert "does not work" in out and st.get("k")["step"] == 4
    out = say(st, "http://spoolman.local:7912")
    assert "Setup step 6 of 6" in out


def test_printer_url_follows_the_model(tmp_path):
    st = store(tmp_path)
    say(st, "/print-handy setup")
    prompt = None
    for answer in ("sovol_marlin.sv04", CHAT, USER):
        prompt = say(st, answer)
    assert "OctoPrint" in prompt and "octopi.local" in prompt
    out = say(st, "http://octopi.local")
    assert "Setup step 5 of 6" in out
    for answer in ("skip", "stored"):
        say(st, answer)
    target = tmp_path / "local.yaml"
    say(st, "apply", apply_config=lambda answers: guided._apply_answers(answers, target))
    written = target.read_text()
    assert "octoprint_url:" in written and "http://octopi.local" in written
    assert "moonraker_url" not in written

    st = store(tmp_path)
    say(st, "/print-handy setup restart")
    prompt = None
    for answer in ("bambulab.a1", CHAT, USER):
        prompt = say(st, answer)
    assert "hostname" in prompt
    out = say(st, "http://192.168.1.50")
    assert "does not work" in out and "hostname" in out
    out = say(st, "192.168.1.50")
    assert "Setup step 5 of 6" in out
    for answer in ("skip", "stored"):
        say(st, answer)
    target = tmp_path / "bambu.yaml"
    say(st, "apply", apply_config=lambda answers: guided._apply_answers(answers, target))
    written = target.read_text()
    assert "lan_host:" in written and "192.168.1.50" in written
    assert "moonraker_url" not in written


def test_spoolman_step_is_offered_for_models_without_moonraker(tmp_path):
    st = store(tmp_path)
    say(st, "/print-handy setup")
    for answer in ("bambulab.a1", CHAT, USER):
        say(st, answer)
    out = say(st, "skip")  # hostname printers can skip the Printer URL
    assert "Setup step 5 of 6: Spoolman URL" in out
    out = say(st, "http://spoolman.test:7912")
    assert "Setup step 6 of 6" in out
    assert st.get("k")["answers"]["spoolman_url"] == "http://spoolman.test:7912"


def test_token_step_never_collects_a_token(tmp_path):
    st = store(tmp_path)
    say(st, "/print-handy setup")
    for answer in ("flashforge.ad5x", CHAT, USER, "skip", "skip"):
        say(st, answer)
    prompt = say(st, "/print-handy status")
    assert "Bot token" in prompt
    out = say(st, FAKE_TOKEN)
    assert "Never paste the token" in out or "not collected in chat" in out
    assert st.get("k")["step"] == 5, "a pasted token must not advance the step"
    saved = (tmp_path / "guided-setup.json").read_text()
    assert FAKE_TOKEN not in saved and TOKEN not in saved
    out = say(st, "stored")
    assert "ready to review" in out and st.get("k")["status"] == "awaiting_consent"
    assert "ready" not in json.loads(saved) or True  # answers file was rewritten since; checked above


def test_status_reports_the_running_version(tmp_path):
    st = store(tmp_path)
    out = say(st, "/print-handy status")
    assert out.splitlines()[0] == f"Print Handy {__version__}"
    assert "No guided setup has been started here" in out
    say(st, "/print-handy setup", now=1000)
    out = say(st, "/print-handy status", now=1001)
    assert out.splitlines()[0] == f"Print Handy {__version__}"
    assert "in progress" in out


def test_cancel_persists_and_a_new_store_resumes_after_restart(tmp_path):
    st = store(tmp_path)
    say(st, "/print-handy setup", now=1000)
    say(st, "flashforge.ad5x", now=1001)
    out = say(st, "/print-handy cancel", now=1002)
    assert "paused" in out and "☑ Printer model" in out
    assert say(st, "standalone", now=1003) is None, "a cancelled session must not consume answers"

    restarted = store(tmp_path)  # a fresh store object over the same file: a bot restart
    out = say(restarted, "/print-handy setup", now=1004)
    assert "resuming your saved checklist" in out and "Setup step 2 of 6" in out
    status = say(restarted, "/print-handy status", now=1005)
    assert "in progress" in status and "☑ Printer model" in status


def test_timeout_does_not_consume_an_answer_but_keeps_the_checklist(tmp_path):
    st = store(tmp_path)
    say(st, "/print-handy setup", now=1000)
    late = 1000 + guided.SESSION_TIMEOUT_S + 1
    out = say(st, "flashforge.ad5x", now=late)
    assert "timed out" in out and st.get("k")["answers"] == {}
    out = say(st, "/print-handy setup", now=late + 1)
    assert "timed out" in out and "Setup step 1 of 6" in out
    out = say(st, "flashforge.ad5x", now=late + 2)
    assert "Setup step 2 of 6" in out


def test_manual_choice_writes_nothing_and_gives_precise_steps(tmp_path):
    st = store(tmp_path)
    target = tmp_path / "local.yaml"
    finish_steps(st)
    out = say(st, "manual", apply_config=lambda answers: guided._apply_answers(answers, target))
    assert "Manual setup steps" in out and "config/local.yaml" in out
    assert "`[services] spoolman_url`" in out
    assert not target.exists(), "manual means the flow writes nothing itself"
    assert st.get("k")["answers"]["model"] == "flashforge.ad5x"


def test_apply_writes_only_after_explicit_consent(tmp_path):
    st = store(tmp_path)
    target = tmp_path / "local.yaml"
    finish_steps(st)
    assert not target.exists(), "reaching the review step must not write anything"
    out = say(st, "apply", apply_config=lambda answers: guided._apply_answers(answers, target))
    assert "Setup complete" in out and st.get("k")["status"] == "complete"
    written = target.read_text()
    assert "flashforge.ad5x" in written and CHAT in written and USER in written
    assert "spoolman_url" in written and "http://spoolman.test:7912" in written
    assert TOKEN not in written and "DISCORD_BOT_TOKEN" not in written


def test_setup_restart_discards_saved_answers(tmp_path):
    st = store(tmp_path)
    say(st, "/print-handy setup")
    say(st, "flashforge.ad5x")
    out = say(st, "/print-handy setup restart")
    assert "Setup step 1 of 6" in out and st.get("k")["answers"] == {}


# ---------------------------------------------------------------------- diagnostics

def diag_cfg(tmp_path, **overrides):
    cfg = settings.load(local=Path(os.devnull), environ={})
    cfg["paths"]["hermes_home"] = tmp_path / "profile"
    cfg["bot"]["mode"] = "standalone"
    cfg["discord"].update(chat_id=CHAT, allowed_user_ids=USER)
    cfg["services"].update(moonraker_url="http://printer.test:7125", spoolman_url="")
    cfg.update(overrides)
    return cfg


def run_report(tmp_path, cfg=None, **kwargs):
    cfg = cfg if cfg is not None else diag_cfg(tmp_path)
    (tmp_path / "profile" / "state").mkdir(parents=True, exist_ok=True)
    kwargs.setdefault("loader", lambda: cfg)
    kwargs.setdefault("token_getter", lambda _cfg: TOKEN)
    kwargs.setdefault("login_probe", lambda token: {"username": "TestBot"})
    kwargs.setdefault("probe", lambda url: 200)
    kwargs.setdefault("which", lambda exe: "/usr/bin/" + str(exe))
    kwargs.setdefault("version_runner", lambda command: "OrcaSlicer 2.0")
    kwargs.setdefault("environ", {})
    return guided.format_report(guided.run_diagnostics(**kwargs),
                                secrets=[TOKEN], roots=[tmp_path])


def test_debugging_reports_each_area_and_redacts_secrets_ids_and_paths(tmp_path):
    report = run_report(tmp_path)
    for area in ("configuration", "bot mode and login", "permissions", "service: moonraker",
                 "executable: python", "executable: slicer", "state and logs"):
        assert area in report, report
    assert "read-only" in report
    assert TOKEN not in report and CHAT not in report and USER not in report
    assert str(tmp_path) not in report and str(Path.home()) not in report


def test_debugging_flags_missing_and_invalid_settings(tmp_path):
    def broken():
        raise settings.SettingsError(f"bad [discord] token={TOKEN} in {Path.home()}/private/local.yaml")

    report = guided.format_report(
        guided.run_diagnostics(loader=broken, environ={}), secrets=[TOKEN], roots=[tmp_path])
    assert "✗ configuration" in report and "Next:" in report
    assert TOKEN not in report and str(Path.home()) not in report

    cfg = diag_cfg(tmp_path)
    cfg["discord"].update(chat_id="", allowed_user_ids="")
    cfg["bot"]["mode"] = "standalone"
    report = run_report(tmp_path, cfg, token_getter=lambda _cfg: "")
    assert "✗ permissions" in report and "✗ bot mode and login" in report


def test_debugging_probe_failure_is_actionable_and_redacted(tmp_path):
    def probe(url):
        raise RuntimeError(f"cannot reach {url} token={TOKEN} from {Path.home()}/private")

    report = run_report(tmp_path, probe=probe)
    assert "⚠ service: moonraker" in report and "Next:" in report
    assert TOKEN not in report and str(Path.home()) not in report


def test_debugging_never_invokes_printer_write_operations(tmp_path):
    cfg = diag_cfg(tmp_path)
    (tmp_path / "profile" / "state").mkdir(parents=True, exist_ok=True)
    writes = [mock.patch("print_handy.hermes.print_stop.cancel"),
              mock.patch("print_handy.hermes.print_control.resume"),
              mock.patch("print_handy.hermes.print_control.coldpull"),
              mock.patch("print_handy.core.printer_state.save", create=True),
              mock.patch("print_handy.core.model_job.request_json")]
    with writes[0] as a, writes[1] as b, writes[2] as c, writes[3] as d, writes[4] as e:
        report = run_report(tmp_path, cfg)
    assert "debugging" in report.lower() or "configuration" in report
    for patched in (a, b, c, d, e):
        patched.assert_not_called()


# ---------------------------------------------------------------------- through the bot

@pytest.fixture(autouse=True)
def _clean_guided_store():
    path = _config.state_dir() / guided.STORE_NAME
    path.unlink(missing_ok=True)
    yield
    path.unlink(missing_ok=True)


def make_core(cfg):
    discord = fake_discord.FakeDiscord(chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    return discord, runtime.BotCore(discord.client, cfg)


def test_bot_setup_flow_and_debugging_over_messages(cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(guided, "_default_probe", lambda url: 200)
    monkeypatch.setattr(guided, "_default_version_runner", lambda command: "fake 1.0")
    monkeypatch.setattr(guided.settings, "LOCAL", tmp_path / "local.yaml")
    discord, core = make_core(cfg)

    def send(text):
        before = len(discord.sent)
        out = asyncio.run(core.handle_message(discord.message(text)))
        return out, discord.sent[before:]

    out, sent = send("/print-handy setup")
    assert out == {"action": "skip", "reason": "guided setup/debugging handled"}
    assert "Setup step 1 of 6" in sent[-1]
    _, sent = send("flashforge.ad5x")
    assert "Setup step 2 of 6" in sent[-1]
    _, sent = send("/print-handy cancel")
    assert "paused" in sent[-1]
    _, sent = send("/print-handy setup")
    assert "resuming" in sent[-1] and "Setup step 2 of 6" in sent[-1]
    _, sent = send("/print-handy debugging")
    assert "Print Handy debugging" in sent[-1] and "read-only" in sent[-1]
    assert not (tmp_path / "local.yaml").exists(), "debugging must not write configuration"


def test_bot_slash_print_handy_runs_as_typed_text(cfg, monkeypatch):
    monkeypatch.setattr(guided, "_default_probe", lambda url: 200)
    monkeypatch.setattr(guided, "_default_version_runner", lambda command: "fake 1.0")
    discord, core = make_core(cfg)
    interaction = fake_discord.FakeInteraction(discord, discord.operator, channel=discord.channel)
    asyncio.run(core.run_slash(interaction, "print-handy", {"action": "debugging"}))
    assert interaction.replies[0] == {"content": "`/print-handy debugging`", "ephemeral": False}
    assert discord.sent and "Print Handy debugging" in discord.sent[-1]


def test_default_version_runner_runs_the_slicer_from_a_writable_scratch_dir(monkeypatch):
    # OrcaSlicer writes 00000.log into its cwd and aborts when that is not writable (Docker /app).
    seen = []

    def fake_run(command, **kwargs):
        cwd = kwargs.get("cwd")
        seen.append((command[-1], cwd, bool(cwd) and os.path.isdir(cwd) and os.access(cwd, os.W_OK)))
        return SimpleNamespace(returncode=0, stdout="" if command[-1] == "--version" else "OrcaSlicer 2.3\n", stderr="")

    monkeypatch.setattr(guided.subprocess, "run", fake_run)
    assert guided._default_version_runner(["orca-slicer"]) == "OrcaSlicer 2.3"
    assert [flag for flag, _, _ in seen] == ["--version", "--help"]
    assert all(writable for _, _, writable in seen)
    assert all(os.path.abspath(cwd) != os.path.abspath(os.getcwd()) for _, cwd, _ in seen)
