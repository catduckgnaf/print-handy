"""Logging configuration, redaction, stage timing and representative failure paths (#150). Offline."""
import io
import json
import logging
import logging.handlers
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from print_handy import logs, settings
from print_handy.core import orca_adapter
from print_handy.hermes import moonraker_event_listener as listener

# Built from parts so no token shaped string sits in the source (GitHub push protection flags those).
DISCORD_TOKEN = ".".join(["MTIzNDU2Nzg5MDEyMzQ1Njc4OQ", "GabcDE", "abcdefghijklmnopqrstuvwxyz0123456789"])


@pytest.fixture(autouse=True)
def clean_root():
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)


def lines(stream):
    return [line for line in stream.getvalue().splitlines() if line]


def test_text_format_has_timestamp_severity_component_and_bound_context():
    stream = io.StringIO()
    logs.configure({"level": "INFO", "format": "text"}, stream=stream)
    token = logs.bind(intake="abc123")
    try:
        logging.getLogger("print_handy.core.demo").warning("slice failed")
    finally:
        logs.unbind(token)
    (line,) = lines(stream)
    stamp, level, component, rest = line.split(" ", 3)
    assert stamp[:4].isdigit() and ("+" in stamp or "-" in stamp[10:])  # ISO time with an offset
    assert (level, component) == ("WARNING", "print_handy.core.demo:")
    assert rest == "slice failed intake=abc123"


def test_json_format_is_one_object_per_line_with_traceback():
    stream = io.StringIO()
    logs.configure({"level": "INFO", "format": "json"}, stream=stream)
    try:
        raise ValueError("bad plate")
    except ValueError:
        logging.getLogger("print_handy.demo").exception("fit failed")
    record = json.loads(stream.getvalue())
    assert record["level"] == "ERROR" and record["logger"] == "print_handy.demo"
    assert record["message"] == "fit failed"
    assert "Traceback" in record["exception"] and "ValueError: bad plate" in record["exception"]


def test_json_format_redacts_quoted_credentials_before_serializing():
    # P1-52: values are redacted before json.dumps, so escaped quotes in the
    # serialized form cannot defeat the patterns or corrupt the JSON.
    stream = io.StringIO()
    logs.configure({"level": "INFO", "format": "json"}, stream=stream)
    logging.getLogger("print_handy.demo").error(
        'payload {"api_key": "hunter2xyz"} and password="hunter2xyz"')
    (line,) = lines(stream)
    record = json.loads(line)  # the line must stay valid JSON
    assert "hunter2xyz" not in line
    assert record["message"] == 'payload {"api_key": "***"} and password="***"'


def test_json_format_redacts_values_of_credential_named_fields():
    # 2.2.5 review: the key-anchored patterns never see a dict key, so a field
    # named api_key / password must be hidden by its name.
    stream = io.StringIO()
    logs.configure({"level": "INFO", "format": "json"}, stream=stream)
    logging.getLogger("print_handy.demo").info(
        "login", extra={"ctx": {"api_key": "abc123xyz", "password": "pw-plain", "nested": {"Authorization": "abc123xyz"},
                                "max_tokens": 5, "printer": "voron"}})
    (line,) = lines(stream)
    record = json.loads(line)
    assert "abc123xyz" not in line and "pw-plain" not in line
    assert record["api_key"] == "***" and record["password"] == "***"
    assert record["max_tokens"] == 5 and record["printer"] == "voron"


def test_json_format_redacts_bound_fields_and_tracebacks():
    stream = io.StringIO()
    logs.configure({"level": "INFO", "format": "json"}, stream=stream)
    token = logs.bind(camera="http://user:hunter2xyz@cam.local/webcam/")
    try:
        try:
            raise ValueError('token="hunter2xyz" refused')
        except ValueError:
            logging.getLogger("print_handy.demo").exception("camera failed")
    finally:
        logs.unbind(token)
    (line,) = lines(stream)
    record = json.loads(line)
    assert "hunter2xyz" not in line
    assert record["camera"] == "http://***@cam.local/webcam/"
    assert 'token="***"' in record["exception"]


def test_level_controls_verbosity_and_debug_shows_detail():
    stream = io.StringIO()
    logs.configure({"level": "INFO"}, stream=stream)
    logging.getLogger("print_handy.demo").debug("detail")
    assert lines(stream) == []
    logs.configure({"level": "DEBUG"}, stream=stream)
    logging.getLogger("print_handy.demo").debug("detail")
    assert "DEBUG print_handy.demo: detail" in stream.getvalue()


def test_configure_twice_does_not_duplicate_lines():
    stream = io.StringIO()
    logs.configure({}, stream=stream)
    logs.configure({}, stream=stream)
    logging.getLogger("print_handy.demo").info("once")
    assert len(lines(stream)) == 1


def test_invalid_level_and_format_fall_back_to_defaults():
    stream = io.StringIO()
    logs.configure({"level": "LOUD", "format": "xml"}, stream=stream)
    assert logging.getLogger().level == logging.INFO
    logging.getLogger("print_handy.demo").info("hello")
    assert "INFO print_handy.demo: hello" in stream.getvalue()


def test_file_option_writes_a_daily_log_file(tmp_path):
    target = tmp_path / "logs" / "print-handy.log"
    logs.configure({"level": "INFO", "file": str(target)}, stream=io.StringIO())
    logging.getLogger("print_handy.demo").info("to disk")
    today = target.with_name(f"print-handy-{datetime.now():%Y-%m-%d}.log")
    assert "to disk" in today.read_text()
    assert logs.current_file(target) == today
    assert any(isinstance(h, logs.DailyFileHandler) for h in logging.getLogger().handlers)


def test_daily_file_rolls_at_midnight_and_prunes_past_retention(tmp_path, monkeypatch):
    target = tmp_path / "print-handy.log"
    for day in ("2026-10-20", "2026-10-22", "2026-10-23"):
        target.with_name(f"print-handy-{day}.log").write_text(f"{day}\n")
    (tmp_path / "print-handy-notes.log").write_text("not a dated file")
    (tmp_path / "other-2026-01-01.log").write_text("another log")
    day = {"now": "2026-11-04"}
    monkeypatch.setattr(logs.DailyFileHandler, "_today", staticmethod(lambda: day["now"]))
    monkeypatch.setattr(logs, "datetime", _FixedDatetime.at(day))
    handler = logs.DailyFileHandler(target, retention_days=14)
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        handler.emit(logging.makeLogRecord({"msg": "before midnight"}))
        # 2026-10-20 and 2026-10-22 are 15 and 13 days old on 11-04: only the first goes.
        assert [d for d, _ in logs.dated_files(target)] == ["2026-10-22", "2026-10-23", "2026-11-04"]
        day["now"] = "2026-11-05"
        handler.emit(logging.makeLogRecord({"msg": "after midnight"}))
    finally:
        handler.close()
    assert target.with_name("print-handy-2026-11-04.log").read_text() == "before midnight\n"
    assert target.with_name("print-handy-2026-11-05.log").read_text() == "after midnight\n"
    assert [d for d, _ in logs.dated_files(target)] == ["2026-10-23", "2026-11-04", "2026-11-05"]
    assert (tmp_path / "print-handy-notes.log").exists() and (tmp_path / "other-2026-01-01.log").exists()


def test_two_processes_share_todays_file_without_renaming(tmp_path):
    target = tmp_path / "print-handy.log"
    first, second = logs.DailyFileHandler(target, 14), logs.DailyFileHandler(target, 14)
    try:
        for handler, msg in ((first, "bot"), (second, "listener"), (first, "bot again")):
            handler.setFormatter(logging.Formatter("%(message)s"))
            handler.emit(logging.makeLogRecord({"msg": msg}))
    finally:
        first.close()
        second.close()
    assert logs.current_file(target).read_text().splitlines() == ["bot", "listener", "bot again"]


def test_retention_days_setting_is_validated_and_env_overridable():
    cfg = settings.load(local=False, user=False, environ={"PRINT_HANDY__LOGGING__RETENTION_DAYS": "30"})
    assert cfg["logging"]["retention_days"] == 30
    with pytest.raises(settings.SettingsError, match="retention_days"):
        settings.load(local=False, user=False, environ={"PRINT_HANDY__LOGGING__RETENTION_DAYS": "0"})


def test_docker_default_writes_logs_to_the_persistent_state_volume():
    root = Path(__file__).resolve().parents[1]
    for suffix in ("yaml", "toml"):
        cfg = settings.load(local=root / "config" / f"docker.{suffix}", user=False, environ={})
        assert cfg["logging"]["file"] == "/data/state/logs/print-handy.log"
        assert cfg["logging"]["retention_days"] == 14
    assert '"$state_home/logs"' in (root / "docker" / "entrypoint.sh").read_text()


class _FixedDatetime(datetime):
    @classmethod
    def at(cls, day):
        cls._day = day
        return cls

    @classmethod
    def now(cls, tz=None):
        return datetime.strptime(cls._day["now"], "%Y-%m-%d")


def test_unwritable_file_falls_back_to_stderr_only(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    logs.configure({"file": str(blocker / "sub" / "x.log")}, stream=io.StringIO())
    assert "logging to stderr only" in capsys.readouterr().err


@pytest.mark.parametrize("secret_line", [
    f"login with {DISCORD_TOKEN}",
    "Authorization: Bearer abcdefghijklmnop12345",
    "request failed token=abcd1234efgh5678",
    'payload {"api_key": "sk-live-123456789"}',
    "GET https://user:hunter2@printer.local/api failed",
    "DISCORD_BOT_TOKEN=abcdef123456",
])
def test_redact_hides_credentials(secret_line):
    cleaned = logs.redact(secret_line)
    for fragment in (DISCORD_TOKEN, "abcdefghijklmnop12345", "abcd1234efgh5678", "sk-live-123456789",
                     "hunter2", "abcdef123456"):
        assert fragment not in cleaned
    assert logs.REDACTED in cleaned


def test_redact_hides_secret_environment_values_and_leaves_normal_text(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "plain-looking-secret-value")
    assert "plain-looking-secret-value" not in logs.redact("calling with plain-looking-secret-value now")
    assert logs.redact("Slicing finished in 12.0s for part.stl") == "Slicing finished in 12.0s for part.stl"


def test_handlers_redact_message_arguments_and_tracebacks():
    stream = io.StringIO()
    logs.configure({"format": "text"}, stream=stream)
    try:
        raise RuntimeError(f"rejected {DISCORD_TOKEN}")
    except RuntimeError:
        logging.getLogger("print_handy.demo").exception("failed with %s", "password=hunter2xyz")
    output = stream.getvalue()
    assert DISCORD_TOKEN not in output and "hunter2xyz" not in output
    assert "Traceback" in output


def test_stage_logs_start_and_elapsed_time():
    stream = io.StringIO()
    logs.configure({"level": "INFO"}, stream=stream)
    with logs.stage(logging.getLogger("print_handy.demo"), "fit check", plate=2):
        pass
    started, done = lines(stream)
    assert "fit check started plate=2" in started
    assert "fit check done in " in done and done.rstrip().endswith("plate=2")


def test_stage_failure_keeps_traceback_and_reraises():
    stream = io.StringIO()
    logs.configure({"level": "INFO"}, stream=stream)
    with pytest.raises(ValueError, match="no geometry"):
        with logs.stage(logging.getLogger("print_handy.demo"), "inspect"):
            raise ValueError("no geometry")
    output = stream.getvalue()
    assert "ERROR print_handy.demo: inspect failed after" in output
    assert "ValueError: no geometry" in output and "Traceback" in output


def test_stage_interruption_is_a_warning_without_a_traceback():
    stream = io.StringIO()
    logs.configure({"level": "INFO"}, stream=stream)
    with pytest.raises(KeyboardInterrupt):
        with logs.stage(logging.getLogger("print_handy.demo"), "slice"):
            raise KeyboardInterrupt
    output = stream.getvalue()
    assert "WARNING print_handy.demo: slice interrupted after" in output and "Traceback" not in output


# --- settings -------------------------------------------------------------------------------------------

def test_logging_defaults_come_from_the_checked_in_config(tmp_path):
    import toml_twin  # a TOML home rendered from home.yaml: the legacy TOML loader path (#1322)
    legacy = toml_twin.write_home_toml(tmp_path / "home.toml")
    for home in (settings.load(local=False, environ={}), settings.load(home=legacy, local=False, environ={})):
        assert home["logging"] == {"level": "INFO", "format": "text", "file": "", "retention_days": 14, "levels": {}}


def test_env_override_sets_debug_json_logging():
    cfg = settings.load(local=False, environ={"PRINT_HANDY__LOGGING__LEVEL": "debug", "PRINT_HANDY__LOGGING__FORMAT": "JSON"})
    assert cfg["logging"]["level"] == "DEBUG" and cfg["logging"]["format"] == "json"


@pytest.mark.parametrize("env, message", [
    ({"PRINT_HANDY__LOGGING__LEVEL": "loud"}, "[logging] level must be one of"),
    ({"PRINT_HANDY__LOGGING__FORMAT": "xml"}, "[logging] format must be one of"),
])
def test_invalid_logging_settings_are_rejected(env, message):
    with pytest.raises(settings.SettingsError, match=message.replace("[", r"\[")):
        settings.load(local=False, environ=env)


# --- representative failure paths -----------------------------------------------------------------------

def test_slicer_nonzero_exit_logs_rc_and_elapsed_time(caplog):
    done = SimpleNamespace(returncode=3, stdout="", stderr="bad\nworse")
    with caplog.at_level(logging.DEBUG, logger=orca_adapter.logger.name), \
            mock.patch.object(orca_adapter.subprocess, "run", return_value=done):
        assert orca_adapter._run_slicer(["orca", "--secret-arg"], "OrcaSlicer", Path("/tmp/job")) is done
    messages = [r.getMessage() for r in caplog.records]
    assert any("OrcaSlicer run finished rc=3 in " in m for m in messages)
    assert [r.levelno for r in caplog.records if "finished" in r.getMessage()] == [logging.WARNING]
    assert not any("--secret-arg" in m for m in messages)  # the command line is never logged


def test_slicer_that_cannot_start_logs_a_traceback_and_raises(caplog):
    with caplog.at_level(logging.INFO, logger=orca_adapter.logger.name), \
            mock.patch.object(orca_adapter.subprocess, "run", side_effect=FileNotFoundError("orca")):
        with pytest.raises(FileNotFoundError):
            orca_adapter._run_slicer(["orca"], "OrcaSlicer", Path("/tmp/job"))
    failure = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert failure and failure[0].exc_info and "did not complete" in failure[0].getMessage()


def test_listener_log_writes_the_legacy_file_and_the_shared_logger(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(listener, "LOG", tmp_path / "logs" / "listener.log")
    with caplog.at_level(logging.INFO, logger=listener.logger.name):
        listener.log("post ok target=discord:1")
        try:
            raise OSError("socket closed")
        except OSError:
            listener.log(f"connection lost: token=abc12345secret; retry in 5s")
    written = logs.current_file(tmp_path / "logs" / "listener.log").read_text()
    assert "post ok target=discord:1" in written and "abc12345secret" not in written
    ok, lost = caplog.records
    assert (ok.levelno, lost.levelno) == (logging.INFO, logging.WARNING)
    assert lost.exc_info and "abc12345secret" not in lost.getMessage()


def test_redact_hides_telegram_bot_tokens_including_in_api_urls():
    # Built from parts so no token shaped string sits in the source.
    token = "123456" + "789:" + "AA" + "x" * 33
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    cleaned = logs.redact(f"POST {url} failed; token was {token}")
    assert token not in cleaned and "AA" + "x" * 33 not in cleaned
    assert "https://api.telegram.org/bot***/sendMessage" in cleaned
    assert logs.redact("Print started at 12:34 for cube.stl") == "Print started at 12:34 for cube.stl"


def test_redact_hides_matrix_access_tokens_and_leaves_user_ids_alone():
    # Built from parts so no token shaped string sits in the source.
    token = "syt_" + "aBcDeF0123" + "_" + "xYz987wVu" + "_2"
    for line in (f"login failed for {token}",
                 f"Authorization: Bearer {token}",
                 f"GET https://hs.example/_matrix/client/v3/sync?access_token={token}&since=s1"):
        cleaned = logs.redact(line)
        assert token not in cleaned and "***" in cleaned
    query = logs.redact(f"GET https://hs.example/_matrix/client/v3/sync?access_token={token}&since=s1")
    assert "since=s1" in query
    ordinary = "@catduck:matrix.org posted in !prints:matrix.org at 12:34"
    assert logs.redact(ordinary) == ordinary


def test_process_binding_reaches_worker_threads_and_task_bind_still_wins():
    """P2-161: a plain threading.Thread does not inherit the ContextVar
    binding, so listener worker-thread lines lost their printer_id. The
    process-wide binding is the fallback every thread sees; a task-level
    bind still overrides it, and clear_context drops both."""
    import threading

    stream = io.StringIO()
    logs.configure({"level": "INFO", "format": "text"}, stream=stream)
    logs.clear_context()
    try:
        logs.bind_process(printer_id="moon-two")
        worker = threading.Thread(
            target=lambda: logging.getLogger("print_handy.demo").info("from a worker thread"))
        worker.start()
        worker.join()
        assert any("printer_id=moon-two" in line for line in lines(stream))
        token = logs.bind(printer_id="moon-one")
        try:
            logging.getLogger("print_handy.demo").info("task line")
        finally:
            logs.unbind(token)
        assert any("printer_id=moon-one" in line for line in lines(stream))
        assert logs.bound_context()["printer_id"] == "moon-two"
    finally:
        logs.clear_context()
    assert logs.bound_context() == {}
