"""Hermes helper tests never touch a live printer, the LAN, Discord, Spoolman, or the real profile.

The settings the helpers read are replaced, at import time, by a test copy of the checked-in home config with
fake `.test` hosts and temp directories. Per test guards then refuse non-GET calls to Moonraker, stub
the screen cancel reboot, and make sockets raise for any private-range IP or configured service host.
"""
import ipaddress
import os
import socket
import sys
import tempfile
import threading
import types
import urllib.parse
from pathlib import Path

import pytest

from print_handy import settings
from print_handy.runtime import _config

TEST_SETTINGS = _config.cfg()

_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _service_hosts():
    hosts = set()
    sources = [TEST_SETTINGS]
    try:
        sources.append(settings.current())
    except Exception:
        pass
    for data in sources:
        for key, value in data["services"].items():
            if key.endswith("_url") and value:
                hosts.add(urllib.parse.urlsplit(value).hostname)
        if data["timelapse"]["archive_host"]:
            hosts.add(data["timelapse"]["archive_host"])
    return hosts


def _is_blocked(host):
    if not isinstance(host, str) or not host:
        return False
    host = host.strip("[]").lower()
    if host in _service_hosts() or host.endswith(".test"):  # .test hosts are the fake service names
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (addr.is_private and not addr.is_loopback) or addr.is_link_local or addr in _CGNAT


@pytest.fixture(autouse=True)
def _no_live_network(monkeypatch):
    """urllib and websockets reach the network through these socket calls; refuse live hosts."""
    def refuse(host):
        raise AssertionError(f"test tried to reach live host {host!r}")

    real_gai = socket.getaddrinfo
    real_cc = socket.create_connection
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def getaddrinfo(host, *a, **k):
        if _is_blocked(host):
            refuse(host)
        return real_gai(host, *a, **k)

    def create_connection(address, *a, **k):
        if _is_blocked(address[0]):
            refuse(address[0])
        return real_cc(address, *a, **k)

    def connect(self, address):
        if isinstance(address, tuple) and _is_blocked(address[0]):
            refuse(address[0])
        return real_connect(self, address)

    def connect_ex(self, address):
        if isinstance(address, tuple) and _is_blocked(address[0]):
            refuse(address[0])
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


@pytest.fixture(autouse=True)
def _neutral_printer_hardware(monkeypatch):
    """Tests that read the hardware record get the neutral AD5X fixture, never the home machine's."""
    from print_handy.core import printer_state
    monkeypatch.setattr(printer_state, "save", lambda *a, **k: None)
    monkeypatch.setattr(printer_state, "STATE", str(Path(__file__).parents[1] / "pipeline/fixtures/printer_hardware.json"))


@pytest.fixture(autouse=True)
def _scratch_root_is_not_an_ancestor(tmp_path_factory, monkeypatch):
    """print_now stops its JOB.json search at $TMPDIR. Keep pytest's per test dirs out from under it."""
    monkeypatch.setenv("TMPDIR", str(tmp_path_factory.mktemp("scratch-root")))


@pytest.fixture(autouse=True)
def _never_touch_live_printer(monkeypatch, request):
    """No test may send a POST to Moonraker or auto restart the printer."""
    try:
        from print_handy.runtime import print_control
    except Exception:
        print_control = None
    if print_control is not None:
        real = print_control._call

        def guarded(path, method="GET", data=None, timeout=10):
            if method != "GET":
                raise RuntimeError(f"test tried to {method} {path} on the live printer")
            return real(path, method, data, timeout)
        monkeypatch.setattr(print_control, "_call", guarded)
    if "test_screen_cancel_reboot_paths" not in request.node.name:
        try:
            from print_handy.runtime import moonraker_event_listener as mel
            monkeypatch.setattr(mel, "screen_cancel_reboot", lambda *a, **k: (False, "test stub"))
        except Exception:
            pass


@pytest.fixture(scope="package", autouse=True)
def _clarify_gateway_stand_in():
    """A `tools.clarify_gateway` name may still be imported by test fakes
    that answer questions the way the old host module did; the module
    installed here delegates every call to the one real registry,
    print_handy.core.prompts, so a question registered by the code under
    test and an answer delivered through the fake land in the same book.

    Package scope, not session: the stand-in must leave with tests/runtime.
    """
    try:
        import tools.clarify_gateway  # noqa: F401
        yield
        return
    except ImportError:
        pass
    from print_handy.core import prompts as core_prompts
    module = types.ModuleType("tools.clarify_gateway")
    module.CANCELLED = core_prompts.CANCELLED
    module.register = core_prompts.register
    module.resolve_gateway_clarify = core_prompts.resolve_gateway_clarify
    module.wait_for_response = core_prompts.wait_for_response
    module.is_open = core_prompts.is_open
    package = sys.modules.get("tools") or types.ModuleType("tools")
    package.clarify_gateway = module
    sys.modules["tools"] = package
    sys.modules["tools.clarify_gateway"] = module
    yield
    sys.modules.pop("tools.clarify_gateway", None)
    sys.modules.pop("tools", None)
