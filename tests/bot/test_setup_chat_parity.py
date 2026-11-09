"""Chat setup parity: one canonical step list on Discord, Telegram, Matrix (S5/S9).

The same answers, driven through the one guided machine each platform's
adapter feeds, must land the same settings keys the Web UI walkthrough
writes — and a Web UI password given in chat must never appear in the
guided session store or any reply.
"""
import json

import yaml

from print_handy.bot import guided, webui_access
from print_handy.webui import access

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
