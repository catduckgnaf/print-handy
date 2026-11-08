"""#387 contract: Spoolman HTTP lives in exactly one module.

print_handy.core.spoolman_client is the only place that may open an HTTP
connection to Spoolman. This test fails CI if a future change reintroduces
a direct Spoolman call anywhere else:

- Rule 1: outside the wrapper, no function may combine a Spoolman API path
  literal (/api/v1/spool|filament|vendor|health) with a direct transport
  call (urlopen, urllib.request.Request, or another service's generic
  client: _get, request_json, _http, get_json). Naming a Spoolman path is
  fine — and necessary — where it is passed to an injected client callable
  whose default transport IS the wrapper (the /spool command layer in
  hermes/spool_sync.py, the runout answer flow, spool_lifecycle's injected
  clients): what is banned is pairing the path with a transport of its own.
- Rule 2: outside the wrapper, no function may combine Spoolman base-URL
  resolution (spoolman_url(...), SPOOLMAN_BASE, service_url("spoolman_url"))
  with a raw urlopen/Request.

Moonraker's /server/spoolman/spool_id endpoint is a Moonraker call, not a
Spoolman call, and is not restricted here.
"""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "print_handy"
WRAPPER = ROOT / "core" / "spoolman_client.py"

SPOOLMAN_PATH = re.compile(r"/api/v1/(?:spool|filament|vendor|health)\b")
BASE_RESOLUTION = re.compile(r"spoolman_url\(|SPOOLMAN_BASE|service_url\(\s*['\"]spoolman_url")
DIRECT_TRANSPORTS = {"urlopen", "Request", "_get", "request_json", "_http", "get_json"}
RAW_TRANSPORTS = {"urlopen", "Request"}


def _called_names(node):
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _functions(path):
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    # ast.get_source_segment() re-splits the whole source on every call, so
    # calling it per statement makes scanning a large module (model_job.py,
    # moonraker_event_listener.py) quadratic in file size and the two
    # whole-tree contract tests below blew the 120 s pytest timeout in CI.
    # Compute each statement's segment from precomputed line-start byte
    # offsets instead: one pass over the source, then O(segment) per slice.
    # ast col_offsets are UTF-8 byte offsets, so slice the encoded source.
    encoded = source.encode("utf-8")
    line_starts = [0]
    for index, byte in enumerate(encoded):
        if byte == 0x0A:  # "\n"
            line_starts.append(index + 1)

    def segment_of(stmt):
        if stmt.end_lineno is None or stmt.end_col_offset is None:
            return ""
        start = line_starts[stmt.lineno - 1] + stmt.col_offset
        end = line_starts[stmt.end_lineno - 1] + stmt.end_col_offset
        return encoded[start:end].decode("utf-8", errors="ignore")

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            segment = "\n".join(segment_of(stmt) for stmt in node.body)
            yield node.name, segment, node


def test_wrapper_exists_and_is_the_transport():
    source = WRAPPER.read_text()
    assert "urlopen" in source  # the one place raw Spoolman HTTP happens
    for name in ("request", "call", "get_spool", "list_spools", "health",
                 "SpoolmanUnavailable", "UNAVAILABLE_NOTICE"):
        assert name in source, f"spoolman_client is missing {name}"


def test_no_spoolman_path_with_direct_transport_outside_wrapper():
    violations = []
    for path in sorted(ROOT.rglob("*.py")):
        if path == WRAPPER:
            continue
        for name, segment, node in _functions(path):
            if SPOOLMAN_PATH.search(segment):
                bad = DIRECT_TRANSPORTS & _called_names(node)
                if bad:
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} {name}() pairs a Spoolman "
                        f"API path with direct transport {sorted(bad)}; route it through "
                        "print_handy.core.spoolman_client (#387)")
    assert not violations, "\n".join(violations)


def test_no_spoolman_base_resolution_with_raw_transport_outside_wrapper():
    violations = []
    for path in sorted(ROOT.rglob("*.py")):
        if path == WRAPPER:
            continue
        for name, segment, node in _functions(path):
            if BASE_RESOLUTION.search(segment):
                bad = RAW_TRANSPORTS & _called_names(node)
                if bad:
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} {name}() resolves a Spoolman "
                        f"base URL and opens raw HTTP {sorted(bad)}; route it through "
                        "print_handy.core.spoolman_client (#387)")
    assert not violations, "\n".join(violations)


# #798: the /spool command layer (hermes/spool_sync.py) is covered whole-module, not only by the
# per-function rules above. Its one remaining private transport, _http, is Moonraker's.
SPOOL_SYNC = ROOT / "hermes" / "spool_sync.py"
MOONRAKER_BASES = {"MOONRAKER", "base"}  # the module setting and moonraker_for's per-printer base


def test_spool_sync_private_transport_is_only_ever_given_a_moonraker_base():
    tree = ast.parse(SPOOL_SYNC.read_text(), filename=str(SPOOL_SYNC))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_http"]
    assert calls, "spool_sync no longer calls _http; update this contract if the transport moved"
    bad = [f"spool_sync.py:{node.lineno} _http({ast.unparse(node.args[0])}, ...)"
           for node in calls if ast.unparse(node.args[0]) not in MOONRAKER_BASES]
    assert not bad, "\n".join(bad) + "\nSpoolman must go through print_handy.core.spoolman_client (#387)"


def test_spool_sync_does_not_open_its_own_connections_to_spoolman():
    tree = ast.parse(SPOOL_SYNC.read_text(), filename=str(SPOOL_SYNC))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            text = ast.unparse(node)
            if "SPOOLMAN" in text and any(name in text for name in ("urlopen", "Request(")):
                pytest.fail(f"spool_sync.py:{node.lineno} opens HTTP with the Spoolman base: {text}")


def test_spool_sync_spoolman_transport_is_the_client_with_the_same_contract(monkeypatch):
    import urllib.request

    from print_handy.core import spoolman_client
    from print_handy.hermes import spool_sync

    def no_direct_http(*args, **kwargs):
        pytest.fail("spool_sync.spoolman opened its own connection")

    monkeypatch.setattr(urllib.request, "urlopen", no_direct_http)
    seen = []

    def fake_request(method, path, body=None, *, base_url=None, timeout=None):
        seen.append((method, path, body, base_url, timeout))
        return {"id": 1}

    monkeypatch.setattr(spoolman_client, "request", fake_request)
    monkeypatch.setattr(spool_sync, "SPOOLMAN", "http://spoolman.test")
    assert spool_sync.spoolman("PATCH", "/api/v1/spool/1", {"location": "Shelf"}) == {"id": 1}
    # The base is the module's setting and the timeout is the long-standing 8 s, not the client's 5 s.
    assert seen == [("PATCH", "/api/v1/spool/1", {"location": "Shelf"}, "http://spoolman.test", 8)]

    def down(*args, **kwargs):
        raise spoolman_client.SpoolmanUnavailable("connection refused")

    monkeypatch.setattr(spoolman_client, "request", down)
    with pytest.raises(spool_sync.SpoolError, match="connection refused") as raised:
        spool_sync.spoolman("GET", "/api/v1/spool/1")
    assert isinstance(raised.value.__cause__, spoolman_client.SpoolmanUnavailable)
