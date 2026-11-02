"""DNS-rebinding pinning for direct file links (P1-114).

assert_public_url validates one DNS answer, but the fetch used to resolve
the hostname again at connect time, so a hostname answering public first
and loopback second steered the download at an internal address. These
tests run the REAL transport (_open_once and the pinned connection
classes) against a stubbed socket layer: model_job._resolve_host_ips is
the validation-time view of DNS, socket.getaddrinfo is the connect-time
view, and they deliberately disagree.
"""
import io
import ipaddress
import socket

import pytest

from print_handy.core import model_job

PUBLIC_IP = "93.184.216.34"
LOOPBACK = "127.0.0.1"
BODY = b"solid direct\nendsolid direct\n"


class FakeSocket:
    """Just enough of a socket for http.client to send a request and
    read a canned HTTP/1.1 response."""

    def __init__(self, payload):
        self._buf = io.BytesIO(payload)
        self.sent = b""

    def sendall(self, data):
        self.sent += data

    def makefile(self, mode, buffering=None, **kwargs):
        return self._buf

    def setsockopt(self, *args):
        pass

    def settimeout(self, timeout):
        pass

    def gettimeout(self):
        return None

    def close(self):
        pass


def _http_response(body):
    return (b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)


@pytest.fixture
def fake_transport(monkeypatch):
    """Stub socket.create_connection; record the address actually dialed.

    A hostname target is resolved through socket.getaddrinfo exactly the
    way the real create_connection resolves it, so a transport that looks
    the name up again lands wherever getaddrinfo currently points."""
    dialed = []
    sockets = []

    def fake_create_connection(address, timeout=None, source_address=None):
        host, port = address[0], address[1]
        try:
            ipaddress.ip_address(host)
            effective = host
        except ValueError:
            info = socket.getaddrinfo(host, port)
            effective = info[0][4][0]
        dialed.append((effective, port))
        sock = FakeSocket(_http_response(BODY))
        sockets.append(sock)
        return sock

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    return dialed, sockets


def _getaddrinfo_loopback(host, port, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (LOOPBACK, port or 0))]


def test_open_once_dials_the_validated_address_not_a_rebound_one(monkeypatch, fake_transport):
    dialed, sockets = fake_transport
    # Validation sees a public address; any later lookup rebinds to loopback.
    monkeypatch.setattr(model_job, "_resolve_host_ips", lambda host: {PUBLIC_IP})
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_loopback)

    response = model_job._open_once("http://files.example/model.stl")

    assert response.read() == BODY
    # The one connection went to the validated public address, never loopback,
    # while the request still carried the original hostname as its Host header.
    assert dialed == [(PUBLIC_IP, 80)]
    assert b"Host: files.example" in sockets[0].sent


def test_fetch_refuses_when_resolution_rebinds_to_private(monkeypatch, fake_transport):
    dialed, _sockets = fake_transport
    answers = iter([{PUBLIC_IP}, {LOOPBACK}])
    monkeypatch.setattr(model_job, "_resolve_host_ips",
                        lambda host: next(answers, {LOOPBACK}))
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_loopback)

    with pytest.raises(model_job.DirectLinkError, match="not a public address"):
        model_job._fetch_direct_response("https://files.example/model.stl")
    assert dialed == []  # nothing connected anywhere


def test_pinned_https_connection_preserves_tls_server_name(monkeypatch):
    dialed = []
    wrapped = []

    class FakeContext:
        def wrap_socket(self, sock, server_hostname=None):
            wrapped.append(server_hostname)
            return sock

    monkeypatch.setattr(
        socket, "create_connection",
        lambda address, timeout=None, source_address=None:
            dialed.append(address) or FakeSocket(b""))
    conn = model_job._PinnedHTTPSConnection(
        "files.example", port=8443, _pinned_ip=PUBLIC_IP,
        context=FakeContext())
    conn.connect()

    assert dialed == [(PUBLIC_IP, 8443)]       # socket target is the validated IP
    assert wrapped == ["files.example"]        # TLS SNI / cert name stays the hostname
    assert conn.host == "files.example"        # and so does the Host header source
