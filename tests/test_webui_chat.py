"""Web UI chat routes: behind the Web UI's auth, wired to the service.

A posted message lands in the chat service's transcript and the page
renders the conversation; both POST routes take the Web UI form
token. Nothing here slices or prints — the service runs everything
through BotCore (tests/bot/test_web_chat.py).
"""
import http.client
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

from print_handy import settings
from print_handy.webui import server as webui_server


def load_settings(tmp, extra=""):
    local = Path(tmp) / "local.yaml"
    local.write_text(
        f"paths:\n  hermes_home: {tmp}/hermes\n"
        "webui:\n  enabled: true\n  port: 8765\n" + extra)
    return settings.load(local=local, environ={})


@pytest.fixture()
def running(tmp_path):
    srv = webui_server.make_server(load_settings(tmp_path), environ={}, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, srv.server_address[1]
    srv.shutdown()


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = dict(headers or {})
    if body is not None:
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=body, headers=hdrs)
    response = conn.getresponse()
    payload = response.read().decode("utf-8", "replace")
    conn.close()
    return response.status, payload


def test_message_round_trip_through_the_page(running):
    srv, port = running
    status, page = call(port, "GET", "/chat")
    assert status == 200 and "Message" in page
    body = urllib.parse.urlencode({"_csrf": srv.form_token, "text": "help"})
    status, _page = call(port, "POST", "/chat", body=body)
    assert status == 303
    deadline = time.time() + 10
    page = ""
    while time.time() < deadline:
        status, page = call(port, "GET", "/chat")
        if "You:" in page and "help" in page:
            break
        time.sleep(0.05)
    assert "You:" in page and "help" in page


def test_post_without_form_token_is_refused(running):
    srv, port = running
    body = urllib.parse.urlencode({"text": "help"})
    status, _page = call(port, "POST", "/chat", body=body)
    assert status == 403


def test_choice_post_without_open_prompt_redirects(running):
    srv, port = running
    body = urllib.parse.urlencode({"_csrf": srv.form_token,
                                   "clarify_id": "", "index": "0"})
    status, _page = call(port, "POST", "/chat/choice", body=body)
    assert status == 303


def test_choice_post_on_a_closed_prompt_gives_feedback(running):
    """A refused press used to redirect silently (PR #1106 review).

    The press resolves nothing — the question is closed or unknown —
    so the page comes back with an explanation instead of a bare
    redirect that looks like success.
    """
    srv, port = running
    body = urllib.parse.urlencode({"_csrf": srv.form_token,
                                   "clarify_id": "nope", "index": "0"})
    status, page = call(port, "POST", "/chat/choice", body=body)
    assert status == 200
    assert "wasn&#x27;t accepted" in page  # _esc renders the apostrophe


def test_concurrent_first_chat_requests_build_one_service(tmp_path, monkeypatch):
    """Two concurrent first /chat requests share one chat service.

    The server is threaded, so the lazy build in _chat_service must be
    serialized: a second service would spawn its own BotCore and
    event-loop thread, and a message submitted to the orphaned one
    would be answered into an outbox the page never renders.
    """
    srv = webui_server.make_server(load_settings(tmp_path), environ={}, port=0)
    try:
        constructed = []
        entered = threading.Event()
        second_entered = threading.Event()
        release = threading.Event()

        def fake_service(_data):
            instance = object()
            constructed.append(instance)
            if len(constructed) > 1:
                second_entered.set()
            entered.set()
            release.wait(timeout=10)
            return instance

        monkeypatch.setattr(
            "print_handy.bot.web.WebChatService", fake_service)
        results = {}

        def call(name):
            handler = webui_server._Handler.__new__(webui_server._Handler)
            handler.server = srv
            results[name] = handler._chat_service()

        first = threading.Thread(target=call, args=("first",))
        first.start()
        assert entered.wait(timeout=10)
        second = threading.Thread(target=call, args=("second",))
        second.start()
        # Unguarded, the second caller enters the constructor too and
        # this returns at once; guarded, it blocks on the build lock
        # until the first service exists, so the wait just times out.
        second_entered.wait(timeout=2)
        release.set()
        first.join(timeout=10)
        second.join(timeout=10)
        assert not first.is_alive() and not second.is_alive()
        assert len(constructed) == 1
        assert results["first"] is results["second"] is constructed[0]
        assert srv.chat_service is constructed[0]
    finally:
        srv.server_close()


def test_chat_requires_the_access_token_when_one_is_set(tmp_path):
    data = load_settings(tmp_path)
    srv = webui_server.make_server(
        data, environ={"WEBUI_ACCESS_TOKEN": "sekrit"}, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        port = srv.server_address[1]
        status, _page = call(port, "GET", "/chat")
        assert status != 200
        status, page = call(port, "GET", "/chat",
                            headers={"X-WebUI-Token": "sekrit"})
        assert status == 200 and "Message" in page
    finally:
        srv.shutdown()
