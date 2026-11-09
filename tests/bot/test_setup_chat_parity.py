"""Chat setup parity: one canonical step list on Discord, Telegram, Matrix (S5/S9).

The same answers, driven through the one guided machine each platform's
adapter feeds, must land the same settings keys the Web UI walkthrough
writes — and a Web UI password given in chat must never appear in the
guided session store or any reply.
"""
import asyncio
import json

import pytest
import yaml

from print_handy.bot import guided, platform, runtime, webui_access
from print_handy.hermes import _config
from print_handy.webui import access

from corpus import fake_discord

PASSWORD = "correct horse battery staple"


def _store(tmp_path):
    return guided.SetupStore(tmp_path / "state" / guided.STORE_NAME)


def _data(tmp_path):
    return {"paths": {"hermes_home": tmp_path},
            "webui": {"access_token_env": "WEBUI_ACCESS_TOKEN", "require_auth": True}}


def _drive(store, key, answers, **kwargs):
    guided.handle_text(key, "/print-handy setup", store=store, now=1000.0, **kwargs)
    reply = None
    for index, answer in enumerate(answers):
        reply = guided.handle_text(key, answer, store=store, now=1001.0 + index, **kwargs)
    return reply


def _apply(store, key, tmp_path, **kwargs):
    target = tmp_path / "local.yaml"
    return guided.handle_text(
        key, "apply", store=store, now=2000.0,
        apply_config=lambda answers: guided._apply_answers(answers, target),
        **kwargs), target


DISCORD_ANSWERS = ("discord", "flashforge.ad5x", "100000000000000001",
                   "200000000000000002", "skip", "skip",
                   "http://camera.test/snapshot.jpg", "on", "stored", "skip")
TELEGRAM_ANSWERS = ("telegram", "flashforge.ad5x", "-1001234567890",
                    "200000000000000002", "skip", "skip",
                    "http://camera.test/snapshot.jpg", "on", "stored", "skip")
MATRIX_ANSWERS = ("matrix", "flashforge.ad5x", "!room123:example.org",
                  "@alice:example.org", "https://matrix.example.org",
                  "skip", "skip", "http://camera.test/snapshot.jpg", "on",
                  "stored", "skip")


def test_canonical_step_keys_are_platform_independent():
    keys = [step.key for step in guided.steps()]
    assert keys[:4] == ["platform", "model", "chat_id", "user_ids"]
    for key in ("moonraker_url", "spoolman_url", "camera_snapshot_url", "timelapse",
                "token_stored", "webui_login", "webui_username", "webui_password",
                "webui_confirm"):
        assert key in keys


def test_discord_flow_writes_the_canonical_keys(tmp_path):
    store = _store(tmp_path)
    _drive(store, "k", DISCORD_ANSWERS)
    reply, target = _apply(store, "k", tmp_path)
    assert reply.startswith("**Setup complete.**")
    written = yaml.safe_load(target.read_text())
    assert written["bot"] == {"mode": "standalone", "platform": "discord"}
    assert written["discord"]["chat_id"] == "100000000000000001"
    assert written["services"]["camera_snapshot_url"] == "http://camera.test/snapshot.jpg"
    assert written["services"]["camera_source"] == "auto"
    assert written["timelapse"] == {"mode": "rendered", "capture_enabled": True}


def test_telegram_flow_writes_its_own_section(tmp_path):
    store = _store(tmp_path)
    _drive(store, "k", TELEGRAM_ANSWERS)
    reply, target = _apply(store, "k", tmp_path)
    assert reply.startswith("**Setup complete.**")
    written = yaml.safe_load(target.read_text())
    assert written["bot"]["platform"] == "telegram"
    assert written["telegram"]["chat_id"] == "-1001234567890"
    assert "discord" not in written
    assert written["services"]["camera_source"] == "auto"
    assert written["timelapse"]["capture_enabled"] is True


def test_matrix_flow_asks_the_homeserver_and_writes_room_keys(tmp_path):
    store = _store(tmp_path)
    reply = _drive(store, "k", MATRIX_ANSWERS[:4])
    assert "homeserver" in reply.casefold()  # the Matrix-only step appears
    _drive_rest = MATRIX_ANSWERS[4:]
    for index, answer in enumerate(_drive_rest):
        reply = guided.handle_text("k", answer, store=store, now=1010.0 + index)
    reply, target = _apply(store, "k", tmp_path)
    assert reply.startswith("**Setup complete.**")
    written = yaml.safe_load(target.read_text())
    assert written["bot"]["platform"] == "matrix"
    assert written["matrix"]["room_id"] == "!room123:example.org"
    assert written["matrix"]["homeserver_url"] == "https://matrix.example.org"
    assert written["matrix"]["allowed_user_ids"] == "@alice:example.org"


def test_matrix_room_and_user_validation(tmp_path):
    store = _store(tmp_path)
    guided.handle_text("k", "/print-handy setup", store=store, now=1000.0)
    guided.handle_text("k", "matrix", store=store, now=1001.0)
    guided.handle_text("k", "flashforge.ad5x", store=store, now=1002.0)
    out = guided.handle_text("k", "not-a-room", store=store, now=1003.0)
    assert "does not work" in out and "room ID" in out
    guided.handle_text("k", "!room123:example.org", store=store, now=1004.0)
    out = guided.handle_text("k", "alice", store=store, now=1005.0)
    assert "does not work" in out and "Matrix user IDs" in out


def test_platform_answer_accepts_numbers_for_numbered_lists(tmp_path):
    store = _store(tmp_path)
    guided.handle_text("k", "/print-handy setup", store=store, now=1000.0)
    out = guided.handle_text("k", "2", store=store, now=1001.0)
    assert "Saved: Chat platform" in out
    assert store.get("k")["answers"]["platform"] == "telegram"


def _drive_to_login(store, key, tmp_path):
    answers = ("discord", "flashforge.ad5x", "100000000000000001", "everyone",
               "skip", "skip", "skip", "skip", "stored")
    _drive(store, key, answers)


def test_login_password_flow_never_persists_the_password(tmp_path):
    store = _store(tmp_path)
    data = _data(tmp_path)
    _drive_to_login(store, "k", tmp_path)
    guided.handle_text("k", "password", store=store, now=1100.0, access_data=data)
    guided.handle_text("k", "catduck", store=store, now=1101.0, access_data=data)
    guided.handle_text("k", PASSWORD, store=store, now=1102.0, access_data=data)
    reply = guided.handle_text("k", PASSWORD, store=store, now=1103.0, access_data=data)
    assert "ready to review" in reply and PASSWORD not in reply
    saved = (tmp_path / "state" / guided.STORE_NAME).read_text()
    assert PASSWORD not in saved and "correct horse" not in saved
    resolved = access.resolve(data, "0.0.0.0", environ={})
    assert resolved.mode == access.PASSWORD
    assert resolved.matches_login("catduck", PASSWORD)
    review = guided.handle_text("k", "/print-handy status", store=store, now=1104.0)
    assert PASSWORD not in review


def test_login_password_mismatch_returns_to_the_password_step(tmp_path):
    store = _store(tmp_path)
    data = _data(tmp_path)
    _drive_to_login(store, "k", tmp_path)
    guided.handle_text("k", "password", store=store, now=1100.0, access_data=data)
    guided.handle_text("k", "catduck", store=store, now=1101.0, access_data=data)
    guided.handle_text("k", PASSWORD, store=store, now=1102.0, access_data=data)
    out = guided.handle_text("k", "a different phrase entirely", store=store,
                             now=1103.0, access_data=data)
    assert "does not work" in out and "password" in out.casefold()
    assert webui_access.saved_claim(data) is None
    assert PASSWORD not in (tmp_path / "state" / guided.STORE_NAME).read_text()


def test_login_leave_open_applies_at_review(tmp_path):
    store = _store(tmp_path)
    data = _data(tmp_path)
    _drive_to_login(store, "k", tmp_path)
    guided.handle_text("k", "open", store=store, now=1100.0, access_data=data)
    assert webui_access.saved_claim(data) is None  # not before apply consent
    reply, _target = _apply(store, "k", tmp_path, access_data=data)
    assert webui_access.saved_claim(data) == "open"
    assert "left open by your saved choice" in reply


def test_login_clear_removes_a_saved_claim_at_apply(tmp_path):
    store = _store(tmp_path)
    data = _data(tmp_path)
    webui_access.set_password(data, "catduck", PASSWORD, PASSWORD, environ={})
    assert webui_access.saved_claim(data) == "password"
    _drive_to_login(store, "k", tmp_path)
    guided.handle_text("k", "clear", store=store, now=1100.0, access_data=data)
    reply, _target = _apply(store, "k", tmp_path, access_data=data)
    assert webui_access.saved_claim(data) is None
    assert "saved Web UI claim was removed" in reply
    assert PASSWORD not in reply


def test_timelapse_off_and_camera_skip_write_no_camera_keys(tmp_path):
    store = _store(tmp_path)
    answers = ("discord", "flashforge.ad5x", "100000000000000001", "everyone",
               "skip", "skip", "skip", "off", "stored", "skip")
    _drive(store, "k", answers)
    _reply, target = _apply(store, "k", tmp_path)
    written = yaml.safe_load(target.read_text())
    assert written["timelapse"] == {"mode": "off", "capture_enabled": False}
    assert "camera_snapshot_url" not in (written.get("services") or {})
    # The session store is valid JSON and holds no secret-shaped values.
    json.loads((tmp_path / "state" / guided.STORE_NAME).read_text())


# ------------------------------------------------- the token step's platform

def test_token_env_follows_the_chosen_platform_not_the_source():
    session = {"answers": {"platform": "telegram"}}
    assert guided.setup_token_env({}, session, "discord") == "TELEGRAM_BOT_TOKEN"
    assert guided.setup_token_env(None, session, "discord") == "TELEGRAM_BOT_TOKEN"


def test_token_env_falls_back_to_the_source_until_a_platform_is_chosen():
    assert guided.setup_token_env({}, None, "telegram") == "TELEGRAM_BOT_TOKEN"
    assert guided.setup_token_env({}, {"answers": {}}, "matrix") == "MATRIX_ACCESS_TOKEN"
    assert guided.setup_token_env({}, None, None) == "DISCORD_BOT_TOKEN"


def test_token_env_reads_the_chosen_platforms_own_setting():
    cfg = {"discord": {"token_env": "WRONG"},
           "telegram": {"token_env": "MY_TG_TOKEN"},
           "matrix": {"access_token_env": "MY_MX_TOKEN"}}
    assert guided.setup_token_env(cfg, {"answers": {"platform": "telegram"}},
                                  "discord") == "MY_TG_TOKEN"
    assert guided.setup_token_env(cfg, {"answers": {"platform": "matrix"}},
                                  "discord") == "MY_MX_TOKEN"


def test_resumed_token_step_keeps_the_token_variable(tmp_path):
    store = _store(tmp_path)
    answers = TELEGRAM_ANSWERS[:8]
    reply = _drive(store, "k", answers, token_env="TELEGRAM_BOT_TOKEN")
    assert "$TELEGRAM_BOT_TOKEN" in reply
    guided.handle_text("k", "/print-handy cancel", store=store, now=1100.0)
    reply = guided.handle_text("k", "/print-handy setup", store=store, now=1101.0,
                               token_env="TELEGRAM_BOT_TOKEN")
    assert "$TELEGRAM_BOT_TOKEN" in reply and "DISCORD_BOT_TOKEN" not in reply


@pytest.fixture
def _clean_guided_store():
    path = _config.state_dir() / guided.STORE_NAME
    path.unlink(missing_ok=True)
    yield
    path.unlink(missing_ok=True)


@pytest.fixture
def core(_clean_guided_store, cfg, monkeypatch, tmp_path):
    cfg["paths"]["hermes_home"] = tmp_path / "profile"
    monkeypatch.setattr(_config, "cfg", lambda: cfg)
    discord = fake_discord.FakeDiscord(chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    return discord, runtime.BotCore(discord.client, cfg)


def _send(discord, bot, text):
    before = len(discord.sent)
    asyncio.run(bot.handle_message(discord.message(text)))
    return discord.sent[before:]


def test_telegram_chosen_on_discord_names_the_telegram_variable(core):
    discord, bot = core
    _send(discord, bot, "/print-handy setup")
    sent = []
    for answer in TELEGRAM_ANSWERS[:8]:
        sent = _send(discord, bot, answer)
    assert "BotFather" in sent[-1]
    assert "$TELEGRAM_BOT_TOKEN" in sent[-1]
    assert "DISCORD_BOT_TOKEN" not in sent[-1]


def test_dispatcher_tells_the_flow_when_messages_cannot_be_deleted(core, monkeypatch):
    discord, bot = core
    monkeypatch.setattr(webui_access, "deletion_supported", lambda adapter: False)
    _send(discord, bot, "/print-handy setup")
    for answer in ("discord", "flashforge.ad5x", "100000000000000001", "everyone",
                   "skip", "skip", "skip", "skip", "stored", "password"):
        _send(discord, bot, answer)
    sent = _send(discord, bot, "catduck")
    assert "cannot delete messages" in sent[-1]


# ----------------------------------------------- the pending password's life

@pytest.fixture
def pending():
    guided._pending_webui_passwords.clear()
    yield guided._pending_webui_passwords
    guided._pending_webui_passwords.clear()


def _to_password_step(store, key, tmp_path, data, **kwargs):
    answers = ("discord", "flashforge.ad5x", "100000000000000001", "everyone",
               "skip", "skip", "skip", "skip", "stored")
    _drive(store, key, answers, **kwargs)
    guided.handle_text(key, "password", store=store, now=1100.0, access_data=data, **kwargs)
    return guided.handle_text(key, "catduck", store=store, now=1101.0, access_data=data, **kwargs)


def _to_confirm_step(store, key, tmp_path, data):
    _to_password_step(store, key, tmp_path, data)
    guided.handle_text(key, PASSWORD, store=store, now=1102.0, access_data=data)


def test_captured_password_is_held_with_its_time(tmp_path, pending):
    store = _store(tmp_path)
    _to_confirm_step(store, "k", tmp_path, _data(tmp_path))
    assert pending["k"] == (PASSWORD, 1102.0)


def test_cancel_clears_the_pending_password(tmp_path, pending):
    store = _store(tmp_path)
    data = _data(tmp_path)
    _to_confirm_step(store, "k", tmp_path, data)
    reply = guided.handle_text("k", "/print-handy cancel", store=store, now=1103.0)
    assert "k" not in pending
    assert "stays saved" not in reply  # nothing was confirmed yet


def test_restart_clears_the_pending_password(tmp_path, pending):
    store = _store(tmp_path)
    _to_confirm_step(store, "k", tmp_path, _data(tmp_path))
    guided.handle_text("k", "/print-handy setup restart", store=store, now=1103.0)
    assert "k" not in pending


def test_expiry_clears_the_pending_password(tmp_path, pending):
    store = _store(tmp_path)
    _to_confirm_step(store, "k", tmp_path, _data(tmp_path))
    later = 1102.0 + guided.SESSION_TIMEOUT_S + 1
    reply = guided.handle_text("k", "anything", store=store, now=later)
    assert "timed out" in reply
    assert "k" not in pending


def test_any_message_purges_other_sessions_stale_passwords(tmp_path, pending):
    store = _store(tmp_path)
    now = 5000.0 + guided.SESSION_TIMEOUT_S
    pending["old"] = ("stale password", 4999.0)
    pending["fresh"] = ("fresh password", now - 10)
    guided.handle_text("someone", "/print-handy status", store=store, now=now)
    assert "old" not in pending
    assert "fresh" in pending


def _assert_not_consumed(reply, data):
    assert "Start the login step again" in reply
    assert PASSWORD not in reply
    assert webui_access.saved_claim(data) is None


def test_confirmation_after_cancel_cannot_consume_the_password(tmp_path, pending):
    store = _store(tmp_path)
    data = _data(tmp_path)
    _to_confirm_step(store, "k", tmp_path, data)
    guided.handle_text("k", "/print-handy cancel", store=store, now=1103.0)
    guided.handle_text("k", "/print-handy setup", store=store, now=1104.0)
    reply = guided.handle_text("k", PASSWORD, store=store, now=1105.0, access_data=data)
    _assert_not_consumed(reply, data)


def test_confirmation_after_expiry_cannot_consume_the_password(tmp_path, pending):
    store = _store(tmp_path)
    data = _data(tmp_path)
    _to_confirm_step(store, "k", tmp_path, data)
    later = 1102.0 + guided.SESSION_TIMEOUT_S + 1
    guided.handle_text("k", "/print-handy setup", store=store, now=later)
    reply = guided.handle_text("k", PASSWORD, store=store, now=later + 1, access_data=data)
    _assert_not_consumed(reply, data)


def test_stale_password_cannot_be_consumed_in_a_session_kept_alive(tmp_path, pending):
    store = _store(tmp_path)
    data = _data(tmp_path)
    _to_confirm_step(store, "k", tmp_path, data)
    # Resuming refreshes the session, but not the password's own age.
    guided.handle_text("k", "/print-handy setup", store=store,
                       now=1102.0 + guided.SESSION_TIMEOUT_S - 10)
    reply = guided.handle_text("k", PASSWORD, store=store,
                               now=1102.0 + guided.SESSION_TIMEOUT_S + 1, access_data=data)
    _assert_not_consumed(reply, data)


def test_cancel_after_a_confirmed_password_says_the_login_stays(tmp_path, pending):
    store = _store(tmp_path)
    data = _data(tmp_path)
    _to_confirm_step(store, "k", tmp_path, data)
    guided.handle_text("k", PASSWORD, store=store, now=1103.0, access_data=data)
    assert webui_access.saved_claim(data) == "password"
    reply = guided.handle_text("k", "/print-handy cancel", store=store, now=1104.0)
    assert "Web UI login you confirmed stays saved" in reply
    assert webui_access.saved_claim(data) == "password"


# ------------------------------------------ chats that cannot delete messages

def test_login_prompts_are_honest_when_messages_cannot_be_deleted(tmp_path, pending):
    store = _store(tmp_path)
    data = _data(tmp_path)
    login = _drive(store, "k", ("discord", "flashforge.ad5x", "100000000000000001", "everyone",
                                "skip", "skip", "skip", "skip", "stored"),
                   can_delete_secrets=False)
    assert "cannot delete messages" in login and "is deleted" not in login
    guided.handle_text("k", "password", store=store, now=1100.0, access_data=data,
                       can_delete_secrets=False)
    ask = guided.handle_text("k", "catduck", store=store, now=1101.0, access_data=data,
                             can_delete_secrets=False)
    assert webui_access.capture_notice(False) in ask
    assert "deleted from chat" not in ask
    confirm = guided.handle_text("k", PASSWORD, store=store, now=1102.0, access_data=data,
                                 can_delete_secrets=False)
    assert "cannot delete messages" in confirm and "deleted from chat" not in confirm
    assert PASSWORD not in confirm


def test_login_prompts_promise_deletion_by_default(tmp_path, pending):
    store = _store(tmp_path)
    ask = _to_password_step(store, "k", tmp_path, _data(tmp_path))
    assert "deleted from chat" in ask and "cannot delete" not in ask


def test_capture_notice_matches_the_adapter_check():
    bare = platform.BasePlatformAdapter([])
    assert webui_access.pre_capture_notice(bare) == webui_access.capture_notice(False)
    assert "cannot delete messages" in webui_access.capture_notice(False)
