"""Chat Web UI access core: validation parity, env-token copy, secrets discipline."""
import asyncio
import json

from print_handy.bot import platform, webui_access
from print_handy.webui import access


def _data(tmp_path):
    # The minimal settings shape the access writers need: a data
    # home under tmp, plus the [webui] token-env name.
    return {"paths": {"state_home": tmp_path},
            "webui": {"access_token_env": "WEBUI_ACCESS_TOKEN",
                      "require_auth": True}}


def test_set_password_writes_a_login_the_webui_resolve_accepts(tmp_path):
    data = _data(tmp_path)
    result = webui_access.set_password(data, "catduck", "correct horse battery",
                                       "correct horse battery", environ={})
    assert result.ok and result.action == "set_password"
    assert "correct horse" not in result.message  # never echoed
    resolved = access.resolve(data, "0.0.0.0", environ={})
    assert resolved.mode == access.PASSWORD
    assert resolved.matches_login("catduck", "correct horse battery")
    assert not resolved.matches_login("catduck", "wrong password")


def test_validation_matches_the_webui_refusals(tmp_path):
    data = _data(tmp_path)
    weak = webui_access.set_password(data, "user", "password", "password", environ={})
    assert not weak.ok and "commonly guessed" in weak.message
    short = webui_access.set_password(data, "user", "short", "short", environ={})
    assert not short.ok and "at least 8" in short.message
    mismatch = webui_access.set_password(data, "user", "long enough phrase",
                                         "different phrase", environ={})
    assert not mismatch.ok and "do not match" in mismatch.message
    bad_name = webui_access.set_password(data, "has space", "long enough phrase",
                                         "long enough phrase", environ={})
    assert not bad_name.ok and "username" in bad_name.message
    assert webui_access.saved_claim(data) is None  # nothing was written


def test_env_token_coexists_and_the_copy_says_so(tmp_path):
    data = _data(tmp_path)
    environ = {"WEBUI_ACCESS_TOKEN": "docker-token"}
    result = webui_access.set_password(data, "user", "long enough phrase",
                                       "long enough phrase", environ=environ)
    assert result.ok and result.env_token_present
    assert "keeps working" in result.message
    resolved = access.resolve(data, "0.0.0.0", environ=environ)
    assert resolved.mode == access.PASSWORD
    assert resolved.matches("docker-token")  # co-credential (Q4)


def test_leave_open_is_an_explicit_saved_record(tmp_path):
    data = _data(tmp_path)
    refused = webui_access.leave_open(data, False, environ={})
    assert not refused.ok and webui_access.saved_claim(data) is None
    result = webui_access.leave_open(data, True, environ={})
    assert result.ok and webui_access.saved_claim(data) == "open"
    saved = json.loads(access.access_path(data).read_text())
    assert saved == {"mode": "open"}


def test_leave_open_does_not_override_an_env_token(tmp_path):
    data = _data(tmp_path)
    environ = {"WEBUI_ACCESS_TOKEN": "docker-token"}
    result = webui_access.leave_open(data, True, environ=environ)
    assert result.ok and "does not override the Docker token" in result.message
    resolved = access.resolve(data, "0.0.0.0", environ=environ)
    assert resolved.mode == access.TOKEN


def test_clear_claim_removes_only_the_saved_file(tmp_path):
    data = _data(tmp_path)
    webui_access.set_password(data, "user", "long enough phrase",
                              "long enough phrase", environ={})
    unconfirmed = webui_access.clear_claim(data, False, environ={})
    assert not unconfirmed.ok and webui_access.saved_claim(data) == "password"
    environ = {"WEBUI_ACCESS_TOKEN": "docker-token"}
    result = webui_access.clear_claim(data, True, environ=environ)
    assert result.ok and webui_access.saved_claim(data) is None
    assert "that token still applies" in result.message
    resolved = access.resolve(data, "0.0.0.0", environ=environ)
    assert resolved.mode == access.TOKEN  # the Docker operator stays authoritative


def test_clear_claim_with_nothing_saved_still_explains_the_token(tmp_path):
    data = _data(tmp_path)
    result = webui_access.clear_claim(data, True, environ={})
    assert result.ok and "no saved Web UI claim" in result.message
    assert "No environment token is set" in result.message


def test_deletion_support_follows_the_adapter_override():
    base = platform.BasePlatformAdapter([])
    assert webui_access.deletion_supported(base) is False
    assert "cannot delete messages" in webui_access.pre_capture_notice(base)

    class Deleting(platform.BasePlatformAdapter):
        async def delete_message(self, chat_id, message_id, metadata=None):
            return True

    adapter = Deleting([])
    assert webui_access.deletion_supported(adapter) is True
    assert "deleted from chat" in webui_access.pre_capture_notice(adapter)
    assert asyncio.run(adapter.delete_message("c", "1")) is True
    assert webui_access.deletion_supported(None) is False
