"""#1844: the suite-wide guard in tests/conftest.py refuses public TCP and UDP.

Each case names a public address that is never contacted: the guard
refuses before any packet leaves. The test clears the
guard's record afterwards so the refusal it provoked does not fail it.
"""
import socket

import pytest

PUBLIC = ("1.1.1.1", 53)
PUBLIC6 = ("2606:4700:4700::1111", 53)
LOCAL = ("127.0.0.1", 9)


@pytest.fixture()
def guard(_no_public_internet):
    yield _no_public_internet
    _no_public_internet.clear()


def test_public_tcp_connect_is_refused(guard):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(OSError, match="public internet"):
            sock.connect(PUBLIC)
        assert sock.connect_ex(PUBLIC) != 0
    assert guard == ["1.1.1.1:53", "1.1.1.1:53"]


def test_public_udp_sendto_is_refused(guard):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(OSError, match="public internet"):
            sock.sendto(b"x", PUBLIC)
        with pytest.raises(OSError, match="public internet"):
            sock.sendto(b"x", 0, PUBLIC)
    assert guard == ["1.1.1.1:53", "1.1.1.1:53"]


def test_public_udp_sendmsg_is_refused(guard):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(OSError, match="public internet"):
            sock.sendmsg([b"x"], [], 0, PUBLIC)
    assert guard == ["1.1.1.1:53"]


def test_connected_udp_send_to_a_public_peer_is_refused(guard):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(PUBLIC)  # sends nothing: allowed
        with pytest.raises(OSError, match="public internet"):
            sock.send(b"x")
        with pytest.raises(OSError, match="public internet"):
            sock.sendall(b"x")
    assert guard == ["1.1.1.1:53", "1.1.1.1:53"]


def test_public_ipv6_is_refused(guard):
    if not socket.has_ipv6:
        pytest.skip("no IPv6 on this host")
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
        with pytest.raises(OSError, match="public internet"):
            sock.sendto(b"x", PUBLIC6)
    assert guard == ["2606:4700:4700::1111:53"]


def test_loopback_udp_is_untouched(guard):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b"ping", receiver.getsockname())
        assert receiver.recv(16) == b"ping"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(b"x", LOCAL)
    assert guard == []
