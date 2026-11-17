"""Issue #1588: the remaining Web UI review items.

1. A first X-Forwarded-For request with empty trusted_proxies logs one
   warning (the shared-lockout trap behind a reverse proxy).
2. A request with an unsupported Transfer-Encoding (chunked, gzip)
   is answered 411 and the connection closes, instead of the body
   being read as length 0 and left on the keep-alive connection; the
   identity coding applies no coding, so a request carrying it beside
   a valid Content-Length is served normally.
3. One CSRF token kind: the Wrapped routes no longer accept the
   per-process form token beside a session; the page embeds the
   session-bound token like every other page.
4. Session state lives in a SessionStore with mint/validate/revoke and
   its own lock; the server's old attribute names delegate to it.
5. One _read_body reader backs _read_form and the two JSON API blocks.
"""
import http.client
import logging
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest

from print_handy import settings
from print_handy.webui import pages, server, spools as spools_module

TOKEN = "test-token-1588"


def load_settings(tmp, extra="", environ=None):
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  state_home: {tmp}/hermes\n"
                     "webui:\n  enabled: true\n  port: 8765\n" + extra)
    return settings.load(local=local, environ=environ or {})


def start(data, environ=None):
    httpd = server.make_server(data, environ=environ or {}, port=0,
                               snapshot_func=lambda: {})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        hdrs = dict(headers or {})
        if body is not None:
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        conn.request(method, path, body=body, headers=hdrs)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


# -- item 4: the SessionStore itself ------------------------------------------

def test_session_store_mint_validate_revoke():
    store = server.SessionStore()
    session_id = store.mint()
    assert store.validate(session_id) == session_id
    assert store.validate("no-such-session") == ""
    assert store.validate("") == ""
    assert store.revoke(session_id) is False  # nothing remembered to persist
    assert store.validate(session_id) == ""


def test_session_store_expiry_and_remembered_sessions():
    store = server.SessionStore()
    now = time.time()
    old = store.mint("old-session", now=now - server.SESSION_TTL_S - 10)
    assert store.validate(old, now=now) == ""
    remembered = "remembered-session"
    store.remember(remembered, now=now)
    assert store.validate(remembered, now=now) == remembered
    assert store.snapshot_remembered() == {
        server._session_digest(remembered): now + server.REMEMBER_TTL_S}
    assert store.revoke(remembered) is True  # a remembered entry went
    assert store.validate(remembered, now=now) == ""


def test_session_store_rotation_changes_both_token_kinds():
    store = server.SessionStore()
    session_id = store.mint()
    process_token = store.form_token
    bound = store.form_token_for(session_id)
    store.rotate_tokens()
    assert store.form_token != process_token
    assert store.form_token_for(session_id) != bound
    assert store.validate(session_id) == session_id  # sessions survive rotation


def test_server_session_attributes_delegate_to_the_store(tmp_path):
    data = load_settings(tmp_path)
    httpd, _port = start(data)
    try:
        assert isinstance(httpd.session_store, server.SessionStore)
        assert httpd.sessions is httpd.session_store.sessions
        assert httpd.remembered_sessions is httpd.session_store.remembered_sessions
        assert httpd.form_token == httpd.session_store.form_token
        assert httpd.remembered_lock is httpd.session_store.lock
        session_id = httpd.session_store.mint()
        assert httpd.sessions[session_id] > time.time()
    finally:
        httpd.shutdown()
        httpd.server_close()


# -- item 2: chunked bodies -----------------------------------------------------

def _raw_request(port, request_bytes):
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(request_bytes)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break  # the server closed the connection
            chunks.append(chunk)
    return b"".join(chunks)


def test_chunked_post_is_411_and_the_connection_closes(tmp_path):
    data = load_settings(tmp_path)
    httpd, port = start(data)
    try:
        response = _raw_request(port, (
            b"POST /login HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello\r\n0\r\n\r\n"))
        status_line = response.split(b"\r\n", 1)[0]
        assert b" 411 " in status_line
        assert b"chunked" in response
        # A fresh connection still serves normally afterwards.
        code, _headers, _body = call(port, "GET", "/healthz")
        assert code in (200, 503)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_gzip_transfer_encoding_is_411_and_the_connection_closes(tmp_path):
    data = load_settings(tmp_path)
    httpd, port = start(data)
    try:
        response = _raw_request(port, (
            b"POST /login HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\n"
            b"Content-Length: 5\r\n"
            b"Transfer-Encoding: gzip\r\n\r\n"
            b"hello"))
        status_line = response.split(b"\r\n", 1)[0]
        assert b" 411 " in status_line
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_identity_transfer_encoding_with_a_content_length_is_served(tmp_path):
    """duckcatgnaf review, PR #1594: the identity coding applies no coding,
    so a request carrying the header beside a valid Content-Length is an
    otherwise valid request and must not be refused with 411."""
    data = load_settings(tmp_path)
    httpd, port = start(data)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.putrequest("GET", "/healthz")
            conn.putheader("Content-Length", "0")
            conn.putheader("Transfer-Encoding", "identity")
            conn.endheaders()
            response = conn.getresponse()
            response.read()
            assert response.status in (200, 503)
        finally:
            conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


# -- item 1: the untrusted-proxy warning -----------------------------------------

def test_forwarded_for_without_trusted_proxies_warns_once(tmp_path, caplog):
    data = load_settings(tmp_path)
    httpd, port = start(data)
    try:
        with caplog.at_level(logging.WARNING, logger="print_handy.webui.server"):
            for _ in range(3):
                call(port, "GET", "/healthz",
                     headers={"X-Forwarded-For": "203.0.113.7"})
        warnings = [r for r in caplog.records
                    if "trusted_proxies" in r.getMessage()]
        assert len(warnings) == 1
        assert "X-Forwarded-For" in warnings[0].getMessage()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_no_forwarded_header_no_warning(tmp_path, caplog):
    data = load_settings(tmp_path)
    httpd, port = start(data)
    try:
        with caplog.at_level(logging.WARNING, logger="print_handy.webui.server"):
            call(port, "GET", "/healthz")
        assert [r for r in caplog.records
                if "trusted_proxies" in r.getMessage()] == []
    finally:
        httpd.shutdown()
        httpd.server_close()


# -- item 3: one CSRF token kind ---------------------------------------------------

def test_wrapped_posts_refuse_the_process_token_beside_a_session(tmp_path):
    data = load_settings(tmp_path, "timelapse:\n  wrapped_enabled: true\n")
    httpd, port = start(data, environ={"WEBUI_ACCESS_TOKEN": TOKEN})
    try:
        session_id = "wrapped-1588-session"
        httpd.sessions[session_id] = time.time() + 3600
        cookie = {"Cookie": f"{server.SESSION_COOKIE}={session_id}"}
        form_headers = {"Content-Type": "application/x-www-form-urlencoded",
                        **cookie}
        for path in ("/wrapped/video", "/wrapped/share"):
            # The per-process token beside a live session: refused now
            # (before #1588 the process_token flag accepted it).
            code, _h, _b = call(
                port, "POST", path,
                urlencode({pages.CSRF_FIELD: httpd.form_token, "year": "abc"}),
                form_headers)
            assert code == 403, path
            # The session-bound token the page now embeds passes the
            # policy and reaches the handler (which refuses the year).
            bound = server.session_form_token(httpd, session_id)
            code, _h, body = call(
                port, "POST", path,
                urlencode({pages.CSRF_FIELD: bound, "year": "abc"}),
                form_headers)
            assert code == 400, path
            assert b"whole number" in body
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_no_route_declares_a_second_token_kind():
    assert not any(hasattr(route, "process_token")
                   for route in __import__("print_handy.webui.routes",
                                           fromlist=["ROUTES"]).ROUTES)


# -- item 5: one body reader --------------------------------------------------------

def test_json_blocks_share_the_body_reader(tmp_path):
    data = load_settings(tmp_path)
    httpd, port = start(data, environ={"WEBUI_ACCESS_TOKEN": TOKEN})
    try:
        auth = {server.TOKEN_HEADER: TOKEN}
        # The control block: a non-JSON content type is a 415, an
        # oversized body a 400, a non-object body a 400.
        code, _h, raw = call(port, "POST", "/api/printers/p1/pause", b"{}",
                             {**auth, "Content-Type": "text/plain"})
        assert code == 415
        code, _h, raw = call(port, "POST", "/api/printers/p1/pause",
                             b'{"x": "' + b"a" * 5000 + b'"}',
                             {**auth, "Content-Type": "application/json"})
        assert code == 400
        assert b"Nothing sent." in raw
        # The spools block: the same reader, its own refusal tail.
        action = spools_module.ACTIONS[0]
        code, _h, raw = call(port, "POST", f"/api/printers/p1/spools/{action}",
                             b"[1, 2]",
                             {**auth, "Content-Type": "application/json"})
        assert code == 400
        assert b"JSON object" in raw and b"Nothing changed." in raw
        # The form reader: an oversized form body is still a 413.
        code, _h, _b = call(port, "POST", "/login", "x=" + "a" * 70000)
        assert code == 413
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
