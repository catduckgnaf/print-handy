"""Bounded Discord startup retries for transient network failures (#364)."""
import errno
import logging
import socket
from unittest import mock

import aiohttp
import discord
import pytest
from aiohttp.client_reqrep import ConnectionKey

from print_handy.bot import client
from print_handy.hermes import _config


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(_config, "discord_token", lambda: "test-token")


def _dns_error():
    key = ConnectionKey("discord.com", 443, True, None, None, None, None)
    return aiohttp.ClientConnectorDNSError(
        key, socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"))


def _bot_factory(monkeypatch, outcomes):
    instances = []

    class FakeBot:
        def __init__(self, cfg):
            self.cfg = cfg
            instances.append(self)

        def run(self, token, log_handler=None):
            assert token == "test-token"
            assert log_handler is None
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome

    monkeypatch.setattr(client, "PrintHandyBot", FakeBot)
    return instances


def _fast_retries(monkeypatch):
    sleeps = []
    monkeypatch.setattr(client, "_startup_retry_delay", lambda attempt: float(attempt))
    monkeypatch.setattr(client, "_startup_sleep", sleeps.append)
    return sleeps


def _retry_lines(caplog):
    return [record.getMessage() for record in caplog.records
            if "DISCORD_STARTUP_RETRY" in record.getMessage()]


def test_transient_dns_failures_then_success_starts_a_fresh_bot_each_time(monkeypatch, caplog, cfg):
    instances = _bot_factory(monkeypatch, [
        socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
        _dns_error(),
        None,
    ])
    sleeps = _fast_retries(monkeypatch)

    with caplog.at_level(logging.WARNING, logger=client.__name__):
        assert client.run_bot(cfg) == 0

    assert len(instances) == 3
    assert len({id(bot) for bot in instances}) == 3  # a closed discord.py client is not reused
    assert sleeps == [1.0, 2.0]
    lines = _retry_lines(caplog)
    assert len(lines) == 2
    assert "attempt 1/5" in lines[0] and "gaierror" in lines[0] and "errno=-3" in lines[0]
    assert "attempt 2/5" in lines[1] and "ClientConnectorDNSError" in lines[1] and "errno=-3" in lines[1]


def test_transient_failure_on_every_attempt_reraises_after_the_bounded_count(monkeypatch, caplog, cfg):
    instances = _bot_factory(monkeypatch, [
        socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        for _ in range(client.STARTUP_ATTEMPTS)
    ])
    sleeps = _fast_retries(monkeypatch)

    with caplog.at_level(logging.WARNING, logger=client.__name__):
        with pytest.raises(socket.gaierror):
            client.run_bot(cfg)

    assert len(instances) == client.STARTUP_ATTEMPTS == 5
    assert sleeps == [1.0, 2.0, 3.0, 4.0]
    lines = _retry_lines(caplog)
    assert len(lines) == client.STARTUP_ATTEMPTS
    assert "attempt 5/5" in lines[-1] and "no retries left" in lines[-1]


def test_exhausted_retry_line_carries_the_taxonomy_marker(monkeypatch, caplog, cfg):
    """#390 Stage 2: the final startup failure is findable by its PH_ marker."""
    _bot_factory(monkeypatch, [
        socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        for _ in range(client.STARTUP_ATTEMPTS)
    ])
    _fast_retries(monkeypatch)

    with caplog.at_level(logging.WARNING, logger=client.__name__):
        with pytest.raises(socket.gaierror):
            client.run_bot(cfg)

    assert "PH_DISCORD_TRANSIENT" in _retry_lines(caplog)[-1]


def test_login_failure_exits_3_immediately_without_retry(monkeypatch, cfg):
    instances = _bot_factory(monkeypatch, [discord.LoginFailure("bad token")])
    sleeps = _fast_retries(monkeypatch)

    assert client.run_bot(cfg) == 3
    assert len(instances) == 1
    assert sleeps == []


def test_privileged_intents_exits_3_immediately_without_retry(monkeypatch, cfg):
    instances = _bot_factory(monkeypatch, [discord.PrivilegedIntentsRequired(0)])
    sleeps = _fast_retries(monkeypatch)

    assert client.run_bot(cfg) == 3
    assert len(instances) == 1
    assert sleeps == []


def test_non_transient_error_is_not_retried(monkeypatch, cfg):
    instances = _bot_factory(monkeypatch, [ValueError("programming error")])
    sleeps = _fast_retries(monkeypatch)

    with pytest.raises(ValueError, match="programming error"):
        client.run_bot(cfg)
    assert len(instances) == 1
    assert sleeps == []


def test_startup_retry_delay_schedule_is_bounded(monkeypatch):
    monkeypatch.setattr(client.random, "uniform", lambda low, high: (low + high) / 2)
    delays = [client._startup_retry_delay(attempt) for attempt in range(1, client.STARTUP_ATTEMPTS)]
    assert delays == [10.0, 20.0, 40.0, 80.0]
    assert sum(delays) == 150.0


def _http_error(status):
    return discord.HTTPException(mock.Mock(status=status, reason="test"), {"message": "oops"})


def test_dns_classification_survives_an_aiohttp_without_the_dns_error_class(monkeypatch):
    """aiohttp before 3.10 has no ClientConnectorDNSError (P2-136), and
    requirements still permit it through discord.py: with the class absent,
    the classifier must not raise AttributeError (which masked the original
    startup failure), and a DNS failure — which then arrives as a plain
    connector error wrapping the gaierror — still classifies through its
    OS error."""
    monkeypatch.setattr(client, "_DNS_ERROR", None)
    key = ConnectionKey("discord.com", 443, True, None, None, None, None)
    dns = aiohttp.ClientConnectorError(
        key, socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"))
    assert client._is_transient_startup_error(dns)
    assert not client._is_transient_startup_error(ValueError("programming error"))


def test_transient_startup_error_classification():
    key = ConnectionKey("discord.com", 443, True, None, None, None, None)

    assert client._is_transient_startup_error(_dns_error())
    assert client._is_transient_startup_error(aiohttp.ClientConnectorError(
        key, ConnectionRefusedError(errno.ECONNREFUSED, "connection refused")))
    assert not client._is_transient_startup_error(aiohttp.ClientConnectorError(
        key, PermissionError(errno.EACCES, "permission denied")))
    assert client._is_transient_startup_error(TimeoutError("timed out"))
    assert client._is_transient_startup_error(aiohttp.ServerTimeoutError("timed out"))
    assert client._is_transient_startup_error(_http_error(503))
    assert client._is_transient_startup_error(_http_error(429))
    assert not client._is_transient_startup_error(_http_error(403))
    assert not client._is_transient_startup_error(discord.LoginFailure("bad token"))
    assert not client._is_transient_startup_error(ValueError("programming error"))


# --- #391 expansion: the classification branches #369's first tests missed ---
#
# #369's tests pinned DNS, a refused connection, a permission-denied
# connector error, timeouts, HTTP 429/503/403 and the login/intent exits.
# The classifier has more branches than that, and each one decides whether
# a container start retries or dies: certificate failures (configuration,
# never weather), gateway discovery, websocket handshake statuses, generic
# connection errors, and the whole transient-errno table. Pin them all so
# a future edit to the classifier cannot silently flip one.

import ssl


def _cert_error():
    key = ConnectionKey("discord.com", 443, True, None, None, None, None)
    return aiohttp.ClientConnectorCertificateError(
        key, ssl.CertificateError("self-signed certificate"))


def _ws_handshake_error(status):
    return aiohttp.WSServerHandshakeError(None, (), status=status, message="oops")


@pytest.mark.parametrize("errno_value", [
    socket.EAI_AGAIN, errno.EAGAIN, errno.EWOULDBLOCK, errno.ECONNABORTED,
    errno.ECONNREFUSED, errno.ECONNRESET, errno.EHOSTUNREACH,
    errno.ENETUNREACH, errno.ETIMEDOUT,
])
def test_every_transient_errno_classifies_transient(errno_value):
    assert client._is_transient_startup_error(OSError(errno_value, "network weather"))


@pytest.mark.parametrize("errno_value", [
    errno.EACCES, errno.EPERM, errno.EINVAL, errno.ENOENT, errno.ENOSPC,
])
def test_non_transient_errnos_do_not_classify_transient(errno_value):
    assert not client._is_transient_startup_error(OSError(errno_value, "local fault"))


def test_an_os_error_without_an_errno_is_not_transient():
    assert not client._is_transient_startup_error(OSError("no errno attached"))


def test_certificate_failure_is_configuration_never_weather():
    """A bad certificate means the operator's TLS setup is wrong; retrying
    five times would only delay the actionable failure (#364 docstring)."""
    assert not client._is_transient_startup_error(_cert_error())


def test_connector_error_inherits_its_os_errors_classification():
    key = ConnectionKey("discord.com", 443, True, None, None, None, None)
    refused = aiohttp.ClientConnectorError(
        key, ConnectionRefusedError(errno.ECONNREFUSED, "connection refused"))
    assert client._is_transient_startup_error(refused)
    denied = aiohttp.ClientConnectorError(
        key, PermissionError(errno.EACCES, "permission denied"))
    assert not client._is_transient_startup_error(denied)
    causeless = aiohttp.ClientConnectorError.__new__(aiohttp.ClientConnectorError)
    causeless._os_error = None  # a connector error carrying no OS cause
    assert not client._is_transient_startup_error(causeless)


def test_gateway_discovery_and_generic_connection_errors_are_transient():
    assert client._is_transient_startup_error(discord.GatewayNotFound())
    assert client._is_transient_startup_error(aiohttp.ClientConnectionError("reset by peer"))
    assert client._is_transient_startup_error(aiohttp.ServerDisconnectedError("gone"))


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_http_statuses_classify_transient(status):
    assert client._is_transient_startup_error(_http_error(status))
    assert client._is_transient_startup_error(_ws_handshake_error(status))


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_http_statuses_do_not_classify_transient(status):
    assert not client._is_transient_startup_error(_http_error(status))
    assert not client._is_transient_startup_error(_ws_handshake_error(status))


def test_certificate_failure_is_not_retried_by_run_bot(monkeypatch, cfg):
    instances = _bot_factory(monkeypatch, [_cert_error()])
    sleeps = _fast_retries(monkeypatch)

    with pytest.raises(aiohttp.ClientConnectorCertificateError):
        client.run_bot(cfg)
    assert len(instances) == 1
    assert sleeps == []


def test_gateway_not_found_is_retried_by_run_bot(monkeypatch, cfg):
    instances = _bot_factory(monkeypatch, [discord.GatewayNotFound(), None])
    sleeps = _fast_retries(monkeypatch)

    assert client.run_bot(cfg) == 0
    assert len(instances) == 2
    assert sleeps == [1.0]


def test_retry_delay_stays_inside_the_jitter_bounds_and_the_cap(monkeypatch):
    seen = []

    def uniform(low, high):
        seen.append((low, high))
        return high

    monkeypatch.setattr(client.random, "uniform", uniform)
    delays = [client._startup_retry_delay(attempt) for attempt in (1, 2, 3, 4, 9)]
    # Nominal 10/20/40/80 s, jittered +/-20%; attempt 9 is still capped at 80.
    assert seen == [(8.0, 12.0), (16.0, 24.0), (32.0, 48.0), (64.0, 96.0), (64.0, 96.0)]
    assert delays == [12.0, 24.0, 48.0, 96.0, 96.0]
