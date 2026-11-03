"""spoolman_client over real sockets (#387).

The pipeline suite forbids real LAN access, so the one client's behaviour
against actual transports — connection refused, HTTP 500, a hanging
server, a healthy answer — is proven here against loopback servers and a
closed port. Loopback only; nothing leaves this machine.
"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from print_handy.core import spoolman_client


def refused_url():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


class _Server:
    def __init__(self, handler):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _handler(status=200, payload=None, delay=0.0):
    class Handler(BaseHTTPRequestHandler):
        def _answer(self):
            if delay:
                import time
                time.sleep(delay)
            body = json.dumps(payload if payload is not None else {}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _answer
        do_POST = _answer
        do_PATCH = _answer

        def log_message(self, *args):
            pass

    return Handler


def test_connection_refused_raises_unavailable_and_best_effort_returns_none():
    url = refused_url()
    with pytest.raises(spoolman_client.SpoolmanUnavailable) as caught:
        spoolman_client.request("GET", "/api/v1/spool/7", base_url=url, timeout=2)
    assert "Connection refused" in str(caught.value)
    assert spoolman_client.call("GET", "/api/v1/spool/7", base_url=url, timeout=2) is None
    assert spoolman_client.get_spool(7, base_url=url, timeout=2) is None
    assert spoolman_client.list_spools(base_url=url, timeout=2) is None
    ok, detail = spoolman_client.health(base_url=url, timeout=2)
    assert ok is False and "Connection refused" in detail


def test_http_500_is_unavailable():
    server = _Server(_handler(status=500, payload={"error": "boom"}))
    try:
        with pytest.raises(spoolman_client.SpoolmanUnavailable):
            spoolman_client.request("GET", "/api/v1/spool/7", base_url=server.url, timeout=2)
        assert spoolman_client.get_spool(7, base_url=server.url, timeout=2) is None
        assert spoolman_client.health(base_url=server.url, timeout=2)[0] is False
    finally:
        server.stop()


def test_unavailable_carries_the_http_status():
    # #839: callers must tell a 404 (no such spool) from an outage (5xx),
    # and a transport failure carries no status at all.
    server = _Server(_handler(status=404, payload={"error": "no such spool"}))
    try:
        with pytest.raises(spoolman_client.SpoolmanUnavailable) as caught:
            spoolman_client.request("GET", "/api/v1/spool/99", base_url=server.url, timeout=2)
        assert caught.value.status == 404
    finally:
        server.stop()
    server = _Server(_handler(status=500, payload={"error": "boom"}))
    try:
        with pytest.raises(spoolman_client.SpoolmanUnavailable) as caught:
            spoolman_client.request("GET", "/api/v1/spool/7", base_url=server.url, timeout=2)
        assert caught.value.status == 500
    finally:
        server.stop()
    with pytest.raises(spoolman_client.SpoolmanUnavailable) as caught:
        spoolman_client.request("GET", "/api/v1/spool/7", base_url=refused_url(), timeout=2)
    assert caught.value.status is None
    with pytest.raises(spoolman_client.SpoolmanUnavailable) as caught:
        spoolman_client.request("GET", "/api/v1/spool/7", base_url="")
    assert caught.value.status is None


def test_timeout_is_unavailable():
    server = _Server(_handler(payload={}, delay=3.0))
    try:
        with pytest.raises(spoolman_client.SpoolmanUnavailable):
            spoolman_client.request("GET", "/api/v1/spool/7", base_url=server.url, timeout=0.3)
    finally:
        server.stop()


def test_success_round_trip_and_health():
    spool = {"id": 7, "remaining_weight": 250.0,
             "filament": {"name": "Black PLA", "material": "PLA"}}
    server = _Server(_handler(payload=spool))
    try:
        assert spoolman_client.get_spool(7, base_url=server.url) == spool
        assert spoolman_client.health(base_url=server.url) == (True, "HTTP 200")
    finally:
        server.stop()


def test_unconfigured_is_unavailable_not_a_crash():
    assert spoolman_client.get_spool(7, base_url="") is None
    assert spoolman_client.health(base_url="")[0] is False
    with pytest.raises(spoolman_client.SpoolmanUnavailable):
        spoolman_client.request("GET", "/api/v1/spool/7", base_url="")


def test_malformed_base_url_is_unavailable_not_a_value_error():
    # P2-154: a configured URL with a malformed authority passes the
    # prefix-only settings validation, and urllib.request.Request raises
    # ValueError while *constructing* the request. That failure is an
    # unavailability outcome (Spoolman bookkeeping must stay non-blocking),
    # never an exception escaping get_spool()/call().
    with pytest.raises(spoolman_client.SpoolmanUnavailable):
        spoolman_client.request("GET", "/api/v1/spool/1", base_url="http://[::1")
    assert spoolman_client.get_spool(1, base_url="http://[::1") is None
    assert spoolman_client.call("GET", "/api/v1/spool/1", base_url="http://[::1") is None


def test_notice_is_emitted_exactly_once_until_reset(capsys):
    spoolman_client.reset_notice()
    assert spoolman_client.note_unavailable_once() is True
    assert spoolman_client.note_unavailable_once() is False
    assert capsys.readouterr().out == spoolman_client.UNAVAILABLE_NOTICE + "\n"
    spoolman_client.reset_notice()
    assert spoolman_client.note_unavailable_once() is True
    spoolman_client.reset_notice()
