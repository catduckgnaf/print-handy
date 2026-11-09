"""Setup-only bootstrap mode: tier split, Q5 bind-at-start, refusals (S6)."""
import time

from print_handy.bot import __main__ as bot_main
from print_handy.bot import guided


def _cfg(platform="discord"):
    return {"bot": {"platform": platform, "mode": "standalone"}}


def test_credential_tier_failure_is_never_setup_only():
    rows = [("token", False, "empty"), ("print channel", False, "empty")]
    assert bot_main.split_check_rows(rows) == (["token"], ["print channel"], [])
    assert bot_main.setup_only_mode(_cfg(), rows) is False


def test_configuration_tier_failures_start_setup_only():
    rows = [("token", True, "set"), ("print channel", False, "empty"),
            ("access", True, "channel (everyone)")]
    assert bot_main.setup_only_mode(_cfg(), rows) is True
    rows = [("token", True, "set"), ("allowed users", False, "none")]
    assert bot_main.setup_only_mode(_cfg("telegram"), rows) is True
    rows = [("token", True, "set"), ("print room", False, "empty"),
            ("allowed users", False, "none")]
    assert bot_main.setup_only_mode(_cfg("matrix"), rows) is True


def test_matrix_homeserver_is_credential_tier_in_effect():
    rows = [("token", True, "set"), ("homeserver", False, "empty"),
            ("print room", False, "empty")]
    assert bot_main.setup_only_mode(_cfg("matrix"), rows) is False


def test_other_failures_and_no_failures_are_not_setup_only():
    assert bot_main.setup_only_mode(_cfg(), [("handlers", False, "boom")]) is False
    assert bot_main.setup_only_mode(_cfg(), [("token", True, "set")]) is False
    assert bot_main.setup_only_mode(
        _cfg(), [("mode", False, "hermes"), ("print channel", False, "")]) is False


def _store(tmp_path):
    return guided.SetupStore(tmp_path / guided.STORE_NAME)


def _start(store, key, now):
    return guided.handle_text(key, "/print-handy setup", store=store, now=now,
                              setup_only=True)


def test_first_interlocutor_binds_at_conversation_start(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    reply = _start(store, "chan::alice", now)
    assert "Guided setup started" in reply
    # A second interlocutor cannot start or join while the bind is live.
    refused = _start(store, "chan::bob", now + 1)
    assert "already in progress with another user" in refused
    plain = guided.handle_text("chan::bob", "flashforge.ad5x", store=store,
                               now=now + 2, setup_only=True)
    assert "already in progress with another user" in plain
    # The owner continues normally.
    answer = guided.handle_text("chan::alice", "discord", store=store,
                                now=now + 3, setup_only=True)
    assert "Saved: Chat platform" in answer


def test_timeout_releases_the_bind(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    _start(store, "chan::alice", now)
    later = now + guided.SESSION_TIMEOUT_S + 10
    reply = _start(store, "chan::bob", later)
    assert "Guided setup started" in reply or "resuming" in reply.casefold()


def test_non_setup_commands_are_refused_in_setup_only_mode(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    for command in ("/print-handy debugging", "/print-handy bundle",
                    "/print-handy debug on", "/print-handy add-printer"):
        reply = guided.handle_text("chan::alice", command, store=store, now=now,
                                   setup_only=True, report=lambda: "REPORT")
        assert "Setup-only mode" in reply, command
    # And a stray plain message gets the setup invitation, not silence.
    reply = guided.handle_text("chan::alice", "print this please", store=store,
                               now=now, setup_only=True)
    assert "/print-handy setup" in reply


def test_setup_only_apply_writes_completion_and_names_the_restart(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    key = "chan::alice"
    _start(store, key, now)
    for index, answer in enumerate(
            ("discord", "flashforge.ad5x", "100000000000000001", "everyone", "skip", "skip",
             "skip", "skip", "skip", "skip")):
        guided.handle_text(key, answer, store=store, now=now + 1 + index, setup_only=True)
    reply = guided.handle_text(key, "apply", store=store, now=now + 10,
                               setup_only=True, apply_config=lambda answers: "written.")
    assert reply.startswith("**Setup complete.**")
    assert "Restart Print Handy" in reply
    from print_handy import setup_state
    record = setup_state.load_completion_in_state_dir(tmp_path)
    assert record["done"] is True and record["surface"] == "chat"


def test_gateway_carries_the_setup_only_flag():
    from types import SimpleNamespace

    from print_handy.bot.runtime import Gateway
    gateway = Gateway(SimpleNamespace(is_allowed_id=lambda user: False), "",
                      setup_only=True)
    assert gateway.setup_only is True
    # The normal authorization is unchanged: setup-only admission does
    # not silently authorize anyone for ordinary commands.
    source = SimpleNamespace(chat_id="1", user_id="2")
    assert gateway._is_user_authorized_for_source(source) is False
