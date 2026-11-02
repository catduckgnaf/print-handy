"""Pipeline tests never reach a live printer, a LAN service, or the real hardware record."""
import ipaddress
import sys
import threading
import types
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from print_handy import settings
from print_handy.core import printer_state

REAL_SLICER = "PRINT_HANDY_REAL_SLICER"
FIXTURES = Path(__file__).parent / "fixtures"


def _blocked_hosts():
    hosts = set()
    for key, value in settings.current()["services"].items():
        if key.endswith("_url") and value:
            hosts.add(urllib.parse.urlsplit(value).hostname)
    return hosts


@pytest.fixture(autouse=True)
def _never_write_live_printer_state(monkeypatch):
    saved = []
    monkeypatch.setattr(printer_state, "save", saved.append)
    return saved


@pytest.fixture(autouse=True)
def _neutral_printer_hardware(monkeypatch):
    """Tests that read the hardware record get a neutral AD5X record, never the home machine's."""
    monkeypatch.setattr(printer_state, "STATE", str(FIXTURES / "printer_hardware.json"))


@pytest.fixture(autouse=True)
def _home_slicer_commands(monkeypatch):
    """A machine's YAML or TOML local config may point at another slicer; these tests assert the home commands."""
    from print_handy.core import orca_adapter
    home = settings.load(local=False, environ={})
    monkeypatch.setattr(orca_adapter, "ORCA_COMMAND", list(home["orca"]["command"]))
    monkeypatch.setattr(orca_adapter, "BAMBU_COMMAND", list(home["bambu"]["command"]))


@pytest.fixture(autouse=True)
def _no_live_lan(monkeypatch):
    """Fail loudly if a test reaches a private-range address or a configured service over HTTP."""
    real = urllib.request.urlopen
    blocked = _blocked_hosts()

    def guarded(req, *args, **kwargs):
        url = str(getattr(req, "full_url", req))
        host = urllib.parse.urlsplit(url).hostname or ""
        private = host in blocked
        try:
            addr = ipaddress.ip_address(host)
            private = private or addr.is_private or addr.is_loopback or addr.is_link_local
        except ValueError:
            pass
        if private:
            raise AssertionError(f"test reached live LAN service: {url}")
        return real(req, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", guarded)


@pytest.fixture(autouse=True)
def _scratch_root_is_not_an_ancestor(tmp_path_factory, monkeypatch):
    """print_now stops its JOB.json search at $TMPDIR. Keep pytest's per test dirs out from under it."""
    monkeypatch.setenv("TMPDIR", str(tmp_path_factory.mktemp("scratch-root")))


@pytest.fixture(autouse=True)
def _no_leftover_background_slice():
    """A background slice is per intake. No test inherits one from the test before it."""
    from print_handy.core.background_slice import _RUNNERS
    _RUNNERS.clear()
    yield
    _RUNNERS.clear()


@pytest.fixture(autouse=True)
def _scratch_state(tmp_path, monkeypatch):
    """Job archives, claims and context sidecars default to a temp dir, never the home profile."""
    from print_handy.core import model_job
    root = tmp_path / "pipeline-state"
    root.mkdir()
    monkeypatch.setenv("PRINT_HANDY_LAUNCH_DB", str(root / "launch-state.db"))
    monkeypatch.setattr(model_job, "ARCHIVE", root / "archive")
    monkeypatch.setattr(model_job, "START_CLAIMS", root / "claims.json")
    monkeypatch.setattr(model_job, "CONTEXT", root / "context.json")
    monkeypatch.setattr(model_job, "BOUNDS_CACHE", root / "bounds-cache")


@pytest.fixture(scope="package", autouse=True)
def _clarify_gateway_stand_in():
    """Hermes' clarify gateway is not installed in CI. Install an in-memory stand in with the same
    calls the intake code makes (register, wait_for_response, resolve_gateway_clarify, is_open, CANCELLED).
    Package scope so it leaves with tests/pipeline (see tests/hermes/conftest.py)."""
    try:
        import tools.clarify_gateway  # noqa: F401
        yield
        return
    except ImportError:
        pass
    lock = threading.Lock()
    prompts = {}

    class Entry:
        def __init__(self):
            self.event = threading.Event()
            self.answer = None

    def entry(cid):
        with lock:
            return prompts.setdefault(cid, Entry())

    def register(cid, session_key, question, choices):
        entry(cid)

    def resolve_gateway_clarify(cid, answer):
        e = entry(cid)
        e.answer = answer
        e.event.set()
        return True

    def wait_for_response(cid, timeout=None):
        e = entry(cid)
        got = e.event.wait(timeout)
        with lock:
            prompts.pop(cid, None)
        return e.answer if got else None

    def is_open(cid):
        with lock:
            e = prompts.get(cid)
            return e is not None and not e.event.is_set()

    module = types.ModuleType("tools.clarify_gateway")
    module.CANCELLED = "__cancelled__"
    module.register = register
    module.resolve_gateway_clarify = resolve_gateway_clarify
    module.wait_for_response = wait_for_response
    module.is_open = is_open
    package = sys.modules.get("tools") or types.ModuleType("tools")
    package.clarify_gateway = module
    sys.modules["tools"] = package
    sys.modules["tools.clarify_gateway"] = module
    yield
    sys.modules.pop("tools.clarify_gateway", None)
    sys.modules.pop("tools", None)


def pytest_configure(config):
    config.addinivalue_line("markers", "real_slicer: needs the real Orca slicer; runs only with PRINT_HANDY_REAL_SLICER=1")


def pytest_collection_modifyitems(config, items):
    import os
    if os.environ.get(REAL_SLICER) == "1":
        return
    skip = pytest.mark.skip(reason=f"needs the real Orca slicer: set {REAL_SLICER}=1")
    for item in items:
        if "real_slicer" in item.keywords:
            item.add_marker(skip)
