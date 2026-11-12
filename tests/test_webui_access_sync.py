"""A login saved by another process reaches the running Web UI."""
import threading
from types import SimpleNamespace

from print_handy.webui import access, server as server_module


def _handler(tmp_path):
    data = {"paths": {"state_home": tmp_path},
            "webui": {"require_auth": True, "access_token_env": "WEBUI_ACCESS_TOKEN"}}
    server = SimpleNamespace(
        settings_data=data, bind_host="0.0.0.0", webui_environ={},
        access=access.resolve(data, "0.0.0.0", {}), sessions={"old": 1},
        access_stamp=server_module._access_stamp(data),
        access_sync_lock=threading.Lock())
    calls = []
    handler = SimpleNamespace(server=server,
                              _rotate_form_token=lambda: calls.append("rotate"),
                              _revoke_remembered_sessions=lambda: calls.append("revoke"),
                              _revoke_upload_links=lambda: calls.append("links"))
    return handler, data, calls


def _sync(handler):
    server_module._Handler._sync_access(handler)


def test_a_login_saved_elsewhere_takes_effect_without_a_restart(tmp_path):
    handler, data, calls = _handler(tmp_path)
    assert handler.server.access.required is False
    password = "correct horse battery staple"
    access.save_password(data, password, password, username="owner")
    _sync(handler)
    assert handler.server.access.mode == access.PASSWORD
    assert handler.server.sessions == {}
    assert calls == ["rotate", "revoke", "links"]


def test_an_unchanged_login_file_changes_nothing(tmp_path):
    handler, _data, calls = _handler(tmp_path)
    _sync(handler)
    assert handler.server.sessions == {"old": 1}
    assert calls == []


def test_a_rewrite_carrying_the_same_credential_changes_nothing(tmp_path):
    """The stamp is the file's mtime, so a rewrite that stores the very
    credential this server already enforces (the file re-saved byte for
    byte, or this process having resolved the login from it directly)
    must not clear sessions or rotate the form token: nothing was issued
    under a different rule. The walkthrough's remove-a-login flow relies
    on this — its POST carries the form token the server currently
    advertises, under the password the file already held."""
    handler, data, calls = _handler(tmp_path)
    password = "correct horse battery staple"
    access.save_password(data, password, password, username="owner")
    _sync(handler)
    assert handler.server.access.mode == access.PASSWORD
    handler.server.sessions = {"current": 2}
    calls.clear()
    path = access.access_path(data)
    path.write_bytes(path.read_bytes())
    _sync(handler)
    assert handler.server.access.mode == access.PASSWORD
    assert handler.server.sessions == {"current": 2}
    assert calls == []


def test_clearing_a_saved_login_elsewhere_takes_effect(tmp_path):
    handler, data, _calls = _handler(tmp_path)
    password = "correct horse battery staple"
    access.save_password(data, password, password, username="owner")
    _sync(handler)
    access.access_path(data).unlink()
    _sync(handler)
    assert handler.server.access.required is False
