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
                                   "clarify_id": "nope", "index": "0"})
    status, _page = call(port, "POST", "/chat/choice", body=body)
    assert status == 303


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
